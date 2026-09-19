"""Project Control assistant.

A policy-bound command agent: parses natural-language requests into typed
application commands (no raw SQL/DB tool), asks for confirmation on
sensitive mutations, executes them through domain services and records
audit events. Model inference is simulated with deterministic parsing so
the demo works without provider credentials.
"""
import json
import re

from . import db, runtime, toolchains, po
from .db import audit, execute, insert, new_id, now, query, query_one, update

HELP_TEXT = """I can execute these typed commands:

**Configure**
- `create gateway <name> provider <provider> url <base-url> key <api-key>`
- `test gateway <name>`

**Toolchains**
- `install dotnet|go|node toolchain` — installs a build toolchain (approval-first)

**Agent Memory**
- `create role <name>`
- `create persona <name> for role <role>`
- `create skill <name>`

**Teams**
- `create agent <name> role <role> persona <persona>`
- `build team <name> with <role1>, <role2>, ...` — reuses only uncommitted agents; hires new agents for roles with nobody free
- `create team <name> with agents <a>, <b>, ...` — refused for agents already serving another project
- `hire <role>` — hire a new agent for a role (optional: `named <name> to team <team>`)
- `add agent <name> to team <team>` — add a free agent (team optional: project's aligned team)
- `move agent <name> to team <team>` — transfer an Idle agent from their other team
- `remove agent <name> from team <team>`
- `align team <name> to this project` — aligns a team to the current project (name optional: newest unaligned team)

**Agile**
- `add backlog item <title> points <n>`
- `add task <title> to sprint <name>` — puts a task directly into a sprint
- `add urgent task <title>` — shows a plan; on approval it enters the active sprint at priority 1 and goes straight to an idle developer
- `create sprint <name>`
- `assign task <title> to <agent>`
- `start task <title>`
- `start sprint execution` / `stop sprint execution`
- `pause execution` / `cancel execution` / `retry last failed task`

**Product Owner**
- `hire product owner` — add a Product Owner agent to the project's team
- `enable product owner` — the PO takes over with your full authority (autonomous mode)
- `disable product owner` — control returns to you; all open tasks remain untouched

**Info**
- `status` — project summary
- `help` — this message

Sensitive commands (gateways, agents, teams) require your confirmation before execution.
`create sprint` shows a full sprint plan (with tasks) for your approval first — reply `confirm` to create it."""


def _save_message(conversation_id, role, content, meta=""):
    insert("messages", {"id": new_id(), "conversation_id": conversation_id,
                        "role": role, "content": content, "meta": meta, "created_at": now()})
    update("conversations", conversation_id, {"updated_at": now()})


def _find_role(name):
    return query_one("SELECT * FROM roles WHERE lower(name) = lower(?)", (name,))


def _find_persona(name):
    return query_one("SELECT * FROM personas WHERE lower(name) = lower(?)", (name,))


def _ensure_role_persona(role):
    """Return the role's first persona, synthesizing one from its Agent Memory
    instruction files (or a generic template) when none exists yet."""
    persona = query_one("SELECT * FROM personas WHERE role_id = ? ORDER BY created_at LIMIT 1", (role["id"],))
    if persona:
        return persona
    now_ts = now()
    files = query("SELECT filename, content FROM instruction_files WHERE role_id = ? AND active = 1 ORDER BY filename", (role["id"],))
    body = "\n\n".join(f"## {f['filename']}\n{f['content']}".strip() for f in files).strip()
    if not body:
        body = (f"You are a {role['name']} on a simulated delivery team. Follow standard professional practices "
                "for your role, coordinate with teammates, and produce concrete artifacts for every task.")
    pid = new_id()
    insert("personas", {"id": pid, "role_id": role["id"], "name": f"Default {role['name']}",
                        "description": f"Auto-generated from Agent Memory instructions for {role['name']}.",
                        "instructions": body, "constraints_text": "",
                        "version": 1, "active": 1, "created_at": now_ts, "updated_at": now_ts})
    insert("persona_versions", {"id": new_id(), "persona_id": pid, "version": 1,
                                "instructions": body, "constraints_text": "",
                                "checksum": db.checksum(body), "created_at": now_ts})
    audit("ensure_persona", "persona", pid,
          f"Auto-created persona 'Default {role['name']}' from {len(files)} Agent Memory instruction file(s)")
    return query_one("SELECT * FROM personas WHERE id = ?", (pid,))


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

# Relative references users say instead of a concrete title, e.g. "the first task".
_TASK_ORDINALS = {"first": 0, "1st": 0, "1": 0, "second": 1, "2nd": 1, "2": 1,
                  "third": 2, "3rd": 2, "3": 2, "fourth": 3, "4th": 3, "4": 3,
                  "fifth": 4, "5th": 4, "5": 4, "last": -1, "latest": -1, "newest": -1}

# Generic agent references that mean "any member of this project's team".
_GENERIC_AGENT_REFS = {"agent", "the agent", "a team member", "team member", "member",
                       "a member", "any member", "anyone", "someone", "any agent",
                       "anybody", "whoever", "a teammate", "teammate"}


def _pick_team_member(project_id, task=None):
    """Pick a member of the project's aligned team for a task: prefer a
    role-family match (idle first, least loaded), then any idle member."""
    members = runtime._team_members(project_id)
    if not members:
        return None
    family = runtime._family_for_task(task) if task else None
    pick, _ = runtime._pick_member(members, family)
    return pick


def _find_task(project_id, title):
    rows = query("SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at", (project_id,))
    if not rows:
        return None
    t = str(title or "").lower().strip().strip(".!?;,")
    exact = [r for r in rows if r["title"].lower() == t]
    if exact:
        return exact[0]
    if t in ("", "task"):
        return rows[0]
    # Normalize relative references: "the first task" -> "first", "task 2" -> "2".
    ref = re.sub(r"^(?:the|a|an)\s+", "", t)
    ref = re.sub(r"^task\s+", "", ref)
    ref = re.sub(r"\s+task$", "", ref).strip()
    if ref == "next":
        return next((r for r in rows if r["status"] not in ("Done", "Cancelled")), rows[0])
    if ref in _TASK_ORDINALS:
        idx = _TASK_ORDINALS[ref]
        return rows[idx] if -len(rows) <= idx < len(rows) else None
    t = ref
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



def _default_gateway_and_model():
    """Pick the AI brain's gateway/model if set, else the first gateway that has models."""
    gw, model = _bot_config()
    if gw and model:
        return gw, model
    for g in query("SELECT * FROM gateways ORDER BY created_at"):
        m = query_one("SELECT * FROM gateway_models WHERE gateway_id = ? ORDER BY created_at LIMIT 1", (g["id"],))
        if m:
            return g, m
    return None, None


_ROLE_KIT_PROMPT = """You are provisioning a complete starter kit for the role "{role_name}" ({role_desc}) \
in a simulated AI software delivery team. Use EXACTLY this output format with the section markers as shown \
(no markdown code fences, no extra commentary):

=== PERSONA_INSTRUCTIONS ===
(2-4 paragraph system prompt for an agent filling this role: identity, expertise, working style, deliverables)
=== PERSONA_CONSTRAINTS ===
(3-5 bullet constraints: things the role must never do / must always do)
=== CHARTER_MD ===
(a markdown charter with sections: Mission, Core Responsibilities, Deliverables, Collaboration, Constraints)
=== SKILL: <short skill name> | <one-line description> ===
(markdown guidance for applying this skill)

Provide exactly 3 SKILL sections that are core to this role's expertise.
"""

_ROLE_KIT_SECTIONS = ("PERSONA_INSTRUCTIONS", "PERSONA_CONSTRAINTS", "CHARTER_MD")


def _parse_role_kit(text: str):
    """Parse the delimiter-format role kit. Returns dict or None."""
    sections: dict = {"skills": []}
    current = None
    last_skill = None
    buf: list = []
    for line in text.splitlines():
        stripped = line.strip()
        marker = None
        if stripped.startswith("=== ") and stripped.endswith(" ==="):
            marker = stripped[4:-4].strip()
        elif stripped in _ROLE_KIT_SECTIONS:
            marker = stripped
        if marker is not None:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            buf = []
            if marker.startswith("SKILL:"):
                spec = marker[len("SKILL:"):].strip()
                sname, sdesc = (spec.split("|", 1) + [""])[:2]
                last_skill = {"name": sname.strip(), "description": sdesc.strip(), "content": ""}
                sections["skills"].append(last_skill)
                current = None
            elif marker in _ROLE_KIT_SECTIONS:
                current = marker
            continue
        if last_skill is not None and current is None:
            last_skill["content"] = (last_skill["content"] + "\n" + line).strip()
        else:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    if not sections.get("PERSONA_INSTRUCTIONS"):
        return None
    return sections


