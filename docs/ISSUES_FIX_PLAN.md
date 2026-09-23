# AgentForge — Technical Implementation Plan: Issue Fixes

> Actionable plan to resolve remaining issues catalogued in `ISSUES_CATALOG.md`.
> **6 issues have already been fixed** (see FIXED section below) — their work items are marked complete.
> **Authentication is intentionally not pursued** — AgentForge is a single-user/local deployment tool.
> Each remaining issue includes: root cause, fix design, code changes, test strategy, and acceptance criteria.

**Legend**:
- ✅ FIXED — Already resolved in the current codebase
- 🔴 HIGH — Fix immediately
- 🟡 MEDIUM — Fix in current sprint
- 🟢 LOW — Fix in next maintenance cycle

---

# COMPLETED FIXES (✅)

The following 6 issues have been resolved. They are included here for reference and regression testing.

---

## ✅ Issue #1: `is_llm_outage` Operator Precedence Bug — FIXED

**File**: `backend/app/codegen.py`
**Severity**: HIGH
**Fix Applied**: Wrapped conditions in parentheses: `bool(error) and (reason or _llm_outage_reason(error) != "")`
**Test**: `backend/tests/test_critical_fixes.py` — verifies `is_llm_outage("")` returns `False`

---

## ✅ Issue #2: Global LLM Health Flag — FIXED

**File**: `backend/app/runtime.py`
**Severity**: HIGH
**Fix Applied**: Per-gateway health tracking with `_gateway_health[gateway_id]` dict, `mark_llm_healthy/failed()`, `is_llm_healthy()`, `_LLM_HEALTH_THRESHOLD = 3`
**Test**: `backend/tests/test_gateway_health.py` — verifies per-gateway isolation

---

## ✅ Issue #3: Race Condition in Baseline Proposal — FIXED

**File**: `backend/app/governance.py`
**Severity**: HIGH
**Fix Applied**: `_baseline_proposal_lock = threading.Lock()` serializes check-and-insert in `propose_baseline()`
**Test**: `backend/tests/test_governance_step1.py` — verifies no duplicate pending baselines

---

## ✅ Issue #4: Subprocess Calls Without Process-Level Timeout — FIXED

**File**: `backend/app/toolchains.py`
**Severity**: MEDIUM
**Fix Applied**: All `subprocess.run()` calls now include explicit `timeout` parameter. Caught `subprocess.TimeoutExpired` returns `(124, "timed out after Xs")`.
**Test**: `backend/tests/test_run_workspace.py` — verifies graceful timeout handling

---

## ✅ Issue #9: `_summary_cache` Unbounded — FIXED

**File**: `backend/app/routers/tasks.py`
**Severity**: MEDIUM
**Fix Applied**: Replaced with bounded cache using eviction policy
**Test**: `backend/tests/test_task_registry.py` — verifies cache behavior

---

## ✅ Issue #15: `_or_404` Helper Duplicated — FIXED

**File**: `backend/app/routers/_util.py` (shared), `backend/app/routers/gateways.py` (local copy remains)
**Severity**: MEDIUM
**Fix Applied**: Most routers now import from `routers._util`. `gateways.py` still has local `_or_404` (minor cleanup item).
**Test**: Existing router tests verify shared utility works

---

# PHASE 1: HIGH SEVERITY FIXES (🔴)

## Issue #1: `is_llm_outage` Operator Precedence Bug

**File**: `backend/app/codegen.py` (lines 200-202)
**Severity**: HIGH
**Type**: Correctness

### Root Cause
```python
# Current (buggy):
return bool(error) and _llm_outage_reason(error) != "" or (...)
# Parsed as: (bool(error) and (_llm_outage_reason(error) != "")) or (...)
# Could return True even when error is empty
```

### Fix Design
Wrap the entire condition in parentheses to enforce correct precedence.

### Implementation

**File**: `backend/app/codegen.py`

```python
# BEFORE (lines 200-202):
def is_llm_outage(error: str) -> bool:
    """Return True when the error string indicates a provider-side outage
    (timeout, 5xx, connection reset) rather than a client mistake or
    quota/billing issue."""
    return bool(error) and _llm_outage_reason(error) != "" or (
        "timeout" in (error or "").lower()
        or "connection" in (error or "").lower()
    )

# AFTER:
def is_llm_outage(error: str) -> bool:
    """Return True when the error string indicates a provider-side outage
    (timeout, 5xx, connection reset) rather than a client mistake or
    quota/billing issue."""
    return bool(error) and (
        _llm_outage_reason(error) != ""
        or "timeout" in error.lower()
        or "connection" in error.lower()
    )
```

### Test Strategy
Update `backend/tests/test_critical_fixes.py`:
- Add test case: `is_llm_outage("")` must return `False`
- Add test case: `is_llm_outage("timeout")` must return `True`
- Verify existing 7 outage patterns still pass

### Acceptance Criteria
- `is_llm_outage("")` → `False`
- `is_llm_outage("Gateway returned HTTP 503")` → `True`
- `is_llm_outage("timeout exceeded")` → `True`
- All existing tests pass

---

## Issue #2: Global LLM Health Flag Affects All Projects

**File**: `backend/app/runtime.py` (lines 44-48)
**Severity**: HIGH
**Type**: Design / Reliability

### Root Cause
```python
# Current: single global flag
_llm_healthy = True
_llm_failure_count = 0

def mark_llm_failed():
    global _llm_healthy, _llm_failure_count
    with _llm_health_lock:
        _llm_failure_count += 1
        if _llm_failure_count >= _LLM_HEALTH_THRESHOLD:
            _llm_healthy = False  # Affects ALL projects
```

### Fix Design
Track health per gateway ID instead of globally.

### Implementation

**File**: `backend/app/runtime.py`

```python
# REPLACE (lines 44-48):
_llm_healthy = True
_llm_failure_count = 0
_LLM_HEALTH_THRESHOLD = 3
_llm_health_lock = threading.Lock()

# WITH:
_gateway_health: dict[str, dict] = {}
_GATEWAY_HEALTH_LOCK = threading.Lock()
_LLM_HEALTH_THRESHOLD = 3


def _gw_health(gateway_id: str) -> dict:
    """Get or create health tracker for a gateway."""
    with _GATEWAY_HEALTH_LOCK:
        if gateway_id not in _gateway_health:
            _gateway_health[gateway_id] = {
                "healthy": True,
                "consecutive_failures": 0,
            }
        return _gateway_health[gateway_id]


def mark_llm_healthy(gateway_id: str):
    with _GATEWAY_HEALTH_LOCK:
        if gateway_id in _gateway_health:
            _gateway_health[gateway_id]["consecutive_failures"] = 0
            _gateway_health[gateway_id]["healthy"] = True


def mark_llm_failed(gateway_id: str):
    with _GATEWAY_HEALTH_LOCK:
        h = _gateway_health.setdefault(gateway_id, {
            "healthy": True,
            "consecutive_failures": 0,
        })
        h["consecutive_failures"] += 1
        if h["consecutive_failures"] >= _LLM_HEALTH_THRESHOLD:
            h["healthy"] = False


def is_llm_healthy(gateway_id: str) -> bool:
    with _GATEWAY_HEALTH_LOCK:
        return _gateway_health.get(gateway_id, {}).get("healthy", True)


def reset_llm_health(gateway_id: str):
    with _GATEWAY_HEALTH_LOCK:
        if gateway_id in _gateway_health:
            _gateway_health[gateway_id]["healthy"] = True
            _gateway_health[gateway_id]["consecutive_failures"] = 0
```

