"""Job queue in Postgres (table `jobs`).

Every function commits its own transaction. `claim` uses FOR UPDATE SKIP LOCKED so
several workers can pull from the queue concurrently without taking the same job.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

MAX_ERROR_LEN = 2000
STALE_ERROR = "stale: worker lost"


def enqueue(
    conn: psycopg.Connection,
    kind: str,
    key: str,
    payload: dict | None = None,
    run_after: datetime | None = None,
) -> bool:
    """Insert a job; return False when (kind, key) already exists."""
    row = conn.execute(
        "insert into jobs(kind, key, payload, run_after)"
        " values (%s, %s, %s, coalesce(%s::timestamptz, now()))"
        " on conflict (kind, key) do nothing returning id",
        (kind, key, Jsonb(payload or {}), run_after),
    ).fetchone()
    conn.commit()
    return row is not None


def claim(conn: psycopg.Connection, kinds: Iterable[str], limit: int = 1) -> list[dict[str, Any]]:
    """Take up to `limit` ready jobs of the given kinds and mark them running."""
    rows = conn.execute(
        """
        update jobs set status = 'running', locked_at = now(), attempts = attempts + 1
        where id in (
          select id from jobs
          where status = 'queued' and run_after <= now() and kind = any(%s::text[])
          order by run_after, id
          limit %s
          for update skip locked)
        returning *
        """,
        (list(kinds), limit),
    ).fetchall()
    conn.commit()
    return sorted(rows, key=lambda r: (r["run_after"], r["id"]))


def complete(conn: psycopg.Connection, job_id: int) -> None:
    """Mark a running job done. No-op if the job is not running (e.g. requeued as stale)."""
    conn.execute(
        "update jobs set status = 'done', locked_at = null where id = %s and status = 'running'",
        (job_id,),
    )
    conn.commit()


def fail(
    conn: psycopg.Connection, job_id: int, error: str, *, now: datetime | None = None
) -> None:
    """Record a failure: retry with exponential backoff, or mark failed when out of attempts.

    No-op if the job is not running; LookupError if the job does not exist.
    """
    now = now or datetime.now(UTC)
    job = conn.execute(
        "select attempts, max_attempts, status from jobs where id = %s for update", (job_id,)
    ).fetchone()
    if job is None:
        conn.rollback()
        raise LookupError(f"job {job_id} not found")
    if job["status"] != "running":
        conn.commit()
        return
    error = error[:MAX_ERROR_LEN]
    if job["attempts"] >= job["max_attempts"]:
        conn.execute(
            "update jobs set status = 'failed', locked_at = null, last_error = %s"
            " where id = %s and status = 'running'",
            (error, job_id),
        )
    else:
        run_after = now + timedelta(minutes=2 ** job["attempts"])
        conn.execute(
            "update jobs set status = 'queued', locked_at = null, last_error = %s,"
            " run_after = %s where id = %s and status = 'running'",
            (error, run_after, job_id),
        )
    conn.commit()


def requeue_stale(conn: psycopg.Connection, older_than: timedelta) -> int:
    """Handle running jobs whose lock is older than `older_than` (worker lost).

    Jobs with attempts left go back to 'queued'; jobs out of attempts become 'failed'.
    Returns the number of rows touched.
    """
    cur = conn.execute(
        """
        update jobs set
          status = case when attempts >= max_attempts then 'failed' else 'queued' end,
          last_error = case when attempts >= max_attempts then %s else last_error end,
          locked_at = null
        where status = 'running' and locked_at < now() - %s::interval
        """,
        (STALE_ERROR, older_than),
    )
    conn.commit()
    return cur.rowcount


def prioritize_provinces(conn: psycopg.Connection, provinces: Sequence[str]) -> int:
    """Move queued document jobs of buyers from `provinces` ahead of the rest of the queue.

    Sets `run_after` a day into the past, so the workers (which claim by `run_after`) take
    these jobs first. Jobs already moved (`run_after` older than an hour) are not touched
    again, so a repeated call only counts new jobs. Returns the number of jobs moved."""
    if not provinces:
        return 0
    cur = conn.execute(
        """
        update jobs j set run_after = now() - interval '1 day'
        from procedures p join buyers b on b.id = p.buyer_id
        where j.status = 'queued'
          and j.kind in ('ezam_docs','ezam_download','ezamawiajacy_docs','ezamawiajacy_download')
          and j.run_after > now() - interval '1 hour'
          and p.ocds_id = j.payload->>'ocds_id'
          and b.province = any(%s)
        """,
        (list(provinces),),
    )
    conn.commit()
    return cur.rowcount
