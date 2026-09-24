# AgentForge — End-to-End Implementation Guide

## Project Overview

AgentForge is an autonomous multi-agent software engineering platform where a team
of AI agents (Product Owner, Business Analyst, Solution Architect, Senior Developer,
Junior Developer, QA Engineer, DevOps Engineer) collaborates to build real software
end-to-end. Each agent has a specific role, persona, and skill set. The platform
manages projects through sprint cycles with review gates, automated testing, and
real code generation.

## Architecture

### Backend (FastAPI + SQLite)

```
backend/
  app/
    main.py               # FastAPI entrypoint + CORS + mount
    db.py                 # SQLite persistence layer (WAL mode)
    schemas.py            # Pydantic request/response models
    runtime.py            # Agent execution engine (phases, retries, events)
    codegen.py            # LLM-based code generation + review verdicts
    memory.py             # Project memory + context briefing
    governance.py         # Project lifecycle enforcement
    po.py                 # Product Owner narrative + task planning
    sprint_gate.py        # Sprint eligibility enforcement
    browser_test.py       # Playwright browser smoke tests
    workspace.py          # File I/O isolation
    toolchains.py         # Stack detection (Python, .NET, Go, Node)
    config.py             # Centralized configuration
    chatbots/             # Chatbot integrations
    routers/              # REST API endpoints
      agents.py, projects.py, roles.py, tasks.py, gateways.py, governance.py
    migrations/           # Database migration scripts
    llm/                  # LLM provider abstraction
      resolver.py         # Model + gateway failover chain
      key_manager.py      # Encrypted API key storage
      gateway_client.py   # Live LLM provider calls
      health.py           # Provider health checks
    services/             # Business logic services
    models/               # ORM/data models (if used)
    static/               # Frontend build artifacts
    tests/                # Backend unit + integration tests
  tests/                  # Additional integration tests
  static/                 # Built frontend assets
  migrations/             # SQL migration files
```

### Frontend (React + TypeScript + Tailwind)

```
frontend/
  src/
    components/           # Reusable UI components
      Layout.tsx          # Shell, sidebar, breadcrumbs
    pages/
      ProjectsPage.tsx    # Dashboard + new project wizard
      ProjectFlowPage.tsx # Sprint + task + workflow management
      RolesPage.tsx        # Role/persona/skill administration
      AgentsPage.tsx       # Agent roster management
      GatewaysPage.tsx     # LLM gateway configuration
      DashboardPage.tsx    # Usage + cost metrics
      ChatbotPage.tsx      # Chat with agents
    api/                  # API client helpers
    lib/                  # Utilities
  public/                 # Static assets
  package.json
  tsconfig.json
  tailwind.config.js
  vite.config.ts
```

### Database (SQLite with WAL)

```
Tables:
  projects           # Project metadata, stack, workspace path
  sprints            # Sprint containers (Backlog, Sprint 1, 2, …)
  sprint_phases      # Sprint lifecycle states (Pre, Open, Executing, Review, Complete, Locked)
  tasks              # Task backlog items with acceptance criteria, status, agent
  task_reviews       # SA/BA review verdicts
  task_dependencies  # Dependency graph (blocks/blocked-by)
  workflow_runs      # Durable run records (status, mode, timestamps)
  execution_events   # Per-project sequenced event log
  agents             # Agent roster (role, persona, model binding, lifecycle)
  teams              # Team containers
  team_agents        # Agent ↔ team membership + role_in_team
  roles              # Role definitions (BA, SA, Sr Dev, Jr Dev, QA, DevOps, PO)
  personas           # Persona definitions (name, instructions, constraints)
  persona_versions   # Historical persona snapshots
  persona_skills     # Persona ↔ skill linking
  skills             # Skill definitions
  role_skills        # Role ↔ skill linking (available to all agents of that role)
  instruction_files  # Markdown instruction files per role (INJECTED into prompts)
  gateways           # LLM provider gateways (OpenAI, Anthropic, etc.)
  gateway_models     # Available models per gateway
  model_bindings     # Global or per-role model binding with settings
  messages           # Agent-to-agent messages
  conversations      # Chatbot conversations
  pending_commands   # Pending agent commands
  audit_events       # Audit trail for all significant actions
  settings           # Key-value store for platform settings
  project_requirements   # Requirement trackers (requirements-mode projects)
  project_baselines      # Requirement snapshots
  architecture_decisions # Architecture decision records
  interface_contracts    # API/interface contracts
  file_contracts         # File ownership contracts
  file_ownership         # File ↔ agent ownership map
  component_registry     # Registered components
  dependency_requests    # Dependency request queue
  context_cache          # Project context cache
  usage_records          # LLM token usage
  change_requests        # Change request records
  project_memory         # Per-project memory notes
  memory_checkpoints     # Memory version history
  agent_messages         # Agent message queue
  workflow_runs          # Run records
  tasks                 # Task definitions
  sprints               # Sprint containers
  backlog_items         # Backlog items
  projects              # Project definitions
  teams                 # Team containers
  agents                # Agent definitions
```