**Update all call sites**:
- `mark_llm_healthy()` → `mark_llm_healthy(gateway_id)`
- `mark_llm_failed()` → `mark_llm_failed(gateway_id)`
- `is_llm_healthy()` → `is_llm_healthy(gateway_id)`
- `reset_llm_health()` → `reset_llm_health(gateway_id)`

**Call site in `_run_phases`** (around line 538):
```python
# BEFORE:
if gen.get("error") and codegen.is_llm_outage(gen["error"]):
    bok = False
    build_summary = gen["error"]
    break

# AFTER:
model_gateway_id = binding.get("gateway_id") if binding else None
if gen.get("error") and codegen.is_llm_outage(gen["error"]):
    if model_gateway_id:
        mark_llm_failed(model_gateway_id)
    bok = False
    build_summary = gen["error"]
    break
```

### Test Strategy
Update `backend/tests/test_lifecycle_fixes.py`:
- Add test: two gateways, one fails → only that gateway is unhealthy
- Add test: healthy gateway still works after another fails
- Verify threshold behavior per gateway

### Acceptance Criteria
- Gateway A fails 3 times → Gateway A unhealthy, Gateway B unaffected
- Gateway B fails 3 times → Gateway B unhealthy, Gateway A unaffected
- Both gateways work → both healthy
- Existing tests pass

---

## Issue #3: Race Condition in Baseline Proposal

**File**: `backend/app/governance.py` (propose_baseline)
**Severity**: HIGH
**Type**: Concurrency

### Root Cause
```python
# Current: check-then-insert without atomicity
existing = query_one("SELECT ... WHERE status = 'pending_approval'")
if existing:
    return {"error": "already pending"}
# RACE: another request inserts here
insert("project_baselines", ...)
```

### Fix Design
Use optimistic concurrency or a unique partial index.

### Implementation

**Option A: Unique Partial Index (preferred)**

**File**: `backend/app/migrations/011_baseline_uniqueness.py` (new)

```python
"""Migration 011 — prevent duplicate pending baselines per project+kind."""


def apply(conn):
    # Unique constraint: only one pending_approval baseline per (project_id, kind)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_baselines_project_kind_pending
        ON project_baselines(project_id, kind)
        WHERE status = 'pending_approval'
    """)
    conn.commit()
```

**File**: `backend/app/governance.py` (propose_baseline)

```python
# BEFORE:
def propose_baseline(project_id: str, kind: str = "requirement",
                     actor: str = "user") -> dict:
    existing = query_one(
        "SELECT id FROM project_baselines WHERE project_id = ? AND kind = ? "
        "AND status = 'pending_approval'", (project_id, kind))
    if existing:
        return {"error": f"Baseline {kind} already pending approval"}
    # ... insert

# AFTER:
def propose_baseline(project_id: str, kind: str = "requirement",
                     actor: str = "user") -> dict:
    try:
        baseline_id = new_id()
        insert("project_baselines", {
            "id": baseline_id,
            "project_id": project_id,
            "kind": kind,
            "code": f"{KIND_PREFIX.get(kind, 'BL')}-{_next_baseline_number(project_id, kind)}.0",
            # ... rest of insert
        })
        return query_one("SELECT * FROM project_baselines WHERE id = ?", (baseline_id,))
    except sqlite3.IntegrityError:
        return {"error": f"Baseline {kind} already pending approval (concurrent request)"}
```

**Add helper**:
```python
def _next_baseline_number(project_id: str, kind: str) -> int:
    row = query_one(
        "SELECT MAX(CAST(SUBSTR(code, 4, 1) AS INTEGER)) AS n "
        "FROM project_baselines WHERE project_id = ? AND kind = ?",
        (project_id, kind))
    return (row["n"] or 0) + 1
```

### Test Strategy
Update `backend/tests/test_governance_step1.py`:
- Add concurrent baseline proposal test (two threads, expect one success + one error)
- Verify unique index prevents duplicates

### Acceptance Criteria
- Concurrent proposals → exactly one succeeds
- Error message clearly indicates concurrent conflict
- Existing baseline tests pass

---

# PHASE 2: MEDIUM SEVERITY FIXES (🟡)

## Issue #4: Subprocess Calls Without Process-Level Timeout

**File**: `backend/app/toolchains.py`
**Severity**: MEDIUM
**Type**: Reliability

### Root Cause
```python
# Current: no timeout on subprocess.run()
result = subprocess.run(cmd, capture_output=True, text=True)
# If the process hangs, the thread hangs
```

### Fix Design
Add explicit timeout to all subprocess calls.

### Implementation

**File**: `backend/app/toolchains.py`

```python
# ADD at module level:
import os
_DEFAULT_SUBPROCESS_TIMEOUT = int(os.environ.get("TOOLCHAIN_SUBPROCESS_TIMEOUT", "120"))


# REPLACE all subprocess.run() calls:
# BEFORE:
result = subprocess.run(
    [sys.executable, "-m", "py_compile", file_path],
    capture_output=True, text=True
)

# AFTER:
result = subprocess.run(
    [sys.executable, "-m", "py_compile", file_path],
    capture_output=True, text=True,
    timeout=_DEFAULT_SUBPROCESS_TIMEOUT
)

# Apply to:
# - build_check() Python path
# - build_check() .NET path
# - build_check() Go path
# - build_check() Node path
# - run_stack_tests() all paths
# - check_modules() pip install
```

**Add timeout error handling**:
```python
try:
    result = subprocess.run(cmd, ..., timeout=_DEFAULT_SUBPROCESS_TIMEOUT)
except subprocess.TimeoutExpired:
    return {
        "ok": False,
        "summary": f"Command timed out after {_DEFAULT_SUBPROCESS_TIMEOUT}s: {cmd[0]}",
        "timeout": True,
    }
```

### Test Strategy
Update `backend/tests/test_run_workspace.py`:
- Mock `subprocess.run` to raise `TimeoutExpired`
- Verify build check returns timeout error gracefully
- Verify retry logic handles timeout

### Acceptance Criteria
- All subprocess calls have timeout
- Timeout produces clear error message
- Timeout doesn't crash the worker thread
- Existing tests pass

---

## Issue #5: Chatbot `handle_message` is 1300+ Lines

**File**: `backend/app/chatbot.py`
**Severity**: MEDIUM
**Type**: Maintainability

### Root Cause
Single function handles: intent matching, AI routing, command execution, confirmation flows, error handling.

### Fix Design
Split into focused handler functions.

### Implementation

**File**: `backend/app/chatbot.py`

