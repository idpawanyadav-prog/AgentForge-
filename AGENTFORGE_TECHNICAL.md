# AgentForge — Technical Reference

> **Purpose:** give an AI coding agent everything it needs to understand the
> AgentForge project, move around the codebase confidently, and make correct
> changes without guessing.

---

## 1  High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         Frontend (React + Vite)                          │
│   Pages: Projects / Teams / Memory / Control / Settings                 │
│   Talks to the API over HTTP/REST + SSE (Server-Sent Events).           │
└────────────────────────────┬────────────────────────────────────────────┘
                             │ HTTP + SSE
┌────────────────────────────▼────────────────────────────────────────────┐
│                    Backend (FastAPI, single-file app)                    │
│   main.py     — routers, schemas, CQRS-lite request handling            │
│   db.py       — raw-SQLite data-access layer                            │
│   runtime.py  — simulated agent execution runtime                       │
│   sprint_gate.py — sprint gating pipeline + gatekeeper                  │
│   po.py       — Product Owner autonomous authority engine               │
│   chatbot.py  — Project Control chat (NL command parser)                │
│   codegen.py  — LLM gateway abstraction + code generation               │
│   toolchains.py — build/test runners per detected stack                 │
│   workspace.py — workspace directory management                         │
│   secrets.py  — credential masking helpers                              │
└─────────────────────────────────────────────────────────────────────────┘
```

**Data store:** single SQLite file `backend/app/agent_office.db` (created
automatically by `db.init_db()`).

---

## 2  Domain Model (Database Tables)

| Table | Purpose |
|---|---|
| `projects` | Top-level container for a software project |
| `teams` | Named groups of agents |
| `roles` | Abstract role definitions (e.g. "Senior Developer", "QA Lead") |
| `personas` | Persona templates scoped to a role (instructions, constraints) |
| `skills` | Reusable instruction/skill files attachable to roles |
| `role_skills` | Join table: roles ↔ skills |
| `agents` | Concrete agent instances (Role + Persona + ModelBinding) |
| `gateways` | LLM gateway connections (OpenAI, Ollama, etc.) |
| `gateway_models` | Model catalog entries per gateway |
| `model_bindings` | Role → Gateway + Model mapping |
| `tasks` | Work items, optionally scoped to a project + sprint |
| `task_dependencies` | Directed edges between tasks |
| `sprints` | Time-boxed delivery iterations with gate columns |
| `sprint_acceptance_criteria` | Per-sprint AC rows validated during gate pipeline |
| `sprint_gate_executions` | Execution log for each gate run |
| `workflow_runs` | Durable execution record for each task run |
| `backlog_items` | Prioritised items not yet in a sprint |
| `project_settings` | Key-value project-level config |
| `settings` | Global key-value config (theme, bot configs) |
| `events` | Append-only event stream per project (SSE feed) |
| `audit` | Cross-cutting audit log |
| `conversations` | Chat conversations per project |
| `messages` | Chat messages within conversations |
| `instruction_files` | Markdown instruction files attached to roles |
| `persona_versions` | Versioned snapshots of persona instructions |
| `persona_skills` | Join table: personas ↔ skills |
| `execution_events` | Detailed execution-step log per workflow run |

### Key Relationships

- **Agent** = one **Role** + one **Persona** + one **ModelBinding**
- **Team** = many agents (with optional `role_in_team` label)
- **Project** optionally references a **Team** and a **Gateway**
- **Task** belongs to a **Project** and optionally a **Sprint**
- **Sprint** has five gate status columns: `development_status`,
  `build_status`, `test_status`, `functional_status`, `acceptance_status`

---

## 3  Frontend Structure

### Entry Point
- `frontend/src/main.tsx` — mounts the `App` component
- `frontend/src/App.tsx` — routing shell, sidebar navigation

### API Client (`frontend/src/api.ts`)
All HTTP calls go through helper functions `get`, `post`, `patch`, `put`, `del`
that wrap `fetch` with the base URL, error handling, and token support.
Also exports formatting helpers (`fmtTime`, `fmtRel`, `fmtDateTime`, `md`) and
shared constants (`AGENT_STATE_CLASS`, `TASK_STATE_CLASS`).

### Pages

| File | Route / Purpose |
|---|---|
| `ProjectsPage.tsx` | Create/edit projects; Tasks / Backlog / Sprints / Settings tabs per project |
| `TeamsPage.tsx` | Manage agents and teams (agent = Role + Persona + Model) |
| `MemoryPage.tsx` | Role-group instructions, skills, and persona editor |
| `ControlPage.tsx` | **Main operational hub** — real-time task execution, agent chat, sprint gates, activity feed |
| `SettingsPage.tsx` | Gateway CRUD, model playground, audit trail, bot configuration |

### Shared Components (`frontend/src/components.tsx`)
- `Badge`, `Field`, `Modal`, `Collapse`, `ErrorNote` — UI primitives
- `useAsyncData` — React hook for loading + reloading data

---

## 4  Backend Route Map

All routes live in `backend/app/main.py` (no separate router files).

### Prefix: `/api/v1`

| Method | Path | Purpose |
|---|---|---|
| GET | `/settings` | Global settings (theme, bot configs) |
| PUT | `/settings` | Set a setting |
| PUT | `/settings/control-bot` | Configure the control-chat bot |
| PUT | `/settings/po-bot` | Configure the Product Owner bot |
| GET | `/gateways` | List gateways |
| POST | `/gateways` | Create gateway |
| PATCH | `/gateways/{id}` | Update gateway |
| DEL | `/gateways/{id}` | Delete gateway |
| GET | `/gateways/{id}/models` | List models for a gateway |
| POST | `/gateways/{id}/models` | Add model to gateway |
| DEL | `/gateways/{id}/models/{mid}` | Delete model |
| POST | `/gateways/{id}/test` | Test gateway connectivity + sync models |
| POST | `/gateways/{id}/chat` | Live playground chat with a model |
| GET | `/roles` | List role groups |
| POST | `/roles` | Create role group |
| PATCH | `/roles/{id}` | Update role group |
| DEL | `/roles/{id}` | Delete role group (deactivate if in use) |
| GET | `/roles/memory` | Roles with memory counts |
| GET | `/roles/{id}/instructions` | List instruction files for a role |
| POST | `/roles/{id}/instructions` | Create instruction file |
| PATCH | `/instructions/{id}` | Update instruction file |
| DEL | `/instructions/{id}` | Delete instruction file |
| GET | `/roles/{id}/skills` | List skills attached to a role |
| POST | `/roles/{id}/skills/{sid}` | Attach skill to role |
| DEL | `/roles/{id}/skills/{sid}` | Detach skill from role |
| GET | `/personas` | List personas (optionally filtered by role) |
| POST | `/roles/{role_id}/personas` | Create persona under a role |
| PATCH | `/personas/{id}` | Update persona (versions on instruction change) |
| DEL | `/personas/{id}` | Delete persona (deactivate if in use) |
| GET | `/personas/{id}/skills` | Skills attached to a persona |
| POST | `/personas/{id}/skills/{skill_id}` | Attach skill to persona |
| DEL | `/personas/{id}/skills/{skill_id}` | Detach skill from persona |
| GET | `/skills` | List all skills |
| POST | `/skills` | Create skill |
| PATCH | `/skills/{id}` | Update skill |
| DEL | `/skills/{id}` | Delete skill |
| POST | `/model-bindings` | Create role → gateway + model binding |
| DEL | `/model-bindings/{id}` | Delete binding |
| GET | `/agents` | List all agents (with role, persona, model) |
| POST | `/agents` | Create agent |
| PATCH | `/agents/{id}` | Update agent (lifecycle state restricted) |
| DEL | `/agents/{id}` | Delete agent (only if Idle or Failed) |
| POST | `/agents/{id}/restart` | Restart an agent (reset lifecycle state) |
| GET | `/teams` | List teams (with member agents) |
| POST | `/teams` | Create team |
| PATCH | `/teams/{id}` | Update team |
| DEL | `/teams/{id}` | Delete team (only if not assigned to project) |
| GET | `/projects` | List projects |
| POST | `/projects` | Create project (prepares workspace) |
| PATCH | `/projects/{id}` | Update project |
| DEL | `/projects/{id}` | Delete project (cascading) |
| GET | `/projects/{id}/summary` | **Control page summary** — full project snapshot |
| GET | `/projects/{id}/backlog` | Backlog items for a project |
| POST | `/projects/{id}/backlog` | Create backlog item |
| PATCH | `/backlog/{id}` | Update backlog item |
| DEL | `/backlog/{id}` | Delete backlog item |
| POST | `/backlog/{id}/promote` | Promote backlog item to task in a sprint |
| GET | `/projects/{id}/sprints` | Sprints for a project |
| POST | `/projects/{id}/sprints` | Create sprint |
| PATCH | `/sprints/{id}` | Update sprint |
| DEL | `/sprints/{id}` | Delete sprint |
| POST | `/sprints/{id}/activate` | Activate a sprint |
| POST | `/sprints/{id}/draft-tasks` | Auto-draft tasks from sprint goal |
| POST | `/sprints/{id}/run-gates` | Run the sprint gate pipeline |
| POST | `/sprints/{id}/reopen` | Reopen a sprint for urgent scope |
| GET | `/sprints/{id}/gates` | Sprint gate summary |
| GET | `/sprints/{id}/gate-history` | Gate execution history |
| POST | `/executions` | Start a task execution (explicit trigger) |
| POST | `/tasks/{id}/start` | Start a task execution |
| POST | `/tasks/{id}/pause` | Pause a workflow run |
| POST | `/tasks/{id}/resume` | Resume a paused run |
| POST | `/tasks/{id}/cancel` | Cancel a workflow run |
| POST | `/tasks/{id}/retry` | Retry a failed/cancelled task |
| PATCH | `/tasks/{id}/status` | Set task status directly |
| GET | `/events` | SSE endpoint — real-time event stream |
| GET | `/chat` | Control chat messages |
| POST | `/chat` | Send a chat message |
| GET | `/chat/suggestions` | Chat suggestion prompts |
| POST | `/po-chat` | Product Owner chat messages |
| GET | `/po-chat/suggestions` | PO chat suggestions |
| POST | `/po-enable` | Enable PO autonomous authority |
| POST | `/po-disable` | Disable PO authority |
| GET | `/audit` | Audit log entries |

---

## 5  Core Execution Flow

### 5.1  Task Execution Lifecycle (`runtime.py`)

```
start_execution(project_id, task_id)
    │
    ├── validate_task_ready()  →  task must be Todo/Ready, assigned, deps met
    ├── sprint_gate.authorize_task()  →  task must belong to active sprint
    ├── Create workflow_run (status = Running)
    ├── Update task → "In Progress", progress = 5
    ├── Emit events: workflow.started, task.status_changed
    │
    └── _run_phases(run_id)  [asyncio loop]
            │
            ├── For each phase in PHASES (analyze → plan → implement → build-test → review → finalize):
            │   ├── Sleep (simulated work)
            │   ├── Optionally call LLM via codegen.call_llm() (for plan, implement steps)
            │   ├── Write files to workspace via toolchains
            │   ├── Run build + tests via toolchains
            │   ├── Emit: workflow.phase, tool.started/completed, usage.recorded
            │   └── Update task progress
            │
            ├── On build/test failure:
            │   ├── Retry implementation up to SELF_FIX_ATTEMPTS (3) times
            │   ├── If still failing → task → "Blocked" (not sent to QA)
            │
            └── On successful completion:
                ├── Task → "Review"
                ├── If QA agent assigned → task → "Waiting QA"
                │   └── _run_qa_phases() runs QA_PHASES via QA-role agent
                │       ├── Verdict: "Pass" → task → "Done"
                │       └── Verdict: "Fail" → task → "Rework"
                │           └── Retry up to MAX_REWORK_CYCLES (2), then escalate to human
                └── If no QA agent → task → "Done"
