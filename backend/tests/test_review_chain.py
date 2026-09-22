"""V3 SA/BA review-gate chain tests.

Runs against a throwaway SQLite DB (AGENT_OFFICE_DB env var) — never the
live database.

Run: "%USERPROFILE%\\.verdent\\agentforge-venv\\Scripts\\python.exe" -m pytest backend/tests/test_review_chain.py -q
"""
import asyncio
import os
import sys
import tempfile

_DB = os.path.join(tempfile.mkdtemp(prefix="af_chain_"), "test.db")
os.environ["AGENT_OFFICE_DB"] = _DB
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from app import db as appdb  # noqa: E402
from app import runtime  # noqa: E402
from app.services import tasks as tasksvc  # noqa: E402

appdb.init_db()

PID = "proj-chain-1"
TID = "team-chain-1"
ROLES = {
    "Senior Developer": "dev",
    "QA Engineer": "qa",
    "Solution Architect": "architecture",
    "Business Analyst": "requirements",
}
AGENTS = {
    "dev1": "Senior Developer",
    "qa1": "QA Engineer",
    "sa1": "Solution Architect",
    "ba1": "Business Analyst",
}


@pytest.fixture()
def fx(monkeypatch):
    for tbl in ("task_reviews", "usage_records", "task_dependencies", "workflow_runs",
                "messages", "conversations", "tasks", "sprint_acceptance_criteria",
                "sprint_gate_executions", "sprints", "backlog_items",
                "team_agents", "teams", "agents", "personas", "roles", "projects"):
        appdb.execute(f"DELETE FROM {tbl}")
    appdb.execute("DELETE FROM execution_events")
    ts = appdb.now()
    for role in ROLES:
        appdb.insert("roles", {"id": "role-" + role, "name": role, "created_at": ts,
                               "updated_at": ts})
        appdb.insert("personas", {"id": "per-" + role, "role_id": "role-" + role,
                                  "name": role + " persona", "created_at": ts,
                                  "updated_at": ts})
    appdb.insert("teams", {"id": TID, "name": "Chain team", "created_at": ts})
    for aid, role in AGENTS.items():
        appdb.insert("agents", {"id": aid, "name": aid.upper(), "role_id": "role-" + role,
                                "persona_id": "per-" + role, "created_at": ts,
                                "updated_at": ts})
        appdb.execute("INSERT INTO team_agents (team_id, agent_id) VALUES (?,?)", (TID, aid))
    appdb.insert("projects", {
        "id": PID, "name": "Chain Test", "goal": "review chain",
        "description": "", "workspace_path": "C:/nonexistent-ws", "team_id": TID,
        "po_enabled": 0, "sa_review_enabled": 0, "ba_review_enabled": 0,
        "created_at": ts, "updated_at": ts})
    appdb.insert("sprints", {"id": "spr-c", "project_id": PID, "name": "S1", "goal": "g",
                             "capacity": 10, "status": "Active", "created_at": ts})

    async def _fast_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(runtime.asyncio, "sleep", _fast_sleep)
    return {"ts": ts}


def _mk_task(status="Testing", assigned="dev1", **over):
    row = {"id": "tk1", "project_id": PID, "sprint_id": "spr-c", "title": "Build X",
           "description": "", "story_points": 3, "priority": 2,
           "assigned_agent_id": assigned, "qa_agent_id": None, "status": status,
           "rework_count": 0, "progress": 75, "evidence": "", "blocked_reason": "",
           "created_at": appdb.now(), "updated_at": appdb.now()}
    row.update(over)
    appdb.insert("tasks", row)
    return appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")


def _mk_run(agent_id):
    appdb.insert("workflow_runs", {"id": "run1", "project_id": PID, "task_id": "tk1",
                                   "agent_id": agent_id, "status": "Running", "mode": "dev",
                                   "current_step": "review", "started_at": appdb.now()})
    return {"paused": False, "cancelled": False}


def test_run_mode_mapping(fx):
    assert runtime._run_mode({"status": "Waiting QA"}) == "qa"
    assert runtime._run_mode({"status": "SA Review"}) == "sa"
    assert runtime._run_mode({"status": "BA Review"}) == "ba"
    assert runtime._run_mode({"status": "Ready"}) == "dev"


def test_transition_table_covers_new_stages(fx):
    assert "SA Review" in tasksvc.TASK_TRANSITIONS["Testing"]
    assert "BA Review" in tasksvc.TASK_TRANSITIONS["SA Review"]
    assert "Waiting QA" in tasksvc.TASK_TRANSITIONS["BA Review"]
    assert "Rework" in tasksvc.TASK_TRANSITIONS["SA Review"]


def test_request_review_assigns_architect_and_records_pending(fx):
    task = _mk_task()
    reviewer = runtime._request_review(PID, task, "SA Review", "dev1", "ev")
    assert reviewer["id"] == "sa1"
    row = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert row["status"] == "SA Review"
    assert row["assigned_agent_id"] == "sa1"
    assert row["qa_agent_id"] == "dev1"  # author kept for rework routing
    rev = appdb.query_one("SELECT * FROM task_reviews WHERE task_id='tk1'")
    assert rev["reviewer_type"] == "solution_architect"
    assert rev["status"] == "pending"


