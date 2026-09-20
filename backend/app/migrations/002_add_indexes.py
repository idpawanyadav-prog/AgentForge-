"""Migration 002 — add performance indexes and schema enhancements.

Adds:
  - gateway_models cost columns (input_cost_per_m, output_cost_per_m)
  - Performance indexes on frequently-queried columns
"""
from app.db import get_db


MIGRATION_SQL = """
-- Cost rates for gateway models (configurable per-model pricing)
ALTER TABLE gateway_models ADD COLUMN input_cost_per_m REAL NOT NULL DEFAULT 0;
ALTER TABLE gateway_models ADD COLUMN output_cost_per_m REAL NOT NULL DEFAULT 0;

-- Performance indexes (issue 11.2)
CREATE INDEX IF NOT EXISTS idx_tasks_project_status
  ON tasks(project_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_project_sprint
  ON tasks(project_id, sprint_id);
CREATE INDEX IF NOT EXISTS idx_events_project_seq
  ON execution_events(project_id, seq);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_project
  ON workflow_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_sprints_project
  ON sprints(project_id);
"""


def apply(conn):
    """Add indexes and cost columns."""
    conn.executescript(MIGRATION_SQL)
    conn.commit()
