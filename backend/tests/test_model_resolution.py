import json
import os
import sys
import tempfile

_DB = os.path.join(tempfile.mkdtemp(prefix="af_resolver_"), "test.db")
os.environ["AGENT_OFFICE_DB"] = _DB
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from app import db as appdb  # noqa: E402
from app.llm.resolver import resolve_project_model  # noqa: E402

appdb.init_db()


def _clear():
    for tbl in ("settings", "projects", "gateway_models", "gateways"):
        appdb.execute(f"DELETE FROM {tbl}")


@pytest.fixture()
def fx():
    _clear()
    ts = appdb.now()
    for gid, name in (("gw-project", "Project Gateway"), ("gw-control", "Control Gateway")):
        appdb.insert("gateways", {
            "id": gid, "name": name, "provider": "openai",
            "base_url": "https://example.test/v1", "api_type": "openai-chat",
            "status": "Active", "created_at": ts, "updated_at": ts,
        })
        appdb.set_gateway_key(gid, f"key-{gid}")
    models = [
        ("pm1", "gw-project", "claude-sonnet-4-5", 1),
        ("pm2", "gw-project", "gpt-4o", 1),
        ("pm-inactive", "gw-project", "claude-opus-4", 0),
        ("cm1", "gw-control", "gpt-4.1", 1),
    ]
    for mid, gid, provider_id, active in models:
        appdb.insert("gateway_models", {
            "id": mid, "gateway_id": gid, "provider_model_id": provider_id,
            "display_name": provider_id, "capabilities": "", "active": active,
        })
    appdb.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        ("control_bot", json.dumps({"gateway_id": "gw-control", "model_id": "cm1"})),
    )


def _project(**overrides):
    data = {"id": "p1", "default_gateway_id": None, "default_model_id": None}
    data.update(overrides)
    return data


def test_project_explicit_model_wins(fx):
    resolved = resolve_project_model(_project(default_gateway_id="gw-project", default_model_id="pm2"))

    assert resolved is not None
    assert resolved.source == "project-explicit"
    assert resolved.gateway["id"] == "gw-project"
    assert resolved.model["id"] == "pm2"


def test_project_gateway_wins_over_control_bot(fx):
    resolved = resolve_project_model(_project(default_gateway_id="gw-project"))

    assert resolved is not None
    assert resolved.source == "project-gateway"
    assert resolved.gateway["id"] == "gw-project"


def test_control_bot_used_when_project_gateway_missing(fx):
    resolved = resolve_project_model(_project())

    assert resolved is not None
    assert resolved.source == "control-bot-fallback"
    assert resolved.gateway["id"] == "gw-control"


def test_inactive_project_model_falls_back(fx):
    resolved = resolve_project_model(
        _project(default_gateway_id="gw-project", default_model_id="pm-inactive")
    )

    assert resolved is not None
    assert resolved.source == "project-gateway"
    assert resolved.model["id"] != "pm-inactive"


def test_invalid_project_model_not_used(fx):
    resolved = resolve_project_model(_project(default_gateway_id="gw-project", default_model_id="cm1"))

    assert resolved is not None
    assert resolved.source == "project-gateway"
    assert resolved.gateway["id"] == "gw-project"


def test_no_available_model_returns_none(fx):
    _clear()
    assert resolve_project_model(_project()) is None

