"""Migration 001 — initial schema.

Creates every table defined in the original SCHEMA constant in db.py.
This is the baseline for all existing databases; on a fresh DB it is
equivalent to CREATE TABLE IF NOT EXISTS on every table.
"""
from app.db import get_db


MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gateways (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  provider TEXT NOT NULL,
  base_url TEXT NOT NULL,
  api_type TEXT NOT NULL DEFAULT 'openai-chat',
  status TEXT NOT NULL DEFAULT 'Active',
  key_mask TEXT,
  api_key_enc TEXT,
  last_tested_at TEXT,
  test_status TEXT,
  test_diagnostic TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gateway_models (
  id TEXT PRIMARY KEY,
  gateway_id TEXT NOT NULL REFERENCES gateways(id),
  provider_model_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  capabilities TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  UNIQUE(gateway_id, provider_model_id)
);

CREATE TABLE IF NOT EXISTS roles (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS personas (
  id TEXT PRIMARY KEY,
  role_id TEXT NOT NULL REFERENCES roles(id),
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  instructions TEXT NOT NULL DEFAULT '',
  constraints_text TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persona_versions (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL REFERENCES personas(id),
  version INTEGER NOT NULL,
  instructions TEXT NOT NULL,
  constraints_text TEXT NOT NULL,
  checksum TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skills (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persona_skills (
  persona_id TEXT NOT NULL REFERENCES personas(id),
  skill_id TEXT NOT NULL REFERENCES skills(id),
  PRIMARY KEY (persona_id, skill_id)
);

CREATE TABLE IF NOT EXISTS model_bindings (
  id TEXT PRIMARY KEY,
  role_id TEXT NOT NULL REFERENCES roles(id),
  gateway_id TEXT NOT NULL REFERENCES gateways(id),
  model_id TEXT NOT NULL REFERENCES gateway_models(id),
  settings_json TEXT NOT NULL DEFAULT '{}',
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  role_id TEXT NOT NULL REFERENCES roles(id),
  persona_id TEXT NOT NULL REFERENCES personas(id),
  model_binding_id TEXT REFERENCES model_bindings(id),
  lifecycle_state TEXT NOT NULL DEFAULT 'Idle',
  current_activity TEXT NOT NULL DEFAULT '',
  current_task_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'Active',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_agents (
  team_id TEXT NOT NULL REFERENCES teams(id),
  agent_id TEXT NOT NULL REFERENCES agents(id),
  role_in_team TEXT NOT NULL DEFAULT 'Member',
  active INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (team_id, agent_id)
);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  goal TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'Active',
  technology_stack TEXT NOT NULL DEFAULT '',
  repository_url TEXT NOT NULL DEFAULT '',
  workspace_path TEXT NOT NULL DEFAULT '',
  default_gateway_id TEXT REFERENCES gateways(id),
  default_model_id TEXT REFERENCES gateway_models(id),
  team_id TEXT REFERENCES teams(id),
  memory_policy TEXT NOT NULL DEFAULT 'project-scoped',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backlog_items (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  priority INTEGER NOT NULL DEFAULT 2,
  acceptance_criteria TEXT NOT NULL DEFAULT '',
  story_points INTEGER NOT NULL DEFAULT 3,
  status TEXT NOT NULL DEFAULT 'Backlog',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sprints (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  name TEXT NOT NULL,
  goal TEXT NOT NULL DEFAULT '',
  start_at TEXT,
  end_at TEXT,
  capacity INTEGER NOT NULL DEFAULT 40,
  status TEXT NOT NULL DEFAULT 'Planned',
  development_status TEXT NOT NULL DEFAULT 'Pending',
  build_status TEXT NOT NULL DEFAULT 'Pending',
  test_status TEXT NOT NULL DEFAULT 'Pending',
  functional_status TEXT NOT NULL DEFAULT 'Pending',
  acceptance_status TEXT NOT NULL DEFAULT 'Pending',
  failure_reason TEXT NOT NULL DEFAULT '',
  started_at TEXT,
  completed_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sprint_gate_executions (
  id TEXT PRIMARY KEY,
  sprint_id TEXT NOT NULL REFERENCES sprints(id),
  gate_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Pending',
  started_at TEXT,
  completed_at TEXT,
  duration_ms INTEGER,
  executed_by TEXT NOT NULL DEFAULT 'sprint-engine',
  error_message TEXT NOT NULL DEFAULT '',
  result_summary TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sprint_acceptance_criteria (
  id TEXT PRIMARY KEY,
  sprint_id TEXT NOT NULL REFERENCES sprints(id),
  code TEXT NOT NULL,
  description TEXT NOT NULL,
  required INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'Pending',
  result TEXT NOT NULL DEFAULT '',
  failure_reason TEXT NOT NULL DEFAULT '',
  validated_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  sprint_id TEXT REFERENCES sprints(id),
  backlog_item_id TEXT REFERENCES backlog_items(id),
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  acceptance_criteria TEXT NOT NULL DEFAULT '',
  story_points INTEGER NOT NULL DEFAULT 3,
  priority INTEGER NOT NULL DEFAULT 2,
  assigned_agent_id TEXT REFERENCES agents(id),
  qa_agent_id TEXT REFERENCES agents(id),
  rework_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'Todo',
  progress INTEGER NOT NULL DEFAULT 0,
  evidence TEXT NOT NULL DEFAULT '',
  blocked_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_dependencies (
  task_id TEXT NOT NULL REFERENCES tasks(id),
  depends_on_task_id TEXT NOT NULL REFERENCES tasks(id),
  dependency_type TEXT NOT NULL DEFAULT 'blocks',
  PRIMARY KEY (task_id, depends_on_task_id)
);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  title TEXT NOT NULL DEFAULT 'New conversation',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  workflow_run_id TEXT,
  agent_run_id TEXT,
  task_id TEXT,
  agent_id TEXT,
  event_type TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_project ON execution_events(project_id, seq);

CREATE TABLE IF NOT EXISTS workflow_runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  task_id TEXT REFERENCES tasks(id),
  agent_id TEXT REFERENCES agents(id),
  status TEXT NOT NULL DEFAULT 'Running',
  current_step TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  completed_at TEXT,
  idempotency_key TEXT
);

CREATE TABLE IF NOT EXISTS usage_records (
  id TEXT PRIMARY KEY,
  workflow_run_id TEXT NOT NULL,
  agent_id TEXT,
  model TEXT NOT NULL DEFAULT '',
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_estimate REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
  audit_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_type TEXT NOT NULL DEFAULT 'user',
  actor_id TEXT NOT NULL DEFAULT 'local-user',
  action TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id TEXT,
  summary TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instruction_files (
  id TEXT PRIMARY KEY,
  role_id TEXT NOT NULL REFERENCES roles(id),
  filename TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(role_id, filename)
);

CREATE TABLE IF NOT EXISTS role_skills (
  role_id TEXT NOT NULL REFERENCES roles(id),
  skill_id TEXT NOT NULL REFERENCES skills(id),
  PRIMARY KEY (role_id, skill_id)
);

CREATE TABLE IF NOT EXISTS pending_commands (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  command TEXT NOT NULL,
  args_json TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  idempotency_key TEXT PRIMARY KEY,
  endpoint TEXT NOT NULL,
  response_body TEXT NOT NULL,
  status_code INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
"""


def apply(conn):
    """Run the initial schema DDL."""
    conn.executescript(MIGRATION_SQL)
    conn.commit()
