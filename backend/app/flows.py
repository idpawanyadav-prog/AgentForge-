"""Project Flow templates — reusable SDLC definitions.

A flow pins the per-task stage chain (``dev`` -> reviews -> ``qa`` ->
``approve``), the team composition that runs it, and two project-level
toggles (the BAS/PDS/TS docs gate and PO autonomy). Projects reference one
flow via ``projects.flow_id``; the runtime walks ``stage_order`` instead of
the old hardcoded chain, so the same engine executes any flow shape.
"""
import json

from . import db
from .db import audit, execute, insert, new_id, now, query, query_one, update

# key -> (kind, role that owns the stage). Kinds: implement / review / test /
# approve. Roles are fixed per stage kind so the existing review prompts,
# QA tooling and approval chat commands keep working for any flow.
STAGE_DEFS = {
    "dev": ("implement", "Senior Developer"),
    "sa": ("review", "Solution Architect"),
    "ba": ("review", "Business Analyst"),
    "qa": ("test", "QA Engineer"),
    "approve": ("approve", "Product Owner"),
}
STAGE_LABEL = {"dev": "Developer", "sa": "SA review", "ba": "BA review",
               "qa": "QA test", "approve": "Approval"}
DEFAULT_STAGES = ["dev", "sa", "ba", "qa"]

_NAME_POOL = ["Ava", "Rex", "Nova", "Iris", "Bolt", "Mia", "Leo", "Ash",
              "Qwen", "Kai", "Zoe", "Max", "Remy", "Suki", "Omar", "Lena"]


class FlowError(Exception):
    """User-facing flow definition problem (rendered as 400/409)."""


# ------------------------------------------------------------------ storage

def _parse(row) -> dict:
    out = dict(row)
    out["stages"] = json.loads(row["stages_json"] or "[]")
    out["team"] = json.loads(row["team_json"] or "[]")
    out["design"] = json.loads(row["design_json"] or "[]") if "design_json" in row.keys() \
        else []
    out.pop("stages_json", None)
    out.pop("team_json", None)
    out.pop("design_json", None)
    return out


def list_flows() -> list:
    rows = query("SELECT * FROM project_flows ORDER BY is_default DESC, created_at")
    out = []
    for r in rows:
        f = _parse(r)
        f["projects_using"] = query_one(
            "SELECT COUNT(*) AS n FROM projects WHERE flow_id = ?", (r["id"],))["n"]
        out.append(f)
    return out


def get_flow(flow_id: str) -> dict | None:
    row = query_one("SELECT * FROM project_flows WHERE id = ?", (flow_id,))
    return _parse(row) if row else None


def default_flow() -> dict:
    row = query_one("SELECT * FROM project_flows WHERE is_default = 1 "
                    "ORDER BY is_builtin DESC, created_at LIMIT 1")
    if row:
        return _parse(row)
    return {"id": None, "name": "Built-in default", "description": "",
            "stages": list(DEFAULT_STAGES), "team": [], "design": [], "docs_gate": 0,
            "po_enabled": 0, "is_default": 0, "is_builtin": 1}


def flow_for_project(project) -> dict:
    """The flow a project runs: its chosen flow, else the chain implied by
    its legacy review-gate flags (projects created before flows existed)."""
    if project and project.get("flow_id"):
        f = get_flow(project["flow_id"])
        if f:
            return f
        return default_flow()
    if project:
        stages = ["dev"]
        if project.get("sa_review_enabled"):
            stages.append("sa")
        if project.get("ba_review_enabled"):
            stages.append("ba")
        stages.append("qa")
        return {"id": None, "name": "Project gates", "description": "",
                "stages": stages, "team": [], "design": [], "docs_gate": 0,
                "po_enabled": project.get("po_enabled") or 0,
                "is_default": 0, "is_builtin": 1}
    return default_flow()


def stage_order(flow: dict) -> list:
    stages = [s for s in (flow.get("stages") or []) if s in STAGE_DEFS]
    return stages or list(DEFAULT_STAGES)


_CANONICAL = ["dev", "sa", "ba", "qa", "approve"]


def next_after(flow: dict, stage: str):
    """The stage key that follows ``stage`` in the flow, or None (= Done).

    A stage that the flow does not contain (a manually parked task, or a
    review gate that was switched off after the task entered it) still
    advances canonically: the next flow stage that sits after it in the
    standard dev -> sa -> ba -> qa -> approve order."""
    order = stage_order(flow)
    if stage in order:
        i = order.index(stage)
        return order[i + 1] if i + 1 < len(order) else None
    if stage not in _CANONICAL:
        return None
    tail = [s for s in order
            if _CANONICAL.index(s) > _CANONICAL.index(stage)]
    return tail[0] if tail else None


