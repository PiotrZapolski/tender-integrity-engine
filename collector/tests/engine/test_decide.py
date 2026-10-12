import json
import math
from itertools import combinations

import pytest
from psycopg.types.json import Jsonb

from tie.ai.providers import BudgetExceeded, ProviderHttpError
from tie.engine.decide import (
    FALLBACK_BIAS,
    QUESTIONS,
    STRENGTH_KEY,
    STRENGTH_LEVELS,
    Decision,
    build_questions,
    combine,
    decide_cluster,
    fallback_decision,
    question_key,
    status_for,
    upsert_signals,
)
from tie.engine.evidence import (
    MAX_CHARS,
    _test_rows,
    build_evidence,
    canonical_json,
    evidence_hash,
    present_items,
)

from .synth import make_market

ROTATION = {"members": 3, "pattern": "rotation", "share": 0.5}
CLUSTER_ID = "c0ffee0123456789"


class FakeJev:
    """Answers every question: noul questions with `noul`, the score with `score`."""

    def __init__(self, noul: float = 0.9, score: float = 3.6, error: Exception | None = None):
        self.noul = noul
        self.score = score
        self.error = error
        self.calls: list[dict] = []

    def decide(self, *, task, state, questions):
        self.calls.append({"task": task, "state": state, "questions": questions})
        if self.error is not None:
            raise self.error
        answers = {}
        for key, q in questions.items():
            if q["type"] == "noul":
                answers[key] = {"type": "noul", "noul": self.noul}
            else:
                answers[key] = {"type": "score", "score": self.score,
                                "legend": dict(enumerate(q["criteria"]))}
        return answers


def _run(db, as_of) -> int:
    row = db.execute("insert into engine_runs(as_of) values (%s) returning run_id",
                     (as_of,)).fetchone()
    return row["run_id"]


def _results(db, run_id, market_id, m, *, strong=True):
    """test_results as tasks 4-6 would write them: high percentiles for the cartel."""
    hi = 0.97 if strong else 0.2
    rows = []
    for a, b in combinations(sorted(m["cartel"]), 2):
        rows.append(("3.1.6", "pair", f"{a}-{b}|{market_id}", 0.95 if strong else 0.1, hi))
        rows.append(("3.3.6", "pair", f"{a}-{b}|{market_id}", 0.02 if strong else 0.3, hi))
        rows.append(("3.2.11", "pair", f"{a}-{b}|{market_id}", 0.0, None))
    for lot in m["cartel_lots"]:
        rows.append(("3.3.7", "lot", str(lot), 3.2 if strong else 0.5, hi))
    rows.append(("3.1.2", "cluster", CLUSTER_ID, 0.9 if strong else 0.1, hi))
    for f in m["cartel"]:
        rows.append(("3.1.1", "firm", f"{f}@{market_id}", 0.3, hi))
    # Outsiders: their results must not reach the cluster's evidence.
    o1, o2 = sorted(m["outsiders"])[:2]
    rows.append(("3.1.1", "firm", f"{o1}@{market_id}", 0.9, 1.0))
    rows.append(("3.1.6", "pair", f"{o1}-{o2}|{market_id}", 1.0, 1.0))
    db.cursor().executemany(
        "insert into test_results(run_id, test_id, level, entity_key, market_id, value,"
        " percentile, n_obs) values (%s, %s, %s, %s, %s, %s, %s, 30)",
        [(run_id, t, lvl, key, market_id, v, p) for t, lvl, key, v, p in rows],
    )


def _setup(db, *, seed=11, strong=True, market=None, run_id=None):
    m = market or make_market(db, seed=seed, n_firms=10, n_lots=60, cpv="45210000",
                              province_code="30", cartel=ROTATION)
    run_id = run_id or _run(db, m["as_of"])
    market_id = "cpv5_prov:45210:30"
    db.execute(
        "insert into markets(market_id, cpv_group, province, level, n_lots, run_id)"
        " values (%s, '45210', '30', 'cpv5_prov', %s, %s)"
        " on conflict (market_id) do update set run_id = excluded.run_id",
        (market_id, len(m["lots"]), run_id),
    )
    db.cursor().executemany(
        "insert into lot_market(lot_id, market_id, run_id) values (%s, %s, %s)"
        " on conflict (lot_id) do update set run_id = excluded.run_id",
        [(lot, market_id, run_id) for lot in m["lots"]],
    )
    db.execute(
        "insert into clusters(run_id, cluster_id, market_id, members, n_lots, density)"
        " values (%s, %s, %s, %s, %s, 1.0)",
        (run_id, CLUSTER_ID, market_id, sorted(m["cartel"]), len(m["cartel_lots"])),
    )
    _results(db, run_id, market_id, m, strong=strong)
    db.commit()
    return m, run_id


