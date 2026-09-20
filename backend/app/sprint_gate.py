"""Sprint gating domain rules (Sprint Gating spec).

A sprint completes only through ALL gates:

    development -> build -> automated tests -> functional validation
                -> acceptance

The next sprint stays LOCKED until the current one completes. Urgent scope
may re-open a sprint mid-gates (sprint back to Active; gates re-run after
the new work drains).

This module owns: sprint state transitions, the Sprint Gatekeeper (task
eligibility), the gate pipeline, rework-task creation and next-sprint
unlock. It may import db/toolchains/codegen — never runtime/po/chatbot at
module level (they import runtime; lazy imports only).
"""

from __future__ import annotations

import time

from . import codegen, toolchains, config
from .db import (audit, emit_event, execute, insert, new_id, now, query,
                 query_one, update)

# ---------------------------------------------------------------- statuses

PLANNED = "Planned"
READY = "Ready"
ACTIVE = "Active"
DEV_COMPLETE = "Development Complete"
BUILD_VALIDATION = "Build Validation"
AUTOMATED_TESTING = "Automated Testing"
FUNCTIONAL_VALIDATION = "Functional Validation"
SPRINT_ACCEPTANCE = "Sprint Acceptance"
COMPLETED = "Completed"
REWORK = "Rework"
FAILED = "Failed"
CANCELLED = "Cancelled"

# A sprint in any of these statuses is "the one active sprint" of a project
# (spec RULE 1: one active sprint — includes in-flight gate phases).
ACTIVE_OR_GATING = (ACTIVE, DEV_COMPLETE, BUILD_VALIDATION, AUTOMATED_TESTING,
                    FUNCTIONAL_VALIDATION, SPRINT_ACCEPTANCE, REWORK)

# Urgent scope may pull a sprint back from any gate phase to Active.
GATE_PHASES = (DEV_COMPLETE, BUILD_VALIDATION, AUTOMATED_TESTING,
               FUNCTIONAL_VALIDATION, SPRINT_ACCEPTANCE)

ALLOWED_TRANSITIONS = {
    PLANNED: {READY, ACTIVE, CANCELLED},
    READY: {ACTIVE, CANCELLED},
    ACTIVE: {DEV_COMPLETE, REWORK, CANCELLED},
    DEV_COMPLETE: {BUILD_VALIDATION, ACTIVE, CANCELLED},          # ACTIVE = urgent re-open
    BUILD_VALIDATION: {AUTOMATED_TESTING, REWORK, ACTIVE, CANCELLED},
    AUTOMATED_TESTING: {FUNCTIONAL_VALIDATION, REWORK, ACTIVE, CANCELLED},
    FUNCTIONAL_VALIDATION: {SPRINT_ACCEPTANCE, REWORK, ACTIVE, CANCELLED},
    SPRINT_ACCEPTANCE: {COMPLETED, REWORK, ACTIVE, CANCELLED},
    REWORK: {ACTIVE, FAILED, CANCELLED},
    COMPLETED: set(),
    FAILED: {ACTIVE, CANCELLED},        # explicit human/PO resolution only
    CANCELLED: {PLANNED},
}

GATE_COLUMNS = ("development_status", "build_status", "test_status",
                "functional_status", "acceptance_status")

MAX_GATE_CYCLES = config.MAX_GATE_CYCLES

GATE_LABELS = {"development_status": "Development", "build_status": "Build",
               "test_status": "Tests", "functional_status": "Functional",
               "acceptance_status": "Acceptance"}


# ---------------------------------------------------------------- helpers

def active_sprint(project_id: str):
    """The single sprint of a project that is active or moving through
    gates (RULE 1). None when the project has no sprint in flight."""
    marks = ",".join("?" * len(ACTIVE_OR_GATING))
    return query_one(
        f"SELECT * FROM sprints WHERE project_id = ? AND status IN ({marks}) "
        "ORDER BY created_at LIMIT 1", (project_id, *ACTIVE_OR_GATING))


