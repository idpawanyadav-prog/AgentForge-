"""seed_if_empty must never wipe a populated DB just because the 'seeded'
flag is missing (audit issue 4.5). Only a fresh DB, or FORCE_RESEED=1, may
trigger the destructive reseed."""
from app import db as appdb


def _seed_flag(value="1"):
    appdb.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('seeded', ?)", (value,))


def _make_gateway(name):
    gid = appdb.new_id()
    ts = appdb.now()
    appdb.insert("gateways", {
        "id": gid, "name": name, "provider": "openai",
        "base_url": "https://api.openai.com/v1", "api_type": "openai-chat",
        "status": "Active", "created_at": ts, "updated_at": ts,
    })
    return gid


def test_missing_flag_with_data_does_not_wipe(monkeypatch):
    monkeypatch.delenv("FORCE_RESEED", raising=False)
    gid = _make_gateway("Keep Me")
    _seed_flag()
    appdb.execute("DELETE FROM settings WHERE key = 'seeded'")

    appdb.seed_if_empty()

    assert appdb.query_one("SELECT id FROM gateways WHERE id = ?", (gid,))
    assert appdb.query_one("SELECT value FROM settings WHERE key = 'seeded'")
    # No demo data was injected on top of the user row.
    assert appdb.query_one("SELECT COUNT(*) AS n FROM gateways")["n"] == 1


def test_fresh_db_seeds_normally(monkeypatch):
    monkeypatch.delenv("FORCE_RESEED", raising=False)
    appdb.seed_if_empty()
    assert appdb.query_one("SELECT value FROM settings WHERE key = 'seeded'")
    assert appdb.query_one("SELECT id FROM gateways LIMIT 1")
