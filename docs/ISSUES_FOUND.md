# AgentForge — Issues Found (Non-Security)

This document lists all non-security issues identified during a comprehensive
review of the AgentForge codebase. Each issue is grouped by category, severity,
and area.

## Severity Legend

- **CRITICAL** — Breaks core functionality or blocks production deployment
- **HIGH** — Significant functionality gaps, performance issues, or major bugs
- **MEDIUM** — Code quality, maintainability, or minor functional defects
- **LOW** — Cosmetic, style, or nice-to-have improvements

---

## 1. Architecture & Code Organization

### 1.1 [CRITICAL] Codegen never injects persona or instruction files into LLM prompts

**Files**: `backend/app/codegen.py`, `backend/app/runtime.py`

**Problem**: Persona instructions and role-level instruction files are stored
in the database (`personas`, `instruction_files` tables), but **no code path
passes them to the LLM**. The `generate_implementation()` function uses
hardcoded  (`_CODEGEN_SYSTEMS`, `_JR_DEV_SYSTEM`) and the
`review_verdict()` function uses hardcoded `_REVIEW_SYSTEM`. Customizing a
persona in the UI has zero observable effect on agent behavior.

**Impact**: Users can configure personas freely but the configured behavior
is never used. The agent persona system is effectively dead code.

**Fix**: Added `codegen.build_system_prompt()` which composes the
into the correct priority order (base stack + role instruction files + persona
instructions) and is used by both `generate_implementation` and `review_verdict`.

**Status**: Fixed.

---

### 1.2 [HIGH] Workspace is reused across runs without isolation

**File**: `backend/app/codegen.py:1041-1049`

**Problem**: `generate_implementation()` writes generated files into the
project's `workspace_path`. If two tasks in the same project run concurrently,
their writes can interleave and overwrite each other's intermediate work. The
runtime does have a "run_ws" concept mentioned in comments, but it's not
actually being used as isolation per task run.

**Fix Recommended**: Implement per-run workspace directories (e.g.,
`workspace_path/.runs/{run_id}/`) with automatic merge-back after task
completion. Concurrent runs would no longer collide.

---

### 1.3 [HIGH] No automatic context compaction for long-running sessions

**File**: `backend/app/chatbot.py:1511` (`_context_brief`)

**Problem**: The context brief is computed on every chatbot call but truncates
at fixed sizes (`tree[:2500]`, `ac[:600]`, etc.) without preferring recent
information. Long-running projects will lose important early context.

**Fix Recommended**: Implement windowed summary of older conversation
messages (e.g., "previous 5 task summaries: ..."), use recency-weighted
sampling for event logs.

---

### 1.4 [MEDIUM] Mixed language protocols in `_extract_object`

**File**: `backend/app/codegen.py:285` (approx)

**Problem**: Multiple LLM output parsing strategies exist but the priority
and consistency is unclear. Some strategies use regex extraction, others
JSON repair libraries.

**Fix Recommended**: Centralize LLM output parsing into a single `parsers/`
module with one robust JSON repair strategy.

---

### 1.5 [MEDIUM] Hardcoded  in `chatbot.py:1950` for AI summarization

**File**: `backend/app/chatbot.py:1950`

**Problem**: AI summarization uses `AI_SYSTEM_PROMPT` constant which is
hardcoded and disconnected from any persona system.

**Fix Recommended**: Allow summarization to use the platform-wide persona
or a dedicated summarizer persona.

---

## 2. Performance & Token Efficiency

### 2.1 [HIGH] Persona instructions and instruction files not injected at all

(See issue 1.1 — this also wastes the cost of storing and managing
personas/instructions without seeing any benefit.)

### 2.2 [HIGH] Junior Developer helper-file tokens added on every codegen call

**File**: `backend/app/codegen.py:1091`

**Problem**: The full helpers index is added to every LLM call. As the helpers
file grows, the index grows, and every call gets heavier. With 50 helpers
and ~50 tokens per helper signature, that's 2500 tokens per call just for the
helpers index.

**Fix Recommended**: 
- Paginate helpers (top 10 most-relevant by task description keyword)
- Move helpers index to a smaller "summarized" form (one-line per helper)
- Or emit a separate file only when the task is clearly a utility task

