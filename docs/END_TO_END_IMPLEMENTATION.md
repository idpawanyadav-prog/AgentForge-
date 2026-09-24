# AgentForge — End-to-End Implementation Reference

## 1. System Overview

AgentForge is a local AI-powered development platform that simulates a full software
team — Product Owner, developers, QA, Solution Architects and Business Analysts —
working inside real on-disk project workspaces. A FastAPI backend drives the
execution runtime, a React frontend provides live dashboards, and all model calls
are simulated or delegated to configured LLM gateways so the platform is fully
demoable without real provider credentials.

### 1.1 Core value proposition

| Feature | What it does |
|---|---|
| Real workspaces | Every project has a real git-backed folder on disk |
| Multi-stack QA | Python / .NET / Go / Node build checks and test runs |
| Lifecycle gates | SA and BA review gates before QA, sprint gating, rework loops |
| Run isolation | Each task/QA run works on a temp copy; merges back atomically |
| Model failover | Gateway + model chains with automatic fallback |
| Persistent memory | Project-wide lesson ledger prevents repeating past mistakes |
| Playwright smoke | Headless browser boot + smoke test on generated apps |
| Browser controls | Pause, resume, cancel and retry any running task |

### 1.2 Technology stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+, FastAPI, SQLite, Pydantic v2 |
| Frontend | React 18, TypeScript, Vite, Zustand, React Router, Vitest |
| Testing | pytest, Playwright, Vitest |
| Agents | Simulated runtime with real LLM fallback when configured |
| Build toolchains | pip, npm, dotnet CLI, go toolchain |
| Browser QA | Playwright (headless Chromium) |

---

## 2. Repository Layout

```
AgentForge/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py              # FastAPI app, lifecycle, CORS, startup hooks
│   │   ├── config.py            # Environment-driven config (timeouts, costs, retries)
│   │   ├── db.py                # SQLite connection, helpers, encryption, audit
│   │   ├── schemas.py           # Pydantic request/response schemas
│   │   ├── runtime.py           # Agent execution runtime (phases, self-fix, rework)
│   │   ├── codegen.py           # Code generation + review verdict (real or scaffold)
│   │   ├── chatbot.py           # Project Control assistant (typed commands + AI)
│   │   ├── workspace.py         # On-disk workspaces, run isolation, merge-back
│   │   ├── toolchains.py        # Multi-stack build checks, test runs, module install
│   │   ├── browser_test.py      # Playwright headless smoke tests
│   │   ├── sprint_gate.py       # Sprint lifecycle gating (open/locked/future)
│   │   ├── governance.py        # V3 lifecycle state gate (opt-in per project)
│   │   ├── memory.py            # Project memory / pitfall ledger
│   │   ├── po.py                # Product Owner autonomy module
│   │   ├── task_registry.py     # Background task lifecycle (startup shutdown)
│   │   ├── rate_limit.py        # Per-gateway rate limiting
│   │   ├── logging_config.py    # Structured logging setup
│   │   ├── llm/
│   │   │   ├── health.py        # Gateway health probes (real HTTP)
│   │   │   └── resolver.py      # Model resolution, failover chain logic
│   │   ├── routers/
│   │   │   ├── _util.py         # or_404 helper
│   │   │   ├── projects.py      # Project CRUD, sprints, backlog, tasks
│   │   │   ├── tasks.py         # Task actions, summary, runs, pause/resume/cancel
│   │   │   ├── agents.py        # Agent CRUD, team management, lifecycle
│   │   │   ├── roles.py         # Roles, personas, skills, instruction files
│   │   │   ├── gateways.py      # LLM gateway + model CRUD, health checks
│   │   │   ├── models.py        # Model binding + chain membership CRUD
│   │   │   ├── chat.py          # SSE + REST chat endpoints
│   │   │   ├── events.py        # Global SSE event stream
│   │   │   ├── settings.py      # Global settings, key management
│   │   │   └── governance.py    # Governance REST endpoints
│   │   ├── migrations/          # Schema migration scripts (001–012)
│   │   └── services/
│   │       ├── tasks.py         # Task service layer (planning, assignment)
│   │       └── sprints.py       # Sprint service layer
│   ├── tests/                   # Backend pytest suite
│   ├── static/                  # Built frontend assets (served by FastAPI)
│   └── Projects/                # Default workspace root (runtime-generated)
├── frontend/
│   └── src/
│       ├── main.tsx             # React entry point
│       ├── App.tsx              # Router shell + layout
│       ├── store.ts             # Zustand global state (persisted)
│       ├── validation.ts        # Zod schemas for frontend form validation
│       ├── MarkdownText.tsx     # XSS-safe markdown renderer
│       ├── ErrorBoundary.tsx    # React error boundary
│       ├── offline.tsx          # Offline detection hook
│       ├── components.tsx       # Shared UI components (Field, Tabs, Modal)
│       ├── pages/
│       │   ├── ControlPage.tsx  # Main control panel (chat + task board)
│       │   ├── ProjectFlowPage.tsx  # Project lifecycle flow diagram
│       │   ├── ProjectsPage.tsx # Project list + creation
│       │   ├── TeamsPage.tsx    # Team management
│       │   ├── ModelsPage.tsx   # Gateway + model binding management
│       │   ├── MemoryPage.tsx   # Project memory / pitfall ledger
│       │   └── SettingsPage.tsx # Settings, toolchains, instructions
│       └── *.test.tsx           # Vitest unit tests
├── docs/                        # Design and planning documents
├── README.md
└── requirements.txt / package.json
```

