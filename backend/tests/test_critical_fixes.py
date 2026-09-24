"""Critical-tier fixes from the 2026-09-23 full-app audit:
dependency integrity (cycles/rollback), FK-complete deletes with 409
guards, LLM outage classification, and /api/v1/health + JSON API 404s."""
import types

import pytest
from fastapi import HTTPException

from app import db as appdb
from app.codegen import _llm_outage_reason
from app.services import tasks as task_svc

TS = "2026-09-23T00:00:00+00:00"


def _seed_org():
    appdb.insert("roles", {"id": "r-dev", "name": "Senior Developer",
                           "created_at": TS, "updated_at": TS})
    appdb.insert("personas", {"id": "p-dev", "role_id": "r-dev", "name": "dev",
                              "created_at": TS, "updated_at": TS})
    appdb.insert("teams", {"id": "t1", "name": "Team 1", "created_at": TS})
    appdb.insert("agents", {"id": "a1", "name": "A1", "role_id": "r-dev",
                            "persona_id": "p-dev", "created_at": TS, "updated_at": TS})
    appdb.insert("projects", {"id": "pr1", "name": "P", "goal": "", "description": "",
                              "workspace_path": "C:/nonexistent-ws", "team_id": "t1",
                              "status": "Active", "created_at": TS, "updated_at": TS})


def _task(tid, status="Todo"):
    appdb.insert("tasks", {"id": tid, "project_id": "pr1", "title": tid,
                           "description": "", "acceptance_criteria": "",
                           "story_points": 1, "priority": 2, "status": status,
                           "created_at": TS, "updated_at": TS})


# ---------------- dependency integrity ----------------

def test_self_dependency_rejected():
    _seed_org()
    _task("t1")
    with pytest.raises(HTTPException) as ei:
        task_svc.validate_new_dependency("t1", "t1")
    assert ei.value.status_code == 400


def test_unknown_dependency_rejected():
    _seed_org()
    _task("t1")
    with pytest.raises(HTTPException) as ei:
        task_svc.validate_new_dependency("t1", "ghost")
    assert ei.value.status_code == 404


def test_dependency_cycle_rejected():
    _seed_org()
    for t in ("t1", "t2", "t3"):
        _task(t)
    # t1 depends on t2, t2 depends on t3 — closing t3 -> t1 is a cycle.
    for a, b in (("t1", "t2"), ("t2", "t3")):
        appdb.execute("INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?,?)", (a, b))
    assert task_svc.dependency_cycle_exists("t3", "t1")
    with pytest.raises(HTTPException) as ei:
        task_svc.validate_new_dependency("t3", "t1")
    assert ei.value.status_code == 400
    assert "Circular dependency" in ei.value.detail
    # A legitimate forward edge is still accepted.
    _task("t4")
    task_svc.validate_new_dependency("t4", "t1")


def test_create_task_bad_dep_rolls_back_without_fk_error():
    """A bad dep mid-list used to leave task_dependencies rows behind, so
    the rollback DELETE blew up with FOREIGN KEY constraint failed (500)."""
    _seed_org()
    _task("good")
    body = types.SimpleNamespace(
        sprint_id=None, backlog_item_id=None, title="T", description="",
        acceptance_criteria="", story_points=1, priority=2, assigned_agent_id=None,
        depends_on=["good", "ghost"])
    with pytest.raises(HTTPException) as ei:
        task_svc.create_task_row("pr1", body, sprint_scope_check=False)
    assert ei.value.status_code == 400
    assert appdb.query_one("SELECT id FROM tasks WHERE id='ghost'") is None
    # The partially-created task and its dep rows are fully gone.
    assert appdb.query_one(
        "SELECT COUNT(*) AS n FROM task_dependencies WHERE depends_on_task_id='good'")["n"] == 0


# ---------------- FK-complete deletes ----------------

def test_delete_task_clears_governance_references():
    from app.routers import tasks as tasks_router
    _seed_org()
    _task("t1")
    _task("t2")
    appdb.execute("INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES ('t1','t2')")
    appdb.insert("task_reviews", {"id": "rev1", "task_id": "t1", "project_id": "pr1",
                                  "reviewer_type": "sa", "reviewer_agent_id": "a1",
                                  "created_at": TS})
    appdb.insert("task_requirements", {"task_id": "t1", "requirement_id": "R-1"})
    appdb.insert("workflow_runs", {"id": "wr1", "project_id": "pr1", "task_id": "t1",
                                   "agent_id": "a1", "status": "Done", "started_at": TS})
    appdb.insert("dependency_requests", {"id": "dr1", "project_id": "pr1", "code": "DEP-1",
                                         "package": "pkg", "requested_by_agent_id": "a1",
                                         "requested_for_task_id": "t1", "created_at": TS,
                                         "updated_at": TS})
    appdb.insert("agent_messages", {"id": "am1", "project_id": "pr1", "from_agent_id": "a1",
                                    "to_agent_id": "a1", "message_type": "note",
                                    "subject": "s", "content": "c", "related_task_id": "t1",
                                    "created_at": TS})
    appdb.insert("change_requests", {"id": "cr1", "project_id": "pr1", "code": "CR-1",
                                     "title": "t", "origin_task_id": "t1",
                                     "created_at": TS, "updated_at": TS})
    tasks_router.delete_task("t1")
    assert appdb.query_one("SELECT id FROM tasks WHERE id='t1'") is None
    assert appdb.query_one("SELECT id FROM task_reviews WHERE id='rev1'") is None
    # The run keeps history but its FK is neutralised.
    assert appdb.query_one("SELECT task_id FROM workflow_runs WHERE id='wr1'")["task_id"] is None


