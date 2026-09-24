"""Migration 016 — Design Phase.

A flow's Design Phase is a user-configurable, strictly-sequential workflow
that runs after the Initial Requirement and before the per-task Stage Chain.
Each step names a responsible role, the catalog processes it performs (each
mapped to a document kind), and whether an approval gate pauses the run.

- ``project_flows.design_json`` holds the template steps.
- ``project_design_runs`` holds one project's live run (which step is next,
  its status), so the engine can resume and the UI can show progress.
- ``design_approvals`` is the append-only approval history (per step:
  decision, actor, timestamp, comments).
"""
import json
import time

# Steps that reproduce the old hardcoded docs-gate pipeline: the BA authors the
# requirement + product-design docs, the SA the technical spec, each behind an
# approval gate, before the SA blueprint + sprint breakdown run.
_STANDARD_DESIGN = [
    {"seq": 1, "role": "Business Analyst",
     "processes": ["Requirement Doc", "Project Definition Sheet"],
     "approval_required": True},
    {"seq": 2, "role": "Solution Architect",
     "processes": ["Technical Spec"],
     "approval_required": True},
]


def apply(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(project_flows)")]
    if "design_json" not in cols:
        conn.execute("ALTER TABLE project_flows ADD COLUMN design_json TEXT "
                     "NOT NULL DEFAULT '[]'")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_design_runs (
          project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
          flow_id TEXT REFERENCES project_flows(id),
          steps_json TEXT NOT NULL DEFAULT '[]',
          current_seq INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'in_progress',
          started_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS design_approvals (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          seq INTEGER NOT NULL,
          role TEXT NOT NULL DEFAULT '',
          decision TEXT NOT NULL,
          actor TEXT NOT NULL DEFAULT '',
          actor_type TEXT NOT NULL DEFAULT 'user',
          comments TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_design_approvals_project "
                 "ON design_approvals(project_id, seq)")

    # Seed the built-in flow's design template once, if it is still empty.
    row = conn.execute("SELECT design_json FROM project_flows "
                       "WHERE is_builtin = 1 ORDER BY created_at LIMIT 1").fetchone()
    if row is not None and (not row[0] or row[0] == "[]"):
        conn.execute("UPDATE project_flows SET design_json = ? WHERE is_builtin = 1",
                     (json.dumps(_STANDARD_DESIGN),))
