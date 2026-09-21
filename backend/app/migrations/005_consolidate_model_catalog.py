"""Migration 005 — consolidate the global model catalog.

Models became a global catalog in 004, but bindings were historically seeded
one-per-role, so the same gateway+model pair shows up several times (e.g. one
"gpt-4.1-mini" per role). Collapse each (gateway_id, model_id) group to a
single canonical binding, repoint every agent that referenced a duplicate,
drop the duplicates, and clear the now-meaningless role_id.

Idempotent: once each pair has one row there are no groups left to merge, and
the role_id clear is a no-op on an already-global catalog.
"""


def apply(conn):
    groups = conn.execute(
        "SELECT gateway_id, model_id, MIN(rowid) AS keep_rowid "
        "FROM model_bindings GROUP BY gateway_id, model_id HAVING COUNT(*) > 1"
    ).fetchall()

    for g in groups:
        keep = conn.execute(
            "SELECT id FROM model_bindings WHERE rowid = ?", (g["keep_rowid"],)
        ).fetchone()
        if keep is None:
            continue
        keep_id = keep["id"]
        dups = conn.execute(
            "SELECT id FROM model_bindings "
            "WHERE gateway_id = ? AND model_id = ? AND id <> ?",
            (g["gateway_id"], g["model_id"], keep_id),
        ).fetchall()
        for d in dups:
            # Repoint agents first so the FK (NO ACTION) is satisfied on delete.
            conn.execute(
                "UPDATE agents SET model_binding_id = ? WHERE model_binding_id = ?",
                (keep_id, d["id"]),
            )
            conn.execute("DELETE FROM model_bindings WHERE id = ?", (d["id"],))

    conn.execute("UPDATE model_bindings SET role_id = NULL WHERE role_id IS NOT NULL")
    conn.commit()
