from datetime import UTC, datetime
from decimal import Decimal
from itertools import combinations
from statistics import median

from tie.engine.tests_pair import compute_pair_tests, pair_key

from .synth import make_market

CPV = "45233142-6"
PROVINCE = "PL22"


def _run(db, as_of):
    return db.execute(
        "insert into engine_runs(as_of) values (%s) returning run_id", (as_of,)
    ).fetchone()["run_id"]


def _prepare(db, out, extra_lots=()):
    """Run row, one market holding every lot of `out` (and `extra_lots`), and its pairs.

    Markets and pairs come from Tasks 2 and 3 in production; here they are filled directly so
    the pair tests depend only on the synthetic market.
    """
    run_id = _run(db, out["as_of"])
    mid = f"cpv5_prov:{CPV[:5]}:{out['province']}"
    lots = [*out["lots"], *extra_lots]
    db.execute(
        "insert into markets(market_id, cpv_group, province, level, n_lots, run_id)"
        " values (%s, %s, %s, 'cpv5_prov', %s, %s)",
        (mid, CPV[:5], out["province"], len(lots), run_id),
    )
    db.execute(
        "insert into lot_market(lot_id, market_id, run_id)"
        " select unnest(%s::bigint[]), %s, %s",
        (lots, mid, run_id),
    )
    _fill_pairs(db, run_id)
    db.commit()
    return run_id, mid


def _fill_pairs(db, run_id):
    """Minimal `build_pairs`: pairs of firms with offers in at least 2 common lots."""
    db.execute(
        "with bb as ("
        "  select distinct b.lot_id, b.bidder_id, lm.market_id from bids b"
        "  join documents d on d.id = b.document_id and d.status = 'parsed'"
        "  join lot_market lm on lm.lot_id = b.lot_id and lm.run_id = %(run)s"
        "  where b.bidder_id is not null"
        "), pr as ("
        "  select x.market_id, x.bidder_id as a, y.bidder_id as b, x.lot_id from bb x"
        "  join bb y on y.lot_id = x.lot_id and x.bidder_id < y.bidder_id"
        ")"
        " insert into pairs(run_id, a, b, market_id, n_together, a_wins, b_wins, other_wins,"
        "  shared_account, shared_address)"
        " select %(run)s, pr.a, pr.b, pr.market_id, count(*),"
        "  count(*) filter (where l.winner_bidder_id = pr.a),"
        "  count(*) filter (where l.winner_bidder_id = pr.b),"
        "  count(*) filter (where l.winner_bidder_id not in (pr.a, pr.b)),"
        "  exists (select 1 from bidder_accounts x join bidder_accounts y"
        "    on y.account_hash = x.account_hash where x.bidder_id = pr.a"
        "    and y.bidder_id = pr.b),"
        "  coalesce((select fa.address = fb.address from bidders fa, bidders fb"
        "    where fa.id = pr.a and fb.id = pr.b), false)"
        " from pr join lots l on l.id = pr.lot_id"
        " group by pr.market_id, pr.a, pr.b having count(*) >= 2",
        {"run": run_id},
    )


def _results(db, run_id, test_id):
    return {
        r["entity_key"]: r for r in db.execute(
            "select * from test_results where run_id = %s and test_id = %s and level = 'pair'",
            (run_id, test_id),
        ).fetchall()
    }


def _key(a, b, mid):
    return pair_key(min(a, b), max(a, b), mid)


def test_rotation_pairs_of_two_member_cartel_score_high(db):
    out = make_market(db, seed=3, n_firms=12, n_lots=120, cpv=CPV, province_code=PROVINCE,
                      cartel={"members": 2, "pattern": "rotation", "share": 0.4})
    run_id, mid = _prepare(db, out)

    assert compute_pair_tests(db, run_id) > 0
    rot = _results(db, run_id, "3.1.6")
    a, b = sorted(out["cartel"])
    r = rot[pair_key(a, b, mid)]
    assert r["value"] > 0.8
    assert r["market_id"] == mid
    assert r["evidence"]["a_wins"] + r["evidence"]["b_wins"] == r["evidence"]["n_together"]
    assert r["n_obs"] >= 10
    assert r["percentile"] == 1.0
    stored = db.execute(
        "select rotation from pairs where run_id = %s and a = %s and b = %s and market_id = %s",
        (run_id, a, b, mid),
    ).fetchone()["rotation"]
    assert abs(stored - r["value"]) < 1e-9

    outsiders = [v["value"] for k, v in rot.items() if k != pair_key(a, b, mid)]
    assert outsiders
    assert median(outsiders) < 0.8


def test_rotation_pairs_of_three_member_cartel_reach_two_thirds(db):
    # Three members winning in turn: each pair wins 2 of every 3 common lots, balanced.
    out = make_market(db, seed=4, n_firms=12, n_lots=120, cpv=CPV, province_code=PROVINCE,
                      cartel={"members": 3, "pattern": "rotation", "share": 0.4})
    run_id, mid = _prepare(db, out)
    compute_pair_tests(db, run_id)
    rot = _results(db, run_id, "3.1.6")
    for a, b in combinations(out["cartel"], 2):
        assert rot[_key(a, b, mid)]["value"] > 0.6


