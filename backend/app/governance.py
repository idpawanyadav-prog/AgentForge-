"""V3 Step 1 — project lifecycle state machine + PO baseline approval gate.

Spec §4: the project walks Draft → Requirement Analysis → Architecture
Analysis → Pending PO Approval → Approved → Scaffolding → Ready for
Planning → Active Development → Final Validation → Completed, with the
exception states Change Requested / Blocked / Paused / Cancelled. Every
transition is validated, persisted and audited (mirrors sprint_gate's
optimistic-transition pattern).

Spec §10/§11: before planning/scaffolding the requirement baseline (RB-x.0)
is frozen from the live project data and must be approved; reject or
request-revision opens a change request and parks the project in
Change Requested. Baselines are never edited in place — a new version is
proposed and the old one keeps its decided status for the record.

Enforcement is opt-in per project (`projects.governance_enabled`, default
off, like the SA/BA review gates): off, the state is tracked and audited
but execution is untouched; on, sprint start and task claims require a
development-permitting state.
"""
from __future__ import annotations

import json
import sqlite3
import threading

from .db import (audit, emit_event, execute, insert, new_id, now, query,
                 query_one, update)

# ---------------------------------------------------------------- states

DRAFT = "Draft"
REQ_ANALYSIS = "Requirement Analysis"
ARCH_ANALYSIS = "Architecture Analysis"
PENDING_PO = "Pending PO Approval"
APPROVED = "Approved"
SCAFFOLDING = "Scaffolding"
READY_PLANNING = "Ready for Planning"
ACTIVE_DEV = "Active Development"
FINAL_VALIDATION = "Final Validation"
COMPLETED = "Completed"
CHANGE_REQUESTED = "Change Requested"
BLOCKED = "Blocked"
PAUSED = "Paused"
CANCELLED = "Cancelled"

STATES = (DRAFT, REQ_ANALYSIS, ARCH_ANALYSIS, PENDING_PO, APPROVED, SCAFFOLDING,
          READY_PLANNING, ACTIVE_DEV, FINAL_VALIDATION, COMPLETED,
          CHANGE_REQUESTED, BLOCKED, PAUSED, CANCELLED)

# Legacy/migration default — pre-V3 projects were simply "Active".
LEGACY_ALIASES = {"": ACTIVE_DEV, "Active": ACTIVE_DEV}

# With governance on, only these states may run sprints/tasks.
DEV_PERMITTED = frozenset({ACTIVE_DEV, FINAL_VALIDATION})

_recoverable = {DRAFT, REQ_ANALYSIS, ARCH_ANALYSIS, PENDING_PO, APPROVED,
                SCAFFOLDING, READY_PLANNING, ACTIVE_DEV, FINAL_VALIDATION,
                CHANGE_REQUESTED}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    DRAFT: {REQ_ANALYSIS, PENDING_PO, CANCELLED},
    REQ_ANALYSIS: {ARCH_ANALYSIS, PENDING_PO, BLOCKED, PAUSED, CANCELLED},
    ARCH_ANALYSIS: {PENDING_PO, BLOCKED, PAUSED, CANCELLED},
    PENDING_PO: {APPROVED, CHANGE_REQUESTED, BLOCKED, CANCELLED},
    # Re-proposing a newer baseline version (RB-1.0 -> RB-2.0) re-enters the
    # approval gate from Approved — baselines are never edited in place.
    APPROVED: {PENDING_PO, SCAFFOLDING, READY_PLANNING, CHANGE_REQUESTED, BLOCKED,
               CANCELLED},
    SCAFFOLDING: {READY_PLANNING, BLOCKED, PAUSED, CANCELLED},
    READY_PLANNING: {ACTIVE_DEV, BLOCKED, PAUSED, CANCELLED},
    ACTIVE_DEV: {FINAL_VALIDATION, CHANGE_REQUESTED, BLOCKED, PAUSED, CANCELLED},
    FINAL_VALIDATION: {COMPLETED, ACTIVE_DEV, CHANGE_REQUESTED, BLOCKED},
    COMPLETED: set(),
    CHANGE_REQUESTED: {REQ_ANALYSIS, ARCH_ANALYSIS, PENDING_PO, BLOCKED, CANCELLED},
    # From an exception state the project recovers into any live (non-terminal) one.
    BLOCKED: _recoverable | {PAUSED, CANCELLED},
    PAUSED: _recoverable | {CANCELLED},
    CANCELLED: set(),
}


def normalize_state(raw: str | None) -> str:
    return LEGACY_ALIASES.get(raw or "", raw if raw in STATES else DRAFT)


def current_state(project_id: str) -> str:
    row = query_one("SELECT lifecycle_state FROM projects WHERE id = ?", (project_id,))
    return normalize_state(row["lifecycle_state"] if row else "")


