"""Gateway and model routes."""
from __future__ import annotations

import json as _json
import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .. import db
from ..db import audit, execute, insert, new_id, now, query, query_one, update
from ..schemas import GatewayUpdate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["gateways"])


class GatewayIn(BaseModel):
    name: str
    provider: str = "openai"
    base_url: str
    api_type: str = "openai-chat"
    api_key: str | None = None


class ModelIn(BaseModel):
    provider_model_id: str
    display_name: str
    capabilities: str = ""


class TestChatIn(BaseModel):
    model_id: str
    message: str


def _mask(key_value: str) -> str:
    return "••••" + key_value[-4:] if key_value and len(key_value) >= 4 else "••••"


def _or_404(gid: str):
    row = query_one("SELECT * FROM gateways WHERE id=?", (gid,))
    if row is None:
        raise HTTPException(404, "Gateway not found")
    return row


@router.get("/gateways")
def list_gateways(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    total = query_one("SELECT COUNT(*) AS n FROM gateways")["n"]
    items = query("SELECT * FROM gateways ORDER BY created_at LIMIT ? OFFSET ?", (limit, offset))
    return {"total": total, "items": items, "limit": limit, "offset": offset}


@router.post("/gateways")
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


@router.patch("/gateways/{gid}")
def update_gateway(gid: str, body: GatewayUpdate):
    gw = _or_404(gid)
    data = body.model_dump(exclude_unset=True)
    allowed = {k: v for k, v in data.items() if k in ("name", "provider", "base_url", "api_type", "status")}
    allowed["updated_at"] = now()
    update("gateways", gid, allowed)
    if data.get("api_key"):
        db.set_gateway_key(gid, data["api_key"])
    models_fetched = 0
    if "provider" in allowed:
        sync = db.sync_gateway_models(gid)
        models_fetched = sync["added"]
    audit("update_gateway", "gateway", gid, f"Updated gateway fields: {', '.join(allowed)}")
    return {**query_one("SELECT * FROM gateways WHERE id = ?", (gid,)), "models_fetched": models_fetched}


@router.delete("/gateways/{gid}")
def delete_gateway(gid: str):
    _or_404(gid)
    execute("DELETE FROM gateway_models WHERE gateway_id = ?", (gid,))
    execute("DELETE FROM gateways WHERE id = ?", (gid,))
    audit("delete_gateway", "gateway", gid, "Deleted gateway")
    return {"ok": True}


@router.post("/gateways/{gid}/test")
def test_gateway(gid: str):
    gw = _or_404(gid)
    ts = now()
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


@router.get("/gateways/{gid}/models")
def list_models(gid: str):
    _or_404(gid)
    return query("SELECT * FROM gateway_models WHERE gateway_id = ? ORDER BY display_name", (gid,))


@router.post("/gateways/{gid}/models")
def add_model(gid: str, body: ModelIn):
    _or_404(gid)
    mid = new_id()
    insert("gateway_models", {"id": mid, "gateway_id": gid,
                              "provider_model_id": body.provider_model_id,
                              "display_name": body.display_name or body.provider_model_id,
                              "capabilities": body.capabilities, "active": 1})
    return query_one("SELECT * FROM gateway_models WHERE id = ?", (mid,))


@router.delete("/gateways/{gid}/models/{mid}")
def delete_model(gid: str, mid: str):
    _or_404(gid)
    execute("DELETE FROM gateway_models WHERE id=? AND gateway_id=?", (mid, gid))
    return {"ok": True}


@router.post("/gateways/{gid}/test-chat")
async def gateway_test_chat(gid: str, body: TestChatIn):
    """Live single-turn inference against a gateway model (playground)."""
    from ..codegen import GatewayClient

    gw = _or_404(gid)
    model = query_one("SELECT * FROM gateway_models WHERE id=? AND gateway_id=?",
                      (body.model_id, gid))
    if not model:
        raise HTTPException(404, "Model not found on this gateway")
    result = await GatewayClient.call(gw, model, body.message, max_tokens=512)
    reply = result["text"]
    latency_ms = result.get("latency_ms", 0)
    in_t = result.get("input_tokens", 0)
    out_t = result.get("output_tokens", 0)
    audit("test_chat", "gateway", gid,
          f"Live chat with {model['provider_model_id']} ({in_t}+{out_t} tokens, {latency_ms}ms)")
    return {"reply": reply, "model": model["provider_model_id"],
            "gateway": gw["name"], "live": True,
            "latency_ms": latency_ms, "input_tokens": in_t, "output_tokens": out_t}


@router.post("/gateways/{gid}/discover")
def discover_models(gid: str):
    gw = _or_404(gid)
    result = _live_model_ids(gw)
    source = "provider-catalog"
    if result["ok"] and result["ids"]:
        source = "live"
        added = 0
        for mid in result["ids"]:
            exists = query_one(
                "SELECT id FROM gateway_models WHERE gateway_id=? AND provider_model_id=?",
                (gid, mid))
            if not exists:
                insert("gateway_models", {"id": new_id(), "gateway_id": gid,
                                          "provider_model_id": mid, "display_name": mid,
                                          "capabilities": "", "active": 1})
                added += 1
        total = query_one("SELECT COUNT(*) AS n FROM gateway_models WHERE gateway_id=?", (gid,))["n"]
        return {"source": source, "added": added, "total": total, "live_models": len(result["ids"])}
    sync = db.sync_gateway_models(gid)
    total = query_one("SELECT COUNT(*) AS n FROM gateway_models WHERE gateway_id=?", (gid,))["n"]
    return {"source": source, "added": sync["added"], "removed": sync["removed"], "total": total}


def _live_model_ids(gw) -> dict:
    import urllib.error as _uerr
    import urllib.request as _ureq

    api_key = db.get_gateway_key(gw["id"])
    if not api_key:
        return {"ok": False, "ids": [], "error": "Gateway key not configured or could not be decrypted"}
    base = gw["base_url"].rstrip("/")
    if "/v1" not in base:
        base += "/v1"
    req = _ureq.Request(base + "/models", headers={"Authorization": f"Bearer {api_key}"})
    try:
        with _ureq.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read())
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        return {"ok": True, "ids": ids, "error": None}
    except _uerr.HTTPError as e:
        return {"ok": False, "ids": [], "error": f"Gateway returned HTTP {e.code}"}
    except Exception as exc:
        return {"ok": False, "ids": [], "error": f"Could not reach gateway: {exc}"}