def test_cover_pairs_have_stable_price_ratio(db):
    out = make_market(db, seed=5, n_firms=12, n_lots=120, cpv=CPV, province_code=PROVINCE,
                      cartel={"members": 3, "pattern": "cover", "share": 0.4})
    run_id, mid = _prepare(db, out)
    compute_pair_tests(db, run_id)
    cv = _results(db, run_id, "3.3.6")

    for a, b in combinations(out["cartel"], 2):
        r = cv[_key(a, b, mid)]
        assert r["value"] < 0.03
        assert r["evidence"]["n_lots"] >= 4
        assert 1.04 < r["evidence"]["mean_ratio"] < 1.09
        stored = db.execute(
            "select ratio_cv from pairs where run_id = %s and a = %s and b = %s"
            " and market_id = %s",
            (run_id, min(a, b), max(a, b), mid),
        ).fetchone()["ratio_cv"]
        assert abs(stored - r["value"]) < 1e-9

    outsiders = set(out["outsiders"])
    random_cv = [
        r["value"] for r in cv.values()
        if r["evidence"]["a"] in outsiders and r["evidence"]["b"] in outsiders
    ]
    assert len(random_cv) >= 5
    assert median(random_cv) > 0.05

    # Inverted percentile: the lowest CV in the market ranks at the top.
    lowest = min(cv.values(), key=lambda r: r["value"])
    assert lowest["n_obs"] >= 10
    assert lowest["percentile"] == 1.0


def test_price_ratio_ignores_offers_outside_bzp_range(db):
    out = make_market(db, seed=5, n_firms=12, n_lots=120, cpv=CPV, province_code=PROVINCE,
                      cartel={"members": 3, "pattern": "cover", "share": 0.4})
    # Mark every losing cartel offer as a likely rejected offer: no valid ratio is left.
    db.execute(
        "update bids set outside_bzp_range = true"
        " where bidder_id = any(%s) and not is_winner",
        (out["cartel"],),
    )
    db.commit()
    run_id, mid = _prepare(db, out)
    compute_pair_tests(db, run_id)
    cv = _results(db, run_id, "3.3.6")
    for a, b in combinations(out["cartel"], 2):
        assert _key(a, b, mid) not in cv
    # Co-bidding still counts for rotation and shared data.
    shared = _results(db, run_id, "3.2.11")
    for a, b in combinations(out["cartel"], 2):
        assert _key(a, b, mid) in shared


def test_shared_account_flags_only_cartel_pairs(db):
    out = make_market(db, seed=6, n_firms=12, n_lots=80, cpv=CPV, province_code=PROVINCE,
                      cartel={"members": 3, "pattern": "shared_account", "share": 0.4})
    run_id, mid = _prepare(db, out)
    compute_pair_tests(db, run_id)
    shared = _results(db, run_id, "3.2.11")

    cartel_keys = {_key(a, b, mid) for a, b in combinations(out["cartel"], 2)}
    assert cartel_keys <= set(shared)
    for key, r in shared.items():
        assert r["percentile"] is None
        if key in cartel_keys:
            assert r["value"] == 1.0
            assert r["evidence"]["shared_account"] is True
        else:
            assert r["value"] == 0.0
    assert len(shared) > len(cartel_keys)


def _add_consortium_lot(db, out, k, leader, raw_name, published_at):
    ocds_id = f"ocds-consortium-{out['procedures'][0]}-{k}"
    db.execute(
        "insert into procedures(ocds_id, bzp_number, notice_number, buyer_id, title, cpv_main,"
        " cpv_all, published_at) values (%s, %s, %s, %s, 'Konsorcjum test', %s, %s, %s)",
        (ocds_id, f"2026/BZP K{k}", f"KONS/{out['procedures'][0]}/{k}", out["buyers"][0], CPV,
         [CPV], published_at),
    )
    lot = db.execute(
        "insert into lots(procedure_id, lot_no, cpv, estimated_value, outcome, n_offers,"
        " price_winner, winner_bidder_id, consortium)"
        " values (%s, 1, %s, 100000, 'umowa', 1, 95000, %s, true) returning id",
        (ocds_id, CPV, leader),
    ).fetchone()["id"]
    doc = db.execute(
        "insert into documents(procedure_id, source, source_doc_id, file_name, doc_type, status)"
        " values (%s, 'synth', %s, 'otwarcie.pdf', 'opening', 'parsed') returning id",
        (ocds_id, f"kons-{ocds_id}"),
    ).fetchone()["id"]
    db.execute(
        "insert into bids(lot_id, document_id, offer_no, bidder_id, bidder_name_raw, price,"
        " is_winner, rank, parse_method) values (%s, %s, 1, %s, %s, %s, true, 1, 'synth')",
        (lot, doc, leader, raw_name, Decimal("95000.00")),
    )
    return lot


def _co_bids(db, a, b):
    """Publication times of lots in which both firms filed their own offers."""
    return [r["published_at"] for r in db.execute(
        "select distinct p.published_at from bids x join bids y on y.lot_id = x.lot_id"
        " join lots l on l.id = x.lot_id join procedures p on p.ocds_id = l.procedure_id"
        " where x.bidder_id = %s and y.bidder_id = %s order by p.published_at",
        (a, b),
    ).fetchall()]