```python
# EXTRACT these handlers from handle_message():

def _match_intent(text: str) -> tuple[str, re.Match] | None:
    """Try regex-based intent matching. Returns (intent_name, match) or None."""
    for intent, pattern in INTENTS.items():
        m = pattern.search(text)
        if m:
            return intent, m
    return None


async def _execute_command(intent: str, match: re.Match, conv_id: str, project_id: str) -> str:
    """Execute a matched typed command."""
    handler = _COMMAND_HANDLERS.get(intent)
    if not handler:
        return "Unknown command"
    try:
        return await handler(match, conv_id, project_id)
    except Exception as exc:
        logger.error("Command %s failed: %s", intent, exc, exc_info=True)
        return f"Error: {_sanitize_exception(exc)}"


async def _handle_confirmation(text: str, conv_id: str, project_id: str) -> str:
    """Handle confirm/cancel for pending commands."""
    text_lower = text.lower().strip()
    if text_lower in ("confirm", "yes", "y"):
        return await _flush_pending(conv_id, project_id)
    elif text_lower in ("cancel", "no", "n"):
        return await _clear_pending(conv_id)
    return None  # Not a confirmation


async def _ai_route(text: str, conv_id: str, project_id: str) -> str:
    """Route to LLM for free-form chat."""
    # ... existing _ai_route logic
    pass


# REFACTORED handle_message:
async def handle_message(project_id: str, conversation_id: str, text: str) -> str:
    """Main entry point for chat messages."""
    # 1. Check for confirmation
    confirm_result = await _handle_confirmation(text, conversation_id, project_id)
    if confirm_result is not None:
        return confirm_result

    # 2. Try typed intent
    intent_match = _match_intent(text)
    if intent_match:
        intent, match = intent_match
        if intent in SENSITIVE:
            return await _queue_pending(intent, match, conversation_id, project_id)
        return await _execute_command(intent, match, conversation_id, project_id)

    # 3. Fall back to AI routing
    return await _ai_route(text, conversation_id, project_id)
```

**Add command handler registry**:
```python
_COMMAND_HANDLERS = {
    "create_gateway": _cmd_create_gateway,
    "test_gateway": _cmd_test_gateway,
    "install_toolchain": _cmd_install_toolchain,
    "create_role": _cmd_create_role,
    "create_persona": _cmd_create_persona,
    # ... all other intents
}
```

### Test Strategy
- Each handler gets unit tests
- Integration test: full message flow through refactored handlers
- Verify all existing chatbot behavior preserved

### Acceptance Criteria
- `handle_message` < 100 lines (orchestrator only)
- Each handler < 200 lines
- All existing chatbot commands work identically
- Test coverage for each handler

---

## Issue #6: Workspace Merge is Not Atomic

**File**: `backend/app/workspace.py` (merge_back_run_workspace)
**Severity**: MEDIUM
**Type**: Reliability

### Root Cause
Files are copied one-by-one. Crash mid-merge leaves partial state.

### Fix Design
Implement atomic merge using temp directory + atomic rename.

### Implementation

**File**: `backend/app/workspace.py`

```python
def merge_back_run_workspace(project_id: str, run_id: str,
                              run_ws: str, manifest: dict) -> dict:
    """Merge run workspace back to project workspace atomically.

    Returns {"applied": [...], "conflicts": [...]}.
    """
    live_ws = query_one(
        "SELECT workspace_path FROM projects WHERE id = ?", (project_id,))["workspace_path"]
    if not live_ws or not os.path.isdir(live_ws):
        return {"applied": [], "conflicts": [], "error": "live workspace missing"}

    # 1. Collect files to merge
    to_apply = manifest.get("modified", []) + manifest.get("added", [])
    if not to_apply:
        return {"applied": [], "conflicts": []}

    # 2. Create temp merge directory
    merge_tmp = live_ws + ".merge-" + run_id[:8]
    os.makedirs(merge_tmp, exist_ok=True)

    applied = []
    conflicts = []

    try:
        # 3. Copy run versions to merge dir
        for rel in to_apply:
            src = os.path.join(run_ws, rel)
            dst = os.path.join(merge_tmp, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(src):
                shutil.copy2(src, dst)
                applied.append(rel)

        # 4. Atomic rename: merge_tmp → live_ws
        # On Windows, use replace; on Unix, rename is atomic
        if os.path.exists(live_ws + ".old"):
            shutil.rmtree(live_ws + ".old", ignore_errors=True)
        os.rename(live_ws, live_ws + ".old")
        os.rename(merge_tmp, live_ws)
        shutil.rmtree(live_ws + ".old", ignore_errors=True)

        return {"applied": applied, "conflicts": conflicts}

    except Exception as exc:
        # Cleanup on failure
        if os.path.exists(merge_tmp):
            shutil.rmtree(merge_tmp, ignore_errors=True)
        if os.path.exists(live_ws + ".old"):
            os.rename(live_ws + ".old", live_ws)
        return {"applied": applied, "conflicts": conflicts,
                "error": f"merge failed: {exc}"}
```

**Note**: For very large workspaces, consider file-level atomicity with a journal.

### Test Strategy
Update `backend/tests/test_run_workspace.py`:
- Test: crash mid-merge (simulate exception) → workspace unchanged
- Test: successful merge is atomic
- Test: merge preserves file permissions

### Acceptance Criteria
- Failed merge leaves workspace in original state
- Successful merge is atomic (no partial state)
- Existing merge tests pass

---

## Issue #7: Chatbot LLM Calls Block Thread During Retries

**File**: `backend/app/chatbot.py`, `backend/app/codegen.py`
**Severity**: MEDIUM
**Type**: Performance

### Root Cause
`GatewayClient.call()` uses `time.sleep()` for retry backoff, blocking the thread pool slot.

### Fix Design
Replace `time.sleep()` with async-compatible backoff, or move LLM calls to a dedicated thread pool.

### Implementation

**Option A: Dedicated thread pool (minimal change)**

**File**: `backend/app/codegen.py`

```python
# ADD:
import concurrent.futures
_llm_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="llm-worker"
)

# In GatewayClient.call():
@classmethod
def call(cls, gw: dict, model: dict, message: str,
         system_prompt: str = "", max_tokens: int = 8000,
         *, retries: int = 2) -> dict:
    """Call the LLM with circuit breaker and exponential backoff retry."""
    last_exc = None
    for attempt in range(retries + 1):
        if cls._breaker(gw["id"]).is_open and attempt > 0:
            raise RuntimeError(f"Circuit breaker open for gateway {gw['id']}")
        try:
            result = call_llm(gw, model["provider_model_id"], system_prompt,
                             message, max_tokens=max_tokens)
            cls._breaker(gw["id"]).record_success()
            return result
        except (RuntimeError, TimeoutError) as exc:
            last_exc = exc
            cls._breaker(gw["id"]).record_failure(str(exc))
            if attempt < retries:
                # Use non-blocking sleep in thread
                time.sleep(min(2 ** attempt, 30))

    raise RuntimeError(f"LLM call failed after {retries + 1} attempts: {last_exc}")
```

**Option B: Async HTTP client (larger refactor)**

Replace `urllib` with `httpx.AsyncClient` throughout. This is the preferred long-term solution but requires more changes.

### Test Strategy
- Load test: concurrent LLM calls don't exhaust thread pool
- Verify retry backoff still works
- Verify circuit breaker still opens

### Acceptance Criteria
- Concurrent LLM calls don't block each other
- Retry behavior unchanged
- Circuit breaker behavior unchanged
- Existing tests pass

---

## Issue #8: No File Size Limits on Generated Code

**File**: `backend/app/codegen.py` (generate_implementation)
**Severity**: MEDIUM
**Type**: Security / Reliability

### Root Cause
LLM can return arbitrarily large file content with no validation.

### Fix Design
Add size limits before writing files.

### Implementation

**File**: `backend/app/codegen.py`

```python
# ADD constants:
_MAX_FILE_SIZE_BYTES = 1_048_576  # 1 MB per file
_MAX_TOTAL_GENERATED_BYTES = 5_242_880  # 5 MB total per task


def _validate_generated_files(files: dict) -> tuple[bool, str]:
    """Validate generated files against size limits."""
    total = 0
    for path, content in files.items():
        size = len(content.encode("utf-8"))
        if size > _MAX_FILE_SIZE_BYTES:
            return False, f"File {path} exceeds size limit ({size} > {_MAX_FILE_SIZE_BYTES} bytes)"
        total += size
        if total > _MAX_TOTAL_GENERATED_BYTES:
            return False, f"Total generated size exceeds limit ({total} > {_MAX_TOTAL_GENERATED_BYTES} bytes)"
    return True, ""


# CALL in generate_implementation() before writing:
def generate_implementation(...):
    # ... generate files dict
    ok, err = _validate_generated_files(files)
    if not ok:
        return {"error": err, "files": {}, "summary": ""}
    # ... continue with writing
```

