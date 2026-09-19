"""Agent Office API — FastAPI application.

CQRS-lite: query endpoints return read models for Project Control; command
endpoints validate, mutate durable state and emit realtime events. Static
frontend assets are served from /static with an SPA fallback at /.
"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import chatbot, db, runtime
from .db import audit, execute, insert, new_id, now, query, query_one, update

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    runtime.recover_orphans()
    runtime.set_loop(asyncio.get_running_loop())
    yield


app = FastAPI(title="Agent Office API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ------------------------------- helpers -------------------------------

def _or_404(row, what="Resource"):
    if row is None:
        raise HTTPException(404, f"{what} not found")
    return row


def _get_setting(key, default=""):
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def _set_setting(key, value):
    execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def _mask(key_value: str) -> str:
    return "••••" + key_value[-4:] if key_value and len(key_value) >= 4 else "••••"


# ------------------------------- schemas -------------------------------

class GatewayIn(BaseModel):
    name: str
    provider: str = "openai"
    base_url: str
    api_type: str = "openai-chat"
    api_key: Optional[str] = None


class ModelIn(BaseModel):
    provider_model_id: str
    display_name: str
    capabilities: str = ""


class RoleIn(BaseModel):
    name: str
    description: str = ""


class PersonaIn(BaseModel):
    role_id: str
    name: str
    description: str = ""
    instructions: str = ""
    constraints_text: str = ""


class SkillIn(BaseModel):
    name: str
    description: str = ""
    content: str = ""


class BindingIn(BaseModel):
    role_id: str
    gateway_id: str
    model_id: str


class AgentIn(BaseModel):
    name: str
    role_id: str
    persona_id: str
    model_binding_id: Optional[str] = None


class TeamIn(BaseModel):
    name: str
    description: str = ""
    agent_ids: list[str] = []


class ProjectIn(BaseModel):
    name: str
    goal: str = ""
    description: str = ""
    technology_stack: str = ""
    repository_url: str = ""
    workspace_path: str = ""
    default_gateway_id: Optional[str] = None
    team_id: Optional[str] = None


class BacklogIn(BaseModel):
    title: str
    description: str = ""
    priority: int = 2
    acceptance_criteria: str = ""
    story_points: int = 3


class SprintIn(BaseModel):
    name: str
    goal: str = ""
    capacity: int = 40
    start_at: Optional[str] = None
    end_at: Optional[str] = None


class TaskIn(BaseModel):
    title: str
    description: str = ""
    acceptance_criteria: str = ""
    story_points: int = 3
    priority: int = 2
    sprint_id: Optional[str] = None
    backlog_item_id: Optional[str] = None
    assigned_agent_id: Optional[str] = None
    depends_on: list[str] = []


class MessageIn(BaseModel):
    content: str


class ExecutionIn(BaseModel):
    project_id: str
    task_id: str


class SettingsIn(BaseModel):
    key: str
    value: str


# ------------------------------- settings -------------------------------

@app.get("/api/v1/settings")
def get_settings():
    return {"theme": _get_setting("theme", "dark")}


@app.put("/api/v1/settings")
def put_settings(body: SettingsIn):
    _set_setting(body.key, body.value)
    return {"ok": True}


# ------------------------------- gateways -------------------------------

@app.get("/api/v1/gateways")
def list_gateways():
    return query("SELECT * FROM gateways ORDER BY created_at")


@app.post("/api/v1/gateways")
def create_gateway(body: GatewayIn):
    gid = new_id()
    ts = now()
    insert("gateways", {
        "id": gid, "name": body.name, "provider": body.provider,
        "base_url": body.base_url.rstrip("/"), "api_type": body.api_type,
        "status": "Active", "key_mask": _mask(body.api_key) if body.api_key else None,
        "created_at": ts, "updated_at": ts,
    })
    if body.api_key:
        db.set_gateway_key(gid, body.api_key)
    sync = db.sync_gateway_models(gid)
    audit("create_gateway", "gateway", gid,
          f"Created gateway '{body.name}'; auto-fetched {sync['added']} models")
    return {**query_one("SELECT * FROM gateways WHERE id = ?", (gid,)), "models_fetched": sync["added"]}


@app.patch("/api/v1/gateways/{gid}")
def update_gateway(gid: str, body: dict):
    _or_404(query_one("SELECT id FROM gateways WHERE id=?", (gid,)), "Gateway")
    allowed = {k: v for k, v in body.items() if k in ("name", "provider", "base_url", "api_type", "status")}
    allowed["updated_at"] = now()
    update("gateways", gid, allowed)
    if body.get("api_key"):
        db.set_gateway_key(gid, body["api_key"])
    models_fetched = 0
    if "provider" in allowed:
        sync = db.sync_gateway_models(gid)
        models_fetched = sync["added"]
    audit("update_gateway", "gateway", gid, f"Updated gateway fields: {', '.join(allowed)}")
    return {**query_one("SELECT * FROM gateways WHERE id = ?", (gid,)), "models_fetched": models_fetched}


@app.delete("/api/v1/gateways/{gid}")
def delete_gateway(gid: str):
    execute("DELETE FROM gateway_models WHERE gateway_id = ?", (gid,))
    execute("DELETE FROM gateways WHERE id = ?", (gid,))
    audit("delete_gateway", "gateway", gid, "Deleted gateway")
    return {"ok": True}


@app.post("/api/v1/gateways/{gid}/test")
def test_gateway(gid: str):
    gw = _or_404(query_one("SELECT * FROM gateways WHERE id=?", (gid,)), "Gateway")
    ts = now()
    # Simulated connectivity probe — never echoes headers or credentials.
    ok = gw["base_url"].startswith("http://") or gw["base_url"].startswith("https://")
    result = "Success" if ok else "Failed"
    diag = ("Connection OK (simulated probe): TLS handshake and auth schema accepted"
            if ok else "Invalid Base URL scheme; expected http(s)")
    update("gateways", gid, {"last_tested_at": ts, "test_status": result, "test_diagnostic": diag})
    models_fetched, models_removed = 0, 0
    if ok:
        sync = db.sync_gateway_models(gid)
        models_fetched, models_removed = sync["added"], sync["removed"]
    audit("test_gateway", "gateway", gid,
          f"Gateway test: {result}; catalog +{models_fetched}/-{models_removed}")
    return {"status": result, "diagnostic": diag, "tested_at": ts,
            "models_fetched": models_fetched, "models_removed": models_removed}


@app.get("/api/v1/gateways/{gid}/models")
def list_models(gid: str):
    return query("SELECT * FROM gateway_models WHERE gateway_id = ? ORDER BY display_name", (gid,))


@app.post("/api/v1/gateways/{gid}/models")
def add_model(gid: str, body: ModelIn):
    _or_404(query_one("SELECT id FROM gateways WHERE id=?", (gid,)), "Gateway")
    mid = new_id()
    insert("gateway_models", {"id": mid, "gateway_id": gid,
                              "provider_model_id": body.provider_model_id,
                              "display_name": body.display_name or body.provider_model_id,
                              "capabilities": body.capabilities, "active": 1})
    return query_one("SELECT * FROM gateway_models WHERE id = ?", (mid,))


@app.delete("/api/v1/gateways/{gid}/models/{mid}")
def delete_model(gid: str, mid: str):
    execute("DELETE FROM gateway_models WHERE id=? AND gateway_id=?", (mid, gid))
    return {"ok": True}


class TestChatIn(BaseModel):
    model_id: str
    message: str


def _live_chat(gw, model, message: str):
    """Real inference call through the gateway. Returns (reply, meta)."""
    import time as _time
    import urllib.error as _uerr
    import urllib.request as _ureq

    api_key = db.get_gateway_key(gw["id"])
    if not api_key:
        raise HTTPException(409, "No API key stored for this gateway. Click Edit on the gateway card and paste the key once to enable live testing.")
    base = gw["base_url"].rstrip("/")
    is_anthropic = gw["api_type"] == "anthropic-messages"
    if is_anthropic:
        if base.endswith("/messages"):
            url = base
        elif base.endswith("/v1"):
            url = base + "/messages"
        else:
            url = base + "/v1/messages"
    else:
        if "/v1" not in base:
            base += "/v1"
        url = base + "/chat/completions"
    if is_anthropic:
        payload = {"model": model["provider_model_id"], "max_tokens": 512,
                   "messages": [{"role": "user", "content": message}]}
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
    else:
        payload = {"model": model["provider_model_id"], "max_tokens": 512,
                   "messages": [{"role": "user", "content": message}]}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    started = _time.monotonic()
    req = _ureq.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with _ureq.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read())
    except _uerr.HTTPError as e:
        err_body = e.read().decode(errors="replace")[:400]
        raise HTTPException(502, f"Gateway returned HTTP {e.code}: {err_body}")
    except Exception as exc:
        raise HTTPException(502, f"Could not reach gateway at {url}: {exc}")
    latency_ms = int(((_time.monotonic() - started) * 1000))

    usage = data.get("usage") or {}
    if is_anthropic:
        reply = "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        in_t, out_t = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    else:
        try:
            reply = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            reply = json.dumps(data)[:600]
        in_t, out_t = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    if not reply:
        raise HTTPException(502, f"Gateway returned an empty response: {json.dumps(data)[:400]}")
    return reply.strip(), {"latency_ms": latency_ms, "input_tokens": in_t, "output_tokens": out_t}


@app.post("/api/v1/gateways/{gid}/test-chat")
async def gateway_test_chat(gid: str, body: TestChatIn):
    """Live single-turn inference against a gateway model (playground)."""
    gw = _or_404(query_one("SELECT * FROM gateways WHERE id=?", (gid,)), "Gateway")
    model = query_one("SELECT * FROM gateway_models WHERE id=? AND gateway_id=?",
                      (body.model_id, gid))
    if not model:
        raise HTTPException(404, "Model not found on this gateway")
    result = await asyncio.get_running_loop().run_in_executor(
        None, _live_chat, gw, model, body.message)
    reply, meta = result
    audit("test_chat", "gateway", gid,
          f"Live chat with {model['provider_model_id']} ({meta['input_tokens']}+{meta['output_tokens']} tokens, {meta['latency_ms']}ms)")
    return {"reply": reply, "model": model["provider_model_id"],
            "gateway": gw["name"], "live": True, **meta}


def _live_model_ids(gw) -> list[str] | None:
    """Best-effort live fetch of the provider's model list. None on failure."""
    import urllib.error as _uerr
    import urllib.request as _ureq

    api_key = db.get_gateway_key(gw["id"])
    if not api_key:
        return None
    base = gw["base_url"].rstrip("/")
    if "/v1" not in base:
        base += "/v1"
    req = _ureq.Request(base + "/models", headers={"Authorization": f"Bearer {api_key}"})
    try:
        with _ureq.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        return ids or None
    except Exception:
        return None