def test_consortium_after_separate_bidding(db):
    out = make_market(db, seed=7, n_firms=8, n_lots=60, cpv=CPV, province_code=PROVINCE)
    names = {r["id"]: r["name"] for r in db.execute(
        "select id, name from bidders where id = any(%s)", (out["firms"],)
    ).fetchall()}
    # The most frequent co-bidding pair.
    best = max(combinations(out["firms"], 2), key=lambda ab: len(_co_bids(db, *ab)))
    leader, partner = best
    times = _co_bids(db, leader, partner)
    assert len(times) >= 4
    after = datetime(2027, 1, 1, 10, tzinfo=UTC)
    raw = f"Konsorcjum: {names[leader]} (lider), {names[partner]}"
    extra = [
        _add_consortium_lot(db, out, 1, leader, raw, after),
        _add_consortium_lot(db, out, 2, leader, raw.upper(), after.replace(month=2)),
        # Before their second separate start: not an event.
        _add_consortium_lot(db, out, 3, leader, raw, times[1]),
    ]
    # A consortium with a firm that never bid next to the leader: no pair, no result.
    loners = [f for f in out["firms"] if f != leader and not _co_bids(db, leader, f)]
    if loners:
        extra.append(_add_consortium_lot(
            db, out, 4, leader, f"Konsorcjum {names[leader]}, {names[loners[0]]}", after))
    db.commit()
    run_id, mid = _prepare(db, out, extra)

    compute_pair_tests(db, run_id)
    cons = _results(db, run_id, "3.1.7")
    r = cons[_key(leader, partner, mid)]
    assert r["value"] == 2.0
    assert r["percentile"] is None
    assert sorted(e["lot_id"] for e in r["evidence"]["lots"]) == sorted(extra[:2])
    assert all(e["separate_before"] >= 2 for e in r["evidence"]["lots"])
    assert len(cons) == 1


def test_consortium_partner_matched_by_nip(db):
    out = make_market(db, seed=8, n_firms=8, n_lots=60, cpv=CPV, province_code=PROVINCE)
    leader, partner = max(combinations(out["firms"], 2), key=lambda ab: len(_co_bids(db, *ab)))
    nip = db.execute("select nip from bidders where id = %s", (partner,)).fetchone()["nip"]
    lot = _add_consortium_lot(
        db, out, 1, leader, f"Lider konsorcjum, partner NIP {nip[:3]}-{nip[3:6]}-{nip[6:8]}-"
        f"{nip[8:]}", datetime(2027, 1, 1, 10, tzinfo=UTC))
    db.commit()
    run_id, mid = _prepare(db, out, [lot])
    compute_pair_tests(db, run_id)
    assert _results(db, run_id, "3.1.7")[_key(leader, partner, mid)]["value"] == 1.0


def test_sparse_pairs_do_not_fail_or_signal(db):
    # Review Focus 2: firms with very few offers and pairs without wins.
    out = make_market(db, seed=9, n_firms=6, n_lots=12, cpv=CPV, province_code=PROVINCE)
    run_id = _run(db, out["as_of"])
    mid = "cpv5_prov:45233:PL22"
    f = out["firms"]
    db.cursor().executemany(
        "insert into pairs(run_id, a, b, market_id, n_together, a_wins, b_wins, other_wins,"
        " shared_account, shared_address) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        [
            (run_id, f[0], f[1], mid, 2, 1, 0, 1, False, None),
            (run_id, f[2], f[3], mid, 5, 0, 0, 5, None, None),
            (run_id, f[4], f[5], mid, 6, None, None, None, False, True),
        ],
    )
    db.commit()

    n = compute_pair_tests(db, run_id)
    rot = _results(db, run_id, "3.1.6")
    assert _key(f[0], f[1], mid) not in rot  # n_together < 5
    assert rot[_key(f[2], f[3], mid)]["value"] == 0.0
    assert rot[_key(f[4], f[5], mid)]["value"] == 0.0
    assert all(r["percentile"] is None for r in rot.values())  # fewer than 10 in the market
    assert _results(db, run_id, "3.3.6") == {}  # no lot_market rows, no priced lots
    shared = _results(db, run_id, "3.2.11")
    assert shared[_key(f[4], f[5], mid)]["value"] == 1.0
    assert shared[_key(f[2], f[3], mid)]["value"] == 0.0
    assert n == len(rot) + len(shared)


def test_rerun_in_same_run_overwrites(db):
    out = make_market(db, seed=5, n_firms=12, n_lots=80, cpv=CPV, province_code=PROVINCE,
                      cartel={"members": 3, "pattern": "cover", "share": 0.4})
    run_id, _ = _prepare(db, out)
    first = compute_pair_tests(db, run_id)
    second = compute_pair_tests(db, run_id)
    assert first == second
    total = db.execute(
        "select count(*) as n from test_results where run_id = %s and level = 'pair'", (run_id,)
    ).fetchone()["n"]
    assert total == first
