"""Playwright browser-smoke gate tests.

Verdict-priority unit tests run against a throwaway SQLite DB
(AGENT_OFFICE_DB) with a mocked browser result — no browser needed.
Two integration tests boot a real app + headless Chromium and are
skipped automatically when Playwright/Chromium is unavailable.

Run: "%USERPROFILE%\\.verdent\\agentforge-venv\\Scripts\\python.exe" -m pytest backend/tests/test_browser_smoke.py -q
"""
import asyncio
import os
import sys
import tempfile

_DB = os.path.join(tempfile.mkdtemp(prefix="af_browsetest_"), "test.db")
os.environ["AGENT_OFFICE_DB"] = _DB
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from app import db as appdb  # noqa: E402
from app import browser_test, runtime  # noqa: E402

appdb.init_db()

PID = "proj-br-1"
TID = "team-br-1"


@pytest.fixture()
def fx(monkeypatch):
    for tbl in ("usage_records", "task_dependencies", "workflow_runs", "tasks",
                "sprints", "team_agents", "teams", "agents", "personas", "roles",
                "projects"):
        appdb.execute(f"DELETE FROM {tbl}")
    appdb.execute("DELETE FROM execution_events")
    ts = appdb.now()
    for role in ("Senior Developer", "QA Engineer"):
        appdb.insert("roles", {"id": "role-" + role, "name": role, "created_at": ts,
                               "updated_at": ts})
        appdb.insert("personas", {"id": "per-" + role, "role_id": "role-" + role,
                                  "name": role + " persona", "created_at": ts,
                                  "updated_at": ts})
    appdb.insert("teams", {"id": TID, "name": "Browser team", "created_at": ts})
    for aid, role in (("dev1", "Senior Developer"), ("qa1", "QA Engineer")):
        appdb.insert("agents", {"id": aid, "name": aid.upper(), "role_id": "role-" + role,
                                "persona_id": "per-" + role, "created_at": ts,
                                "updated_at": ts})
        appdb.execute("INSERT INTO team_agents (team_id, agent_id) VALUES (?,?)", (TID, aid))
    appdb.insert("projects", {
        "id": PID, "name": "Browser Test", "goal": "g", "description": "",
        "workspace_path": "C:/nonexistent-ws", "team_id": TID, "po_enabled": 0,
        "sa_review_enabled": 0, "ba_review_enabled": 0,
        "created_at": ts, "updated_at": ts})
    appdb.insert("sprints", {"id": "spr-b", "project_id": PID, "name": "S1", "goal": "g",
                             "capacity": 5, "status": "Active", "created_at": ts})
    appdb.insert("tasks", {"id": "tkb1", "project_id": PID, "sprint_id": "spr-b",
                           "title": "Feature X", "description": "", "story_points": 2,
                           "priority": 2, "assigned_agent_id": "qa1", "qa_agent_id": "dev1",
                           "status": "Waiting QA", "rework_count": 0, "progress": 88,
                           "evidence": "", "blocked_reason": "",
                           "created_at": ts, "updated_at": ts})
    appdb.insert("workflow_runs", {"id": "run-b1", "project_id": PID, "task_id": "tkb1",
                                   "agent_id": "qa1", "status": "Running",
                                   "current_step": "qa-report", "mode": "qa",
                                   "started_at": ts})

    async def _fast_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(runtime.asyncio, "sleep", _fast_sleep)
    return {"ts": ts}


def _finish(fx, browser_ctrl, test_summary=""):
    task = appdb.query_one("SELECT * FROM tasks WHERE id='tkb1'")
    asyncio.run(runtime._finish_qa_run("run-b1", browser_ctrl, PID, "tkb1", "qa1",
                                       task, 10, 10, 0.01, test_summary))
    return appdb.query_one("SELECT * FROM tasks WHERE id='tkb1'")


def test_browser_failed_outranks_tests_pass(fx):
    t = _finish(fx, {"browser": {"status": "failed",
                                 "summary": "browser FAILED: root: server error 500"}},
                test_summary="tests passed: 5 ok")
    assert t["status"] == "Rework"
    assert "browser smoke failed" in t["blocked_reason"]
    assert t["rework_count"] == 1
    assert t["assigned_agent_id"] == "dev1"  # back to the developer


def test_browser_passed_without_unit_tests_is_a_real_pass(fx):
    t = _finish(fx, {"browser": {"status": "passed",
                                 "summary": "browser passed (3 pages)"}})
    assert t["status"] == "Done"
    assert "qa:passed" in t["evidence"]


def test_tests_fail_even_when_browser_passed(fx):
    t = _finish(fx, {"browser": {"status": "passed", "summary": "browser passed"}},
                test_summary="tests FAILED: 2 assertions")
    assert t["status"] == "Rework"
    assert "QA test run failed" in t["blocked_reason"]


def test_skipped_browser_defers_to_unit_tests(fx):
    t = _finish(fx, {"browser": {"status": "skipped",
                                 "summary": "browser skipped: not installed"}},
                test_summary="tests passed: 3 ok")
    assert t["status"] == "Done"


def test_run_smoke_skips_when_unavailable(monkeypatch):
    monkeypatch.setattr(browser_test, "available", lambda: (False, "Chromium not installed"))
    r = browser_test.run_smoke({"workspace_path": "C:/whatever", "technology_stack": "python"})
    assert r["status"] == "skipped"
    assert "Chromium" in r["summary"]


# ------------------------------------------------------------- integration

_HAS_PW = browser_test.available()[0]


def _mk_ws(tmp_path, main_src):
    ws = tmp_path / "ws"
    (ws / "app").mkdir(parents=True)
    (ws / "app" / "__init__.py").write_text("")
    (ws / "app" / "main.py").write_text(main_src, encoding="utf-8")
    return str(ws)


@pytest.mark.skipif(not _HAS_PW, reason="playwright/chromium not installed")
def test_run_smoke_passes_on_working_app(tmp_path):
    ws = _mk_ws(tmp_path, (
        "from fastapi import FastAPI\n"
        "from fastapi.responses import HTMLResponse\n"
        "app = FastAPI()\n"
        "@app.get('/', response_class=HTMLResponse)\n"
        "def home():\n"
        "    return \"<html><body><h1>Hi</h1><a href='/p2'>p2</a></body></html>\"\n"
        "@app.get('/p2', response_class=HTMLResponse)\n"
        "def p2():\n"
        "    return '<html><body><h1>Page 2</h1></body></html>'\n"))
    r = browser_test.run_smoke({"workspace_path": ws, "technology_stack": "python"})
    assert r["status"] == "passed", r["summary"]
    assert r["pages"] >= 2
    assert os.path.isdir(os.path.join(ws, "qa_artifacts"))


@pytest.mark.skipif(not _HAS_PW, reason="playwright/chromium not installed")
def test_run_smoke_fails_on_broken_app(tmp_path):
    ws = _mk_ws(tmp_path, "x = (\nfrom __future__ import annotations\n")
    r = browser_test.run_smoke({"workspace_path": ws, "technology_stack": "python"})
    assert r["status"] == "failed"
    assert "browser FAILED" in r["summary"]
