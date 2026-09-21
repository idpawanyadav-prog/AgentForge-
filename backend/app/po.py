"""Product Owner agent: per-project autonomous authority mode.

A project team may include an agent with the "Product Owner" role. When the
project's ``po_enabled`` flag is on, that agent acts on the user's behalf:

- reviews the whole requirement set (goal, backlog, sprints, tasks),
- rewrites requirements and sprint tasks directly (no human approval loop),
- assigns tasks, unblocks escalated items and starts/stops sprints,
- keeps the delivery loop running until the project is complete.

The user talks to the PO through a dedicated chat (with file attachments);
disabling the toggle hands control back and leaves every open task untouched.
"""
import json
import logging
import os
import threading

from . import db, workspace, runtime, toolchains, sprint_gate, config
from .db import audit, emit_event, execute, insert, new_id, now, query, query_one, update

logger = logging.getLogger(__name__)

PO_ROLE = "Product Owner"
PO_CONV_TITLE = "Product Owner"
MAIN_CONV_TITLE = "Project Control"

_FAMILY_WORD = {"dev": "development", "qa": "QA", "architecture": "architecture",
                "requirements": "business analysis", "devops": "DevOps", "design": "design"}

_MAX_ATTACHMENT_CHARS = 8000
_MAX_ACTION_TOKENS = 1800

PO_SYSTEM_PROMPT = """You are the Product Owner agent of "AgentForge" with FULL DECISION AUTHORITY \
over this project, acting on the owner's behalf. You review requirements, shape the backlog and \
sprints, and direct the delivery team. Be decisive: do not ask for approval, act.

You respond with ONLY a JSON object (no markdown fences):
{"reply": "<short message to the owner summarizing what you decided and why>",
 "actions": [ ... ]}

Available actions:
- {"action": "update_project", "goal"?: str, "description"?: str}
- {"action": "add_backlog", "items": [{"title": str, "description"?: str, "priority"?: 1-3,
    "acceptance_criteria"?: str, "points"?: 1-8}]}
- {"action": "create_sprint", "name": str, "goal"?: str, "capacity"?: int,
    "tasks": [{"title": str, "description"?: str, "points"?: 1-8, "priority"?: 1-3}]}
- {"action": "add_task", "title": str, "description"?: str, "points"?: 1-8, "priority"?: 1-3,
    "sprint"?: "<sprint number/name; default: current active sprint>"}
- {"action": "update_task", "task": "<title or partial>", "title"?: str, "description"?: str,
    "priority"?: 1-3, "points"?: 1-8}
- {"action": "assign_task", "task": "<title or partial>", "agent": "<agent name>"}
- {"action": "install_library", "packages": ["package1", "package2"],
    "reason"?: str} — pip-installs missing Python packages into the QA/dev environment.
    Use it whenever a task is blocked by ModuleNotFoundError / ImportError (e.g. sqlalchemy,
    slowapi), then unblock_task the affected task(s) so they rerun.
- {"action": "unblock_task", "task": "<title or partial>",
    "note"?: str} — clears the blocker and makes the task Ready again (also resets its rework counter)
- {"action": "cancel_task", "task": "<title or partial>", "reason"?: str}
- {"action": "start_sprint", "sprint"?: "<sprint number/name; default: active or first planned>"}
- {"action": "stop_sprint"}

Rules:
- Task titles must match real work: development tasks go to developers, QA tasks to QA agents,
  architecture to the Solution Architect. Use "assign_task" only when the default role-based
  assignment is wrong.
- SPRINT GATING: only one sprint runs at a time. The next sprint stays LOCKED until the current
  one completes all gates (build, tests, functional validation, acceptance). Never plan work
  into a future sprint to work around this — the current sprint must finish first.
- Urgent scope MAY be added to the current active sprint at any time (even mid-validation):
  use add_task with the current sprint — the sprint re-opens for development automatically
  and gates re-run after the new work completes. Non-urgent scope waits for the next sprint.
- Unblock escalated tasks when the fix is obvious (rework, small defect) — say so in the reply.
- Missing-dependency blockers are yours to fix: install the library yourself, never mark the
  task Done or cancel it because of ModuleNotFoundError.
- NEVER duplicate completed work. If a task is already Done, do not create it again in another
  sprint. When a task is Blocked, fix the cause (install_library + unblock_task) instead of
  re-creating it as a new task.
- Keep all idle developers busy: when several independent tasks are open, they should run in
  parallel across every idle developer, not one after another on a single agent.
- Keep "reply" under 6 sentences. Never invent agents that are not on the team roster.
- If nothing needs to change, return an empty "actions" list.

Current project state:
"""


# ---------------------------------------------------------------- narration

