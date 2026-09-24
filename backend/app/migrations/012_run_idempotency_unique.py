"""Make execution idempotency keys unique across concurrent workers."""


def apply(conn):
    # Keep the first run as the replay target if older deployments already
    # wrote duplicate keys. Later runs remain in history with no key.
    conn.execute("""
        UPDATE workflow_runs SET idempotency_key = NULL
        WHERE idempotency_key IS NOT NULL AND rowid NOT IN (
            SELECT MIN(rowid) FROM workflow_runs
            WHERE idempotency_key IS NOT NULL GROUP BY idempotency_key
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_runs_idempotency_key
        ON workflow_runs(idempotency_key) WHERE idempotency_key IS NOT NULL
    """)
    conn.commit()
