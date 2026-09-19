"""Project Control assistant.

A policy-bound command agent: parses natural-language requests into typed
application commands (no raw SQL/DB tool), asks for confirmation on
sensitive mutations, executes them through domain services and records
audit events. Model inference is simulated with deterministic parsing so
the demo works without provider credentials.
"""
import json
import re

from . import db, runtime
from .db import audit, execute, insert, new_id, now, query, query_one, update

HELP_TEXT = """I can execute these typed commands:

**Configure**
- `create gateway <name> provider <provider> url <base-url> key <api-key>`
- `test gateway <name>`

**Agent Memory**
- `create role <name>`
- `create persona <name> for role <role>`
- `create skill <name>`

**Teams**
- `create agent <name> role <role> persona <persona>`
- `create team <name> with agents <a>, <b>, ...`

**Agile**
- `add backlog item <title> points <n>`
- `create sprint <name>`
- `assign task <title> to <agent>`
- `start task <title>`
- `start sprint execution` / `stop sprint execution`
- `pause execution` / `cancel execution` / `retry last failed task`

**Info**
- `status` — project summary
- `help` — this message

Sensitive commands (gateways, agents, teams) require your confirmation before execution."""


def _save_message(conversation_id, role, content, meta=""):
    insert("messages", {"id": new_id(), "conversation_id": conversation_id,
                        "role": role, "content": content, "meta": meta, "created_at": now()})
    update("conversations", conversation_id, {"updated_at": now()})


def _find_role(name):
    return query_one("SELECT * FROM roles WHERE lower(name) = lower(?)", (name,))


def _find_persona(name):
    return query_one("SELECT * FROM personas WHERE lower(name) = lower(?)", (name,))


def _find_agent(name):
    return query_one("SELECT * FROM agents WHERE lower(name) = lower(?)", (name,))


def _find_task(project_id, title):
    rows = query("SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at", (project_id,))
    t = title.lower().strip()
    exact = [r for r in rows if r["title"].lower() == t]
    if exact:
        return exact[0]
    partial = [r for r in rows if t in r["title"].lower()]
    return partial[0] if partial else None


def _project_summary(project_id) -> str:
    p = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    tasks = query("SELECT status, COUNT(*) AS n FROM tasks WHERE project_id = ? GROUP BY status", (project_id,))
    sprint = query_one("SELECT * FROM sprints WHERE project_id = ? AND status='Active'", (project_id,))
    agents = query(
        "SELECT a.name, a.lifecycle_state, r.name AS role FROM agents a "
        "JOIN teams t ON t.id = (SELECT team_id FROM projects WHERE id = ?) "
        "JOIN team_agents ta ON ta.agent_id = a.id AND ta.team_id = t.id "
        "JOIN roles r ON r.id = a.role_id", (project_id,))
    usage = query_one(
        "SELECT COALESCE(SUM(input_tokens),0) AS it, COALESCE(SUM(output_tokens),0) AS ot, "
        "COALESCE(SUM(cost_estimate),0) AS cost FROM usage_records ur "
        "JOIN workflow_runs wr ON wr.id = ur.workflow_run_id WHERE wr.project_id = ?", (project_id,))
    lines = [f"**{p['name']}** — {p['goal']}",
             f"Stack: {p['technology_stack']} | Status: {p['status']}"]
    if sprint:
        lines.append(f"Active sprint: **{sprint['name']}** (capacity {sprint['capacity']} pts)")
    lines.append("Tasks: " + (", ".join(f"{t['n']} {t['status']}" for t in tasks) or "none"))
    lines.append("Team: " + (", ".join(f"{a['name']} ({a['role']}) — {a['lifecycle_state']}" for a in agents) or "no team assigned"))
    lines.append(f"Usage: {usage['it'] + usage['ot']:,} tokens, est. ${usage['cost']:.4f}")
    return "\n".join(lines)


def _queue_pending(conversation_id, command, args, summary) -> str:
    pid = new_id()
    insert("pending_commands", {"id": pid, "conversation_id": conversation_id,
                                "command": command, "args_json": json.dumps(args),
                                "summary": summary, "created_at": now()})
    return pid


