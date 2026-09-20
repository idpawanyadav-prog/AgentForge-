"""Centralised AgentForge configuration loaded from environment variables."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _resolve_db_path(raw: str) -> str:
    """Return an absolute path for the database file.

    Relative paths resolve against the ``backend/`` directory (this module
    lives in ``backend/app/``), matching the original ``db.py`` layout of
    ``backend/agent_office.db``.
    """
    p = Path(raw)
    if p.is_absolute():
        return str(p)
    return str(Path(__file__).resolve().parent.parent / p)


# Database
# `AGENT_OFFICE_DB` is the historical name (used by the test suite);
# `AGENT_OFFICE_DB_PATH` is the newer alias. Honor both, with the legacy
# name taking precedence so tests keep isolating their throwaway DB.
DB_PATH: str = _resolve_db_path(
    _env("AGENT_OFFICE_DB", _env("AGENT_OFFICE_DB_PATH", "agent_office.db")))

# Workspace root
WORKSPACES_DIR: str = _env("AGENT_OFFICE_WORKSPACES", "Projects")

# Logging
LOG_LEVEL: str = _env("LOG_LEVEL", "INFO")

# Phase/sprint execution timeouts (seconds)
PHASE_TIMEOUT_S: int = _env_int("PHASE_TIMEOUT_S", 300)

# Self-fix loop limits
SELF_FIX_ATTEMPTS: int = _env_int("SELF_FIX_ATTEMPTS", 3)
MAX_REWORK_CYCLES: int = _env_int("MAX_REWORK_CYCLES", 2)
MAX_GATE_CYCLES: int = _env_int("MAX_GATE_CYCLES", 3)

# Default LLM costs ($ per million tokens) — used when gateway_models row has no overrides
LLM_INPUT_COST_PER_M: float = _env_float("LLM_INPUT_COST_PER_M", 2.0)
LLM_OUTPUT_COST_PER_M: float = _env_float("LLM_OUTPUT_COST_PER_M", 8.0)

# Scheduler idle thresholds (rounds without progress before giving up)
SCHEDULER_IDLE_GIVE_UP_BLOCKED: int = _env_int("SCHEDULER_IDLE_GIVE_UP_BLOCKED", 60)
SCHEDULER_IDLE_GIVE_UP_CLEAN: int = _env_int("SCHEDULER_IDLE_GIVE_UP_CLEAN", 600)

# PO / chatbot tick interval (seconds)
PO_TICK_INTERVAL: int = _env_int("PO_TICK_INTERVAL", 10)

# Rate limiting (per client IP over RATE_LIMIT_WINDOW_S seconds)
RATE_LIMIT_DEFAULT: int = _env_int("RATE_LIMIT_DEFAULT", 300)
RATE_LIMIT_MUTATION: int = _env_int("RATE_LIMIT_MUTATION", 120)
RATE_LIMIT_WINDOW_S: int = _env_int("RATE_LIMIT_WINDOW_S", 60)
