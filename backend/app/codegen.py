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
import os
import re
import shutil
import time
import urllib.error as uerr

from .db import get_gateway_key, query, query_one
from .llm.resolver import resolve_project_model
from . import toolchains


# ---------------------------------------------------------------- LLM access

def call_llm(gw, model, system_prompt: str, user_text: str, max_tokens: int = 8000) -> dict:
    """Live inference call. Returns {"text", "input_tokens", "output_tokens"}."""
    import urllib.error as uerr
    import urllib.request as ureq

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
        payload = {"model": model["provider_model_id"], "max_tokens": max_tokens,
                   "system": system_prompt,
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
        with ureq.urlopen(req, timeout=300) as resp:
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


# ---------------------------------------------------------------------------
# Circuit breaker + retry wrapper
# ---------------------------------------------------------------------------

class _BreakerState:
    """Per-gateway circuit breaker state."""
    __slots__ = ("consecutive_failures", "opened_at", "last_failure")

    def __init__(self):
        self.consecutive_failures: int = 0
        self.opened_at: float = 0.0
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
        self.last_failure = ""

    def record_failure(self, reason: str):
        self.consecutive_failures += 1
        if self.is_open:
            self.opened_at = time.monotonic()
        self.last_failure = reason


class GatewayClient:
    """High-level gateway client with circuit breaker and retry.

    All gateway calls in the runtime and chatbot should go through this
    class rather than calling :func:`call_llm` or rolling their own
    ``_call_llm`` helpers.

    Usage::

        result = await GatewayClient.call(gw, model, user_message, max_tokens=512)
        text = result["text"]
    """

    _breakers: dict[str, _BreakerState] = {}
    _lock = __import__("threading").Lock()

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
             *, retries: int = 2) -> dict:
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
                result = call_llm(gw, model, system_prompt, message, max_tokens)
                breaker.record_success()
                latency_ms = result.get("latency_ms", 0)
                result["latency_ms"] = latency_ms
                return result
            except (uerr.URLError, TimeoutError, RuntimeError) as exc:
                last_exc = exc
                is_timeout = isinstance(exc, (TimeoutError,))
                is_transient = isinstance(exc, RuntimeError) and (
                    "Could not reach gateway" in str(exc) or is_timeout)
                if not is_transient:
                    breaker.record_failure(str(exc))
                    raise
                breaker.record_failure(str(exc))
                wait = min(2 ** attempt, 8)
                time.sleep(wait)

        raise RuntimeError(
            f"Gateway '{gw.get('name', gw['id'])}' unreachable after {retries + 1} attempts: "
            f"{last_exc}"
        )


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
    return bool(error) and _llm_outage_reason(error) != "" or \
        (error or "").lower().startswith(("llm usage limit", "llm quota",
                                          "llm rate limited", "llm provider overloaded"))


def _llm_outage_reason(text: str) -> str:
    """Detect an LLM outage / quota / rate-limit message in the response
    text. Returns a clean reason string, or '' when this looks like a
    normal (but unusable) reply. These must surface as outages, not
    'no usable files' — the model literally cannot help right now."""
    low = text.lower()
    for pat, label in (
        ("usage limit", "LLM usage limit reached (quota exhausted)"),
        ("quota", "LLM quota exhausted"),
        ("rate limit", "LLM rate limited"),
        ("overloaded", "LLM provider overloaded"),
        ("insufficient", "LLM insufficient credits/balance"),
        ("temporarily unavailable", "LLM temporarily unavailable"),
        ("service unavailable", "LLM service unavailable"),
    ):
        if pat in low:
            return label
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
}


