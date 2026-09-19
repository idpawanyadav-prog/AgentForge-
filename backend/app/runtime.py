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
import re

from . import db, workspace, codegen, toolchains
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

# QA verification phases, run by a QA-role agent after dev completion.
QA_PHASES = [
    ("verify", "QA: reviewing implementation against acceptance criteria", ["memory.read", "file.read"], "Testing", 30),
    ("qa-test", "QA: running test suite and exploratory checks", ["shell.run", "test.run"], "Testing", 70),
    ("qa-report", "QA: writing verdict and defect summary", ["code-review"], "Testing", 95),
]

# Defect summaries QA may find (simulated verdicts).
_QA_ISSUES = [
    "API returns 500 when city lookup has no results",
    "temperature unit toggle does not persist after reload",
    "forecast cache never invalidates between cities",
    "wind direction shows NaN for some stations",
    "layout overflows on narrow screens",
    "hourly forecast skips the midnight slot",
]

# After this many QA rejections the task is escalated to a human.
MAX_REWORK_CYCLES = 2


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


def _is_qa_run(task) -> bool:
    return task["status"] == "Waiting QA"


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

    qa_mode = _is_qa_run(task)
    run_id = new_id()
    insert("workflow_runs", {
        "id": run_id, "project_id": project_id, "task_id": task_id,
        "agent_id": agent["id"], "status": "Running",
        "current_step": (QA_PHASES if qa_mode else PHASES)[0][0], "started_at": now(),
        "idempotency_key": idempotency_key,
    })
    update("tasks", task_id, {"status": "In Progress" if not qa_mode else "Testing", "progress": 5,
                              "blocked_reason": "", "updated_at": now()})
    _emit(project_id, "workflow.started",
          {"run_id": run_id, "task": task["title"], "agent": agent["name"],
           "model": model_name, "persona_version": persona["version"],
           "mode": "qa" if qa_mode else "dev"},
          workflow_run_id=run_id, task_id=task_id, agent_id=agent["id"])
    _set_agent(agent["id"], "Working",
               ("QA: " if qa_mode else "") + f"Starting: {task['title']}",
               task_id=task_id, project_id=project_id)
    _emit(project_id, "task.status_changed",
          {"task_id": task_id, "task": task["title"],
           "status": "Testing" if qa_mode else "In Progress", "progress": 5},
          workflow_run_id=run_id, task_id=task_id)
    audit("start_execution", "workflow_run", run_id,
          f"Started {'QA verification' if qa_mode else 'execution'} of task '{task['title']}'")

    ctrl = {"paused": False, "cancelled": False}
    handle = _spawn(_run_phases(run_id, ctrl, qa_mode))
    _registry[run_id] = {**ctrl, "task": handle}
    return {"run": get_run(run_id)}


