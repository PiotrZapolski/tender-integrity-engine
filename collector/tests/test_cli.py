import json
import logging
import os
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx

from tie import cli
from tie.ai.providers import BudgetExceeded
from tie.bzp.fetch import BZP_URL
from tie.handlers import HANDLERS, Context, next_utc_midnight, run_once
from tie.http import PoliteClient
from tie.jobs import enqueue
from tie.rawstore import RawStore


@pytest.fixture
def dburl(db, monkeypatch):
    # db.info.dsn has no password; CI exposes the full URL in TEST_DATABASE_URL.
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    return os.environ["TEST_DATABASE_URL"]


def ctx_for(db, tmp_path=None):
    return Context(conn=db, settings=None, client=PoliteClient("ua", {}, sleep=lambda s: None),
                   rawstore=RawStore(tmp_path) if tmp_path else None, text_provider=None,
                   decider=None)


def job(db, kind):
    return db.execute("select * from jobs where kind = %s", (kind,)).fetchone()


def test_handlers_registered():
    assert {"bzp_day", "ezam_docs", "ezam_download", "opening_parse", "identity",
            "whitelist"} <= set(HANDLERS)


def test_backfill_enqueues_days(db, dburl):
    assert cli.main(["backfill", "--since", "2026-10-01", "--until", "2026-10-03"]) == 0
    rows = db.execute(
        "select key, payload from jobs where kind='bzp_day' order by run_after").fetchall()
    assert [r["key"] for r in rows] == ["2026-10-03", "2026-10-02", "2026-10-01"]
    assert rows[0]["payload"] == {"date": "2026-10-03"}
    # Running it again does not duplicate jobs.
    assert cli.main(["backfill", "--since", "2026-10-01", "--until", "2026-10-03"]) == 0
    assert db.execute("select count(*) c from jobs").fetchone()["c"] == 3


def test_backfill_rejects_reversed_range(db, dburl):
    assert cli.main(["backfill", "--since", "2026-10-03", "--until", "2026-10-01"]) == 2


def test_daily_enqueues(db, dburl, monkeypatch):
    monkeypatch.setattr(cli, "today", lambda: date(2026, 10, 12))
    assert cli.main(["daily"]) == 0
    kinds = {r["kind"] for r in db.execute("select kind from jobs").fetchall()}
    assert kinds == {"bzp_day", "identity", "whitelist"}
    rows = db.execute("select key, payload from jobs where kind='bzp_day'").fetchall()
    payloads = [r["payload"] for r in rows]
    assert {"date": "2026-10-11"} in payloads and {"date": "2026-10-10"} in payloads
    assert {r["key"] for r in rows} == {"2026-10-11@2026-10-12", "2026-10-10@2026-10-12"}
    assert job(db, "identity")["key"] == "2026-10-12"
    assert job(db, "whitelist")["key"] == "2026-10-12"


