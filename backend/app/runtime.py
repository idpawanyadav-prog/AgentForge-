"""Simulated agent execution runtime.

Mimics the MAF workflow runtime contract described in the spec: durable
workflow runs, streamed execution events with per-project sequence numbers,
pause/resume/cancel/retry controls, usage recording and audit events. Model
calls are simulated so the platform is fully demoable without real provider
credentials.
"""
import asyncio
import json
import logging
import random
import re
import sqlite3
import threading
import time

from . import db, workspace, codegen, toolchains, po, sprint_gate, browser_test, memory
from . import config, governance, budget, flows
from .db import emit_event, execute, insert, now, new_id, query_one, query, update, audit
from .task_registry import background_tasks

logger = logging.getLogger(__name__)

# run_id -> {"task": asyncio.Task, "paused": bool, "cancelled": bool}
_registry: dict = {}
# Locks guard dict mutations from concurrent sync/async callers.
_registry_lock = threading.Lock()
# project_id -> {"task": asyncio.Task, "cancelled": bool}
_schedulers: dict = {}
_schedulers_lock = threading.Lock()
# Serializes the check-then-insert claim in start_execution: the scheduler
# thread, chatbot commands and the REST API can otherwise both see "no
# active run" and double-claim the same task/agent.
_claim_lock = threading.Lock()

# Background fire-and-forget coroutines are tracked centrally by
# app.task_registry (see _spawn below); _loop is kept for the stop path's
# call_soon_threadsafe cancellation.

TASK_STATUSES = ["Todo", "Ready", "In Progress", "Blocked", "Review", "Testing",
                 "Waiting QA", "Code Review", "SA Review", "BA Review", "Rework",
                 "Done", "Cancelled"]
AGENT_STATES = ["Idle", "Working", "Waiting", "Blocked", "Failed", "Paused", "Completed"]

# Track provider health per gateway; a failure in one project must not
# degrade another project's independent gateway.
_gateway_health: dict[str, dict[str, int | bool]] = {}
_LLM_HEALTH_THRESHOLD = 3  # consecutive failures -> mark unhealthy
_llm_health_lock = threading.Lock()

_loop: asyncio.AbstractEventLoop | None = None

# Concurrency guard for module-cache mutations.
_module_cache_lock = threading.Lock()

# Scheduler idle thresholds (rounds of 2-second sleeps before giving up).
IDLE_GIVE_UP_BLOCKED = config.SCHEDULER_IDLE_GIVE_UP_BLOCKED
IDLE_GIVE_UP_CLEAN = config.SCHEDULER_IDLE_GIVE_UP_CLEAN

# Event retention is intentionally bounded per project. The scheduler checks
# this periodically without adding another long-running background service.
_EVENT_ARCHIVE_IDLE_INTERVAL = 30
_DEFAULT_EVENT_KEEP = 10000


def _sanitize_exception(exc: Exception) -> str:
    """Extract a user-friendly message from an exception — first line only,
    no stack traces, no internal file paths."""
    msg = str(exc).strip()
    # Take only the first line to avoid tracebacks / chained messages
    msg = msg.splitlines()[0].strip()
    # Strip common internal path prefixes
    import re as _re
    msg = _re.sub(r'(?:\\\\|/)[^\\\\/]*agent_forge[^\\\\/]*', "", msg, flags=_re.IGNORECASE)
    msg = msg.strip(" :;-\\/")
    return msg or "An unexpected error occurred"


def set_loop(loop: asyncio.AbstractEventLoop):
    """Capture the main event loop so worker threads can spawn coroutines on
    it (and so the shared background-task registry can do the same)."""
    global _loop
    _loop = loop
    background_tasks.set_loop(loop)


def _spawn(coro):
    """Schedule a coroutine on the main loop (thread-safe) and track it in the
    shared registry so background failures are logged and shutdown cancels it."""
    return background_tasks.create(coro)

# (step, activity, tools, next_task_status, progress)
PHASES = [
    ("analyze", "Analyzing task requirements and acceptance criteria", [], "In Progress", 15),
    ("plan", "Drafting implementation plan from persona instructions", ["memory.read"], "In Progress", 25),
    ("implement", "Writing code in project workspace", ["file.write", "file.read"], "In Progress", 55),
    ("build-test", "Building and running unit tests", ["shell.run", "test.run", "browser.test"], "Testing", 75),
    ("review", "Self-review against definition of done", ["code-review"], "Review", 90),
    ("finalize", "Recording evidence and completing task", [], "Done", 100),
]

# QA verification phases, run by a QA-role agent after dev completion.
QA_PHASES = [
    ("verify", "QA: reviewing implementation against acceptance criteria", ["memory.read", "file.read"], "Testing", 30),
    ("qa-test", "QA: running test suite and browser checks", ["shell.run", "test.run", "browser.test"], "Testing", 70),
    ("qa-report", "QA: writing verdict and defect summary", ["code-review"], "Testing", 95),
]

# Deterministic review gates (V3): a single verdict phase each, run by the
# Solution Architect / Business Analyst before the task may reach QA.
SA_REVIEW_PHASES = [
    ("sa-review", "Solution Architect: reviewing architecture alignment", ["code-review"], "SA Review", 92),
]
BA_REVIEW_PHASES = [
    ("ba-review", "Business Analyst: reviewing functional alignment", ["code-review"], "BA Review", 92),
]
CR_REVIEW_PHASES = [
    ("cr-review", "Senior Developer: reviewing code quality and correctness", ["code-review"], "Code Review", 92),
]

_PHASES_BY_MODE = {
    "dev": PHASES, "qa": QA_PHASES, "sa": SA_REVIEW_PHASES, "ba": BA_REVIEW_PHASES,
    "cr": CR_REVIEW_PHASES,
}

# Modes whose run is a single LLM review-gate verdict (vs. authoring/testing).
_REVIEW_MODES = ("sa", "ba", "cr")
# review mode -> the task status / gate label it represents
_MODE_STAGE = {"sa": "SA Review", "ba": "BA Review", "cr": "Code Review"}

# Review-stage status -> (task_reviews.reviewer_type, team role family)
_REVIEW_STAGES = {
    "SA Review": ("solution_architect", "architecture"),
    "BA Review": ("business_analyst", "requirements"),
    "Code Review": ("code_review", "dev"),
}

_RUN_MODE_LABEL = {"dev": "", "qa": "QA: ", "sa": "SA review: ", "ba": "BA review: ",
                   "cr": "Code review: "}

# After this many QA rejections the task is escalated to a human.
MAX_REWORK_CYCLES = config.MAX_REWORK_CYCLES

# Dev self-test: when the dev's own build/tests fail, retry the
# implementation this many times with the exact error as feedback before
# the task is blocked for review. Code that still fails is NEVER handed
# to QA — QA would just bounce back the same error.
SELF_FIX_ATTEMPTS = config.SELF_FIX_ATTEMPTS

# Per-phase timeout (seconds). A hung LLM call or subprocess must not
# block a workflow indefinitely.
PHASE_TIMEOUT_S = config.PHASE_TIMEOUT_S


def _emit(project_id, event_type, payload, **kw):
    return emit_event(project_id, event_type, payload, **kw)


async def _wait_bounded(coro, seconds: float, label: str):
    """Hard backstop for a blocking to_thread step. Worker threads cannot be
    killed, so codegen/review calls ALSO get a deadline argument to wind down
    on their own; this fires only if a thread wedges (hung subprocess, stuck
    pipe) — the run then fails visibly (task -> Blocked with a clear reason)
    instead of hanging the sprint forever. PHASE_TIMEOUT_S was defined for
    exactly this and previously applied nowhere."""
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"{label} exceeded its {seconds:.0f}s phase budget (PHASE_TIMEOUT_S)")


async def _codegen_impl(project, task, agent_id, feedback, pinned_ref):
    deadline = time.monotonic() + PHASE_TIMEOUT_S
    return await _wait_bounded(
        asyncio.to_thread(codegen.generate_implementation, project, task,
                          agent_id, feedback, pinned_ref=pinned_ref,
                          deadline=deadline),
        PHASE_TIMEOUT_S + 60, "Codegen")


async def _browser_smoke(project):
    # run_smoke self-limits (TOTAL_BUDGET_S + boot + lock waits); this is the
    # deadlock backstop so a wedged app/browser can never hang the run.
    return await _wait_bounded(
        asyncio.to_thread(browser_test.run_smoke, project),
        browser_test.TOTAL_BUDGET_S * 2 + 180, "Browser smoke")


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


def active_run_for_agent(agent_id):
    return query_one(
        "SELECT * FROM workflow_runs WHERE agent_id = ? AND status IN ('Running','Paused','Queued')",
        (agent_id,))


def mark_llm_healthy(gateway_id: str):
    with _llm_health_lock:
        _gateway_health[gateway_id] = {"healthy": True, "consecutive_failures": 0}


def mark_llm_failed(gateway_id: str):
    with _llm_health_lock:
        health = _gateway_health.setdefault(
            gateway_id, {"healthy": True, "consecutive_failures": 0})
        health["consecutive_failures"] += 1
        if health["consecutive_failures"] >= _LLM_HEALTH_THRESHOLD:
            health["healthy"] = False

def is_llm_healthy(gateway_id: str) -> bool:
    with _llm_health_lock:
        return bool(_gateway_health.get(gateway_id, {}).get("healthy", True))


def reset_llm_health(gateway_id: str):
    """Call when the provider may have recovered (e.g. after a delay)."""
    mark_llm_healthy(gateway_id)


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



def _run_mode(task) -> str:
    """Which pipeline a start_execution claim runs: the waiting status of the
    task selects dev / QA / SA-review / BA-review."""
    return _MODE_BY_STATUS.get(task["status"], "dev")


_MODE_BY_STATUS = {"Waiting QA": "qa", "SA Review": "sa", "BA Review": "ba",
                   "Code Review": "cr"}


def start_execution(project_id: str, task_id: str, idempotency_key: str | None = None):
    # The eligibility checks below are check-then-act; holding _claim_lock
    # from validation through the run-row insert makes the claim atomic, so
    # concurrent scheduler/API/chatbot callers can't both start a run for
    # the same task or agent.
    with _claim_lock:
        return _start_execution_locked(project_id, task_id, idempotency_key)