def _signals(db):
    return db.execute("select * from signals order by signal_id").fetchall()


# Pure functions (no database).

def test_combine_formula_weights_a_and_c():
    # 3.1.6 is A (w=1), 3.1.3 is C (w=0.6).
    assert combine({"3.1.6": 0.5}) == pytest.approx(0.5)
    assert combine({"3.1.3": 0.5}) == pytest.approx(0.3)
    assert combine({"3.1.6": 0.5, "3.1.3": 0.5}) == pytest.approx(1 - 0.5 * 0.7)


def test_combine_uses_only_the_three_strongest_items():
    """I2: nine weak answers must not add up to a publishable probability."""
    weak = {item: 0.25 for item in
            ("3.1.1", "3.1.2", "3.1.5", "3.1.6", "3.2.11", "3.3.3", "3.3.5", "3.3.6", "3.3.7")}
    assert combine(weak) == pytest.approx(1 - 0.75 ** 3)
    # The strongest three by weighted contribution: the C item (0.9 * 0.6) loses to A items.
    mixed = {"3.1.6": 0.8, "3.3.6": 0.7, "3.3.7": 0.6, "3.1.3": 0.9, "3.3.9": 0.1}
    assert combine(mixed) == pytest.approx(1 - 0.2 * 0.3 * (1 - 0.6))


def _jev(per_item, probability=0.99):
    return Decision(probability=probability, strength=4.0, per_item=per_item, source="jev")


def test_publish_gate_and_max_status():
    strong = _jev({"3.1.6": 0.95, "3.3.6": 0.7})
    assert status_for(strong, 0.91, 0.6, "published") == "published"
    # Max status caps the result; the default of upsert_signals is "review".
    assert status_for(strong, 0.91, 0.6, "review") == "review"
    assert status_for(strong, 0.91, 0.6, "hidden") == "hidden"
    # Strongest item below 0.8: no publication even with a high combined probability.
    assert status_for(_jev({"3.1.6": 0.79, "3.3.6": 0.79, "3.3.7": 0.79}), 0.91, 0.6,
                      "published") == "review"
    # Only one item at 0.6 or more: no publication.
    assert status_for(_jev({"3.1.6": 0.99, "3.3.6": 0.5, "3.3.7": 0.5}), 0.91, 0.6,
                      "published") == "review"
    # The fallback is never published.
    fallback = Decision(probability=0.99, strength=4.0,
                        per_item={"3.1.6": 1.0, "3.3.6": 1.0}, source="fallback")
    assert status_for(fallback, 0.91, 0.6, "published") == "review"
    low = _jev({"3.1.6": 0.1}, probability=0.1)
    assert status_for(low, 0.91, 0.6, "published") == "hidden"


def test_present_items_drop_null_percentiles_and_zero_flags():
    evidence = {"oecd": {
        "3.1.6": {"pct": 0.9, "value": 0.5},
        "3.1.1": {"pct": None, "value": 0.9},       # too few observations: not sent
        "3.2.11": {"pct": None, "value": 0.0},      # no shared data: absent
        "3.3.5": {"pct": 0.95, "value": 0.0},       # no identical prices: absent
        "3.1.7": {"pct": None, "value": 2.0},       # consortium events: present
        "3.3.10": {"pct": None, "value": -0.3, "signal": True},  # no placebo: not sent
        "3.4.1": {"pct": 1.0},                      # N item: never
    }}
    assert set(present_items(evidence)) == {"3.1.6", "3.1.7"}
    jev = FakeJev(noul=0.9)
    d = decide_cluster(jev, evidence)
    assert set(d.per_item) == {"3.1.6", "3.1.7"}
    sent = jev.calls[0]
    assert set(sent["state"]["oecd"]) == {"3.1.6", "3.1.7"}
    assert set(sent["questions"]) == {question_key("3.1.6"), question_key("3.1.7"), STRENGTH_KEY}


