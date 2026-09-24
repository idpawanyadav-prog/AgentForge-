"""V3 Step 2 — project-initiation spec pipeline tests.

Covers: BA/SA document drafting (BAS/PDS/TS) into project_documents +
requirements + RB baseline, the single approval gate (human chat approve,
PO-agent auto-review with one automatic revision round, reject), the SA
blueprint written into the 009 contract tables with an auto-approved AB
baseline, blueprint-driven sprint breakdown, chat command/intent wiring,
the CONTRACTS block injected into dev prompts, and the document routes.

LLM calls are stubbed at specs._llm (the single choke point) and thread
spawns are captured so everything runs deterministically in one thread.
"""
import json
import re
import threading as _real_threading
from types import SimpleNamespace

import pytest

from app import db as appdb
from app import governance as gov
from app import specs

TS = "2026-09-23T00:00:00+00:00"
LONG_MD = ("# Doc\n\nRequirement: the app must show the current temperature and "
           "condition for a searched city, cache results for ten minutes, and fail "
           "gracefully on unknown cities. " * 8)

BLUEPRINT = {
    "decisions": [{"title": "FastAPI monolith", "decision": "single service",
                   "consequences": "simple deploy"}],
    "components": [{"code": "C-1", "name": "api", "path": "app/main.py",
                    "purpose": "routes", "owner_role": "Senior Developer"}],
    "file_contracts": [
        {"path": "app/weather.py", "module": "weather", "owner_role": "Senior Developer",
         "purpose": "weather logic + cache", "must_implement": ["get_forecast(city)"],
         "allowed_deps": ["app/helpers.py"], "forbidden": ["print"],
         "technical_ac": "cached 10 min"},
        {"path": "app/main.py", "module": "api", "owner_role": "Senior Developer",
         "purpose": "entry point", "must_implement": ["app"], "allowed_deps": [],
         "forbidden": [], "technical_ac": ""}],
    "interface_contracts": [{"name": "get_forecast", "module": "weather",
                             "signature": "get_forecast(city: str) -> dict"}]}

SPRINTS = {"sprints": [
    {"name": "S1 Foundation", "goal": "running skeleton + weather core", "tasks": [
        {"title": "Scaffold FastAPI app", "description": "create app/main.py",
         "acceptance_criteria": "GET /health returns 200", "points": 3, "priority": 1,
         "files": ["app/main.py"]},
        {"title": "Weather module", "description": "implement app/weather.py get_forecast",
         "acceptance_criteria": "Given a city, returns temp dict", "points": 5,
         "priority": 2, "files": ["app/weather.py"]}]},
    {"name": "S2 UI", "goal": "usable dashboard", "tasks": [
        {"title": "Dashboard route", "description": "render search page",
         "acceptance_criteria": "page lists forecast", "points": 3, "priority": 2,
         "files": []}]}]}


def _seed_project(governance=1, state="Draft"):
    if appdb.query_one("SELECT id FROM projects WHERE id = 'pr1'"):
        appdb.execute("UPDATE projects SET governance_enabled = ?, lifecycle_state = ?",
                      (governance, state))
        return
    appdb.insert("projects", {"id": "pr1", "name": "P", "goal": "ship weather app",
                              "description": "d", "technology_stack": "FastAPI",
                              "workspace_path": "C:/nonexistent-ws", "team_id": None,
                              "po_enabled": 0, "status": "Active",
                              "lifecycle_state": state, "governance_enabled": governance,
                              "created_at": TS, "updated_at": TS})


@pytest.fixture
def posted(monkeypatch):
    messages = []
    monkeypatch.setattr(specs, "_post", lambda pid, text, cid=None: messages.append(text))
    return messages


@pytest.fixture
def spawned(monkeypatch):
    """Capture thread spawns so the pipeline runs inline and deterministically."""
    jobs = []

    class FakeThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
            self.t = (target, args, kwargs or {})

        def start(self):
            jobs.append(self.t)

    monkeypatch.setattr(specs, "threading",
                        SimpleNamespace(Thread=FakeThread,
                                        get_ident=_real_threading.get_ident))
    return jobs


def _drain(jobs):
    while jobs:
        target, args, kwargs = jobs.pop(0)
        target(*args, **kwargs)


def _fake_llm(responses):
    def fake(project_id, system, user, max_tokens=4000, label=""):
        r = responses.get(label, responses.get("*"))
        if r is None:
            raise AssertionError(f"unexpected LLM label {label!r}")
        return r() if callable(r) else r
    return fake


# ---------------------------------------------------------------- happy paths


