"""Agent and team routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..db import audit, execute, insert, new_id, now, query, query_one, update
from ..schemas import AgentUpdate, TeamUpdate
from ._util import or_404 as _or_404

router = APIRouter(prefix="/api/v1", tags=["agents"])


class AgentIn(BaseModel):
    name: str
    role_id: str
    persona_id: str
    model_binding_id: str | None = None


class TeamIn(BaseModel):
    name: str
    description: str = ""
    agent_ids: list[str] = []


@router.get("/agents")
def list_agents(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    total = query_one("SELECT COUNT(*) AS n FROM agents")["n"]
    items = query(
        "SELECT a.*, r.name AS role_name, p.name AS persona_name, p.version AS persona_version, "
        "mb.name AS model_name, gm.provider_model_id FROM agents a "
        "LEFT JOIN roles r ON r.id = a.role_id "
        "LEFT JOIN personas p ON p.id = a.persona_id "
        "LEFT JOIN model_bindings mb ON mb.id = a.model_binding_id "
        "LEFT JOIN gateway_models gm ON gm.id = mb.model_id ORDER BY a.name LIMIT ? OFFSET ?", (limit, offset))
    return {"total": total, "items": items, "limit": limit, "offset": offset}


@router.post("/agents")
def create_agent(body: AgentIn):
    aid = new_id()
    ts = now()
    insert("agents", {"id": aid, "name": body.name, "role_id": body.role_id,
                      "persona_id": body.persona_id, "model_binding_id": body.model_binding_id,
                      "lifecycle_state": "Idle", "created_at": ts, "updated_at": ts})
    audit("create_agent", "agent", aid, f"Created agent '{body.name}'")
    return query_one("SELECT * FROM agents WHERE id = ?", (aid,))


@router.patch("/agents/{aid}")
def update_agent(aid: str, body: AgentUpdate):
    agent = _or_404(query_one("SELECT id FROM agents WHERE id=?", (aid,)), "Agent")
    data = body.model_dump(exclude_unset=True)
    allowed = {k: v for k, v in data.items() if k in ("name", "role_id", "persona_id", "model_binding_id")}
    if "lifecycle_state" in data:
        state = data["lifecycle_state"]
        if state not in ("Idle", "Paused"):
            raise HTTPException(422, "Only 'Idle' (unstick) or 'Paused' can be set manually; "
                                     "other states are managed by the execution runtime")
        allowed["lifecycle_state"] = state
        if state == "Idle":
            allowed["current_activity"] = ""
            allowed["current_task_id"] = None
    allowed["updated_at"] = now()
    update("agents", aid, allowed)
    audit("update_agent", "agent", aid, f"Updated agent fields: {', '.join(allowed)}")
    return query_one("SELECT * FROM agents WHERE id = ?", (aid,))


@router.delete("/agents/{aid}")
def delete_agent(aid: str):
    agent = _or_404(query_one("SELECT * FROM agents WHERE id=?", (aid,)), "Agent")
    if agent["lifecycle_state"] not in ("Idle", "Failed"):
        raise HTTPException(409, "Agent is busy; stop its execution first")
    open_tasks = query_one(
        "SELECT COUNT(*) AS n FROM tasks WHERE assigned_agent_id = ? AND status NOT IN ('Done','Cancelled')",
        (aid,))["n"]
    if open_tasks:
        raise HTTPException(409, f"Agent still owns {open_tasks} open task(s) — "
                                 "reassign or cancel them first")
    # FK-complete: history rows keep a nullable FK to the agent, so clear the
    # references instead of letting SQLite reject the delete with a 500.
    execute("UPDATE workflow_runs SET agent_id=NULL WHERE agent_id=?", (aid,))
    execute("UPDATE tasks SET assigned_agent_id=NULL WHERE assigned_agent_id=?", (aid,))
    execute("UPDATE tasks SET qa_agent_id=NULL WHERE qa_agent_id=?", (aid,))
    execute("UPDATE task_reviews SET reviewer_agent_id=NULL WHERE reviewer_agent_id=?", (aid,))
    execute("UPDATE dependency_requests SET requested_by_agent_id=NULL WHERE requested_by_agent_id=?", (aid,))
    execute("UPDATE project_memory SET owner_agent_id=NULL WHERE owner_agent_id=?", (aid,))
    execute("UPDATE agent_messages SET from_agent_id=NULL WHERE from_agent_id=?", (aid,))
    execute("UPDATE agent_messages SET to_agent_id=NULL WHERE to_agent_id=?", (aid,))
    execute("DELETE FROM team_agents WHERE agent_id=?", (aid,))
    execute("DELETE FROM agents WHERE id=?", (aid,))
    return {"ok": True}


# ------------------ teams ------------------

@router.get("/teams")
def list_teams(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    total = query_one("SELECT COUNT(*) AS n FROM teams")["n"]
    teams = query("SELECT * FROM teams ORDER BY name LIMIT ? OFFSET ?", (limit, offset))
    for t in teams:
        t["agents"] = query(
            "SELECT a.id, a.name, a.lifecycle_state, a.current_activity, r.name AS role_name, ta.role_in_team "
            "FROM team_agents ta JOIN agents a ON a.id = ta.agent_id JOIN roles r ON r.id = a.role_id "
            "WHERE ta.team_id = ? AND ta.active = 1", (t["id"],))
    return {"total": total, "items": teams, "limit": limit, "offset": offset}


@router.post("/teams")
def create_team(body: TeamIn):
    if query_one("SELECT id FROM teams WHERE lower(name)=lower(?)", (body.name,)):
        raise HTTPException(409, "Team name already exists")
    tid = new_id()
    insert("teams", {"id": tid, "name": body.name, "description": body.description,
                     "status": "Active", "created_at": now()})
    for agent_id in body.agent_ids:
        execute("INSERT OR IGNORE INTO team_agents (team_id, agent_id, role_in_team, active) VALUES (?,?,?,1)",
                (tid, agent_id, "Member"))
    audit("create_team", "team", tid, f"Created team '{body.name}'")
    return query_one("SELECT * FROM teams WHERE id = ?", (tid,))


@router.patch("/teams/{tid}")
def update_team(tid: str, body: TeamUpdate):
    _or_404(query_one("SELECT id FROM teams WHERE id=?", (tid,)), "Team")
    data = body.model_dump(exclude_unset=True)
    if "agent_ids" in data:
        execute("DELETE FROM team_agents WHERE team_id=?", (tid,))
        for agent_id in data["agent_ids"]:
            execute("INSERT OR IGNORE INTO team_agents (team_id, agent_id, role_in_team, active) VALUES (?,?,?,1)",
                    (tid, agent_id, "Member"))
    allowed = {k: v for k, v in data.items() if k in ("name", "description", "status")}
    if allowed:
        update("teams", tid, allowed)
    return {"ok": True}


@router.delete("/teams/{tid}")
def delete_team(tid: str):
    used = query_one("SELECT id FROM projects WHERE team_id=? LIMIT 1", (tid,))
    if used:
        raise HTTPException(409, "Team is assigned to a project")
    execute("DELETE FROM team_agents WHERE team_id=?", (tid,))
    execute("DELETE FROM teams WHERE id=?", (tid,))
    return {"ok": True}
