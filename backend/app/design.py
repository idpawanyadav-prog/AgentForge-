"""Design Phase — a project flow's configurable pre-delivery workflow.

The Design Phase sits between the Initial Requirement and the per-task Stage
Chain. It is an ordered list of steps, each owned by a role and made of
processes from a fixed catalog (every process maps to a document kind the
authoring pipeline already knows how to write). The run is strictly
sequential: a step's processes are authored, and only once a step that
requires approval is approved does the next step start. Rejection sends the
step back to its role for a revision pass, not forward.

When the final step clears, the authored documents are frozen into the
requirement baseline and the existing blueprint + sprint breakdown runs —
so Design Phase is a drop-in generalisation of the old hardcoded docs gate.
"""
from __future__ import annotations

import json
import logging
import threading

from . import governance, po, specs
from .db import (audit, execute, insert, new_id, now, query, query_one,
                 update)

logger = logging.getLogger(__name__)

# Fixed process catalog: display name -> document kind. Every kind here must
# exist in specs.DOC_SPECS so it can be authored. `feeds_baseline` marks the
# documents that become requirement items when the phase completes (the ones
# the blueprint prompt already consumes).
PROCESS_CATALOG = {
    "Requirement Doc": {"kind": "BAS", "feeds_baseline": True},
    "Project Definition Sheet": {"kind": "PDS", "feeds_baseline": True},
    "Technical Spec": {"kind": "TS", "feeds_baseline": True},
    "Backend Architecture": {"kind": "ARCH", "feeds_baseline": False},
    "Development Approach": {"kind": "DEVPLAN", "feeds_baseline": False},
    "Implementation Plan": {"kind": "IMPLPLAN", "feeds_baseline": False},
    "Test Strategy": {"kind": "TESTPLAN", "feeds_baseline": False},
}

BASELINE_KINDS = ("BAS", "PDS", "TS")

# run status
RUNNING = "running"
AWAITING = "awaiting_approval"
COMPLETED = "completed"


class DesignError(RuntimeError):
    pass


def catalog() -> list:
    """The process catalog for the UI (name + document kind)."""
    return [{"process": name, "kind": meta["kind"]} for name, meta in
            PROCESS_CATALOG.items()]


def _parse_run(row) -> dict:
    out = dict(row)
    out["steps"] = json.loads(row["steps_json"] or "[]")
    out.pop("steps_json", None)
    return out


def get_run(project_id: str) -> dict | None:
    row = query_one("SELECT * FROM project_design_runs WHERE project_id = ?",
                    (project_id,))
    return _parse_run(row) if row else None


def _set_run(project_id: str, values: dict):
    """Update the run row (keyed by project_id, not the generic id column)."""
    sets = ", ".join(f"{k} = ?" for k in values)
    execute(f"UPDATE project_design_runs SET {sets} WHERE project_id = ?",
            tuple(values.values()) + (project_id,))


def normalize_steps(design: list) -> list:
    """Validate + renumber a design-step list, returning cleaned steps.
    Raises DesignError with a user-facing message on the first problem."""
    if not design:
        return []
    known_roles = {r["name"]: r["id"] for r in query("SELECT id, name FROM roles")}
    steps = []
    for i, raw in enumerate(design):
        seq = i + 1
        role = str((raw or {}).get("role") or "").strip()
        if not role:
            raise DesignError(f"Design step {seq} needs a role.")
        if role not in known_roles:
            raise DesignError(f"Design step {seq} references unknown role '{role}'.")
        procs = (raw or {}).get("processes") or []
        if not isinstance(procs, list) or not procs:
            raise DesignError(f"Design step {seq} ({role}) needs at least one process.")
        cleaned = []
        for p in procs:
            if p not in PROCESS_CATALOG:
                raise DesignError(f"Unknown design process '{p}' in step {seq}.")
            if p not in cleaned:
                cleaned.append(p)
        steps.append({"seq": seq, "role": role, "processes": cleaned,
                      "approval_required": bool((raw or {}).get("approval_required"))})
    return steps


# ------------------------------------------------------------------ start / step

_lock = threading.Lock()
_running: dict[str, int] = {}   # project_id -> thread ident owning the phase


def _claim(project_id: str) -> bool:
    with _lock:
        if project_id in _running:
            return False
        _running[project_id] = threading.get_ident()
        return True


def _release(project_id: str):
    with _lock:
        _running.pop(project_id, None)


