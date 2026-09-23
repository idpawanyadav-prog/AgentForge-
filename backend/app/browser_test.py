"""Playwright browser smoke testing for generated project web apps.

Gives dev/QA agents a real functional check: launch the generated app on a
private port, drive it with headless Chromium, and verify the pages render,
internal links resolve, and forms don't crash the server. A missing
Playwright install SKIPS the gate (never a fake pass or fail), matching the
SA/BA gate contract. Screenshots land in <workspace>/qa_artifacts/.

v1: deterministic smoke for dev self-test + QA. LLM scenario scripts (QA)
build on the same launch/teardown plumbing in a later step.
"""
from __future__ import annotations

import glob
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request

from . import toolchains

_smoke_lock = threading.Semaphore(1)  # one browser test at a time
_available_cache: tuple[bool, str] | None = None

MAX_PAGES = 15          # crawl cap
MAX_FORMS = 2           # form-submission cap
PAGE_TIMEOUT_MS = 15000
TOTAL_BUDGET_S = 180    # whole smoke run wall-clock cap
BOOT_TIMEOUT_S = 25


def _chromium_dir() -> str | None:
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
    hits = sorted(glob.glob(os.path.join(base, "chromium-*")))
    return hits[-1] if hits else None


def available() -> tuple[bool, str]:
    """(ok, reason). Cached: import playwright + a Chromium build installed."""
    global _available_cache
    if _available_cache is not None:
        return _available_cache
    try:
        import playwright  # noqa: F401
    except ImportError:
        _available_cache = (False, "playwright package not installed")
        return _available_cache
    if not _chromium_dir():
        _available_cache = (False, "Chromium not installed — run scripts/setup_playwright.ps1")
        return _available_cache
    _available_cache = (True, "")
    return _available_cache


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_status(url: str, timeout: float = 5.0) -> int:
    """GET url; returns status code, 0 on connection error."""
    req = urllib.request.Request(url, headers={"User-Agent": "agentforge-qa/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


# ---------------------------------------------------------------- launching

_PY_BOOT = """
import importlib, sys
sys.path.insert(0, {ws!r})
from fastapi import FastAPI
for mod in {entries!r}:
    try:
        m = importlib.import_module(mod)
    except Exception as e:
        print(f"AF_BOOT_IMPORT_FAIL {{mod}}: {{e}}", file=sys.stderr)
        continue
    app = next((v for v in vars(m).values() if isinstance(v, FastAPI)), None)
    if app is not None:
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port={port}, log_level="warning")
        sys.exit(0)
print("AF_NO_FASTAPI_APP", file=sys.stderr)
sys.exit(3)
"""


def _python_entry(ws: str) -> tuple[list[str], str] | None:
    """(module names, human description) for a FastAPI entry point."""
    if os.path.isfile(os.path.join(ws, "app", "main.py")):
        return ["app.main", "main"], "app/main.py"
    for f in ("main.py", "app.py"):
        if os.path.isfile(os.path.join(ws, f)):
            return [f[:-3]], f
    return None


def _static_index(ws: str) -> str | None:
    for d in ("", "dist", "public", "static", "site"):
        if os.path.isfile(os.path.join(ws, d, "index.html")):
            return d or "."
    return None


class _App:
    def __init__(self, proc, base_url, kind, entry):
        self.proc = proc
        self.base_url = base_url
        self.kind = kind      # "fastapi" | "static"
        self.entry = entry

    def stop(self):
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass


def _spawn_boot(ws: str, port: int, env: dict) -> subprocess.Popen:
    entry = _python_entry(ws)
    script = _PY_BOOT.format(ws=ws, entries=entry[0], port=port)
    return subprocess.Popen([sys.executable, "-c", script], cwd=ws, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def _boot_failure_tail(proc) -> str:
    try:
        out = (proc.stdout.read() if proc.stdout else "") or ""
    except Exception:
        out = ""
    lines = [l for l in out.strip().splitlines() if l.strip()]
    return " | ".join(lines[-6:])[:260] or f"exit {proc.returncode}"


def _wait_boot(app: "_App", kind: str) -> bool:
    deadline = time.time() + BOOT_TIMEOUT_S
    while time.time() < deadline:
        if app.proc.poll() is not None:
            return False
        st = _http_status(app.base_url + "/")
        # FastAPI apps may 404 on "/" — accept /docs as alive.
        if st and st < 500:
            return True
        if kind == "fastapi" and _http_status(app.base_url + "/docs") == 200:
            return True
        time.sleep(0.5)
    return False


def launch_app(project) -> tuple[_App | None, str]:
    """Start the generated app on a private port. (app, '') on success or
    (None, reason) when no runnable web entry exists / boot fails."""
    ws = project["workspace_path"]
    if not ws or not os.path.isdir(ws):
        return None, "no workspace directory"
    stack = toolchains.detect_stack(project, ws)
    port = _free_port()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
           **toolchains._lib_env(ws), "PYTHONUNBUFFERED": "1"}
    env.pop("AGENT_OFFICE_DB", None)  # generated app must never touch our DB

    if stack == "python" and _python_entry(ws):
        proc = _spawn_boot(ws, port, env)
        kind, desc = "fastapi", _python_entry(ws)[1]
    elif _static_index(ws):
        rel = _static_index(ws)
        proc = subprocess.Popen([sys.executable, "-m", "http.server", str(port),
                                 "--bind", "127.0.0.1"],
                                cwd=os.path.join(ws, rel), env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        kind, desc = "static", f"{rel}/index.html"
    elif stack in ("go", "dotnet") or stack == "node":
        return None, f"{stack} app launching not supported yet"
    else:
        return None, "no runnable web entry detected"

    app = _App(proc, f"http://127.0.0.1:{port}", kind, desc)
    if _wait_boot(app, kind):
        return app, ""
    why = "app exited on boot: " + _boot_failure_tail(proc) if proc.poll() is not None else \
        f"app did not answer within {BOOT_TIMEOUT_S}s"
    # Self-heal obvious implicit deps: FastAPI Form usage asks for
    # python-multipart by name at import time; source imports are already
    # preflighted, but pip names an error text mentions are not.
    wanted = re.findall(r"pip install (\S+)", why)
    if kind == "fastapi" and proc.poll() is not None and wanted:
        res = toolchains.lib_install(ws, wanted[:5], stack="python")
        if res.get("ok"):
            # Rebuild env: lib/ (and its PYTHONPATH entry) may not have
            # existed when the first env was assembled.
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                   **toolchains._lib_env(ws), "PYTHONUNBUFFERED": "1"}
            env.pop("AGENT_OFFICE_DB", None)
            proc = _spawn_boot(ws, port, env)
            app.proc = proc
            if _wait_boot(app, kind):
                return app, ""
            why = "app exited on boot: " + _boot_failure_tail(proc)
    app.stop()
    return None, why


# ---------------------------------------------------------------- smoke run

def _smoke_pages(base_url: str, artifacts: str) -> dict:
    """Drive the app with headless Chromium. Returns result dict; raises
    RuntimeError only when Playwright itself cannot run (→ caller skips)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(f"playwright unavailable: {exc}")
    os.makedirs(artifacts, exist_ok=True)
    issues: list[str] = []
    warnings: list[str] = []
    visited: set[str] = set()
    shots: list[str] = []
    t_end = time.time() + TOTAL_BUDGET_S

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-gpu"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        page_errs: list[str] = []
        page.on("pageerror", lambda e: page_errs.append(str(e)[:160]))

        def shot(name):
            f = os.path.join(artifacts, f"smoke_{name}.png")
            try:
                page.screenshot(path=f, full_page=False)
                shots.append(os.path.basename(f))
            except Exception:
                pass

        def visit(url, label, fail_on_4xx=False):
            if url in visited or len(visited) >= MAX_PAGES or time.time() > t_end:
                return False
            visited.add(url)
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            except Exception as exc:
                issues.append(f"{label}: navigation failed ({str(exc)[:100]})")
                return False
            st = resp.status if resp else 0
            if st >= 500:
                issues.append(f"{label}: server error {st}")
                shot(label.replace("/", "_")[:30])
                return False
            if st >= 400 and fail_on_4xx:
                issues.append(f"{label}: {st}")
                return False
            if st >= 400:
                warnings.append(f"{label}: {st}")
            return True

        # Root document must render: this is the hard gate.
        origin = base_url + "/"
        if not visit(origin, "root", fail_on_4xx=True):
            pass  # issues already recorded below via status check
        try:
            body_len = page.evaluate("document.body ? document.body.innerText.trim().length + document.body.children.length : 0")
            if not body_len:
                issues.append("root: empty page (no content)")
        except Exception as exc:
            issues.append(f"root: DOM check failed ({str(exc)[:100]})")
        shot("root")
        if page_errs:
            issues.append(f"root: uncaught JS errors: {page_errs[:2]}")
            page_errs.clear()

        # Crawl same-origin links one level deep.
        try:
            hrefs = page.eval_on_selector_all(
                "a[href]",
                "(els, origin) => els.map(e => e.href).filter(h => h.startsWith(origin))",
                base_url)
        except Exception:
            hrefs = []
        for h in hrefs[:MAX_PAGES]:
            h = h.split("#")[0]
            if not h or h.endswith((".png", ".jpg", ".svg", ".css", ".js", ".ico")):
                continue
            visit(h, h.replace(base_url, "") or "/")
        if page_errs:
            warnings.append(f"JS errors on crawled pages: {page_errs[:2]}")

        # Form heuristic: fill and submit, server must not crash.
        try:
            forms = page.query_selector_all("form")
        except Exception:
            forms = []
        for i, form in enumerate(forms[:MAX_FORMS]):
            if time.time() > t_end:
                break
            try:
                for inp in form.query_selector_all(
                        "input[type=text], input[type=number], input[type=email], "
                        "input[type=search], input:not([type]), textarea"):
                    inp.fill("123" if "number" in (inp.get_attribute("type") or "") else "test")
                btn = (form.query_selector("button[type=submit], input[type=submit]")
                       or form.query_selector("button, input[type=button]"))
                if btn:
                    btn.click()
                    page.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                    if any(f"form[{i}]" in s for s in issues):
                        continue
                    cur = page.url
                    if _http_status(cur) >= 500:
                        issues.append(f"form[{i}]: submit caused server error")
                        shot(f"form{i}")
                page.goto(origin, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            except Exception as exc:
                warnings.append(f"form[{i}]: interaction skipped ({str(exc)[:80]})")

        browser.close()

    ok = not issues
    summary = (f"browser passed ({len(visited)} pages, {len(shots)} screenshots)"
               if ok else "browser FAILED: " + " | ".join(issues[:4]))
    if ok and warnings:
        summary += " — warnings: " + " | ".join(warnings[:3])[:180]
    return {"ok": ok, "summary": summary, "pages": len(visited), "screenshots": shots}


def run_smoke(project) -> dict:
    """Full smoke pipeline: availability → launch → drive → teardown.
    Never raises; returns {"status": passed|failed|skipped, "summary", ...}.
    A missing toolchain or unsupported stack SKIPS — the gate must not
    punish the dev for infrastructure the machine doesn't have."""
    ok, why = available()
    if not ok:
        return {"status": "skipped", "summary": f"browser skipped: {why}",
                "pages": 0, "screenshots": []}
    if not _smoke_lock.acquire(timeout=TOTAL_BUDGET_S):
        return {"status": "skipped", "summary": "browser skipped: another browser test in progress",
                "pages": 0, "screenshots": []}
    try:
        app, why = launch_app(project)
        if not app:
            status = "skipped" if "not supported" in why or "no workspace" in why \
                or "no runnable" in why else "failed"
            return {"status": status, "summary": f"browser {'skipped' if status == 'skipped' else 'FAILED'}: {why}",
                    "pages": 0, "screenshots": []}
        artifacts = os.path.join(project["workspace_path"], "qa_artifacts")
        try:
            res = _smoke_pages(app.base_url, artifacts)
        except RuntimeError as exc:  # playwright broke mid-flight
            return {"status": "skipped", "summary": f"browser skipped: {exc}",
                    "pages": 0, "screenshots": []}
        except Exception as exc:
            return {"status": "failed", "summary": f"browser FAILED: smoke run crashed ({str(exc)[:140]})",
                    "pages": 0, "screenshots": []}
        finally:
            app.stop()
        res["status"] = "passed" if res.pop("ok") else "failed"
        return res
    finally:
        _smoke_lock.release()
