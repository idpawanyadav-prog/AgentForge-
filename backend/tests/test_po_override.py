"""Per-project Product Owner override.

A project's PO defaults to its aligned team's Product Owner agent. Setting
``projects.po_agent_id`` overrides that for one project, but only when the id
points at an active member of that project's team; otherwise resolution falls
back to the team default (so a deleted / off-team id can't strand the project).
"""
import pytest

from app import db as appdb, po
from fastapi.testclient import TestClient

TS = "2026-09-24T00:00:00+00:00"
PID = "proj-po-1"
TID = "team-po-1"          # the project's team (has a Product Owner)
TID2 = "team-po-2"         # an unrelated team
ROLES = ["Product Owner", "Senior Developer"]


@pytest.fixture()
def seed():
    for tbl in ("projects", "team_agents", "agents", "personas", "roles", "teams"):
        appdb.execute(f"DELETE FROM {tbl}")
    for role in ROLES:
        appdb.insert("roles", {"id": "role-" + role, "name": role,
                               "created_at": TS, "updated_at": TS})
        appdb.insert("personas", {"id": "per-" + role, "role_id": "role-" + role,
                                  "name": role + " persona", "created_at": TS,
                                  "updated_at": TS})
    appdb.insert("teams", {"id": TID, "name": "Team 1", "created_at": TS})
    appdb.insert("teams", {"id": TID2, "name": "Team 2", "created_at": TS})
    # Team 1: one Product Owner + one developer.
    for aid, role in (("ag-po", "Product Owner"), ("ag-dev", "Senior Developer")):
        appdb.insert("agents", {"id": aid, "name": aid.upper(), "role_id": "role-" + role,
                                "persona_id": "per-" + role, "created_at": TS, "updated_at": TS})
        appdb.insert("team_agents", {"team_id": TID, "agent_id": aid,
                                     "role_in_team": "Member", "active": 1})
    # Team 2 owns the other agent (NOT on Team 1).
    appdb.insert("agents", {"id": "ag-other", "name": "OTHER", "role_id": "role-Product Owner",
                            "persona_id": "per-Product Owner", "created_at": TS, "updated_at": TS})
    appdb.insert("team_agents", {"team_id": TID2, "agent_id": "ag-other",
                                 "role_in_team": "Member", "active": 1})
    appdb.insert("projects", {"id": PID, "name": "PO Test", "goal": "g",
                              "workspace_path": "C:/nonexistent-ws", "team_id": TID,
                              "po_enabled": 0, "status": "Active",
                              "created_at": TS, "updated_at": TS})
    return {}


def test_defaults_to_team_product_owner(seed):
    agent = po.po_agent_for_project(PID)
    assert agent["id"] == "ag-po"
    st = po.po_status(PID)
    assert st["agent_id"] == "ag-po" and st["overridden"] is False


def test_override_to_on_team_non_po_agent(seed):
    appdb.update("projects", PID, {"po_agent_id": "ag-dev"})
    assert po.po_agent_for_project(PID)["id"] == "ag-dev"
    st = po.po_status(PID)
    assert st["agent_id"] == "ag-dev" and st["overridden"] is True


def test_offteam_override_falls_back_to_team_po(seed):
    appdb.update("projects", PID, {"po_agent_id": "ag-other"})
    assert po.po_agent_for_project(PID)["id"] == "ag-po"
    assert po.po_status(PID)["overridden"] is False


def test_missing_agent_override_falls_back(seed):
    appdb.update("projects", PID, {"po_agent_id": "ghost-agent"})
    assert po.po_agent_for_project(PID)["id"] == "ag-po"
    assert po.po_status(PID)["overridden"] is False


# ---------------------------------------------------------------- HTTP PATCH

def test_patch_accepts_on_team_override_and_clear(seed):
    from app.main import app
    client = TestClient(app)
    r = client.patch(f"/api/v1/projects/{PID}", json={"po_agent_id": "ag-dev"})
    assert r.status_code == 200 and r.json()["po_agent_id"] == "ag-dev"
    assert po.po_agent_for_project(PID)["id"] == "ag-dev"
    r = client.patch(f"/api/v1/projects/{PID}", json={"po_agent_id": None})
    assert r.status_code == 200 and r.json()["po_agent_id"] is None
    assert po.po_agent_for_project(PID)["id"] == "ag-po"  # back to team default


def test_patch_rejects_off_team_override(seed):
    from app.main import app
    client = TestClient(app)
    r = client.patch(f"/api/v1/projects/{PID}", json={"po_agent_id": "ag-other"})
    assert r.status_code == 400
    assert appdb.query_one("SELECT po_agent_id FROM projects WHERE id=?", (PID,))["po_agent_id"] is None


def test_changing_team_clears_override(seed):
    from app.main import app
    client = TestClient(app)
    appdb.update("projects", PID, {"po_agent_id": "ag-dev"})
    r = client.patch(f"/api/v1/projects/{PID}", json={"team_id": TID2})
    assert r.status_code == 200 and r.json()["po_agent_id"] is None