---

### 2.3 [HIGH] Project tree sent on every call, no caching

**File**: `backend/app/codegen.py:1069` (`_existing_tree`)

**Problem**: Each LLM call walks the workspace tree (via `os.walk`) and
includes it in the prompt. For workspaces with 100+ files, this is 5KB+ of
context. The tree doesn't change frequently.

**Fix Recommended**:
- Cache the workspace tree keyed by `mtime` of the workspace root
- Invalidate cache when a file is written
- For unchanged workspaces, reuse the cached tree

---

### 2.4 [HIGH] No token budget enforcement

**File**: `backend/app/codegen.py:1133` (approx)

**Problem**: LLM calls use `max_tokens=8000` by default with no per-call
budget tracking. Long coder responses can hit provider-specific output caps
without warning.

**Fix Recommended**: 
- Compute estimated token budget based on file count × average file size
- Set `max_tokens` dynamically based on task complexity
- Track estimated input + output per phase; fail early if over budget

---

### 2.5 [MEDIUM] Every retry regenerates the entire 

**File**: `backend/app/codegen.py:1133-1140`

**Problem**: When LLM retries the same task with feedback, the entire 
is recomputed and re-sent. The LLM pays the input cost again.

**Fix Recommended**: 
- Pass the previous response + feedback as additional user message
- Use prompt caching where the provider supports it (Anthropic, OpenAI)
- Send a short "see previous reply" hint when the system is unchanged

---

## 3. Error Handling & Resilience

### 3.1 [HIGH] Browser smoke test can hang indefinitely

**File**: `backend/app/browser_test.py`

**Problem**: Even with the `_wait_bounded` deadline in `runtime.py:171-176`,
the browser subprocess may have its own retries/internal timeouts that
exceed the deadline.

**Fix Recommended**: Configure Playwright with explicit `timeout_ms` and
`action_timeout_ms`; pass a separate deadline to the browser subprocess and
have it exit cooperatively.

---

### 3.2 [HIGH] `_claim_lock` held for entire claim+insert sequence

**File**: `backend/app/runtime.py:35`

**Problem**: The `_claim_lock` is held across a synchronous DB insert, which
can cause throughput bottlenecks if many tasks are claimed simultaneously.

**Fix Recommended**: Use a faster, in-memory claim mechanism (single-threaded
dispatcher) or rely on SQLite's natural serialization with a retry loop.

---

### 3.3 [HIGH] DB connection leaks under heavy load

**File**: `backend/app/db.py:24-38` (`get_db`)

**Problem**: `get_db()` caches a per-thread connection. If a thread creates
many requests without releasing the connection (e.g., long-lived background
workers in the same thread), the connection is held indefinitely.

**Fix Recommended**: 
- Document per-request release pattern
- Add explicit `close_db()` for workers
- Use connection pool (`sqlite_pool`) for the FastAPI thread pool

---

### 3.4 [MEDIUM] `setup_module` patterns with no cleanup hooks

**File**: `backend/tests/*.py`

**Problem**: Several test files use `setup_module` / `teardown_module` but
rely on global module state that isn't properly reset between test files.

**Fix Recommended**: Use pytest fixtures with `tmp_path` for database and
workspace isolation per test.

---

## 4. Database & Data Integrity

### 4.1 [HIGH] No foreign key cascade deletes

**File**: `backend/app/db.py` migrations

**Problem**: Foreign keys are enforced (`PRAGMA foreign_keys=ON`) but there
are no `ON DELETE CASCADE` clauses. Deleting a role leaves orphan
`instruction_files` and `persona_skills` rows.

**Fix Recommended**: Add cascade rules in migration, or implement soft-delete
with `active=0` for roles/personas instead of hard delete.

---

### 4.2 [HIGH] Idempotency key relies on exact hash but race in insert

**File**: `backend/app/runtime.py:331-343`

**Problem**: The `INSERT` is wrapped in `try/except IntegrityError` but the
unique constraint isn't a `UNIQUE INDEX` on `idempotency_key`; it's a
nullable column. Without an index, the race could allow duplicate runs.

**Fix Recommended**: Add `UNIQUE INDEX` on `idempotency_key` in the migration.

---

### 4.3 [MEDIUM] `audit()` calls before insert in some flows

