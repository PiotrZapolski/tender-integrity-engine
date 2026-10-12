from decimal import Decimal

import pytest

from .synth import make_market

ROTATION = {"members": 3, "pattern": "rotation", "share": 0.4}


def _lots(db, lot_ids):
    return db.execute(
        "select l.id, l.winner_bidder_id, l.n_offers, l.price_winner, l.price_min,"
        " l.price_max, l.estimated_value, p.buyer_id, p.published_at"
        " from lots l join procedures p on p.ocds_id = l.procedure_id"
        " where l.id = any(%s) order by p.published_at, l.id",
        (lot_ids,),
    ).fetchall()


def _bids(db, lot_id):
    return db.execute(
        "select b.*, d.status doc_status from bids b join documents d on d.id = b.document_id"
        " where b.lot_id = %s order by b.offer_no",
        (lot_id,),
    ).fetchall()


def _snapshot(db, out):
    """Market content keyed by stable values (NIP, notice number), not by serial ids."""
    rows = db.execute(
        "select p.notice_number, p.published_at, l.estimated_value, b.offer_no, f.nip,"
        " b.price, b.is_winner, b.rank, w.nip winner_nip, y.nip buyer_nip"
        " from bids b join lots l on l.id = b.lot_id"
        " join procedures p on p.ocds_id = l.procedure_id"
        " join bidders f on f.id = b.bidder_id"
        " left join bidders w on w.id = l.winner_bidder_id"
        " join buyers y on y.id = p.buyer_id"
        " where l.id = any(%s) order by p.notice_number, b.offer_no",
        (out["lots"],),
    ).fetchall()
    cartel = db.execute(
        "select nip from bidders where id = any(%s) order by nip", (out["cartel"],)
    ).fetchall()
    return [tuple(r.values()) for r in rows], [r["nip"] for r in cartel]


def _reset(db):
    for table in ("bids", "documents", "lots", "procedures", "bidder_accounts", "bidders",
                  "buyers"):
        db.execute(f"delete from {table}")
    db.commit()


def test_rotation_market_shape(db):
    out = make_market(db, seed=1, n_firms=12, n_lots=80, cpv="45233142-6", province_code="PL22",
                      cartel=ROTATION)
    assert len(out["lots"]) == 80
    assert len(out["firms"]) == 12
    assert len(out["cartel"]) == 3
    assert set(out["cartel"]) <= set(out["firms"])
    assert db.execute("select count(*) c from lots").fetchone()["c"] == 80
    assert db.execute("select count(*) c from bidders").fetchone()["c"] == 12

    lots = _lots(db, out["lots"])
    for lot in lots:
        bids = _bids(db, lot["id"])
        assert 2 <= len(bids) <= 6
        assert lot["n_offers"] == len(bids)
        assert all(b["doc_status"] == "parsed" for b in bids)
        assert all(b["price_scale"] == 1 for b in bids)
        assert all(b["outside_bzp_range"] is False for b in bids)
        assert len({b["bidder_id"] for b in bids}) == len(bids)
        winners = [b for b in bids if b["is_winner"]]
        assert len(winners) == 1
        w = winners[0]
        assert w["bidder_id"] == lot["winner_bidder_id"]
        assert w["price"] == lot["price_winner"] == lot["price_min"]
        assert w["rank"] == 1
        assert all(b["price"] > w["price"] for b in bids if not b["is_winner"])
        assert max(b["price"] for b in bids) == lot["price_max"]

    cartel = set(out["cartel"])
    cartel_wins = [lot for lot in lots if lot["winner_bidder_id"] in cartel]
    assert len(cartel_wins) >= 25

    # Rotation: in the cartel lots all members bid together and take turns winning.
    cartel_lots = _lots(db, out["cartel_lots"])
    assert len(cartel_lots) == 32
    for lot in cartel_lots:
        assert cartel <= {b["bidder_id"] for b in _bids(db, lot["id"])}
    order = [lot["winner_bidder_id"] for lot in cartel_lots]
    assert all(a != b for a, b in zip(order, order[1:], strict=False))
    for member in cartel:
        assert order.count(member) >= 10


