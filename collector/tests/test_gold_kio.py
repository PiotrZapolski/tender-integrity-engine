import json
import os
from datetime import date

import httpx
import pytest
import respx
from pydantic import ValidationError

from tie import cli
from tie.gold import kio
from tie.http import PoliteClient

FIXTURE = "kio_2378_21_excerpt.html"
SEED_2378 = kio.find_seed("KIO 2378/21")

EXTRACTION = {
    "signature": "KIO 2378/21",
    "ruling_date": "2021-10-01",
    "buyer": "Gmina Jedlnia - Letnisko",
    "subject": "świadczenie usługi dowozu dzieci oraz dzieci niepełnosprawnych z terenu"
               " Gminy Jedlnia-Letnisko na zajęcia szkolne",
    "procedure_ref": "2021/BZP 00100048/01",
    "bzp_number": "2021/BZP 00100048/01",
    "ocds_id": None,
    "appellant": "AMP Sp. z o.o.",
    "appellant_role": "competitor",
    "colluders": [{"name": "E. O.", "nip": None},
                  {"name": "PKS Kielce sp. z o.o.", "nip": "PL 657-000-11-22"}],
    "decision": "upheld",
    "reasoning": "Izba nakazała wykluczenie E. O. na podstawie art. 108 ust. 1 pkt 5.",
}


class FakeProvider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete_json(self, *, task, system, user):
        self.calls.append({"task": task, "system": system, "user": user})
        return self.payload


def client():
    return PoliteClient("TieTest/1", {}, sleep=lambda s: None, max_retries=0)


def fixture_html(fixtures) -> bytes:
    return (fixtures / "kio" / FIXTURE).read_bytes()


def mock_ruling(doc_id, response):
    return respx.route(method="GET", host=kio.HOST,
                       path=f"/Home/ContentHtml/{doc_id}").mock(return_value=response)


def add_procedure(db, ocds, bzp, title, published, buyer="Gmina Jedlnia-Letnisko"):
    buyer_id = db.execute("insert into buyers(name) values (%s) returning id",
                          (buyer,)).fetchone()["id"]
    db.execute("insert into procedures(ocds_id, bzp_number, notice_number, buyer_id, title,"
               " published_at) values (%s, %s, %s, %s, %s, %s)",
               (ocds, bzp, bzp + "/01", buyer_id, title, published))
    db.commit()


# --- seed list -------------------------------------------------------------------------

def test_seed_list_is_curated():
    sigs = [s for seed in kio.SEED for s in seed.signatures]
    assert len(sigs) == len(set(sigs))
    assert 20 <= len(kio.SEED) <= 80
    assert {s.expected for s in kio.SEED} == {"upheld", "overturned"}
    for seed in kio.SEED:
        assert date(2021, 1, 1) <= seed.ruling_date <= date(2026, 12, 31)
        assert seed.signature == kio.normalize_signature(seed.signature)
        assert seed.url == f"https://orzeczenia.uzp.gov.pl/Home/Details/{seed.doc_id}"


def test_find_seed_by_any_signature_of_a_joint_case():
    seed = kio.find_seed("kio  2258/21")
    assert seed is not None and seed.signature == "KIO 2254/21"
    assert kio.find_seed("KIO 9999/99") is None
    assert SEED_2378.doc_id == 15733 and SEED_2378.expected == "upheld"


# --- text ------------------------------------------------------------------------------

def test_html_to_text_real_ruling_excerpt(fixtures):
    text = kio.html_to_text(fixture_html(fixtures).decode("utf-8"))
    assert text.splitlines()[0] == "Sygn. akt: KIO 2378/21"
    assert "2021/BZP 00100048/01" in text
    assert "wykluczenie wykonawcy E. O. prowadzącej" in text
    # <br> inside a paragraph becomes a space, not a glued word
    assert "zamówienia (część I) jako najkorzystniejszej" in text
    assert "\xa0" not in text


def test_excerpt_short_text_is_unchanged():
    assert kio.excerpt("abc\ndef", max_chars=100) == "abc\ndef"


def test_excerpt_long_text_keeps_head_tail_and_keyword_windows():
    text = ("H" * 20000 + " filler " * 3000 + " zawarli porozumienie mające na celu "
            + " filler " * 3000 + "T" * 8000)
    out = kio.excerpt(text, max_chars=24000)
    assert len(out) <= 24000
    assert out.startswith("H" * 100)
    assert out.endswith("T" * 100)
    assert "zawarli porozumienie" in out


# --- extraction ------------------------------------------------------------------------

