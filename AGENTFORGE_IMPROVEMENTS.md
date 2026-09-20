# AgentForge — Improvement Areas & Issues

> Non-security findings from a full codebase review. Each item includes the
> affected location, a description of the problem, and a suggested fix or
> mitigation.

---

## 1  Architecture & Code Organization

### 1.1  Monolithic `main.py` (~1300 lines)
**File:** `backend/app/main.py`

All routes, schemas, and request handlers live in a single file. There is no
router separation, no service layer, and no middleware stack beyond what
FastAPI provides out of the box.

**Impact:** Hard to navigate, hard to test in isolation, merge conflicts are
likely when multiple people work on different feature areas.

**Suggested fix:** Split into router modules (e.g. `routers/projects.py`,
`routers/agents.py`, `routers/sprints.py`) mounted on the app in `main.py`.
Move business logic that spans multiple routes (e.g. task transitions, sprint
activation) into a service layer.

### 1.2  No Service / Domain Layer
**Files:** all `backend/app/*.py`

Business logic is embedded directly in route handlers and in `runtime.py`,
`po.py`, `sprint_gate.py`. There is no intermediate layer between the HTTP
boundary and the data-access layer.

**Impact:** Logic is duplicated across routes, chatbot commands, and the
runtime. Changes require touching multiple files.

**Suggested fix:** Extract shared domain logic (task transitions, sprint
state transitions, agent assignment) into dedicated service functions that
all callers use.

### 1.3  Duplicate LLM Call Code
**Files:** `codegen.py:call_llm`, `main.py:_live_chat`, `chatbot.py:_call_llm`

Three separate functions build HTTP requests to LLM gateways with nearly
identical payload/header construction, differing only in Anthropic vs OpenAI
format.

**Impact:** Bug fixes or feature additions (retries, streaming, timeouts)
must be applied three times.

**Suggested fix:** Consolidate into a single `GatewayClient` class in
`codegen.py` that both the runtime and the chat routes use.

---

## 2  Error Handling & Observability

### 2.1  Silent Exception Swallowing
**Files:** `db.py`, `po.py`, `runtime.py`, `chatbot.py`, `main.py:lifespan`

Bare `except Exception:` blocks appear throughout the codebase. In many cases
the exception is discarded entirely:

```python
# db.py:572 — gateway key decryption failure
try:
    return _secrets.decrypt(row["api_key_enc"])
except Exception:
    return None          # silently loses the real error

# main.py:lifespan — PO resume at startup
try:
    ...
except Exception:
    pass                # silently ignores ALL startup failures
```

**Impact:** Real problems (bad encryption, DB corruption, network issues) are
invisible. Debugging production issues requires attaching a debugger.

**Suggested fix:** Log every caught exception at minimum. For startup code,
distinguish between "expected" (no PO projects) and "unexpected" (DB
corruption) failures.

### 2.2  No Structured Logging
**Files:** all backend modules

There is no `logging` usage anywhere. The only visibility into runtime
behavior is the `audit` table and `events` table, which are application-level
and not a substitute for operational logging.

**Impact:** No log levels, no log formatting, no log aggregation, no
correlation IDs between requests and background tasks.

**Suggested fix:** Add `logging.getLogger(__name__)` to every module. Log
at INFO for normal operations (task started, gate passed) and WARNING/ERROR
for anomalies (LLM outage, build failure, retry). Include `run_id` and
`project_id` in every log record.

### 2.3  Raw Exception Strings Sent to Clients
**File:** `runtime.py:500-510`

```python
update("tasks", task_id, {"status": "Blocked",
                          "blocked_reason": f"Execution failed: {exc}",
                          ...})
```

The raw exception string (which may contain internal paths, stack frames,
DB schema details) is stored in `blocked_reason` and returned to the
frontend.

**Suggested fix:** Store a sanitized user-facing message and a separate
internal error detail. Show only the sanitized message in the UI.

### 2.4  SSE Connections Have No Timeout or Cleanup
**File:** `main.py` — `/api/v1/events` endpoint

The SSE endpoint streams events indefinitely. There is no heartbeat, no
client-disconnect handling, and no connection timeout.

