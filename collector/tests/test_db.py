from tie.db import migrate

EXPECTED = {
    "schema_migrations", "buyers", "procedures", "lots", "bidders", "bids",
    "documents", "jobs", "ai_calls", "bidder_accounts", "wl_checks",
}


def test_tables_exist(db):
    rows = db.execute(
        "select table_name from information_schema.tables where table_schema='public'"
    ).fetchall()
    assert EXPECTED <= {r["table_name"] for r in rows}


def test_migrate_is_idempotent(db):
    assert migrate(db) == []


def test_pg_trgm_enabled(db):
    row = db.execute("select extname from pg_extension where extname='pg_trgm'").fetchone()
    assert row is not None