def governance_enabled(project_id: str) -> bool:
    row = query_one("SELECT governance_enabled FROM projects WHERE id = ?", (project_id,))
    return bool(row and row["governance_enabled"])


def may_execute(project_id: str) -> tuple[bool, str]:
    """Gate consulted by sprint start / task claim. Always True when
    governance is off — the state machine then stays purely observational."""
    if not governance_enabled(project_id):
        return True, ""
    state = current_state(project_id)
    if state in DEV_PERMITTED:
        return True, ""
    return False, (f"Project lifecycle state is '{state}' — development may run "
                   f"in {sorted(DEV_PERMITTED)} only (governance is enabled).")


def transition_project(project_id: str, to_state: str, reason: str = "",
                       actor: str = "user") -> tuple[bool, str]:
    """Validated, optimistic project lifecycle transition (spec §4:
    persisted AND audited)."""
    project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        return False, "Project not found."
    raw_from = project["lifecycle_state"] or ""
    cur = normalize_state(raw_from)
    if to_state not in STATES:
        return False, f"Unknown lifecycle state: {to_state}"
    if cur == to_state:
        return True, ""
    # A legacy row ('Active') still obeys Active Development's edges.
    if to_state not in ALLOWED_TRANSITIONS.get(cur, set()):
        return False, f"Illegal project transition {cur} -> {to_state}"
    done = execute("UPDATE projects SET lifecycle_state = ?, updated_at = ? "
                   "WHERE id = ? AND IFNULL(lifecycle_state, '') = ?",
                  (to_state, now(), project_id, raw_from))
    if done.rowcount != 1:
        return False, f"Concurrent transition detected (project left {cur})"
    emit_event(project_id, "project.lifecycle_changed",
               {"from": cur, "to": to_state, "reason": reason})
    audit("project_lifecycle", "project", project_id,
          f"Project '{project['name']}': {cur} -> {to_state}"
          + (f" ({reason})" if reason else ""), actor=actor)
    return True, ""


# ---------------------------------------------------------------- requirements (AF3-001)

def create_requirement(project_id: str, title: str, raw_content: str,
                       source_type: str = "text", source_path: str = "",
                       actor: str = "user") -> dict:
    """Append-only requirement intake: re-submitting the same title never
    overwrites the original — it supersedes it with a new version."""
    ts = now()
    row = query_one("SELECT MAX(version) AS v FROM project_requirements "
                    "WHERE project_id = ? AND title = ?", (project_id, title))
    version = (row["v"] or 0) + 1
    if version > 1:
        execute("UPDATE project_requirements SET status = 'superseded', updated_at = ? "
                "WHERE project_id = ? AND title = ? AND status = 'active'",
                (ts, project_id, title))
    rid = new_id()
    insert("project_requirements", {
        "id": rid, "project_id": project_id, "source_type": source_type,
        "source_path": source_path, "title": title[:200],
        "raw_content": raw_content[:20000], "version": version,
        "status": "active", "created_at": ts, "updated_at": ts})
    emit_event(project_id, "requirement.created",
               {"title": title, "version": version, "requirement_id": rid})
    audit("requirement_created", "project", project_id,
          f"Requirement '{title}' v{version} recorded", actor=actor)
    return query_one("SELECT * FROM project_requirements WHERE id = ?", (rid,))


# ---------------------------------------------------------------- baselines (AF3-005)

KIND_PREFIX = {"requirement": "RB", "architecture": "AB"}


def active_requirements(project_id: str) -> list:
    return query("SELECT * FROM project_requirements WHERE project_id = ? "
                 "AND status = 'active' ORDER BY title, version DESC", (project_id,))


def latest_baseline(project_id: str, kind: str = "requirement"):
    return query_one("SELECT * FROM project_baselines WHERE project_id = ? AND kind = ? "
                     "ORDER BY created_at DESC LIMIT 1", (project_id, kind))


def _baseline_snapshot(project: dict, kind: str) -> dict:
    backlog = query("SELECT title, acceptance_criteria, story_points, priority "
                    "FROM backlog_items WHERE project_id = ? ORDER BY priority, created_at",
                    (project["id"],))
    snap = {
        "goal": project["goal"] or "",
        "description": project["description"] or "",
        "technology_stack": project["technology_stack"] or "",
        "requirements": [{"title": r["title"], "version": r["version"],
                          "source_type": r["source_type"]}
                         for r in active_requirements(project["id"])],
        "backlog": [{"title": b["title"], "acceptance_criteria": b["acceptance_criteria"],
                     "story_points": b["story_points"], "priority": b["priority"]}
                    for b in backlog],
    }
    if kind == "architecture":
        snap["stack_only"] = project["technology_stack"] or ""
        snap["decisions"] = [
            {"code": a["code"], "title": a["title"], "status": a["status"]}
            for a in query("SELECT * FROM architecture_decisions WHERE project_id = ? "
                           "ORDER BY created_at", (project["id"],))]
    return snap


