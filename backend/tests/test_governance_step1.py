"""V3 Step 1 — project lifecycle state machine + PO baseline approval gate.

Covers: transition validation/audit, legacy 'Active' alias, opt-in
enforcement (start_execution / sprint start), requirement versioning,
baseline propose/approve/reject flows, change requests, PO action
dispatch, and the governance HTTP routes.
"""
import json
from types import SimpleNamespace

import pytest

from app import db as appdb
from app import governance as gov

TS = "2026-09-23T00:00:00+00:00"


def _seed_project(governance=0, state="Active"):
    _seed_team()
    appdb.insert("projects", {"id": "pr1", "name": "P", "goal": "ship weather app",
                              "description": "d", "technology_stack": "FastAPI",
                              "workspace_path": "C:/nonexistent-ws", "team_id": "t1",
                              "po_enabled": 0, "status": "Active",
                              "lifecycle_state": state,
                              "governance_enabled": governance,
                              "created_at": TS, "updated_at": TS})


def _seed_team():
    if appdb.query_one("SELECT id FROM teams WHERE id='t1'"):
        return
    appdb.insert("roles", {"id": "r-dev", "name": "Senior Developer",
                           "created_at": TS, "updated_at": TS})
    appdb.insert("personas", {"id": "p-dev", "role_id": "r-dev", "name": "dev",
                              "created_at": TS, "updated_at": TS})
    appdb.insert("teams", {"id": "t1", "name": "Team 1", "created_at": TS})


def _seed_exec():
    """Full org + one claimable task in an active sprint."""
    _seed_team()
    appdb.insert("agents", {"id": "a-dev", "name": "DEV", "role_id": "r-dev",
                            "persona_id": "p-dev", "created_at": TS, "updated_at": TS})
    appdb.insert("sprints", {"id": "s1", "project_id": "pr1", "name": "S1",
                             "status": "Active", "created_at": TS})
    appdb.insert("tasks", {"id": "tk1", "project_id": "pr1", "sprint_id": "s1",
                           "title": "Weather page", "description": "",
                           "acceptance_criteria": "", "story_points": 1, "priority": 2,
                           "status": "Ready", "evidence": "",
                           "assigned_agent_id": "a-dev", "rework_count": 0,
                           "created_at": TS, "updated_at": TS})


# ---------------------------------------------------------------- state machine

def test_legacy_active_alias_and_normal_flow():
    _seed_project()
    assert gov.current_state("pr1") == "Active Development"
    ok, err = gov.transition_project("pr1", "Final Validation")
    assert ok, err
    ok, err = gov.transition_project("pr1", "Completed", "shipped")
    assert ok, err
    assert gov.current_state("pr1") == "Completed"
    # Completed is terminal.
    ok, err = gov.transition_project("pr1", "Active Development")
    assert not ok and "Illegal" in err


def test_illegal_and_unknown_transitions_rejected():
    _seed_project(state="Draft")
    assert gov.current_state("pr1") == "Draft"
    ok, err = gov.transition_project("pr1", "Completed")
    assert not ok and "Draft -> Completed" in err
    ok, err = gov.transition_project("pr1", "Nonsense")
    assert not ok and "Unknown lifecycle state" in err


def test_transition_persists_event_and_audit():
    _seed_project(state="Draft")
    gov.transition_project("pr1", "Requirement Analysis", "BA starts", actor="user")
    ev = appdb.query_one("SELECT * FROM execution_events WHERE event_type = "
                         "'project.lifecycle_changed'")
    assert json.loads(ev["payload"])["to"] == "Requirement Analysis"
    au = appdb.query_one("SELECT * FROM audit_events WHERE action = 'project_lifecycle'")
    assert "Draft -> Requirement Analysis" in au["summary"]


def test_may_execute_only_blocks_when_governance_on():
    _seed_project(governance=0, state="Draft")
    assert gov.may_execute("pr1") == (True, "")
    appdb.execute("UPDATE projects SET governance_enabled = 1")
    ok, reason = gov.may_execute("pr1")
    assert not ok and "Draft" in reason
    appdb.execute("UPDATE projects SET lifecycle_state = 'Active Development'")
    assert gov.may_execute("pr1")[0]


# ---------------------------------------------------------------- requirements

def test_requirement_versioning_never_overwrites():
    _seed_project()
    r1 = gov.create_requirement("pr1", "Login", "v1 content")
    r2 = gov.create_requirement("pr1", "Login", "v2 content")
    assert (r1["version"], r2["version"]) == (1, 2)
    old = appdb.query_one("SELECT * FROM project_requirements WHERE id = ?", (r1["id"],))
    assert old["status"] == "superseded" and old["raw_content"] == "v1 content"
    other = gov.create_requirement("pr1", "Export", "x")
    assert other["version"] == 1
    assert {r["title"] for r in gov.active_requirements("pr1")} == {"Login", "Export"}