def transition_sprint(sprint_id: str, new_status: str, reason: str = "",
                      executed_by: str = "sprint-engine") -> tuple[bool, str]:
    """Validated, optimistic sprint status transition (spec §13). The
    WHERE status = <expected> clause makes concurrent transitions
    impossible even with parallel writers."""
    sprint = query_one("SELECT * FROM sprints WHERE id = ?", (sprint_id,))
    if not sprint:
        return False, "Sprint not found."
    if sprint["status"] == new_status:
        return True, ""
    if new_status not in ALLOWED_TRANSITIONS.get(sprint["status"], set()):
        return False, f"Illegal sprint transition {sprint['status']} -> {new_status}"
    cur = execute("UPDATE sprints SET status = ? WHERE id = ? AND status = ?",
                  (new_status, sprint_id, sprint["status"]))
    if cur.rowcount != 1:
        return False, f"Concurrent transition detected (sprint left {sprint['status']})"
    emit_event(sprint["project_id"], "sprint.status.changed",
               {"sprint": sprint["name"], "status": new_status, "reason": reason})
    audit("sprint_transition", "sprint", sprint_id,
          f"Sprint '{sprint['name']}': {sprint['status']} -> {new_status}"
          + (f" ({reason})" if reason else ""), actor=executed_by)
    return True, ""


def authorize_task(project_id: str, task_id: str) -> dict:
    """Sprint Gatekeeper (spec §10): may this task be claimed right now?
    Backend law — the UI never gets the final say (spec §11)."""
    sprint = active_sprint(project_id)
    if not sprint:
        return {"allowed": False, "reason": "No active sprint.", "sprint_id": None}
    task = query_one("SELECT * FROM tasks WHERE id = ? AND project_id = ?",
                     (task_id, project_id))
    if not task:
        return {"allowed": False, "reason": "Task not found.", "sprint_id": sprint["id"]}
    if not task["sprint_id"]:
        return {"allowed": False, "reason": "Task is not committed to a sprint.",
                "sprint_id": sprint["id"]}
    if task["sprint_id"] != sprint["id"]:
        other = query_one("SELECT name, status FROM sprints WHERE id = ?",
                          (task["sprint_id"],))
        oname = other["name"] if other else task["sprint_id"]
        ostatus = other["status"] if other else "unknown"
        return {"allowed": False,
                "reason": f"Task belongs to a locked sprint ('{oname}' — {ostatus}). "
                          f"Active sprint: '{sprint['name']}'.",
                "sprint_id": sprint["id"]}
    if sprint["status"] != ACTIVE:
        return {"allowed": False,
                "reason": f"Sprint is not accepting development work "
                          f"(status: {sprint['status']}).",
                "sprint_id": sprint["id"]}
    return {"allowed": True, "reason": "Task authorized.", "sprint_id": sprint["id"]}


def reopen_sprint_for_scope(sprint_id: str, reason: str = "urgent scope added") -> tuple[bool, str]:
    """Urgent task added mid-gates: pull the sprint back to Active, reset
    the gate statuses (they re-run once the new work drains). No-op for an
    already-Active sprint; rejected for terminal statuses."""
    sprint = query_one("SELECT * FROM sprints WHERE id = ?", (sprint_id,))
    if not sprint:
        return False, "Sprint not found."
    if sprint["status"] == ACTIVE:
        return True, ""
    if sprint["status"] not in GATE_PHASES + (REWORK,):
        return False, f"Sprint in status '{sprint['status']}' cannot be re-opened."
    ok, err = transition_sprint(sprint_id, ACTIVE, f"Re-opened for scope: {reason}")
    if not ok:
        return False, err
    vals = {col: "Pending" for col in GATE_COLUMNS}
    vals["failure_reason"] = ""
    update("sprints", sprint_id, vals)
    emit_event(sprint["project_id"], "project.updated",
               {"note": f"Sprint '{sprint['name']}' re-opened for urgent scope — "
                        f"gates will re-run after the new work completes"})
    audit("sprint_reopen_scope", "sprint", sprint_id,
          f"Re-opened '{sprint['name']}' for scope: {reason}")
    return True, ""


# ------------------------------------------------------- gate executions

