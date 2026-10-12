from collections import Counter

import psycopg
import pytest

from tie.engine.oecd import CATALOG, SECTIONS, seed_catalog

ENGINE_TABLES = {
    "engine_runs", "markets", "lot_market", "test_results", "pairs", "clusters", "signals",
    "explanations", "oecd_catalog",
}


def test_catalog_has_51_unique_items():
    assert len(CATALOG) == 51
    items = [c.item for c in CATALOG]
    assert len(set(items)) == 51


def test_catalog_status_counts():
    assert Counter(c.status for c in CATALOG) == {"A": 14, "C": 12, "N": 25}


def test_catalog_sections_and_fields():
    assert Counter(c.section for c in CATALOG) == {
        "3.1": 8, "3.2": 11, "3.3": 13, "3.4": 9, "3.5": 10,
    }
    assert set(SECTIONS) == {"3.1", "3.2", "3.3", "3.4", "3.5"}
    for c in CATALOG:
        assert c.item.startswith(c.section + ".")
        assert c.title and c.how
        text = c.title + c.how
        assert chr(0x2014) not in text and chr(0x2013) not in text


def test_catalog_known_rows():
    by_item = {c.item: c for c in CATALOG}
    assert by_item["3.1.6"].title == "Wygrywanie na zmianę"
    assert by_item["3.1.6"].status == "A"
    assert by_item["3.1.6"].how == "wskaźnik rotacji w parach i klastrach"
    assert by_item["3.3.6"].how.startswith("stałość stosunków cen par oferentów")
    assert by_item["3.5.10"].status == "C"
    for n in range(1, 10):
        row = by_item[f"3.4.{n}"]
        assert row.status == "N"
        assert row.how == "wypowiedzi nie są w danych publicznych"


def test_engine_tables_exist(db):
    rows = db.execute(
        "select table_name from information_schema.tables where table_schema='public'"
    ).fetchall()
    assert ENGINE_TABLES <= {r["table_name"] for r in rows}
    names = {r["name"] for r in db.execute("select name from schema_migrations").fetchall()}
    assert "005_engine.sql" in names


def test_pairs_require_a_lower_than_b(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "insert into pairs(run_id, a, b, market_id, n_together) values (1, 5, 3, 'm', 2)"
        )
    db.rollback()


def test_signal_status_is_checked(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("insert into signals(signal_id, status) values ('s1', 'draft')")
    db.rollback()


def test_seed_catalog_is_idempotent(db):
    assert seed_catalog(db) == 51
    assert seed_catalog(db) == 51
    rows = db.execute("select item, section, title, status, how from oecd_catalog").fetchall()
    assert len(rows) == 51
    assert Counter(r["status"] for r in rows) == {"A": 14, "C": 12, "N": 25}
    row = next(r for r in rows if r["item"] == "3.2.11")
    assert row["section"] == "3.2" and row["status"] == "A"


def test_seed_catalog_overwrites_and_prunes(db):
    seed_catalog(db)
    db.execute("update oecd_catalog set title = 'x' where item = '3.1.1'")
    db.execute(
        "insert into oecd_catalog(item, section, title, status, how)"
        " values ('9.9.9', '9.9', 'stary', 'N', 'stary')"
    )
    db.commit()
    assert seed_catalog(db) == 51
    assert db.execute("select count(*) c from oecd_catalog").fetchone()["c"] == 51
    title = db.execute("select title from oecd_catalog where item = '3.1.1'").fetchone()["title"]
    assert title == "Przetargi często wygrywa ten sam dostawca"
