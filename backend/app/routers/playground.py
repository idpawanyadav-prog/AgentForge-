"""Role playground (AGENT_DEV_SPEEDUP 4.2): preview and run one agent's
composed prompt against a model ad-hoc, without starting a workflow.

Works with DRY_RUN=1 (no provider calls, no gateway needed) so the whole
prompt-iteration loop runs offline; with tracing on, every run lands in
backend/debug/prompts/playground/ like any real call.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import codegen
from ..db import get_gateway_key, query, query_one
from ..prompt_lint import lint_prompt

router = APIRouter(prefix="/api/v1/playground", tags=["playground"])

_DEFAULT_USER_PROMPT = ("Reply with a short acknowledgement that you understand "
                       "your role instructions, then one sentence describing how "
                       "you would approach a small coding task.")


class PlaygroundIn(BaseModel):
    agent_id: str | None = None
    stack: str = "python"
    prompt: str = ""
    system_prompt: str | None = None
    max_tokens: int = 2000


def _agent_ctx(agent_id: str | None):
    if not agent_id:
        return None
    agent = query_one("SELECT * FROM agents WHERE id = ?", (agent_id,))
    if not agent:
        raise HTTPException(404, "Agent not found")
    persona = None
    if agent.get("persona_id"):
        persona = query_one("SELECT * FROM personas WHERE id = ?", (agent["persona_id"],))
    return {"agent": agent, "persona": persona, "role_id": agent.get("role_id")}


def _system_prompt(body: PlaygroundIn) -> str:
    if body.system_prompt is not None:
        return body.system_prompt
    ctx = _agent_ctx(body.agent_id)
    if not ctx:
        return codegen._system_for(body.stack)
    return codegen.build_system_prompt(body.stack, ctx["persona"], ctx["role_id"])


def _pick_gateway_model(agent_id: str | None):
    """First usable (gateway, model): the agent's binding chain head, else the
    first Active gateway that has a key and at least one model."""
    if codegen._dry_run_enabled():
        return None, None
    if agent_id:
        agent = query_one("SELECT model_binding_id FROM agents WHERE id = ?", (agent_id,))
        chain = codegen._binding_chain(agent["model_binding_id"]) if agent else []
        if chain:
            return chain[0]["gw"], chain[0]["model"]
    for g in query("SELECT * FROM gateways WHERE status = 'Active' ORDER BY created_at"):
        if not get_gateway_key(g["id"]):
            continue
        m = query_one(
            "SELECT * FROM gateway_models WHERE gateway_id = ? ORDER BY created_at LIMIT 1",
            (g["id"],))
        if m:
            return g, m
    raise HTTPException(400, "No usable gateway/model found — configure one, or run "
                             "the server with DRY_RUN=1 to exercise prompts offline")


@router.post("/preview")
def preview(body: PlaygroundIn):
    system_prompt = _system_prompt(body)
    return {
        "system_prompt": system_prompt,
        "system_chars": len(system_prompt),
        "user_prompt": body.prompt or _DEFAULT_USER_PROMPT,
        "lint_warnings": lint_prompt(system_prompt),
        "dry_run": codegen._dry_run_enabled(),
    }


@router.post("/run")
def run(body: PlaygroundIn):
    system_prompt = _system_prompt(body)
    user_text = body.prompt or _DEFAULT_USER_PROMPT
    gw, model = _pick_gateway_model(body.agent_id)
    try:
        resp = codegen.call_llm(gw, model, system_prompt, user_text,
                                max_tokens=max(16, min(body.max_tokens, 8000)),
                                timeout=120, trace_label="playground")
    except RuntimeError as exc:
        raise HTTPException(400, f"Playground call failed: {exc}") from exc
    return {
        "text": resp["text"],
        "input_tokens": resp.get("input_tokens", 0),
        "output_tokens": resp.get("output_tokens", 0),
        "latency_ms": resp.get("latency_ms"),
        "dry_run": bool(resp.get("dry_run")),
        "gateway": gw["name"] if gw else None,
        "model": (model or {}).get("provider_model_id") if model else None,
    }
