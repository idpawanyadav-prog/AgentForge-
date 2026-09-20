"""Sprint gating tests (spec §39).

Runs against a throwaway SQLite DB (AGENT_OFFICE_DB env var) — never the
live database. Toolchain/LLM calls are monkeypatched so gates are tested
in isolation.

Run: "%USERPROFILE%\\.verdent\\agentforge-venv\\Scripts\\python.exe" -m pytest backend/tests/test_sprint_gating.py -q
"""
import os
import sys
import tempfile

_DB = os.path.join(tempfile.mkdtemp(prefix="af_gate_"), "test.db")
os.environ["AGENT_OFFICE_DB"] = _DB
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from app import db as appdb  # noqa: E402
from app import sprint_gate  # noqa: E402

appdb.init_db()

PID = "proj-test-1"


@pytest.fixture()
def fx(monkeypatch):
    """Fresh project + two sprints + tasks per test. Deletes in FK-safe
    order (init_db seeds demo data referencing tasks/projects)."""
    for tbl in ("usage_records", "task_dependencies", "workflow_runs",
                "messages", "conversations", "tasks",
                "sprint_acceptance_criteria", "sprint_gate_executions",
                "sprints", "backlog_items", "projects"):
        appdb.execute(f"DELETE FROM {tbl}")
    appdb.execute("DELETE FROM execution_events")
    ts = appdb.now()
    appdb.insert("projects", {
        "id": PID, "name": "Gate Test", "goal": "Test sprint gating",
        "description": "", "workspace_path": "C:/nonexistent-ws",
        "default_gateway_id": None, "po_enabled": 0,
        "created_at": ts, "updated_at": ts})
    s1 = appdb.insert("sprints", {
        "id": "spr-1", "project_id": PID, "name": "Sprint 1", "goal": "g1",
        "capacity": 20, "status": "Active", "created_at": ts})
    s2 = appdb.insert("sprints", {
        "id": "spr-2", "project_id": PID, "name": "Sprint 2", "goal": "g2",
        "capacity": 20, "status": "Planned", "created_at": ts})
    t1 = appdb.insert("tasks", {
        "id": "t1", "project_id": PID, "sprint_id": "spr-1", "title": "S1 work",
        "description": "", "story_points": 3, "priority": 2, "status": "Done",
        "created_at": ts, "updated_at": ts})
    t2 = appdb.insert("tasks", {
        "id": "t2", "project_id": PID, "sprint_id": "spr-2", "title": "S2 work",
        "description": "", "story_points": 3, "priority": 2, "status": "Todo",
        "created_at": ts, "updated_at": ts})
    return {"s1": s1, "s2": s2, "t1": t1, "t2": t2}


def _events(event_type):
    return appdb.query(
        "SELECT * FROM execution_events WHERE event_type = ?", (event_type,))


# ------------------------------------------------------------- transitions

def test_illegal_transitions_rejected(fx):
    ok, err = sprint_gate.transition_sprint("spr-1", "Completed", "cheat")
    assert not ok  # Active -> Completed is not a legal transition
    ok, _ = sprint_gate.transition_sprint("spr-1", "Development Complete", "tasks done")
    assert ok
    ok, _ = sprint_gate.transition_sprint("spr-1", "Active", "reopen")
    assert ok
    assert appdb.query_one("SELECT status FROM sprints WHERE id='spr-1'")["status"] == "Active"


def test_cancelled_is_terminal(fx):
    assert sprint_gate.transition_sprint("spr-1", "Cancelled", "scratch")[0]
    ok, err = sprint_gate.transition_sprint("spr-1", "Active", "revive")
    assert not ok


# ------------------------------------------------------------- gatekeeper

def test_future_sprint_task_rejected(fx):
    """Spec Test 1: Sprint 2 is LOCKED while Sprint 1 is active."""
    auth = sprint_gate.authorize_task(PID, "t2")
    assert not auth["allowed"]
    assert "locked sprint" in auth["reason"]


def test_active_sprint_task_allowed(fx):
    """Spec Test 2: active-sprint tasks are claimable."""
    appdb.execute("UPDATE tasks SET status='Todo' WHERE id='t1'")
    auth = sprint_gate.authorize_task(PID, "t1")
    assert auth["allowed"]


def test_no_active_sprint_denies(fx):
    appdb.execute("UPDATE sprints SET status='Planned'")
    auth = sprint_gate.authorize_task(PID, "t1")
    assert not auth["allowed"]
    assert "No active sprint" in auth["reason"]


def test_gate_phase_denies_dev_work(fx):
    appdb.execute("UPDATE tasks SET status='Todo' WHERE id='t1'")
    sprint_gate.transition_sprint("spr-1", "Development Complete", "")
    sprint_gate.transition_sprint("spr-1", "Build Validation", "")
    auth = sprint_gate.authorize_task(PID, "t1")
    assert not auth["allowed"]
    assert "not accepting development work" in auth["reason"]