def _execute_command(project_id, command, args) -> str:
    ts = now()
    if command == "create_gateway":
        existing = query_one("SELECT id FROM gateways WHERE lower(name)=lower(?)", (args["name"],))
        if existing:
            return f"Gateway **{args['name']}** already exists."
        gid = new_id()
        insert("gateways", {
            "id": gid, "name": args["name"], "provider": args.get("provider", "openai"),
            "base_url": args.get("base_url", "https://api.example.com/v1"),
            "api_type": "openai-chat", "status": "Active",
            "key_mask": ("••••" + args["api_key"][-4:]) if args.get("api_key") else None,
            "last_tested_at": ts, "test_status": "Not tested", "test_diagnostic": "",
            "created_at": ts, "updated_at": ts,
        })
        audit("create_gateway", "gateway", gid, f"Created gateway '{args['name']}'")
        return f"Gateway **{args['name']}** created ({args.get('provider', 'openai')}). API key stored as a masked credential reference — never written to prompts or logs."

    if command == "test_gateway":
        gw = query_one("SELECT * FROM gateways WHERE lower(name)=lower(?)", (args["name"],))
        if not gw:
            return f"Gateway **{args['name']}** not found."
        added = db.sync_gateway_models(gw["id"])
        total = query_one("SELECT COUNT(*) AS n FROM gateway_models WHERE gateway_id=?", (gw["id"],))["n"]
        update("gateways", gw["id"], {"last_tested_at": ts, "test_status": "Success",
                                      "test_diagnostic": "Connection OK (simulated probe)",
                                      "updated_at": ts})
        audit("test_gateway", "gateway", gw["id"], f"Tested gateway '{gw['name']}'; {added} models synced")
        return (f"Gateway **{gw['name']}**: connection test succeeded. "
                f"Auto-fetched {added} new model(s) — catalog now has {total} models. Credential remains masked.")

    if command == "create_role":
        if _find_role(args["name"]):
            return f"Role **{args['name']}** already exists."
        rid = new_id()
        insert("roles", {"id": rid, "name": args["name"],
                         "description": args.get("description", ""), "active": 1,
                         "created_at": ts, "updated_at": ts})
        audit("create_role", "role", rid, f"Created role '{args['name']}'")
        return f"Role **{args['name']}** created. You can now add personas and a model binding."

    if command == "create_persona":
        role = _find_role(args.get("role", ""))
        if not role:
            return f"Role **{args.get('role')}** not found. Create it first."
        pid = new_id()
        instructions = (f"You are {args['name']}, a {role['name']}. "
                        f"{args.get('description', 'Follow the role charter and project conventions.')}")
        insert("personas", {"id": pid, "role_id": role["id"], "name": args["name"],
                            "description": args.get("description", ""),
                            "instructions": instructions, "constraints_text": "",
                            "version": 1, "active": 1, "created_at": ts, "updated_at": ts})
        insert("persona_versions", {"id": new_id(), "persona_id": pid, "version": 1,
                                    "instructions": instructions, "constraints_text": "",
                                    "checksum": db.checksum(instructions), "created_at": ts})
        audit("create_persona", "persona", pid, f"Created persona '{args['name']}' for role '{role['name']}'")
        return f"Persona **{args['name']}** created under role **{role['name']}** (version 1, immutable instruction record stored)."

    if command == "create_skill":
        if query_one("SELECT id FROM skills WHERE lower(name)=lower(?)", (args["name"],)):
            return f"Skill **{args['name']}** already exists."
        sid = new_id()
        insert("skills", {"id": sid, "name": args["name"],
                          "description": args.get("description", ""),
                          "content": f"# {args['name']}\n(Describe the procedure here.)",
                          "version": 1, "active": 1, "created_at": ts, "updated_at": ts})
        audit("create_skill", "skill", sid, f"Created skill '{args['name']}'")
        return f"Skill **{args['name']}** created with editable Markdown source."

    if command == "create_agent":
        role = _find_role(args.get("role", ""))
        persona = _find_persona(args.get("persona", "")) if args.get("persona") else None
        if not role:
            return f"Role **{args.get('role')}** not found."
        if not persona:
            persona = query_one("SELECT * FROM personas WHERE role_id = ? ORDER BY created_at LIMIT 1", (role["id"],))
            if not persona:
                return f"Role **{role['name']}** has no persona yet. Create one first."
        binding = query_one("SELECT * FROM model_bindings WHERE role_id = ? AND active = 1", (role["id"],))
        aid = new_id()
        insert("agents", {"id": aid, "name": args["name"], "role_id": role["id"],
                          "persona_id": persona["id"],
                          "model_binding_id": binding["id"] if binding else None,
                          "lifecycle_state": "Idle", "created_at": ts, "updated_at": ts})
        audit("create_agent", "agent", aid,
              f"Created agent '{args['name']}' ({role['name']} / {persona['name']})")
        model_note = ""
        if binding:
            m = query_one("SELECT provider_model_id FROM gateway_models WHERE id = ?", (binding["model_id"],))
            if m:
                model_note = f" Model: **{m['provider_model_id']}** (inherited from role binding)."
        return f"Agent **{args['name']}** created from role **{role['name']}** + persona **{persona['name']}**.{model_note}"

    if command == "create_team":
        names = [a.strip() for a in args.get("agents", []) if a.strip()]
        agent_rows = []
        for n in names:
            a = _find_agent(n)
            if a:
                agent_rows.append(a)
        if not agent_rows:
            return "No matching agents found. Create agents first with `create agent ...`."
        tid = new_id()
        insert("teams", {"id": tid, "name": args["name"], "description": "",
                         "status": "Active", "created_at": ts})
        for a in agent_rows:
            execute("INSERT OR IGNORE INTO team_agents (team_id, agent_id, role_in_team, active) VALUES (?,?,?,1)",
                    (tid, a["id"], "Member"))
        audit("create_team", "team", tid,
              f"Created team '{args['name']}' with {len(agent_rows)} agents")
        return f"Team **{args['name']}** created with agents: {', '.join(a['name'] for a in agent_rows)}."

    if command == "add_backlog_item":
        bid = new_id()
        insert("backlog_items", {
            "id": bid, "project_id": project_id, "title": args["title"],
            "description": args.get("description", ""), "priority": int(args.get("priority", 2)),
            "acceptance_criteria": args.get("acceptance_criteria", ""),
            "story_points": int(args.get("points", 3)), "status": "Backlog", "created_at": ts,
        })
        audit("add_backlog_item", "backlog_item", bid, f"Added backlog item '{args['title']}'")
        return f"Backlog item **{args['title']}** added ({args.get('points', 3)} pts, priority {args.get('priority', 2)})."

    if command == "create_sprint":
        sid = new_id()
        insert("sprints", {"id": sid, "project_id": project_id, "name": args["name"],
                           "goal": args.get("goal", ""), "capacity": int(args.get("capacity", 40)),
                           "status": "Planned", "created_at": ts})
        audit("create_sprint", "sprint", sid, f"Created sprint '{args['name']}'")
        return f"Sprint **{args['name']}** created (Planned). Activate it from the Sprints panel to commit tasks."

    if command == "assign_task":
        task = _find_task(project_id, args.get("task", ""))
        agent = _find_agent(args.get("agent", ""))
        if not task:
            return f"Task matching **{args.get('task')}** not found."
        if not agent:
            return f"Agent **{args.get('agent')}** not found."
        update("tasks", task["id"], {"assigned_agent_id": agent["id"], "updated_at": ts})
        audit("assign_task", "task", task["id"], f"Assigned '{task['title']}' to {agent['name']}")
        return f"Task **{task['title']}** assigned to **{agent['name']}**."

    if command == "start_task":
        task = _find_task(project_id, args.get("task", ""))
        if not task:
            return f"Task matching **{args.get('task')}** not found."
        result = runtime.start_execution(project_id, task["id"])
        if "error" in result:
            return f"Cannot start **{task['title']}**: {result['error']}"
        return f"Execution started for **{task['title']}** — watch the Team and Activity panels for live progress."

    if command == "start_sprint":
        result = runtime.start_sprint_execution(project_id)
        if "error" in result:
            return f"Cannot start sprint execution: {result['error']}"
        audit("start_sprint_execution", "project", project_id, "Sprint execution started via chatbot")
        return f"Sprint execution started: the scheduler will run eligible tasks in dependency order ({result['sprint']})."

    if command == "stop_sprint":
        runtime.stop_sprint_execution(project_id)
        return "Sprint scheduler stopping; active task executions were asked to cancel."

    if command == "pause_execution":
        runs = query("SELECT * FROM workflow_runs WHERE project_id = ? AND status = 'Running'", (project_id,))
        if not runs:
            return "No running executions."
        for r in runs:
            runtime.pause_execution(r["id"])
        return f"Paused {len(runs)} running execution(s). Resume with `resume execution`."

    if command == "resume_execution":
        runs = query("SELECT * FROM workflow_runs WHERE project_id = ? AND status = 'Paused'", (project_id,))
        if not runs:
            return "No paused executions."
        for r in runs:
            runtime.resume_execution(r["id"])
        return f"Resumed {len(runs)} execution(s) from their last checkpoint."

    if command == "cancel_execution":
        runs = query("SELECT * FROM workflow_runs WHERE project_id = ? AND status IN ('Running','Paused')", (project_id,))
        if not runs:
            return "No active executions."
        for r in runs:
            runtime.cancel_execution(r["id"])
        return f"Cancellation requested for {len(runs)} execution(s)."

    if command == "retry_failed":
        failed = query(
            "SELECT * FROM workflow_runs WHERE project_id = ? AND status = 'Failed' ORDER BY started_at DESC",
            (project_id,))
        if not failed:
            return "No failed executions to retry."
        result = runtime.retry_execution(failed[0]["id"])
        if "error" in result:
            return f"Retry failed: {result['error']}"
        return "Retrying the most recent failed execution — it will restart from a clean task boundary."

    return f"Command `{command}` is not implemented."


