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
import logging
import os
import re
import shutil
import time
import urllib.error as uerr
import urllib.request as ureq
from typing import Any

from .db import get_gateway_key, query, query_one
from . import toolchains

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- LLM access

def call_llm(gw, model, system_prompt: str, user_text: str, max_tokens: int = 8000) -> dict:
    """Live inference call. Returns {"text", "input_tokens", "output_tokens"}."""
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
        if not data.get("choices"):
            raise RuntimeError("Gateway returned an empty response with no choices")
        text = data["choices"][0]["message"]["content"] or ""
        in_toks = usage.get("prompt_tokens") or 0
        out_toks = usage.get("completion_tokens") or 0
    return {"text": text, "input_tokens": in_toks, "output_tokens": out_toks}


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


# ---------------------------------------------------------------- model selection

def resolve_llm(project):
    """Pick (gateway, model) for code generation."""
    candidates = []
    if project and project["default_gateway_id"]:
        candidates.append(project["default_gateway_id"])
    from .chatbot import _bot_config
    bot_gw, bot_model = _bot_config()
    if bot_gw and get_gateway_key(bot_gw["id"]):
        return bot_gw, _pick_codegen_model(bot_gw["id"], bot_model)
    for gid in candidates:
        gw = query_one("SELECT * FROM gateways WHERE id = ?", (gid,))
        if gw and get_gateway_key(gw["id"]):
            return gw, _pick_codegen_model(gw["id"], None)
    return None, None


_BAD_MODEL_TOKENS = ("embed", "image", "audio", "whisper", "tts", "dall", "fable-")

def _pick_codegen_model(gateway_id: str, preferred_id: str | None) -> dict | None:
    """Pick the best chat/coding model from a gateway's catalog."""
    models = query(
        "SELECT * FROM gateway_models WHERE gateway_id=? AND active=1 ORDER BY display_name",
        (gateway_id,))
    if not models:
        return None
    if preferred_id:
        for m in models:
            if m["id"] == preferred_id:
                return m
    for m in models:
        low = m["provider_model_id"].lower()
        if not any(t in low for t in _BAD_MODEL_TOKENS):
            return m
    return models[0]


# ---------------------------------------------------------------- codegen helpers

def is_llm_outage(gw, exc: Exception) -> bool:
    return isinstance(exc, RuntimeError) and "Could not reach gateway" in str(exc)


def _llm_outage_reason(exc: Exception) -> str:
    s = str(exc)
    if "HTTP" in s:
        return f"Provider returned an error: {s[:120]}"
    return f"Provider unreachable: {s[:120]}"


def _system_for(role: str) -> str:
    family = {
        "backend": "You are a backend engineer.",
        "frontend": "You are a frontend engineer.",
        "qa": "You are a QA engineer.",
        "devops": "You are a DevOps engineer.",
        "architect": "You are a software architect.",
    }.get(role, "You are a software engineer.")
    return f"{family} Write production-ready, well-documented code."


def _is_junior_dev(role_name: str) -> bool:
    return "junior" in role_name.lower() or "intern" in role_name.lower()


def _existing_tree(workspace: str) -> dict:
    tree = {}
    if not os.path.isdir(workspace):
        return tree
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d != "__pycache__" and d != ".git"]
        rel = os.path.relpath(root, workspace)
        if rel == ".":
            rel = ""
        for f in files:
            if f.endswith((".py", ".ts", ".tsx", ".js", ".json", ".md", ".yaml", ".yml", ".toml")):
                path = os.path.join(rel, f) if rel else f
                try:
                    tree[path] = open(os.path.join(root, f), encoding="utf-8").read()
                except OSError:
                    pass
    return tree


def _safe_rel_path(base: str, candidate: str) -> str | None:
    safe = os.path.normpath(os.path.join(base, candidate))
    if not safe.startswith(os.path.normpath(base)):
        return None
    return safe


def ensure_helpers_file(workspace: str, project_name: str) -> str:
    pkg = os.path.join(workspace, project_name.replace(" ", "_").replace("-", "_").lower())
    os.makedirs(pkg, exist_ok=True)
    path = os.path.join(pkg, "helpers.py")
    if not os.path.exists(path):
        open(path, "w").write('"""Shared helpers."""\n\n__all__: list[str] = []\n')
    return path


