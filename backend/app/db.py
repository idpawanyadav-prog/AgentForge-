"""SQLite persistence layer for Agent Office.

SQLite is used as the container-local durable store for the MVP build.
For production deployments the spec calls for PostgreSQL; the data-access
layer is intentionally simple SQL so it can be swapped later.
"""
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

DB_PATH = os.environ.get("AGENT_OFFICE_DB", os.path.join(os.path.dirname(__file__), "..", "agent_office.db"))

_local = threading.local()


def get_db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
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
    last_exc = None
    for attempt in range(5):
        try:
            cur = db.execute(sql, params)
            db.commit()
            return cur
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            last_exc = exc
            time.sleep(0.3 * (attempt + 1))
    raise last_exc


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


def sync_gateway_models(gateway_id: str) -> dict:
    """Make the gateway's model catalog match the provider's available models.

    The provider catalog is authoritative: models from other providers that
    ended up in the catalog (e.g. via manual registration or an older sync)
    are removed along with any model bindings pointing at them. Returns
    {"added": n, "removed": m}.
    """
    gw = query_one("SELECT provider FROM gateways WHERE id=?", (gateway_id,))
    if not gw:
        return {"added": 0, "removed": 0}
    catalog = PROVIDER_CATALOGS.get(gw["provider"], PROVIDER_CATALOGS["custom"])
    keep = {pid for pid, _, _ in catalog}
    added, removed = 0, 0

    for row in query("SELECT id, provider_model_id FROM gateway_models WHERE gateway_id=?", (gateway_id,)):
        if row["provider_model_id"] not in keep:
            execute("DELETE FROM model_bindings WHERE model_id=?", (row["id"],))
            execute("DELETE FROM gateway_models WHERE id=?", (row["id"],))
            removed += 1

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
    return {"added": added, "removed": removed}


