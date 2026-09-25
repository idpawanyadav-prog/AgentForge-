"""Project Flow templates: CRUD, validation, provisioning, and the runtime
walking a project's flow instead of the hardcoded dev->SA->BA->QA chain.

Runs against a throwaway SQLite DB (AGENT_OFFICE_DB env var) — never the
live database.
"""
import pytest

from app import chatbot, flows, runtime
from app import db as appdb

TS = "2026-09-23T00:00:00+00:00"
PID = "proj-flow-1"
TID = "team-flow-1"

ROLES = ["Senior Developer", "Solution Architect", "Business Analyst",
         "QA Engineer", "Product Owner"]


@pytest.fixture()
def seed():
    for tbl in ("task_reviews", "tasks", "sprints", "team_agents", "teams",
                "agents", "personas", "roles", "projects", "project_flows"):
        appdb.execute(f"DELETE FROM {tbl}")
    appdb.execute("DELETE FROM execution_events")
    appdb.execute("DELETE FROM messages")
    for role in ROLES:
        appdb.insert("roles", {"id": "role-" + role, "name": role,
                               "created_at": TS, "updated_at": TS})
        appdb.insert("personas", {"id": "per-" + role, "role_id": "role-" + role,
                                  "name": role + " persona", "created_at": TS,
                                  "updated_at": TS})
    appdb.insert("teams", {"id": TID, "name": "Flow team", "created_at": TS})
    for i, role in enumerate(ROLES):
        aid = f"ag{i}"
        appdb.insert("agents", {"id": aid, "name": f"A{i}", "role_id": "role-" + role,
                                "persona_id": "per-" + role, "created_at": TS,
                                "updated_at": TS})
        appdb.execute("INSERT INTO team_agents (team_id, agent_id) VALUES (?,?)",
                      (TID, aid))
    appdb.insert("projects", {
        "id": PID, "name": "Flow Test", "goal": "flow driven delivery",
        "description": "", "workspace_path": "C:/nonexistent-ws", "team_id": TID,
        "po_enabled": 0, "sa_review_enabled": 1, "ba_review_enabled": 1,
        "status": "Active", "created_at": TS, "updated_at": TS})
    appdb.insert("sprints", {"id": "spr-f", "project_id": PID, "name": "S1", "goal": "g",
                             "capacity": 10, "status": "Active", "created_at": TS})
    return {}


@pytest.fixture()
def fx(seed, monkeypatch):
    # Patch asyncio.sleep only for runtime tests. `runtime.asyncio` is the
    # shared asyncio module, so this must NOT be active while a TestClient
    # runs its own event loop — the fake sleep deadlocks the portal.
    async def _fast_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(runtime.asyncio, "sleep", _fast_sleep)
    monkeypatch.setattr(runtime.threading, "Thread", _FakeThread)
    return {}


class _FakeThread:
    def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _project():
    return appdb.query_one("SELECT * FROM projects WHERE id = ?", (PID,))


def _task(status="In Progress", **over):
    row = {"id": "tk1", "project_id": PID, "sprint_id": "spr-f", "title": "Build X",
           "description": "", "story_points": 3, "priority": 2,
           "assigned_agent_id": "ag0", "qa_agent_id": None, "status": status,
           "rework_count": 0, "progress": 75, "evidence": "", "blocked_reason": "",
           "functional_ac": "", "technical_ac": "",
           "created_at": TS, "updated_at": TS}
    row.update(over)
    appdb.execute("DELETE FROM tasks WHERE id = 'tk1'")
    appdb.insert("tasks", row)
    return appdb.query_one("SELECT * FROM tasks WHERE id = 'tk1'")


def _mk_flow(stages, team=None, name="Test Flow", **kw):
    return flows.create_flow(name, "t", stages,
                             team if team is not None else [
                                 {"role": "Senior Developer", "count": 1},
                                 {"role": "Solution Architect", "count": 1},
                                 {"role": "Business Analyst", "count": 1},
                                 {"role": "QA Engineer", "count": 1}], **kw)


# --------------------------------------------------------------------- CRUD

