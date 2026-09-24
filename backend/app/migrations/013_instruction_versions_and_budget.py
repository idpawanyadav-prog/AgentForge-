"""Migration 013 — developer speed-up (AGENT_DEV_SPEEDUP 2.4/4.3/3.3).

instruction_file_versions: full history of every instruction-file content
change so a role can be rolled back to any earlier version, and the original
seeded content acts as the "baseline" for a reset button. (Personas already
have persona_versions from an earlier migration; this mirrors it for role
instruction files.)

projects.budget_usd: optional per-project LLM cost ceiling. 0 (default)
means "no limit", preserving current behavior for every existing project.
"""


def apply(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS instruction_file_versions (
          id TEXT PRIMARY KEY,
          instruction_file_id TEXT NOT NULL REFERENCES instruction_files(id),
          version INTEGER NOT NULL,
          filename TEXT NOT NULL DEFAULT '',
          content TEXT NOT NULL DEFAULT '',
          checksum TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ifv_file "
        "ON instruction_file_versions(instruction_file_id, version)")

    cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
    if "budget_usd" not in cols:
        conn.execute("ALTER TABLE projects ADD COLUMN budget_usd REAL NOT NULL DEFAULT 0")

    # Backfill: every existing instruction file gets a snapshot at its
    # current version so history/restore works from day one (content before
    # this migration is unrecoverable; this snapshot is the best baseline).
    rows = conn.execute(
        "SELECT id, version, filename, content, created_at FROM instruction_files "
        "WHERE id NOT IN (SELECT instruction_file_id FROM instruction_file_versions)"
    ).fetchall()
    for rid, version, filename, content, created_at in rows:
        conn.execute(
            "INSERT INTO instruction_file_versions "
            "(id, instruction_file_id, version, filename, content, checksum, created_at) "
            "VALUES (lower(hex(randomblob(16))), ?, ?, ?, ?, ?, ?)",
            (rid, version or 1, filename, content or "", "", created_at or ""))
    conn.commit()