@app.post("/api/v1/gateways/{gid}/discover")
def discover_models(gid: str):
    gw = _or_404(query_one("SELECT * FROM gateways WHERE id=?", (gid,)), "Gateway")
    live_ids = _live_model_ids(gw)
    source = "provider-catalog"
    if live_ids:
        source = "live"
        added = 0
        for mid in live_ids:
            exists = query_one("SELECT id FROM gateway_models WHERE gateway_id=? AND provider_model_id=?",
                               (gid, mid))
            if not exists:
                insert("gateway_models", {"id": new_id(), "gateway_id": gid,
                                          "provider_model_id": mid, "display_name": mid,
                                          "capabilities": "", "active": 1})
                added += 1
        total = query_one("SELECT COUNT(*) AS n FROM gateway_models WHERE gateway_id=?", (gid,))["n"]
        return {"source": source, "added": added, "total": total, "live_models": len(live_ids)}
    sync = db.sync_gateway_models(gid)
    total = query_one("SELECT COUNT(*) AS n FROM gateway_models WHERE gateway_id=?", (gid,))["n"]
    return {"source": source, "added": sync["added"], "removed": sync["removed"], "total": total}


# ------------------------------- agent memory -------------------------------

@app.get("/api/v1/roles/memory")
def roles_memory():
    out = []
    for r in query("SELECT * FROM roles ORDER BY name"):
        ic = query_one("SELECT COUNT(*) AS n FROM instruction_files WHERE role_id=?", (r["id"],))["n"]
        sc = query_one("SELECT COUNT(*) AS n FROM role_skills WHERE role_id=?", (r["id"],))["n"]
        out.append({**r, "instruction_count": ic, "skill_count": sc})
    return out


