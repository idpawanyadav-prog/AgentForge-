"""Real on-disk workspaces for AgentForge projects.

Every project gets a real folder on the local machine:
- If the project has a git repository URL, the workspace is a clone of that
  repository.
- Otherwise a fresh folder is created under the local ``Projects`` root
  (``<repo>/Projects`` by default, overridable via ``AGENT_OFFICE_WORKSPACES``).

During task execution agents write real deliverable files into the workspace
and best-effort commit them when the workspace is a git repository.
"""
import logging
import os
import re
import shutil
import subprocess
import threading
import time

from .db import execute, now, query, query_one, update

logger = logging.getLogger(__name__)

# Per-project git locks: with parallel task execution several agents can
# finish and commit in the same workspace at the same time. Serializing
# add+commit per workspace avoids git index.lock races.
_commit_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _commit_lock(project_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _commit_locks.get(project_id)
        if lock is None:
            lock = _commit_locks[project_id] = threading.Lock()
        return lock


def workspaces_root() -> str:
    root = os.environ.get("AGENT_OFFICE_WORKSPACES")
    if not root:
        # <repo>/Projects — sits next to the backend/ folder.
        root = os.path.join(os.path.dirname(__file__), "..", "..", "Projects")
    return os.path.abspath(root)


def relocate_workspaces() -> int:
    """Re-point stored workspace paths whose folder was moved — e.g. when the
    projects root moved out of the published source tree (runtime data such as
    generated workspaces must not ship with the source). Returns count fixed."""
    moved = 0
    for row in query("SELECT id, workspace_path FROM projects "
                     "WHERE workspace_path IS NOT NULL AND workspace_path != ''"):
        old = row["workspace_path"]
        if os.path.isdir(old):
            continue
        leaf = os.path.basename(old.rstrip("\\/"))
        cand = os.path.join(workspaces_root(), leaf)
        if os.path.isdir(cand):
            execute("UPDATE projects SET workspace_path=?, updated_at=? WHERE id=?",
                    (cand, now(), row["id"]))
            moved += 1
    return moved


def safe_dir_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "project").strip()).strip("-.")
    return cleaned or "project"


def validate_workspace_path(path: str | None) -> str | None:
    """Validate an explicit workspace path for a project.

    Returns an error message string when the path is unusable, otherwise
    ``None``. Empty/blank paths are always valid (the caller falls back to
    the default location under the workspace root).

    Guards against the failure modes called out in the code review:
    a misconfigured path that points at a filesystem root, a read-only
    directory, or a location that cannot be created — each of which would
    otherwise fail silently during code generation.
    """
    path = (path or "").strip()
    if not path:
        return None
    abs_path = os.path.abspath(path)
    # Reject filesystem roots (e.g. "C:\\", "/") — nothing should be
    # generated directly into a drive root.
    parent = os.path.dirname(abs_path)
    if abs_path == parent:
        return f"workspace_path '{path}' is a filesystem root — choose a subdirectory"
    try:
        if os.path.isdir(abs_path):
            if not os.access(abs_path, os.W_OK):
                return f"workspace_path '{path}' is not writable"
            return None
        # Confirm the directory tree is actually creatable (and writable)
        # before the project starts writing deliverables into it.
        os.makedirs(abs_path, exist_ok=True)
        return None
    except OSError as exc:
        return f"workspace_path '{path}' could not be created: {exc}"


def get_workspace(project_id: str) -> str | None:
    row = query_one("SELECT workspace_path FROM projects WHERE id = ?", (project_id,))
    if row and row["workspace_path"] and os.path.isdir(row["workspace_path"]):
        return row["workspace_path"]
    return None


