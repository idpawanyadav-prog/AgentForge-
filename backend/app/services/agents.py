"""Agent assignment logic shared by runtime, routers, and chatbot.

Wraps the internal ``runtime._family_for_task``, ``runtime._family_of_role``,
``runtime._pick_member``, ``runtime._team_members``, and
``runtime.auto_assign_tasks`` helpers so callers don't depend on
dunder-prefixed runtime internals.
"""
from __future__ import annotations

from .. import runtime
from ..db import execute, now, query, query_one, update


def family_for_task(task: dict) -> str | None:
    """Return the role-family keyword matched by *task* (dev/qa/architecture/…)."""
    return runtime._family_for_task(task)


def family_of_role(role_name: str) -> str | None:
    return runtime._family_of_role(role_name)


def pick_member(members: list[dict], family: str | None):
    """Pick the best member for *family*. Returns ``(member, matched)``."""
    return runtime._pick_member(members, family)


def team_members(project_id: str):
    return runtime._team_members(project_id)


def agent_load(agent_id: str) -> int:
    return runtime._agent_load(agent_id)


def auto_assign_tasks(project_id: str) -> dict:
    """Assign unassigned sprint tasks to idle team members by role match."""
    return runtime.auto_assign_tasks(project_id)


def pick_qa_member(project_id: str):
    return runtime._pick_qa_member(project_id)