**File**: `backend/app/db.py` (multiple uses)

**Problem**: Several `audit()` calls happen before the insert that created
the entity they're auditing. If the insert fails, the audit log references
a non-existent entity.

**Fix Recommended**: Audit after insert succeeds; use the same transaction.

---

### 4.4 [MEDIUM] No schema versioning

**File**: `backend/app/migrations/`

**Problem**: Migration files are numbered (`010_project_governance_flag.py`)
but there's no `schema_version` table tracking which migrations have run.
Re-running migrations is not idempotent in all cases.

**Fix Recommended**: Add a `schema_migrations` table with `version`, `applied_at`,
`checksum`, and check before applying.

---

### 4.5 [MEDIUM] `seed_if_empty` deletes rows from production tables

**File**: `backend/app/db.py:1015-1041`

**Problem**: The `seed_if_empty()` function DELETEs all rows from 40 tables
when `seeded` is unset. If somehow `seeded` flag is reset, the entire
production database is wiped.

**Fix Recommended**: 
- Add an explicit confirmation flag (`FORCE_RESEED=1`) before destructive
- Default to never re-seed once `seeded` is set, even if it's accidentally unset
- Use soft-delete on the data instead of `DELETE FROM`

---

## 5. Frontend

### 5.1 [HIGH] Large compiled JS bundles ship unused code

**File**: `backend/static/assets/index-CLsvpvVO.js` (970KB+)

**Problem**: The frontend bundle is one ~1MB file containing all routes and
components. No code splitting, no lazy-loaded routes, no tree-shaking audit.

**Fix Recommended**:
- Implement `React.lazy()` + `Suspense` for route-level code splitting
- Audit the bundle with `rollup-plugin-visualizer` to find largest imports
- Split vendor into a separate cacheable chunk

---

### 5.2 [HIGH] ProjectsPage contains inline state machine logic

**File**: `frontend/src/pages/ProjectsPage.tsx`

**Problem**: Hundreds of lines of inline reducer logic and API call orchestration
that would benefit from extraction into a custom hook (`useProjectsApi`) and
a state-machine library (`xstate`).

**Fix Recommended**: Extract to `useProjectsApi`, `useSprintFlow`, etc.

---

### 5.3 [MEDIUM] No optimistic UI updates for sprint/task mutations

**File**: `frontend/src/pages/ProjectsPage.tsx`

**Problem**: Every mutation triggers a full re-fetch and re-render of the
project page. The UI feels slow during multi-task updates.

**Fix Recommended**: Implement optimistic updates (assume success, rollback
on error) for status changes; keep polled/realtime updates as final state.

---

### 5.4 [MEDIUM] No pagination on long lists

**File**: `frontend/src/pages/ProjectsPage.tsx`, `TasksPage`, etc.

**Problem**: Long task lists (50+ tasks) load all at once in a single render.
Scroll performance degrades on large projects.

**Fix Recommended**: Implement `react-virtual` or `react-window` for
virtualized list rendering.

---

### 5.5 [MEDIUM] No component test coverage

**Files**: `frontend/src/components/**`, `frontend/src/pages/**`

**Problem**: No Vitest/RTL component tests. Changes to UI behavior can break
unnoticed.

**Fix Recommended**: Add at least one component test per page covering
loading, error, and success states.

---

## 6. LLM Integration

### 6.1 [HIGH] No provider-agnostic retry strategy

**File**: `backend/app/codegen.py`, `backend/app/llm/gateway_client.py`

**Problem**: Each provider has different retry semantics (rate limit, timeout,
network errors). The current code retries on `Exception` broadly.

**Fix Recommended**:
- Categorize exceptions: `RateLimitError`, `AuthError`, `TimeoutError`,
  `NetworkError`, `ContentFilterError`
- Only retry `RateLimitError` and `NetworkError` (with backoff)
- Surface `AuthError` to the admin (no retry); surface `ContentFilterError`
  as a hard failure of the task

---

### 6.2 [HIGH] No streaming responses for long-running calls

**File**: `backend/app/codegen.py:1049`

**Problem**: Each LLM call is a synchronous `urllib.request` call. Long
codegen calls wait for the full response before showing progress. The UI
shows "implementing..." but no incremental updates.

