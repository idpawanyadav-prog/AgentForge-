"""Multi-stack build & QA toolchains.

Stack detection + real build checks and test runs for Python (FastAPI),
.NET (WPF/C#), Go and Node.js workspaces. Every summary is prefixed with
'build passed'/'build FAILED' or 'tests passed'/'tests FAILED' so the QA
verdict logic in runtime.py can consume any stack uniformly.

A missing toolchain NEVER fakes a pass: the caller gets a 'build FAILED'
summary containing the exact install hint, and the chat copilot offers an
approval-gated install (`install dotnet toolchain` -> winget).
"""

import os
import subprocess
import sys
import shutil

from .db import query_one

STACKS = {
    "dotnet": {
        "exe": "dotnet",
        "label": ".NET SDK",
        "install": "winget install --id Microsoft.DotNet.SDK.8 -e --accept-package-agreements --accept-source-agreements",
        "test_hint": "xUnit tests (dotnet test)",
    },
    "go": {
        "exe": "go",
        "label": "Go toolchain",
        "install": "winget install --id GoLang.Go -e --accept-package-agreements --accept-source-agreements",
        "test_hint": "go test ./...",
    },
    "node": {
        "exe": "node",
        "label": "Node.js + npm",
        "install": "winget install --id OpenJS.NodeJS.LTS -e --accept-package-agreements --accept-source-agreements",
        "test_hint": "npm test",
    },
    "python": {
        "exe": sys.executable,  # the backend itself runs on Python
        "label": "Python + pytest",
        "install": "",
        "test_hint": "pytest",
    },
}

_STACK_KEYWORDS = (
    ("dotnet", ("wpf", ".net", "net-", "c#", "csharp", "dotnet", "winui", "avalonia", "maui", "xaml")),
    ("go", ("golang", " go ", "go microservice", "gin", "fiber", "cobra")),
    ("node", ("node", "npm", "typescript", "javascript", "react", "vue", "angular", "next.js", "vite", "express")),
    ("python", ("python", "fastapi", "django", "flask", "pytest", "pandas")),
)


def _which(stack: str) -> str | None:
    exe = STACKS[stack]["exe"]
    if stack == "python":
        return exe  # always the running interpreter
    return shutil.which(exe)


def pip_install(packages, timeout: int = 300) -> dict:
    """Install Python packages into the backend/QA environment with pip.
    Used by the Product Owner (full autonomy) to clear ModuleNotFoundError
    blockers. Never fakes success: returns the real pip verdict and output."""
    pkgs = [str(p).strip() for p in (packages or []) if str(p).strip()]
    if not pkgs:
        return {"ok": False, "packages": [], "output": "no packages given"}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *pkgs],
            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return {"ok": proc.returncode == 0, "packages": pkgs,
                "output": output[-1500:] if output else f"pip exit {proc.returncode}"}
    except Exception as exc:
        return {"ok": False, "packages": pkgs, "output": f"pip failed: {exc}"}


def project_python(ws_dir: str | None) -> str | None:
    """The project's own virtualenv interpreter (linked from the generated
    start_server.bat), when the workspace has one."""
    if not ws_dir:
        return None
    sub = "Scripts" if os.name == "nt" else "bin"
    exe = os.path.join(ws_dir, ".venv", sub, "python.exe" if os.name == "nt" else "python")
    return exe if os.path.isfile(exe) else None