def po_narrate(project_id: str, text: str):
    """Post a Product Owner update into the project's MAIN control chat so
    autonomous PO activity is visible where the owner chats. The dedicated
    'Product Owner' conversation is excluded (it has its own thread)."""
    if not str(text or "").strip():
        return None
    conv = query_one(
        "SELECT * FROM conversations WHERE project_id = ? AND title != ? "
        "ORDER BY updated_at DESC LIMIT 1", (project_id, PO_CONV_TITLE))
    if not conv:
        ts = now()
        insert("conversations", {"id": (cid := new_id()), "project_id": project_id,
                                 "title": MAIN_CONV_TITLE, "created_at": ts, "updated_at": ts})
        conv = query_one("SELECT * FROM conversations WHERE id = ?", (cid,))
    insert("messages", {"id": new_id(), "conversation_id": conv["id"], "role": "assistant",
                        "content": text, "created_at": now()})
    update("conversations", conv["id"], {"updated_at": now()})
    return conv["id"]


def _narrate_actions(project_id: str, agent_name: str, summary: str, actions: list[str]):
    """Main-chat narration for an autonomous PO pass that took actions."""
    agent_name = agent_name or "Product Owner"
    lines = [f"👑 **{agent_name} (Product Owner)** — autonomous update:",
             summary or "I reviewed the project and took action."]
    if actions:
        lines.append("")
        lines.extend(f"- {a}" for a in actions)
    po_narrate(project_id, "\n".join(lines))


# ---------------------------------------------------------------- status

def po_agent_for_project(project_id: str):
    """The Product Owner agent on this project's aligned team (or None)."""
    role = query_one("SELECT id FROM roles WHERE lower(name) = lower(?)", (PO_ROLE,))
    if not role:
        return None
    return query_one(
        "SELECT a.*, r.name AS role_name FROM team_agents ta "
        "JOIN teams tm ON tm.id = ta.team_id "
        "JOIN agents a ON a.id = ta.agent_id JOIN roles r ON r.id = a.role_id "
        "JOIN projects p ON p.team_id = tm.id "
        "WHERE p.id = ? AND a.role_id = ? AND ta.active = 1 LIMIT 1",
        (project_id, role["id"]))


def po_status(project_id: str) -> dict:
    agent = po_agent_for_project(project_id)
    project = query_one("SELECT po_enabled FROM projects WHERE id = ?", (project_id,))
    return {
        "has_po": agent is not None,
        "po_enabled": bool(project and project["po_enabled"]),
        "agent_name": agent["name"] if agent else None,
    }


def po_enabled(project_id: str) -> bool:
    st = po_status(project_id)
    return st["has_po"] and st["po_enabled"]


def set_po_enabled(project_id: str, enabled: bool) -> dict:
    project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        return {"error": "Project not found"}
    agent = po_agent_for_project(project_id)
    if enabled and not agent:
        return {"error": (f"No {PO_ROLE} agent on this project's team — add one first, e.g. "
                          f"say `hire product owner` in the control chat.")}
    update("projects", project_id, {"po_enabled": 1 if enabled else 0, "updated_at": now()})
    if enabled:
        audit("po_enabled", "project", project_id,
              f"Product Owner agent '{agent['name']}' enabled for project '{project['name']}'",
              actor="product-owner")
        # Full autonomy: make sure delivery is actually running.
        started = None
        start_error = None
        if project_id not in runtime._schedulers:
            active = query_one(
                "SELECT id FROM sprints WHERE project_id = ? AND status IN ('Active','Planned')",
                (project_id,))
            if active:
                result = runtime.start_sprint_execution(project_id)
                started = result.get("sprint") if result.get("ok") else None
                start_error = result.get("error")
        if started:
            note = (f"{agent['name']} ({PO_ROLE}) now acts with full authority on the owner's behalf. "
                    f"Sprint '{started}' is running under PO control. An initial project review is next.")
        elif start_error:
            note = (f"{agent['name']} ({PO_ROLE}) now acts with full authority on the owner's behalf. "
                    f"Sprint could not start ({start_error}) — the PO is reviewing the project now "
                    "to plan next steps.")
        else:
            note = (f"{agent['name']} ({PO_ROLE}) now acts with full authority on the owner's behalf. "
                    "An initial project review is running now.")
        emit_event(project_id, "po.enabled", {"agent": agent["name"], "note": note})
        po_narrate(project_id, f"👑 **{agent['name']} (Product Owner)** has taken control of this "
                               "project. " + note.split('. ', 1)[-1])
        # Immediate autonomous review so enabling always produces visible
        # activity — the PO may unblock items, re-plan, or start a next sprint.
        threading.Thread(
            target=po_autonomy_tick, args=(project_id,),
            kwargs={"instruction": (
                "You have just been given full authority over this project. Do an initial Product "
                "Owner review: if the current sprint cannot start or has no open tasks, plan and "
                "start the next sprint from the backlog; otherwise keep the current plan moving. "
                "Take no action if the requirements are met and no work remains."),
                    "emit_no_model": True},
            daemon=True).start()
        return {"ok": True, "enabled": True, "agent": agent["name"], "sprint_started": started,
                "start_error": start_error}
    emit_event(project_id, "po.disabled",
               {"note": "Product Owner authority disabled — control returns to the owner. "
                        "All open tasks remain as they are."})
    audit("po_disabled", "project", project_id,
          f"Product Owner authority disabled for project '{project['name']}'")
    po_narrate(project_id,
               "Product Owner authority disabled — control is back with you. All existing "
               "tasks remain open exactly as they are; say stop sprint execution if you "
               "also want the run halted.")
    return {"ok": True, "enabled": False}


