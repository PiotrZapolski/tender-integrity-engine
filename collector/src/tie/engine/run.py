"""One engine run: markets, co-bidder graph, OECD tests, signals and explanations.

`run_engine` opens a new `engine_runs` row and executes every step in order under its
`run_id`: OECD catalog seed, markets, pairs, clusters, lot/firm/pair/market tests, BZP-only
screens with their winner-based clusters (`tie.engine.bzp_only`), signals, explanations of
published signals that have none for their current evidence. Each step is committed on its
own; the step counters land in `engine_runs.params["counts"]` and the
status becomes `done`. Any error marks the run `failed` (with the step and the error) and
re-raises, so the job queue retries it.

A session advisory lock keeps two runs from writing the analytical tables at once (for
example the worker job and a manual `tie engine-run --now`).

After a run is `done`, per-run history is pruned: `test_results`, `pairs` and `clusters`
keep only the rows of the last `KEEP_DONE_RUNS` done runs (rows of failed or abandoned runs
go too), and `explanations` drops the rows of an older evidence hash once the signal has an
explanation for its current hash. `lot_market` and `markets` hold no history (one row per
lot / market). A pruning error is logged and does not fail the finished run.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from tie.config import engine_max_status, engine_min_lots
from tie.engine.bzp_only import compute_bzp_only
from tie.engine.decide import upsert_signals
from tie.engine.explain import explain_signal
from tie.engine.graph import build_pairs, find_clusters
from tie.engine.markets import assign_markets
from tie.engine.oecd import seed_catalog
from tie.engine.tests_firm import compute_firm_tests
from tie.engine.tests_lot import compute_lot_tests
from tie.engine.tests_market import compute_market_tests
from tie.engine.tests_pair import compute_pair_tests

log = logging.getLogger(__name__)

# Arbitrary constant key of the advisory lock that serializes engine runs.
ENGINE_LOCK_KEY = 720_261_012
# A run still `running` after this long was lost (worker killed); it is marked failed.
STALE_RUN_INTERVAL = "12 hours"
MAX_ERROR_LEN = 2000
# Per-run tables keep the rows of this many latest done runs.
KEEP_DONE_RUNS = 2
PER_RUN_TABLES = ("test_results", "pairs", "clusters")

UNEXPLAINED_SQL = """
select s.signal_id from signals s
where s.status = 'published'
  and not exists (select 1 from explanations e
                  where e.signal_id = s.signal_id and e.evidence_hash = s.evidence_hash)
order by s.signal_id
"""


PRUNE_EXPLANATIONS_SQL = """
delete from explanations e
using signals s
where s.signal_id = e.signal_id
  and e.evidence_hash is distinct from s.evidence_hash
  and exists (select 1 from explanations c
              where c.signal_id = s.signal_id and c.evidence_hash = s.evidence_hash)