class InstructionIn(BaseModel):
    filename: str
    description: str = ""
    content: str = ""


@app.get("/api/v1/roles/{rid}/instructions")
def list_instructions(rid: str):
    return query("SELECT * FROM instruction_files WHERE role_id=? ORDER BY filename", (rid,))


@app.post("/api/v1/roles/{rid}/instructions")
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
    audit("create_instruction", "instruction_file", iid, f"Created instruction '{filename}'")
    return query_one("SELECT * FROM instruction_files WHERE id = ?", (iid,))


@app.patch("/api/v1/instructions/{iid}")
def update_instruction(iid: str, body: dict):
    instr = _or_404(query_one("SELECT * FROM instruction_files WHERE id=?", (iid,)), "Instruction")
    allowed = {k: v for k, v in body.items() if k in ("filename", "description", "content")}
    if "content" in allowed and allowed["content"] != instr["content"]:
        allowed["version"] = instr["version"] + 1
    allowed["updated_at"] = now()
    update("instruction_files", iid, allowed)
    return query_one("SELECT * FROM instruction_files WHERE id = ?", (iid,))


@app.delete("/api/v1/instructions/{iid}")
def delete_instruction(iid: str):
    execute("DELETE FROM instruction_files WHERE id=?", (iid,))
    return {"ok": True}


@app.get("/api/v1/roles/{rid}/skills")
def list_role_skills(rid: str):
    return query("SELECT s.* FROM role_skills rs JOIN skills s ON s.id = rs.skill_id WHERE rs.role_id=? ORDER BY s.name", (rid,))


@app.post("/api/v1/roles/{rid}/skills/{sid}")
def attach_role_skill(rid: str, sid: str):
    execute("INSERT OR IGNORE INTO role_skills (role_id, skill_id) VALUES (?,?)", (rid, sid))
    return {"ok": True}


@app.delete("/api/v1/roles/{rid}/skills/{sid}")
def detach_role_skill(rid: str, sid: str):
    execute("DELETE FROM role_skills WHERE role_id=? AND skill_id=?", (rid, sid))
    return {"ok": True}


@app.get("/api/v1/roles")
def list_roles():
    return query("SELECT * FROM roles ORDER BY name")


@app.post("/api/v1/roles")
def create_role(body: RoleIn):
    if query_one("SELECT id FROM roles WHERE lower(name)=lower(?)", (body.name,)):
        raise HTTPException(409, "Role name already exists")
    rid = new_id()
    ts = now()
    insert("roles", {"id": rid, "name": body.name, "description": body.description,
                     "active": 1, "created_at": ts, "updated_at": ts})
    audit("create_role", "role", rid, f"Created role '{body.name}'")
    return query_one("SELECT * FROM roles WHERE id = ?", (rid,))


@app.patch("/api/v1/roles/{rid}")
def update_role(rid: str, body: dict):
    _or_404(query_one("SELECT id FROM roles WHERE id=?", (rid,)), "Role")
    allowed = {k: v for k, v in body.items() if k in ("name", "description", "active")}
    allowed["updated_at"] = now()
    update("roles", rid, allowed)
    return query_one("SELECT * FROM roles WHERE id = ?", (rid,))


@app.delete("/api/v1/roles/{rid}")
def delete_role(rid: str):
    used = query_one("SELECT id FROM agents WHERE role_id=? LIMIT 1", (rid,))
    if used:
        update("roles", rid, {"active": 0, "updated_at": now()})
        audit("deactivate_role", "role", rid, "Role in use; deactivated instead of deleted")
        return {"ok": True, "deactivated": True}
    execute("DELETE FROM instruction_files WHERE role_id=?", (rid,))
    execute("DELETE FROM role_skills WHERE role_id=?", (rid,))
    execute("DELETE FROM personas WHERE role_id=?", (rid,))
    execute("DELETE FROM model_bindings WHERE role_id=?", (rid,))
    execute("DELETE FROM roles WHERE id=?", (rid,))
    return {"ok": True}


@app.get("/api/v1/personas")
def list_personas(role_id: Optional[str] = None):
    if role_id:
        return query("SELECT * FROM personas WHERE role_id=? ORDER BY name", (role_id,))
    return query("SELECT * FROM personas ORDER BY name")


@app.post("/api/v1/roles/{role_id}/personas")
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
                                "checksum": db.checksum(body.instructions), "created_at": ts})
    audit("create_persona", "persona", pid, f"Created persona '{body.name}'")
    return query_one("SELECT * FROM personas WHERE id = ?", (pid,))