def _template_role_kit(role):
    rn = role["name"]
    return {
        "persona_instructions": (
            f"You are a senior {rn} on a simulated delivery team. You bring deep expertise in your domain, "
            f"break work into concrete verifiable steps, and produce clear artifacts for every task. "
            f"You coordinate with adjacent roles and keep commitments small and shippable."),
        "persona_constraints": (
            f"- Never commit changes that bypass the definition of done.\n"
            f"- Always document decisions and open questions.\n"
            f"- Escalate blockers instead of going silent."),
        "charter_md": (
            f"# {rn} — Role Charter\n\n## Mission\nDeliver expert {rn} work across all project phases.\n\n"
            f"## Core Responsibilities\n- Own {rn} work items end to end\n"
            f"- Apply best practices and quality standards\n- Review and support teammates' work\n\n"
            f"## Deliverables\n- Task artifacts with evidence\n- Clear status updates\n\n"
            f"## Collaboration\n- Coordinate with the team daily\n- Hand off work with documentation\n\n"
            f"## Constraints\n- Follow project conventions\n- Escalate blockers early"),
        "skills": [
            {"name": f"{rn} Fundamentals", "description": f"Core practices of the {rn} role",
             "content": f"Apply standard {rn} practices: plan, execute in small steps, verify, document."},
            {"name": "Quality Standards", "description": "Definition of done and review checklist",
             "content": "- Meets acceptance criteria\n- Reviewed by a peer\n- Documented"},
            {"name": "Team Collaboration", "description": "Working agreements across roles",
             "content": "Communicate status daily; hand off with context; escalate blockers within a day."},
        ],
    }


def _generate_role_kit(role) -> str:
    """Create persona + instruction files + skills + model binding for a new role.
    Uses the AI brain when configured; falls back to templates. Returns a summary string."""
    now_ts = now()
    try:
        kit = _template_role_kit(role)
        gw, model = _bot_config()
        if gw and model:
            try:
                raw = _call_llm(gw, model,
                                _ROLE_KIT_PROMPT.format(role_name=role["name"],
                                                        role_desc=role["description"] or role["name"]),
                                f"Generate the role kit for: {role['name']}",
                                max_tokens=3000)
                parsed = _parse_role_kit(raw)
                if parsed:
                    kit = {
                        "persona_instructions": parsed.get("PERSONA_INSTRUCTIONS") or kit["persona_instructions"],
                        "persona_constraints": parsed.get("PERSONA_CONSTRAINTS") or kit["persona_constraints"],
                        "charter_md": parsed.get("CHARTER_MD") or kit["charter_md"],
                        "skills": [s for s in parsed.get("skills", []) if s.get("name")] or kit["skills"],
                    }
            except Exception:
                pass  # fall back to template kit

        # Persona
        pid = new_id()
        insert("personas", {"id": pid, "role_id": role["id"], "name": f"Default {role['name']}",
                            "description": f"Auto-generated expertise kit for {role['name']}.",
                            "instructions": kit["persona_instructions"],
                            "constraints_text": kit["persona_constraints"],
                            "version": 1, "active": 1, "created_at": now_ts, "updated_at": now_ts})
        insert("persona_versions", {"id": new_id(), "persona_id": pid, "version": 1,
                                    "instructions": kit["persona_instructions"],
                                    "constraints_text": kit["persona_constraints"],
                                    "checksum": db.checksum(kit["persona_instructions"]), "created_at": now_ts})
        # Instruction files
        insert("instruction_files", {"id": new_id(), "role_id": role["id"],
                                     "filename": f"{role['name'].replace(' ', '-')}-Charter.md",
                                     "description": f"Role charter for {role['name']}",
                                     "content": kit["charter_md"], "version": 1,
                                     "created_at": now_ts, "updated_at": now_ts})
        # Skills
        n_skills = 0
        for s in kit["skills"][:4]:
            if not isinstance(s, dict) or not s.get("name"):
                continue
            sid = new_id()
            insert("skills", {"id": sid, "name": str(s["name"])[:60], "description": str(s.get("description", "")),
                              "content": str(s.get("content", "")), "version": 1, "active": 1,
                              "created_at": now_ts, "updated_at": now_ts})
            execute("INSERT OR IGNORE INTO role_skills (role_id, skill_id) VALUES (?,?)", (role["id"], sid))
            n_skills += 1
        # Model binding from an available gateway
        binding_note = "no model binding (configure a gateway in Settings)"
        gw, model = _default_gateway_and_model()
        if gw and model:
            insert("model_bindings", {"id": new_id(), "role_id": role["id"], "gateway_id": gw["id"],
                                      "model_id": model["id"], "settings_json": '{"temperature": 0.3}',
                                      "active": 1})
            binding_note = f"model binding to {model['provider_model_id']} @ {gw['name']}"
        return (f"persona 'Default {role['name']}', charter instruction file, "
                f"{n_skills} skill(s), {binding_note}")
    except Exception as exc:
        return f"kit generation failed ({exc}) — add persona/instructions manually"


def _coerce_int(value, default: int = 0) -> int:
    """Coerce AI-supplied values like 'high', 'P1', '8 points' to an int."""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    try:
        return int(text)
    except ValueError:
        pass
    words = {
        "critical": 1, "urgent": 1, "asap": 1, "highest": 1, "p0": 1,
        "high": 2, "important": 2, "p1": 2,
        "medium": 3, "normal": 3, "moderate": 3, "mid": 3, "p2": 3,
        "low": 4, "minor": 4, "p3": 4,
        "lowest": 5, "trivial": 5, "nice to have": 5, "p4": 5,
    }
    if text in words:
        return words[text]
    m = re.search(r"\d+", text)
    return int(m.group()) if m else default


_HIRE_NAMES = ["Kai", "Mia", "Zoe", "Leo", "Ivy", "Ash", "Max", "Rue", "Finn", "Sky",
               "Juno", "Pax", "Nia", "Orion", "Vega"]


def _resolve_role(name):
    role = _find_role(name)
    if role:
        return role
    all_roles = query("SELECT * FROM roles WHERE active = 1")
    n = str(name).lower().strip()
    partial = [r for r in all_roles if n in r["name"].lower() or r["name"].lower() in n]
    if partial:
        return partial[0]
    wanted = set(re.findall(r"[a-z0-9]+", n))

    def overlap(r):
        return len(wanted & set(re.findall(r"[a-z0-9]+", r["name"].lower())))
    best = max(all_roles, key=overlap, default=None)
    return best if best is not None and overlap(best) > 0 else None


def _next_agent_name(role):
    taken = {r["name"].lower() for r in query("SELECT name FROM agents")}
    name = next((n for n in _HIRE_NAMES if n.lower() not in taken), None)
    if not name:
        base = role["name"].split()[0].title()
        n = query_one("SELECT COUNT(*) AS n FROM agents WHERE role_id = ?", (role["id"],))["n"] + 1
        name = f"{base}-{n:02d}"
        # Exact-name uniqueness only — fuzzy _find_agent would match any agent
        # of the same role and loop forever.
        while query_one("SELECT id FROM agents WHERE lower(name) = lower(?)", (name,)):
            n += 1
            name = f"{base}-{n:02d}"
    return name


def _hire_agent_for_role(role, name=None):
    """Create a fully functional agent for a role (persona + role model binding)."""
    persona = _ensure_role_persona(role)
    name = name or _next_agent_name(role)
    binding = query_one("SELECT * FROM model_bindings WHERE role_id = ? AND active = 1", (role["id"],))
    aid = new_id()
    ts = now()
    insert("agents", {"id": aid, "name": name, "role_id": role["id"],
                      "persona_id": persona["id"],
                      "model_binding_id": binding["id"] if binding else None,
                      "lifecycle_state": "Idle", "created_at": ts, "updated_at": ts})
    return query_one("SELECT * FROM agents WHERE id = ?", (aid,))


def _find_agent_strict(name):
    """Agent lookup by name only — never by role. The role fallback in _find_agent
    would silently pick a different agent for roster operations."""
    n = str(name or "").lower().strip()
    if not n:
        return None
    exact = query_one("SELECT * FROM agents WHERE lower(name) = lower(?)", (n,))
    if exact:
        return exact
    rows = query("SELECT * FROM agents WHERE lower(name) LIKE ? ORDER BY name", (f"%{n}%",))
    return rows[0] if len(rows) == 1 else None


def _agent_project_map(agent_id):
    """project name -> team name for every project the agent serves via an
    aligned team. Keyed by the 1-agent-serves-1-project rule."""
    align = _team_alignment_map()
    out = {}
    for row in query(
            "SELECT ta.team_id, t.name AS team_name FROM team_agents ta "
            "JOIN teams t ON t.id = ta.team_id WHERE ta.agent_id = ? AND ta.active = 1", (agent_id,)):
        proj = align.get(row["team_id"])
        if proj:
            out[proj] = row["team_name"]
    return out