# ---------------------------------------------------------------- context

def _project_brief(project_id: str, focus_blockers: bool = False) -> str:
    p = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    lines = [f"PROJECT: {p['name']}", f"GOAL: {p['goal'] or '(not set)'}",
             f"DESCRIPTION: {p['description'] or '(not set)'}",
             f"TECH STACK: {p['technology_stack'] or '(not set)'}"]
    backlog = query("SELECT title, priority, story_points, status, acceptance_criteria "
                    "FROM backlog_items WHERE project_id = ? ORDER BY priority, created_at", (project_id,))
    if backlog:
        lines.append("BACKLOG:")
        lines.extend(f"- {b['title']} (P{b['priority']}, {b['story_points']} pts, {b['status']})"
                     + (f" — AC: {b['acceptance_criteria']}" if b["acceptance_criteria"] else "")
                     for b in backlog)
    sprints = query("SELECT * FROM sprints WHERE project_id = ? ORDER BY created_at", (project_id,))
    for s in sprints:
        lines.append(f"SPRINT: {s['name']} [{s['status']}] capacity {s['capacity']} — goal: {s['goal'] or 'n/a'}")
        if s["status"] in sprint_gate.ACTIVE_OR_GATING:
            gates = ", ".join(f"{sprint_gate.GATE_LABELS[c]}={s[c]}"
                              for c in sprint_gate.GATE_COLUMNS)
            lines.append(f"  GATES: {gates}"
                         + (f" — FAILURE: {s['failure_reason'][:120]}" if s["failure_reason"] else ""))
        elif s["status"] == sprint_gate.PLANNED and sprint_gate.active_sprint(project_id):
            lines.append("  (LOCKED — the current sprint must complete before this one can start)")
        tasks = query(
            "SELECT t.*, a.name AS agent_name, qa.name AS qa_name FROM tasks t "
            "LEFT JOIN agents a ON a.id = t.assigned_agent_id "
            "LEFT JOIN agents qa ON qa.id = t.qa_agent_id "
            "WHERE t.sprint_id = ? ORDER BY t.priority, t.created_at", (s["id"],))
        for t in tasks:
            line = (f"  - {t['title']} [{t['status']}] {t['story_points']}pts P{t['priority']} "
                    f"assigned: {t['agent_name'] or 'UNASSIGNED'}")
            if t["qa_name"]:
                line += f" (qa: {t['qa_name']})"
            if t["blocked_reason"]:
                line += f" BLOCKED: {t['blocked_reason']} (rework cycle {t['rework_count']})"
            lines.append(line)
    team = query(
        "SELECT a.name, r.name AS role, a.lifecycle_state FROM team_agents ta "
        "JOIN agents a ON a.id = ta.agent_id JOIN roles r ON r.id = a.role_id "
        "WHERE ta.team_id = ? AND ta.active = 1 ORDER BY a.name", (p["team_id"],))
    lines.append("TEAM: " + ("; ".join(f"{m['name']} ({m['role']}, {m['lifecycle_state']})" for m in team)
                             if team else "(no team aligned)"))
    if focus_blockers:
        blocked = [l for l in lines if "BLOCKED:" in l or "UNASSIGNED" in l]
        if blocked:
            lines.append("ATTENTION — items needing a Product Owner decision right now:")
            lines.extend(blocked)
    return "\n".join(lines)


# ---------------------------------------------------------------- action execution

def _resolve_task(project_id: str, ref: str):
    """Resolve a task by exact or partial title. Open tasks (not Done/
    Cancelled) always win: an action aimed at a blocked task must never
    land on a completed one just because the titles partially match."""
    ref = str(ref or "").lower().strip()
    if not ref:
        return None
    rows = query("SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at", (project_id,))
    open_rows = [t for t in rows if t["status"] not in ("Done", "Cancelled")]
    for pool in (open_rows, rows):
        for t in pool:
            if t["title"].lower() == ref:
                return t
        for t in pool:
            if ref in t["title"].lower():
                return t
    return None


