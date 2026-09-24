"""Agent-process improvements (post-audit tier): pitfall ledger, real code
context for dev/reviewer prompts, and QA refusing to fabricate a verdict."""
import asyncio

import pytest

from app import db as appdb
from app import memory
from app.codegen import _changed_files_block, _task_relevant_files

TS = "2026-09-23T00:00:00+00:00"


def _seed():
    appdb.insert("roles", {"id": "r-dev", "name": "Senior Developer",
                           "created_at": TS, "updated_at": TS})
    appdb.insert("personas", {"id": "p-dev", "role_id": "r-dev", "name": "dev",
                              "created_at": TS, "updated_at": TS})
    appdb.insert("teams", {"id": "t1", "name": "Team 1", "created_at": TS})
    appdb.insert("agents", {"id": "a-dev", "name": "DEV", "role_id": "r-dev",
                            "persona_id": "p-dev", "created_at": TS, "updated_at": TS})
    appdb.insert("agents", {"id": "a-qa", "name": "QA", "role_id": "r-dev",
                            "persona_id": "p-dev", "created_at": TS, "updated_at": TS})
    appdb.insert("projects", {"id": "pr1", "name": "P", "goal": "", "description": "",
                              "workspace_path": "C:/nonexistent-ws", "team_id": "t1",
                              "po_enabled": 0, "status": "Active",
                              "created_at": TS, "updated_at": TS})
    appdb.insert("tasks", {"id": "tk1", "project_id": "pr1", "title": "Weather page",
                           "description": "", "acceptance_criteria": "",
                           "story_points": 1, "priority": 2, "status": "Testing",
                           "evidence": "code:app/x.py", "assigned_agent_id": "a-dev",
                           "qa_agent_id": "a-dev", "rework_count": 1,
                           "created_at": TS, "updated_at": TS})
    appdb.insert("workflow_runs", {"id": "wr1", "project_id": "pr1", "task_id": "tk1",
                                   "agent_id": "a-qa", "status": "Running",
                                   "started_at": TS})


# ---------------- pitfall ledger ----------------

def test_pitfall_recorded_deduped_and_surfaced():
    _seed()
    mid = memory.record_pitfall("pr1", "tk1", "qa", "page crashes on empty city")
    assert mid
    again = memory.record_pitfall("pr1", "tk1", "qa", "page crashes on empty city")
    assert again == mid
    rows = appdb.query("SELECT * FROM project_memory")
    assert len(rows) == 1
    assert rows[0]["importance"] == 3  # 2 + reinforcement
    block = memory.pitfalls_block("pr1")
    assert "page crashes on empty city" in block
    assert memory.pitfalls_block("other-project") == ""
    # Empty content is never recorded.
    assert memory.record_pitfall("pr1", "tk2", "qa", "   ") is None


def test_po_brief_surfaces_pitfalls():
    from app import po
    _seed()
    memory.record_pitfall("pr1", "tk1", "qa", "form submit returns 500",
                          subject="Weather page")
    brief = po._project_brief("pr1")
    assert "KNOWN PITFALLS" in brief
    assert "form submit returns 500" in brief
    # With no ledger entries the section stays absent.
    appdb.execute("DELETE FROM project_memory")
    assert "KNOWN PITFALLS" not in po._project_brief("pr1")


# ---------------- code context for prompts ----------------

def test_changed_files_block_reads_evidence(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "x.py").write_text("def x():\n    return 42\n", encoding="utf-8")
    (tmp_path / "app" / "gone.py").write_text("x=1", encoding="utf-8")
    evidence = "code:app/x.py; code:app/gone.py; tests-pass:ok"
    # gone.py no longer exists on disk (later task overwrote the layout).
    (tmp_path / "app" / "gone.py").unlink()
    block = _changed_files_block(str(tmp_path), evidence)
    assert "def x():" in block
    assert "gone.py" not in block
    assert _changed_files_block(str(tmp_path), "") == ""


def test_task_relevant_files_picks_by_terminology(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "weather_router.py").write_text("WEATHER = True", encoding="utf-8")
    (tmp_path / "app" / "billing.py").write_text("INVOICE = True", encoding="utf-8")
    task = {"title": "Add forecast to weather page", "description": "weather api caching"}
    block = _task_relevant_files(str(tmp_path), task, set())
    assert "WEATHER = True" in block
    assert "INVOICE" not in block


# ---------------- QA unverified path ----------------

def test_qa_without_evidence_blocks_instead_of_coinflip():
    from app import runtime
    _seed()

    async def _run():
        task = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
        await runtime._finish_qa_run("wr1", {}, "pr1", "tk1", "a-qa", task,
                                     0, 0, 0.0, test_summary="")
    asyncio.run(_run())
    t = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert t["status"] == "Blocked"
    assert "could not verify automatically" in t["blocked_reason"]
    assert "qa:unverified" in t["evidence"]
    # Nothing failed measurably -> no extra rework cycle charged.
    assert t["rework_count"] == 1
    run = appdb.query_one("SELECT * FROM workflow_runs WHERE id='wr1'")
    assert run["status"] == "Completed" and run["current_step"] == "qa-unverified"
    evs = appdb.query("SELECT event_type FROM execution_events WHERE project_id='pr1'")
    assert any(e["event_type"] == "qa.unverified" for e in evs)


def test_qa_real_failure_still_rejects_with_pitfall():
    from app import runtime
    _seed()
    # Fresh budget (rework 1 of MAX_REWORK_CYCLES=2) so this rejects to
    # Rework rather than escalating straight to Blocked.
    appdb.execute("UPDATE tasks SET rework_count=0 WHERE id='tk1'")

    async def _run():
        task = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
        await runtime._finish_qa_run("wr1", {}, "pr1", "tk1", "a-qa", task,
                                     0, 0, 0.0,
                                     test_summary="tests FAILED: 1 error in test_x.py")
    asyncio.run(_run())
    t = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert t["status"] == "Rework"
    assert t["rework_count"] == 1
    # The rejection reason is now in the ledger for the next attempt.
    rows = appdb.query("SELECT * FROM project_memory WHERE memory_type='pitfall'")
    assert len(rows) == 1 and "test_x.py" in rows[0]["content"]