```

### 5.2  Workflow States (Task Statuses)

```
Todo → Ready → In Progress → Review → Testing → Done
                ↑              ↓         ↓
                └── Blocked    └── Waiting QA → Rework → (loop back to In Progress)
                                └── Blocked
```

### 5.3  Agent Lifecycle States

`Idle → Working → Waiting → Blocked → Paused → Completed / Failed`

### 5.4  Sprint Execution Scheduler (`runtime.py`)

`start_sprint_execution(project_id)` spawns a long-running scheduler loop
(`_run_sprint`) that continuously:
1. Finds the active sprint and its open tasks (Todo/Ready)
2. Auto-assigns unassigned tasks to idle agents via `auto_assign_tasks()`
3. Starts execution for each assigned task
4. Waits for completions and loops

The scheduler is cancellable via `stop_sprint_execution()`. Only one scheduler
can run per project at a time (`_schedulers` dict keyed by project_id).

---

## 6  Sprint Gating System (`sprint_gate.py`)

### 6.1  Sprint Statuses

```
Planned → Ready → Active → Development Complete → Build Validation →
Automated Testing → Functional Validation → Sprint Acceptance → Completed
                                        ↑                      ↑
                                        └── Rework (loop back) └── Rework
                                        └── Failed (after 3 cycles)
