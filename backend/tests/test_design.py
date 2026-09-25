"""Design Phase — configurable, strictly-sequential pre-delivery workflow.

Covers: step normalisation/validation, flow design persistence, the
sequential run (human approval gate, reject returns to the same step, a
no-approval step auto-advances), approval history, PO-autonomous review, the
process catalog, and the design routes.

The LLM choke point (specs.author_doc / specs._llm) is stubbed; thread spawns
are captured and drained so everything runs deterministically in one thread.
"""
import threading as _real_threading
from types import SimpleNamespace

import pytest

from app import db as appdb
from app import design, specs
from app import governance as gov

TS = "2026-09-23T00:00:00+00:00"
PID = "pr-design"

ROLES = ["Senior Developer", "Solution Architect", "Business Analyst",
         "QA Engineer", "Product Owner"]


@pytest.fixture
def seed():
    for tbl in ("design_approvals", "project_design_runs", "project_documents",
                "project_baselines", "project_requirements", "change_requests",
                "lifecycle_events", "tasks", "sprints", "team_agents", "teams",
                "agents", "personas", "roles", "projects", "project_flows"):
        try:
            appdb.execute(f"DELETE FROM {tbl}")
        except Exception:
            pass
    for role in ROLES:
        appdb.insert("roles", {"id": "role-" + role, "name": role,
                               "created_at": TS, "updated_at": TS})
        appdb.insert("personas", {"id": "per-" + role, "role_id": "role-" + role,
                                  "name": role + " persona", "created_at": TS,
                                  "updated_at": TS})
    appdb.insert("projects", {
        "id": PID, "name": "Design Test", "goal": "ship it", "description": "d",
        "technology_stack": "FastAPI", "workspace_path": "C:/nonexistent-ws",
        "team_id": None, "po_enabled": 0, "governance_enabled": 0,
        "lifecycle_state": "Draft", "status": "Active",
        "created_at": TS, "updated_at": TS})
    return {}


@pytest.fixture
def posted(monkeypatch):
    messages = []
    monkeypatch.setattr(specs, "_post", lambda pid, text, cid=None: messages.append(text))
    return messages


@pytest.fixture
def spawned(monkeypatch):
    jobs = []

    class FakeThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
            self.t = (target, args, kwargs or {})

        def start(self):
            jobs.append(self.t)

    monkeypatch.setattr(design, "threading",
                        SimpleNamespace(Thread=FakeThread,
                                        get_ident=_real_threading.get_ident))
    # Blueprint continuation would spin up the spec pipeline; neutralise it.
    monkeypatch.setattr(specs, "on_requirements_approved", lambda pid: None)
    # Stub document authoring: write a real doc row without an LLM.
    def fake_author(pid, kind, review_notes=""):
        return specs.save_doc(pid, kind, f"# {kind}\n\n" + "content. " * 60,
                              review_notes=review_notes)
    monkeypatch.setattr(specs, "author_doc", fake_author)
    return jobs


def _drain(jobs):
    while jobs:
        target, args, kwargs = jobs.pop(0)
        target(*args, **kwargs)


def _flow(design_steps):
    return {"id": None, "name": "test", "design": design_steps, "stages": ["dev"],
            "team": [], "docs_gate": 0, "po_enabled": 0}


# ------------------------------------------------------------------ normalize

def test_normalize_steps_renumbers_and_validates(seed):
    steps = design.normalize_steps([
        {"role": "Business Analyst", "processes": ["Requirement Doc"],
         "approval_required": True},
        {"role": "Solution Architect", "processes": ["Technical Spec"],
         "approval_required": False}])
    assert [s["seq"] for s in steps] == [1, 2]
    assert steps[0]["approval_required"] is True
    assert steps[1]["approval_required"] is False


def test_normalize_rejects_unknown_role(seed):
    with pytest.raises(design.DesignError):
        design.normalize_steps([{"role": "Ghost", "processes": ["Requirement Doc"]}])


def test_normalize_rejects_unknown_process(seed):
    with pytest.raises(design.DesignError):
        design.normalize_steps([{"role": "Business Analyst", "processes": ["Nonsense"]}])


def test_normalize_requires_a_process(seed):
    with pytest.raises(design.DesignError):
        design.normalize_steps([{"role": "Business Analyst", "processes": []}])


# ------------------------------------------------------------------ flow persistence