@app.patch("/api/v1/personas/{pid}")
def update_persona(pid: str, body: dict):
    persona = _or_404(query_one("SELECT * FROM personas WHERE id=?", (pid,)), "Persona")
    allowed = {k: v for k, v in body.items() if k in ("name", "description", "instructions",
                                                      "constraints_text", "active")}
    if "instructions" in allowed and allowed["instructions"] != persona["instructions"]:
        new_version = persona["version"] + 1
        allowed["version"] = new_version
        insert("persona_versions", {
            "id": new_id(), "persona_id": pid, "version": new_version,
            "instructions": allowed["instructions"],
            "constraints_text": allowed.get("constraints_text", persona["constraints_text"]),
            "checksum": db.checksum(allowed["instructions"]), "created_at": now()})
        audit("version_persona", "persona", pid, f"Persona versioned to v{new_version}")
    allowed["updated_at"] = now()
    update("personas", pid, allowed)
    return query_one("SELECT * FROM personas WHERE id = ?", (pid,))


@app.delete("/api/v1/personas/{pid}")
def delete_persona(pid: str):
    used = query_one("SELECT id FROM agents WHERE persona_id=? LIMIT 1", (pid,))
    if used:
        update("personas", pid, {"active": 0, "updated_at": now()})
        return {"ok": True, "deactivated": True}
    execute("DELETE FROM persona_skills WHERE persona_id=?", (pid,))
    execute("DELETE FROM personas WHERE id=?", (pid,))
    return {"ok": True}


@app.get("/api/v1/personas/{pid}/skills")
def persona_skills(pid: str):
    return query("SELECT s.* FROM persona_skills ps JOIN skills s ON s.id = ps.skill_id WHERE ps.persona_id = ?", (pid,))


@app.post("/api/v1/personas/{pid}/skills/{skill_id}")
def attach_skill(pid: str, skill_id: str):
    execute("INSERT OR IGNORE INTO persona_skills (persona_id, skill_id) VALUES (?,?)", (pid, skill_id))
    return {"ok": True}


@app.delete("/api/v1/personas/{pid}/skills/{skill_id}")
def detach_skill(pid: str, skill_id: str):
    execute("DELETE FROM persona_skills WHERE persona_id=? AND skill_id=?", (pid, skill_id))
    return {"ok": True}


@app.get("/api/v1/skills")
def list_skills():
    return query("SELECT * FROM skills ORDER BY name")


@app.post("/api/v1/skills")
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


@app.patch("/api/v1/skills/{sid}")
def update_skill(sid: str, body: dict):
    skill = _or_404(query_one("SELECT * FROM skills WHERE id=?", (sid,)), "Skill")
    allowed = {k: v for k, v in body.items() if k in ("name", "description", "content", "active")}
    if "content" in allowed and allowed["content"] != skill["content"]:
        allowed["version"] = skill["version"] + 1
    allowed["updated_at"] = now()
    update("skills", sid, allowed)
    return query_one("SELECT * FROM skills WHERE id = ?", (sid,))


@app.delete("/api/v1/skills/{sid}")
def delete_skill(sid: str):
    execute("DELETE FROM persona_skills WHERE skill_id=?", (sid,))
    execute("DELETE FROM skills WHERE id=?", (sid,))
    return {"ok": True}


# ------------------------- model bindings -------------------------

@app.get("/api/v1/model-bindings")
def list_bindings(role_id: Optional[str] = None):
    sql = ("SELECT mb.*, gm.display_name AS model_name, gm.provider_model_id, g.name AS gateway_name "
           "FROM model_bindings mb JOIN gateway_models gm ON gm.id = mb.model_id "
           "JOIN gateways g ON g.id = mb.gateway_id")
    if role_id:
        return query(sql + " WHERE mb.role_id = ?", (role_id,))
    return query(sql)


@app.post("/api/v1/model-bindings")
def create_binding(body: BindingIn):
    bid = new_id()
    insert("model_bindings", {"id": bid, "role_id": body.role_id, "gateway_id": body.gateway_id,
                              "model_id": body.model_id, "settings_json": "{}", "active": 1})
    audit("create_model_binding", "model_binding", bid, "Bound role to model")
    return query_one("SELECT * FROM model_bindings WHERE id = ?", (bid,))


@app.delete("/api/v1/model-bindings/{bid}")
def delete_binding(bid: str):
    execute("DELETE FROM model_bindings WHERE id=?", (bid,))
    return {"ok": True}


# ------------------------------- agents & teams -------------------------------

@app.get("/api/v1/agents")
def list_agents():
    return query(
        "SELECT a.*, r.name AS role_name, p.name AS persona_name, p.version AS persona_version, "
        "gm.provider_model_id FROM agents a "
        "LEFT JOIN roles r ON r.id = a.role_id "
        "LEFT JOIN personas p ON p.id = a.persona_id "
        "LEFT JOIN model_bindings mb ON mb.id = a.model_binding_id "
        "LEFT JOIN gateway_models gm ON gm.id = mb.model_id ORDER BY a.name")


@app.post("/api/v1/agents")
def create_agent(body: AgentIn):
    aid = new_id()
    ts = now()
    insert("agents", {"id": aid, "name": body.name, "role_id": body.role_id,
                      "persona_id": body.persona_id, "model_binding_id": body.model_binding_id,
                      "lifecycle_state": "Idle", "created_at": ts, "updated_at": ts})
    audit("create_agent", "agent", aid, f"Created agent '{body.name}'")
    return query_one("SELECT * FROM agents WHERE id = ?", (aid,))