def test_stats(db, dburl, capsys):
    enqueue(db, "bzp_day", "2026-10-01", {"date": "2026-10-01"})
    assert cli.main(["stats"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["procedures"] == 0 and "jobs" in out
    assert out["lots"] == 0 and out["bids"] == 0 and out["documents"] == {}
    assert out["jobs"] == {"bzp_day": {"queued": 1}}
    assert out["ai_cost_today_usd"] == 0
    assert out["bids_identity_excluded"] == 0
    assert out["partial"] == 0


def test_stats_counts_partially_parsed_documents(db, dburl, capsys):
    db.execute("insert into procedures(ocds_id, bzp_number, notice_number, title, published_at)"
               " values ('o','b','n','t', now())")
    for i, (status, error) in enumerate([
            ("parsed", None), ("parsed", "partial: lot3:prices"),
            ("parsed", "partial: lot2:count; notes: bzp_partial"), ("parsed", "notes: bzp_partial"),
            ("mismatch", "lot1:count")]):
        db.execute("insert into documents(procedure_id, source, source_doc_id, file_name,"
                   " doc_type, status, error) values ('o', 'ezamowienia', %s, 'f.pdf',"
                   " 'opening', %s, %s)", (f"d{i}", status, error))
    db.commit()
    assert cli.main(["stats"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["documents"] == {"mismatch": 1, "parsed": 4}
    assert out["partial"] == 2


def test_stats_counts_identity_excluded_bids(db, dburl, capsys):
    db.execute("insert into procedures(ocds_id, bzp_number, notice_number, title, published_at)"
               " values ('o','b','n','t', now())")
    lot = db.execute("insert into lots(procedure_id, lot_no, outcome) values ('o',1,'umowa')"
                     " returning id").fetchone()["id"]
    for offer_no, fails in enumerate([0, 2, 3, 5], start=1):
        db.execute("insert into bids(lot_id, offer_no, bidder_name_raw, parse_method,"
                   " identity_failures) values (%s, %s, 'x', 'llm', %s)",
                   (lot, offer_no, fails))
    db.commit()
    assert cli.main(["stats"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["bids"] == 4 and out["bids_identity_excluded"] == 2


def test_migrate(db, dburl):
    assert cli.main(["migrate"]) == 0


def test_next_utc_midnight():
    now = datetime(2026, 10, 12, 23, 59, tzinfo=UTC)
    assert next_utc_midnight(now) == datetime(2026, 10, 13, tzinfo=UTC)


def test_run_once_complete_fail_and_empty(db, monkeypatch):
    monkeypatch.setitem(HANDLERS, "t_ok", lambda ctx, payload: None)

    def boom(ctx, payload):
        raise ValueError("x")

    monkeypatch.setitem(HANDLERS, "t_boom", boom)
    ctx = ctx_for(db)
    assert run_once(ctx, ["t_ok"]) is False
    enqueue(db, "t_ok", "a")
    enqueue(db, "t_boom", "b")
    assert run_once(ctx, ["t_ok"]) is True
    assert job(db, "t_ok")["status"] == "done"
    assert run_once(ctx, ["t_boom"]) is True
    j = job(db, "t_boom")
    assert j["status"] == "queued" and j["attempts"] == 1
    assert j["last_error"] == "ValueError('x')"


def test_run_once_budget_exceeded_requeues_to_midnight(db, monkeypatch):
    def broke(ctx, payload):
        raise BudgetExceeded("limit")

    monkeypatch.setitem(HANDLERS, "t_ai", broke)
    enqueue(db, "t_ai", "a")
    before = datetime.now(UTC)
    assert run_once(ctx_for(db), ["t_ai"]) is True
    j = job(db, "t_ai")
    assert j["status"] == "queued" and j["attempts"] == 0 and j["locked_at"] is None
    assert j["run_after"] in {next_utc_midnight(before), next_utc_midnight(datetime.now(UTC))}
    assert "BudgetExceeded" in j["last_error"]


def test_opening_parse_needs_deepseek(db):
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY not set"):
        HANDLERS["opening_parse"](ctx_for(db), {"document_id": 1})


def test_identity_needs_jev(db):
    with pytest.raises(RuntimeError, match="JEV_API_KEY not set"):
        HANDLERS["identity"](ctx_for(db), {})


@respx.mock
def test_bzp_day_stores_notices_and_skips_bad_records(db, fixtures, tmp_path, caplog):
    records = [json.loads((fixtures / "bzp" / n).read_text("utf-8"))
               for n in ("result_multilot.json", "result_spytkowo.json")]
    records.append({"noticeNumber": "2026/BZP 99999999/01"})  # no bzpNumber: parse error
    route = respx.get(BZP_URL).mock(return_value=httpx.Response(200, json=records))
    with caplog.at_level(logging.INFO, logger="tie.handlers"):
        HANDLERS["bzp_day"](ctx_for(db, tmp_path), {"date": "2026-10-01"})
    assert route.call_count == 1
    assert route.calls.last.request.url.params["PublicationDateFrom"] == "2026-10-01"
    assert db.execute("select count(*) c from procedures").fetchone()["c"] == 2
    assert job(db, "ezam_docs") is not None
    assert "skipped=1" in caplog.text
    assert any((tmp_path / "bzp").rglob("*.zst"))


@respx.mock
def test_bzp_day_all_records_bad_raises(db, tmp_path):
    respx.get(BZP_URL).mock(
        return_value=httpx.Response(200, json=[{"noticeNumber": "2026/BZP 1/01"}]))
    with pytest.raises(RuntimeError, match="all 1 records failed"):
        HANDLERS["bzp_day"](ctx_for(db, tmp_path), {"date": "2026-10-01"})


def test_worker_stops_after_max_jobs(db, dburl, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setitem(HANDLERS, "t_ok", lambda ctx, payload: seen.append(payload))
    monkeypatch.setenv("RAW_DIR", str(tmp_path))
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    enqueue(db, "t_ok", "a", {"n": 1})
    enqueue(db, "t_ok", "b", {"n": 2}, run_after=datetime.now(UTC) + timedelta(seconds=-1))
    assert cli.main(["worker", "--kinds", "t_ok", "--max-jobs", "2"]) == 0
    assert sorted(p["n"] for p in seen) == [1, 2]
    statuses = {r["status"] for r in db.execute("select status from jobs").fetchall()}
    assert statuses == {"done"}
