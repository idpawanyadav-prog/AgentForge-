"""Execution-lifecycle fixes (audit item 6): orphan recovery resumes in the
right phase, cancelling a stale run un-strands its task, task claiming is
atomic, and a failed merge-back can never end as Done / a gate handoff."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from app import db as appdb
from app import runtime

TS = "2026-09-23T00:00:00+00:00"


def _seed(task_status="Ready", run=None, agent_state="Idle"):
    appdb.insert("roles", {"id": "r-dev", "name": "Senior Developer",
                           "created_at": TS, "updated_at": TS})
    appdb.insert("personas", {"id": "p-dev", "role_id": "r-dev", "name": "dev",
                              "created_at": TS, "updated_at": TS})
    appdb.insert("teams", {"id": "t1", "name": "Team 1", "created_at": TS})
    appdb.insert("agents", {"id": "a-dev", "name": "DEV", "role_id": "r-dev",
                            "persona_id": "p-dev", "lifecycle_state": agent_state,
                            "created_at": TS, "updated_at": TS})
    appdb.insert("projects", {"id": "pr1", "name": "P", "goal": "", "description": "",
                              "workspace_path": "C:/nonexistent-ws", "team_id": "t1",
                              "po_enabled": 0, "status": "Active",
                              "created_at": TS, "updated_at": TS})
    appdb.insert("sprints", {"id": "s1", "project_id": "pr1", "name": "S1",
                             "status": "Active", "created_at": TS})
    appdb.insert("tasks", {"id": "tk1", "project_id": "pr1", "sprint_id": "s1",
                           "title": "Weather page", "description": "",
                           "acceptance_criteria": "", "story_points": 1, "priority": 2,
                           "status": task_status, "evidence": "",
                           "assigned_agent_id": "a-dev", "rework_count": 0,
                           "created_at": TS, "updated_at": TS})
    if run:
        appdb.insert("workflow_runs", dict(
            {"id": "wr1", "project_id": "pr1", "task_id": "tk1", "agent_id": "a-dev",
             "status": "Running", "started_at": TS}, **run))


# ---------------- #13: orphan recovery routing ----------------

def test_recovered_status_maps_each_phase():
    assert runtime._recovered_status("In Progress") == "Ready"
    assert runtime._recovered_status("Review") == "Ready"
    assert runtime._recovered_status("Testing") == "Waiting QA"
    # Already phase-routed or human-parked statuses are left alone.
    for s in ("Waiting QA", "SA Review", "BA Review", "Rework", "Blocked", "Ready"):
        assert runtime._recovered_status(s) == s


def test_recover_orphans_resumes_in_place_not_from_scratch():
    _seed(task_status="Testing", run={"current_step": "Run tests"})
    appdb.execute("UPDATE tasks SET blocked_reason='' WHERE id='tk1'")
    runtime.recover_orphans()
    t = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert t["status"] == "Waiting QA"  # not Ready -> dev phase is not redone
    run = appdb.query_one("SELECT * FROM workflow_runs WHERE id='wr1'")
    assert run["status"] == "Failed"
    assert "restart" in run["current_step"]
    # QA agent waiting on this task is freed for the next claim.
    a = appdb.query_one("SELECT lifecycle_state FROM agents WHERE id='a-dev'")
    assert a["lifecycle_state"] == "Idle"


def test_recover_orphans_dev_phase_resumes_as_ready():
    _seed(task_status="In Progress", run={"mode": "dev"})
    appdb.execute("UPDATE tasks SET blocked_reason='' WHERE id='tk1'")
    runtime.recover_orphans()
    t = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert t["status"] == "Ready"


def test_recover_orphans_keeps_review_gate_positions():
    _seed(task_status="SA Review", run={"mode": "sa"})
    runtime.recover_orphans()
    t = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert t["status"] == "SA Review"


# ---------------- #14: cancelling a stale run un-strands the task ----------

def test_cancel_orphan_run_unstrands_task_and_agent():
    _seed(task_status="In Progress", run={"mode": "dev"}, agent_state="Working")
    assert "wr1" not in runtime._registry
    res = runtime.cancel_execution("wr1")
    assert res["ok"]
    run = appdb.query_one("SELECT * FROM workflow_runs WHERE id='wr1'")
    assert run["status"] == "Cancelled"
    t = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert t["status"] == "Blocked"
    assert "Cancelled by operator" in t["blocked_reason"]
    a = appdb.query_one("SELECT lifecycle_state FROM agents WHERE id='a-dev'")
    assert a["lifecycle_state"] == "Idle"
    evs = appdb.query("SELECT event_type FROM execution_events WHERE project_id='pr1'")
    assert any(e["event_type"] == "workflow.cancelled" for e in evs)
    assert any(e["event_type"] == "task.status_changed" for e in evs)


# ---------------- #15: atomic claim ----------------

def test_idempotent_execution_replays_after_task_status_changes():
    _seed(task_status="In Progress", run={"idempotency_key": "request-1"})
    replay = runtime.start_execution("pr1", "tk1", "request-1")
    assert replay["idempotent_replay"] is True
    assert replay["run"]["id"] == "wr1"
    wrong_scope = runtime.start_execution("another-project", "tk1", "request-1")
    assert "error" in wrong_scope
    assert len(appdb.query("SELECT id FROM workflow_runs")) == 1


def test_concurrent_start_execution_claims_exactly_one_run(monkeypatch):
    _seed()
    spawned = []

    def fake_spawn(coro):
        coro.close()
        handle = SimpleNamespace(cancel=lambda: None)
        spawned.append(handle)
        return handle

    monkeypatch.setattr(runtime, "_spawn", fake_spawn)
    try:
        results = []
        barrier = threading.Barrier(2)

        def call():
            barrier.wait()
            results.append(runtime.start_execution("pr1", "tk1"))

        threads = [threading.Thread(target=call) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=15)
        assert len(results) == 2
        ok = [r for r in results if "run" in r]
        err = [r for r in results if "error" in r]
        assert len(ok) == 1 and len(err) == 1
        assert len(appdb.query("SELECT id FROM workflow_runs")) == 1
    finally:
        with runtime._registry_lock:
            for rid in [k for k, v in runtime._registry.items() if v.get("task") in spawned]:
                del runtime._registry[rid]


# ---------------- #16: Done only after a successful merge ----------------

def test_merge_failure_blocks_instead_of_done_or_handoff():
    _seed(task_status="In Progress", run={"mode": "dev"})
    appdb.execute("UPDATE projects SET sa_review_enabled=1, ba_review_enabled=1 WHERE id='pr1'")
    task = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")

    async def _run():
        await runtime._finish_dev_run("wr1", {}, "pr1", "tk1", "a-dev", task,
                                      ["scaffold:ok", "merge-error:lease denied"],
                                      0, 0, 0.0, "", "lease denied")
    asyncio.run(_run())

    t = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert t["status"] == "Blocked" and t["progress"] == 90
    assert "could not merge back" in t["blocked_reason"]
    assert "lease denied" in t["blocked_reason"]
    # Gates enabled, yet neither gate nor QA nor Done was reached.
    assert t["status"] not in ("SA Review", "Waiting QA", "Done")
    assert "merge-error:lease denied" in t["evidence"]
    run = appdb.query_one("SELECT * FROM workflow_runs WHERE id='wr1'")
    assert run["status"] == "Completed" and run["current_step"] == "merge-failed"
    evs = appdb.query("SELECT event_type, payload FROM execution_events WHERE project_id='pr1' "
                      "ORDER BY created_at")
    assert any(e["event_type"] == "workflow.completed" and "merge-failed" in e["payload"]
               for e in evs)


def test_clean_merge_still_routes_through_gates(monkeypatch):
    _seed(task_status="In Progress", run={"mode": "dev"})
    appdb.execute("UPDATE projects SET sa_review_enabled=1, ba_review_enabled=0 WHERE id='pr1'")
    # _request_review returns a truthy run id -> handed off to the SA gate.
    monkeypatch.setattr(runtime, "_request_review",
                        lambda pid, task, stage, aid, ev: "wr-sa")
    task = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")

    async def _run():
        await runtime._finish_dev_run("wr1", {}, "pr1", "tk1", "a-dev", task,
                                      ["merged:3"], 0, 0, 0.0, "", "")
    asyncio.run(_run())
    run = appdb.query_one("SELECT * FROM workflow_runs WHERE id='wr1'")
    assert run["status"] == "Completed" and run["current_step"] == "sa-review-handoff"
    t = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert t["status"] == "In Progress"  # the gate mover owns the next transition