def test_decision_without_present_items_is_empty_and_skips_jev():
    jev = FakeJev()
    d = decide_cluster(jev, {"oecd": {"3.1.6": {"pct": None, "value": 1.0}}})
    assert d.source == "empty" and jev.calls == []


def test_evidence_hash_ignores_small_drift_but_not_new_items():
    """I3: the window slides daily; market sizes and small rank moves keep the hash."""
    base = {
        "cluster_id": "c1", "market": {"market_id": "m", "n_lots": 300},
        "members": [{"key": "A", "bidder_id": 1}, {"key": "B", "bidder_id": 2}],
        "n_lots": 30,
        "oecd": {"3.1.6": {"pct": 0.961, "value": 0.81, "n": 1},
                 "3.3.7": {"pct": 0.902, "value": 2.1, "n": 30, "mean": 2.2, "high": 12}},
    }
    drift = json.loads(json.dumps(base))
    drift["market"]["n_lots"] = 305
    drift["n_lots"] = 31
    drift["oecd"]["3.1.6"].update(pct=0.968, value=0.83, n=1)
    drift["oecd"]["3.3.7"].update(pct=0.91, n=31, mean=2.25, high=13)
    assert evidence_hash(drift) == evidence_hash(base)

    moved = json.loads(json.dumps(base))
    moved["oecd"]["3.1.6"]["pct"] = 0.85
    assert evidence_hash(moved) != evidence_hash(base)
    more = json.loads(json.dumps(base))
    more["oecd"]["3.2.11"] = {"pct": None, "value": 1.0}
    assert evidence_hash(more) != evidence_hash(base)
    other = json.loads(json.dumps(base))
    other["members"].append({"key": "C", "bidder_id": 3})
    assert evidence_hash(other) != evidence_hash(base)
    # A zero-valued flag or an item without a percentile is not part of the decision.
    absent = json.loads(json.dumps(base))
    absent["oecd"]["3.2.11"] = {"pct": None, "value": 0.0}
    absent["oecd"]["3.1.1"] = {"pct": None, "value": 0.5}
    assert evidence_hash(absent) == evidence_hash(base)


def test_questions_templates():
    assert QUESTIONS[STRENGTH_KEY]["type"] == "score"
    assert QUESTIONS[STRENGTH_KEY]["criteria"] == STRENGTH_LEVELS
    assert STRENGTH_LEVELS[0] == "brak sygnału" and STRENGTH_LEVELS[-1] == "bardzo silny sygnał"
    q = QUESTIONS[question_key("3.1.6")]
    assert q["type"] == "noul"
    assert "Wygrywanie na zmianę" in q["instructions"]["pytanie"]
    # N items are never asked.
    assert question_key("3.4.1") not in QUESTIONS


def test_build_questions_only_items_present_with_numbers():
    numbers = {"level": "pair", "value": 0.9, "pct": 0.99, "n": 3}
    evidence = {"oecd": {"3.1.6": numbers}}
    qs = build_questions(evidence)
    assert set(qs) == {question_key("3.1.6"), STRENGTH_KEY}
    assert qs[question_key("3.1.6")]["instructions"]["liczby"] == numbers
    # The template itself is not mutated.
    assert QUESTIONS[question_key("3.1.6")]["instructions"]["liczby"] is None


def test_decide_cluster_from_fake_answers():
    evidence = {"oecd": {"3.1.6": {"pct": 0.99}, "3.3.6": {"pct": 0.98}}}
    jev = FakeJev(noul=0.9, score=3.0)
    d = decide_cluster(jev, evidence)
    assert d.source == "jev"
    assert d.per_item == {"3.1.6": 0.9, "3.3.6": 0.9}
    assert d.probability == pytest.approx(1 - 0.1 * 0.1)
    assert d.strength == pytest.approx(4.0)
    assert jev.calls[0]["task"] == "signal_decide"
    assert jev.calls[0]["state"] == evidence


