import itertools
from datetime import UTC, date, datetime

from tie.engine.markets import (
    assign_markets,
    cpv_group,
    market_id,
    normalize_province,
    window_start,
)

AS_OF = date(2026, 10, 12)
_seq = itertools.count(1)


def _run(conn, as_of=AS_OF):
    run_id = conn.execute(
        "insert into engine_runs(started_at, as_of, params, status)"
        " values (now(), %s, '{}', 'running') returning run_id",
        (as_of,),
    ).fetchone()["run_id"]
    conn.commit()
    return run_id


def _buyer(conn, province):
    return conn.execute(
        "insert into buyers(name, province) values (%s, %s) returning id",
        (f"Zamawiajacy {next(_seq)}", province),
    ).fetchone()["id"]


def _lots(conn, n, *, cpv, province, published=date(2026, 3, 1), contract=None,
          cpv_main=None):
    """Create ``n`` single-lot procedures of one buyer; return the lot ids."""
    buyer = _buyer(conn, province)
    ids = []
    for _ in range(n):
        k = next(_seq)
        ocds = f"ocds-test-{k}"
        conn.execute(
            "insert into procedures(ocds_id, bzp_number, notice_number, buyer_id, title,"
            " cpv_main, published_at) values (%s, %s, %s, %s, %s, %s, %s)",
            (ocds, f"2026/BZP {k:08d}", f"2026/BZP {k:08d}/01", buyer, f"Zamowienie {k}",
             cpv_main, datetime(published.year, published.month, published.day, 12,
                                tzinfo=UTC)),
        )
        ids.append(conn.execute(
            "insert into lots(procedure_id, lot_no, cpv, outcome, contract_date)"
            " values (%s, 1, %s, 'umowa', %s) returning id",
            (ocds, cpv, contract),
        ).fetchone()["id"])
    conn.commit()
    return ids


def _assigned(conn):
    rows = conn.execute("select lot_id, market_id, run_id from lot_market").fetchall()
    return {r["lot_id"]: r["market_id"] for r in rows}


def _markets(conn):
    return {r["market_id"]: r for r in conn.execute("select * from markets").fetchall()}


def test_cpv_group():
    assert cpv_group("45233140-2", 5) == "45233"
    assert cpv_group("45233140-2", 3) == "452"
    assert cpv_group(" 45233140 - 2", 5) == "45233"
    assert cpv_group("4523", 5) is None
    assert cpv_group(None, 5) is None
    assert cpv_group("", 3) is None
    assert cpv_group("abc", 3) is None


def test_helpers():
    assert market_id("cpv5_prov", "45233", "PL12") == "cpv5_prov:45233:PL12"
    assert market_id("cpv3_pl", "452", None) == "cpv3_pl:452:PL"
    assert normalize_province("PL12") == "PL12"
    assert normalize_province(" pl26 ") == "PL26"
    assert normalize_province("Świętokrzyskie") == "PL26"
    assert normalize_province("województwo małopolskie") == "PL12"
    assert normalize_province("Bawaria") is None
    assert normalize_province(None) is None
    assert window_start(date(2026, 10, 12), 36) == date(2023, 10, 12)
    assert window_start(date(2024, 2, 29), 12) == date(2023, 2, 28)
    assert window_start(date(2026, 1, 31), 2) == date(2025, 11, 30)


def test_large_cpv5_stays_rare_cpv5_falls_back_to_cpv3_prov(db):
    big = _lots(db, 30, cpv="45233140-2", province="PL12")
    rare_a = _lots(db, 10, cpv="45231000-5", province="PL12")
    rare_b = _lots(db, 25, cpv="45232000-2", province="PL12")
    run_id = _run(db)

    assert assign_markets(db, run_id, AS_OF) == 65

    got = _assigned(db)
    assert {got[i] for i in big} == {"cpv5_prov:45233:PL12"}
    assert {got[i] for i in rare_a + rare_b} == {"cpv3_prov:452:PL12"}
    markets = _markets(db)
    assert set(markets) == {"cpv5_prov:45233:PL12", "cpv3_prov:452:PL12"}
    m5 = markets["cpv5_prov:45233:PL12"]
    assert (m5["level"], m5["cpv_group"], m5["province"], m5["n_lots"], m5["run_id"]) == (
        "cpv5_prov", "45233", "PL12", 30, run_id)
    m3 = markets["cpv3_prov:452:PL12"]
    assert (m3["level"], m3["cpv_group"], m3["province"], m3["n_lots"]) == (
        "cpv3_prov", "452", "PL12", 35)


