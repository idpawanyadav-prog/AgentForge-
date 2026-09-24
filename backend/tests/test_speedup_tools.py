"""AGENT_DEV_SPEEDUP tooling tests: instruction/persona versions + restore
(2.4/4.3), prompt linter (4.4), task templates (2.1), per-project cost budget
(3.3), cost rollup endpoint (3.5), and the playground (4.2)."""
import json

import pytest
from fastapi.testclient import TestClient

from app import budget
from app import codegen
from app import db as appdb
from app import task_templates as tt
from app.main import app
from app.prompt_lint import lint_prompt

TS = "2026-09-23T00:00:00+00:00"


@pytest.fixture()
def client():
    return TestClient(app)


def _seed_role(role_id="r1", name="Senior Developer"):
    appdb.insert("roles", {"id": role_id, "name": name, "description": "",
                           "active": 1, "created_at": TS, "updated_at": TS})


def _seed_project(pid="pr1", **over):
    if not appdb.query_one("SELECT id FROM teams WHERE id='t1'"):
        appdb.insert("teams", {"id": "t1", "name": "T", "created_at": TS})
    appdb.insert("projects", {
        "id": pid, "name": "P", "goal": "g", "description": "d",
        "technology_stack": "FastAPI", "workspace_path": "C:/nonexistent-ws",
        "team_id": "t1", "po_enabled": 0, "status": "Active",
        "lifecycle_state": over.pop("lifecycle_state", "Active"),
        "governance_enabled": over.pop("governance_enabled", 0),
        "budget_usd": over.pop("budget_usd", 0),
        "created_at": TS, "updated_at": TS, **over})


# ------------------------------------------------ instruction versions (2.4/4.3)

def test_instruction_versioning_and_restore(client):
    _seed_role()
    r = client.post("/api/v1/roles/r1/instructions",
                    json={"filename": "guidelines", "content": "V1 text"})
    assert r.status_code == 200
    iid = r.json()["id"]
    assert r.json()["version"] == 1
    assert "lint_warnings" in r.json()

    r = client.patch(f"/api/v1/instructions/{iid}", json={"content": "V2 text"})
    assert r.json()["version"] == 2

    versions = client.get(f"/api/v1/instructions/{iid}/versions").json()
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[1]["content_chars"] == len("V1 text")

    r = client.post(f"/api/v1/instructions/{iid}/restore", json={"version": 1})
    body = r.json()
    assert body["restored"] is True and body["restored_from"] == 1
    assert body["content"] == "V1 text" and body["version"] == 3  # append-only

    # Restoring the same content again is a no-op but reports it.
    body = client.post(f"/api/v1/instructions/{iid}/restore",
                       json={"version": 1}).json()
    assert body["restored"] is False

    assert client.post(f"/api/v1/instructions/{iid}/restore",
                       json={"version": 99}).status_code == 404
    assert client.post(f"/api/v1/instructions/{iid}/restore",
                       json={}).status_code == 400

    assert client.delete(f"/api/v1/instructions/{iid}").json() == {"ok": True}
    assert appdb.query_one(
        "SELECT COUNT(*) AS n FROM instruction_file_versions "
        "WHERE instruction_file_id=?", (iid,))["n"] == 0


def test_baseline_version_exists_for_every_instruction(client):
    _seed_role()
    iid = client.post("/api/v1/roles/r1/instructions",
                      json={"filename": "a.md", "content": "start"})\
        .json()["id"]
    client.patch(f"/api/v1/instructions/{iid}", json={"content": "changed"})
    versions = client.get(f"/api/v1/instructions/{iid}/versions").json()
    baseline = [v for v in versions if v["version"] == 1]
    assert baseline and baseline[0]["filename"].endswith("a.md")


def test_persona_restore_round_trip(client):
    _seed_role("r-dev", "Senior Developer")
    p = client.post("/api/v1/roles/r-dev/personas",
                    json={"name": "Ada", "instructions": "I1",
                          "constraints_text": "C1"}).json()
    client.patch(f"/api/v1/personas/{p['id']}",
                 json={"instructions": "I2", "constraints_text": "C2"})
    versions = client.get(f"/api/v1/personas/{p['id']}/versions").json()
    assert [v["version"] for v in versions] == [2, 1]
    r = client.post(f"/api/v1/personas/{p['id']}/restore",
                    json={"version": 1}).json()
    assert r["restored"] is True and r["instructions"] == "I1"
    assert r["constraints_text"] == "C1"