def _norm_title(title: str) -> str:
    return " ".join(str(title or "").lower().split())


_DUPLICATE_STOPWORDS = {"add", "implement", "build", "create", "make", "setup", "set up",
                        "the", "a", "an", "with", "and", "for", "to", "in", "of", "or"}


def _title_tokens(title: str) -> set:
    return {t for t in _norm_title(title).replace("°", "").split()
            if t and t not in _DUPLICATE_STOPWORDS}


def _duplicate_task_exists(project_id: str, title: str) -> bool:
    """True when an equivalent task already exists in any sprint of this
    project — blocks the PO from re-creating completed work. Matches exact
    normalized titles AND near-duplicates (reworded titles like 'Add X' vs
    'Implement X' whose content tokens overlap heavily)."""
    norm = _norm_title(title)
    if not norm:
        return False
    tokens = _title_tokens(title)
    for t in query("SELECT title FROM tasks WHERE project_id = ?", (project_id,)):
        if _norm_title(t["title"]) == norm:
            return True
        other = _title_tokens(t["title"])
        if not tokens or not other:
            continue
        overlap = len(tokens & other) / max(1, min(len(tokens), len(other)))
        if overlap >= 0.75:
            return True
    return False


def _resolve_agent(project_id: str, name: str):
    n = str(name or "").lower().strip()
    team_id = query_one("SELECT team_id FROM projects WHERE id = ?", (project_id,))["team_id"]
    if not team_id:
        return None
    rows = query(
        "SELECT a.*, r.name AS role_name FROM team_agents ta "
        "JOIN agents a ON a.id = ta.agent_id JOIN roles r ON r.id = a.role_id "
        "WHERE ta.team_id = ? AND ta.active = 1 ORDER BY a.name", (team_id,))
    for a in rows:
        if a["name"].lower() == n:
            return a
    for a in rows:
        if n and n in a["name"].lower():
            return a
    return None


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _apply_actions(project_id: str, actions) -> list[str]:
    log: list[str] = []
    for a in actions or []:
        if not isinstance(a, dict):
            continue
        act = str(a.get("action") or "").strip()
        try:
            if act == "update_project":
                vals = {"updated_at": now()}
                for k in ("goal", "description"):
                    if str(a.get(k) or "").strip():
                        vals[k] = str(a[k]).strip()[:2000]
                update("projects", project_id, vals)
                log.append("Updated project requirements (goal/description).")
                audit("po_update_project", "project", project_id, "PO revised project requirements",
                      actor="product-owner")
                emit_event(project_id, "po.action", {"action": "update_project"})
            elif act == "add_backlog":
                ts = now()
                n = 0
                for item in a.get("items") or []:
                    title = str((item or {}).get("title") or "").strip()
                    if not title:
                        continue
                    insert("backlog_items", {
                        "id": new_id(), "project_id": project_id, "title": title[:120],
                        "description": str(item.get("description") or "")[:1000],
                        "priority": _clamp(item.get("priority"), 1, 3, 2),
                        "acceptance_criteria": str(item.get("acceptance_criteria") or "")[:1000],
                        "story_points": _clamp(item.get("points"), 1, 8, 3),
                        "status": "Backlog", "created_at": ts,
                    })
                    n += 1
                if n:
                    log.append(f"Added {n} backlog item(s).")
                    emit_event(project_id, "po.action", {"action": "add_backlog", "count": n})
            elif act == "create_sprint":
                name = str(a.get("name") or "").strip()
                if not name:
                    continue
                if query_one("SELECT id FROM sprints WHERE project_id=? AND lower(name)=lower(?)",
                             (project_id, name)):
                    log.append(f"Sprint '{name}' already exists — skipped.")
                    continue
                sid = new_id()
                insert("sprints", {"id": sid, "project_id": project_id, "name": name[:120],
                                   "goal": str(a.get("goal") or "")[:1000],
                                   "capacity": _clamp(a.get("capacity"), 1, 200, 40),
                                   "status": "Planned", "created_at": now()})
                ts = now()
                made = skipped = 0
                for t in a.get("tasks") or []:
                    title = str((t or {}).get("title") or "").strip()
                    if not title:
                        continue
                    if _duplicate_task_exists(project_id, title):
                        skipped += 1
                        continue
                    insert("tasks", {"id": new_id(), "project_id": project_id, "sprint_id": sid,
                                     "title": title[:120],
                                     "description": str(t.get("description") or "")[:1000],
                                     "acceptance_criteria": str(t.get("acceptance_criteria") or "")[:1000],
                                     "story_points": _clamp(t.get("points"), 1, 8, 3),
                                     "priority": _clamp(t.get("priority"), 1, 3, 2),
                                     "status": "Todo", "created_at": ts, "updated_at": ts})
                    made += 1
                log.append(f"Created sprint '{name}' with {made} task(s)"
                           + (f" ({skipped} duplicate(s) of existing work skipped)" if skipped else "") + ".")
                audit("po_create_sprint", "sprint", sid, f"PO created sprint '{name}' ({made} tasks)",
                      actor="product-owner")
                emit_event(project_id, "po.action", {"action": "create_sprint", "sprint": name, "tasks": made})
            elif act == "add_task":
                title = str(a.get("title") or "").strip()
                if not title:
                    continue
                if _duplicate_task_exists(project_id, title):
                    log.append(f"Skipped duplicate task '{title}' — it already exists "
                               "(re-create completed work is not allowed).")
                    continue
                sprint = None
                ref = str(a.get("sprint") or "").strip()
                if ref:
                    sprints = query("SELECT * FROM sprints WHERE project_id = ? ORDER BY created_at", (project_id,))
                    for s in sprints:
                        if s["name"].lower() == ref.lower() or ref.lower() in s["name"].lower():
                            sprint = s
                            break
                if not sprint:
                    sprint = sprint_gate.active_sprint(project_id)
                if not sprint:
                    log.append(f"Could not add task '{title}': no sprint to put it in.")
                    continue
                ts = now()
                insert("tasks", {"id": new_id(), "project_id": project_id, "sprint_id": sprint["id"],
                                 "title": title[:120], "description": str(a.get("description") or "")[:1000],
                                 "story_points": _clamp(a.get("points"), 1, 8, 3),
                                 "priority": _clamp(a.get("priority"), 1, 3, 2),
                                 "status": "Todo", "created_at": ts, "updated_at": ts})
                log.append(f"Added task '{title}' to sprint '{sprint['name']}'.")
                emit_event(project_id, "po.action", {"action": "add_task", "task": title})
                # Sprint gating: adding work to the ACTIVE sprint mid-gates
                # re-opens development (urgent scope); gates re-run after the
                # new work drains. Future sprints stay locked.
                if sprint["status"] in sprint_gate.GATE_PHASES + (sprint_gate.REWORK,):
                    rok, rerr = sprint_gate.reopen_sprint_for_scope(
                        sprint["id"], f"urgent task '{title[:60]}' added by Product Owner")
                    if rok:
                        log.append(f"Sprint '{sprint['name']}' re-opened for the urgent task — "
                                   "validation gates will re-run after it completes.")
                        emit_event(project_id, "project.updated",
                                   {"note": f"Urgent task '{title[:60]}' re-opened sprint "
                                            f"'{sprint['name']}' mid-validation"})
            elif act == "update_task":
                t = _resolve_task(project_id, a.get("task"))
                if not t:
                    log.append(f"Task '{a.get('task')}' not found — update skipped.")
                    continue
                vals = {"updated_at": now()}
                if str(a.get("title") or "").strip():
                    vals["title"] = str(a["title"]).strip()[:120]
                if str(a.get("description") or "").strip():
                    vals["description"] = str(a["description"]).strip()[:1000]
                if a.get("priority") is not None:
                    vals["priority"] = _clamp(a.get("priority"), 1, 3, t["priority"])
                if a.get("points") is not None:
                    vals["story_points"] = _clamp(a.get("points"), 1, 8, t["story_points"])
                update("tasks", t["id"], vals)
                log.append(f"Updated task '{t['title']}'.")
                emit_event(project_id, "po.action", {"action": "update_task", "task": t["title"]})
            elif act == "assign_task":
                t = _resolve_task(project_id, a.get("task"))
                agent = _resolve_agent(project_id, a.get("agent"))
                if not t:
                    log.append(f"Task '{a.get('task')}' not found — assign skipped.")
                    continue
                # Role-family enforcement: a dev task is never handed to QA/BA/
                # DevOps/PO even if the PO named them. A wrong-role pick falls
                # back to the runtime's role-based specialist selection.
                family = runtime._family_for_task(t)
                if agent and family and runtime._family_of_role(agent["role_name"]) != family:
                    log.append(f"Override: {agent['name']} ({agent['role_name']}) is not "
                               f"{_FAMILY_WORD.get(family, family)} staff — picking a specialist instead.")
                    agent = None
                if not agent:
                    members = [m for m in runtime._team_members(project_id)
                               if m["lifecycle_state"] == "Idle"]
                    pick, matched = runtime._pick_member(members, family)
                    agent = pick
                if not agent:
                    log.append(f"No idle eligible agent for '{t['title']}' — assign skipped.")
                    continue
                update("tasks", t["id"], {"assigned_agent_id": agent["id"], "updated_at": now()})
                log.append(f"Assigned '{t['title']}' to {agent['name']} ({agent['role_name']}).")
                audit("po_assign_task", "task", t["id"],
                      f"PO assigned '{t['title']}' to {agent['name']} ({agent['role_name']})",
                      actor="product-owner")
                emit_event(project_id, "po.action",
                           {"action": "assign_task", "task": t["title"], "agent": agent["name"],
                            "role": agent["role_name"]})
            elif act == "install_library":
                pkgs = a.get("packages") or ([a.get("package")] if a.get("package") else [])
                reason = str(a.get("reason") or "").strip()
                ws = workspace.get_workspace(project_id)
                if ws and os.path.isdir(ws):
                    # Primary: the PROJECT-LOCAL environment (.venv) that the
                    # generated start_server.bat activates and QA reuses.
                    result = toolchains.ensure_project_env(ws, extra_packages=pkgs)
                    where = "project-local .venv"
                    if not result.get("ok"):
                        # Fallback: install into <workspace>/lib/ and LINK it
                        # (.pth / PYTHONPATH / NODE_PATH / GOPATH / nuget).
                        stack = toolchains.detect_stack(None, ws)
                        result = toolchains.lib_install(ws, pkgs, stack=stack)
                        where = f"project lib/ folder ({stack}, linked)"
                else:
                    result = toolchains.pip_install(pkgs)
                    where = "AgentForge environment"
                if result.get("ok"):
                    log.append(f"Installed into {where}: "
                               + (", ".join(result.get("packages") or pkgs) or "requirements.txt")
                               + (f" ({result['output'].splitlines()[-1][:120]})" if result.get("output") else ""))
                    audit("po_install_library", "project", project_id,
                          f"PO installed libraries into {where}: "
                          + (", ".join(result.get("packages") or ["requirements.txt"]))
                          + (f" — {reason}" if reason else ""), actor="product-owner")
                    emit_event(project_id, "po.action",
                               {"action": "install_library", "where": where,
                                "packages": result.get("packages") or pkgs})
                else:
                    log.append(f"Library install into {where} FAILED: "
                               f"{str(result.get('output'))[-200:]}")
                    emit_event(project_id, "po.action",
                               {"action": "install_library", "ok": False,
                                "packages": result.get("packages") or pkgs})
            elif act == "unblock_task":
                t = _resolve_task(project_id, a.get("task"))
                if not t:
                    log.append(f"Task '{a.get('task')}' not found — unblock skipped.")
                    continue
                update("tasks", t["id"], {"status": "Ready", "blocked_reason": "",
                                          "rework_count": 0, "updated_at": now()})
                note = str(a.get("note") or "").strip()
                log.append(f"Unblocked '{t['title']}'" + (f" — {note}" if note else "") + ".")
                audit("po_unblock_task", "task", t["id"],
                      f"PO unblocked task '{t['title']}': {note}", actor="product-owner")
                emit_event(project_id, "po.action",
                           {"action": "unblock_task", "task": t["title"], "note": note})
            elif act == "cancel_task":
                t = _resolve_task(project_id, a.get("task"))
                if not t:
                    continue
                update("tasks", t["id"], {"status": "Cancelled",
                                          "blocked_reason": str(a.get("reason") or "cancelled by Product Owner"),
                                          "updated_at": now()})
                log.append(f"Cancelled task '{t['title']}'.")
                emit_event(project_id, "po.action", {"action": "cancel_task", "task": t["title"]})
            elif act == "start_sprint":
                result = runtime.start_sprint_execution(project_id, a.get("sprint"))
                if "error" in result:
                    log.append(f"Could not start sprint: {result['error']}")
                else:
                    log.append(f"Started sprint '{result['sprint']}' — tasks auto-assigned by role.")
            elif act == "stop_sprint":
                runtime.stop_sprint_execution(project_id)
                log.append("Stopped sprint execution; open tasks remain.")
            else:
                log.append(f"Unknown action '{act}' ignored.")
        except Exception as exc:  # one bad action must not kill the whole review
            logger.exception("PO action '%s' failed for project %s", act, project_id)
            log.append(f"Action '{act}' failed: {exc}")
    return log


