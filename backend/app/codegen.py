"""Real code generation for AgentForge task runs.

Tasks no longer produce only markdown deliverables: during the implement
phase the assigned agent's role model (or the control-bot gateway as
fallback) generates real, runnable application code which is written into
the project workspace, and the build-test / QA phases execute the
workspace's pytest suite for real.

When no usable LLM gateway is configured the module falls back to a
deterministic, runnable FastAPI + pytest scaffold so the workspace always
contains working code.
"""
import json
import asyncio
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error as uerr
import httpx

from .db import get_gateway_key, query, query_one
from .llm.resolver import resolve_project_model
from . import budget, config, memory, toolchains


def _charge_llm_call(project, model, resp: dict) -> None:
    """Record a live call's estimated cost against the project's in-flight
    budget so the NEXT call in the same run sees the spend (usage_records
    only land at run end)."""
    if resp.get("dry_run"):
        return
    pid = (project or {}).get("id") if isinstance(project, dict) else None
    if not pid:
        return
    in_rate = float((model or {}).get("input_cost_per_m") or config.LLM_INPUT_COST_PER_M)
    out_rate = float((model or {}).get("output_cost_per_m") or config.LLM_OUTPUT_COST_PER_M)
    cost = (resp.get("input_tokens") or 0) / 1e6 * in_rate + \
           (resp.get("output_tokens") or 0) / 1e6 * out_rate
    budget.add_inflight(pid, cost)


# ---------------------------------------------------------------
# Persona / role instruction injection into LLM prompts
# ---------------------------------------------------------------

def _role_instructions_block(role_id: str) -> str:
    """Collect active instruction files for a role and format them
    as a prompt fragment the LLM can follow. Returns '' when the role
    has no instructions or role_id is falsy."""
    if not role_id:
        return ""
    rows = query(
        "SELECT filename, content FROM instruction_files "
        "WHERE role_id = ? AND active = 1 ORDER BY filename",
        (role_id,),
    )
    if not rows:
        return ""
    parts = []
    for row in rows:
        fn = row["filename"] or "instruction"
        content = (row["content"] or "").strip()
        if content:
            parts.append(f"### {fn}\n{content}")
    if not parts:
        return ""
    return (
        "\n\n## YOUR ROLE INSTRUCTIONS (follow these exactly)\n"
        + "\n\n".join(parts)
        + "\n"
    )


def _persona_block(persona: dict) -> str:
    """Format a persona record as a prompt fragment."""
    if not persona:
        return ""
    parts = []
    instr = (persona.get("instructions") or "").strip()
    if instr:
        parts.append(f"## Persona: {persona.get('name', 'Unnamed')}\n{instr}")
    constraints = (persona.get("constraints_text") or "").strip()
    if constraints:
        parts.append(f"## Hard Constraints\n{constraints}")
    if not parts:
        return ""
    return "\n\n" + "\n\n".join(parts) + "\n"


def _persona_override() -> dict | None:
    """PERSONA_OVERRIDE=/path/to.json swaps the ENTIRE persona + role
    instruction source for a test run, so a developer can iterate on one
    role's prompts from a local file without touching the database (and
    without affecting other roles). Expected JSON shape:

    {"junior": false,
     "role_instructions": [{"name": "...", "content": "..."}],
     "persona": {"name": "...", "instructions": "...", "constraints_text": "..."}}
    """
    path = os.environ.get("PERSONA_OVERRIDE", "").strip()
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def build_system_prompt(stack: str, persona: dict, role_id: str) -> str:
    """Compose the full  for a code generation run.

    Order (highest-priority last, so it overrides earlier generic text):
    1. Stack-specific system (_CODEGEN_SYSTEMS / _JR_DEV_SYSTEM)
    2. Role instruction files from the database
    3. Persona instructions + constraints from the database

    The persona/constraints are intentionally placed last so they override
    any generic guidance in the stack prompt without duplicating it.

    With PERSONA_OVERRIDE set, steps 2-3 come from the local JSON file
    instead of the database — the whole system prompt is then editable and
    diffable from one file.
    """
    ov = _persona_override()
    if ov is not None:
        base = _JR_DEV_SYSTEM if (stack == "python" and ov.get("junior")) else _system_for(stack)
        role_block = "".join(
            f"\n\n## {r.get('name', 'Role instruction')}\n{(r.get('content') or '').strip()}"
            for r in ov.get("role_instructions") or [] if (r.get("content") or "").strip())
        p = ov.get("persona") or {}
        persona_block = _persona_block({
            "name": p.get("name") or "Override Persona",
            "instructions": p.get("instructions") or "",
            "constraints_text": p.get("constraints_text") or "",
        }) if (p.get("instructions") or p.get("constraints_text")) else ""
        return base + role_block + persona_block

    is_jr = False
    if persona and persona.get("role_id"):
        is_jr = bool(query_one(
            "SELECT 1 FROM roles WHERE id = ? AND lower(name) = lower('Junior Developer') LIMIT 1",
            (persona["role_id"],),
        ))
    base = _JR_DEV_SYSTEM if (stack == "python" and is_jr) else _system_for(stack)

    role_block = _role_instructions_block(role_id)
    persona_block = _persona_block(persona)

    return base + role_block + persona_block


# ---------------------------------------------------------------- LLM access

def _truthy_env(key: str) -> bool:
    return (os.environ.get(key) or "").strip().lower() in ("1", "true", "yes", "on")


def _dry_run_enabled() -> bool:
    """DRY_RUN=1 makes every LLM call return a deterministic stub instead of
    hitting a provider: iterate on prompts/personas in milliseconds at zero
    cost. The stub is valid codegen JSON (empty file list), so the full
    runtime path still executes."""
    return _truthy_env("DRY_RUN")


_DRY_RUN_TEXT = json.dumps({"summary": "dry run: no code generated (DRY_RUN=1)", "files": []})
_trace_lock = threading.Lock()
_trace_seq = 0