SCENARIO = """
CREATE TABLE IF NOT EXISTS persona_version_seed_marker (id TEXT PRIMARY KEY);
"""


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    # lightweight column migrations for databases created before a column existed
    cols = {r["name"] for r in query("PRAGMA table_info(gateways)")}
    if "api_key_enc" not in cols:
        execute("ALTER TABLE gateways ADD COLUMN api_key_enc TEXT")
    task_cols = {r["name"] for r in query("PRAGMA table_info(tasks)")}
    if "qa_agent_id" not in task_cols:
        execute("ALTER TABLE tasks ADD COLUMN qa_agent_id TEXT REFERENCES agents(id)")
    if "rework_count" not in task_cols:
        execute("ALTER TABLE tasks ADD COLUMN rework_count INTEGER NOT NULL DEFAULT 0")
    instr_cols = {r["name"] for r in query("PRAGMA table_info(instruction_files)")}
    if "active" not in instr_cols:
        execute("ALTER TABLE instruction_files ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
    proj_cols = {r["name"] for r in query("PRAGMA table_info(projects)")}
    if "po_enabled" not in proj_cols:
        execute("ALTER TABLE projects ADD COLUMN po_enabled INTEGER NOT NULL DEFAULT 0")
    # Sprint gating: gate status columns on pre-existing sprints.
    sprint_cols = {r["name"] for r in query("PRAGMA table_info(sprints)")}
    for col, ddl in (
        ("development_status", "TEXT NOT NULL DEFAULT 'Pending'"),
        ("build_status", "TEXT NOT NULL DEFAULT 'Pending'"),
        ("test_status", "TEXT NOT NULL DEFAULT 'Pending'"),
        ("functional_status", "TEXT NOT NULL DEFAULT 'Pending'"),
        ("acceptance_status", "TEXT NOT NULL DEFAULT 'Pending'"),
        ("failure_reason", "TEXT NOT NULL DEFAULT ''"),
        ("started_at", "TEXT"),
        ("completed_at", "TEXT"),
    ):
        if col not in sprint_cols:
            execute(f"ALTER TABLE sprints ADD COLUMN {col} {ddl}")
    db.commit()
    seed_if_empty()
    seed_instruction_files()
    ensure_product_owner()
    ensure_junior_developer()


def _mask(key_value: str) -> str:
    return "••••" + key_value[-4:] if key_value and len(key_value) >= 4 else "••••"


def set_gateway_key(gateway_id: str, raw_key: str):
    """Store an API key encrypted at rest. Only a mask is ever exposed."""
    from . import secrets as _secrets
    update("gateways", gateway_id, {"api_key_enc": _secrets.encrypt(raw_key),
                                    "key_mask": _mask(raw_key), "updated_at": now()})


def get_gateway_key(gateway_id: str) -> str | None:
    from . import secrets as _secrets
    row = query_one("SELECT api_key_enc FROM gateways WHERE id=?", (gateway_id,))
    if not row or not row["api_key_enc"]:
        return None
    try:
        return _secrets.decrypt(row["api_key_enc"])
    except Exception:
        return None


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
            "Junior Developer": [
                ("helpers-playbook.md", "How to grow the shared reusable helpers file",
                 "# Junior Developer Playbook\n\n## Mission\n- One shared helpers file per project, owned by you\n- Every helper is small, typed, easy to build and easy to reuse\n\n## Rules\n- Before writing anything, check whether a helper already exists\n- Repeated boilerplate becomes a helper — the next agent calls it instead of regenerating it\n- Fewer regenerated lines means fewer tokens spent per task\n- One concern per function; no speculative features"),
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


PO_CHARTER = """# Product Owner Charter

## Authority
- When enabled, the Product Owner acts with the project owner's full authority:
  review and rewrite requirements, shape the backlog, plan sprints, assign or
  reassign tasks, unblock escalated items and start/stop sprint execution.
- Decisions are logged to the audit trail; the owner can disable the takeover
  at any time and every open task is left untouched.

## Duties
- Keep a single, coherent requirement set: project goal, backlog and sprint
  tasks must never contradict each other.
- Plan sprints as vertical slices that end in working, testable software.
- Write tasks for the right specialist: development work for developers, test
  plans for QA, architecture for the Solution Architect, requirements for the BA.
- Unblock escalated tasks when the fix is obvious; escalate to the owner only
  for genuine product ambiguity or missing tooling.

## Constraints
- Never invent agents that are not on the team roster.
- Never mark a task Done without QA evidence.
- Scope added mid-sprint goes through the backlog first unless it is urgent.
"""


def ensure_product_owner():
    """Idempotently seed the Product Owner role (persona, skill, instruction
    file, model binding) so it exists on fresh and existing databases alike."""
    ts = now()
    role = query_one("SELECT * FROM roles WHERE lower(name) = lower('Product Owner')")
    if not role:
        rid = new_id()
        insert("roles", {"id": rid, "name": "Product Owner",
                         "description": "Owns requirements and product decisions; can run the "
                                        "project autonomously when enabled.",
                         "active": 1, "created_at": ts, "updated_at": ts})
        role = query_one("SELECT * FROM roles WHERE id = ?", (rid,))
        audit("seed_po_role", "role", rid, "Seeded Product Owner role")
    rid = role["id"]

    if not query_one("SELECT id FROM instruction_files WHERE role_id = ?", (rid,)):
        insert("instruction_files", {
            "id": new_id(), "role_id": rid, "filename": "product-owner-charter.md",
            "description": "Authority, duties and constraints of the Product Owner agent",
            "content": PO_CHARTER, "version": 1, "created_at": ts, "updated_at": ts,
        })

    skill = query_one("SELECT * FROM skills WHERE name = 'product-ownership'")
    if not skill:
        sid = new_id()
        insert("skills", {"id": sid, "name": "product-ownership",
                          "description": "Turn owner intent into backlog, sprints and task assignments.",
                          "content": ("# Product Ownership\n"
                                      "- Maintain a prioritized, INVEST-clean backlog\n"
                                      "- Slice work into vertical, testable increments\n"
                                      "- Assign tasks by role family; unblock quickly\n"
                                      "- Decide with the owner's authority; log everything"),
                          "version": 1, "active": 1, "created_at": ts, "updated_at": ts})
        skill = query_one("SELECT * FROM skills WHERE id = ?", (sid,))
    execute("INSERT OR IGNORE INTO role_skills (role_id, skill_id) VALUES (?,?)", (rid, skill["id"]))

    persona = query_one("SELECT * FROM personas WHERE role_id = ? ORDER BY created_at LIMIT 1", (rid,))
    if not persona:
        pid = new_id()
        instr = ("You are the Product Owner with the owner's full authority. Review the whole "
                 "requirement set, keep goal, backlog and sprint tasks coherent, plan sprints as "
                 "vertical slices and direct the team decisively. Log every decision.")
        insert("personas", {"id": pid, "role_id": rid, "name": "Vision Keeper",
                            "description": "Decisive product owner who runs projects end to end.",
                            "instructions": instr,
                            "constraints_text": "Never invent agents; never mark tasks Done without QA evidence.",
                            "version": 1, "active": 1, "created_at": ts, "updated_at": ts})
        insert("persona_versions", {"id": new_id(), "persona_id": pid, "version": 1,
                                    "instructions": instr, "constraints_text": "",
                                    "checksum": checksum(instr), "created_at": ts})
        execute("INSERT INTO persona_skills (persona_id, skill_id) VALUES (?,?)", (pid, skill["id"]))
        execute("INSERT OR IGNORE INTO role_skills (role_id, skill_id) VALUES (?,?)", (rid, skill["id"]))
        audit("seed_po_persona", "persona", pid, "Seeded Product Owner persona 'Vision Keeper'")

    if not query_one("SELECT id FROM model_bindings WHERE role_id = ? AND active = 1", (rid,)):
        gw = query_one("SELECT id FROM gateways WHERE status = 'Active' ORDER BY created_at LIMIT 1")
        model = query_one(
            "SELECT id FROM gateway_models WHERE gateway_id = ? AND active = 1 "
            "AND provider_model_id LIKE '%mini%' LIMIT 1", (gw["id"],)) if gw else None
        if not model and gw:
            model = query_one("SELECT id FROM gateway_models WHERE gateway_id = ? AND active = 1 LIMIT 1",
                              (gw["id"],))
        if gw and model:
            insert("model_bindings", {"id": new_id(), "role_id": rid, "gateway_id": gw["id"],
                                      "model_id": model["id"],
                                      "settings_json": json.dumps({"temperature": 0.3}), "active": 1})


def ensure_junior_developer():
    """Idempotently guarantee the Junior Developer role and — per the team
    charter — one Junior Developer agent on EVERY team. The Jr Dev owns the
    shared reusable helpers file: small, easy-to-build functions that
    teammates call instead of regenerating boilerplate, which keeps the
    code (and therefore token usage) per task down."""
    ts = now()
    role = query_one("SELECT * FROM roles WHERE lower(name) = lower('Junior Developer')")
    if not role:
        rid = new_id()
        insert("roles", {"id": rid, "name": "Junior Developer",
                         "description": "Builds small reusable helper functions in the shared helpers "
                                        "file so teammates generate less code and fewer tokens.",
                         "active": 1, "created_at": ts, "updated_at": ts})
        role = query_one("SELECT * FROM roles WHERE id = ?", (rid,))
        audit("seed_jr_dev_role", "role", rid, "Seeded Junior Developer role")
    rid = role["id"]

    if not query_one("SELECT id FROM instruction_files WHERE role_id = ?", (rid,)):
        insert("instruction_files", {
            "id": new_id(), "role_id": rid, "filename": "helpers-playbook.md",
            "description": "How to grow the shared reusable helpers file",
            "content": ("# Junior Developer Playbook\n\n## Mission\n- One shared helpers file per project, "
                        "owned by you\n- Every helper is small, typed, easy to build and easy to reuse\n\n"
                        "## Rules\n- Before writing anything, check whether a helper already exists\n"
                        "- Repeated boilerplate becomes a helper — the next agent calls it instead of "
                        "regenerating it\n- Fewer regenerated lines means fewer tokens spent per task\n"
                        "- One concern per function; no speculative features"),
            "version": 1, "created_at": ts, "updated_at": ts,
        })

    persona = query_one("SELECT * FROM personas WHERE role_id = ? ORDER BY created_at LIMIT 1", (rid,))
    if not persona:
        pid = new_id()
        instr = ("You are the Junior Developer. Your job is small, easy-to-build reusable functions: keep "
                 "the shared helpers file growing so teammates call helpers instead of re-implementing "
                 "them — less generated code, fewer tokens. Keep every function tiny, typed and tested.")
        insert("personas", {"id": pid, "role_id": rid, "name": "Helper Builder",
                            "description": "Turns repeated boilerplate into tiny reusable helpers.",
                            "instructions": instr,
                            "constraints_text": "Never duplicate a helper that already exists. No clever one-liners.",
                            "version": 1, "active": 1, "created_at": ts, "updated_at": ts})
        insert("persona_versions", {"id": new_id(), "persona_id": pid, "version": 1,
                                    "instructions": instr,
                                    "constraints_text": "Never duplicate a helper that already exists.",
                                    "checksum": checksum(instr), "created_at": ts})
        persona = query_one("SELECT * FROM personas WHERE id = ?", (pid,))
        audit("seed_jr_dev_persona", "persona", pid, "Seeded Junior Developer persona 'Helper Builder'")

    binding = query_one("SELECT id FROM model_bindings WHERE role_id = ? AND active = 1", (rid,))
    if not binding:
        gw = query_one("SELECT id FROM gateways WHERE status = 'Active' ORDER BY created_at LIMIT 1")
        model = query_one(
            "SELECT id FROM gateway_models WHERE gateway_id = ? AND active = 1 "
            "AND provider_model_id LIKE '%mini%' LIMIT 1", (gw["id"],)) if gw else None
        if not model and gw:
            model = query_one("SELECT id FROM gateway_models WHERE gateway_id = ? AND active = 1 LIMIT 1",
                              (gw["id"],))
        if gw and model:
            insert("model_bindings", {"id": (bid := new_id()), "role_id": rid, "gateway_id": gw["id"],
                                      "model_id": model["id"],
                                      "settings_json": json.dumps({"temperature": 0.2}), "active": 1})
            binding = query_one("SELECT id FROM model_bindings WHERE id = ?", (bid,))

    # One Junior Developer per team, on every team that lacks one.
    for team in query("SELECT * FROM teams ORDER BY created_at"):
        has_jr = query_one(
            "SELECT ta.agent_id FROM team_agents ta JOIN agents a ON a.id = ta.agent_id "
            "JOIN roles r ON r.id = a.role_id "
            "WHERE ta.team_id = ? AND ta.active = 1 AND lower(r.name) = lower('Junior Developer') "
            "LIMIT 1", (team["id"],))
        if has_jr:
            continue
        n = query_one("SELECT COUNT(*) AS n FROM agents a JOIN roles r ON r.id = a.role_id "
                      "WHERE lower(r.name) = lower('Junior Developer')")["n"]
        name = "Pip" if n == 0 else f"Pip {n + 1}"
        aid = new_id()
        insert("agents", {"id": aid, "name": name, "role_id": rid, "persona_id": persona["id"],
                          "model_binding_id": binding["id"] if binding else None,
                          "lifecycle_state": "Idle", "current_activity": "",
                          "created_at": ts, "updated_at": ts})
        execute("INSERT INTO team_agents (team_id, agent_id, role_in_team, active) VALUES (?,?,?,1)",
                (team["id"], aid, "Shared Helpers"))
        audit("seed_jr_dev_agent", "agent", aid,
              f"Added Junior Developer '{name}' to team '{team['name']}' (owns the shared helpers file)")


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
        ("Junior Developer", "Builds small reusable helper functions in the shared helpers "
                             "file so teammates generate less code and fewer tokens."),
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
        ("Junior Developer", "Helper Builder", "Turns repeated boilerplate into tiny reusable helpers.",
         "You are the Junior Developer. Your job is small, easy-to-build reusable functions: keep the shared "
         "helpers file growing so teammates call helpers instead of re-implementing them — less generated "
         "code, fewer tokens. Keep every function tiny, typed and tested.",
         "Never duplicate a helper that already exists. No clever one-liners.", ["python-coding"]),
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
        "Junior Developer": "gpt-4.1-mini",
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
        ("Nova", "Senior Developer"), ("Pip", "Junior Developer"),
        ("Iris", "QA Engineer"), ("Bolt", "DevOps Engineer"),
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
                                    ("Junior Developer", "Shared Helpers"),
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