def _start_gate_exec(sprint_id: str, gate_type: str) -> str:
    gid = new_id()
    insert("sprint_gate_executions", {
        "id": gid, "sprint_id": sprint_id, "gate_type": gate_type,
        "status": "Running", "started_at": now(), "executed_by": "sprint-engine",
        "created_at": now()})
    return gid


def _finish_gate_exec(exec_id: str, status: str, summary: str = "", error: str = ""):
    row = query_one("SELECT started_at FROM sprint_gate_executions WHERE id = ?", (exec_id,))
    dur = None
    if row and row["started_at"]:
        try:
            import datetime as _dt
            started = _dt.datetime.fromisoformat(row["started_at"])
            dur = int(( _dt.datetime.now() - started).total_seconds() * 1000)
        except Exception:
            pass
    update("sprint_gate_executions", exec_id,
           {"status": status, "completed_at": now(), "duration_ms": dur,
            "result_summary": summary[:500], "error_message": error[:500]})


def _emit_gate(project_id, sprint_name, gate, status, summary=""):
    emit_event(project_id, "sprint.gate.changed",
               {"sprint": sprint_name, "gate": gate, "status": status,
                "summary": str(summary)[:200]})


def _set_gate_status(sprint_id: str, column: str, status: str):
    update("sprints", sprint_id, {column: status})


# ---------------------------------------------------- acceptance criteria

def _split_ac(text: str) -> list[str]:
    """Split raw acceptance-criteria text into individual criteria
    (newlines or semicolons as separators)."""
    out: list[str] = []
    for line in (text or "").splitlines():
        for part in line.split(";"):
            part = part.strip().lstrip("-*•0123456789.). ").strip()
            if len(part) >= 8:
                out.append(part)
    if not out:
        text = (text or "").strip()
        if len(text) >= 8:
            out = [text]
    return out


def seed_sprint_acs(sprint) -> int:
    """Derive sprint acceptance-criteria rows from the sprint's tasks'
    acceptance_criteria text (falling back to the sprint goal). Incremental:
    new descriptions are appended (urgent scope adds its criteria too)."""
    existing = {r["description"] for r in query(
        "SELECT description FROM sprint_acceptance_criteria WHERE sprint_id = ?",
        (sprint["id"],))}
    texts: list[str] = []
    for t in query("SELECT acceptance_criteria FROM tasks WHERE sprint_id = ? "
                   "AND status != 'Cancelled' ORDER BY created_at", (sprint["id"],)):
        texts.extend(_split_ac(t["acceptance_criteria"] or ""))
    if not texts:
        texts = _split_ac(sprint["goal"] or "")
    added = 0
    n = len(existing)
    for txt in texts:
        if txt in existing or len(txt) < 8:
            continue
        n += 1
        insert("sprint_acceptance_criteria", {
            "id": new_id(), "sprint_id": sprint["id"], "code": f"AC-{n:02d}",
            "description": txt[:400], "required": 1, "status": "Pending",
            "created_at": now()})
        existing.add(txt)
        added += 1
    return added


# ---------------------------------------------------------- rework tasks

def _insert_rework_task(project_id, sprint_id, title, description) -> bool:
    if query_one("SELECT id FROM tasks WHERE sprint_id = ? AND title = ? "
                 "AND status NOT IN ('Done','Cancelled') LIMIT 1",
                 (sprint_id, title)):
        return False
    ts = now()
    insert("tasks", {
        "id": new_id(), "project_id": project_id, "sprint_id": sprint_id,
        "backlog_item_id": None, "title": title[:120],
        "description": description[:1000], "story_points": 3, "priority": 1,
        "status": "Todo", "created_at": ts, "updated_at": ts})
    return True


