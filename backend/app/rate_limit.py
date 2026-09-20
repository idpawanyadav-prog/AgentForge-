"""Lightweight in-memory rate limiter middleware.

Guards the API against accidental overload or abuse on expensive endpoints
(LLM calls, gateway tests, chat, SSE) without adding a third-party
dependency. Limits are per client IP + HTTP method over a sliding window,
so legitimate polling/SSE traffic is unaffected while a tight burst of
mutations is throttled.

Limits are configurable via environment variables (see ``config.py``).
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

from . import config

# Limits per client IP over WINDOW_S seconds.
DEFAULT_LIMIT = config.RATE_LIMIT_DEFAULT       # GET / HEAD etc.
MUTATION_LIMIT = config.RATE_LIMIT_MUTATION     # POST / PATCH / PUT / DELETE
WINDOW_S = config.RATE_LIMIT_WINDOW_S

_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()
_last_prune = 0.0
_PRUNE_INTERVAL_S = 60.0


def _key(request: Request) -> str:
    client = request.client.host if request.client else "unknown"
    return f"{client}:{request.method}"


def _prune_all(now: float):
    """Drop expired timestamps so memory stays bounded under idle traffic."""
    for key in list(_hits.keys()):
        dq = _hits[key]
        while dq and dq[0] <= now - WINDOW_S:
            dq.popleft()
        if not dq:
            del _hits[key]


def _allow(request: Request) -> bool:
    now = time.monotonic()
    with _lock:
        global _last_prune
        if now - _last_prune > _PRUNE_INTERVAL_S:
            _prune_all(now)
            _last_prune = now
        key = _key(request)
        dq = _hits[key]
        while dq and dq[0] <= now - WINDOW_S:
            dq.popleft()
        limit = (MUTATION_LIMIT
                 if request.method in ("POST", "PATCH", "PUT", "DELETE")
                 else DEFAULT_LIMIT)
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


async def rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api"):
        return await call_next(request)
    if not _allow(request):
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests — slow down and retry shortly"},
        )
    return await call_next(request)
