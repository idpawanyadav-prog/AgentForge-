# AgentForge — End-to-End Implementation Guide

## 1. System Overview

AgentForge is an autonomous AI-powered software development orchestration platform. It simulates a delivery team of LLM-driven agents (developers, QA, Solution Architects, Business Analysts, Product Owners) that implement real software tasks inside isolated project workspaces.

### Core Value Proposition
- Agents write real code, run real builds/tests, and perform real browser smoke checks
- Workspace isolation prevents concurrent agent collisions
- Circuit breaker + model failover chains for LLM resilience
- Per-gateway health tracking (no single failure takes down all projects)
- Governance opt-in lifecycle state machine per project
- Product Owner autonomous mode for self-driving delivery
- Review gates: SA review, BA review before QA handoff

---

## 2. Architecture

### 2.1 Layered Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Frontend (React 18 + TypeScript + Vite)                    │
│  - SPA with 7 pages (Control, Flow, Projects, Teams, etc.)  │
│  - Polling-based real-time updates (1.2-1.5s intervals)     │
│  - No router, no global state, inline styles                │
├─────────────────────────────────────────────────────────────┤
│  FastAPI Backend (main.py)                                  │
│  - 10 routers under /api/v1                                 │
│  - CORS + rate limiting middleware                          │
│  - SPA fallback routing                                     │
├─────────────────────────────────────────────────────────────┤
│  Router Layer (routers/*.py)                                │
│  - HTTP request/response handling                           │
│  - Request validation (Pydantic)                            │
│  - Delegates to services/runtime                            │
├─────────────────────────────────────────────────────────────┤
│  Service Layer (services/*.py)                              │
│  - tasks.py: Task CRUD, transitions, dependencies           │
│  - sprints.py: Sprint state machine                         │
│  - agents.py: Agent selection/assignment (thin wrapper)     │
├─────────────────────────────────────────────────────────────┤
│  Runtime Engine (runtime.py)                                │
│  - Task execution phases (analyze → plan → implement → ...) │
│  - Workspace isolation + merge-back                         │
│  - Pause/resume/cancel/retry controls                       │
│  - Background task management                               │
│  - Orphan recovery on startup                               │
├─────────────────────────────────────────────────────────────┤
│  Business Logic Modules                                     │
│  - po.py: Product Owner autonomous mode                     │
│  - chatbot.py: Natural language command interface           │
│  - codegen.py: LLM code generation + circuit breaker        │
│  - governance.py: Lifecycle state machine + baselines       │
│  - sprint_gate.py: Sprint-level gating                      │
│  - memory.py: Project pitfall ledger                        │
│  - toolchains.py: Multi-language build/test support         │
│  - browser_test.py: Playwright smoke testing                │
├─────────────────────────────────────────────────────────────┤
│  Data Access Layer (db.py)                                  │
│  - SQLite with WAL mode                                     │
│  - Parameterized queries                                    │
│  - Connection per thread (threading.local)                  │
│  - Migration system (001-010)                               │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 Technology Stack

| Layer | Technology |
|-------|-----------|
| Backend Framework | FastAPI (Python 3.10+) |
| Database | SQLite (WAL mode, per-test isolation) |
| ORM | Raw SQL (no ORM — swapable for PostgreSQL) |
| Frontend | React 18 + TypeScript + Vite |
| LLM Integration | Direct HTTP (Anthropic + OpenAI-compatible) |
| Browser Testing | Playwright (Chromium) |
| Encryption | Fernet (cryptography library) |
| Concurrency | asyncio + threading + locks |
| Rate Limiting | In-memory sliding window + optional Redis |

---

## 3. Database Schema

### 3.1 Core Tables (Migration 001)

| Table | Purpose |
|-------|---------|
| `settings` | Key-value configuration |
| `gateways` | LLM provider configurations (encrypted keys) |
| `gateway_models` | Available models per gateway |
| `roles` | Agent role definitions (Developer, QA, SA, BA, etc.) |
| `personas` | Role-specific behavior instructions |
| `persona_versions` | Historical persona snapshots |
| `skills` | Reusable capability definitions |
| `persona_skills` | Role-skill associations |
| `model_bindings` | Role-to-model mapping with failover chains |
| `agents` | Team member instances |
| `teams` | Project-aligned agent groups |
| `team_agents` | Agent-team membership |
| `projects` | Project metadata + workspace paths |
| `backlog_items` | Product backlog |
| `sprints` | Sprint iterations with gate statuses |
| `sprint_gate_executions` | Gate run records |
| `sprint_acceptance_criteria` | AC items for sprint validation |
| `tasks` | Work items with lifecycle tracking |
| `task_dependencies` | Task dependency graph |
| `conversations` | Chat threads |
| `messages` | Chat messages |
| `execution_events` | Real-time workflow events |
| `workflow_runs` | Task execution instances |
| `usage_records` | Token/cost tracking |
| `audit_events` | Immutable audit trail |
| `instruction_files` | Role-specific instruction files |
| `role_skills` | Role-skill associations |
| `pending_commands` | Chatbot confirmation queue |
| `idempotency_keys` | Safe retry tokens |

### 3.2 Governance Tables (Migration 009)

| Table | Purpose |
|-------|---------|
| `project_requirements` | Requirement intake (append-only, versioned) |
| `project_baselines` | Frozen scope snapshots (RB-x.0, AB-x.0) |
| `architecture_decisions` | ADR records |
| `interface_contracts` | Module interface contracts |
| `file_contracts` | Per-file ownership + constraints |
| `file_ownership` | Path-pattern → role mapping |
| `component_registry` | Component catalog |
| `dependency_requests` | Package install requests + approval |
| `task_requirements` | Task-to-requirement links |
| `task_reviews` | SA/BA review records |
| `project_memory` | Pitfalls, patterns, decisions |
| `memory_checkpoints` | Memory snapshots |
| `agent_messages` | Inter-agent communication |
| `change_requests` | Scope change requests |
| `context_cache` | LLM context caching |

### 3.3 Migration Strategy

- **001**: Initial schema (all core tables)
- **002**: Indexes for performance
- **003-008**: Model binding evolution (name, role, consolidation, chains)
- **009**: V3 governance, contracts, memory (30+ new tables + indexes)
- **010**: Project governance opt-in flag

Migrations are applied sequentially via `run_migrations()` in `db.py`.

---

## 4. Key Execution Flows

### 4.1 Task Execution Lifecycle

```
1. User/PO creates task → status: Todo
2. Scheduler/API starts execution:
   a. Validate task readiness (status, agent, deps)
   b. Check governance (may_execute)
   c. Check sprint gate (authorize_task)
   d. Atomic claim (threading.Lock)
   e. Create workflow_run row
   f. Spawn _run_phases coroutine

3. Dev phases execute sequentially:
   a. analyze → emit event, sleep
   b. plan → emit event, sleep
   c. implement → real code generation (LLM or scaffold)
      - Write files to isolated workspace
      - Record model used (failover chain)
      - Validate file sizes before writing
   d. build-test → real build check + tests
      - Self-fix loop (up to SELF_FIX_ATTEMPTS)
      - Process-level timeout on subprocess calls
      - If still failing → Blocked
   e. review → self-review
   f. finalize → merge workspace, record evidence

4. Post-dev routing:
   a. SA Review enabled? → SA Review status
   b. BA Review enabled? → BA Review status
   c. Otherwise → Waiting QA

5. QA phases (if applicable):
   a. verify → review against AC
   b. qa-test → run tests + browser smoke
      - Browser smoke result outranks test results
   c. qa-report → write verdict

6. Final status:
   - Passed → Done
   - Failed → Rework (rework_count++)
   - Max rework exceeded → Blocked (human escalation)
```

### 4.2 Model Resolution & Failover

```
1. Project default_gateway_id + default_model_id (explicit)
2. Project default_gateway_id + preferred codegen model
3. Control bot gateway/model
4. First active configured gateway/model

At runtime:
- Circuit breaker per gateway (5 failures → open 60s)
- Ordered model chain per agent binding
- Pinned model per task (re-checked on restart)
- Cost tracking per model (input/output rates)
- Per-gateway health: unhealthy gateways skipped
- LLM outage classification: quota/rate-limit/unreachable/unknown
```

### 4.3 Governance Lifecycle

```
Draft → Requirement Analysis → Architecture Analysis
  → Pending PO Approval → Approved → Scaffolding
  → Ready for Planning → Active Development
  → Final Validation → Completed

Exception states:
- Change Requested (rejection)
- Blocked (technical blocker)
- Paused (human pause)
- Cancelled (terminal)

Enforcement (opt-in):
- Only Active Development + Final Validation allow execution
- All transitions validated + audited
- Baselines (RB-x.0) required before planning
- Duplicate baselines prevented (serialized proposal)
- FK-complete deletes with 409 guards
```

### 4.4 Sprint Execution Pipeline

```
1. Activate sprint → demote previous active sprint
2. Start sprint execution:
   - Scheduler picks Ready tasks
   - Gates lock sprint during validation
3. Development phase:
   - Tasks execute in parallel (per-agent isolation)
   - Urgent tasks can reopen sprint
4. Gate validation (sequential):
   a. Build Validation → compile/build
   b. Automated Test → test suite
   c. Functional Validation → functional checks
   d. Acceptance → AC verification
5. Outcomes:
   - All pass → Completed, unlock next sprint
   - Any fail → Rework, reopen sprint
   - Max retries → Failed (terminal)
```

---

## 5. Key Modules Deep Dive

### 5.1 Runtime Engine (runtime.py)

**Purpose**: Orchestrates autonomous task execution through phases with real tool calls.

**Key Components**:
- `_registry`: Active run state (run_id → control dict)
- `_schedulers`: Per-project scheduler tasks
- `_claim_lock`: Atomic task claiming
- `PHASES`: Dev execution phases
- `QA_PHASES`: QA verification phases
- `SA_REVIEW_PHASES` / `BA_REVIEW_PHASES`: Review gates

**Concurrency Model**:
- Thread locks for dict mutations
- Async coroutines for I/O-bound work
- `asyncio.to_thread()` for blocking subprocess calls
- `_wait_bounded()` for phase timeouts

**Workspace Isolation**:
- `begin_run_workspace()`: Copy project to temp dir
- `merge_back_run_workspace()`: Apply changes with per-file leases
- `discard_run_workspace()`: Cleanup on cancel/fail

**Error Handling**:
- Orphan recovery on startup (resume interrupted runs)
- LLM outage classification (quota/rate-limit/unreachable)
- Subprocess timeouts (process-level, not just asyncio)
- Self-fix loop with circuit breaker per gateway

### 5.2 Code Generation (codegen.py)

**Purpose**: Generates real, runnable code via LLM or deterministic scaffold.

**Key Features**:
- Multi-provider support (Anthropic + OpenAI)
- Circuit breaker per gateway (5 failures → 60s open)
- Model failover chains (ordered attempts)
- Deterministic FastAPI scaffold fallback
- Prompt assembly with workspace context + pitfalls + helpers
- File size limits (1MB per file, 10MB total)
- LLM outage classification (quota/rate-limit/unreachable/unknown)

**Cost Tracking**:
- Per-model input/output rates
- Token counting from LLM responses
- Usage records per workflow run

### 5.3 Product Owner (po.py)

**Purpose**: Autonomous project control when enabled.

**Capabilities**:
- Review and rewrite requirements/backlog
- Create sprints with tasks
- Assign tasks, unblock escalated items
- Install missing libraries
- Governance actions (baselines, change requests)
- Keep delivery loop running until complete

**Activation**:
- `po_enabled` flag on project
- PO agent in project team with "Product Owner" role
- Dedicated chat interface

### 5.4 Chatbot (chatbot.py)

**Purpose**: Natural language command interface + AI routing.

**Features**:
- Regex-based typed command parsing
- Sensitive command confirmation flow
- LLM-driven intent classification
- Robust JSON extraction from LLM responses
- 40+ supported commands

### 5.5 Governance (governance.py)

**Purpose**: V3 project lifecycle + baseline approval gate.

**States**: 14 lifecycle states with validated transitions
**Baselines**: Append-only, versioned, PO-approval required
**Change Requests**: Scope change tracking + decision
**Enforcement**: Opt-in per project via `governance_enabled`
**Concurrency**: Serialized baseline proposals (prevents duplicate pending baselines)

### 5.6 Sprint Gates (sprint_gate.py)

**Purpose**: Enforce sprint-level constraints and validation pipeline.

**Gates**:
1. Build Validation (compile/build)
2. Automated Test (test suite)
3. Functional Validation (functional checks)
4. Acceptance (AC verification)

**Features**:
- Locked sprints reject new work
- Module health checks create rework tasks
- Retry cap (MAX_GATE_CYCLES)
- Idempotent AC seeding

### 5.7 Toolchains (toolchains.py)

**Purpose**: Multi-language build/test execution in isolated workspaces.

**Supported Languages**:
- Python (pip, venv, pytest)
- Node.js (npm, npx)
- Go (go build, go test, go vet)
- .NET (dotnet build, dotnet test)

**Key Features**:
- Process-level timeouts on all subprocess calls
- Graceful failure (build check reports, doesn't crash)
- Language detection via project files
- Library installation into project `lib/` directory

### 5.8 Browser Testing (browser_test.py)

**Purpose**: Playwright-based browser smoke testing for QA verification.

**Key Features**:
- Headless Chromium via Playwright
- Port probing (find available port)
- Screenshot capture to `qa_artifacts/`
- Timeout enforcement (TOTAL_BUDGET_S)
- Result outranks test results (browser fail = Rework)

---

## 6. API Surface

### 6.1 Router Inventory

| Router | Prefix | Endpoints (~) | Purpose |
|--------|--------|---------------|---------|
| `agents.py` | /api/v1 | 8 | Agent + team CRUD |
| `chat.py` | /api/v1 | 8 | PO chat + conversations |
| `gateways.py` | /api/v1 | 12 | LLM gateway + model management |
| `governance.py` | /api/v1 | 8 | Lifecycle, requirements, baselines |
| `models.py` | /api/v1 | 6 | Named model catalog (failover chains) |
| `projects.py` | /api/v1 | 15 | Project, backlog, sprint CRUD |
| `roles.py` | /api/v1 | 15 | Roles, personas, skills, bindings |
| `settings.py` | /api/v1 | 6 | Settings, bot configs, audit |
| `tasks.py` | /api/v1 | 12 | Task CRUD, executions, events |
| `events.py` | /api/v1 | 1 | SSE stream |

**Total**: ~90 endpoints

### 6.2 Key Patterns

- **Idempotency-Key**: Used on 5 POST endpoints
- **Audit**: Every mutation calls `audit()`
- **FK-aware deletes**: Nullify references before delete, 409 guards
- **Soft-delete**: Used for roles, personas, model bindings
- **Pagination**: `{total, items, limit, offset}` envelope
- **Error handling**: 404 helpers (shared `_util.py`), 409 conflicts, 422 validation
- **Rate Limiting**: Per-IP sliding window (in-memory + optional Redis)
- **LLM Health**: Per-gateway health tracking (3 consecutive failures → unhealthy)

---

## 7. Frontend Architecture

### 7.1 Tech Stack

- **Framework**: React 18.3
- **Language**: TypeScript 5.6 (strict mode)
- **Bundler**: Vite 5.4
- **Routing**: Manual state-based (no router library)
- **State**: Local `useState` everywhere (no global store)
- **Styling**: Global CSS + inline styles
- **Markdown**: Custom regex-based renderer

### 7.2 Pages

1. **ControlPage** (768 lines): Main hub — chat, team view, activity feed, sprint tasks
2. **ProjectFlowPage** (396 lines): Visual pipeline diagram (SVG)
3. **ProjectsPage** (534 lines): Project CRUD + sub-tabs
4. **TeamsPage** (227 lines): Agent/team management
5. **ModelsPage** (197 lines): Model failover chains
6. **MemoryPage** (559 lines): Roles, instructions, skills, personas
7. **SettingsPage** (390 lines): Gateways, models, audit trail

### 7.3 Communication

- **REST API**: All data operations via `fetch`
- **Polling**: ControlPage (1.2s), ProjectFlowPage (1.5s), App health (8s)
- **SSE**: Global event stream via `events.py`
- **Offline**: `localStorage` cache + health-probe banner

---

## 8. Deployment & Operations

### 8.1 Environment Variables

```bash
# Database
AGENT_OFFICE_DB=agent_office.db
AGENT_OFFICE_DB_PATH=agent_office.db  # alias

# Workspaces
AGENT_OFFICE_WORKSPACES=Projects

# Logging
LOG_LEVEL=INFO

# Timeouts
PHASE_TIMEOUT_S=300
SELF_FIX_ATTEMPTS=3
MAX_REWORK_CYCLES=2
MAX_GATE_CYCLES=3

# Costs
LLM_INPUT_COST_PER_M=2.0
LLM_OUTPUT_COST_PER_M=8.0

# Scheduler
SCHEDULER_IDLE_GIVE_UP_BLOCKED=60
SCHEDULER_IDLE_GIVE_UP_CLEAN=600
PO_TICK_INTERVAL=10

# Rate Limiting
RATE_LIMIT_DEFAULT=300
RATE_LIMIT_MUTATION=120
RATE_LIMIT_WINDOW_S=60
RATE_LIMIT_REDIS_URL=  # optional Redis for distributed limiting
```

### 8.2 Startup Sequence

1. `main.py` lifespan:
   - `db.init_db()` — run migrations
   - `workspace.relocate_workspaces()` — fix moved paths
   - `runtime.recover_orphans()` — resume interrupted runs
   - `runtime.set_loop()` — capture event loop
   - Spawn PO resume task (delayed 2s)
2. Mount routers
3. Serve SPA from `backend/static/`

### 8.3 Workspace Layout

```
<repo>/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── db.py
│   │   ├── runtime.py
│   │   ├── ...
│   │   └── routers/
│   ├── static/
│   │   ├── index.html
│   │   └── assets/
│   └── tests/
├── frontend/
│   ├── src/
│   ├── package.json
│   └── vite.config.ts
└── Projects/  # Workspace root (runtime data)
    └── <project-name>/
        ├── <source files>
        └── qa_artifacts/  # Browser screenshots
```

---

## 9. Testing Strategy

### 9.1 Test Coverage

- **~100 tests** across 12 test modules
- **Per-test isolated SQLite DB** via `conftest.py` autouse fixture
- **Coverage areas**:
  - Gateway health + model resolution
  - Task registry + execution
  - Review chain (SA/BA gates)
  - Browser smoke testing
  - Workspace isolation + merge
  - Critical fixes (FK deletes, cycles, outages)
  - Governance lifecycle + baselines
  - Lifecycle fixes (orphan recovery, atomic claim)
  - Process improvements (pitfall ledger)
  - Sprint gating + 4-gate pipeline
  - Codegen fixes (operator precedence, file size limits)

### 9.2 Testing Patterns

- `monkeypatch` for HTTP/toolchain/LLM mocking
- `asyncio.run` per test
- Direct DB seeding for integration tests
- FastAPI `TestClient` without context manager (skips lifespan)
- Threading + `Barrier` for concurrency tests
- Spec-mapped test naming

### 9.3 Coverage Gaps

- No API-level tests for agents, teams, roles, gateways, projects CRUD
- No frontend tests (no test runner configured)
- No tests for `codegen.generate_implementation()` happy path
- No lifespan startup tests
- No security/authorization tests
- No tests for `memory.py` beyond pitfalls
- No tests for governance Step 2+ features

---

## 10. Known Limitations & Future Work

### Current Limitations

1. **SQLite concurrency**: Write contention under high parallelism (mitigated by locks)
2. **No distributed deployment**: Single-process, single-machine only
3. **No authentication/authorization**: All clients have full access
4. **Frontend state management**: No global store, manual polling
5. **No frontend routing**: Manual page switching
6. **Simulated LLM for some paths**: PO/chatbot can use deterministic parsing
7. **No log rotation**: Logs grow unbounded
8. **No frontend tests**: Zero test coverage on UI

### Planned Enhancements

- PostgreSQL migration path (data-access layer already swapable)
- Authentication + role-based access control
- WebSocket/SSE for real-time updates (replace polling)
- Frontend router + global state (Zustand/Redux)
- Frontend test suite (Vitest + Testing Library)
- Distributed rate limiting (Redis)
- Log rotation + structured logging
- Multi-project workspace isolation in containers
- LLM streaming responses
- Advanced conflict resolution for workspace merge

---

## 11. Development Setup

### 11.1 Backend Setup

```bash
# Create virtual environment
python -m venv agentforge-venv
.\agentforge-venv\Scripts\activate  # Windows
# source agentforge-venv/bin/activate  # Unix

# Install dependencies
cd backend
pip install -r requirements.txt

# Run database migrations
python -c "from app.db import init_db; init_db()"

# Start development server
uvicorn app.main:app --reload --port 8000

# Run tests
pytest backend/tests/ -v
```

### 11.2 Frontend Setup

```bash
cd frontend
npm install
npm run dev  # Starts Vite dev server on port 5173 (proxies /api to 8000)
npm run build  # Production build to dist/
npm run preview  # Preview production build
```

### 11.3 Playwright Setup (for browser testing)

```bash
# Install Playwright browsers
playwright install chromium

# Or use the setup script
powershell scripts/setup_playwright.ps1
```

---

## 12. Security Considerations

### Current Measures

1. **API Key Encryption**: Gateway keys encrypted at rest (Fernet)
2. **Key Masking**: API responses only show masked keys
3. **Path Traversal Protection**: Workspace paths validated
4. **SQL Injection Prevention**: Parameterized queries throughout
5. **CORS Configuration**: Configurable origins
6. **Rate Limiting**: Per-IP sliding window (in-memory + optional Redis)
7. **No Stack Traces in Errors**: `_sanitize_exception()` strips internal paths
8. **HTML Escaping**: Frontend markdown renderer escapes HTML before `dangerouslySetInnerHTML`

### Areas for Improvement

- No authentication/authorization (any client has full access)
- No HTTPS enforcement (Vite proxy in dev only)
- Decrypted keys in memory (vulnerable to dumps)
- No audit of sensitive operations beyond basic logging
- No secrets rotation mechanism
- No input sanitization beyond basic escaping
- No CSRF protection

---

## 13. Performance Characteristics

### Current Performance

- **Task execution**: 2.5-4.5s sleep per phase (simulated thinking)
- **Polling**: 1.2-1.5s intervals for real-time updates
- **Database**: SQLite WAL mode (concurrent readers, single writer)
- **Event retention**: 10,000 events per project (bounded)
- **Background tasks**: Unlimited (fire-and-forget, tracked)
- **Rate Limiting**: Per-IP sliding window with periodic pruning

### Bottlenecks

1. **Polling overhead**: 1.2s polling × N concurrent users × M projects
2. **SQLite write contention**: Single writer limits throughput
3. **Thread pool exhaustion**: Subprocess calls consume threads
4. **Event queue growth**: SSE queue unbounded (slow clients)
5. **Memory leaks**: Unbounded event lists, summary cache

### Optimization Opportunities

- Replace polling with WebSocket/SSE
- Implement connection pooling
- Add event queue max-size with backpressure
- Use LRU cache for summary data
- Implement connection pooling for SQLite (or PostgreSQL)

---

## 14. Monitoring & Observability

### Current Monitoring

- **Audit Events**: Every mutation logged with actor, action, resource
- **Execution Events**: Real-time workflow events per project
- **Usage Records**: Token counts + cost estimates per run
- **LLM Health**: Per-gateway health tracking + failure count
- **Circuit Breakers**: Per-gateway failure tracking
- **Rate Limiting**: Hit counting with Redis optional

### Missing Observability

- No structured logging (JSON logs)
- No metrics (Prometheus, StatsD)
- No tracing (OpenTelemetry)
- No alerting
- No performance profiling
- No error tracking (Sentry, Rollbar)

---

## 15. Quick Reference

### Key File Paths

- **Entry Point**: `backend/app/main.py`
- **Database**: `backend/app/db.py`
- **Runtime Engine**: `backend/app/runtime.py`
- **Code Generation**: `backend/app/codegen.py`
- **Product Owner**: `backend/app/po.py`
- **Chatbot**: `backend/app/chatbot.py`
- **Governance**: `backend/app/governance.py`
- **Sprint Gates**: `backend/app/sprint_gate.py`
- **Browser Tests**: `backend/app/browser_test.py`
- **Rate Limiter**: `backend/app/rate_limit.py`
- **Frontend Entry**: `frontend/src/App.tsx`
- **Tests**: `backend/tests/`

### Important Constants

- `TASK_STATUSES`: 11 statuses (Todo → Cancelled)
- `AGENT_STATES`: 7 states (Idle → Completed)
- `DEV_PERMITTED`: {Active Development, Final Validation}
- `MAX_REWORK_CYCLES`: 2
- `SELF_FIX_ATTEMPTS`: 3
- `PHASE_TIMEOUT_S`: 300
- `TOTAL_BUDGET_S`: 180 (browser smoke)
- `_LLM_HEALTH_THRESHOLD`: 3 consecutive failures
- `_MAX_FILE_SIZE_BYTES`: 1MB (generated file limit)
- `_MAX_TOTAL_GENERATED_BYTES`: 10MB (total generation limit)
- `RATE_LIMIT_DEFAULT`: 300 requests per window
- `RATE_LIMIT_MUTATION`: 120 mutations per window

---

*Generated by AgentForge Architecture Review — 2026*