# ------------------------------------------------------------- lint (4.4)

def test_lint_size_warnings():
    assert lint_prompt("") == []
    assert lint_prompt("short and clear") == []
    w = lint_prompt("x" * 4500)
    assert len(w) == 1 and "growing" in w[0]
    w = lint_prompt("x" * 9000)
    assert len(w) == 1 and "recurring cost" in w[0]


def test_lint_unknown_skill_reference():
    appdb.insert("skills", {"id": "s1", "name": "Code Review", "description": "",
                            "content": "", "version": 1, "active": 1,
                            "created_at": TS, "updated_at": TS})
    w = lint_prompt("Always use the skill `Code Review` and skill named 'Ghost Tool' here.")
    assert any("ghost tool" in m.lower() for m in w)
    assert not any("code review" in m.lower() for m in w)


def test_lint_contradiction_pair():
    w = lint_prompt("You should ship fast and approve quickly, but also be thorough.")
    assert any("contradiction" in m.lower() for m in w)


def test_lint_warnings_surface_on_save(client):
    _seed_role()
    r = client.post("/api/v1/roles/r1/instructions",
                    json={"filename": "big.md",
                          "content": "ship fast, but be thorough. " + "y" * 5000})
    warnings = r.json()["lint_warnings"]
    assert len(warnings) >= 2


# ------------------------------------------------------- task templates (2.1)

def test_templates_load_and_fill():
    ids = {t["id"] for t in tt.list_templates()}
    assert {"api-endpoint", "unit-test", "frontend-component", "bug-fix"} <= ids
    applied = tt.apply_template("api-endpoint", {
        "feature": "comments", "method": "POST",
        "resource": "tickets/comments", "success_status": "201"})
    assert applied["title"] == "Implement comments endpoint"
    assert "POST /api/tickets/comments" in applied["description"]
    assert "201" in applied["acceptance_criteria"]
    assert applied["missing_variables"] == []
    assert applied["suggested_role"] == "Senior Developer"


def test_template_missing_variables():
    applied = tt.apply_template("api-endpoint", {})
    assert set(applied["missing_variables"]) == {
        "feature", "method", "resource", "success_status"}
    assert "[feature?]" in applied["title"]
    assert tt.apply_template("nope", {}) is None
    assert tt.get_template("../../etc/passwd") is None


def test_template_routes(client):
    templates = client.get("/api/v1/task-templates").json()
    assert len(templates) >= 4
    r = client.post("/api/v1/task-templates/bug-fix/preview",
                    json={"component": "login form"})
    assert r.status_code == 200
    assert r.json()["title"]
    assert client.post("/api/v1/task-templates/zzz/preview", json={}).status_code == 404


def test_task_creation_with_template(client):
    _seed_project()
    r = client.post("/api/v1/projects/pr1/tasks", json={
        "template_id": "api-endpoint",
        "template_variables": {"feature": "invoices", "method": "GET",
                               "resource": "invoices", "success_status": "200"}})
    assert r.status_code == 200, r.text
    task = r.json()
    assert task["title"] == "Implement invoices endpoint"
    assert "GET /api/invoices" in task["description"]
    assert task["acceptance_criteria"]

    # Missing variables with no explicit title is rejected.
    r = client.post("/api/v1/projects/pr1/tasks",
                    json={"template_id": "api-endpoint", "template_variables": {}})
    assert r.status_code == 400 and "Template variables missing" in r.text
    r = client.post("/api/v1/projects/pr1/tasks", json={"template_id": "ghost"})
    assert r.status_code == 400 and "Unknown task template" in r.text
    # Explicit title wins over the template title.
    r = client.post("/api/v1/projects/pr1/tasks", json={
        "title": "My own title", "template_id": "api-endpoint",
        "template_variables": {}})
    assert r.json()["title"] == "My own title"


# ---------------------------------------------------------- budget (3.3)