def test_crud_and_validation(fx):
    flow = _mk_flow(["dev", "ba", "qa"])
    assert flow["stages"] == ["dev", "ba", "qa"] and flow["is_builtin"] == 0
    assert flows.list_flows()[0]["id"] == flow["id"]
    with pytest.raises(flows.FlowError, match="first"):
        flows.create_flow("Bad", "", ["ba", "dev"])
    with pytest.raises(flows.FlowError, match="only once"):
        flows.create_flow("Dup stages", "", ["dev", "qa", "qa"])
    with pytest.raises(flows.FlowError, match="already exists"):
        flows.create_flow("Test Flow", "", ["dev"])
    with pytest.raises(flows.FlowError, match="QA Engineer"):
        flows.create_flow("No QA team", "", ["dev", "qa"],
                          team=[{"role": "Senior Developer", "count": 1}])
    upd = flows.update_flow(flow["id"], name="Test Flow", stages=["dev", "qa"],
                            team=[{"role": "Senior Developer", "count": 2},
                                  {"role": "QA Engineer", "count": 1}])
    assert upd["stages"] == ["dev", "qa"]
    flows.delete_flow(flow["id"])
    assert flows.get_flow(flow["id"]) is None


def test_delete_guarded_when_in_use_and_builtin(fx):
    flow = _mk_flow(["dev", "qa"])
    appdb.update("projects", PID, {"flow_id": flow["id"]})
    with pytest.raises(flows.FlowError, match="still use"):
        flows.delete_flow(flow["id"])
    appdb.update("projects", PID, {"flow_id": None})
    builtin = appdb.query_one("SELECT id FROM project_flows WHERE is_builtin = 1")
    if builtin:
        with pytest.raises(flows.FlowError, match="built-in"):
            flows.delete_flow(builtin["id"])


def test_default_flag_is_exclusive(fx):
    a = _mk_flow(["dev", "qa"], name="Flow A", is_default=1)
    b = _mk_flow(["dev", "ba", "qa"], name="Flow B", is_default=1)
    assert flows.get_flow(a["id"])["is_default"] == 0
    assert flows.get_flow(b["id"])["is_default"] == 1
    assert flows.default_flow()["id"] == b["id"]


# ------------------------------------------------------- flow resolution

def test_legacy_projects_follow_gate_flags(fx):
    f = flows.flow_for_project(_project())
    assert f["stages"] == ["dev", "sa", "ba", "qa"]
    appdb.update("projects", PID, {"sa_review_enabled": 0, "ba_review_enabled": 0})
    assert flows.flow_for_project(_project())["stages"] == ["dev", "qa"]


def test_next_after_canonical_fallback_for_foreign_stage():
    flow = {"stages": ["dev", "qa"]}
    assert flows.next_after(flow, "dev") == "qa"
    assert flows.next_after(flow, "qa") is None
    assert flows.next_after(flow, "sa") == "qa"   # parked manually, still advances
    assert flows.next_after(flow, "approve") is None


# ------------------------------------------------------------ provisioning

def test_provision_team_creates_agents_and_binding(fx):
    flow = _mk_flow(["dev", "qa"], team=[{"role": "Senior Developer", "count": 2},
                                         {"role": "QA Engineer", "count": 1}])
    appdb.update("projects", PID, {"team_id": None})
    done = flows.provision_team(flow, _project())
    team_id = done["team_id"]
    assert len(done["created_agents"]) == 3
    members = appdb.query(
        "SELECT r.name FROM team_agents ta JOIN agents a ON a.id = ta.agent_id "
        "JOIN roles r ON r.id = a.role_id WHERE ta.team_id = ?", (team_id,))
    names = sorted(r["name"] for r in members)
    assert names == ["QA Engineer", "Senior Developer", "Senior Developer"]
    assert _project()["team_id"] == team_id
    assert flows.team_gaps(flow, team_id) == []


def test_team_gaps_reports_missing_reviewer(fx):
    flow = _mk_flow(["dev", "sa", "qa"])
    gaps = flows.team_gaps(flow, TID)
    assert gaps == []  # fixture team has every role
    appdb.execute("DELETE FROM team_agents WHERE agent_id = 'ag1'")
    assert "Solution Architect" in flows.team_gaps(flow, TID)[0]