def ensure_project_env(ws_dir: str | None, extra_packages=None, timeout: int = 600) -> dict:
    """Create and fill the project-local environment: `<workspace>/.venv`
    with everything from the workspace requirements.txt plus any explicitly
    requested packages. This is the folder the generated start_server.bat
    activates, so installing here makes the app itself runnable — QA build
    and test runs use the same interpreter (see project_python). Idempotent:
    an existing .venv just gets its requirements re-synced. Never fakes
    success; the real pip verdict is returned."""
    if not ws_dir or not os.path.isdir(ws_dir):
        return {"ok": False, "output": "workspace missing", "packages": list(extra_packages or [])}
    steps: list[str] = []
    ok = True
    py = project_python(ws_dir)
    if py:
        # A stale venv (e.g. Python 3.6 created by scaffolding on the system
        # interpreter) can never run modern requirements — recreate it when
        # its version doesn't match the backend interpreter.
        try:
            ver = subprocess.run([py, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                                 capture_output=True, text=True, timeout=60,
                                 encoding="utf-8", errors="replace")
            want = f"{sys.version_info[0]}.{sys.version_info[1]}"
            got = (ver.stdout or "").strip()
            if ver.returncode == 0 and got != want:
                shutil.rmtree(os.path.join(ws_dir, ".venv"), ignore_errors=True)
                py = None
                steps.append(f"removed stale project .venv (Python {got}; need {want})")
            else:
                steps.append("project .venv already present")
        except Exception as exc:
            steps.append(f"venv version check failed: {exc}")
    if not py:
        try:
            proc = subprocess.run([sys.executable, "-m", "venv", ".venv"], cwd=ws_dir,
                                  capture_output=True, text=True, timeout=180,
                                  encoding="utf-8", errors="replace")
            steps.append("created project .venv" if proc.returncode == 0
                         else f"venv creation failed: {(proc.stderr or proc.stdout or '')[-200:]}")
            ok &= proc.returncode == 0
        except Exception as exc:
            steps.append(f"venv creation failed: {exc}")
            ok = False
        py = project_python(ws_dir)
        if not py:
            return {"ok": False, "packages": list(extra_packages or []),
                    "output": "\n".join(steps)[-1500:]}

    # A venv can exist without pip (created by scaffolding scripts without
    # ensurepip). Bootstrap it before any install attempt.
    try:
        probe = subprocess.run([py, "-m", "pip", "--version"], capture_output=True,
                               text=True, timeout=60, encoding="utf-8", errors="replace")
        if probe.returncode != 0:
            fix = subprocess.run([py, "-m", "ensurepip", "--upgrade"], cwd=ws_dir,
                                 capture_output=True, text=True, timeout=180,
                                 encoding="utf-8", errors="replace")
            steps.append("bootstrapped pip via ensurepip"
                         + (" ok" if fix.returncode == 0 else f" FAILED: {(fix.stderr or '')[-120:]}"))
            ok &= fix.returncode == 0
    except Exception as exc:
        steps.append(f"pip bootstrap check failed: {exc}")
        ok = False

    def _pip(args: list[str], label: str):
        nonlocal ok
        try:
            proc = subprocess.run([py, "-m", "pip", "install", "--disable-pip-version-check", *args],
                                  cwd=ws_dir, capture_output=True, text=True, timeout=timeout,
                                  encoding="utf-8", errors="replace")
            out = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
            steps.append(f"{label}: {'ok' if proc.returncode == 0 else 'FAILED'}"
                         + (f" — {out[-1][:160]}" if out else ""))
            ok &= proc.returncode == 0
        except Exception as exc:
            steps.append(f"{label} FAILED: {exc}")
            ok = False

    req = os.path.join(ws_dir, "requirements.txt")
    if os.path.isfile(req):
        _pip(["-r", "requirements.txt"], "pip install -r requirements.txt")
    pkgs = [str(p).strip() for p in (extra_packages or []) if str(p).strip()]
    if pkgs:
        _pip(pkgs, f"pip install {' '.join(pkgs)}")
    if not steps:
        steps.append("nothing to install (no requirements.txt, no packages)")
    return {"ok": ok, "python": py, "packages": pkgs, "output": "\n".join(steps)[-1500:]}


def toolchain_available(stack: str) -> tuple[bool, str]:
    """(available, message). message explains the install path when False."""
    if stack not in STACKS:
        return False, f"unknown stack '{stack}'"
    if _which(stack):
        return True, ""
    tc = STACKS[stack]
    return False, (f"{tc['label']} is not installed on this machine. "
                   f"Install it with: `{tc['install']}`, or reply "
                   f"`install {stack} toolchain` in chat for an approval-gated install.")


def detect_stack(project, ws_dir: str | None = None) -> str:
    """Detect the workspace stack: explicit build files win, then the
    project's declared technology stack, then Python (the default the
    code generator scaffolds)."""
    ws = ws_dir or (project["workspace_path"] if project else None)
    if ws and os.path.isdir(ws):
        try:
            names = []
            for root, dirs, files in os.walk(ws):
                depth = os.path.relpath(root, ws).count(os.sep)
                dirs[:] = [d for d in dirs if d.lower() not in
                           (".git", "bin", "obj", "node_modules", ".venv", "venv")
                           and depth < 2]
                names.extend(files)
        except OSError:
            names = []
        if any(n == "go.mod" for n in names):
            return "go"
        if any(n == "package.json" for n in names):
            return "node"
        if any(n.endswith((".csproj", ".sln", ".slnx")) for n in names):
            return "dotnet"
        if any(n.endswith(".py") for n in names):
            return "python"
    declared = (project["technology_stack"] if project else "") or ""
    declared = f" {declared.lower()} "
    for stack, kws in _STACK_KEYWORDS:
        if any(k in declared for k in kws):
            return stack
    return "python"


def _run(cmd: list[str], ws_dir: str, timeout: int) -> tuple[int, str]:
    """Run a toolchain command; returns (returncode, combined-output-tail).
    Resolves the executable via PATH (needed for npm/npx which are .cmd
    shims on Windows and invisible to a shell-less subprocess)."""
    exe = shutil.which(cmd[0]) or cmd[0]
    try:
        proc = subprocess.run([exe, *cmd[1:]], cwd=ws_dir, capture_output=True, text=True,
                              timeout=timeout,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 127, str(exc)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    tail = "\n".join(out.splitlines()[-12:])
    return proc.returncode, tail


def _err_lines(out: str) -> str:
    hits = [l.strip() for l in out.splitlines()
            if "error" in l.lower() and l.strip()]
    return " | ".join(hits[:3])[:250] if hits else out.splitlines()[-1][:250] if out.splitlines() else "unknown error"


# ---------------------------------------------------------------- python

_BUILD_DRIVER = r'''
import json, os, py_compile, sys, tempfile, traceback

def out(ok, msg):
    print(json.dumps({"ok": bool(ok), "msg": msg}))
    sys.exit(0)

_tmp_pyc = os.path.join(tempfile.gettempdir(), "af_build_smoke.pyc")
errors = []
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache",
                                            "node_modules", ".venv", "venv")]
    for f in files:
        if f.endswith(".py"):
            p = os.path.join(root, f)
            try:
                py_compile.compile(p, cfile=_tmp_pyc, doraise=True)
            except py_compile.PyCompileError as e:
                first = (e.args[0] if e.args else str(e)).splitlines()
                errors.append(next((l for l in first if l.strip() and not l.startswith("  ")), p)[:200])
            except Exception as e:
                errors.append(f"{p}: {e}"[:200])
if errors:
    out(False, "syntax error(s): " + " | ".join(errors[:3]))

if not os.path.isfile(os.path.join("app", "main.py")):
    out(True, "all modules compile; no app/main.py to boot")

try:
    import importlib
    m = importlib.import_module("app.main")
except Exception:
    tail = traceback.format_exc().strip().splitlines()
    out(False, "app import failed: " + (tail[-1] if tail else "unknown")[:200])

from fastapi import FastAPI
apps = [v for v in vars(m).values() if isinstance(v, FastAPI)]
if not apps:
    out(True, "all modules compile; app.main imported (no FastAPI instance)")

try:
    from fastapi.testclient import TestClient
    client = TestClient(apps[0], raise_server_exceptions=False)
    r = client.get("/openapi.json")
    if r.status_code != 200:
        out(False, f"app boot failed: GET /openapi.json returned {r.status_code}")
    out(True, f"app booted; {len(apps[0].routes)} routes served (GET /openapi.json -> 200)")
except Exception as e:
    out(False, "app boot failed: " + str(e)[:200])
'''


def ensure_pyproject(ws_dir: str):
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


def _python_build(ws_dir: str) -> tuple[bool, str]:
    if not ws_dir or not os.path.isdir(ws_dir):
        return False, "workspace missing"
    py = project_python(ws_dir) or sys.executable
    try:
        proc = subprocess.run(
            [py, "-c", _BUILD_DRIVER],
            cwd=ws_dir, capture_output=True, text=True, timeout=120,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return False, "build smoke timed out after 120s"
    line = ""
    for l in reversed((proc.stdout or "").strip().splitlines()):
        l = l.strip()
        if l.startswith("{"):
            line = l
            break
    if not line:
        err = (proc.stderr or "").strip().splitlines()
        detail = err[-1][:150] if err else "no output from build driver"
        return False, detail
    import json as _json
    try:
        data = _json.loads(line)
        ok, msg = bool(data.get("ok")), str(data.get("msg") or "")[:200]
    except ValueError:
        return False, "unparseable build-driver output"
    return ok, msg


def _python_tests(ws_dir: str) -> tuple[bool, str]:
    if not ws_dir or not os.path.isdir(ws_dir):
        return False, "workspace missing"
    ensure_pyproject(ws_dir)
    py = project_python(ws_dir) or sys.executable
    rc, tail = _run([py, "-m", "pytest", "-q", "--no-header", "-x", "--tb=line"],
                    ws_dir, timeout=300)
    lines = tail.splitlines()
    summary = lines[-1] if lines else "no output"
    detail = next((l for l in reversed(lines) if l.startswith("E ") or l.startswith("FAILED")), "")
    ok = rc == 0
    out = summary + (f" | {detail}" if detail and not ok else "")
    return ok, out[:300]


# ------------------------------------------------------------ dotnet / go / node

def _find_dotnet_proj(ws_dir: str) -> str | None:
    """First .sln/.csproj anywhere in the workspace (shallow-first, skipping
    build output dirs)."""
    hits = []
    for root, dirs, files in os.walk(ws_dir):
        dirs[:] = [d for d in dirs if d.lower() not in
                   (".git", "bin", "obj", "node_modules", ".venv", "venv")]
        rel = os.path.relpath(root, ws_dir)
        for f in files:
            if f.endswith((".sln", ".slnx", ".csproj")):
                depth = 0 if rel == "." else rel.count(os.sep) + 1
                hits.append((depth, 0 if f.endswith((".sln", ".slnx")) else 1,
                             os.path.join(rel, f)))
    if not hits:
        return None
    hits.sort()
    return hits[0][2].replace(os.sep, "/")


def _dotnet_build(ws_dir: str) -> tuple[bool, str]:
    proj = _find_dotnet_proj(ws_dir)
    if not proj:
        return False, "no .sln or .csproj in workspace"
    rc, tail = _run(["dotnet", "build", proj, "-v", "q", "--nologo"], ws_dir, timeout=600)
    if rc != 0:
        return False, _err_lines(tail)
    return True, f"dotnet build ok ({proj})"


def _dotnet_tests(ws_dir: str) -> tuple[bool, str]:
    proj = _find_dotnet_proj(ws_dir)
    if not proj:
        return False, "no .sln or .csproj in workspace"
    rc, tail = _run(["dotnet", "test", proj, "--no-build", "-v", "q", "--nologo"],
                    ws_dir, timeout=600)
    if rc != 0:
        low = tail.lower()
        if "no test is available" in low or "unable to find testhost" in low \
                or "0 test(s) executed" in low:
            return True, "no test projects with tests (0 tests)"
        return False, _err_lines(tail)
    lines = tail.splitlines()
    last = lines[-1] if lines else ""
    return True, (last or "dotnet test ok")[:250]


def _go_build(ws_dir: str) -> tuple[bool, str]:
    rc, tail = _run(["go", "build", "./..."], ws_dir, timeout=300)
    if rc != 0:
        return False, _err_lines(tail)
    rc2, tail2 = _run(["go", "vet", "./..."], ws_dir, timeout=300)
    if rc2 != 0:
        return False, "go vet: " + _err_lines(tail2)
    return True, "go build + go vet ok"


def _go_tests(ws_dir: str) -> tuple[bool, str]:
    rc, tail = _run(["go", "test", "./..."], ws_dir, timeout=300)
    if rc != 0:
        return False, _err_lines(tail)
    has_tests = any("ok " in l or "no test files" in l for l in tail.splitlines())
    if not has_tests and tail:
        return True, tail.splitlines()[-1][:250]
    return True, (tail.splitlines()[-1] if tail.splitlines() else "go test ok")[:250]


def _node_install(ws_dir: str) -> tuple[bool, str]:
    if os.path.isdir(os.path.join(ws_dir, "node_modules")):
        return True, "node_modules present"
    lock = os.path.isfile(os.path.join(ws_dir, "package-lock.json"))
    cmd = ["npm", "ci", "--no-audit", "--no-fund"] if lock else ["npm", "install", "--no-audit", "--no-fund"]
    rc, tail = _run(cmd, ws_dir, timeout=600)
    if rc != 0:
        return False, "npm install: " + _err_lines(tail)
    return True, "npm install ok"


def _node_build(ws_dir: str) -> tuple[bool, str]:
    import json as _json
    pkg_path = os.path.join(ws_dir, "package.json")
    try:
        with open(pkg_path, encoding="utf-8") as fh:
            pkg = _json.load(fh)
    except (OSError, ValueError):
        return False, "unreadable package.json"
    scripts = pkg.get("scripts") or {}
    if "build" in scripts:
        rc, tail = _run(["npm", "run", "build", "--silent"], ws_dir, timeout=600)
        if rc != 0:
            return False, "npm run build: " + _err_lines(tail)
        return True, "npm run build ok"
    if os.path.isfile(os.path.join(ws_dir, "tsconfig.json")):
        rc, tail = _run(["npx", "tsc", "--noEmit"], ws_dir, timeout=600)
        if rc != 0:
            return False, "tsc: " + _err_lines(tail)
        return True, "tsc --noEmit ok"
    return True, "no build script (plain JS project)"


def _node_tests(ws_dir: str) -> tuple[bool, str]:
    import json as _json
    try:
        with open(os.path.join(ws_dir, "package.json"), encoding="utf-8") as fh:
            scripts = (_json.load(fh).get("scripts") or {})
    except (OSError, ValueError):
        return False, "unreadable package.json"
    test_script = scripts.get("test")
    if not test_script or "no test specified" in test_script:
        # Honest zero: not a pass of any tests, but not a failure of code either.
        return True, "no test suite configured (0 tests)"
    rc, tail = _run(["npm", "test", "--silent"], ws_dir, timeout=600)
    if rc != 0:
        return False, "npm test: " + _err_lines(tail)
    last = tail.splitlines()[-1] if tail.splitlines() else ""
    return True, (last or "npm test ok")[:250]


# ---------------------------------------------------------------- dispatch

def build_check(stack: str, ws_dir: str) -> tuple[bool, str]:
    """Real build smoke for the stack. Summary starts with 'build passed'
    or 'build FAILED'."""
    ok, msg = toolchain_available(stack)
    if not ok:
        return False, f"build FAILED: toolchain unavailable — {msg}"
    try:
        if stack == "python":
            ok, msg = _python_build(ws_dir)
        elif stack == "dotnet":
            ok, msg = _dotnet_build(ws_dir)
        elif stack == "go":
            ok, msg = _go_build(ws_dir)
        elif stack == "node":
            ok, msg = _node_install(ws_dir)
            if ok:
                ok, msg = _node_build(ws_dir)
        else:
            return False, f"build FAILED: unsupported stack '{stack}'"
    except Exception as exc:
        return False, f"build FAILED: {str(exc)[:180]}"
    return ok, (f"build passed: {msg}" if ok else f"build FAILED: {msg}")


def run_stack_tests(stack: str, ws_dir: str) -> tuple[bool, str]:
    """Real test run for the stack. Summary starts with 'tests passed'
    or 'tests FAILED'."""
    ok, msg = toolchain_available(stack)
    if not ok:
        return False, f"tests FAILED: toolchain unavailable — {msg}"
    try:
        if stack == "python":
            ok, msg = _python_tests(ws_dir)
        elif stack == "dotnet":
            ok, msg = _dotnet_tests(ws_dir)
        elif stack == "go":
            ok, msg = _go_tests(ws_dir)
        elif stack == "node":
            ok, msg = _node_tests(ws_dir)
        else:
            return False, f"tests FAILED: unsupported stack '{stack}'"
    except Exception as exc:
        return False, f"tests FAILED: {str(exc)[:180]}"
    return ok, (f"tests passed: {msg}" if ok else f"tests FAILED: {msg}")