---

## 3. Architecture Deep-Dive

### 3.1 Backend core (`main.py`)

FastAPI mounts routers, initializes the SQLite database, and exposes the health
endpoint. Startup hooks trigger workspace recovery and event archiving. The app
serves the built frontend from `static/` in production and falls back to Vite dev
server proxy in development.

### 3.2 Data layer (`db.py`)

All data lives in a single SQLite database. The module provides:

- **Connection management**: one `sqlite3.Connection` per request via FastAPI
  dependency injection.
- **CRUD helpers**: `query`, `query_one`, `insert`, `update`, `execute`.
- **IDs and timestamps**: `new_id()` generates ULIDs; `now()` returns ISO-8601.
- **Audit logging**: every sensitive mutation writes to `audit_log`.
- **Gateway key encryption**: API keys are stored encrypted via Fernet; the
  encryption key comes from `AGENTFORGE_SECRET_KEY`.
- **Event emission**: `emit_event` writes to `execution_events` with per-project
  sequence numbers for SSE consumers.

### 3.3 Runtime (`runtime.py`)

The runtime is the heart of the platform. It implements a state-machine executor
that runs agents through a fixed phase pipeline:

**Dev phases:**
1. `analyze` — parse task requirements
2. `plan` — draft implementation plan, read memory
3. `implement` — real code generation via LLM or scaffold
4. `build-test` — build check + unit tests + browser smoke; self-fix loop
5. `review` — self-review against definition of done
6. `finalize` — record evidence, complete task

**QA phases:**
1. `verify` — review against acceptance criteria
2. `qa-test` — run test suite + browser checks
3. `qa-report` — write verdict + defect summary

**SA/BA review phases:**
1. Single `sa-review` or `ba-review` phase with LLM verdict.

**Key runtime mechanisms:**

| Mechanism | How it works |
|---|---|
| Run isolation | Each dev/QA run copies the workspace to `.af-run-ws/<run_id>/` |
| Merge-back | Changed files are atomically merged back under per-file leases |
| Self-fix loop | If build/tests/browser fail, the dev retries up to `SELF_FIX_ATTEMPTS` with the error as feedback |
| Pause/resume | A `paused` flag in the run control dict; `_wait_if_paused` yields control |
| Cancellation | `cancelled` flag checked at each phase boundary |
| Idempotency | `idempotency_key` on `workflow_runs` prevents duplicate starts |
| Claim lock | `_claim_lock` serializes concurrent start requests for the same task/agent |
| Cost tracking | Token counts are recorded with model-specific cost rates |

