"""Gateway health checks that perform real provider probes."""
from __future__ import annotations

import json
import socket
import time
import urllib.error as uerr
import urllib.parse as uparse
import urllib.request as ureq
from dataclasses import dataclass, field
from typing import Any

from .. import db


@dataclass
class GatewayHealthResult:
    ok: bool
    status: str
    latency_ms: int
    checks: dict[str, str] = field(default_factory=dict)
    model_count: int = 0
    diagnostic: str = ""
    error_code: str = ""
    models: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "checks": self.checks,
            "model_count": self.model_count,
            "diagnostic": self.diagnostic,
            "error_code": self.error_code,
            "models": self.models,
        }


def _base_with_v1(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    return base if "/v1" in base else base + "/v1"


def _map_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, uerr.HTTPError):
        if exc.code == 401:
            return "unauthorized", "Gateway rejected the API key"
        if exc.code == 403:
            return "forbidden", "Gateway refused access"
        if exc.code == 429:
            return "rate_limited", "Gateway rate limit reached"
        return "provider_error", f"Gateway returned HTTP {exc.code}"
    reason = getattr(exc, "reason", None)
    if isinstance(reason, socket.gaierror):
        return "dns_error", "Gateway host could not be resolved"
    if isinstance(reason, ConnectionRefusedError):
        return "connection_refused", "Gateway refused the TCP connection"
    msg = str(exc).lower()
    if "timed out" in msg or isinstance(exc, TimeoutError):
        return "timeout", "Gateway did not respond before the timeout"
    if "certificate" in msg or "ssl" in msg or "tls" in msg:
        return "tls_error", "Gateway TLS/certificate validation failed"
    return "provider_error", f"Could not reach gateway: {exc}"


def test_gateway_models(gateway: dict[str, Any]) -> GatewayHealthResult:
    started = time.perf_counter()
    checks = {"url": "pending", "authentication": "pending", "models_endpoint": "pending"}
    try:
        parsed = uparse.urlparse(gateway.get("base_url") or "")
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return GatewayHealthResult(
                False, "Failed", int((time.perf_counter() - started) * 1000),
                checks={"url": "failed"},
                diagnostic="Invalid Base URL scheme; expected http(s)",
                error_code="invalid_url",
            )
        checks["url"] = "passed"
        api_key = db.get_gateway_key(gateway["id"])
        if not api_key:
            return GatewayHealthResult(
                False, "Failed", int((time.perf_counter() - started) * 1000),
                checks={**checks, "authentication": "failed"},
                diagnostic="Gateway key is not configured or could not be decrypted",
                error_code="unauthorized",
            )
        checks["authentication"] = "passed"

        provider = (gateway.get("provider") or "").lower()
        if provider == "ollama":
            url = gateway["base_url"].rstrip("/") + "/api/tags"
            req = ureq.Request(url)
            with ureq.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            ids = [m.get("name") for m in data.get("models", []) if m.get("name")]
        else:
            url = _base_with_v1(gateway["base_url"]) + "/models"
            headers = {"Authorization": f"Bearer {api_key}"}
            if gateway.get("api_type") == "anthropic-messages":
                headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
            req = ureq.Request(url, headers=headers)
            with ureq.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        checks["models_endpoint"] = "passed"
        return GatewayHealthResult(
            True, "Success", int((time.perf_counter() - started) * 1000),
            checks=checks, model_count=len(ids), models=ids,
            diagnostic="Gateway reachable and authenticated",
        )
    except json.JSONDecodeError:
        return GatewayHealthResult(
            False, "Failed", int((time.perf_counter() - started) * 1000),
            checks={**checks, "models_endpoint": "failed"},
            diagnostic="Gateway returned invalid JSON",
            error_code="invalid_response",
        )
    except Exception as exc:
        code, diagnostic = _map_error(exc)
        return GatewayHealthResult(
            False, "Failed", int((time.perf_counter() - started) * 1000),
            checks={**checks, "models_endpoint": "failed"},
            diagnostic=diagnostic,
            error_code=code,
        )