**Impact:** Dead connections accumulate on the server. Under load this
becomes a resource leak.

**Suggested fix:** Add periodic heartbeat comments (`: ping\n\n`) and
handle `ClientDisconnect` to break the loop and clean up.

---

## 3  Concurrency & Race Conditions

### 3.1  Shared Mutable State Without Locks
**File:** `runtime.py`

```python
_schedulers: dict = {}   # modified by start_sprint_execution, stop_sprint_execution
_registry: dict = {}     # modified by start_execution, _run_phases, cancel_execution
```

`_schedulers` and `_registry` are plain dicts mutated from multiple
coroutines and thread-pool callbacks without any lock or synchronization.

**Impact:** Under concurrent requests (e.g. two operators starting two
projects at once), dict mutations can interleave. The `_schedulers` dict
check in `start_sprint_execution` (line 1206) is a TOCTOU race.

**Suggested fix:** Guard all mutations with `asyncio.Lock` instances, or
use `dict` operations only from the event loop thread (they are atomic in
CPython but the TOCTOU check-then-act pattern is not).

### 3.2  Fire-and-Forget Coroutines
**File:** `runtime.py:874-884`, `runtime.py:702-707`

```python
async def _po_next_sprint():
    ...
_spawn(_po_next_sprint())   # result is never awaited or tracked
```

Multiple `_spawn()` calls create background tasks whose completion or
failure is never observed.

**Impact:** Exceptions in these coroutines are silently lost. The scheduler
loop has no way to know if the PO review succeeded or failed.

**Suggested fix:** Store task references and add a `gather`-style waiter or
at least an `add_done_callback` that logs failures.

### 3.3  No Pagination on List Endpoints
**Files:** `main.py` — `list_gateways`, `list_agents`, `list_skills`,
`list_roles`, `list_teams`, `list_projects`

All list endpoints return the full table contents. As the system scales,
response sizes grow unbounded.

**Suggested fix:** Add `limit`/`offset` query parameters (or cursor-based
pagination) to all list endpoints. The frontend should request pages.

---

## 4  Data Layer

### 4.1  No Migration System
**File:** `db.py:SCHEMA`

Schema changes are made by editing the `SCHEMA` string in `db.py` and
relying on `CREATE TABLE IF NOT EXISTS`. There is no version tracking,
no migration files, and no way to apply incremental changes to an existing
database.

**Impact:** Adding/removing columns or renaming tables requires manual SQL
or a database reset, which loses all data.

**Suggested fix:** Introduce a `schema_version` table and a migration runner
(e.g. using `alembic` or a lightweight custom system).

### 4.2  No Connection Pooling or WAL Mode
**File:** `db.py`

SQLite is opened in the default mode (rollback journal). Under concurrent
reads and writes, the database hits `OperationalError: database is locked`,
which the code works around with retry+sleep.

**Impact:** Write throughput is serialized. The retry logic (`execute()`)
adds latency and still occasionally fails under high concurrency.

**Suggested fix:** Enable WAL mode (`PRAGMA journal_mode=WAL`) and
consider `PRAGMA busy_timeout=5000` instead of application-level retries.

### 4.3  `_or_404` Semantics Inconsistent
**File:** `main.py`

```python
def _or_404(row, label):
    if not row:
        raise HTTPException(404, f"{label} not found")
    return row
```

Some PATCH endpoints use it, but others (e.g. `update_backlog`) do a
manual 404 check that returns `None` instead of raising, leading to
inconsistent error responses.

**Suggested fix:** Make `_or_404` usage consistent across all mutation
endpoints, or create a decorator.

### 4.4  Events Table Grows Unbounded
**File:** `db.py` — `events` table

Events are append-only with no pruning, archiving, or retention policy.
Over a long-running project with frequent agent activity, this table will
grow to millions of rows.

**Impact:** Summary queries (e.g. `control_summary` loading the last 50
events) slow down. DB file size grows indefinitely.

**Suggested fix:** Add a periodic cleanup job that archives events older
than N days. Or cap the per-project event history and prune oldest first.

---

## 5  Reliability & Resilience

### 5.1  No Circuit Breaker for LLM Calls
**Files:** `codegen.py`, `chatbot.py`