### 3.4 Code generation (`codegen.py`)

Two modes:

1. **LLM mode**: sends the task prompt to a configured gateway/model binding.
   Supports streaming, fallback chains, and pinning a working model per task.
2. **Scaffold mode**: generates a deterministic placeholder file when no gateway
   is configured — keeps the platform demoable without credentials.

The same module provides `review_verdict()` which runs an LLM-based SA/BA/QA
review and returns a structured verdict (approved / rejected with findings).

### 3.5 Workspace management (`workspace.py`)

- `prepare_workspace()`: clones a git repo or creates a local folder.
- `begin_run_workspace()`: creates an isolated copy for each execution run.
- `merge_back_run_workspace()`: applies changed files back to the live workspace
  under per-file leases; uses a journal for crash recovery.
- `commit_all()`: serialized git commit per workspace.

### 3.6 Toolchains (`toolchains.py`)

Real build and test execution for four stacks:

| Stack | Build check | Test command |
|---|---|---|
| Python | `py_compile` each module + FastAPI boot | `pytest` |
| .NET | `dotnet build` | `dotnet test` |
| Go | `go build ./...` | `go test ./...` |
| Node.js | `npm install` + `npm run build` | `npm test` |

Module pre-flight (`check_modules`) installs missing packages into `lib/` before
any build or test runs. Stale broken test modules are quarantined automatically
so they don't poison the suite.

### 3.7 Chatbot (`chatbot.py`)

A policy-bound command agent that provides two interaction modes:

1. **Typed commands**: regex-based intent matching for structured operations
   (create team, start task, install toolchain, etc.). Sensitive mutations
   require `confirm`.
2. **AI routing**: when a gateway is configured, natural language is sent to
   the LLM for intent extraction; falls back to typed commands on failure.

The chatbot never executes raw SQL — all mutations go through domain services.

### 3.8 Product Owner (`po.py`)

An autonomous module that periodically reviews project state and can:
- Rewrite blocked tasks
- Reassign work
- Cancel stalled tasks
- Install missing modules
- Adjust sprint scope

Controlled by `PO_ENABLED`, `PO_TICK_S`, and max rework cycle limits.

### 3.9 Sprint Gate (`sprint_gate.py`)

Enforces sprint lifecycle rules:
- `Open` sprints: tasks can be claimed and executed.
- `Locked`/future sprints: task starts are rejected with a clear message.
- Module-level health tasks auto-generate when a sprint completes with failures.

### 3.10 Governance (`governance.py`)

V3 opt-in lifecycle gate (per-project). When enabled, tasks can only execute
when the project is in a `development`-permitting state. States include
`draft`, `planning`, `development`, `testing`, `review`, `deployed`, `archived`.

### 3.11 Memory (`memory.py`)

Project-wide pitfall ledger. When QA/SA/BA rejects a task, the rejection reason
is recorded as a "pitfall". Future code generation prompts include recent
pitfalls so agents don't repeat the same mistakes.

### 3.12 Frontend

- **State management**: Zustand with `persist` middleware for localStorage.
- **Routing**: React Router v6 with five routes (Control, Flow, Projects, Teams,
  Models, Memory, Settings).
- **Markdown**: `MarkdownText` renders `**bold**` and `` `code` `` only — no HTML
  injection.
- **Communication**: REST for CRUD; SSE (`/api/v1/events`) for live workflow
  events.

### 3.13 Event system (`routers/events.py`)

Global SSE endpoint that polls `execution_events` every 2 seconds and streams
new rows to connected clients. Heartbeats every 30 seconds keep connections
alive. A bounded `asyncio.Queue` per connection drops old events when the client
falls behind.

---

## 4. Data Model (Key Tables)

