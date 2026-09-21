"""Migration 004 — make model_bindings.role_id optional.

Models become a global catalog (a named gateway + model preset) that any agent
can use, so a model no longer needs a role. SQLite cannot drop a NOT NULL
constraint in place, so the table is rebuilt. The rebuild preserves all rows
(including the ``name`` column added in 003) and keeps the
``agents.model_binding_id`` foreign key intact.

Per the SQLite ALTER TABLE procedure, foreign keys are disabled during the
swap and ``legacy_alter_table`` is enabled so renaming the old table does not
rewrite the FK reference inside ``agents``.
"""


_CREATE = """
CREATE TABLE model_bindings (
  id TEXT PRIMARY KEY,
  role_id TEXT REFERENCES roles(id),
  gateway_id TEXT NOT NULL REFERENCES gateways(id),
  model_id TEXT NOT NULL REFERENCES gateway_models(id),
  name TEXT,
  settings_json TEXT NOT NULL DEFAULT '{}',
  active INTEGER NOT NULL DEFAULT 1
)
"""


def apply(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(model_bindings)").fetchall()}
    if "name" not in cols:
        # 003 should have added it; be defensive for odd upgrade paths.
        conn.execute("ALTER TABLE model_bindings ADD COLUMN name TEXT")
    # role_id is already nullable -> nothing to do (idempotent re-run).
    not_null = {r[1]: r[3] for r in conn.execute("PRAGMA table_info(model_bindings)").fetchall()}
    if not_null.get("role_id", 0) == 0:
        return

    # PRAGMA foreign_keys is a no-op inside a transaction; use autocommit and
    # drive the transaction manually so the swap is atomic.
    prev_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE model_bindings RENAME TO _model_bindings_old")
        conn.execute(_CREATE)
        conn.execute(
            "INSERT INTO model_bindings (id, role_id, gateway_id, model_id, name, settings_json, active) "
            "SELECT id, role_id, gateway_id, model_id, name, settings_json, active FROM _model_bindings_old"
        )
        conn.execute("DROP TABLE _model_bindings_old")
        conn.execute("COMMIT")
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")
        raise
    finally:
        conn.isolation_level = prev_isolation
