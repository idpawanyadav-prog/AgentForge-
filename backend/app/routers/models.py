"""Named model catalog routes.

A "model" is a named gateway + model preset stored in the ``model_bindings``
table with no role attached (a global catalog). Agents reference one of these
by id, so updating a model here (e.g. swapping the underlying provider model)
propagates to every agent that uses it.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db import audit, execute, insert, new_id, query, query_one, update

router = APIRouter(prefix="/api/v1", tags=["models"])


class ModelIn(BaseModel):
    name: str
    gateway_id: str
    model_id: str


class ModelUpdate(BaseModel):
    name: str | None = None
    gateway_id: str | None = None
    model_id: str | None = None
    active: int | None = None


_SELECT = (
    "SELECT mb.id, mb.role_id, mb.gateway_id, mb.model_id, mb.active, "
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


@router.get("/models")
def list_models():
    return query(_SELECT + " ORDER BY name")


@router.post("/models")
def create_model(body: ModelIn):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "Model name is required")
    _validate(body.gateway_id, body.model_id)
    mid = new_id()
    insert("model_bindings", {"id": mid, "role_id": None, "gateway_id": body.gateway_id,
                              "model_id": body.model_id, "name": name,
                              "settings_json": "{}", "active": 1})
    audit("create_model", "model", mid, f"Created model '{name}'")
    return query_one(_SELECT + " WHERE mb.id = ?", (mid,))


@router.patch("/models/{mid}")
def update_model(mid: str, body: ModelUpdate):
    existing = _or_404(query_one("SELECT * FROM model_bindings WHERE id=?", (mid,)))
    data = body.model_dump(exclude_unset=True)
    allowed = {k: v for k, v in data.items()
               if k in ("name", "gateway_id", "model_id", "active")}
    if "name" in allowed and not str(allowed["name"]).strip():
        raise HTTPException(422, "Model name cannot be empty")
    if {"gateway_id", "model_id"} & set(allowed):
        _validate(allowed.get("gateway_id", existing["gateway_id"]),
                  allowed.get("model_id", existing["model_id"]))
    if not allowed:
        return query_one(_SELECT + " WHERE mb.id = ?", (mid,))
    update("model_bindings", mid, allowed)
    audit("update_model", "model", mid, f"Updated model fields: {', '.join(allowed)}")
    return query_one(_SELECT + " WHERE mb.id = ?", (mid,))


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
