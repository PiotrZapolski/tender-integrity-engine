import json
import os
import re
from decimal import Decimal as D
from importlib import resources
from itertools import pairwise

import httpx
import pytest
import respx
from pydantic import ValidationError

from tie import cli
from tie.ai.providers import BudgetExceeded
from tie.gold.extract import (
    RAW_SOURCE,
    SYSTEM_PROMPT,
    TASK,
    GoldCase,
    GoldExtractError,
    GoldExtraction,
    chunk_text,
    compact_text,
    download_decision,
    export_gold,
    extract_case,
    load_cases,
    run_gold_extract,
    store_case,
)
from tie.gold.sources import UOKIK_DECISIONS, Decision, get_decision
from tie.http import PoliteClient
from tie.rawstore import RawStore

RLU = get_decision("RLU-7/2023")
SEED = ("RGD-10/2023", "RGD-11/2023", "RGD-12/2023", "RLU-7/2023")
DASHES = re.compile("[" + chr(0x2013) + chr(0x2014) + "]")  # en and em dash


class FakeProvider:
    """Returns the payloads in order (the last one repeats); records every call."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def complete_json(self, *, task, system, user):
        self.calls.append({"task": task, "system": system, "user": user})
        i = min(len(self.calls), len(self.payloads)) - 1
        payload = self.payloads[i]
        if isinstance(payload, Exception):
            raise payload
        return payload


def gorzkow_payload(**overrides):
    payload = {
        "decision": "RLU-7/2023",
        "year": 2023,
        "colluders": [
            {"name": "Usługi Transportowe Skiba Marcin", "nip": None},
            {"name": "TACHO-SYSTEM Marek Pilip", "nip": None},
        ],
        "tenders": [{
            "buyer": "Gmina Gorzków",
            "subject": "Dowóz uczniów do szkoły z terenu gminy Gorzków w roku szkolnym 2019/2020",
            "date": "2019-07-26",
            "year": 2019,
            "bids": [
                {"bidder": "TACHO-SYSTEM Marek Pilip", "price": 88944.0, "won": False},
                {"bidder": "Usługi Transportowe Skiba Marcin", "price": "98.100,00 zł",
                 "won": True},
                {"bidder": "PUH Mateusz Lal", "price": 103550, "won": False},
                {"bidder": "Usługi Transportowe Lal Mieczysław", "price": "114450.00",
                 "won": False},
                {"bidder": "Przewóz Osób Marta Brzozowska", "price": 119900.0, "won": False},
            ],
            "rigged": True,
        }],
        "pattern": ["bid_suppression", "cover_bids"],
        "notes": "Najtańsza oferta wycofana, umowę zawarto z droższym uczestnikiem zmowy.",
    }
    payload.update(overrides)
    return payload


def excerpt(fixtures) -> str:
    return (fixtures / "gold" / "rlu_7_2023_excerpt.txt").read_text("utf-8")


def pdf_bytes(fixtures) -> bytes:
    return (fixtures / "openings" / "spytkowo_unit_prices.pdf").read_bytes()


def quiet_client():
    return PoliteClient("ua", {}, sleep=lambda s: None, max_retries=0)


# ---------------------------------------------------------------- sources


def test_sources_list_is_curated():
    ids = [d.id for d in UOKIK_DECISIONS]
    assert 15 <= len(ids) <= 40
    assert len(set(ids)) == len(ids)
    assert set(SEED) <= set(ids)
    for d in UOKIK_DECISIONS:
        assert isinstance(d, Decision)
        assert re.fullmatch(r"(R[A-ZŁ]{2}|DOK)-\d{1,2}/\d{4}", d.id), d.id
        assert d.year == int(d.id.rsplit("/", 1)[1])
        assert 2015 <= d.year <= 2026
        assert d.url.startswith("https://decyzje.uokik.gov.pl/") and d.url.endswith(".pdf")
        assert " " not in d.url
        assert d.sector and d.region and d.note
        assert d.verified is True
        assert not DASHES.search(d.note + d.sector + d.region)


def test_get_decision():
    assert get_decision("RLU-7/2023").region == "lubelskie"
    assert get_decision(" RGD-10/2023 ").year == 2023
    with pytest.raises(KeyError):
        get_decision("RXX-1/2020")


# ---------------------------------------------------------------- text and models


def test_compact_text_keeps_columns_and_amounts(fixtures):
    text = compact_text(excerpt(fixtures))
    assert "98.100,00" in text and "88.944,00" in text
    assert "   " not in text
    assert "Skiba  98.100,00" in text


def test_chunk_text_short_and_empty():
    assert chunk_text("abc\ndef") == ["abc\ndef"]
    assert chunk_text("") == []
    assert chunk_text("  \n ") == []
    with pytest.raises(ValueError):
        chunk_text("x", max_chars=10, overlap=10)


def test_chunk_text_long_text_overlaps_at_line_boundaries():
    lines = [f"linia {i:04d} " + "x" * 40 for i in range(400)]
    text = "\n".join(lines)
    chunks = chunk_text(text, max_chars=2000, overlap=300)
    assert len(chunks) > 5
    assert all(len(c) <= 2000 for c in chunks)
    joined = "".join(chunks)
    assert all(line in joined for line in lines)
    for a, b in pairwise(chunks):
        # the next chunk starts with a full line that also ends the previous chunk
        first = b.split("\n", 1)[0]
        assert first in lines and first in a
    assert chunks[0].startswith("linia 0000") and chunks[-1].endswith(lines[-1])


def test_chunk_text_hard_splits_a_very_long_line():
    chunks = chunk_text("y" * 5000, max_chars=1000, overlap=100)
    assert all(len(c) <= 1000 for c in chunks)
    assert chunks[0] + "".join(c[100:] for c in chunks[1:]) == "y" * 5000


def test_extraction_model_cleans_values():
    out = GoldExtraction.model_validate({
        "decision": "RLU-7/2023", "year": "2023",
        "colluders": [{"name": "  Alfa  Sp. z o.o. ", "nip": "PL 593-233-53-16"},
                      {"name": "Beta", "nip": "1234567890"}],
        "tenders": [{"buyer": "Gmina X", "subject": "Remont", "date": "", "year": "brak",
                     "bids": [{"bidder": "Alfa", "price": "98.100,00 zł", "won": "true"},
                              {"bidder": "Beta", "price": None}]}],
        "pattern": ["rotation", "rotacja", "rotation", "COVER_BIDS"],
        "notes": None,
    })
    a, b = out.colluders
    assert a.name == "Alfa Sp. z o.o." and a.nip == "5932335316"
    assert b.nip is None
    t = out.tenders[0]
    assert t.date is None and t.year is None and t.rigged is False
    assert t.bids[0].price == D("98100.00") and t.bids[0].won is True
    assert t.bids[1].price is None and t.bids[1].won is False
    assert out.pattern == ["rotation", "other", "cover_bids"]
    assert out.notes == ""


def test_extraction_model_rejects_bad_structure():
    with pytest.raises(ValidationError):
        GoldExtraction.model_validate({"tenders": "brak"})
    with pytest.raises(ValidationError):
        GoldExtraction.model_validate({"tenders": [{"buyer": "X", "subject": "Y",
                                                    "bids": [{"bidder": " ", "price": 1}]}]})


def test_case_json_has_numeric_prices():
    case = GoldCase.model_validate({**gorzkow_payload(), "source": {"url": "u"}})
    dumped = case.model_dump(mode="json")
    prices = [b["price"] for b in dumped["tenders"][0]["bids"]]
    assert prices == [88944.0, 98100.0, 103550.0, 114450.0, 119900.0]
    assert dumped["source"] == {"url": "u"}


# ---------------------------------------------------------------- extraction


def test_extract_case_single_chunk(fixtures):
    fake = FakeProvider(gorzkow_payload(decision="RLU 7/2023", year=2019))
    case = extract_case(fake, RLU, excerpt(fixtures))
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["task"] == TASK == "gold_extract"
    assert call["system"] == SYSTEM_PROMPT
    assert "RLU-7/2023" in call["user"] and "98.100,00" in call["user"]
    # the decision number and year come from the curated source, not from the model
    assert case.decision == "RLU-7/2023" and case.year == 2023
    t = case.tenders[0]
    assert t.rigged is True and t.year == 2019 and t.date == "2019-07-26"
    assert [b.price for b in t.bids] == [D("88944.0"), D("98100.00"), D(103550),
                                        D("114450.00"), D("119900.0")]
    assert [b.won for b in t.bids] == [False, True, False, False, False]
    assert case.pattern == ["bid_suppression", "cover_bids"]
    assert case.source["chunks"] == 1 and case.source["failed_chunks"] == []


def test_extract_case_merges_chunks():
    text = "\n".join(f"wiersz {i} " + "z" * 60 for i in range(300))
    first = gorzkow_payload(tenders=[{
        "buyer": "Gmina Gorzków", "subject": "Dowóz uczniów 2019/2020", "date": None,
        "year": None, "rigged": False,
        "bids": [{"bidder": "TACHO-SYSTEM Marek Pilip", "price": None, "won": False},
                 {"bidder": "Usługi Transportowe Skiba Marcin", "price": 98100, "won": False}]}],
        pattern=["bid_suppression"], notes="Pierwszy fragment.")
    second = {
        "decision": None, "year": None,
        "colluders": [{"name": "TACHO SYSTEM Marek Pilip", "nip": "593-233-53-16"},
                      {"name": "Spółdzielnia Socjalna SZT", "nip": None}],
        "tenders": [
            {"buyer": "GMINA GORZKÓW", "subject": "Dowóz  uczniów 2019/2020",
             "date": "2019-07-26", "year": 2019, "rigged": True,
             "bids": [{"bidder": "Tacho-System Marek Pilip", "price": 88944, "won": False},
                      {"bidder": "Usługi Transportowe Skiba Marcin", "price": 98100,
                       "won": True},
                      {"bidder": "PUH Mateusz Lal", "price": 103550, "won": False}]},
            {"buyer": "Gmina Potok Górny", "subject": "Dowóz dzieci 2019/2020",
             "date": None, "year": 2019, "rigged": True,
             "bids": [{"bidder": "TACHO-SYSTEM Marek Pilip", "price": 82.9, "won": True}]}],
        "pattern": ["cover_bids", "bid_suppression"],
        "notes": "Drugi fragment.",
    }
    empty = {"colluders": [], "tenders": [], "pattern": [], "notes": "Pierwszy fragment."}
    fake = FakeProvider(first, second, empty)
    case = extract_case(fake, RLU, text, max_chars=8000, overlap=500)
    assert len(fake.calls) == 3
    assert "fragment 1/3" in fake.calls[0]["user"] and "fragment 3/3" in fake.calls[2]["user"]
    assert case.source["chunks"] == 3
    assert [t.buyer for t in case.tenders] == ["Gmina Gorzków", "Gmina Potok Górny"]
    gorzkow = case.tenders[0]
    assert gorzkow.rigged is True and gorzkow.date == "2019-07-26" and gorzkow.year == 2019
    by_bidder = {b.bidder: b for b in gorzkow.bids}
    assert len(gorzkow.bids) == 3
    assert by_bidder["TACHO-SYSTEM Marek Pilip"].price == D(88944)
    assert by_bidder["Usługi Transportowe Skiba Marcin"].won is True
    names = [c.name for c in case.colluders]
    assert names == ["Usługi Transportowe Skiba Marcin", "TACHO-SYSTEM Marek Pilip",
                     "Spółdzielnia Socjalna SZT"]
    assert case.colluders[1].nip == "5932335316"
    assert case.pattern == ["bid_suppression", "cover_bids"]
    assert case.notes == "Pierwszy fragment. Drugi fragment."


def test_extract_case_keeps_two_prices_of_one_bidder():
    tender = {"buyer": "Gmina", "subject": "Dowóz", "rigged": True,
              "bids": [{"bidder": "Alfa", "price": 10, "won": True}]}
    other = {**tender, "bids": [{"bidder": "Alfa", "price": 12, "won": False}]}
    fake = FakeProvider({"tenders": [tender]}, {"tenders": [other]})
    case = extract_case(fake, RLU, "a" * 300 + "\n" + "b" * 300, max_chars=400, overlap=50)
    assert [b.price for b in case.tenders[0].bids] == [D(10), D(12)]


def test_extract_case_skips_invalid_chunk():
    fake = FakeProvider(gorzkow_payload(), {"tenders": "brak"})
    case = extract_case(fake, RLU, "a" * 300 + "\n" + "b" * 300, max_chars=400, overlap=50)
    assert len(case.tenders) == 1
    assert case.source["chunks"] == 2 and case.source["failed_chunks"] == [2]


def test_extract_case_skips_chunk_with_provider_error():
    fake = FakeProvider(RuntimeError("deepseek returned no valid JSON content"), gorzkow_payload())
    case = extract_case(fake, RLU, "a" * 300 + "\n" + "b" * 300, max_chars=400, overlap=50)
    assert len(fake.calls) == 2 and len(case.tenders) == 1
    assert case.source["failed_chunks"] == [1]


def test_extract_case_all_chunks_invalid_raises():
    with pytest.raises(GoldExtractError):
        extract_case(FakeProvider({"tenders": "brak"}), RLU, "tekst decyzji")
    with pytest.raises(GoldExtractError):
        extract_case(FakeProvider(gorzkow_payload()), RLU, "   ")


def test_extract_case_budget_exceeded_propagates():
    fake = FakeProvider(gorzkow_payload(), BudgetExceeded("limit"))
    with pytest.raises(BudgetExceeded):
        extract_case(fake, RLU, "a" * 300 + "\n" + "b" * 300, max_chars=400, overlap=50)


def test_system_prompt():
    assert SYSTEM_PROMPT.startswith("Jesteś analitykiem decyzji Prezesa UOKiK")
    for word in ("rotation", "cover_bids", "market_split", "bid_suppression", "other",
                 "colluders", "tenders", "rigged", "won"):
        assert word in SYSTEM_PROMPT
    assert not DASHES.search(SYSTEM_PROMPT)


# ---------------------------------------------------------------- download


@respx.mock
def test_download_decision_stores_raw(fixtures, tmp_path):
    data = pdf_bytes(fixtures)
    respx.get(RLU.url).mock(return_value=httpx.Response(200, content=data))
    rs = RawStore(tmp_path)
    got, sha = download_decision(quiet_client(), rs, RLU)
    assert got == data
    assert RAW_SOURCE == "gold" and rs.get(sha, source="gold") == data


@respx.mock
def test_download_decision_rejects_errors(tmp_path):
    rs = RawStore(tmp_path)
    respx.get(RLU.url).mock(return_value=httpx.Response(404, text="nie ma"))
    with pytest.raises(GoldExtractError, match="404"):
        download_decision(quiet_client(), rs, RLU)
    respx.get(RLU.url).mock(return_value=httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(GoldExtractError, match="PDF"):
        download_decision(quiet_client(), rs, RLU)


# ---------------------------------------------------------------- database


def test_migration_004_creates_gold_cases(db):
    assert resources.files("tie.migrations").joinpath("004_gold_cases.sql").is_file()
    cols = {r["column_name"]: r["data_type"] for r in db.execute(
        "select column_name, data_type from information_schema.columns"
        " where table_name = 'gold_cases'")}
    assert cols == {"decision": "text", "payload": "jsonb",
                    "extracted_at": "timestamp with time zone"}


def test_store_case_upserts(db):
    case = GoldCase.model_validate(gorzkow_payload())
    store_case(db, case)
    store_case(db, case.model_copy(update={"notes": "nowe"}))
    rows = db.execute("select * from gold_cases").fetchall()
    assert len(rows) == 1 and rows[0]["decision"] == "RLU-7/2023"
    assert rows[0]["payload"]["notes"] == "nowe"
    assert rows[0]["payload"]["tenders"][0]["bids"][1]["price"] == 98100
    assert load_cases(db)[0]["decision"] == "RLU-7/2023"


@respx.mock
def test_run_gold_extract_end_to_end(db, fixtures, tmp_path):
    other = get_decision("RGD-10/2023")
    respx.get(RLU.url).mock(return_value=httpx.Response(200, content=pdf_bytes(fixtures)))
    respx.get(other.url).mock(return_value=httpx.Response(500))
    rs = RawStore(tmp_path)
    fake = FakeProvider(gorzkow_payload())
    summary = run_gold_extract(db, quiet_client(), rs, fake, [RLU, other])
    assert summary["extracted"] == ["RLU-7/2023"]
    assert list(summary["failed"]) == ["RGD-10/2023"]
    assert summary["skipped"] == [] and summary["budget_exceeded"] is False
    payload = db.execute("select payload from gold_cases").fetchone()["payload"]
    assert payload["decision"] == "RLU-7/2023"
    assert payload["source"]["url"] == RLU.url
    assert payload["source"]["region"] == "lubelskie"
    assert payload["source"]["text_method"] == "pdftext"
    assert rs.exists(payload["source"]["sha256"], source="gold")
    # a second run skips what is stored, unless forced
    again = run_gold_extract(db, quiet_client(), rs, fake, [RLU])
    assert again["skipped"] == ["RLU-7/2023"] and len(fake.calls) == 1
    forced = run_gold_extract(db, quiet_client(), rs, fake, [RLU], force=True)
    assert forced["extracted"] == ["RLU-7/2023"] and len(fake.calls) == 2


@respx.mock
def test_run_gold_extract_stops_on_budget(db, fixtures, tmp_path):
    other = get_decision("RGD-10/2023")
    for d in (RLU, other):
        respx.get(d.url).mock(return_value=httpx.Response(200, content=pdf_bytes(fixtures)))
    fake = FakeProvider(BudgetExceeded("limit"))
    summary = run_gold_extract(db, quiet_client(), RawStore(tmp_path), fake, [RLU, other])
    assert summary["budget_exceeded"] is True
    assert summary["extracted"] == [] and summary["failed"] == {}
    assert len(fake.calls) == 1
    assert db.execute("select count(*) c from gold_cases").fetchone()["c"] == 0


def test_export_gold(db, tmp_path):
    store_case(db, GoldCase.model_validate(gorzkow_payload()))
    store_case(db, GoldCase.model_validate(gorzkow_payload(decision="RGD-10/2023", tenders=[])))
    path = tmp_path / "validation" / "gold" / "uokik.json"
    assert export_gold(db, path) == 2
    out = json.loads(path.read_text("utf-8"))
    assert out["count"] == 2
    assert [c["decision"] for c in out["cases"]] == ["RGD-10/2023", "RLU-7/2023"]
    assert out["cases"][1]["tenders"][0]["bids"][1]["price"] == 98100
    assert "Gorzków" in path.read_text("utf-8")  # written as UTF-8, not escaped


# ---------------------------------------------------------------- CLI


def test_cli_gold_extract_unknown_decision():
    assert cli.main(["gold-extract", "--only", "RXX-1/2020"]) == 2


def test_cli_gold_extract_needs_deepseek_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert cli.main(["gold-extract", "--only", "RLU-7/2023"]) == 2


@respx.mock
def test_cli_gold_extract_and_export(db, fixtures, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    respx.get(RLU.url).mock(return_value=httpx.Response(200, content=pdf_bytes(fixtures)))
    respx.post("https://api.deepseek.com/chat/completions").mock(return_value=httpx.Response(
        200, json={"choices": [{"message": {"content": json.dumps(gorzkow_payload())}}],
                   "usage": {"prompt_tokens": 1200, "completion_tokens": 300}}))
    assert cli.main(["gold-extract", "--only", "RLU-7/2023"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["extracted"] == ["RLU-7/2023"]
    assert db.execute("select count(*) c from gold_cases").fetchone()["c"] == 1
    calls = db.execute("select task, ok from ai_calls").fetchall()
    assert [(c["task"], c["ok"]) for c in calls] == [("gold_extract", True)]
    db.commit()
    out = tmp_path / "uokik.json"
    assert cli.main(["gold-export", str(out)]) == 0
    assert json.loads(out.read_text("utf-8"))["cases"][0]["decision"] == "RLU-7/2023"