def _resolve_team(project, team_ref):
    """(team_row, error) for roster commands; defaults to the project's aligned team."""
    if team_ref:
        ref = str(team_ref).lower().strip()
        for t in query("SELECT * FROM teams ORDER BY created_at DESC"):
            if t["name"].lower() == ref or ref in t["name"].lower():
                return t, None
        return None, ("Team matching **" + str(team_ref) + "** not found. Teams: "
                      + ", ".join(t["name"] for t in query("SELECT name FROM teams")) + ".")
    if project["team_id"]:
        return query_one("SELECT * FROM teams WHERE id = ?", (project["team_id"],)), None
    return None, ("This project has no aligned team — name one (e.g. `to team <name>`) "
                  "or run `build team` first.")


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
        role = query_one("SELECT * FROM roles WHERE id = ?", (rid,))
        kit_note = _generate_role_kit(role)
        audit("create_role", "role", rid,
              f"Created role '{args['name']}' with full kit ({kit_note})")
        return (f"Role **{args['name']}** created with a full expertise kit: "
                f"{kit_note}. Everything is editable later in the Agent Memory tab.")



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
            persona = _ensure_role_persona(role)
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
        project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        blocked, kept = [], []
        for a in agent_rows:
            others = [p for p in _agent_project_map(a["id"]) if not project or p != project["name"]]
            if others:
                blocked.append(f"{a['name']} ({', '.join(others)})")
            else:
                kept.append(a)
        agent_rows = kept
        skipped_note = ""
        if blocked:
            skipped_note = (" Skipped (already aligned to another project — one agent serves one project): "
                            + ", ".join(blocked) + ".")
        if not agent_rows:
            return ("No usable agents: " + ", ".join(blocked)
                    + ". One agent serves one project only — use `build team` to hire new agents instead.")
        existing = query_one("SELECT * FROM teams WHERE lower(name) = lower(?)", (str(args["name"]).strip(),))
        if existing:
            member_ids = {r["agent_id"] for r in
                          query("SELECT agent_id FROM team_agents WHERE team_id = ? AND active = 1",
                                (existing["id"],))}
            added = [a["name"] for a in agent_rows if a["id"] not in member_ids]
            for a in agent_rows:
                execute("INSERT OR IGNORE INTO team_agents (team_id, agent_id, role_in_team, active) VALUES (?,?,?,1)",
                        (existing["id"], a["id"], "Member"))
            audit("create_team", "team", existing["id"],
                  f"Added {len(added)} agent(s) to existing team '{existing['name']}'")
            if added:
                return (f"Team **{existing['name']}** already exists — added missing agents: "
                        + ", ".join(added) + ". No duplicate team was created." + skipped_note)
            return (f"Team **{existing['name']}** already exists and all requested agents "
                    f"({', '.join(a['name'] for a in agent_rows)}) are already on it." + skipped_note)
        tid = new_id()
        insert("teams", {"id": tid, "name": args["name"], "description": "",
                         "status": "Active", "created_at": ts})
        for a in agent_rows:
            execute("INSERT OR IGNORE INTO team_agents (team_id, agent_id, role_in_team, active) VALUES (?,?,?,1)",
                    (tid, a["id"], "Member"))
        audit("create_team", "team", tid,
              f"Created team '{args['name']}' with {len(agent_rows)} agents")
        return f"Team **{args['name']}** created with agents: {', '.join(a['name'] for a in agent_rows)}." + skipped_note

    if command == "build_team":
        raw_roles = args.get("roles") or []
        if isinstance(raw_roles, str):
            raw_roles = [a.strip() for a in re.split(r",| and ", raw_roles) if a.strip()]
        roles_requested = [str(r).strip() for r in raw_roles if str(r).strip()]
        project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not project:
            return "Project not found for this conversation."
        if not roles_requested:
            return "No roles given. Use `build team <name> with <role1>, <role2>, …`."
        args["name"] = str(args.get("name") or "").strip() or f"{project['name']} Team"
        aligned = _agent_alignment_map()          # agent_id -> [Project (Team)]
        used_ids: set = set()                     # agents already staffed in this build
        staff, reused, hired = [], [], []

        for role_name in roles_requested:
            role = _resolve_role(role_name)
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
                hired_agent = _hire_agent_for_role(role)
                audit("hire_agent", "agent", hired_agent["id"],
                      f"Hired agent '{hired_agent['name']}' ({role['name']}) for team '{args['name']}'")
                used_ids.add(hired_agent["id"])
                staff.append(hired_agent)
                hired.append(f"{hired_agent['name']} ({role['name']})")
        project_team = query_one("SELECT * FROM teams WHERE id = ?", (project["team_id"],)) if project["team_id"] else None
        target = query_one("SELECT * FROM teams WHERE lower(name) = lower(?)", (str(args["name"]).strip(),))
        if target is None and project_team is not None:
            target = project_team  # staff the project's aligned team instead of creating a rival one
        if target:
            tid = target["id"]
            member_ids = {r["agent_id"] for r in
                          query("SELECT agent_id FROM team_agents WHERE team_id = ? AND active = 1", (tid,))}
            added = [a["name"] for a in staff if a["id"] not in member_ids]
            for a in staff:
                execute("INSERT OR IGNORE INTO team_agents (team_id, agent_id, role_in_team, active) VALUES (?,?,?,1)",
                        (tid, a["id"], "Member"))
            audit("build_team", "team", tid,
                  f"Added {len(added)} agent(s) to existing team '{target['name']}' — hired {len(hired)}")
            notes = [f"Staffed existing team **{target['name']}** — no duplicate created."]
            if hired:
                notes.append("Hired new agents: " + ", ".join(hired) + ".")
            if added:
                notes.append("Added to the team: " + ", ".join(added) + ".")
            if not added and not hired:
                notes.append("All staffed agents were already on it.")
            if project["team_id"] == tid:
                pass  # already the project's aligned team
            elif not project["team_id"]:
                update("projects", project_id, {"team_id": tid, "updated_at": ts})
                notes.append(f"Aligned **{project['name']}** to this team.")
            else:
                notes.append(f"Note: **{project['name']}** is currently aligned to **{project_team['name']}** — "
                             "say `align this team to the project` to switch.")
            return " ".join(notes)
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

    if command == "hire_agent":
        project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        role_text = re.sub(r"\s*(?:as well|too|please|also|in this project|for this project|"
                           r"to this project|for the project|for our team|here|for me)+\s*$", "",
                           str(args.get("role") or ""), flags=re.IGNORECASE).strip(" ,.")
        role = _resolve_role(role_text)
        if not role:
            return ("Role **" + (role_text or "?") + "** not found. Available roles: "
                    + ", ".join(r["name"] for r in query("SELECT name FROM roles WHERE active=1")) + ".")
        name = str(args.get("name") or "").strip() or None
        if name and query_one("SELECT id FROM agents WHERE lower(name) = lower(?)", (name,)):
            return (f"Agent **{name}** already exists. Pick a different name, "
                    f"or use `add agent {name} to the team` to reuse them (if free).")
        team, err = (None, None)
        if args.get("team") or (project and project["team_id"]):
            team, err = _resolve_team(project, args.get("team"))
        if err:
            return err
        agent = _hire_agent_for_role(role, name)
        audit("hire_agent", "agent", agent["id"],
              f"Hired agent '{agent['name']}' ({role['name']})")
        note = ""
        if team:
            execute("INSERT OR IGNORE INTO team_agents (team_id, agent_id, role_in_team, active) VALUES (?,?,?,1)",
                    (team["id"], agent["id"], "Member"))
            note = f" Joined team **{team['name']}**."
        persona_name = query_one("SELECT name FROM personas WHERE id = ?", (agent["persona_id"],))
        return (f"Hired **{agent['name']}** as **{role['name']}**"
                f" (persona **{persona_name['name'] if persona_name else 'default'}**).{note}")

    if command == "add_agent_to_team":
        project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        agent = _find_agent_strict(args.get("agent"))
        if not agent:
            return (f"Agent **{args.get('agent')}** not found (use the agent's name). Agents: "
                    + ", ".join(a["name"] for a in query("SELECT name FROM agents ORDER BY name")) + ".")
        team, err = _resolve_team(project, args.get("team"))
        if err:
            return err
        if query_one("SELECT 1 FROM team_agents WHERE team_id=? AND agent_id=? AND active=1",
                     (team["id"], agent["id"])):
            return f"**{agent['name']}** is already on team **{team['name']}**."
        others = {p: t for p, t in _agent_project_map(agent["id"]).items() if not project or p != project["name"]}
        if others:
            detail = ", ".join(f"{p} ({t})" for p, t in sorted(others.items()))
            return (f"Cannot add **{agent['name']}** — an agent serves one project only, and they are "
                    f"aligned to {detail}. Hire a new agent via `hire <role>`, or "
                    f"`move agent {agent['name']} to team {team['name']}` to transfer them (only if Idle).")
        execute("INSERT OR IGNORE INTO team_agents (team_id, agent_id, role_in_team, active) VALUES (?,?,?,1)",
                (team["id"], agent["id"], "Member"))
        audit("add_agent_to_team", "team", team["id"],
              f"Added {agent['name']} to team '{team['name']}'")
        return f"**{agent['name']}** added to team **{team['name']}**."

    if command == "add_agent_from_other_team":
        project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        agent = _find_agent_strict(args.get("agent"))
        if not agent:
            return f"Agent **{args.get('agent')}** not found (use the agent's name)."
        team, err = _resolve_team(project, args.get("team"))
        if err:
            return err
        if agent["lifecycle_state"] != "Idle":
            return (f"**{agent['name']}** is currently **{agent['lifecycle_state']}** — only Idle agents "
                    "can be moved between teams. Wait for them to finish their task first.")
        from_ref = str(args.get("from_team") or "").strip()
        memberships = query(
            "SELECT ta.team_id, t.name AS team_name FROM team_agents ta JOIN teams t ON t.id = ta.team_id "
            "WHERE ta.agent_id = ? AND ta.active = 1", (agent["id"],))
        if from_ref:
            memberships = [m for m in memberships if from_ref.lower() in m["team_name"].lower()]
            if not memberships:
                return f"**{agent['name']}** is not on a team matching **{from_ref}**."
        sources = [m for m in memberships if m["team_id"] != team["id"]]
        if not sources:
            return (f"**{agent['name']}** has no other team to move from — "
                    f"use `add agent {agent['name']} to team {team['name']}` instead.")
        left = []
        for m in sources:
            execute("DELETE FROM team_agents WHERE team_id=? AND agent_id=?", (m["team_id"], agent["id"]))
            left.append(m["team_name"])
        execute("INSERT OR IGNORE INTO team_agents (team_id, agent_id, role_in_team, active) VALUES (?,?,?,1)",
                (team["id"], agent["id"], "Member"))
        audit("add_agent_from_other_team", "agent", agent["id"],
              f"Moved {agent['name']} from {', '.join(left)} to team '{team['name']}'")
        return (f"**{agent['name']}** moved from {', '.join(left)} to team **{team['name']}**. "
                "They now serve this team's project only.")

    if command == "remove_agent":
        project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        agent = _find_agent_strict(args.get("agent"))
        if not agent:
            return f"Agent **{args.get('agent')}** not found (use the agent's name)."
        team, err = _resolve_team(project, args.get("team"))
        if err:
            return err
        if not query_one("SELECT 1 FROM team_agents WHERE team_id=? AND agent_id=? AND active=1",
                         (team["id"], agent["id"])):
            return f"**{agent['name']}** is not on team **{team['name']}**."
        execute("DELETE FROM team_agents WHERE team_id=? AND agent_id=?", (team["id"], agent["id"]))
        audit("remove_agent", "team", team["id"],
              f"Removed {agent['name']} from team '{team['name']}'")
        remaining = _agent_project_map(agent["id"])
        status = (" They are now uncommitted (free)." if not remaining else
                  " They still serve: " + ", ".join(sorted(remaining)) + ".")
        return f"**{agent['name']}** removed from team **{team['name']}**.{status}"

    if command == "install_toolchain":
        stack = _normalize_stack(args.get("stack"))
        tc = toolchains.STACKS.get(stack)
        if not tc:
            return f"Unknown toolchain `{stack}` — supported: dotnet, go, node, python."
        ok, _msg = toolchains.toolchain_available(stack)
        if ok:
            return f"**{tc['label']}** is already installed — nothing to do."
        import subprocess as _sp
        cmd = tc["install"].split()
        audit("install_toolchain", "toolchain", stack, f"Running: {' '.join(cmd)}")
        try:
            proc = _sp.run(cmd, capture_output=True, text=True, timeout=1800)
        except _sp.TimeoutExpired:
            return (f"Install of **{tc['label']}** timed out after 30 minutes. "
                    "Run it manually in an admin terminal if winget is waiting for input.")
        tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-5:])
        ok2, _msg2 = toolchains.toolchain_available(stack)
        if ok2:
            audit("install_toolchain", "toolchain", stack, f"Installed {tc['label']}")
            return (f"**{tc['label']}** installed successfully (exit code {proc.returncode}). "
                    "Re-run the affected task — build and QA will now use the real toolchain.")
        return (f"Install of **{tc['label']}** finished with exit code {proc.returncode} "
                f"but the tool is still not on PATH.\nOutput tail:\n```\n{tail[:600]}\n```\n"
                "Restart the backend (to refresh PATH) and try again, or install manually.")

    if command == "add_backlog_item":
        bid = new_id()
        priority = max(1, min(5, _coerce_int(args.get("priority"), 2)))
        points = max(0, _coerce_int(args.get("points"), 3))
        insert("backlog_items", {
            "id": bid, "project_id": project_id, "title": args["title"],
            "description": args.get("description", ""), "priority": priority,
            "acceptance_criteria": args.get("acceptance_criteria", ""),
            "story_points": points, "status": "Backlog", "created_at": ts,
        })
        audit("add_backlog_item", "backlog_item", bid, f"Added backlog item '{args['title']}'")
        return f"Backlog item **{args['title']}** added ({points} pts, priority {priority})."

    if command == "create_sprint":
        name = str(args.get("name") or args.get("title") or args.get("sprint") or "").strip()
        if not name:
            return ("Sprint name is required. Use `create sprint <name>`, or say e.g. "
                    "\"create a sprint for the Weather App\" and I'll draft a full plan for your approval.")
        dup = query_one("SELECT * FROM sprints WHERE project_id = ? AND lower(name) = lower(?)",
                        (project_id, name))
        tasks = args.get("tasks") if isinstance(args.get("tasks"), list) else []
        if dup:
            # Never create a same-named sprint twice — merge tasks into the existing one.
            if not tasks:
                return (f"Sprint **{dup['name']}** already exists in this project ({dup['status']}). "
                        "Say \"start sprint\" to run it, or use a different name to create another.")
            sid = dup["id"]
            summary = f"Sprint **{dup['name']}** already exists — added the new task(s) to it"
        else:
            sid = new_id()
            insert("sprints", {"id": sid, "project_id": project_id, "name": name,
                               "goal": str(args.get("goal") or ""),
                               "capacity": max(0, _coerce_int(args.get("capacity"), 40)),
                               "status": "Planned", "created_at": ts})
            audit("create_sprint", "sprint", sid, f"Created sprint '{name}'")
            summary = f"Sprint **{name}** created (Planned)"
        created, total_pts = [], 0
        for t in tasks:
            if not isinstance(t, dict):
                continue
            t_title = str(t.get("title") or t.get("task") or "").strip()
            if not t_title:
                continue
            pts = max(0, _coerce_int(t.get("points"), 3))
            priority = max(1, min(5, _coerce_int(t.get("priority"), 2)))
            total_pts += pts
            tid = new_id()
            insert("tasks", {"id": tid, "project_id": project_id, "sprint_id": sid,
                             "title": t_title, "description": str(t.get("description") or ""),
                             "acceptance_criteria": str(t.get("acceptance_criteria") or ""),
                             "story_points": pts, "priority": priority,
                             "status": "Todo", "created_at": ts, "updated_at": ts})
            created.append(f"- {t_title} ({pts} pts, priority {priority})")
        if not created:
            return summary + ". Say \"start sprint\" to begin autonomous execution."
        audit("create_sprint_tasks", "sprint", sid, f"Added {len(created)} task(s) to sprint '{name}'")
        return (summary + f" with {len(created)} task(s), {total_pts} pts:\n" + "\n".join(created)
                + "\n\nSay \"start sprint\" to begin autonomous execution.")

    if command == "add_task":
        title = str(args.get("title") or args.get("task") or args.get("name") or "").strip()
        if not title:
            return "Task title is required. Use `add task <title> to sprint <name>`."
        sprint = None
        sprints = query("SELECT * FROM sprints WHERE project_id = ? ORDER BY created_at DESC", (project_id,))
        sprint_ref = str(args.get("sprint") or "").strip()
        if sprint_ref:
            sprint = next((s for s in sprints if s["name"].lower() == sprint_ref.lower()
                           or sprint_ref.lower() in s["name"].lower()), None)
            if not sprint:
                return ("Sprint matching **" + sprint_ref + "** not found. Sprints in this project: "
                        + (", ".join(s["name"] for s in sprints) if sprints else "(none yet)") + ".")
        if not sprint:
            sprint = next((s for s in sprints if s["status"] == "Active"), None)
        if not sprint:
            sprint = next((s for s in sprints if s["status"] == "Planned"), None)
        priority = max(1, min(5, _coerce_int(args.get("priority"), 2)))
        points = max(0, _coerce_int(args.get("points"), 3))
        tid = new_id()
        insert("tasks", {"id": tid, "project_id": project_id,
                         "sprint_id": sprint["id"] if sprint else None,
                         "title": title, "description": args.get("description", ""),
                         "acceptance_criteria": "", "story_points": points, "priority": priority,
                         "status": "Todo", "created_at": ts, "updated_at": ts})
        audit("add_task", "task", tid,
              f"Added task '{title}'" + (f" to sprint '{sprint['name']}'" if sprint else " (no sprint)"))
        where = f" to sprint **{sprint['name']}**" if sprint else " (no sprint found — create one and re-add if needed)"
        return f"Task **{title}** added{where} ({points} pts, priority {priority})."

    if command == "add_urgent_task":
        title = str(args.get("title") or args.get("task") or args.get("name") or "").strip()
        if not title:
            return ("Urgent task title is required. Say e.g. \"add urgent task Fix login 500 error\" "
                    "and I'll draft a plan for your approval.")
        sprint = query_one(
            "SELECT * FROM sprints WHERE project_id = ? AND status = 'Active' ORDER BY created_at LIMIT 1",
            (project_id,))
        if not sprint:
            sprints = query("SELECT name, status FROM sprints WHERE project_id = ? ORDER BY created_at DESC", (project_id,))
            return ("No active sprint to add the urgent task to. "
                    + ("Active sprints: " + ", ".join(s["name"] for s in sprints if s["status"] != "Planned")
                       if any(s["status"] != "Planned" for s in sprints)
                       else "Sprints in this project: " + (", ".join(s["name"] for s in sprints) or "(none yet)"))
                    + ". Say \"start sprint\" to activate one first.")
        members = runtime._team_members(project_id) or []
        devs = [m for m in members if runtime._family_of_role(m["role_name"]) == "dev"]
        idle_devs = [m for m in devs if m["lifecycle_state"] == "Idle"]
        generalists = [m for m in members if runtime._family_of_role(m["role_name"]) is None]
        idle_generalists = [m for m in generalists if m["lifecycle_state"] == "Idle"]
        pick = None
        if idle_devs:
            pick = min(idle_devs, key=lambda m: (runtime._agent_load(m["id"]), m["name"]))
        elif devs:
            pick = min(devs, key=lambda m: (runtime._agent_load(m["id"]), m["name"]))
        elif idle_generalists:
            pick = min(idle_generalists, key=lambda m: (runtime._agent_load(m["id"]), m["name"]))
        if not pick:
            return ("No developer is available for the urgent task — the aligned team has no developer agents "
                    "(or they are all assigned). Hire a developer, free one up, or align a team first.")
        points = max(0, _coerce_int(args.get("points"), 3))
        tid = new_id()
        insert("tasks", {"id": tid, "project_id": project_id, "sprint_id": sprint["id"],
                         "title": title, "description": str(args.get("description") or ""),
                         "acceptance_criteria": str(args.get("acceptance_criteria") or ""),
                         "story_points": points, "priority": 1,
                         "assigned_agent_id": pick["id"], "status": "Ready",
                         "progress": 0, "evidence": "", "blocked_reason": "",
                         "qa_agent_id": None, "rework_count": 0,
                         "created_at": ts, "updated_at": ts})
        db.emit_event(project_id, "task.assigned",
                      {"task_id": tid, "task": title, "agent": pick["name"],
                       "role": pick["role_name"], "urgent": True},
                      task_id=tid, agent_id=pick["id"])
        audit("add_urgent_task", "task", tid,
              f"Urgent task '{title}' created in sprint '{sprint['name']}' and assigned to {pick['name']}")
        slot = " (idle)" if pick["lifecycle_state"] == "Idle" else ""
        return (f"Urgent task **{title}** added to sprint **{sprint['name']}** with priority 1 and assigned "
                f"directly to **{pick['name']}** ({pick['role_name']}){slot}. "
                "It runs next — no further input needed unless QA or execution blocks it.")

    if command == "align_team":
        team = None
        team_ref = str(args.get("team") or "").strip()
        if team_ref:
            teams = query("SELECT * FROM teams ORDER BY created_at DESC")
            team = next((t for t in teams if t["name"].lower() == team_ref.lower()
                         or team_ref.lower() in t["name"].lower()), None)
            if not team:
                return ("Team matching **" + team_ref + "** not found. Teams: "
                        + ", ".join(t["name"] for t in teams) + ".")
        else:
            aligned_ids = {r["team_id"] for r in query("SELECT team_id FROM projects WHERE team_id IS NOT NULL")}
            team = query_one("SELECT * FROM teams ORDER BY created_at DESC")
            for t in query("SELECT * FROM teams ORDER BY created_at DESC"):
                if t["id"] not in aligned_ids:
                    team = t
                    break
        if not team:
            return "No teams exist yet. Build or create one first."
        project = query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
        if project["team_id"] == team["id"]:
            return f"Team **{team['name']}** is already aligned to **{project['name']}**."
        prev = query_one("SELECT name FROM teams WHERE id = ?", (project["team_id"],)) if project["team_id"] else None
        update("projects", project_id, {"team_id": team["id"], "updated_at": ts})
        audit("align_team", "project", project_id,
              f"Aligned project '{project['name']}' to team '{team['name']}'")
        note = f" (previous team **{prev['name']}** stays active but unaligned)" if prev else ""
        return f"**{project['name']}** is now aligned to team **{team['name']}**.{note}"

    if command == "assign_task":
        agent_ref = str(args.get("agent") or "").strip().strip(".!?;,")
        # Compound request: "assign X to Y and start execution".
        auto_start = False
        tail = re.search(r"\s+(?:and|then)\s+(?:start|run|execute)\b.*$", agent_ref, re.IGNORECASE)
        if tail:
            agent_ref = agent_ref[:tail.start()].strip()
            auto_start = True
        task = _find_task(project_id, args.get("task", ""))
        if not task:
            titles = [r["title"] for r in query(
                "SELECT title FROM tasks WHERE project_id = ? ORDER BY created_at DESC LIMIT 8", (project_id,))]
            return (f"Task matching **{args.get('task')}** not found. Current tasks: "
                    + ("; ".join(titles) if titles else "(none yet)") + ".")
        agent = _find_agent(agent_ref)
        if not agent and agent_ref.lower().strip(".!?;, ") in _GENERIC_AGENT_REFS:
            agent = _pick_team_member(project_id, task)
        if not agent:
            names = [r["name"] for r in query("SELECT name FROM agents ORDER BY name LIMIT 8")]
            return (f"Agent **{agent_ref}** not found. Available agents: "
                    + ("; ".join(names) if names else "(none yet)")
                    + ". You can also say \"a team member\" to auto-pick one by role.")
        agent_role = query_one(
            "SELECT r.name AS role_name FROM agents a JOIN roles r ON r.id = a.role_id WHERE a.id = ?",
            (agent["id"],))
        mismatch = ""
        if agent_role and task["status"] not in ("Waiting QA",):
            family = runtime._family_for_task(task)
            agent_family = runtime._family_of_role(agent_role["role_name"] or "")
            if family and agent_family and family != agent_family:
                mismatch = (f" Note: **{agent['name']}** is a {agent_role['role_name']} and this looks like a "
                            f"{runtime._FAMILY_LABEL.get(family, family)} task — QA will verify regardless.")
        update("tasks", task["id"], {"assigned_agent_id": agent["id"], "updated_at": ts})
        audit("assign_task", "task", task["id"], f"Assigned '{task['title']}' to {agent['name']}")
        reply = f"Task **{task['title']}** assigned to **{agent['name']}**.{mismatch}"
        if auto_start:
            result = runtime.start_execution(project_id, task["id"])
            if "error" in result:
                reply += f" Could not start execution: {result['error']}"
            else:
                audit("start_task", "task", task["id"], f"Auto-started '{task['title']}' after assignment")
                reply += " Execution started — watch the Team and Activity panels for live progress."
        return reply

    if command == "start_task":
        task = _find_task(project_id, args.get("task", ""))
        if not task:
            return f"Task matching **{args.get('task')}** not found."
        result = runtime.start_execution(project_id, task["id"])
        if "error" in result:
            return f"Cannot start **{task['title']}**: {result['error']}"
        return f"Execution started for **{task['title']}** — watch the Team and Activity panels for live progress."

    if command == "start_sprint":
        result = runtime.start_sprint_execution(project_id, args.get("sprint"))
        if "error" in result:
            return f"Cannot start sprint execution: {result['error']}"
        audit("start_sprint_execution", "project", project_id,
              f"Sprint execution started autonomously ({result['sprint']})")
        assigned = result.get("auto_assigned") or 0
        note = f" Auto-assigned {assigned} unassigned task(s) to team members by role." if assigned else ""
        return (f"Sprint **{result['sprint']}** started in autonomous mode — tasks are assigned "
                f"and executed in dependency order without further input.{note} "
                "I'll only surface for help if something gets blocked.")

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

    if command == "enable_po":
        result = po.set_po_enabled(project_id, True)
        if "error" in result:
            return f"Cannot enable the Product Owner agent: {result['error']}"
        msg = (f"**{result['agent']}** (Product Owner) now acts with your full authority on this "
               "project: requirements, backlog, sprint planning, task assignment and unblocking "
               "all run autonomously. Every decision is logged to the audit trail.")
        if result.get("sprint_started"):
            msg += f" Sprint **{result['sprint_started']}** is running under PO control."
        msg += " Say `disable product owner` (or flip the toggle) at any time to take back control."
        return msg

    if command == "disable_po":
        result = po.set_po_enabled(project_id, False)
        if "error" in result:
            return f"Cannot disable the Product Owner agent: {result['error']}"
        return ("Product Owner authority disabled — control is back with you. All existing tasks "
                "remain open exactly as they are; say `stop sprint execution` if you also want the "
                "run halted.")

    return f"Command `{command}` is not implemented."