| Table | Purpose |
|---|---|
| `projects` | Project metadata, workspace path, lifecycle state |
| `sprints` | Sprint definitions (name, status, capacity) |
| `backlog_items` | Product backlog with priority, points, acceptance criteria |
| `tasks` | Work items with status, assignee, rework count, evidence |
| `task_dependencies` | Inter-task dependency graph |
| `task_reviews` | SA/BA review records (pending, approved, rejected) |
| `agents` | Agent config (name, role, persona, model binding, state) |
| `roles` | Role definitions (Developer, QA, SA, BA, PO, etc.) |
| `personas` | Persona configs (prompts, constraints) |
| `skills` | Reusable skill definitions |
| `teams` | Team definitions (members, aligned project) |
| `gateways` | LLM provider endpoints (URL, API type, status) |
| `gateway_models` | Available models per gateway |
| `model_bindings` | Agent-to-model mappings |
| `model_bindings_members` | Failover chain members for a binding |
| `workflow_runs` | Execution run records (status, mode, step, progress) |
| `execution_events` | Ordered event log per project (SSE source) |
| `usage_records` | Token usage and cost per run |
| `conversations` | Chat conversations per project |
| `chat_messages` | Chat message history |
| `project_memory` | Pitfall ledger and other project memories |
| `instruction_files` | Role-specific instruction documents |
| `audit_log` | Audit trail for sensitive operations |
| `pending_commands` | Chatbot pending command queue |
| `project_governance` | V3 lifecycle state per project |

---

## 5. Configuration

All tunable parameters live in `backend/app/config.py` and are read from
environment variables with sensible defaults:

| Variable | Default | Purpose |
|---|---|---|
| `AGENTFORGE_DB_PATH` | `agentforge.db` | SQLite database file path |
| `AGENTFORGE_SECRET_KEY` | (required) | Fernet encryption key for gateway API keys |
| `AGENTFORGE_MAX_CONCURRENT_RUNS` | `4` | Parallel execution limit |
| `MAX_REWORK_CYCLES` | `3` | Max QA rejection cycles before escalation |
| `SELF_FIX_ATTEMPTS` | `2` | Dev self-test retry count |
| `PHASE_TIMEOUT_S` | `300` | Per-phase execution timeout |
| `LLM_INPUT_COST_PER_M` | `0` | Default input cost per million tokens |
| `LLM_OUTPUT_COST_PER_M` | `0` | Default output cost per million tokens |
| `SCHEDULER_IDLE_GIVE_UP_BLOCKED` | `18` | Scheduler idle rounds before giving up on blocked tasks |
| `SCHEDULER_IDLE_GIVE_UP_CLEAN` | `6` | Scheduler idle rounds before clean-up exit |
| `PO_ENABLED` | `false` | Enable Product Owner autonomy |
| `PO_TICK_S` | `120` | PO autonomy check interval |
| `AGENTFORGE_PROJECT_WORKSPACE_BYTES` | `1GB` | Per-project workspace quota |
| `AGENTFORGE_WORKSPACES` | `<repo>/Projects` | Workspace root directory |
| `GOVERNANCE_ENFORCEMENT` | `false` | Enable V3 lifecycle gate enforcement |
| `AGENTFORGE_BACKEND_URL` | `http://localhost:8000` | Backend URL for frontend proxy |
| `AGENTFORGE_FRONTEND_URL` | `http://localhost:5173` | Frontend dev server URL |

---

## 6. API Surface (Summary)

All routes are mounted under `/api/v1`.

### Projects
- `GET /projects` — list
- `POST /projects` — create
- `GET /projects/{pid}` — detail
- `PATCH /projects/{pid}` — update
- `DELETE /projects/{pid}` — delete
- `GET /projects/{pid}/sprints` — list sprints
- `POST /projects/{pid}/sprints` — create sprint
- `GET /projects/{pid}/backlog` — list backlog items
- `POST /projects/{pid}/backlog` — add backlog item

