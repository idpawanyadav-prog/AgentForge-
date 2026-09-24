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
import uuid
import os
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

_redis = None
if os.environ.get("RATE_LIMIT_REDIS_URL"):
    try:
        import redis
        _redis = redis.Redis.from_url(os.environ["RATE_LIMIT_REDIS_URL"],
                                      socket_connect_timeout=0.5, socket_timeout=0.5)
    except (ImportError, ValueError):
        _redis = None

_REDIS_SLIDING_WINDOW = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1] - ARGV[2])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
redis.call('EXPIRE', KEYS[1], math.ceil(ARGV[2]) + 1)
return 1
"""


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
    global _redis
    limit = (MUTATION_LIMIT
             if request.method in ("POST", "PATCH", "PUT", "DELETE")
             else DEFAULT_LIMIT)
    if _redis is not None:
        try:
            return bool(_redis.eval(
                _REDIS_SLIDING_WINDOW, 1, f"agentforge:rate:{_key(request)}",
                time.time(), WINDOW_S, limit, uuid.uuid4().hex))
        except Exception:
            # An unavailable Redis server must not take down the local API.
            _redis = None
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