```

Constants:
- `PLANNED = "Planned"`, `READY = "Ready"`, `ACTIVE = "Active"`
- `DEV_COMPLETE = "Development Complete"`, `BUILD_VALIDATION = "Build Validation"`
- `AUTOMATED_TESTING = "Automated Testing"`, `FUNCTIONAL_VALIDATION = "Functional Validation"`
- `SPRINT_ACCEPTANCE = "Sprint Acceptance"`, `COMPLETED = "Completed"`
- `REWORK = "Rework"`, `FAILED = "Failed"`, `CANCELLED = "Cancelled"`

Transitions are governed by `ALLOWED_TRANSITIONS` — a dict mapping each status
to its legal next states. `transition_sprint()` uses an optimistic `WHERE status = ?`
check so concurrent transitions are impossible.

### 6.2  Gate Pipeline (`run_sprint_gates`)

Called synchronously (via `asyncio.to_thread`). Steps:

1. **Development Complete** — all sprint tasks are `Done`
2. **Build Validation** — `toolchains.build_check()` runs the build
3. **Automated Testing** — `toolchains.run_stack_tests()` runs tests
4. **Functional Validation** — LLM validates each acceptance criterion
5. **Acceptance** — all required ACs must be `Passed`

If any gate fails:
- Sprint → `Rework` status
- Re-work tasks are auto-created
- Gate phases reset so the pipeline re-runs after rework drains
- After `MAX_GATE_CYCLES` (3) failures → sprint → `Failed` (needs human/PO)

### 6.3  Sprint Gatekeeper (`authorize_task`)

The backend enforces that tasks can only be claimed if:
- There is an active sprint
- The task belongs to that sprint (not a locked/future sprint)
- The sprint status is `Active` (not mid-gates)

### 6.4  Next Sprint Unlock

When a sprint completes, the next `Planned` sprint is automatically
transitioned to `Ready`. It stays `Ready` until the operator explicitly
activates it.

---

## 7  Product Owner Engine (`po.py`)

The Product Owner is an AI agent that can:

1. **Chat interface** — in the Control page, toggle between user chat and PO chat
2. **Autonomous authority** — when enabled (`po_enabled` on project), the PO
   periodically reviews sprint state and takes actions:
   - Reviewing and rewriting acceptance criteria
   - Assigning unassigned tasks to appropriate agents
   - Unblocking tasks with stale blocked reasons
   - Running sprint gate pipelines
   - Managing sprint transitions
   - Installing missing Python libraries when tasks hit ModuleNotFoundError

PO actions are emitted as events (`po.action`, `po.review`) and visible in
the activity feed. The PO narrates its actions into the main control chat
via `po_narrate()`.

**PO trigger points:**
- Chat message to `/po-chat` endpoint → `handle_po_message()`
- Periodic `po_autonomy_tick()` — called after each sprint gate failure
- Enabling PO authority → immediate initial review

**PO actions available** (from `PO_SYSTEM_PROMPT`):
- `update_project`, `add_backlog`, `create_sprint`, `add_task`
- `update_task`, `assign_task`, `install_library`, `unblock_task`
- `cancel_task`, `start_sprint`, `stop_sprint`

---

## 8  Project Control Chat (`chatbot.py`)

The Control page has a chat interface that understands natural-language
commands for:

- Creating/starting/pausing/cancelling sprints
- Creating projects, teams, agents
- Assigning tasks
- Querying agent and task status
- Running builds and tests
- Running sprint gates
- Hiring agents, building teams, aligning teams to projects
- Adding urgent tasks mid-sprint

When a control bot is configured (gateway + model in settings), the chatbot
uses the LLM to parse commands. Without a bot, only typed slash commands work.
Sensitive commands (gateways, agents, teams) require user confirmation.

---

## 9  Key Code Patterns

### Database Layer (`db.py`)
All queries use raw SQL via helper functions:
- `query(sql, params)` → list of dicts
- `query_one(sql, params)` → single dict or None
- `execute(sql, params)` → rowcount (with retry on SQLite lock)
- `insert(table, dict)` → inserts, returns new ID
- `update(table, id, dict)` → updates by primary key
- `emit_event(project_id, event_type, payload)` → appends to `events` table
- `audit(action, resource_type, resource_id, summary)` → appends to `audit`

### Event System
- Events are stored in the `events` table with per-project `seq` numbers
- The frontend consumes them via SSE at `/api/v1/events`
- Events are the backbone of the real-time UI: agent states, task changes,
  workflow progress, sprint gates, PO actions, usage records

### LLM Abstraction (`codegen.py`)
- `call_llm(gw, model, system, user)` → sends a chat completion
- `resolve_llm(project)` → picks gateway + model for a project
- `generate_implementation()` → generates real code for a task
- `is_llm_outage(error)` → detects provider outages from error text
- Supports both OpenAI-compatible and Anthropic API formats

### Toolchains (`toolchains.py`)
- `detect_stack(project)` → identifies the project's tech stack from files
- `build_check(stack, workspace)` → runs the build
- `run_stack_tests(stack, workspace)` → runs the test suite
- `failing_tests(stack, workspace)` → extracts failing test names
- `check_modules(workspace, stack)` → verifies/installs missing Python modules
- `ensure_project_env(workspace)` → creates/verifies project .venv
- `lib_install(workspace, packages, stack)` → installs into project lib/
- Supported stacks: dotnet, go, node, python

### Workspace (`workspace.py`)
- Each project gets a real folder on disk under `Projects/` (or `AGENT_OFFICE_WORKSPACES`)
- If project has a git URL, the workspace is a clone
- Agents write real deliverable files and commit them
- Per-project git locks serialize concurrent commits

### Secrets (`secrets.py`)
- API keys stored masked in DB (`_mask()` shows last 4 chars)
- Frontend never receives raw keys
- `get_gateway_key(gw_id)` returns the real key server-side only

---

## 10  Frontend Data Flow (Control Page)

```
ControlPage mounts
  │
  ├── Load summary via GET /projects/{id}/summary
  │     └── Returns: project, agents, sprint, sprint_tasks, sprint_gates,
  │                  next_locked_sprint, events, usage, active_runs,
  │                  backlog_count, scheduler_running, po
  │
  ├── SSE connection to /events
  │     └── Live events appended to liveEvents state
  │         └── Auto-scrolls activity feed
  │
  ├── Poll summary every 3s (configurable)
  │     └── Refreshes agent states, task progress, sprint status
  │
  ├── User interactions:
  │   ├── Send chat message → POST /chat
  │   ├── Start task → POST /tasks/{id}/start
  │   ├── Pause/Resume/Cancel/Retry → POST /tasks/{id}/{action}
  │   ├── Set task status → PATCH /tasks/{id}/status
  │   ├── Sprint actions → POST /sprints/{id}/{action}
  │   └── PO chat toggle → switches between /chat and /po-chat
  │
  └── Sidebar: project selector, conversation list
