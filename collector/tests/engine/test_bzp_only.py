"""BZP-only screens (`tie.engine.bzp_only`) and their winner-based clusters."""

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from tie.engine.bzp_only import (
    CLUSTER_PREFIX,
    ROTATION_PERMUTATIONS,
    TEST_OVER_ESTIMATE,
    TEST_ROTATION,
    TEST_SHARED,
    TEST_SPREAD,
    compute_bzp_only,
    max_turns,
    pick_group,
    turns,
)
from tie.engine.decide import (
    GATE_MIN_BUYERS,
    GATE_OK,
    GATE_SINGLE_BUYER,
    GATE_WEAK_ONLY,
    Decision,
    evidence_gate,
    strong_items_fired,
    upsert_signals,
    winner_support,
)
from tie.engine.evidence import build_evidence
from tie.engine.markets import assign_markets
from tie.engine.tests_pair import pair_key

from .synth import make_market

AS_OF = date(2026, 1, 1)
CPV = "45233142-6"


class HighJev:
    """Strong answers on every item, as a convinced Jev would give."""

    def __init__(self):
        self.calls = 0

    def decide(self, *, task, state, questions):
        self.calls += 1
        return {key: ({"type": "score", "score": 4} if q.get("type") == "score"
                      else {"type": "noul", "noul": 0.97})
                for key, q in questions.items()}


def _prepare(db, as_of=AS_OF) -> int:
    run_id = db.execute("insert into engine_runs(as_of) values (%s) returning run_id",
                        (as_of,)).fetchone()["run_id"]
    assign_markets(db, run_id, as_of)
    return run_id


def _rotating_buyer(db, *, cpv=CPV, province="PL22", n_lots=15, n_firms=3,
                    start=date(2023, 3, 1), step_days=65, tag="rot", n_offers=3,
                    price=95000):
    """One buyer whose repeated tenders (5 a year) go to `n_firms` firms in turn. Only BZP
    facts: no opening documents, no bids. Estimate 100000, winning price `price`, the
    highest offer a near-constant 9-11% above it."""
    buyer = db.execute(
        "insert into buyers(nip, name, province) values (%s, %s, %s) returning id",
        (f"{tag}-buyer", f"Gmina Rotacyjna {tag}", province)).fetchone()["id"]
    firms = [db.execute(
        "insert into bidders(nip, name, name_norm, address) values (%s, %s, %s, %s)"
        " returning id",
        (f"{tag}-{i}", f"Rotacja {i} {tag} sp. z o.o.", f"rotacja {i} {tag}",
         f"ul. Rotacyjna {i}, {tag}")).fetchone()["id"] for i in range(n_firms)]
    lots = []
    for k in range(n_lots):
        published = datetime.combine(start + timedelta(days=k * step_days),
                                     datetime.min.time(), tzinfo=UTC) + timedelta(hours=10)
        ocds_id = f"ocds-{tag}-{k:03d}"
        db.execute(
            "insert into procedures(ocds_id, bzp_number, notice_number, buyer_id, title,"
            " cpv_main, published_at) values (%s, %s, %s, %s, %s, %s, %s)",
            (ocds_id, f"{tag}/{k}", f"{tag}/N/{k}", buyer, f"Odbiór odpadów {k}", cpv,
             published))
        lots.append(db.execute(
            "insert into lots(procedure_id, lot_no, cpv, estimated_value, outcome, n_offers,"
            " n_rejected, price_min, price_max, price_winner, contract_value, contract_date,"
            " winner_bidder_id) values (%s, 1, %s, 100000, 'umowa', %s, 0, %s, %s, %s,"
            " %s, %s, %s) returning id",
            (ocds_id, cpv, n_offers, price, price + 9000 + 150 * k, price, price,
             published.date() + timedelta(days=14), firms[k % n_firms])).fetchone()["id"])
    db.commit()
    return {"buyer": buyer, "firms": firms, "lots": lots}


# Pure functions (no database).