# ------------------------------------------------------------ urgent scope

def test_urgent_reopen_mid_gates(fx):
    """Urgent task added while the sprint is mid-gates: back to Active,
    gates reset, dev work allowed again."""
    appdb.execute("UPDATE tasks SET status='Todo' WHERE id='t1'")
    for st in ("Development Complete", "Build Validation"):
        sprint_gate.transition_sprint("spr-1", st, "")
    appdb.execute("UPDATE sprints SET build_status='Running' WHERE id='spr-1'")
    ok, err = sprint_gate.reopen_sprint_for_scope("spr-1", "urgent task added")
    assert ok, err
    row = appdb.query_one("SELECT * FROM sprints WHERE id='spr-1'")
    assert row["status"] == "Active"
    assert row["build_status"] == "Pending"
    assert sprint_gate.authorize_task(PID, "t1")["allowed"]
    # next sprint still locked
    assert not sprint_gate.authorize_task(PID, "t2")["allowed"]


def test_completed_sprint_cannot_reopen(fx):
    for st in ("Development Complete", "Build Validation", "Automated Testing",
               "Functional Validation", "Sprint Acceptance", "Completed"):
        sprint_gate.transition_sprint("spr-1", st, "")
    ok, _ = sprint_gate.reopen_sprint_for_scope("spr-1", "late urgent task")
    assert not ok


# ------------------------------------------------------------ gate pipeline

def _mock_gates(monkeypatch, build_ok=True, tests_ok=True, func=None):
    monkeypatch.setattr(sprint_gate.toolchains, "detect_stack", lambda p: "python")
    monkeypatch.setattr(sprint_gate.toolchains, "build_check",
                        lambda *a, **k: (build_ok, "build ok" if build_ok else "build FAILED: boom"))
    monkeypatch.setattr(sprint_gate.toolchains, "run_stack_tests",
                        lambda *a, **k: (tests_ok, "tests passed: 10 passed" if tests_ok
                                         else "tests FAILED: 2 failed"))
    if func is None:
        func = (True, "3/3 acceptance criteria passed", [], False)
    monkeypatch.setattr(sprint_gate, "_validate_functional", lambda *a, **k: func)
    monkeypatch.setattr(sprint_gate, "seed_sprint_acs", lambda s: 0)


def test_all_gates_pass_completes_and_unlocks(fx, monkeypatch):
    """Spec Test 7: full pipeline -> COMPLETED + next sprint READY +
    SPRINT_UNLOCKED event."""
    _mock_gates(monkeypatch)
    outcome = sprint_gate.run_sprint_gates(PID, "spr-1", {})
    assert outcome == "Completed"
    assert appdb.query_one("SELECT status FROM sprints WHERE id='spr-1'")["status"] == "Completed"
    assert appdb.query_one("SELECT status FROM sprints WHERE id='spr-2'")["status"] == "Ready"
    assert _events("sprint.unlocked")
    history = appdb.query("SELECT * FROM sprint_gate_executions WHERE sprint_id='spr-1'")
    assert {h["gate_type"] for h in history} == {"BUILD", "AUTOMATED_TEST", "FUNCTIONAL", "ACCEPTANCE"}
    assert all(h["status"] == "Passed" for h in history)


def test_build_failure_blocks_completion(fx, monkeypatch):
    """Spec Test 3: build fails -> sprint not completed, next locked."""
    _mock_gates(monkeypatch, build_ok=False)
    outcome = sprint_gate.run_sprint_gates(PID, "spr-1", {})
    assert outcome == "Rework"
    row = appdb.query_one("SELECT status, build_status FROM sprints WHERE id='spr-1'")
    assert row["status"] == "Active"          # re-opened for rework
    assert row["build_status"] == "Failed"
    assert appdb.query_one("SELECT status FROM sprints WHERE id='spr-2'")["status"] == "Planned"
    assert appdb.query("SELECT * FROM tasks WHERE sprint_id='spr-1' "
                       "AND status NOT IN ('Done','Cancelled')")  # rework task created


def test_failure_blocks_and_rework_tasks_created(fx, monkeypatch):
    """Spec Test 4: tests fail -> rework tasks for failing modules."""
    monkeypatch.setattr(sprint_gate.toolchains, "detect_stack", lambda p: "python")
    monkeypatch.setattr(sprint_gate.toolchains, "build_check",
                        lambda *a, **k: (True, "build ok"))
    monkeypatch.setattr(sprint_gate.toolchains, "run_stack_tests",
                        lambda *a, **k: (False, "tests FAILED: 2 failed"))
    monkeypatch.setattr(sprint_gate.toolchains, "failing_tests",
                        lambda *a, **k: {"tests/test_x.py::test_a", "tests/test_y.py::test_b"})
    monkeypatch.setattr(sprint_gate.toolchains, "failing_test_details",
                        lambda *a, **k: "AssertionError: boom")
    outcome = sprint_gate.run_sprint_gates(PID, "spr-1", {})
    assert outcome == "Rework"
    rework = appdb.query("SELECT * FROM tasks WHERE title LIKE 'Sprint gate rework:%'")
    assert len(rework) == 2  # one per failing module
    assert all("tests/" in t["title"] for t in rework)