"""


class EngineBusy(RuntimeError):
    """Another engine run holds the lock."""


def prune(conn: psycopg.Connection, keep: int = KEEP_DONE_RUNS) -> dict[str, int]:
    """Delete per-run rows outside the last `keep` done runs and stale explanations.

    Must run under the engine lock (no other run writing). Commits; returns deleted rows per
    table. With no done run nothing is deleted.
    """
    kept = [r["run_id"] for r in conn.execute(
        "select run_id from engine_runs where status = 'done' order by run_id desc limit %s",
        (keep,),
    ).fetchall()]
    deleted: dict[str, int] = {}
    if kept:
        for table in PER_RUN_TABLES:
            cur = conn.execute(f"delete from {table} where not (run_id = any(%s))", (kept,))
            deleted[table] = cur.rowcount
    deleted["explanations"] = conn.execute(PRUNE_EXPLANATIONS_SQL).rowcount
    conn.commit()
    return deleted


def _start(conn: psycopg.Connection, as_of: date, full: bool, max_status: str,
           min_lots: int) -> int:
    conn.execute(
        "update engine_runs set status = 'failed', finished_at = now(),"
        " params = params || jsonb_build_object('error', 'abandoned: still running after '"
        "   || %s::text)"
        " where status = 'running' and started_at < now() - %s::interval",
        (STALE_RUN_INTERVAL, STALE_RUN_INTERVAL),
    )
    run_id = conn.execute(
        "insert into engine_runs(as_of, params, status) values (%s, %s, 'running')"
        " returning run_id",
        (as_of, Jsonb({"full": full, "max_status": max_status, "min_lots": min_lots})),
    ).fetchone()["run_id"]
    conn.commit()
    return run_id


def _finish(conn: psycopg.Connection, run_id: int, status: str, extra: dict) -> None:
    conn.execute(
        "update engine_runs set status = %s, finished_at = now(), params = params || %s"
        " where run_id = %s",
        (status, Jsonb(extra), run_id),
    )
    conn.commit()


def _explain_published(conn: psycopg.Connection, provider: Any) -> dict[str, int]:
    """Explain published signals without an explanation for their current evidence hash.

    One signal failing (budget, provider error) does not stop the others; it is retried by
    the next run because it still has no explanation.
    """
    ids = [r["signal_id"] for r in conn.execute(UNEXPLAINED_SQL).fetchall()]
    conn.commit()
    sources: dict[str, int] = {}
    failed = 0
    for signal_id in ids:
        try:
            source = explain_signal(conn, provider, signal_id)
            conn.commit()
        except (psycopg.OperationalError, psycopg.InterfaceError):
            raise  # the database is gone: fail the run
        except Exception:
            conn.rollback()
            failed += 1
            log.exception("engine: explanation of signal %s failed", signal_id)
            continue
        sources[source] = sources.get(source, 0) + 1
    return {"explained": sum(sources.values()), "failed": failed, **sources}


def run_engine(ctx, as_of: date, *, full: bool = False) -> dict:
    """Run the whole engine for `as_of` and return `{run_id, status, as_of, full, counts}`.

    `full` is recorded in the run params. Today every run recomputes all markets in its
    window; the flag marks the weekly full recompute once daily runs become incremental.
    Signals use `ctx.decider` (Jev; None means the fallback regression and at most
    `review`), explanations use `ctx.text_provider` (DeepSeek; None keeps the template).
    No signal gets a status above `ENGINE_MAX_STATUS` (`ctx.settings`, else the environment;
    default `review`).
    """
    conn = ctx.conn
    settings = getattr(ctx, "settings", None)
    max_status = (settings.engine_max_status if settings is not None
                  else engine_max_status())
    min_lots = getattr(settings, "engine_min_lots", None)
    if min_lots is None:
        min_lots = engine_min_lots()
    got = conn.execute("select pg_try_advisory_lock(%s) ok",
                       (ENGINE_LOCK_KEY,)).fetchone()["ok"]
    conn.commit()
    if not got:
        raise EngineBusy("another engine run is in progress")
    try:
        run_id = _start(conn, as_of, full, max_status, min_lots)
        log.info("engine run %s: as_of=%s full=%s max_status=%s min_lots=%s", run_id, as_of,
                 full, max_status, min_lots)
        steps = [
            ("catalog", lambda: seed_catalog(conn)),
            ("markets", lambda: assign_markets(conn, run_id, as_of)),
            ("pairs", lambda: build_pairs(conn, run_id)),
            ("clusters", lambda: find_clusters(conn, run_id)),
            ("lot_tests", lambda: compute_lot_tests(conn, run_id)),
            ("firm_tests", lambda: compute_firm_tests(conn, run_id)),
            ("pair_tests", lambda: compute_pair_tests(conn, run_id)),
            ("market_tests", lambda: compute_market_tests(conn, run_id)),
            # After `clusters`: find_clusters replaces every cluster of the run.
            ("bzp_only", lambda: compute_bzp_only(conn, run_id)),
            ("signals", lambda: upsert_signals(conn, run_id, ctx.decider,
                                               max_status=max_status, min_lots=min_lots)),
            ("explanations", lambda: _explain_published(conn, ctx.text_provider)),
        ]
        counts: dict[str, Any] = {}
        for name, step in steps:
            try:
                counts[name] = step()
                conn.commit()
            except BaseException as exc:
                log.exception("engine run %s: step %s failed", run_id, name)
                try:
                    conn.rollback()
                    _finish(conn, run_id, "failed", {
                        "counts": counts, "failed_step": name,
                        "error": repr(exc)[:MAX_ERROR_LEN]})
                except psycopg.Error:
                    log.warning("engine run %s: could not record the failure", run_id,
                                exc_info=True)
                raise
            log.info("engine run %s: %s %s", run_id, name, counts[name])
        _finish(conn, run_id, "done", {"counts": counts})
        log.info("engine run %s: done", run_id)
        try:
            counts["prune"] = prune(conn)
            conn.execute(
                "update engine_runs set params = jsonb_set(params, '{counts,prune}', %s)"
                " where run_id = %s",
                (Jsonb(counts["prune"]), run_id),
            )
            conn.commit()
            log.info("engine run %s: prune %s", run_id, counts["prune"])
        except psycopg.Error:
            conn.rollback()
            log.warning("engine run %s: pruning failed", run_id, exc_info=True)
        return {"run_id": run_id, "status": "done", "as_of": as_of.isoformat(),
                "full": full, "counts": counts}
    finally:
        try:
            conn.rollback()
            conn.execute("select pg_advisory_unlock(%s)", (ENGINE_LOCK_KEY,))
            conn.commit()
        except psycopg.Error:
            log.warning("engine: advisory unlock failed", exc_info=True)