def _git(args, cwd=None) -> str | None:
    git = shutil.which("git")
    if not git:
        return None
    try:
        proc = subprocess.run([git, *args], cwd=cwd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            return None
        return (proc.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


def _is_git_url(url: str) -> bool:
    return bool(url) and (url.startswith(("http://", "https://", "git@", "ssh://")) or url.endswith(".git"))


def _write_readme(ws_dir: str, project):
    readme = os.path.join(ws_dir, "README.md")
    if os.path.exists(readme):
        return
    stack = project["technology_stack"] or "Not specified"
    content = (f"# {project['name']}\n\n"
               f"**Goal:** {project['goal'] or '—'}\n\n"
               f"{project['description'] or ''}\n\n"
               f"## Technology stack\n\n{stack}\n\n"
               f"Managed by AgentForge. Agent deliverables are written to `docs/tasks/`.\n")
    os.makedirs(os.path.dirname(readme), exist_ok=True)
    with open(readme, "w", encoding="utf-8") as fh:
        fh.write(content)


def prepare_workspace(project) -> dict:
    """Create (or clone) the real workspace folder for a project.

    Returns {"workspace_path", "cloned", "git_init", "note"}.
    Never raises: on clone failure the project still gets a local folder.
    """
    project_id = project["id"]
    explicit = (project.get("workspace_path") or "").strip()
    target = explicit or os.path.join(workspaces_root(), f"{safe_dir_name(project['name'])}-{project_id[:6]}")
    target = os.path.abspath(target)

    result = {"workspace_path": target, "cloned": False, "git_init": False, "note": ""}
    repo_url = (project.get("repository_url") or "").strip()

    if repo_url and _is_git_url(repo_url) and not os.path.isdir(os.path.join(target, ".git")):
        if os.path.isdir(target) and os.listdir(target):
            result["note"] = "Clone skipped: target folder is not empty"
        else:
            out = _git(["clone", repo_url, target])
            if out is not None:
                result["cloned"] = True
            else:
                result["note"] = "Clone failed (unreachable or private repository); created local folder instead"

    if not os.path.isdir(target):
        os.makedirs(target, exist_ok=True)

    if not os.path.isdir(os.path.join(target, ".git")):
        if _git(["init", "-q"], cwd=target) is not None:
            result["git_init"] = True

    _write_readme(target, project)
    update("projects", project_id, {"workspace_path": target, "updated_at": now()})
    return result


def write_deliverable(project_id: str, task, run_id: str) -> str | None:
    """Write a real deliverable file for a task into the project workspace.

    Returns the path relative to the workspace root, or None if the workspace
    is unavailable.
    """
    ws_dir = get_workspace(project_id)
    if not ws_dir:
        return None
    rel_dir = os.path.join("docs", "tasks")
    os.makedirs(os.path.join(ws_dir, rel_dir), exist_ok=True)
    slug = safe_dir_name(task["title"]).lower()[:40]
    rel_path = os.path.join(rel_dir, f"{slug}-{run_id[:8]}.md")
    content = (f"# {task['title']}\n\n"
               f"- **Task:** {task['id']}\n"
               f"- **Run:** {run_id}\n"
               f"- **Status:** implemented by agent\n\n"
               f"## Description\n\n{task['description'] or '—'}\n\n"
               f"## Acceptance criteria\n\n{task['acceptance_criteria'] or '—'}\n")
    try:
        with open(os.path.join(ws_dir, rel_path), "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError:
        return None
    return rel_path.replace("\\", "/")


def commit_all(project_id: str) -> str | None:
    """Best-effort `git add -A` + commit of the workspace. Returns short hash.
    Serialized per workspace so concurrent agent runs can't race on git."""
    ws_dir = get_workspace(project_id)
    if not ws_dir or not os.path.isdir(os.path.join(ws_dir, ".git")):
        return None
    with _commit_lock(project_id):
        _git(["add", "-A"], cwd=ws_dir)
        _git(["-c", "user.name=AgentForge", "-c", "user.email=agents@agentforge.local",
              "commit", "-q", "-m", "AgentForge: agent deliverable update"], cwd=ws_dir)
        return _git(["rev-parse", "--short", "HEAD"], cwd=ws_dir)


# ------------------------------------------------------------------ run isolation
#
# Every dev/QA workflow run works on a TEMPORARY COPY of the project workspace
# (<workspaces_root>/.af-run-ws/<run_id>/), never on the live folder. Two
# agents can therefore never edit the same file at the same time, and QA
# activity (pytest, booting the app, browser smoke, pip self-heal) cannot
# disturb other agents' on-going work. When a run finishes successfully its
# changed files are merged back under a per-file lease registry: merges that
# touch overlapping files serialize instead of clobbering each other, and a
# live file that moved since the clone is committed to git BEFORE being
# overwritten, so no agent's work is silently lost.

_RUNWS_LEASE_WAIT_S = 90.0
_ENV_DIRS = (".venv", "node_modules")            # linked, never copied/merged
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
# old QA screenshots are dead weight in a fresh copy; NEW artifacts created by
# the run still flow back to the live workspace through merge (kept out of
# _SKIP_DIRS so the merge walk sees them)
_COPY_SKIP_DIRS = _SKIP_DIRS | {"qa_artifacts"}

_file_leases: dict[str, dict[str, str]] = {}     # project_id -> {rel_norm: run_id}
_lease_guard = threading.Lock()


def run_ws_root() -> str:
    return os.path.join(workspaces_root(), ".af-run-ws")


def _rel_key(base: str, path: str) -> str:
    return os.path.relpath(path, base).replace("\\", "/")


def _env_link(link_path: str, target: str) -> bool:
    """Point a run copy's .venv/node_modules at the live one (environment,
    not source). Returns False when linking is unavailable — the run then
    simply has no env folder and self-heals via lib/ + PYTHONPATH."""
    try:
        if os.name == "nt":
            os.makedirs(os.path.dirname(link_path), exist_ok=True)
            r = subprocess.run(["cmd", "/c", "mklink", "/J", link_path, target],
                               capture_output=True, text=True, timeout=60)
            return r.returncode == 0 and os.path.isdir(link_path)
        os.symlink(target, link_path)
        return True
    except Exception as exc:
        logger.warning("env link %s -> %s failed: %s", link_path, target, exc)
        return False


def _copy_for_run(src: str, dst: str) -> dict:
    """Copy the live tree into the run folder. Returns the merge manifest:
    rel-path -> (size, mtime_ns) captured at clone time. Env dirs are linked,
    caches/git are skipped."""
    manifest: dict[str, tuple[int, int]] = {}
    for cur, dirs, files in os.walk(src):
        rel_root = _rel_key(src, cur)
        base = "" if rel_root == "." else rel_root + "/"
        dirs[:] = sorted(d for d in dirs if d not in _COPY_SKIP_DIRS)
        for d in list(dirs):
            if d in _ENV_DIRS:
                dirs.remove(d)
                _env_link(os.path.join(dst, base + d), os.path.join(cur, d))
        dst_dir = os.path.join(dst, base) if base else dst
        os.makedirs(dst_dir, exist_ok=True)
        for f in sorted(files):
            s = os.path.join(cur, f)
            try:
                shutil.copy2(s, os.path.join(dst_dir, f))
                st = os.stat(s)
                manifest[base + f] = (st.st_size, st.st_mtime_ns)
            except OSError:
                continue  # transient lock on some file: the run sees the rest
    return manifest


def _gc_run_workspaces(root: str) -> None:
    """Remove copies left behind by finished runs (crash, interrupted cleanup)."""
    if not os.path.isdir(root):
        return
    for name in os.listdir(root):
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        try:
            row = query_one("SELECT status FROM workflow_runs WHERE id = ?", (name,))
            stale = (time.time() - os.path.getmtime(p)) > 24 * 3600
            if row is None or row["status"] != "Running" or stale:
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            continue


def begin_run_workspace(project_id: str, run_id: str) -> tuple[str, dict]:
    """Create the isolated per-run copy. Returns ("", {}) when there is no
    workspace or the copy failed — the run then behaves as before (live dir)."""
    ws_dir = get_workspace(project_id)
    if not ws_dir:
        return "", {}
    root = run_ws_root()
    try:
        _gc_run_workspaces(root)
        os.makedirs(root, exist_ok=True)
        dst = os.path.join(root, run_id)
        if os.path.isdir(dst):
            shutil.rmtree(dst, ignore_errors=True)
        os.makedirs(dst, exist_ok=True)
        return dst, _copy_for_run(ws_dir, dst)
    except OSError as exc:
        logger.warning("run workspace for %s could not be created: %s", run_id, exc)
        try:
            shutil.rmtree(os.path.join(root, run_id), ignore_errors=True)
        except OSError:
            pass
        return "", {}


def _run_changed_files(run_ws: str, manifest: dict) -> list[str]:
    """Files in the run copy that are new or modified since the clone."""
    changed: list[str] = []
    for cur, dirs, files in os.walk(run_ws):
        rel_root = _rel_key(run_ws, cur)
        base = "" if rel_root == "." else rel_root + "/"
        dirs[:] = sorted(d for d in dirs
                         if d not in _SKIP_DIRS and not (base == "" and d in _ENV_DIRS))
        for f in sorted(files):
            rel = base + f
            s = os.path.join(cur, f)
            entry = manifest.get(rel)
            if entry is None:
                changed.append(rel)
                continue
            try:
                st = os.stat(s)
            except OSError:
                continue
            if (st.st_size, st.st_mtime_ns) != entry:
                changed.append(rel)
    return changed


def merge_back_run_workspace(project_id: str, run_id: str, run_ws: str,
                             manifest: dict) -> dict:
    """Apply the run's changed files to the live workspace under per-file
    leases. Never raises. Returns {applied, conflicts, commit, error}."""
    result: dict = {"applied": [], "conflicts": [], "commit": None, "error": ""}
    ws_dir = get_workspace(project_id)
    if not ws_dir or not run_ws or not os.path.isdir(run_ws):
        result["error"] = "live or run workspace missing"
        return result
    try:
        changed = _run_changed_files(run_ws, manifest)
        if not changed:
            return result
        keys = [r.lower() for r in changed]
        leased = False
        deadline = time.monotonic() + _RUNWS_LEASE_WAIT_S
        while True:
            with _lease_guard:
                reg = _file_leases.setdefault(project_id, {})
                holders = {k for k in keys if reg.get(k) and reg[k] != run_id}
                if not holders or time.monotonic() >= deadline:
                    if holders:  # waited too long: proceed anyway, flagged below
                        for k in keys:
                            if reg.get(k) and reg[k] != run_id:
                                result["conflicts"].append(
                                    changed[keys.index(k)])
                    for k in keys:
                        reg[k] = run_id
                    leased = True
                    break
            time.sleep(1.0)
        try:
            # Git safety net: capture whatever the live folder holds BEFORE
            # overwriting, so a lost-update conflict is recoverable from history.
            commit_all(project_id)
            for rel in changed:
                src = os.path.join(run_ws, *rel.split("/"))
                dst = os.path.join(ws_dir, *rel.split("/"))
                m_entry = manifest.get(rel)
                if m_entry is not None:
                    try:
                        live_st = os.stat(dst)
                        if (live_st.st_size, live_st.st_mtime_ns) != m_entry \
                                and rel not in result["conflicts"]:
                            result["conflicts"].append(rel)
                    except OSError:
                        pass  # deleted live or never existed: a new file for us
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                result["applied"].append(rel)
            result["commit"] = commit_all(project_id)
        except OSError as exc:
            result["error"] = str(exc)[:200]
        finally:
            if leased:
                with _lease_guard:
                    reg = _file_leases.get(project_id, {})
                    for k in keys:
                        if reg.get(k) == run_id:
                            del reg[k]
    except Exception as exc:  # merge must never fail the run itself
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
    result["conflicts"] = sorted(set(result["conflicts"]))
    return result


def discard_run_workspace(run_ws: str) -> None:
    """Delete a run copy (after merge, or after cancel/failure without merge)."""
    if not run_ws or not os.path.isdir(run_ws):
        return
    for _ in range(5):
        shutil.rmtree(run_ws, ignore_errors=True)
        if not os.path.isdir(run_ws):
            return
        time.sleep(0.6)
    logger.warning("run workspace %s could not be removed; GC will retry", run_ws)