def _module_rework_specs(project, failing: set) -> list[dict]:
    """One focused rework task per failing test module (with the actual
    assertion details) — small scopes converge; a catch-all never does."""
    modules: dict[str, list[str]] = {}
    for nodeid in failing or set():
        path = str(nodeid).split("::")[0].replace("\\", "/")
        modules.setdefault(path, []).append(str(nodeid))
    specs = []
    stack = toolchains.detect_stack(project)
    ws = project["workspace_path"]
    for path, ids in sorted(modules.items()):
        details = toolchains.failing_test_details(stack, ws, path)
        specs.append({
            "title": f"Sprint gate rework: fix failing tests in {path}",
            "description": ("Sprint tests gate failed. Fix app and/or test code until "
                            "this module's tests pass.\n" + details)[:1000]})
    return specs


def _gate_failed(project, sprint_id: str, gate_column: str, reason: str,
                 rework_specs: list[dict]) -> str:
    """A gate failed: sprint -> Rework -> Active with rework tasks, or
    -> Failed after MAX_GATE_CYCLES (spec §34/§35)."""
    sprint = query_one("SELECT * FROM sprints WHERE id = ?", (sprint_id,))
    # The current cycle's gate execution is already recorded as 'Failed' by
    # the caller (_finish_gate_exec runs before _gate_failed), so this count
    # includes it. Do NOT add +1 here — that would double-count the current
    # failure and cap the sprint one cycle early.
    prior_failures = query_one(
        "SELECT COUNT(*) AS n FROM sprint_gate_executions "
        "WHERE sprint_id = ? AND status = 'Failed'", (sprint_id,))["n"]
    cycle = prior_failures
    update("sprints", sprint_id, {"failure_reason": reason[:400]})
    _emit_gate(project["id"], sprint["name"], GATE_LABELS[gate_column], "Failed", reason)

    ok, _ = transition_sprint(sprint_id, REWORK, f"{GATE_LABELS[gate_column]} gate failed "
                                                  f"(cycle {cycle}): {reason[:150]}")
    if not ok:
        return "Aborted"

    if cycle >= MAX_GATE_CYCLES:
        ok, _ = transition_sprint(sprint_id, FAILED,
                                  f"Gates failed {cycle} times — needs explicit resolution")
        if not ok:
            return "Aborted"
        emit_event(project["id"], "project.updated",
                   {"note": f"Sprint '{sprint['name']}' FAILED after {cycle} gate "
                            f"cycles — manual or PO resolution required"})
        return "Failed"

    created = 0
    for spec in rework_specs:
        if _insert_rework_task(project["id"], sprint_id, spec["title"], spec["description"]):
            created += 1
    if not created and not rework_specs:
        # No actionable specs (e.g. LLM outage): a generic retry task so the
        # loop has something to run and the failure stays visible.
        _insert_rework_task(project["id"], sprint_id,
                            f"Sprint gate rework: resolve {GATE_LABELS[gate_column]} gate failure",
                            f"Gate failed: {reason[:800]}")
    transition_sprint(sprint_id, ACTIVE, f"Rework queued ({created} task(s)) — development re-opened")
    return "Rework"


# ------------------------------------------------- functional validation