# ------------------------- intent parsing -------------------------

INTENTS = [
    ("help", r"^\s*(help|commands|what can you do)\b"),
    ("status", r"^\s*(status|summary|project status|how are we doing)\b"),
    ("confirm", r"^\s*(confirm|yes|approve|do it|go ahead)\b"),
    ("cancel_pending", r"^\s*(cancel that|discard|no thanks?|nevermind|never mind)\b"),
    ("show_pending", r"^\s*(?:show|share|display|see)\s+(?:me\s+)?(?:the\s+)?(?:proposed|purposed|drafted|pending|current)?\s*(?:sprint\s+)?(?:plan|proposal|sprint(?:\s+plan)?)\s*$"),
    ("show_pending", r"^\s*what(?:'s| is)?\s+(?:the\s+)?(?:proposed|purposed|drafted|pending)\s+(?:sprint|plan)\b"),
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
    ("add_urgent_task", r"add (?:an? )?urgent task\s+(?P<title>.+?)(?:\s+points?\s+(?P<points>\d+))?\s*$"),
    ("add_urgent_task", r"(?:urgent|asap)(?:ly)?(?:\s+task)?\s*[:\-]\s*(?P<title>.+?)\s*$"),
    ("add_task", r"add (?:a )?task\s+(?P<title>.+?)(?:\s+to\s+(?:the\s+)?(?:sprint\s+)?(?P<sprint>.+?))?(?:\s+points?\s+(?P<points>\d+))?(?:\s+priority\s+(?P<priority>\d+))?\s*$"),
    ("create_sprint", r"create (?:a )?sprint\s+(?P<name>.+?)\s*$"),
    ("hire_agent", r"hire (?:a |an |one )?(?:new )?(?P<role>.+?)(?:\s+named\s+(?P<name>.+?))?(?:\s+(?:to|into|for)\s+(?:the\s+)?(?:team\s+)?(?P<team>.+?))?\s*$"),
    ("add_agent_to_team", r"add (?:the )?agent\s+(?P<agent>.+?)(?:\s+(?:to|into)\s+(?:the\s+)?(?:team\s+)?(?P<team>.+?))?\s*$"),
    ("add_agent_from_other_team", r"(?:move|transfer)\s+(?:the )?agent\s+(?P<agent>.+?)(?:\s+from\s+(?:the\s+)?(?:team\s+)?(?P<from_team>.+?))?(?:\s+(?:to|into)\s+(?:the\s+)?(?:team\s+)?(?P<team>.+?))?\s*$"),
    ("remove_agent", r"remove (?:the )?agent\s+(?P<agent>.+?)(?:\s+from\s+(?:the\s+)?(?:team\s+)?(?P<team>.+?))?\s*$"),
    ("install_toolchain", r"install\s+(?:the\s+)?(?P<stack>dotnet|\.net|net|go|golang|node(?:\.?js)?|npm|python|wpf)(?:\s+(?:toolchain|sdk|runtime|framework|environment))?\s*$"),
    ("align_team", r"align(?:ed)?\s+(?:it|that team|the new team|the team)?\s*(?:to|in|with)?\s*(?:this\s+)?project\s*$"),
    ("align_team", r"(?:align|link|attach|assign)\s+(?:the\s+)?team\s+(?P<team>.+?)\s+(?:to|with)\s+(?:this\s+)?project\s*$"),
    ("assign_task", r"assign(?: task)?\s+(?P<task>.+?)\s+to\s+(?P<agent>.+?)\s*$"),
    ("start_task", r"(?:start|run|execute)\s+(?:the )?task\s+(?P<task>.+?)\s*$"),
    ("start_sprint", r"start\s+(?:the )?sprint\s+(?!execution\b)(?:number\s+)?(?P<sprint>.+?)\s*$"),
    ("start_sprint", r"start\s+(?:the )?sprint(?: execution)?\s*$"),
    ("stop_sprint", r"stop\s+(?:the )?sprint(?: execution)?\s*$"),
    ("pause_execution", r"pause\s+(?:the )?execution\s*$"),
    ("resume_execution", r"resume\s+(?:the )?execution\s*$"),
    ("cancel_execution", r"cancel\s+(?:the )?execution\s*$"),
    ("retry_failed", r"retry\s+(?:the )?(?:last )?(?:failed )?(?:task|execution)\s*$"),
    ("enable_po", r"^\s*enable\s+(?:the\s+)?product\s+owner(?:\s+agent(?:\s+mode)?)?\s*$"),
    ("disable_po", r"^\s*disable\s+(?:the\s+)?product\s+owner(?:\s+agent(?:\s+mode)?)?\s*$"),
]