def stage_enabled(flow: dict, stage: str) -> bool:
    return stage in stage_order(flow)


# --------------------------------------------------------------- validation

def validate(name, stages, team, design=None, *, ignore_id=None) -> list:
    errors = []
    if not (name or "").strip():
        errors.append("Flow name is required.")
    dup = query_one("SELECT id FROM project_flows WHERE lower(name) = lower(?)",
                    ((name or "").strip(),))
    if dup and dup["id"] != ignore_id:
        errors.append(f"A flow named '{name.strip()}' already exists.")
    if not stages:
        errors.append("A flow needs at least the Developer stage.")
    elif stages[0] != "dev":
        errors.append("The Developer stage must come first.")
    elif len(stages) != len(set(stages)):
        errors.append("Each stage may appear only once.")
    elif any(s not in STAGE_DEFS for s in stages):
        errors.append("Unknown stage in the chain.")
    roles = {}
    for entry in team or []:
        role, count = entry.get("role"), entry.get("count", 1)
        if not role or not isinstance(count, int) or count < 1:
            errors.append(f"Team entry '{role or '?'}' needs a count of 1 or more.")
            continue
        if not query_one("SELECT id FROM roles WHERE lower(name) = lower(?)", (role,)):
            errors.append(f"Unknown role '{role}' in the team.")
            continue
        roles[role.lower()] = roles.get(role.lower(), 0) + count
    for s in stages or []:
        kind, role = STAGE_DEFS.get(s, ("", ""))
        if kind in ("implement", "review", "test") and role.lower() not in roles:
            errors.append(f"Stage '{STAGE_LABEL.get(s, s)}' needs at least one "
                          f"{role} on the team.")
    for step in design or []:
        role = (step or {}).get("role")
        if role and role.lower() not in roles:
            seq = (step or {}).get("seq") or (design or []).index(step) + 1
            errors.append(f"Design step {seq} needs at least one {role} on the team.")
    return errors


# --------------------------------------------------------------------- CRUD

def _norm_design(design) -> list:
    """Validate + renumber design steps, turning a DesignError into a
    FlowError so the route renders it as a 400."""
    if not design:
        return []
    from . import design as _design
    try:
        return _design.normalize_steps(design)
    except _design.DesignError as exc:
        raise FlowError(str(exc))


def create_flow(name, description="", stages=None, team=None, docs_gate=0,
                po_enabled=0, is_default=0, design=None) -> dict:
    stages = list(stages or DEFAULT_STAGES)
    design = _norm_design(design)
    errors = validate(name, stages, team, design)
    if errors:
        raise FlowError("; ".join(errors))
    fid = new_id()
    if is_default:
        execute("UPDATE project_flows SET is_default = 0")
    insert("project_flows", {"id": fid, "name": name.strip(),
                             "description": description or "",
                             "stages_json": json.dumps(stages),
                             "team_json": json.dumps(team or []),
                             "design_json": json.dumps(design),
                             "docs_gate": 1 if docs_gate else 0,
                             "po_enabled": 1 if po_enabled else 0,
                             "is_default": 1 if is_default else 0,
                             "is_builtin": 0,
                             "created_at": now(), "updated_at": now()})
    audit("flow_create", "flow", fid, f"Created flow '{name.strip()}' "
          f"({', '.join(stages)})")
    return get_flow(fid)


def update_flow(flow_id, **fields) -> dict:
    flow = get_flow(flow_id)
    if not flow:
        raise FlowError("Flow not found.")
    if flow["is_builtin"] and fields.get("stages") is not None \
            and fields["stages"] != flow["stages"]:
        raise FlowError("The built-in flow's stage chain is fixed; copy it to "
                        "experiment with a different chain.")
    name = fields.get("name", flow["name"])
    stages = fields.get("stages", flow["stages"])
    team = fields.get("team", flow["team"])
    design = _norm_design(fields.get("design", flow.get("design")))
    errors = validate(name, stages, team, design, ignore_id=flow_id)
    if errors:
        raise FlowError("; ".join(errors))
    values = {"name": name.strip(), "description": fields.get("description", flow["description"]),
              "stages_json": json.dumps(list(stages)), "team_json": json.dumps(team or []),
              "design_json": json.dumps(design),
              "docs_gate": 1 if fields.get("docs_gate", flow["docs_gate"]) else 0,
              "po_enabled": 1 if fields.get("po_enabled", flow["po_enabled"]) else 0,
              "updated_at": now()}
    if fields.get("is_default", flow["is_default"]):
        execute("UPDATE project_flows SET is_default = 0")
        values["is_default"] = 1
    update("project_flows", flow_id, values)
    audit("flow_update", "flow", flow_id, f"Updated flow '{values['name']}'")
    return get_flow(flow_id)