def start_run(project_id: str, flow: dict, conversation_id: str | None = None) -> str:
    """Begin (or restart) the Design Phase for a project from its flow."""
    steps = normalize_steps(flow.get("design") or [])
    if not steps:
        return "This flow has no Design Phase steps configured."
    project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        return "Project not found."
    state = governance.current_state(project_id)
    if state in (governance.ACTIVE_DEV, governance.FINAL_VALIDATION, governance.COMPLETED):
        return (f"Project is already in '{state}' — the design phase does not re-run there. "
                "Use change requests / new sprints for new scope.")
    if not _claim(project_id):
        return "The design phase is already running for this project — I'll post here as " \
               "each step finishes."
    run = get_run(project_id)
    if run and run["status"] == COMPLETED:
        _release(project_id)
        return "The design phase for this project is already complete."
    if run and run["status"] == AWAITING:
        _release(project_id)
        return ("The design phase is paused at an approval gate — reply `approve design` "
                "or `reject design: <notes>` before restarting it.")
    if not project["governance_enabled"]:
        update("projects", project_id, {"governance_enabled": 1, "updated_at": now()})
    if state == governance.DRAFT:
        governance.transition_project(project_id, governance.REQ_ANALYSIS,
                                      reason="design phase started", actor="agent")
    ts = now()
    if run:
        _set_run(project_id, {
            "flow_id": flow.get("id"), "steps_json": json.dumps(steps),
            "current_seq": 1, "status": RUNNING, "updated_at": ts,
            "completed_at": None})
    else:
        insert("project_design_runs", {
            "project_id": project_id, "flow_id": flow.get("id"),
            "steps_json": json.dumps(steps), "current_seq": 1, "status": RUNNING,
            "started_at": ts, "updated_at": ts, "completed_at": None})
    audit("design_started", "project", project_id,
          f"Design phase started with {len(steps)} step(s)", actor="agent")
    threading.Thread(target=_run_pipeline, args=(project_id, conversation_id),
                     daemon=True, name=f"design-{project_id[:8]}").start()
    return (f"🎨 **Design phase started** — {len(steps)} step(s). I'll post each document "
            "here as the responsible role authors it, and pause for approval where a step "
            "requires it.")


def _post(project_id, text, conversation_id):
    specs._post(project_id, text, conversation_id)


def _doc_links(project_id, kinds):
    return specs._doc_links(project_id, kinds)


def _author_step(project_id: str, step: dict, conversation_id, review_notes: str = ""):
    """Run one step's processes (author each mapped document)."""
    kinds = [PROCESS_CATALOG[p]["kind"] for p in step["processes"]]
    for proc, kind in zip(step["processes"], kinds):
        _post(project_id, f"✍️ Step {step['seq']} — {step['role']} is authoring "
                          f"**{proc}** ({kind}) …", conversation_id)
        specs.author_doc(project_id, kind, review_notes)
    return kinds


def _finalize_run(project_id: str):
    try:
        _complete(project_id, None)
    finally:
        _release(project_id)


_MAX_PO_CYCLES = 1


def _set_next(project_id: str, run) -> int | None:
    """Advance current_seq past ``current_seq``. Returns the new seq, or None
    if the current step was the last."""
    nxt = [s["seq"] for s in run["steps"] if s["seq"] > run["current_seq"]]
    if not nxt:
        return None
    new_seq = min(nxt)
    _set_run(project_id, {"current_seq": new_seq,
                                               "status": RUNNING, "updated_at": now()})
    _post(project_id, f"✅ Step {run['current_seq']} cleared — starting step {new_seq} …",
          None)
    return new_seq


def _gate_message(project_id: str, step: dict) -> str:
    return (f"🛑 **Step {step['seq']}** ({step['role']}) awaits approval:\n"
            + _doc_links(project_id, [PROCESS_CATALOG[p]["kind"]
                                      for p in step["processes"]])
            + "\n\n" + _reviewer_line(project_id))