# ------------------------------------------------------- runtime honours flow

def test_dev_hands_off_to_first_flow_stage_not_hardcoded_sa(fx):
    flow = _mk_flow(["dev", "ba", "qa"])
    appdb.update("projects", PID, {"flow_id": flow["id"]})
    task = _task()
    handed, outcome = runtime._advance_stage(_project(), task, "dev", "ag0", "ev")
    assert handed and outcome == "ba-review-handoff"
    row = appdb.query_one("SELECT * FROM tasks WHERE id = 'tk1'")
    assert row["status"] == "BA Review"
    assert row["assigned_agent_id"] == "ag2"  # the BA, not the SA


def test_flow_without_qa_ends_in_done(fx):
    flow = _mk_flow(["dev", "sa"], team=[{"role": "Senior Developer", "count": 1},
                                         {"role": "Solution Architect", "count": 1}])
    appdb.update("projects", PID, {"flow_id": flow["id"]})
    task = _task(status="SA Review", assigned_agent_id="ag1", qa_agent_id="ag0")
    handed, _ = runtime._advance_stage(_project(), task, "sa", "ag0", "ev")
    assert not handed
    row = appdb.query_one("SELECT * FROM tasks WHERE id = 'tk1'")
    assert row["status"] == "SA Review"  # untouched by the failed hop


def test_cr_stage_validates_and_maps_to_mode(fx):
    assert "cr" in flows.STAGE_DEFS
    flow = _mk_flow(["dev", "cr", "sa"], name="CR Flow")
    assert flow["stages"] == ["dev", "cr", "sa"]
    assert runtime._run_mode({"status": "Code Review"}) == "cr"


def _add_second_senior_dev():
    appdb.insert("agents", {"id": "ag5", "name": "A5", "role_id": "role-Senior Developer",
                            "persona_id": "per-Senior Developer", "created_at": TS,
                            "updated_at": TS})
    appdb.execute("INSERT INTO team_agents (team_id, agent_id) VALUES (?,?)", (TID, "ag5"))


def test_dev_hands_off_to_code_review_gate(fx):
    # A 2nd Senior Developer lets the cr gate run: the author is excluded.
    _add_second_senior_dev()
    flow = _mk_flow(["dev", "cr", "sa", "qa"], name="CR chain")
    appdb.update("projects", PID, {"flow_id": flow["id"]})
    task = _task()
    handed, outcome = runtime._advance_stage(_project(), task, "dev", "ag0", "ev")
    assert handed and outcome == "cr-review-handoff"
    row = appdb.query_one("SELECT * FROM tasks WHERE id = 'tk1'")
    assert row["status"] == "Code Review"
    assert row["assigned_agent_id"] == "ag5"   # the other Senior Developer
    assert row["qa_agent_id"] == "ag0"          # author preserved for rework routing
    rev = appdb.query_one("SELECT * FROM task_reviews WHERE task_id = 'tk1'")
    assert rev["reviewer_type"] == "code_review"
    assert rev["status"] == "pending"


def test_code_review_gate_skips_without_second_dev(fx):
    # Only one Senior Developer (the author): cr is skipped and the chain continues.
    flow = _mk_flow(["dev", "cr", "sa", "qa"], name="CR skip")
    appdb.update("projects", PID, {"flow_id": flow["id"]})
    task = _task()
    handed, outcome = runtime._advance_stage(_project(), task, "dev", "ag0", "ev")
    assert handed and outcome == "sa-review-handoff"
    row = appdb.query_one("SELECT * FROM tasks WHERE id = 'tk1'")
    assert row["status"] == "SA Review"
    assert row["assigned_agent_id"] == "ag1"