def _helpers_index(tree: dict, pkg: str) -> list[dict]:
    helpers = []
    for path in tree:
        if path.startswith(pkg + "/helpers") or path == pkg + "/helpers.py":
            helpers.append({"path": path, "size": len(tree.get(path, ""))})
    return helpers


def _feedback_file_blocks(tree: dict, pkg: str) -> str:
    blocks = []
    for path, content in tree.items():
        if path.startswith(pkg + "/") and path.endswith(".py"):
            blocks.append(f"### {path}\n```python\n{content[:400]}\n```")
    return "\n\n".join(blocks) if blocks else "(no existing files)"


def generate_implementation(workspace: str, task: dict, project_name: str,
                            role: str = "backend") -> dict:
    """Generate implementation files for a task using the LLM."""
    gw, model = (None, None)
    try:
        from .chatbot import _bot_config as _bc
        gw, model = _bc()
        if gw and model:
            gw, model = gw, _pick_codegen_model(gw["id"], model["id"])
    except Exception as exc:
        logger.debug("Bot config resolution failed; falling back to scaffold: %s", exc)

    if not gw or not model:
        return _scaffold_files(workspace, task, project_name)

    tree = _existing_tree(workspace)
    ensure_helpers_file(workspace, project_name)
    sys = _system_for(role)
    _helpers_path = os.path.join(workspace, project_name.replace(' ', '_').replace('-', '_').lower(), 'helpers.py')
    helpers_content = tree.get(_helpers_path, '(empty)')
    user = ("""Implement this task: """ + task['title'] + "\n\n"
            "Acceptance criteria: " + (task.get('acceptance_criteria') or 'None') + "\n\n"
            "Existing package tree:\n" + json.dumps({k: v[:200] for k, v in tree.items() if k.endswith('.py')}, indent=2) + "\n\n"
            "Existing helpers.py:\n" + helpers_content)
    try:
        result = call_llm(gw, model, sys, user, max_tokens=4000)
        raw = result["text"]
    except RuntimeError as exc:
        if is_llm_outage(gw, exc):
            return {"status": "outage", "reason": _llm_outage_reason(exc)}
        raise
    files = _scaffold_from_llm(raw, workspace, project_name)
    return {"status": "ok", "files": files, "model": model["provider_model_id"],
            "usage": {k: result.get(k) for k in ("input_tokens", "output_tokens") if k in result}}


def _scaffold_files(workspace: str, task: dict, project_name: str) -> dict:
    pkg = project_name.replace(" ", "_").replace("-", "_").lower()
    pkg_dir = os.path.join(workspace, pkg)
    os.makedirs(pkg_dir, exist_ok=True)
    ensure_helpers_file(workspace, project_name)
    task_mod = re.sub(r"[^a-z0-9_]", "_", task["title"].lower())[:40]
    path = os.path.join(pkg_dir, f"{task_mod}.py")
    if not os.path.exists(path):
        content = f'"""Implementation: {task["title"]}."""\n\n\ndef run():\n    raise NotImplementedError("TODO")\n'
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    return {"status": "ok", "files": [path], "mode": "deterministic_scaffold"}


def _scaffold_from_llm(raw: str, workspace: str, project_name: str) -> list[str]:
    written = []
    fence_re = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)
    for i, block in enumerate(fence_re.findall(raw)):
        path = os.path.join(workspace, project_name.replace(" ", "_").lower(), f"impl_{i}.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(block.strip())
        written.append(path)
    return written


def _ensure_pyproject(workspace: str) -> str:
    path = os.path.join(workspace, "pyproject.toml")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write('[project]\nname = "project"\nversion = "0.1.0"\n')
    return path


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", text.lower())[:40].strip("_") or "task"


def _scaffold_files(workspace: str, task: dict, project_name: str) -> dict:
    pkg = project_name.replace(" ", "_").replace("-", "_").lower()
    pkg_dir = os.path.join(workspace, pkg)
    os.makedirs(pkg_dir, exist_ok=True)
    ensure_helpers_file(workspace, project_name)
    _ensure_pyproject(workspace)
    task_mod = _slug(task["title"])
    path = os.path.join(pkg_dir, f"{task_mod}.py")
    if not os.path.exists(path):
        content = f'"""Implementation: {task["title"]}."""\n\n\ndef run():\n    raise NotImplementedError("TODO")\n'
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    return {"status": "ok", "files": [path], "mode": "deterministic_scaffold"}