```

---

## 11  Memory Page Deep Dive

The Memory page manages **role-based agent memory**:

1. **Role Groups** — top-level organizational unit (e.g. "Backend Team")
2. **Instructions** — markdown files attached to a role, injected into agent
   context at runtime
3. **Skills** — reusable knowledge packages attachable to multiple roles
4. **Personas** — named persona templates (name, description, instructions,
   constraints) scoped to a role

An agent inherits the instructions + skills of its role group. Personas are
selected per-agent (each agent picks one persona from its role). Persona edits
are versioned — changing instructions creates a new `persona_versions` entry
with an incremented version number.

---

## 12  Configuration & Secrets

- Gateway API keys are stored masked (last 4 chars visible) in the database
- The `secrets.py` module provides `_mask()` for display
- Frontend never receives raw API keys from the backend
- Settings bot configs (`control_bot`, `po_bot`) store gateway_id + model_id
  as JSON strings in the `settings` table
- Workspace root is `AGENT_OFFICE_WORKSPACES` env var or `<repo>/Projects`
- All IDs are UUID hex (32 chars, no dashes)

---

## 13  Server Lifecycle (`main.py` lifespan)

1. `db.init_db()` — create all tables if they don't exist
2. `workspace.relocate_workspaces()` — migrate old workspace paths if needed
3. `runtime.recover_orphans()` — restart in-progress workflow runs after crash
4. `runtime.set_loop()` — capture the asyncio event loop
5. Background task: resume PO execution loops for all `po_enabled` projects
   with open sprint work (after 2s delay to let everything settle)

---

## 14  File Reference

| File | Approx Lines | Purpose |
|---|---|---|
| `backend/app/main.py` | ~1300 | FastAPI app, all routes, schemas |
| `backend/app/db.py` | ~430 | SQLite data access layer, schema DDL |
| `backend/app/runtime.py` | ~1300 | Simulated agent execution runtime + scheduler |
| `backend/app/sprint_gate.py` | ~600 | Sprint gating pipeline + gatekeeper |
| `backend/app/po.py` | ~590 | Product Owner autonomous engine |
| `backend/app/chatbot.py` | ~600 | Control chat NL parser |
| `backend/app/codegen.py` | ~250 | LLM gateway + code generation |
| `backend/app/toolchains.py` | ~200 | Build/test runners per tech stack |
| `backend/app/workspace.py` | ~130 | Workspace directory management |
| `backend/app/secrets.py` | — | Credential masking |
| `frontend/src/api.ts` | — | HTTP client, constants |
| `frontend/src/App.tsx` | — | Routing shell |
| `frontend/src/pages/ControlPage.tsx` | ~760 | Main control hub |
| `frontend/src/pages/ProjectsPage.tsx` | ~360 | Project + sprint + task management |
| `frontend/src/pages/TeamsPage.tsx` | ~225 | Agent + team management |
| `frontend/src/pages/MemoryPage.tsx` | ~560 | Role memory editor |
| `frontend/src/pages/SettingsPage.tsx` | ~390 | Gateways, audit, bot config |
| `frontend/src/components.tsx` | — | Reusable UI components |

---

## 15  Critical Invariants

- **One active sprint per project** — enforced by `active_sprint()` queries
  and sprint gate state machine
- **One active workflow run per task** — enforced by `active_run_for_task()`
- **One active run per agent** — enforced by `active_run_for_agent()`
- **Only Idle/Failed agents can be deleted** — enforced in `delete_agent()`
- **Only Idle/Paused lifecycle states can be set manually** — other states
  are owned by the execution runtime
- **Broken code never reaches QA** — dev self-fix loop (`SELF_FIX_ATTEMPTS=3`)
  runs before the task is handed to QA
- **No infinite gate retries** — `MAX_GATE_CYCLES=3` then sprint → Failed
- **LLM outage detection** — consecutive failures mark the provider unhealthy;
  retries stop to avoid burning quota
- **Next sprint stays locked** — `Planned` sprints cannot be activated until
  the current sprint completes all gates
