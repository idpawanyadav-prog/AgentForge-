# AgentForge — Issues Catalog

This document lists every issue found during a full codebase review, organized by
category. Security issues are explicitly flagged. Each entry includes the file,
line range, severity, description, and recommended fix.

---

## 1. SECURITY Issues

### 1.1 SSRF via unvalidated gateway base_url in discovery

- **File**: `backend/app/routers/gateways.py:221-241`
- **Severity**: High
- **Category**: Security / SSRF
- **Description**: The `_live_model_ids()` function sends an HTTP GET to
  `gw["base_url"] + "/models"` after only checking that the scheme is `http` or
  `https`. An attacker who can create or update a gateway can set `base_url` to
  an internal address (`http://169.254.169.254`, `http://localhost:8080/admin`,
  `http://internal-service:3000`) and make the backend probe it. This is a
  classic Server-Side Request Forgery (SSRF) vector.
- **Impact**: Access to cloud metadata endpoints, internal services, or any
  HTTP-reachable resource from the backend host.
- **Fix**: Validate `base_url` against an allowlist of known LLM provider
  domains, or at minimum reject private/reserved IP ranges (127.0.0.0/8,
  10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16, ::1, etc.) and
  well-known metadata endpoints before making the request.

### 1.2 Unvalidated subprocess calls in workspace path

- **File**: `backend/app/workspace.py:186-196`
- **Severity**: High
- **Category**: Security / Command injection
- **Description**: `_git()` runs `git` with arbitrary arguments derived from
  `repo_url` (user-provided). While `_git(["clone", repo_url, target])` passes
  the URL as a single argument, a crafted URL containing shell metacharacters
  could exploit subprocess behavior on some platforms. The `prepare_workspace()`
  function at line 232-236 calls `_git(["clone", repo_url, target])` where
  `repo_url` comes from user input with no URL validation beyond `_is_git_url()`.
- **Impact**: Potential command injection if git subprocess interprets URL
  characters differently than expected.
- **Fix**: Validate `repo_url` with `urllib.parse.urlparse` and reject URLs
  with unexpected schemes (only `https`, `ssh`, `git`). Consider using
  `gitpython` or validating the URL format strictly.

### 1.3 Time-of-check-time-of-use (TOCTOU) in workspace size quota

- **File**: `backend/app/workspace.py:469-478`
- **Severity**: Medium
- **Category**: Security / TOCTOU
- **Description**: `merge_back_run_workspace()` calculates `projected_bytes`
  before copying files (`shutil.copy2` at line 502) but checks the quota at
  line 476. Between the check and the copy, another concurrent merge could
  increase the workspace size beyond the quota. The per-file lease serializes
  merges per project, but the size check happens before the lease loop.
- **Impact**: Workspace could exceed configured quota under concurrent merges.
- **Fix**: Re-check the size after acquiring the lease and before applying
  changes, or enforce the quota at the filesystem level.

### 1.4 Weak Fernet key derivation

- **File**: `backend/app/db.py` (encryption setup)
- **Severity**: Medium
- **Category**: Security / Cryptography
- **Description**: Gateway API keys are encrypted with Fernet, which is
  appropriate, but the encryption key is derived from a single environment
  variable `AGENTFORGE_SECRET_KEY`. If this key is weak or leaked, all stored
  gateway API keys are compromised. There is no key rotation mechanism.
- **Impact**: Compromise of all LLM provider credentials stored in the database.
- **Fix**: Implement key rotation support. Require a minimum key length (32
  bytes for Fernet). Log key usage without logging the key itself.

---

## 2. Reliability / Correctness Issues

### 2.1 Race condition in summary cache eviction

- **File**: `backend/app/routers/tasks.py:106-111`
- **Severity**: Medium
- **Category**: Reliability / Concurrency
- **Description**: `_summary_cache` eviction logic is not thread-safe. The
  cleanup loop at line 106-111 and the insert at line 112 can interleave with
  concurrent reads/writes from different request threads. The `_registry_lock`
  protects workflow registry mutations but not this cache.
- **Impact**: Potential `RuntimeError: dictionary changed size during iteration`
  or lost cache entries under concurrent load.
- **Fix**: Wrap cache operations in a `threading.Lock`.

### 2.2 Unhandled `base_url` path concatenation in discovery