def test_human_path_end_to_end(monkeypatch, posted, spawned):
    _seed_project()
    monkeypatch.setattr(specs.po, "po_agent_for_project", lambda pid: None)
    monkeypatch.setattr(specs, "_llm", _fake_llm(
        {"doc:BAS": LONG_MD, "doc:PDS": LONG_MD, "doc:TS": LONG_MD,
         "blueprint": json.dumps(BLUEPRINT), "sprint-plan": json.dumps(SPRINTS)}))

    msg = specs.start_pipeline("pr1")
    assert msg.startswith("Starting the initiation pipeline")
    assert gov.governance_enabled("pr1")
    _drain(spawned)
    assert sorted(d["kind"] for d in specs.docs("pr1")) == ["BAS", "PDS", "TS"]
    b = specs.pending_requirement_baseline("pr1")
    assert b and b["code"] == "RB-1.0"
    assert gov.current_state("pr1") == "Pending PO Approval"
    assert not gov.may_execute("pr1")[0]
    assert any("/documents/BAS" in m and "Reply `approve`" in m for m in posted)

    reply = specs.chat_approve("pr1")
    assert reply.startswith("✅")
    _drain(spawned)

    assert gov.current_state("pr1") == "Ready for Planning"
    adrs = appdb.query("SELECT * FROM architecture_decisions WHERE project_id='pr1'")
    assert [a["code"] for a in adrs] == ["ADR-1"] and adrs[0]["status"] == "accepted"
    fc = {f["path"]: f for f in appdb.query(
        "SELECT * FROM file_contracts WHERE project_id='pr1' AND status='active'")}
    assert set(fc) == {"app/weather.py", "app/main.py"}
    assert json.loads(fc["app/weather.py"]["must_implement"]) == ["get_forecast(city)"]
    assert appdb.query("SELECT * FROM interface_contracts WHERE project_id='pr1'")
    assert appdb.query("SELECT * FROM component_registry WHERE project_id='pr1'")
    ab = gov.latest_baseline("pr1", "architecture")
    assert ab["code"] == "AB-1.0" and ab["status"] == "approved"
    assert "single-gate" in ab["approved_by"]
    sprints = appdb.query("SELECT * FROM sprints WHERE project_id='pr1' ORDER BY created_at")
    assert [s["name"] for s in sprints] == ["S1 Foundation", "S2 UI"]
    assert all(s["status"] == "Planned" for s in sprints)
    task = appdb.query_one("SELECT * FROM tasks WHERE title = 'Weather module'")
    assert "app/weather.py" in task["description"]
    assert task["technical_ac"] == "cached 10 min"
    assert specs.latest_doc("pr1", "BLUEPRINT")["status"] == "approved"
    assert specs.latest_doc("pr1", "SPRINT-PLAN") is not None
    assert specs.latest_doc("pr1", "BAS")["status"] == "approved"
    assert any("Sprint breakdown" in m and "start sprint" in m for m in posted)


def test_po_auto_reviews_approves_and_continues(monkeypatch, posted, spawned):
    _seed_project()
    monkeypatch.setattr(specs.po, "po_agent_for_project",
                        lambda pid: {"id": "a-po", "name": "Nova"})
    monkeypatch.setattr(specs, "_llm", _fake_llm(
        {"doc:BAS": LONG_MD, "doc:PDS": LONG_MD, "doc:TS": LONG_MD,
         "po:doc-review": json.dumps({"verdict": "approve", "notes": "right-sized"}),
         "blueprint": json.dumps(BLUEPRINT), "sprint-plan": json.dumps(SPRINTS)}))
    assert specs._claim("pr1")
    specs._documents_worker("pr1", None)
    assert gov.current_state("pr1") == "Ready for Planning"
    assert gov.latest_baseline("pr1")["status"] == "approved"
    assert appdb.query_one("SELECT * FROM sprints WHERE project_id='pr1'")
    assert any("Nova approved RB-1.0" in m for m in posted)