### Tasks
- `GET /tasks` — list (with status/agent filters)
- `GET /tasks/{tid}` — detail
- `PATCH /tasks/{tid}` — update
- `POST /tasks/{tid}/start` — start execution
- `POST /tasks/{tid}/pause` — pause run
- `POST /tasks/{tid}/resume` — resume run
- `POST /tasks/{tid}/cancel` — cancel run
- `GET /tasks/{tid}/runs` — execution history

### Agents
- `GET /agents` — list
- `POST /agents` — create
- `GET /agents/{aid}` — detail
- `PATCH /agents/{aid}` — update
- `DELETE /agents/{aid}` — delete
- `POST /teams` — create team
- `POST /teams/{tid}/members` — add member
- `POST /teams/build` — build team from roles

### Roles / Personas / Skills
- `GET/POST/PATCH/DELETE /roles`
- `GET/POST/PATCH/DELETE /personas`
- `GET/POST/PATCH/DELETE /skills`
- `GET/POST /roles/{rid}/instructions`

### Gateways / Models
- `GET/POST/PATCH/DELETE /gateways`
- `POST /gateways/{gid}/test` — health check
- `POST /gateways/{gid}/discover` — live model discovery
- `GET/POST/PATCH/DELETE /models`
- `POST /models/{mid}/members` — add chain member
- `POST /models/{mid}/reorder` — reorder chain

### Chat
- `GET /projects/{pid}/conversations` — list
- `POST /projects/{pid}/conversations` — create
- `POST /conversations/{cid}/messages` — send message (sync)
- `POST /conversations/{cid}/messages/async` — send message (async)
- `GET /events` — global SSE event stream

### Settings
- `GET /settings` — global settings
- `PATCH /settings` — update settings
- `POST /settings/toolchains/install` — install toolchain (approval-gated)
- `GET /settings/instructions` — global instruction files
- `POST /settings/instructions` — create instruction

### Governance (V3)
- `GET /projects/{pid}/governance` — current state
- `PATCH /projects/{pid}/governance` — transition state
- `POST /projects/{pid}/governance/advance` — advance lifecycle
- `GET /projects/{pid}/governance/available` — available transitions

---

## 7. Execution Flow (Task Lifecycle)

```
1. User creates project → workspace prepared (git init or clone)
2. Sprint created with tasks → tasks assigned to agents
3. User (or scheduler) starts task →
   a. validate_task_ready (status, agent, dependencies, no active run)
   b. governance.may_execute (V3 lifecycle gate)
   c. sprint_gate.authorize_task (sprint must be open)
   d. Parallel guard: agent must not already have an active run
   e. Create workflow_run row (status=Running)
   f. Spawn _run_phases coroutine
4. _run_phases executes through the phase pipeline:
   a. Create isolated run workspace copy
   b. Capture baseline test failures
   c. For each phase:
      - Emit agent.activity event
      - Execute tools (file.write, test.run, browser.test, code-review)
      - file.write → real codegen (LLM or scaffold)
      - test.run → build check → self-fix loop if failed → test run → self-fix loop if failed
      - browser.test → Playwright smoke → self-fix loop if failed
      - code-review → LLM verdict (SA/BA/QA)
      - Update task status and progress
   d. Merge changed files back to live workspace
   e. Record usage (tokens, cost)
   f. Route to _finish_dev_run / _finish_qa_run / _finish_review_run
5. _finish_dev_run:
   a. If build/test passed → move to SA Review (if configured) or QA
   b. If self-test failed after SELF_FIX_ATTEMPTS → Block, PO review
   c. If QA passed → Done
   d. If QA rejected → Rework (increment rework_count, feed findings to codegen)
6. SA/BA review:
   a. LLM verdict: approved → proceed to QA
   b. Rejected → Rework with findings as feedback
7. QA review:
   a. Approved → Done
   b. Rejected → Rework (up to MAX_REWORK_CYCLES)
   c. Exceeded → Blocked (escalation)
```

---

## 8. Frontend Architecture

### 8.1 State management (Zustand)

