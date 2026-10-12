"""Job queue in Postgres (table `jobs`).

Every function commits its own transaction. `claim` uses FOR UPDATE SKIP LOCKED so
several workers can pull from the queue concurrently without taking the same job.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

MAX_ERROR_LEN = 2000


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
    conn.execute("update jobs set status = 'done', locked_at = null where id = %s", (job_id,))
    conn.commit()


def fail(
    conn: psycopg.Connection, job_id: int, error: str, *, now: datetime | None = None
) -> None:
    """Record a failure: retry with exponential backoff, or mark failed when out of attempts."""
    now = now or datetime.now(UTC)
    job = conn.execute(
        "select attempts, max_attempts from jobs where id = %s", (job_id,)
    ).fetchone()
    if job is None:
        conn.rollback()
        raise LookupError(f"job {job_id} not found")
    error = error[:MAX_ERROR_LEN]
    if job["attempts"] >= job["max_attempts"]:
        conn.execute(
            "update jobs set status = 'failed', locked_at = null, last_error = %s where id = %s",
            (error, job_id),
        )
    else:
        run_after = now + timedelta(minutes=2 ** job["attempts"])
        conn.execute(
            "update jobs set status = 'queued', locked_at = null, last_error = %s,"
            " run_after = %s where id = %s",
            (error, run_after, job_id),
        )
    conn.commit()


def requeue_stale(conn: psycopg.Connection, older_than: timedelta) -> int:
    """Put running jobs whose lock is older than `older_than` back in the queue."""
    cur = conn.execute(
        "update jobs set status = 'queued', locked_at = null"
        " where status = 'running' and locked_at < now() - %s::interval",
        (older_than,),
    )
    conn.commit()
    return cur.rowcount