def _run_pipeline(project_id: str, conversation_id, first_notes: str = ""):
    """Walk the design run from the current step to the next human gate (or
    completion). The caller owns the project claim for the whole call."""
    try:
        notes = first_notes
        while True:
            run = get_run(project_id)
            if not run:
                return
            step = next((s for s in run["steps"] if s["seq"] == run["current_seq"]), None)
            if step is None:
                _complete(project_id, conversation_id)
                return
            _author_step(project_id, step, conversation_id, notes)
            notes = ""
            if not step["approval_required"]:
                _record_approval(project_id, step["seq"], step["role"], "approve",
                                 "auto (no approval required)", "system")
                if _set_next(project_id, run) is None:
                    _complete(project_id, conversation_id)
                    return
                continue
            # Approval gate. Human default; PO autonomy reviews automatically.
            cycle = 0
            while True:
                _set_run(project_id, {"status": AWAITING,
                                                           "updated_at": now()})
                _post(project_id, _gate_message(project_id, step), conversation_id)
                assigned = po.po_agent_for_project(project_id)
                if not (assigned and po.po_enabled(project_id)) or cycle > _MAX_PO_CYCLES:
                    return  # leave awaiting a human decision
                verdict = _po_review(project_id, step)
                if verdict["verdict"] == "approve":
                    _post(project_id, f"👑 **{assigned['name']} approved step {step['seq']}** "
                                      f"— notes: {verdict['notes'][:400]}", conversation_id)
                    _record_approval(project_id, step["seq"], step["role"], "approve",
                                     assigned["name"], "agent", verdict["notes"])
                    run = get_run(project_id)
                    if _set_next(project_id, run) is None:
                        _complete(project_id, conversation_id)
                        return
                    break  # outer loop → next step
                cycle += 1
                _post(project_id, f"👑 **{assigned['name']} sent step {step['seq']} back** "
                                  f"for revision:\n- " + "\n- ".join(
                                      ln.strip() for ln in
                                      verdict["notes"].splitlines() if ln.strip())[:800],
                          conversation_id)
                _record_approval(project_id, step["seq"], step["role"], "reject",
                                 assigned["name"], "agent", verdict["notes"])
                _set_run(project_id, {"status": RUNNING,
                                                           "updated_at": now()})
                _author_step(project_id, step, conversation_id, verdict["notes"])
                # loop again → re-present gate / re-review (bounded by cycle cap)
    except Exception as exc:
        logger.exception("design pipeline failed for %s", project_id)
        _post(project_id, f"⚠️ The design phase stopped: {str(exc)[:300]}\n"
                          "Fix the cause, then say `start design phase` again.",
              conversation_id)
    finally:
        _release(project_id)


def _po_review(project_id: str, step: dict) -> dict:
    kinds = [PROCESS_CATALOG[p]["kind"] for p in step["processes"]]
    summary = "\n\n".join(
        f"=== {k} ===\n{(d := specs.latest_doc(project_id, k)) and d['content_md'][:6000]}"
        for k in kinds if specs.latest_doc(project_id, k))
    text = specs._llm(project_id,
                      specs.PO_DOC_REVIEW_SYSTEM + specs._role_voice(project_id, po.PO_ROLE),
                      specs._brief_block(project_id) + "\n\n" + summary,
                      max_tokens=700, label="po:design-review")
    data = specs.codegen._extract_object(text)
    verdict = str(data.get("verdict") or "").lower()
    if verdict not in ("approve", "request_revision"):
        verdict = "approve"
    return {"verdict": "approve" if verdict == "approve" else "reject",
            "notes": str(data.get("notes") or "")[:1000]}


def _reviewer_line(project_id: str) -> str:
    assigned = po.po_agent_for_project(project_id)
    if assigned and po.po_enabled(project_id):
        return f"{assigned['name']} (Product Owner) is set to review this step automatically."
    if assigned:
        return (f"{assigned['name']} (Product Owner) is assigned — turn on PO autonomy to "
                "auto-review, or reply `approve design` yourself.")
    return "Reply `approve design` to continue, or `reject design: <notes>` to send it back."


# ------------------------------------------------------------------ complete


def _complete(project_id: str, conversation_id):
    """Design phase finished: freeze baseline docs into the requirement
    baseline and run blueprint + sprint breakdown (the existing pipeline)."""
    _set_run(project_id, {"status": COMPLETED, "updated_at": now(),
                                               "completed_at": now()})
    _post(project_id, "🎉 **Design phase complete** — freezing the requirement baseline and "
                      "authoring the blueprint + sprint breakdown …", conversation_id)
    for kind in BASELINE_KINDS:
        d = specs.latest_doc(project_id, kind)
        if d:
            governance.create_requirement(project_id, kind, d["content_md"],
                                          source_type="doc", source_path=f"docs/{kind}.md",
                                          actor="agent")
    res = governance.propose_baseline(project_id, "requirement", actor="agent")
    if res.get("error"):
        _post(project_id, f"⚠️ Design docs are ready but the baseline could not be "
                          f"proposed: {res['error']}", conversation_id)
        return
    bid = res["baseline"]["id"]
    governance.decide_baseline(project_id, bid, "approve",
                               "design phase approved", actor="design-phase")
    audit("design_completed", "project", project_id,
          "Design phase completed — requirement baseline approved", actor="agent")


# ------------------------------------------------------------------ decide (human / UI)

def pending_step(project_id: str):
    run = get_run(project_id)
    if not run or run["status"] != AWAITING:
        return None
    return next((s for s in run["steps"] if s["seq"] == run["current_seq"]), None)


def _record_approval(project_id, seq, role, decision, actor, actor_type, comments=""):
    insert("design_approvals", {
        "id": new_id(), "project_id": project_id, "seq": seq, "role": role or "",
        "decision": decision, "actor": actor or "", "actor_type": actor_type,
        "comments": (comments or "")[:2000], "created_at": now()})
    emit = "design.step.approved" if decision == "approve" else "design.step.rejected"
    from .db import emit_event
    emit_event(project_id, emit, {"seq": seq, "decision": decision, "actor": actor})