def test_turns_and_maximum():
    perfect = np.array([0, 1, 2] * 5)
    assert turns(perfect, 3)[0] == max_turns(15, 3) == 14 + 13
    blocks = np.array([0] * 5 + [1] * 5 + [2] * 5)
    assert turns(blocks, 3)[0] == 2
    rows = turns(np.vstack([perfect, blocks]), 3)
    assert rows.tolist() == [27, 2]


def test_pick_group():
    assert pick_group(np.array([7, 8, 9] * 5)) == [7, 8, 9]
    # A dominating winner and two occasional ones: no rotation group.
    assert pick_group(np.array([1] * 12 + [2, 2, 3, 3])) is None
    # Many winners, none with a large share.
    assert pick_group(np.arange(20)) is None
    # Small group covering too little of the buyer's lots.
    assert pick_group(np.array([1, 1, 1, 2, 2, 2] + list(range(10, 20)))) is None


def _decision(**per_item) -> Decision:
    return Decision(probability=0.95, strength=4.0,
                    per_item={k.replace("_", "."): v for k, v in per_item.items()},
                    source="jev")


def _rotation_evidence(pct, *, n_lots=12, n_buyers=1, cluster_id="w:0123", spread=None,
                       over=None):
    oecd = {"3.1.6": {"level": "cluster", "value": 0.9, "pct": pct, "n": 1,
                      "test": TEST_ROTATION}}
    if spread is not None:
        oecd["3.3.7"] = {"level": "cluster", "value": 1.1, "pct": spread, "n": 1,
                         "test": TEST_SPREAD}
    if over is not None:
        oecd["3.3.9"] = {"level": "firm", "value": 1.2, "pct": over, "n": 3,
                         "test": TEST_OVER_ESTIMATE}
    return {"cluster_id": cluster_id, "n_lots": n_lots, "n_buyers": n_buyers, "oecd": oecd}


def test_rotation_is_strong_only_from_percentile_0999():
    decision = _decision(**{"3_1_6": 0.9})
    assert strong_items_fired(decision, _rotation_evidence(0.9995)) == ["3.1.6"]
    assert strong_items_fired(decision, _rotation_evidence(0.995)) == []
    # The co-bidder rotation (no `test`) keeps its usual rule.
    ev = _rotation_evidence(0.95)
    del ev["oecd"]["3.1.6"]["test"]
    assert strong_items_fired(decision, ev) == ["3.1.6"]


def test_rotation_alone_is_weak_only():
    """A strong rotation (pct >= 0.999) without a second independent support."""
    decision = _decision(**{"3_1_6": 0.9, "3_3_7": 0.9, "3_3_9": 0.9})
    for ev in (_rotation_evidence(0.9995), _rotation_evidence(0.9995, n_buyers=3),
               _rotation_evidence(0.9995, spread=0.85, over=0.5)):
        assert evidence_gate(decision, ev, 8) == (GATE_WEAK_ONLY, "review")
        assert winner_support(decision, ev) == (False, "rotation_without_second_support")


def test_rotation_with_narrow_spread_passes():
    decision = _decision(**{"3_1_6": 0.9, "3_3_7": 0.9})
    ev = _rotation_evidence(0.9995, spread=0.95)
    # Buyer-level rotation: one buyer is enough with at least 10 lots, never above review.
    assert evidence_gate(decision, ev, 8) == (GATE_SINGLE_BUYER, "review")
    assert winner_support(decision, ev) == (True, "rotation+spread")
    ev = _rotation_evidence(0.9995, n_buyers=2, over=0.92)
    assert evidence_gate(decision, ev, 8) == (GATE_OK, "review")
    assert winner_support(decision, ev) == (True, "rotation+over_estimate")
    # The second support does not rescue a rotation below 0.999.
    assert evidence_gate(decision, _rotation_evidence(0.995, spread=0.99), 8) == (
        GATE_WEAK_ONLY, "review")