def test_request_review_skipped_without_specialist(fx):
    appdb.execute("DELETE FROM team_agents WHERE agent_id IN ('sa1','ba1')")
    appdb.execute("DELETE FROM team_agents WHERE agent_id='qa1'")  # only the dev remains
    task = _mk_task()
    assert runtime._request_review(PID, task, "SA Review", "dev1", "ev") is None
    row = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert row["status"] == "Testing"
    assert not appdb.query("SELECT * FROM task_reviews")


def test_dev_done_routes_to_sa_review(fx):
    appdb.update("projects", PID, {"sa_review_enabled": 1})
    task = _mk_task()
    ctrl = _mk_run("dev1")
    asyncio.run(runtime._finish_dev_run("run1", ctrl, PID, "tk1", "dev1", task,
                                        ["build-pass"], 0, 0, 0.0))
    row = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert row["status"] == "SA Review"
    assert row["assigned_agent_id"] == "sa1"
    run = appdb.query_one("SELECT * FROM workflow_runs WHERE id='run1'")
    assert run["status"] == "Completed"
    assert run["current_step"] == "sa-review-handoff"


def test_dev_done_without_gates_goes_to_qa(fx):
    task = _mk_task()
    ctrl = _mk_run("dev1")
    asyncio.run(runtime._finish_dev_run("run1", ctrl, PID, "tk1", "dev1", task,
                                        ["build-pass"], 0, 0, 0.0))
    row = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert row["status"] == "Waiting QA"
    assert row["assigned_agent_id"] == "qa1"


def _seed_review(task_status, ctrl_review):
    task = _mk_task(status=task_status, assigned="sa1" if task_status == "SA Review" else "ba1",
                    qa_agent_id="dev1")
    ctrl = _mk_run(task["assigned_agent_id"])
    if ctrl_review:
        ctrl["review"] = ctrl_review
    return task, ctrl


def test_sa_rework_returns_to_developer(fx):
    task, ctrl = _seed_review("SA Review", {
        "decision": "rework", "findings": "UI layer imports Data layer",
        "rework_class": "ARCHITECTURE_VIOLATION", "input_tokens": 10, "output_tokens": 5})
    asyncio.run(runtime._finish_review_run("run1", ctrl, PID, "tk1", "sa1", task, "sa",
                                           10, 5, 0.1))
    row = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert row["status"] == "Rework"
    assert row["assigned_agent_id"] == "dev1"
    assert row["rework_count"] == 1
    assert row["rework_class"] == "ARCHITECTURE_VIOLATION"
    rev = appdb.query_one("SELECT * FROM task_reviews WHERE task_id='tk1'")
    assert rev["status"] == "rework"
    assert "UI layer" in rev["findings"]


def test_sa_approved_advances_to_ba_then_qa(fx):
    appdb.update("projects", PID, {"sa_review_enabled": 1, "ba_review_enabled": 1})
    approved = {"decision": "approved", "findings": "", "rework_class": "",
                "input_tokens": 0, "output_tokens": 0}
    task, ctrl = _seed_review("SA Review", approved)
    asyncio.run(runtime._finish_review_run("run1", ctrl, PID, "tk1", "sa1", task, "sa",
                                           0, 0, 0.0))
    row = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert row["status"] == "BA Review"
    assert row["assigned_agent_id"] == "ba1"
    assert row["qa_agent_id"] == "dev1"  # author preserved through the chain

    appdb.insert("workflow_runs", {"id": "run2", "project_id": PID, "task_id": "tk1",
                                   "agent_id": "ba1", "status": "Running", "mode": "ba",
                                   "current_step": "ba-review", "started_at": appdb.now()})
    asyncio.run(runtime._finish_review_run("run2", {"review": approved}, PID, "tk1", "ba1",
                                           row, "ba", 0, 0, 0.0))
    final = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert final["status"] == "Waiting QA"
    assert final["assigned_agent_id"] == "qa1"


def test_unavailable_verdict_skips_gate_to_qa(fx):
    # No gateway configured: the gate must not punish the developer.
    task = _mk_task(status="SA Review", assigned="sa1", qa_agent_id="dev1")
    asyncio.run(runtime._finish_review_run("run1", {}, PID, "tk1", "sa1", task, "sa",
                                           0, 0, 0.0))
    row = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert row["status"] == "Waiting QA"
    rev = appdb.query_one("SELECT * FROM task_reviews WHERE task_id='tk1'")
    assert rev["status"] == "skipped"


def test_review_rework_escalates_after_max_cycles(fx):
    task = _mk_task(status="SA Review", assigned="sa1", qa_agent_id="dev1",
                    rework_count=runtime.MAX_REWORK_CYCLES - 1)
    ctrl = {"review": {"decision": "rework", "findings": "still wrong",
                       "rework_class": "CONTRACT_MISMATCH",
                       "input_tokens": 0, "output_tokens": 0}}
    _mk_run("sa1")
    asyncio.run(runtime._finish_review_run("run1", ctrl, PID, "tk1", "sa1", task, "sa",
                                           0, 0, 0.0))
    row = appdb.query_one("SELECT * FROM tasks WHERE id='tk1'")
    assert row["status"] == "Blocked"
    assert "needs human review" in row["blocked_reason"]