LLM gateway calls have a 300s timeout but no circuit breaker. If the
gateway is down, every agent in every running task will timeout
independently, burning through the phase timeout budget.

**Impact:** A single LLM outage cascades into every in-flight task failing
simultaneously.

**Suggested fix:** Add a lightweight circuit breaker per gateway. After N
consecutive failures, short-circuit further calls and surface the outage
immediately rather than waiting for timeouts.

### 5.2  Scheduler Idle Give-Up Thresholds Are Magic Numbers
**File:** `runtime.py:912-928`

```python
give_up_after = 60 if blocked else 600
if idle_rounds > give_up_after:
    break
```

The scheduler gives up after 60 idle rounds (with blocked tasks) or 600
rounds (without). Each round sleeps 2 seconds, so this is 2 minutes or 20
minutes of idle time.

**Impact:** Operators have no visibility into why the scheduler stopped.
The threshold is arbitrary and not configurable.

**Suggested fix:** Make the thresholds configurable (project settings or
env vars) and emit a clear event when the scheduler gives up so the UI
can surface it.

### 5.3  Orphan Recovery Is Best-Effort
**File:** `runtime.py:1283-1301`

`recover_orphans()` marks interrupted runs as Failed and resets agents to
Idle. But it does not re-create the scheduler for the project, and it does
not check if the sprint is mid-gates (which needs a different recovery path).

**Impact:** After a server restart, projects that were mid-sprint-gate
pipeline silently lose progress. The sprint status stays in a gate phase
with no pipeline running.

**Suggested fix:** After `recover_orphans()`, check for sprints stuck in
gate phases and either auto-resume the gate pipeline or notify the operator.

### 5.4  `_live_model_ids` Silently Returns `None`
**File:** `main.py:418-424`

```python
except Exception:
    return None
```

When fetching model IDs from an OpenAI-compatible endpoint, any error
results in `None`. The caller cannot distinguish "no models listed" from
"endpoint unreachable".

**Suggested fix:** Return a tri-state result (success/empty/error) so the
caller can surface meaningful diagnostics.

---

## 6  API Design

### 6.1  PATCH Endpoints Accept Unconstrained `dict`
**Files:** `main.py` — `update_gateway`, `update_role`, `update_persona`,
`update_skill`, `update_project`, `update_sprint`, `update_backlog`

```python
@app.patch("/api/v1/gateways/{gid}")
def update_gateway(gid: str, body: dict):
```

PATCH endpoints accept `body: dict` with manual field filtering inside the
handler. This bypasses Pydantic validation, provides no OpenAPI schema, and
gives no client-side type safety.

**Suggested fix:** Define Pydantic models for each update operation
(e.g. `GatewayUpdate`, `RoleUpdate`) with optional fields.

### 6.2  No Rate Limiting
**File:** `main.py`

No rate limiting on any endpoint. The SSE endpoint and chat endpoints are
particularly vulnerable to abuse or accidental overload.

**Suggested fix:** Add a lightweight rate limiter (e.g. `slowapi`) on
expensive endpoints (LLM calls, gateway tests, SSE connections).

### 6.3  No Request Idempotency Keys (Except start_execution)
**File:** `main.py:1330`

Only `start_execution` accepts an `Idempotency-Key` header. All other
mutation endpoints (create project, create sprint, add task) are vulnerable
to duplicate creation on retry.

**Suggested fix:** Either add idempotency keys to all create endpoints, or
document that clients should check-before-create.

### 6.4  Cost Estimate Formula Is Hardcoded
**File:** `runtime.py:466`

```python
cost = round(in_tokens / 1_000_000 * 2.0 + out_tokens / 1_000_000 * 8.0, 4)
```

Token costs are fixed at `$2/M input, $8/M output`. This is only accurate
for a specific model tier and will be wrong for cheaper or more expensive
models.

**Suggested fix:** Store per-model pricing in the `gateway_models` table or
a config, and look it up at usage-record time.

---

## 7  Frontend

### 7.1  Redundant Polling + SSE
**File:** `frontend/src/pages/ControlPage.tsx`