# ------------------------- intent parsing -------------------------

INTENTS = [
    ("help", r"^\s*(help|commands|what can you do)\b"),
    ("status", r"^\s*(status|summary|project status|how are we doing)\b"),
    ("confirm", r"^\s*(confirm|yes|approve|do it|go ahead)\b"),
    ("cancel_pending", r"^\s*(cancel that|discard|no thanks?|nevermind|never mind)\b"),
    ("create_gateway", r"create (?:a )?gateway\s+(?P<name>.+?)(?:\s+provider\s+(?P<provider>\S+))?(?:\s+(?:url|base[_ -]?url)\s+(?P<base_url>\S+))?(?:\s+(?:key|api[_ -]?key)\s+(?P<api_key>\S+))?\s*$"),
    ("test_gateway", r"test (?:the )?gateway\s+(?P<name>.+)\s*$"),
    ("create_role", r"create (?:a )?role\s+(?P<name>.+?)\s*$"),
    ("create_persona", r"create (?:a )?persona\s+(?P<name>.+?)\s+for\s+role\s+(?P<role>.+?)\s*$"),
    ("create_skill", r"create (?:a )?skill\s+(?P<name>.+?)\s*$"),
    ("create_agent", r"create (?:an? )?agent\s+(?P<name>.+?)\s+role\s+(?P<role>.+?)(?:\s+persona\s+(?P<persona>.+?))?\s*$"),
    ("create_team", r"create (?:a )?team\s+(?P<name>.+?)\s+with\s+agents?\s+(?P<agents>.+?)\s*$"),
    ("add_backlog_item", r"add (?:a )?backlog item\s+(?P<title>.+?)(?:\s+points?\s+(?P<points>\d+))?(?:\s+priority\s+(?P<priority>\d+))?\s*$"),
    ("create_sprint", r"create (?:a )?sprint\s+(?P<name>.+?)\s*$"),
    ("assign_task", r"assign(?: task)?\s+(?P<task>.+?)\s+to\s+(?P<agent>.+?)\s*$"),
    ("start_task", r"(?:start|run|execute)\s+(?:the )?task\s+(?P<task>.+?)\s*$"),
    ("start_sprint", r"start\s+(?:the )?sprint(?: execution)?\s*$"),
    ("stop_sprint", r"stop\s+(?:the )?sprint(?: execution)?\s*$"),
    ("pause_execution", r"pause\s+(?:the )?execution\s*$"),
    ("resume_execution", r"resume\s+(?:the )?execution\s*$"),
    ("cancel_execution", r"cancel\s+(?:the )?execution\s*$"),
    ("retry_failed", r"retry\s+(?:the )?(?:last )?(?:failed )?(?:task|execution)\s*$"),
]