SENSITIVE = {"create_gateway", "create_agent", "create_team", "build_team",
             "hire_agent", "add_agent_from_other_team"}

# Commands that are never executed directly: a full plan is shown for approval
# and only `confirm` creates anything.
PROPOSAL_COMMANDS = {"create_sprint", "add_urgent_task", "install_toolchain"}


_STACK_ALIASES = {
    "dotnet": "dotnet", ".net": "dotnet", "net": "dotnet", "wpf": "dotnet",
    "go": "go", "golang": "go",
    "node": "node", "nodejs": "node", "node.js": "node", "npm": "node",
    "python": "python",
}


def _normalize_stack(raw) -> str:
    return _STACK_ALIASES.get(str(raw or "").strip().lower(), "")


def _sprint_plan_name(args) -> str:
    return str(args.get("name") or args.get("title") or args.get("sprint") or "").strip()


def _sprint_proposal_text(args) -> str:
    name = _sprint_plan_name(args) or "(unnamed)"
    lines = [f"- **Name**: {name}"]
    if str(args.get("goal") or "").strip():
        lines.append(f"- **Goal**: {args['goal']}")
    if args.get("capacity") not in (None, ""):
        lines.append(f"- **Capacity**: {max(0, _coerce_int(args.get('capacity'), 40))} pts")
    tasks = args.get("tasks") if isinstance(args.get("tasks"), list) else []
    if tasks:
        lines.append("- **Tasks**:")
        idx = 0
        for t in tasks:
            t_title = str(t.get("title") or t.get("task") or "").strip() if isinstance(t, dict) else str(t).strip()
            if not t_title:
                continue
            idx += 1
            pts = _coerce_int(t.get("points"), 3) if isinstance(t, dict) else 3
            pri = _coerce_int(t.get("priority"), 2) if isinstance(t, dict) else 2
            desc = str(t.get("description") or "").strip() if isinstance(t, dict) else ""
            line = f"  {idx}. **{t_title}** — {pts} pts, priority {pri}"
            if desc:
                line += f" — {desc[:120]}"
            lines.append(line)
        if idx:
            lines.append(f"  Total: {sum(_coerce_int(t.get('points'), 3) for t in tasks if isinstance(t, dict))} pts across {idx} task(s)")
    return "\n".join(lines)