def test_functional_failure_blocks_completion(fx, monkeypatch):
    """Spec Test 5: functional validation fails -> rework, not completed."""
    _mock_gates(monkeypatch, func=(False, "1/3 acceptance criteria passed",
                                   [{"title": "Sprint gate rework: satisfy AC-02",
                                     "description": "fix it"}], False))
    outcome = sprint_gate.run_sprint_gates(PID, "spr-1", {})
    assert outcome == "Rework"
    row = appdb.query_one("SELECT status, functional_status FROM sprints WHERE id='spr-1'")
    assert row["status"] == "Active"
    assert row["functional_status"] == "Failed"
    assert appdb.query_one("SELECT status FROM sprints WHERE id='spr-2'")["status"] == "Planned"


def test_acceptance_failure_blocks_completion(fx, monkeypatch):
    """Spec Test 6: unmet required ACs -> rework, not completed."""
    _mock_gates(monkeypatch)
    # functional passes but leaves an AC unmet -> acceptance gate fails
    monkeypatch.setattr(sprint_gate, "seed_sprint_acs", lambda s: 0)
    monkeypatch.setattr(sprint_gate, "_validate_functional", lambda *a, **k: (True, "ok", [], False))
    appdb.insert("sprint_acceptance_criteria", {
        "id": "ac1", "sprint_id": "spr-1", "code": "AC-01", "description": "must work",
        "required": 1, "status": "Failed", "failure_reason": "nope",
        "created_at": appdb.now()})
    outcome = sprint_gate.run_sprint_gates(PID, "spr-1", {})
    assert outcome == "Rework"
    assert appdb.query_one("SELECT acceptance_status FROM sprints WHERE id='spr-1'")["status"] \
        if False else True
    assert appdb.query_one("SELECT acceptance_status FROM sprints "
                           "WHERE id='spr-1'")["acceptance_status"] == "Failed"


def test_retry_cap_marks_sprint_failed(fx, monkeypatch):
    """Spec §35: after MAX_GATE_CYCLES failed gate cycles -> Failed."""
    _mock_gates(monkeypatch, build_ok=False)
    for _ in range(sprint_gate.MAX_GATE_CYCLES):
        outcome = sprint_gate.run_sprint_gates(PID, "spr-1", {})
        assert outcome in ("Rework", "Failed")
    assert appdb.query_one("SELECT status FROM sprints WHERE id='spr-1'")["status"] == "Failed"
    # Failed is terminal for the engine: another run aborts
    assert sprint_gate.run_sprint_gates(PID, "spr-1", {}) == "Aborted"


# ---------------------------------------------------------------- unlock

def test_unlock_requires_completion(fx):
    sprint_gate.transition_sprint("spr-1", "Development Complete", "")
    assert sprint_gate.unlock_next_sprint(PID, "spr-1") is None
    assert appdb.query_one("SELECT status FROM sprints WHERE id='spr-2'")["status"] == "Planned"


def test_unlock_no_next_sprint(fx, monkeypatch):
    appdb.execute("UPDATE tasks SET sprint_id='spr-1' WHERE sprint_id='spr-2'")
    appdb.execute("DELETE FROM sprints WHERE id='spr-2'")
    _mock_gates(monkeypatch)
    assert sprint_gate.run_sprint_gates(PID, "spr-1", {}) == "Completed"
    assert sprint_gate.unlock_next_sprint(PID, "spr-1") is None  # nothing to unlock


# ------------------------------------------------------------------ misc

def test_ac_seeding_from_tasks(fx):
    appdb.execute("UPDATE tasks SET acceptance_criteria='User can create a project.; "
                  "Project name is mandatory.' WHERE id='t1'")
    sprint = appdb.query_one("SELECT * FROM sprints WHERE id='spr-1'")
    added = sprint_gate.seed_sprint_acs(sprint)
    assert added == 2
    again = sprint_gate.seed_sprint_acs(sprint)
    assert again == 0  # idempotent
    acs = appdb.query("SELECT * FROM sprint_acceptance_criteria WHERE sprint_id='spr-1'")
    assert [a["code"] for a in acs] == ["AC-01", "AC-02"]


def test_gate_summary_shape(fx):
    summ = sprint_gate.sprint_gate_summary("spr-1")
    assert summ["status"] == "Active"
    assert [g["gate"] for g in summ["gates"]] == ["Development", "Build", "Tests",
                                                  "Functional", "Acceptance"]
