"""Task, execution, and event routes."""
from __future__ import annotations

import asyncio as _asyncio
import json as _json
import time as _time
import threading as _threading
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel

from .. import po, runtime
from ..db import audit, execute, insert, new_id, now, query, query_one, run_idempotent, update
from ..schemas import TaskUpdate
from ..services import tasks as task_svc
from ._util import or_404 as _or_404

router = APIRouter(prefix="/api/v1", tags=["tasks"])


class TaskIn(BaseModel):
    title: str = ""
    description: str = ""
    acceptance_criteria: str = ""
    story_points: int = 3
    priority: int = 2
    sprint_id: str | None = None
    backlog_item_id: str | None = None
    assigned_agent_id: str | None = None
    depends_on: list[str] = []
    template_id: str | None = None
    template_variables: dict = {}


@router.get("/task-templates")
def task_templates():
    from .. import task_templates as tt
    return tt.list_templates()


@router.post("/task-templates/{template_id}/preview")
def task_template_preview(template_id: str, variables: dict | None = None):
    from .. import task_templates as tt
    applied = tt.apply_template(template_id, variables or {})
    if not applied:
        raise HTTPException(404, "Unknown task template")
    return applied


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
    task_svc.validate_new_dependency(tid, dep_id)
    execute("INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_task_id) VALUES (?,?)", (tid, dep_id))
    return {"ok": True}


@router.delete("/tasks/{tid}/dependencies/{dep_id}")
def remove_dependency(tid: str, dep_id: str):
    execute("DELETE FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?", (tid, dep_id))
    return {"ok": True}


@router.delete("/tasks/{tid}")
def delete_task(tid: str):
    # FK-complete: the V3 governance tables reference tasks too, so deleting
    # a reviewed/linked task without this cleanup fails with a 500.
    execute("DELETE FROM task_dependencies WHERE task_id=? OR depends_on_task_id=?", (tid, tid))
    execute("DELETE FROM task_reviews WHERE task_id=?", (tid,))
    execute("DELETE FROM task_requirements WHERE task_id=?", (tid,))
    execute("UPDATE workflow_runs SET task_id=NULL WHERE task_id=?", (tid,))
    execute("UPDATE dependency_requests SET requested_for_task_id=NULL WHERE requested_for_task_id=?", (tid,))
    execute("UPDATE agent_messages SET related_task_id=NULL WHERE related_task_id=?", (tid,))
    execute("UPDATE change_requests SET origin_task_id=NULL WHERE origin_task_id=?", (tid,))
    execute("UPDATE context_cache SET task_id=NULL WHERE task_id=?", (tid,))
    execute("DELETE FROM tasks WHERE id=?", (tid,))
    return {"ok": True}


# ------------------ project control ------------------

# Short-TTL cache for the control summary. The Control page polls this every
# ~1.2s and it performs ~6 queries + event loads; caching for a fraction of
# the poll interval absorbs repeated/parallel requests without serving stale
# state for long (events still arrive via the incremental /events poll).
_summary_cache: dict[str, tuple[float, dict]] = {}
_summary_cache_lock = _threading.Lock()
_SUMMARY_TTL_S = 1.5
_MAX_SUMMARY_CACHE_SIZE = 100


@router.get("/projects/{pid}/control/summary")
def control_summary(pid: str):
    with _summary_cache_lock:
        cached = _summary_cache.get(pid)
    if cached and _time.monotonic() - cached[0] < _SUMMARY_TTL_S:
        return cached[1]
    result = _build_summary(pid)
    # Expire old entries and cap the cache even when many project IDs are
    # requested once. The endpoint is polled frequently by each open page.
    cutoff = _time.monotonic() - _SUMMARY_TTL_S
    with _summary_cache_lock:
        for key, (created, _) in list(_summary_cache.items()):
            if created < cutoff:
                _summary_cache.pop(key, None)
        if pid not in _summary_cache and len(_summary_cache) >= _MAX_SUMMARY_CACHE_SIZE:
            oldest = min(_summary_cache, key=lambda key: _summary_cache[key][0])
            _summary_cache.pop(oldest, None)
        _summary_cache[pid] = (_time.monotonic(), result)
    return result


def _build_summary(pid: str) -> dict:
    from .. import flows as _flows
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
    flow = _flows.flow_for_project(project)
    return {
        "project": project,
        "flow": {"id": flow.get("id"), "name": flow.get("name"),
                 "stages": _flows.stage_order(flow)},
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
async def start_execution(body: ExecutionIn,
                          idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")):
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


@router.get("/projects/{pid}/costs")
def project_costs(pid: str, by: str = Query("role", pattern="^(role|agent|model)$"),
                  sprint_id: str | None = Query(None)):
    """Cost rollup for a project grouped by role, agent or model
    (AGENT_DEV_SPEEDUP 3.5) plus the current budget status. usage_records
    has no direct role/sprint link, so resolve each run's task -> sprint."""
    _or_404(query_one("SELECT id FROM projects WHERE id=?", (pid,)), "Project")
    if by == "role":
        group_col = "COALESCE(r.name, '(unknown role)')"
        extra_join = "LEFT JOIN agents a ON a.id = ur.agent_id LEFT JOIN roles r ON r.id = a.role_id"
        label = "role"
    elif by == "agent":
        group_col = "COALESCE(a.name, '(unknown agent)')"
        extra_join = "LEFT JOIN agents a ON a.id = ur.agent_id"
        label = "agent"
    else:
        group_col = "CASE WHEN ur.model = '' THEN '(scaffold)' ELSE ur.model END"
        extra_join = ""
        label = "model"
    params: list = [pid]
    sprint_filter = ""
    if sprint_id:
        sprint_filter = (" AND (wr.task_id IS NULL OR wr.task_id IN "
                         "(SELECT id FROM tasks WHERE sprint_id = ?))")
        params.append(sprint_id)
    rows = query(
        f"SELECT {group_col} AS {label}, COUNT(*) AS calls, "
        "COALESCE(SUM(ur.input_tokens),0) AS input_tokens, "
        "COALESCE(SUM(ur.output_tokens),0) AS output_tokens, "
        "ROUND(COALESCE(SUM(ur.cost_estimate),0), 4) AS cost_usd "
        "FROM usage_records ur JOIN workflow_runs wr ON wr.id = ur.workflow_run_id "
        f"{extra_join} WHERE wr.project_id = ?{sprint_filter} "
        f"GROUP BY {group_col} ORDER BY cost_usd DESC", params)
    from .. import budget
    return {"by": by, "items": rows, **budget.budget_status(pid)}
