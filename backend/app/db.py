"""SQLite persistence layer for Agent Office.

SQLite is used as the container-local durable store for the MVP build.
For production deployments the spec calls for PostgreSQL; the data-access
layer is intentionally simple SQL so it can be swapped later.
"""
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

DB_PATH = os.environ.get("AGENT_OFFICE_DB", os.path.join(os.path.dirname(__file__), "..", "agent_office.db"))

_local = threading.local()


def get_db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


def rows_to_dicts(cursor):
    return [dict(r) for r in cursor.fetchall()]


def query(sql: str, params=()):
    return rows_to_dicts(get_db().execute(sql, params))


def query_one(sql: str, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=()):
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur


def insert(table: str, values: dict) -> str:
    cols = ", ".join(values.keys())
    ph = ", ".join(["?"] * len(values))
    execute(f"INSERT INTO {table} ({cols}) VALUES ({ph})", tuple(values.values()))
    return values.get("id", "")


def update(table: str, entity_id: str, values: dict):
    sets = ", ".join(f"{k} = ?" for k in values)
    execute(f"UPDATE {table} SET {sets} WHERE id = ?", tuple(values.values()) + (entity_id,))


SCHEMA = """
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
"""


def audit(action: str, resource_type: str, resource_id=None, summary: str = "", actor="user"):
    insert("audit_events", {
        "actor_type": actor,
        "actor_id": "local-user",
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "summary": summary,
        "created_at": now(),
    })


def emit_event(project_id: str, event_type: str, payload: dict, *,
               workflow_run_id=None, agent_run_id=None, task_id=None, agent_id=None) -> int:
    return insert("execution_events", {
        "project_id": project_id,
        "workflow_run_id": workflow_run_id,
        "agent_run_id": agent_run_id,
        "task_id": task_id,
        "agent_id": agent_id,
        "event_type": event_type,
        "payload": json.dumps(payload),
        "created_at": now(),
    })