def test_fallback_logistic_formula():
    evidence = {"oecd": {"3.1.6": {"pct": 1.0}, "3.2.11": {"pct": None, "value": 1.0},
                         "3.1.1": {"pct": 1.0}, "3.3.9": {"pct": None, "value": 2.0}}}
    d = fallback_decision(evidence)
    z = FALLBACK_BIAS + 2.0 * 1.0 + 2.5 * 1.0  # 3.1.1 has no weight, 3.3.9 has no percentile
    assert d.source == "fallback"
    assert d.probability == pytest.approx(1 / (1 + math.exp(-z)), abs=1e-6)
    assert d.per_item == {"3.1.6": 1.0, "3.2.11": 1.0, "3.3.9": 0.0}
    assert isinstance(d, Decision)


def test_fallback_ignores_entry_test_without_signal():
    base = {"3.1.6": {"pct": 1.0}}
    no_drop = fallback_decision({"oecd": {**base, "3.3.10": {"pct": 0.95, "signal": False}}})
    drop = fallback_decision({"oecd": {**base, "3.3.10": {"pct": 0.95, "signal": True}}})
    assert no_drop.per_item["3.3.10"] == 0.0
    assert drop.per_item["3.3.10"] == pytest.approx(0.95)
    assert drop.probability > no_drop.probability


def test_fallback_without_evidence_is_low():
    assert fallback_decision({"oecd": {}}).probability < 0.05


# Database.

def test_build_evidence_for_cartel_cluster(db):
    m, run_id = _setup(db)
    ev = build_evidence(db, run_id, CLUSTER_ID)
    members = sorted(m["cartel"])
    assert ev["members"] == [{"key": k, "bidder_id": b} for k, b in zip("ABC", members)]
    assert ev["n_lots"] == len(m["cartel_lots"])
    assert ev["n_procedures"] == len(m["cartel_lots"])
    assert ev["period"]["from"] <= ev["period"]["to"]
    assert ev["market"]["market_id"] == "cpv5_prov:45210:30"
    # 3.2.11 rows are all 0 (no shared data): absent, not a weak item.
    assert set(ev["oecd"]) == {"3.1.1", "3.1.2", "3.1.6", "3.3.6", "3.3.7"}
    assert ev["oecd"]["3.1.6"]["n"] == 3  # outsider pair excluded
    assert ev["oecd"]["3.1.6"]["pct"] == pytest.approx(0.97)
    assert ev["oecd"]["3.1.1"]["n"] == 3  # outsider firm excluded
    assert ev["oecd"]["3.1.2"]["top"][0]["e"] == "klaster"
    assert ev["oecd"]["3.3.7"]["n"] == len(m["cartel_lots"])
    assert ev["oecd"]["3.3.7"]["high"] == len(m["cartel_lots"])
    assert 0 < len(ev["lots"]) <= 20
    assert all(lot["winner"] in ("A", "B", "C") for lot in ev["lots"])
    assert all(len(lot["bidders"]) == 3 for lot in ev["lots"])
    text = canonical_json(ev)
    assert len(text) <= MAX_CHARS
    assert "Firma Testowa" not in text and "Jan Testowy" not in text
    assert evidence_hash(ev) == evidence_hash(json.loads(text))
    assert len(evidence_hash(ev)) == 64


def test_evidence_query_reads_only_the_cluster_entities(db):
    """C2: rows of outsiders (firm and pair keys of the same market) are not fetched."""
    m, run_id = _setup(db)
    members = sorted(m["cartel"])
    market_id = "cpv5_prov:45210:30"
    o1, o2 = sorted(m["outsiders"])[:2]
    # An outsider's lot row of the same market must not be read either.
    outsider_lot = next(lot for lot in m["lots"] if lot not in set(m["cartel_lots"]))
    db.execute(
        "insert into test_results(run_id, test_id, level, entity_key, market_id, value,"
        " percentile, n_obs) values (%s, '3.3.7', 'lot', %s, %s, 9.0, 1.0, 30)",
        (run_id, str(outsider_lot), market_id),
    )
    db.commit()
    rows = _test_rows(db, run_id, CLUSTER_ID, market_id, members, m["cartel_lots"])
    keys = {r["entity_key"] for r in rows}
    assert f"{o1}@{market_id}" not in keys
    assert f"{o1}-{o2}|{market_id}" not in keys
    assert str(outsider_lot) not in keys
    assert {f"{f}@{market_id}" for f in members} <= keys
    assert CLUSTER_ID in keys
    assert {str(lot) for lot in m["cartel_lots"]} <= keys


