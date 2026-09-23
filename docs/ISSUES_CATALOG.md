# AgentForge — Issues Catalog (Non-Security)

> Excludes security findings (see separate security review).
> Issues are organized by severity and categorized by type.
> **FIXED** items are noted — they have been resolved in the current codebase.
> Items marked **Not Pursuing** are intentional design decisions, not gaps.

---

## FIXED Issues

### 1. Codegen operator precedence bug in `is_llm_outage` ✅ FIXED

**File**: `backend/app/codegen.py`
**Category**: Correctness
**Fix Applied**: Wrapped conditions in parentheses: `bool(error) and (reason or _llm_outage_reason(error) != "")`

---

### 2. Global LLM health flag affects all projects ✅ FIXED

**File**: `backend/app/runtime.py`
**Category**: Design / Reliability
**Fix Applied**: Per-gateway health tracking with `_gateway_health[gateway_id]` dict, `mark_llm_healthy/failed()`, `is_llm_healthy()`, and `_LLM_HEALTH_THRESHOLD = 3`

---

### 3. Race condition in baseline proposal ✅ FIXED

**File**: `backend/app/governance.py`
**Category**: Concurrency
**Fix Applied**: `_baseline_proposal_lock = threading.Lock()` serializes check-and-insert in `propose_baseline()`

---

### 4. Subprocess calls without process-level timeout ✅ FIXED

**File**: `backend/app/toolchains.py`
**Category**: Reliability
**Fix Applied**: All `subprocess.run()` calls now include explicit `timeout` parameter. Caught `subprocess.TimeoutExpired` returns `(124, "timed out after Xs")`.

---

### 5. `_or_404` helper duplicated across routers ✅ FIXED

**File**: `backend/app/routers/_util.py` (shared), `backend/app/routers/gateways.py` (local copy remains)
**Category**: Maintainability
**Fix Applied**: Most routers now import from `routers._util`. `gateways.py` still has local `_or_404` (minor cleanup item).

---

### 6. `_summary_cache` in tasks.py is unbounded ✅ FIXED

**File**: `backend/app/routers/tasks.py`
**Category**: Memory
**Fix Applied**: Replaced with bounded cache using `functools.lru_cache` pattern or equivalent eviction.

---

## HIGH Severity Issues (Remaining)

### H1. No authentication/authorization — **NOT PURSUING**

**Files**: `backend/app/main.py`, all routers
**Category**: Design Decision (not an issue)
**Impact**: Any client has full access to all APIs

The backend has zero authentication. All 90+ endpoints are fully open. This is **intentional for now** — AgentForge is a single-user, local/internal deployment tool. Multi-tenant or production use would require auth, but that is deferred.

**Status**: Design decision. Not a bug, not a gap for current deployment model.

---

### H2. Chatbot `handle_message` is 1300+ lines

**File**: `backend/app/chatbot.py`
**Category**: Maintainability
**Impact**: Single function violates SRP, hard to test/debug

The `handle_message` function handles intent matching, AI routing, command execution, confirmation flows, and error handling all in one monolithic function.

**Recommendation**: Split into separate handlers: `_match_intent()`, `_execute_command()`, `_handle_confirmation()`, `_ai_route()`.

---

### H3. Chatbot LLM calls block thread during retries

**File**: `backend/app/chatbot.py` (_call_llm)
**Category**: Performance
**Impact**: Thread pool exhaustion under load

`GatewayClient.call()` uses `time.sleep()` for retry backoff, blocking the thread pool slot. Under high load, this exhausts the thread pool.

**Recommendation**: Use async HTTP client (httpx) with async retry logic, or increase thread pool size.

---

### H4. Workspace merge is not atomic

**File**: `backend/app/workspace.py` (merge_back_run_workspace)
**Category**: Reliability
**Impact**: Crash mid-merge leaves workspace in partially-merged state

Files are copied one-by-one. If the process crashes or is killed mid-merge, the workspace has a mix of old and new files with no way to detect the partial state.

**Recommendation**: Implement atomic merge (copy to temp dir, then rename) or add merge journaling.

---

### H5. No file size limits on generated code (partially fixed)

**File**: `backend/app/codegen.py` (generate_implementation)
**Category**: Reliability / Security
**Status**: File size limits now enforced (1MB per file, 10MB total), but no per-project disk quota

**Remaining Risk**: A single project could still fill the disk with many large files across multiple generations.

**Recommendation**: Add per-project disk quota and cleanup of old workspace runs.

---

## MEDIUM Severity Issues

### 6. Chatbot JSON repair is best-effort

**File**: `backend/app/chatbot.py` (_repair_json)
**Category**: Reliability
**Impact**: Truncated LLM responses may produce invalid JSON that passes through to execution

The `_repair_json` function attempts to fix truncated JSON but may produce invalid structures that then get executed as commands.

**Recommendation**: Add stricter validation after repair and reject malformed JSON with a clear error.

---

### 7. Pending command race condition

**File**: `backend/app/chatbot.py` (_queue_pending)
**Category**: Concurrency
**Impact**: Concurrent chatbot sessions could race on pending_commands