def _ensure_delivery_running(project_id: str, stop_requested: bool = False) -> str | None:
    """After PO actions add or change work, make sure the delivery loop is
    actually running. The loop may have exited earlier (the project looked
    complete, or it gave up while idle), which would leave newly added tasks
    sitting unexecuted until the owner toggled the PO off/on."""
    if stop_requested or not po_enabled(project_id):
        return None
    if project_id in runtime._schedulers:
        return None
    open_work = query_one(
        "SELECT COUNT(*) AS n FROM tasks WHERE project_id = ? AND sprint_id IS NOT NULL "
        "AND status NOT IN ('Done','Cancelled')", (project_id,))["n"]
    if not open_work:
        return None
    result = runtime.start_sprint_execution(project_id)
    if "error" in result:
        return f"Could not resume sprint execution: {result['error']}"
    return f"Resumed sprint execution on '{result['sprint']}' — the new work will run now."


def _stop_requested(actions) -> bool:
    return any(isinstance(a, dict) and str(a.get("action") or "").strip() == "stop_sprint"
               for a in (actions or []))


# ---------------------------------------------------------------- LLM plumbing

def _po_llm(project_id: str):
    """Brain for the Product Owner. Priority: the explicit PO model config
    from Settings → Product Owner AI, then the project's default gateway
    with a plain chat/coding model (never the control bot's tool-calling
    routing model, whose tool-call XML breaks JSON action parsing), then
    the control-bot model as a last resort."""
    from . import codegen
    cfg_raw = query_one("SELECT value FROM settings WHERE key = 'po_bot'")
    if cfg_raw and cfg_raw["value"]:
        try:
            cfg = json.loads(cfg_raw["value"])
            gw_id = cfg.get("gateway_id")
            if gw_id:
                gw = query_one("SELECT * FROM gateways WHERE id = ?", (gw_id,))
                model = (query_one("SELECT * FROM gateway_models WHERE id = ?", (cfg["model_id"],))
                         if cfg.get("model_id") else None)
                if gw and not model:
                    # The saved model id went stale (the gateway re-synced its
                    # catalog and minted new ids). Recover by picking a usable
                    # chat model on the same gateway the owner chose, so the PO
                    # keeps working instead of silently resolving to nothing.
                    model = query_one(
                        "SELECT * FROM gateway_models WHERE gateway_id = ? AND active = 1 "
                        "AND lower(provider_model_id) NOT LIKE '%embed%' "
                        "ORDER BY display_name LIMIT 1", (gw_id,))
                if gw and model:
                    return gw, model
        except ValueError:
            logger.warning("Failed to parse po_bot settings")
    project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    gw, model = (None, None)
    if project:
        gw, model = codegen.resolve_llm(project)
    if not (gw and model):
        from .chatbot import _bot_config
        gw, model = _bot_config()
    return gw, model