## Execution Model

### Phase Pipeline

Every task execution follows a fixed pipeline with distinct phases. The runtime
advances through each phase, calling the assigned agent's model at each step.

```
Task Claimed → Analyze → Plan → Implement → Build/Test → Review → Finalize → Done
                                          ↓ Retry (SELF_FIX_ATTEMPTS)
                                          ↓ QA → Verify → QA Test → QA Report
                                          ↓ SA Review → BA Review
```

**Dev Mode** (default):
1. `analyze` (15%) — Analyze requirements, read existing code
2. `plan` (25%) — Draft implementation plan from persona instructions
3. `implement` (55%) — Write code in project workspace
4. `build-test` (75%) — Run unit tests, browser smoke
5. `review` (90%) — Self-review against definition of done
6. `finalize` (100%) — Record evidence, complete task

**QA Mode** (run after dev completes):
1. `verify` (30%) — Review implementation against AC
2. `qa-test` (70%) — Run test suite and browser checks
3. `qa-report` (95%) — Write verdict and defect summary

**SA Review Mode** (runs before QA):
1. `sa-review` (92%) — Architecture alignment review

**BA Review Mode** (runs after SA, before QA):
1. `ba-review` (92%) — Functional alignment review

### Retry and Rework Loops

- **Self-Fix Attempts** (configurable, default 3): When the dev's own build/tests
  fail, the runtime retries the implementation with the exact error as feedback.
  Code that still fails is never handed to QA — it moves straight to Blocked.

- **QA Rejection Cycles** (configurable, default 3): After QA rejects a task,
  the runtime loops back to dev with the QA findings as feedback. After
  MAX_REWORK_CYCLES rejections, the task is escalated to a human.

- **Review Gate Failures**: If SA or BA review returns "rework", the task goes
  back to the dev phase with the specific class id and findings as feedback.

### Model Resolution and Failover

Each agent has a `model_binding` that points to a `gateway_models` entry. Models
are organized as an ordered failover chain within each binding. When a model fails
(LLM outage, timeout, quota), the runtime falls back to the next member in the
chain. The successful member is pinned for the rest of the task to avoid
reprobing on every phase.

**Priority**:
1. Pinned ref (the member that worked earlier in this task)
2. Members ordered by `priority` within the binding
3. Global fallback binding

### Context Assembly

Before each LLM call, the runtime assembles a rich context from:

1. **Project metadata** (name, goal, stack, workspace files)
2. **Task details** (title, description, acceptance criteria, evidence)
3. **Workspace state** (directory tree, `app/main.py`, related source files)
4. **Shared helpers** (`app/helpers.py` index — only for Python projects)
5. **Previous attempt feedback** (when in a rework loop)
6. **Project memory** (pitfalls, lessons learned)
7. **Persona + role instructions** (injected by `codegen.py`)
8. **Review verdicts** (from SA/BA review gates)

The  is assembled by `codegen.build_system_prompt()` which combines
the stack-specific base with role instruction files and persona instructions in
priority order.

## Sprint Execution Flow

```
Sprint Started
  → Sprint state set to "Executing"
  → Eligible tasks identified (Todo → Ready transition)
  → Tasks auto-assigned to team agents (round-robin by workload)
  → Runtime begins pulling Ready tasks and executing them in parallel
     (one task per agent at a time)
  → Completed tasks accumulate evidence
  → Sprint review gates (SA/BA) applied
  → QA verifies completed tasks
  → Sprint marked Complete when all Done
  → New sprint can be created
```