# ---------------------------------------------------------------- baselines

def test_baseline_propose_approve_and_renumber():
    _seed_project(governance=1, state="Requirement Analysis")
    appdb.insert("backlog_items", {"id": "b1", "project_id": "pr1", "title": "Login page",
                                   "description": "", "priority": 1, "story_points": 3,
                                   "acceptance_criteria": "Given..", "status": "Open",
                                   "created_at": TS})
    gov.create_requirement("pr1", "Auth spec", "must use email+password")
    res = gov.propose_baseline("pr1")
    assert "baseline" in res, res
    b = res["baseline"]
    assert b["code"] == "RB-1.0" and b["status"] == "pending_approval"
    snap = json.loads(b["content_json"])
    assert snap["goal"] == "ship weather app"
    assert snap["backlog"][0]["title"] == "Login page"
    assert snap["requirements"][0]["title"] == "Auth spec"
    assert gov.current_state("pr1") == "Pending PO Approval"
    # No duplicate pending baseline.
    assert "error" in gov.propose_baseline("pr1")
    # Approve via PO-agent-style actor.
    dec = gov.decide_baseline("pr1", b["id"], "approve", actor="PO agent Nova")
    assert dec["ok"] and dec["state"] == "Approved"
    row = appdb.query_one("SELECT * FROM project_baselines WHERE id = ?", (b["id"],))
    assert row["status"] == "approved" and row["approved_by"] == "PO agent Nova"
    # A re-baseline after approval gets the next code.
    res2 = gov.propose_baseline("pr1")
    assert res2["baseline"]["code"] == "RB-2.0"


def test_baseline_reject_opens_change_request_cycle():
    _seed_project(governance=1, state="Architecture Analysis")
    b = gov.propose_baseline("pr1")["baseline"]
    dec = gov.decide_baseline("pr1", b["id"], "reject", notes="goal is wrong",
                              actor="user")
    assert dec["ok"] and dec["state"] == "Change Requested"
    assert dec["change_request"]["code"] == "CR-1"
    assert dec["change_request"]["business_impact"] == "goal is wrong"
    crs = gov.list_change_requests("pr1", status="open")
    assert len(crs) == 1
    # Incorporating moves the project back into analysis for a new baseline.
    res = gov.decide_change_request("pr1", crs[0]["id"], "incorporate", notes="rewrote goal")
    assert res["state"] == "Requirement Analysis"
    assert gov.list_change_requests("pr1")[0]["status"] == "incorporate"
    # Second CR gets CR-2 and declining leaves the state alone.
    b2 = gov.propose_baseline("pr1")["baseline"]
    assert b2["code"] == "RB-2.0"
    dec2 = gov.decide_baseline("pr1", b2["id"], "request_revision", notes="split phase 2")
    assert dec2["change_request"]["code"] == "CR-2"
    res2 = gov.decide_change_request("pr1", dec2["change_request"]["id"], "decline")
    assert res2["state"] == "Change Requested"
    # Deciding a decided baseline is rejected.
    assert "error" in gov.decide_baseline("pr1", b["id"], "approve")


# ---------------------------------------------------------------- enforcement

def test_start_execution_and_eligibility_respect_lifecycle(monkeypatch):
    from app import runtime
    _seed_project(governance=1, state="Pending PO Approval")
    _seed_exec()
    res = runtime.start_execution("pr1", "tk1")
    assert "TASK_BLOCKED_BY_LIFECYCLE" in res.get("error", "")
    assert gov.current_state("pr1") != "Active Development"
    assert runtime._eligible_tasks("pr1") == []
    appdb.execute("UPDATE projects SET lifecycle_state = 'Active Development'")
    assert [t["id"] for t in runtime._eligible_tasks("pr1")] == ["tk1"]

    def fake_spawn(coro):
        coro.close()
        return SimpleNamespace(cancel=lambda: None)
    monkeypatch.setattr(runtime, "_spawn", fake_spawn)
    try:
        ok = runtime.start_execution("pr1", "tk1")
        assert "run" in ok
    finally:
        with runtime._registry_lock:
            for rid in list(runtime._registry):
                runtime._registry.pop(rid, None)


def test_sprint_start_blocked_by_lifecycle():
    from app import runtime
    _seed_project(governance=1, state="Draft")
    _seed_exec()
    res = runtime.start_sprint_execution("pr1", "s1")
    assert "SPRINT_BLOCKED_BY_LIFECYCLE" in res.get("error", "")