def _ask_po(project_id: str, user_text: str) -> dict | None:
    """Call the PO brain; returns parsed {reply, actions}. Raises RuntimeError
    with the model's actual output head when no valid plan could be parsed,
    so quota/network problems surface honestly."""
    from .chatbot import _extract_json
    from . import codegen
    gw, model = _po_llm(project_id)
    if not (gw and model):
        return None
    raw = codegen.call_llm(gw, model, PO_SYSTEM_PROMPT + _project_brief(project_id, focus_blockers=True),
                           user_text, max_tokens=_MAX_ACTION_TOKENS)
    text = raw.get("text") or ""
    parsed = _extract_json(text)
    if parsed is None:
        raise RuntimeError(f"model returned no valid plan: {text[:160]}")
    return parsed


# ---------------------------------------------------------------- PO chat

def _po_conversation(project_id: str):
    conv = query_one(
        "SELECT * FROM conversations WHERE project_id = ? AND title = ? ORDER BY created_at LIMIT 1",
        (project_id, PO_CONV_TITLE))
    if conv:
        return conv
    cid = new_id()
    ts = now()
    insert("conversations", {"id": cid, "project_id": project_id, "title": PO_CONV_TITLE,
                             "created_at": ts, "updated_at": ts})
    insert("messages", {
        "id": new_id(), "conversation_id": cid, "role": "assistant",
        "content": (f"Hi, I'm the Product Owner for this project. Share requirements here — plain "
                    f"text or attached files (`.md`, `.txt`, `.json`, source). Say `enable product owner` "
                    f"in the control chat (or flip the toggle) to give me full authority to run the "
                    f"project autonomously."),
        "created_at": ts,
    })
    return query_one("SELECT * FROM conversations WHERE id = ?", (cid,))