- **File**: `backend/app/routers/gateways.py:226`
- **Severity**: Medium
- **Category**: Reliability
- **Description**: `_live_model_ids()` appends `/v1` to `base_url` if it
  doesn't contain `/v1`. This can produce malformed URLs like
  `https://example.com/path/v1` → `https://example.com/path/v1/v1` if the user
  already included `/v1` in a different casing or as part of a path segment.
- **Impact**: Failed model discovery; confusing error messages.
- **Fix**: Use `urllib.parse.urljoin` or parse the URL and check the path
  component explicitly.

### 2.3 SSE poller creates unbounded tasks on restart

- **File**: `backend/app/routers/events.py:72`
- **Severity**: Medium
- **Category**: Reliability / Resource leak
- **Description**: Every SSE connection creates a `_poll` task via
  `asyncio.create_task()` and registers it with `background_tasks.track()`. If
  the server process is restarted (e.g., during development), the in-memory
  `_sse_connections` set is cleared but the database-backed `workflow_runs`
  table may still show `Running` runs from before the restart. The runtime's
  `_registry` is also lost, so those runs become zombie tasks.
- **Impact**: Stale `Running` workflow runs that can never complete or cancel.
- **Fix**: On startup, query `workflow_runs` for `Running` status and reset
  them to `Failed` or `Blocked` with a clear reason ("server restarted").

### 2.4 `_git()` returns None on any failure, masking errors

- **File**: `backend/app/workspace.py:186-196`
- **Severity**: Low
- **Category**: Reliability
- **Description**: `_git()` catches both `OSError` and `subprocess.TimeoutExpired`
  and returns `None`. The caller (`prepare_workspace`) interprets `None` as
  "git not installed" rather than "git command failed", losing diagnostic
  information.
- **Impact**: Users see "git not available" when the real issue is a network
  timeout or authentication failure.
- **Fix**: Return a result object with `ok`, `error`, and `output` fields
  instead of `None`.

### 2.5 `_run_changed_files` walks the entire run workspace on every merge

- **File**: `backend/app/workspace.py:410-431`
- **Severity**: Low
- **Category**: Performance
- **Description**: `_run_changed_files()` does a full `os.walk()` of the run
  workspace to compute changed files. For large workspaces, this is expensive
  and redundant — the manifest already contains the original file list.
- **Impact**: Slow merge-back for large workspaces.
- **Fix**: Track which files were actually written during the run (the runtime
  already collects `evidence` with written paths) and only check those.

### 2.6 `_quarantine_broken_tests` runs pytest collection twice

- **File**: `backend/app/toolchains.py:666-673`
- **Severity**: Low
- **Category**: Performance
- **Description**: The function runs `pytest --collect-only` to find broken
  test modules, and then the regular test run (`_python_tests`) runs pytest
  again. The collection phase could be cached or combined with the test run.
- **Impact**: ~2-5s of unnecessary overhead per Python workspace.
- **Fix**: Combine collection and test execution in a single pytest invocation,
  or cache the collection result.

### 2.7 `_copy_for_run` silently skips files on OSError

- **File**: `backend/app/workspace.py:360-365`
- **Severity**: Low
- **Category**: Reliability
- **Description**: In the `except OSError: continue` block, files that can't be
  copied (e.g., locked by another process) are silently dropped from the run
  workspace. The agent then works on an incomplete copy.
- **Impact**: Agents may miss files, leading to incomplete or incorrect
  implementations.
- **Fix**: Log a warning for each skipped file; if critical files are missing,
  fail the run setup and surface the error.

---

## 3. Missing Features / Gaps

### 3.1 No test coverage for `memory.py`

- **File**: `backend/app/memory.py`
- **Severity**: Medium
- **Category**: Testing / Coverage
- **Description**: The pitfall ledger (`memory.py`) has no dedicated test file.
  It is a new module (added in migration 009) and its deduplication logic,
  importance escalation, and `pitfalls_block()` formatting are untested.
- **Fix**: Add `test_memory.py` covering record_pitfall dedup, importance
  escalation, recent_pitfalls ordering, and pitfalls_block formatting.

### 3.2 No test coverage for `governance.py`

- **File**: `backend/app/governance.py`
- **Severity**: Medium
- **Category**: Testing / Coverage
- **Description**: The governance module is tested only via
  `test_governance_step1.py`. The state machine transitions, available
  transitions logic, and edge cases (invalid transitions, concurrent state
  changes) lack direct unit tests.
