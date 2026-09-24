"""Settings and audit routes."""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..db import audit, execute, insert, new_id, now, query, query_one, update
from ._util import or_404 as _or_404

router = APIRouter(prefix="/api/v1", tags=["settings"])


def _get_setting(key, default=""):
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def _set_setting(key, value):
    execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def _mask(key_value: str) -> str:
    return "••••" + key_value[-4:] if key_value and len(key_value) >= 4 else "••••"


def _bot_payload(key: str) -> dict:
    bot = {"gateway_id": None, "model_id": None, "gateway_name": None, "model_name": None}
    raw = _get_setting(key, "")
    if raw:
        try:
            cfg = json.loads(raw)
            gw = query_one("SELECT name FROM gateways WHERE id = ?", (cfg.get("gateway_id", ""),))
            m = query_one("SELECT provider_model_id FROM gateway_models WHERE id = ?", (cfg.get("model_id", ""),))
            bot.update({"gateway_id": cfg.get("gateway_id"), "model_id": cfg.get("model_id"),
                        "gateway_name": gw["name"] if gw else None,
                        "model_name": m["provider_model_id"] if m else None})
        except ValueError:
            pass
    return bot


class SettingsIn(BaseModel):
    key: str
    value: str


@router.get("/settings")
def get_settings():
    return {"theme": _get_setting("theme", "dark"),
            "control_bot": _bot_payload("control_bot"),
            "po_bot": _bot_payload("po_bot")}


from ..schemas import BotConfigUpdate

class BotConfig(BaseModel):
    gateway_id: Optional[str] = None
    model_id: Optional[str] = None

def _put_bot_config(key: str, body: BotConfigUpdate, action: str):
    gid, mid = body.gateway_id, body.model_id
    if gid:
        _or_404(query_one("SELECT id FROM gateways WHERE id=?", (gid,)), "Gateway")
    if mid:
        _or_404(query_one("SELECT id FROM gateway_models WHERE id=? AND gateway_id=?", (mid, gid)), "Model")
    _set_setting(key, json.dumps({"gateway_id": gid, "model_id": mid}))
    audit(action, "settings", key,
          f"{key.replace('_', ' ').title()} set to gateway/model ({bool(gid)}, {bool(mid)})")
    return {"ok": True}


@router.put("/settings/control-bot")
def put_control_bot(body: BotConfigUpdate):
    return _put_bot_config("control_bot", body, "configure_control_bot")


@router.put("/settings/po-bot")
def put_po_bot(body: BotConfigUpdate):
    return _put_bot_config("po_bot", body, "configure_po_bot")


@router.put("/settings")
def put_settings(body: SettingsIn):
    _set_setting(body.key, body.value)
    return {"ok": True}


# ------------------ audit ------------------

@router.get("/audit")
def list_audit(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    total = query_one("SELECT COUNT(*) AS n FROM audit_events")["n"]
    items = query("SELECT * FROM audit_events ORDER BY audit_seq DESC LIMIT ? OFFSET ?", (limit, offset))
    return {"total": total, "items": items, "limit": limit, "offset": offset}
