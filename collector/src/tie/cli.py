"""`tie` command line: migrate, backfill, daily, worker, stats, gold-extract, gold-export."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg

from tie.config import load_settings
from tie.db import connect, migrate
from tie.gold.extract import GOLD_RATES, export_gold, run_gold_extract
from tie.gold.sources import UOKIK_DECISIONS, get_decision
from tie.handlers import Context, run_once
from tie.identity.bidders import MAX_IDENTITY_FAILURES
from tie.jobs import enqueue, requeue_stale

log = logging.getLogger("tie.cli")

IDLE_SLEEP_S = 5.0
REQUEUE_EVERY = 100
# Longer than any single job runs (identity stops itself after 10 minutes).
STALE_AFTER = timedelta(hours=1)


def today() -> date:
    return date.today()


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from exc


def _connect() -> psycopg.Connection:
    return connect(load_settings().database_url)


def cmd_migrate(args: argparse.Namespace) -> int:
    with _connect() as conn:
        applied = migrate(conn)
    log.info("migrate: applied %s", applied or "nothing")
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    until = args.until or today() - timedelta(days=1)
    if args.since > until:
        log.error("backfill: --since %s is after --until %s", args.since, until)
        return 2
    base = datetime.now(UTC)
    added = total = 0
    with _connect() as conn:
        day = until
        # Newest day first: each older day gets a run_after one second later.
        while day >= args.since:
            iso = day.isoformat()
            added += enqueue(conn, "bzp_day", iso, {"date": iso},
                             run_after=base + timedelta(seconds=total))
            total += 1
            day -= timedelta(days=1)
    log.info("backfill %s..%s: %d days, %d new jobs", args.since, until, total, added)
    return 0


def cmd_daily(args: argparse.Namespace) -> int:
    t = today()
    added = 0
    with _connect() as conn:
        for back in (1, 2):
            iso = (t - timedelta(days=back)).isoformat()
            # The run date in the key forces a fresh fetch of a day that may have been done.
            added += enqueue(conn, "bzp_day", f"{iso}@{t.isoformat()}", {"date": iso})
        added += enqueue(conn, "identity", t.isoformat())
        added += enqueue(conn, "whitelist", t.isoformat())
    log.info("daily %s: %d new jobs", t, added)
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()] if args.kinds else None
    stopping = False

    def on_signal(signum, frame):
        nonlocal stopping
        stopping = True
        log.info("worker: signal %s, stopping after the current job", signum)

    previous = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT)}
    ctx = Context.from_settings(load_settings())
    log.info("worker: kinds=%s max_jobs=%s text_provider=%s decider=%s", kinds or "all",
             args.max_jobs, ctx.text_provider is not None, ctx.decider is not None)
    done = since_requeue = 0
    try:
        requeue_stale(ctx.conn, STALE_AFTER)
        while not stopping and (args.max_jobs is None or done < args.max_jobs):
            if run_once(ctx, kinds):
                done += 1
                since_requeue += 1
                if since_requeue >= REQUEUE_EVERY:
                    requeue_stale(ctx.conn, STALE_AFTER)
                    since_requeue = 0
            else:
                requeue_stale(ctx.conn, STALE_AFTER)
                since_requeue = 0
                time.sleep(IDLE_SLEEP_S)
    finally:
        ctx.close()
        for s, handler in previous.items():
            signal.signal(s, handler)
    log.info("worker: stopped after %d jobs", done)
    return 0


def _stats(conn: psycopg.Connection) -> dict:
    def count(table: str) -> int:
        return conn.execute(f"select count(*) n from {table}").fetchone()["n"]

    documents = {r["status"]: r["n"] for r in conn.execute(
        "select status, count(*) n from documents group by status order by status")}
    jobs: dict[str, dict[str, int]] = {}
    for r in conn.execute("select kind, status, count(*) n from jobs"
                          " group by kind, status order by kind, status"):
        jobs.setdefault(r["kind"], {})[r["status"]] = r["n"]
    cost = conn.execute(
        "select coalesce(sum(cost_usd), 0) s from ai_calls"
        " where created_at >= date_trunc('day', now())").fetchone()["s"]
    checked = sum(documents.get(s, 0) for s in ("parsed", "mismatch", "needs_review"))
    excluded = conn.execute(
        "select count(*) n from bids where identity_failures >= %s",
        (MAX_IDENTITY_FAILURES,)).fetchone()["n"]
    conn.commit()
    return {
        "procedures": count("procedures"),
        "lots": count("lots"),
        "bids": count("bids"),
        "bids_identity_excluded": excluded,
        "bidders": count("bidders"),
        "documents": documents,
        "mismatch_share": round(documents.get("mismatch", 0) / checked, 4) if checked else None,
        "jobs": jobs,
        "ai_cost_today_usd": float(cost),
    }


def cmd_stats(args: argparse.Namespace) -> int:
    with _connect() as conn:
        print(json.dumps(_stats(conn), ensure_ascii=False, sort_keys=True))
    return 0


def cmd_gold_extract(args: argparse.Namespace) -> int:
    if args.only:
        try:
            decisions = (get_decision(args.only),)
        except KeyError:
            log.error("gold-extract: unknown decision %r (see tie.gold.sources)", args.only)
            return 2
    else:
        decisions = UOKIK_DECISIONS
    settings = load_settings()
    if not settings.deepseek_api_key:
        log.error("gold-extract: DEEPSEEK_API_KEY is not set")
        return 2
    ctx = Context.from_settings(settings)
    ctx.client.rates.update(GOLD_RATES)
    try:
        summary = run_gold_extract(ctx.conn, ctx.client, ctx.rawstore, ctx.text_provider,
                                   decisions, force=args.force)
    finally:
        ctx.close()
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 1 if summary["failed"] or summary["budget_exceeded"] else 0


def cmd_gold_export(args: argparse.Namespace) -> int:
    with _connect() as conn:
        n = export_gold(conn, Path(args.path))
    log.info("gold-export: %d cases written to %s", n, args.path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tie", description="Tender Integrity Engine collector")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)

    b = sub.add_parser("backfill", help="queue bzp_day jobs for a range of days")
    b.add_argument("--since", type=_date, required=True, help="first day, YYYY-MM-DD")
    b.add_argument("--until", type=_date, help="last day, YYYY-MM-DD (default: yesterday)")
    b.set_defaults(func=cmd_backfill)

    sub.add_parser("daily", help="queue the daily jobs").set_defaults(func=cmd_daily)

    w = sub.add_parser("worker", help="run queued jobs")
    w.add_argument("--kinds", help="comma separated job kinds (default: all)")
    w.add_argument("--max-jobs", type=int, help="exit after this many jobs")
    w.set_defaults(func=cmd_worker)

    sub.add_parser("stats", help="print counts as JSON").set_defaults(func=cmd_stats)

    g = sub.add_parser("gold-extract",
                       help="structure UOKiK bid-rigging decisions into gold_cases (DeepSeek)")
    g.add_argument("--only", metavar="ID", help="one decision, for example RLU-7/2023")
    g.add_argument("--force", action="store_true", help="extract again decisions already stored")
    g.set_defaults(func=cmd_gold_extract)

    e = sub.add_parser("gold-export", help="write gold_cases to a JSON file")
    e.add_argument("path", help="output file, for example validation/gold/uokik.json")
    e.set_defaults(func=cmd_gold_export)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