_baseline_proposal_lock = threading.Lock()


def propose_baseline(project_id: str, kind: str = "requirement",
                     actor: str = "user") -> dict:
    # Serializing the check, insert and lifecycle transition also avoids
    # cross-thread SQLite write contention in the single-process server.
    with _baseline_proposal_lock:
        return _propose_baseline_locked(project_id, kind, actor)


def _propose_baseline_locked(project_id: str, kind: str, actor: str) -> dict:
    """Freeze the current scope into RB-n.0 / AB-n.0 and put the project at
    the PO approval gate. A pending baseline of the same kind blocks
    duplicates — decide it first."""
    project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        return {"error": "Project not found"}
    if kind not in KIND_PREFIX:
        return {"error": f"Unknown baseline kind: {kind}"}
    if query_one("SELECT id FROM project_baselines WHERE project_id = ? AND kind = ? "
                 "AND status = 'pending_approval'", (project_id, kind)):
        return {"error": "A baseline is already pending approval — approve, reject "
                         "or request revision first."}
    # code numbering: RB-1.0, RB-2.0... by count of prior baselines of this kind
    count = query_one("SELECT COUNT(*) AS n FROM project_baselines "
                      "WHERE project_id = ? AND kind = ?", (project_id, kind))["n"]
    code = f"{KIND_PREFIX[kind]}-{count + 1}.0"
    ts = now()
    bid = new_id()
    try:
        insert("project_baselines", {
            "id": bid, "project_id": project_id, "kind": kind, "code": code,
            "content_json": json.dumps(_baseline_snapshot(project, kind)),
            "status": "pending_approval", "created_at": ts, "updated_at": ts})
    except sqlite3.IntegrityError:
        return {"error": "A baseline is already pending approval — approve, reject "
                         "or request revision first."}
    ok, err = transition_project(project_id, PENDING_PO,
                                 reason=f"{code} submitted for approval", actor=actor)
    emit_event(project_id, "baseline.pending_approval",
               {"baseline_id": bid, "code": code, "kind": kind})
    audit("baseline_proposed", "project", project_id,
          f"Baseline {code} ({kind}) proposed for approval", actor=actor)
    if not ok:
        return {"error": f"Baseline {code} recorded, but the project could not "
                         f"enter the approval gate: {err}", "baseline_id": bid}
    return {"baseline": query_one("SELECT * FROM project_baselines WHERE id = ?", (bid,))}


def decide_baseline(project_id: str, baseline_id: str, decision: str,
                    notes: str = "", actor: str = "user") -> dict:
    """Approve / reject / request revision on a pending baseline.
    Reject and revision both open a change request (spec §11) and park the
    project in Change Requested — the baseline itself is kept for the record."""
    if decision not in ("approve", "reject", "request_revision"):
        return {"error": "decision must be approve | reject | request_revision"}
    b = query_one("SELECT * FROM project_baselines WHERE id = ? AND project_id = ?",
                  (baseline_id, project_id))
    if not b:
        return {"error": "Baseline not found"}
    if b["status"] != "pending_approval":
        return {"error": f"Baseline {b['code']} is {b['status']}, not pending approval"}
    ts = now()
    if decision == "approve":
        execute("UPDATE project_baselines SET status = 'approved', approved_by = ?, "
                "approved_at = ?, updated_at = ? WHERE id = ?",
                (actor, ts, ts, baseline_id))
        ok, err = transition_project(project_id, APPROVED,
                                     reason=f"{b['code']} approved", actor=actor)
        emit_event(project_id, "baseline.approved",
                   {"baseline_id": baseline_id, "code": b["code"], "decided_by": actor})
        audit("baseline_approved", "project", project_id,
              f"Baseline {b['code']} approved by {actor}", actor=actor)
        return {"ok": ok, "error": err, "state": current_state(project_id)}
    new_status = "rejected" if decision == "reject" else "revision_requested"
    execute("UPDATE project_baselines SET status = ?, approved_by = ?, updated_at = ? "
            "WHERE id = ?", (new_status, actor, ts, baseline_id))
    cr = _open_change_request(
        project_id, title=f"Baseline {b['code']} "
                      + ("rejected" if decision == "reject" else "needs revision"),
        description=notes or "(no reason given)", origin="baseline-decision",
        impact_field="business_impact" if decision == "reject" else "technical_impact",
        actor=actor)
    ok, err = transition_project(project_id, CHANGE_REQUESTED,
                                 reason=f"{b['code']} {new_status.replace('_', ' ')}",
                                 actor=actor)
    emit_event(project_id, f"baseline.{new_status}",
               {"baseline_id": baseline_id, "code": b["code"], "decided_by": actor,
                "change_request": cr["code"]})
    audit("baseline_rejected" if decision == "reject" else "baseline_revision_requested",
          "project", project_id,
          f"Baseline {b['code']} {new_status.replace('_', ' ')} by {actor}: "
          f"{(notes or '')[:200]}", actor=actor)
    return {"ok": ok, "error": err, "change_request": cr,
            "state": current_state(project_id)}