def test_flow_design_roundtrip(seed):
    from app import flows
    team = [{"role": "Business Analyst", "count": 1},
            {"role": "Solution Architect", "count": 1},
            {"role": "Senior Developer", "count": 1},
            {"role": "QA Engineer", "count": 1}]
    f = flows.create_flow("With Design", "d", ["dev", "qa"], team,
                          design=[{"role": "Business Analyst",
                                   "processes": ["Requirement Doc"],
                                   "approval_required": True},
                                  {"role": "Solution Architect",
                                   "processes": ["Technical Spec"],
                                   "approval_required": True}])
    assert [s["seq"] for s in f["design"]] == [1, 2]
    assert f["design"][0]["role"] == "Business Analyst"
    assert f["design"][0]["approval_required"] is True
    # design survives an update that keeps the stage chain untouched
    flows.update_flow(f["id"], name="With Design", stages=["dev", "qa"],
                      team=team, design=f["design"])
    assert len(flows.get_flow(f["id"])["design"]) == 2


# ------------------------------------------------------------------ sequential run

def test_human_path_two_steps_then_complete(seed, posted, spawned):
    flow = _flow([{"role": "Business Analyst",
                   "processes": ["Requirement Doc", "Project Definition Sheet"],
                   "approval_required": True},
                  {"role": "Solution Architect", "processes": ["Technical Spec"],
                   "approval_required": True}])
    design.start_run(PID, flow)
    _drain(spawned)
    st = design.state(PID)
    assert st["status"] == "awaiting_approval" and st["current_seq"] == 1
    # step 1 authored both of its docs
    assert specs.latest_doc(PID, "BAS") and specs.latest_doc(PID, "PDS")
    assert not specs.latest_doc(PID, "TS")

    design.approve_design(PID)
    _drain(spawned)
    st = design.state(PID)
    assert st["status"] == "awaiting_approval" and st["current_seq"] == 2
    assert specs.latest_doc(PID, "TS")

    design.approve_design(PID)
    _drain(spawned)
    st = design.state(PID)
    assert st["status"] == "completed"
    assert all(s["state"] == "approved" for s in st["steps"])
    # the requirement baseline was proposed and approved (single downstream gate)
    rb = appdb.query_one("SELECT * FROM project_baselines WHERE project_id = ? "
                         "AND kind = 'requirement' ORDER BY created_at DESC LIMIT 1", (PID,))
    assert rb and rb["status"] == "approved"
    # one approval record per step
    approvals = [h for h in st["history"] if h["decision"] == "approve"
                 and h["actor_type"] == "user"]
    assert len(approvals) == 2


def test_reject_returns_to_same_step(seed, posted, spawned):
    flow = _flow([{"role": "Business Analyst", "processes": ["Requirement Doc"],
                   "approval_required": True}])
    design.start_run(PID, flow)
    _drain(spawned)
    assert design.state(PID)["current_seq"] == 1
    v1 = specs.latest_doc(PID, "BAS")["version"]

    design.reject_design(PID, "add error states")
    _drain(spawned)
    st = design.state(PID)
    # still step 1, awaiting again, doc re-authored as a new version
    assert st["current_seq"] == 1 and st["status"] == "awaiting_approval"
    assert specs.latest_doc(PID, "BAS")["version"] == v1 + 1
    # history records the rejection with its comments
    rej = [h for h in st["history"] if h["decision"] == "reject"]
    assert rej and rej[-1]["comments"] == "add error states"
    assert any(s["state"] == "awaiting_approval" for s in st["steps"])


def test_no_approval_step_auto_advances(seed, posted, spawned):
    flow = _flow([{"role": "Business Analyst", "processes": ["Requirement Doc"],
                   "approval_required": False},
                  {"role": "Solution Architect", "processes": ["Technical Spec"],
                   "approval_required": False}])
    design.start_run(PID, flow)
    _drain(spawned)
    st = design.state(PID)
    assert st["status"] == "completed"
    # both docs authored, auto-approvals recorded as system
    assert specs.latest_doc(PID, "BAS") and specs.latest_doc(PID, "TS")
    assert all(h["actor_type"] == "system" for h in st["history"])


def test_po_autonomy_reviews_and_approves(seed, posted, spawned, monkeypatch):
    appdb.update("projects", PID, {"po_enabled": 1})
    appdb.insert("teams", {"id": "t1", "name": "T", "created_at": TS})
    appdb.insert("agents", {"id": "po1", "name": "Po", "role_id": "role-Product Owner",
                            "persona_id": "per-Product Owner", "created_at": TS,
                            "updated_at": TS})
    appdb.execute("INSERT INTO team_agents (team_id, agent_id, active) VALUES (?,?,1)",
                  ("t1", "po1"))
    appdb.update("projects", PID, {"team_id": "t1"})
    monkeypatch.setattr(specs, "_llm",
                        lambda *a, **k: '{"verdict": "approve", "notes": "good"}')
    flow = _flow([{"role": "Business Analyst", "processes": ["Requirement Doc"],
                   "approval_required": True},
                  {"role": "Solution Architect", "processes": ["Technical Spec"],
                   "approval_required": True}])
    design.start_run(PID, flow)
    _drain(spawned)
    st = design.state(PID)
    assert st["status"] == "completed"   # PO cleared both gates automatically
    assert any(h["actor_type"] == "agent" for h in st["history"])