### Test Strategy
Update `backend/tests/test_critical_fixes.py`:
- Test: generate file > 1MB → rejected
- Test: generate multiple files totaling > 5MB → rejected
- Test: normal-sized files pass

### Acceptance Criteria
- Files > 1MB are rejected with clear error
- Total > 5MB rejected with clear error
- Normal files pass through
- Existing tests pass

---

## Issue #9: `_summary_cache` in tasks.py is Unbounded

**File**: `backend/app/routers/tasks.py` (lines 97-98)
**Severity**: MEDIUM
**Type**: Memory

### Root Cause
```python
_summary_cache: dict[str, tuple[float, dict]] = {}
# Grows forever, no eviction
```

### Fix Design
Use `functools.lru_cache` or implement TTL eviction.

### Implementation

**File**: `backend/app/routers/tasks.py`

```python
# REPLACE:
_summary_cache: dict[str, tuple[float, dict]] = {}
_SUMMARY_TTL_S = 1.5

# WITH:
from functools import lru_cache
import time

_summary_cache: dict[str, tuple[float, dict]] = {}
_MAX_CACHE_SIZE = 100
_SUMMARY_TTL_S = 1.5


def _cache_get(key: str) -> dict | None:
    """Get cached value if not expired."""
    entry = _summary_cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > _SUMMARY_TTL_S:
        del _summary_cache[key]
        return None
    return value


def _cache_put(key: str, value: dict):
    """Put value in cache with eviction."""
    _summary_cache[key] = (time.monotonic(), value)
    # Evict oldest if over limit
    if len(_summary_cache) > _MAX_CACHE_SIZE:
        oldest = min(_summary_cache, key=lambda k: _summary_cache[k][0])
        del _summary_cache[oldest]


# UPDATE _build_summary to use cache:
def _build_summary(pid: str) -> dict:
    cached = _cache_get(f"summary:{pid}")
    if cached is not None:
        return cached
    # ... existing logic
    result = {...}
    _cache_put(f"summary:{pid}", result)
    return result
```

### Test Strategy
- Test: cache returns fresh data within TTL
- Test: cache returns None after TTL
- Test: cache evicts oldest when full
- Test: multiple projects don't interfere

### Acceptance Criteria
- Cache size bounded to 100 entries
- Entries expire after 1.5s
- Oldest entries evicted first
- Existing tests pass

---

## Issue #10: SSE Queue Has No Backpressure

**File**: `backend/app/routers/events.py`
**Severity**: MEDIUM
**Type**: Reliability

### Root Cause
```python
queue = asyncio.Queue()  # No max size
# Slow clients cause unbounded growth
```

### Fix Design
Set max queue size and implement backpressure.

### Implementation

**File**: `backend/app/routers/events.py`

```python
# REPLACE:
queue = asyncio.Queue()

# WITH:
MAX_QUEUE_SIZE = 1000
queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)


# ADD backpressure handling in poll task:
async def _poll_events():
    """Poll for new events and enqueue them."""
    while True:
        try:
            events = await _fetch_new_events()
            for event in events:
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    # Drop oldest event to make room
                    try:
                        queue.get_nowait()
                        queue.put_nowait(event)
                    except asyncio.QueueEmpty:
                        pass
        except Exception:
            await asyncio.sleep(2)


# ADD client health check:
async def _sse_generator(request: Request):
    """Generate SSE events with backpressure handling."""
    conn_id = new_connection_id()
    _sse_connections.add(conn_id)

    try:
        while True:
            try:
                # Wait for event with timeout
                event = await asyncio.wait_for(queue.get(), timeout=30)
                yield f"data: {json.dumps(event)}\n\n"
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
    finally:
        _sse_connections.discard(conn_id)
```

### Test Strategy
- Test: slow client doesn't cause unbounded memory growth
- Test: queue drops oldest when full
- Test: heartbeat sent on timeout
- Test: disconnect cleans up connection

### Acceptance Criteria
- Queue size bounded to 1000
- Slow clients don't cause memory leaks
- Heartbeats keep connection alive
- Existing SSE tests pass

---

## Issue #11: `start_execution` Ignores Idempotency Key

**File**: `backend/app/routers/tasks.py` (lines 161-165)
**Severity**: MEDIUM
**Type**: Correctness

### Root Cause
```python
# Header declared but not used
idempotency_key: str | None = Header(None, alias="Idempotency-Key")
# ...
runtime.start_execution(project_id, task_id)  # key never passed
```

### Fix Design
Pass idempotency key through to `start_execution` and use `run_idempotent`.

### Implementation

**File**: `backend/app/routers/tasks.py`

```python
# BEFORE:
@router.post("/executions")
async def start_execution_route(
    project_id: str,
    task_id: str,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key")
):
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, runtime.start_execution, project_id, task_id)
    return result

# AFTER:
@router.post("/executions")
async def start_execution_route(
    project_id: str,
    task_id: str,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key")
):
    loop = asyncio.get_running_loop()

    def _do():
        return runtime.start_execution(project_id, task_id, idempotency_key=idempotency_key)

    result = await loop.run_in_executor(None, _do)
    return result
```

**File**: `backend/app/runtime.py`

```python
# start_execution already accepts idempotency_key:
def start_execution(project_id: str, task_id: str, idempotency_key: str | None = None):
    with _claim_lock:
        return _start_execution_locked(project_id, task_id, idempotency_key)


# _start_execution_locked already uses it:
def _start_execution_locked(project_id: str, task_id: str,
                            idempotency_key: str | None = None):
    # ...
    if idempotency_key:
        existing = query_one(
            "SELECT * FROM workflow_runs WHERE idempotency_key = ?", (idempotency_key,))
        if existing:
            return {"run": existing, "idempotent_replay": True}
    # ...
```

### Test Strategy
Update `backend/tests/test_lifecycle_fixes.py`:
- Test: same idempotency key twice → returns same run
- Test: different idempotency keys → different runs
- Test: no idempotency key → new run each time

### Acceptance Criteria
- Duplicate idempotency keys return existing run
- No duplicate workflow_runs created
- Existing execution tests pass

---

## Issue #12: Dead Code `_create_module_health_tasks`

**File**: `backend/app/runtime.py`
**Severity**: MEDIUM
**Type**: Maintainability

### Fix Design
Remove dead alias and update references.

### Implementation

**File**: `backend/app/runtime.py`

```python
# REMOVE:
def _create_module_health_tasks(*args, **kwargs):
    """Legacy alias — use sprint_gate._module_rework_specs instead."""
    from .sprint_gate import _module_rework_specs
    return _module_rework_specs(*args, **kwargs)


# SEARCH for any callers:
# grep -r "_create_module_health_tasks" backend/
# If found, replace with sprint_gate._module_rework_specs
```

### Test Strategy
- Run full test suite
- Verify no references remain

### Acceptance Criteria
- Function removed
- No broken references
- Tests pass

---

## Issue #13: Chatbot JSON Repair is Best-Effort

**File**: `backend/app/chatbot.py` (_repair_json)
**Severity**: MEDIUM
**Type**: Reliability

