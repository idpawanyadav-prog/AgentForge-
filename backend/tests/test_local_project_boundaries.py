"""Single-user local app: project IDs must not mix related records."""
from fastapi.testclient import TestClient

from app import db, runtime
from app.main import app


TS = "2026-09-24T00:00:00+00:00"


def _seed():
    for pid in ("p1", "p2"):
        db.insert("projects", {"id": pid, "name": pid, "goal": "", "description": "",
                               "workspace_path": "", "status": "Active",
                               "created_at": TS, "updated_at": TS})
        db.insert("sprints", {"id": f"s-{pid}", "project_id": pid,
                              "name": f"Sprint {pid}", "created_at": TS})
        db.insert("backlog_items", {"id": f"b-{pid}", "project_id": pid,
                                    "title": f"Backlog {pid}", "created_at": TS})
        db.insert("tasks", {"id": f"t-{pid}", "project_id": pid,
                            "title": f"Task {pid}", "created_at": TS,
                            "updated_at": TS})
        db.insert("conversations", {"id": f"c-{pid}", "project_id": pid,
                                    "title": pid, "created_at": TS,
                                    "updated_at": TS})


def test_task_links_must_stay_within_project():
    _seed()
    client = TestClient(app)
    base = {"title": "New task"}
    for field, value in (("sprint_id", "s-p2"),
                         ("backlog_item_id", "b-p2"),
                         ("depends_on", ["t-p2"])):
        response = client.post("/api/v1/projects/p1/tasks", json={**base, field: value})
        assert response.status_code == 400, (field, response.text)
    assert db.query_one("SELECT COUNT(*) AS n FROM tasks WHERE project_id='p1'")["n"] == 1

    for field, value in (("sprint_id", "s-p2"), ("backlog_item_id", "b-p2")):
        response = client.patch("/api/v1/tasks/t-p1", json={field: value})
        assert response.status_code == 400, (field, response.text)
    response = client.post("/api/v1/tasks/t-p1/dependencies/t-p2")
    assert response.status_code == 400
    assert db.query_one("SELECT COUNT(*) AS n FROM task_dependencies")["n"] == 0

    response = client.post("/api/v1/projects/p1/tasks", json={
        **base, "sprint_id": "s-p1", "backlog_item_id": "b-p1", "depends_on": ["t-p1"]})
    assert response.status_code == 200, response.text


def test_conversation_message_route_checks_project():
    _seed()
    client = TestClient(app)
    response = client.post("/api/v1/projects/p1/conversations/c-p2/messages",
                           json={"content": "hello"})
    assert response.status_code == 404
    assert db.query_one("SELECT COUNT(*) AS n FROM messages")["n"] == 0


def test_execution_claim_checks_task_project():
    _seed()
    result = runtime.start_execution("p1", "t-p2")
    assert result == {"error": "Task does not belong to project"}
    assert db.query_one("SELECT COUNT(*) AS n FROM workflow_runs")["n"] == 0
