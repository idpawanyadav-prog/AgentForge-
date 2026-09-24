"""Migration 010 — V3 Step 1: project governance opt-in flag.

lifecycle_state already exists from migration 009 (default 'Active', the
legacy value that maps to 'Active Development'). governance_enabled turns
the lifecycle + baseline approval gate on per project; off (default) the
state machine is still tracked and audited but never blocks execution.
"""


def apply(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
    if "governance_enabled" not in cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN governance_enabled "
            "INTEGER NOT NULL DEFAULT 0")
    conn.commit()