def review_verdict(project, task, mode: str) -> dict:
    """One LLM call for an SA or BA review gate. Returns
    {"decision": "approved"|"rework"|"unavailable", "rework_class",
     "findings", "input_tokens", "output_tokens"}. 'unavailable' means the
    gate could not run (no gateway, outage, unusable reply) — the pipeline
    skips the gate instead of punishing the developer."""
    empty = {"decision": "unavailable", "rework_class": "", "findings": "",
             "input_tokens": 0, "output_tokens": 0}
    gw, model = resolve_llm(project)
    if not (gw and model):
        return {**empty, "findings": "No LLM gateway configured — review gate skipped"}
    tree = _existing_tree(project["workspace_path"])[:2500]
    ac = task["acceptance_criteria"] or ""
    if task["functional_ac"]:
        ac = (ac + "\n" if ac else "") + "FUNCTIONAL AC: " + task["functional_ac"]
    if task["technical_ac"]:
        ac = (ac + "\n" if ac else "") + "TECHNICAL AC: " + task["technical_ac"]
    user = (
        f"PROJECT: {project['name']}\nGOAL: {project['goal'] or 'n/a'}\n"
        f"STACK: {project['technology_stack'] or 'n/a'}\n"
        f"WORKSPACE FILES:\n{tree}\n\n"
        f"TASK: {task['title']}\n{task['description']}\n"
        f"ACCEPTANCE CRITERIA:\n{ac or 'n/a'}\n\n"
        f"CHANGE EVIDENCE: {(task['evidence'] or '')[:600]}\n\n"
        "Is this implementation acceptable from your reviewing role?")
    try:
        raw = call_llm(gw, model, _REVIEW_SYSTEM[mode], user, max_tokens=500)
    except Exception as exc:
        return {**empty, "findings": f"review call failed: {str(exc)[:160]}"}
    text = raw.get("text") or ""
    from .chatbot import _extract_json  # lazy: chatbot imports runtime, runtime imports codegen
    data = _extract_json(text) or {}
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


def _existing_tree(ws_dir: str, limit: int = 60) -> str:
    entries = []
    for root, dirs, files in os.walk(ws_dir):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache", "node_modules")]
        rel = os.path.relpath(root, ws_dir)
        for f in files:
            entries.append(os.path.normpath(os.path.join(rel, f)).replace("\\", "/"))
    entries = sorted(entries)[:limit]
    return "\n".join(entries) or "(empty workspace)"


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


def generate_implementation(project, task, agent_ref: str, feedback: str = "",
                            pinned_ref: str | None = None) -> dict:
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
    Never raises: python LLM failures degrade to the deterministic scaffold;
    non-python stacks return an error so the rework loop reports honestly
    instead of bolting a FastAPI file onto a Go/.NET/Node project.
    """
    ws_dir = project["workspace_path"]
    stack = toolchains.detect_stack(project, ws_dir)
    result = {"files": [], "summary": "", "mode": "scaffold", "input_tokens": 0,
              "output_tokens": 0, "error": "", "pinned_ref": None,
              "used_model_id": None, "used_provider_model_id": None, "fallbacks": []}
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
        from .chatbot import _extract_json
        system_prompt = (_JR_DEV_SYSTEM if stack == "python" and _is_junior_dev(role_name or agent_ref)
                         else _system_for(stack))
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
        user_prompt = (f"PROJECT: {project['name']}\nGOAL: {project['goal'] or 'n/a'}\n"
                       f"TECH STACK: {project['technology_stack'] or 'Python (FastAPI where appropriate)'}\n\n"
                       f"TASK TO IMPLEMENT: {task['title']}\n"
                       f"DESCRIPTION: {task['description'] or 'n/a'}\n"
                       f"ACCEPTANCE CRITERIA: {task['acceptance_criteria'] or 'n/a'}"
                       f"{feedback_block}\n"
                       f"EXISTING WORKSPACE FILES:\n{_existing_tree(ws_dir)}\n"
                       f"{helpers_block}\n"
                       f"CURRENT app/main.py:\n{main_block}"
                       f"{_feedback_file_blocks(ws_dir, feedback)}\n\n"
                       "Implement this task now. Return the JSON object with complete file contents.")

        def _usable_files(text: str):
            data = _extract_json(text)
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
            try:
                resp = call_llm(gw, model, system_prompt, user_prompt, max_tokens=8000)
                data, files = _usable_files(resp["text"])
                if not files:
                    retry_prompt = (user_prompt + "\n\nIMPORTANT: your previous reply was not the "
                                    "required JSON object. Reply with ONLY the raw JSON object "
                                    '{"summary": ..., "files": [{"path": ..., "content": ...}]} '
                                    "starting with { and ending with }.")
                    resp = call_llm(gw, model, system_prompt, retry_prompt, max_tokens=8000)
                    data, files = _usable_files(resp["text"])
                if files:
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
            result["fallbacks"].append({"model": model_label, "error": error[:160]})
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

router = APIRouter(prefix=\"/{slug}\")

_ITEMS: dict[int, dict] = {{}}
_NEXT = [1]


@router.get(\"/health\")
def health():
    return {{\"feature\": \"{slug}\", \"status\": \"ok\"}}


@router.post(\"/items\", status_code=201)
def create_item(payload: dict):
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=422, detail=\"payload required\")
    item_id = _NEXT[0]
    _NEXT[0] += 1
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
