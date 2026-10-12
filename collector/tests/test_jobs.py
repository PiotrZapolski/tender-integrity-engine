from datetime import UTC, datetime, timedelta

from tie.jobs import claim, complete, enqueue, fail, requeue_stale


def test_enqueue_dedup(db):
    assert enqueue(db, "bzp_day", "2026-10-01") is True
    assert enqueue(db, "bzp_day", "2026-10-01") is False


def test_claim_complete(db):
    enqueue(db, "bzp_day", "d1", {"date": "2026-10-01"})
    [job] = claim(db, ["bzp_day"])
    assert job["payload"] == {"date": "2026-10-01"} and job["attempts"] == 1
    assert claim(db, ["bzp_day"]) == []
    complete(db, job["id"])
    row = db.execute("select status from jobs where id=%s", (job["id"],)).fetchone()
    assert row["status"] == "done"


def test_fail_backoff_then_failed(db):
    enqueue(db, "k", "x")
    db.execute("update jobs set max_attempts=2")
    db.commit()
    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    [j] = claim(db, ["k"])
    fail(db, j["id"], "boom", now=now)
    row = db.execute("select status, run_after, last_error from jobs").fetchone()
    assert row["status"] == "queued" and row["last_error"] == "boom"
    assert row["run_after"] == now + timedelta(minutes=2)
    db.execute("update jobs set run_after=now()")
    db.commit()
    [j] = claim(db, ["k"])
    fail(db, j["id"], "boom2", now=now)
    assert db.execute("select status from jobs").fetchone()["status"] == "failed"


def test_future_job_not_claimed(db):
    enqueue(db, "k", "later", run_after=datetime.now(UTC) + timedelta(hours=1))
    assert claim(db, ["k"]) == []


def test_requeue_stale(db):
    enqueue(db, "k", "s")
    claim(db, ["k"])
    db.execute("update jobs set locked_at = now() - interval '2 hours'")
    db.commit()
    assert requeue_stale(db, timedelta(hours=1)) == 1
    assert claim(db, ["k"]) != []


def test_requeue_stale_out_of_attempts_fails(db):
    enqueue(db, "k", "dead")
    db.execute("update jobs set max_attempts=1")
    db.commit()
    claim(db, ["k"])
    db.execute("update jobs set locked_at = now() - interval '2 hours'")
    db.commit()
    assert requeue_stale(db, timedelta(hours=1)) == 1
    row = db.execute("select status, last_error from jobs").fetchone()
    assert row["status"] == "failed" and row["last_error"] == "stale: worker lost"
    assert claim(db, ["k"]) == []


def test_late_complete_after_requeue_is_noop(db):
    enqueue(db, "k", "late")
    [j] = claim(db, ["k"])
    db.execute("update jobs set locked_at = now() - interval '2 hours'")
    db.commit()
    assert requeue_stale(db, timedelta(hours=1)) == 1
    complete(db, j["id"])
    assert db.execute("select status from jobs").fetchone()["status"] == "queued"
    fail(db, j["id"], "late boom")
    row = db.execute("select status, last_error from jobs").fetchone()
    assert row["status"] == "queued" and row["last_error"] is None
