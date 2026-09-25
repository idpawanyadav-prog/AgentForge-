"""Task transition validation and task creation helpers.

Used by both the REST routers and the chatbot command handler so
business rules live in one place.
"""
from __future__ import annotations

from fastapi import HTTPException

from .. import db, runtime, sprint_gate
from ..db import audit, execute, insert, new_id, now, query, query_one, update
from ..schemas import TaskUpdate


TASK_TRANSITIONS: dict[str, set[str]] = {
    "Todo": {"Ready", "Cancelled"},
    "Ready": {"In Progress", "Todo", "Cancelled"},
    "In Progress": {"Blocked", "Review", "Testing", "Waiting QA", "Code Review", "SA Review", "BA Review", "Cancelled"},
    "Blocked": {"Ready", "Todo", "Rework", "Cancelled"},
    "Review": {"Testing", "In Progress", "Done", "Waiting QA", "Code Review", "SA Review", "BA Review", "Cancelled"},
    "Testing": {"Done", "In Progress", "Review", "Waiting QA", "Code Review", "SA Review", "BA Review", "Rework", "Cancelled"},
    "Code Review": {"SA Review", "BA Review", "Waiting QA", "Testing", "Done", "Rework", "Blocked", "Cancelled"},
    "SA Review": {"BA Review", "Waiting QA", "Testing", "Done", "Rework", "Blocked", "Cancelled"},
    "BA Review": {"Waiting QA", "Testing", "Done", "Rework", "Blocked", "Cancelled"},
    "Waiting QA": {"Testing", "Done", "Rework", "Blocked", "Cancelled"},
    "Rework": {"In Progress", "Ready", "Todo", "Blocked", "Cancelled"},
    "Done": set(),
    "Cancelled": {"Todo"},
}


# Dependency-integrity helpers (shared by the REST router, task creation and
# the chatbot command path). Without a cycle check, one cyclic dependency
# silently freezes every downstream task: the scheduler skips tasks whose
# deps are unmet and they never become Done, so the sprint never drains.
def _dependency_graph() -> dict[str, list[str]]:
    graph: dict[str, list[str]] = {}
    for r in db.query("SELECT task_id, depends_on_task_id FROM task_dependencies"):
        graph.setdefault(r["task_id"], []).append(r["depends_on_task_id"])
    return graph


def dependency_cycle_exists(task_id: str, dep_id: str) -> bool:
    """True when adding `task_id depends_on dep_id` closes a cycle, i.e.
    dep_id already (transitively) depends on task_id."""
    graph = _dependency_graph()
    stack, seen = [dep_id], set()
    while stack:
        node = stack.pop()
        if node == task_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, []))
    return False


def validate_new_dependency(task_id: str, dep_id: str) -> None:
    if task_id == dep_id:
        raise HTTPException(400, "A task cannot depend on itself")
    task = db.query_one("SELECT project_id FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise HTTPException(404, "Task not found")
    dependency = db.query_one("SELECT project_id FROM tasks WHERE id=?", (dep_id,))
    if not dependency:
        raise HTTPException(404, f"Dependency task not found: {dep_id}")
    if dependency["project_id"] != task["project_id"]:
        raise HTTPException(400, "Dependency task belongs to another project")
    if dependency_cycle_exists(task_id, dep_id):
        raise HTTPException(
            400, f"Circular dependency rejected: '{dep_id}' already depends on this task")


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
    if not query_one("SELECT id FROM projects WHERE id=?", (project_id,)):
        raise HTTPException(404, "Project not found")
    template_id = getattr(body, "template_id", None)
    if template_id:
        from .. import task_templates as tt
        applied = tt.apply_template(template_id,
                                    getattr(body, "template_variables", None) or {})
        if applied is None:
            raise HTTPException(400, f"Unknown task template: {template_id}")
        if not (body.title or "").strip():
            if applied["missing_variables"]:
                raise HTTPException(
                    400, "Template variables missing: " + ", ".join(applied["missing_variables"]))
            body.title = applied["title"]
        if not body.description:
            body.description = applied["description"]
        if not body.acceptance_criteria:
            body.acceptance_criteria = applied["acceptance_criteria"]
        # Only adopt template defaults where the caller left plain defaults.
        if body.story_points == 3 and applied["story_points"] != 3:
            body.story_points = applied["story_points"]
        if body.priority == 2 and applied["priority"] != 2:
            body.priority = applied["priority"]
    if not (body.title or "").strip():
        raise HTTPException(400, "Task title is required (directly or via template)")
    _validate_project_refs(project_id, body.sprint_id, body.backlog_item_id)
    for dep in body.depends_on:
        dependency = query_one("SELECT project_id FROM tasks WHERE id=?", (dep,))
        if not dependency:
            raise HTTPException(400, f"Unknown dependency task: {dep}")
        if dependency["project_id"] != project_id:
            raise HTTPException(400, "Dependency task belongs to another project")
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
    _validate_project_refs(task["project_id"], allowed.get("sprint_id"),
                           allowed.get("backlog_item_id"))
    new_status = allowed.get("status")
    if new_status and new_status != task["status"]:
        validate_task_transition(task, new_status)
        emit_task_status_change(task, new_status)
    if "assigned_agent_id" in allowed and allowed.get("assigned_agent_id"):
        record_task_assignment(task, allowed["assigned_agent_id"])
    allowed["updated_at"] = now()
    update("tasks", task_id, allowed)
    return query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))


def _validate_project_refs(project_id: str, sprint_id: str | None,
                           backlog_item_id: str | None) -> None:
    for table, ref_id, label in (("sprints", sprint_id, "Sprint"),
                                 ("backlog_items", backlog_item_id, "Backlog item")):
        if ref_id and not query_one(f"SELECT id FROM {table} WHERE id=? AND project_id=?",
                                    (ref_id, project_id)):
            raise HTTPException(400, f"{label} does not belong to this project")
