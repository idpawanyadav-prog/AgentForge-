"""Authoritative project model resolution.

Resolution order:
1. Project default_gateway_id + default_model_id, when valid and active.
2. Project default_gateway_id + preferred active code-generation model.
3. Control bot gateway/model.
4. First active configured gateway/model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..db import get_gateway_key, query, query_one


@dataclass
class ResolvedModel:
    gateway: dict[str, Any]
    model: dict[str, Any]
    source: str


_CODEGEN_MODEL_PREF = (
    "claude-sonnet-5", "claude-sonnet-4-6", "claude-sonnet-4-5",
    "claude-sonnet-4", "claude-opus-5", "claude-opus-4", "gpt-4.1", "gpt-4o",
)
_BAD_MODEL_TOKENS = ("embedding", "whisper", "dall-e", "tts", "image", "fable", "default")


def _has_key(gateway: dict[str, Any] | None) -> bool:
    return bool(gateway and get_gateway_key(gateway["id"]))


def _active_model(model: dict[str, Any] | None) -> bool:
    return bool(model and int(model.get("active", 1)) == 1)


def pick_codegen_model(gateway_id: str) -> dict[str, Any] | None:
    rows = query(
        "SELECT * FROM gateway_models WHERE gateway_id = ? AND active = 1",
        (gateway_id,),
    )
    text_models = [
        r for r in rows
        if not any(b in (r.get("provider_model_id") or "").lower() for b in _BAD_MODEL_TOKENS)
    ]
    for pref in _CODEGEN_MODEL_PREF:
        for model in text_models:
            if model["provider_model_id"] == pref:
                return model
    for pref in _CODEGEN_MODEL_PREF:
        for model in text_models:
            if model["provider_model_id"].startswith(pref):
                return model
    return text_models[0] if text_models else None


def _control_bot_config() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    from ..chatbot import _bot_config

    return _bot_config()


def resolve_project_model(project: dict[str, Any] | None) -> ResolvedModel | None:
    if project:
        gateway_id = project.get("default_gateway_id")
        model_id = project.get("default_model_id")
        if gateway_id:
            gateway = query_one("SELECT * FROM gateways WHERE id = ?", (gateway_id,))
            if _has_key(gateway):
                if model_id:
                    model = query_one(
                        "SELECT * FROM gateway_models WHERE id = ? AND gateway_id = ?",
                        (model_id, gateway_id),
                    )
                    if _active_model(model):
                        return ResolvedModel(gateway, model, "project-explicit")
                model = pick_codegen_model(gateway_id)
                if model:
                    return ResolvedModel(gateway, model, "project-gateway")

    gateway, model = _control_bot_config()
    if _has_key(gateway) and _active_model(model):
        return ResolvedModel(gateway, model, "control-bot-fallback")

    row = query_one(
        "SELECT g.* FROM gateways g "
        "WHERE g.status = 'Active' "
        "AND EXISTS (SELECT 1 FROM gateway_models m WHERE m.gateway_id = g.id AND m.active = 1) "
        "ORDER BY g.created_at LIMIT 1"
    )
    if _has_key(row):
        model = pick_codegen_model(row["id"])
        if model:
            return ResolvedModel(row, model, "global-fallback")
    return None

