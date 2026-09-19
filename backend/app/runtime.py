"""Simulated agent execution runtime.

Mimics the MAF workflow runtime contract described in the spec: durable
workflow runs, streamed execution events with per-project sequence numbers,
pause/resume/cancel/retry controls, usage recording and audit events. Model
calls are simulated so the platform is fully demoable without real provider
credentials.
"""
import asyncio
import json
import random

from . import db
from .db import emit_event, execute, insert, now, new_id, query_one, query, update, audit

# run_id -> {"task": asyncio.Task, "paused": bool, "cancelled": bool}
_registry: dict = {}
# project_id -> {"task": asyncio.Task, "cancelled": bool}
_schedulers: dict = {}

TASK_STATUSES = ["Todo", "Ready", "In Progress", "Blocked", "Review", "Testing", "Done", "Cancelled"]
AGENT_STATES = ["Idle", "Working", "Waiting", "Blocked", "Failed", "Paused", "Completed"]

_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop):
    """Capture the main event loop so worker threads can spawn coroutines on it."""
    global _loop
    _loop = loop


def _spawn(coro):
    if _loop is not None and _loop.is_running():
        return asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        return asyncio.get_event_loop().create_task(coro)

# (step, activity, tools, next_task_status, progress)
PHASES = [
    ("analyze", "Analyzing task requirements and acceptance criteria", [], "In Progress", 15),
    ("plan", "Drafting implementation plan from persona instructions", ["memory.read"], "In Progress", 25),
    ("implement", "Writing code in project workspace", ["file.write", "file.read"], "In Progress", 55),
    ("build-test", "Building and running unit tests", ["shell.run", "test.run"], "Testing", 75),
    ("review", "Self-review against definition of done", ["code-review"], "Review", 90),
    ("finalize", "Recording evidence and completing task", [], "Done", 100),
]


def _emit(project_id, event_type, payload, **kw):
    return emit_event(project_id, event_type, payload, **kw)


def _set_agent(agent_id, state, activity="", task_id=None, project_id=None):
    update("agents", agent_id, {
        "lifecycle_state": state, "current_activity": activity,
        "current_task_id": task_id, "updated_at": now(),
    })
    agent = query_one("SELECT * FROM agents WHERE id = ?", (agent_id,))
    if agent and project_id:
        _emit(project_id, "agent.state_changed",
              {"agent_id": agent_id, "agent_name": agent["name"], "state": state, "activity": activity},
              agent_id=agent_id, task_id=task_id)


def get_run(run_id):
    return query_one("SELECT * FROM workflow_runs WHERE id = ?", (run_id,))


def active_run_for_task(task_id):
    return query_one(
        "SELECT * FROM workflow_runs WHERE task_id = ? AND status IN ('Running','Paused','Queued')",
        (task_id,))


def dependencies_satisfied(task_id) -> tuple[bool, list]:
    deps = query(
        "SELECT t.id, t.title, t.status FROM task_dependencies d JOIN tasks t ON t.id = d.depends_on_task_id "
        "WHERE d.task_id = ?", (task_id,))
    unmet = [d for d in deps if d["status"] not in ("Done", "Cancelled")]
    return (not unmet), unmet


def validate_task_ready(task) -> tuple[bool, str]:
    if not task:
        return False, "Task not found"
    if task["status"] in ("Done", "Cancelled", "In Progress", "Review", "Testing"):
        return False, f"Task is {task['status']}; not eligible to start"
    if not task["assigned_agent_id"]:
        return False, "Task has no assigned agent"
    ok, unmet = dependencies_satisfied(task["id"])
    if not ok:
        names = ", ".join(d["title"] for d in unmet)
        return False, f"Dependencies incomplete: {names}"
    if active_run_for_task(task["id"]):
        return False, "Task already has an active execution"
    return True, ""


