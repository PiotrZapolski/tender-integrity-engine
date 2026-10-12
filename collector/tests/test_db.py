from importlib import resources

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


def test_migrations_are_package_resources():
    assert resources.files("tie.migrations").joinpath("001_init.sql").is_file()
    assert resources.files("tie.migrations").joinpath("002_identity_failures.sql").is_file()


def test_migration_002_applied(db):
    names = {r["name"] for r in db.execute("select name from schema_migrations").fetchall()}
    assert "002_identity_failures.sql" in names
    col = db.execute(
        "select data_type, column_default, is_nullable from information_schema.columns"
        " where table_name = 'bids' and column_name = 'identity_failures'").fetchone()
    assert col["data_type"] == "smallint" and col["is_nullable"] == "NO"
    assert col["column_default"] == "0"


def test_pg_trgm_enabled(db):
    row = db.execute("select extname from pg_extension where extname='pg_trgm'").fetchone()
    assert row is not None