def checksum(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# Provider model catalogs used to auto-populate a gateway's model catalog.
# In production these are fetched live from each provider's list-models
# endpoint; the shape of the sync logic is identical.
PROVIDER_CATALOGS: dict[str, list[tuple[str, str, str]]] = {
    "openai": [
        ("gpt-4.1", "GPT-4.1", "reasoning,coding"),
        ("gpt-4.1-mini", "GPT-4.1 Mini", "fast,coding"),
        ("gpt-4.1-nano", "GPT-4.1 Nano", "fast"),
        ("gpt-4o", "GPT-4o", "reasoning,vision"),
        ("gpt-4o-mini", "GPT-4o Mini", "fast,vision"),
        ("o4-mini", "o4-mini", "reasoning,fast"),
        ("o3", "o3", "reasoning"),
        ("text-embedding-3-small", "Embedding 3 Small", "embedding"),
        ("text-embedding-3-large", "Embedding 3 Large", "embedding"),
        ("dall-e-3", "DALL-E 3", "image"),
        ("whisper-1", "Whisper", "audio"),
    ],
    "azure-openai": [
        ("gpt-4.1", "GPT-4.1", "reasoning,coding"),
        ("gpt-4.1-mini", "GPT-4.1 Mini", "fast,coding"),
        ("gpt-4o", "GPT-4o", "reasoning,vision"),
        ("gpt-4o-mini", "GPT-4o Mini", "fast,vision"),
        ("o4-mini", "o4-mini", "reasoning,fast"),
        ("text-embedding-3-small", "Embedding 3 Small", "embedding"),
    ],
    "anthropic": [
        ("claude-opus-4-5", "Claude Opus 4.5", "reasoning,coding"),
        ("claude-sonnet-4-5", "Claude Sonnet 4.5", "reasoning,coding,fast"),
        ("claude-haiku-4-5", "Claude Haiku 4.5", "fast"),
        ("claude-3-5-haiku-latest", "Claude 3.5 Haiku", "fast"),
    ],
    "ollama": [
        ("llama3.2", "Llama 3.2", "reasoning"),
        ("llama3.2:1b", "Llama 3.2 1B", "fast"),
        ("qwen2.5-coder", "Qwen2.5 Coder", "coding"),
        ("qwen2.5", "Qwen2.5", "reasoning"),
        ("mistral", "Mistral", "fast"),
        ("phi4", "Phi-4", "reasoning"),
        ("gemma2", "Gemma 2", "fast"),
        ("nomic-embed-text", "Nomic Embed", "embedding"),
    ],
    "custom": [
        ("default", "Default Model", ""),
        ("default-mini", "Default Mini", "fast"),
    ],
}


def sync_gateway_models(gateway_id: str) -> int:
    """Fetch the provider's available models into the gateway catalog.

    Idempotent: existing provider_model_ids are left untouched, new ones are
    registered. Returns the number of models added.
    """
    gw = query_one("SELECT provider FROM gateways WHERE id=?", (gateway_id,))
    if not gw:
        return 0
    catalog = PROVIDER_CATALOGS.get(gw["provider"], PROVIDER_CATALOGS["custom"])
    added = 0
    for provider_model_id, display_name, capabilities in catalog:
        exists = query_one(
            "SELECT id FROM gateway_models WHERE gateway_id=? AND provider_model_id=?",
            (gateway_id, provider_model_id))
        if not exists:
            insert("gateway_models", {
                "id": new_id(), "gateway_id": gateway_id,
                "provider_model_id": provider_model_id,
                "display_name": display_name, "capabilities": capabilities, "active": 1,
            })
            added += 1
    return added


SCENARIO = """
CREATE TABLE IF NOT EXISTS persona_version_seed_marker (id TEXT PRIMARY KEY);
"""


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()
    seed_if_empty()
    seed_instruction_files()


def seed_instruction_files():
    """Seed role-group instruction files and role-level skill attachments."""
    ts = now()
    if not query_one("SELECT id FROM instruction_files LIMIT 1"):
        files = {
            "Senior Developer": [
                ("coding-standards.md", "General coding standards and best practices",
                 "# Coding Standards for Senior Developers\n\n## General Principles\n- Write clean, maintainable, and well-documented code\n- Follow SOLID principles\n- Prefer simplicity over cleverness\n- Ensure test coverage for critical logic\n\n## Error Handling\n- Implement proper error handling\n- Use structured logging\n- Provide meaningful error messages\n\n## Performance\n- Optimize for readability first, then performance\n- Avoid premature optimization\n- Use profiling tools for bottlenecks"),
                ("code-review.md", "Code review guidelines and checklist",
                 "# Code Review Guidelines\n\n## Checklist\n- Correctness: does the change do what it claims?\n- Tests: are new paths covered by meaningful assertions?\n- Security: no secrets, no injection risks, no unsafe deserialization\n- Readability: clear names, small functions, helpful comments\n\n## Etiquette\n- Review within one business day\n- Comment on code, never on people"),
            ],
            "Solution Architect": [
                ("architecture-guidelines.md", "System design and architecture guidelines",
                 "# Architecture Guidelines\n\n## Principles\n- Prefer boring, proven technology\n- Document decisions and trade-offs (ADRs)\n- Design for the failure modes you actually expect\n- Version every external contract\n\n## Review Gates\n- No new dependency without a documented rationale"),
            ],
            "Business Analyst": [
                ("requirements-playbook.md", "How to write backlog items and acceptance criteria",
                 "# Requirements Playbook\n\n## Writing Stories\n- INVEST-check every story\n- Acceptance criteria in Given / When / Then form\n- Surface assumptions explicitly\n\n## Prioritization\n- P1: blocks release\n- P2: core value\n- P3: polish"),
            ],
            "QA Engineer": [
                ("test-charter.md", "Risk-based testing charter",
                 "# Test Charter\n\n## Approach\n- Test risk, not coverage vanity\n- Every acceptance criterion gets an explicit check\n- Report severity and reproduction steps\n\n## Gates\n- Never mark an item done with a failing criterion"),
            ],
            "DevOps Engineer": [
                ("release-checklist.md", "Deployment and release checklist",
                 "# Release Checklist\n\n## Before deploy\n- Pipeline green on main\n- Rollback plan documented\n- Budget/cost alerts armed\n\n## After deploy\n- Health checks verified\n- Error budget reviewed"),
            ],
        }
        for role in query("SELECT * FROM roles"):
            for fn, desc, content in files.get(role["name"], []):
                insert("instruction_files", {
                    "id": new_id(), "role_id": role["id"], "filename": fn,
                    "description": desc, "content": content, "version": 1,
                    "created_at": ts, "updated_at": ts,
                })
    if not query_one("SELECT role_id FROM role_skills LIMIT 1"):
        for r in query("SELECT DISTINCT p.role_id AS role_id, ps.skill_id AS skill_id "
                       "FROM persona_skills ps JOIN personas p ON p.id = ps.persona_id"):
            execute("INSERT OR IGNORE INTO role_skills (role_id, skill_id) VALUES (?,?)",
                    (r["role_id"], r["skill_id"]))


def seed_if_empty():
    if query_one("SELECT value FROM settings WHERE key = 'seeded'"):
        return
    # Clean any partial seed so the demo data set is complete and consistent.
    for table in ("settings", "pending_commands", "messages", "conversations", "execution_events",
                  "usage_records", "workflow_runs", "task_dependencies", "tasks", "sprints",
                  "backlog_items", "projects", "team_agents", "teams", "agents", "model_bindings",
                  "persona_skills", "skills", "persona_versions", "personas", "roles",
                  "gateway_models", "gateways", "audit_events", "instruction_files", "role_skills"):
        execute(f"DELETE FROM {table}")
    ts = now()

    # --- Gateway + model catalog (no real credentials stored) ---
    gw = new_id()
    insert("gateways", {
        "id": gw, "name": "Primary LLM Gateway", "provider": "openai",
        "base_url": "https://api.openai.com/v1", "api_type": "openai-chat",
        "status": "Active", "key_mask": "sk-••••7f2a",
        "last_tested_at": ts, "test_status": "Success",
        "test_diagnostic": "Connection OK, 6 models discovered",
        "created_at": ts, "updated_at": ts,
    })
    models = [
        ("gpt-4.1", "GPT-4.1", "reasoning,coding"),
        ("gpt-4.1-mini", "GPT-4.1 Mini", "fast,coding"),
        ("gpt-4o", "GPT-4o", "reasoning,vision"),
        ("o4-mini", "o4-mini", "reasoning,fast"),
        ("text-embedding-3-small", "Embedding 3 Small", "embedding"),
        ("dall-e-3", "DALL-E 3", "image"),
    ]
    model_ids = {}
    for mid, name, caps in models:
        m_id = new_id()
        model_ids[mid] = m_id
        insert("gateway_models", {
            "id": m_id, "gateway_id": gw, "provider_model_id": mid,
            "display_name": name, "capabilities": caps, "active": 1,
        })
    # Auto-fetch the remaining provider catalog so the demo starts complete.
    sync_gateway_models(gw)

    # --- Roles ---
    role_defs = [
        ("Business Analyst", "Elicits requirements, writes backlog items and acceptance criteria."),
        ("Solution Architect", "Designs system architecture, selects patterns and tech stack."),
        ("Senior Developer", "Implements features, writes production-quality code and tests."),
        ("QA Engineer", "Designs test plans, validates acceptance criteria, reports defects."),
        ("DevOps Engineer", "Manages build, deployment pipelines and infrastructure."),
    ]
    role_ids = {}
    for name, desc in role_defs:
        rid = new_id()
        role_ids[name] = rid
        insert("roles", {"id": rid, "name": name, "description": desc, "active": 1,
                         "created_at": ts, "updated_at": ts})

    # --- Skills ---
    skill_defs = [
        ("requirements-analysis", "Elicit and structure software requirements.",
         "# Requirements Analysis\n1. Interview stakeholders\n2. Draft user stories with INVEST check\n3. Define measurable acceptance criteria (Given/When/Then)"),
        ("api-design", "Design clean REST APIs.", "# API Design\n- Resource-oriented routes\n- Version from day one (/api/v1)\n- Typed request/response DTOs"),
        ("python-coding", "Write idiomatic, tested Python.", "# Python Coding\n- Type hints everywhere\n- Small pure functions\n- pytest with Arrange/Act/Assert"),
        ("unit-testing", "Write thorough unit tests.", "# Unit Testing\n- Cover happy path and edge cases\n- Deterministic fixtures\n- Aim for meaningful coverage, not vanity metrics"),
        ("code-review", "Review code for quality and safety.", "# Code Review\n- Correctness, readability, tests, security\n- Flag secrets, injection, unsafe deserialization"),
        ("containerization", "Docker and deployment.", "# Containerization\n- Multi-stage builds\n- Non-root user\n- Health checks"),
    ]
    skill_ids = {}
    for name, desc, content in skill_defs:
        sid = new_id()
        skill_ids[name] = sid
        insert("skills", {"id": sid, "name": name, "description": desc, "content": content,
                          "version": 1, "active": 1, "created_at": ts, "updated_at": ts})

    # --- Personas (one per role) + versions ---
    persona_defs = [
        ("Business Analyst", "Precision BA", "Turns vague ideas into testable backlog items.",
         "You are a meticulous Business Analyst. Clarify ambiguity before assuming. Write backlog items with measurable acceptance criteria.",
         "Never invent requirements the user did not state.", ["requirements-analysis"]),
        ("Solution Architect", "Pragmatic Architect", "Chooses boring technology that ships.",
         "You are a pragmatic Solution Architect. Prefer simple, proven patterns. Document decisions with trade-offs.",
         "Avoid over-engineering; no speculative generality.", ["api-design"]),
        ("Senior Developer", "Lead Coder", "Ships clean, tested code.",
         "You are a Senior Developer. Write small, typed, well-tested modules. Follow the project's existing conventions.",
         "No secrets in code. No untested public functions.", ["python-coding", "unit-testing"]),
        ("QA Engineer", "Quality Gatekeeper", "Breaks it before users do.",
         "You are a QA Engineer. Design risk-based test plans and verify each acceptance criterion explicitly.",
         "Never mark an item done with a failing criterion.", ["unit-testing", "code-review"]),
        ("DevOps Engineer", "Platform Operator", "Keeps pipelines green and costs visible.",
         "You are a DevOps Engineer. Automate builds and deployments; make failures observable.",
         "No production changes without approval gates.", ["containerization"]),
    ]
    persona_ids = {}
    for role_name, pname, pdesc, instr, cons, skills in persona_defs:
        pid = new_id()
        persona_ids[role_name] = pid
        insert("personas", {
            "id": pid, "role_id": role_ids[role_name], "name": pname, "description": pdesc,
            "instructions": instr, "constraints_text": cons, "version": 1, "active": 1,
            "created_at": ts, "updated_at": ts,
        })
        insert("persona_versions", {
            "id": new_id(), "persona_id": pid, "version": 1,
            "instructions": instr, "constraints_text": cons,
            "checksum": checksum(instr), "created_at": ts,
        })
        for s in skills:
            execute("INSERT INTO persona_skills (persona_id, skill_id) VALUES (?, ?)", (pid, skill_ids[s]))

    # --- Model bindings per role ---
    binding_map = {
        "Business Analyst": "gpt-4.1-mini",
        "Solution Architect": "gpt-4.1",
        "Senior Developer": "gpt-4.1",
        "QA Engineer": "gpt-4.1-mini",
        "DevOps Engineer": "gpt-4.1-mini",
    }
    binding_ids = {}
    for role_name, model_key in binding_map.items():
        bid = new_id()
        binding_ids[role_name] = bid
        insert("model_bindings", {
            "id": bid, "role_id": role_ids[role_name], "gateway_id": gw,
            "model_id": model_ids[model_key], "settings_json": json.dumps({"temperature": 0.3}),
            "active": 1,
        })

    # --- Agents ---
    agent_defs = [
        ("Ava", "Business Analyst"), ("Rex", "Solution Architect"),
        ("Nova", "Senior Developer"), ("Iris", "QA Engineer"), ("Bolt", "DevOps Engineer"),
    ]
    agent_ids = {}
    for aname, role_name in agent_defs:
        aid = new_id()
        agent_ids[role_name] = aid
        insert("agents", {
            "id": aid, "name": aname, "role_id": role_ids[role_name],
            "persona_id": persona_ids[role_name], "model_binding_id": binding_ids[role_name],
            "lifecycle_state": "Idle", "current_activity": "",
            "created_at": ts, "updated_at": ts,
        })

    # --- Team ---
    team_id = new_id()
    insert("teams", {"id": team_id, "name": "Core Delivery Team",
                     "description": "Full-stack delivery team for web projects.",
                     "status": "Active", "created_at": ts})
    for role_name, role_in_team in [("Business Analyst", "Requirements"),
                                    ("Solution Architect", "Design"),
                                    ("Senior Developer", "Implementation"),
                                    ("QA Engineer", "Quality"),
                                    ("DevOps Engineer", "Operations")]:
        execute("INSERT INTO team_agents (team_id, agent_id, role_in_team, active) VALUES (?,?,?,1)",
                (team_id, agent_ids[role_name], role_in_team))

    # --- Project ---
    project_id = new_id()
    insert("projects", {
        "id": project_id, "name": "Customer Portal", "goal": "Launch a self-service customer portal with auth, ticketing and reporting.",
        "description": "Flagship demo project: customer-facing web portal.",
        "status": "Active", "technology_stack": "React, FastAPI, PostgreSQL",
        "repository_url": "https://git.example.com/acme/customer-portal",
        "workspace_path": "/workspace/customer-portal",
        "default_gateway_id": gw, "team_id": team_id,
        "created_at": ts, "updated_at": ts,
    })

    # --- Backlog ---
    bl_defs = [
        ("User authentication", "Email/password login with session refresh.", 1, 5,
         "Given a registered user, when they log in with valid credentials, then they receive a session."),
        ("Ticket submission form", "Customers can submit support tickets with attachments.", 1, 5,
         "Given an authenticated customer, when they submit the form, then a ticket is created and confirmation shown."),
        ("Ticket status tracking", "Customers can view the status and history of their tickets.", 2, 3,
         "Given a ticket exists, when the customer opens it, then status timeline is visible."),
        ("Reporting dashboard", "Agents see KPIs: open tickets, resolution time, CSAT.", 3, 8,
         "Given resolved tickets exist, when the dashboard loads, then KPIs render within 2s."),
        ("Email notifications", "Customers get notified on ticket state changes.", 2, 3,
         "Given a state change, when it is committed, then an email is queued."),
    ]
    bl_ids = []
    for title, desc, prio, pts, ac in bl_defs:
        bid = new_id()
        bl_ids.append(bid)
        insert("backlog_items", {
            "id": bid, "project_id": project_id, "title": title, "description": desc,
            "priority": prio, "acceptance_criteria": ac, "story_points": pts,
            "status": "Backlog", "created_at": ts,
        })

    # --- Sprint (active) ---
    sprint_id = new_id()
    insert("sprints", {
        "id": sprint_id, "project_id": project_id, "name": "Sprint 1 – Foundation",
        "goal": "Standing skeleton: auth working end to end and CI pipeline green.",
        "start_at": ts, "end_at": ts, "capacity": 21, "status": "Active", "created_at": ts,
    })

    # --- Tasks with dependencies ---
    task_defs = [
        ("Set up repository and CI pipeline", "Repo scaffold, lint, test, container build.", 3, 2, "DevOps Engineer", "Done"),
        ("Design auth API contract", "Endpoints, DTOs, error model for authentication.", 3, 2, "Solution Architect", "Done"),
        ("Implement login endpoint", "Email/password login with secure session tokens.", 5, 1, "Senior Developer", "Ready"),
        ("Implement ticket submission API", "Create/list tickets with validation.", 5, 1, "Senior Developer", "Todo"),
        ("Build login UI", "Login form with error handling.", 3, 2, "Senior Developer", "Todo"),
        ("Write auth test plan", "Risk-based plan covering auth acceptance criteria.", 2, 2, "QA Engineer", "Todo"),
    ]
    task_ids = []
    for title, desc, pts, prio, agent_role, status in task_defs:
        tid = new_id()
        task_ids.append(tid)
        insert("tasks", {
            "id": tid, "project_id": project_id, "sprint_id": sprint_id,
            "backlog_item_id": bl_ids[0] if "auth" in title.lower() or title.startswith(("Design auth", "Implement login", "Build login", "Write auth")) else None,
            "title": title, "description": desc, "acceptance_criteria": "Review approved with evidence attached.",
            "story_points": pts, "priority": prio, "assigned_agent_id": agent_ids[agent_role],
            "status": status, "progress": 100 if status == "Done" else 0,
            "created_at": ts, "updated_at": ts,
        })
    # Dependencies: implement login depends on design; test plan depends on design; ticket API depends on design
    for dep_idx in (2, 5, 3):
        execute("INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?,?)",
                (task_ids[dep_idx], task_ids[1]))
    execute("INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?,?)",
            (task_ids[4], task_ids[2]))

    # --- Welcome conversation ---
    conv_id = new_id()
    insert("conversations", {"id": conv_id, "project_id": project_id,
                             "title": "Getting started", "created_at": ts, "updated_at": ts})
    insert("messages", {
        "id": new_id(), "conversation_id": conv_id, "role": "assistant",
        "content": "Welcome to Project Control for **Customer Portal**. I can create roles, personas, agents, teams, gateways, backlog items and sprints, assign tasks and start executions. Type `help` to see available commands.",
        "created_at": ts,
    })

    # --- Settings defaults ---
    execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('theme', 'dark')")
    execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('seeded', '1')")

    audit("seed", "system", summary="Seeded demo gateway, roles, personas, skills, agents, team, project, sprint and tasks")