def _add_usage(run_id, project_id, cost, task_id=None, agent_id=None, model="m"):
    appdb.insert("workflow_runs", {"id": run_id, "project_id": project_id,
                                   "task_id": task_id, "agent_id": agent_id,
                                   "status": "Completed", "started_at": TS,
                                   "completed_at": TS})
    appdb.insert("usage_records", {"id": f"u-{run_id}", "workflow_run_id": run_id,
                                   "agent_id": agent_id, "model": model,
                                   "input_tokens": 100, "output_tokens": 50,
                                   "cost_estimate": cost, "created_at": TS})


def test_budget_status_and_may_spend():
    _seed_project(budget_usd=0)
    st = budget.budget_status("pr1")
    assert st["unlimited"] and not st["over"]
    assert budget.may_spend("pr1") == (True, "")

    appdb.execute("UPDATE projects SET budget_usd = 5 WHERE id='pr1'")
    _add_usage("wr1", "pr1", 2.0)
    assert budget.may_spend("pr1")[0] is True
    _add_usage("wr2", "pr1", 3.5)
    ok, reason = budget.may_spend("pr1")
    assert not ok and "budget exhausted" in reason and "$5.00" in reason


def test_start_execution_blocked_by_budget():
    from app import runtime
    _seed_project(budget_usd=1)
    appdb.insert("roles", {"id": "r-dev", "name": "Senior Developer",
                           "created_at": TS, "updated_at": TS})
    appdb.insert("personas", {"id": "p-dev", "role_id": "r-dev", "name": "dev",
                              "created_at": TS, "updated_at": TS})
    appdb.insert("agents", {"id": "a-dev", "name": "DEV", "role_id": "r-dev",
                            "persona_id": "p-dev", "created_at": TS, "updated_at": TS})
    appdb.insert("sprints", {"id": "s1", "project_id": "pr1", "name": "S1",
                             "status": "Active", "created_at": TS})
    appdb.insert("tasks", {"id": "tk1", "project_id": "pr1", "sprint_id": "s1",
                           "title": "T", "description": "", "acceptance_criteria": "",
                           "story_points": 1, "priority": 2, "status": "Ready",
                           "evidence": "", "assigned_agent_id": "a-dev",
                           "rework_count": 0, "created_at": TS, "updated_at": TS})
    _add_usage("wr1", "pr1", 5.0, task_id="tk1", agent_id="a-dev")
    res = runtime.start_execution("pr1", "tk1")
    assert "TASK_BLOCKED_BY_BUDGET" in res.get("error", "")
    assert runtime._eligible_tasks("pr1") == []
    res = runtime.start_sprint_execution("pr1")
    assert "SPRINT_BLOCKED_BY_BUDGET" in res.get("error", "")
    # Raising the budget unblocks everything again.
    appdb.execute("UPDATE projects SET budget_usd = 100 WHERE id='pr1'")
    assert [t["id"] for t in runtime._eligible_tasks("pr1")] == ["tk1"]


def test_budget_patch_validation(client):
    _seed_project()
    r = client.patch("/api/v1/projects/pr1", json={"budget_usd": 42.5})
    assert r.json()["budget_usd"] == 42.5
    r = client.patch("/api/v1/projects/pr1", json={"budget_usd": -5})
    assert r.json()["budget_usd"] == 0  # clamped, negative budgets are meaningless
    assert client.patch("/api/v1/projects/pr1",
                        json={"budget_usd": "abc"}).status_code in (400, 422)


# ------------------------------------------------------- costs endpoint (3.5)

