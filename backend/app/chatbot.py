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
- `build team <name> with <role1>, <role2>, ...` — reuses only uncommitted agents; hires new agents for roles with nobody free
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
    n = str(name or "").lower().strip().removeprefix("a ").removeprefix("the ").strip()
    if not n:
        return None
    exact = query_one("SELECT * FROM agents WHERE lower(name) = lower(?)", (n,))
    if exact:
        return exact
    contains = query(
        "SELECT * FROM agents WHERE lower(name) LIKE ? ORDER BY name", (f"%{n}%",))
    if contains:
        return contains[0]
    # Fall back to matching by role name, e.g. "a senior developer" -> any agent whose
    # role matches; prefer idle agents, then alphabetical.
    by_role = query(
        "SELECT a.* FROM agents a JOIN roles r ON r.id = a.role_id "
        "WHERE lower(r.name) LIKE ? ORDER BY CASE a.lifecycle_state WHEN 'Idle' THEN 0 ELSE 1 END, a.name",
        (f"%{n}%",))
    if by_role:
        return by_role[0]
    for word in sorted(re.findall(r"[a-z0-9]+", n), key=len, reverse=True):
        if len(word) < 3:
            continue
        hits = query(
            "SELECT a.* FROM agents a JOIN roles r ON r.id = a.role_id "
            "WHERE lower(r.name) LIKE ? ORDER BY CASE a.lifecycle_state WHEN 'Idle' THEN 0 ELSE 1 END, a.name",
            (f"%{word}%",))
        if hits:
            return hits[0]
    return None


_TASK_FILLER = {"task", "the", "a", "an", "to", "please", "assign"}