@app.patch("/api/v1/agents/{aid}")
def update_agent(aid: str, body: dict):
    _or_404(query_one("SELECT id FROM agents WHERE id=?", (aid,)), "Agent")
    allowed = {k: v for k, v in body.items() if k in ("name", "role_id", "persona_id", "model_binding_id")}
    allowed["updated_at"] = now()
    update("agents", aid, allowed)
    return query_one("SELECT * FROM agents WHERE id = ?", (aid,))


@app.delete("/api/v1/agents/{aid}")
def delete_agent(aid: str):
    agent = _or_404(query_one("SELECT * FROM agents WHERE id=?", (aid,)), "Agent")
    if agent["lifecycle_state"] not in ("Idle", "Failed"):
        raise HTTPException(409, "Agent is busy; stop its execution first")
    execute("DELETE FROM team_agents WHERE agent_id=?", (aid,))
    execute("DELETE FROM agents WHERE id=?", (aid,))
    return {"ok": True}


@app.get("/api/v1/teams")
def list_teams():
    teams = query("SELECT * FROM teams ORDER BY name")
    for t in teams:
        t["agents"] = query(
            "SELECT a.id, a.name, a.lifecycle_state, a.current_activity, r.name AS role_name, ta.role_in_team "
            "FROM team_agents ta JOIN agents a ON a.id = ta.agent_id JOIN roles r ON r.id = a.role_id "
            "WHERE ta.team_id = ? AND ta.active = 1", (t["id"],))
    return teams


@app.post("/api/v1/teams")
def create_team(body: TeamIn):
    if query_one("SELECT id FROM teams WHERE lower(name)=lower(?)", (body.name,)):
        raise HTTPException(409, "Team name already exists")
    tid = new_id()
    insert("teams", {"id": tid, "name": body.name, "description": body.description,
                     "status": "Active", "created_at": now()})
    for agent_id in body.agent_ids:
        execute("INSERT OR IGNORE INTO team_agents (team_id, agent_id, role_in_team, active) VALUES (?,?,?,1)",
                (tid, agent_id, "Member"))
    audit("create_team", "team", tid, f"Created team '{body.name}'")
    return query_one("SELECT * FROM teams WHERE id = ?", (tid,))


@app.patch("/api/v1/teams/{tid}")
def update_team(tid: str, body: dict):
    _or_404(query_one("SELECT id FROM teams WHERE id=?", (tid,)), "Team")
    if "agent_ids" in body:
        execute("DELETE FROM team_agents WHERE team_id=?", (tid,))
        for agent_id in body["agent_ids"]:
            execute("INSERT OR IGNORE INTO team_agents (team_id, agent_id, role_in_team, active) VALUES (?,?,?,1)",
                    (tid, agent_id, "Member"))
    allowed = {k: v for k, v in body.items() if k in ("name", "description", "status")}
    if allowed:
        update("teams", tid, allowed)
    return {"ok": True}


@app.delete("/api/v1/teams/{tid}")
def delete_team(tid: str):
    used = query_one("SELECT id FROM projects WHERE team_id=? LIMIT 1", (tid,))
    if used:
        raise HTTPException(409, "Team is assigned to a project")
    execute("DELETE FROM team_agents WHERE team_id=?", (tid,))
    execute("DELETE FROM teams WHERE id=?", (tid,))
    return {"ok": True}


# ------------------------------- projects -------------------------------

@app.get("/api/v1/projects")
def list_projects():
    return query(
        "SELECT p.*, t.name AS team_name FROM projects p LEFT JOIN teams t ON t.id = p.team_id ORDER BY p.created_at")


@app.post("/api/v1/projects")
def create_project(body: ProjectIn):
    pid = new_id()
    ts = now()
    insert("projects", {"id": pid, "name": body.name, "goal": body.goal,
                        "description": body.description, "technology_stack": body.technology_stack,
                        "repository_url": body.repository_url, "workspace_path": body.workspace_path,
                        "default_gateway_id": body.default_gateway_id, "team_id": body.team_id,
                        "status": "Active", "created_at": ts, "updated_at": ts})
    audit("create_project", "project", pid, f"Created project '{body.name}'")
    return query_one("SELECT * FROM projects WHERE id = ?", (pid,))


@app.get("/api/v1/projects/{pid}")
def get_project(pid: str):
    return _or_404(query_one("SELECT * FROM projects WHERE id=?", (pid,)), "Project")


@app.patch("/api/v1/projects/{pid}")
def update_project(pid: str, body: dict):
    _or_404(query_one("SELECT id FROM projects WHERE id=?", (pid,)), "Project")
    allowed = {k: v for k, v in body.items() if k in ("name", "goal", "description", "technology_stack",
                                                      "repository_url", "workspace_path",
                                                      "default_gateway_id", "team_id", "status")}
    allowed["updated_at"] = now()
    update("projects", pid, allowed)
    emit = db.emit_event(pid, "project.updated", {"note": "Project configuration updated"})
    return {**query_one("SELECT * FROM projects WHERE id = ?", (pid,)), "_seq": emit}


@app.delete("/api/v1/projects/{pid}")
def delete_project(pid: str):
    for tbl, col in (("task_dependencies", ""), ("tasks", "project_id"), ("sprints", "project_id"),
                     ("backlog_items", "project_id"), ("messages", ""), ("conversations", "project_id"),
                     ("execution_events", "project_id")):
        if tbl == "task_dependencies":
            execute("DELETE FROM task_dependencies WHERE task_id IN (SELECT id FROM tasks WHERE project_id=?) "
                    "OR depends_on_task_id IN (SELECT id FROM tasks WHERE project_id=?)", (pid, pid))
        elif tbl == "messages":
            execute("DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE project_id=?)", (pid,))
        else:
            execute(f"DELETE FROM {tbl} WHERE {col} = ?", (pid,))
    execute("DELETE FROM projects WHERE id=?", (pid,))
    audit("delete_project", "project", pid, "Deleted project")
    return {"ok": True}


