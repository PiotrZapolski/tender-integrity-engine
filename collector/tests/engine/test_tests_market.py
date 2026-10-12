from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from tie.engine.tests_market import compute_market_tests

from .synth import make_market

MARKET = "cpv5_prov:45233:14"


def _new_run(db, as_of=date(2026, 1, 1)):
    run_id = db.execute(
        "insert into engine_runs(as_of) values (%s) returning run_id", (as_of,)
    ).fetchone()["run_id"]
    db.commit()
    return run_id


def _assign(db, run_id, lot_ids, market_id=MARKET):
    """Stand-in for Task 2: put the given lots into one market of this run."""
    db.execute(
        "insert into markets(market_id, cpv_group, province, level, n_lots, run_id)"
        " values (%s, '45233', '14', 'cpv5_prov', %s, %s)",
        (market_id, len(lot_ids), run_id),
    )
    db.executemany(
        "insert into lot_market(lot_id, market_id, run_id) values (%s, %s, %s)",
        [(lot_id, market_id, run_id) for lot_id in lot_ids],
    )
    db.commit()


def _cluster(db, run_id, members, cluster_id="c0ffee0000000001", market_id=MARKET):
    """Stand-in for Task 3: one cluster with the given members."""
    db.execute(
        "insert into clusters(run_id, cluster_id, market_id, members, n_lots, density)"
        " values (%s, %s, %s, %s, 20, 1.0)",
        (run_id, cluster_id, market_id, list(members)),
    )
    db.commit()
    return cluster_id


def _result(db, run_id, test_id, level, entity_key):
    return db.execute(
        "select * from test_results where run_id = %s and test_id = %s and level = %s"
        " and entity_key = %s",
        (run_id, test_id, level, entity_key),
    ).fetchone()


def _synth_run(db, pattern):
    out = make_market(
        db, seed=7, n_firms=12, n_lots=80, cpv="45233000-7", province_code="14",
        cartel={"members": 3, "pattern": pattern, "share": 0.4},
    )
    run_id = _new_run(db, out["as_of"])
    _assign(db, run_id, out["lots"])
    cid = _cluster(db, run_id, out["cartel"])
    return out, run_id, cid


def test_split_cartel_has_high_territorial_exclusivity(db):
    out, run_id, cid = _synth_run(db, "split")

    written = compute_market_tests(db, run_id)

    row = _result(db, run_id, "3.1.2", "cluster", cid)
    assert written >= 1
    assert row is not None
    assert row["market_id"] == MARKET
    assert row["value"] > 0.8
    assert row["n_obs"] >= 10
    # Members split the buyers far more than a random reshuffle of the same wins would.
    assert row["percentile"] is not None and row["percentile"] > 0.9
    assert sorted(row["evidence"]["members"]) == sorted(out["cartel"])


def test_rotation_cartel_is_not_a_territorial_split(db):
    _, run_id, cid = _synth_run(db, "rotation")

    compute_market_tests(db, run_id)

    row = _result(db, run_id, "3.1.2", "cluster", cid)
    assert row is not None
    assert row["value"] < 0.6


def test_rerun_replaces_results_of_the_same_run(db):
    _, run_id, cid = _synth_run(db, "split")

    first = compute_market_tests(db, run_id)
    second = compute_market_tests(db, run_id)

    n = db.execute(
        "select count(*) n from test_results where run_id = %s and test_id in ('3.1.2', '3.3.10')",
        (run_id,),
    ).fetchone()["n"]
    assert first == second == n


def test_no_clusters_no_cluster_results(db):
    run_id = _new_run(db)

    assert compute_market_tests(db, run_id) == 0


# --- 3.3.10: hand-built market with full control over who bids when ---------------------

START = date(2024, 1, 1)


def _firm(cur, i):
    return cur.execute(
        "insert into bidders(nip, name, name_norm, is_jdg) values (%s, %s, %s, false)"
        " returning id",
        (f"52100000{i:02d}", f"Firma Rynkowa {i}", f"firma rynkowa {i}"),
    ).fetchone()["id"]