def test_market_metadata(db):
    out = make_market(db, seed=2, n_firms=8, n_lots=30, cpv="90611000-3", province_code="PL12")
    assert out["cartel"] == [] and out["cartel_lots"] == []
    procs = db.execute(
        "select p.*, y.province from procedures p join buyers y on y.id = p.buyer_id"
    ).fetchall()
    assert len(procs) == 30
    assert all(p["cpv_main"] == "90611000-3" and p["province"] == "PL12" for p in procs)
    lots = _lots(db, out["lots"])
    for lot in lots:
        assert lot["estimated_value"] > 0
        assert lot["published_at"].date() <= out["as_of"]
    dates = db.execute("select contract_date from lots").fetchall()
    assert all(r["contract_date"] < out["as_of"] for r in dates)
    jdg = db.execute("select count(*) c from bidders where is_jdg").fetchone()["c"]
    assert jdg == len(out["jdg"]) == 1


def test_two_markets_in_one_database(db):
    a = make_market(db, seed=3, n_firms=6, n_lots=20, cpv="45233142-6", province_code="PL22")
    b = make_market(db, seed=3, n_firms=6, n_lots=20, cpv="45233142-6", province_code="PL14")
    assert not set(a["lots"]) & set(b["lots"])
    assert not set(a["firms"]) & set(b["firms"])
    assert db.execute("select count(*) c from lots").fetchone()["c"] == 40


def test_deterministic_for_same_seed(db):
    out = make_market(db, seed=1, n_firms=12, n_lots=80, cpv="45233142-6", province_code="PL22",
                      cartel=ROTATION)
    first = _snapshot(db, out)
    _reset(db)
    out = make_market(db, seed=1, n_firms=12, n_lots=80, cpv="45233142-6", province_code="PL22",
                      cartel=ROTATION)
    assert _snapshot(db, out) == first
    _reset(db)
    out = make_market(db, seed=7, n_firms=12, n_lots=80, cpv="45233142-6", province_code="PL22",
                      cartel=ROTATION)
    assert _snapshot(db, out)[0] != first[0]


def test_cover_losers_priced_5_to_8_percent_above(db):
    out = make_market(db, seed=4, n_firms=10, n_lots=60, cpv="34144000-8", province_code="PL30",
                      cartel={"members": 3, "pattern": "cover", "share": 0.5})
    cartel = set(out["cartel"])
    assert len(out["cartel_lots"]) == 30
    for lot_id in out["cartel_lots"]:
        bids = _bids(db, lot_id)
        assert cartel <= {b["bidder_id"] for b in bids}
        winner = next(b for b in bids if b["is_winner"])
        assert winner["bidder_id"] in cartel
        for b in bids:
            if b["bidder_id"] in cartel and not b["is_winner"]:
                ratio = b["price"] / winner["price"]
                assert Decimal("1.0499") <= ratio <= Decimal("1.0801")


def test_split_members_win_only_at_own_buyers(db):
    out = make_market(db, seed=5, n_firms=10, n_lots=60, cpv="15800000-6", province_code="PL02",
                      cartel={"members": 3, "pattern": "split", "share": 0.4})
    own = out["own_buyers"]
    assert set(own) == set(out["cartel"])
    all_own = [b for buyers in own.values() for b in buyers]
    assert len(all_own) == len(set(all_own))
    lots = _lots(db, out["lots"])
    member_wins = {m: 0 for m in out["cartel"]}
    for lot in lots:
        bidders = {b["bidder_id"] for b in _bids(db, lot["id"])}
        for m in out["cartel"]:
            if m in bidders:
                assert lot["buyer_id"] in own[m]
        if lot["winner_bidder_id"] in own:
            member_wins[lot["winner_bidder_id"]] += 1
            assert lot["buyer_id"] in own[lot["winner_bidder_id"]]
    assert all(n >= 4 for n in member_wins.values())


def test_shared_account(db):
    out = make_market(db, seed=6, n_firms=10, n_lots=40, cpv="50000000-5", province_code="PL24",
                      cartel={"members": 2, "pattern": "shared_account", "share": 0.3})
    shared = out["account_hash"]
    assert shared
    rows = db.execute(
        "select bidder_id from bidder_accounts where account_hash = %s order by bidder_id",
        (shared,),
    ).fetchall()
    assert [r["bidder_id"] for r in rows] == sorted(out["cartel"])
    # Every firm has an account of its own, so only the cartel shares one.
    dup = db.execute(
        "select account_hash from bidder_accounts group by account_hash having count(*) > 1"
    ).fetchall()
    assert [r["account_hash"] for r in dup] == [shared]


def test_rejects_unknown_pattern(db):
    with pytest.raises(ValueError):
        make_market(db, seed=1, n_firms=8, n_lots=10, cpv="45233142-6", province_code="PL22",
                    cartel={"members": 3, "pattern": "bogus", "share": 0.4})
