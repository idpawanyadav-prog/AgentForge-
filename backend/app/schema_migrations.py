"""Database migration runner.

Tracks schema version in a ``schema_version`` table and applies
incremental migrations from the ``migrations/`` package on startup.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

from app.db import get_db

_SCHEMA_VERSION_TABLE = "schema_version"
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _current_version(conn) -> int:
    """Return the current schema version (0 when the table is absent)."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (_SCHEMA_VERSION_TABLE,),
    ).fetchone()
    if row is None:
        return 0
    v = conn.execute(
        "SELECT MAX(version) FROM " + _SCHEMA_VERSION_TABLE
    ).fetchone()
    return v[0] or 0 if v else 0


def _set_version(conn, version: int):
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_SCHEMA_VERSION_TABLE} "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
    )
    conn.execute(
        f"INSERT OR REPLACE INTO {_SCHEMA_VERSION_TABLE} (version, applied_at) "
        "VALUES (?, ?)",
        (version, __import__("app.db", fromlist=["now"]).now()),
    )
    conn.commit()


def _discover_migrations() -> list[tuple[int, object]]:
    """Return sorted list of (version, module) for migration files."""
    found: list[tuple[int, object]] = []
    if not _MIGRATIONS_DIR.is_dir():
        return found
    # Ensure the package is importable: migrations are imported as
    # "app.migrations.X", so the *backend* dir (parent of app/) must be on
    # sys.path — inserting app/ itself would shadow stdlib names (secrets…).
    pkg_dir = str(_MIGRATIONS_DIR.parent.parent)
    if pkg_dir not in __import__("sys").path:
        __import__("sys").path.insert(0, pkg_dir)
    for fname in sorted(_MIGRATIONS_DIR.iterdir()):
        if fname.suffix != ".py" or fname.name.startswith("_"):
            continue
        try:
            ver = int(fname.stem.split("_")[0])
        except (ValueError, IndexError):
            continue
        mod_name = f"app.migrations.{fname.stem}"
        mod = importlib.import_module(mod_name)
        found.append((ver, mod))
    return found


def run_migrations():
    """Apply any pending migrations and return the final version."""
    conn = get_db()
    current = _current_version(conn)
    migrations = _discover_migrations()
    for version, mod in migrations:
        if version <= current:
            continue
        apply_fn = getattr(mod, "apply", None)
        if apply_fn is None:
            continue
        apply_fn(conn)
        _set_version(conn, version)
        current = version
    return current