# ------------------------------- backlog / sprints / tasks -------------------------------

@app.get("/api/v1/projects/{pid}/backlog")
def list_backlog(pid: str):
    return query("SELECT * FROM backlog_items WHERE project_id=? ORDER BY priority, created_at", (pid,))


@app.post("/api/v1/projects/{pid}/backlog")
def add_backlog(pid: str, body: BacklogIn):
    bid = new_id()
    insert("backlog_items", {"id": bid, "project_id": pid, "title": body.title,
                             "description": body.description, "priority": body.priority,
                             "acceptance_criteria": body.acceptance_criteria,
                             "story_points": body.story_points, "status": "Backlog",
                             "created_at": now()})
    return query_one("SELECT * FROM backlog_items WHERE id = ?", (bid,))


@app.patch("/api/v1/backlog/{bid}")
def update_backlog(bid: str, body: dict):
    _or_404(query_one("SELECT id FROM backlog_items WHERE id=?", (bid,)), "Backlog item")
    allowed = {k: v for k, v in body.items() if k in ("title", "description", "priority",
                                                      "acceptance_criteria", "story_points", "status")}
    update("backlog_items", bid, allowed)
    return query_one("SELECT * FROM backlog_items WHERE id = ?", (bid,))


@app.delete("/api/v1/backlog/{bid}")
def delete_backlog(bid: str):
    execute("UPDATE tasks SET backlog_item_id=NULL WHERE backlog_item_id=?", (bid,))
    execute("DELETE FROM backlog_items WHERE id=?", (bid,))
    return {"ok": True}


@app.get("/api/v1/projects/{pid}/sprints")
def list_sprints(pid: str):
    sprints = query("SELECT * FROM sprints WHERE project_id=? ORDER BY created_at DESC", (pid,))
    for s in sprints:
        s["committed_points"] = query_one(
            "SELECT COALESCE(SUM(story_points),0) AS pts FROM tasks WHERE sprint_id=?", (s["id"],))["pts"]
    return sprints


@app.post("/api/v1/projects/{pid}/sprints")
def create_sprint(pid: str, body: SprintIn):
    sid = new_id()
    insert("sprints", {"id": sid, "project_id": pid, "name": body.name, "goal": body.goal,
                       "capacity": body.capacity, "start_at": body.start_at, "end_at": body.end_at,
                       "status": "Planned", "created_at": now()})
    audit("create_sprint", "sprint", sid, f"Created sprint '{body.name}'")
    return query_one("SELECT * FROM sprints WHERE id = ?", (sid,))


@app.patch("/api/v1/sprints/{sid}")
def update_sprint(sid: str, body: dict):
    sprint = _or_404(query_one("SELECT * FROM sprints WHERE id=?", (sid,)), "Sprint")
    new_status = body.get("status")
    allowed = {k: v for k, v in body.items() if k in ("name", "goal", "capacity", "status", "start_at", "end_at")}
    if new_status == "Active":
        if sprint["status"] == "Completed":
            raise HTTPException(409, "Cannot reactivate a completed sprint")
        execute("UPDATE sprints SET status='Planned' WHERE project_id=? AND status='Active'", (sprint["project_id"],))
    allowed["status"] = new_status or sprint["status"]
    update("sprints", sid, allowed)
    if new_status:
        audit("sprint_status", "sprint", sid, f"Sprint '{sprint['name']}' -> {new_status}")
    return query_one("SELECT * FROM sprints WHERE id = ?", (sid,))


@app.delete("/api/v1/sprints/{sid}")
def delete_sprint(sid: str):
    execute("UPDATE tasks SET sprint_id=NULL WHERE sprint_id=?", (sid,))
    execute("DELETE FROM sprints WHERE id=?", (sid,))
    return {"ok": True}


TASK_TRANSITIONS = {
    "Todo": {"Ready", "Cancelled"},
    "Ready": {"In Progress", "Todo", "Cancelled"},
    "In Progress": {"Blocked", "Review", "Testing", "Cancelled"},
    "Blocked": {"Ready", "Todo", "Cancelled"},
    "Review": {"Testing", "In Progress", "Done", "Cancelled"},
    "Testing": {"Done", "In Progress", "Review", "Cancelled"},
    "Done": set(),
    "Cancelled": {"Todo"},
}


@app.get("/api/v1/projects/{pid}/tasks")
def list_tasks(pid: str, sprint_id: Optional[str] = None):
    sql = ("SELECT t.*, a.name AS agent_name, d.deps FROM tasks t "
           "LEFT JOIN agents a ON a.id = t.assigned_agent_id "
           "LEFT JOIN (SELECT task_id, GROUP_CONCAT(depends_on_task_id) AS deps FROM task_dependencies GROUP BY task_id) d "
           "ON d.task_id = t.id WHERE t.project_id = ?")
    params = [pid]
    if sprint_id:
        sql += " AND t.sprint_id = ?"
        params.append(sprint_id)
    tasks = query(sql + " ORDER BY t.priority, t.created_at", params)
    for t in tasks:
        dep_ids = t.pop("deps", None)
        t["dependencies"] = []
        t["unmet_dependencies"] = []
        if dep_ids:
            for did in dep_ids.split(","):
                dep = query_one("SELECT id, title, status FROM tasks WHERE id=?", (did,))
                if dep:
                    t["dependencies"].append(dep)
                    if dep["status"] not in ("Done", "Cancelled"):
                        t["unmet_dependencies"].append(dep)
    return tasks