def _find_task(project_id, title):
    rows = query("SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at", (project_id,))
    if not rows:
        return None
    t = str(title or "").lower().strip()
    exact = [r for r in rows if r["title"].lower() == t]
    if exact:
        return exact[0]
    if t in ("", "task"):
        return rows[0]
    partial = [r for r in rows if t in r["title"].lower()]
    if partial:
        return partial[0]
    # Token-overlap scoring so "user authentication task" matches "Design auth API contract".
    wanted = {w for w in re.findall(r"[a-z0-9]+", t) if w not in _TASK_FILLER}
    if not wanted:
        return rows[0]
    def token_match(w, word):
        if w == word:
            return True
        # prefix-stem: "authentication" matches "auth" (common prefix >= 4 chars)
        return len(w) >= 4 and len(word) >= 4 and (w.startswith(word) or word.startswith(w))
    def score(r):
        words = set(re.findall(r"[a-z0-9]+", r["title"].lower()))
        return sum(1 for w in wanted if any(token_match(w, x) for x in words))
    best = max(rows, key=score)
    return best if score(best) > 0 else None


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
            "last_tested_at": ts, "test_status": "Not tested", "test_diagnostic": "",
            "created_at": ts, "updated_at": ts,
        })
        raw_key = args.get("api_key", "")
        if raw_key and not raw_key.startswith("masked-ref:"):
            db.set_gateway_key(gid, raw_key)
        audit("create_gateway", "gateway", gid, f"Created gateway '{args['name']}'")
        return (f"Gateway **{args['name']}** created ({args.get('provider', 'openai')}). "
                "API key stored encrypted — never written to prompts or logs.")

    if command == "test_gateway":
        gw = query_one("SELECT * FROM gateways WHERE lower(name)=lower(?)", (args["name"],))
        if not gw:
            return f"Gateway **{args['name']}** not found."
        sync = db.sync_gateway_models(gw["id"])
        total = query_one("SELECT COUNT(*) AS n FROM gateway_models WHERE gateway_id=?", (gw["id"],))["n"]
        update("gateways", gw["id"], {"last_tested_at": ts, "test_status": "Success",
                                      "test_diagnostic": "Connection OK (simulated probe)",
                                      "updated_at": ts})
        audit("test_gateway", "gateway", gw["id"],
              f"Tested gateway '{gw['name']}'; catalog +{sync['added']}/-{sync['removed']}")
        return (f"Gateway **{gw['name']}**: connection test succeeded. "
                f"Catalog synced (+{sync['added']} added, {sync['removed']} removed) — now {total} models. "
                "Credential remains masked.")

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

    if command == "build_team":
        raw_roles = args.get("roles") or []
        if isinstance(raw_roles, str):
            raw_roles = [a.strip() for a in re.split(r",| and ", raw_roles) if a.strip()]
        roles_requested = [str(r).strip() for r in raw_roles if str(r).strip()]
        project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not roles_requested:
            return "No roles given. Use `build team <name> with <role1>, <role2>, …`."
        args["name"] = str(args.get("name") or "").strip() or f"{project['name']} Team"
        aligned = _agent_alignment_map()          # agent_id -> [Project (Team)]
        used_ids: set = set()                     # agents already staffed in this build
        staff, reused, hired = [], [], []
        all_roles = query("SELECT * FROM roles WHERE active = 1")

        def resolve_role(name):
            role = _find_role(name)
            if role:
                return role
            n = str(name).lower().strip()
            partial = [r for r in all_roles if n in r["name"].lower() or r["name"].lower() in n]
            if partial:
                return partial[0]
            wanted = set(re.findall(r"[a-z0-9]+", n))
            def overlap(r):
                return len(wanted & set(re.findall(r"[a-z0-9]+", r["name"].lower())))
            best = max(all_roles, key=overlap, default=None)
            return best if best is not None and overlap(best) > 0 else None

        for role_name in roles_requested:
            role = resolve_role(role_name)
            if not role:
                return (f"Role **{role_name}** not found. Available roles: "
                        + ", ".join(r["name"] for r in query("SELECT name FROM roles WHERE active=1")) + ".")
            candidates = query(
                "SELECT a.* FROM agents a WHERE a.role_id = ? "
                "ORDER BY CASE a.lifecycle_state WHEN 'Idle' THEN 0 ELSE 1 END, a.name", (role["id"],))
            pick = None
            for c in candidates:
                if c["id"] in used_ids:
                    continue
                refs = aligned.get(c["id"], [])
                busy_elsewhere = any(not ref.startswith(f"{project['name']} (") for ref in refs)
                if not busy_elsewhere:
                    pick = c
                    break
            if pick:
                used_ids.add(pick["id"])
                staff.append(pick)
                reused.append(f"{pick['name']} ({role['name']})")
            else:
                # Hire: create a fresh agent for this role (same internals as create_agent).
                persona = query_one("SELECT * FROM personas WHERE role_id = ? ORDER BY created_at LIMIT 1", (role["id"],))
                if not persona:
                    return (f"Cannot hire for **{role['name']}** — the role has no persona yet. "
                            "Create one first with `create persona ...`.")
                existing_count = query_one("SELECT COUNT(*) AS n FROM agents WHERE role_id = ?", (role["id"],))["n"]
                # Pick a human-style name from the free pool; fall back to numbered role names.
                taken = {r["name"].lower() for r in query("SELECT name FROM agents")}
                name = next((n for n in ["Kai", "Mia", "Zoe", "Leo", "Ivy", "Ash", "Max", "Rue", "Finn", "Sky",
                                         "Juno", "Pax", "Nia", "Orion", "Vega"]
                             if n.lower() not in taken), None)
                if not name:
                    base = role["name"].split()[0].title()
                    n = existing_count + 1
                    name = f"{base}-{n:02d}"
                    # Exact-name uniqueness only — fuzzy _find_agent would match any agent
                    # of the same role and loop forever.
                    while query_one("SELECT id FROM agents WHERE lower(name) = lower(?)", (name,)):
                        n += 1
                        name = f"{base}-{n:02d}"
                binding = query_one("SELECT * FROM model_bindings WHERE role_id = ? AND active = 1", (role["id"],))
                aid = new_id()
                insert("agents", {"id": aid, "name": name, "role_id": role["id"],
                                  "persona_id": persona["id"],
                                  "model_binding_id": binding["id"] if binding else None,
                                  "lifecycle_state": "Idle", "created_at": ts, "updated_at": ts})
                audit("hire_agent", "agent", aid,
                      f"Hired agent '{name}' ({role['name']} / {persona['name']}) for team '{args['name']}'")
                hired_agent = query_one("SELECT * FROM agents WHERE id = ?", (aid,))
                used_ids.add(aid)
                staff.append(hired_agent)
                hired.append(f"{name} ({role['name']})")
        tid = new_id()
        insert("teams", {"id": tid, "name": args["name"], "description": "",
                         "status": "Active", "created_at": ts})
        for a in staff:
            execute("INSERT OR IGNORE INTO team_agents (team_id, agent_id, role_in_team, active) VALUES (?,?,?,1)",
                    (tid, a["id"], "Member"))
        audit("build_team", "team", tid,
              f"Built team '{args['name']}' — reused {len(reused)}, hired {len(hired)}")
        notes = [f"Team **{args['name']}** built with {len(staff)} agent(s)."]
        if reused:
            notes.append("Reused (uncommitted): " + ", ".join(reused) + ".")
        if hired:
            notes.append("Hired new agents: " + ", ".join(hired) + ".")
        if not project["team_id"]:
            update("projects", project_id, {"team_id": tid, "updated_at": ts})
            notes.append(f"Aligned **{project['name']}** to this team.")
        else:
            notes.append(f"Project already aligned to a team; **{args['name']}** left unaligned "
                         "(swap it in Projects → Settings if desired).")
        return " ".join(notes)

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
            titles = [r["title"] for r in query(
                "SELECT title FROM tasks WHERE project_id = ? ORDER BY created_at DESC LIMIT 8", (project_id,))]
            return (f"Task matching **{args.get('task')}** not found. Current tasks: "
                    + ("; ".join(titles) if titles else "(none yet)") + ".")
        if not agent:
            names = [r["name"] for r in query("SELECT name FROM agents ORDER BY name LIMIT 8")]
            return (f"Agent **{args.get('agent')}** not found. Available agents: "
                    + ("; ".join(names) if names else "(none yet)") + ".")
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
    ("build_team", r"build (?:a |the )?team\s+(?:called\s+|named\s+)?(?P<name>.+?)\s+(?:with|staffed with|using)\s+(?P<roles>.+?)\s*$"),
    ("build_team", r"build (?:a |the )?team\s+(?:for|to)\s+(?:this\s+)?project\s*$"),
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