def _urgent_proposal_text(args) -> str:
    lines = [f"- **Title**: {str(args.get('title') or '(untitled)').strip()}",
             "- **Priority**: 1 (urgent) — jumps to the front of the sprint",
             "- **Sprint**: the current active sprint",
             "- **Assignment**: directly to an idle developer on the aligned team"]
    if str(args.get("description") or "").strip():
        lines.append(f"- **Description**: {args['description']}")
    if str(args.get("acceptance_criteria") or "").strip():
        lines.append(f"- **Acceptance criteria**: {args['acceptance_criteria']}")
    if args.get("points") not in (None, ""):
        lines.append(f"- **Points**: {max(0, _coerce_int(args.get('points'), 3))}")
    return "\n".join(lines)


def _install_proposal_text(args) -> str:
    stack = _normalize_stack(args.get("stack"))
    tc = toolchains.STACKS.get(stack)
    if not tc:
        return f"- **Unknown toolchain**: `{stack}` — supported: dotnet, go, node, python"
    lines = [f"- **Toolchain**: {tc['label']} (`{stack}`)",
             f"- **Action**: run `{tc['install']}` on this machine",
             "- **Requires**: admin rights and network; the install can take several minutes"]
    if not str(tc.get("install") or "").strip():
        return f"- **Toolchain**: {tc['label']} — always available with the backend, nothing to install"
    lines.append("- Until it is installed, build/QA keeps reporting **toolchain unavailable** for that stack (never a fake pass).")
    return "\n".join(lines)


