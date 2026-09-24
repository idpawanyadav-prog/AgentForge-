"""Project memory: the pitfall ledger.

Agents record WHY their work got rejected (QA / SA / BA findings) here, and
the code generator reads the ledger back into every future dev prompt, so
the same mistake stops recurring across tasks and sprints. Before this,
project_memory (migration 009) was dead schema — nothing was ever written
or read, and each task started with zero institutional knowledge.
"""
from __future__ import annotations

from .db import execute, insert, new_id, now, query, query_one


def record_pitfall(project_id: str, source_id: str, kind: str, content: str,
                   subject: str = "", owner_agent_id: str | None = None) -> str | None:
    """Record one active pitfall for a rejected task. Repeated rejections of
    the same task+kind reinforce the existing entry's importance instead of
    flooding the ledger."""
    content = (content or "").strip()
    if not (project_id and source_id and content):
        return None
    dup = query_one(
        "SELECT id FROM project_memory WHERE project_id=? AND memory_type='pitfall' "
        "AND source_type=? AND source_id=? AND status='active' "
        "ORDER BY created_at DESC LIMIT 1", (project_id, kind, source_id))
    if dup:
        execute("UPDATE project_memory SET importance = MIN(5, importance + 1), "
                "updated_at=? WHERE id=?", (now(), dup["id"]))
        return dup["id"]
    mid = new_id()
    insert("project_memory", {
        "id": mid, "project_id": project_id, "memory_type": "pitfall",
        "subject": (subject or kind)[:200], "content": content[:600],
        "source_type": kind, "source_id": source_id,
        "owner_agent_id": owner_agent_id, "importance": 2, "status": "active",
        "created_at": now(), "updated_at": now()})
    return mid


def recent_pitfalls(project_id: str, limit: int = 6) -> list[dict]:
    return query(
        "SELECT subject, content, source_type, importance FROM project_memory "
        "WHERE project_id=? AND memory_type='pitfall' AND status='active' "
        "ORDER BY importance DESC, created_at DESC LIMIT ?", (project_id, limit))


def pitfalls_block(project_id: str) -> str:
    """Ready-to-append prompt fragment, or '' when the ledger is empty."""
    rows = recent_pitfalls(project_id)
    if not rows:
        return ""
    lines = [f"- [{r['source_type']}] {(r['content'] or '')[:240]}" for r in rows]
    return ("\n\nLESSONS FROM PREVIOUSLY REJECTED WORK in this project "
            "(do not repeat these mistakes):\n" + "\n".join(lines) + "\n")