def test_project_costs_rollup(client):
    _seed_project(budget_usd=10)
    _seed_role("r-dev", "Senior Developer")
    appdb.insert("personas", {"id": "p-dev", "role_id": "r-dev", "name": "d",
                              "created_at": TS, "updated_at": TS})
    appdb.insert("agents", {"id": "a-dev", "name": "DEV", "role_id": "r-dev",
                            "persona_id": "p-dev", "created_at": TS, "updated_at": TS})
    appdb.insert("sprints", {"id": "s1", "project_id": "pr1", "name": "S1",
                             "status": "Active", "created_at": TS})
    appdb.insert("sprints", {"id": "s2", "project_id": "pr1", "name": "S2",
                             "status": "Future", "created_at": TS})
    for tid, sid in (("tk1", "s1"), ("tk2", "s2")):
        appdb.insert("tasks", {"id": tid, "project_id": "pr1", "sprint_id": sid,
                               "title": tid, "status": "Done", "description": "",
                               "acceptance_criteria": "", "story_points": 1,
                               "priority": 2, "evidence": "", "rework_count": 0,
                               "assigned_agent_id": "a-dev",
                               "created_at": TS, "updated_at": TS})
    _add_usage("wr1", "pr1", 1.0, task_id="tk1", agent_id="a-dev", model="gpt-big")
    _add_usage("wr2", "pr1", 0.5, task_id="tk1", agent_id="a-dev", model="")
    _add_usage("wr3", "pr1", 4.0, task_id="tk2", agent_id="a-dev", model="gpt-big")

    by_role = client.get("/api/v1/projects/pr1/costs").json()
    assert by_role["by"] == "role" and by_role["budget_usd"] == 10
    assert by_role["items"][0]["role"] == "Senior Developer"
    assert by_role["items"][0]["cost_usd"] == 5.5

    by_model = client.get("/api/v1/projects/pr1/costs?by=model").json()
    labels = {i["model"]: i["cost_usd"] for i in by_model["items"]}
    assert labels["gpt-big"] == 5.0 and labels["(scaffold)"] == 0.5

    sprint1 = client.get("/api/v1/projects/pr1/costs?by=agent&sprint_id=s1").json()
    assert sprint1["items"][0]["agent"] == "DEV"
    assert sprint1["items"][0]["cost_usd"] == 1.5

    assert client.get("/api/v1/projects/ghost/costs").status_code == 404
    assert client.get("/api/v1/projects/pr1/costs?by=nonsense").status_code == 422


# --------------------------------------------------------- playground (4.2)

def _seed_agent(persona_instructions="ALWAYS hum a tune before coding"):
    appdb.insert("teams", {"id": "t1", "name": "T", "created_at": TS})
    _seed_role("r-dev", "Senior Developer")
    appdb.insert("personas", {"id": "p1", "role_id": "r-dev", "name": "Ada",
                              "instructions": persona_instructions,
                              "constraints_text": "No globals", "version": 1,
                              "active": 1, "created_at": TS, "updated_at": TS})
    appdb.insert("agents", {"id": "a1", "name": "Dev", "role_id": "r-dev",
                            "persona_id": "p1", "created_at": TS, "updated_at": TS})


def test_playground_preview(client):
    r = client.post("/api/v1/playground/preview",
                    json={"stack": "python"}).json()
    assert r["system_prompt"].strip()
    assert r["system_chars"] == len(r["system_prompt"])
    assert r["user_prompt"]

    _seed_agent()
    r = client.post("/api/v1/playground/preview",
                    json={"agent_id": "a1"}).json()
    assert "ALWAYS hum a tune" in r["system_prompt"]
    assert "No globals" in r["system_prompt"]

    r = client.post("/api/v1/playground/preview",
                    json={"system_prompt": "CUSTOM", "prompt": "hi"}).json()
    assert r["system_prompt"] == "CUSTOM" and r["user_prompt"] == "hi"

    assert client.post("/api/v1/playground/preview",
                       json={"agent_id": "ghost"}).status_code == 404


