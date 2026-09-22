"""Sprint state-machine transitions.

Concentrates the ALLOWED_TRANSITIONS map and the ``transition_sprint``
function so that both the REST routers and the chatbot share identical
sprint state rules.
"""
from __future__ import annotations

from .. import db
from ..db import audit, execute, query_one

# Sprint status constants (mirrors sprint_gate.py — import from there
# at call sites to avoid circular imports).

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "Planned": {"Ready", "Active", "Cancelled"},
    "Ready": {"Active", "Cancelled"},
    "Active": {"Development Complete", "Rework", "Cancelled"},
    "Development Complete": {"Build Validation", "Active", "Cancelled"},
    "Build Validation": {"Automated Testing", "Rework", "Active", "Cancelled"},
    "Automated Testing": {"Functional Validation", "Rework", "Active", "Cancelled"},
    "Functional Validation": {"Sprint Acceptance", "Rework", "Active", "Cancelled"},
    "Sprint Acceptance": {"Completed", "Rework", "Active", "Cancelled"},
    "Rework": {"Active", "Failed", "Cancelled"},
    "Completed": set(),
    "Failed": {"Active", "Cancelled"},
    "Cancelled": {"Planned"},
}


def is_allowed_transition(current: str, new_status: str) -> bool:
    return new_status in ALLOWED_TRANSITIONS.get(current, set())


def transition_sprint(
    sprint_id: str,
    new_status: str,
    reason: str = "",
    executed_by: str = "sprint-engine",
) -> tuple[bool, str]:
    """Validated, optimistic sprint status transition.

    Returns ``(ok, error_message)``.
    """
    sprint = query_one("SELECT * FROM sprints WHERE id = ?", (sprint_id,))
    if not sprint:
        return False, "Sprint not found."
    if sprint["status"] == new_status:
        return True, ""
    if not is_allowed_transition(sprint["status"], new_status):
        return False, (
            f"Illegal sprint transition {sprint['status']} -> {new_status}"
        )
    cur = execute(
        "UPDATE sprints SET status = ? WHERE id = ? AND status = ?",
        (new_status, sprint_id, sprint["status"]),
    )
    if cur.rowcount != 1:
        return False, (
            f"Concurrent transition detected (sprint left {sprint['status']})"
        )
    db.emit_event(
        sprint["project_id"],
        "sprint.status.changed",
        {"sprint": sprint["name"], "status": new_status, "reason": reason},
    )
    audit(
        "sprint_transition",
        "sprint",
        sprint_id,
        f"Sprint '{sprint['name']}': {sprint['status']} -> {new_status}"
        + (f" ({reason})" if reason else ""),
        actor=executed_by,
    )
    return True, ""