`_queue_pending` and confirmation handlers both touch `pending_commands` without explicit locking.

**Recommendation**: Add per-conversation lock or use atomic DB operations.

---

### 8. SSE queue has no backpressure

**File**: `backend/app/routers/events.py`
**Category**: Reliability
**Impact**: Slow clients cause unbounded memory growth

The SSE `asyncio.Queue` has no max size. If a client is slow to consume events, the queue grows indefinitely.

**Recommendation**: Set `maxsize` on the queue and implement backpressure (drop oldest or pause polling).

---

### 9. `start_execution` ignores idempotency key

**File**: `backend/app/routers/tasks.py`
**Category**: Correctness
**Impact**: Concurrent retries can spawn duplicate workflow runs

The `Idempotency-Key` header is declared but not passed to `run_idempotent`. Concurrent POSTs to `/executions` can create multiple runs for the same task.

**Recommendation**: Pass `idempotency_key` to `run_idempotent()` or validate at the `start_execution` level.

---

### 10. Dead code: `_create_module_health_tasks`

**File**: `backend/app/runtime.py`
**Category**: Maintainability
**Impact**: Dead code adds confusion

`_create_module_health_tasks()` is a thin alias for `sprint_gate._module_rework_specs()` and is never called directly.

**Recommendation**: Remove the alias and update any references.

---

### 11. Circular import workaround in projects.py

**File**: `backend/app/routers/projects.py`
**Category**: Maintainability
**Impact**: Fragile import pattern, hard to refactor

Uses `__import__("app.db", fromlist=["emit_event"]).emit_event` to avoid circular imports.

**Recommendation**: Move event emission to a separate module or use deferred imports.

---

### 12. `_live_model_ids` uses synchronous urllib in async router

**File**: `backend/app/routers/gateways.py`
**Category**: Performance
**Impact**: Could block event loop under slow networks

Synchronous `urllib.request` is used in an endpoint that could be async. FastAPI's threadpool mitigates this only if the function is `async def`.

**Recommendation**: Use `httpx` with `await` for async HTTP calls.

---

### 13. No frontend routing library

**File**: `frontend/src/App.tsx`
**Category**: Architecture
**Impact**: No deep links, URL state, browser history, or route guards

Page switching is a `switch` on a string state variable.

**Recommendation**: Introduce React Router for proper routing.

---

### 14. All frontend state is local

**File**: `frontend/src/pages/*.tsx`
**Category**: Architecture
**Impact**: Data duplication, stale state across pages

Each page independently fetches data. Creating an agent in TeamsPage doesn't update the agent list in ProjectsPage.

**Recommendation**: Introduce lightweight global state (Zustand or React Context).

---

### 15. `dangerouslySetInnerHTML` for markdown

**File**: `frontend/src/api.ts` (md function)
**Category**: Security (XSS risk — see security review)
**Impact**: If escaping is incomplete, XSS is possible

Markdown is rendered via `dangerouslySetInnerHTML`. The `md()` function does basic HTML escaping, but any future change must preserve it.

**Recommendation**: Use a well-audited markdown library (react-markdown) with sanitization.

---

### 16. In-memory rate limiting doesn't scale

**File**: `backend/app/rate_limit.py`
**Category**: Scalability
**Impact**: Multi-worker deployments have independent counters

**Status**: Redis fallback implemented but not required. Works for single-process deployments.

**Recommendation**: Use Redis or shared cache for distributed rate limiting in production.

---

### 17. Decrypted API keys in memory

**File**: `backend/app/secrets.py`
**Category**: Security (defense-in-depth — see security review)
**Impact**: Keys vulnerable to memory dumps

Decrypted keys live in Python strings for the process lifetime. Acceptable for single-user/local deployment.

**Recommendation**: Cache with TTL, zeroize after use (where possible) when multi-user deployment is needed.

---

### 18. Circuit breaker state lost on restart

**File**: `backend/app/codegen.py` (_BreakerState)
**Category**: Reliability
**Impact**: After restart, previously-open breakers are forgotten

In-memory breaker state doesn't persist. A still-down gateway gets hammered immediately after restart.

**Recommendation**: Persist breaker state to disk or use exponential backoff on startup.

---

### 19. `project lib/` pip install path may not match PYTHONPATH

**File**: `backend/app/toolchains.py` (check_modules)
**Category**: Correctness
**Impact**: Installed packages may not be importable

Packages are installed to `project lib/` but the project may not have this in `sys.path`.

**Recommendation**: Ensure `PYTHONPATH` includes the lib directory or use a virtual environment.

---

### 20. Playwright port conflicts possible

**File**: `backend/app/browser_test.py` (_free_port)
**Category**: Reliability
**Impact**: Rare port collision could cause smoke test to fail

Uses `127.0.0.1:0` to get a free port, which is generally safe but not 100% collision-proof.

**Recommendation**: Add retry with alternative port if bind fails.

---

## LOW Severity Issues

### 21. No log rotation

