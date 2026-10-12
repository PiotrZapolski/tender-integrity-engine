from datetime import UTC, datetime, timedelta

from tie.jobs import claim, complete, enqueue, fail, prioritize_provinces, requeue_stale


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


def _procedure_with_job(db, province, n, kind="ezam_docs"):
    buyer_id = db.execute(
        "insert into buyers(name, province) values (%s, %s) returning id",
        (f"Buyer {province}", province),
    ).fetchone()["id"]
    ocds_id = f"ocds-148610-{n}"
    db.execute(
        "insert into procedures(ocds_id, bzp_number, notice_number, buyer_id, title, published_at)"
        " values (%s, %s, %s, %s, 't', now())",
        (ocds_id, f"bzp-{n}", f"notice-{n}", buyer_id),
    )
    db.commit()
    later = datetime.now(UTC) + timedelta(minutes=30)
    enqueue(db, kind, ocds_id, {"ocds_id": ocds_id}, run_after=later)
    return ocds_id


def _run_after_in_past(db, ocds_id):
    row = db.execute("select run_after < now() as past from jobs where key = %s",
                     (ocds_id,)).fetchone()
    return row["past"]


def test_prioritize_provinces_moves_only_listed_province(db):
    pl14 = _procedure_with_job(db, "PL14", 1)
    pl12 = _procedure_with_job(db, "PL12", 2)
    assert prioritize_provinces(db, ["PL14"]) == 1
    assert _run_after_in_past(db, pl14) is True
    assert _run_after_in_past(db, pl12) is False
    [claimed] = claim(db, ["ezam_docs"])
    assert claimed["key"] == pl14


def test_prioritize_provinces_empty_list_returns_zero(db):
    ocds_id = _procedure_with_job(db, "PL14", 1)
    assert prioritize_provinces(db, []) == 0
    assert _run_after_in_past(db, ocds_id) is False


def test_prioritize_provinces_skips_already_moved_jobs(db):
    _procedure_with_job(db, "PL14", 1)
    assert prioritize_provinces(db, ["PL14"]) == 1
    assert prioritize_provinces(db, ["PL14"]) == 0
