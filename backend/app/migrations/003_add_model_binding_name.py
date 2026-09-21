"""Migration 003 — add a friendly name to model bindings.

Model bindings become the user-facing "Models" catalog (role-scoped
gateway + model presets that agents reference). Adds:
  - model_bindings.name (nullable; the display name shown in the Models tab)
  - backfills a name for every pre-existing binding so current agents keep
    working and show up in the catalog without repointing.
"""


def apply(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(model_bindings)").fetchall()}
    if "name" not in cols:
        conn.execute("ALTER TABLE model_bindings ADD COLUMN name TEXT")
    # Auto-name existing bindings in place: "<gateway> · <provider model>".
    # Falls back to a stable short id when the gateway/model rows are gone.
    conn.execute(
        """
        UPDATE model_bindings
        SET name = COALESCE(
              (SELECT g.name || ' \u00b7 ' || gm.provider_model_id
                 FROM gateways g, gateway_models gm
                WHERE g.id = model_bindings.gateway_id
                  AND gm.id = model_bindings.model_id),
              'Model ' || substr(model_bindings.id, 1, 6))
        WHERE name IS NULL OR name = ''
        """
    )
    conn.commit()