- **Fix**: Add `test_governance_state_machine.py` covering all valid/invalid
  transitions and the `available_transitions` logic.

### 3.3 No test coverage for `po.py`

- **File**: `backend/app/po.py`
- **Severity**: Medium
- **Category**: Testing / Coverage
- **Description**: The Product Owner autonomy module has no test file. Its
  decision logic (rewrite, reassign, cancel) is critical for the autonomous
  loop and currently only exercised through integration tests.
- **Fix**: Add `test_po.py` covering each decision path and the tick interval
  behavior.

### 3.4 No test coverage for `task_registry.py`

- **File**: `backend/app/task_registry.py`
- **Severity**: Low
- **Category**: Testing / Coverage
- **Description**: The background task registry that tracks all fire-and-forget
  coroutines has no direct test. Its cleanup-on-shutdown behavior is critical
  for graceful restarts.
- **Fix**: Add `test_task_registry.py` covering create, track, cancel, and
  shutdown cleanup.

### 3.5 No rate-limit test coverage

- **File**: `backend/app/rate_limit.py`
- **Severity**: Low
- **Category**: Testing / Coverage
- **Description**: The per-gateway rate limiter has no test file. Its token-bucket
  behavior, window reset, and per-gateway isolation are untested.
- **Fix**: Add `test_rate_limit.py` covering rate limiting, window expiry, and
  multi-gateway isolation.

### 3.6 No integration test for the full sprint lifecycle

- **Severity**: Medium
- **Category**: Testing / Coverage
- **Description**: While individual components (sprint_gate, runtime, review
  chain) have unit tests, there is no end-to-end test that creates a project,
  defines a sprint, assigns agents, executes tasks through dev → SA → BA → QA,
  and verifies the final state.
- **Fix**: Add an integration test using the test database that exercises the
  full lifecycle from project creation to task completion.

### 3.7 No frontend test for SSE event consumption

- **File**: `frontend/src/`
- **Severity**: Low
- **Category**: Testing / Coverage
- **Description**: The React frontend has no test for the SSE event consumer.
  The ControlPage's live-updating task board depends on this, and a regression
  in event parsing would be invisible without frontend tests.
- **Fix**: Add a test for the event hook/consumer component using a mock
  EventSource.

### 3.8 No test for workspace size quota enforcement

- **File**: `backend/app/workspace.py:476-478`
- **Severity**: Low
- **Category**: Testing / Coverage
- **Description**: The workspace quota check raises `ValueError` but there is
  no test verifying that oversized merges are rejected.
- **Fix**: Add a test case in `test_run_workspace.py` that creates a workspace
  exceeding the quota and verifies the merge is rejected.

---

## 4. Code Quality / Maintainability Issues

### 4.1 `runtime.py` is 1000+ lines with deeply nested control flow

- **File**: `backend/app/runtime.py`
- **Severity**: Medium
- **Category**: Maintainability
- **Description**: The `_run_phases()` function is 300+ lines with nested loops,
  inline closures (`_deps`, `_record_model`), and multiple self-fix retry loops.
  The function handles dev, QA, SA, and BA modes with branching at every step.
- **Impact**: Hard to reason about, difficult to extend, high risk of
  introducing bugs when modifying phase behavior.
- **Fix**: Extract each mode's phase execution into a separate method
  (`_run_dev_phases`, `_run_qa_phases`, `_run_review_phases`). Extract the
  self-fix loop into a reusable `_self_fix_loop()` helper.

### 4.2 `chatbot.py` is 1969 lines with 100+ intent patterns

- **File**: `backend/app/chatbot.py`
- **Severity**: Medium
- **Category**: Maintainability
- **Description**: The chatbot packs intent definitions, command execution,
  confirmation handling, AI routing, and message persistence into one file.
  The `INTENTS` list has 100+ regex patterns, and `_execute_command()` handles
  every command type in a single function.
- **Impact**: Adding new commands requires editing a monolithic file. Intent
  patterns are hard to test in isolation.
- **Fix**: Split into `chatbot/intents.py`, `chatbot/commands/`, and
  `chatbot/router.py`. Each command gets its own handler class.

### 4.3 `toolchains.py` is 1000+ lines with mixed concerns

- **File**: `backend/app/toolchains.py`
- **Severity**: Low
- **Category**: Maintainability
- **Description**: Module installation, environment setup, build checks, test
  runs, and quarantine logic are all in one file. The Python build driver is
  embedded as a raw string literal.