**File**: `backend/app/logging_config.py`
**Category**: Operations
**Impact**: Log files grow unbounded on long-running deployments

**Recommendation**: Add `RotatingFileHandler` or `TimedRotatingFileHandler`.

---

### 22. No environment overrides for tunable parameters

**File**: `backend/app/config.py`
**Category**: Operations
**Impact**: Cannot tune per-deployment without code changes

Most values have defaults with no env var override mechanism.

**Recommendation**: Add `_env()` calls for all tunable parameters.

---

### 23. `LEGACY_ALIASES` references `ACTIVE_DEV` before definition

**File**: `backend/app/governance.py`
**Category**: Style
**Impact**: Works but is fragile and confusing

`LEGACY_ALIASES = {"": ACTIVE_DEV, "Active": ACTIVE_DEV}` references `ACTIVE_DEV` which is defined later.

**Recommendation**: Move `ACTIVE_DEV` definition above `LEGACY_ALIASES`.

---

### 24. `_NEXT = [1]` hack for mutable integer

**File**: `backend/app/codegen.py` (scaffold generator)
**Category**: Style
**Impact**: Works but is confusing

Uses a list `[1]` as a mutable integer counter in the scaffold template.

**Recommendation**: Use a proper counter or closure.

---

### 25. No form validation in frontend

**File**: `frontend/src/pages/*.tsx`
**Category**: UX / Correctness
**Impact**: Users can submit invalid data (no max-length, no date validation)

Forms use manual `useState` with minimal validation (only "title is non-empty").

**Recommendation**: Add validation library (Zod) or at least inline validation rules.

---

### 26. Window.prompt/confirm used for critical actions

**File**: `frontend/src/pages/ProjectsPage.tsx` (GovernanceTab)
**Category**: UX
**Impact**: Blocking, unstyled, inaccessible dialogs

Uses `window.prompt` and `window.confirm` for governance decisions.

**Recommendation**: Replace with proper modal components.

---

### 27. No tests for `codegen.generate_implementation()` happy path

**File**: `backend/tests/`
**Category**: Testing
**Impact**: Critical code path untested

The main code generation function is only mocked in tests. No test covers the actual LLM call, retry logic, or scaffold fallback.

**Recommendation**: Add integration test with mocked LLM responses.

---

### 28. No lifespan startup tests

**File**: `backend/tests/`
**Category**: Testing
**Impact**: PO resume, scheduler startup, workspace relocation untested

All tests use `TestClient(app)` without context manager, skipping lifespan.

**Recommendation**: Add at least one test that exercises the lifespan startup sequence.

---

### 29. No security/authorization tests

**File**: `backend/tests/`
**Category**: Testing / Security
**Impact**: No verification of access controls

No tests verify that cross-project access is denied, that actors are validated, or that sensitive operations require authorization.

**Recommendation**: Add authorization tests for each sensitive endpoint.

---

### 30. Test files duplicate DB bootstrap logic

**File**: `backend/tests/conftest.py` + test modules
**Category**: Maintainability
**Impact**: Redundant code, risk of inconsistency

Six test files duplicate the `AGENT_OFFICE_DB` + `sys.path` + `init_db()` setup from conftest.

**Recommendation**: Remove duplication, rely on conftest autouse fixture.

---

### 31. No pytest configuration

**File**: `backend/tests/`
**Category**: Testing
**Impact**: Test discovery relies on defaults, inconsistent setup

No `pytest.ini`, `pyproject.toml`, or `setup.cfg`.

**Recommendation**: Add `pytest.ini` with `testpaths`, `addopts`, and `pythonpath`.

---

### 32. `useAbortable` hook is never used

**File**: `frontend/src/useAbortable.ts`
**Category**: Dead Code
**Impact**: Unused code adds maintenance burden

The hook exists but is never imported or used.

**Recommendation**: Remove or integrate into polling patterns.

---

### 33. Frontend has no test runner configured

**File**: `frontend/package.json`
**Category**: Testing
**Impact**: Zero frontend test coverage

No vitest, jest, or testing-library configured.

**Recommendation**: Add vitest + @testing-library/react to devDependencies.

---

## Summary

| Severity | Count | Key Areas |
|----------|-------|-----------|
| FIXED | 6 | Correctness, concurrency, reliability, maintainability |
| NOT PURSUING | 1 | Auth — intentional for single-user deployment |
| HIGH | 4 | Maintainability, performance, reliability |
| MEDIUM | 10 | Concurrency, reliability, architecture |
| LOW | 8 | Style, operations, testing, UX |

**Total Issues**: 22 remaining (6 fixed, 1 intentional)

**Priority Recommendations**:
1. **HIGH**: Refactor chatbot (H1 renamed) + fix thread blocking (H2)
2. **HIGH**: Atomic workspace merge (H3), SSE backpressure (8), idempotency (9)
3. **MEDIUM**: Address remaining concurrency and reliability items
4. **LOW**: Schedule cleanup items for ongoing improvement

---

*Generated by AgentForge Architecture Review — 2026*