def delete_flow(flow_id: str):
    flow = get_flow(flow_id)
    if not flow:
        raise FlowError("Flow not found.")
    if flow["is_builtin"]:
        raise FlowError("The built-in flow cannot be deleted.")
    used = query_one("SELECT COUNT(*) AS n FROM projects WHERE flow_id = ?",
                     (flow_id,))["n"]
    if used:
        raise FlowError(f"{used} project(s) still use this flow — reassign them first.")
    execute("DELETE FROM project_flows WHERE id = ?", (flow_id,))
    audit("flow_delete", "flow", flow_id, f"Deleted flow '{flow['name']}'")


# ------------------------------------------------------------------- teams

def _agent_name() -> str:
    taken = {r["name"] for r in query("SELECT name FROM agents")}
    for candidate in _NAME_POOL:
        if candidate not in taken:
            return candidate
    return f"Agent {len(taken) + 1}"


def _persona_for(role) -> str:
    persona = query_one("SELECT id FROM personas WHERE role_id = ? ORDER BY created_at "
                        "LIMIT 1", (role["id"],))
    if persona:
        return persona["id"]
    pid = new_id()
    insert("personas", {"id": pid, "role_id": role["id"],
                        "name": f"Default {role['name']}",
                        "instructions": "", "constraints_text": "", "version": 1,
                        "created_at": now(), "updated_at": now()})
    return pid


def provision_team(flow: dict, project) -> dict:
    """Create a team matching the flow's team spec and attach it to project."""
    team_spec = flow.get("team") or []
    if not team_spec:
        return {"team_id": None, "created_agents": [], "warnings": [
            "Flow has no team spec — assign a team manually."]}
    binding = None
    if project.get("default_gateway_id") and project.get("default_model_id"):
        binding = db.ensure_model_binding(project["default_gateway_id"],
                                          project["default_model_id"])
    team_id = new_id()
    insert("teams", {"id": team_id, "name": f"{project['name']} Team",
                     "description": f"Provisioned from flow '{flow['name']}'.",
                     "status": "Active", "created_at": now()})
    created = []
    for entry in team_spec:
        role = query_one("SELECT * FROM roles WHERE lower(name) = lower(?)",
                         (entry["role"],))
        if not role:
            continue
        persona_id = _persona_for(role)
        for _ in range(int(entry.get("count", 1))):
            aid = new_id()
            name = _agent_name()
            insert("agents", {"id": aid, "name": name, "role_id": role["id"],
                              "persona_id": persona_id, "model_binding_id": binding,
                              "lifecycle_state": "Idle", "current_activity": "",
                              "created_at": now(), "updated_at": now()})
            execute("INSERT INTO team_agents (team_id, agent_id, role_in_team, active) "
                    "VALUES (?,?,?,1)", (team_id, aid, role["name"]))
            created.append(f"{name} ({role['name']})")
    update("projects", project["id"], {"team_id": team_id, "updated_at": now()})
    audit("flow_provision_team", "team", team_id,
          f"Provisioned {len(created)} agent(s) from flow '{flow['name']}' "
          f"for project '{project['name']}'")
    return {"team_id": team_id, "created_agents": created, "warnings": []}


def team_gaps(flow: dict, team_id: str) -> list:
    """Roles the flow needs for its stages but the chosen team lacks."""
    have = {r["name"].lower() for r in query(
        "SELECT r.name FROM team_agents ta JOIN agents a ON a.id = ta.agent_id "
        "JOIN roles r ON r.id = a.role_id WHERE ta.team_id = ? AND ta.active = 1",
        (team_id,))}
    gaps = []
    for stage in stage_order(flow):
        kind, role = STAGE_DEFS[stage]
        if kind in ("implement", "review", "test") and role.lower() not in have:
            gaps.append(f"{role} (needed for {STAGE_LABEL[stage]})")
    return gaps