def test_extraction_normalizes_fields():
    ext = kio.KioExtraction.model_validate(EXTRACTION)
    assert ext.bzp_number == "2021/BZP 00100048"
    assert ext.colluders[1].nip == "6570001122"
    assert ext.colluders[0].nip is None
    assert ext.ruling_date == date(2021, 10, 1)
    ext = kio.KioExtraction.model_validate(
        {**EXTRACTION, "ruling_date": "01-10-2021", "bzp_number": "brak",
         "ocds_id": "ocds-148610-0446493b-6b68-4fa8-aa08-45d7d62c808c", "buyer": "  ",
         "colluders": [{"name": "X sp. z o.o.", "nip": "123"}], "decision": " Upheld "})
    assert ext.ruling_date == date(2021, 10, 1)
    assert ext.bzp_number is None
    assert ext.ocds_id == "ocds-148610-0446493b-6b68-4fa8-aa08-45d7d62c808c"
    assert ext.buyer is None
    assert ext.colluders[0].nip is None
    assert ext.decision == "upheld"
    assert kio.KioExtraction.model_validate({**EXTRACTION, "ocds_id": "123"}).ocds_id is None


def test_extraction_rejects_unknown_decision():
    with pytest.raises(ValidationError):
        kio.KioExtraction.model_validate({**EXTRACTION, "decision": "maybe"})
    with pytest.raises(ValidationError):
        kio.KioExtraction.model_validate({k: v for k, v in EXTRACTION.items()
                                          if k != "decision"})


def test_extract_ruling_calls_provider_with_task_and_text(fixtures):
    text = kio.html_to_text(fixture_html(fixtures).decode("utf-8"))
    provider = FakeProvider(EXTRACTION)
    ext = kio.extract_ruling(provider, text, signature="KIO 2378/21")
    assert ext.decision == "upheld"
    (call,) = provider.calls
    assert call["task"] == "gold_kio"
    assert "KIO 2378/21" in call["user"]
    assert "Gmina Jedlnia - Letnisko" in call["user"]
    assert "108 ust. 1 pkt 5" in call["system"]


# --- fetch -----------------------------------------------------------------------------

@respx.mock
def test_fetch_ruling_html_decodes_utf8(fixtures):
    route = mock_ruling(15733, httpx.Response(200, content=fixture_html(fixtures),
                                              headers={"content-type": "text/html"}))
    with client() as c:
        html = kio.fetch_ruling_html(c, 15733)
    assert "Krajowa Izba Odwoławcza" in html
    assert route.calls.last.request.url.params["Kind"] == "KIO"


@respx.mock
def test_fetch_ruling_html_raises_on_http_error():
    mock_ruling(15733, httpx.Response(404, text="not found"))
    with client() as c, pytest.raises(RuntimeError, match="HTTP 404"):
        kio.fetch_ruling_html(c, 15733)


# --- matcher ---------------------------------------------------------------------------

def test_match_by_ocds_then_bzp(db):
    add_procedure(db, "ocds-148610-aaaaaaaa-0000-0000-0000-000000000001", "2021/BZP 00100048",
                  "Dowóz dzieci", "2021-11-15T10:00:00Z")
    ext = kio.KioExtraction.model_validate(
        {**EXTRACTION, "ocds_id": "ocds-148610-aaaaaaaa-0000-0000-0000-000000000001"})
    m = kio.match_procedure(db, ext, date(2021, 10, 1))
    assert (m.ocds_id, m.method) == ("ocds-148610-aaaaaaaa-0000-0000-0000-000000000001", "ocds")
    ext = kio.KioExtraction.model_validate(EXTRACTION)
    m = kio.match_procedure(db, ext, date(2021, 10, 1))
    assert (m.ocds_id, m.method) == ("ocds-148610-aaaaaaaa-0000-0000-0000-000000000001", "bzp")


def test_match_by_buyer_and_subject(db):
    add_procedure(db, "ocds-148610-bbbbbbbb-0000-0000-0000-000000000002", "2021/BZP 00200000",
                  "Świadczenie usługi dowozu dzieci oraz dzieci niepełnosprawnych z terenu"
                  " Gminy Jedlnia-Letnisko na zajęcia szkolne", "2021-11-15T10:00:00Z")
    add_procedure(db, "ocds-148610-cccccccc-0000-0000-0000-000000000003", "2021/BZP 00300000",
                  "Dostawa oleju opałowego", "2021-11-16T10:00:00Z")
    ext = kio.KioExtraction.model_validate(EXTRACTION)
    m = kio.match_procedure(db, ext, date(2021, 10, 1))
    assert m.ocds_id == "ocds-148610-bbbbbbbb-0000-0000-0000-000000000002"
    assert m.method == "buyer_subject"
    assert 0.5 <= m.score <= 1.0
    # outside the date window around the ruling: no match
    assert kio.match_procedure(db, ext, date(2025, 1, 1)) is None


def test_no_match_for_other_buyer(db):
    add_procedure(db, "ocds-148610-dddddddd-0000-0000-0000-000000000004", "2021/BZP 00400000",
                  "Świadczenie usługi dowozu dzieci", "2021-11-15T10:00:00Z",
                  buyer="Powiat Radomski")
    ext = kio.KioExtraction.model_validate({**EXTRACTION, "bzp_number": None})
    assert kio.match_procedure(db, ext, date(2021, 10, 1)) is None


