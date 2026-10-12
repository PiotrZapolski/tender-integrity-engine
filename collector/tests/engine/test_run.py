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
def test_full_run_on_cartel_market(db):
    from tie.engine.run import run_engine

    from .synth import make_market

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
