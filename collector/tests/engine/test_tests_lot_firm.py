import statistics
from decimal import Decimal

import pytest

from tie.engine.tests_firm import compute_firm_tests
from tie.engine.tests_lot import compute_lot_tests

from .synth import make_market

COVER = {"members": 3, "pattern": "cover", "share": 0.5}


def _run(db, as_of):
    run_id = db.execute(
        "insert into engine_runs(as_of) values (%s) returning run_id", (as_of,)
    ).fetchone()["run_id"]
    db.commit()
    return run_id


def _assign(db, run_id, out, level="cpv5_prov"):
    """Put every lot of a synthetic market into one market of the run (as Task 2 would)."""
    group = out["cpv"].replace("-", "")[:5]
    market_id = f"{level}:{group}:{out['province']}"
    db.execute(
        "insert into markets(market_id, cpv_group, province, level, n_lots, run_id)"
        " values (%s, %s, %s, %s, %s, %s)",
        (market_id, group, out["province"], level, len(out["lots"]), run_id),
    )
    with db.cursor() as cur:
        cur.executemany(
            "insert into lot_market(lot_id, market_id, run_id) values (%s, %s, %s)",
            [(lot, market_id, run_id) for lot in out["lots"]],
        )
    db.commit()
    return market_id


def _results(db, run_id, test_id, level):
    rows = db.execute(
        "select * from test_results where run_id = %s and test_id = %s and level = %s",
        (run_id, test_id, level),
    ).fetchall()
    return {r["entity_key"]: r for r in rows}


def _expected_pct(values):
    n = len(values)
    return {k: (1 + sum(w < v for w in values.values())) / n for k, v in values.items()}


def _bids(db, lot_id):
    return db.execute(
        "select * from bids where lot_id = %s order by offer_no", (lot_id,)
    ).fetchall()


def _add_firm(db, name, nip):
    return db.execute(
        "insert into bidders(nip, name, name_norm, is_jdg) values (%s, %s, %s, false)"
        " returning id",
        (nip, name, name.lower()),
    ).fetchone()["id"]


def _add_bid(db, lot_id, bidder_id, name, price, *, offer_no=99, outside=False):
    doc = db.execute(
        "select d.id from documents d join lots l on l.procedure_id = d.procedure_id"
        " where l.id = %s",
        (lot_id,),
    ).fetchone()["id"]
    db.execute(
        "insert into bids(lot_id, document_id, offer_no, bidder_id, bidder_name_raw, price,"
        " price_scale, is_winner, parse_method, outside_bzp_range)"
        " values (%s, %s, %s, %s, %s, %s, 1, false, 'synth', %s)",
        (lot_id, doc, offer_no, bidder_id, name, price, outside),
    )


def test_cover_bids_give_high_relative_distance_percentile(db):
    out = make_market(db, seed=11, n_firms=12, n_lots=200, cpv="45233142-6",
                      province_code="PL22", cartel=COVER)
    run_id = _run(db, out["as_of"])
    market = _assign(db, run_id, out)
    written = compute_lot_tests(db, run_id)
    assert written == db.execute(
        "select count(*) c from test_results where run_id = %s and level = 'lot'", (run_id,)
    ).fetchone()["c"]

    rd = _results(db, run_id, "3.3.7", "lot")
    assert rd and all(r["market_id"] == market and r["n_obs"] == len(rd) for r in rd.values())
    assert all(0 < r["percentile"] <= 1 for r in rd.values())
    members = set(out["cartel"])
    cartel_lots = set(out["cartel_lots"])
    # Every cartel lot has the 3 members, so 3+ valid offers and a 3.3.7 result.
    assert all(str(lot) in rd for lot in cartel_lots)
    # Lots where only the cartel bid: the losing members sit 5-8% above the winner.
    cartel_only = [lot for lot in cartel_lots
                   if {b["bidder_id"] for b in _bids(db, lot)} <= members]
    assert len(cartel_only) >= 5
    cartel_pct = [rd[str(lot)]["percentile"] for lot in cartel_only]
    normal_pct = [r["percentile"] for k, r in rd.items() if int(k) not in cartel_lots]
    assert statistics.median(cartel_pct) >= 0.85
    assert statistics.median(normal_pct) <= 0.75
    assert statistics.fmean(cartel_pct) - statistics.fmean(normal_pct) >= 0.25
    assert sum(p >= 0.9 for p in normal_pct) <= 0.15 * len(normal_pct)

    # A second pass in the same run replaces the rows instead of adding to them.
    assert compute_lot_tests(db, run_id) == written