### Root Cause
Truncated JSON may produce invalid structures that execute as commands.

### Fix Design
Add stricter validation after repair.

### Implementation

**File**: `backend/app/chatbot.py`

```python
# ADD:
import json


def _validate_po_response(data: dict) -> tuple[bool, str]:
    """Validate PO response structure."""
    if not isinstance(data, dict):
        return False, "Response must be a JSON object"
    if "reply" not in data:
        return False, "Response must contain 'reply' field"
    if "actions" not in data:
        return False, "Response must contain 'actions' field"
    if not isinstance(data.get("actions"), list):
        return False, "'actions' must be an array"
    # Validate each action
    for i, action in enumerate(data.get("actions", [])):
        if not isinstance(action, dict):
            return False, f"Action {i} must be an object"
        if "action" not in action:
            return False, f"Action {i} missing 'action' field"
    return True, ""


# UPDATE _extract_json:
def _extract_json(text: str) -> dict | None:
    """Extract JSON from LLM response with validation."""
    # ... existing extraction logic
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        data = _repair_json(match.group(1))

    # NEW: validate structure
    if data is not None:
        ok, err = _validate_po_response(data)
        if not ok:
            logger.warning("Invalid PO response: %s", err)
            return None

    return data
```

### Test Strategy
Update `backend/tests/test_process_improvements.py`:
- Test: truncated JSON that can't be repaired → returns None
- Test: valid JSON passes validation
- Test: JSON missing required fields → rejected

### Acceptance Criteria
- Invalid JSON returns None (not executed)
- Valid JSON passes through
- Truncated but repairable JSON still works
- Tests pass

---

## Issue #14: Pending Command Race Condition

**File**: `backend/app/chatbot.py` (_queue_pending)
**Severity**: MEDIUM
**Type**: Concurrency

### Root Cause
Concurrent chatbot sessions could race on `pending_commands` table.

### Fix Design
Use atomic DB operations with conversation-level locking.

### Implementation

**File**: `backend/app/chatbot.py`

```python
# ADD per-conversation lock:
_pending_locks: dict[str, threading.Lock] = {}
_pending_locks_guard = threading.Lock()


def _pending_lock(conv_id: str) -> threading.Lock:
    with _pending_locks_guard:
        if conv_id not in _pending_locks:
            _pending_locks[conv_id] = threading.Lock()
        return _pending_locks[conv_id]


# UPDATE _queue_pending:
def _queue_pending(intent: str, match: re.Match, conv_id: str, project_id: str) -> str:
    """Queue a sensitive command for confirmation."""
    lock = _pending_lock(conv_id)
    with lock:
        # Atomic: delete old + insert new
        execute("DELETE FROM pending_commands WHERE conversation_id = ?", (conv_id,))
        cmd_id = new_id()
        insert("pending_commands", {
            "id": cmd_id,
            "conversation_id": conv_id,
            "command": intent,
            "args_json": json.dumps(match.groupdict()),
            "summary": _summarize_intent(intent, match),
            "created_at": now(),
        })
    return f"⚠️ Confirm: {_summarize_intent(intent, match)}\nReply `confirm` or `cancel`"


# UPDATE _flush_pending:
async def _flush_pending(conv_id: str, project_id: str) -> str:
    """Execute pending command after confirmation."""
    lock = _pending_lock(conv_id)
    with lock:
        pending = query_one(
            "SELECT * FROM pending_commands WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 1",
            (conv_id,))
        if not pending:
            return "Nothing to confirm"
        execute("DELETE FROM pending_commands WHERE id = ?", (pending["id"],))

    # Execute outside lock to avoid deadlock
    intent = pending["command"]
    args = json.loads(pending["args_json"])
    handler = _COMMAND_HANDLERS.get(intent)
    if handler:
        return await handler(args, conv_id, project_id)
    return "Unknown command"
```