def _validate_functional(project, sprint, ctrl) -> tuple[bool, str, list[dict], bool]:
    """Validate every sprint acceptance criterion against the workspace.
    Returns (passed, summary, rework_specs, outage). On LLM outage no
    rework specs are created — it's infrastructure, not a code failure."""
    seed_sprint_acs(sprint)
    acs = query("SELECT * FROM sprint_acceptance_criteria WHERE sprint_id = ? "
                "ORDER BY code", (sprint["id"],))
    if not acs:
        return True, "No acceptance criteria recorded — nothing to validate", [], False
    gw, model = codegen.resolve_llm(project)
    if not (gw and model):
        return False, "No LLM gateway configured for functional validation", [], False
    stack = toolchains.detect_stack(project)
    tree = codegen._existing_tree(project["workspace_path"])[:3000]
    _tok, test_summary = toolchains.run_stack_tests(stack, project["workspace_path"])
    from .chatbot import _extract_json  # lazy: chatbot imports runtime
    system = ("You are a QA acceptance validator. Given one sprint acceptance "
              "criterion and project evidence (workspace files, test results), "
              "decide whether the implemented project satisfies it. Be strict: "
              "only 'passed' when the evidence clearly shows it. Return ONLY "
              'a JSON object: {"passed": true|false, "evidence": "one sentence"}')
    specs, passed_n = [], 0
    for ac in acs:
        if ac["status"] == "Passed" and ac["result"] == "manually accepted":
            passed_n += 1  # human override (spec §38) — not re-validated
            continue
        if ctrl and ctrl.get("cancelled"):
            return False, "cancelled", [], False
        user = (f"PROJECT: {project['name']}\nGOAL: {project['goal'] or 'n/a'}\n"
                f"SPRINT: {sprint['name']} — {sprint['goal'] or 'n/a'}\n"
                f"WORKSPACE FILES:\n{tree}\n\nTEST RESULT: {test_summary[:300]}\n\n"
                f"ACCEPTANCE CRITERION {ac['code']}: {ac['description']}\n\n"
                "Is this criterion satisfied by the implemented workspace?")
        try:
            raw = codegen.call_llm(gw, model, system, user, max_tokens=400)
        except Exception as exc:
            update("sprint_acceptance_criteria", ac["id"],
                   {"status": "Blocked", "failure_reason": str(exc)[:300],
                    "validated_at": now()})
            return False, f"Functional validation error: {str(exc)[:150]}", [], True
        text = (raw or {}).get("text") or ""
        data = _extract_json(text)
        if not data or not isinstance(data.get("passed"), bool):
            outage = codegen._llm_outage_reason(text)
            if outage:
                return False, f"Functional validation could not run: {outage}", [], True
            data = {"passed": False, "evidence": "validator returned unusable output"}
        ok = data["passed"]
        evidence = str(data.get("evidence") or "")[:300]
        update("sprint_acceptance_criteria", ac["id"],
               {"status": "Passed" if ok else "Failed",
                "result": evidence, "failure_reason": "" if ok else evidence,
                "validated_at": now()})
        if ok:
            passed_n += 1
        else:
            specs.append({
                "title": f"Sprint gate rework: satisfy {ac['code']}",
                "description": (f"Functional validation failed for {ac['code']}: "
                                f"{ac['description']}\nEvidence: {evidence}")[:1000]})
    return (passed_n == len(acs),
            f"{passed_n}/{len(acs)} acceptance criteria passed", specs, False)


# ------------------------------------------------------------ pipeline