def start_execution(project_id: str, task_id: str, idempotency_key: str | None = None):
    task = query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    ok, reason = validate_task_ready(task)
    if not ok:
        return {"error": reason}

    if idempotency_key:
        existing = query_one(
            "SELECT * FROM workflow_runs WHERE idempotency_key = ?", (idempotency_key,))
        if existing:
            return {"run": existing, "idempotent_replay": True}

    agent = query_one("SELECT a.*, r.name AS role_name FROM agents a JOIN roles r ON r.id = a.role_id WHERE a.id = ?",
                      (task["assigned_agent_id"],))
    persona = query_one("SELECT * FROM personas WHERE id = ?", (agent["persona_id"],))
    binding = None
    model_name = "unknown"
    if agent["model_binding_id"]:
        binding = query_one(
            "SELECT mb.*, gm.display_name AS model_name, gm.provider_model_id FROM model_bindings mb "
            "JOIN gateway_models gm ON gm.id = mb.model_id WHERE mb.id = ?",
            (agent["model_binding_id"],))
        if binding:
            model_name = binding["provider_model_id"]

    run_id = new_id()
    insert("workflow_runs", {
        "id": run_id, "project_id": project_id, "task_id": task_id,
        "agent_id": agent["id"], "status": "Running",
        "current_step": PHASES[0][0], "started_at": now(),
        "idempotency_key": idempotency_key,
    })
    update("tasks", task_id, {"status": "In Progress", "progress": 5,
                              "blocked_reason": "", "updated_at": now()})
    _emit(project_id, "workflow.started",
          {"run_id": run_id, "task": task["title"], "agent": agent["name"],
           "model": model_name, "persona_version": persona["version"]},
          workflow_run_id=run_id, task_id=task_id, agent_id=agent["id"])
    _set_agent(agent["id"], "Working", f"Starting: {task['title']}", task_id=task_id, project_id=project_id)
    _emit(project_id, "task.status_changed",
          {"task_id": task_id, "task": task["title"], "status": "In Progress", "progress": 5},
          workflow_run_id=run_id, task_id=task_id)
    audit("start_execution", "workflow_run", run_id,
          f"Started execution of task '{task['title']}'")

    ctrl = {"paused": False, "cancelled": False}
    handle = _spawn(_run_phases(run_id, ctrl))
    _registry[run_id] = {**ctrl, "task": handle}
    return {"run": get_run(run_id)}