def test_po_revision_round_then_approve(monkeypatch, posted, spawned):
    _seed_project()
    monkeypatch.setattr(specs.po, "po_agent_for_project",
                        lambda pid: {"id": "a-po", "name": "Nova"})
    verdicts = iter([json.dumps({"verdict": "request_revision",
                                 "notes": "drop the admin portal; add units toggle"}),
                     json.dumps({"verdict": "approve", "notes": "fixed"})])
    monkeypatch.setattr(specs, "_llm", _fake_llm(
        {"doc:BAS": LONG_MD, "doc:PDS": LONG_MD, "doc:TS": LONG_MD,
         "po:doc-review": lambda: next(verdicts),
         "blueprint": json.dumps(BLUEPRINT), "sprint-plan": json.dumps(SPRINTS)}))
    assert specs._claim("pr1")
    specs._documents_worker("pr1", None)
    assert specs.latest_doc("pr1", "BAS")["version"] == 2
    assert specs.latest_doc("pr1", "BAS")["review_notes"].startswith("drop the admin")
    rb = gov.latest_baseline("pr1")
    assert rb["code"] == "RB-2.0" and rb["status"] == "approved"
    assert gov.current_state("pr1") == "Ready for Planning"
    # The rejected RB-1.0 stays on record; a change request was opened.
    assert appdb.query_one("SELECT * FROM project_baselines WHERE code = 'RB-1.0'")["status"] \
        == "revision_requested"
    assert appdb.query_one("SELECT * FROM change_requests WHERE project_id='pr1'")


def _forbidden():
    raise AssertionError("blueprint/sprint LLM must not be reached in this test")


def test_po_double_revision_pauses_for_human(monkeypatch, posted, spawned):
    _seed_project()
    monkeypatch.setattr(specs.po, "po_agent_for_project",
                        lambda pid: {"id": "a-po", "name": "Nova"})
    monkeypatch.setattr(specs, "_llm", _fake_llm(
        {"doc:BAS": LONG_MD, "doc:PDS": LONG_MD, "doc:TS": LONG_MD,
         "po:doc-review": json.dumps({"verdict": "request_revision", "notes": "still wrong"}),
         "*": _forbidden}))
    assert specs._claim("pr1")
    specs._documents_worker("pr1", None)
    assert specs.pending_requirement_baseline("pr1")["code"] == "RB-2.0"
    assert gov.current_state("pr1") == "Pending PO Approval"
    assert not appdb.query_one("SELECT * FROM sprints WHERE project_id='pr1'")
    assert any("requested revision twice" in m for m in posted)


def test_human_reject_and_revise(monkeypatch, posted, spawned):
    _seed_project()
    monkeypatch.setattr(specs.po, "po_agent_for_project", lambda pid: None)
    monkeypatch.setattr(specs, "_llm", _fake_llm(
        {"doc:BAS": LONG_MD, "doc:PDS": LONG_MD, "doc:TS": LONG_MD}))
    assert specs._claim("pr1")
    specs._documents_worker("pr1", None)
    specs._release("pr1")

    out = specs.chat_revise("pr1", "narrow scope to one city")
    assert out.startswith("🔁")
    _drain(spawned)
    assert specs.latest_doc("pr1", "BAS")["version"] == 2
    assert specs.pending_requirement_baseline("pr1")["code"] == "RB-2.0"

    out = specs.chat_reject("pr1", "goal changed")
    assert out.startswith("❌")
    assert gov.current_state("pr1") == "Change Requested"
    assert gov.list_change_requests("pr1", status="open")


# ---------------------------------------------------------------- guards


def test_start_pipeline_guards(monkeypatch, spawned):
    _seed_project(state="Active Development")
    assert "already in" in specs.start_pipeline("pr1")
    appdb.execute("UPDATE projects SET lifecycle_state='Draft' WHERE id='pr1'")
    assert specs.start_pipeline("pr1").startswith("Starting")
    assert "already running" in specs.start_pipeline("pr1")
    specs._release("pr1")


def test_llm_guard_without_gateway(monkeypatch):
    _seed_project()
    from app import codegen
    monkeypatch.setattr(codegen, "resolve_llm", lambda project: (None, None))
    with pytest.raises(specs.SpecsError, match="No LLM gateway"):
        specs._llm("pr1", "s", "u")


# ---------------------------------------------------------------- chat wiring


def test_new_intents_route():
    from app import chatbot

    def intent_of(text):
        for name, pattern in chatbot.INTENTS:
            if re.match(pattern, text, re.IGNORECASE):
                return name, re.match(pattern, text, re.IGNORECASE)
        return None, None

    assert intent_of("proceed")[0] == "confirm"
    assert intent_of("ok")[0] == "confirm"
    assert intent_of("approve")[0] == "confirm"
    assert intent_of("analyze project")[0] == "analyze_project"
    assert intent_of("draft the specs")[0] == "analyze_project"
    name, m = intent_of("reject the documents goal is wrong")
    assert name == "reject_baseline" and "goal is wrong" in m.group("notes")
    name, m = intent_of("revise the documents drop admin")
    assert name == "revise_baseline" and "drop admin" in m.group("notes")
    assert intent_of("status")[0] == "status"