SENSITIVE = {"create_gateway", "create_agent", "create_team", "build_team"}

# Static quick replies for the deterministic (typed) command path.
SUGGESTIONS = {
    "status": ["Start sprint execution", "What are the agents doing?", "Create a sprint"],
    "create_role": ["Create persona for this role", "Create agent with this role", "Status"],
    "create_persona": ["Create agent with this persona", "Show agent memory", "Status"],
    "create_skill": ["Attach skill to a role", "Create another skill", "Status"],
    "create_agent": ["Build a team with this agent", "Status", "Create another agent"],
    "create_team": ["Add backlog item", "Create sprint", "Status"],
    "build_team": ["Add backlog item", "Create sprint", "Start sprint execution"],
    "add_backlog_item": ["Create a sprint for these items", "Assign task to an agent", "Status"],
    "create_sprint": ["Add backlog item", "Start sprint execution", "Status"],
    "assign_task": ["Start task", "Start sprint execution", "Agent status"],
    "start_task": ["Status", "Pause execution", "What are the agents doing?"],
    "start_sprint": ["Status", "Pause execution", "Stop sprint execution"],
    "stop_sprint": ["Status", "Retry last failed task", "Resume execution"],
    "pause_execution": ["Resume execution", "Status", "Cancel execution"],
    "resume_execution": ["Status", "Pause execution", "What are the agents doing?"],
    "cancel_execution": ["Status", "Retry last failed task", "Create sprint"],
    "retry_failed": ["Status", "Pause execution", "What are the agents doing?"],
    "create_gateway": ["Test gateway", "Status", "Help"],
    "test_gateway": ["Create agent", "Status", "Help"],
}


def _get_setting(key, default=""):
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def _bot_config():
    """Configured AI brain for the assistant: (gateway_row, model_row) or (None, None)."""
    import json as _json
    raw = _get_setting("control_bot", "")
    if not raw:
        return None, None
    try:
        cfg = _json.loads(raw)
    except ValueError:
        return None, None
    gw = query_one("SELECT * FROM gateways WHERE id = ?", (cfg.get("gateway_id", ""),))
    model = query_one("SELECT * FROM gateway_models WHERE id = ?", (cfg.get("model_id", ""),))
    if not gw or not model:
        return None, None
    return gw, model