def test_entry_test_prefers_cluster_row_and_keeps_signal_flag(db):
    _, run_id = _setup(db)
    market_id = "cpv5_prov:45210:30"
    db.cursor().executemany(
        "insert into test_results(run_id, test_id, level, entity_key, market_id, value,"
        " percentile, n_obs, evidence) values (%s, '3.3.10', %s, %s, %s, %s, %s, 20, %s)",
        [(run_id, "cluster", CLUSTER_ID, market_id, -0.02, 0.6, Jsonb({"signal": False})),
         (run_id, "market", market_id, market_id, -0.3, 0.99, Jsonb({"signal": True}))],
    )
    db.commit()
    item = build_evidence(db, run_id, CLUSTER_ID)["oecd"]["3.3.10"]
    assert item["level"] == "cluster" and item["signal"] is False
    assert item["value"] == pytest.approx(-0.02)
    assert {t["e"]: t["s"] for t in item["top"]} == {"klaster": False, "rynek": True}


def test_default_max_status_never_publishes(db, monkeypatch):
    """I2: without ENGINE_MAX_STATUS the strongest Jev answer stays in review."""
    monkeypatch.delenv("ENGINE_MAX_STATUS", raising=False)
    _, run_id = _setup(db)
    counts = upsert_signals(db, run_id, FakeJev(noul=0.99))
    assert counts["published"] == 0 and counts["review"] == 1
    assert _signals(db)[0]["status"] == "review"


def test_max_status_from_environment(db, monkeypatch):
    monkeypatch.setenv("ENGINE_MAX_STATUS", "published")
    _, run_id = _setup(db)
    assert upsert_signals(db, run_id, FakeJev(noul=0.9))["published"] == 1


def test_cartel_published_with_fake_jev(db):
    _, run_id = _setup(db)
    jev = FakeJev(noul=0.9)
    counts = upsert_signals(db, run_id, jev, max_status="published")
    assert counts["published"] == 1 and counts["jev_calls"] == 1
    (s,) = _signals(db)
    assert s["signal_id"] == CLUSTER_ID and s["cluster_id"] == CLUSTER_ID
    assert s["status"] == "published"
    assert s["probability"] >= 0.91
    assert 1 <= s["strength"] <= 5
    assert s["oecd"]["3.1.6"] == pytest.approx(0.9)
    assert s["evidence"]["decision"]["source"] == "jev"
    assert len(s["evidence_hash"]) == 64


def test_low_answers_are_hidden(db):
    _, run_id = _setup(db)
    counts = upsert_signals(db, run_id, FakeJev(noul=0.05, score=0.2))
    assert counts["hidden"] == 1
    assert _signals(db)[0]["status"] == "hidden"


def test_no_decider_gives_review_from_fallback(db):
    """Review Focus 5: no Jev key means fallback signals capped at review."""
    _, run_id = _setup(db)
    counts = upsert_signals(db, run_id, None)
    assert counts == {"clusters": 1, "published": 0, "review": 1, "hidden": 0,
                      "jev_calls": 0, "reused": 0, "fallback": 1}
    (s,) = _signals(db)
    assert s["status"] == "review"
    assert s["evidence"]["decision"]["source"] == "fallback"


@pytest.mark.parametrize("error", [BudgetExceeded("limit"), RuntimeError("jev 500")])
def test_provider_failure_falls_back_to_review(db, error):
    _, run_id = _setup(db)
    counts = upsert_signals(db, run_id, FakeJev(error=error))
    assert counts["fallback"] == 1 and counts["review"] == 1
    (s,) = _signals(db)
    assert s["status"] == "review"
    assert s["evidence"]["decision"]["source"] == "fallback"


def test_fallback_never_publishes_even_above_publish_threshold(db):
    _, run_id = _setup(db)
    upsert_signals(db, run_id, None, publish_threshold=0.1, review_threshold=0.05)
    assert _signals(db)[0]["status"] == "review"