- **Fix**: Split into `toolchains/installer.py`, `toolchains/build.py`,
  `toolchains/test.py`, `toolchains/quarantine.py`.

### 4.4 Inconsistent error handling patterns

- **Severity**: Low
- **Category**: Maintainability
- **Description**: Some functions return `(bool, str)` tuples, others return
  `dict` with `ok`/`error` keys, and others raise exceptions. The runtime
  handles all three patterns. This makes error propagation fragile.
- **Fix**: Standardize on a single error representation (e.g., a `Result`
  dataclass) across all toolchain functions.

### 4.5 Magic numbers scattered throughout runtime

- **File**: `backend/app/runtime.py`
- **Severity**: Low
- **Category**: Maintainability
- **Description**: Values like `88` (progress during review), `5` (initial
  progress), `1.2` (sleep before agent idle), and `140`/`160` (evidence
  truncation lengths) appear as literals.
- **Fix**: Extract to named constants in `config.py` or at the top of
  `runtime.py`.

---

## 5. Performance Issues

### 5.1 SSE poller polls database every 2 seconds per connection

- **File**: `backend/app/routers/events.py:68`
- **Severity**: Medium
- **Category**: Performance
- **Description**: Each SSE connection runs its own `SELECT * FROM
  execution_events WHERE seq > ?` query every 2 seconds. With 50 concurrent
  dashboard users, that's 25 queries/second, all hitting the same SQLite file.
- **Impact**: SQLite lock contention under load; increased latency for write
  operations (event inserts).
- **Fix**: Use a single shared poller that broadcasts to all connections via
  `asyncio.Queue` per connection, or use SQLite's WAL mode with shared cache.

### 5.2 No connection pooling for SQLite

- **File**: `backend/app/db.py`
- **Severity**: Low
- **Category**: Performance
- **Description**: Each request opens a new SQLite connection. Under concurrent
  load, SQLite serializes writes, and connection overhead adds up.
- **Fix**: Use a connection pool (e.g., `aiosqlite` with a pool, or SQLite's
  built-in WAL mode with shared cache). Ensure `PRAGMA journal_mode=WAL` is set.

### 5.3 `_workspace_bytes()` walks entire tree for quota check

- **File**: `backend/app/workspace.py:29-37`
- **Severity**: Low
- **Category**: Performance
- **Description**: `_workspace_bytes()` is called during every merge-back to
  check the workspace quota. It walks the entire workspace tree on every call.
- **Impact**: Slow merges for large workspaces.
- **Fix**: Cache the workspace size and update it incrementally when files are
  added/removed. Only recalculate when the cache is stale.

### 5.4 No connection limit on LLM gateway calls

- **File**: `backend/app/codegen.py` (GatewayClient)
- **Severity**: Low
- **Category**: Performance / Reliability
- **Description**: `GatewayClient.acall()` opens a new `httpx.AsyncClient` for
  each call (no connection pooling). Under burst loads (multiple agents
  generating code simultaneously), this can exhaust file descriptors.
- **Fix**: Use a shared `httpx.AsyncClient` with connection pooling per gateway.

---

## 6. API Design Issues

### 6.1 `body.model_dump(exclude_unset=True)` silently drops `None` values

