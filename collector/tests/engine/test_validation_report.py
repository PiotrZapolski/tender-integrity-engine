"""Validation tests B and C, calibration, `tie validate` and the view `api.validation`."""

import json
import os
from datetime import date

import pytest
from psycopg.types.json import Jsonb

from tie import cli
from tie.engine.run import run_engine
from tie.handlers import Context
from tie.http import PoliteClient
from tie.validation.report import (
    DEFAULT_PUBLISH,
    DEFAULT_REVIEW,
    markdown_summary,
    propose_thresholds,
    run_validation,
)

from .synth import make_market

ROTATION = {"members": 3, "pattern": "rotation", "share": 0.4}
OUTSIDE_NIP = "1234567883"
PERSON_NAMES = ("Kowalski", "Nowak", "Jan Testowy")


def test_thresholds_tighten_on_noisy_controls():
    control = [0.1] * 80 + [0.8] * 15 + [0.97] * 5
    out = propose_thresholds(control, [0.99, 0.85, 0.5])
    assert out["basis"] == "control_clusters"
    assert out["review_threshold"] == 0.81
    assert out["publish_threshold"] == 0.98
    assert out["control_share_review"] == 0.05 and out["control_share_publish"] == 0.0
    assert out["recall_review"] == pytest.approx(0.6667)
    assert out["recall_publish"] == pytest.approx(0.3333)


def test_thresholds_never_drop_below_engine_defaults():
    out = propose_thresholds([0.05] * 100, [])
    assert out["review_threshold"] == DEFAULT_REVIEW
    assert out["publish_threshold"] == DEFAULT_PUBLISH
    assert out["recall_review"] is None


def test_thresholds_keep_defaults_with_few_controls():
    out = propose_thresholds([0.99] * 10, [0.99])
    assert out["basis"] == "defaults"
    assert (out["review_threshold"], out["publish_threshold"]) == (DEFAULT_REVIEW,
                                                                   DEFAULT_PUBLISH)


def _ctx(db):
    return Context(conn=db, settings=None, client=PoliteClient("ua", {}, sleep=lambda s: None),
                   rawstore=None, text_provider=None, decider=None)


def _firms(db, ids):
    return [db.execute("select name, nip from bidders where id = %s", (i,)).fetchone()
            for i in ids]


def _insert_kio(db, signature, day, ocds_id, decision, colluders):
    db.execute(
        "insert into gold_kio(signature, ruling_date, ocds_id, decision, colluders)"
        " values (%s, %s, %s, %s, %s)",
        (signature, day, ocds_id, decision, Jsonb(colluders)))


