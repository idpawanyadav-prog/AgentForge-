"""Migration 006 — per-agent ordered model fallback chain.

An agent can now list several Models-catalog bindings in priority order: if the
first model is down the agent falls back to the next. The chain lives in
``agents.model_binding_ids`` as a JSON array of ``model_bindings.id`` values.

``agents.model_binding_id`` (the single primary binding) is kept and mirrored to
the first chain entry so every existing consumer — cost resolution, the
Models-tab delete guard and the agent/task list joins — keeps working unchanged.

Idempotent: the ALTER only runs when the column is missing, and the backfill is
a no-op once each agent's chain is populated.
"""
import json


def apply(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall()]
    if "model_binding_ids" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN model_binding_ids TEXT NOT NULL DEFAULT '[]'")

    # Backfill: agents that already had a primary binding get a one-element
    # chain; the rest keep the default '[]'. Only touch rows that are still
    # empty so re-running never clobbers a curated chain.
    rows = conn.execute(
        "SELECT id, model_binding_id, model_binding_ids FROM agents"
    ).fetchall()
    for r in rows:
        try:
            existing = json.loads(r["model_binding_ids"] or "[]")
        except (ValueError, TypeError):
            existing = []
        if existing:
            continue
        primary = r["model_binding_id"]
        if primary:
            conn.execute(
                "UPDATE agents SET model_binding_ids = ? WHERE id = ?",
                (json.dumps([primary]), r["id"]),
            )
    conn.commit()
