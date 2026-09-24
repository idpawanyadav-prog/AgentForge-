"""Role, persona, skill, instruction, and model-binding routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..db import audit, checksum, execute, insert, new_id, now, query, query_one, update
from ..prompt_lint import lint_prompt
from ..schemas import InstructionUpdate, PersonaUpdate, RoleUpdate, SkillUpdate
from ._util import or_404 as _or_404

router = APIRouter(prefix="/api/v1", tags=["roles"])


class RoleIn(BaseModel):
    name: str
    description: str = ""


class PersonaIn(BaseModel):
    role_id: str = ""
    name: str
    description: str = ""
    instructions: str = ""
    constraints_text: str = ""


class SkillIn(BaseModel):
    name: str
    description: str = ""
    content: str = ""


class InstructionIn(BaseModel):
    filename: str
    description: str = ""
    content: str = ""


class BindingIn(BaseModel):
    role_id: str
    gateway_id: str
    model_id: str


# ------------------ agent memory ------------------

@router.get("/roles/memory")
def roles_memory():
    out = []
    for r in query("SELECT * FROM roles ORDER BY name"):
        ic = query_one("SELECT COUNT(*) AS n FROM instruction_files WHERE role_id=?", (r["id"],))["n"]
        sc = query_one("SELECT COUNT(*) AS n FROM role_skills WHERE role_id=?", (r["id"],))["n"]
        pc = query_one("SELECT COUNT(*) AS n FROM personas WHERE role_id=?", (r["id"],))["n"]
        out.append({**r, "instruction_count": ic, "skill_count": sc, "persona_count": pc})
    return out


@router.get("/roles/{rid}/instructions")
def list_instructions(rid: str):
    return query("SELECT * FROM instruction_files WHERE role_id=? ORDER BY filename", (rid,))


@router.post("/roles/{rid}/instructions")
def create_instruction(rid: str, body: InstructionIn):
    _or_404(query_one("SELECT id FROM roles WHERE id=?", (rid,)), "Role")
    filename = body.filename if body.filename.endswith(".md") else body.filename + ".md"
    if query_one("SELECT id FROM instruction_files WHERE role_id=? AND lower(filename)=lower(?)", (rid, filename)):
        raise HTTPException(409, "An instruction file with that name already exists in this group")
    ts = now()
    iid = new_id()
    insert("instruction_files", {"id": iid, "role_id": rid, "filename": filename,
                                 "description": body.description, "content": body.content,
                                 "version": 1, "created_at": ts, "updated_at": ts})
    _snapshot_instruction_version(iid, 1, filename, body.content, ts)
    audit("create_instruction", "instruction_file", iid, f"Created instruction '{filename}'")
    row = query_one("SELECT * FROM instruction_files WHERE id = ?", (iid,))
    row["lint_warnings"] = lint_prompt(body.content)
    return row


def _snapshot_instruction_version(iid: str, version: int, filename: str,
                                  content: str, ts: str):
    insert("instruction_file_versions", {
        "id": new_id(), "instruction_file_id": iid, "version": version,
        "filename": filename, "content": content,
        "checksum": checksum(content), "created_at": ts})


@router.patch("/instructions/{iid}")
def update_instruction(iid: str, body: InstructionUpdate):
    instr = _or_404(query_one("SELECT * FROM instruction_files WHERE id=?", (iid,)), "Instruction")
    data = body.model_dump(exclude_unset=True)
    allowed = {k: v for k, v in data.items() if k in ("filename", "description", "content")}
    warnings: list[str] = []
    if "content" in allowed and allowed["content"] != instr["content"]:
        allowed["version"] = instr["version"] + 1
        _snapshot_instruction_version(iid, allowed["version"],
                                      allowed.get("filename", instr["filename"]),
                                      allowed["content"], now())
        warnings = lint_prompt(allowed["content"])
    if "active" in data:
        allowed["active"] = 1 if data["active"] in (True, 1, "1", "on") else 0
        if allowed["active"] == 0:
            audit("disable_instruction", "instruction_file", iid,
                  f"Disabled instruction '{instr['filename']}'")
        else:
            audit("enable_instruction", "instruction_file", iid,
                  f"Enabled instruction '{instr['filename']}'")
    allowed["updated_at"] = now()
    update("instruction_files", iid, allowed)
    row = query_one("SELECT * FROM instruction_files WHERE id = ?", (iid,))
    row["lint_warnings"] = warnings
    return row


@router.get("/instructions/{iid}/versions")
def list_instruction_versions(iid: str):
    _or_404(query_one("SELECT id FROM instruction_files WHERE id=?", (iid,)), "Instruction")
    return query("SELECT id, version, filename, checksum, created_at, "
                 "LENGTH(content) AS content_chars FROM instruction_file_versions "
                 "WHERE instruction_file_id=? ORDER BY version DESC", (iid,))


@router.post("/instructions/{iid}/restore")
def restore_instruction_version(iid: str, body: dict):
    """Roll an instruction file back to an earlier version (or to the
    seeded baseline with {"version": 1}). The restore itself becomes a new
    version so history is append-only."""
    instr = _or_404(query_one("SELECT * FROM instruction_files WHERE id=?", (iid,)), "Instruction")
    try:
        version = int(body.get("version"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Provide {\"version\": <int>}")
    snap = query_one("SELECT * FROM instruction_file_versions "
                     "WHERE instruction_file_id=? AND version=?", (iid, version))
    if not snap:
        raise HTTPException(404, f"No stored version {version} for this instruction")
    if snap["content"] == instr["content"]:
        return {**instr, "restored": False, "note": f"Already at version {version} content"}
    new_v = instr["version"] + 1
    ts = now()
    update("instruction_files", iid, {"content": snap["content"], "version": new_v,
                                      "updated_at": ts})
    _snapshot_instruction_version(iid, new_v, instr["filename"], snap["content"], ts)
    audit("restore_instruction", "instruction_file", iid,
          f"Restored to version {version} (now v{new_v})")
    row = query_one("SELECT * FROM instruction_files WHERE id = ?", (iid,))
    return {**row, "restored": True, "restored_from": version}


@router.delete("/instructions/{iid}")
def delete_instruction(iid: str):
    execute("DELETE FROM instruction_file_versions WHERE instruction_file_id=?", (iid,))
    execute("DELETE FROM instruction_files WHERE id=?", (iid,))
    return {"ok": True}


# ------------------ role skills ------------------

@router.get("/roles/{rid}/skills")
def list_role_skills(rid: str):
    return query(
        "SELECT s.* FROM role_skills rs JOIN skills s ON s.id = rs.skill_id "
        "WHERE rs.role_id=? ORDER BY s.name", (rid,))


@router.post("/roles/{rid}/skills/{sid}")
def attach_role_skill(rid: str, sid: str):
    execute("INSERT OR IGNORE INTO role_skills (role_id, skill_id) VALUES (?,?)", (rid, sid))
    return {"ok": True}


@router.delete("/roles/{rid}/skills/{sid}")
def detach_role_skill(rid: str, sid: str):
    execute("DELETE FROM role_skills WHERE role_id=? AND skill_id=?", (rid, sid))
    return {"ok": True}


# ------------------ roles ------------------

@router.get("/roles")
def list_roles(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    total = query_one("SELECT COUNT(*) AS n FROM roles")["n"]
    items = query("SELECT * FROM roles ORDER BY name LIMIT ? OFFSET ?", (limit, offset))
    return {"total": total, "items": items, "limit": limit, "offset": offset}


@router.post("/roles")
def create_role(body: RoleIn):
    if query_one("SELECT id FROM roles WHERE lower(name)=lower(?)", (body.name,)):
        raise HTTPException(409, "Role name already exists")
    rid = new_id()
    ts = now()
    insert("roles", {"id": rid, "name": body.name, "description": body.description,
                     "active": 1, "created_at": ts, "updated_at": ts})
    audit("create_role", "role", rid, f"Created role '{body.name}'")
    return query_one("SELECT * FROM roles WHERE id = ?", (rid,))


@router.patch("/roles/{rid}")
def update_role(rid: str, body: RoleUpdate):
    _or_404(query_one("SELECT id FROM roles WHERE id=?", (rid,)), "Role")
    data = body.model_dump(exclude_unset=True)
    allowed = {k: v for k, v in data.items() if k in ("name", "description", "active")}
    allowed["updated_at"] = now()
    update("roles", rid, allowed)
    return query_one("SELECT * FROM roles WHERE id = ?", (rid,))


@router.delete("/roles/{rid}")
def delete_role(rid: str):
    used = query_one("SELECT id FROM agents WHERE role_id=? LIMIT 1", (rid,))
    if used:
        update("roles", rid, {"active": 0, "updated_at": now()})
        audit("deactivate_role", "role", rid, "Role in use; deactivated instead of deleted")
        return {"ok": True, "deactivated": True}
    execute("DELETE FROM instruction_files WHERE role_id=?", (rid,))
    execute("DELETE FROM role_skills WHERE role_id=?", (rid,))
    execute("DELETE FROM persona_versions WHERE persona_id IN (SELECT id FROM personas WHERE role_id=?)", (rid,))
    execute("DELETE FROM personas WHERE role_id=?", (rid,))
    execute("DELETE FROM model_bindings WHERE role_id=?", (rid,))
    execute("DELETE FROM roles WHERE id=?", (rid,))
    return {"ok": True}


# ------------------ personas ------------------

@router.get("/personas")
def list_personas(role_id: str | None = Query(None), limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    if role_id:
        total = query_one("SELECT COUNT(*) AS n FROM personas WHERE role_id=?", (role_id,))["n"]
        items = query("SELECT * FROM personas WHERE role_id=? ORDER BY name LIMIT ? OFFSET ?", (role_id, limit, offset))
        return {"total": total, "items": items, "limit": limit, "offset": offset}
    total = query_one("SELECT COUNT(*) AS n FROM personas")["n"]
    items = query("SELECT * FROM personas ORDER BY name LIMIT ? OFFSET ?", (limit, offset))
    return {"total": total, "items": items, "limit": limit, "offset": offset}


@router.post("/roles/{role_id}/personas")
def create_persona(role_id: str, body: PersonaIn):
    _or_404(query_one("SELECT id FROM roles WHERE id=?", (role_id,)), "Role")
    pid = new_id()
    ts = now()
    insert("personas", {"id": pid, "role_id": role_id, "name": body.name,
                        "description": body.description, "instructions": body.instructions,
                        "constraints_text": body.constraints_text, "version": 1, "active": 1,
                        "created_at": ts, "updated_at": ts})
    insert("persona_versions", {"id": new_id(), "persona_id": pid, "version": 1,
                                "instructions": body.instructions,
                                "constraints_text": body.constraints_text,
                                "checksum": checksum(body.instructions), "created_at": ts})
    audit("create_persona", "persona", pid, f"Created persona '{body.name}'")
    row = query_one("SELECT * FROM personas WHERE id = ?", (pid,))
    row["lint_warnings"] = lint_prompt(body.instructions) + lint_prompt(body.constraints_text)
    return row


@router.patch("/personas/{pid}")
def update_persona(pid: str, body: PersonaUpdate):
    persona = _or_404(query_one("SELECT * FROM personas WHERE id=?", (pid,)), "Persona")
    data = body.model_dump(exclude_unset=True)
    allowed = {k: v for k, v in data.items() if k in ("name", "description", "instructions",
                                                      "constraints_text", "active")}
    if "instructions" in allowed and allowed["instructions"] != persona["instructions"]:
        new_version = persona["version"] + 1
        allowed["version"] = new_version
        insert("persona_versions", {
            "id": new_id(), "persona_id": pid, "version": new_version,
            "instructions": allowed["instructions"],
            "constraints_text": allowed.get("constraints_text", persona["constraints_text"]),
            "checksum": checksum(allowed["instructions"]), "created_at": now()})
        audit("version_persona", "persona", pid, f"Persona versioned to v{new_version}")
    allowed["updated_at"] = now()
    update("personas", pid, allowed)
    row = query_one("SELECT * FROM personas WHERE id = ?", (pid,))
    row["lint_warnings"] = (lint_prompt(allowed.get("instructions", ""))
                            + lint_prompt(allowed.get("constraints_text", "")))
    return row


@router.get("/personas/{pid}/versions")
def list_persona_versions(pid: str):
    _or_404(query_one("SELECT id FROM personas WHERE id=?", (pid,)), "Persona")
    return query("SELECT id, version, checksum, created_at, "
                 "LENGTH(instructions) AS instructions_chars FROM persona_versions "
                 "WHERE persona_id=? ORDER BY version DESC", (pid,))


@router.post("/personas/{pid}/restore")
def restore_persona_version(pid: str, body: dict):
    """Restore a persona to a stored earlier version (v1 = seeded baseline).
    Append-only: the restore itself is recorded as a new version."""
    persona = _or_404(query_one("SELECT * FROM personas WHERE id=?", (pid,)), "Persona")
    try:
        version = int(body.get("version"))
    except (TypeError, ValueError):
        raise HTTPException(400, "Provide {\"version\": <int>}")
    snap = query_one("SELECT * FROM persona_versions WHERE persona_id=? AND version=?",
                     (pid, version))
    if not snap:
        raise HTTPException(404, f"No stored version {version} for this persona")
    if snap["instructions"] == persona["instructions"] and \
            snap["constraints_text"] == persona["constraints_text"]:
        return {**persona, "restored": False, "note": f"Already at version {version} content"}
    new_v = persona["version"] + 1
    ts = now()
    insert("persona_versions", {
        "id": new_id(), "persona_id": pid, "version": new_v,
        "instructions": snap["instructions"], "constraints_text": snap["constraints_text"],
        "checksum": checksum(snap["instructions"]), "created_at": ts})
    update("personas", pid, {"instructions": snap["instructions"],
                             "constraints_text": snap["constraints_text"],
                             "version": new_v, "updated_at": ts})
    audit("restore_persona", "persona", pid, f"Restored to version {version} (now v{new_v})")
    row = query_one("SELECT * FROM personas WHERE id = ?", (pid,))
    return {**row, "restored": True, "restored_from": version}


@router.delete("/personas/{pid}")
def delete_persona(pid: str):
    used = query_one("SELECT id FROM agents WHERE persona_id=? LIMIT 1", (pid,))
    if used:
        update("personas", pid, {"active": 0, "updated_at": now()})
        return {"ok": True, "deactivated": True}
    execute("DELETE FROM persona_versions WHERE persona_id=?", (pid,))
    execute("DELETE FROM persona_skills WHERE persona_id=?", (pid,))
    execute("DELETE FROM personas WHERE id=?", (pid,))
    return {"ok": True}


@router.get("/personas/{pid}/skills")
def persona_skills(pid: str):
    return query(
        "SELECT s.* FROM persona_skills ps JOIN skills s ON s.id = ps.skill_id "
        "WHERE ps.persona_id = ?", (pid,))


@router.post("/personas/{pid}/skills/{skill_id}")
def attach_skill(pid: str, skill_id: str):
    execute("INSERT OR IGNORE INTO persona_skills (persona_id, skill_id) VALUES (?,?)", (pid, skill_id))
    return {"ok": True}


@router.delete("/personas/{pid}/skills/{skill_id}")
def detach_skill(pid: str, skill_id: str):
    execute("DELETE FROM persona_skills WHERE persona_id=? AND skill_id=?", (pid, skill_id))
    return {"ok": True}


# ------------------ skills ------------------

@router.get("/skills")
def list_skills(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    total = query_one("SELECT COUNT(*) AS n FROM skills")["n"]
    items = query("SELECT * FROM skills ORDER BY name LIMIT ? OFFSET ?", (limit, offset))
    return {"total": total, "items": items, "limit": limit, "offset": offset}


@router.post("/skills")
def create_skill(body: SkillIn):
    if query_one("SELECT id FROM skills WHERE lower(name)=lower(?)", (body.name,)):
        raise HTTPException(409, "Skill name already exists")
    sid = new_id()
    ts = now()
    insert("skills", {"id": sid, "name": body.name, "description": body.description,
                      "content": body.content, "version": 1, "active": 1,
                      "created_at": ts, "updated_at": ts})
    audit("create_skill", "skill", sid, f"Created skill '{body.name}'")
    return query_one("SELECT * FROM skills WHERE id = ?", (sid,))


@router.patch("/skills/{sid}")
def update_skill(sid: str, body: SkillUpdate):
    skill = _or_404(query_one("SELECT * FROM skills WHERE id=?", (sid,)), "Skill")
    data = body.model_dump(exclude_unset=True)
    allowed = {k: v for k, v in data.items() if k in ("name", "description", "content", "active")}
    if "content" in allowed and allowed["content"] != skill["content"]:
        allowed["version"] = skill["version"] + 1
    allowed["updated_at"] = now()
    update("skills", sid, allowed)
    return query_one("SELECT * FROM skills WHERE id = ?", (sid,))


@router.delete("/skills/{sid}")
def delete_skill(sid: str):
    execute("DELETE FROM persona_skills WHERE skill_id=?", (sid,))
    execute("DELETE FROM role_skills WHERE skill_id=?", (sid,))
    execute("DELETE FROM skills WHERE id=?", (sid,))
    return {"ok": True}


# ------------------ model bindings ------------------

@router.get("/model-bindings")
def list_bindings(role_id: str | None = Query(None)):
    sql = (
        "SELECT mb.*, gm.display_name AS model_name, gm.provider_model_id, g.name AS gateway_name "
        "FROM model_bindings mb JOIN gateway_models gm ON gm.id = mb.model_id "
        "JOIN gateways g ON g.id = mb.gateway_id")
    if role_id:
        return query(sql + " WHERE mb.role_id = ?", (role_id,))
    return query(sql)


@router.post("/model-bindings")
def create_binding(body: BindingIn):
    bid = new_id()
    insert("model_bindings", {"id": bid, "role_id": body.role_id, "gateway_id": body.gateway_id,
                              "model_id": body.model_id, "settings_json": "{}", "active": 1})
    audit("create_model_binding", "model_binding", bid, "Bound role to model")
    return query_one("SELECT * FROM model_bindings WHERE id = ?", (bid,))


@router.delete("/model-bindings/{bid}")
def delete_binding(bid: str):
    if query_one("SELECT id FROM agents WHERE model_binding_id=? LIMIT 1", (bid,)):
        raise HTTPException(409, "Model is assigned to an agent — reassign the agent first")
    execute("DELETE FROM model_bindings WHERE id=?", (bid,))
    return {"ok": True}
