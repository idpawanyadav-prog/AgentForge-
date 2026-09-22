"""Migration 008 — add project-level default model binding."""


def apply(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
    if "default_model_id" not in cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN default_model_id TEXT REFERENCES gateway_models(id)"
        )
    conn.commit()