def test_sprint_start_recovers_stranded_scaffolding(monkeypatch):
    """A project left in Scaffolding (post-approval blueprint/breakdown run
    died after RB approval) must be unblocked when a sprint is already
    planned — walking the legal edges Scaffolding -> Ready -> Active Dev."""
    from app import runtime
    _seed_project(governance=1, state="Scaffolding")
    appdb.insert("agents", {"id": "a-dev", "name": "DEV", "role_id": "r-dev",
                            "persona_id": "p-dev", "created_at": TS, "updated_at": TS})
    appdb.insert("sprints", {"id": "sp1", "project_id": "pr1", "name": "1",
                             "status": "Planned", "created_at": TS})
    appdb.insert("tasks", {"id": "tk1", "project_id": "pr1", "sprint_id": "sp1",
                           "title": "Weather page", "description": "",
                           "acceptance_criteria": "", "story_points": 1, "priority": 2,
                           "status": "Todo", "evidence": "", "rework_count": 0,
                           "created_at": TS, "updated_at": TS})

    def fake_spawn(coro):
        coro.close()
        return SimpleNamespace(cancel=lambda: None)
    monkeypatch.setattr(runtime, "_spawn", fake_spawn)
    try:
        res = runtime.start_sprint_execution("pr1", "sp1")
        assert "SPRINT_BLOCKED_BY_LIFECYCLE" not in res.get("error", "")
        assert res.get("ok") is True
        assert gov.current_state("pr1") == "Active Development"
    finally:
        with runtime._schedulers_lock:
            runtime._schedulers.pop("pr1", None)


def test_scaffolding_without_planned_sprint_stays_blocked():
    from app import runtime
    _seed_project(governance=1, state="Scaffolding")
    res = runtime.start_sprint_execution("pr1", None)
    assert "SPRINT_BLOCKED_BY_LIFECYCLE" in res.get("error", "")
    assert gov.current_state("pr1") == "Scaffolding"


# ---------------------------------------------------------------- PO dispatch

def test_po_governance_actions_dispatch():
    from app import po
    _seed_project(governance=1, state="Requirement Analysis")
    log = po._apply_actions("pr1", [
        {"action": "add_requirement", "title": "Dark mode", "content": "toggle in settings"},
        {"action": "propose_baseline"},
    ])
    assert any("Dark mode" in l for l in log) and any("RB-1.0" in l for l in log)
    assert gov.current_state("pr1") == "Pending PO Approval"
    log2 = po._apply_actions("pr1", [
        {"action": "decide_baseline", "decision": "approve", "code": "RB-1.0"},
        {"action": "transition_project", "to": "Ready for Planning"},
    ])
    assert any("Approved" in l for l in log2)
    assert gov.current_state("pr1") == "Ready for Planning"
    # Illegal PO transitions are reported, not applied.
    log3 = po._apply_actions("pr1", [{"action": "transition_project", "to": "Completed"}])
    assert any("Illegal" in l for l in log3)
    assert gov.current_state("pr1") == "Ready for Planning"


def test_po_brief_shows_lifecycle():
    from app import po
    _seed_project(governance=1, state="Pending PO Approval")
    gov.create_requirement("pr1", "X", "y")
    gov.propose_baseline("pr1")
    brief = po._project_brief("pr1")
    assert "LIFECYCLE: Pending PO Approval (governance ON; pending approval: RB-1.0)" in brief
    assert "GOVERNANCE ON" in brief


# ---------------------------------------------------------------- routes

def test_governance_routes():
    from fastapi.testclient import TestClient
    from app.main import app
    _seed_project(governance=1, state="Draft")
    client = TestClient(app)
    r = client.get("/api/v1/projects/pr1/lifecycle")
    assert r.status_code == 200
    assert r.json()["state"] == "Draft"
    r = client.post("/api/v1/projects/pr1/lifecycle/transition",
                    json={"to": "Completed", "reason": ""})
    assert r.status_code == 409
    r = client.post("/api/v1/projects/pr1/lifecycle/transition",
                    json={"to": "Requirement Analysis"})
    assert r.status_code == 200 and r.json()["state"] == "Requirement Analysis"
    r = client.post("/api/v1/projects/pr1/requirements",
                    json={"title": "Spec A", "content": "abc"})
    assert r.status_code == 200 and r.json()["version"] == 1
    r = client.post("/api/v1/projects/pr1/baseline", json={"kind": "bogus"})
    assert r.status_code == 409
    r = client.post("/api/v1/projects/pr1/baseline", json={"kind": "requirement"})
    assert r.status_code == 200 and r.json()["code"] == "RB-1.0"
    bid = r.json()["id"]
    r = client.post(f"/api/v1/projects/pr1/baseline/{bid}/decision",
                    json={"decision": "approve"})
    assert r.status_code == 200 and r.json()["state"] == "Approved"
    r = client.get("/api/v1/projects/pr1/change_requests")
    assert r.status_code == 200 and r.json() == []
    r = client.get("/api/v1/projects/missing/lifecycle")
    assert r.status_code == 404
    r = client.post("/api/v1/projects/pr1/requirements", json={"title": "  "})
    assert r.status_code == 400