def _start_execution_locked(project_id: str, task_id: str,
                            idempotency_key: str | None = None):
    if idempotency_key:
        existing = query_one(
            "SELECT * FROM workflow_runs WHERE idempotency_key = ?", (idempotency_key,))
        if existing:
            if existing["project_id"] != project_id or existing["task_id"] != task_id:
                return {"error": "Idempotency key was already used for another execution"}
            return {"run": existing, "idempotent_replay": True}

    task = query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if task and task["project_id"] != project_id:
        return {"error": "Task does not belong to project"}
    ok, reason = validate_task_ready(task)
    if not ok:
        return {"error": reason}
    # V3 Step 1 lifecycle gate (opt-in per project): with governance on,
    # no task may run outside a development-permitting lifecycle state.
    g_ok, g_reason = governance.may_execute(project_id)
    if not g_ok:
        return {"error": f"TASK_BLOCKED_BY_LIFECYCLE: {g_reason}"}
    # Per-project LLM cost budget (AGENT_DEV_SPEEDUP 3.3). 0 = unlimited.
    b_ok, b_reason = budget.may_spend(project_id)
    if not b_ok:
        return {"error": f"TASK_BLOCKED_BY_BUDGET: {b_reason}"}

    # Sprint Gatekeeper (spec §11): backend validates sprint eligibility at
    # claim time — tasks of locked/future sprints are rejected outright.
    auth = sprint_gate.authorize_task(project_id, task_id)
    if not auth["allowed"]:
        _emit(project_id, "task.blocked_by_sprint_gate",
              {"task_id": task_id, "task": task["title"] if task else task_id,
               "reason": auth["reason"]}, task_id=task_id)
        return {"error": f"TASK_BLOCKED_BY_SPRINT_GATE: {auth['reason']}"}

    agent = query_one("SELECT a.*, r.name AS role_name FROM agents a JOIN roles r ON r.id = a.role_id WHERE a.id = ?",
                      (task["assigned_agent_id"],))
    if not agent:
        return {"error": "Assigned agent not found"}
    # Parallel execution guard: one active run per agent at a time.
    if active_run_for_agent(agent["id"]):
        return {"error": f"{agent['name']} is already working on another task"}
    persona = query_one("SELECT * FROM personas WHERE id = ?", (agent["persona_id"],))
    binding = None
    model_name = "unknown"
    input_cost_per_m = config.LLM_INPUT_COST_PER_M
    output_cost_per_m = config.LLM_OUTPUT_COST_PER_M
    if agent["model_binding_id"]:
        binding = query_one(
            "SELECT mb.*, gm.display_name AS model_name, gm.provider_model_id, "
            "gm.input_cost_per_m, gm.output_cost_per_m FROM model_bindings mb "
            "JOIN gateway_models gm ON gm.id = mb.model_id WHERE mb.id = ?",
            (agent["model_binding_id"],))
        if binding:
            model_name = binding["provider_model_id"]
            if binding.get("input_cost_per_m"):
                input_cost_per_m = binding["input_cost_per_m"]
                output_cost_per_m = binding["output_cost_per_m"]

    mode = _run_mode(task)
    review_mode = mode in _REVIEW_MODES
    run_id = new_id()
    try:
        insert("workflow_runs", {
            "id": run_id, "project_id": project_id, "task_id": task_id,
            "agent_id": agent["id"], "status": "Running", "mode": mode,
            "current_step": _PHASES_BY_MODE[mode][0][0], "started_at": now(),
            "idempotency_key": idempotency_key,
        })
    except sqlite3.IntegrityError:
        existing = query_one("SELECT * FROM workflow_runs WHERE idempotency_key = ?",
                             (idempotency_key,))
        if existing and existing["project_id"] == project_id and existing["task_id"] == task_id:
            return {"run": existing, "idempotent_replay": True}
        return {"error": "Idempotency key was already used for another execution"}
    if review_mode:
        # Waiting-review statuses stand on their own; only refresh progress.
        update("tasks", task_id, {"progress": 88, "updated_at": now()})
    else:
        update("tasks", task_id, {"status": "In Progress" if mode == "dev" else "Testing",
                                  "progress": 5,
                                  "blocked_reason": "", "updated_at": now()})
    _emit(project_id, "workflow.started",
          {"run_id": run_id, "task": task["title"], "agent": agent["name"],
           "model": model_name, "persona_version": persona["version"],
           "mode": mode},
          workflow_run_id=run_id, task_id=task_id, agent_id=agent["id"])
    _set_agent(agent["id"], "Working",
               _RUN_MODE_LABEL[mode] + f"Starting: {task['title']}",
               task_id=task_id, project_id=project_id)
    if not review_mode:
        _emit(project_id, "task.status_changed",
              {"task_id": task_id, "task": task["title"],
               "status": "Testing" if mode == "qa" else "In Progress", "progress": 5},
              workflow_run_id=run_id, task_id=task_id)
    audit("start_execution", "workflow_run", run_id,
          f"Started {mode} run of task '{task['title']}'")

    # The live worker and pause/resume/cancel (and stop_sprint_execution) all
    # mutate THIS dict, so it must be the same object stored in the registry —
    # a copy would strand cancellation flags away from the running coroutine.
    ctrl = {"paused": False, "cancelled": False,
            "input_cost_per_m": input_cost_per_m, "output_cost_per_m": output_cost_per_m,
            "model_used": model_name, "pinned_ref": None}
    handle = _spawn(_run_phases(run_id, ctrl, mode))
    ctrl["task"] = handle
    with _registry_lock:
        _registry[run_id] = ctrl
    return {"run": get_run(run_id)}


