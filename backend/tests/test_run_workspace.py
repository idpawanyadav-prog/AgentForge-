"""Per-run isolated workspace + lease merge-back tests.

Runs against a throwaway SQLite DB (AGENT_OFFICE_DB) and temp folders;
no git required (commit_all degrades to None outside a repo).
"""
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

_TMP = tempfile.mkdtemp(prefix="af_runws_")
os.environ["AGENT_OFFICE_WORKSPACES"] = os.path.join(_TMP, "workspaces")

import pytest  # noqa: E402

from app import db as appdb  # noqa: E402
from app import workspace  # noqa: E402

PID = "proj-rw-1"


def _mk(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf8") as fh:
        fh.write(text)


@pytest.fixture()
def live_ws():
    for tbl in ("workflow_runs", "projects"):
        appdb.execute(f"DELETE FROM {tbl}")
    live = os.path.join(os.environ["AGENT_OFFICE_WORKSPACES"], "live-" + PID)
    os.makedirs(live, exist_ok=True)
    _mk(os.path.join(live, "app", "main.py"), "print('v1')\n")
    _mk(os.path.join(live, "lib", "pkg.py"), "X = 1\n")
    _mk(os.path.join(live, "__pycache__", "junk.pyc"), "junk")
    _mk(os.path.join(live, ".git", "config"), "[core]\n")
    _mk(os.path.join(live, "qa_artifacts", "old.png"), "old")
    ts = appdb.now()
    appdb.insert("projects", {"id": PID, "name": "RW", "goal": "g", "description": "",
                              "workspace_path": live, "team_id": None,
                              "created_at": ts, "updated_at": ts})
    yield live
    workspace._file_leases.pop(PID, None)


def _begin(run_id):
    return workspace.begin_run_workspace(PID, run_id)


def test_begin_copies_source_and_skips_caches(live_ws):
    run_ws, manifest = _begin("run-a")
    assert run_ws and os.path.isdir(run_ws)
    assert open(os.path.join(run_ws, "app", "main.py")).read() == "print('v1')\n"
    assert os.path.isfile(os.path.join(run_ws, "lib", "pkg.py"))
    assert ".git" not in os.listdir(run_ws)
    assert "__pycache__" not in os.listdir(run_ws)
    assert "qa_artifacts" not in os.listdir(run_ws)
    assert set(manifest) == {"app/main.py", "lib/pkg.py"}
    workspace.discard_run_workspace(run_ws)
    assert not os.path.isdir(run_ws)


def test_merge_applies_modified_new_and_artifacts(live_ws):
    run_ws, manifest = _begin("run-b")
    _mk(os.path.join(run_ws, "app", "main.py"), "print('v2 from run')\n")
    _mk(os.path.join(run_ws, "notes.md"), "hello\n")
    _mk(os.path.join(run_ws, "qa_artifacts", "shot.png"), "screenshot")
    res = workspace.merge_back_run_workspace(PID, "run-b", run_ws, manifest)
    assert sorted(res["applied"]) == ["app/main.py", "notes.md", "qa_artifacts/shot.png"]
    assert not res["conflicts"] and not res["error"]
    assert open(os.path.join(live_ws, "app", "main.py")).read() == "print('v2 from run')\n"
    assert os.path.isfile(os.path.join(live_ws, "notes.md"))
    assert not workspace._file_leases.get(PID), "leases must be released"


def test_merge_failure_rolls_back_all_replaced_files(live_ws, monkeypatch):
    note_path = os.path.join(live_ws, "notes.md")
    prior_note = open(note_path).read() if os.path.isfile(note_path) else None
    run_ws, manifest = _begin("run-rollback")
    _mk(os.path.join(run_ws, "app", "main.py"), "print('new')\n")
    _mk(os.path.join(run_ws, "notes.md"), "new file\n")
    original_replace = workspace.os.replace
    calls = 0

    def fail_second_replace(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated merge failure")
        return original_replace(src, dst)

    monkeypatch.setattr(workspace.os, "replace", fail_second_replace)
    result = workspace.merge_back_run_workspace(PID, "run-rollback", run_ws, manifest)
    assert "simulated merge failure" in result["error"]
    assert result["applied"] == []
    assert open(os.path.join(live_ws, "app", "main.py")).read() == "print('v1')\n"
    assert (open(note_path).read() if os.path.isfile(note_path) else None) == prior_note
    assert not workspace._file_leases.get(PID)


def test_merge_of_untouched_copy_changes_nothing(live_ws):
    run_ws, manifest = _begin("run-c")
    res = workspace.merge_back_run_workspace(PID, "run-c", run_ws, manifest)
    assert res["applied"] == [] and res["conflicts"] == []
    assert not res["error"]


def test_project_quota_rejects_merge_without_changes(live_ws, monkeypatch):
    run_ws, manifest = _begin("run-quota")
    _mk(os.path.join(run_ws, "large.txt"), "x" * 100)
    monkeypatch.setattr(workspace, "MAX_PROJECT_WORKSPACE_BYTES", 50)
    result = workspace.merge_back_run_workspace(PID, "run-quota", run_ws, manifest)
    assert "quota exceeded" in result["error"]
    assert not os.path.exists(os.path.join(live_ws, "large.txt"))


def test_concurrent_merges_cannot_exceed_project_quota(live_ws, monkeypatch):
    first_ws, first_manifest = _begin("quota-one")
    second_ws, second_manifest = _begin("quota-two")
    _mk(os.path.join(first_ws, "first.txt"), "a" * 10)
    _mk(os.path.join(second_ws, "second.txt"), "b" * 10)
    monkeypatch.setattr(workspace, "commit_all", lambda *_: None)
    monkeypatch.setattr(workspace, "MAX_PROJECT_WORKSPACE_BYTES",
                        workspace._workspace_bytes(live_ws) + 15)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(workspace.merge_back_run_workspace, PID, "quota-one",
                          first_ws, first_manifest)
        two = pool.submit(workspace.merge_back_run_workspace, PID, "quota-two",
                          second_ws, second_manifest)
        results = [one.result(), two.result()]
    assert sum(not r["error"] for r in results) == 1
    assert sum(os.path.exists(os.path.join(live_ws, name)) for name in
               ("first.txt", "second.txt")) == 1


def test_startup_recovers_interrupted_merge(live_ws):
    stage = tempfile.mkdtemp(prefix=".af-merge-", dir=os.path.dirname(live_ws))
    old = os.path.join(stage, "old", "app", "main.py")
    _mk(old, "print('v1')\n")
    workspace._write_merge_journal(stage, live_ws, ["app/main.py", "new.txt"])
    _mk(os.path.join(live_ws, "app", "main.py"), "print('partial')\n")
    _mk(os.path.join(live_ws, "new.txt"), "partial")
    assert workspace.recover_merge_journals() == 1
    assert open(os.path.join(live_ws, "app", "main.py")).read() == "print('v1')\n"
    assert not os.path.exists(os.path.join(live_ws, "new.txt"))
    assert not os.path.exists(stage)


def test_merge_detects_conflict_with_externally_changed_live_file(live_ws):
    run_ws, manifest = _begin("run-d")
    # another agent/human moved the live file after our clone
    _mk(os.path.join(live_ws, "app", "main.py"), "print('someone else')\n")
    _mk(os.path.join(run_ws, "app", "main.py"), "print('my run')\n")
    res = workspace.merge_back_run_workspace(PID, "run-d", run_ws, manifest)
    assert "app/main.py" in res["conflicts"]
    assert "app/main.py" in res["applied"]  # run version wins, live state was committed
    assert open(os.path.join(live_ws, "app", "main.py")).read() == "print('my run')\n"


def test_lease_held_by_other_run_is_awaited_then_forced(live_ws, monkeypatch):
    monkeypatch.setattr(workspace, "_RUNWS_LEASE_WAIT_S", 0.1)
    run_ws, manifest = _begin("run-e")
    _mk(os.path.join(run_ws, "app", "main.py"), "print('mine')\n")
    workspace._file_leases.setdefault(PID, {})["app/main.py"] = "other-run"
    res = workspace.merge_back_run_workspace(PID, "run-e", run_ws, manifest)
    assert "app/main.py" in res["conflicts"]  # forced past the held lease
    # our lease took over; the stale holder entry is gone
    assert workspace._file_leases[PID] == {}


def test_stale_copy_of_finished_run_is_garbage_collected(live_ws):
    ts = appdb.now()
    appdb.insert("workflow_runs", {"id": "run-old", "project_id": PID, "task_id": None,
                                   "agent_id": None, "status": "Completed",
                                   "current_step": "", "started_at": ts})
    old_dir = os.path.join(workspace.run_ws_root(), "run-old")
    _mk(os.path.join(old_dir, "x.py"), "x")
    run_ws, _ = _begin("run-f")
    assert not os.path.isdir(old_dir)
    assert os.path.isdir(run_ws)


def test_no_workspace_returns_empty(live_ws):
    appdb.execute("UPDATE projects SET workspace_path='' WHERE id=?", (PID,))
    run_ws, manifest = _begin("run-g")
    assert run_ws == "" and manifest == {}


def test_repository_url_validation():
    assert workspace._is_git_url("https://example.com/team/repo.git")
    assert workspace._is_git_url("git@example.com:team/repo.git")
    for url in ("-u evil", "ext::sh -c whoami", "file:///tmp/repo.git",
                "https://user:secret@example.com/repo.git", "repo.git"):
        assert not workspace._is_git_url(url)


def test_run_copy_failure_is_reported(live_ws, monkeypatch):
    original = workspace.shutil.copy2
    def fail_source(src, dst, *args, **kwargs):
        if src.endswith("main.py") and str(live_ws) in src:
            raise OSError("locked")
        return original(src, dst, *args, **kwargs)
    monkeypatch.setattr(workspace.shutil, "copy2", fail_source)
    run_ws, manifest = _begin("run-copy-fail")
    assert (run_ws, manifest) == ("", {})


# ------------------------------------------------ end-to-end runtime isolation

def test_dev_run_works_on_copy_and_merges_back(live_ws, monkeypatch):
    """Full _run_phases dev loop: every write during the run must land in the
    .af-run-ws copy (never the live folder), and only the merge-back at the
    end may touch the live workspace."""
    import asyncio
    from app import browser_test, codegen, runtime, toolchains  # noqa: E402

    ts = appdb.now()
    for tbl in ("usage_records", "workflow_runs", "tasks", "sprints", "team_agents",
                "teams", "agents", "personas", "roles"):
        appdb.execute(f"DELETE FROM {tbl}")
    appdb.insert("roles", {"id": "r-dev", "name": "Senior Developer",
                           "created_at": ts, "updated_at": ts})
    appdb.insert("personas", {"id": "p-dev", "role_id": "r-dev", "name": "persona",
                              "created_at": ts, "updated_at": ts})
    appdb.insert("teams", {"id": "t-rw", "name": "RW team", "created_at": ts})
    appdb.insert("agents", {"id": "a-dev", "name": "DEV", "role_id": "r-dev",
                            "persona_id": "p-dev", "created_at": ts, "updated_at": ts})
    appdb.execute("INSERT INTO team_agents (team_id, agent_id) VALUES (?,?)",
                  ("t-rw", "a-dev"))
    appdb.insert("sprints", {"id": "sp-rw", "project_id": PID, "name": "S1", "goal": "g",
                             "capacity": 5, "status": "Active", "created_at": ts})
    appdb.insert("tasks", {"id": "tk-rw", "project_id": PID, "sprint_id": "sp-rw",
                           "title": "Generate a module", "description": "", "story_points": 1,
                           "priority": 2, "assigned_agent_id": "a-dev",
                           "qa_agent_id": "a-dev", "status": "In Progress",
                           "rework_count": 0, "progress": 55, "evidence": "",
                           "blocked_reason": "", "created_at": ts, "updated_at": ts})
    appdb.insert("workflow_runs", {"id": "run-e2e", "project_id": PID, "task_id": "tk-rw",
                                   "agent_id": "a-dev", "status": "Running",
                                   "current_step": "analyze", "mode": "dev",
                                   "started_at": ts})

    seen = []

    def fake_gen(project, task, agent_ref, feedback="", pinned_ref=None, deadline=None):
        ws = project["workspace_path"]
        seen.append(ws)
        # the core guarantee: the codegen target is the temp copy, never live
        assert ws.startswith(workspace.run_ws_root()), "codegen must write to the copy"
        assert not os.path.isfile(os.path.join(live_ws, "app", "gen.py")), \
            "live workspace must stay untouched mid-run"
        with open(os.path.join(ws, "app", "gen.py"), "w", encoding="utf8") as fh:
            fh.write("VALUE = 'merged'\n")
        return {"files": ["app/gen.py"], "mode": "scaffold", "input_tokens": 5,
                "output_tokens": 5, "summary": "ok", "error": "",
                "pinned_ref": pinned_ref or "pin-1", "fallbacks": []}

    async def _fast_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(codegen, "generate_implementation", fake_gen)
    monkeypatch.setattr(toolchains, "detect_stack", lambda *a, **k: "python")
    monkeypatch.setattr(toolchains, "failing_tests", lambda *a, **k: set())
    monkeypatch.setattr(toolchains, "check_modules", lambda *a, **k: {"ok": True, "installed": []})
    monkeypatch.setattr(toolchains, "build_check", lambda *a, **k: (True, "build ok"))
    monkeypatch.setattr(toolchains, "run_stack_tests", lambda *a, **k: (True, "tests passed: 1"))
    monkeypatch.setattr(browser_test, "run_smoke",
                        lambda *a, **k: {"status": "passed", "summary": "browser passed (1 pages)",
                                         "pages": 1, "screenshots": []})
    monkeypatch.setattr(runtime.asyncio, "sleep", _fast_sleep)

    ctrl = {"paused": False, "cancelled": False, "pinned_ref": None}
    asyncio.run(runtime._run_phases("run-e2e", ctrl, "dev"))

    assert seen, "codegen never ran"
    run_ws = seen[0]
    # merged back into live:
    assert open(os.path.join(live_ws, "app", "gen.py")).read() == "VALUE = 'merged'\n"
    # run copy cleaned up:
    assert not os.path.isdir(run_ws)
    # isolation + merge events recorded:
    types = [r["event_type"] for r in appdb.query(
        "SELECT event_type FROM execution_events WHERE workflow_run_id='run-e2e'")]
    assert "workspace.run_isolated" in types
    assert "workspace.committed" in types
    assert not workspace._file_leases.get(PID)