# --- run -------------------------------------------------------------------------------

@respx.mock
def test_run_stores_ruling_and_link(db, fixtures):
    add_procedure(db, "ocds-148610-bbbbbbbb-0000-0000-0000-000000000002", "2021/BZP 00200000",
                  "Świadczenie usługi dowozu dzieci oraz dzieci niepełnosprawnych z terenu"
                  " Gminy Jedlnia-Letnisko na zajęcia szkolne", "2021-11-15T10:00:00Z")
    mock_ruling(15733, httpx.Response(200, content=fixture_html(fixtures)))
    provider = FakeProvider(EXTRACTION)
    with client() as c:
        summary = kio.run(db, c, provider, only="kio 2378/21")
    assert summary["processed"] == 1 and summary["failed"] == 0
    assert summary["upheld"] == 1 and summary["matched"] == 1
    assert summary["expected_mismatch"] == []
    row = db.execute("select * from gold_kio").fetchone()
    assert row["signature"] == "KIO 2378/21"
    assert row["ruling_date"] == date(2021, 10, 1)
    assert row["decision"] == "upheld"
    assert row["buyer"] == "Gmina Jedlnia - Letnisko"
    assert row["bzp_number"] == "2021/BZP 00100048"
    assert row["procedure_ref"] == "2021/BZP 00100048/01"
    assert row["ocds_id"] == "ocds-148610-bbbbbbbb-0000-0000-0000-000000000002"
    assert row["url"] == "https://orzeczenia.uzp.gov.pl/Home/Details/15733"
    assert [c["name"] for c in row["colluders"]] == ["E. O.", "PKS Kielce sp. z o.o."]
    assert row["payload"]["doc_id"] == 15733
    assert row["payload"]["expected"] == "upheld"
    assert row["payload"]["match"]["method"] == "buyer_subject"
    assert row["payload"]["extraction"]["appellant_role"] == "competitor"
    # a second run updates in place
    provider.payload = {**EXTRACTION, "decision": "other"}
    with client() as c:
        summary = kio.run(db, c, provider, only="KIO 2378/21")
    assert summary["expected_mismatch"] == ["KIO 2378/21"]
    rows = db.execute("select decision from gold_kio").fetchall()
    assert [r["decision"] for r in rows] == ["other"]


@respx.mock
def test_run_counts_failures_and_continues(db, fixtures):
    seeds = (kio.SeedRuling("KIO 1/21", date(2021, 5, 1), 1, "upheld"),
             kio.SeedRuling("KIO 2/21", date(2021, 5, 2), 2, "overturned"))
    mock_ruling(1, httpx.Response(500, text="boom"))
    mock_ruling(2, httpx.Response(200, content=fixture_html(fixtures)))
    provider = FakeProvider({**EXTRACTION, "decision": "overturned", "bzp_number": None,
                             "buyer": None})
    with client() as c:
        summary = kio.run(db, c, provider, seeds=seeds)
    assert summary["processed"] == 1 and summary["failed"] == 1
    assert summary["failures"] == ["KIO 1/21"]
    assert summary["matched"] == 0 and summary["overturned"] == 1
    row = db.execute("select * from gold_kio").fetchone()
    assert row["signature"] == "KIO 2/21" and row["ocds_id"] is None


@respx.mock
def test_run_counts_invalid_ai_answer_as_failure(db, fixtures):
    mock_ruling(15733, httpx.Response(200, content=fixture_html(fixtures)))
    with client() as c:
        summary = kio.run(db, c, FakeProvider({"decision": "maybe"}), only="KIO 2378/21")
    assert summary["failed"] == 1
    assert db.execute("select count(*) n from gold_kio").fetchone()["n"] == 0


# --- cli -------------------------------------------------------------------------------

def test_cli_parser_has_gold_kio():
    args = cli.build_parser().parse_args(["gold-kio", "--only", "KIO 2378/21"])
    assert args.only == "KIO 2378/21"
    assert cli.build_parser().parse_args(["gold-kio"]).only is None


def test_cli_gold_kio_needs_deepseek_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert cli.main(["gold-kio"]) == 2


def test_cli_gold_kio_rejects_unknown_signature(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    assert cli.main(["gold-kio", "--only", "KIO 9999/99"]) == 2


@respx.mock
def test_cli_gold_kio_runs_one_ruling(db, fixtures, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    mock_ruling(15733, httpx.Response(200, content=fixture_html(fixtures)))
    respx.post("https://api.deepseek.com/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(EXTRACTION)}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}))
    assert cli.main(["gold-kio", "--only", "KIO 2378/21"]) == 0
    assert '"processed": 1' in capsys.readouterr().out
    assert db.execute("select decision from gold_kio").fetchone()["decision"] == "upheld"
    call = db.execute("select task, ok from ai_calls").fetchone()
    assert (call["task"], call["ok"]) == ("gold_kio", True)
