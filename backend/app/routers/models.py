"""Named model catalog routes.

A "model" is a named, ordered failover chain stored in the ``model_bindings``
table with no role attached (a global catalog). Each model carries one or more
gateway+model *members* (``members_json``); agents reference a single model by
id and inference walks that model's members in priority order, falling through
to the next when one errors or is unavailable. Updating a model here (e.g.
swapping a member) propagates to every agent that uses it.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db import audit, execute, insert, new_id, query, query_one, update

router = APIRouter(prefix="/api/v1", tags=["models"])


class MemberIn(BaseModel):
    gateway_id: str
    model_id: str


class ModelIn(BaseModel):
    name: str
    gateway_id: str | None = None
    model_id: str | None = None
    members: list[MemberIn] | None = None


class ModelUpdate(BaseModel):
    name: str | None = None
    gateway_id: str | None = None
    model_id: str | None = None
    members: list[MemberIn] | None = None
    active: int | None = None


_SELECT = (
    "SELECT mb.id, mb.role_id, mb.gateway_id, mb.model_id, mb.active, mb.members_json, "
    "COALESCE(NULLIF(mb.name, ''), g.name || ' \u00b7 ' || gm.provider_model_id) AS name, "
    "g.name AS gateway_name, g.provider AS gateway_provider, "
    "gm.provider_model_id, gm.display_name AS model_display_name "
    "FROM model_bindings mb "
    "JOIN gateways g ON g.id = mb.gateway_id "
    "JOIN gateway_models gm ON gm.id = mb.model_id"
)


def _or_404(row, what="Model"):
    if row is None:
        raise HTTPException(404, f"{what} not found")
    return row


def _validate(gateway_id: str, model_id: str):
    if not query_one("SELECT id FROM gateways WHERE id=?", (gateway_id,)):
        raise HTTPException(404, "Gateway not found")
    if not query_one("SELECT id FROM gateway_models WHERE id=? AND gateway_id=?",
                     (model_id, gateway_id)):
        raise HTTPException(422, "Model does not belong to the selected gateway")


def _resolve_members(members, gateway_id, model_id):
    """Collapse the requested members into an ordered, de-duplicated failover
    chain. ``members`` (the explicit ordered list) wins when present; a lone
    ``gateway_id``/``model_id`` pair becomes a one-member chain. Each pair is
    validated against the catalog. Returns ``(members_json, primary_gateway_id,
    primary_model_id)`` — the primary pair is mirrored onto the row's legacy
    ``gateway_id``/``model_id`` columns so cost resolution, the list joins and
    the delete guard keep working. Returns ``(None, None, None)`` when empty."""
    raw = members
    if raw is None:
        raw = [{"gateway_id": gateway_id, "model_id": model_id}] if gateway_id and model_id else []
    cleaned, seen = [], set()
    for m in raw:
        gid = str((m.get("gateway_id") if isinstance(m, dict) else m.gateway_id) or "").strip()
        mid = str((m.get("model_id") if isinstance(m, dict) else m.model_id) or "").strip()
        ref = f"{gid}:{mid}"
        if not gid or not mid or ref in seen:
            continue
        _validate(gid, mid)
        cleaned.append({"gateway_id": gid, "model_id": mid})
        seen.add(ref)
    if not cleaned:
        return None, None, None
    return (json.dumps(cleaned), cleaned[0]["gateway_id"], cleaned[0]["model_id"])


def _members_detail(members_json, gateway_id, model_id):
    """Expand a model's stored chain into display rows, in priority order:
    ``[{gateway_id, gateway_name, gateway_provider, model_id, name}, ...]``.
    Falls back to the row's own gateway/model pair when the chain is empty."""
    try:
        members = json.loads(members_json) if members_json else []
    except (ValueError, TypeError):
        members = []
    if not (isinstance(members, list) and members):
        members = [{"gateway_id": gateway_id, "model_id": model_id}]
    pairs = [(str(m.get("gateway_id")), str(m.get("model_id")))
             for m in members if isinstance(m, dict) and m.get("gateway_id") and m.get("model_id")]
    if not pairs:
        return []
    model_ids = [mid for _, mid in pairs]
    rows = query(
        "SELECT gm.id AS model_id, gm.gateway_id, gm.provider_model_id, "
        "gm.display_name, g.name AS gateway_name, g.provider AS gateway_provider "
        "FROM gateway_models gm JOIN gateways g ON g.id = gm.gateway_id "
        "WHERE gm.id IN (%s)" % ",".join("?" * len(model_ids)), model_ids)
    by_id = {r["model_id"]: r for r in rows}
    detail = []
    for gid, mid in pairs:
        r = by_id.get(mid)
        if not r or r["gateway_id"] != gid:
            continue
        detail.append({
            "gateway_id": gid, "model_id": mid,
            "gateway_name": r["gateway_name"], "gateway_provider": r["gateway_provider"],
            "name": r["display_name"] or r["provider_model_id"],
            "provider_model_id": r["provider_model_id"],
        })
    return detail