def run_sprint_gates(project_id: str, sprint_id: str, ctrl: dict | None = None) -> str:
    """The full sprint completion pipeline (spec §21). Synchronous — call
    via asyncio.to_thread. Returns 'Completed' | 'Rework' | 'Failed' |
    'Aborted' (aborted = cancelled or the sprint moved underneath us,
    e.g. re-opened for urgent scope). Resumes from the current status
    after a crash/restart."""
    ctrl = ctrl or {}
    project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    sprint = query_one("SELECT * FROM sprints WHERE id = ?", (sprint_id,))
    if not project or not sprint or not project.get("workspace_path"):
        return "Aborted"

    def _current_status():
        row = query_one("SELECT status FROM sprints WHERE id = ?", (sprint_id,))
        return row["status"] if row else CANCELLED

    status = _current_status()

    # -- development complete --------------------------------------------
    if status == ACTIVE:
        done = query_one("SELECT COUNT(*) AS n FROM tasks WHERE sprint_id = ? "
                         "AND status = 'Done'", (sprint_id,))["n"]
        ok, err = transition_sprint(sprint_id, DEV_COMPLETE,
                                    f"All sprint tasks complete ({done} done)")
        if not ok:
            return "Aborted"
        _set_gate_status(sprint_id, "development_status", "Passed")
        _emit_gate(project_id, sprint["name"], "Development", "Passed",
                   f"{done} task(s) completed")
        status = DEV_COMPLETE

    # -- build -----------------------------------------------------------
    if status == DEV_COMPLETE:
        ok, _ = transition_sprint(sprint_id, BUILD_VALIDATION, "Development complete — build validation")
        if not ok:
            return "Aborted"
        status = BUILD_VALIDATION
    if status == BUILD_VALIDATION:
        if ctrl.get("cancelled"):
            return "Aborted"
        exec_id = _start_gate_exec(sprint_id, "BUILD")
        _set_gate_status(sprint_id, "build_status", "Running")
        _emit_gate(project_id, sprint["name"], "Build", "Running")
        stack = toolchains.detect_stack(project)
        bok, summary = toolchains.build_check(stack, project["workspace_path"])
        _finish_gate_exec(exec_id, "Passed" if bok else "Failed",
                          summary, "" if bok else summary)
        _set_gate_status(sprint_id, "build_status", "Passed" if bok else "Failed")
        if not bok:
            return _gate_failed(
                project, sprint_id, "build_status", f"Build failed: {summary[:250]}",
                [{"title": "Sprint gate rework: fix build failure",
                  "description": f"The sprint build gate failed — the build MUST pass "
                                 f"before delivery.\n{summary[:800]}"}])
        _emit_gate(project_id, sprint["name"], "Build", "Passed", summary)
        if _current_status() != BUILD_VALIDATION:
            return "Aborted"  # re-opened for urgent scope mid-gate
        ok, _ = transition_sprint(sprint_id, AUTOMATED_TESTING, "Build passed — automated testing")
        if not ok:
            return "Aborted"
        status = AUTOMATED_TESTING

    # -- automated tests ---------------------------------------------------
    if status == AUTOMATED_TESTING:
        if ctrl.get("cancelled"):
            return "Aborted"
        exec_id = _start_gate_exec(sprint_id, "AUTOMATED_TEST")
        _set_gate_status(sprint_id, "test_status", "Running")
        _emit_gate(project_id, sprint["name"], "Tests", "Running")
        stack = toolchains.detect_stack(project)
        tok, summary = toolchains.run_stack_tests(stack, project["workspace_path"])
        _finish_gate_exec(exec_id, "Passed" if tok else "Failed",
                          summary, "" if tok else summary)
        _set_gate_status(sprint_id, "test_status", "Passed" if tok else "Failed")
        if not tok:
            failing = toolchains.failing_tests(stack, project["workspace_path"])
            specs = _module_rework_specs(project, failing) if failing else []
            return _gate_failed(project, sprint_id, "test_status",
                                f"Test suite failed: {summary[:250]}", specs)
        _emit_gate(project_id, sprint["name"], "Tests", "Passed", summary)
        if _current_status() != AUTOMATED_TESTING:
            return "Aborted"
        ok, _ = transition_sprint(sprint_id, FUNCTIONAL_VALIDATION,
                                  "Tests passed — functional validation")
        if not ok:
            return "Aborted"
        status = FUNCTIONAL_VALIDATION

    # -- functional validation -------------------------------------------
    if status == FUNCTIONAL_VALIDATION:
        if ctrl.get("cancelled"):
            return "Aborted"
        exec_id = _start_gate_exec(sprint_id, "FUNCTIONAL")
        _set_gate_status(sprint_id, "functional_status", "Running")
        _emit_gate(project_id, sprint["name"], "Functional", "Running")
        fok, fsummary, specs, outage = _validate_functional(project, sprint, ctrl)
        _finish_gate_exec(exec_id, "Passed" if fok else "Failed", fsummary,
                          "" if fok else fsummary)
        _set_gate_status(sprint_id, "functional_status", "Passed" if fok else "Failed")
        if not fok:
            return _gate_failed(project, sprint_id, "functional_status",
                                f"Functional validation failed: {fsummary[:250]}",
                                [] if outage else specs)
        _emit_gate(project_id, sprint["name"], "Functional", "Passed", fsummary)
        if _current_status() != FUNCTIONAL_VALIDATION:
            return "Aborted"
        ok, _ = transition_sprint(sprint_id, SPRINT_ACCEPTANCE,
                                  "Functional validation passed — sprint acceptance")
        if not ok:
            return "Aborted"
        status = SPRINT_ACCEPTANCE

    # -- acceptance ---------------------------------------------------------
    if status == SPRINT_ACCEPTANCE:
        if ctrl.get("cancelled"):
            return "Aborted"
        exec_id = _start_gate_exec(sprint_id, "ACCEPTANCE")
        _set_gate_status(sprint_id, "acceptance_status", "Running")
        _emit_gate(project_id, sprint["name"], "Acceptance", "Running")
        acs = query("SELECT * FROM sprint_acceptance_criteria WHERE sprint_id = ? "
                    "AND required = 1", (sprint_id,))
        unmet = [a for a in acs if a["status"] != "Passed"]
        if unmet:
            summary = f"{len(acs) - len(unmet)}/{len(acs)} required criteria passed"
            _finish_gate_exec(exec_id, "Failed", summary, summary)
            _set_gate_status(sprint_id, "acceptance_status", "Failed")
            specs = [{"title": f"Sprint gate rework: satisfy {a['code']}",
                      "description": (f"Acceptance criterion not met: {a['description']}"
                                      f"\nFailure: {a['failure_reason'][:400]}")}
                     for a in unmet]
            return _gate_failed(project, sprint_id, "acceptance_status",
                                f"Acceptance failed: {summary}", specs)
        summary = f"All {len(acs)} required acceptance criteria passed" if acs \
            else "No required acceptance criteria recorded"
        _finish_gate_exec(exec_id, "Passed", summary)
        _set_gate_status(sprint_id, "acceptance_status", "Passed")
        ok, err = transition_sprint(sprint_id, COMPLETED,
                                    "All gates passed — sprint completed")
        if not ok:
            return "Aborted"
        update("sprints", sprint_id, {"completed_at": now(), "end_at": now(),
                                      "failure_reason": ""})
        _emit_gate(project_id, sprint["name"], "Acceptance", "Passed", summary)
        emit_event(project_id, "project.updated",
                   {"note": f"Sprint '{sprint['name']}' COMPLETED — all gates passed"})
        unlock_next_sprint(project_id, sprint_id)
        return "Completed"

    return "Aborted"


