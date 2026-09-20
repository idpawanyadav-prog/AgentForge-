"""Project, backlog, and sprint routes."""
from __future__ import annotations

import json as _json
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from .. import sprint_gate, workspace
from ..db import audit, execute, insert, new_id, now, query, query_one, run_idempotent, update
from ..schemas import BacklogUpdate, ProjectUpdate, SprintUpdate, TaskUpdate

router = APIRouter(prefix="/api/v1", tags=["projects"])


def _or_404(row, what="Resource"):
    if row is None:
        raise HTTPException(404, f"{what} not found")
    return row


class ProjectIn(BaseModel):
    name: str
    goal: str = ""
    description: str = ""
    technology_stack: str = ""
    repository_url: str = ""
    workspace_path: str = ""
    default_gateway_id: str | None = None
    team_id: str | None = None


class BacklogIn(BaseModel):
    title: str
    description: str = ""
    priority: int = 2
    acceptance_criteria: str = ""
    story_points: int = 3


class SprintIn(BaseModel):
    name: str
    goal: str = ""
    capacity: int = 40
    start_at: str | None = None
    end_at: str | None = None


# ------------------ projects ------------------

@router.get("/projects")
def list_projects(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    total = query_one("SELECT COUNT(*) AS n FROM projects")["n"]
    items = query(
        "SELECT p.*, t.name AS team_name FROM projects p "
        "LEFT JOIN teams t ON t.id = p.team_id ORDER BY p.created_at LIMIT ? OFFSET ?",
        (limit, offset))
    return {"total": total, "items": items, "limit": limit, "offset": offset}


@router.post("/projects")
def create_project(body: ProjectIn, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")):
    err = workspace.validate_workspace_path(body.workspace_path)
    if err:
        raise HTTPException(400, err)

    def _create():
        pid = new_id()
        ts = now()
        insert("projects", {"id": pid, "name": body.name, "goal": body.goal,
                            "description": body.description, "technology_stack": body.technology_stack,
                            "repository_url": body.repository_url, "workspace_path": body.workspace_path,
                            "default_gateway_id": body.default_gateway_id, "team_id": body.team_id,
                            "status": "Active", "created_at": ts, "updated_at": ts})
        project = query_one("SELECT * FROM projects WHERE id = ?", (pid,))
        ws = workspace.prepare_workspace(project)
        _emit = __import__("app.db", fromlist=["emit_event"]).emit_event
        _seq = _emit(pid, "project.workspace_ready", {
            "workspace_path": ws["workspace_path"], "cloned": ws["cloned"],
            "git_init": ws["git_init"], "note": ws["note"],
            "source": "git-clone" if ws["cloned"] else "local-projects-folder",
        })
        audit("create_project", "project", pid,
              f"Created project '{body.name}' with workspace at {ws['workspace_path']}")
        return {**project, "workspace_path": ws["workspace_path"], "_seq": _seq}

    return run_idempotent(idempotency_key, "POST /projects", _create)


@router.get("/projects/{pid}")
def get_project(pid: str):
    return _or_404(query_one("SELECT * FROM projects WHERE id=?", (pid,)), "Project")


@router.patch("/projects/{pid}")
def update_project(pid: str, body: ProjectUpdate):
    _or_404(query_one("SELECT id FROM projects WHERE id=?", (pid,)), "Project")
    data = body.model_dump(exclude_unset=True)
    allowed = {k: v for k, v in data.items() if k in ("name", "goal", "description", "technology_stack",
                                                      "repository_url", "workspace_path",
                                                      "default_gateway_id", "team_id", "status",
                                                      "po_enabled")}
    if "workspace_path" in allowed:
        err = workspace.validate_workspace_path(allowed["workspace_path"])
        if err:
            raise HTTPException(400, err)
    if "po_enabled" in allowed:
        allowed["po_enabled"] = 1 if allowed["po_enabled"] else 0
    allowed["updated_at"] = now()
    update("projects", pid, allowed)
    _db = __import__("app.db", fromlist=["emit_event"])
    emit = _db.emit_event(pid, "project.updated", {"note": "Project configuration updated"})
    return {**query_one("SELECT * FROM projects WHERE id = ?", (pid,)), "_seq": emit}


@router.delete("/projects/{pid}")
def delete_project(pid: str):
    for tbl, col in (("task_dependencies", ""), ("tasks", "project_id"),
                     ("sprint_gate_executions", "sprint"), ("sprint_acceptance_criteria", "sprint"),
                     ("sprints", "project_id"),
                     ("backlog_items", "project_id"), ("messages", ""), ("conversations", "project_id"),
                     ("execution_events", "project_id")):
        if tbl == "task_dependencies":
            execute("DELETE FROM task_dependencies WHERE task_id IN (SELECT id FROM tasks WHERE project_id=?) "
                    "OR depends_on_task_id IN (SELECT id FROM tasks WHERE project_id=?)", (pid, pid))
        elif tbl == "messages":
            execute("DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE project_id=?)", (pid,))
        elif tbl in ("sprint_gate_executions", "sprint_acceptance_criteria"):
            execute(f"DELETE FROM {tbl} WHERE sprint_id IN (SELECT id FROM sprints WHERE project_id=?)", (pid,))
        else:
            execute(f"DELETE FROM {tbl} WHERE {col} = ?", (pid,))
    execute("DELETE FROM projects WHERE id=?", (pid,))
    audit("delete_project", "project", pid, "Deleted project")
    return {"ok": True}


# ------------------ backlog ------------------

@router.get("/projects/{pid}/backlog")
def list_backlog(pid: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    total = query_one("SELECT COUNT(*) AS n FROM backlog_items WHERE project_id=?", (pid,))["n"]
    items = query("SELECT * FROM backlog_items WHERE project_id=? ORDER BY priority, created_at LIMIT ? OFFSET ?",
                  (pid, limit, offset))
    return {"total": total, "items": items, "limit": limit, "offset": offset}


@router.post("/projects/{pid}/backlog")
def add_backlog(pid: str, body: BacklogIn, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")):
    def _create():
        bid = new_id()
        insert("backlog_items", {"id": bid, "project_id": pid, "title": body.title,
                                 "description": body.description, "priority": body.priority,
                                 "acceptance_criteria": body.acceptance_criteria,
                                 "story_points": body.story_points, "status": "Backlog",
                                 "created_at": now()})
        return query_one("SELECT * FROM backlog_items WHERE id = ?", (bid,))

    return run_idempotent(idempotency_key, f"POST /projects/{pid}/backlog", _create)


@router.patch("/backlog/{bid}")
def update_backlog(bid: str, body: BacklogUpdate):
    _or_404(query_one("SELECT id FROM backlog_items WHERE id=?", (bid,)), "Backlog item")
    data = body.model_dump(exclude_unset=True)
    allowed = {k: v for k, v in data.items() if k in ("title", "description", "priority",
                                                      "acceptance_criteria", "story_points", "status")}
    update("backlog_items", bid, allowed)
    return query_one("SELECT * FROM backlog_items WHERE id = ?", (bid,))


@router.delete("/backlog/{bid}")
def delete_backlog(bid: str):
    execute("UPDATE tasks SET backlog_item_id=NULL WHERE backlog_item_id=?", (bid,))
    execute("DELETE FROM backlog_items WHERE id=?", (bid,))
    return {"ok": True}


# ------------------ sprints ------------------

@router.get("/projects/{pid}/sprints")
def list_sprints(pid: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    total = query_one("SELECT COUNT(*) AS n FROM sprints WHERE project_id=?", (pid,))["n"]
    sprints = query("SELECT * FROM sprints WHERE project_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (pid, limit, offset))
    for s in sprints:
        s["committed_points"] = query_one(
            "SELECT COALESCE(SUM(story_points),0) AS pts FROM tasks WHERE sprint_id=?", (s["id"],))["pts"]
    return {"total": total, "items": sprints, "limit": limit, "offset": offset}


@router.post("/projects/{pid}/sprints")
def create_sprint(pid: str, body: SprintIn, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")):
    def _create():
        dup = query_one("SELECT id FROM sprints WHERE project_id=? AND lower(name)=lower(?)", (pid, body.name))
        if dup:
            raise HTTPException(409, f"A sprint named '{body.name}' already exists in this project")
        sid = new_id()
        insert("sprints", {"id": sid, "project_id": pid, "name": body.name, "goal": body.goal,
                           "capacity": body.capacity, "start_at": body.start_at, "end_at": body.end_at,
                           "status": "Planned", "created_at": now()})
        audit("create_sprint", "sprint", sid, f"Created sprint '{body.name}'")
        return query_one("SELECT * FROM sprints WHERE id = ?", (sid,))

    return run_idempotent(idempotency_key, f"POST /projects/{pid}/sprints", _create)


@router.patch("/sprints/{sid}")
def update_sprint(sid: str, body: SprintUpdate):
    sprint = _or_404(query_one("SELECT * FROM sprints WHERE id=?", (sid,)), "Sprint")
    data = body.model_dump(exclude_unset=True)
    new_status = data.get("status")
    allowed = {k: v for k, v in data.items() if k in ("name", "goal", "capacity", "status", "start_at", "end_at")}
    if new_status == "Active":
        if sprint["status"] == "Completed":
            raise HTTPException(409, "Cannot reactivate a completed sprint")
        execute("UPDATE sprints SET status='Planned' WHERE project_id=? AND status='Active'", (sprint["project_id"],))
    allowed["status"] = new_status or sprint["status"]
    update("sprints", sid, allowed)
    if new_status:
        audit("sprint_status", "sprint", sid, f"Sprint '{sprint['name']}' -> {new_status}")
    return query_one("SELECT * FROM sprints WHERE id = ?", (sid,))


@router.delete("/sprints/{sid}")
def delete_sprint(sid: str):
    execute("UPDATE tasks SET sprint_id=NULL WHERE sprint_id=?", (sid,))
    execute("DELETE FROM sprint_gate_executions WHERE sprint_id=?", (sid,))
    execute("DELETE FROM sprint_acceptance_criteria WHERE sprint_id=?", (sid,))
    execute("DELETE FROM sprints WHERE id=?", (sid,))
    return {"ok": True}


@router.get("/projects/{pid}/sprints/active")
def get_active_sprint(pid: str):
    _or_404(query_one("SELECT id FROM projects WHERE id=?", (pid,)), "Project")
    sprint = sprint_gate.active_sprint(pid)
    if not sprint:
        return {"sprint": None, "gates": None, "next_locked_sprint": sprint_gate.next_locked_sprint(pid)}
    return {"sprint": sprint,
            "gates": sprint_gate.sprint_gate_summary(sprint["id"]),
            "next_locked_sprint": sprint_gate.next_locked_sprint(pid)}


@router.post("/projects/{pid}/sprints/{sid}/activate")
def activate_sprint(pid: str, sid: str):
    _or_404(query_one("SELECT id FROM projects WHERE id=?", (pid,)), "Project")
    sprint = _or_404(query_one("SELECT * FROM sprints WHERE id=? AND project_id=?", (sid, pid)), "Sprint")
    active = sprint_gate.active_sprint(pid)
    if active and active["id"] != sid:
        raise HTTPException(409, f"Sprint '{active['name']}' is already active "
                                 f"({active['status']}) — complete or cancel it first")
    if sprint["status"] == sprint_gate.COMPLETED:
        raise HTTPException(409, "Sprint is already completed")
    from ..services.sprints import transition_sprint
    ok, err = transition_sprint(sid, sprint_gate.ACTIVE,
                                "Activated via API", executed_by="user")
    if not ok:
        raise HTTPException(409, err)
    if not sprint.get("started_at"):
        update("sprints", sid, {"started_at": now()})
    audit("sprint_activated", "sprint", sid, f"Sprint '{sprint['name']}' activated via API")
    return query_one("SELECT * FROM sprints WHERE id = ?", (sid,))


@router.get("/sprints/{sid}/gates")
def sprint_gates(sid: str):
    _or_404(query_one("SELECT id FROM sprints WHERE id=?", (sid,)), "Sprint")
    return {"summary": sprint_gate.sprint_gate_summary(sid),
            "history": sprint_gate.gate_history(sid)}


@router.get("/sprints/{sid}/history")
def sprint_history(sid: str):
    sprint = _or_404(query_one("SELECT id, project_id FROM sprints WHERE id=?", (sid,)), "Sprint")
    rows = query(
        "SELECT * FROM execution_events WHERE project_id = ? AND event_type IN "
        "('sprint.status.changed','sprint.gate.changed','sprint.unlocked','sprint.activated') "
        "ORDER BY seq DESC LIMIT 100", (sprint["project_id"],))
    for r in rows:
        r["payload"] = _json.loads(r["payload"])
    return rows


@router.post("/sprints/{sid}/accept")
def accept_sprint(sid: str):
    """Manual acceptance override."""
    sprint = _or_404(query_one("SELECT * FROM sprints WHERE id=?", (sid,)), "Sprint")
    if sprint["status"] == sprint_gate.COMPLETED:
        return {"ok": True, "note": "already completed"}
    if sprint["status"] not in sprint_gate.ACTIVE_OR_GATING:
        raise HTTPException(409, f"Sprint is {sprint['status']} — cannot accept")
    sprint_gate.seed_sprint_acs(sprint)
    execute("UPDATE sprint_acceptance_criteria SET status='Passed', "
            "result='manually accepted', validated_at=? WHERE sprint_id=? AND required=1",
            (now(), sid))
    audit("sprint_accept_override", "sprint", sid,
          f"Sprint '{sprint['name']}' manually accepted by user", actor="user")
    outcome = sprint_gate.run_sprint_gates(sprint["project_id"], sid, {})
    return {"ok": outcome == "Completed", "outcome": outcome}