def _proposal_text(command: str, args) -> str:
    if command == "create_sprint":
        return _sprint_proposal_text(args)
    if command == "add_urgent_task":
        return _urgent_proposal_text(args)
    if command == "install_toolchain":
        return _install_proposal_text(args)
    return "\n".join(f"- **{k}**: {v}" for k, v in (args or {}).items() if k != "api_key")

# Static quick replies for the deterministic (typed) command path.
SUGGESTIONS = {
    "status": ["Start sprint execution", "What are the agents doing?", "Create a sprint"],
    "create_role": ["Create persona for this role", "Create agent with this role", "Status"],
    "create_persona": ["Create agent with this persona", "Show agent memory", "Status"],
    "create_skill": ["Attach skill to a role", "Create another skill", "Status"],
    "create_agent": ["Build a team with this agent", "Status", "Create another agent"],
    "create_team": ["Align this team to the project", "Add backlog item", "Status"],
    "build_team": ["Align this team to the project", "Add backlog item", "Status"],
    "hire_agent": ["Assign task to the new agent", "Status", "Build a team"],
    "add_agent_to_team": ["Assign task to an agent", "Status", "Create sprint"],
    "add_agent_from_other_team": ["Assign task to an agent", "Status", "Build a team"],
    "remove_agent": ["Hire an agent", "Build a team", "Status"],
    "add_backlog_item": ["Create a sprint for these items", "Assign task to an agent", "Status"],
    "add_task": ["Create a sprint", "Assign task to an agent", "Start sprint execution"],
    "add_urgent_task": ["Start sprint execution", "Status"],
    "create_sprint": ["Add backlog item", "Start sprint execution", "Status"],
    "align_team": ["Status", "Assign task to an agent", "Start sprint execution"],
    "assign_task": ["Start task", "Start sprint execution", "Agent status"],
    "start_task": ["Status", "Pause execution", "What are the agents doing?"],
    "start_sprint": ["Status", "Pause execution", "Stop sprint execution"],
    "stop_sprint": ["Status", "Retry last failed task", "Resume execution"],
    "pause_execution": ["Resume execution", "Status", "Cancel execution"],
    "resume_execution": ["Status", "Pause execution", "What are the agents doing?"],
    "cancel_execution": ["Status", "Retry last failed task", "Create sprint"],
    "retry_failed": ["Status", "Pause execution", "What are the agents doing?"],
    "enable_po": ["Status", "Disable product owner", "Chat with Product Owner"],
    "disable_po": ["Status", "Enable product owner", "Start sprint execution"],
    "create_gateway": ["Test gateway", "Status", "Help"],
    "test_gateway": ["Create agent", "Status", "Help"],
    "install_toolchain": ["Status", "Help"],
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
    po_state = po.po_status(project_id)
    if po_state["has_po"]:
        lines.append(f"Product Owner agent: {po_state['agent_name']} — authority "
                     + ("ENABLED (running the project autonomously)" if po_state["po_enabled"]
                       else "disabled (you make the decisions)"))
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
- hire_agent: {role, name?, team?} — creates a NEW agent for a role (persona + model binding), optionally joining a team (default: the project's aligned team)
- add_agent_to_team: {agent, team?} — adds a FREE agent by name to a team (default: the project's aligned team); refused if the agent serves another project
- add_agent_from_other_team: {agent, team?, from_team?} — moves an IDLE agent from their other team into this team; they leave the old team
- remove_agent: {agent, team?} — removes an agent from a team (default: the project's aligned team)
- add_backlog_item: {title, points?, priority?} — for the product backlog
- add_task: {title, sprint?, points?, priority?} — creates ONE task IN a sprint; use this (not add_backlog_item) when the user wants a single item added to a sprint
- add_urgent_task: {title, description?, acceptance_criteria?, points?} — for urgent/ASAP/production-fix requests. Draft a concrete plan (title, short description, acceptance criteria, small point estimate); after user approval it is created with priority 1 in the CURRENT active sprint and assigned directly to an idle developer, running next without further input.
- create_sprint: {name, goal?, capacity?, tasks?: [{"title", "description?", "points?", "priority?"}]} — when the user asks to create a sprint, ALWAYS draft a complete end-to-end plan: infer a sprint name, goal and capacity, and 3-6 concrete tasks from the project goal and backlog (points 1-8, priority 1-3). Nothing is created until the user approves the plan; add_task is only for a single task, not sprint planning.
- align_team: {team?} — aligns a team to the CURRENT project; team name optional (defaults to the newest team not aligned to any project). Use when the user says things like "align it in this project" after building a team.
- assign_task: {task, agent}
- start_task: {task}
- start_sprint: {sprint?: "<sprint number, name, or id>"} — "start sprint 2" passes sprint="2". If the target sprint has no tasks yet, the next-phase task plan is drafted automatically from the project goal, then the sprint starts autonomously and tasks are assigned by role. Never offer to assign tasks manually after starting; the run only surfaces for real blockers.
- stop_sprint: {}
- pause_execution: {}
- resume_execution: {}
- cancel_execution: {}
- retry_failed: {}
- enable_po: {} — enable the project's Product Owner agent to act autonomously with the user's full authority (requires a Product Owner agent on the team; if missing, offer `hire_agent` with role "Product Owner" first)
- disable_po: {} — hand control back to the user; existing tasks remain open
- install_toolchain: {stack: "dotnet"|"go"|"node"|"python"} — when a task or QA report says a build toolchain is unavailable, offer to install it (e.g. `dotnet` for WPF/.NET, `go`, `node`/npm). A plan is proposed and the user must confirm before anything runs on the machine.

Rules:
- Pick a command only when the user clearly wants that action; otherwise set command to null and just answer.
- ALIGNMENT IS FACT: each agent line in the context states which project(s) it is aligned to via its team, or "uncommitted (free)". Never claim an aligned agent is free; never assign an agent aligned to a DIFFERENT project. Teams also show their aligned project and full member roster.
- When the user asks to build/staff a team (for this or any project), use build_team with the needed role names. build_team automatically reuses only uncommitted agents and hires new agents for roles with nobody free. Only use create_team if the user explicitly names agents AND every named agent is uncommitted (free) or aligned to the CURRENT project.
- ONE AGENT SERVES ONE PROJECT: an agent aligned (via its team) to a project must never be added to another project's team. To staff another project: use build_team or hire_agent (new agent), reuse only free agents, or add_agent_from_other_team to transfer an IDLE agent (they leave their old team). Never add every known agent to a team — staff exactly the roles requested.
- Reference tasks by the closest title in the context (partial titles are fine, e.g. "user authentication" for "User authentication API"). Resolve relative references to the concrete title: "the first task" -> the first task listed in the context, "the last/newest task" -> the most recent, "the next task" -> the first not-yet-done task.
- Reference agents by exact name, or by role when the user means "any/one <role>" (e.g. agent "a senior developer" if a developer role exists); the resolver picks the best idle agent for that role. If the user says "a team member"/"the agent"/"anyone" without naming anyone, pick a specific IDLE member of the project's aligned team and pass their exact name.
- "assign X to Y and start execution" -> use assign_task; the resolver handles starting the run.
- If the user asks about status or progress, set command to null and summarize from the context.
- Your reply is sent BEFORE the command runs; the real execution result is appended after it. Never claim an action already succeeded and never pre-announce outcomes (e.g. don't say "is now running" or "has been created") — describe what you are going to do.
- start_sprint is fully autonomous: it auto-activates a planned sprint and auto-assigns tasks to team members by role. Never offer to assign tasks manually after starting; the run only surfaces for real blockers.
- Workflow: tasks are matched to the agent's role family (dev/architecture/QA/BA/DevOps/design) — a development task never auto-assigns to QA or BA when a developer exists. After a developer finishes, the task moves to "Waiting QA" and a QA-role agent verifies it; QA pass -> Done, QA rejection -> "Rework" back to the developer with a defect summary; two rejections escalate to the user.
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
    start = t.find("{")
    if start == -1:
        return None
    end = t.rfind("}")
    chunk = t[start:end + 1] if end > start else t[start:]  # no closing brace yet = truncated
    candidates = [chunk]
    pos = len(chunk)
    for _ in range(16):
        pos = max(chunk.rfind(c, 0, pos) for c in ('"', ",", "{", "[", "}", "]"))
        if pos <= 1:
            break
        candidates.append(chunk[:pos + 1])
        pos -= 1
    for candidate in candidates:
        for fixed in (candidate, _repair_json(candidate)):
            if not fixed:
                continue
            try:
                data = json.loads(fixed)
            except ValueError:
                continue
            return data if isinstance(data, dict) else None
    return None


def _repair_json(chunk: str) -> str | None:
    """Salvage truncated JSON (e.g. hit max_tokens) by closing open strings
    and containers. Returns None when the chunk cannot be repaired."""
    stack = []
    in_str = False
    esc = False
    for ch in chunk:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    if in_str:
        chunk = chunk.rstrip()
        while chunk.endswith("\\"):
            chunk = chunk[:-1]
        chunk += '"'
    chunk = chunk.rstrip()
    while chunk.endswith((",", ":")):
        chunk = chunk[:-1].rstrip()
    if not stack:
        return None
    return chunk + "".join("}" if c == "{" else "]" for c in reversed(stack))


def _call_llm(gw, model, system_prompt: str, user_text: str, max_tokens: int = 700) -> str:
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
    parsed = None
    last_exc = None
    for attempt in range(2):
        try:
            raw = _call_llm(gw, model, AI_SYSTEM_PROMPT + _context_brief(project_id), text, max_tokens=1500)
            parsed = _extract_json(raw)
            if parsed and ("reply" in parsed or parsed.get("command")):
                last_exc = None
                break
            raise RuntimeError("The model did not return a valid response.")
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            last_exc = exc
    if parsed is None or not (parsed.get("reply") or parsed.get("command")):
        reply = (f"AI routing failed — falling back to typed commands. ({last_exc})\n\n"
                 "Try `help` for the command catalog.")
        _save_message(conversation_id, "assistant", reply)
        return {"reply": reply, "suggestions": ["Status", "Help"]}

    reply = str(parsed.get("reply") or "").strip()
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
        if name in ("create_sprint",):
            if not _sprint_plan_name(args):
                for alias in ("title", "sprint"):
                    if str(args.get(alias) or "").strip():
                        args["name"] = args[alias]
                        break
            if isinstance(args.get("tasks"), str):
                args["tasks"] = [{"title": t.strip()} for t in re.split(r",| and ", args["tasks"]) if t.strip()]
        if name in ("add_urgent_task",):
            if not str(args.get("title") or "").strip():
                for alias in ("task", "name", "description"):
                    if str(args.get(alias) or "").strip():
                        args["title"] = args[alias]
                        break
        if name in SENSITIVE or name in PROPOSAL_COMMANDS:
            pid = _queue_pending(conversation_id, name, args, name)
            if name in PROPOSAL_COMMANDS:
                labels = {"create_sprint": "Sprint plan for your approval",
                          "add_urgent_task": "Urgent task plan for your approval",
                          "install_toolchain": "Toolchain install for your approval"}
                hints = {
                    "create_sprint": "Reply `confirm` to create the sprint and its tasks, "
                                     "or `cancel that` to discard.",
                    "add_urgent_task": "Reply `confirm` to add it to the current sprint and assign an idle developer, "
                                       "or `cancel that` to discard.",
                    "install_toolchain": "Reply `confirm` to run the install on this machine, "
                                         "or `cancel that` to skip it.",
                }
                label = labels.get(name, "Plan for your approval")
                confirm_hint = hints.get(name, "Reply `confirm` to execute or `cancel that` to discard.")
                reply = (f"{reply}\n\n**{label}** (`{name}`):\n"
                         + _proposal_text(name, args) + "\n\n" + confirm_hint)
            else:
                reply = (f"{reply}\n\n**Confirmation required** to run `{name}` with:\n"
                         + "\n".join(f"- **{k}**: {v}" for k, v in args.items() if k != "api_key")
                         + "\n\nReply `confirm` to execute or `cancel that` to discard.")
            result.update({"reply": reply, "needs_confirmation": True,
                           "pending_command": pid, "proposal": args})
            _save_message(conversation_id, "assistant", reply, meta=json.dumps({"pending_command": pid}))
            return result
        try:
            outcome = _execute_command(project_id, name, args)
        except Exception as exc:
            outcome = (f"⚠️ The command `{name}` failed with: {exc}. "
                       "Try rephrasing, or use the typed command (e.g. `help`).")
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
        # Match against the ORIGINAL text (case-insensitively) so captured
        # titles keep their casing instead of being lowercased.
        m = re.match(pattern, text, re.IGNORECASE)
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
        try:
            reply = _execute_command(project_id, pending["command"], args)
        except Exception as exc:  # keep pending so the user can retry with confirm
            reply = (f"The command failed with: {exc}\n\n"
                     "The request is still pending — reply `confirm` to retry or `cancel that` to discard.")
            _save_message(conversation_id, "assistant", reply)
            return {"reply": reply, "needs_confirmation": True, "pending_command": pending["id"],
                    "suggestions": ["confirm", "cancel that"]}
        execute("DELETE FROM pending_commands WHERE id = ?", (pending["id"],))
        _save_message(conversation_id, "assistant", reply)
        return {"reply": reply, "suggestions": SUGGESTIONS.get(pending["command"], ["Status", "Help"])}

    if intent == "cancel_pending":
        execute("DELETE FROM pending_commands WHERE conversation_id = ?", (conversation_id,))
        reply = "Pending command discarded."
        _save_message(conversation_id, "assistant", reply)
        return {"reply": reply, "suggestions": ["Status", "Help"]}

    if intent == "show_pending":
        pending = query_one(
            "SELECT * FROM pending_commands WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 1",
            (conversation_id,))
        if not pending:
            reply = ("There is no pending plan right now. Say e.g. \"create a sprint for this project\" "
                     "and I'll draft one for your approval.")
            _save_message(conversation_id, "assistant", reply)
            return {"reply": reply, "suggestions": ["Create a sprint", "Status"]}
        args = json.loads(pending["args_json"])
        if pending["command"] == "create_sprint":
            reply = ("**Sprint plan for your approval**:\n" + _proposal_text(pending["command"], args)
                     + "\n\nReply `confirm` to create the sprint and its tasks, or `cancel that` to discard.")
            _save_message(conversation_id, "assistant", reply, meta=json.dumps({"pending_command": pending["id"]}))
            return {"reply": reply, "needs_confirmation": True, "pending_command": pending["id"],
                    "proposal": args, "suggestions": ["confirm", "cancel that"]}
        if pending["command"] in PROPOSAL_COMMANDS:
            reply = ("**Urgent task plan for your approval**:\n" + _proposal_text(pending["command"], args)
                     + "\n\nReply `confirm` to add it to the current sprint and assign an idle developer, "
                       "or `cancel that` to discard.")
            _save_message(conversation_id, "assistant", reply, meta=json.dumps({"pending_command": pending["id"]}))
            return {"reply": reply, "needs_confirmation": True, "pending_command": pending["id"],
                    "proposal": args, "suggestions": ["confirm", "cancel that"]}
        reply = (f"Pending command `{pending['command']}` is awaiting confirmation. "
                 "Reply `confirm` to execute or `cancel that` to discard.")
        _save_message(conversation_id, "assistant", reply)
        return {"reply": reply, "needs_confirmation": True, "pending_command": pending["id"],
                "suggestions": ["confirm", "cancel that"]}

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
    if intent == "add_urgent_task" and "points" in args:
        args["points"] = int(args["points"])
    # The raw key is kept in args so it can be stored encrypted on execution;
    # it is never echoed back into the chat.

    if intent in SENSITIVE or intent in PROPOSAL_COMMANDS:
        pid = _queue_pending(conversation_id, intent, args, intent)
        if intent in PROPOSAL_COMMANDS:
            labels = {"create_sprint": "Sprint plan for your approval",
                      "add_urgent_task": "Urgent task plan for your approval",
                      "install_toolchain": "Toolchain install for your approval"}
            hints = {
                "create_sprint": "Reply `confirm` to create the sprint and its tasks, "
                                 "or `cancel that` to discard.",
                "add_urgent_task": "Reply `confirm` to add it to the current sprint and assign an idle developer, "
                                   "or `cancel that` to discard.",
                "install_toolchain": "Reply `confirm` to run the install on this machine, "
                                     "or `cancel that` to skip it.",
            }
            label = labels.get(intent, "Plan for your approval")
            confirm_hint = hints.get(intent, "Reply `confirm` to execute or `cancel that` to discard.")
            reply = (f"**{label}**:\n" + _proposal_text(intent, args) + "\n\n" + confirm_hint)
        else:
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
