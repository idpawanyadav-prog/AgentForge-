"""Task, execution, and event routes."""
from __future__ import annotations

import asyncio as _asyncio
import json as _json
import time as _time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from .. import po, runtime
from ..db import audit, execute, insert, new_id, now, query, query_one, run_idempotent, update
from ..schemas import TaskUpdate
from ..services import tasks as task_svc

router = APIRouter(prefix="/api/v1", tags=["tasks"])


def _or_404(row, what="Resource"):
    if row is None:
        raise HTTPException(404, f"{what} not found")
    return row


class TaskIn(BaseModel):
    title: str
    description: str = ""
    acceptance_criteria: str = ""
    story_points: int = 3
    priority: int = 2
    sprint_id: str | None = None
    backlog_item_id: str | None = None
    assigned_agent_id: str | None = None
    depends_on: list[str] = []


class ExecutionIn(BaseModel):
    project_id: str
    task_id: str


# ------------------ tasks ------------------

@router.get("/projects/{pid}/tasks")
def list_tasks(pid: str, sprint_id: Optional[str] = None):
    return task_svc.list_tasks_with_deps(pid, sprint_id)


@router.post("/projects/{pid}/tasks")
def create_task(pid: str, body: TaskIn, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")):
    return run_idempotent(
        idempotency_key, f"POST /projects/{pid}/tasks",
        lambda: task_svc.create_task_row(pid, body))


@router.patch("/tasks/{tid}")
def update_task(tid: str, body: TaskUpdate):
    return task_svc.update_task_row(tid, body)


@router.post("/tasks/{tid}/dependencies/{dep_id}")
def add_dependency(tid: str, dep_id: str):
    if tid == dep_id:
        raise HTTPException(400, "A task cannot depend on itself")
    execute("INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_task_id) VALUES (?,?)", (tid, dep_id))
    return {"ok": True}


@router.delete("/tasks/{tid}/dependencies/{dep_id}")
def remove_dependency(tid: str, dep_id: str):
    execute("DELETE FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?", (tid, dep_id))
    return {"ok": True}


@router.delete("/tasks/{tid}")
def delete_task(tid: str):
    execute("DELETE FROM task_dependencies WHERE task_id=? OR depends_on_task_id=?", (tid, tid))
    execute("DELETE FROM tasks WHERE id=?", (tid,))
    return {"ok": True}


# ------------------ project control ------------------

# Short-TTL cache for the control summary. The Control page polls this every
# ~1.2s and it performs ~6 queries + event loads; caching for a fraction of
# the poll interval absorbs repeated/parallel requests without serving stale
# state for long (events still arrive via the incremental /events poll).
_summary_cache: dict[str, tuple[float, dict]] = {}
_SUMMARY_TTL_S = 1.5


@router.get("/projects/{pid}/control/summary")
def control_summary(pid: str):
    cached = _summary_cache.get(pid)
    if cached and _time.monotonic() - cached[0] < _SUMMARY_TTL_S:
        return cached[1]
    result = _build_summary(pid)
    _summary_cache[pid] = (_time.monotonic(), result)
    return result


def _build_summary(pid: str) -> dict:
    from ..sprint_gate import active_sprint, next_locked_sprint, sprint_gate_summary
    project = _or_404(query_one(
        "SELECT p.*, t.name AS team_name, g.name AS gateway_name FROM projects p "
        "LEFT JOIN teams t ON t.id = p.team_id LEFT JOIN gateways g ON g.id = p.default_gateway_id "
        "WHERE p.id = ?", (pid,)), "Project")
    agents = query(
        "SELECT a.id, a.name, a.lifecycle_state, a.current_activity, a.current_task_id, "
        "r.name AS role_name, p.name AS persona_name, gm.provider_model_id, ta.role_in_team "
        "FROM team_agents ta JOIN teams tm ON tm.id = ta.team_id "
        "JOIN agents a ON a.id = ta.agent_id JOIN roles r ON r.id = a.role_id "
        "LEFT JOIN personas p ON p.id = a.persona_id "
        "LEFT JOIN model_bindings mb ON mb.id = a.model_binding_id "
        "LEFT JOIN gateway_models gm ON gm.id = mb.model_id "
        "LEFT JOIN tasks tk ON tk.id = a.current_task_id "
        "JOIN projects pr ON pr.team_id = tm.id AND pr.id = ? "
        "WHERE ta.active = 1 ORDER BY a.name", (pid,))
    tasks = task_svc.list_tasks_with_deps(pid, sprint_id=None)
    sprint_tasks = [t for t in tasks if t["sprint_id"]]
    active_s = active_sprint(pid)
    sprint_gates = sprint_gate_summary(active_s["id"]) if active_s else None
    next_locked = next_locked_sprint(pid)
    events = query("SELECT * FROM execution_events WHERE project_id=? ORDER BY seq DESC LIMIT 40", (pid,))
    for e in events:
        e["payload"] = _json.loads(e["payload"])
    usage = query_one(
        "SELECT COALESCE(SUM(ur.input_tokens),0) AS input_tokens, COALESCE(SUM(ur.output_tokens),0) AS output_tokens, "
        "COALESCE(SUM(ur.cost_estimate),0) AS cost FROM usage_records ur JOIN workflow_runs wr ON wr.id = ur.workflow_run_id "
        "WHERE wr.project_id = ?", (pid,))
    active_runs = query("SELECT * FROM workflow_runs WHERE project_id=? AND status IN ('Running','Paused')", (pid,))
    backlog_count = query_one("SELECT COUNT(*) AS n FROM backlog_items WHERE project_id=? AND status='Backlog'", (pid,))["n"]
    return {
        "project": project,
        "agents": agents,
        "sprint": active_s,
        "sprint_gates": sprint_gates,
        "next_locked_sprint": next_locked,
        "sprint_tasks": sprint_tasks,
        "events": list(reversed(events)),
        "usage": usage,
        "active_runs": active_runs,
        "backlog_count": backlog_count,
        "scheduler_running": pid in runtime._schedulers,
        "po": po.po_status(pid),
    }


# ------------------ executions ------------------

@router.post("/executions")
async def start_execution(body: ExecutionIn, idempotency_key: Optional[str] = None):
    result = await _asyncio.get_running_loop().run_in_executor(
        None, lambda: runtime.start_execution(body.project_id, body.task_id, idempotency_key))
    if "error" in result:
        raise HTTPException(409, result["error"])
    return result


def _run_action(run_id: str, action: str):
    run = _or_404(query_one("SELECT * FROM workflow_runs WHERE id=?", (run_id,)), "Execution")
    fn = {"pause": runtime.pause_execution, "resume": runtime.resume_execution,
          "cancel": runtime.cancel_execution, "stop": runtime.cancel_execution}
    if action == "retry":
        result = runtime.retry_execution(run_id)
    elif action in fn:
        result = fn[action](run_id)
    else:
        raise HTTPException(400, "Unknown action")
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(409, result["error"])
    audit(f"{action}_execution", "workflow_run", run_id, f"Execution control: {action}")
    return result


@router.post("/executions/{run_id}/{action}")
async def execution_action(run_id: str, action: str):
    return await _asyncio.get_running_loop().run_in_executor(None, _run_action, run_id, action)


@router.post("/projects/{pid}/sprint-execution/start")
async def start_sprint_exec(pid: str, body: dict = None):
    sprint_ref = (body or {}).get("sprint") if isinstance(body, dict) else None
    result = await _asyncio.get_running_loop().run_in_executor(
        None, runtime.start_sprint_execution, pid, sprint_ref)
    if "error" in result:
        raise HTTPException(409, result["error"])
    return result


@router.post("/projects/{pid}/sprint-execution/stop")
async def stop_sprint_exec(pid: str):
    await _asyncio.get_running_loop().run_in_executor(None, runtime.stop_sprint_execution, pid)
    return {"ok": True}


# ------------------ events ------------------

@router.get("/projects/{pid}/events")
def project_events(pid: str, after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000),
                   before: int = Query(0, ge=0), latest: int = Query(0, ge=0, le=1000)):
    if latest:
        events = query("SELECT * FROM execution_events WHERE project_id=? ORDER BY seq DESC LIMIT ?",
                       (pid, latest))
    elif before:
        events = query("SELECT * FROM execution_events WHERE project_id=? AND seq < ? ORDER BY seq DESC LIMIT ?",
                       (pid, before, limit))
    else:
        events = query("SELECT * FROM execution_events WHERE project_id=? AND seq > ? ORDER BY seq LIMIT ?",
                       (pid, after, limit))
    for e in events:
        e["payload"] = _json.loads(e["payload"])
    if latest or before:
        page = latest or limit
        return {"events": events, "last_seq": events[0]["seq"] if events else after,
                "exhausted": len(events) < page}
    last = events[-1]["seq"] if events else after
    return {"events": events, "last_seq": last}


@router.get("/executions/{run_id}/events")
def run_events(run_id: str, after: int = Query(0, ge=0)):
    _or_404(query_one("SELECT id FROM workflow_runs WHERE id=?", (run_id,)), "Execution")
    events = query("SELECT * FROM execution_events WHERE workflow_run_id=? AND seq > ? ORDER BY seq", (run_id, after))
    for e in events:
        e["payload"] = _json.loads(e["payload"])
    return {"events": events, "last_seq": events[-1]["seq"] if events else after}


@router.get("/projects/{pid}/usage")
def project_usage(pid: str):
    by_agent = query(
        "SELECT a.name AS agent, COALESCE(SUM(ur.input_tokens),0) AS input_tokens, "
        "COALESCE(SUM(ur.output_tokens),0) AS output_tokens, COALESCE(SUM(ur.cost_estimate),0) AS cost "
        "FROM usage_records ur JOIN workflow_runs wr ON wr.id = ur.workflow_run_id "
        "LEFT JOIN agents a ON a.id = ur.agent_id WHERE wr.project_id=? GROUP BY ur.agent_id", (pid,))
    return {"by_agent": by_agent}