def test_approve_stage_parks_task_and_chat_resolves_it(fx, monkeypatch):
    posted = []
    from app import specs
    monkeypatch.setattr(specs, "_post", lambda pid, text, conversation_id=None:
                        posted.append(text))
    flow = _mk_flow(["dev", "approve"], team=[{"role": "Senior Developer", "count": 1}])
    appdb.update("projects", PID, {"flow_id": flow["id"], "po_enabled": 0})
    task = _task()
    handed, outcome = runtime._advance_stage(_project(), task, "dev", "ag0", "ev")
    assert handed and outcome == "approval-handoff"
    row = appdb.query_one("SELECT * FROM tasks WHERE id = 'tk1'")
    assert row["status"] == "Pending Approval"
    assert posted and "approve task" in posted[0]

    reply = chatbot._execute_command(PID, "approve_task", {"ref": "tk1"})
    assert "approved" in reply
    assert appdb.query_one("SELECT status FROM tasks WHERE id='tk1'")["status"] == "Done"

    _task(status="Pending Approval", qa_agent_id="ag0")
    reply = chatbot._execute_command(PID, "reject_task",
                                     {"rest": "tk1: tighten the error copy"})
    assert "sent back" in reply
    row = appdb.query_one("SELECT * FROM tasks WHERE id = 'tk1'")
    assert row["status"] == "Rework" and "error copy" in row["blocked_reason"]
    assert row["assigned_agent_id"] == "ag0"


def test_approve_rejection_escalates_after_max_cycles(fx):
    flow = _mk_flow(["dev", "approve"], team=[{"role": "Senior Developer", "count": 1}])
    appdb.update("projects", PID, {"flow_id": flow["id"]})
    _task(status="Pending Approval", qa_agent_id="ag0",
          rework_count=runtime.MAX_REWORK_CYCLES - 1)
    runtime.resolve_task_approval(PID, "tk1", "rework", "nope")
    assert appdb.query_one("SELECT status FROM tasks WHERE id='tk1'")["status"] == "Blocked"


def test_intent_routing_for_task_approval():
    name, m = chatbot.match_intent("approve task tk1")
    assert name == "approve_task" and m.group("ref") == "tk1"
    name, m = chatbot.match_intent("reject task tk1: fix the copy")
    assert name == "reject_task" and m.group("rest") == "tk1: fix the copy"
    # Bare approve still belongs to the confirm/baseline fallback, not tasks.
    assert chatbot.match_intent("approve")[0] == "confirm"


# ------------------------------------------------------------------ routes

def test_flow_routes(seed):
    from fastapi.testclient import TestClient

    from app.main import app
    client = TestClient(app)
    r = client.get("/api/v1/flows")
    assert r.status_code == 200
    r = client.get("/api/v1/flows/stage_kinds")
    assert r.status_code == 200 and {k["key"] for k in r.json()} == \
        {"dev", "cr", "sa", "ba", "qa", "approve"}
    r = client.post("/api/v1/flows", json={"name": "Route Flow",
                                           "stages": ["dev", "qa"],
                                           "team": [{"role": "Senior Developer", "count": 1},
                                                    {"role": "QA Engineer", "count": 1}]})
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    r = client.put(f"/api/v1/flows/{fid}", json={"name": "Route Flow",
                                                 "stages": ["dev"], "team": []})
    assert r.status_code == 400  # QA stage needs a QA member on the team
    r = client.put(f"/api/v1/flows/{fid}", json={"name": "Route Flow 2",
                                                 "stages": ["dev", "qa"],
                                                 "team": [{"role": "Senior Developer", "count": 1},
                                                          {"role": "QA Engineer", "count": 1}]})
    assert r.status_code == 200 and r.json()["name"] == "Route Flow 2"
    r = client.delete(f"/api/v1/flows/{fid}")
    assert r.status_code == 200
    r = client.get(f"/api/v1/flows/{fid}")
    assert r.status_code == 404


def test_control_summary_exposes_project_flow(seed):
    from fastapi.testclient import TestClient

    from app.main import app
    client = TestClient(app)
    flow = _mk_flow(["dev", "cr", "sa", "ba", "qa"], name="Summary Flow",
                    team=[{"role": "Senior Developer", "count": 1},
                          {"role": "Solution Architect", "count": 1},
                          {"role": "Business Analyst", "count": 1},
                          {"role": "QA Engineer", "count": 1}])
    appdb.update("projects", PID, {"flow_id": flow["id"]})
    body = client.get(f"/api/v1/projects/{PID}/control/summary").json()
    assert body["flow"]["id"] == flow["id"]
    assert body["flow"]["stages"] == ["dev", "cr", "sa", "ba", "qa"]