def test_small_cpv3_prov_falls_back_to_poland(db):
    krk = _lots(db, 12, cpv="30192000-1", province="PL12")
    waw = _lots(db, 9, cpv="30197000-6", province="PL14")
    unknown = _lots(db, 2, cpv="30199000-0", province=None)
    by_name = _lots(db, 30, cpv="30192000-1", province="Mazowieckie")
    run_id = _run(db)

    assert assign_markets(db, run_id, AS_OF) == 53

    got = _assigned(db)
    assert {got[i] for i in krk + waw + unknown} == {"cpv3_pl:301:PL"}
    assert {got[i] for i in by_name} == {"cpv5_prov:30192:PL14"}
    pl = _markets(db)["cpv3_pl:301:PL"]
    assert (pl["level"], pl["cpv_group"], pl["province"], pl["n_lots"]) == (
        "cpv3_pl", "301", None, 23)


def test_min_lots_is_configurable(db):
    ids = _lots(db, 5, cpv="33600000-6", province="PL02")
    run_id = _run(db)
    assert assign_markets(db, run_id, AS_OF, min_lots=5) == 5
    assert {_assigned(db)[i] for i in ids} == {"cpv5_prov:33600:PL02"}


def test_lots_outside_window_are_not_assigned(db):
    kw = {"cpv": "45233140-2", "province": "PL12"}
    too_old = _lots(db, 1, published=date(2023, 10, 12), **kw)
    first_day = _lots(db, 1, published=date(2023, 10, 13), **kw)
    last_day = _lots(db, 1, published=AS_OF, **kw)
    future = _lots(db, 1, published=date(2026, 10, 13), **kw)
    old_notice_recent_contract = _lots(db, 1, published=date(2020, 5, 5),
                                       contract=date(2025, 1, 10), **kw)
    recent_notice_old_contract = _lots(db, 1, published=date(2026, 1, 5),
                                       contract=date(2023, 1, 5), **kw)
    run_id = _run(db)

    assert assign_markets(db, run_id, AS_OF) == 3

    assert set(_assigned(db)) == set(first_day + last_day + old_notice_recent_contract)
    for ids in (too_old, future, recent_notice_old_contract):
        assert ids[0] not in _assigned(db)

    assert assign_markets(db, run_id, AS_OF, window_months=60) == 5


def test_lot_cpv_falls_back_to_procedure_cpv_and_missing_cpv_is_skipped(db):
    from_main = _lots(db, 1, cpv=None, cpv_main="71320000-7", province="PL22")
    no_cpv = _lots(db, 1, cpv=None, province="PL22")
    run_id = _run(db)

    assert assign_markets(db, run_id, AS_OF) == 1

    got = _assigned(db)
    assert got[from_main[0]] == "cpv3_pl:713:PL"
    assert no_cpv[0] not in got


def test_rerun_same_run_does_not_duplicate(db):
    _lots(db, 30, cpv="45233140-2", province="PL12")
    _lots(db, 4, cpv="45231000-5", province="PL12")
    run_id = _run(db)

    assert assign_markets(db, run_id, AS_OF) == 34
    first_lots = _assigned(db)
    first_markets = {k: dict(v) for k, v in _markets(db).items()}

    assert assign_markets(db, run_id, AS_OF) == 34
    assert db.execute("select count(*) as n from lot_market").fetchone()["n"] == 34
    assert _assigned(db) == first_lots
    assert {k: dict(v) for k, v in _markets(db).items()} == first_markets


def test_new_run_replaces_assignment_and_drops_lots_that_left_the_window(db):
    stays = _lots(db, 30, cpv="45233140-2", province="PL12", published=date(2025, 6, 1))
    leaves = _lots(db, 2, cpv="45233140-2", province="PL12", published=date(2023, 11, 1))
    first = _run(db)
    assert assign_markets(db, first, AS_OF) == 32
    assert _markets(db)["cpv5_prov:45233:PL12"]["n_lots"] == 32

    later = date(2026, 12, 1)
    second = _run(db, later)
    assert assign_markets(db, second, later) == 30

    rows = db.execute("select lot_id, run_id from lot_market").fetchall()
    assert {r["lot_id"] for r in rows} == set(stays)
    assert {r["run_id"] for r in rows} == {second}
    assert not set(leaves) & {r["lot_id"] for r in rows}
    m = _markets(db)["cpv5_prov:45233:PL12"]
    assert (m["n_lots"], m["run_id"]) == (30, second)