def test_rerun_without_changes_does_not_call_jev_nor_duplicate(db):
    """Review Focus 3: second run with the same evidence reuses the decision."""
    m, run1 = _setup(db)
    upsert_signals(db, run1, FakeJev(noul=0.9), max_status="published")
    first = _signals(db)[0]

    run2 = _run(db, m["as_of"])
    _setup(db, market=m, run_id=run2)
    jev = FakeJev(noul=0.9)
    counts = upsert_signals(db, run2, jev, max_status="published")
    assert len(jev.calls) == 0
    assert counts["reused"] == 1 and counts["jev_calls"] == 0
    rows = _signals(db)
    assert len(rows) == 1
    s = rows[0]
    assert s["signal_id"] == first["signal_id"]
    assert s["run_id"] == run2
    assert s["evidence_hash"] == first["evidence_hash"]
    assert s["status"] == "published"
    assert s["probability"] == pytest.approx(first["probability"])
    assert s["created_at"] == first["created_at"]


def test_changed_evidence_asks_jev_again(db):
    m, run1 = _setup(db)
    upsert_signals(db, run1, FakeJev(noul=0.9))
    run2 = _run(db, m["as_of"])
    _setup(db, market=m, run_id=run2, strong=False)
    jev = FakeJev(noul=0.05, score=0.0)
    upsert_signals(db, run2, jev)
    assert len(jev.calls) == 1
    rows = _signals(db)
    assert len(rows) == 1 and rows[0]["status"] == "hidden"


def test_fallback_signal_asks_jev_once_available(db):
    m, run1 = _setup(db)
    upsert_signals(db, run1, None)
    run2 = _run(db, m["as_of"])
    _setup(db, market=m, run_id=run2)
    jev = FakeJev(noul=0.9)
    counts = upsert_signals(db, run2, jev, max_status="published")
    assert len(jev.calls) == 1 and counts["published"] == 1


class HttpFailJev(FakeJev):
    """Fails every call with the given HTTP status."""

    def __init__(self, status: int):
        super().__init__(error=ProviderHttpError(
            "jev", status, f"jev request failed: HTTP {status}: unknown field 'questions'"))


def _clusters(db, run_id, m, n):
    """n more clusters of the same members in the run (distinct ids, same evidence)."""
    for i in range(n):
        db.execute(
            "insert into clusters(run_id, cluster_id, market_id, members, n_lots, density)"
            " values (%s, %s, 'cpv5_prov:45210:30', %s, %s, 1.0)",
            (run_id, f"c0ffee00000000{i:02d}", sorted(m["cartel"]), len(m["cartel_lots"])),
        )
    db.commit()


@pytest.mark.parametrize("status", [400, 404, 422, 401])
def test_client_error_disables_jev_for_the_rest_of_the_run(db, status):
    """C3: one 4xx (other than 429) and Jev is not asked again in this run."""
    m, run_id = _setup(db)
    _clusters(db, run_id, m, 4)
    jev = HttpFailJev(status)
    counts = upsert_signals(db, run_id, jev)
    assert len(jev.calls) == 1
    assert counts["clusters"] == 5 and counts["fallback"] == 5 and counts["jev_calls"] == 0
    assert counts["jev_disabled_reason"] == f"HTTP {status}"
    assert counts["warning"] == "decider configured, 0 jev calls"
    params = db.execute("select params from engine_runs where run_id = %s",
                        (run_id,)).fetchone()["params"]
    assert params["jev_disabled_reason"] == f"HTTP {status}"


def test_rate_limit_keeps_asking_but_stops_after_consecutive_errors(db):
    m, run_id = _setup(db)
    _clusters(db, run_id, m, 7)
    jev = HttpFailJev(429)
    counts = upsert_signals(db, run_id, jev)
    assert len(jev.calls) == 5
    assert counts["fallback"] == 8
    assert counts["jev_disabled_reason"] == "5 consecutive errors"


def test_budget_exceeded_is_recorded(db):
    m, run_id = _setup(db)
    _clusters(db, run_id, m, 2)
    jev = FakeJev(error=BudgetExceeded("limit"))
    counts = upsert_signals(db, run_id, jev)
    assert len(jev.calls) == 1 and counts["jev_disabled_reason"] == "budget exceeded"