def test_winner_cluster_gates():
    decision = _decision(**{"3_1_6": 0.9, "3_2_11": 0.9})
    assert evidence_gate(decision, _rotation_evidence(0.9995, n_lots=9, spread=0.95), 8) == (
        GATE_MIN_BUYERS, "hidden")
    # A co-bidder cluster keeps the two-buyer rule.
    assert evidence_gate(decision, _rotation_evidence(0.9995, cluster_id="c0ffee"), 8) == (
        GATE_MIN_BUYERS, "hidden")
    shared = {"cluster_id": "w:abcd", "n_lots": 20, "n_buyers": 4,
              "oecd": {"3.2.11": {"level": "cluster", "value": 1.0, "pct": None, "n": 1,
                                  "test": TEST_SHARED}}}
    # A shared bank account or address stays strong on its own.
    assert evidence_gate(decision, shared, 8) == (GATE_OK, "review")
    assert winner_support(decision, shared) == (True, "shared_winners")
    both = {**shared, "oecd": {**shared["oecd"], **_rotation_evidence(0.9995)["oecd"]}}
    assert winner_support(decision, both) == (True, "rotation+shared_winners")
    # Shared data at a single buyer is not the rotation exception.
    assert evidence_gate(decision, {**shared, "n_buyers": 1}, 8) == (GATE_MIN_BUYERS, "hidden")


# On the database.

def test_rotating_buyer_is_detected(db, monkeypatch):
    monkeypatch.setenv("ENGINE_MAX_STATUS", "published")
    make_market(db, seed=5, n_firms=12, n_lots=60, cpv=CPV, province_code="PL22")
    rot = _rotating_buyer(db)
    run_id = _prepare(db)
    counts = compute_bzp_only(db, run_id)
    assert counts["rotation_groups"] >= 1 and set(counts["seconds"]) >= {"load", "write"}

    members = sorted(rot["firms"])
    cluster = db.execute(
        "select cluster_id, market_id, n_lots, buyers from clusters"
        " where run_id = %s and members = %s::bigint[]", (run_id, members)).fetchone()
    assert cluster is not None and cluster["cluster_id"].startswith(CLUSTER_PREFIX)
    assert cluster["n_lots"] == 15 and cluster["buyers"] == [rot["buyer"]]
    row = db.execute(
        "select value, percentile, n_obs, evidence from test_results"
        " where run_id = %s and test_id = %s and entity_key = %s",
        (run_id, TEST_ROTATION, cluster["cluster_id"])).fetchone()
    assert row["percentile"] >= 0.999 and row["n_obs"] == 15
    assert row["value"] == pytest.approx(1.0)
    ev = row["evidence"]
    assert ev["oecd"] == "3.1.6" and ev["coverage"] == 1.0
    assert ev["turns"] == ev["max_turns"] == 27 and ev["draws"] == ROTATION_PERMUTATIONS
    assert ev["buyers"][0]["buyer_id"] == rot["buyer"]

    evidence = build_evidence(db, run_id, cluster["cluster_id"])
    assert evidence["basis"] == "bzp_winners"
    assert evidence["n_lots"] == 15 and evidence["n_buyers"] == 1
    assert evidence["oecd"]["3.1.6"]["test"] == TEST_ROTATION
    assert evidence["oecd"]["3.1.6"]["pct"] >= 0.999
    # The near-constant spread of the rotating lots is the second, independent support.
    assert evidence["oecd"]["3.3.7"]["pct"] >= 0.9
    assert {m["bidder_id"] for m in evidence["members"]} == set(members)
    assert len(evidence["lots"]) == 15
    assert all(lot["winner"] in ("A", "B", "C") for lot in evidence["lots"])

    # Even with publication allowed and a convinced Jev, a BZP-only cluster stays in review.
    jev = HighJev()
    signals = upsert_signals(db, run_id, jev, max_status="published")
    assert signals["published"] == 0 and jev.calls >= 1
    signal = db.execute("select status, evidence from signals where signal_id = %s",
                        (cluster["cluster_id"],)).fetchone()
    assert signal["status"] == "review"
    assert signal["evidence"]["decision"]["gate"] == GATE_SINGLE_BUYER
    assert signal["evidence"]["decision"]["gate_detail"].startswith("rotation+")
    assert "spread" in signal["evidence"]["decision"]["gate_detail"]
    assert db.execute("select * from api.signals").fetchall() == []


