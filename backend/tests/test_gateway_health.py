import json
import os
import sys
import tempfile
import urllib.error

_DB = os.path.join(tempfile.mkdtemp(prefix="af_health_"), "test.db")
os.environ["AGENT_OFFICE_DB"] = _DB
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from app import db as appdb  # noqa: E402
from app.llm import health  # noqa: E402

appdb.init_db()


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


@pytest.fixture()
def gateway():
    appdb.execute("DELETE FROM gateway_models")
    appdb.execute("DELETE FROM gateways")
    ts = appdb.now()
    appdb.insert("gateways", {
        "id": "gw", "name": "GW", "provider": "openai",
        "base_url": "https://example.test/v1", "api_type": "openai-chat",
        "status": "Active", "created_at": ts, "updated_at": ts,
    })
    appdb.set_gateway_key("gw", "secret-key")
    return appdb.query_one("SELECT * FROM gateways WHERE id='gw'")


def test_gateway_test_200(monkeypatch, gateway):
    monkeypatch.setattr(
        health.ureq, "urlopen",
        lambda *a, **k: _Resp(json.dumps({"data": [{"id": "gpt-test"}]}).encode()),
    )

    result = health.test_gateway_models(gateway)

    assert result.ok
    assert result.status == "Success"
    assert result.model_count == 1
    assert result.models == ["gpt-test"]


def test_gateway_test_401(monkeypatch, gateway):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("url", 401, "Unauthorized", hdrs=None, fp=None)

    monkeypatch.setattr(health.ureq, "urlopen", fail)

    result = health.test_gateway_models(gateway)

    assert not result.ok
    assert result.error_code == "unauthorized"


def test_gateway_test_429(monkeypatch, gateway):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("url", 429, "Rate limited", hdrs=None, fp=None)

    monkeypatch.setattr(health.ureq, "urlopen", fail)

    result = health.test_gateway_models(gateway)

    assert not result.ok
    assert result.error_code == "rate_limited"


def test_gateway_test_timeout(monkeypatch, gateway):
    monkeypatch.setattr(health.ureq, "urlopen", lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))

    result = health.test_gateway_models(gateway)

    assert not result.ok
    assert result.error_code == "timeout"


def test_gateway_test_invalid_json(monkeypatch, gateway):
    monkeypatch.setattr(health.ureq, "urlopen", lambda *a, **k: _Resp(b"not-json"))

    result = health.test_gateway_models(gateway)

    assert not result.ok
    assert result.error_code == "invalid_response"


def test_gateway_latency_recorded(monkeypatch, gateway):
    monkeypatch.setattr(health.ureq, "urlopen", lambda *a, **k: _Resp(b'{"data": []}'))

    result = health.test_gateway_models(gateway)

    assert isinstance(result.latency_ms, int)
    assert result.latency_ms >= 0