def test_chat_approve_when_nothing_pending_is_confirm_fallback(monkeypatch, tmp_path):
    """`approve` with no pending chat command but a pending document baseline
    decides the baseline instead of saying 'nothing to confirm'."""
    from app import chatbot
    _seed_project(state="Pending PO Approval")
    monkeypatch.setattr(specs, "chat_approve", lambda pid: "APPROVED-CALLED")
    appdb.insert("conversations", {"id": "c1", "project_id": "pr1", "title": "Control",
                                   "created_at": TS, "updated_at": TS})
    appdb.execute("UPDATE projects SET governance_enabled = 0")  # keep confirm path simple
    appdb.insert("project_baselines", {
        "id": "bl1", "project_id": "pr1", "kind": "requirement", "code": "RB-1.0",
        "content_json": "{}", "status": "pending_approval", "created_at": TS,
        "updated_at": TS})
    out = chatbot._confirm_pending("pr1", "c1")
    assert out["reply"] == "APPROVED-CALLED"


def test_execute_command_wrappers(monkeypatch):
    from app import chatbot
    monkeypatch.setattr(specs, "chat_approve", lambda pid: "A")
    monkeypatch.setattr(specs, "chat_reject", lambda pid, notes: f"R:{notes}")
    monkeypatch.setattr(specs, "chat_revise", lambda pid, notes: f"V:{notes}")
    monkeypatch.setattr(specs, "start_pipeline", lambda pid: "P")
    assert chatbot._execute_command("pr1", "analyze_project", {}) == "P"
    assert chatbot._execute_command("pr1", "approve_baseline", {}) == "A"
    assert chatbot._execute_command("pr1", "reject_baseline", {"notes": " x "}) == "R:x"
    assert chatbot._execute_command("pr1", "revise_baseline", {"notes": " y "}) == "V:y"
    assert "Tell me what to change" in chatbot._execute_command(
        "pr1", "revise_baseline", {})


# ---------------------------------------------------------------- contracts block


def _seed_blueprint_rows():
    _seed_project(state="Ready for Planning")
    appdb.insert("file_contracts", {
        "id": "f1", "project_id": "pr1", "path": "app/weather.py", "module": "weather",
        "owner_role": "Senior Developer", "purpose": "weather logic",
        "must_implement": json.dumps(["get_forecast(city)"]),
        "allowed_deps": json.dumps(["app/helpers.py"]), "forbidden": json.dumps(["print"]),
        "technical_ac": "cached 10 min", "requirement_ids": "[]", "status": "active",
        "created_at": TS, "updated_at": TS})
    appdb.insert("interface_contracts", {
        "id": "i1", "project_id": "pr1", "name": "get_forecast", "module": "weather",
        "revision": 1, "signature": "get_forecast(city: str) -> dict",
        "status": "active", "superseded_by": None, "created_at": TS, "updated_at": TS})


def test_dev_contracts_block_matches_task_files():
    _seed_blueprint_rows()
    task = {"title": "Weather", "description": "implement app/weather.py",
            "acceptance_criteria": "", "technical_ac": ""}
    block = specs.dev_contracts_block("pr1", task)
    assert "BLUEPRINT CONTRACTS" in block
    assert "must implement: get_forecast(city)" in block
    assert "get_forecast(city: str) -> dict" in block
    other = {"title": "Docs", "description": "readme only", "acceptance_criteria": "",
             "technical_ac": ""}
    assert specs.dev_contracts_block("pr1", other) == ""
    assert specs.dev_contracts_block("missing-project", task) == ""


def test_governance_may_execute_hint_and_record_helper():
    _seed_project(governance=1, state="Draft")
    ok, reason = gov.may_execute("pr1")
    assert not ok and "analyze project" in reason
    _seed_blueprint_rows()
    b = gov.record_approved_baseline("pr1", "architecture", approved_by="single-gate")
    assert b["code"] == "AB-1.0" and b["status"] == "approved"
    snap = json.loads(b["content_json"])
    assert snap["decisions"] == []  # no ADR rows seeded in this test


# ---------------------------------------------------------------- routes


def test_document_routes():
    from fastapi.testclient import TestClient
    from app.main import app
    _seed_blueprint_rows()
    specs.save_doc("pr1", "BAS", "# BAS\nhello from the Business Analyst", status="approved")
    client = TestClient(app)
    r = client.get("/api/v1/projects/pr1/documents")
    assert r.status_code == 200
    assert r.json()[0]["kind"] == "BAS"
    r = client.get("/api/v1/projects/pr1/documents/bas")
    assert r.status_code == 200 and "hello from the Business Analyst" in r.text
    r = client.get("/api/v1/projects/pr1/documents/TS")
    assert r.status_code == 404
    r = client.get("/api/v1/projects/nope/documents/BAS")
    assert r.status_code == 404