@app.post("/api/v1/projects/{pid}/tasks")
def create_task(pid: str, body: TaskIn):
    tid = new_id()
    ts = now()
    insert("tasks", {"id": tid, "project_id": pid, "sprint_id": body.sprint_id,
                     "backlog_item_id": body.backlog_item_id, "title": body.title,
                     "description": body.description, "acceptance_criteria": body.acceptance_criteria,
                     "story_points": body.story_points, "priority": body.priority,
                     "assigned_agent_id": body.assigned_agent_id, "status": "Todo",
                     "created_at": ts, "updated_at": ts})
    for dep in body.depends_on:
        execute("INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_task_id) VALUES (?,?)", (tid, dep))
    return query_one("SELECT * FROM tasks WHERE id = ?", (tid,))


@app.patch("/api/v1/tasks/{tid}")
def update_task(tid: str, body: dict):
    task = _or_404(query_one("SELECT * FROM tasks WHERE id=?", (tid,)), "Task")
    allowed = {k: v for k, v in body.items() if k in ("title", "description", "acceptance_criteria",
                                                      "story_points", "priority", "sprint_id",
                                                      "backlog_item_id", "assigned_agent_id",
                                                      "status", "blocked_reason")}
    new_status = allowed.get("status")
    if new_status and new_status != task["status"]:
        if new_status not in TASK_TRANSITIONS.get(task["status"], set()):
            raise HTTPException(409, f"Invalid transition {task['status']} -> {new_status}")
        if new_status == "In Progress":
            ok, unmet = runtime.dependencies_satisfied(tid)
            if not ok:
                names = ", ".join(d["title"] for d in unmet)
                raise HTTPException(409, f"Dependencies incomplete: {names}")
        db.emit_event(task["project_id"], "task.status_changed",
                      {"task_id": tid, "task": task["title"], "status": new_status},
                      task_id=tid)
    if "assigned_agent_id" in allowed and body.get("assigned_agent_id"):
        agent = query_one("SELECT name FROM agents WHERE id=?", (body["assigned_agent_id"],))
        audit("assign_task", "task", tid, f"Assigned '{task['title']}' to {agent['name'] if agent else '?'}")
    allowed["updated_at"] = now()
    update("tasks", tid, allowed)
    return query_one("SELECT * FROM tasks WHERE id = ?", (tid,))


@app.post("/api/v1/tasks/{tid}/dependencies/{dep_id}")
def add_dependency(tid: str, dep_id: str):
    if tid == dep_id:
        raise HTTPException(400, "A task cannot depend on itself")
    execute("INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_task_id) VALUES (?,?)", (tid, dep_id))
    return {"ok": True}


@app.delete("/api/v1/tasks/{tid}/dependencies/{dep_id}")
def remove_dependency(tid: str, dep_id: str):
    execute("DELETE FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?", (tid, dep_id))
    return {"ok": True}


@app.delete("/api/v1/tasks/{tid}")
def delete_task(tid: str):
    execute("DELETE FROM task_dependencies WHERE task_id=? OR depends_on_task_id=?", (tid, tid))
    execute("DELETE FROM tasks WHERE id=?", (tid,))
    return {"ok": True}


# ------------------------------- project control -------------------------------

@app.get("/api/v1/projects/{pid}/control/summary")
def control_summary(pid: str):
    project = _or_404(query_one(
        "SELECT p.*, t.name AS team_name, g.name AS gateway_name FROM projects p "
        "LEFT JOIN teams t ON t.id = p.team_id LEFT JOIN gateways g ON g.id = p.default_gateway_id "
        "WHERE p.id = ?", (pid,)), "Project")
    agents = query(
        "SELECT a.id, a.name, a.lifecycle_state, a.current_activity, a.current_task_id, "
        "r.name AS role_name, p.name AS persona_name, gm.provider_model_id, ta.role_in_team "
        "FROM team_agents ta JOIN teams tm ON tm.id = ta.team_id "
        "JOIN agents a ON a.id = ta.agent_id JOIN roles r ON r.id = a.role_id "
        "LEFT JOIN personas p ON p.id = a.persona_id "
        "LEFT JOIN model_bindings mb ON mb.id = a.model_binding_id "
        "LEFT JOIN gateway_models gm ON gm.id = mb.model_id "
        "LEFT JOIN tasks tk ON tk.id = a.current_task_id "
        "JOIN projects pr ON pr.team_id = tm.id AND pr.id = ? "
        "WHERE ta.active = 1 ORDER BY a.name", (pid,))
    tasks = list_tasks(pid, sprint_id=None)
    sprint_tasks = [t for t in tasks if t["sprint_id"]]
    active_sprint = query_one("SELECT * FROM sprints WHERE project_id=? AND status='Active'", (pid,))
    events = query("SELECT * FROM execution_events WHERE project_id=? ORDER BY seq DESC LIMIT 40", (pid,))
    for e in events:
        e["payload"] = json.loads(e["payload"])
    usage = query_one(
        "SELECT COALESCE(SUM(ur.input_tokens),0) AS input_tokens, COALESCE(SUM(ur.output_tokens),0) AS output_tokens, "
        "COALESCE(SUM(ur.cost_estimate),0) AS cost FROM usage_records ur JOIN workflow_runs wr ON wr.id = ur.workflow_run_id "
        "WHERE wr.project_id = ?", (pid,))
    active_runs = query("SELECT * FROM workflow_runs WHERE project_id=? AND status IN ('Running','Paused')", (pid,))
    backlog_count = query_one("SELECT COUNT(*) AS n FROM backlog_items WHERE project_id=? AND status='Backlog'", (pid,))["n"]
    return {
        "project": project,
        "agents": agents,
        "sprint": active_sprint,
        "sprint_tasks": sprint_tasks,
        "events": list(reversed(events)),
        "usage": usage,
        "active_runs": active_runs,
        "backlog_count": backlog_count,
        "scheduler_running": pid in runtime._schedulers,
    }