async def _run_phases(run_id: str, ctrl: dict, mode: str = "dev"):
    run = get_run(run_id)
    project_id, task_id, agent_id = run["project_id"], run["task_id"], run["agent_id"]
    task = query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    phases = _PHASES_BY_MODE[mode]
    review_mode = mode in _REVIEW_MODES
    evidence = []
    llm_tokens = {"in": 0, "out": 0}
    test_summary = ""
    selftest_failed = False
    selftest_reason = ""
    # Isolation: dev/QA runs work on a TEMP copy of the workspace, so no two
    # agents ever edit the same file live and QA testing cannot disturb
    # on-going work. Changed files merge back (per-file leases) when the run
    # finishes; cancelled/failed runs discard their copy.
    run_ws, ws_manifest = "", {}
    merge_error = ""
    isolation_failed = False
    if mode in ("dev", "qa"):
        run_ws, ws_manifest = await asyncio.to_thread(
            workspace.begin_run_workspace, project_id, run_id)
        isolation_failed = bool(workspace.get_workspace(project_id) and not run_ws)
        if run_ws:
            project = dict(project, workspace_path=run_ws)
            _emit(project_id, "workspace.run_isolated",
                  {"agent_id": agent_id, "task": task["title"], "run_id": run_id},
                  workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)

    def _record_model(gen):
        """Persist the chain member that actually served this codegen call onto
        the run control so later calls in the SAME task reuse it (the chosen
        model is pinned per task). A new task run resets it, so the chain is
        only re-checked from the top when the agent moves on or the sprint
        restarts. Emits a fallback event whenever the agent had to switch."""
        for fallback in gen.get("fallbacks") or []:
            gateway_id = fallback.get("gateway_id")
            if gateway_id and codegen.is_llm_outage(fallback.get("error") or ""):
                mark_llm_failed(gateway_id)
        if gen.get("used_gateway_id"):
            mark_llm_healthy(gen["used_gateway_id"])
        used_ref = gen.get("pinned_ref")
        if used_ref and ctrl.get("pinned_ref") != used_ref:
            ctrl["pinned_ref"] = used_ref
            if gen.get("used_provider_model_id"):
                ctrl["model_used"] = gen["used_provider_model_id"]
            mid = gen.get("used_model_id")
            if mid:
                row = query_one("SELECT input_cost_per_m, output_cost_per_m "
                                "FROM gateway_models WHERE id = ?", (mid,))
                if row and row["input_cost_per_m"]:
                    ctrl["input_cost_per_m"] = row["input_cost_per_m"]
                    ctrl["output_cost_per_m"] = (row["output_cost_per_m"]
                                                 or config.LLM_OUTPUT_COST_PER_M)
        skipped = gen.get("fallbacks") or []
        if skipped:
            _emit(project_id, "agent.model_fallback",
                  {"agent_id": agent_id, "task": task["title"],
                   "skipped": skipped, "using": ctrl.get("model_used") or used_ref},
                  workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
            trail = "→".join((s.get("model") or "?") for s in skipped)
            evidence.append(f"model-fallback:{trail}→"
                            f"{ctrl.get('model_used') or gen.get('used_provider_model_id') or used_ref or 'ok'}"[:160])
    # Module-fix tasks (sprint gate rework / legacy health tasks) verify
    # only their own test module, not the whole suite (other modules'
    # pre-existing failures belong to their own tasks).
    health_scope = None
    if (task["title"] or "").startswith(("Workspace health:", "Sprint gate rework:")):
        m = re.search(r"in (tests/\S+\.py)", task["title"] or "")
        if m:
            health_scope = m.group(1)
    # Pre-work baseline of failing tests: legacy failures (other in-flight
    # tasks, stale checks) must not fail THIS task's gate — only NEW
    # failures introduced by the work count.
    baseline_failures = None
    try:
        if not isolation_failed and toolchains.detect_stack(project, project["workspace_path"]) == "python":
            baseline_failures = await asyncio.to_thread(
                toolchains.failing_tests, "python", project["workspace_path"],
                health_scope)
    except Exception:
        logger.debug("baseline test detection failed for run %s; continuing without it", run_id, exc_info=True)
        baseline_failures = None
    try:
        if isolation_failed:
            raise RuntimeError("Isolated workspace copy failed; live files were not modified")
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

                if mode == "dev" and step == "implement" and tool == "file.write":
                    # REAL code generation (LLM when configured, runnable
                    # scaffold otherwise) written into the project workspace.
                    # rework_count > 0 means a previous attempt failed QA.
                    feedback = task["blocked_reason"] if task["rework_count"] else ""
                    gen = await _codegen_impl(project, task, run["agent_id"], feedback,
                                              ctrl.get("pinned_ref"))
                    _record_model(gen)
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
                    if not run_ws:  # not isolated: commit live directly
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

                    async def _deps():
                        """Module pre-flight: before any module is used,
                        verify it resolves in the project environment and
                        install missing ones into the project lib/ folder."""
                        d = await asyncio.to_thread(
                            toolchains.check_modules, project["workspace_path"], stack)
                        if d.get("installed"):
                            evidence.append("deps-installed:" + ",".join(d["installed"])[:140])
                            _emit(project_id, "tool.completed",
                                  {"agent_id": agent_id, "tool": "deps.check", "step": step,
                                   "result": "ok",
                                   "summary": "installed to project lib/: "
                                              + ", ".join(d["installed"])[:180]},
                                  workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
                        elif not d.get("ok"):
                            evidence.append("deps-failed:" + str(d.get("missing"))[:120])
                        return d

                    await _deps()
                    bok, build_summary = await asyncio.to_thread(
                        toolchains.build_check, stack, project["workspace_path"])
                    if not bok and mode == "dev":
                        # Dev self-test: the dev knows the code is broken —
                        # fix it now instead of shipping it to QA.
                        for attempt in range(1, SELF_FIX_ATTEMPTS + 1):
                            _emit(project_id, "agent.activity",
                                  {"agent_id": agent_id, "step": step,
                                   "activity": f"Self-fix {attempt}/{SELF_FIX_ATTEMPTS}: "
                                               f"{build_summary[:110]}"},
                                  workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
                            gen = await _codegen_impl(
                                project, task, run["agent_id"],
                                f"Your code FAILED the build check: {build_summary}\n"
                                "Return the corrected complete file(s).",
                                ctrl.get("pinned_ref"))
                            _record_model(gen)
                            if gen.get("error") and codegen.is_llm_outage(gen["error"]):
                                bok = False
                                build_summary = gen["error"]
                                break  # LLM down — retrying just burns quota
                            commit = None if run_ws else workspace.commit_all(project_id)
                            if commit:
                                evidence.append("commit:" + commit)
                            await _deps()  # new imports from the fix round
                            bok, build_summary = await asyncio.to_thread(
                                toolchains.build_check, stack, project["workspace_path"])
                            if bok or ctrl["cancelled"]:
                                break
                    evidence.append(("build-pass:" if bok else "build-fail:") + build_summary[:140])
                    _emit(project_id, "tool.completed",
                          {"agent_id": agent_id, "tool": f"build.smoke[{stack}]", "step": step,
                           "result": "ok" if bok else "failed", "summary": build_summary[:200]},
                          workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
                    if bok:
                        # REAL test run with the stack's standard runner.
                        await _deps()  # test files may import extra modules
                        ok, test_summary = await asyncio.to_thread(
                            toolchains.run_stack_tests, stack, project["workspace_path"],
                            baseline_failures, health_scope)
                        if not ok and mode == "dev":
                            # Self-test on unit-test failures too, with the
                            # failing test output as feedback.
                            for attempt in range(1, SELF_FIX_ATTEMPTS + 1):
                                _emit(project_id, "agent.activity",
                                      {"agent_id": agent_id, "step": step,
                                       "activity": f"Self-fix {attempt}/{SELF_FIX_ATTEMPTS}: "
                                                   f"{test_summary[:110]}"},
                                      workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
                                gen = await _codegen_impl(
                                    project, task, run["agent_id"],
                                    f"Your code FAILED the test suite: {test_summary}\n"
                                    "Return the corrected complete file(s).",
                                    ctrl.get("pinned_ref"))
                                _record_model(gen)
                                if gen.get("error") and codegen.is_llm_outage(gen["error"]):
                                    ok = False
                                    test_summary = gen["error"]
                                    break  # LLM down — retrying just burns quota
                                commit = None if run_ws else workspace.commit_all(project_id)
                                if commit:
                                    evidence.append("commit:" + commit)
                                await _deps()  # new imports from the fix round
                                ok, test_summary = await asyncio.to_thread(
                                    toolchains.run_stack_tests, stack, project["workspace_path"],
                                    baseline_failures, health_scope)
                                if ok or ctrl["cancelled"]:
                                    break
                    else:
                        # A build error fails the whole check; tests would
                        # give a misleading verdict on broken code.
                        ok, test_summary = False, build_summary
                    # NOTE: workspace-wide health (pre-existing/legacy test
                    # failures) is intentionally NOT fixed here — it burned
                    # LLM quota without converging. The sprint-level Tests
                    # gate (sprint_gate.run_sprint_gates) now owns it via
                    # per-module rework tasks once all sprint tasks drain.
                    evidence.append(("tests-pass:" if ok else "tests-fail:") + test_summary[:160])
                    _emit(project_id, "tool.completed",
                          {"agent_id": agent_id, "tool": f"{tool}[{stack}]", "step": step,
                           "result": "ok" if ok else "failed", "summary": test_summary[:200]},
                          workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
                    if mode == "dev":
                        selftest_failed = not (bok and ok)
                    continue

                elif tool == "browser.test":
                    # Playwright smoke: boot the generated app on a private
                    # port and drive it in a real headless browser. Missing
                    # Playwright SKIPS (never a fake pass/fail); a real
                    # failure triggers the dev self-fix loop like build and
                    # unit-test failures do.
                    res = await _browser_smoke(project)
                    if res["status"] == "failed" and mode == "dev":
                        for attempt in range(1, SELF_FIX_ATTEMPTS + 1):
                            _emit(project_id, "agent.activity",
                                  {"agent_id": agent_id, "step": step,
                                   "activity": f"Self-fix {attempt}/{SELF_FIX_ATTEMPTS} (browser): "
                                               f"{res['summary'][:110]}"},
                                  workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
                            gen = await _codegen_impl(
                                project, task, run["agent_id"],
                                f"Your app FAILED the browser smoke test: {res['summary']}\n"
                                "The app was launched and visited with a real headless "
                                "browser: pages must render content without uncaught JS "
                                "errors, internal links must resolve, form submits must "
                                "not return server errors.\n"
                                "Return the corrected complete file(s).",
                                ctrl.get("pinned_ref"))
                            _record_model(gen)
                            if gen.get("error") and codegen.is_llm_outage(gen["error"]):
                                break  # LLM down — retrying just burns quota
                            commit = None if run_ws else workspace.commit_all(project_id)
                            if commit:
                                evidence.append("commit:" + commit)
                            res = await _browser_smoke(project)
                            if res["status"] != "failed" or ctrl["cancelled"]:
                                break
                    ctrl["browser"] = res
                    tag = {"passed": "browser-pass",
                           "failed": "browser-fail"}.get(res["status"], "browser-skip")
                    evidence.append(f"{tag}:" + res["summary"][:160])
                    _emit(project_id, "tool.completed",
                          {"agent_id": agent_id, "tool": "browser.smoke", "step": step,
                           "result": "ok" if res["status"] != "failed" else "failed",
                           "summary": res["summary"][:200],
                           "pages": res.get("pages", 0),
                           "screenshots": res.get("screenshots", [])[:6]},
                          workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
                    if mode == "dev" and res["status"] == "failed":
                        selftest_failed = True
                        selftest_reason = res["summary"]
                    continue

                if review_mode and tool == "code-review":
                    # The gate's LLM verdict, computed once per review run;
                    # _finish_review_run routes the task from ctrl["review"].
                    verdict = await _wait_bounded(
                        asyncio.to_thread(codegen.review_verdict, project, task, mode,
                                          agent_ref=agent_id,
                                          deadline=time.monotonic() + PHASE_TIMEOUT_S),
                        PHASE_TIMEOUT_S + 60, "Review verdict")
                    ctrl["review"] = verdict
                    llm_tokens["in"] += verdict["input_tokens"]
                    llm_tokens["out"] += verdict["output_tokens"]
                    evidence.append(
                        f"review-{mode}-{verdict['decision']}:"
                        + (verdict["findings"] or "")[:140])
                    _emit(project_id, "tool.completed",
                          {"agent_id": agent_id, "tool": "review.verdict", "step": step,
                           "result": verdict["decision"],
                           "summary": (verdict["findings"] or "")[:200]},
                          workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
                    continue

                _emit(project_id, "tool.completed",
                      {"agent_id": agent_id, "tool": tool, "step": step,
                       "result": "ok"},
                      workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)

            if mode == "dev" and next_status == "Done":
                # Never write Done from the loop: the code still lives in
                # the private run copy until merge_back below. Final routing
                # (gates / QA / Done) happens in _finish_dev_run AFTER the
                # merge, so success always means the code reached the live
                # workspace.
                continue
            if next_status != task["status"] or progress:
                update("tasks", task_id, {"status": next_status, "progress": progress,
                                          "updated_at": now()})
                _emit(project_id, "task.status_changed",
                      {"task_id": task_id, "task": task["title"],
                       "status": next_status, "progress": progress},
                      workflow_run_id=run_id, task_id=task_id)

        # Merge the run's private copy back into the live workspace: only the
        # files this run changed are applied, under per-file leases — that is
        # the single point where two agents could ever collide, and the lease
        # serializes it (a live file that moved meanwhile is committed to git
        # first, so no agent's work is silently lost).
        if run_ws:
            merged = await asyncio.to_thread(
                workspace.merge_back_run_workspace,
                project_id, run_id, run_ws, ws_manifest)
            if merged["applied"]:
                evidence.append(f"merged:{len(merged['applied'])}")
                if merged.get("commit"):
                    evidence.append("commit:" + merged["commit"])
                _emit(project_id, "workspace.committed",
                      {"agent_id": agent_id, "commit": merged.get("commit") or "",
                       "files": merged["applied"][:30], "mode": mode},
                      workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
            if merged["conflicts"]:
                _emit(project_id, "workspace.file_conflict",
                      {"agent_id": agent_id, "task": task["title"],
                       "files": merged["conflicts"][:20],
                       "note": "live files changed after this run started; "
                               "previous state committed before overwrite"},
                      workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
            if merged["error"]:
                merge_error = merged["error"]
                evidence.append("merge-error:" + merged["error"][:140])

        # Re-fetch task at this point — rework/fix cycles may have changed
        # blocked_reason, rework_count, or status on the DB row.
        task = query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        in_tokens = llm_tokens["in"]
        out_tokens = llm_tokens["out"]
        model_name = ctrl.get("model_used") or run.get("model_used") or "scaffold"
        cost = 0.0  # scaffold-only runs record no usage; keep cost bound for the finishers
        if not in_tokens and not out_tokens:
            insert("usage_records", {
                "id": new_id(), "workflow_run_id": run_id, "agent_id": agent_id,
                "model": model_name, "input_tokens": 0, "output_tokens": 0,
                "cost_estimate": 0.0, "created_at": now(),
            })
            _emit(project_id, "usage.recorded",
                  {"run_id": run_id, "input_tokens": 0, "output_tokens": 0,
                   "cost_usd": 0.0, "mode": "scaffold"},
                  workflow_run_id=run_id, agent_id=agent_id)
        else:
            # Use model-specific cost rates (migration 002); fall back to
            # global config defaults when the columns are still zero.
            with _registry_lock:
                reg = dict(_registry.get(run_id, {}))
            in_cost = reg.get("input_cost_per_m") or config.LLM_INPUT_COST_PER_M
            out_cost = reg.get("output_cost_per_m") or config.LLM_OUTPUT_COST_PER_M
            cost = round(in_tokens / 1_000_000 * in_cost + out_tokens / 1_000_000 * out_cost, 4)
            insert("usage_records", {
                "id": new_id(), "workflow_run_id": run_id, "agent_id": agent_id,
                "model": model_name, "input_tokens": in_tokens, "output_tokens": out_tokens,
                "cost_estimate": cost, "created_at": now(),
            })
            _emit(project_id, "usage.recorded",
                  {"run_id": run_id, "input_tokens": in_tokens,
                   "output_tokens": out_tokens, "cost_usd": cost},
                  workflow_run_id=run_id, agent_id=agent_id)
        # This run's cost is now in usage_records — drop its in-flight
        # estimate so it isn't double-counted against the project budget.
        budget.clear_inflight(project_id)

        if mode == "qa":
            await _finish_qa_run(run_id, ctrl, project_id, task_id, agent_id, task,
                                 in_tokens, out_tokens, cost, test_summary)
        elif review_mode:
            await _finish_review_run(run_id, ctrl, project_id, task_id, agent_id, task,
                                     mode, in_tokens, out_tokens, cost)
        elif selftest_failed:
            # Dev self-test failed even after fix attempts — block for
            # review instead of handing known-broken code to QA.
            await _finish_dev_selftest_failed(run_id, project_id, task_id, agent_id, task,
                                              evidence, in_tokens, out_tokens, cost,
                                              (selftest_reason or test_summary))
        else:
            await _finish_dev_run(run_id, ctrl, project_id, task_id, agent_id, task,
                                  evidence, in_tokens, out_tokens, cost, test_summary,
                                  merge_error)
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
        friendly = _sanitize_exception(exc)
        logger.error("Workflow run %s failed: %s", run_id, friendly, exc_info=True)
        update("workflow_runs", run_id, {"status": "Failed", "completed_at": now()})
        update("tasks", task_id, {"status": "Blocked",
                                  "blocked_reason": friendly,
                                  "blocked_detail": f"{type(exc).__name__}: {str(exc)}",
                                  "updated_at": now()})
        _emit(project_id, "workflow.failed",
              {"run_id": run_id, "error": friendly, "recoverable": True},
              workflow_run_id=run_id, task_id=task_id)
        _emit(project_id, "task.status_changed",
              {"task_id": task_id, "status": "Blocked", "blocked_reason": friendly},
              workflow_run_id=run_id, task_id=task_id)
        _set_agent(agent_id, "Failed", f"Failed: {task['title']}", task_id=task_id, project_id=project_id)
        await asyncio.sleep(1.2)
        _set_agent(agent_id, "Idle", "", task_id=None, project_id=project_id)
        audit("execution_failed", "workflow_run", run_id, friendly)
    finally:
        with _registry_lock:
            _registry.pop(run_id, None)
        if run_ws:
            # Fire-and-forget so this is safe even when the task itself is
            # being cancelled; anything left over is GC'd by the next run.
            try:
                asyncio.get_running_loop().run_in_executor(
                    None, workspace.discard_run_workspace, run_ws)
            except Exception:
                logger.debug("run workspace cleanup skipped for %s", run_id,
                             exc_info=True)


async def _finish_dev_selftest_failed(run_id, project_id, task_id, agent_id, task,
                                      evidence, in_tokens, out_tokens, cost,
                                      test_summary=""):
    """Dev run failed its OWN build/test check even after self-fix attempts.
    Never hand this to QA — QA would only bounce back the same error. Block
    the task with the real error and trigger a PO/human review instead."""
    reason = (f"Dev self-test failed after {SELF_FIX_ATTEMPTS} fix attempts: "
              f"{test_summary[:220]}")
    update("tasks", task_id, {"status": "Blocked", "progress": 75,
                              "blocked_reason": reason,
                              "evidence": "; ".join(evidence) if evidence else "",
                              "updated_at": now()})
    update("workflow_runs", run_id, {"status": "Completed", "current_step": "selftest-failed",
                                     "completed_at": now()})
    _emit(project_id, "task.status_changed",
          {"task_id": task_id, "task": task["title"], "status": "Blocked",
           "blocked_reason": reason},
          workflow_run_id=run_id, task_id=task_id)
    _emit(project_id, "workflow.completed",
          {"run_id": run_id, "task": task["title"], "outcome": "dev-selftest-failed",
           "error": reason[:300],
           "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens,
                     "cost_usd": cost}},
          workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
    _set_agent(agent_id, "Failed", f"Self-test failed: {task['title']}",
               task_id=task_id, project_id=project_id)
    await asyncio.sleep(1.2)
    _set_agent(agent_id, "Idle", "", task_id=None, project_id=project_id)
    audit("dev_selftest_failed", "workflow_run", run_id,
          f"Task '{task['title']}' failed dev self-test after fix attempts; blocked for review")
    if po.po_enabled(project_id):
        # PO reviews the failure right away: rewrite the task, reassign it,
        # or park it — a human-like decision instead of an endless loop.
        async def _po_review():
            try:
                await asyncio.to_thread(
                    po.po_autonomy_tick, project_id,
                    f"The task '{task['title']}' repeatedly failed its build/test check "
                    f"({test_summary[:200]}). Decide: rewrite or re-scope the task, reassign "
                    "it to another developer, or cancel it and note why.")
            except Exception as exc:
                logger.warning("PO review after dev self-test failure failed for project %s: %s",
                               project_id, exc)
        _spawn(_po_review())


def _request_review(project_id, task, stage, dev_agent_id, evidence_str):
    """Open a review gate for a task: assign a reviewer from the stage's
    role family and create a pending task_reviews record. Returns the
    reviewer, or None when no suitable agent is available (the gate is
    then skipped rather than blocking the pipeline)."""
    reviewer_type, family = _REVIEW_STAGES[stage]
    members = _team_members(project_id) or []
    reviewer, _ = _pick_member(members, family)
    if reviewer and reviewer["id"] in (dev_agent_id, task["assigned_agent_id"]):
        alt = [m for m in members if m["id"] not in (dev_agent_id, task["assigned_agent_id"])
               and _family_of_role(m["role_name"]) == family]
        reviewer = alt[0] if alt else None
    if not reviewer:
        return None
    insert("task_reviews", {
        "id": new_id(), "task_id": task["id"], "project_id": project_id,
        "reviewer_type": reviewer_type, "reviewer_agent_id": reviewer["id"],
        "status": "pending", "created_at": now(),
    })
    update("tasks", task["id"], {"status": stage, "progress": 85,
                                 "assigned_agent_id": reviewer["id"],
                                 "qa_agent_id": dev_agent_id,
                                 "evidence": evidence_str, "updated_at": now()})
    _emit(project_id, "task.status_changed",
          {"task_id": task["id"], "task": task["title"], "status": stage, "progress": 85},
          task_id=task["id"])
    _emit(project_id, "task.review_requested",
          {"task_id": task["id"], "task": task["title"], "stage": stage,
           "reviewer": reviewer["name"]}, task_id=task["id"], agent_id=reviewer["id"])
    return reviewer


def _qa_handoff(project_id, task, dev_agent_id, evidence_str):
    """Hand an approved task to QA. Returns False when no QA member is
    available (the caller then marks the task Done directly)."""
    qa = _pick_qa_member(project_id)
    if not qa or qa["id"] == dev_agent_id:
        return False
    update("tasks", task["id"], {"status": "Waiting QA", "progress": 95,
                                 "assigned_agent_id": qa["id"],
                                 "qa_agent_id": dev_agent_id,
                                 "evidence": evidence_str, "updated_at": now()})
    _emit(project_id, "task.status_changed",
          {"task_id": task["id"], "task": task["title"], "status": "Waiting QA", "progress": 95},
          task_id=task["id"])
    _emit(project_id, "task.qa_handoff",
          {"task_id": task["id"], "task": task["title"], "dev": dev_agent_id,
           "qa": qa["id"], "qa_name": qa["name"]}, task_id=task["id"], agent_id=dev_agent_id)
    return True


_STAGE_STATUS = {"sa": "SA Review", "ba": "BA Review", "cr": "Code Review"}


def _advance_stage(project, task, from_stage, dev_agent_id, evidence_str):
    """Walk the project's flow to the stage after ``from_stage`` and hand the
    task over. Stages whose actor is unavailable (no reviewer / no QA agent)
    are skipped, matching the pre-flow gate behaviour. Returns
    (handed_off, outcome); (False, 'done') means the flow is exhausted."""
    flow = flows.flow_for_project(project)
    nxt = flows.next_after(flow, from_stage)
    while nxt:
        if nxt in _STAGE_STATUS:
            if _request_review(project["id"], task, _STAGE_STATUS[nxt],
                               dev_agent_id, evidence_str):
                return True, f"{nxt}-review-handoff"
        elif nxt == "qa":
            if _qa_handoff(project["id"], task, dev_agent_id, evidence_str):
                return True, "qa-handoff"
        elif nxt == "approve":
            if _request_approval(project, task, dev_agent_id, evidence_str):
                return True, "approval-handoff"
        nxt = flows.next_after(flow, nxt)
    return False, "done"


def _request_approval(project, task, dev_agent_id, evidence_str):
    """Park a flow-finished task in 'Pending Approval': a human approves or
    rejects it in the control chat, or the PO agent decides autonomously."""
    update("tasks", task["id"], {"status": "Pending Approval", "progress": 97,
                                 "assigned_agent_id": dev_agent_id,
                                 "qa_agent_id": dev_agent_id,
                                 "evidence": evidence_str, "updated_at": now()})
    _emit(project["id"], "task.status_changed",
          {"task_id": task["id"], "task": task["title"], "status": "Pending Approval",
           "progress": 97}, task_id=task["id"])
    _emit(project["id"], "task.approval_requested",
          {"task_id": task["id"], "task": task["title"]},
          task_id=task["id"], agent_id=dev_agent_id)
    from . import specs
    specs._post(project["id"],
                f"🛑 **{task['title']}** has cleared every automated stage of the flow "
                f"and awaits approval. Say `approve task {task['id'][:8]}` to finish it, "
                f"or `reject task {task['id'][:8]}: <what to change>` to send it back.")
    if po.po_enabled(project["id"]):
        def _po_decide():
            try:
                verdict = codegen.approval_verdict(project, task, evidence_str)
                decision = "approve" if verdict["decision"] == "approve" else "rework"
                resolve_task_approval(project["id"], task["id"], decision,
                                      verdict.get("findings") or "", actor="PO agent")
            except Exception as exc:
                logger.warning("PO approval decision failed for project %s: %s",
                               project["id"], exc)
        threading.Thread(target=_po_decide, daemon=True,
                         name=f"approve-{task['id'][:8]}").start()
    return True


def _complete_task(project_id, task, evidence_str):
    update("tasks", task["id"], {"status": "Done", "progress": 100,
                                 "evidence": evidence_str, "blocked_reason": "",
                                 "updated_at": now()})
    _emit(project_id, "task.status_changed",
          {"task_id": task["id"], "task": task["title"], "status": "Done",
           "progress": 100}, task_id=task["id"])


def resolve_task_approval(project_id, task_ref, decision, notes="", actor="user") -> str:
    """Approve or reject a task parked in 'Pending Approval'."""
    ref = (task_ref or "").strip()
    pending = query("SELECT * FROM tasks WHERE project_id = ? AND status = 'Pending Approval' "
                    "ORDER BY updated_at", (project_id,))
    task = None
    if ref:
        for t in pending:
            if t["id"].startswith(ref) or t["title"].lower() == ref.lower():
                task = t
                break
        if not task:
            pending_by_title = [t for t in pending if ref.lower() in t["title"].lower()]
            task = pending_by_title[0] if pending_by_title else None
    elif len(pending) == 1:
        task = pending[0]
    if not task:
        if not pending:
            return "No task is waiting for approval on this project."
        return ("Several tasks await approval — name one, e.g. "
                f"`{'approve' if decision == 'approve' else 'reject'} task "
                f"{pending[0]['id'][:8]}`.")
    dev_agent_id = _author_agent_id(project_id, task, task["assigned_agent_id"])
    evidence_str = ((task["evidence"] + "; ") if task["evidence"] else "") + \
        f"approval:{actor}={decision}"
    if decision == "approve":
        project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        advanced, _ = _advance_stage(project, task, "approve", dev_agent_id, evidence_str)
        if not advanced:
            _complete_task(project_id, task, evidence_str)
        audit("task_approved", "task", task["id"],
              f"'{task['title']}' approved by {actor}", actor=actor)
        return f"✅ **{task['title']}** approved by {actor} — marked Done."
    rework = (task["rework_count"] or 0) + 1
    reason = f"{actor} rejected: {(notes or 'no notes given')[:600]}"
    if rework >= MAX_REWORK_CYCLES:
        update("tasks", task["id"], {"status": "Blocked", "progress": 90,
                                     "blocked_reason": reason, "rework_count": rework,
                                     "evidence": evidence_str, "updated_at": now()})
        _emit(project_id, "task.status_changed",
              {"task_id": task["id"], "task": task["title"], "status": "Blocked",
               "blocked_reason": reason}, task_id=task["id"])
        audit("approval_escalated", "task", task["id"],
              f"'{task['title']}' blocked after {rework} approval rejections", actor=actor)
        return (f"⛔ **{task['title']}** rejected by {actor} {rework} times — "
                "blocked for review.")
    update("tasks", task["id"], {"status": "Rework", "assigned_agent_id": dev_agent_id,
                                 "blocked_reason": f"Rework cycle {rework}: {reason}",
                                 "rework_count": rework, "evidence": evidence_str,
                                 "updated_at": now()})
    _emit(project_id, "task.status_changed",
          {"task_id": task["id"], "task": task["title"], "status": "Rework",
           "blocked_reason": f"Rework cycle {rework}: {reason}"}, task_id=task["id"])
    _emit(project_id, "task.approval_rejected",
          {"task_id": task["id"], "task": task["title"], "actor": actor,
           "notes": notes[:400], "cycle": rework}, task_id=task["id"])
    audit("task_rejected", "task", task["id"],
          f"'{task['title']}' sent back by {actor} (cycle {rework})", actor=actor)
    return (f"🔁 **{task['title']}** sent back to the developer (cycle {rework}): "
            f"{(notes or 'no notes given')[:300]}")


async def _finish_dev_run(run_id, ctrl, project_id, task_id, agent_id, task,
                          evidence, in_tokens, out_tokens, cost, test_summary="",
                          merge_error=""):
    """Dev run finished building: route through the enabled SA/BA review
    gates, then hand off to QA ('Waiting QA'); when nothing downstream
    exists (no reviewers, no QA), mark the task Done directly. A failed
    merge-back means the code never reached the live workspace — that is
    blocked territory, never a gate handoff or Done."""
    evidence_str = "; ".join(evidence) if evidence else "dev complete"
    if merge_error:
        reason = ("Code could not merge back into the live workspace: "
                  f"{merge_error[:200]}")
        update("tasks", task_id, {"status": "Blocked", "progress": 90,
                                  "blocked_reason": reason,
                                  "evidence": evidence_str,
                                  "updated_at": now()})
        update("workflow_runs", run_id, {"status": "Completed", "current_step": "merge-failed",
                                         "completed_at": now()})
        _emit(project_id, "task.status_changed",
              {"task_id": task_id, "task": task["title"], "status": "Blocked",
               "blocked_reason": reason},
              workflow_run_id=run_id, task_id=task_id)
        _emit(project_id, "workflow.completed",
              {"run_id": run_id, "task": task["title"], "outcome": "merge-failed",
               "error": reason[:300],
               "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens,
                         "cost_usd": cost}},
              workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
        _set_agent(agent_id, "Failed", f"Merge failed: {task['title']}",
                   task_id=task_id, project_id=project_id)
        await asyncio.sleep(1.2)
        _set_agent(agent_id, "Idle", "", task_id=None, project_id=project_id)
        audit("merge_failed", "workflow_run", run_id,
              f"Task '{task['title']}' build could not merge into the live workspace; blocked")
        if po.po_enabled(project_id):
            async def _po_review_merge():
                try:
                    await asyncio.to_thread(
                        po.po_autonomy_tick, project_id,
                        f"The task '{task['title']}' completed its build but the code "
                        f"could not merge into the live workspace ({merge_error[:200]}). "
                        "Decide: retry the task, reassign it, or cancel it and note why.")
                except Exception as exc:
                    logger.warning("PO review after merge failure failed for project %s: %s",
                                   project_id, exc)
            _spawn(_po_review_merge())
        return
    project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    handed_off, outcome = _advance_stage(project, task, "dev", agent_id, evidence_str)
    if handed_off:
        update("workflow_runs", run_id, {"status": "Completed", "current_step": outcome,
                                         "completed_at": now()})
        _emit(project_id, "workflow.completed",
              {"run_id": run_id, "task": task["title"], "evidence": evidence,
               "outcome": outcome,
               "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens,
                         "cost_usd": cost}},
              workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
        _set_agent(agent_id, "Completed", f"Completed (dev): {task['title']}",
                   task_id=task_id, project_id=project_id)
        await asyncio.sleep(1.2)
        _set_agent(agent_id, "Idle", "", task_id=None, project_id=project_id)
        audit("dev_completed", "workflow_run", run_id,
              f"Task '{task['title']}' completed dev phase -> {outcome}")
        return

    update("tasks", task_id, {
        "status": "Done", "progress": 100,
        "evidence": evidence_str,
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


async def _finish_review_run(run_id, ctrl, project_id, task_id, agent_id, task, mode,
                             in_tokens, out_tokens, cost):
    """SA/BA review gate finished: record the verdict, then advance the task
    (next gate -> QA -> Done) or bounce it to the developer as Rework.
    After MAX_REWORK_CYCLES the task is blocked for human/PO review, and an
    'unavailable' verdict (no gateway / LLM outage) never punishes the dev —
    the gate is skipped and the chain advances."""
    stage = _MODE_STAGE[mode]
    reviewer_type, _ = _REVIEW_STAGES[stage]
    verdict = ctrl.get("review") or {
        "decision": "unavailable", "findings": "review verdict missing", "rework_class": ""}
    decision = verdict["decision"]
    findings = (verdict["findings"] or "")[:600]
    rework_class = verdict.get("rework_class") or ""
    task = query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    dev_agent_id = _author_agent_id(project_id, task, agent_id)
    reviewer = query_one("SELECT name FROM agents WHERE id = ?", (agent_id,))
    reviewer_name = reviewer["name"] if reviewer else stage
    row = query_one("SELECT * FROM task_reviews WHERE task_id = ? AND reviewer_type = ? "
                    "AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                    (task_id, reviewer_type))
    review_status = "skipped" if decision == "unavailable" else decision
    if row:
        update("task_reviews", row["id"], {
            "status": review_status, "findings": findings,
            "rework_class": rework_class, "completed_at": now()})
    else:
        # Gate entered without _request_review (manual status change) —
        # record the verdict as a completed review so evidence is never lost.
        insert("task_reviews", {
            "id": new_id(), "task_id": task_id, "project_id": project_id,
            "reviewer_type": reviewer_type, "reviewer_agent_id": agent_id,
            "status": review_status, "findings": findings,
            "rework_class": rework_class, "created_at": now(),
            "completed_at": now()})
    evidence_str = ((task["evidence"] + "; ") if task["evidence"] else "") + \
        f"review:{reviewer_type}={decision}"

    if decision == "rework":
        rework = (task["rework_count"] or 0) + 1
        # findings already carry the reviewer's full detail (600 chars);
        # cutting further to 400 here would silently drop it before the
        # dev sees the rework feedback.
        reason = f"{reviewer_name} rework: {findings}"[:1200]
        if rework >= MAX_REWORK_CYCLES:
            blocked = f"{reason}. Failed {stage} {rework} times — needs human review."
            update("tasks", task_id, {"status": "Blocked", "progress": 85,
                                      "blocked_reason": blocked, "rework_count": rework,
                                      "rework_class": rework_class,
                                      "evidence": evidence_str, "updated_at": now()})
            _emit(project_id, "task.status_changed",
                  {"task_id": task_id, "task": task["title"], "status": "Blocked",
                   "blocked_reason": blocked},
                  workflow_run_id=run_id, task_id=task_id)
            _emit(project_id, "review.escalated",
                  {"task_id": task_id, "task": task["title"], "stage": stage,
                   "cycles": rework, "needs_human": True},
                  workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
            audit("review_escalated", "task", task_id,
                  f"Task '{task['title']}' escalated after {rework} {stage} rejections")
            if po.po_enabled(project_id):
                async def _po_review():
                    try:
                        await asyncio.to_thread(po.po_autonomy_tick, project_id)
                    except Exception as exc:
                        logger.warning("PO review failed for project %s: %s", project_id, exc)
                _spawn(_po_review())
        else:
            update("tasks", task_id, {"status": "Rework",
                                      "assigned_agent_id": dev_agent_id,
                                      "blocked_reason": f"Rework cycle {rework}: {reason}",
                                      "rework_count": rework, "rework_class": rework_class,
                                      "evidence": evidence_str, "updated_at": now()})
            _emit(project_id, "task.status_changed",
                  {"task_id": task_id, "task": task["title"], "status": "Rework",
                   "blocked_reason": f"Rework cycle {rework}: {reason}"},
                  workflow_run_id=run_id, task_id=task_id)
            _emit(project_id, "task.review_rejected",
                  {"task_id": task_id, "task": task["title"], "stage": stage,
                   "reviewer": reviewer_name, "findings": findings,
                   "rework_class": rework_class, "cycle": rework},
                  workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
            audit("review_rejected", "workflow_run", run_id,
                  f"{stage} rejected task '{task['title']}' (cycle {rework}): {findings[:150]}")
        # Cross-run learning: the reviewer's findings go to the pitfall
        # ledger so the next attempt (this task or a future one) is
        # generated with the mistake visible in the prompt.
        memory.record_pitfall(project_id, task_id, f"{reviewer_type} review",
                              findings, subject=task["title"], owner_agent_id=dev_agent_id)
    else:
        project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        advanced, _outcome = _advance_stage(project, task, mode, dev_agent_id,
                                            evidence_str)
        if not advanced:
            update("tasks", task_id, {"status": "Done", "progress": 100,
                                      "evidence": evidence_str,
                                      "blocked_reason": "", "updated_at": now()})
            _emit(project_id, "task.status_changed",
                  {"task_id": task_id, "task": task["title"], "status": "Done", "progress": 100},
                  workflow_run_id=run_id, task_id=task_id)
            audit("review_completed", "workflow_run", run_id,
                  f"{stage} cleared task '{task['title']}' (no QA agent — Done)")

    update("workflow_runs", run_id, {"status": "Completed",
                                     "current_step": f"{mode}-reviewed",
                                     "completed_at": now()})
    _emit(project_id, "workflow.completed",
          {"run_id": run_id, "task": task["title"], "outcome": f"{mode}-review-{decision}",
           "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens,
                     "cost_usd": cost}},
          workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
    _set_agent(agent_id, "Completed", f"{stage}: {task['title']}",
               task_id=task_id, project_id=project_id)
    await asyncio.sleep(1.2)
    _set_agent(agent_id, "Idle", "", task_id=None, project_id=project_id)


async def _finish_qa_run(run_id, ctrl, project_id, task_id, agent_id, task,
                         in_tokens, out_tokens, cost, test_summary=""):
    """QA run finished: verdict comes from the REAL pytest run when one was
    executed; falls back to a simulated verdict only when pytest is
    unavailable. Reject -> task back to the developer ('Rework') with an
    error summary. After MAX_REWORK_CYCLES rejections the task is escalated
    to a human instead of looping forever."""
    browser = ctrl.get("browser") or {}
    browser_status = browser.get("status", "")
    browser_summary = browser.get("summary", "")
    if browser_status == "failed":
        # Real browser evidence outranks everything else: the app broke in
        # an actual browser, so no unit-test pass can carry this task.
        verdict_pass = False
        summary = f"QA browser smoke failed: {browser_summary[len('browser FAILED: '):].strip()}"
    elif test_summary.startswith(("tests FAILED", "pytest FAILED")):
        verdict_pass = False
        marker = "tests FAILED: " if test_summary.startswith("tests FAILED") else "pytest FAILED: "
        summary = f"QA test run failed: {test_summary[len(marker):].strip()}"
    elif test_summary.startswith("build FAILED"):
        verdict_pass = False
        summary = f"QA build smoke failed: {test_summary[len('build FAILED: '):].strip()}"
    elif test_summary.startswith(("tests passed", "pytest passed")):
        # Baseline-tolerance results pass at TASK level: this task's own work
        # is clean. Workspace-wide health (legacy failures) is enforced later
        # by the sprint-level Tests gate (sprint_gate.run_sprint_gates).
        verdict_pass = True
        summary = ""
    elif browser_status == "passed":
        # The app booted and worked in a real browser: a genuine pass, so
        # the unverified path below never decides this task's fate.
        verdict_pass = True
        summary = ""
    else:
        # No real evidence reached this point (an LLM outage froze the
        # self-fix before any test/browser signal, or no runner produced a
        # verdict at all). A coin-flip here would fabricate QA results, so
        # the task goes to a human/PO as UNVERIFIED — rework_count stays
        # untouched because nothing actually failed.
        await _finish_qa_unverified(run_id, project_id, task_id, agent_id, task,
                                    test_summary, browser_status,
                                    in_tokens, out_tokens, cost)
        return
    dev_agent_id = _author_agent_id(project_id, task, agent_id)
    browser = ctrl.get("browser") or {}
    browser_status = browser.get("status", "")
    browser_summary = browser.get("summary", "")
    dev = query_one("SELECT name FROM agents WHERE id = ?", (dev_agent_id,))
    qa_agent = query_one("SELECT name FROM agents WHERE id = ?", (agent_id,))

    if verdict_pass:
        evidence = (task["evidence"] + "; " if task["evidence"] else "") + "qa:passed"
        project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        advanced, _outcome = _advance_stage(project, task, "qa", dev_agent_id, evidence)
        update("workflow_runs", run_id, {"status": "Completed",
                                         "current_step": "done" if not advanced else _outcome,
                                         "completed_at": now()})
        _emit(project_id, "task.qa_passed",
              {"task_id": task_id, "task": task["title"], "qa": qa_agent["name"] if qa_agent else agent_id},
              workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
        if not advanced:
            _complete_task(project_id, task, evidence)
        _emit(project_id, "workflow.completed",
              {"run_id": run_id, "task": task["title"],
               "outcome": "qa-passed" if not advanced else _outcome,
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
    memory.record_pitfall(project_id, task_id, "qa", summary,
                          subject=task["title"], owner_agent_id=dev_agent_id)
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
        if po.po_enabled(project_id):
            # Full autonomy: the Product Owner reviews the escalation right
            # away instead of leaving it for a human.
            async def _po_review():
                try:
                    await asyncio.to_thread(po.po_autonomy_tick, project_id)
                except Exception as exc:
                    logger.warning("PO review failed for project %s: %s", project_id, exc)
            _spawn(_po_review())
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


async def _finish_qa_unverified(run_id, project_id, task_id, agent_id, task,
                                test_summary, browser_status,
                                in_tokens, out_tokens, cost):
    """QA produced no real evidence (no test run, no browser smoke — e.g. an
    LLM outage froze the self-fix first). Never fabricate a verdict: the
    task goes Blocked-for-review with rework_count untouched, since nothing
    measurably failed."""
    signal = (test_summary or f"browser:{browser_status or 'none'}")[:200]
    reason = (f"QA could not verify automatically — no test or browser "
              f"evidence (last signal: {signal}). Needs human or PO review.")
    qa = query_one("SELECT name FROM agents WHERE id = ?", (agent_id,))
    evidence = (task["evidence"] + "; " if task["evidence"] else "") + "qa:unverified"
    update("tasks", task_id, {"status": "Blocked", "progress": 85,
                              "blocked_reason": reason, "evidence": evidence,
                              "updated_at": now()})
    update("workflow_runs", run_id, {"status": "Completed", "current_step": "qa-unverified",
                                     "completed_at": now()})
    _emit(project_id, "task.status_changed",
          {"task_id": task_id, "task": task["title"], "status": "Blocked",
           "blocked_reason": reason}, workflow_run_id=run_id, task_id=task_id)
    _emit(project_id, "qa.unverified",
          {"task_id": task_id, "task": task["title"],
           "qa": qa["name"] if qa else agent_id, "signal": signal,
           "needs_human": True}, workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
    _emit(project_id, "workflow.completed",
          {"run_id": run_id, "task": task["title"], "outcome": "qa-unverified",
           "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens,
                     "cost_usd": cost}},
          workflow_run_id=run_id, task_id=task_id, agent_id=agent_id)
    audit("qa_unverified", "task", task_id,
          f"Task '{task['title']}' unverifiable by QA — blocked for review ({signal})")
    _set_agent(agent_id, "Completed", f"QA unverified: {task['title']}",
               task_id=task_id, project_id=project_id)
    await asyncio.sleep(1.2)
    _set_agent(agent_id, "Idle", "", task_id=None, project_id=project_id)
    if po.po_enabled(project_id):
        async def _po_review():
            try:
                await asyncio.to_thread(po.po_autonomy_tick, project_id)
            except Exception as exc:
                logger.warning("PO review failed for project %s: %s", project_id, exc)
        _spawn(_po_review())


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
            # Stale DB row (worker died with the process): the live-cancel path
            # in _run_phases never runs, so clear the task here or it stays
            # In Progress forever and blocks its sprint from draining.
            if run["task_id"]:
                update("tasks", run["task_id"],
                       {"status": "Blocked", "blocked_reason": "Cancelled by operator",
                        "updated_at": now()})
                _emit(run["project_id"], "task.status_changed",
                      {"task_id": run["task_id"], "status": "Blocked",
                       "blocked_reason": "Cancelled by operator"},
                      workflow_run_id=run_id, task_id=run["task_id"])
            _emit(run["project_id"], "workflow.cancelled", {"run_id": run_id},
                  workflow_run_id=run_id, task_id=run["task_id"])
            if run["agent_id"] and not active_run_for_agent(run["agent_id"]):
                _set_agent(run["agent_id"], "Idle", "", task_id=None,
                           project_id=run["project_id"])
        return {"ok": True, "note": "run was not active"}
    ctrl["cancelled"] = True
    ctrl["paused"] = False
    _cancel_handle(ctrl.get("task"))
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
    # Governance-opted projects outside a development-permitting lifecycle
    # state claim nothing at all (V3 Step 1).
    g_ok, _ = governance.may_execute(project_id)
    if not g_ok:
        return []
    if not budget.may_spend(project_id)[0]:
        return []
    # Sprint Gatekeeper (RULE 2): only tasks of the ONE active sprint may
    # run. Future sprints stay locked until the current one completes.
    sprint = sprint_gate.active_sprint(project_id)
    if not sprint:
        return []
    tasks = query(
        "SELECT * FROM tasks WHERE project_id = ? AND sprint_id = ? "
        "AND status IN ('Todo','Ready','Rework','Waiting QA','Code Review','SA Review','BA Review') AND assigned_agent_id IS NOT NULL "
        "ORDER BY priority, created_at", (project_id, sprint["id"]))
    eligible = []
    for t in tasks:
        ok, _ = dependencies_satisfied(t["id"])
        if ok and not active_run_for_task(t["id"]):
            agent = query_one("SELECT lifecycle_state FROM agents WHERE id = ?", (t["assigned_agent_id"],))
            if agent and agent["lifecycle_state"] == "Idle":
                eligible.append(t)
    return eligible


def _set_waiting_agents(project_id: str, waiting: bool):
    """While the sprint moves through validation gates, idle agents show a
    waiting state (spec §22/§36) — no polling, no model calls."""
    rows = query(
        "SELECT DISTINCT a.id, a.name, a.lifecycle_state FROM agents a WHERE a.id IN "
        "(SELECT assigned_agent_id FROM tasks WHERE project_id = ? "
        "AND assigned_agent_id IS NOT NULL)", (project_id,))
    for a in rows:
        if a["lifecycle_state"] != "Idle":
            continue
        activity = "Waiting for sprint validation" if waiting else ""
        update("agents", a["id"], {"current_activity": activity, "updated_at": now()})
        _emit(project_id, "agent.status.changed",
              {"agent_id": a["id"], "agent": a["name"], "state": "Idle",
               "activity": activity}, agent_id=a["id"])


async def _run_sprint(project_id: str, ctrl: dict):
    _emit(project_id, "project.updated", {"note": "Sprint execution started"})
    try:
        await _sprint_loop(project_id, ctrl)
    finally:
        # Always release the scheduler slot, even on force-cancel or an
        # unexpected error, so the UI's "Stop" button flips back to "Start".
        with _schedulers_lock:
            _schedulers.pop(project_id, None)


async def _sprint_loop(project_id: str, ctrl: dict):
    idle_rounds = 0
    while not ctrl["cancelled"]:
        eligible = _eligible_tasks(project_id)
        if not eligible:
            in_flight = _active_project_runs(project_id)
            active = sprint_gate.active_sprint(project_id)
            remaining = query(
                "SELECT COUNT(*) AS n FROM tasks WHERE project_id = ? AND sprint_id = ? "
                "AND status NOT IN ('Done','Cancelled')",
                (project_id, active["id"]))[0]["n"] if active else 0
            if remaining == 0 and not in_flight:
                if active and active["status"] in (sprint_gate.ACTIVE,) + sprint_gate.GATE_PHASES:
                    # Sprint gating (RULE 5-11): the completion pipeline —
                    # build, automated tests, functional validation,
                    # acceptance. Rework tasks it creates re-enter this
                    # loop; only COMPLETED ends the sprint. The next sprint
                    # stays LOCKED until then.
                    _set_waiting_agents(project_id, True)
                    try:
                        outcome = await asyncio.to_thread(
                            sprint_gate.run_sprint_gates, project_id, active["id"], ctrl)
                    finally:
                        _set_waiting_agents(project_id, False)
                    if outcome == "Completed":
                        _emit(project_id, "project.updated",
                              {"note": "Sprint execution complete — all gates passed",
                               "remaining_tasks": 0})
                        if po.po_enabled(project_id):
                            # Continuous delivery: hand control straight back
                            # to the Product Owner to plan/start the next
                            # sprint (now READY) from the backlog.
                            async def _po_next_sprint():
                                try:
                                    await asyncio.to_thread(
                                        po.po_autonomy_tick, project_id,
                                        "The sprint just completed and the next sprint is "
                                        "unlocked (Ready). If backlog items or requirements "
                                        "remain, plan and start the next sprint; if the project "
                                        "is done, take no action and summarize completion.")
                                except Exception as exc:
                                    logger.warning("PO next-sprint tick failed for project %s: %s",
                                                   project_id, exc)
                            _spawn(_po_next_sprint())
                        break
                    if outcome == "Failed":
                        _emit(project_id, "project.updated",
                              {"note": "Sprint FAILED after repeated gate failures — "
                                       "manual or PO resolution required"})
                        break
                    # Rework (tasks created, sprint back to Active) or Aborted
                    # (re-opened for urgent scope mid-gates): keep working.
                    auto_assign_tasks(project_id)
                    idle_rounds = 0
                    await asyncio.sleep(1.0)
                    continue
                _emit(project_id, "project.updated",
                      {"note": "Sprint execution complete", "remaining_tasks": 0})
                break
            if in_flight:
                # Every agent is heads-down on a run — that's progress, not
                # idleness. Wait for capacity; don't count toward give-up.
                await asyncio.sleep(2.0)
                continue
            # Nothing runnable right now: tasks may exist unassigned (newly
            # drafted or freed by a state change). Re-attempt role-based
            # assignment instead of giving up; give up only after a stretch
            # with no progress at all. With the Product Owner enabled, the PO
            # periodically reviews and unblocks/replans, so the loop persists
            # much longer instead of surfacing to a human.
            blocked = 0
            auto_assign_tasks(project_id)
            idle_rounds += 1
            # Dependency-stall visibility: pending tasks waiting on a Blocked
            # dependency are skipped silently by _eligible_tasks, so the sprint
            # can never drain while the blocker stands. Surface the exact pairs
            # (throttled) instead of letting the loop idle anonymously — the PO
            # tick and the Control UI act on this event.
            if stall := dependency_stall(project_id, active["id"] if active else None):
                if idle_rounds % 5 == 1:
                    _emit(project_id, "sprint.dependency_stall",
                          {"sprint": active["name"] if active else "",
                           "stalled": stall[:10],
                           "note": "Tasks wait on Blocked dependencies — unblock or "
                                   "cancel the blockers to let the sprint drain."})
            # Default the give-up threshold up front: with the PO enabled it is
            # only recomputed inside the %10 review branch below, so without a
            # default an idle round between reviews would reference an
            # unassigned variable and crash the whole scheduler loop (which
            # silently stopped assigning tasks to devs).
            give_up_after = (IDLE_GIVE_UP_CLEAN if po.po_enabled(project_id)
                             else IDLE_GIVE_UP_BLOCKED)
            if po.po_enabled(project_id):
                if idle_rounds % 10 == 0:
                    try:
                        # Periodic idle pass: suppress the "nothing to do"
                        # narration so a steady loop can't spam the main chat.
                        # Genuine actions and stuck/error states still surface.
                        await asyncio.to_thread(po.po_autonomy_tick, project_id,
                                                announce_no_action=False)
                    except Exception as exc:
                        logger.warning("PO autonomy tick failed for project %s: %s",
                                       project_id, exc)
                    blocked = query(
                        "SELECT COUNT(*) AS n FROM tasks WHERE project_id = ? "
                        "AND sprint_id IS NOT NULL AND status = 'Blocked'",
                        (project_id,))[0]["n"]
                    # Blocked work = unfinished business: keep persisting while
                    # the PO reviews it (each review may re-scope and unblock it).
                    give_up_after = (IDLE_GIVE_UP_BLOCKED if blocked
                                     else IDLE_GIVE_UP_CLEAN)
            else:
                give_up_after = IDLE_GIVE_UP_BLOCKED
            if idle_rounds > give_up_after:
                _emit(project_id, "project.updated",
                      {"note": (f"Scheduler gave up — no eligible tasks for "
                                f"{idle_rounds} rounds (threshold {give_up_after}). "
                                "Tasks may be blocked or the PO may need to replan."),
                       "remaining_tasks": remaining,
                       "reason": "idle_no_eligible_tasks",
                       "blocked": blocked if po.po_enabled(project_id) else None,
                       "give_up_after": give_up_after})
                break
            await asyncio.sleep(2.0)
            continue
        idle_rounds = 0
        # Fan out: start every eligible task at once — each on its own idle
        # agent — instead of running them one by one. One task per agent per
        # pass; the next loop iteration picks up whatever becomes eligible
        # as agents free up (including fresh QA handoffs).
        started_agents = set()
        for task in eligible:
            if task["assigned_agent_id"] in started_agents:
                continue
            result = start_execution(project_id, task["id"])
            run = result.get("run") or {}
            if not run.get("id"):
                busy = "already working" in str(result.get("error", ""))
                if not busy:
                    update("tasks", task["id"], {"status": "Blocked",
                                                 "blocked_reason": result.get("error", "could not start"),
                                                 "updated_at": now()})
                continue
            started_agents.add(task["assigned_agent_id"])
        await asyncio.sleep(1.0)

        # Periodic event archive (issue 4.4): every N rounds the scheduler
        # is alive, prune execution_events beyond the keep_count threshold so
        # the events table stays bounded during long sprints.
        if idle_rounds and idle_rounds % _EVENT_ARCHIVE_IDLE_INTERVAL == 0:
            try:
                removed = db.archive_old_events(project_id, keep_count=_DEFAULT_EVENT_KEEP)
                if removed:
                    logger.info("Archived %d execution events for project %s",
                                removed, project_id)
            except Exception as exc:
                logger.debug("Event archive failed for project %s: %s",
                             project_id, exc)


def _active_project_runs(project_id) -> int:
    """Live in-process workflow runs for a project (registry-backed, so stale
    'Running' rows from a crashed server don't count)."""
    rows = query("SELECT id FROM workflow_runs WHERE project_id = ? AND status = 'Running'",
                 (project_id,))
    return sum(1 for r in rows if r["id"] in _registry)


def dependency_stall(project_id, sprint_id):
    """Detect pending sprint tasks that can never start because a dependency
    is itself stuck (Blocked/Cancelled-dead or waiting on the same set). This
    is the silent sprint-freeze case: `_eligible_tasks` skips them, `remaining`
    never reaches 0, and the gates never run. Returns a list of
    {task, blocker, blocked_reason} for the tasks whose blockers are not
    themselves making progress (i.e. no active run can unblock them soon)."""
    if not sprint_id:
        return []
    pending = query(
        "SELECT id, title FROM tasks WHERE project_id = ? AND sprint_id = ? "
        "AND status IN ('Todo','Ready','Rework','Waiting QA','Code Review','SA Review','BA Review')",
        (project_id, sprint_id))
    stalled = []
    for t in pending:
        ok, unmet = dependencies_satisfied(t["id"])
        if ok:
            continue
        # A blocker is "stuck" if it is Blocked (needs human/PO) — its own
        # dependents can't progress until it is cleared.
        for d in unmet:
            if d["status"] == "Blocked":
                blocker = query_one(
                    "SELECT blocked_reason FROM tasks WHERE id = ?", (d["id"],))
                stalled.append({"task": t["title"], "blocker": d["title"],
                                "blocker_status": d["status"],
                                "blocker_reason": (blocker or {}).get("blocked_reason", "")})
    return stalled



# Role families group equivalent roles so a dev task never lands on QA/BA
# when a developer exists on the team.
ROLE_FAMILIES = {
    "dev": ("Senior Developer", "Developer", "Software Engineer", "Backend Developer",
            "Frontend Developer", "Full Stack Developer", "Junior Developer"),
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
    """Pick the QA reviewer for a finished dev task. With several tasks
    finishing in parallel, prefer a QA specialist who is actually Idle (and
    least loaded) so simultaneous QA handoffs spread across QA agents
    instead of piling onto one busy reviewer."""
    members = _team_members(project_id) or []
    qa, _ = _pick_member(members, "qa")
    if qa and qa["lifecycle_state"] != "Idle":
        idle_qa = [m for m in members
                   if m["id"] != qa["id"] and m["lifecycle_state"] == "Idle"
                   and _family_of_role(m["role_name"]) == "qa"]
        if idle_qa:
            qa = min(idle_qa, key=lambda m: (_agent_load(m["id"]), m["name"]))
    return qa


def _team_members(project_id):
    project = query_one("SELECT team_id FROM projects WHERE id = ?", (project_id,))
    if not project or not project["team_id"]:
        return None
    return query(
        "SELECT a.*, r.name AS role_name FROM team_agents ta JOIN agents a ON a.id = ta.agent_id "
        "JOIN roles r ON r.id = a.role_id WHERE ta.team_id = ? AND ta.active = 1 ORDER BY a.name",
        (project["team_id"],))


def _author_agent_id(project_id, task, fallback_agent_id):
    """The developer a rejected task returns to for rework: the recorded
    author when that agent is still a live dev-family teammate, otherwise the
    least-loaded developer on the team. Only falls back to the recorded id
    when the team has no developer at all. Guards against a re-provisioned
    team, where a stale/foreign qa_agent_id would otherwise bounce dev work
    onto the SA/BA/QA reviewer that just rejected it."""
    qa = task.get("qa_agent_id")
    members = _team_members(project_id) or []
    live = {m["id"]: m for m in members}
    if qa and qa in live and _family_of_role(live[qa]["role_name"]) == "dev":
        return qa
    dev, _ = _pick_member(members, "dev")
    return dev["id"] if dev else (qa or fallback_agent_id)


def _agent_load(agent_id):
    return query_one(
        "SELECT COUNT(*) AS n FROM tasks WHERE assigned_agent_id = ? "
        "AND status IN ('Ready','In Progress','Review','Testing','Waiting QA','Code Review','SA Review','BA Review','Rework')", (agent_id,))["n"]


def auto_assign_tasks(project_id: str) -> dict:
    """Assign unassigned sprint tasks to team members, matching role keywords
    in the task title/description first, then least-loaded as fallback.

    Work spreads across ALL idle agents. Tasks whose specialist is busy are
    placed on a pending queue and re-checked in the next assignment round
    instead of being skipped forever."""
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
    # Mutable availability snapshot: assigning a task makes that member busy
    # for the rest of this pass so several idle devs get work in one round.
    avail = [dict(m) for m in members]
    assigned = 0
    pending = []
    assigned_lines = []
    for t in tasks:
        family = _family_for_task(t)
        pick, matched = _pick_member(avail, family)
        if not pick or pick["lifecycle_state"] != "Idle":
            # Specialist is busy; queue for next round rather than skipping forever
            pending.append(t)
            continue
        update("tasks", t["id"], {"assigned_agent_id": pick["id"], "updated_at": now()})
        pick["lifecycle_state"] = "Working"  # reflected for the remaining tasks
        note = "" if matched else f" (no {_FAMILY_LABEL.get(family, 'matching')} specialist on the team)"
        emit_event(project_id, "task.assigned",
                   {"task_id": t["id"], "task": t["title"], "agent": pick["name"],
                    "role": pick["role_name"], "family": family, "role_matched": matched,
                    "note": note.strip(), "auto": True},
                   task_id=t["id"], agent_id=pick["id"])
        assigned_lines.append(f"- '{t['title']}' → {pick['name']} ({pick['role_name']}){note}")
        assigned += 1
    if assigned_lines:
        # Surface scheduler auto-assignments in the main chat too — the owner
        # should see work reaching the devs even when it wasn't the PO who
        # handed it out. Only fires on a real assignment (state change), so it
        # can't spam an idle loop.
        try:
            po.po_narrate(
                project_id,
                "🛠️ **Scheduler** auto-assigned work to the team:\n"
                + "\n".join(assigned_lines))
        except Exception as exc:  # narration must never break the loop
            logger.debug("auto-assign narration skipped: %s", exc)
    if pending:
        logger.debug("%d task(s) pending assignment (specialist busy) for project %s",
                     len(pending), project_id)
    return {"assigned": assigned, "note": "", "pending": len(pending)}


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
    # Spec pipeline (V3 Step 2): a project whose blueprint-derived sprint
    # plan is ready enters Active Development the moment a sprint starts.
    if governance.governance_enabled(project_id):
        st = governance.current_state(project_id)
        # A project can be stranded in Scaffolding when the post-approval
        # blueprint/breakdown background run fails after the requirement
        # baseline (the one human gate) is already approved. If a sprint has
        # been planned (auto or manually), walk the remaining legal edges
        # Scaffolding -> Ready for Planning so execution can proceed.
        if st == governance.SCAFFOLDING:
            planned = query_one(
                "SELECT 1 AS x FROM sprints WHERE project_id = ? "
                "AND status IN ('Planned','Ready') LIMIT 1", (project_id,))
            if planned:
                governance.transition_project(
                    project_id, governance.READY_PLANNING,
                    reason="sprint planned; recovered scaffolding after breakdown",
                    actor="user")
                st = governance.READY_PLANNING
        if st == governance.READY_PLANNING:
            governance.transition_project(project_id, governance.ACTIVE_DEV,
                                          reason="sprint execution started", actor="user")
    g_ok, g_reason = governance.may_execute(project_id)
    if not g_ok:
        return {"error": f"SPRINT_BLOCKED_BY_LIFECYCLE: {g_reason}"}
    b_ok, b_reason = budget.may_spend(project_id)
    if not b_ok:
        return {"error": f"SPRINT_BLOCKED_BY_BUDGET: {b_reason}"}
    sprint = _resolve_sprint(project_id, sprint_ref)
    if sprint is None and not sprint_ref:
        sprint = sprint_gate.active_sprint(project_id)
    if sprint is None:
        planned = query_one(
            "SELECT * FROM sprints WHERE project_id = ? AND status IN ('Planned','Ready') "
            "ORDER BY created_at", (project_id,))
        if not planned:
            if not _team_members(project_id):
                # Brand-new projects usually lack BOTH a team and a sprint;
                # naming only the sprint step strands users in a loop where
                # the next sprint still cannot assign anything.
                return {"error": "SETUP_INCOMPLETE: this project has no agents assigned yet. "
                                 "First say \"align team <name> to this project\" in the control chat "
                                 "(or pick a team in Project Settings), then \"create a sprint\"; "
                                 "execution can start after that."}
            return {"error": "No sprint to start — create one first (e.g. say \"create a sprint\")"}
        sprint = planned
    with _schedulers_lock:
        if project_id in _schedulers:
            return {"error": "Sprint execution already running"}

    # RULE 1: only one active sprint per project. Another sprint that is
    # active or moving through gates blocks activation outright — no silent
    # demotion, the current sprint must complete or be cancelled first.
    active = sprint_gate.active_sprint(project_id)
    if active and active["id"] != sprint["id"]:
        return {"error": f"Sprint '{active['name']}' is already active ({active['status']}) — "
                         "complete or cancel it before starting another sprint"}
    if sprint["status"] not in ("Planned", "Ready", "Active", "Failed"):
        return {"error": f"Sprint '{sprint['name']}' is {sprint['status']} — cannot start it"}

    task_count = query_one(
        "SELECT COUNT(*) AS n FROM tasks WHERE project_id = ? AND sprint_id = ? "
        "AND status NOT IN ('Done','Cancelled')",
        (project_id, sprint["id"]))["n"]
    if task_count == 0:
        if sprint["status"] in sprint_gate.GATE_PHASES:
            # Mid-gates with no open tasks: resume the gate pipeline — never
            # draft new work into a sprint that's being validated.
            drafted_n = 0
        else:
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

    # Activate via the validated state machine (optimistic, audited).
    if sprint["status"] != "Active":
        ok, err = sprint_gate.transition_sprint(
            sprint["id"], "Active", "Sprint activated by start command",
            executed_by="user")
        if not ok:
            return {"error": f"Could not activate sprint: {err}"}
        sprint = query_one("SELECT * FROM sprints WHERE id = ?", (sprint["id"],))
        if not sprint.get("started_at"):
            update("sprints", sprint["id"], {"started_at": now()})
        _emit(project_id, "sprint.activated",
              {"sprint": sprint["name"], "note": "Sprint activated by start command"})
    assign = auto_assign_tasks(project_id)
    ctrl = {"cancelled": False}
    with _schedulers_lock:
        _schedulers[project_id] = ctrl  # same dict the loop watches, so stop works
    handle = _spawn(_run_sprint(project_id, ctrl))
    ctrl["task"] = handle
    return {"ok": True, "sprint": sprint["name"], "auto_assigned": assign["assigned"],
            "auto_drafted": drafted_n}


def _cancel_handle(handle):
    """Best-effort force-cancel of a spawned task from another thread.

    _spawn returns either a concurrent.futures.Future (threadsafe .cancel())
    or an asyncio.Task (must be cancelled on its own loop)."""
    if handle is None:
        return
    try:
        if isinstance(handle, asyncio.Future) and _loop is not None and _loop.is_running():
            _loop.call_soon_threadsafe(handle.cancel)
        else:
            handle.cancel()
    except Exception as exc:
        logger.debug("Force-cancel failed: %s", exc)


def stop_sprint_execution(project_id: str):
    with _schedulers_lock:
        sched = _schedulers.get(project_id)
    if sched:
        sched["cancelled"] = True
    # Snapshot live run ids under the lock; do DB reads outside it.
    with _registry_lock:
        run_ids = [rid for rid, rc in _registry.items() if not rc.get("cancelled")]
    for rid in run_ids:
        run = get_run(rid)
        if run and run["project_id"] == project_id:
            with _registry_lock:
                rc = _registry.get(rid)
            if rc:
                rc["cancelled"] = True
                rc["paused"] = False
                _cancel_handle(rc.get("task"))
    # Cancel the scheduler task too: it may be parked in a to_thread gate/PO
    # step that doesn't observe the flag promptly, and this guarantees the
    # coroutine unwinds so _run_sprint frees the _schedulers slot right away.
    _cancel_handle(sched.get("task") if sched else None)
    return {"ok": True}


# Where a task interrupted by a service restart resumes. Flattening
# everything to Ready re-ran dev from scratch and destroyed the pipeline
# position: a task waiting on SA review went back to coding. Waiting
# statuses are kept as-is — the scheduler re-claims them in the right mode
# via _MODE_BY_STATUS. Only half-done dev work is re-queued (Ready);
# review/QA stages re-enter through their own waiting status.
def _recovered_status(current: str) -> str:
    return {"In Progress": "Ready", "Review": "Ready",
            "Testing": "Waiting QA"}.get(current, current)


def recover_orphans():
    """Called at startup: mark interrupted runs as failed and free agents.
    Each interrupted task resumes at the phase its status implies (kept
    assignment) so the scheduler re-runs the right mode — a service restart
    is not a work blocker and not a reason to redo finished stages."""
    for run in query("SELECT * FROM workflow_runs WHERE status IN ('Running','Paused')"):
        update("workflow_runs", run["id"], {"status": "Failed", "completed_at": now(),
                                            "current_step": "interrupted (service restart)"})
        if run["task_id"]:
            task = query_one("SELECT status FROM tasks WHERE id = ?", (run["task_id"],))
            resume = _recovered_status(task["status"]) if task else "Ready"
            fields = {"status": resume, "updated_at": now()}
            if resume == "Ready":
                fields["blocked_reason"] = ""
            update("tasks", run["task_id"], fields)
            _emit(run["project_id"], "task.status_changed",
                  {"task_id": run["task_id"], "status": resume,
                   "note": "resumed after service restart"})
        _emit(run["project_id"], "workflow.failed",
              {"run_id": run["id"], "error": "Service restarted; run marked recoverable",
               "recoverable": True}, workflow_run_id=run["id"])
    for agent in query("SELECT * FROM agents WHERE lifecycle_state NOT IN ('Idle')"):
        update("agents", agent["id"], {"lifecycle_state": "Idle", "current_activity": "",
                                       "current_task_id": None, "updated_at": now()})

    # Resume sprints stuck mid-gate: after a service restart the gate pipeline
    # is the one piece of work the in-process scheduler was responsible for.
    # Mark it as recovered and let the runtime scheduler pick it up so the
    # validation phases actually complete instead of staying frozen at
    # "Development Complete" / "Functional Validation" / etc.
    for sprint in query(
        "SELECT * FROM sprints WHERE status IN "
        "(?, ?, ?, ?, ?) AND project_id IS NOT NULL",
        ("Development Complete", "Build Validation", "Automated Testing",
         "Functional Validation", "Sprint Acceptance")):
        pid = sprint["project_id"]
        if not pid:
            continue
        logger.info("Recovering mid-gate sprint %s (%s) for project %s",
                    sprint["name"], sprint["status"], pid)
        # Kick off a fresh scheduler — start_sprint_execution is idempotent
        # for already-active sprints (it resumes the gate pipeline).
        try:
            start_sprint_execution(pid, sprint["id"])
        except Exception as exc:
            logger.error("Failed to resume mid-gate sprint %s: %s",
                         sprint["name"], exc)