def test_rotation_without_second_support_is_weak_only(db, monkeypatch):
    """Perfect rotation, but no spread data (2 offers) and winning prices well below the
    estimate: no second support, so the gate is `weak_only`."""
    monkeypatch.setenv("ENGINE_MAX_STATUS", "published")
    make_market(db, seed=5, n_firms=12, n_lots=60, cpv=CPV, province_code="PL22")
    rot = _rotating_buyer(db, n_offers=2, price=60000)
    run_id = _prepare(db)
    compute_bzp_only(db, run_id)
    cluster = db.execute(
        "select cluster_id from clusters where run_id = %s and members = %s::bigint[]",
        (run_id, sorted(rot["firms"]))).fetchone()
    evidence = build_evidence(db, run_id, cluster["cluster_id"])
    assert evidence["oecd"]["3.1.6"]["pct"] >= 0.999
    assert "3.3.7" not in evidence["oecd"]
    upsert_signals(db, run_id, HighJev(), max_status="published")
    signal = db.execute("select status, evidence from signals where signal_id = %s",
                        (cluster["cluster_id"],)).fetchone()
    assert signal["status"] == "review"
    assert signal["evidence"]["decision"]["gate"] == GATE_WEAK_ONLY
    assert signal["evidence"]["decision"]["gate_detail"] == "rotation_without_second_support"


def test_honest_market_has_no_rotation(db):
    out = make_market(db, seed=7, n_firms=30, n_lots=240, cpv="90910000-9",
                      province_code="PL14")
    run_id = _prepare(db, out["as_of"])
    counts = compute_bzp_only(db, run_id)
    assert counts["lots"] == 240
    strong = db.execute(
        "select entity_key, percentile from test_results where run_id = %s"
        " and test_id = %s and percentile >= 0.99", (run_id, TEST_ROTATION)).fetchall()
    assert strong == []
    assert db.execute(
        "select count(*) n from test_results where run_id = %s and test_id = %s",
        (run_id, TEST_SHARED)).fetchone()["n"] == 0
    # The firm screens ran on the same data.
    assert db.execute(
        "select count(*) n from test_results where run_id = %s and test_id = %s",
        (run_id, TEST_OVER_ESTIMATE)).fetchone()["n"] > 0


def test_shared_account_between_two_winners(db, monkeypatch):
    monkeypatch.setenv("ENGINE_MAX_STATUS", "published")
    out = make_market(db, seed=9, n_firms=10, n_lots=60, cpv=CPV, province_code="PL30",
                      cartel={"members": 2, "pattern": "shared_account", "share": 0.4})
    # BZP-only: the screens must not need the opening documents.
    db.execute("delete from bids")
    db.execute("delete from documents")
    db.commit()
    run_id = _prepare(db, out["as_of"])
    compute_bzp_only(db, run_id)

    a, b = sorted(out["cartel"])
    market_id = db.execute("select market_id from lot_market where lot_id = %s",
                           (out["lots"][0],)).fetchone()["market_id"]
    pair = db.execute(
        "select value, evidence from test_results where run_id = %s and test_id = %s"
        " and level = 'pair' and entity_key = %s",
        (run_id, TEST_SHARED, pair_key(a, b, market_id))).fetchone()
    assert pair is not None and pair["value"] == 1.0
    assert pair["evidence"]["shared_account"] is True
    assert pair["evidence"]["a_wins"] > 0 and pair["evidence"]["b_wins"] > 0
    assert db.execute(
        "select count(*) n from test_results where run_id = %s and test_id = %s"
        " and level = 'pair'", (run_id, TEST_SHARED)).fetchone()["n"] == 1

    cluster = db.execute(
        "select cluster_id, n_lots, buyers from clusters where run_id = %s"
        " and members = %s::bigint[]", (run_id, [a, b])).fetchone()
    won = db.execute(
        "select count(*) n from lots where winner_bidder_id = any(%s)", ([a, b],)
    ).fetchone()["n"]
    assert cluster is not None and cluster["buyers"] is None and cluster["n_lots"] == won

    evidence = build_evidence(db, run_id, cluster["cluster_id"])
    assert evidence["oecd"]["3.2.11"]["test"] == TEST_SHARED
    assert evidence["n_buyers"] >= 2

    upsert_signals(db, run_id, HighJev(), max_status="published")
    signal = db.execute("select status, evidence from signals where signal_id = %s",
                        (cluster["cluster_id"],)).fetchone()
    assert signal["status"] == "review"
    assert signal["evidence"]["decision"]["gate"] == GATE_OK
    assert "shared_winners" in signal["evidence"]["decision"]["gate_detail"]


