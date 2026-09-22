"""Shared pytest fixtures.

Every test runs against its own throwaway SQLite file built from the
migration schema only -- no demo seed. Two reasons:

1. Isolation. Without this, the test modules share one cached thread-local
   connection (``app.db`` binds ``DB_PATH`` once per process), so rows written
   by one test leak into the next.
2. Determinism. ``init_db()`` seeds gateways/projects/tasks with foreign-key
   children; the per-test fixtures then delete parents (``projects``,
   ``gateways``) before those children exist and hit
   ``FOREIGN KEY constraint failed``. A clean, unseeded schema makes those
   setup deletes no-ops.

The env var must be set BEFORE ``app.config`` is first imported, otherwise
``DB_PATH`` resolves to the live database and a stray ``init_db()`` at module
import time would seed it.
"""
import os
import sys
import tempfile
from pathlib import Path

# Bind the whole test session to a throwaway file before any app import.
_SESSION_DB = os.path.join(tempfile.mkdtemp(prefix="af_pytest_"), "test.db")
os.environ["AGENT_OFFICE_DB"] = _SESSION_DB

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from app import db as appdb  # noqa: E402


def _close_conn():
    conn = getattr(appdb._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    appdb._local.conn = None


def _build_schema(path: str):
    """Point app.db at ``path`` and create the migration schema (no seed)."""
    _close_conn()
    appdb.DB_PATH = path
    from app.schema_migrations import run_migrations
    run_migrations()
    appdb._legacy_column_migrations()
    appdb._ensure_idempotency_table()
    appdb.get_db().commit()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    _build_schema(str(tmp_path / "test.db"))
    yield
    _close_conn()