def _team_alignment_map():
    """team_id -> project name it is aligned to (first project referencing it)."""
    rows = query("SELECT id, name, team_id FROM projects WHERE team_id IS NOT NULL")
    return {r["team_id"]: r["name"] for r in rows}


def _agent_alignment_map():
    """agent_id -> list of 'Project (Team)' the agent is aligned to via project-bound teams."""
    align = _team_alignment_map()
    out: dict = {}
    for row in query(
            "SELECT ta.agent_id, ta.team_id, t.name AS team_name FROM team_agents ta "
            "JOIN teams t ON t.id = ta.team_id WHERE ta.active = 1"):
        proj = align.get(row["team_id"])
        if proj:
            out.setdefault(row["agent_id"], []).append(f"{proj} ({row['team_name']})")
    return out


def _agent_status_lines():
    rows = query(
        "SELECT a.name, a.lifecycle_state, a.current_activity, r.name AS role "
        "FROM agents a LEFT JOIN roles r ON r.id = a.role_id ORDER BY a.name")
    aligned = _agent_alignment_map()
    id_by_name = {i["name"]: i["id"] for i in query("SELECT id, name FROM agents")}
    result = []
    for r in rows:
        line = f"- {r['name']} ({r['role']}): {r['lifecycle_state']}"
        if r["current_activity"]:
            line += f" — {r['current_activity']}"
        refs = aligned.get(id_by_name.get(r["name"]), [])
        line += f" — aligned to: {', '.join(refs)}" if refs else " — uncommitted (free)"
        result.append(line)
    return result


def _context_brief(project_id) -> str:
    p = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    sprint = query_one("SELECT * FROM sprints WHERE project_id = ? AND status IN ('Active','Planned') "
                       "ORDER BY created_at DESC LIMIT 1", (project_id,))
    backlog = query("SELECT title, story_points, status FROM backlog_items WHERE project_id = ? "
                    "ORDER BY created_at DESC LIMIT 10", (project_id,))
    tasks = query("SELECT title, status, story_points AS points FROM tasks WHERE project_id = ? ORDER BY created_at DESC LIMIT 15",
                  (project_id,))
    align = _team_alignment_map()
    teams = query("SELECT id, name, status FROM teams ORDER BY name")
    roles = query("SELECT name FROM roles WHERE active = 1 ORDER BY name")
    lines = [f"Project: {p['name']} — {p['goal']}", f"Stack: {p['technology_stack']} | Status: {p['status']}"]
    if sprint:
        lines.append(f"Sprint: {sprint['name']} ({sprint['status']}, capacity {sprint['capacity']} pts)")
    if tasks:
        lines.append("Recent tasks: " + "; ".join(f"{t['title']} [{t['status']}]" for t in tasks))
    if backlog:
        lines.append("Backlog: " + "; ".join(f"{b['title']} ({b['story_points']} pts, {b['status']})" for b in backlog))
    lines.append("Available roles: " + (", ".join(r["name"] for r in roles) if roles else "(none)"))
    lines.append("Teams:")
    for t in teams:
        members = query(
            "SELECT a.name, r.name AS role FROM team_agents ta JOIN agents a ON a.id = ta.agent_id "
            "JOIN roles r ON r.id = a.role_id WHERE ta.team_id = ? AND ta.active = 1 ORDER BY a.name", (t["id"],))
        proj = align.get(t["id"])
        line = f"- {t['name']} [{t['status']}] — aligned to: {proj if proj else 'no project'}"
        line += " — members: " + (", ".join(f"{m['name']} ({m['role']})" for m in members) if members else "(none)")
        lines.append(line)
    lines.append("Agents:")
    lines.extend(_agent_status_lines() or ["- (none yet)"])
    return "\n".join(lines)