**Fix Recommended**: Use streaming SSE/HTTP streaming so the user sees
file-by-file progress as the agent writes code.

---

### 6.3 [HIGH] Token usage not surfaced back to user in real time

**File**: `backend/app/llm/resolver.py`

**Problem**: Each call records input/output tokens but the user only sees
totals after the run completes. Cost overruns are discovered after the fact.

**Fix Recommended**: 
- Emit a `usage.recorded` event after each call
- Surface per-task running cost on the UI
- Add a budget cap per project that's enforced before each call

---

### 6.4 [MEDIUM] `_llm_health` only counts consecutive failures

**File**: `backend/app/runtime.py:47`

**Problem**: A gateway can have one catastrophic failure followed by 3
healthy minutes, then fail again — but `_llm_health[gateway_id] =
consecutive_failures` would reset to 1 and never mark unhealthy.

**Fix Recommended**: Use a sliding window (e.g., failures in last N
attempts) instead of consecutive failures.

---

### 6.5 [MEDIUM] No LLM provider circuit breaker

**File**: `backend/app/runtime.py`

**Problem**: When a provider is unhealthy, the runtime tries to fall back
to the next model in the chain. But it doesn't skip providers that have
failed recently across multiple projects.

**Fix Recommended**: Per-project health tracking with global aggregation.
Persist health state across restarts.

---

## 7. Browser Smoke Testing

### 7.1 [HIGH] Browser test starts a separate Python process per call

**File**: `backend/app/browser_test.py`

**Problem**: Each `_browser_smoke` call spins up a fresh browser + app
subprocess. Cold start is 5-10s on top of test time.

**Fix Recommended**: 
- Keep a warm browser pool (one Playwright browser per worker)
- Reuse the app subprocess across test calls
- Tear down only on idle timeout

---

### 7.2 [HIGH] No cleanup of crashed app/browser subprocesses

**File**: `backend/app/browser_test.py`

**Problem**: If the app subprocess is killed unexpectedly (memory leak,
crash), zombie processes accumulate.

**Fix Recommended**:
- Use `subprocess.Popen` with a process group and explicit `terminate()`
- Register an `atexit` handler that kills all running subprocesses
- Periodically reap dead processes via `os.waitpid`

---

## 8. Project Lifecycle & Governance

### 8.1 [HIGH] Governance flag migration has no downgrade

**File**: `backend/app/migrations/010_project_governance_flag.py`

**Problem**: The migration adds a `governance_level` column but provides
no `down()` function. Rolling back the migration requires manual SQL.

**Fix Recommended**: Add a `down()` function to every migration.

---

### 8.2 [MEDIUM] No version field on `projects`

**File**: `backend/app/db.py` schema

**Problem**: Project configuration (goal, stack, workspace path) can be
edited mid-sprint. There's no audit record of what changed.

**Fix Recommended**: Add a `project_versions` table mirroring the project
record at each sprint start.

---

## 9. Documentation

### 9.1 [HIGH] No architecture diagram

**Files**: Repository root

**Problem**: There's no high-level architecture diagram showing how
FastAPI + SQLite + React + LLM providers + Playwright fit together.
New developers must read code to understand the system.

**Fix Recommended**: Add an `ARCHITECTURE.md` with a Mermaid graph of
the major components.

---

### 9.2 [HIGH] No API documentation beyond FastAPI auto-generated

**File**: `backend/app/routers/`

**Problem**: REST API is documented only via FastAPI's auto-generated
`/docs` endpoint. There's no architectural overview of why endpoints are
grouped the way they are, the lifecycle of a single API call, or the
state machine of a task.

**Fix Recommended**: Add an `API.md` describing each router group,
the canonical task lifecycle (create → claim → run → verify → complete).

---

### 9.3 [MEDIUM] Test files aren't named consistently

**File**: `backend/tests/*.py`

**Problem**: `test_critical_fixes.py`, `test_governance_step1.py`,
`test_lifecycle_fixes.py`, `test_process_improvements.py` — the naming
implies a fix PR rather than behavior.

**Fix Recommended**: Rename to describe the behavior tested
(e.g., `test_task_lifecycle.py`, `test_governance_blocks_unfunded_tasks.py`).