PERF_CPVS = ", ".join(f"'{c}'" for c in (
    "45233142-6", "45000000-7", "90910000-9", "79710000-4", "33600000-6", "09310000-5",
    "15800000-6", "34144000-8", "72000000-5", "71000000-8"))


def _bulk_market(db, *, n_lots=20_000, n_buyers=300, n_firms=2_000):
    """A national-like set of BZP lots built with set-based SQL (no documents)."""
    db.execute("select setseed(0.42)")
    db.execute(
        "insert into buyers(nip, name, province)"
        " select 'perf-b-' || g, 'Zamawiajacy ' || g,"
        "  'PL' || lpad((2 + 2 * (g % 16))::text, 2, '0')"
        f" from generate_series(1, {n_buyers}) g")
    db.execute(
        "insert into bidders(nip, name, name_norm, address)"
        " select 'perf-f-' || g, 'Firma ' || g, 'firma ' || g,"
        "  'ul. Wydajna ' || (g % 1500) || ', Miasto'"
        f" from generate_series(1, {n_firms}) g")
    db.execute(
        "insert into bidder_accounts(bidder_id, account_hash)"
        " select id, md5('acc-' || ((id - (select min(id) from bidders)) % 1800))"
        " from bidders")
    db.execute(
        "insert into procedures(ocds_id, bzp_number, notice_number, buyer_id, title,"
        " cpv_main, published_at)"
        " select 'perf-' || g, 'perf-' || g, 'perf-' || g,"
        f"  (select min(id) from buyers) + (g % {n_buyers}), 'Zamowienie ' || g,"
        f"  (array[{PERF_CPVS}])[1 + g % 10],"
        "  timestamptz '2023-01-15 10:00+00' + (g % 1000) * interval '1 day'"
        f" from generate_series(1, {n_lots}) g")
    db.execute(
        "insert into lots(procedure_id, lot_no, outcome, estimated_value, n_offers,"
        " n_rejected, price_min, price_max, price_winner, contract_value, winner_bidder_id)"
        " select 'perf-' || g, 1, 'umowa', round(est::numeric, 2), n, 0,"
        "  round((est * f)::numeric, 2), round((est * f * (1 + s))::numeric, 2),"
        "  round((est * f)::numeric, 2), round((est * f)::numeric, 2), w"
        " from (select g, 50000 + random() * 500000 as est,"
        "        1 + floor(random() * 6)::int as n, 0.8 + random() * 0.4 as f,"
        "        0.01 + random() * 0.5 as s,"
        "        (select min(id) from bidders)"
        f"         + floor(random() * random() * {n_firms})::int as w"
        f"       from generate_series(1, {n_lots}) g) x")
    db.commit()


def test_performance_smoke_on_20k_lots(db):
    """Runs end to end on about 20k lots (no timing assertion)."""
    _bulk_market(db)
    run_id = _prepare(db)
    counts = compute_bzp_only(db, run_id)
    assert counts["lots"] == 20_000
    assert set(counts["seconds"]) == {"load", "rotation", "shared_winners", "over_estimate",
                                      "spread_firms", "clusters", "write"}
    assert counts["clusters"] == db.execute(
        "select count(*) n from clusters where run_id = %s and cluster_id like 'w:%%'",
        (run_id,)).fetchone()["n"]
    signals = upsert_signals(db, run_id, None, max_status="published")
    assert signals["published"] == 0
    assert signals["clusters"] == counts["clusters"]
    # Second call on the same run replaces its rows instead of adding to them.
    rows = db.execute("select count(*) n from test_results where run_id = %s",
                      (run_id,)).fetchone()["n"]
    compute_bzp_only(db, run_id)
    assert db.execute("select count(*) n from test_results where run_id = %s",
                      (run_id,)).fetchone()["n"] == rows