### Sprint Phases (Lifecycle)

Sprints go through these phases:
- `Pre` — Sprint is being prepared, tasks are being added
- `Open` — Sprint is open but not yet executing
- `Executing` — Agent runtime is actively executing tasks
- `Review` — All tasks done, SA/BA review in progress
- `Complete` — Sprint is done, all tasks verified
- `Locked` — Sprint is frozen, no new tasks can be added or modified

### Task Lifecycle

Tasks transition through these states:
- `Todo` — Initial state, not yet ready
- `Ready` — Ready for execution
- `In Progress` — Agent is actively working
- `Blocked` — Blocked by dependency or error
- `SA Review` — Awaiting Solution Architect review
- `BA Review` — Awaiting Business Analyst review
- `Rework` — Returned to dev for rework
- `Testing` — QA phase in progress
- `Waiting QA` — Awaiting QA assignment
- `Review` — Self-review phase
- `Done` — Completed and verified
- `Cancelled` — Cancelled

## Key Configuration

Configuration is centralized in `backend/app/config.py`. Key settings:

| Setting | Default | Purpose |
|---|---|---|
| `MAX_REWORK_CYCLES` | 3 | Max QA rejection cycles before human escalation |
| `SELF_FIX_ATTEMPTS` | 3 | Max self-fix retries after build/test failures |
| `PHASE_TIMEOUT_S` | 300 | Max seconds per phase before hard timeout |
| `SCHEDULER_IDLE_GIVE_UP_BLOCKED` | 30 | Rounds of idle before scheduler stops |
| `SCHEDULER_IDLE_GIVE_UP_CLEAN` | 10 | Rounds of idle for clean shutdown |
| `LLM_INPUT_COST_PER_M` | $0.002 | Default input cost per million tokens |
| `LLM_OUTPUT_COST_PER_M` | $0.002 | Default output cost per million tokens |
| `TOTAL_BUDGET_S` | 120 | Browser smoke test total budget |

## Security Architecture

### LLM API Key Storage

API keys are encrypted at rest using the platform's encryption module
(`backend/app/secrets.py`). Keys are never exposed in API responses — only
the `key_mask` (e.g., `sk-••••7f2a`) is returned.

### Authentication

- All REST endpoints are currently open (no auth layer in MVP)
- In production: add OAuth2/JWT or API key authentication
- Gateways store keys encrypted; the `get_gateway_key()` function decrypts
  on demand

### SQL Injection Prevention

- All queries use parameterized statements (`?` placeholders)
- No raw SQL string interpolation for user input
- Foreign keys are enforced (`PRAGMA foreign_keys=ON`)
- WAL mode + busy_timeout prevent corruption under concurrent access

###  Mitigation

-  are assembled server-side from trusted database content
- User content (task descriptions, acceptance criteria) is isolated in the
  user message, never injected into the 
- The `_sanitize_exception()` function strips internal paths from error messages

### Workspace Isolation

- Each project has its own workspace directory
- The runtime runs browser smoke tests in a subprocess
- File operations use relative paths within the workspace

### Audit Trail

All significant actions (role changes, task status changes, model calls,
audit events) are logged to the `audit_events` table with actor, action,
target, and timestamp.

## Deployment

### Local Development

1. Clone the repository
2. Set up Python 3.9+ virtual environment
3. Install dependencies: `pip install -r backend/requirements.txt`
4. Install frontend deps: `cd frontend && npm install`
5. Configure `.env` in backend/ with LLM gateway credentials
6. Run migrations: `cd backend && python -m app.migrations.010_project_governance_flag`
7. Start backend: `cd backend && uvicorn app.main:app --reload --port 8000`
8. Build frontend: `cd frontend && npm run build`
9. Serve: `cd backend && python -m http.server 8080` (from static/)

### Docker

A multi-stage Dockerfile builds the frontend, copies it into the backend
image, and serves both from a single container. The SQLite database is
mounted as a volume for persistence.

### Production Considerations

- SQLite → PostgreSQL (spec calls for PostgreSQL)
- Add authentication layer (OAuth2/JWT)
- Add rate limiting on LLM calls
- Implement background task queue (Celery/RQ) for long-running workflows
- Add Redis for distributed locking and caching
- Implement model call accounting and budget enforcement