def test_delete_project_is_fk_complete():
    from app.routers import projects as projects_router
    _seed_org()
    _task("t1")
    appdb.insert("workflow_runs", {"id": "wr1", "project_id": "pr1", "task_id": "t1",
                                   "status": "Done", "started_at": TS})
    appdb.insert("usage_records", {"id": "ur1", "workflow_run_id": "wr1", "model": "m",
                                   "input_tokens": 1, "output_tokens": 1,
                                   "created_at": TS})
    appdb.insert("task_reviews", {"id": "rev1", "task_id": "t1", "project_id": "pr1",
                                  "reviewer_type": "ba", "created_at": TS})
    appdb.insert("project_memory", {"id": "pm1", "project_id": "pr1", "memory_type": "pitfall",
                                    "subject": "s", "content": "c", "owner_agent_id": "a1",
                                    "created_at": TS, "updated_at": TS})
    appdb.insert("agent_messages", {"id": "am1", "project_id": "pr1", "message_type": "note",
                                    "subject": "s", "content": "c", "from_agent_id": "a1",
                                    "to_agent_id": "a1", "created_at": TS})
    appdb.insert("conversations", {"id": "cv1", "project_id": "pr1", "title": "t",
                                   "created_at": TS, "updated_at": TS})
    appdb.insert("messages", {"id": "msg1", "conversation_id": "cv1", "role": "user",
                              "content": "hi", "created_at": TS})
    projects_router.delete_project("pr1")
    assert appdb.query_one("SELECT id FROM projects WHERE id='pr1'") is None
    assert appdb.query_one("SELECT id FROM usage_records WHERE id='ur1'") is None
    assert appdb.query_one("SELECT id FROM agent_messages WHERE id='am1'") is None


def test_delete_guards_return_409():
    from app.routers import agents as agents_router
    from app.routers import gateways as gateways_router
    from app.routers import roles as roles_router
    _seed_org()
    appdb.insert("gateways", {"id": "gw1", "name": "GW", "provider": "openai",
                              "base_url": "https://x.test/v1", "api_type": "openai-chat",
                              "status": "Active", "created_at": TS, "updated_at": TS})
    appdb.insert("gateway_models", {"id": "gm1", "gateway_id": "gw1",
                                    "provider_model_id": "gpt-x", "display_name": "GPT-X"})
    appdb.insert("model_bindings", {"id": "mb1", "role_id": "r-dev", "gateway_id": "gw1",
                                    "model_id": "gm1"})
    appdb.execute("UPDATE agents SET model_binding_id='mb1' WHERE id='a1'")
    with pytest.raises(HTTPException) as ei:
        gateways_router.delete_gateway("gw1")
    assert ei.value.status_code == 409
    with pytest.raises(HTTPException) as ei:
        gateways_router.delete_model("gw1", "gm1")
    assert ei.value.status_code == 409
    with pytest.raises(HTTPException) as ei:
        roles_router.delete_binding("mb1")
    assert ei.value.status_code == 409
    # Agent owning an open task cannot vanish under the task's FK.
    _task("t1", status="In Progress")
    appdb.execute("UPDATE tasks SET assigned_agent_id='a1' WHERE id='t1'")
    with pytest.raises(HTTPException) as ei:
        agents_router.delete_agent("a1")
    assert ei.value.status_code == 409
    # Done tasks don't block deletion, and the FK is cleared.
    appdb.execute("UPDATE tasks SET status='Done' WHERE id='t1'")
    agents_router.delete_agent("a1")
    assert appdb.query_one("SELECT assigned_agent_id FROM tasks WHERE id='t1'")["assigned_agent_id"] is None


# ---------------- LLM outage classification ----------------

@pytest.mark.parametrize("msg", [
    "Gateway returned HTTP 429: Too Many Requests",
    "Gateway returned HTTP 503: Service Unavailable",
    "Gateway returned HTTP 502: bad gateway",
    "Could not reach gateway at https://api.x.test after 3 attempts",
    "Gateway circuit breaker open; retry later",
    "Request timed out after 300s",
    "LLM usage limit reached for this org",
])
def test_outage_messages_are_classified(msg):
    assert _llm_outage_reason(msg) != ""


@pytest.mark.parametrize("msg", [
    "",
    "I reviewed the files but could not find the module you mentioned.",
    "```json\n{}\n```",
])
def test_normal_replies_are_not_outages(msg):
    assert _llm_outage_reason(msg) == ""


# ---------------- health route + API 404s ----------------

def test_health_route_and_api_404_json():
    from fastapi.testclient import TestClient
    from app.main import app
    # Built without the context manager: lifespan (init_db/PO resume) stays off.
    client = TestClient(app)
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    r = client.get("/api/v1/definitely-not-a-route")
    assert r.status_code == 404
    assert r.json()["detail"] == "Not Found"
