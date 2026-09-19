"""Real on-disk workspaces for AgentForge projects.

Every project gets a real folder on the local machine:
- If the project has a git repository URL, the workspace is a clone of that
  repository.
- Otherwise a fresh folder is created under the local ``Projects`` root
  (``<repo>/Projects`` by default, overridable via ``AGENT_OFFICE_WORKSPACES``).

During task execution agents write real deliverable files into the workspace
and best-effort commit them when the workspace is a git repository.
"""
import os
import re
import shutil
import subprocess

from .db import execute, now, query_one, update


def workspaces_root() -> str:
    root = os.environ.get("AGENT_OFFICE_WORKSPACES")
    if not root:
        # <repo>/Projects — sits next to the backend/ folder.
        root = os.path.join(os.path.dirname(__file__), "..", "..", "Projects")
    return os.path.abspath(root)


def safe_dir_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "project").strip()).strip("-.")
    return cleaned or "project"


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
    """Best-effort `git add -A` + commit of the workspace. Returns short hash."""
    ws_dir = get_workspace(project_id)
    if not ws_dir or not os.path.isdir(os.path.join(ws_dir, ".git")):
        return None
    _git(["add", "-A"], cwd=ws_dir)
    _git(["-c", "user.name=AgentForge", "-c", "user.email=agents@agentforge.local",
          "commit", "-q", "-m", "AgentForge: agent deliverable update"], cwd=ws_dir)
    return _git(["rev-parse", "--short", "HEAD"], cwd=ws_dir)