# ---------------------------------------------------------------- unlock

def unlock_next_sprint(project_id: str, completed_sprint_id: str):
    """Next sprint Planned -> Ready after the current one completes (spec
    §20). READY != ACTIVE — activation is an explicit later step."""
    done = query_one("SELECT * FROM sprints WHERE id = ?", (completed_sprint_id,))
    if not done or done["status"] != COMPLETED:
        return None
    nxt = query_one(
        "SELECT * FROM sprints WHERE project_id = ? AND status = ? AND id != ? "
        "ORDER BY created_at LIMIT 1", (project_id, PLANNED, completed_sprint_id))
    if not nxt:
        return None
    ok, _ = transition_sprint(nxt["id"], READY,
                              f"Unlocked after '{done['name']}' completed")
    if not ok:
        return None
    emit_event(project_id, "sprint.unlocked",
               {"sprint": nxt["name"], "previous": done["name"]})
    return nxt


def next_locked_sprint(project_id: str):
    """The sprint waiting on the current one (for UI lock display)."""
    return query_one(
        "SELECT id, name, status FROM sprints WHERE project_id = ? AND status = ? "
        "ORDER BY created_at LIMIT 1", (project_id, PLANNED))


# ------------------------------------------------------------- summaries

def sprint_gate_summary(sprint_id: str) -> dict | None:
    """Gate checklist + acceptance criteria for API/UI (spec §27)."""
    sprint = query_one("SELECT * FROM sprints WHERE id = ?", (sprint_id,))
    if not sprint:
        return None
    gates = [{"gate": GATE_LABELS[col], "status": sprint[col]}
             for col in GATE_COLUMNS]
    acs = query("SELECT code, description, required, status, result, failure_reason "
                "FROM sprint_acceptance_criteria WHERE sprint_id = ? ORDER BY code",
                (sprint_id,))
    return {"sprint_id": sprint_id, "sprint": sprint["name"],
            "status": sprint["status"], "gates": gates,
            "acceptance_criteria": acs, "failure_reason": sprint["failure_reason"]}


def gate_history(sprint_id: str) -> list[dict]:
    return query("SELECT * FROM sprint_gate_executions WHERE sprint_id = ? "
                 "ORDER BY created_at DESC LIMIT 50", (sprint_id,))