def _with_members(row):
    if row:
        row["members"] = _members_detail(row.get("members_json"),
                                         row.get("gateway_id"), row.get("model_id"))
    return row


@router.get("/models")
def list_models():
    return [_with_members(r) for r in query(_SELECT + " ORDER BY name")]


@router.post("/models")
def create_model(body: ModelIn):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "Model name is required")
    members_json, primary_gid, primary_mid = _resolve_members(
        [m.model_dump() for m in body.members] if body.members is not None else None,
        body.gateway_id, body.model_id)
    if not members_json:
        raise HTTPException(422, "Select at least one gateway + model")
    mid = new_id()
    insert("model_bindings", {"id": mid, "role_id": None, "gateway_id": primary_gid,
                              "model_id": primary_mid, "name": name,
                              "members_json": members_json,
                              "settings_json": "{}", "active": 1})
    audit("create_model", "model", mid, f"Created model '{name}'")
    return _with_members(query_one(_SELECT + " WHERE mb.id = ?", (mid,)))


@router.patch("/models/{mid}")
def update_model(mid: str, body: ModelUpdate):
    existing = _or_404(query_one("SELECT * FROM model_bindings WHERE id=?", (mid,)))
    data = body.model_dump(exclude_unset=True)
    allowed = {k: v for k, v in data.items() if k in ("name", "active")}
    if "name" in allowed and not str(allowed["name"]).strip():
        raise HTTPException(422, "Model name cannot be empty")
    if "members" in data or "gateway_id" in data or "model_id" in data:
        members_json, primary_gid, primary_mid = _resolve_members(
            [m.model_dump() for m in body.members] if data.get("members") is not None
            else data.get("members"),
            data.get("gateway_id", existing["gateway_id"]),
            data.get("model_id", existing["model_id"]))
        if not members_json:
            raise HTTPException(422, "Model must keep at least one gateway + model")
        allowed["members_json"] = members_json
        allowed["gateway_id"] = primary_gid
        allowed["model_id"] = primary_mid
    if not allowed:
        return _with_members(query_one(_SELECT + " WHERE mb.id = ?", (mid,)))
    update("model_bindings", mid, allowed)
    audit("update_model", "model", mid, f"Updated model fields: {', '.join(allowed)}")
    return _with_members(query_one(_SELECT + " WHERE mb.id = ?", (mid,)))


@router.delete("/models/{mid}")
def delete_model(mid: str):
    _or_404(query_one("SELECT id FROM model_bindings WHERE id=?", (mid,)))
    used = query_one("SELECT id FROM agents WHERE model_binding_id=? LIMIT 1", (mid,))
    if used:
        update("model_bindings", mid, {"active": 0})
        audit("deactivate_model", "model", mid, "Model in use by an agent; deactivated")
        return {"ok": True, "deactivated": True}
    execute("DELETE FROM model_bindings WHERE id=?", (mid,))
    audit("delete_model", "model", mid, "Deleted model")
    return {"ok": True}