def test_start_requires_design_steps(seed):
    assert design.start_run(PID, _flow([])).startswith("This flow has no")


def test_second_start_rejected_while_awaiting(seed, posted, spawned):
    flow = _flow([{"role": "Business Analyst", "processes": ["Requirement Doc"],
                   "approval_required": True}])
    design.start_run(PID, flow)
    _drain(spawned)
    msg = design.start_run(PID, flow)
    assert ("already running" in msg or "awaiting approval" in msg or "complete" in msg
            or "paused at an approval gate" in msg)


# ------------------------------------------------------------------ catalog + routes

def test_catalog_lists_document_kinds(seed):
    cat = design.catalog()
    kinds = {c["kind"] for c in cat}
    assert {"BAS", "PDS", "TS", "ARCH", "DEVPLAN", "IMPLPLAN", "TESTPLAN"} <= kinds


def test_state_exposes_doc_progress(seed, posted, spawned):
    flow = _flow([{"role": "Business Analyst",
                   "processes": ["Requirement Doc", "Project Definition Sheet"],
                   "approval_required": True},
                  {"role": "Solution Architect", "processes": ["Technical Spec"],
                   "approval_required": True}])
    design.start_run(PID, flow)
    _drain(spawned)
    st = design.state(PID)
    s1, s2 = st["steps"]
    # step 1 fully authored -> its docs done, carrying a title + kind
    assert [d["kind"] for d in s1["docs"]] == ["BAS", "PDS"]
    assert all(d["done"] for d in s1["docs"])
    assert s1["docs"][0]["title"]            # human title present
    # step 2 untouched -> not done, no preparing pulse (step 1 already gated)
    assert all(not d["done"] for d in s2["docs"])
    assert not any(d.get("preparing") for d in s2["docs"])


def test_reveal_document_validation_only(seed):
    # Uses only the pre-filesystem branches so no OS file manager is launched.
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    # a name that fails the [A-Za-z0-9_-]+ guard is rejected (400) before any
    # filesystem / OS work runs.
    assert client.get(f"/api/v1/projects/{PID}/documents/bad%20kind/reveal"
                      ).status_code == 400
    # a project with no workspace path returns a message, not an OS open
    appdb.execute("UPDATE projects SET workspace_path = '' WHERE id = ?", (PID,))
    r = client.get(f"/api/v1/projects/{PID}/documents/BAS/reveal")
    assert r.status_code == 200 and "no workspace path" in r.text.lower()
    # missing project
    assert client.get("/api/v1/projects/nope/documents/BAS/reveal").status_code == 404


def test_design_routes(seed):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    r = client.get("/api/v1/flows/process_catalog")
    assert r.status_code == 200 and len(r.json()) >= 7
    assert client.get(f"/api/v1/projects/{PID}/design").json() is None
    assert client.get("/api/v1/projects/missing/design").status_code == 404
    d = client.post(f"/api/v1/projects/{PID}/design/decide",
                    json={"decision": "approve"})
    assert d.status_code == 200 and "awaiting" in d.json()["message"].lower() \
        or "Could not" in d.json()["message"]


def test_delete_project_cascades_design_rows(seed, posted, spawned):
    # A finished/awaiting design run leaves rows in project_documents,
    # project_design_runs and design_approvals; deleting the project must clear
    # all three (they carry NOT NULL FKs) or the delete 500s.
    from fastapi.testclient import TestClient
    from app.main import app
    flow = _flow([{"role": "Business Analyst",
                   "processes": ["Requirement Doc", "Project Definition Sheet"],
                   "approval_required": True}])
    design.start_run(PID, flow)
    _drain(spawned)
    # rows exist across all three design tables
    assert appdb.query_one(
        "SELECT COUNT(*) n FROM project_documents WHERE project_id=?", (PID,))["n"] > 0
    assert appdb.query_one(
        "SELECT COUNT(*) n FROM project_design_runs WHERE project_id=?", (PID,))["n"] == 1
    client = TestClient(app)
    r = client.delete(f"/api/v1/projects/{PID}")
    assert r.status_code == 200, r.text
    for tbl in ("project_documents", "project_design_runs", "design_approvals"):
        assert appdb.query_one(
            f"SELECT COUNT(*) n FROM {tbl} WHERE project_id=?", (PID,))["n"] == 0, tbl
