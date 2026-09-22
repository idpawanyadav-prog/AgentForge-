"""Migration 009 — V3 governance, contracts and shared-memory schema.

Adds the durable artifacts of the autonomous-delivery architecture:
requirement intake, baselines, ADRs, machine-readable contracts,
component registry, dependency requests, review records, project
memory, checkpoints, agent collaboration messages, change requests
and the context cache.

New task statuses ('SA Review', 'BA Review') need no schema change —
tasks.status is unconstrained TEXT.
"""

TABLES = """
CREATE TABLE IF NOT EXISTS project_requirements (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  source_type TEXT NOT NULL DEFAULT 'text',
  source_path TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  raw_content TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_baselines (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  kind TEXT NOT NULL DEFAULT 'requirement',
  code TEXT NOT NULL,
  content_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending_approval',
  approved_by TEXT NOT NULL DEFAULT '',
  approved_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS architecture_decisions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  code TEXT NOT NULL,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'proposed',
  decision TEXT NOT NULL DEFAULT '',
  consequences TEXT NOT NULL DEFAULT '',
  superseded_by TEXT REFERENCES architecture_decisions(id),
  doc_path TEXT NOT NULL DEFAULT '',
  valid_from TEXT,
  valid_to TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interface_contracts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  name TEXT NOT NULL,
  module TEXT NOT NULL DEFAULT '',
  revision INTEGER NOT NULL DEFAULT 1,
  signature TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  superseded_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_contracts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  path TEXT NOT NULL,
  module TEXT NOT NULL DEFAULT '',
  owner_role TEXT NOT NULL DEFAULT '',
  purpose TEXT NOT NULL DEFAULT '',
  must_implement TEXT NOT NULL DEFAULT '[]',
  allowed_deps TEXT NOT NULL DEFAULT '[]',
  forbidden TEXT NOT NULL DEFAULT '[]',
  technical_ac TEXT NOT NULL DEFAULT '',
  requirement_ids TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_ownership (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  path_pattern TEXT NOT NULL,
  owner_role TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS component_registry (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  path TEXT NOT NULL DEFAULT '',
  owner_role TEXT NOT NULL DEFAULT '',
  purpose TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '1.0',
  used_by TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dependency_requests (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  code TEXT NOT NULL,
  package TEXT NOT NULL,
  version TEXT NOT NULL DEFAULT '',
  requested_by_agent_id TEXT REFERENCES agents(id),
  requested_for_task_id TEXT REFERENCES tasks(id),
  reason TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT 'project',
  approval TEXT NOT NULL DEFAULT 'pending',
  install_status TEXT NOT NULL DEFAULT 'requested',
  provenance TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_requirements (
  task_id TEXT NOT NULL REFERENCES tasks(id),
  requirement_id TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'bas',
  PRIMARY KEY (task_id, requirement_id)
);

CREATE TABLE IF NOT EXISTS task_reviews (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  project_id TEXT NOT NULL REFERENCES projects(id),
  reviewer_type TEXT NOT NULL,
  reviewer_agent_id TEXT REFERENCES agents(id),
  status TEXT NOT NULL DEFAULT 'pending',
  findings TEXT NOT NULL DEFAULT '',
  rework_class TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS project_memory (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  memory_type TEXT NOT NULL,
  subject TEXT NOT NULL,
  content TEXT NOT NULL,
  source_type TEXT NOT NULL DEFAULT '',
  source_id TEXT NOT NULL DEFAULT '',
  owner_agent_id TEXT REFERENCES agents(id),
  importance INTEGER NOT NULL DEFAULT 2,
  status TEXT NOT NULL DEFAULT 'active',
  superseded_by TEXT,
  valid_from TEXT,
  valid_to TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_checkpoints (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  scope TEXT NOT NULL,
  scope_id TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_messages (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  from_agent_id TEXT REFERENCES agents(id),
  to_agent_id TEXT REFERENCES agents(id),
  channel TEXT NOT NULL DEFAULT '',
  message_type TEXT NOT NULL,
  subject TEXT NOT NULL,
  content TEXT NOT NULL,
  related_task_id TEXT REFERENCES tasks(id),
  related_entity_type TEXT NOT NULL DEFAULT '',
  related_entity_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS change_requests (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  code TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL DEFAULT '',
  origin_task_id TEXT REFERENCES tasks(id),
  business_impact TEXT NOT NULL DEFAULT '',
  technical_impact TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  decided_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_cache (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  task_id TEXT REFERENCES tasks(id),
  context_type TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  token_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_requirements_project ON project_requirements(project_id, status);
CREATE INDEX IF NOT EXISTS idx_baselines_project ON project_baselines(project_id, status);
CREATE INDEX IF NOT EXISTS idx_adr_project ON architecture_decisions(project_id, status);
CREATE INDEX IF NOT EXISTS idx_iface_contracts_project ON interface_contracts(project_id, status);
CREATE INDEX IF NOT EXISTS idx_file_contracts_project ON file_contracts(project_id, status);
CREATE INDEX IF NOT EXISTS idx_file_contracts_path ON file_contracts(project_id, path);
CREATE INDEX IF NOT EXISTS idx_components_project ON component_registry(project_id, status);
CREATE INDEX IF NOT EXISTS idx_dep_requests_project ON dependency_requests(project_id, approval);
CREATE INDEX IF NOT EXISTS idx_task_reviews_task ON task_reviews(task_id, reviewer_type);
CREATE INDEX IF NOT EXISTS idx_memory_project_type ON project_memory(project_id, memory_type, status);
CREATE INDEX IF NOT EXISTS idx_memory_subject ON project_memory(project_id, memory_type, subject);
CREATE INDEX IF NOT EXISTS idx_checkpoints_project ON memory_checkpoints(project_id, scope, scope_id);
CREATE INDEX IF NOT EXISTS idx_agent_messages_project ON agent_messages(project_id, status);
CREATE INDEX IF NOT EXISTS idx_change_requests_project ON change_requests(project_id, status);
CREATE INDEX IF NOT EXISTS idx_context_cache_project ON context_cache(project_id, context_type);
"""


def _add_columns(conn, table, columns):
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, ddl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def apply(conn):
    conn.executescript(TABLES)
    conn.executescript(INDEXES)
    # Project-level review gates (default off: existing behavior unchanged).
    _add_columns(conn, "projects", [
        ("sa_review_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("ba_review_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("lifecycle_state", "TEXT NOT NULL DEFAULT 'Active'"),
    ])
    _add_columns(conn, "tasks", [
        ("functional_ac", "TEXT NOT NULL DEFAULT ''"),
        ("technical_ac", "TEXT NOT NULL DEFAULT ''"),
        ("allowed_files", "TEXT NOT NULL DEFAULT ''"),
        ("rework_class", "TEXT NOT NULL DEFAULT ''"),
    ])
    _add_columns(conn, "workflow_runs", [
        ("mode", "TEXT NOT NULL DEFAULT 'dev'"),
        ("context_hash", "TEXT NOT NULL DEFAULT ''"),
    ])
    conn.commit()