def test_playground_run_dry_and_no_gateway(client, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    r = client.post("/api/v1/playground/run",
                    json={"system_prompt": "s", "prompt": "u"})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert json.loads(body["text"])["files"] == []

    monkeypatch.delenv("DRY_RUN")
    r = client.post("/api/v1/playground/run", json={"prompt": "u"})
    assert r.status_code == 400 and "DRY_RUN" in r.text


# ------------------------------------- mid-run budget enforcement (3.3 hardening)

def test_inflight_ledger_counts_toward_budget():
    _seed_project(budget_usd=1)
    assert budget.headroom_usd("pr1") == 1
    budget.add_inflight("pr1", 0.5)
    st = budget.budget_status("pr1")
    assert st["inflight_usd"] == 0.5 and not st["over"]
    assert budget.headroom_usd("pr1") == 0.5
    budget.add_inflight("pr1", 0.6)
    ok, reason = budget.may_spend("pr1")
    assert not ok and "budget exhausted" in reason
    budget.clear_inflight("pr1")
    assert budget.may_spend("pr1")[0] and budget.inflight_usd("pr1") == 0
    assert budget.headroom_usd("prU") is None  # unlimited when no cap is set


def test_charge_llm_call_rules():
    _seed_project("prC", budget_usd=10)
    # dry-run replies are free; models without per-million rates fall back
    # to the global config defaults.
    codegen._charge_llm_call({"id": "prC"}, {}, {"dry_run": True, "input_tokens": 9999999})
    assert budget.inflight_usd("prC") == 0
    codegen._charge_llm_call({"id": "prC"},
                             {"input_cost_per_m": 2.0, "output_cost_per_m": 4.0},
                             {"input_tokens": 1_000_000, "output_tokens": 500_000})
    assert budget.inflight_usd("prC") == 4.0
    codegen._charge_llm_call(None, {}, {"input_tokens": 5, "output_tokens": 5})
    assert budget.inflight_usd("prC") == 4.0


def test_output_budget_shrinks_with_headroom():
    _seed_project("pr-cap", budget_usd=5)
    task = {"title": "Build a dashboard", "description": "new endpoint", "points": 8}
    assert codegen._output_budget(task, "pr-cap") == 8000      # full headroom
    budget.add_inflight("pr-cap", 1.0)
    assert codegen._output_budget(task, "pr-cap") == 4000      # under $5 left
    budget.add_inflight("pr-cap", 3.05)
    assert codegen._output_budget(task, "pr-cap") == 1500      # under $1 left
    # Uncapped projects keep the generous envelope.
    assert codegen._output_budget(task, "pr-none") == 8000


def test_midrun_budget_stops_chain(tmp_path, monkeypatch):
    # Member 1 burns the whole $1 budget in one call; the chain must stop
    # before trying member 2 instead of doubling the spend mid-run.
    _seed_project("pr-mid", budget_usd=1)
    pr = {"id": "pr-mid", "name": "P", "goal": "", "technology_stack": "Python",
          "workspace_path": str(tmp_path)}
    task = {"title": "Build a dashboard", "description": "", "points": 8,
            "acceptance_criteria": "", "id": "tk-mid"}
    entries = [
        {"ref": "gw:m1", "gateway_id": "gw", "model_id": "m1",
         "gw": {"id": "gw", "name": "G"},
         "model": {"id": "m1", "provider_model_id": "m1",
                   "input_cost_per_m": 1.0, "output_cost_per_m": 0}},
        {"ref": "gw:m2", "gateway_id": "gw", "model_id": "m2",
         "gw": {"id": "gw", "name": "G"},
         "model": {"id": "m2", "provider_model_id": "m2",
                   "input_cost_per_m": 1.0, "output_cost_per_m": 0}},
    ]
    monkeypatch.setattr(codegen.toolchains, "detect_stack", lambda *args: "python")
    monkeypatch.setattr(codegen, "_model_chain", lambda *args: (entries, "Developer"))
    monkeypatch.setattr(codegen.memory, "pitfalls_block", lambda *args: "")
    monkeypatch.setattr(codegen, "_ensure_helpers", lambda *args: None, raising=False)
    calls = []

    def fake_llm(*args, **kwargs):
        calls.append(kwargs)
        return {"text": "not json at all", "input_tokens": 1_000_000,
                "output_tokens": 0}
    monkeypatch.setattr(codegen, "call_llm", fake_llm)
    result = codegen.generate_implementation(pr, task, "agent")
    # member 1 got its call + the format retry; member 2 was never tried.
    assert len(calls) == 2
    assert "budget exhausted" in result["error"]
    assert any("budget exhausted" in str(f) for f in result["fallbacks"])


def test_review_gate_skips_when_budget_exhausted(monkeypatch):
    _seed_project("pr-rv", budget_usd=1)
    budget.add_inflight("pr-rv", 2.0)
    monkeypatch.setattr(codegen, "resolve_llm",
                        lambda p: pytest.fail("resolve_llm must not run"))
    out = codegen.review_verdict({"id": "pr-rv", "workspace_path": "."}, {},
                                 "sa", "agent")
    assert out["decision"] == "unavailable"
    assert "budget exhausted" in out["findings"]