async def _run_phases(run_id: str, ctrl: dict):
    run = get_run(run_id)
    project_id, task_id, agent_id = run["project_id"], run["task_id"], run["agent_id"]
    task = query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    evidence = []
    try:
        for step, activity, tools, next_status, progress in PHASES:
            if ctrl["cancelled"]:
                raise asyncio.CancelledError()
            await _wait_if_paused(run_id, ctrl, project_id, task_id, agent_id)
            update("workflow_runs", run_id, {"current_step": step})
            _emit(project_id, "agent.activity",
                  {"agent_id": agent_id, "step": step, "activity": activity},
                  workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
            await _sleep_with_control(ctrl, random.uniform(2.5, 4.5))

            for tool in tools:
                if ctrl["cancelled"]:
                    raise asyncio.CancelledError()
                await _wait_if_paused(run_id, ctrl, project_id, task_id, agent_id)
                _emit(project_id, "tool.started",
                      {"agent_id": agent_id, "tool": tool, "step": step},
                      workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
                await _sleep_with_control(ctrl, random.uniform(0.8, 1.8))
                # Simulated transient failure (~8% on the build-test phase)
                if step == "build-test" and tool == "test.run" and random.random() < 0.08:
                    _emit(project_id, "tool.failed",
                          {"agent_id": agent_id, "tool": tool,
                           "error": "2 unit tests failed: session expiry edge case"},
                          workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
                    raise RuntimeError("Unit tests failed during build-test phase")
                _emit(project_id, "tool.completed",
                      {"agent_id": agent_id, "tool": tool, "step": step,
                       "result": "ok"},
                      workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
                if step == "implement" and tool == "file.write":
                    evidence.append("commit:" + new_id()[:8])
                if step == "build-test" and tool == "test.run":
                    evidence.append(f"tests:{random.randint(12, 48)} passed")

            if next_status != task["status"] or progress:
                update("tasks", task_id, {"status": next_status, "progress": progress,
                                          "updated_at": now()})
                _emit(project_id, "task.status_changed",
                      {"task_id": task_id, "task": task["title"],
                       "status": next_status, "progress": progress},
                      workflow_run_id=run_id, task_id=task_id)

        await _wait_if_paused(run_id, ctrl, project_id, task_id, agent_id)
        # ---- completion ----
        in_tokens, out_tokens = random.randint(1800, 5200), random.randint(600, 2400)
        cost = round(in_tokens / 1_000_000 * 2.0 + out_tokens / 1_000_000 * 8.0, 4)
        insert("usage_records", {
            "id": new_id(), "workflow_run_id": run_id, "agent_id": agent_id,
            "model": "gpt-4.1", "input_tokens": in_tokens, "output_tokens": out_tokens,
            "cost_estimate": cost, "created_at": now(),
        })
        update("tasks", task_id, {
            "status": "Done", "progress": 100,
            "evidence": "; ".join(evidence) if evidence else "review approved",
            "updated_at": now(),
        })
        update("workflow_runs", run_id, {"status": "Completed", "current_step": "done",
                                         "completed_at": now()})
        _emit(project_id, "task.status_changed",
              {"task_id": task_id, "task": task["title"], "status": "Done", "progress": 100},
              workflow_run_id=run_id, task_id=task_id)
        _emit(project_id, "workflow.completed",
              {"run_id": run_id, "task": task["title"], "evidence": evidence,
               "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens, "cost_usd": cost}},
              workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
        _emit(project_id, "usage.recorded",
              {"run_id": run_id, "input_tokens": in_tokens,
               "output_tokens": out_tokens, "cost_usd": cost},
              workflow_run_id=run_id, agent_id=agent_id)
        _set_agent(agent_id, "Completed", f"Completed: {task['title']}", task_id=task_id, project_id=project_id)
        await asyncio.sleep(1.2)
        _set_agent(agent_id, "Idle", "", task_id=None, project_id=project_id)
        audit("execution_completed", "workflow_run", run_id, f"Task '{task['title']}' completed")
    except asyncio.CancelledError:
        update("workflow_runs", run_id, {"status": "Cancelled", "completed_at": now()})
        update("tasks", task_id, {"status": "Blocked", "blocked_reason": "Cancelled by operator",
                                  "updated_at": now()})
        _emit(project_id, "workflow.cancelled", {"run_id": run_id},
              workflow_run_id=run_id, task_id=task_id)
        _emit(project_id, "task.status_changed",
              {"task_id": task_id, "status": "Blocked", "blocked_reason": "Cancelled by operator"},
              workflow_run_id=run_id, task_id=task_id)
        _set_agent(agent_id, "Idle", "", task_id=None, project_id=project_id)
        audit("cancel_execution", "workflow_run", run_id, "Execution cancelled")
    except Exception as exc:  # simulated failure path
        update("workflow_runs", run_id, {"status": "Failed", "completed_at": now()})
        update("tasks", task_id, {"status": "Blocked",
                                  "blocked_reason": f"Execution failed: {exc}",
                                  "updated_at": now()})
        _emit(project_id, "workflow.failed",
              {"run_id": run_id, "error": str(exc), "recoverable": True},
              workflow_run_id=run_id, task_id=task_id)
        _emit(project_id, "task.status_changed",
              {"task_id": task_id, "status": "Blocked", "blocked_reason": str(exc)},
              workflow_run_id=run_id, task_id=task_id)
        _set_agent(agent_id, "Failed", f"Failed: {task['title']}", task_id=task_id, project_id=project_id)
        await asyncio.sleep(1.2)
        _set_agent(agent_id, "Idle", "", task_id=None, project_id=project_id)
        audit("execution_failed", "workflow_run", run_id, str(exc))
    finally:
        _registry.pop(run_id, None)


async def _wait_if_paused(run_id, ctrl, project_id, task_id, agent_id):
    if not ctrl["paused"]:
        return
    # State/events were emitted immediately by pause_execution(); just wait here.
    while ctrl["paused"] and not ctrl["cancelled"]:
        await asyncio.sleep(0.4)
    if ctrl["cancelled"]:
        raise asyncio.CancelledError()


async def _sleep_with_control(ctrl: dict, seconds: float):
    slept = 0.0
    while slept < seconds:
        if ctrl["cancelled"]:
            raise asyncio.CancelledError()
        if not ctrl["paused"]:
            slept += 0.25
        await asyncio.sleep(0.25)


def pause_execution(run_id):
    ctrl = _registry.get(run_id)
    if not ctrl:
        return {"error": "No active worker for this run"}
    if not ctrl["paused"]:
        ctrl["paused"] = True
        run = get_run(run_id)
        if run and run["status"] == "Running":
            update("workflow_runs", run_id, {"status": "Paused"})
            _set_agent(run["agent_id"], "Paused", "Paused by operator",
                       task_id=run["task_id"], project_id=run["project_id"])
            _emit(run["project_id"], "workflow.paused", {"run_id": run_id},
                  workflow_run_id=run_id, task_id=run["task_id"])
    return {"ok": True}


def resume_execution(run_id):
    ctrl = _registry.get(run_id)
    if not ctrl:
        return {"error": "No active worker for this run"}
    if ctrl["paused"]:
        ctrl["paused"] = False
        run = get_run(run_id)
        update("workflow_runs", run_id, {"status": "Running"})
        _set_agent(run["agent_id"], "Working", "Resumed",
                   task_id=run["task_id"], project_id=run["project_id"])
        _emit(run["project_id"], "workflow.resumed", {"run_id": run_id},
              workflow_run_id=run_id, task_id=run["task_id"])
    return {"ok": True}


def cancel_execution(run_id):
    ctrl = _registry.get(run_id)
    if not ctrl:
        run = get_run(run_id)
        if run and run["status"] in ("Running", "Paused"):
            update("workflow_runs", run_id, {"status": "Cancelled", "completed_at": now()})
        return {"ok": True, "note": "run was not active"}
    ctrl["cancelled"] = True
    ctrl["paused"] = False
    return {"ok": True}


def retry_execution(run_id):
    run = get_run(run_id)
    if not run:
        return {"error": "Run not found"}
    task = query_one("SELECT * FROM tasks WHERE id = ?", (run["task_id"],))
    if task and task["status"] == "Blocked":
        update("tasks", task["id"], {"status": "Ready", "blocked_reason": "", "updated_at": now()})
    return start_execution(run["project_id"], run["task_id"])


def _eligible_tasks(project_id):
    tasks = query(
        "SELECT * FROM tasks WHERE project_id = ? AND sprint_id IS NOT NULL "
        "AND status IN ('Todo','Ready') AND assigned_agent_id IS NOT NULL "
        "ORDER BY priority, created_at", (project_id,))
    eligible = []
    for t in tasks:
        ok, _ = dependencies_satisfied(t["id"])
        if ok and not active_run_for_task(t["id"]):
            agent = query_one("SELECT lifecycle_state FROM agents WHERE id = ?", (t["assigned_agent_id"],))
            if agent and agent["lifecycle_state"] == "Idle":
                eligible.append(t)
    return eligible


async def _run_sprint(project_id: str, ctrl: dict):
    _emit(project_id, "project.updated", {"note": "Sprint execution started"})
    while not ctrl["cancelled"]:
        eligible = _eligible_tasks(project_id)
        if not eligible:
            remaining = query(
                "SELECT COUNT(*) AS n FROM tasks WHERE project_id = ? AND sprint_id IS NOT NULL "
                "AND status NOT IN ('Done','Cancelled')", (project_id,))[0]["n"]
            _emit(project_id, "project.updated",
                  {"note": ("Sprint execution complete" if remaining == 0
                            else "No eligible tasks: blocked/waiting items remain"),
                   "remaining_tasks": remaining})
            break
        task = eligible[0]
        result = start_execution(project_id, task["id"])
        run = result.get("run") or {}
        run_id = run.get("id") or (result.get("run") or {}).get("id")
        if not run_id:
            update("tasks", task["id"], {"status": "Blocked",
                                         "blocked_reason": result.get("error", "could not start"),
                                         "updated_at": now()})
            continue
        while run_id in _registry:
            await asyncio.sleep(0.5)
        await asyncio.sleep(1.0)
    _schedulers.pop(project_id, None)


def start_sprint_execution(project_id: str):
    sprint = query_one("SELECT * FROM sprints WHERE project_id = ? AND status = 'Active'", (project_id,))
    if not sprint:
        return {"error": "No active sprint for this project"}
    if project_id in _schedulers:
        return {"error": "Sprint execution already running"}
    ctrl = {"cancelled": False}
    handle = _spawn(_run_sprint(project_id, ctrl))
    _schedulers[project_id] = {**ctrl, "task": handle}
    return {"ok": True, "sprint": sprint["name"]}


def stop_sprint_execution(project_id: str):
    ctrl = _schedulers.get(project_id)
    if ctrl:
        ctrl["cancelled"] = True
    for run_id, rc in list(_registry.items()):
        run = get_run(run_id)
        if run and run["project_id"] == project_id:
            rc["cancelled"] = True
    return {"ok": True}


def recover_orphans():
    """Called at startup: mark interrupted runs as failed and free agents."""
    for run in query("SELECT * FROM workflow_runs WHERE status IN ('Running','Paused')"):
        update("workflow_runs", run["id"], {"status": "Failed", "completed_at": now(),
                                            "current_step": "interrupted (service restart)"})
        if run["task_id"]:
            update("tasks", run["task_id"],
                   {"status": "Blocked", "blocked_reason": "Interrupted by service restart",
                    "updated_at": now()})
        _emit(run["project_id"], "workflow.failed",
              {"run_id": run["id"], "error": "Service restarted; run marked recoverable",
               "recoverable": True}, workflow_run_id=run["id"])
    for agent in query("SELECT * FROM agents WHERE lifecycle_state NOT IN ('Idle')"):
        update("agents", agent["id"], {"lifecycle_state": "Idle", "current_activity": "",
                                       "current_task_id": None, "updated_at": now()})