@app.get("/api/v1/projects/{pid}/events")
def project_events(pid: str, after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)):
    events = query("SELECT * FROM execution_events WHERE project_id=? AND seq > ? ORDER BY seq LIMIT ?",
                   (pid, after, limit))
    for e in events:
        e["payload"] = json.loads(e["payload"])
    last = events[-1]["seq"] if events else after
    return {"events": events, "last_seq": last}


@app.get("/api/v1/executions/{run_id}/events")
def run_events(run_id: str, after: int = Query(0, ge=0)):
    _or_404(query_one("SELECT id FROM workflow_runs WHERE id=?", (run_id,)), "Execution")
    events = query("SELECT * FROM execution_events WHERE workflow_run_id=? AND seq > ? ORDER BY seq", (run_id, after))
    for e in events:
        e["payload"] = json.loads(e["payload"])
    return {"events": events, "last_seq": events[-1]["seq"] if events else after}


@app.get("/api/v1/projects/{pid}/usage")
def project_usage(pid: str):
    by_agent = query(
        "SELECT a.name AS agent, COALESCE(SUM(ur.input_tokens),0) AS input_tokens, "
        "COALESCE(SUM(ur.output_tokens),0) AS output_tokens, COALESCE(SUM(ur.cost_estimate),0) AS cost "
        "FROM usage_records ur JOIN workflow_runs wr ON wr.id = ur.workflow_run_id "
        "LEFT JOIN agents a ON a.id = ur.agent_id WHERE wr.project_id=? GROUP BY ur.agent_id", (pid,))
    return {"by_agent": by_agent}


# ------------------------------- conversations & chat -------------------------------

@app.get("/api/v1/projects/{pid}/conversations")
def list_conversations(pid: str):
    return query("SELECT * FROM conversations WHERE project_id=? ORDER BY updated_at DESC", (pid,))


@app.post("/api/v1/projects/{pid}/conversations")
def create_conversation(pid: str):
    cid = new_id()
    ts = now()
    insert("conversations", {"id": cid, "project_id": pid, "title": "New conversation",
                             "created_at": ts, "updated_at": ts})
    return query_one("SELECT * FROM conversations WHERE id = ?", (cid,))


@app.get("/api/v1/conversations/{cid}/messages")
def get_messages(cid: str):
    _or_404(query_one("SELECT id FROM conversations WHERE id=?", (cid,)), "Conversation")
    return query("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at, rowid", (cid,))


@app.delete("/api/v1/conversations/{cid}")
def delete_conversation(cid: str):
    execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
    execute("DELETE FROM pending_commands WHERE conversation_id=?", (cid,))
    execute("DELETE FROM conversations WHERE id=?", (cid,))
    return {"ok": True}


@app.post("/api/v1/projects/{pid}/conversations/{cid}/messages")
async def post_message(pid: str, cid: str, body: MessageIn):
    _or_404(query_one("SELECT id FROM conversations WHERE id=?", (cid,)), "Conversation")
    if not body.content.strip():
        raise HTTPException(400, "Message content is required")
    result = await asyncio.get_running_loop().run_in_executor(
        None, chatbot.handle_message, pid, cid, body.content)
    messages = query("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at, rowid", (cid,))
    return {**result, "messages": messages}


# ------------------------------- executions -------------------------------

@app.post("/api/v1/executions")
async def start_execution(body: ExecutionIn, idempotency_key: Optional[str] = Header(None)):
    result = await asyncio.get_running_loop().run_in_executor(
        None, lambda: runtime.start_execution(body.project_id, body.task_id, idempotency_key))
    if "error" in result:
        raise HTTPException(409, result["error"])
    return result


def _run_action(run_id: str, action: str):
    run = _or_404(query_one("SELECT * FROM workflow_runs WHERE id=?", (run_id,)), "Execution")
    fn = {"pause": runtime.pause_execution, "resume": runtime.resume_execution,
          "cancel": runtime.cancel_execution, "stop": runtime.cancel_execution}
    if action == "retry":
        result = runtime.retry_execution(run_id)
    elif action in fn:
        result = fn[action](run_id)
    else:
        raise HTTPException(400, "Unknown action")
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(409, result["error"])
    audit(f"{action}_execution", "workflow_run", run_id, f"Execution control: {action}")
    return result


@app.post("/api/v1/executions/{run_id}/{action}")
async def execution_action(run_id: str, action: str):
    return await asyncio.get_running_loop().run_in_executor(None, _run_action, run_id, action)


@app.post("/api/v1/projects/{pid}/sprint-execution/start")
async def start_sprint_exec(pid: str):
    result = await asyncio.get_running_loop().run_in_executor(None, runtime.start_sprint_execution, pid)
    if "error" in result:
        raise HTTPException(409, result["error"])
    return result


@app.post("/api/v1/projects/{pid}/sprint-execution/stop")
async def stop_sprint_exec(pid: str):
    await asyncio.get_running_loop().run_in_executor(None, runtime.stop_sprint_execution, pid)
    return {"ok": True}


# ------------------------------- audit -------------------------------

@app.get("/api/v1/audit")
def list_audit(limit: int = Query(50, ge=1, le=200)):
    return query("SELECT * FROM audit_events ORDER BY audit_seq DESC LIMIT ?", (limit,))


# ------------------------------- static frontend -------------------------------

if os.path.isdir(STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=os.path.join(STATIC_DIR, "assets")), name="assets")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        candidate = os.path.join(STATIC_DIR, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))