def test_validation_end_to_end(db):
    out = make_market(db, seed=1, n_firms=12, n_lots=80, cpv="45233142-6",
                      province_code="PL22", cartel=ROTATION)
    run = run_engine(_ctx(db), out["as_of"])
    assert run["status"] == "done"

    cartel = _firms(db, out["cartel"])
    lot_index = out["lots"].index(out["cartel_lots"][0])
    tenders = [{
        "buyer": "Gmina Testowa", "subject": f"Droga {k}", "rigged": True,
        "bids": [{"bidder": f["name"], "price": 100000 + 1000 * j + 5000 * k, "won": j == k}
                 for j, f in enumerate(cartel)],
    } for k in range(3)]
    payload = {
        "decision": "RGD-99/2027", "year": 2027, "tenders": tenders, "pattern": ["rotation"],
        "colluders": [{"name": f["name"], "nip": f["nip"]} for f in cartel] + [
            {"name": "Usługi Transportowe Jan Kowalski", "nip": OUTSIDE_NIP},
            {"name": "Przewóz Osób Anna Nowak", "nip": None}],
        "notes": "", "source": {"sector": "drogi", "region": "pomorskie"},
    }
    db.execute("insert into gold_cases(decision, payload) values (%s, %s)",
               ("RGD-99/2027", Jsonb(payload)))
    _insert_kio(db, "KIO 1/26", date(2026, 6, 1), out["procedures"][lot_index], "upheld",
                [{"name": f["name"], "nip": None} for f in cartel[:2]])
    _insert_kio(db, "KIO 2/26", date(2026, 6, 2), None, "upheld",
                [{"name": "Nieznana Firma sp. z o.o.", "nip": None},
                 {"name": "Usługi Remontowe Jan Testowy", "nip": None}])
    outsiders = _firms(db, out["outsiders"][:2])
    _insert_kio(db, "KIO 3/26", date(2026, 6, 3), None, "overturned",
                [{"name": f["name"], "nip": f["nip"]} for f in outsiders])
    db.commit()

    report = run_validation(db, as_of=out["as_of"], sample=50, seed=1)
    assert isinstance(report["calibration_run_id"], int)
    rows = {r["key"]: r for r in report["cases"]}
    assert set(rows) == {"RGD-99/2027", "KIO 1/26", "KIO 2/26", "KIO 3/26"}

    uokik = rows["RGD-99/2027"]
    assert uokik["engine"]["in_cluster"] is True and uokik["engine"]["lead"] is True
    assert uokik["engine"]["reason"] == "ok" and uokik["probability"] is not None
    assert uokik["engine"]["matched"] == 3
    assert uokik["sector"] == "drogi" and uokik["year"] == 2027
    assert f"NIP {OUTSIDE_NIP}" in uokik["firms"]
    assert sorted(f for f in uokik["firms"] if not f.startswith("NIP ")) == sorted(
        f["name"] for f in cartel)

    kio = rows["KIO 1/26"]
    assert kio["detected"] is True and kio["status"] == "detected"
    assert kio["engine"]["linked_procedure"] is True and kio["engine"]["lead"] is True
    assert rows["KIO 2/26"]["detected"] is None
    assert rows["KIO 2/26"]["reason"] == "colluders_not_in_our_data"
    assert rows["KIO 3/26"]["decision"] == "overturned"

    metrics, params = report["metrics"], report["params"]
    b = metrics["test_b"]
    assert b["engine_runs"] == 1
    assert b["positives"]["evaluable"] == 2 and b["positives"]["in_cluster"] == 2
    assert b["kio_overturned"]["n"] == 1
    c = metrics["test_c"]
    assert c["run_id"] == run["run_id"]
    assert c["sampled"] <= c["candidates"]
    cartel_cluster = db.execute(
        "select cluster_id from clusters where run_id = %s and members @> %s::bigint[]",
        (run["run_id"], out["cartel"])).fetchone()
    assert cartel_cluster is not None
    assert params["basis"] == "defaults"
    assert params["review_threshold"] == DEFAULT_REVIEW
    assert params["publish_threshold"] == DEFAULT_PUBLISH
    assert params["engine_max_status"] == "review"
    gate = metrics["gate"]
    assert gate["test_a"] == "insufficient_data" and gate["ready_for_human_review"] is False

    stored = db.execute("select params, metrics, cases from calibration_runs").fetchall()
    assert len(stored) == 1 and stored[0]["params"]["engine_max_status"] == "review"

    view = db.execute("select * from api.validation order by position").fetchall()
    assert [v["kind"] for v in view] == ["summary", "case", "case", "case", "case"]
    assert view[0]["metrics"]["gate"]["engine_max_status"] == "review"
    assert view[0]["params"]["publish_threshold"] == DEFAULT_PUBLISH
    by_key = {v["case_key"]: v for v in view[1:]}
    assert by_key["KIO 1/26"]["detected"] is True and by_key["KIO 1/26"]["source"] == "kio"
    assert by_key["RGD-99/2027"]["year"] == 2027
    assert by_key["RGD-99/2027"]["detail"]["engine"]["in_cluster"] is True
    text = json.dumps([dict(v) for v in view], default=str, ensure_ascii=False)
    for name in PERSON_NAMES:
        assert name not in text
    assert f"NIP {OUTSIDE_NIP}" in text

    db.execute("set role tie_reader")
    try:
        assert db.execute("select count(*) n from api.validation").fetchone()["n"] == 5
    finally:
        db.rollback()
        db.execute("reset role")
        db.commit()

    summary = markdown_summary(report)
    assert "Walidacja silnika TIE" in summary and "KIO 1/26" in summary
    for name in PERSON_NAMES:
        assert name not in summary


@pytest.fixture
def dburl(db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    return os.environ["TEST_DATABASE_URL"]


def test_cli_validate_writes_report(db, dburl, tmp_path, capsys):
    path = tmp_path / "out" / "validation.json"
    assert cli.main(["validate", "--write-report", str(path), "--as-of", "2026-01-01",
                     "--sample", "10"]) == 0
    report = json.loads(path.read_text(encoding="utf-8"))
    assert set(report) >= {"params", "metrics", "cases", "calibration_run_id", "run_at"}
    assert report["cases"] == []
    assert report["metrics"]["test_c"]["reason"] == "no_engine_run"
    assert report["metrics"]["gate"]["engine_max_status"] == "review"
    assert path.with_suffix(".md").read_text(encoding="utf-8").startswith(
        "# Walidacja silnika TIE")
    assert "Walidacja silnika TIE" in capsys.readouterr().out
    assert db.execute("select count(*) n from calibration_runs").fetchone()["n"] == 1
    view = db.execute("select kind from api.validation").fetchall()
    assert [v["kind"] for v in view] == ["summary"]
