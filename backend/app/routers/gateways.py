"""Gateway and model routes."""
from __future__ import annotations

import asyncio
import json as _json
import logging
import httpx
import ipaddress
import os
import socket
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .. import db
from ..db import audit, execute, insert, new_id, now, query, query_one, update
from ..llm.health import test_gateway_models
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
    # FK guards: model_bindings and project defaults reference this
    # gateway's rows; without these checks the delete 500s on FK violation.
    if query_one("SELECT id FROM model_bindings WHERE gateway_id=? LIMIT 1", (gid,)):
        raise HTTPException(409, "Gateway is used by models in the catalog — "
                                 "delete or re-point those models first")
    if query_one("SELECT id FROM projects WHERE default_gateway_id=? OR default_model_id IN "
                 "(SELECT id FROM gateway_models WHERE gateway_id=?) LIMIT 1", (gid, gid)):
        raise HTTPException(409, "Gateway (or one of its models) is a project default — "
                                 "pick another gateway for those projects first")
    execute("DELETE FROM gateway_models WHERE gateway_id = ?", (gid,))
    execute("DELETE FROM gateways WHERE id = ?", (gid,))
    audit("delete_gateway", "gateway", gid, "Deleted gateway")
    return {"ok": True}


@router.post("/gateways/{gid}/test")
def test_gateway(gid: str):
    gw = _or_404(gid)
    ts = now()
    health = test_gateway_models(gw)
    update("gateways", gid, {
        "last_tested_at": ts,
        "test_status": health.status,
        "test_diagnostic": health.diagnostic,
    })
    models_fetched, models_removed = 0, 0
    if health.ok:
        for provider_model_id in health.models:
            exists = query_one(
                "SELECT id FROM gateway_models WHERE gateway_id=? AND provider_model_id=?",
                (gid, provider_model_id),
            )
            if not exists:
                insert("gateway_models", {
                    "id": new_id(), "gateway_id": gid,
                    "provider_model_id": provider_model_id,
                    "display_name": provider_model_id,
                    "capabilities": "", "active": 1,
                })
                models_fetched += 1
    audit("test_gateway", "gateway", gid,
          f"Gateway test: {health.status}; catalog +{models_fetched}/-{models_removed}")
    return {
        **health.as_dict(),
        "tested_at": ts,
        "models_fetched": models_fetched,
        "models_removed": models_removed,
    }


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
    if query_one("SELECT id FROM model_bindings WHERE model_id=? LIMIT 1", (mid,)):
        raise HTTPException(409, "Model is referenced by catalog entries — "
                                 "remove it from those models first")
    if query_one("SELECT id FROM projects WHERE default_model_id=? LIMIT 1", (mid,)):
        raise HTTPException(409, "Model is a project default — clear that first")
    execute("DELETE FROM gateway_models WHERE id=? AND gateway_id=?", (mid, gid))
    return {"ok": True}


@router.post("/gateways/{gid}/test-chat")
def gateway_test_chat(gid: str, body: TestChatIn):
    """Live single-turn inference against a gateway model (playground)."""
    from ..codegen import GatewayClient

    gw = _or_404(gid)
    model = query_one("SELECT * FROM gateway_models WHERE id=? AND gateway_id=?",
                      (body.model_id, gid))
    if not model:
        raise HTTPException(404, "Model not found on this gateway")
    result = GatewayClient.call(gw, model, body.message, max_tokens=512)
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
async def discover_models(gid: str):
    gw = _or_404(gid)
    result = await _live_model_ids(gw)
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


async def _live_model_ids(gw) -> dict:
    api_key = db.get_gateway_key(gw["id"])
    if not api_key:
        return {"ok": False, "ids": [], "error": "Gateway key not configured or could not be decrypted"}
    try:
        url = await _models_discovery_url(gw["base_url"])
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url,
                                        headers={"Authorization": f"Bearer {api_key}"})
            response.raise_for_status()
            data = response.json()
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        return {"ok": True, "ids": ids, "error": None}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "ids": [],
                "error": f"Gateway returned HTTP {exc.response.status_code}"}
    except Exception as exc:
        return {"ok": False, "ids": [], "error": f"Could not reach gateway: {exc}"}


async def _models_discovery_url(base_url: str) -> str:
    """Validate the destination at call time; allow loopback for local models."""
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Gateway URL must be an HTTP(S) host without credentials")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"metadata.google.internal", "metadata", "instance-data"}:
        raise ValueError("Gateway host is reserved for instance metadata")
    allow_private = os.environ.get("AGENTFORGE_ALLOW_PRIVATE_GATEWAYS") == "1"
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, host, parsed.port or
                                            (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError(f"Gateway host could not be resolved: {exc}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if ip.is_loopback:
            continue
        if not allow_private and not ip.is_global:
            raise ValueError("Gateway host resolves to a private or reserved address")
    path = parsed.path.rstrip("/")
    if not path.lower().endswith("/v1"):
        path += "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path + "/models", "", ""))