`src/store.ts` holds:
- `activeProject` / `setActiveProject`
- `theme` / `setTheme`
- `collapsed` / `setCollapsed`
- Persisted to `localStorage` via `zustand/middleware`.

### 8.2 Routing

```tsx
<BrowserRouter>
  <AppShell>
    <Routes>
      <Route path="/"        → Navigate to /control />
      <Route path="/control" → ControlPage (chat + task board) />
      <Route path="/flow"    → ProjectFlowPage (lifecycle diagram) />
      <Route path="/projects"→ ProjectsPage />
      <Route path="/teams"   → TeamsPage />
      <Route path="/models"  → ModelsPage />
      <Route path="/memory"  → MemoryPage />
      <Route path="/settings"→ SettingsPage />
    </Routes>
  </AppShell>
</BrowserRouter>
```

### 8.3 Communication patterns

| Pattern | Implementation |
|---|---|
| REST API | Direct `fetch()` calls to `/api/v1/*` |
| Live events | `EventSource` on `/api/v1/events` with `after` cursor |
| Offline detection | `navigator.onLine` + `online`/`offline` events |
| Form validation | Zod schemas (`validation.ts`) |

### 8.4 Security

- No `dangerouslySetInnerHTML` — all markdown is rendered via a custom parser
  that strips HTML tags.
- Gateway API keys are never exposed to the frontend; the backend stores them
  encrypted and only returns masks.
- Zod validation rejects `javascript:` URLs and other XSS vectors.
- React 18 with automatic JSX transform — no `__proto__` pollution vectors in
  the build chain.

---

## 9. Testing Strategy

### 9.1 Backend (pytest)

| Test file | Coverage |
|---|---|
| `test_run_workspace.py` | Workspace isolation, merge-back, lease conflicts |
| `test_browser_smoke.py` | Playwright smoke test harness |
| `test_gateway_health.py` | Health check, error mapping |
| `test_sprint_gating.py` | Sprint lifecycle enforcement |
| `test_review_chain.py` | SA/BA review gate flow |
| `test_model_resolution.py` | Failover chain, model selection |
| `test_task_registry.py` | Background task lifecycle |
| `test_run_lifecycle_recovery.py` | Orphan recovery, atomic claim, merge-back |
| `test_governance_step1.py` | Governance step 1 enforcement |
| `test_delete_and_outage_integrity.py` | FK deletes, outage classification, health API |
| `test_pitfalls_and_review_context.py` | Pitfall ledger, reviewer context, QA honesty |
| `test_codegen_happy_path.py` | Code generation happy path |
| `test_async_chat.py` | Async chatbot routing |
| `test_codegen_resource_bounds.py` | Codegen correctness and size bounds |
| `test_local_project_boundaries.py` | Project data isolation |

### 9.2 Frontend (Vitest)

| Test file | Coverage |
|---|---|
| `MarkdownText.test.tsx` | Markdown rendering, XSS prevention |
| `validation.test.tsx` | Zod schema validation, XSS in base_url |

### 9.3 Browser (Playwright)

`test_browser_smoke.py` drives a real headless Chromium against the generated app,
checking page renders, link resolution, and form submission.

---

## 10. Security Model

| Concern | Mitigation |
|---|---|
| SQL injection | Parameterized queries everywhere; no string interpolation |
| XSS | No `dangerouslySetInnerHTML`; custom markdown parser strips HTML |
| Credential exposure | Gateway API keys encrypted with Fernet; only masks returned |
| SSRF | URL scheme validation in health checks; only `http(s)` allowed |
| Path traversal | `safe_dir_name()` sanitizes workspace names; merge validates rel paths |
| Authorization | Project-scoped queries; agents validated against project teams |
| Audit trail | `audit()` records every sensitive mutation |
| Rate limiting | Per-gateway rate limiter on codegen calls |
| Event flooding | Bounded SSE queues; heartbeat keep-alive |
| Workspace quota | `MAX_PROJECT_WORKSPACE_BYTES` enforced at merge-back |
