"""Engine orchestration: `run_engine`, `tie engine-run` and the `engine_run` job handler.

The end-to-end tests need every engine module (Tasks 1-9 of part 2); until they are all
merged those tests are skipped and only the CLI and handler wiring is checked.
"""

import importlib.util
import os
import sys
import types
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tie import cli
from tie.handlers import HANDLERS, Context
from tie.http import PoliteClient

ENGINE_MODULES = ("oecd", "markets", "graph", "tests_lot", "tests_firm", "tests_pair",
                  "tests_market", "evidence", "decide", "explain")
HERE = Path(__file__).parent
ROTATION = {"members": 3, "pattern": "rotation", "share": 0.4}


def _missing_engine() -> list[str]:
    missing = []
    for name in ENGINE_MODULES:
        try:
            found = importlib.util.find_spec(f"tie.engine.{name}") is not None
        except ImportError:
            found = False
        if not found:
            missing.append(f"tie.engine.{name}")
    for name in ("__init__.py", "synth.py"):
        if not (HERE / name).exists():
            missing.append(f"tests/engine/{name}")
    return missing


MISSING = _missing_engine()
needs_engine = pytest.mark.skipif(
    bool(MISSING), reason=f"engine modules not merged yet: {', '.join(MISSING)}")


@pytest.fixture
def dburl(db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    return os.environ["TEST_DATABASE_URL"]


def ctx_for(db, decider=None, text_provider=None):
    return Context(conn=db, settings=None, client=PoliteClient("ua", {}, sleep=lambda s: None),
                   rawstore=None, text_provider=text_provider, decider=decider)


def _find(node, key):
    if isinstance(node, dict):
        if key in node:
            yield node[key]
        for value in node.values():
            yield from _find(value, key)
    elif isinstance(node, list):
        for value in node:
            yield from _find(value, key)


class FakeJev:
    """Jev stand-in: strong answers for the cartel's cluster, weak for any other cluster."""

    def __init__(self, db, cartel):
        self.db = db
        self.cartel = set(cartel)
        self.calls = 0

    def _members(self, state) -> set[int]:
        for cluster_id in _find(state, "cluster_id"):
            row = self.db.execute(
                "select members from clusters where cluster_id = %s"
                " order by run_id desc limit 1", (cluster_id,)).fetchone()
            if row:
                return set(row["members"])
        for members in _find(state, "members"):
            if isinstance(members, list):
                return {m if isinstance(m, int) else m.get("bidder_id") for m in members}
        return set()

    def decide(self, *, task, state, questions):
        self.calls += 1
        hit = self._members(state) == self.cartel
        answers = {}
        for key, question in questions.items():
            if question.get("type") == "score":
                answers[key] = {"type": "score", "score": 4 if hit else 0}
            else:
                answers[key] = {"type": "noul", "noul": 0.97 if hit else 0.02}
        return answers


def test_engine_run_handler_registered():
    assert "engine_run" in HANDLERS


def test_cli_engine_run_queues_job(db, dburl, monkeypatch):
    monkeypatch.setattr(cli, "today", lambda: date(2026, 10, 12))
    assert cli.main(["engine-run"]) == 0
    assert cli.main(["engine-run", "--as-of", "2026-01-01", "--full"]) == 0
    rows = db.execute("select key, payload from jobs where kind = 'engine_run'"
                      " order by id").fetchall()
    assert [r["payload"] for r in rows] == [{"as_of": "2026-10-12", "full": False},
                                            {"as_of": "2026-01-01", "full": True}]
    assert rows[0]["key"].startswith("2026-10-12@")
    assert rows[1]["key"].startswith("2026-01-01:full@")


def test_cli_engine_run_rejects_bad_date():
    with pytest.raises(SystemExit):
        cli.main(["engine-run", "--as-of", "12.10.2026"])


def test_engine_run_handler_calls_run_engine(db, monkeypatch):
    seen = []

    def fake_run_engine(ctx, as_of, *, full=False):
        seen.append((ctx, as_of, full))
        return {"run_id": 1, "status": "done", "as_of": as_of.isoformat(), "full": full,
                "counts": {}}

    fake = types.ModuleType("tie.engine.run")
    fake.run_engine = fake_run_engine
    monkeypatch.setitem(sys.modules, "tie.engine.run", fake)
    ctx = ctx_for(db)
    HANDLERS["engine_run"](ctx, {"as_of": "2026-01-01", "full": True})
    HANDLERS["engine_run"](ctx, {})
    assert seen[0] == (ctx, date(2026, 1, 1), True)
    assert seen[1][1] in {datetime.now(UTC).date(), date.today()} and seen[1][2] is False


def _published(db):
    return db.execute(
        "select s.signal_id, s.evidence_hash, c.members from signals s"
        " join clusters c on c.cluster_id = s.cluster_id and c.run_id = s.run_id"
        " where s.status = 'published' order by s.signal_id").fetchall()


@needs_engine
def test_full_run_on_cartel_market(db, monkeypatch):
    from tie.engine.run import run_engine

    from .synth import make_market

    # Publication needs an explicit operator decision (default is "review").
    monkeypatch.setenv("ENGINE_MAX_STATUS", "published")
    out = make_market(db, seed=1, n_firms=12, n_lots=80, cpv="45233142-6",
                      province_code="PL22", cartel=ROTATION)
    jev = FakeJev(db, out["cartel"])
    ctx = ctx_for(db, decider=jev)

    first = run_engine(ctx, out["as_of"])
    assert first["status"] == "done"
    run = db.execute("select * from engine_runs where run_id = %s",
                     (first["run_id"],)).fetchone()
    assert run["status"] == "done" and run["finished_at"] is not None
    assert run["as_of"] == out["as_of"]
    assert run["params"]["full"] is False and "signals" in run["params"]["counts"]
    assert run["params"]["max_status"] == "published"
    assert run["params"]["counts"]["prune"]["test_results"] == 0

    published = _published(db)
    assert len(published) == 1
    signal = published[0]
    assert set(signal["members"]) == set(out["cartel"])
    explanations = db.execute(
        "select source, evidence_hash from explanations where signal_id = %s",
        (signal["signal_id"],)).fetchall()
    assert [(e["source"], e["evidence_hash"]) for e in explanations] == [
        ("template", signal["evidence_hash"])]
    n_signals = db.execute("select count(*) n from signals").fetchone()["n"]

    # Second run on the same day: new run, the same signal, no duplicates.
    second = run_engine(ctx, out["as_of"])
    assert second["status"] == "done"
    assert second["run_id"] != first["run_id"]
    again = _published(db)
    assert [s["signal_id"] for s in again] == [signal["signal_id"]]
    assert db.execute("select count(*) n from signals").fetchone()["n"] == n_signals
    assert db.execute("select count(*) n from explanations where signal_id = %s",
                      (signal["signal_id"],)).fetchone()["n"] == 1
    statuses = [r["status"] for r in db.execute(
        "select status from engine_runs order by run_id").fetchall()]
    assert statuses == ["done", "done"]

    # C1: a third run prunes the first one; the API does not change.
    api_before = db.execute("select * from api.signals").fetchall()
    assert [s["signal_id"] for s in api_before] == [signal["signal_id"]]
    # An explanation of an older evidence version of the signal goes once the current exists.
    db.execute(
        "insert into explanations(signal_id, evidence_hash, source, title, summary)"
        " values (%s, 'h-older', 'template', 'stary', 'stary')", (signal["signal_id"],))
    db.commit()
    third = run_engine(ctx, out["as_of"])
    assert third["status"] == "done"
    for table in ("test_results", "pairs", "clusters"):
        runs = {r["run_id"] for r in db.execute(f"select distinct run_id from {table}")}
        assert runs == {second["run_id"], third["run_id"]}, table
    assert third["counts"]["prune"]["test_results"] > 0
    assert third["counts"]["prune"]["explanations"] == 1
    assert [e["evidence_hash"] for e in db.execute(
        "select evidence_hash from explanations where signal_id = %s",
        (signal["signal_id"],))] == [signal["evidence_hash"]]
    api_after = db.execute("select * from api.signals").fetchall()
    assert api_after == api_before
    params = db.execute("select params from engine_runs where run_id = %s",
                        (third["run_id"],)).fetchone()["params"]
    assert params["counts"]["prune"]["explanations"] == 1


@needs_engine
def test_default_max_status_publishes_nothing(db, monkeypatch):
    from tie.engine.run import run_engine

    from .synth import make_market

    monkeypatch.delenv("ENGINE_MAX_STATUS", raising=False)
    out = make_market(db, seed=1, n_firms=12, n_lots=80, cpv="45233142-6",
                      province_code="PL22", cartel=ROTATION)
    result = run_engine(ctx_for(db, decider=FakeJev(db, out["cartel"])), out["as_of"])
    assert result["counts"]["signals"]["published"] == 0
    assert result["counts"]["signals"]["review"] >= 1
    assert db.execute("select * from api.signals").fetchall() == []


class HalfJev:
    """Jev answering 0.5 on every item of every cluster."""

    def __init__(self):
        self.calls = []

    def decide(self, *, task, state, questions):
        self.calls.append(state)
        return {key: ({"type": "score", "score": 2} if q.get("type") == "score"
                      else {"type": "noul", "noul": 0.5})
                for key, q in questions.items()}


PROVINCES = [f"PL{n:02d}" for n in range(2, 33, 2)]


def _spread_buyers(db, buyers):
    """Put each buyer in another voivodeship, so the market is pooled across Poland."""
    for i, buyer in enumerate(buyers):
        db.execute("update buyers set province = %s where id = %s",
                   (PROVINCES[i % len(PROVINCES)], buyer))


def _home_wins(db, out):
    """Each of the two firms wins at its own buyers: the first at the first half of the
    buyers, the second at the rest. Both still bid in every lot (prices swapped)."""
    a, b = out["firms"]
    half = set(out["buyers"][: len(out["buyers"]) // 2])
    for lot_id in out["lots"]:
        buyer = db.execute(
            "select p.buyer_id from lots l join procedures p on p.ocds_id = l.procedure_id"
            " where l.id = %s", (lot_id,)).fetchone()["buyer_id"]
        want = a if buyer in half else b
        bids = {r["bidder_id"]: r for r in db.execute(
            "select id, bidder_id, price from bids where lot_id = %s", (lot_id,))}
        low, high = sorted(r["price"] for r in bids.values())
        loser = b if want == a else a
        db.execute("update bids set price = %s, is_winner = true, rank = 1 where id = %s",
                   (low, bids[want]["id"]))
        db.execute("update bids set price = %s, is_winner = false, rank = 2 where id = %s",
                   (high, bids[loser]["id"]))
        db.execute("update lots set winner_bidder_id = %s, price_winner = %s,"
                   " contract_value = %s where id = %s", (want, low, low, lot_id))


@needs_engine
def test_two_vets_in_a_pooled_market_are_never_published(db, monkeypatch):
    """Final review I2: two honest practices, 40 common lots, each wins at its own buyers, in
    a cpv3_pl market pooled from small regional markets. Even with publication allowed and
    a Jev answering 0.5 on every item, the signal must not be published."""
    from tie.engine.run import run_engine

    from .synth import make_market

    monkeypatch.setenv("ENGINE_MAX_STATUS", "published")
    vets = make_market(db, seed=22, n_firms=2, n_lots=40, cpv="85200000-1",
                       province_code="PL04")
    # Other veterinary firms in the same 3-digit group, also spread over Poland.
    others = make_market(db, seed=23, n_firms=10, n_lots=60, cpv="85210000-3",
                         province_code="PL04")
    _spread_buyers(db, vets["buyers"])
    _spread_buyers(db, others["buyers"])
    _home_wins(db, vets)
    db.commit()

    jev = HalfJev()
    result = run_engine(ctx_for(db, decider=jev), vets["as_of"])
    assert result["status"] == "done"

    pair = sorted(vets["firms"])
    cluster = db.execute(
        "select cluster_id, market_id from clusters where run_id = %s"
        " and members = %s::bigint[]",
        (result["run_id"], pair)).fetchone()
    assert cluster is not None and cluster["market_id"] == "cpv3_pl:852:PL"
    split = db.execute(
        "select value from test_results where run_id = %s and test_id = '3.1.2'"
        " and entity_key = %s", (result["run_id"], cluster["cluster_id"])).fetchone()
    assert split is not None and split["value"] == 1.0  # the scenario of the review
    signal = db.execute("select status, probability, oecd, evidence from signals"
                        " where signal_id = %s", (cluster["cluster_id"],)).fetchone()
    assert signal["evidence"]["decision"]["source"] == "jev"
    assert signal["status"] != "published"
    assert db.execute("select count(*) n from signals where status = 'published'"
                      ).fetchone()["n"] == 0
    assert db.execute("select * from api.signals").fetchall() == []
    # Items without a percentile never reached the model.
    for state in jev.calls:
        for item, numbers in state["oecd"].items():
            assert numbers.get("pct") is not None or item in ("3.2.11", "3.3.5", "3.1.7")


@needs_engine
def test_failed_step_marks_run_failed_and_releases_lock(db, monkeypatch):
    from tie.engine import run as run_mod

    def boom(conn, run_id, as_of, **kwargs):
        raise RuntimeError("markets broke")

    monkeypatch.setattr(run_mod, "assign_markets", boom)
    with pytest.raises(RuntimeError, match="markets broke"):
        run_mod.run_engine(ctx_for(db), date(2026, 1, 1))
    run = db.execute("select * from engine_runs order by run_id desc limit 1").fetchone()
    assert run["status"] == "failed" and run["finished_at"] is not None
    assert run["params"]["failed_step"] == "markets"
    assert "markets broke" in run["params"]["error"]
    assert run["params"]["counts"]["catalog"] == 51

    monkeypatch.undo()
    # The lock was released: an empty database runs to the end.
    assert run_mod.run_engine(ctx_for(db), date(2026, 1, 1))["status"] == "done"
