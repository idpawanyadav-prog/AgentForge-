"""Migration 014 — V3 Step 2 spec pipeline: project documents.

project_documents stores the BAS / PDS / TS specs (and later the BLUEPRINT
and SPRINT-PLAN) authored by the BA/SA agents during the project-initiation
pipeline. Documents are versioned append-only: regenerating a kind
supersedes the previous row instead of editing it in place, mirroring
project_requirements / project_baselines.
"""


def apply(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_documents (
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id),
          kind TEXT NOT NULL,
          title TEXT NOT NULL,
          content_md TEXT NOT NULL DEFAULT '',
          version INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'draft',
          review_notes TEXT NOT NULL DEFAULT '',
          approved_by TEXT NOT NULL DEFAULT '',
          approved_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_documents "
        "ON project_documents(project_id, kind, version)")