The Control page polls `GET /projects/{id}/summary` every 3 seconds while
also maintaining an SSE connection to `/events`. Both channels deliver the
same state changes.

**Impact:** Double the server load. Events and polling can arrive out of
order, causing UI flicker.

**Suggested fix:** Use SSE as the primary source of truth. Use polling only
as a fallback when SSE is disconnected (with exponential backoff).

### 7.2  All State Is Local Component State
**File:** `frontend/src/pages/ControlPage.tsx` (~760 lines)

The Control page manages agent states, task lists, sprint status, events,
conversations, and modals entirely through local `useState` hooks. There is
no global store (Redux, Zustand, Jotai).

**Impact:** State is passed through prop chains, making it hard to share
state between components (e.g. sidebar and main panel). Re-renders are
expensive on a 760-line component.

**Suggested fix:** Introduce a lightweight global store for project state
and split the Control page into smaller, focused components.

### 7.3  Minified Bundle Without Source Maps
**File:** `backend/static/assets/index-9dpEl99J.js`

The frontend is served as a single minified React bundle with no source
maps. Debugging frontend issues in production is impossible.

**Suggested fix:** Build a separate source map file and serve it in
non-production environments.

### 7.4  No Request Cancellation on Unmount
**File:** `frontend/src/api.ts`, `frontend/src/pages/ControlPage.tsx`

Fetch requests started in `useEffect` or event handlers are not cancelled
when the component unmounts. This can cause state updates on unmounted
components (React warnings) and unnecessary server load.

**Suggested fix:** Use `AbortController` and clean up in effect cleanup
functions.

---

## 8  Configuration & Hardcoded Values

### 8.1  Configuration Is Split Between DB and Code
**Files:** `runtime.py`, `toolchains.py`, `main.py`, `po.py`

Many behavioral constants are hardcoded:

| Constant | Location | Value |
|---|---|---|
| `PHASE_TIMEOUT_S` | `runtime.py:89` | 300 |
| `SELF_FIX_ATTEMPTS` | `runtime.py` | 3 |
| `MAX_REWORK_CYCLES` | `runtime.py` | 2 |
| `MAX_GATE_CYCLES` | `sprint_gate.py:70` | 3 |
| LLM cost rate | `runtime.py:466` | $2/$8 per M tokens |
| Poll interval | `ControlPage.tsx` | 3000ms |
| Sleep per phase | `runtime.py:290` | 2.5–4.5s random |
| Idle give-up | `runtime.py:925-927` | 60 / 600 rounds |
| PO tick interval | `runtime.py:914` | every 10 rounds |

**Impact:** Tuning behavior for different project sizes or environments
requires code changes and redeployment.

**Suggested fix:** Move operational constants into a config module or
database-backed `project_settings` table.

### 8.2  No Environment Profiles
**Files:** `backend/app/main.py`, `backend/app/db.py`

The same code runs in all environments. The DB path is `agent_office.db`
in the working directory. There is no way to configure dev vs. prod
behavior without changing code.

**Suggested fix:** Use `pydantic-settings` or `environ-config` for
environment-based configuration (DB path, log level, LLM timeouts, etc.).

---

## 9  Testing

### 9.1  No Backend Tests
**Files:** `backend/app/` — no `test_*.py` files

There are zero automated tests for the backend. The only test-like code is
a string template in `codegen.py` (`_STARTER_HELPERS_TEST`) that gets
written into generated project files, not executed.

**Impact:** Every change to the sprint gate pipeline, runtime, PO engine,
or chat parser is made without regression safety.

**Suggested fix:** Add pytest tests for:
- Sprint gate state machine transitions
- `authorize_task` gatekeeper logic
- `po._apply_actions` (each action type)
- `toolchains.detect_stack` and `build_check`
- `recover_orphans` behavior
- Task transition validation (`TASK_TRANSITIONS`)

### 9.2  Frontend Tests Are Absent
**File:** `frontend/src/` — no test files

No unit tests, integration tests, or E2E tests for the React frontend.

**Suggested fix:** Add Vitest for unit tests and Playwright or Cypress
for E2E smoke tests covering the Control page SSE flow and chat commands.

---

## 10  Resilience Edge Cases

