"""Task transition validation and task creation helpers.

Used by both the REST routers and the chatbot command handler so
business rules live in one place.
"""
from __future__ import annotations

from fastapi import HTTPException

from .. import db, runtime, sprint_gate
from ..db import audit, execute, insert, new_id, now, query, query_one, update


TASK_TRANSITIONS: dict[str, set[str]] = {
    "Todo": {"Ready", "Cancelled"},
    "Ready": {"In Progress", "Todo", "Cancelled"},
    "In Progress": {"Blocked", "Review", "Testing", "Waiting QA", "SA Review", "BA Review", "Cancelled"},
    "Blocked": {"Ready", "Todo", "Rework", "Cancelled"},
    "Review": {"Testing", "In Progress", "Done", "Waiting QA", "SA Review", "BA Review", "Cancelled"},
    "Testing": {"Done", "In Progress", "Review", "Waiting QA", "SA Review", "BA Review", "Rework", "Cancelled"},
    "SA Review": {"BA Review", "Waiting QA", "Testing", "Done", "Rework", "Blocked", "Cancelled"},
    "BA Review": {"Waiting QA", "Testing", "Done", "Rework", "Blocked", "Cancelled"},
    "Waiting QA": {"Testing", "Done", "Rework", "Blocked", "Cancelled"},
    "Rework": {"In Progress", "Ready", "Todo", "Blocked", "Cancelled"},
    "Done": set(),
    "Cancelled": {"Todo"},
}


def validate_task_transition(task: dict, new_status: str) -> None:
    """Raise HTTPException 409 if the status change is not allowed."""
    if new_status not in TASK_TRANSITIONS.get(task["status"], set()):
        raise HTTPException(
            409, f"Invalid transition {task['status']} -> {new_status}"
        )
    if new_status == "In Progress":
        ok, unmet = runtime.dependencies_satisfied(task["id"])
        if not ok:
            names = ", ".join(d["title"] for d in unmet)
            raise HTTPException(409, f"Dependencies incomplete: {names}")


def emit_task_status_change(task: dict, new_status: str) -> None:
    db.emit_event(
        task["project_id"],
        "task.status_changed",
        {"task_id": task["id"], "task": task["title"], "status": new_status},
        task_id=task["id"],
    )


def record_task_assignment(task: dict, agent_id: str) -> None:
    agent = query_one("SELECT name FROM agents WHERE id=?", (agent_id,))
    audit(
        "assign_task",
        "task",
        task["id"],
        f"Assigned '{task['title']}' to {agent['name'] if agent else '?'}",
    )


def create_task_row(project_id: str, body, *, sprint_scope_check: bool = True) -> dict:
    """Insert a new task and return the created row.

    When *sprint_scope_check* is True (default) and the task is urgent
    (priority==1) in an active sprint, the sprint may be re-opened
    mid-validation.
    """
    tid = new_id()
    ts = now()
    insert("tasks", {
        "id": tid,
        "project_id": project_id,
        "sprint_id": body.sprint_id,
        "backlog_item_id": body.backlog_item_id,
        "title": body.title,
        "description": body.description,
        "acceptance_criteria": body.acceptance_criteria,
        "story_points": body.story_points,
        "priority": body.priority,
        "assigned_agent_id": body.assigned_agent_id,
        "status": "Todo",
        "created_at": ts,
        "updated_at": ts,
    })
    for dep in body.depends_on:
        execute(
            "INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_task_id) VALUES (?,?)",
            (tid, dep),
        )
    if sprint_scope_check and body.sprint_id and body.priority == 1:
        sprint = query_one(
            "SELECT * FROM sprints WHERE id = ? AND project_id = ?",
            (body.sprint_id, project_id),
        )
        if sprint and sprint["status"] in sprint_gate.GATE_PHASES + (sprint_gate.REWORK,):
            ok, _ = sprint_gate.reopen_sprint_for_scope(
                body.sprint_id,
                f"urgent task '{body.title[:60]}' added",
            )
            if ok:
                audit(
                    "sprint_reopen_task",
                    "task",
                    tid,
                    f"Urgent task re-opened sprint '{sprint['name']}' mid-validation",
                )
    return query_one("SELECT * FROM tasks WHERE id = ?", (tid,))


def list_tasks_with_deps(
    project_id: str, sprint_id: str | None = None
) -> list[dict]:
    """Return tasks with resolved dependency arrays."""
    sql = (
        "SELECT t.*, a.name AS agent_name, d.deps FROM tasks t "
        "LEFT JOIN agents a ON a.id = t.assigned_agent_id "
        "LEFT JOIN (SELECT task_id, GROUP_CONCAT(depends_on_task_id) AS deps "
        "  FROM task_dependencies GROUP BY task_id) d "
        "ON d.task_id = t.id WHERE t.project_id = ?"
    )
    params: list = [project_id]
    if sprint_id:
        sql += " AND t.sprint_id = ?"
        params.append(sprint_id)
    tasks = query(sql + " ORDER BY t.priority, t.created_at", params)
    for t in tasks:
        dep_ids = t.pop("deps", None)
        t["dependencies"] = []
        t["unmet_dependencies"] = []
        if dep_ids:
            for did in dep_ids.split(","):
                dep = query_one(
                    "SELECT id, title, status FROM tasks WHERE id=?", (did,)
                )
                if dep:
                    t["dependencies"].append(dep)
                    if dep["status"] not in ("Done", "Cancelled"):
                        t["unmet_dependencies"].append(dep)
    return tasks


def update_task_row(task_id: str, body: TaskUpdate) -> dict:
    """Apply an update to a task with transition validation and events."""
    task = query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise HTTPException(404, "Task not found")
    data = body.model_dump(exclude_unset=True)
    allowed = {k: v for k, v in data.items() if k in ("title", "description", "acceptance_criteria",
                                                      "story_points", "priority", "sprint_id",
                                                      "backlog_item_id", "assigned_agent_id",
                                                      "status", "blocked_reason")}
    new_status = allowed.get("status")
    if new_status and new_status != task["status"]:
        validate_task_transition(task, new_status)
        emit_task_status_change(task, new_status)
    if "assigned_agent_id" in allowed and allowed.get("assigned_agent_id"):
        record_task_assignment(task, allowed["assigned_agent_id"])
    allowed["updated_at"] = now()
    update("tasks", task_id, allowed)
    return query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
