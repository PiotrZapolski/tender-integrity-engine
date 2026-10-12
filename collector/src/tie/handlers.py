"""Job handlers and the single-job step of the worker.

`HANDLERS` maps a job kind to `handler(ctx, payload)`. `run_once` claims one ready job,
runs its handler and marks the job done, or records the failure (retry with backoff).
A handler hitting the AI daily budget (`BudgetExceeded`) does not use up an attempt:
the job goes back to the queue until the next UTC midnight, when the budget resets.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from tie.ai.providers import Budget, BudgetExceeded, DeepSeekText, JevDecisions
from tie.bzp.fetch import fetch_day, store_day
from tie.bzp.parser import parse_result_notice
from tie.bzp.store import upsert_notice
from tie.config import Settings
from tie.db import connect
from tie.ezam.documents import download_document, sync_documents
from tie.http import PoliteClient
from tie.identity.bidders import UNMATCHED_SQL, resolve_unmatched_bids
from tie.identity.whitelist import enrich_bidders
from tie.jobs import MAX_ERROR_LEN, claim, complete, enqueue, fail
from tie.openings.store import process_opening_document
from tie.rawstore import RawStore

log = logging.getLogger(__name__)

# Minimal gap in seconds between two requests to a host (Global Constraints).
RATES: dict[str, float] = {
    # Above the 1 request/s default: the e-Zamowienia WAF throttles (HTTP 403) under load.
    "ezamowienia.gov.pl": 2.0,
    "wl-api.mf.gov.pl": 2.0,
    "api.typesafe.ai": 0.1,
    "api.deepseek.com": 0.2,
}
# Extra statuses retried per host: the e-Zamowienia WAF intermittently answers 403 under
# load, and the same request succeeds minutes later.
RETRY_STATUSES_BY_HOST: dict[str, set[int]] = {"ezamowienia.gov.pl": {403}}
# One identity job works at most this long, then queues a continuation so other jobs
# get their turn.
IDENTITY_TIME_LIMIT_S = 600


@dataclass
class Context:
    conn: psycopg.Connection
    settings: Settings | None
    client: PoliteClient
    rawstore: RawStore | None
    text_provider: DeepSeekText | None
    decider: JevDecisions | None
    _owned: list = field(default_factory=list, repr=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> Context:
        conn = connect(settings.database_url)
        client = PoliteClient(settings.user_agent, RATES,
                              retry_statuses_by_host=RETRY_STATUSES_BY_HOST)
        owned: list = [client, conn]
        text_provider = decider = None
        if settings.deepseek_api_key or settings.jev_api_key:
            # Own autocommit connection: Budget commits each `ai_calls` row at once, which
            # on the handler's connection would also commit the handler's unfinished work.
            budget_conn = psycopg.connect(settings.database_url, autocommit=True,
                                          row_factory=dict_row)
            owned.append(budget_conn)
            budget = Budget(budget_conn, settings.ai_daily_budget_usd)
            if settings.deepseek_api_key:
                text_provider = DeepSeekText(client, settings.deepseek_api_key,
                                             settings.deepseek_model, budget)
            if settings.jev_api_key:
                decider = JevDecisions(client, settings.jev_api_key, budget)
        return cls(conn=conn, settings=settings, client=client,
                   rawstore=RawStore(settings.raw_dir), text_provider=text_provider,
                   decider=decider, _owned=owned)

    def close(self) -> None:
        for resource in self._owned:
            try:
                resource.close()
            except Exception:  # closing on shutdown, nothing to recover
                log.warning("close failed for %r", resource, exc_info=True)


def _bzp_day(ctx: Context, payload: dict) -> None:
    day = date.fromisoformat(payload["date"])
    records = fetch_day(ctx.client, day)
    sha = store_day(ctx.rawstore, day, records)
    stored = skipped = 0
    for record in records:
        try:
            notice = parse_result_notice(record)
            upsert_notice(ctx.conn, notice, sha)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            raise  # the database is gone: retry the whole day later
        except Exception:
            ctx.conn.rollback()
            skipped += 1
            log.exception("bzp_day %s: record %s skipped", day, record.get("noticeNumber"))
            continue
        stored += 1
    log.info("bzp_day %s: records=%d stored=%d skipped=%d raw=%s",
             day, len(records), stored, skipped, sha)
    if records and not stored:
        # Every record failing points at a systemic problem (parser, schema), not at data.
        raise RuntimeError(f"bzp_day {day}: all {len(records)} records failed")


def _ezam_docs(ctx: Context, payload: dict) -> None:
    n = sync_documents(ctx.conn, ctx.client, payload["ocds_id"])
    log.info("ezam_docs %s: %d opening documents", payload["ocds_id"], n)


def _ezam_download(ctx: Context, payload: dict) -> None:
    download_document(ctx.conn, ctx.client, ctx.rawstore, payload["ocds_id"], payload["doc_id"])


def _opening_parse(ctx: Context, payload: dict) -> None:
    if ctx.text_provider is None:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    process_opening_document(ctx.conn, ctx.rawstore, ctx.text_provider,
                             int(payload["document_id"]))


def _count(conn: psycopg.Connection, sql: str) -> int:
    n = conn.execute(sql).fetchone()["n"]
    conn.commit()
    return n


def _identity(ctx: Context, payload: dict) -> None:
    if ctx.decider is None:
        raise RuntimeError("JEV_API_KEY not set")
    deadline = time.monotonic() + IDENTITY_TIME_LIMIT_S
    # Bids that failed in this run (logged by resolve_unmatched_bids) are not retried until
    # the next identity job, so one bad bid cannot stall the rest. Bids that failed in 3 runs
    # (identity_failures) are excluded altogether.
    skip: set[int] = set()

    def unmatched() -> int:
        n = ctx.conn.execute(
            f"select count(*) n from bids where {UNMATCHED_SQL}"
            " and not (id = any(%s::bigint[]))", (list(skip),)).fetchone()["n"]
        ctx.conn.commit()
        return n

    left = unmatched()
    while left:
        if time.monotonic() >= deadline:
            key = f"more@{datetime.now(UTC).isoformat()}"
            enqueue(ctx.conn, "identity", key)
            log.info("identity: %d bids left, continuation %s queued", left, key)
            return
        resolve_unmatched_bids(ctx.conn, ctx.decider, skip=skip)
        before, left = left, unmatched()
        if left >= before:
            raise RuntimeError(f"identity: no progress, {left} bids still unmatched")
    if skip:
        log.warning("identity: %d bids skipped after errors, first ids: %s", len(skip),
                    sorted(skip)[:20])


def _whitelist(ctx: Context, payload: dict) -> None:
    pending_sql = ("select count(*) n from bidders"
                   " where nip is not null and wl_checked_at is null")
    left = _count(ctx.conn, pending_sql)
    total = 0
    while left:
        total += enrich_bidders(ctx.conn, ctx.client, daily_cap=ctx.settings.wl_daily_cap,
                                today=datetime.now(UTC).date())
        before, left = left, _count(ctx.conn, pending_sql)
        if left >= before:
            break  # daily request cap reached; the rest waits for the next run
    log.info("whitelist: %d bidders enriched, %d still unchecked", total, left)


HANDLERS: dict[str, Callable[[Context, dict], None]] = {
    "bzp_day": _bzp_day,
    "ezam_docs": _ezam_docs,
    "ezam_download": _ezam_download,
    "opening_parse": _opening_parse,
    "identity": _identity,
    "whitelist": _whitelist,
}


def next_utc_midnight(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)


def _defer_to_midnight(conn: psycopg.Connection, job_id: int, error: str) -> None:
    """Put a running job back in the queue until the budget resets, giving back its attempt."""
    conn.execute(
        "update jobs set status = 'queued', locked_at = null, last_error = %s,"
        " attempts = greatest(attempts - 1, 0), run_after = %s"
        " where id = %s and status = 'running'",
        (error[:MAX_ERROR_LEN], next_utc_midnight(), job_id),
    )
    conn.commit()


def run_once(ctx: Context, kinds: list[str] | None = None) -> bool:
    """Claim and run one job; False when no job of `kinds` (default: all) is ready."""
    jobs = claim(ctx.conn, kinds if kinds is not None else list(HANDLERS))
    if not jobs:
        return False
    job: dict[str, Any] = jobs[0]
    job_id, kind = job["id"], job["kind"]
    started = time.monotonic()
    try:
        handler = HANDLERS.get(kind)
        if handler is None:
            raise LookupError(f"no handler for job kind {kind!r}")
        handler(ctx, job["payload"])
    except BudgetExceeded as exc:
        ctx.conn.rollback()
        log.warning("job %s %s %s deferred to next UTC midnight: %s",
                    job_id, kind, job["key"], exc)
        _defer_to_midnight(ctx.conn, job_id, repr(exc))
    except Exception as exc:
        ctx.conn.rollback()
        log.exception("job %s %s %s failed (attempt %s)", job_id, kind, job["key"],
                      job["attempts"])
        fail(ctx.conn, job_id, repr(exc))
    else:
        complete(ctx.conn, job_id)
        log.info("job %s %s %s done in %.1fs", job_id, kind, job["key"],
                 time.monotonic() - started)
    return True