---

## 10. Build & Tooling

### 10.1 [HIGH] No CI/CD configuration

**File**: Repository root

**Problem**: No `.github/workflows/` (or equivalent) for CI. No automated
test runs, no lint enforcement, no dependency checks.

**Fix Recommended**: Add GitHub Actions workflow:
- `pytest` on every push
- `ruff check` for Python lint
- `npm run lint` for frontend
- `npm test` for frontend
- `npm run build` to confirm production build succeeds

---

### 10.2 [HIGH] No dependency vulnerability scanning

**File**: `backend/requirements.txt`, `frontend/package.json`

**Problem**: No `pip-audit` or `npm audit` step. Vulnerable dependencies
silently shipped.

**Fix Recommended**: Add `pip-audit` and `npm audit --production` to CI.

---

### 10.3 [MEDIUM] Frontend has no error boundary

**File**: `frontend/src/components/Layout.tsx`

**Problem**: A render error anywhere in the tree shows a blank screen
with no recovery option.

**Fix Recommended**: Add an `<ErrorBoundary>` at the route level. On
error, show a recovery UI with a "reset" button.

---

### 10.4 [MEDIUM] No env validation at startup

**File**: `backend/app/main.py`

**Problem**: Missing env vars are silently tolerated (default to None or
empty string). The app starts and then fails later with cryptic errors.

**Fix Recommended**: Validate environment at startup with `pydantic-settings`,
fail-fast on missing required vars (`DB_PATH`, `ENCRYPTION_KEY`).

---

## 11. Observability

### 11.1 [HIGH] No structured logging

**File**: All backend modules

**Problem**: `logger.info(...)` calls use string formatting. Fields like
agent_id, task_id, project_id are not queryable.

**Fix Recommended**: Configure `structlog` with JSON output and bind
`agent_id`, `task_id`, `project_id` to the logger context. Then log
queries like "all errors for project X in the last 24h" become trivial.

---

### 11.2 [HIGH] No metrics/telemetry

**Files**: All

**Problem**: No Prometheus or OpenTelemetry export. Operator has no insight
into LLM call rate, failure rate, cost, or latency trends.

**Fix Recommended**: Add `opentelemetry-instrumentation-fastapi` and
instrument LLM calls. Export to `/metrics` endpoint.

---

### 11.3 [MEDIUM] No user-facing audit log view

**File**: `frontend/src/pages/`

**Problem**: `audit_events` are written but never displayed to the user.
Operators can't see who did what.

**Fix Recommended**: Add an `AuditPage` showing recent events filtered by
project, agent, action type.

---

## 12. Testing

### 12.1 [HIGH] No end-to-end tests

**File**: `backend/tests/*.py`

**Problem**: Tests are unit-level only. There's no test that simulates a
full task lifecycle: claim → codegen → review → QA → done.

**Fix Recommended**: Add Playwright or integration tests that run a full
sprint against a real LLM (or a deterministic mock) and assert the
workflow produces correct outcomes.

---

### 12.2 [HIGH] No load/stress testing

**Files**: Tests directory

**Problem**: No way to know how the system behaves under load.

**Fix Recommended**: Add `locust` load tests against a deployment that
ramps up to N concurrent sprints / M concurrent tasks / K LLM calls per
minute. Assert error rates, latency targets, no deadlocks.

---

### 12.3 [MEDIUM] No property-based tests

**Files**: `backend/app/codegen.py` parser, etc.

**Problem**: Edge cases like empty responses, JSON with trailing commas,
malformed output are tested ad-hoc.

**Fix Recommended**: Use `hypothesis` for property-based tests on the
JSON parser, helpers index generator, and other input-parsing code.

---

## Summary by Severity

| Severity | Count |
|---|---|
| CRITICAL | 1 |
| HIGH | 17 |
| MEDIUM | 22 |
| LOW | 0 |

The single CRITICAL issue (1.1 — persona instructions never reach LLM) has
been fixed in this update. The 17 HIGH-severity issues should be triaged
for the next sprint: start with code organization and reliability fixes
(1.2-1.5, 2.1-2.5, 3.1-3.3), then LLM/observability (6.x, 11.x), then
CI/CD and testing (10.x, 12.x).
