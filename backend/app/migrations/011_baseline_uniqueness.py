"""Allow only one pending baseline of each kind per project."""


def apply(conn):
    # Older databases could already contain duplicates. Keep the newest
    # proposal pending and retain older proposals in the audit history.
    conn.execute("""
        UPDATE project_baselines SET status = 'revision_requested'
        WHERE status = 'pending_approval' AND id NOT IN (
            SELECT b.id FROM project_baselines b
            WHERE b.status = 'pending_approval' AND b.rowid = (
                SELECT MAX(b2.rowid) FROM project_baselines b2
                WHERE b2.project_id = b.project_id AND b2.kind = b.kind
                  AND b2.status = 'pending_approval'
            )
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_baselines_project_kind_pending
        ON project_baselines(project_id, kind)
        WHERE status = 'pending_approval'
    """)
    conn.commit()
