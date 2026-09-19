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

from .db import get_gateway_key, query, query_one
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
    return {"text": text, "input_tokens": in_toks, "output_tokens": out_toks}


def resolve_llm(project):
    """Pick (gateway, model) for code generation: the project's default
    gateway when it has a key, else the control-bot gateway. (None, None)
    when nothing usable is configured. The model is chosen from the
    gateway's catalog — plain chat/coding models only; the control bot's
    tool-calling routing model is never used for code generation."""
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


# Preferred code-generation models, in order. Anything whose id contains a
# _BAD_MODEL_TOKENS entry is never selected (embedders, image/audio models,
# and the control bot's tool-call-tuned "fable" routing models).
_CODEGEN_MODEL_PREF = (
    "claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4-5",
    "claude-sonnet-4", "claude-opus-5", "claude-opus-4", "gpt-4.1", "gpt-4o",
)
_BAD_MODEL_TOKENS = ("embedding", "whisper", "dall-e", "tts", "image",
                     "fable", "default")


def _pick_codegen_model(gateway_id: str, fallback_model) -> dict:
    rows = query(
        "SELECT provider_model_id FROM gateway_models WHERE gateway_id = ? AND active = 1",
        (gateway_id,))
    text_models = [r["provider_model_id"] for r in rows
                   if not any(b in (r["provider_model_id"] or "").lower()
                              for b in _BAD_MODEL_TOKENS)]
    for pref in _CODEGEN_MODEL_PREF:
        for m in text_models:
            if m == pref:
                return {"provider_model_id": m}
    for pref in _CODEGEN_MODEL_PREF:
        for m in text_models:
            if m.startswith(pref):
                return {"provider_model_id": m}
    if text_models:
        return {"provider_model_id": text_models[0]}
    return fallback_model or {"provider_model_id": "claude-sonnet-5"}


# ---------------------------------------------------------------- codegen

_SHARED_RULES = """- Contribute to ONE coherent application with a conventional
  layout for its stack — extend existing files rather than creating
  task-named one-offs.
- NEVER name a file after the task title.
- Non-source files (launcher scripts like .bat/.sh, README.md, .env.example,
  Dockerfile, config) are expected whenever the task asks for them — create
  exactly the requested file type.
- Every change MUST come with or update tests that pass with the stack's
  standard test runner.
- Keep the full content of every file you touch (no diffs, no placeholders
  like "...", no TODO-only stubs). 6 files maximum."""

_CODEGEN_SYSTEMS = {
    "python": """You are a senior software engineer implementing one task of a real project.
Return ONLY a JSON object (no markdown fences, no prose, no tool calls):
{"summary": "<one line>", "files": [{"path": "<relative/path>", "content": "<full file content>"}]}
Structure rules:
- Contribute to ONE coherent application with a conventional layout:
  app/main.py (FastAPI app + include_router calls), app/routers/, app/services/,
  app/models.py, requirements.txt, tests/test_*.py.
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


def generate_implementation(project, task, agent_name: str, feedback: str = "") -> dict:
    """Generate real code files for a task into the project workspace.

    Returns {"files": [relative paths], "summary": str, "mode": "llm"|"scaffold",
             "input_tokens": int, "output_tokens": int, "error": str|""}.
    Never raises: python LLM failures degrade to the deterministic scaffold;
    non-python stacks return an error so the rework loop reports honestly
    instead of bolting a FastAPI file onto a Go/.NET/Node project.
    """
    ws_dir = project["workspace_path"]
    stack = toolchains.detect_stack(project, ws_dir)
    result = {"files": [], "summary": "", "mode": "scaffold",
              "input_tokens": 0, "output_tokens": 0, "error": ""}
    if not ws_dir or not os.path.isdir(ws_dir):
        result["error"] = "workspace missing"
        return result

    gw, model = resolve_llm(project)
    if gw and model:
        from .chatbot import _extract_json
        system_prompt = _system_for(stack)
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
                       f"EXISTING WORKSPACE FILES:\n{_existing_tree(ws_dir)}\n\n"
                       f"CURRENT app/main.py:\n{main_block}\n\n"
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
                result.update({"files": [r for r, _ in files], "summary": str(data.get("summary") or "")[:200],
                               "mode": "llm", "input_tokens": resp["input_tokens"],
                               "output_tokens": resp["output_tokens"]})
                if stack == "python":
                    toolchains.ensure_pyproject(ws_dir)
                return result
            result["error"] = "model returned no usable files"
        except Exception as exc:
            result["error"] = str(exc)[:200]

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