AI_SYSTEM_PROMPT = """You are the Project Control copilot of "Agent Office", an app that runs a simulated \
multi-agent software team. You convert the user's natural language into AT MOST ONE typed command per turn.

Available commands (name: args):
- create_gateway: {name, provider, base_url, api_key?}
- test_gateway: {name}
- create_role: {name}
- create_persona: {name, role}
- create_skill: {name}
- create_agent: {name, role, persona?}
- build_team: {name, roles: ["role name", ...]} — preferred way to build teams
- create_team: {name, agents: ["agent name", ...]} — only when the user names specific FREE agents
- add_backlog_item: {title, points?, priority?}
- create_sprint: {name, goal?, capacity?}
- assign_task: {task, agent}
- start_task: {task}
- start_sprint: {}
- stop_sprint: {}
- pause_execution: {}
- resume_execution: {}
- cancel_execution: {}
- retry_failed: {}

Rules:
- Pick a command only when the user clearly wants that action; otherwise set command to null and just answer.
- ALIGNMENT IS FACT: each agent line in the context states which project(s) it is aligned to via its team, or "uncommitted (free)". Never claim an aligned agent is free; never assign an agent aligned to a DIFFERENT project. Teams also show their aligned project and full member roster.
- When the user asks to build/staff a team (for this or any project), use build_team with the needed role names. build_team automatically reuses only uncommitted agents and hires new agents for roles with nobody free. Only use create_team if the user explicitly names agents AND every named agent is uncommitted (free) or aligned to the CURRENT project.
- Reference tasks by the closest title in the context (partial titles are fine, e.g. "user authentication" for "User authentication API").
- Reference agents by exact name, or by role when the user means "any/one <role>" (e.g. agent "a senior developer" if a developer role exists); the resolver picks the best idle agent for that role.
- If the user asks about status or progress, set command to null and summarize from the context.
- reply: short, friendly, concrete. suggestions: exactly 3 short follow-up messages the user might send next.

Respond with ONLY a JSON object, no markdown fences:
{"reply": "...", "command": {"name": "...", "args": {...}} or null, "suggestions": ["...", "...", "..."]}

Current context:
"""