def test_lot_tests_use_scaled_valid_prices(db):
    out = make_market(db, seed=9, n_firms=6, n_lots=30, cpv="90611000-3", province_code="PL12")
    run_id = _run(db, out["as_of"])
    _assign(db, run_id, out)
    multi = [lot for lot in out["lots"] if len(_bids(db, lot)) >= 3]
    assert len(multi) >= 3
    rd_lot, dup_lot, outside_lot = multi[:3]

    # 3.3.7: one offer stored at half price with scale 2 (= 100.00), losers 150, 151, ...
    # and a cheaper offer outside the BZP range that must be ignored.
    bids = _bids(db, rd_lot)
    db.execute("update bids set price = 50.00, price_scale = 2 where id = %s", (bids[0]["id"],))
    for i, b in enumerate(bids[1:]):
        db.execute("update bids set price = %s, price_scale = 1 where id = %s",
                   (Decimal(150 + i), b["id"]))
    rejected = _add_firm(db, "Odrzucona Oferta", "1111111111")
    _add_bid(db, rd_lot, rejected, "Odrzucona Oferta", Decimal("10.00"), outside=True)

    # 3.3.5: the same price after scaling (1000.00 * 1 and 500.00 * 2).
    bids = _bids(db, dup_lot)
    db.execute("update bids set price = 1000.00, price_scale = 1 where id = %s",
               (bids[0]["id"],))
    db.execute("update bids set price = 500.00, price_scale = 2 where id = %s",
               (bids[1]["id"],))
    # An identical price on an offer outside the BZP range does not count.
    bids = _bids(db, outside_lot)
    db.execute("update bids set price = %s, outside_bzp_range = true where id = %s",
               (bids[0]["price"], bids[1]["id"]))
    db.commit()

    compute_lot_tests(db, run_id)
    rd = _results(db, run_id, "3.3.7", "lot")
    losers = [150 + i for i in range(len(_bids(db, rd_lot)) - 2)]
    expected = (150 - 100) / statistics.stdev(losers)
    assert rd[str(rd_lot)]["value"] == pytest.approx(expected)
    assert rd[str(rd_lot)]["evidence"]["p1"] == 100.0
    assert rd[str(rd_lot)]["evidence"]["n_valid"] == len(losers) + 1

    dup = _results(db, run_id, "3.3.5", "lot")
    assert dup[str(dup_lot)]["value"] == 1.0
    assert dup[str(dup_lot)]["evidence"]["identical"] == [{"price": 1000.0, "count": 2}]
    assert dup[str(outside_lot)]["value"] == 0.0

    ratio = _results(db, run_id, "3.3.9", "lot")
    lot = db.execute("select * from lots where id = %s", (out["lots"][0],)).fetchone()
    assert ratio[str(lot["id"])]["value"] == pytest.approx(
        float(lot["price_winner"]) / float(lot["estimated_value"]))


def test_never_winning_firm_and_single_offer_firm(db):
    out = make_market(db, seed=5, n_firms=10, n_lots=60, cpv="90611000-3", province_code="PL12")
    run_id = _run(db, out["as_of"])
    market = _assign(db, run_id, out)

    loser = _add_firm(db, "Zawsze Przegrywa sp. z o.o.", "2222222222")
    for lot in out["lots"][:10]:
        top = max(b["price"] for b in _bids(db, lot))
        _add_bid(db, lot, loser, "Zawsze Przegrywa sp. z o.o.", top * Decimal("1.2"))
    single = _add_firm(db, "Jedna Oferta sp. z o.o.", "3333333333")
    _add_bid(db, out["lots"][10], single, "Jedna Oferta sp. z o.o.", Decimal("999999.00"))
    db.commit()

    written = compute_firm_tests(db, run_id)
    rows = db.execute(
        "select * from test_results where run_id = %s and level = 'firm'", (run_id,)
    ).fetchall()
    assert written == len(rows)

    never = _results(db, run_id, "3.1.5", "firm")
    key = f"{loser}@{market}"
    assert key in never
    assert never[key]["value"] == 10
    assert never[key]["evidence"] == {"bidder_id": loser, "starts": 10, "wins": 0}
    assert never[key]["n_obs"] >= 10 and never[key]["percentile"] > 0.5
    assert all(r["evidence"]["wins"] == 0 and r["value"] >= 8 for r in never.values())
    share = _results(db, run_id, "3.1.1", "firm")
    assert share[key]["value"] == 0.0

    # Review Focus 2: a firm with one offer gets no firm result at all.
    assert not [r for r in rows if r["evidence"]["bidder_id"] == single]
    assert not [r for r in rows if r["entity_key"].startswith(f"{single}@")]

    assert compute_firm_tests(db, run_id) == written


def test_percentiles_only_within_market(db):
    """Review Focus 1: a market of 2 firms and 40 lots is ranked only against itself and its
    firm results have no percentile."""
    big = make_market(db, seed=21, n_firms=12, n_lots=80, cpv="45233142-6",
                      province_code="PL22")
    small = make_market(db, seed=22, n_firms=2, n_lots=40, cpv="85200000-1",
                        province_code="PL04")
    run_id = _run(db, big["as_of"])
    big_market = _assign(db, run_id, big)
    small_market = _assign(db, run_id, small)
    compute_lot_tests(db, run_id)
    compute_firm_tests(db, run_id)

    ratio = _results(db, run_id, "3.3.9", "lot")
    for out, market in ((big, big_market), (small, small_market)):
        lots = db.execute(
            "select id, price_winner, estimated_value from lots where id = any(%s)",
            (out["lots"],),
        ).fetchall()
        values = {str(r["id"]): float(r["price_winner"]) / float(r["estimated_value"])
                  for r in lots}
        expected = _expected_pct(values)
        for key in values:
            assert ratio[key]["market_id"] == market
            assert ratio[key]["n_obs"] == len(out["lots"])
            assert ratio[key]["percentile"] == pytest.approx(expected[key])

    dup = _results(db, run_id, "3.3.5", "lot")
    assert all(dup[str(lot)]["n_obs"] == 40 for lot in small["lots"])
    rd = _results(db, run_id, "3.3.7", "lot")
    assert not [r for r in rd.values() if r["market_id"] == small_market]

    firm_rows = db.execute(
        "select * from test_results where run_id = %s and level = 'firm' and market_id = %s",
        (run_id, small_market),
    ).fetchall()
    assert {r["test_id"] for r in firm_rows} == {"3.1.1", "3.3.3"}
    assert all(r["n_obs"] == 2 and r["percentile"] is None for r in firm_rows)
    big_rows = db.execute(
        "select * from test_results where run_id = %s and level = 'firm' and market_id = %s"
        " and test_id = '3.1.1'",
        (run_id, big_market),
    ).fetchall()
    assert big_rows and all(r["percentile"] is not None for r in big_rows)
    assert {r["evidence"]["bidder_id"] for r in firm_rows} == set(small["firms"])