- **File**: Multiple routers (`agents.py`, `projects.py`, `gateways.py`, etc.)
- **Severity**: Medium
- **Category**: API Design
- **Description**: PATCH endpoints use `body.model_dump(exclude_unset=True)` to
  construct the update payload. This means a client cannot explicitly set a
  field to `None` (e.g., clearing an agent's model binding) by sending `null`
  in the JSON body — the field is treated as "unset" and ignored.
- **Impact**: Some fields cannot be cleared via PATCH.
- **Fix**: Use `exclude_none=True` instead, or add a sentinel value for
  explicit clearing.

### 6.2 Chatbot confirmation flow blocks non-sensitive commands

- **File**: `backend/app/chatbot.py:1903`
- **Severity**: Low
- **Category**: API Design / UX
- **Description**: The confirmation gate checks `intent in SENSITIVE or intent in
  PROPOSAL_COMMANDS`. All `PROPOSAL_COMMANDS` (create sprint, add urgent task,
  install toolchain) require confirmation even though they are not sensitive
  mutations. This makes the chatbot feel slow for routine operations.
- **Fix**: Separate "proposal" commands (which benefit from preview) from
  "sensitive" commands (which need security confirmation). Allow users to
  configure auto-approval for proposal commands.

### 6.3 No pagination cursor on event stream

- **File**: `backend/app/routers/events.py:47`
- **Severity**: Low
- **Category**: API Design
- **Description**: The SSE endpoint accepts an `after` sequence number but
  clients have no way to know the current `seq` without fetching events first.
  If a client reconnects after a gap, it may miss events that were purged
  before it reconnected.
- **Fix**: Include the latest sequence number in a separate endpoint or in
  heartbeat frames.

---

## 7. Frontend Issues

### 7.1 Frontend markdown parser is incomplete

- **File**: `frontend/src/MarkdownText.tsx:4-12`
- **Severity**: Low
- **Category**: Frontend / UX
- **Description**: The custom markdown parser only handles `` `code` `` and
  `**bold**`. Links, lists, headings, and line breaks are rendered as plain
  text. This is a deliberate XSS-safe choice but limits usability.
- **Impact**: Chat messages and instruction previews lose formatting.
- **Fix**: Extend the parser incrementally (links, lists) while maintaining
  XSS safety. Consider `react-markdown` with a sanitizer like `rehype-sanitize`.

### 7.2 Zustand persist middleware stores secrets in localStorage

- **File**: `frontend/src/store.ts`
- **Severity**: Medium
- **Category**: Security / Frontend
- **Description**: The Zustand store uses `persist` middleware which writes the
  entire store to `localStorage`. If the store ever holds sensitive data (e.g.,
  gateway keys, even masked ones), it would be stored in plaintext in the
  browser.
- **Impact**: XSS attacks could read localStorage and extract any stored data.
- **Fix**: Use `partialize` to exclude sensitive fields from persistence, or
  switch to `sessionStorage` for session-only data.

### 7.3 No React error boundary for SSE failures

- **File**: `frontend/src/App.tsx` (or ControlPage)
- **Severity**: Low
- **Category**: Frontend / Reliability
- **Description**: If the SSE connection drops and the event consumer throws,
  the entire React tree unmounts without recovery. There is no error boundary
  around the event consumer.
- **Fix**: Wrap the event-consuming component tree in an `ErrorBoundary` that
  attempts to reconnect.

### 7.4 Frontend has no loading/error states for SSE

- **File**: `frontend/src/pages/ControlPage.tsx`
- **Severity**: Low
- **Category**: Frontend / UX
- **Description**: The ControlPage's live-updating board depends on SSE events
  but has no visible loading state while waiting for the first event, and no
  retry UI when the connection drops.
- **Fix**: Add connection status indicator (connected / reconnecting / failed)
  and a manual retry button.

---

## 8. Testing Issues

### 8.1 No concurrency tests for workspace merge

- **File**: `backend/tests/test_run_workspace.py`
- **Severity**: Medium
- **Category**: Testing
- **Description**: The test file covers single-run merge scenarios but does not
  test concurrent merges from two different runs touching overlapping files.
  The per-file lease mechanism is untested under concurrency.
- **Fix**: Add a test that spawns two merge operations with overlapping files
  and verifies serialization and conflict detection.

### 8.2 No stress test for runtime with many concurrent runs

- **Severity**: Medium
- **Category**: Testing
- **Description**: The `MAX_CONCURRENT_RUNS` limit is configurable but there is
  no test that verifies the runtime correctly rejects runs beyond the limit or
  handles cancellation correctly under load.
- **Fix**: Add a stress test that starts `MAX_CONCURRENT_RUNS + 1` runs and
  verifies the excess is rejected.

### 8.3 No test for SSE queue overflow behavior

- **File**: `backend/app/routers/events.py:24-27`
- **Severity**: Low
- **Category**: Testing
- **Description**: The SSE queue has `MAX_QUEUE_SIZE=1000` and drops old events
  when full (`queue.get_nowait()`). There is no test verifying this behavior.
- **Fix**: Add a test that fills the queue and verifies old events are dropped
  and new events are delivered.

### 8.4 Frontend tests are minimal (2 test files)

- **File**: `frontend/src/*.test.tsx`
- **Severity**: Low
- **Category**: Testing
- **Description**: Only `MarkdownText.test.tsx` and `validation.test.tsx` exist.
  No tests for routing, state management, API calls, or page components.
- **Fix**: Add tests for the Zustand store, API service layer, and key page
  components (at minimum: ControlPage task list, project creation form).

---

## 9. Documentation Issues

### 9.1 No API documentation (OpenAPI/Swagger)

- **Severity**: Low
- **Category**: Documentation
- **Description**: FastAPI auto-generates OpenAPI specs at `/docs` and
  `/openapi.json`, but these are not versioned or included in the repository.
  Consumers of the API have no contract to reference.
- **Fix**: Export the OpenAPI spec to `docs/openapi.json` and include it in
  version control. Consider `swagger-ui` for interactive docs.

### 9.2 No architecture decision records (ADRs)

- **Severity**: Low
- **Category**: Documentation
- **Description**: Major architectural decisions (run isolation, model failover,
  workspace merge leases, pitfall ledger) are documented in code comments but
  not as standalone ADRs. Future contributors lack context for why these
  patterns exist.
- **Fix**: Add ADRs for: run isolation strategy, model failover design,
  workspace merge lease mechanism, and pitfall ledger architecture.

### 9.3 `docs/ISSUES_CATALOG.md` is a duplicate of this file

- **Severity**: Low
- **Category**: Documentation
- **Description**: The existing `docs/ISSUES_CATALOG.md` was generated by an
  earlier review and overlaps with this document. The two should be merged to
  avoid confusion.
- **Fix**: Replace the old file with this one, or cross-reference them.

---

## 10. Summary by Severity

| Severity | Count | Categories |
|---|---|---|
| High | 1 | SSRF |
| Medium | 10 | Security, Reliability, Testing, Frontend |
| Low | 16 | Performance, Maintainability, Documentation |

### Top priority fixes

1. **SSRF in gateway discovery** (1.1) — add private IP / metadata endpoint
   filtering.
2. **Race condition in summary cache** (2.1) — add locking.
3. **No concurrency tests for workspace merge** (8.1) — add tests.
4. **Zustand persist may store sensitive data** (7.2) — use `partialize`.
5. **Zustand cache eviction not thread-safe** (2.1) — add locking.

---

## 11. Non-Security Recommendations

These are not bugs but would significantly improve the development experience:

### 11.1 Add a CI/CD pipeline

The repository has no `.github/workflows/` or equivalent. Add CI that runs:
- `pytest` on every push
- `vitest` on frontend changes
- `ruff` / `mypy` linting on Python files
- `eslint` / `tsc` on TypeScript files
- Build verification (backend + frontend)

### 11.2 Add structured logging with correlation IDs

Currently, logs are plain text. Add a request-scoped correlation ID (from
`X-Request-ID` header or generated) and include it in all log lines, audit
records, and workflow events. This makes tracing a single task execution across
runtime, codegen, and toolchain logs much easier.

### 11.3 Add health check endpoints

The backend has no `/health` or `/ready` endpoint. Add a health check that
verifies the database is reachable and the event archiver is running. This is
essential for container orchestration and load balancers.

### 11.4 Add metrics export

Expose runtime metrics (active runs, queue depth, LLM call latency, workspace
sizes) via Prometheus `/metrics` endpoint. This enables alerting and capacity
planning.

### 11.5 Add a development seed script

Creating a project, sprint, team, agents, and gateway from scratch requires many
manual API calls. Add a `seed.py` script that creates a complete demo project
with pre-configured agents and a working sprint.

### 11.6 Standardize error responses

Currently, errors are returned as plain strings or ad-hoc dicts. Define a
standard error envelope (`{"error": {"code": "...", "message": "...",
"details": ...}}`) and use it consistently across all routers.

### 11.7 Add request timeout configuration

The SSE poller, codegen calls, and health checks have hardcoded timeouts.
Expose these as configurable parameters so operators can tune for their
environment.

### 11.8 Add workspace path validation on project creation

`validate_workspace_path()` exists but is not called during project creation.
Add it to the `POST /projects` handler to fail fast on misconfigured paths.

### 11.9 Add rate limiting on the SSE endpoint

The SSE endpoint has no rate limiting. A malicious client could open hundreds
of connections and consume resources. Add a connection limit per IP and a
maximum connection duration.

### 11.10 Add database migration version tracking

The migration system runs scripts but does not track which migrations have been
applied. Add a `schema_migrations` table (if not already present) that records
each migration's version and timestamp, and verify it before applying.