### 10.1  `_resume_po_projects` Calls `start_sprint_execution` Unconditionally
**File:** `main.py:36-44`

At startup, if `po_enabled` is set and there are open tasks, the code calls
`runtime.start_sprint_execution(p["id"])` for every such project. But
`start_sprint_execution` returns an error if a scheduler is already
running (e.g. the previous instance is still shutting down), and the error
is silently swallowed by `except Exception: pass`.

**Impact:** PO-enabled projects may silently not resume after a restart.

**Suggested fix:** Check `project_id in runtime._schedulers` before
calling `start_sprint_execution`. Log a warning if the resume fails.

### 10.2  `auto_assign_tasks` Skips Wrong-Specialist Tasks
**File:** `runtime.py:1097-1098`

```python
if not pick or pick["lifecycle_state"] != "Idle":
    continue  # busy specialist: leave for the next round, never queue
```

When a task requires a specialist role (e.g. QA) but the only QA agent is
busy, the task is left unassigned indefinitely — it is never reassigned
even after the QA agent finishes.

**Impact:** Tasks can stall indefinitely waiting for a busy specialist with
no fallback or retry mechanism.

**Suggested fix:** Either queue the task for the next assignment round, or
allow generalist agents as fallback for specialist tasks with a warning.

### 10.3  Workspace Path Validation Is Missing
**File:** `backend/app/workspace.py`

When a project's `workspace_path` is set or updated, there is no validation
that the path is within the expected workspace root or that it is writable.

**Impact:** A misconfigured `workspace_path` could point outside the
workspace root, or to a read-only directory, causing silent failures during
code generation.

**Suggested fix:** Validate workspace paths on creation/update: ensure they
are within the configured workspace root and are writable.

---

## 11  Performance

### 11.1  `control_summary` Loads All Events
**File:** `main.py` — `control_summary` endpoint

The summary endpoint loads the last 100 events for every project on every
request (every 3s from the frontend). As event volume grows, this becomes
a hot query.

**Suggested fix:** Cache the summary with a short TTL (1-2s), or push
incremental updates via SSE and let the frontend maintain state.

### 11.2  No Indexing Beyond Primary Keys
**File:** `db.py:SCHEMA`

The schema defines primary keys but no secondary indexes. Queries like
`SELECT * FROM tasks WHERE project_id = ? AND status = ?` scan the full
tasks table.

**Impact:** As task count grows, task lookups, sprint gating, and event
queries slow down.

**Suggested fix:** Add indexes on `(project_id, status)`, `(project_id,
sprint_id)`, `(project_id, created_at)` for the most common query patterns.

---

## 12  Frontend-Specific

### 12.1  No Error Boundary Recovery
**File:** `frontend/src/ErrorBoundary.tsx`

The error boundary catches render errors but only shows a static message.
There is no retry button or error reporting integration.

**Suggested fix:** Add a "Retry" button that resets the error boundary
state. Consider integrating with an error reporting service (Sentry).

### 12.2  No Offline / Degraded Mode
**File:** `frontend/src/api.ts`

If the backend is unreachable, the frontend shows a generic error. There
is no cached state, no offline queue, and no degraded-mode UI.

**Suggested fix:** Cache the last-known project state in `localStorage`.
Show stale data with a "disconnected" indicator rather than a blank page.

### 12.3  `ControlPage` Re-renders on Every Event
**File:** `frontend/src/pages/ControlPage.tsx`

The SSE callback appends to a state array, triggering a full re-render of
the 760-line component on every event. With high-frequency events (tool
started/completed, phase transitions), this causes visible jank.

**Suggested fix:** Memoize event list rendering, or use a virtualized list
for the activity feed. Split the page into sub-components that re-render
independently.

---

## Summary by Severity

| Severity | Count | Key Items |
|---|---|---|
| **High** | 4 | Monolithic main.py, no tests, SSE leak, race conditions in shared state |
| **Medium** | 8 | Silent exceptions, no pagination, hardcoded costs, no circuit breaker, unbounded events table, missing indexes |
| **Low** | 7 | Minified bundle, magic numbers, no env profiles, polling+SSE redundancy, no offline mode |