def _save_attachments(project_id: str, attachments) -> list[dict]:
    """Persist attachment texts into the workspace and return prompt-ready info."""
    saved = []
    ws_dir = workspace.get_workspace(project_id)
    for att in (attachments or [])[:5]:
        if not isinstance(att, dict):
            continue
        name = os.path.basename(str(att.get("name") or "attachment.txt"))[:120] or "attachment.txt"
        content = str(att.get("content") or "")
        if ws_dir:
            req_dir = os.path.join(ws_dir, "docs", "requirements")
            try:
                os.makedirs(req_dir, exist_ok=True)
                base, ext = os.path.splitext(name)
                target = os.path.join(req_dir, name)
                i = 1
                while os.path.exists(target):
                    target = os.path.join(req_dir, f"{base}-{i}{ext}")
                    i += 1
                with open(target, "w", encoding="utf-8") as fh:
                    fh.write(content)
                saved.append({"name": os.path.basename(target),
                              "path": os.path.relpath(target, ws_dir).replace("\\", "/"),
                              "content": content[:_MAX_ATTACHMENT_CHARS]})
                continue
            except OSError:
                logger.warning("Failed to save attachment %s for project %s", name, project_id)
        saved.append({"name": name, "path": None, "content": content[:_MAX_ATTACHMENT_CHARS]})
    return saved