### Test Strategy
Update `backend/tests/test_process_improvements.py`:
- Test: concurrent confirm/cancel → no double-execution
- Test: conversation isolation (two conversations don't interfere)

### Acceptance Criteria
- No double-execution under concurrent access
- Conversations don't interfere
- Existing chatbot tests pass

---

## Issue #15: `_or_404` Helper Duplicated Across Routers

**File**: Multiple routers
**Severity**: MEDIUM
**Type**: Maintainability

### Fix Design
Lift to shared utility module.

### Implementation

**File**: `backend/app/routers/_util.py` (new)

```python
"""Shared utilities for routers."""
from fastapi import HTTPException


def or_404(row, what: str = "Resource"):
    """Return row or raise 404."""
    if row is None:
        raise HTTPException(404, f"{what} not found")
    return row


def mask_key(key: str, visible: int = 4) -> str:
    """Mask API key, showing only last N characters."""
    if not key:
        return ""
    if len(key) <= visible:
        return "*" * len(key)
    return "*" * (len(key) - visible) + key[-visible:]
```

**Update all routers**:
```python
# BEFORE (in each router):
def _or_404(row, what="Resource"):
    if row is None:
        raise HTTPException(404, f"{what} not found")
    return row

# AFTER:
from ._util import or_404, mask_key
```

### Test Strategy
- Unit test `or_404` and `mask_key`
- Verify all routers still work

### Acceptance Criteria
- Single source of truth for `_or_404`
- All routers use shared utility
- Tests pass

---

## Issue #16: Circular Import Workaround in projects.py

**File**: `backend/app/routers/projects.py` (lines 88, 138)
**Severity**: MEDIUM
**Type**: Maintainability

### Fix Design
Move event emission to a helper module or use deferred imports properly.

### Implementation

**File**: `backend/app/events.py` (new)

```python
"""Event emission helpers for routers."""
from __future__ import annotations

from typing import Any

from . import db


def emit(project_id: str, event_type: str, payload: dict[str, Any], **kwargs):
    """Emit an execution event. Safe to import from routers."""
    return db.emit_event(project_id, event_type, payload, **kwargs)
```

**File**: `backend/app/routers/projects.py`

```python
# BEFORE:
from __import__("app.db", fromlist=["emit_event"]).emit_event

# AFTER:
from ..events import emit
# Then use: emit(project_id, "event.type", payload, ...)
```

### Test Strategy
- Verify events still emitted correctly
- Check for circular import errors

### Acceptance Criteria
- No `__import__` workarounds
- Events still emitted
- No circular imports
- Tests pass

---

## Issue #17: `_live_model_ids` Uses Synchronous urllib

**File**: `backend/app/routers/gateways.py`
**Severity**: MEDIUM
**Type**: Performance

### Fix Design
Use `httpx` with async/await.

### Implementation

**File**: `backend/app/routers/gateways.py`

```python
# ADD import:
import httpx


# REPLACE:
def _live_model_ids(gateway: dict) -> set[str]:
    """Fetch live model IDs from gateway /v1/models endpoint."""
    # ... existing synchronous urllib code

# WITH:
async def _live_model_ids(gateway: dict) -> set[str]:
    """Fetch live model IDs from gateway /v1/models endpoint."""
    base = gateway["base_url"].rstrip("/")
    if "/v1" not in base:
        base += "/v1"
    url = base + "/models"

    api_key = get_gateway_key(gateway["id"])
    if not api_key:
        return set()

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return {m["id"] for m in data.get("data", [])}
    except Exception:
        return set()
```

**Update call sites to await**:
```python
# BEFORE:
model_ids = _live_model_ids(gateway)

# AFTER:
model_ids = await _live_model_ids(gateway)
```

### Test Strategy
- Mock `httpx.AsyncClient` in tests
- Verify async behavior
- Verify error handling

### Acceptance Criteria
- HTTP calls are async
- Error handling preserved
- Tests pass

---

## Issue #18: No Frontend Routing Library

**File**: `frontend/src/App.tsx`
**Severity**: MEDIUM
**Type**: Architecture

### Fix Design
Introduce React Router v6.

### Implementation

**File**: `frontend/package.json`

```json
{
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1",
    "react-router-dom": "^6.20.0"  # ADD
  }
}
```

**File**: `frontend/src/App.tsx`

```tsx
// BEFORE:
function App() {
  const [page, setPage] = useState("control");
  // ...
  return (
    <Layout>
      {page === "control" && <ControlPage />}
      {page === "flow" && <ProjectFlowPage />}
      {/* ... */}
    </Layout>
  );
}

// AFTER:
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";

function App() {
  return (
    <BrowserRouter>
      <Layout>
        <Routes>
          <Route path="/" element={<Navigate to="/control" replace />} />
          <Route path="/control" element={<ControlPage />} />
          <Route path="/flow" element={<ProjectFlowPage />} />
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/teams" element={<TeamsPage />} />
          <Route path="/models" element={<ModelsPage />} />
          <Route path="/memory" element={<MemoryPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Routes>
      </Layout>
    </BrowserRouter>
  );
}
```

**Update navigation links**:
```tsx
// BEFORE:
<button onClick={() => setPage("control")}>Control</button>

// AFTER:
import { useNavigate } from "react-router-dom";
const navigate = useNavigate();
<button onClick={() => navigate("/control")}>Control</button>
```

### Test Strategy
- Test deep linking (e.g., `/control` loads ControlPage)
- Test browser back/forward
- Test 404 handling
- Update existing tests

### Acceptance Criteria
- Deep links work
- Browser history works
- All pages accessible via URL
- Tests pass

---

## Issue #19: All Frontend State is Local

**File**: `frontend/src/pages/*.tsx`
**Severity**: MEDIUM
**Type**: Architecture

### Fix Design
Introduce Zustand for lightweight global state.

### Implementation

**File**: `frontend/package.json`

```json
{
  "dependencies": {
    "zustand": "^4.4.0"  # ADD
  }
}
```

**File**: `frontend/src/store.ts` (new)

```typescript
import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface AppState {
  activeProject: string | null;
  setActiveProject: (id: string | null) => void;
  theme: 'light' | 'dark';
  toggleTheme: () => void;
  sidebarCollapsed: boolean;
  toggleSidebar: () => void;
}

export const useAppStore = create<AppState>()(
  persist(
    (set) => ({
      activeProject: null,
      setActiveProject: (id) => set({ activeProject: id }),
      theme: 'light',
      toggleTheme: () => set((s) => ({ theme: s.theme === 'light' ? 'dark' : 'light' })),
      sidebarCollapsed: false,
      toggleSidebar: () => set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),
    }),
    { name: 'app-storage' }
  )
);
```

**Update App.tsx**:
```tsx
// BEFORE:
const [activeProject, setActiveProject] = useState<string | null>(null);

// AFTER:
import { useAppStore } from './store';
const activeProject = useAppStore(s => s.activeProject);
const setActiveProject = useAppStore(s => s.setActiveProject);
```

### Test Strategy
- Test state persistence
- Test cross-page state sharing
- Test theme toggle

### Acceptance Criteria
- State shared across pages
- Persisted to localStorage
- Existing functionality preserved
- Tests pass

---

## Issue #20: `dangerouslySetInnerHTML` for Markdown

**File**: `frontend/src/api.ts`
**Severity**: MEDIUM
**Type**: Security (XSS risk)

### Fix Design
Use `react-markdown` with sanitization.

### Implementation

**File**: `frontend/package.json`

```json
{
  "dependencies": {
    "react-markdown": "^9.0.0",
    "react-syntax-highlighter": "^7.3.0",
    "dompurify": "^3.0.0",
    "@types/dompurify": "^3.0.0"  # ADD
  }
}
```

**File**: `frontend/src/api.ts`

```typescript
// BEFORE:
export function md(text: string): string {
  const escaped = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    // ... regex replacements
  return `<div class="md">${escaped}</div>`;
}

// Usage:
<div dangerouslySetInnerHTML={{ __html: md(text) }} />

// AFTER:
import ReactMarkdown from 'react-markdown';
import DOMPurify from 'dompurify';

export function Markdown({ text }: { text: string }) {
  const clean = DOMPurify.sanitize(text);
  return <ReactMarkdown>{clean}</ReactMarkdown>;
}

// Usage:
<Markdown text={text} />
```

### Test Strategy
- Test: XSS payloads are sanitized
- Test: valid markdown renders correctly
- Test: code blocks render

### Acceptance Criteria
- No XSS vulnerabilities
- Markdown renders correctly
- Tests pass

---

# PHASE 3: LOW SEVERITY FIXES (🟢)

## Issue #21: No Log Rotation

**File**: `backend/app/logging_config.py`
**Severity**: LOW
**Type**: Operations

### Fix

```python
from logging.handlers import RotatingFileHandler

# ADD:
def setup_logging():
    handler = RotatingFileHandler(
        "agentforge.log",
        maxBytes=10_485_760,  # 10 MB
        backupCount=5
    )
    handler.setFormatter(...)
    logging.getLogger().addHandler(handler)
```

---

## Issue #22: In-Memory Rate Limiting

**File**: `backend/app/rate_limit.py`
**Severity**: LOW
**Type**: Scalability

### Fix
Add Redis backend option (keep in-memory as fallback):

```python
# ADD:
try:
    import redis
    _redis = redis.Redis(decode_responses=True)
    _use_redis = True
except ImportError:
    _use_redis = False


def _check_rate_limit(key: str, limit: int, window: int) -> bool:
    if _use_redis:
        # Redis sliding window
        count = _redis.incr(key)
        if count == 1:
            _redis.expire(key, window)
        return count <= limit
    else:
        # In-memory fallback
        ...
```

---

## Issue #23: Decrypted API Keys in Memory

**File**: `backend/app/secrets.py`
**Severity**: LOW
**Type**: Security (defense-in-depth)

### Fix
Add caching with TTL:

```python
_key_cache: dict[str, tuple[float, str]] = {}
_KEY_CACHE_TTL = 300  # 5 minutes


def get_gateway_key(gw_id: str) -> str:
    now_ts = time.time()
    cached = _key_cache.get(gw_id)
    if cached and now_ts - cached[0] < _KEY_CACHE_TTL:
        return cached[1]
    key = decrypt(...)
    _key_cache[gw_id] = (now_ts, key)
    return key


def invalidate_key_cache(gw_id: str):
    """Call after key update."""
    _key_cache.pop(gw_id, None)
```

---

## Issue #24: No Environment Overrides for Tunable Parameters

**File**: `backend/app/config.py`
**Severity**: LOW
**Type**: Operations

### Fix
Add `_env()` calls for all tunable parameters:

```python
# Already exists for some, add for rest:
PHASE_TIMEOUT_S = _env_int("PHASE_TIMEOUT_S", 300)
SELF_FIX_ATTEMPTS = _env_int("SELF_FIX_ATTEMPTS", 3)
MAX_REWORK_CYCLES = _env_int("MAX_REWORK_CYCLES", 2)
MAX_GATE_CYCLES = _env_int("MAX_GATE_CYCLES", 3)
SCHEDULER_IDLE_GIVE_UP_BLOCKED = _env_int("SCHEDULER_IDLE_GIVE_UP_BLOCKED", 60)
SCHEDULER_IDLE_GIVE_UP_CLEAN = _env_int("SCHEDULER_IDLE_GIVE_UP_CLEAN", 600)
```

---

## Issue #25: Circuit Breaker State Lost on Restart

**File**: `backend/app/codegen.py`
**Severity**: LOW
**Type**: Reliability

### Fix
Persist breaker state to disk:

```python
_BREAKER_STATE_FILE = "breaker_state.json"


def _load_breakers():
    try:
        with open(_BREAKER_STATE_FILE) as f:
            data = json.load(f)
            for gw_id, state in data.items():
                _breakers[gw_id] = _BreakerState()
                _breakers[gw_id].consecutive_failures = state["consecutive_failures"]
                _breakers[gw_id].opened_at = state["opened_at"]
    except FileNotFoundError:
        pass


def _save_breakers():
    data = {
        gw_id: {
            "consecutive_failures": b.consecutive_failures,
            "opened_at": b.opened_at,
        }
        for gw_id, b in _breakers.items()
    }
    with open(_BREAKER_STATE_FILE, "w") as f:
        json.dump(data, f)


# Call _load_breakers() on startup, _save_breakers() on state change
```

---

## Issue #26: `project lib/` Path May Not Match PYTHONPATH

**File**: `backend/app/toolchains.py`
**Severity**: LOW
**Type**: Correctness

### Fix
Ensure PYTHONPATH includes lib directory:

```python
def check_modules(workspace_path: str, stack: str) -> dict:
    if stack == "python":
        lib_path = os.path.join(workspace_path, "lib")
        # Ensure lib is in PYTHONPATH
        env = os.environ.copy()
        pythonpath = env.get("PYTHONPATH", "")
        if lib_path not in pythonpath:
            env["PYTHONPATH"] = f"{lib_path}{os.pathsep}{pythonpath}" if pythonpath else lib_path
        # Use env in subprocess calls
```

---

## Issue #27: Playwright Port Conflicts

**File**: `backend/app/browser_test.py`
**Severity**: LOW
**Type**: Reliability

### Fix
Add retry with backoff:

```python
def _free_port_with_retry(max_retries: int = 3) -> int:
    for attempt in range(max_retries):
        port = _free_port()
        # Verify port is actually free
        try:
            with socket.socket() as s:
                s.settimeout(1)
                s.connect(("127.0.0.1", port))
            # Port in use, try again
            continue
        except ConnectionRefusedError:
            return port
    raise RuntimeError(f"Could not find free port after {max_retries} attempts")
```

---

## Issue #28: `LEGACY_ALIASES` References `ACTIVE_DEV` Before Definition

**File**: `backend/app/governance.py` (line 50)
**Severity**: LOW
**Type**: Style

### Fix

```python
# Move ACTIVE_DEV definition above LEGACY_ALIASES:
ACTIVE_DEV = "Active Development"
# ... other state constants ...

LEGACY_ALIASES = {"": ACTIVE_DEV, "Active": ACTIVE_DEV}
```

---

## Issue #29: `_NEXT = [1]` Hack for Mutable Integer

**File**: `backend/app/codegen.py` (scaffold)
**Severity**: LOW
**Type**: Style

### Fix
Use proper counter:

```python
# BEFORE (in scaffold template):
_NEXT = [1]

# AFTER:
import itertools
_counter = itertools.count(1)

# In template:
next_id = next(_counter)
```

---

## Issue #30: No Form Validation in Frontend

**File**: `frontend/src/pages/*.tsx`
**Severity**: LOW
**Type**: UX / Correctness

### Fix
Add Zod validation:

```typescript
// File: frontend/src/validation.ts
import { z } from 'zod';

export const TaskSchema = z.object({
  title: z.string().min(1, "Title required").max(200),
  description: z.string().max(2000).optional(),
  story_points: z.number().min(1).max(8),
  priority: z.number().min(1).max(3),
});

export type TaskFormData = z.infer<typeof TaskSchema>;
```

---

## Issue #31: Window.prompt/confirm Used for Critical Actions

**File**: `frontend/src/pages/ProjectsPage.tsx`
**Severity**: LOW
**Type**: UX

### Fix
Replace with Modal components:

```tsx
// BEFORE:
const confirmed = window.confirm("Are you sure?");
if (!confirmed) return;

// AFTER:
const [showConfirm, setShowConfirm] = useState(false);
return (
  <>
    <button onClick={() => setShowConfirm(true)}>Delete</button>
    {showConfirm && (
      <Modal onClose={() => setShowConfirm(false)} onConfirm={handleDelete}>
        <p>Are you sure?</p>
      </Modal>
    )}
  </>
);
```

---

## Issue #32: No Tests for `codegen.generate_implementation()`

**File**: `backend/tests/`
**Severity**: LOW
**Type**: Testing

### Fix
Add integration test:

```python
# File: backend/tests/test_codegen_integration.py
def test_generate_implementation_with_mock_llm(monkeypatch, isolated_db):
    """Test codegen with mocked LLM responses."""
    def mock_call_llm(gw, model, system, user, **kw):
        return {
            "text": "```python\nprint('hello')\n```",
            "input_tokens": 100,
            "output_tokens": 50,
        }

    monkeypatch.setattr("app.codegen.call_llm", mock_call_llm)

    project = {...}  # Seed project
    task = {...}  # Seed task
    result = codegen.generate_implementation(project, task, agent_id, feedback="")

    assert "files" in result
    assert len(result["files"]) > 0
    assert result["error"] == ""
```

---

## Issue #33: No Lifespan Startup Tests

**File**: `backend/tests/`
**Severity**: LOW
**Type**: Testing

### Fix
Add lifespan test:

```python
# File: backend/tests/test_lifespan.py
from fastapi.testclient import TestClient
from contextlib import asynccontextmanager

@asynccontextmanager
async def test_lifespan(app):
    yield  # Actually run lifespan

def test_lifespan_startup(monkeypatch):
    """Test lifespan startup sequence."""
    startup_calls = []

    original_init_db = appdb.init_db
    def mock_init_db():
        startup_calls.append("init_db")

    monkeypatch.setattr("app.db.init_db", mock_init_db)

    with TestClient(app) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert "init_db" in startup_calls
```

---

## Issue #34: No Security/Authorization Tests

**File**: `backend/tests/`
**Severity**: LOW
**Type**: Testing / Security

### Fix
Add authorization test suite:

```python
# File: backend/tests/test_authorization.py
def test_cross_project_access_denied(client, project_a, project_b):
    """Verify agents can't access other projects' data."""
    # Create task in project_a
    task = create_task(project_a["id"], ...)

    # Try to access from project_b
    response = client.get(f"/api/v1/projects/{project_a['id']}/tasks")
    assert response.status_code == 403  # or 404


def test_delete_agent_with_active_task_denied(client, agent, task):
    """Verify can't delete agent with active task."""
    response = client.delete(f"/api/v1/agents/{agent['id']}")
    assert response.status_code == 409
```

---

## Issue #35: Test Files Duplicate DB Bootstrap

**File**: `backend/tests/conftest.py` + 6 test modules
**Severity**: LOW
**Type**: Maintainability

### Fix
Remove duplication, rely on conftest:

```python
# REMOVE from all test files:
# - AGENT_OFFICE_DB setup
# - sys.path.insert
# - appdb.init_db() calls

# KEEP in conftest.py only
```

---

## Issue #36: No pytest Configuration

**File**: `backend/tests/`
**Severity**: LOW
**Type**: Testing

### Fix
Add `pytest.ini`:

```ini
[pytest]
testpaths = tests
pythonpath = ..
addopts = -ra -q
filterwarnings =
    ignore::DeprecationWarning
    ignore::PendingDeprecationWarning
```

---

## Issue #37: Hardcoded Windows venv Paths

**File**: Multiple test files
**Severity**: LOW
**Type**: Style

### Fix
Use environment variable:

```python
# BEFORE:
"""
Run with: %USERPROFILE%\\.verdent\\agentforge-venv\\Scripts\\python.exe -m pytest
"""

# AFTER:
"""
Run with: python -m pytest
(Virtual environment should be activated)
"""
```

---

## Issue #38: `useAbortable` Hook is Never Used

**File**: `frontend/src/useAbortable.ts`
**Severity**: LOW
**Type**: Dead Code

### Fix
Remove or integrate:

```typescript
// Option 1: Remove
// Delete useAbortable.ts and any references

// Option 2: Integrate into polling
export function useAbortablePolling(fn: () => void, interval: number) {
  const abortController = useRef<AbortController>(null);

  useEffect(() => {
    abortController.current = new AbortController();
    const id = setInterval(fn, interval);
    return () => {
      clearInterval(id);
      abortController.current?.abort();
    };
  }, [fn, interval]);
}
```

---

## Issue #39: No Frontend Test Runner

**File**: `frontend/package.json`
**Severity**: LOW
**Type**: Testing

### Fix
Add Vitest:

```json
{
  "devDependencies": {
    "vitest": "^1.0.0",
    "@testing-library/react": "^14.0.0",
    "@testing-library/jest-dom": "^6.0.0"
  },
  "scripts": {
    "test": "vitest",
    "test:ui": "vitest --ui"
  }
}
```

**Add vitest config**:
```typescript
// vite.config.ts
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
  },
});
```

---

## Issue #40: `_call_llm` Blocks Thread During Retries

**File**: `backend/app/chatbot.py`
**Severity**: LOW
**Type**: Performance

### Fix
Same as Issue #7 — use dedicated thread pool.

---

# IMPLEMENTATION SCHEDULE

> Reflects current state: 6 issues already fixed, auth not pursued, 22 remaining issues.

## Week 1: HIGH Severity (Issues #5, #7, #13, #14 — original #1, #2, #3 already fixed)

**Note**: Issues #1-3 (codegen precedence, per-gateway health, baseline race) are already resolved.

- [ ] Day 1-2: Refactor chatbot `handle_message` (Issue #5 — was #5, 1300-line monolith)
- [ ] Day 2-3: Fix thread-blocking LLM retries in chatbot (Issue #7 — was #7)
- [ ] Day 3-4: Add workspace merge atomicity (Issue #6 — was #6)
- [ ] Day 4-5: Add JSON validation + pending command lock (Issues #13, #14)

## Week 2: MEDIUM Severity — Reliability & Concurrency (Issues #6, #8, #10, #11, #12)

**Note**: Issue #4 (subprocess timeouts) and #9 (summary cache) are already resolved.

- [ ] Day 1-2: Implement atomic workspace merge (Issue #6)
- [ ] Day 2-3: File size limits already enforced — add per-project disk quota (Issue #8 partial)
- [ ] Day 3-4: Add SSE queue backpressure (Issue #10)
- [ ] Day 4-5: Fix idempotency key passthrough (Issue #11)
- [ ] Day 5: Remove dead code `_create_module_health_tasks` (Issue #12)

## Week 3: MEDIUM Severity — Maintainability (Issues #15, #16, #17)

**Note**: Issue #15 (`_or_404` dedup) is already resolved.

- [ ] Day 1-2: Fix circular import in projects.py (Issue #16)
- [ ] Day 2-3: Replace synchronous urllib with httpx in gateways.py (Issue #17)
- [ ] Day 3-5: Remaining maintainability cleanup

## Week 4: MEDIUM Severity — Architecture & Frontend (Issues #18, #19, #20)

**Note**: Issue #20 (`dangerouslySetInnerHTML`) is a security concern (covered in security review), not in this non-security plan.

- [ ] Day 1-2: Add React Router for frontend routing (Issue #18)
- [ ] Day 2-3: Add Zustand for global state (Issue #19)
- [ ] Day 4-5: Replace dangerouslySetInnerHTML with react-markdown + DOMPurify (Issue #20 — security)

## Week 5-6: LOW Severity (Issues #21-40)

**Note**: Several items already partially addressed (rate limiting has Redis fallback, pytest.ini added).

- [ ] Day 1-2: Log rotation, rate limiting Redis backend
- [ ] Day 3-4: Key caching with TTL, env overrides for tunable params, breaker persistence
- [ ] Day 5-6: Frontend fixes (validation, modals, remove dead code)
- [ ] Day 7-8: Test improvements (coverage, config, deduplication, new test files)
- [ ] Day 9-10: Cleanup, documentation, final verification

---

---

# TESTING STRATEGY

## Unit Tests
- Each fix includes specific test cases
- Target: 90%+ code coverage
- Run: `pytest backend/tests/ -v --cov=app --cov-report=html`

## Integration Tests
- Test cross-module interactions
- Test concurrent access patterns
- Test failure scenarios

## Frontend Tests
- Component tests with Vitest
- E2E tests with Playwright
- Target: 80%+ component coverage

## Performance Tests
- Load test: concurrent task execution
- Stress test: SQLite write contention
- Memory test: long-running deployment simulation

---

# ROLLBACK PLAN

## For Each Fix

1. **Branch strategy**: Each fix in its own feature branch
2. **Commit granularity**: One fix per commit
3. **Testing**: Full test suite passes before merge
4. **Feature flags**: Use env vars to disable risky changes
5. **Monitoring**: Watch for errors in first 24h after deploy

## Emergency Rollback

```bash
# Rollback specific fix
git revert <commit-hash>
pytest backend/tests/ -v  # Verify rollback
systemctl restart agentforge  # Restart service
```

---

# SUCCESS METRICS

## Already Achieved (6 of 40)
- ✅ `is_llm_outage` precedence bug fixed (correctness)
- ✅ Per-gateway LLM health tracking implemented
- ✅ Baseline proposal race condition resolved (threading lock)
- ✅ Subprocess timeouts enforced process-wide
- ✅ `_summary_cache` memory leak eliminated (bounded cache)
- ✅ `_or_404` helper consolidated across most routers

## Remaining Targets (22 of 40)
### Code Quality
- ⏳ All 22 remaining issues fixed
- ⏳ 90%+ test coverage maintained
- ⏳ Zero linting errors
- ⏳ TypeScript strict mode passes

### Performance
- ⏳ No memory leaks in 24h stress test
- ⏳ SSE queue bounded (already partial)
- ⏳ Cache eviction working (already partial)

### Reliability
- ⏳ No duplicate workflow runs with idempotency keys (Issue #11)
- ⏳ Workspace merge is atomic (Issue #6)
- ⏳ LLM retries don't block thread pool (Issue #7)

### Maintainability
- ⏳ Chatbot split into focused handlers (Issue #5)
- ⏳ No dead code (Issue #12)
- ⏳ No circular import workarounds (Issue #16)
- ⏳ Async HTTP for gateways (Issue #17)

### Not Pursued (1 of 40)
- 🚫 Authentication/authorization — intentional for single-user deployment

---

*Generated by AgentForge Architecture Review — 2026*
