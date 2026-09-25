"""Migration 017 — per-project Product Owner override.

The project's Product Owner already defaults to the aligned team's PO agent
(``po_agent_for_project`` resolves it through ``projects.team_id``). This adds a
nullable ``po_agent_id`` so a single project can point at one specific agent
instead. NULL keeps the team default. Intentionally NOT a foreign key: a
deleted agent must not block deletion nor the project — resolution falls back
to the team PO when the id is missing or no longer on the team.
"""


def apply(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)")]
    if "po_agent_id" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN po_agent_id TEXT")