# ---------------------------------------------------------------- change requests (AF3-007)

def _open_change_request(project_id: str, title: str, description: str, origin: str,
                         impact_field: str = "business_impact",
                         origin_task_id: str | None = None,
                         actor: str = "user") -> dict:
    count = query_one("SELECT COUNT(*) AS n FROM change_requests WHERE project_id = ?",
                      (project_id,))["n"]
    ts = now()
    cid = new_id()
    row = {"id": cid, "project_id": project_id, "code": f"CR-{count + 1}",
           "title": title[:200], "description": description[:2000], "origin": origin,
           "origin_task_id": origin_task_id, "business_impact": "",
           "technical_impact": "", "status": "open", "decided_by": actor,
           "created_at": ts, "updated_at": ts}
    row[impact_field] = description[:2000]
    insert("change_requests", row)
    return row


def open_agent_change_request(project_id: str, task_id: str | None, title: str,
                              description: str, actor: str) -> dict:
    """An agent-side scope change request (dev/PO flags material change)."""
    cr = _open_change_request(project_id, title, description,
                              origin="agent", impact_field="technical_impact",
                              origin_task_id=task_id, actor=actor)
    emit_event(project_id, "change_request.opened",
               {"code": cr["code"], "title": title, "task_id": task_id})
    return cr


def list_change_requests(project_id: str, status: str | None = None) -> list:
    if status:
        return query("SELECT * FROM change_requests WHERE project_id = ? AND status = ? "
                     "ORDER BY created_at", (project_id, status))
    return query("SELECT * FROM change_requests WHERE project_id = ? ORDER BY created_at",
                 (project_id,))


def decide_change_request(project_id: str, cr_id: str, decision: str,
                          notes: str = "", actor: str = "user") -> dict:
    """incorporate -> back to Requirement Analysis (a new baseline version
    will follow); decline -> closed, project returns to the gate it was at
    only via an explicit transition (kept simple: state untouched)."""
    if decision not in ("incorporate", "decline"):
        return {"error": "decision must be incorporate | decline"}
    cr = query_one("SELECT * FROM change_requests WHERE id = ? AND project_id = ?",
                   (cr_id, project_id))
    if not cr:
        return {"error": "Change request not found"}
    if cr["status"] != "open":
        return {"error": f"Change request {cr['code']} is {cr['status']}, not open"}
    ts = now()
    execute("UPDATE change_requests SET status = ?, technical_impact = "
            "CASE WHEN technical_impact = '' THEN ? ELSE technical_impact || ' | ' || ? END, "
            "decided_by = ?, updated_at = ? WHERE id = ?",
            (decision, notes[:500], notes[:500], actor, ts, cr_id))
    emit_event(project_id, f"change_request.{decision}",
               {"code": cr["code"], "title": cr["title"], "decided_by": actor})
    audit("change_request_" + decision, "project", project_id,
          f"Change request {cr['code']} {decision}d by {actor}: {(notes or '')[:160]}",
          actor=actor)
    if decision == "incorporate" and current_state(project_id) == CHANGE_REQUESTED:
        transition_project(project_id, REQ_ANALYSIS,
                           reason=f"{cr['code']} incorporated", actor=actor)
    return {"ok": True, "state": current_state(project_id)}


# ---------------------------------------------------------------- view for UI/API

def lifecycle_view(project_id: str) -> dict:
    project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        return {"error": "Project not found"}
    state = normalize_state(project["lifecycle_state"])
    pending = query("SELECT id, kind, code, created_at FROM project_baselines "
                    "WHERE project_id = ? AND status = 'pending_approval' "
                    "ORDER BY created_at", (project_id,))
    return {
        "state": state,
        "governance_enabled": bool(project["governance_enabled"]),
        "allowed_transitions": sorted(ALLOWED_TRANSITIONS.get(state, set())),
        "dev_permitted": state in DEV_PERMITTED or not project["governance_enabled"],
        "requirements": active_requirements(project_id),
        "baselines": query("SELECT id, kind, code, status, approved_by, approved_at, "
                           "created_at FROM project_baselines WHERE project_id = ? "
                           "ORDER BY created_at DESC LIMIT 20", (project_id,)),
        "pending_baselines": pending,
        "change_requests": [c for c in list_change_requests(project_id)],
    }