def _trace_prompt(system_prompt: str, user_text: str, label: str,
                  model: str, dry_run: bool) -> None:
    """DEBUG_PROMPTS=1 dumps the FINAL composed prompt (the one thing you
    can't reconstruct from logs: task + workspace + persona + helpers +
    memory all merged) to backend/debug/prompts/ for post-mortem debugging."""
    global _trace_seq
    if not _truthy_env("DEBUG_PROMPTS"):
        return
    try:
        with _trace_lock:
            _trace_seq += 1
            seq = _trace_seq
        out_dir = os.path.join(
            os.environ.get("DEBUG_PROMPTS_DIR")
            or os.path.join(os.path.dirname(__file__), "..", "debug", "prompts"),
            (label or "llm").replace("/", "-").replace(" ", "_"))
        os.makedirs(out_dir, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
        path = os.path.join(out_dir, f"{stamp}-{seq:04d}-{model or 'none'}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"# AgentForge prompt trace\n"
                     f"# time: {stamp}Z  label: {label}  model: {model}  dry_run: {dry_run}\n"
                     f"# system_chars: {len(system_prompt or '')}  user_chars: {len(user_text or '')}\n\n"
                     f"===== SYSTEM PROMPT =====\n{system_prompt}\n\n"
                     f"===== USER PROMPT =====\n{user_text}\n")
    except OSError:
        pass  # tracing must never break the call it instruments


def _dry_run_response() -> dict:
    return {"text": _DRY_RUN_TEXT, "input_tokens": 0, "output_tokens": 0,
            "latency_ms": 0, "dry_run": True}


def call_llm(gw, model, system_prompt: str, user_text: str, max_tokens: int = 8000,
             timeout: int = 300, trace_label: str = "") -> dict:
    """Live inference call. Returns {"text", "input_tokens", "output_tokens"}."""
    import urllib.error as uerr
    import urllib.request as ureq

    dry = _dry_run_enabled()
    _trace_prompt(system_prompt, user_text, trace_label,
                  (model or {}).get("provider_model_id") or "", dry)
    if dry:
        return _dry_run_response()

    api_key = get_gateway_key(gw["id"])
    if not api_key:
        raise RuntimeError("No API key stored for the configured gateway")
    base = gw["base_url"].rstrip("/")
    is_anthropic = gw["api_type"] == "anthropic-messages"
    if is_anthropic:
        if base.endswith("/messages"):
            url = base
        elif base.endswith("/v1"):
            url = base + "/messages"
        else:
            url = base + "/v1/messages"
        # Anthropic prompt caching: the system block is stable within a task
        # (persona + role instructions + stack base) and re-sent on every
        # self-fix/retry call, so mark it ephemeral-cacheable — repeated
        # calls then read it at ~10% of the input price (AGENT_DEV_SPEEDUP 3.1).
        payload = {"model": model["provider_model_id"], "max_tokens": max_tokens,
                   "system": [{"type": "text", "text": system_prompt,
                               "cache_control": {"type": "ephemeral"}}],
                   "messages": [{"role": "user", "content": user_text}]}
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
    else:
        if "/v1" not in base:
            base += "/v1"
        url = base + "/chat/completions"
        payload = {"model": model["provider_model_id"], "max_tokens": max_tokens,
                   "messages": [{"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_text}]}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    req = ureq.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    started = time.perf_counter()
    try:
        with ureq.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except uerr.HTTPError as e:
        raise RuntimeError(f"Gateway returned HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
    except Exception as exc:
        raise RuntimeError(f"Could not reach gateway: {exc}")
    usage = data.get("usage") or {}
    if is_anthropic:
        text = "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        in_toks = usage.get("input_tokens") or 0
        out_toks = usage.get("output_tokens") or 0
    else:
        text = data["choices"][0]["message"]["content"] or ""
        in_toks = usage.get("prompt_tokens") or 0
        out_toks = usage.get("completion_tokens") or 0
    return {
        "text": text,
        "input_tokens": in_toks,
        "output_tokens": out_toks,
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


async def acall_llm(gw, model, system_prompt: str, user_text: str,
                    max_tokens: int = 8000, timeout: int = 300,
                    trace_label: str = "") -> dict:
    """Async transport for chat so HTTP and retry backoff use no worker slot."""
    dry = _dry_run_enabled()
    _trace_prompt(system_prompt, user_text, trace_label,
                  (model or {}).get("provider_model_id") or "", dry)
    if dry:
        return _dry_run_response()

    api_key = get_gateway_key(gw["id"])
    if not api_key:
        raise RuntimeError("No API key stored for the configured gateway")
    base = gw["base_url"].rstrip("/")
    anthropic = gw["api_type"] == "anthropic-messages"
    if anthropic:
        url = base if base.endswith("/messages") else base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        payload = {"model": model["provider_model_id"], "max_tokens": max_tokens,
                   "system": [{"type": "text", "text": system_prompt,
                               "cache_control": {"type": "ephemeral"}}],
                   "messages": [{"role": "user", "content": user_text}]}
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    else:
        url = (base if "/v1" in base else base + "/v1") + "/chat/completions"
        payload = {"model": model["provider_model_id"], "max_tokens": max_tokens,
                   "messages": [{"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_text}]}
        headers = {"Authorization": f"Bearer {api_key}"}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"Gateway returned HTTP {exc.response.status_code}: {exc.response.text[:200]}") from exc
    except (httpx.RequestError, ValueError) as exc:
        raise RuntimeError(f"Could not reach gateway: {exc}") from exc
    usage = data.get("usage") or {}
    if anthropic:
        message = "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        in_toks, out_toks = usage.get("input_tokens") or 0, usage.get("output_tokens") or 0
    else:
        message = data["choices"][0]["message"]["content"] or ""
        in_toks, out_toks = usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0
    return {"text": message, "input_tokens": in_toks, "output_tokens": out_toks,
            "latency_ms": int((time.perf_counter() - started) * 1000)}


# ---------------------------------------------------------------------------
# Circuit breaker + retry wrapper
# ---------------------------------------------------------------------------

class _BreakerState:
    """Per-gateway circuit breaker state."""
    __slots__ = ("consecutive_failures", "opened_at", "opened_at_wall", "last_failure")

    def __init__(self):
        self.consecutive_failures: int = 0
        self.opened_at: float = 0.0
        self.opened_at_wall: float = 0.0
        self.last_failure: str = ""

    @property
    def is_open(self) -> bool:
        return self.consecutive_failures >= 5

    @property
    def is_half_open(self) -> bool:
        if not self.is_open:
            return False
        return (time.monotonic() - self.opened_at) > 60

    def record_success(self):
        self.consecutive_failures = 0
        self.opened_at = 0.0
        self.opened_at_wall = 0.0
        self.last_failure = ""

    def record_failure(self, reason: str):
        self.consecutive_failures += 1
        if self.is_open:
            self.opened_at = time.monotonic()
            self.opened_at_wall = time.time()
        self.last_failure = reason


_BREAKER_STATE_FILE = os.path.join(os.path.dirname(config.DB_PATH), "breaker_state.json")


def _load_breakers() -> dict[str, _BreakerState]:
    """Restore only still-active cooldowns, converting wall time to monotonic."""
    try:
        with open(_BREAKER_STATE_FILE, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(saved, dict):
        return {}
    breakers = {}
    for gateway_id, state in saved.items():
        try:
            failures = max(0, int(state["consecutive_failures"]))
            opened_wall = float(state.get("opened_at_wall") or 0)
            elapsed = max(0.0, time.time() - opened_wall)
            if failures >= 5 and elapsed >= 60:
                continue
            breaker = _BreakerState()
            breaker.consecutive_failures = failures
            if failures >= 5:
                breaker.opened_at_wall = opened_wall
                breaker.opened_at = time.monotonic() - elapsed
                breaker.last_failure = "prior provider failure"
            breakers[str(gateway_id)] = breaker
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
    return breakers


def _save_breakers(breakers: dict[str, _BreakerState]) -> None:
    """Persist breaker counters atomically without writing error details."""
    data = {gateway_id: {
        "consecutive_failures": state.consecutive_failures,
        "opened_at_wall": state.opened_at_wall,
    } for gateway_id, state in breakers.items() if state.consecutive_failures}
    directory = os.path.dirname(_BREAKER_STATE_FILE)
    try:
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".breaker-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(temp_path, _BREAKER_STATE_FILE)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
    except OSError:
        # Breaker persistence is best effort; inference must still work.
        pass


class GatewayClient:
    """High-level gateway client with circuit breaker and retry.

    All gateway calls in the runtime and chatbot should go through this
    class rather than calling :func:`call_llm` or rolling their own
    ``_call_llm`` helpers.

    Usage::

        result = await GatewayClient.call(gw, model, user_message, max_tokens=512)
        text = result["text"]
    """

    _breakers: dict[str, _BreakerState] = _load_breakers()
    _lock = threading.Lock()

    @classmethod
    def _record_success(cls, breaker: _BreakerState):
        with cls._lock:
            breaker.record_success()
            _save_breakers(cls._breakers)

    @classmethod
    def _record_failure(cls, breaker: _BreakerState, reason: str):
        with cls._lock:
            breaker.record_failure(reason)
            _save_breakers(cls._breakers)

    @classmethod
    def _breaker(cls, gateway_id: str) -> _BreakerState:
        with cls._lock:
            if gateway_id not in cls._breakers:
                cls._breakers[gateway_id] = _BreakerState()
            return cls._breakers[gateway_id]

    @classmethod
    def call(cls, gw: dict, model: dict, message: str,
             system_prompt: str = "",
             max_tokens: int = 8000,
             *, retries: int = 2, trace: str = "gateway") -> dict:
        """Call the LLM with circuit breaker and exponential backoff retry.

        On transient errors (timeout, connection reset) the call is retried
        up to *retries* times with exponential backoff starting at 1 s.
        After 5 consecutive failures the breaker opens for 60 s; the
        half-open probe is the next retry attempt.
        """
        breaker = cls._breaker(gw["id"])

        if breaker.is_open and not breaker.is_half_open:
            raise RuntimeError(
                f"Circuit breaker open for gateway '{gw.get('name', gw['id'])}' "
                f"(last failure: {breaker.last_failure}). Retry after 60 s."
            )

        last_exc: Exception | None = None
        for attempt in range(max(1, retries + 1)):
            try:
                result = call_llm(gw, model, system_prompt, message, max_tokens,
                                  trace_label=trace)
                cls._record_success(breaker)
                latency_ms = result.get("latency_ms", 0)
                result["latency_ms"] = latency_ms
                return result
            except (uerr.URLError, TimeoutError, RuntimeError) as exc:
                last_exc = exc
                is_timeout = isinstance(exc, (TimeoutError,))
                is_transient = isinstance(exc, RuntimeError) and (
                    "Could not reach gateway" in str(exc) or is_timeout)
                if not is_transient:
                    cls._record_failure(breaker, str(exc))
                    raise
                cls._record_failure(breaker, str(exc))
                wait = min(2 ** attempt, 8)
                time.sleep(wait)

        raise RuntimeError(
            f"Gateway '{gw.get('name', gw['id'])}' unreachable after {retries + 1} attempts: "
            f"{last_exc}"
        )

    @classmethod
    async def acall(cls, gw: dict, model: dict, message: str,
                    system_prompt: str = "", max_tokens: int = 8000,
                    *, retries: int = 2, trace: str = "gateway") -> dict:
        breaker = cls._breaker(gw["id"])
        if breaker.is_open and not breaker.is_half_open:
            raise RuntimeError(f"Circuit breaker open for gateway '{gw.get('name', gw['id'])}'. Retry after 60 s.")
        last_exc = None
        for attempt in range(max(1, retries + 1)):
            try:
                result = await acall_llm(gw, model, system_prompt, message, max_tokens,
                                         trace_label=trace)
                cls._record_success(breaker)
                return result
            except (TimeoutError, RuntimeError) as exc:
                last_exc = exc
                cls._record_failure(breaker, str(exc))
                transient = isinstance(exc, TimeoutError) or "Could not reach gateway" in str(exc)
                if not transient:
                    raise
                if attempt < retries:
                    await asyncio.sleep(min(2 ** attempt, 8))
        raise RuntimeError(
            f"Gateway '{gw.get('name', gw['id'])}' unreachable after {retries + 1} attempts: {last_exc}")


def resolve_llm(project):
    """Pick (gateway, model) for code generation via the central resolver."""
    resolved = resolve_project_model(project)
    if not resolved:
        return None, None
    return resolved.gateway, resolved.model


def is_llm_outage(error: str) -> bool:
    """True when a codegen error is an LLM outage/quota condition — the
    caller must stop retrying (each retry burns quota) and block with the
    clear reason instead of burning self-fix / health rounds."""
    return bool(error) and (
        _llm_outage_reason(error) != ""
        or error.lower().startswith(("llm usage limit", "llm quota",
                                     "llm rate limited", "llm provider overloaded"))
    )


_MAX_FILE_SIZE_BYTES = 1_048_576
_MAX_TOTAL_GENERATED_BYTES = 5_242_880


def _extract_object(text: str) -> dict:
    """Parse a complete model JSON object without chatbot-specific fields."""
    start = text.find("{")
    if start < 0:
        return {}
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _validate_generated_files(files: list[tuple[str, str]]) -> str:
    """Return an error before any generated file is written, or an empty string."""
    total = 0
    for rel, content in files:
        size = len(content.encode("utf-8"))
        if size > _MAX_FILE_SIZE_BYTES:
            return f"File {rel} exceeds size limit ({size} > {_MAX_FILE_SIZE_BYTES} bytes)"
        total += size
        if total > _MAX_TOTAL_GENERATED_BYTES:
            return ("Total generated size exceeds limit "
                    f"({total} > {_MAX_TOTAL_GENERATED_BYTES} bytes)")
    return ""


def _llm_outage_reason(text: str) -> str:
    """Detect an LLM outage / quota / rate-limit / unreachable-gateway
    message in a response text OR a raised error string. Returns a clean
    reason string, or '' when this looks like a normal (but unusable)
    reply. These must surface as outages, not 'no usable files' — the
    model literally cannot help right now, and a scaffold must never
    mask that (the contract in generate_implementation)."""
    low = text.lower()
    for pat, label in (
        ("usage limit", "LLM usage limit reached (quota exhausted)"),
        ("quota", "LLM quota exhausted"),
        ("rate limit", "LLM rate limited"),
        ("rate_limit", "LLM rate limited"),
        ("http 429", "LLM rate limited (HTTP 429)"),
        ("too many requests", "LLM rate limited (HTTP 429)"),
        ("overloaded", "LLM provider overloaded"),
        ("insufficient", "LLM insufficient credits/balance"),
        ("temporarily unavailable", "LLM temporarily unavailable"),
        ("service unavailable", "LLM service unavailable"),
        ("could not reach gateway", "LLM gateway unreachable"),
        ("unreachable after", "LLM gateway unreachable"),
        ("circuit breaker open", "LLM gateway circuit breaker open"),
        ("timed out", "LLM gateway request timed out"),
        ("timeout", "LLM gateway request timed out"),
    ):
        if pat in low:
            return label
    # 5xx from the HTTPError text ("Gateway returned HTTP 502: ...")
    if re.search(r"http 5\d\d", low):
        return "LLM gateway server error (HTTP 5xx)"
    return ""


# ---------------------------------------------------------------- review gates

_REVIEW_SYSTEM = {
    "sa": (
        "You are a Solution Architect reviewing one completed development task "
        "in an autonomous software project. Judge ONLY architecture alignment: "
        "module placement, dependency direction, interface/file-contract "
        "compliance, duplicate helper implementations, and whether the change "
        "respects the project's stated stack and conventions. Do not re-run "
        "the build — automated gates already did that. Return ONLY a JSON "
        'object: {"decision": "approved"|"rework", "rework_class": '
        '"ARCHITECTURE_VIOLATION"|"CONTRACT_MISMATCH"|"HELPER_DUPLICATION"|"", '
        '"findings": "one or two concrete sentences, empty when approved"}'),
    "ba": (
        "You are a Business Analyst reviewing one completed development task "
        "in an autonomous software project. Judge ONLY functional alignment: "
        "does the implemented behavior serve the task description, the "
        "acceptance criteria and the project's business rules, including "
        "obvious edge cases? Do not review code style or architecture. "
        'Return ONLY a JSON object: {"decision": "approved"|"rework", '
        '"rework_class": "FUNCTIONAL_MISMATCH"|"REQUIREMENT_GAP"|"", '
        '"findings": "one or two concrete sentences, empty when approved"}'),
    "cr": (
        "You are a Senior Developer performing a code review on one completed "
        "development task in an autonomous software project. Judge ONLY code "
        "quality and correctness of the change: readability, dead or duplicated "
        "code, missing error handling on the paths the change touches, obvious "
        "bugs, and violations of the project's stated conventions. Do NOT re-run "
        "the build (automated gates already did) and do NOT judge business "
        "requirements or high-level architecture. Return ONLY a JSON object: "
        '{"decision": "approved"|"rework", "rework_class": '
        '"CODE_QUALITY"|"CORRECTNESS"|"CONVENTION"|"", "findings": "one or two '
        'concrete sentences, empty when approved"}'),
}


def review_verdict(project, task, mode: str, agent_ref: str, deadline: float | None = None) -> dict:
    """One LLM call for an SA or BA review gate. Returns
    {"decision": "approved"|"rework"|"unavailable", "rework_class",
     "findings", "input_tokens", "output_tokens"}. 'unavailable' means the
    gate could not run (no gateway, outage, unusable reply) — the pipeline
    skips the gate instead of punishing the developer. ``deadline`` (a
    time.monotonic() stamp) bounds the HTTP call so a slow provider cannot
    hang the review run."""
    empty = {"decision": "unavailable", "rework_class": "", "findings": "",
             "input_tokens": 0, "output_tokens": 0}
    b_ok, b_reason = budget.may_spend(project["id"])
    if not b_ok:
        return {**empty, "findings": f"{b_reason} — review gate skipped"}
    gw, model = resolve_llm(project)
    if not (gw and model):
        return {**empty, "findings": "No LLM gateway configured — review gate skipped"}
    call_timeout = 300
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining < 15:
            return {**empty, "findings": "review phase budget exhausted — gate skipped"}
        call_timeout = max(15, min(300, int(remaining) - 5))
    tree = _existing_tree(project["workspace_path"])[:2500]
    ac = task["acceptance_criteria"] or ""
    if task["functional_ac"]:
        ac = (ac + "\n" if ac else "") + "FUNCTIONAL AC: " + task["functional_ac"]
    if task["technical_ac"]:
        ac = (ac + "\n" if ac else "") + "TECHNICAL AC: " + task["technical_ac"]
    contracts_hint = ""
    if mode == "sa":
        try:
            from . import specs as _specs
            contracts_hint = _specs.dev_contracts_block(project["id"], task)
        except Exception:
            contracts_hint = ""
    user = (
        f"PROJECT: {project['name']}\nGOAL: {project['goal'] or 'n/a'}\n"
        f"STACK: {project['technology_stack'] or 'n/a'}\n"
        f"WORKSPACE FILES:\n{tree}\n\n"
        f"TASK: {task['title']}\n{task['description']}\n"
        f"ACCEPTANCE CRITERIA:\n{ac or 'n/a'}\n\n"
        f"{contracts_hint}"
        f"CHANGE EVIDENCE: {(task['evidence'] or '')[:600]}"
        f"{_changed_files_block(project['workspace_path'], task['evidence'] or '')}\n\n"
        "Is this implementation acceptable from your reviewing role?")
    try:
        review_sys = _REVIEW_SYSTEM.get(mode, "")
        agent_row = query_one(
            "SELECT a.persona_id, a.role_id, r.name AS role_name FROM agents a "
            "JOIN roles r ON r.id = a.role_id "
            "WHERE a.id = ? OR lower(a.name) = lower(?) LIMIT 1",
            (agent_ref, agent_ref or ""),
        )
        if agent_row:
            p = query_one("SELECT * FROM personas WHERE id = ?", (agent_row["persona_id"],)) if agent_row.get("persona_id") else None
            review_sys = review_sys + _persona_block(p) + _role_instructions_block(agent_row.get("role_id"))
        raw = call_llm(gw, model, review_sys, user, max_tokens=500,
                       trace_label="review",
                       timeout=call_timeout)
        _charge_llm_call(project, model, raw)
    except Exception as exc:
        return {**empty, "findings": f"review call failed: {str(exc)[:160]}"}
    text = raw.get("text") or ""
    data = _extract_object(text)
    decision = str(data.get("decision") or "").lower()
    tokens = {"input_tokens": raw.get("input_tokens", 0),
              "output_tokens": raw.get("output_tokens", 0)}
    if decision not in ("approved", "rework"):
        outage = _llm_outage_reason(text)
        reason = outage or "reviewer returned unusable output — gate skipped"
        return {**empty, **tokens, "findings": reason}
    return {"decision": decision,
            "rework_class": str(data.get("rework_class") or "")[:60],
            "findings": str(data.get("findings") or "")[:600], **tokens}


_APPROVE_SYSTEM = """You are the Product Owner approval gate for a finished task.
The task already passed every automated stage of the project's delivery flow
(development, reviews, tests). Your job is the final business sign-off.

Judge ONLY what a product owner judges:
- does the change deliver the task's goal and acceptance criteria?
- is the evidence concrete (files changed, tests run, behaviour observed)?
- anything shipped that the task never asked for?

Reply with ONE JSON object and nothing else:
{"decision": "approve" | "rework", "findings": "<one paragraph, <=400 chars>"}
Use "rework" only for a real business gap, and say exactly what to change."""


def approval_verdict(project, task, evidence: str) -> dict:
    """PO-agent sign-off for a task parked in 'Pending Approval'. Returns
    {"decision": "approve"|"rework"|"unavailable", "findings": ...}; an
    unusable reply or missing gateway yields 'unavailable' so the caller can
    leave the task with the human instead of guessing."""
    empty = {"decision": "unavailable", "findings": ""}
    b_ok, b_reason = budget.may_spend(project["id"])
    if not b_ok:
        return {**empty, "findings": b_reason}
    gw, model = resolve_llm(project)
    if not (gw and model):
        return {**empty, "findings": "No LLM gateway configured"}
    user = (f"PROJECT: {project['name']}\nGOAL: {project['goal'] or 'n/a'}\n"
            f"TASK: {task['title']}\n{task['description']}\n"
            f"ACCEPTANCE CRITERIA:\n{(task['acceptance_criteria'] or 'n/a')[:1200]}\n\n"
            f"EVIDENCE: {(evidence or task['evidence'] or '')[:900]}\n\n"
            "Sign off or send it back?")
    try:
        raw = call_llm(gw, model, _APPROVE_SYSTEM, user, max_tokens=400,
                       trace_label="approval", timeout=120)
        _charge_llm_call(project, model, raw)
    except Exception as exc:
        return {**empty, "findings": f"approval call failed: {str(exc)[:160]}"}
    data = _extract_object(raw.get("text") or "")
    decision = str(data.get("decision") or "").lower()
    if decision not in ("approve", "rework"):
        return {**empty, "findings": "PO returned unusable output — left for the human"}
    return {"decision": decision,
            "findings": str(data.get("findings") or "")[:600]}


# ---------------------------------------------------------------- codegen

_SHARED_RULES = """- Contribute to ONE coherent application with a conventional
  layout for its stack — extend existing files rather than creating
  task-named one-offs.
- NEVER name a file after the task title.
- REUSE the shared helpers file (see REUSABLE HELPERS below): call its
  functions instead of re-implementing small utilities. New tiny reusable
  utilities go into that same file (return its FULL updated content).
- Non-source files (launcher scripts like .bat/.sh, README.md, .env.example,
  Dockerfile, config) are expected whenever the task asks for them — create
  exactly the requested file type.
- Every change MUST come with or update tests that pass with the stack's
  standard test runner.
- The app is verified in a REAL headless browser (Playwright smoke): it
  must boot, every page must render content without uncaught JS errors,
  internal links must resolve, and form submits must not return server
  errors. Write browser-testable acceptance criteria and UI accordingly.
- Keep the full content of every file you touch (no diffs, no placeholders
  like "...", no TODO-only stubs). 6 files maximum."""

_CODEGEN_SYSTEMS = {
    "python": """You are a senior software engineer implementing one task of a real project.
Return ONLY a JSON object (no markdown fences, no prose, no tool calls):
{"summary": "<one line>", "files": [{"path": "<relative/path>", "content": "<full file content>"}]}
Structure rules:
- Contribute to ONE coherent application with a conventional layout:
  app/main.py (FastAPI app + include_router calls), app/routers/, app/services/,
  app/models.py, app/helpers.py (shared reusable helpers — owned by the
  Junior Developer), requirements.txt, tests/test_*.py.
- If you add an APIRouter, register it in app/main.py and return main.py's
  FULL updated content in files.
- Use FastAPI for web/API projects; otherwise plain Python modules.
""" + _SHARED_RULES,

    "dotnet": """You are a senior .NET engineer implementing one task of a real project (WPF,
WinForms, ASP.NET or a class library).
Return ONLY a JSON object (no markdown fences, no prose, no tool calls):
{"summary": "<one line>", "files": [{"path": "<relative/path>", "content": "<full file content>"}]}
Structure rules:
- Provide/maintain the .sln (or single .csproj), one project per concern:
  src/<App>/ for the application (WPF XAML + code-behind/MVVM, or ASP.NET
  controllers/services) and tests/<App>.Tests/ for xUnit tests
  ([Fact]/[Theory] in *.Tests.csproj with Microsoft.NET.Test.Sdk + xunit).
- Include <TargetFramework> that matches the installed SDK (net8.0 unless the
  project already declares another), and <Nullable>enable</Nullable>.
- For WPF include App.xaml/App.xaml.cs and MainWindow when relevant; use
  MVVM (ViewModels + INotifyPropertyChanged) for new UI logic.
- Tests must pass with `dotnet test`.
""" + _SHARED_RULES,

    "go": """You are a senior Go engineer implementing one task of a real project.
Return ONLY a JSON object (no markdown fences, no prose, no tool calls):
{"summary": "<one line>", "files": [{"path": "<relative/path>", "content": "<full file content>"}]}
Structure rules:
- Provide/maintain go.mod (module name from the project), cmd/<app>/main.go
  for entrypoints, internal/ packages for logic.
- Idiomatic Go: exported symbols documented, errors wrapped with %w, no panics
  for expected failures.
- Table-driven tests in *_test.go (testing package) next to the code they
  cover; they must pass with `go test ./...`.
""" + _SHARED_RULES,

    "node": """You are a senior Node.js/TypeScript engineer implementing one task of a real project.
Return ONLY a JSON object (no markdown fences, no prose, no tool calls):
{"summary": "<one line>", "files": [{"path": "<relative/path>", "content": "<full file content>"}]}
Structure rules:
- Provide/maintain package.json (name, scripts: build/test/start, typed
  dependencies with pinned versions), src/ for code, tests/ for suites.
- Use vitest or jest (declare as devDependency) with tests in
  tests/*.test.ts or *.test.js; they must pass with `npm test`.
- If TypeScript, include tsconfig.json and keep `tsc --noEmit` clean.
""" + _SHARED_RULES,
}


def _system_for(stack: str) -> str:
    return _CODEGEN_SYSTEMS.get(stack) or _CODEGEN_SYSTEMS["python"]


_JR_DEV_SYSTEM = """You are the Junior Developer implementing one task of a real project.
Return ONLY a JSON object (no markdown fences, no prose, no tool calls):
{"summary": "<one line>", "files": [{"path": "<relative/path>", "content": "<full file content>"}]}
Your specialty is small, easy-to-build REUSABLE functions that save the team
tokens: every utility a teammate could re-implement belongs in the shared
helpers file (app/helpers.py) so the next agent just calls it.
Rules:
- Grow app/helpers.py: append tiny, typed, single-purpose functions (one
  concern each, ~10 lines max) and return the file's FULL updated content.
- Never duplicate a helper that already exists — extend or fix it instead.
- Update tests/test_helpers.py with one focused test per new helper.
- Only touch other files when the task explicitly needs feature code; then
  import and call the helpers rather than inlining utility logic.
""" + _SHARED_RULES


def _is_junior_dev(agent_ref: str) -> bool:
    """True when the implementer (agent id or name) has the Junior
    Developer role."""
    ref = (agent_ref or "").strip()
    if not ref:
        return False
    row = query_one(
        "SELECT r.name AS role_name FROM agents a JOIN roles r ON r.id = a.role_id "
        "WHERE a.id = ? OR lower(a.name) = lower(?) LIMIT 1", (ref, ref))
    return bool(row) and "junior" in (row["role_name"] or "").lower()


def _tree_stamp(ws_dir: str) -> tuple:
    """Cheap fingerprint of a workspace's file layout: max mtime over all
    directories plus the total file count. Creating/removing/renaming any
    file changes its directory's mtime, so the pair detects layout changes
    without building the tree string."""
    max_mtime = 0.0
    file_count = 0
    for root, dirs, files in os.walk(ws_dir):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache", "node_modules")]
        file_count += len(files)
        try:
            max_mtime = max(max_mtime, os.stat(root).st_mtime)
        except OSError:
            pass
    return (round(max_mtime, 4), file_count)


# ws_dir -> (layout fingerprint, rendered tree). Bounded: one entry per active
# workspace; projects are few, and stale workspaces age out on first miss.
_tree_cache: dict[str, tuple] = {}
_TREE_CACHE_MAX = 32


def _existing_tree(ws_dir: str, limit: int = 60) -> str:
    # Identical workspace layout (the common case across retries and
    # self-fix rounds of one task) reuses the cached string instead of
    # re-walking and re-rendering the tree on every LLM call.
    try:
        stamp = _tree_stamp(ws_dir)
    except OSError:
        stamp = None
    if stamp is not None:
        hit = _tree_cache.get(ws_dir)
        if hit and hit[0] == stamp:
            return hit[1]
    entries = []
    for root, dirs, files in os.walk(ws_dir):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache", "node_modules")]
        rel = os.path.relpath(root, ws_dir)
        for f in files:
            entries.append(os.path.normpath(os.path.join(rel, f)).replace("\\", "/"))
    entries = sorted(entries)[:limit]
    out = "\n".join(entries) or "(empty workspace)"
    if stamp is not None:
        if len(_tree_cache) >= _TREE_CACHE_MAX and ws_dir not in _tree_cache:
            _tree_cache.pop(next(iter(_tree_cache)))
        _tree_cache[ws_dir] = (stamp, out)
    return out


_SKIP_DIRS = (".git", "__pycache__", ".pytest_cache", "node_modules")
_SRC_SUFFIXES = (".py", ".js", ".ts", ".tsx", ".go", ".cs", ".html", ".css")


def _read_capped(ws_dir: str, rel: str, cap: int) -> str | None:
    safe = _safe_rel_path(rel)
    if not safe:
        return None
    path = os.path.join(ws_dir, safe.replace("/", os.sep))
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
    except OSError:
        return None
    return f"--- {safe} ---\n{src[:cap]}" + ("\n... (truncated)" if len(src) > cap else "")


def _changed_files_block(ws_dir: str, evidence: str, max_files: int = 4,
                         per_file: int = 3500) -> str:
    """Contents of the files THIS task actually wrote (its evidence carries
    a 'code:<path>' entry per written file). SA/BA reviewers must judge the
    real code, not a filename tree."""
    if not (ws_dir and evidence):
        return ""
    seen, blocks = set(), []
    for m in re.finditer(r"code:([^;]+)", evidence):
        rel = m.group(1).strip().replace("\\", "/")
        if not rel or rel in seen:
            continue
        seen.add(rel)
        b = _read_capped(ws_dir, rel, per_file)
        if b:
            blocks.append(b)
        if len(blocks) >= max_files:
            break
    if not blocks:
        return ""
    return ("\n\nFILES CHANGED BY THIS TASK (actual current content):\n"
            + "\n".join(blocks))


def _task_relevant_files(ws_dir: str, task: dict, exclude: set,
                         max_files: int = 3, per_file: int = 3000) -> str:
    """Existing source files whose path matches the task's own terminology,
    included with real content: the dev integrates with the codebase
    instead of guessing at it from a filename list."""
    text = f"{task.get('title') or ''} {task.get('description') or ''}".lower()
    words = re.findall(r"[a-z0-9]{4,}", text)
    if not words or not ws_dir:
        return ""
    candidates = []
    for root, dirs, files in os.walk(ws_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        rel_dir = os.path.relpath(root, ws_dir).replace("\\", "/")
        for f in files:
            rel = f if rel_dir == "." else f"{rel_dir}/{f}"
            if rel in exclude or not rel.endswith(_SRC_SUFFIXES):
                continue
            hay = rel.lower().replace("_", " ").replace(".", " ").replace("/", " ")
            score = sum(hay.count(w) for w in words)
            if score:
                candidates.append((score, rel))
    candidates.sort(key=lambda t: (-t[0], t[1]))
    blocks = [b for b in (_read_capped(ws_dir, rel, per_file)
                          for _, rel in candidates[:max_files]) if b]
    if not blocks:
        return ""
    return ("\n\nRELATED EXISTING CODE (same terminology as the task — extend and "
            "integrate with these files, never duplicate them):\n" + "\n".join(blocks))


def _safe_rel_path(p: str) -> str | None:
    p = (p or "").replace("\\", "/").strip().lstrip("/")
    if not p or ".." in p.split("/") or p.startswith((".", "~")) or ":" in p:
        return None
    return p


# ---------------------------------------------------------------- shared helpers
# One reusable helper function file per project workspace, owned by the
# team's Junior Developer. Teammates CALL these helpers instead of
# re-implementing small utilities, so every task regenerates less code —
# fewer output tokens and smaller prompts on later passes.

HELPERS_REL_PATH = "app/helpers.py"

_STARTER_HELPERS = '''"""Shared reusable helpers — owned by the team's Junior Developer.

Small, typed, easy-to-reuse functions. CALL these instead of
re-implementing them: less duplicated code, fewer tokens per task.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone


def slugify(text: str) -> str:
    """URL/file-safe slug: 'Hello World!' -> 'hello-world'."""
    cleaned = "".join(c if c.isalnum() else " " for c in (text or "").lower())
    return "-".join(cleaned.split()) or "item"


def truncate(text: str, limit: int = 200) -> str:
    """Cut text to limit, appending '...' when it was longer."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def compact_json(data) -> str:
    """Smallest possible JSON string (no spaces, ascii kept as-is)."""
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=str)


def deep_get(data: dict, path: str, default=None):
    """Safely read nested keys: deep_get(d, "user.address.city")."""
    for key in path.split("."):
        if not isinstance(data, dict) or key not in data:
            return default
        data = data[key]
    return data


def parse_bool(value, default: bool = False) -> bool:
    """Forgiving bool from strings ('true'/'1'/'yes'/'on') or None."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def now_iso() -> str:
    """UTC timestamp string, e.g. '2026-09-20T15:20:20+00:00'."""
    return datetime.now(timezone.utc).isoformat()


def paginate(items: list, page: int = 1, size: int = 20) -> dict:
    """Standard pagination envelope: items, page, size, total."""
    page, size = max(1, page), max(1, min(size, 200))
    start = (page - 1) * size
    return {"items": items[start:start + size], "page": page,
            "size": size, "total": len(items)}
'''

_STARTER_HELPERS_TEST = '''"""Tests for the shared helpers (owned by the Junior Developer)."""
from app.helpers import compact_json, deep_get, paginate, parse_bool, slugify, truncate


def test_slugify():
    assert slugify("Hello World!") == "hello-world"
    assert slugify("  Multiple   Spaces  ") == "multiple-spaces"
    assert slugify("!!!") == "item"


def test_truncate():
    assert truncate("short") == "short"
    assert len(truncate("x" * 500, 50)) == 50
    assert truncate("x" * 500, 50).endswith("...")


def test_compact_json():
    assert compact_json({"a": 1, "b": 2}) == '{"a":1,"b":2}'


def test_deep_get():
    assert deep_get({"a": {"b": 7}}, "a.b") == 7
    assert deep_get({}, "a.b.c", "fallback") == "fallback"


def test_parse_bool():
    assert parse_bool("yes") is True
    assert parse_bool("0") is False
    assert parse_bool(None, default=True) is True


def test_paginate():
    result = paginate(list(range(50)), page=2, size=10)
    assert result["items"][0] == 10
    assert result["total"] == 50
'''


def ensure_helpers_file(ws_dir: str) -> bool:
    """Create the shared helpers file (+ its tests) when missing. Returns
    True when the starter file was written, False when it already existed."""
    path = os.path.join(ws_dir, HELPERS_REL_PATH.replace("/", os.sep))
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_STARTER_HELPERS)
    test_path = os.path.join(ws_dir, "tests", "test_helpers.py")
    if not os.path.exists(test_path):
        os.makedirs(os.path.dirname(test_path), exist_ok=True)
        with open(test_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_STARTER_HELPERS_TEST)
    return True


def _helpers_index(ws_dir: str, limit: int = 40) -> str:
    """Compact one-line-per-function index of the helpers file (signature +
    first docstring line) for prompts. Signatures only — never the full
    file — so the prompt stays small and tokens stay low."""
    path = os.path.join(ws_dir, HELPERS_REL_PATH.replace("/", os.sep))
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
    except OSError:
        return ""
    entries = []
    for m in re.finditer(
            r'^def (\w+)\(([^)]*)\)(\s*->\s*[^:\n]+)?:\s*\n\s*"""([^"\n]*)',
            src, re.M):
        sig = f"{m.group(1)}({m.group(2).strip()})" + (m.group(3) or "").rstrip()
        doc = m.group(4).strip()
        entries.append(f"- {sig} — {doc}" if doc else f"- {sig}")
    return "\n".join(entries[:limit])


def _feedback_file_blocks(ws_dir: str, feedback: str, limit: int = 3) -> str:
    """Contents of workspace files the failure feedback points at, so the
    model can actually fix the file that failed instead of guessing blind."""
    if not feedback or not ws_dir:
        return ""
    seen, blocks = set(), []
    for m in re.finditer(r"[A-Za-z0-9_./\\-]+\.(?:py|js|ts|tsx|go|cs|html|css|json|md)", feedback):
        rel = m.group(0).replace("\\", "/").lstrip("./")
        safe = _safe_rel_path(rel)
        if not safe or safe in seen:
            continue
        path = os.path.join(ws_dir, safe.replace("/", os.sep))
        if not os.path.isfile(path):
            continue
        seen.add(safe)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                src = fh.read()
        except OSError:
            continue
        blocks.append(f"CURRENT {safe} (this file FAILED — fix it):\n{src[:6000]}"
                      + ("\n... (truncated)" if len(src) > 6000 else ""))
        if len(blocks) >= limit:
            break
    return ("\n\n" + "\n\n".join(blocks)) if blocks else ""


def _find_agent(agent_ref: str):
    """Resolve an agent row by id (what the runtime passes) or by name."""
    if not agent_ref:
        return None
    return (query_one("SELECT * FROM agents WHERE id = ?", (agent_ref,))
            or query_one("SELECT * FROM agents WHERE lower(name) = lower(?)", (agent_ref,)))


def _agent_role_name(agent) -> str:
    if not agent:
        return ""
    r = query_one("SELECT name FROM roles WHERE id = ?", (agent["role_id"],))
    return r["name"] if r else ""


def _binding_chain(binding_id: str):
    """Ordered fallback chain carried by ONE Models-catalog entry.

    A model is now an ordered set of gateway+model members (``members_json``);
    the agent just picks this one model and inference fails over across its
    members. Each usable member is ``{ref, gateway_id, model_id, gw, model}``.
    Members whose gateway has no stored key or whose model row is gone are
    skipped. Falls back to the row's own ``gateway_id``/``model_id`` when
    ``members_json`` is empty (pre-chain data), so existing models keep working.
    """
    if not binding_id:
        return []
    row = query_one("SELECT gateway_id, model_id, members_json FROM model_bindings "
                    "WHERE id = ? AND active = 1", (binding_id,))
    if not row:
        return []
    try:
        members = json.loads(row["members_json"]) if row["members_json"] else []
    except (ValueError, TypeError):
        members = []
    if not (isinstance(members, list) and members):
        members = [{"gateway_id": row["gateway_id"], "model_id": row["model_id"]}]
    chain, seen = [], set()
    for m in members:
        if not isinstance(m, dict):
            continue
        gid = str(m.get("gateway_id") or "")
        mid = str(m.get("model_id") or "")
        ref = f"{gid}:{mid}"
        if not gid or not mid or ref in seen:
            continue
        gw = query_one("SELECT * FROM gateways WHERE id = ?", (gid,))
        gm = query_one("SELECT * FROM gateway_models WHERE id = ?", (mid,))
        if gw and gm and get_gateway_key(gw["id"]):
            chain.append({"ref": ref, "gateway_id": gid, "model_id": mid, "gw": gw, "model": gm})
            seen.add(ref)
    return chain


def _model_chain(project, agent_ref):
    """Ordered fallback chain the agent runs on, as
    ``([{'ref', 'gateway_id', 'model_id', 'gw', 'model'}, ...], role_name)``.

    The agent picks a single Model from the catalog; that model's member list is
    the chain. Falls back to the project/bot default (a one-member chain) when
    the agent has no binding or the binding resolves to nothing usable, so
    behaviour matches the old single-model resolution.
    """
    agent = _find_agent(agent_ref)
    role_name = _agent_role_name(agent)
    chain = []
    if agent and agent["model_binding_id"]:
        chain = _binding_chain(agent["model_binding_id"])
    if not chain:
        gw, model = resolve_llm(project)
        if gw and model:
            chain = [{"ref": f"{gw['id']}:{model.get('id') or ''}",
                      "gateway_id": gw["id"], "model_id": model.get("id"),
                      "gw": gw, "model": model}]
    return chain, role_name


def _ordered_for_pin(chain: list, pinned_ref):
    """Reorder the chain so the pinned (already-working-this-task) member is
    tried first, keeping the rest as continued fallback. An unknown/None pin
    leaves the chain untouched (re-check from the top)."""
    if not pinned_ref:
        return chain
    pinned = [e for e in chain if e.get("ref") == pinned_ref]
    if not pinned:
        return chain
    rest = [e for e in chain if e.get("ref") != pinned_ref]
    return pinned + rest


def _output_budget(task, project_id: str | None = None) -> int:
    """Estimated max output tokens for one codegen call. Small/fix-up tasks
    rarely need the full 8000-token envelope; a tight budget makes the
    provider stream faster and bills less, while anything ambiguous keeps the
    generous default so a file is never cut off mid-JSON. When the project
    has a capped budget, the remaining headroom shrinks the envelope so a
    nearly-spent project degrades to small replies instead of big ones."""
    try:
        points = int(task["points"] or 0)
    except (KeyError, IndexError, TypeError, ValueError):
        points = 0
    if points and points <= 2:
        cap = 4000
    else:
        cap = 8000
        text = ""
        for key in ("title", "description"):
            try:
                text += f" {task[key] or ''}"
            except (KeyError, IndexError, TypeError):
                pass
        lowered = text.lower()
        if points <= 3 and any(w in lowered for w in
                               ("fix", "rename", "typo", "copy", "label",
                                "config", "message", "text", "icon")):
            cap = 4000
    if project_id:
        headroom = budget.headroom_usd(project_id)
        if headroom is not None:
            cap = min(cap, 1500 if headroom < 1 else 4000 if headroom < 5 else cap)
    return cap


def generate_implementation(project, task, agent_ref: str, feedback: str = "",
                            pinned_ref: str | None = None,
                            deadline: float | None = None) -> dict:
    """Generate real code files for a task into the project workspace.

    Returns {"files": [relative paths], "summary": str, "mode": "llm"|"scaffold",
             "input_tokens": int, "output_tokens": int, "error": str|""} plus
             "pinned_ref" / "used_model_id" / "used_provider_model_id" (the
             member that actually served the call) and "fallbacks"
             ([{model, error}] for each earlier chain member that was skipped).

    The chosen Model's ordered member chain is tried in priority order; a member
    that errors, hits an outage, or returns no usable files falls through to the
    next. ``pinned_ref`` (the member that already worked earlier in the SAME
    task) is tried first so the chain isn't re-probed on every call —
    re-checking from the top happens only when a new task run starts.
    ``deadline`` (time.monotonic() stamp) bounds the whole call: each HTTP
    request is capped at the remaining budget and the chain stops trying new
    members when it runs out, so the worker thread returns before the
    runtime's hard phase-timeout backstop (which would strand it).
    Never raises: python LLM failures degrade to the deterministic scaffold;
    non-python stacks return an error so the rework loop reports honestly
    instead of bolting a FastAPI file onto a Go/.NET/Node project.
    """
    ws_dir = project["workspace_path"]
    stack = toolchains.detect_stack(project, ws_dir)
    result = {"files": [], "summary": "", "mode": "scaffold", "input_tokens": 0,
              "output_tokens": 0, "error": "", "pinned_ref": None,
              "used_model_id": None, "used_gateway_id": None,
              "used_provider_model_id": None, "fallbacks": []}
    if not ws_dir or not os.path.isdir(ws_dir):
        result["error"] = "workspace missing"
        return result

    # The Junior Developer's shared reusable-function file: present in every
    # python workspace so all agents reuse it instead of regenerating
    # boilerplate (fewer output tokens, smaller later prompts).
    helpers_block = ""
    if stack == "python":
        ensure_helpers_file(ws_dir)
        index = _helpers_index(ws_dir)
        if index:
            helpers_block = ("\nREUSABLE HELPERS in app/helpers.py (maintained by the Junior "
                             "Developer — CALL these instead of re-implementing, it saves tokens):\n"
                             f"{index}\n")

    chain, role_name = _model_chain(project, agent_ref)
    if chain:
        # Resolve the agent's persona and role once, so both the
        # stack-specific base system and the per-role overrides flow
        # into the same LLM call.
        agent_row = query_one(
            "SELECT a.*, r.name AS role_name FROM agents a "
            "JOIN roles r ON r.id = a.role_id "
            "WHERE a.id = ? OR lower(a.name) = lower(?) LIMIT 1",
            (agent_ref, agent_ref or ""),
        )
        persona = None
        persona_id = None
        role_id = None
        if agent_row:
            role_id = agent_row.get("role_id")
            persona_id = agent_row.get("persona_id")
        if persona_id:
            persona = query_one("SELECT * FROM personas WHERE id = ?", (persona_id,)) or None
        stack_token = stack or "python"
        system_prompt = build_system_prompt(stack_token, persona, role_id)
        feedback_block = f"\n\nPREVIOUS ATTEMPT FEEDBACK (fix these issues):\n{feedback}\n" if feedback else ""
        main_py = os.path.join(ws_dir, "app", "main.py")
        main_block = "(app/main.py does not exist yet)"
        if os.path.isfile(main_py):
            try:
                with open(main_py, encoding="utf-8", errors="replace") as fh:
                    src = fh.read()
                main_block = src[:4000] + ("\n... (truncated)" if len(src) > 4000 else "")
            except OSError:
                pass
        # Blueprint contracts (V3 Step 2): when the SA has written file
        # contracts for the files this task names, the dev must build exactly
        # against them — the SA review gate then judges conformance to the
        # same written contracts.
        contracts_block = ""
        try:
            from . import specs as _specs
            contracts_block = _specs.dev_contracts_block(project["id"], task)
        except Exception:
            contracts_block = ""
        user_prompt = (f"PROJECT: {project['name']}\nGOAL: {project['goal'] or 'n/a'}\n"
                       f"TECH STACK: {project['technology_stack'] or 'Python (FastAPI where appropriate)'}\n\n"
                       f"TASK TO IMPLEMENT: {task['title']}\n"
                       f"DESCRIPTION: {task['description'] or 'n/a'}\n"
                       f"ACCEPTANCE CRITERIA: {task['acceptance_criteria'] or 'n/a'}"
                       f"{feedback_block}\n"
                       f"{contracts_block}"
                       f"EXISTING WORKSPACE FILES:\n{_existing_tree(ws_dir)}\n"
                       f"{helpers_block}\n"
                       f"CURRENT app/main.py:\n{main_block}"
                       f"{_task_relevant_files(ws_dir, task, {'app/main.py', HELPERS_REL_PATH})}"
                       f"{_feedback_file_blocks(ws_dir, feedback)}"
                       f"{memory.pitfalls_block(project['id'])}\n\n"
                       "Implement this task now. Return the JSON object with complete file contents.")

        def _usable_files(text: str):
            data = _extract_object(text)
            files = []
            if data and isinstance(data.get("files"), list):
                for f in data["files"]:
                    rel = _safe_rel_path(str(f.get("path") or ""))
                    content = f.get("content")
                    if rel and isinstance(content, str) and content.strip():
                        files.append((rel, content))
            return data, files

        # Try each member of the (pin-aware) chain in order. A member that
        # errors, reports an outage, or returns no usable files is recorded and
        # we move to the next; the first to produce files wins and is returned.
        for entry in _ordered_for_pin(chain, pinned_ref):
            gw, model = entry["gw"], entry["model"]
            model_label = model.get("provider_model_id") or model.get("display_name") or "?"
            b_ok, b_reason = budget.may_spend(project["id"])
            if not b_ok:
                # Mid-run enforcement: self-fix cycles and chain fall-through
                # bill against the in-flight ledger, so a run can stop the
                # moment the project's cap is reached instead of after it.
                result["fallbacks"].append({"model": model_label, "error": b_reason})
                result["error"] = b_reason
                break
            max_out = _output_budget(task, project["id"])
            call_timeout = 300
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining < 20:
                    # Out of phase budget — record honestly and stop trying
                    # members; the caller's evidence shows the exhaustion.
                    result["fallbacks"].append(
                        {"model": model_label, "error": "phase budget exhausted"})
                    result["error"] = result["error"] or \
                        "LLM phase budget exhausted before this member was tried"
                    break
                call_timeout = max(15, min(300, int(remaining) - 10))
            try:
                resp = call_llm(gw, model, system_prompt, user_prompt, max_tokens=max_out,
                                trace_label=f"codegen-task-{str(task.get('id') if isinstance(task, dict) else task['id'])[:8]}",
                                timeout=call_timeout)
                _charge_llm_call(project, model, resp)
                data, files = _usable_files(resp["text"])
                if not files:
                    retry_prompt = (user_prompt + "\n\nIMPORTANT: your previous reply was not the "
                                    "required JSON object. Reply with ONLY the raw JSON object "
                                    '{"summary": ..., "files": [{"path": ..., "content": ...}]} '
                                    "starting with { and ending with }.")
                    resp = call_llm(gw, model, system_prompt, retry_prompt, max_tokens=max_out,
                                    timeout=call_timeout)
                    _charge_llm_call(project, model, resp)
                    data, files = _usable_files(resp["text"])
                if files:
                    size_error = _validate_generated_files(files)
                    if size_error:
                        result["error"] = size_error
                        result["summary"] = f"failed: {size_error}"
                        return result
                    for rel, content in files:
                        dest = os.path.join(ws_dir, rel.replace("/", os.sep))
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with open(dest, "w", encoding="utf-8", newline="\n") as fh:
                            fh.write(content)
                    result.update({"files": [r for r, _ in files],
                                   "summary": str(data.get("summary") or "")[:200],
                                   "mode": "llm", "input_tokens": resp["input_tokens"],
                                   "output_tokens": resp["output_tokens"],
                                   "pinned_ref": entry.get("ref"),
                                   "used_model_id": entry.get("model_id"),
                                   "used_gateway_id": gw.get("id"),
                                   "used_provider_model_id": model.get("provider_model_id")})
                    if stack == "python":
                        toolchains.ensure_pyproject(ws_dir)
                    return result
                # Distinguish a genuine outage (quota / usage limit / overloaded)
                # from a model that just didn't follow the format.
                error = _llm_outage_reason(resp.get("text") or "") or "model returned no usable files"
            except Exception as exc:
                error = str(exc)[:200]
            # This member failed — record it as a fallback step and try the next.
            result["fallbacks"].append({"model": model_label,
                                         "gateway_id": gw.get("id"),
                                         "error": error[:160]})
            result["error"] = error[:200]
        # Whole chain failed. A genuine outage (quota / rate limit / provider
        # down) must surface as a clear, non-retryable error and NOT fall
        # through to a task-named scaffold that looks like real work — the
        # runtime keys off is_llm_outage() to stop burning quota.
        if result["error"]:
            result["summary"] = f"failed: {result['error']}"

    if is_llm_outage(result.get("error") or ""):
        return result
    if stack != "python":
        # No fake scaffold for other stacks — surface the failure so the
        # rework loop / human escalation sees the real error.
        result["error"] = result["error"] or f"LLM produced no files for {stack} workspace"
        result["summary"] = f"failed: {result['error']}"
        return result

    # Deterministic runnable scaffold fallback (python only).
    files = _scaffold_files(project, task, feedback)
    size_error = _validate_generated_files(files)
    if size_error:
        result["error"] = size_error
        result["summary"] = f"failed: {size_error}"
        return result
    for rel, content in files:
        dest = os.path.join(ws_dir, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
    result.update({"files": [r for r, _ in files],
                   "summary": "deterministic scaffold (no LLM configured)" if not result["error"]
                   else f"LLM failed ({result['error'][:80]}); scaffold written",
                   "mode": "scaffold"})
    return result


def _ensure_pyproject(ws_dir: str):
    """Make sure `python -m pytest` can import the app package."""
    ini = os.path.join(ws_dir, "pytest.ini")
    if not os.path.exists(ini):
        with open(ini, "w", encoding="utf-8") as fh:
            fh.write("[pytest]\npythonpath = .\ntestpaths = tests\n")
    pkg = os.path.join(ws_dir, "app", "__init__.py")
    if not os.path.exists(pkg):
        os.makedirs(os.path.dirname(pkg), exist_ok=True)
        with open(pkg, "w", encoding="utf-8") as fh:
            fh.write("")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "task").lower()).strip("_")
    return s or "task"


def _scaffold_files(project, task, feedback: str) -> list[tuple[str, str]]:
    """A real, runnable FastAPI feature module + passing pytest tests,
    derived from the task title. Lives under app/features/ so the fallback
    never clutters the main application package."""
    slug = _slug(task["title"])
    feat = "".join(w.capitalize() for w in slug.split("_")[:4]) or "Feature"
    mod = f"""
\"\"\"{task['title']}

Auto-generated feature module for project '{project['name']}'.
\"\"\"
from fastapi import APIRouter, HTTPException
from itertools import count

router = APIRouter(prefix=\"/{slug}\")

_ITEMS: dict[int, dict] = {{}}
_next_id = count(1)


@router.get(\"/health\")
def health():
    return {{\"feature\": \"{slug}\", \"status\": \"ok\"}}


@router.post(\"/items\", status_code=201)
def create_item(payload: dict):
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=422, detail=\"payload required\")
    item_id = next(_next_id)
    _ITEMS[item_id] = {{\"id\": item_id, **payload}}
    return _ITEMS[item_id]


@router.get(\"/items/{{item_id}}\")
def get_item(item_id: int):
    item = _ITEMS.get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail=\"item not found\")
    return item


@router.get(\"/items\")
def list_items():
    return list(_ITEMS.values())
"""
    test = f"""
\"\"\"Tests for {task['title']} (auto-generated).\"\"\"
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.features.{slug} import router


def _client():
    application = FastAPI()
    application.include_router(router)
    return TestClient(application)


def test_health():
    resp = _client().get(\"/{slug}/health\")
    assert resp.status_code == 200
    assert resp.json()[\"status\"] == \"ok\"


def test_create_and_get_item():
    client = _client()
    created = client.post(\"/{slug}/items\", json={{\"name\": \"demo\"}})
    assert created.status_code == 201
    item_id = created.json()[\"id\"]
    fetched = client.get(f\"/{slug}/items/{{item_id}}\")
    assert fetched.status_code == 200
    assert fetched.json()[\"name\"] == \"demo\"


def test_get_missing_item_returns_404():
    resp = _client().get(\"/{slug}/items/99999\")
    assert resp.status_code == 404
"""
    return [(f"app/features/{slug}.py", mod),
            ("app/features/__init__.py", ""),
            (f"tests/test_{slug}.py", test)]