def _entry_market(db, *, entrant: bool):
    """24 months, 2 lots a month, all with cluster members bidding.

    Three members win in turn, two outsiders bid from the very first month. Winning price is
    about 1.10 of the estimate. With `entrant`, a new firm joins every lot from month 12 on
    and from then the winning price is about 0.88 of the estimate (a 20% drop).
    """
    with db.cursor() as cur:
        buyer = cur.execute(
            "insert into buyers(nip, name, province) values ('9990000001', 'Gmina Wzorcowa', '14')"
            " returning id"
        ).fetchone()["id"]
        members = [_firm(cur, i) for i in range(3)]
        outsiders = [_firm(cur, i) for i in range(3, 5)]
        new_firm = _firm(cur, 5)
        lots = []
        for k in range(48):
            month = k // 2
            day = START + timedelta(days=round(k * 365.25 / 24))
            entered = entrant and month >= 12
            level = Decimal("0.88") if entered else Decimal("1.10")
            noise = Decimal(1) + Decimal("0.01") * ((k % 5) - 2)
            estimate = Decimal("100000.00")
            winner = members[k % 3]
            price_winner = (estimate * level * noise).quantize(Decimal("0.01"))
            bidders = [winner, members[(k + 1) % 3], outsiders[0]]
            if k % 2 == 1:
                bidders.append(outsiders[1])
            if entered:
                bidders.append(new_firm)
            ocds = f"ocds-entry-{k:03d}"
            cur.execute(
                "insert into procedures(ocds_id, bzp_number, notice_number, buyer_id, title,"
                " cpv_main, published_at) values (%s, %s, %s, %s, 'Roboty', '45233000-7', %s)",
                (ocds, f"2024/BZP {k:05d}", f"ENTRY/{k:03d}", buyer,
                 datetime.combine(day, datetime.min.time(), tzinfo=UTC)),
            )
            lot_id = cur.execute(
                "insert into lots(procedure_id, lot_no, cpv, estimated_value, outcome, n_offers,"
                " price_winner, contract_date, winner_bidder_id)"
                " values (%s, 1, '45233000-7', %s, 'umowa', %s, %s, %s, %s) returning id",
                (ocds, estimate, len(bidders), price_winner, day + timedelta(days=14), winner),
            ).fetchone()["id"]
            doc = cur.execute(
                "insert into documents(procedure_id, source, source_doc_id, file_name, doc_type,"
                " status) values (%s, 'test', %s, 'otwarcie.pdf', 'opening', 'parsed')"
                " returning id",
                (ocds, f"entry-{k}"),
            ).fetchone()["id"]
            cur.executemany(
                "insert into bids(lot_id, document_id, offer_no, bidder_id, bidder_name_raw,"
                " price, is_winner, parse_method) values (%s, %s, %s, %s, %s, %s, %s, 'test')",
                [(lot_id, doc, pos + 1, b, f"Firma {b}",
                  price_winner * (Decimal(1) + Decimal("0.03") * pos), b == winner)
                 for pos, b in enumerate(bidders)],
            )
            lots.append(lot_id)
    db.commit()
    run_id = _new_run(db, date(2026, 1, 1))
    _assign(db, run_id, lots)
    cid = _cluster(db, run_id, members)
    return run_id, cid, new_firm


def test_no_new_entrant_gives_no_entry_result(db):
    run_id, _, _ = _entry_market(db, entrant=False)

    compute_market_tests(db, run_id)

    n = db.execute(
        "select count(*) n from test_results where run_id = %s and test_id = '3.3.10'",
        (run_id,),
    ).fetchone()["n"]
    assert n == 0


def test_entrant_with_twenty_percent_price_drop(db):
    run_id, cid, new_firm = _entry_market(db, entrant=True)

    compute_market_tests(db, run_id)

    row = _result(db, run_id, "3.3.10", "cluster", cid)
    assert row is not None
    assert abs(row["value"] - (-0.2)) < 0.03
    assert row["market_id"] == MARKET
    assert row["evidence"]["entrants"] == [new_firm]
    assert row["evidence"]["signal"] is True
    assert row["percentile"] is not None and row["percentile"] > 0.5

    market_row = _result(db, run_id, "3.3.10", "market", MARKET)
    assert market_row is not None
    assert abs(market_row["value"] - (-0.2)) < 0.03