def handle_po_message(project_id: str, text: str, attachments=None) -> dict:
    conv = _po_conversation(project_id)
    saved = _save_attachments(project_id, attachments)
    att_note = ""
    if saved:
        att_note = "\n\nAttached file(s): " + ", ".join(
            f"`{s['name']}`" + (f" (saved to `{s['path']}`)" if s["path"] else "") for s in saved)
    insert("messages", {"id": new_id(), "conversation_id": conv["id"], "role": "user",
                        "content": text + (f"\n\n{att_note}" if att_note else ""),
                        "created_at": now()})
    update("conversations", conv["id"], {"updated_at": now()})

    prompt_parts = [text or "(no text)"]
    for s in saved:
        prompt_parts.append(f"--- FILE: {s['name']} ---\n{s['content']}")
    actions, reply = [], ""
    parsed = None
    try:
        parsed = _ask_po(project_id, "\n\n".join(prompt_parts))
    except Exception as exc:
        logger.exception("PO message handling failed for project %s", project_id)
        reply = (f"⚠️ I could not act right now: {exc}. Your message and any files are saved; "
                 "try again once the gateway is reachable.")
    if parsed:
        reply = str(parsed.get("reply") or "").strip() or "Noted."
        actions = _apply_actions(project_id, parsed.get("actions"))
        resumed = _ensure_delivery_running(project_id, _stop_requested(parsed.get("actions")))
        if resumed:
            actions.append(resumed)
    elif not reply:
        reply = ("My decision model is not configured (or returned no usable plan). Set the "
                 "project gateway key under Settings and resend — your message and any files are "
                 "stored in the project either way.")
    full_reply = reply + ("\n\n**Actions taken:**\n- " + "\n- ".join(actions) if actions else "")
    insert("messages", {"id": new_id(), "conversation_id": conv["id"], "role": "assistant",
                        "content": full_reply, "created_at": now()})
    messages = query("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at, rowid", (conv["id"],))
    return {"reply": full_reply, "actions": actions, "messages": messages}


# ---------------------------------------------------------------- autonomous review tick

def po_autonomy_tick(project_id: str, instruction: str | None = None,
                     emit_no_model: bool = False) -> str | None:
    """One autonomous Product Owner review pass. Called from the sprint loop
    and right after enabling: the PO reviews current state and acts. Returns
    a short summary of what changed (None when the PO could not run)."""
    if not po_enabled(project_id):
        return None
    ask = instruction or (
        "Do a Product Owner review pass: examine requirements, sprints and tasks "
        "(especially blocked or unassigned items) and take any actions needed to "
        "keep the project moving to completion.")
    try:
        parsed = _ask_po(project_id, ask)
    except Exception as exc:
        logger.warning("PO autonomy tick failed for project %s: %s", project_id, exc)
        emit_event(project_id, "po.review", {"ok": False, "error": str(exc)[:300]})
        return None
    if not parsed:
        if emit_no_model:
            emit_event(project_id, "po.review",
                       {"ok": False, "error": "PO model returned no valid plan "
                                              "(check gateway key / usage quota under Settings)"})
        return None
    actions = _apply_actions(project_id, parsed.get("actions"))
    resumed = _ensure_delivery_running(project_id, _stop_requested(parsed.get("actions")))
    if resumed:
        actions.append(resumed)
    summary = str(parsed.get("reply") or "").strip()
    emit_event(project_id, "po.review",
               {"ok": True, "summary": summary[:400],
                "actions": actions[:10] if actions else [],
                "acted": bool(actions)})
    if actions:
        agent = po_agent_for_project(project_id)
        _narrate_actions(project_id, agent["name"] if agent else None, summary, actions)
    return summary
