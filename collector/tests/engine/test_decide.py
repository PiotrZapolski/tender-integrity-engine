import json
import math
from itertools import combinations

import pytest

from tie.ai.providers import BudgetExceeded
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
    upsert_signals,
)
from tie.engine.evidence import MAX_CHARS, build_evidence, canonical_json, evidence_hash

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
    assert jev.calls[0]["state"] is evidence


def test_fallback_logistic_formula():
    evidence = {"oecd": {"3.1.6": {"pct": 1.0}, "3.2.11": {"pct": None, "value": 1.0},
                         "3.1.1": {"pct": 1.0}, "3.3.9": {"pct": None, "value": 2.0}}}
    d = fallback_decision(evidence)
    z = FALLBACK_BIAS + 2.0 * 1.0 + 2.5 * 1.0  # 3.1.1 has no weight, 3.3.9 has no percentile
    assert d.source == "fallback"
    assert d.probability == pytest.approx(1 / (1 + math.exp(-z)), abs=1e-6)
    assert d.per_item == {"3.1.6": 1.0, "3.2.11": 1.0, "3.3.9": 0.0}
    assert isinstance(d, Decision)


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
    assert set(ev["oecd"]) == {"3.1.1", "3.1.2", "3.1.6", "3.2.11", "3.3.6", "3.3.7"}
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


def test_cartel_published_with_fake_jev(db):
    _, run_id = _setup(db)
    jev = FakeJev(noul=0.9)
    counts = upsert_signals(db, run_id, jev)
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
    upsert_signals(db, run1, FakeJev(noul=0.9))
    first = _signals(db)[0]

    run2 = _run(db, m["as_of"])
    _setup(db, market=m, run_id=run2)
    jev = FakeJev(noul=0.9)
    counts = upsert_signals(db, run2, jev)
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
    counts = upsert_signals(db, run2, jev)
    assert len(jev.calls) == 1 and counts["published"] == 1
