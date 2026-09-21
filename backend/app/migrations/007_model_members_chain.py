"""Migration 007 — the Model carries the fallback chain, not the agent.

A Models-catalog entry ("model") is now an ORDERED set of gateway+model members
stored in ``model_bindings.members_json`` as ``[{"gateway_id", "model_id"}, ...]``.
Inference walks that list and fails over, so an agent that picks this one model
transparently gets the resilience. ``gateway_id`` / ``model_id`` stay mirrored
to the first member for cost resolution, joins and the delete guard.

Idempotent: the ALTER only runs when the column is missing, and each row is
backfilled at most once (existing non-empty member lists are left untouched).
"""
import json


def apply(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(model_bindings)").fetchall()]
    if "members_json" not in cols:
        conn.execute(
            "ALTER TABLE model_bindings ADD COLUMN members_json TEXT NOT NULL DEFAULT '[]'"
        )

    rows = conn.execute(
        "SELECT id, gateway_id, model_id, members_json FROM model_bindings"
    ).fetchall()
    for r in rows:
        try:
            existing = json.loads(r["members_json"] or "[]")
        except (ValueError, TypeError):
            existing = []
        if existing:
            continue
        if r["gateway_id"] and r["model_id"]:
            conn.execute(
                "UPDATE model_bindings SET members_json = ? WHERE id = ?",
                (json.dumps([{"gateway_id": r["gateway_id"], "model_id": r["model_id"]}]),
                 r["id"]),
            )
    conn.commit()