def decide(project_id: str, decision: str, notes: str = "", actor: str = "user") -> dict:
    """Approve or reject the current awaiting step. Reject returns the step to
    its role for revision; approve advances (or completes) the phase."""
    if decision not in ("approve", "reject"):
        return {"error": "decision must be approve | reject"}
    step = pending_step(project_id)
    if not step:
        return {"error": "There is no design step awaiting approval right now."}
    _record_approval(project_id, step["seq"], step["role"], decision, actor,
                     "user", notes)
    if decision == "reject":
        _set_run(project_id, {"status": RUNNING, "updated_at": now()})
        return {"ok": True, "seq": step["seq"], "decision": "reject"}
    _set_run(project_id, {"status": RUNNING, "updated_at": now()})
    return {"ok": True, "seq": step["seq"], "decision": "approve"}


def approve_design(project_id: str) -> str:
    r = decide(project_id, "approve", actor="user")
    if r.get("error"):
        return f"Could not approve the design step: {r['error']}"
    if not _claim(project_id):
        return (f"✅ Step {r['seq']} approved — but the phase is busy; it will continue "
                "shortly.")
    run = get_run(project_id)
    if _set_next(project_id, run) is None:
        threading.Thread(target=_finalize_run, args=(project_id,),
                         daemon=True, name=f"design-{project_id[:8]}").start()
    else:
        threading.Thread(target=_run_pipeline, args=(project_id, None),
                         daemon=True, name=f"design-{project_id[:8]}").start()
    return (f"✅ **Step {r['seq']} approved** — the design phase is moving to the next "
            "step (or finishing). Watch this chat.")


def reject_design(project_id: str, notes: str = "") -> str:
    r = decide(project_id, "reject", notes, actor="user")
    if r.get("error"):
        return f"Could not reject the design step: {r['error']}"
    if not _claim(project_id):
        return (f"❌ Step {r['seq']} rejected — the phase is busy; say "
                "`start design phase` once it is idle.")
    threading.Thread(target=_run_pipeline, args=(project_id, None),
                     kwargs={"first_notes": notes or "General polish requested."},
                     daemon=True, name=f"design-{project_id[:8]}").start()
    return (f"❌ **Step {r['seq']} rejected** — sending it back to its role for revision: "
            f"“{(notes or '')[:160]}”")


# ------------------------------------------------------------------ state (UI)

def history(project_id: str) -> list:
    return query("SELECT * FROM design_approvals WHERE project_id = ? "
                 "ORDER BY created_at", (project_id,))


def state(project_id: str) -> dict | None:
    run = get_run(project_id)
    if not run:
        return None
    hist = history(project_id)
    by_seq: dict[int, list] = {}
    for h in hist:
        by_seq.setdefault(h["seq"], []).append(h)
    steps = []
    for s in run["steps"]:
        latest = (by_seq.get(s["seq"]) or [{}])[-1]
        if s["seq"] < run["current_seq"] or run["status"] == COMPLETED:
            step_state = "approved"
        elif s["seq"] == run["current_seq"]:
            step_state = {RUNNING: "in_progress", AWAITING: "awaiting_approval"}
            step_state = step_state.get(run["status"], run["status"])
            if run["status"] == RUNNING and latest.get("decision") == "reject":
                step_state = "in_revision"
        else:
            step_state = "pending"
        steps.append({**s, "state": step_state,
                      "docs": [PROCESS_CATALOG[p]["kind"] for p in s["processes"]]})
    return {"project_id": project_id, "flow_id": run["flow_id"],
            "status": run["status"], "current_seq": run["current_seq"],
            "steps": steps, "history": hist,
            "started_at": run["started_at"], "completed_at": run["completed_at"]}


def brief(project_id: str) -> str:
    """Lines for the copilot context brief so routing knows the design state."""
    st = state(project_id)
    if not st:
        return ""
    done = sum(1 for s in st["steps"] if s["state"] == "approved")
    cur = next((s for s in st["steps"] if s["seq"] == st["current_seq"]), None)
    if st["status"] == COMPLETED:
        return (f"Design phase: complete ({done}/{len(st['steps'])} steps) — the blueprint "
                "and sprints follow; `start sprint` runs them.")
    awaiting = cur and cur["state"] == "awaiting_approval"
    line = (f"Design phase: step {st['current_seq']} of {len(st['steps'])} "
            f"({cur['role'] if cur else '?'}) — "
            + ("awaiting approval; `approve design` to continue or "
               "`reject design: <notes>` to send back" if awaiting else
               "documents being authored"))
    return line + " ." if not awaiting else line + "."