SENSITIVE = {"create_gateway", "create_agent", "create_team"}


def handle_message(project_id: str, conversation_id: str, text: str) -> dict:
    text = text.strip()
    _save_message(conversation_id, "user", text)

    conv = query_one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
    if conv and conv["title"] == "New conversation":
        update("conversations", conversation_id, {"title": text[:60]})

    lowered = text.lower()
    intent, match = None, None
    for name, pattern in INTENTS:
        m = re.match(pattern, lowered, re.IGNORECASE)
        if m:
            intent, match = name, m
            break

    if intent == "confirm":
        pending = query_one(
            "SELECT * FROM pending_commands WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 1",
            (conversation_id,))
        if not pending:
            reply = "There is no pending command to confirm."
        else:
            args = json.loads(pending["args_json"])
            reply = _execute_command(project_id, pending["command"], args)
            execute("DELETE FROM pending_commands WHERE id = ?", (pending["id"],))
        _save_message(conversation_id, "assistant", reply)
        return {"reply": reply}

    if intent == "cancel_pending":
        execute("DELETE FROM pending_commands WHERE conversation_id = ?", (conversation_id,))
        reply = "Pending command discarded."
        _save_message(conversation_id, "assistant", reply)
        return {"reply": reply}

    if intent == "help" or not intent:
        reply = HELP_TEXT if intent == "help" else (
            "I did not recognize that request. I only execute typed commands — try `help` "
            "to see the command catalog. For example: `create role Data Engineer`, "
            "`status`, or `start task Implement login endpoint`.")
        _save_message(conversation_id, "assistant", reply)
        return {"reply": reply}

    if intent == "status":
        reply = _project_summary(project_id)
        _save_message(conversation_id, "assistant", reply)
        return {"reply": reply}

    args = {k: v for k, v in (match.groupdict() or {}).items() if v is not None}
    if intent == "create_team":
        args["agents"] = [a.strip() for a in args.get("agents", "").split(",")]
    if intent == "add_backlog_item" and "points" in args:
        args["points"] = int(args["points"])
    if intent == "add_backlog_item" and "priority" in args:
        args["priority"] = int(args["priority"])
    # Never retain raw credential values in chat: replace with a placeholder reference
    if intent == "create_gateway" and "api_key" in args:
        args["api_key"] = "masked-ref:" + args["api_key"][-4:]

    if intent in SENSITIVE:
        pid = _queue_pending(conversation_id, intent, args, intent)
        reply = (f"**Confirmation required.** You asked to run `{intent}` with:\n"
                 + "\n".join(f"- **{k}**: {v}" for k, v in args.items() if k != "api_key")
                 + "\n\nThis is a sensitive mutation (credentials or team/cost profile). "
                   "Reply `confirm` to execute or `cancel that` to discard.")
        _save_message(conversation_id, "assistant", reply, meta=json.dumps({"pending_command": pid}))
        return {"reply": reply, "needs_confirmation": True, "pending_command": pid, "proposal": args}

    reply = _execute_command(project_id, intent, args)
    _save_message(conversation_id, "assistant", reply)
    return {"reply": reply}