async def _run_phases(run_id: str, ctrl: dict, qa_mode: bool = False):
    run = get_run(run_id)
    project_id, task_id, agent_id = run["project_id"], run["task_id"], run["agent_id"]
    task = query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    phases = QA_PHASES if qa_mode else PHASES
    evidence = []
    llm_tokens = {"in": 0, "out": 0}
    test_summary = ""
    try:
        for step, activity, tools, next_status, progress in phases:
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

                if not qa_mode and step == "implement" and tool == "file.write":
                    # REAL code generation (LLM when configured, runnable
                    # scaffold otherwise) written into the project workspace.
                    # rework_count > 0 means a previous attempt failed QA.
                    feedback = task["blocked_reason"] if task["rework_count"] else ""
                    gen = await asyncio.to_thread(
                        codegen.generate_implementation, project, task, run["agent_id"], feedback)
                    llm_tokens["in"] += gen["input_tokens"]
                    llm_tokens["out"] += gen["output_tokens"]
                    for rel in gen["files"]:
                        evidence.append("code:" + rel)
                        _emit(project_id, "workspace.file_written",
                              {"agent_id": agent_id, "path": rel, "task": task["title"],
                               "mode": gen["mode"]},
                              workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
                    if gen["summary"]:
                        evidence.append("summary:" + gen["summary"][:120])
                    if gen["error"]:
                        evidence.append("codegen-error:" + gen["error"][:120])
                    commit = workspace.commit_all(project_id)
                    if commit:
                        evidence.append("commit:" + commit)
                        _emit(project_id, "workspace.committed",
                              {"agent_id": agent_id, "commit": commit,
                               "files": gen["files"], "mode": gen["mode"]},
                              workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
                elif tool == "test.run":
                    # REAL build check for the detected stack: python
                    # (compile + FastAPI boot), dotnet build, go build+vet
                    # or npm install + build/tsc. A missing toolchain never
                    # fakes a pass — it fails with an install hint.
                    stack = toolchains.detect_stack(project)
                    bok, build_summary = await asyncio.to_thread(
                        toolchains.build_check, stack, project["workspace_path"])
                    evidence.append(("build-pass:" if bok else "build-fail:") + build_summary[:140])
                    _emit(project_id, "tool.completed",
                          {"agent_id": agent_id, "tool": f"build.smoke[{stack}]", "step": step,
                           "result": "ok" if bok else "failed", "summary": build_summary[:200]},
                          workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
                    if bok:
                        # REAL test run with the stack's standard runner.
                        ok, test_summary = await asyncio.to_thread(
                            toolchains.run_stack_tests, stack, project["workspace_path"])
                    else:
                        # A build error fails the whole check; tests would
                        # give a misleading verdict on broken code.
                        ok, test_summary = False, build_summary
                    evidence.append(("tests-pass:" if ok else "tests-fail:") + test_summary[:160])
                    _emit(project_id, "tool.completed",
                          {"agent_id": agent_id, "tool": f"{tool}[{stack}]", "step": step,
                           "result": "ok" if ok else "failed", "summary": test_summary[:200]},
                          workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
                    continue

                _emit(project_id, "tool.completed",
                      {"agent_id": agent_id, "tool": tool, "step": step,
                       "result": "ok"},
                      workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)

            if next_status != task["status"] or progress:
                update("tasks", task_id, {"status": next_status, "progress": progress,
                                          "updated_at": now()})
                _emit(project_id, "task.status_changed",
                      {"task_id": task_id, "task": task["title"],
                       "status": next_status, "progress": progress},
                      workflow_run_id=run_id, task_id=task_id)

        await _wait_if_paused(run_id, ctrl, project_id, task_id, agent_id)
        task = query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        in_tokens = llm_tokens["in"] or random.randint(1800, 5200)
        out_tokens = llm_tokens["out"] or random.randint(600, 2400)
        cost = round(in_tokens / 1_000_000 * 2.0 + out_tokens / 1_000_000 * 8.0, 4)
        insert("usage_records", {
            "id": new_id(), "workflow_run_id": run_id, "agent_id": agent_id,
            "model": "gpt-4.1", "input_tokens": in_tokens, "output_tokens": out_tokens,
            "cost_estimate": cost, "created_at": now(),
        })
        _emit(project_id, "usage.recorded",
              {"run_id": run_id, "input_tokens": in_tokens,
               "output_tokens": out_tokens, "cost_usd": cost},
              workflow_run_id=run_id, agent_id=agent_id)

        if qa_mode:
            await _finish_qa_run(run_id, ctrl, project_id, task_id, agent_id, task,
                                 in_tokens, out_tokens, cost, test_summary)
        else:
            await _finish_dev_run(run_id, ctrl, project_id, task_id, agent_id, task,
                                  evidence, in_tokens, out_tokens, cost, test_summary)
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


async def _finish_dev_run(run_id, ctrl, project_id, task_id, agent_id, task,
                          evidence, in_tokens, out_tokens, cost, test_summary=""):
    """Dev run finished building: hand off to QA (status 'Waiting QA') or,
    when no QA specialist exists on the team, mark the task Done directly."""
    qa = _pick_qa_member(project_id)
    if qa and qa["id"] != agent_id:
        update("tasks", task_id, {"status": "Waiting QA", "progress": 95,
                                  "assigned_agent_id": qa["id"], "qa_agent_id": agent_id,
                                  "evidence": "; ".join(evidence) if evidence else "dev complete",
                                  "updated_at": now()})
        update("workflow_runs", run_id, {"status": "Completed", "current_step": "qa-handoff",
                                         "completed_at": now()})
        _emit(project_id, "task.status_changed",
              {"task_id": task_id, "task": task["title"], "status": "Waiting QA", "progress": 95},
              workflow_run_id=run_id, task_id=task_id)
        _emit(project_id, "task.qa_handoff",
              {"task_id": task_id, "task": task["title"], "dev": task["assigned_agent_id"],
               "qa": qa["id"], "qa_name": qa["name"]},
              workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
        _emit(project_id, "workflow.completed",
              {"run_id": run_id, "task": task["title"], "evidence": evidence,
               "outcome": "dev-done", "qa_handoff": qa["name"],
               "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens,
                         "cost_usd": cost}},
              workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
        _set_agent(agent_id, "Completed", f"Completed (dev): {task['title']}",
                   task_id=task_id, project_id=project_id)
        await asyncio.sleep(1.2)
        _set_agent(agent_id, "Idle", "", task_id=None, project_id=project_id)
        audit("dev_completed", "workflow_run", run_id,
              f"Task '{task['title']}' handed off to QA ({qa['name']})")
        return

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
           "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens,
                     "cost_usd": cost}},
          workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
    _set_agent(agent_id, "Completed", f"Completed: {task['title']}", task_id=task_id, project_id=project_id)
    await asyncio.sleep(1.2)
    _set_agent(agent_id, "Idle", "", task_id=None, project_id=project_id)
    audit("execution_completed", "workflow_run", run_id, f"Task '{task['title']}' completed")


async def _finish_qa_run(run_id, ctrl, project_id, task_id, agent_id, task,
                         in_tokens, out_tokens, cost, test_summary=""):
    """QA run finished: verdict comes from the REAL pytest run when one was
    executed; falls back to a simulated verdict only when pytest is
    unavailable. Reject -> task back to the developer ('Rework') with an
    error summary. After MAX_REWORK_CYCLES rejections the task is escalated
    to a human instead of looping forever."""
    if test_summary.startswith(("tests passed", "pytest passed")):
        verdict_pass = True
        summary = ""
    elif test_summary.startswith(("tests FAILED", "pytest FAILED")):
        verdict_pass = False
        marker = "tests FAILED: " if test_summary.startswith("tests FAILED") else "pytest FAILED: "
        summary = f"QA test run failed: {test_summary[len(marker):].strip()}"
    elif test_summary.startswith("build FAILED"):
        verdict_pass = False
        summary = f"QA build smoke failed: {test_summary[len('build FAILED: '):].strip()}"
    else:
        verdict_pass = random.random() >= 0.15
        summary = "" if verdict_pass else f"QA found defects: {', '.join(random.sample(_QA_ISSUES, 1))}"
    dev_agent_id = task["qa_agent_id"] or agent_id
    dev = query_one("SELECT name FROM agents WHERE id = ?", (dev_agent_id,))
    qa_agent = query_one("SELECT name FROM agents WHERE id = ?", (agent_id,))

    if verdict_pass:
        evidence = (task["evidence"] + "; " if task["evidence"] else "") + "qa:passed"
        update("tasks", task_id, {"status": "Done", "progress": 100, "evidence": evidence,
                                  "blocked_reason": "", "updated_at": now()})
        update("workflow_runs", run_id, {"status": "Completed", "current_step": "done",
                                         "completed_at": now()})
        _emit(project_id, "task.status_changed",
              {"task_id": task_id, "task": task["title"], "status": "Done", "progress": 100},
              workflow_run_id=run_id, task_id=task_id)
        _emit(project_id, "task.qa_passed",
              {"task_id": task_id, "task": task["title"], "qa": qa_agent["name"] if qa_agent else agent_id},
              workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
        _emit(project_id, "workflow.completed",
              {"run_id": run_id, "task": task["title"], "outcome": "qa-passed",
               "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens,
                         "cost_usd": cost}},
              workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
        _set_agent(agent_id, "Completed", f"QA passed: {task['title']}",
                   task_id=task_id, project_id=project_id)
        await asyncio.sleep(1.2)
        _set_agent(agent_id, "Idle", "", task_id=None, project_id=project_id)
        audit("qa_passed", "workflow_run", run_id, f"QA approved task '{task['title']}'")
        return

    rework = (task["rework_count"] or 0) + 1
    if rework >= MAX_REWORK_CYCLES:
        update("tasks", task_id, {"status": "Blocked",
                                  "blocked_reason": f"{summary}. Failed QA {rework} times — needs human review.",
                                  "rework_count": rework, "updated_at": now()})
        update("workflow_runs", run_id, {"status": "Completed", "current_step": "qa-escalated",
                                         "completed_at": now()})
        _emit(project_id, "task.status_changed",
              {"task_id": task_id, "task": task["title"], "status": "Blocked",
               "blocked_reason": f"{summary}. Failed QA {rework} times — needs human review."},
              workflow_run_id=run_id, task_id=task_id)
        _emit(project_id, "qa.escalated",
              {"task_id": task_id, "task": task["title"], "summary": summary,
               "cycles": rework, "needs_human": True},
              workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
        audit("qa_escalated", "task", task_id,
              f"Task '{task['title']}' escalated to human after {rework} QA rejections")
    else:
        update("tasks", task_id, {"status": "Rework",
                                  "assigned_agent_id": dev_agent_id,
                                  "blocked_reason": f"Rework cycle {rework}: {summary}",
                                  "rework_count": rework, "updated_at": now()})
        update("workflow_runs", run_id, {"status": "Completed", "current_step": "qa-rejected",
                                         "completed_at": now()})
        _emit(project_id, "task.status_changed",
              {"task_id": task_id, "task": task["title"], "status": "Rework",
               "blocked_reason": f"Rework cycle {rework}: {summary}"},
              workflow_run_id=run_id, task_id=task_id)
        _emit(project_id, "task.qa_rejected",
              {"task_id": task_id, "task": task["title"], "qa": qa_agent["name"] if qa_agent else agent_id,
               "dev": dev["name"] if dev else dev_agent_id, "summary": summary, "cycle": rework},
              workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
        audit("qa_rejected", "workflow_run", run_id,
              f"QA rejected task '{task['title']}' (cycle {rework}): {summary}")
    _set_agent(agent_id, "Completed", f"QA rejected: {task['title']}",
               task_id=task_id, project_id=project_id)
    await asyncio.sleep(1.2)
    _set_agent(agent_id, "Idle", "", task_id=None, project_id=project_id)


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
        "AND status IN ('Todo','Ready','Rework','Waiting QA') AND assigned_agent_id IS NOT NULL "
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
    idle_rounds = 0
    while not ctrl["cancelled"]:
        eligible = _eligible_tasks(project_id)
        if not eligible:
            remaining = query(
                "SELECT COUNT(*) AS n FROM tasks WHERE project_id = ? AND sprint_id IS NOT NULL "
                "AND status NOT IN ('Done','Cancelled')", (project_id,))[0]["n"]
            if remaining == 0:
                _emit(project_id, "project.updated",
                      {"note": "Sprint execution complete", "remaining_tasks": 0})
                break
            # Nothing runnable right now: tasks may exist unassigned (newly
            # drafted or freed by a state change). Re-attempt role-based
            # assignment instead of giving up; give up only after a stretch
            # with no progress at all.
            auto_assign_tasks(project_id)
            idle_rounds += 1
            if idle_rounds > 60:
                _emit(project_id, "project.updated",
                      {"note": "No eligible tasks: blocked/waiting items remain",
                       "remaining_tasks": remaining})
                break
            await asyncio.sleep(2.0)
            continue
        idle_rounds = 0
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


# Role families group equivalent roles so a dev task never lands on QA/BA
# when a developer exists on the team.
ROLE_FAMILIES = {
    "dev": ("Senior Developer", "Developer", "Software Engineer", "Backend Developer",
            "Frontend Developer", "Full Stack Developer"),
    "qa": ("QA Engineer", "QA Analyst", "Test Engineer", "Tester"),
    "architecture": ("Solution Architect", "Tech Lead", "Technical Lead"),
    "requirements": ("Business Analyst", "Product Owner", "Product Manager", "Scrum Master"),
    "devops": ("DevOps Engineer", "Platform Engineer", "SRE", "Site Reliability Engineer"),
    "design": ("UI/UX Designer", "Designer"),
}

# Keyword families for inferring what kind of work a task is. Order matters:
# more specific families (qa, devops) are checked before generic ones.
_FAMILY_KEYWORDS = [
    ("qa", ("test plan", "test case", "qa", "test suite", "unit test", "regression",
            "quality assurance", "bug report", "test coverage")),
    ("devops", ("deploy", " ci", " cd", "pipeline", "docker", "infra", "release",
                "devops", "monitoring", "observability")),
    ("requirements", ("requirement", "backlog", "user stor", "acceptance criteria",
                      "scope", "research", "evaluate", "evaluate and select")),
    ("architecture", ("architecture", "design system", "system design", "contract",
                      "data model", "schema", "tech stack", "caching", "geo hierarchy")),
    ("design", ("ui/ux", "wireframe", "mockup", "figma", "visual design")),
    ("dev", ("implement", "build", "code", "develop", "integration", "endpoint", "api",
             "refactor", "scaffold", "ingestion", "component", "service", "feature", "fix")),
]

_FAMILY_LABEL = {"dev": "development", "qa": "QA", "architecture": "architecture",
                 "requirements": "business analysis", "devops": "DevOps", "design": "design"}


def _family_for_task(task) -> str | None:
    text = (task["title"] + " " + (task["description"] or "")).lower()
    for family, keywords in _FAMILY_KEYWORDS:
        if any(k in text for k in keywords):
            return family
    return None


def _family_of_role(role_name: str) -> str | None:
    rl = (role_name or "").lower()
    for family, names in ROLE_FAMILIES.items():
        if any(n.lower() == rl or n.lower() in rl for n in names):
            return family
    return None


def _pick_member(members, family: str | None):
    """Pick the best member for a task family. Family tasks go to a role
    specialist first, then to a generalist (role outside all families) —
    never to a wrong specialist (a dev task is never handed to QA/BA).
    Returns (member, matched)."""
    if not members:
        return None, False

    def load(m):
        return (_agent_load(m["id"]), m["lifecycle_state"] != "Idle", m["name"])

    if family:
        fam = [m for m in members if _family_of_role(m["role_name"]) == family]
        if fam:
            return min(fam, key=load), True
        generalists = [m for m in members if _family_of_role(m["role_name"]) is None]
        if generalists:
            idle_g = [m for m in generalists if m["lifecycle_state"] == "Idle"]
            return min(idle_g or generalists, key=load), False
        return None, False
    idle = [m for m in members if m["lifecycle_state"] == "Idle"]
    if idle:
        return min(idle, key=load), False
    return min(members, key=load), False


def _pick_qa_member(project_id: str):
    members = _team_members(project_id) or []
    qa, _ = _pick_member(members, "qa")
    return qa


def _team_members(project_id):
    project = query_one("SELECT team_id FROM projects WHERE id = ?", (project_id,))
    if not project or not project["team_id"]:
        return None
    return query(
        "SELECT a.*, r.name AS role_name FROM team_agents ta JOIN agents a ON a.id = ta.agent_id "
        "JOIN roles r ON r.id = a.role_id WHERE ta.team_id = ? AND ta.active = 1 ORDER BY a.name",
        (project["team_id"],))


def _agent_load(agent_id):
    return query_one(
        "SELECT COUNT(*) AS n FROM tasks WHERE assigned_agent_id = ? "
        "AND status IN ('Ready','In Progress','Review','Testing','Waiting QA','Rework')", (agent_id,))["n"]


def auto_assign_tasks(project_id: str) -> dict:
    """Assign unassigned sprint tasks to team members, matching role keywords
    in the task title/description first, then least-loaded as fallback."""
    sprint = query_one("SELECT * FROM sprints WHERE project_id = ? AND status = 'Active'", (project_id,))
    if not sprint:
        return {"assigned": 0, "note": "No active sprint"}
    members = _team_members(project_id)
    if not members:
        return {"assigned": 0, "note": "No team aligned to this project"}
    if not any(m["lifecycle_state"] == "Idle" for m in members):
        return {"assigned": 0, "note": "No idle agents on the aligned team"}
    tasks = query(
        "SELECT * FROM tasks WHERE project_id = ? AND sprint_id = ? "
        "AND status IN ('Todo','Ready') AND assigned_agent_id IS NULL ORDER BY priority, created_at",
        (project_id, sprint["id"]))
    assigned = 0
    for t in tasks:
        family = _family_for_task(t)
        pick, matched = _pick_member(members, family)
        if not pick:
            continue
        update("tasks", t["id"], {"assigned_agent_id": pick["id"], "updated_at": now()})
        note = "" if matched else f" (no {_FAMILY_LABEL.get(family, 'matching')} specialist on the team)"
        emit_event(project_id, "task.assigned",
                   {"task_id": t["id"], "task": t["title"], "agent": pick["name"],
                    "role": pick["role_name"], "family": family, "role_matched": matched,
                    "note": note.strip(), "auto": True},
                   task_id=t["id"], agent_id=pick["id"])
        assigned += 1
    return {"assigned": assigned, "note": ""}


def _resolve_sprint(project_id: str, sprint_ref):
    """Resolve a sprint by id, exact/partial name, or number ("2",
    "sprint 2", "sprint-2"). Falls back to the nth sprint created. Returns
    None when the reference cannot be matched."""
    if not sprint_ref:
        return None
    ref = str(sprint_ref).strip()
    if not ref:
        return None
    s = query_one("SELECT * FROM sprints WHERE project_id = ? AND id = ?", (project_id, ref))
    if s:
        return s
    s = query_one("SELECT * FROM sprints WHERE project_id = ? AND lower(name) = lower(?)",
                  (project_id, ref))
    if s:
        return s
    rows = query("SELECT * FROM sprints WHERE project_id = ? ORDER BY created_at", (project_id,))
    m = re.search(r"\d+", ref)
    if m:
        n = m.group(0)
        for row in rows:
            if re.search(rf"(?<!\d){re.escape(n)}(?!\d)", row["name"] or ""):
                return row
        if ref.isdigit() and 1 <= int(ref) <= len(rows):
            return rows[int(ref) - 1]
    return query_one("SELECT * FROM sprints WHERE project_id = ? AND lower(name) LIKE lower(?) "
                     "ORDER BY created_at LIMIT 1", (project_id, f"%{ref}%"))


_TASK_PLAN_SYSTEM = """You are a technical product owner planning the next sprint.
Return ONLY a JSON object (no prose, no fences):
{"tasks": [{"title": "...", "description": "...", "acceptance_criteria": "...", "points": 1-8, "priority": 1-3}]}
Plan 3-6 concrete, implementable tasks that build on the already-completed work.
Titles are short (< 70 chars) and describe deliverables, not documents."""


def _generate_sprint_tasks(project, sprint) -> list[dict]:
    """Autonomously draft the next phase of tasks for an empty sprint.
    Returns a validated list of task dicts ([] when nothing usable)."""
    gw, model = codegen.resolve_llm(project)
    if not (gw and model):
        return []
    from .chatbot import _extract_json
    done = [r["title"] for r in query(
        "SELECT title FROM tasks WHERE project_id = ? AND status = 'Done' ORDER BY created_at",
        (project["id"],))]
    existing_tree = codegen._existing_tree(project["workspace_path"])
    user = (f"PROJECT: {project['name']}\nGOAL: {project['goal'] or 'n/a'}\n"
            f"TECH STACK: {project['technology_stack'] or 'n/a'}\n"
            f"SPRINT TO PLAN: {sprint['name']}\nSPRINT GOAL: {sprint['goal'] or 'n/a'}\n"
            f"ALREADY COMPLETED (do not repeat): {'; '.join(done) if done else 'nothing yet'}\n"
            f"CURRENT WORKSPACE FILES:\n{existing_tree}\n\n"
            "Draft the next phase of tasks for this sprint.")
    try:
        raw = codegen.call_llm(gw, model, _TASK_PLAN_SYSTEM, user, max_tokens=2000)
    except Exception:
        return []
    data = _extract_json(raw.get("text") or "")
    tasks = []
    if data and isinstance(data.get("tasks"), list):
        for t in data["tasks"]:
            if not isinstance(t, dict):
                continue
            title = str(t.get("title") or "").strip()
            if not title:
                continue
            try:
                pts = max(1, min(8, int(t.get("points") or 3)))
            except (TypeError, ValueError):
                pts = 3
            try:
                prio = max(1, min(3, int(t.get("priority") or 2)))
            except (TypeError, ValueError):
                prio = 2
            tasks.append({"title": title[:80], "description": str(t.get("description") or "")[:500],
                          "acceptance_criteria": str(t.get("acceptance_criteria") or "")[:500],
                          "points": pts, "priority": prio})
    return tasks[:6]


def start_sprint_execution(project_id: str, sprint_ref=None):
    # Blockers that genuinely need a human decision:
    project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        return {"error": "Project not found"}
    sprint = _resolve_sprint(project_id, sprint_ref)
    if sprint is None and not sprint_ref:
        sprint = query_one("SELECT * FROM sprints WHERE project_id = ? AND status = 'Active'", (project_id,))
    if sprint is None:
        planned = query_one(
            "SELECT * FROM sprints WHERE project_id = ? AND status = 'Planned' ORDER BY created_at", (project_id,))
        if not planned:
            return {"error": "No sprint to start — create one first (e.g. say \"create a sprint\")"}
        sprint = planned
    if project_id in _schedulers:
        return {"error": "Sprint execution already running"}

    task_count = query_one(
        "SELECT COUNT(*) AS n FROM tasks WHERE project_id = ? AND sprint_id = ? "
        "AND status NOT IN ('Done','Cancelled')",
        (project_id, sprint["id"]))["n"]
    if task_count == 0:
        # Stealth mode: an empty sprint is not a blocker — draft the next
        # phase of tasks from the project goal and start immediately.
        drafted = _generate_sprint_tasks(project, sprint)
        if drafted:
            ts = now()
            for t in drafted:
                insert("tasks", {"id": new_id(), "project_id": project_id, "sprint_id": sprint["id"],
                                 "title": t["title"], "description": t["description"],
                                 "acceptance_criteria": t["acceptance_criteria"],
                                 "story_points": t["points"], "priority": t["priority"],
                                 "status": "Todo", "created_at": ts, "updated_at": ts})
            audit("generate_sprint_tasks", "sprint", sprint["id"],
                  f"Auto-drafted {len(drafted)} task(s) for '{sprint['name']}'")
            _emit(project_id, "sprint.tasks_drafted",
                  {"sprint": sprint["name"], "tasks": [t["title"] for t in drafted]})
            drafted_n = len(drafted)
        else:
            return {"error": f"Sprint '{sprint['name']}' has no open tasks and no AI model is "
                             "configured to draft them — add tasks before starting"}
    else:
        drafted_n = 0

    # Activate the planned sprint, auto-assign, then run autonomously.
    if sprint["status"] != "Active":
        # Only one sprint per project may be Active at a time.
        execute("UPDATE sprints SET status = 'Planned' WHERE project_id = ? AND status = 'Active' AND id != ?",
                (project_id, sprint["id"]))
        execute("UPDATE sprints SET status = 'Active' WHERE id = ?", (sprint["id"],))
        sprint = query_one("SELECT * FROM sprints WHERE id = ?", (sprint["id"],))
        _emit(project_id, "sprint.activated",
              {"sprint": sprint["name"], "note": "Sprint activated automatically by start command"})
    assign = auto_assign_tasks(project_id)
    ctrl = {"cancelled": False}
    handle = _spawn(_run_sprint(project_id, ctrl))
    _schedulers[project_id] = {**ctrl, "task": handle}
    return {"ok": True, "sprint": sprint["name"], "auto_assigned": assign["assigned"],
            "auto_drafted": drafted_n}


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
