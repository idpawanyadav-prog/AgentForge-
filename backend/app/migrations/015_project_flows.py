"""Migration 015 — Project Flow templates.

A flow is a reusable SDLC template: an ordered stage chain (dev / sa / ba /
qa / approve), the team composition that executes it, and project-level
toggles (docs gate, PO autonomy). Projects pick one flow at creation; the
runtime walks the flow's stage order instead of the old hardcoded
dev -> SA -> BA -> QA chain.
"""
import json
import time


def apply(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_flows (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL UNIQUE,
          description TEXT NOT NULL DEFAULT '',
          stages_json TEXT NOT NULL DEFAULT '["dev","sa","ba","qa"]',
          team_json TEXT NOT NULL DEFAULT '[]',
          docs_gate INTEGER NOT NULL DEFAULT 0,
          po_enabled INTEGER NOT NULL DEFAULT 0,
          is_default INTEGER NOT NULL DEFAULT 0,
          is_builtin INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
    """)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)")]
    if "flow_id" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN flow_id TEXT "
                     "REFERENCES project_flows(id)")

    ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    exists = conn.execute("SELECT id FROM project_flows WHERE is_builtin = 1").fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO project_flows (id, name, description, stages_json, team_json, "
            "docs_gate, po_enabled, is_default, is_builtin, created_at, updated_at) "
            "VALUES ('flow-standard-delivery', 'Standard Delivery', "
            "'BA/SA author BAS, PDS and TS, one approval gate, then blueprint and "
            "sprints; every task runs dev -> SA review -> BA review -> QA.', "
            "?, ?, 1, 0, 1, 1, ?, ?)",
            (json.dumps(["dev", "sa", "ba", "qa"]),
             json.dumps([
                 {"role": "Senior Developer", "count": 2},
                 {"role": "Solution Architect", "count": 1},
                 {"role": "Business Analyst", "count": 1},
                 {"role": "QA Engineer", "count": 1},
             ]), ts, ts))