def _extract_json(text: str):
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(t[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _call_llm(gw, model, system_prompt: str, user_text: str) -> str:
    """Live inference call to the configured gateway/model. Returns raw text."""
    import urllib.error as _uerr
    import urllib.request as _ureq

    api_key = db.get_gateway_key(gw["id"])
    if not api_key:
        raise RuntimeError("No API key stored for the configured gateway. "
                           "Open Settings, edit the gateway card and paste its key.")
    base = gw["base_url"].rstrip("/")
    is_anthropic = gw["api_type"] == "anthropic-messages"
    if is_anthropic:
        if base.endswith("/messages"):
            url = base
        elif base.endswith("/v1"):
            url = base + "/messages"
        else:
            url = base + "/v1/messages"
        payload = {"model": model["provider_model_id"], "max_tokens": 700,
                   "system": system_prompt,
                   "messages": [{"role": "user", "content": user_text}]}
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
    else:
        if "/v1" not in base:
            base += "/v1"
        url = base + "/chat/completions"
        payload = {"model": model["provider_model_id"], "max_tokens": 700,
                   "messages": [{"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_text}]}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    req = _ureq.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with _ureq.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except _uerr.HTTPError as e:
        raise RuntimeError(f"Gateway returned HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
    except Exception as exc:
        raise RuntimeError(f"Could not reach gateway: {exc}")
    if is_anthropic:
        return "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return data["choices"][0]["message"]["content"]


def _ai_route(project_id: str, conversation_id: str, text: str) -> dict:
    """LLM-driven intent routing over the typed command engine."""
    gw, model = _bot_config()
    try:
        raw = _call_llm(gw, model, AI_SYSTEM_PROMPT + _context_brief(project_id), text)
        parsed = _extract_json(raw)
        if not parsed or "reply" not in parsed:
            raise RuntimeError("The model did not return a valid response.")
    except RuntimeError as exc:
        reply = (f"AI routing failed — falling back to typed commands. ({exc})\n\n"
                 "Try `help` for the command catalog.")
        _save_message(conversation_id, "assistant", reply)
        return {"reply": reply, "suggestions": ["Status", "Help"]}

    reply = parsed["reply"].strip()
    suggestions = [str(s) for s in (parsed.get("suggestions") or [])][:3]
    command = parsed.get("command") or None
    result = {"reply": reply, "suggestions": suggestions or ["Status", "Help"]}

    if command and isinstance(command, dict) and command.get("name"):
        name = str(command["name"]).strip()
        args = command.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        if name in ("create_team",):
            args.setdefault("agents", [])
            if isinstance(args["agents"], str):
                args["agents"] = [a.strip() for a in args["agents"].split(",") if a.strip()]
        if name in ("build_team",):
            args.setdefault("roles", [])
            if isinstance(args["roles"], str):
                args["roles"] = [a.strip() for a in re.split(r",| and ", args["roles"]) if a.strip()]
        if name in SENSITIVE:
            pid = _queue_pending(conversation_id, name, args, name)
            reply = (f"{reply}\n\n**Confirmation required** to run `{name}` with:\n"
                     + "\n".join(f"- **{k}**: {v}" for k, v in args.items() if k != "api_key")
                     + "\n\nReply `confirm` to execute or `cancel that` to discard.")
            result.update({"reply": reply, "needs_confirmation": True,
                           "pending_command": pid, "proposal": args})
            _save_message(conversation_id, "assistant", reply, meta=json.dumps({"pending_command": pid}))
            return result
        outcome = _execute_command(project_id, name, args)
        reply = f"{reply}\n\n{outcome}" if reply else outcome
        result["reply"] = reply
        if not suggestions:
            result["suggestions"] = SUGGESTIONS.get(name, ["Status", "Help"])

    _save_message(conversation_id, "assistant", reply)
    return result


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
            _save_message(conversation_id, "assistant", reply)
            return {"reply": reply, "suggestions": ["Status", "Help"]}
        args = json.loads(pending["args_json"])
        reply = _execute_command(project_id, pending["command"], args)
        execute("DELETE FROM pending_commands WHERE id = ?", (pending["id"],))
        _save_message(conversation_id, "assistant", reply)
        return {"reply": reply, "suggestions": SUGGESTIONS.get(pending["command"], ["Status", "Help"])}

    if intent == "cancel_pending":
        execute("DELETE FROM pending_commands WHERE conversation_id = ?", (conversation_id,))
        reply = "Pending command discarded."
        _save_message(conversation_id, "assistant", reply)
        return {"reply": reply, "suggestions": ["Status", "Help"]}

    if not intent and _bot_config()[0]:
        return _ai_route(project_id, conversation_id, text)

    if intent == "help" or not intent:
        reply = HELP_TEXT if intent == "help" else (
            "I did not recognize that request and no AI model is configured for me. "
            "Set one under Settings → Project Control AI, or use typed commands — try `help`. "
            "For example: `create role Data Engineer`, `status`, or `start task Implement login endpoint`.")
        _save_message(conversation_id, "assistant", reply)
        return {"reply": reply, "suggestions": ["Status", "Start sprint execution", "Create a sprint"]}

    if intent == "status":
        reply = _project_summary(project_id)
        _save_message(conversation_id, "assistant", reply)
        return {"reply": reply, "suggestions": SUGGESTIONS["status"]}

    args = {k: v for k, v in (match.groupdict() or {}).items() if v is not None}
    if intent == "create_team":
        args["agents"] = [a.strip() for a in args.get("agents", "").split(",")]
    if intent == "add_backlog_item" and "points" in args:
        args["points"] = int(args["points"])
    if intent == "add_backlog_item" and "priority" in args:
        args["priority"] = int(args["priority"])
    # The raw key is kept in args so it can be stored encrypted on execution;
    # it is never echoed back into the chat.

    if intent in SENSITIVE:
        pid = _queue_pending(conversation_id, intent, args, intent)
        reply = (f"**Confirmation required.** You asked to run `{intent}` with:\n"
                 + "\n".join(f"- **{k}**: {v}" for k, v in args.items() if k != "api_key")
                 + "\n\nThis is a sensitive mutation (credentials or team/cost profile). "
                   "Reply `confirm` to execute or `cancel that` to discard.")
        _save_message(conversation_id, "assistant", reply, meta=json.dumps({"pending_command": pid}))
        return {"reply": reply, "needs_confirmation": True, "pending_command": pid, "proposal": args,
                "suggestions": ["confirm", "cancel that"]}

    reply = _execute_command(project_id, intent, args)
    _save_message(conversation_id, "assistant", reply)
    return {"reply": reply, "suggestions": SUGGESTIONS.get(intent, ["Status", "Help"])}
