import json
import os
import re
from datetime import date

import httpx
import pytest
import respx

from tie import cli
from tie.config import load_settings
from tie.handlers import HANDLERS, RATES, Context
from tie.http import PoliteClient
from tie.identity import msig
from tie.identity.msig import (
    DETAILS_URL,
    SEARCH_URL,
    birth_year,
    enqueue_due,
    normalize_full_name,
    parse_announcement,
    person_key,
    search_announcements,
    sync_bidder,
    valid_pesel,
)
from tie.jobs import enqueue

KRS = "0000047217"
SECRET = "test-secret-not-for-production"
IDS = [9263478, 8953421, 8788047, 5958037, 5938641, 5944222, 5025160, 2300354, 18606798]
# Fake, checksum-valid PESEL numbers used in the redacted fixtures, by fake surname.
FAKE = {
    "NOWAKOWSKI": "72031501234", "ZIELIŃSKI": "80112004565", "ŁĄCKA": "65050507891",
    "KOWALCZYK": "02211507897", "WIŚNIEWSKI": "75070702341", "DĄBROWSKA": "58122403456",
    "LEWANDOWSKI": "83041205678", "WÓJCIK": "69090906784", "MAZUR": "77010108909",
    "KAMIŃSKI": "61060609015", "SZYMAŃSKA": "85080810127", "WOŹNIAK": "90022810123",
    "KRAWCZYK": "91051504568",
}


def detail(fixtures, source_id):
    return json.loads((fixtures / "msig" / f"detail_{source_id}.json").read_text("utf-8"))


def client():
    return PoliteClient("ua", {}, sleep=lambda s: None)


def summary(ann):
    return [(p.role, p.op, p.surname, p.pesel) for p in ann.persons]


# --- PESEL, names, keys ---------------------------------------------------------------


def test_valid_pesel():
    for pesel in FAKE.values():
        assert valid_pesel(pesel), pesel
    assert not valid_pesel("72031501235")  # checksum
    assert not valid_pesel("72131501236")  # month 13
    assert not valid_pesel("7203150123")  # too short
    assert not valid_pesel("7203150123a")
    assert not valid_pesel("")


def test_birth_year():
    assert birth_year("72031501234") == 1972
    assert birth_year("02211507897") == 2002  # month + 20 = born in the 2000s
    assert birth_year("58122403456") == 1958


def test_normalize_full_name():
    # Diacritics, case, spacing and OCR-split words do not change the normalized name.
    assert normalize_full_name("ŁĄCKA", "ANNA MARIA") == "LACKA ANNA"
    assert normalize_full_name("ZIELIŃS KI", "PIOTR  MAREK") == "ZIELINSKI PIOTR"
    assert normalize_full_name("Kowalska-Nowak", "Ewa") == "KOWALSKANOWAK EWA"
    # Only the first given name counts: MSiG lists the same person (same PESEL) once as
    # "MARTA MARIA" and once as "MARTA".
    assert normalize_full_name("MAZUR", "MARTA MARIA") == normalize_full_name("MAZUR", "MARTA")


def test_person_key_deterministic():
    k1 = person_key(SECRET, "MAZUR", "MARTA MARIA", "77010108909")
    assert k1 == person_key(SECRET, "Mazur", "MARTA", "77010108909")
    assert re.fullmatch(r"[0-9a-f]{64}", k1)
    assert "77010108909" not in k1
    assert k1 != person_key("other-secret", "MAZUR", "MARTA", "77010108909")
    assert k1 != person_key(SECRET, "MAZUR", "MARTA", "61060609015")
    assert k1 != person_key(SECRET, "NOWAK", "MARTA", "77010108909")


def test_person_key_requires_secret():
    with pytest.raises(ValueError):
        person_key("", "MAZUR", "MARTA", "77010108909")
    with pytest.raises(ValueError):
        person_key(None, "MAZUR", "MARTA", "77010108909")


def test_settings_secret_has_no_default():
    s = load_settings({"DATABASE_URL": "x"})
    assert s.person_hmac_secret is None
    assert s.msig_daily_cap == 500
    s = load_settings({"DATABASE_URL": "x", "PERSON_HMAC_SECRET": "k", "MSIG_DAILY_CAP": "7"})
    assert s.person_hmac_secret == "k" and s.msig_daily_cap == 7


# --- parser ---------------------------------------------------------------------------


def test_parse_modern_remove_and_add(fixtures):
    ann = parse_announcement(detail(fixtures, 2300354))
    assert ann.source_id == "2300354"
    assert ann.published == date(2020, 11, 27)
    assert ann.entry_date == date(2020, 11, 23)  # "W dniu 23.1 1.2020" (OCR space)
    assert ann.entry_no == 45
    assert summary(ann) == [
        ("board", "remove", "NOWAKOWSKI", FAKE["NOWAKOWSKI"]),
        ("board", "remove", "ZIELIŃSKI", FAKE["ZIELIŃSKI"]),
        ("board", "add", "ŁĄCKA", FAKE["ŁĄCKA"]),
        ("board", "add", "KOWALCZYK", FAKE["KOWALCZYK"]),
    ]
    assert ann.persons[2].given == "ANNA MARIA"


def test_parse_old_format_change_and_proxy(fixtures):
    ann = parse_announcement(detail(fixtures, 9263478))
    assert ann.entry_date == date(2002, 8, 22)
    assert summary(ann) == [
        ("board", "add", "NOWAKOWSKI", FAKE["NOWAKOWSKI"]),
        ("board", "add", "ZIELIŃSKI", FAKE["ZIELIŃSKI"]),
        # "zmienić: 3 (dla pozycji 1. ... 3. PESEL) 4. PREZES na CZŁONEK ZARZĄDU"
        ("board", "keep", "WIŚNIEWSKI", FAKE["WIŚNIEWSKI"]),
        ("proxy", "remove", "DĄBROWSKA", FAKE["DĄBROWSKA"]),
    ]


def test_parse_board_and_supervisory(fixtures):
    ann = parse_announcement(detail(fixtures, 8953421))
    assert ann.entry_date == date(2003, 12, 18)
    assert summary(ann) == [
        ("board", "remove", "WIŚNIEWSKI", FAKE["WIŚNIEWSKI"]),
        ("board", "add", "LEWANDOWSKI", FAKE["LEWANDOWSKI"]),
        ("supervisory", "add", "WÓJCIK", FAKE["WÓJCIK"]),
        ("supervisory", "add", "MAZUR", FAKE["MAZUR"]),
        ("supervisory", "add", "KAMIŃSKI", FAKE["KAMIŃSKI"]),
    ]
    assert ann.persons[3].given == "MARTA MARIA"


def test_parse_function_change_keeps_member(fixtures):
    # "(dla pozycji: 1. X 2. Y 3. PESEL) wykreślić: 5. PREZES ZARZĄDU wpisać: 5. CZŁONEK"
    # changes a field of a member, it does not remove the member. Branches (Dz. 1 Rub. 3)
    # are ignored.
    ann = parse_announcement(detail(fixtures, 5025160))
    assert summary(ann) == [("board", "keep", "ZIELIŃSKI", FAKE["ZIELIŃSKI"])]


def test_parse_ignores_shareholders_and_reads_organ_removal(fixtures):
    # Dz. 1 Rub. 7 (shareholders) is ignored; removing the whole supervisory board removes
    # every member listed under it.
    ann = parse_announcement(detail(fixtures, 5944222))
    assert ann.entry_date == date(2016, 12, 19)
    assert summary(ann) == [
        ("supervisory", "remove", "MAZUR", FAKE["MAZUR"]),
        ("supervisory", "remove", "WOŹNIAK", FAKE["WOŹNIAK"]),
        ("supervisory", "remove", "KRAWCZYK", FAKE["KRAWCZYK"]),
    ]


def test_parse_without_persons(fixtures):
    ann = parse_announcement(detail(fixtures, 18606798))
    assert ann.persons == [] and ann.invalid_pesel == 0
    assert ann.entry_date == date(2025, 6, 30)


def test_parse_rejects_invalid_pesel_and_ignores_other_sections():
    doc = {
        "id": 1, "dateOfPublication": "2024-03-01T00:00:00",
        "textInBody": (
            "W dniu 17 .02 .2024 dokonano wpisu do rejestru KRS  nr 9 następującej treści: "
            "Dz. 2. Rub. 1. Organ uprawniony do reprezentacji podmiotu "
            "PRub. Dane osób wchodzących w skład organu wpisać: "
            "1 1. NOWAK 2. JAN 3. 72031501235 5. PREZES ZARZĄDU "
            "2 1. KOT 2. ADAM 3. 8011200456 5 5. CZŁONEK ZARZĄDU "
            "Dz.  5.  Rub.  1.  Kurator wpisać: 1  1.  WILK  2. MAREK 3. 65050507891 "
            "5. POSTANOWIENIE SĄDU"
        ),
    }
    ann = parse_announcement(doc)
    assert ann.entry_date == date(2024, 2, 17)
    # Bad checksum dropped; a PESEL split by an OCR space is joined; the curator (Dz. 5) is
    # not a board member.
    assert summary(ann) == [("board", "add", "KOT", "80112004565")]
    assert ann.invalid_pesel == 1


def test_parse_falls_back_to_publication_date():
    ann = parse_announcement({"id": 5, "dateOfPublication": "2019-05-06T00:00:00",
                              "textInBody": "bez daty"})
    assert ann.entry_date == date(2019, 5, 6) and ann.entry_no is None


def test_announcement_repr_hides_personal_data(fixtures):
    ann = parse_announcement(detail(fixtures, 2300354))
    text = repr(ann) + str(ann.persons)
    assert FAKE["ŁĄCKA"] not in text and "ŁĄCKA" not in text


# --- HTTP -----------------------------------------------------------------------------


def test_msig_rate_limit_registered():
    assert RATES["wyszukiwarka-msig.ms.gov.pl"] >= 0.6


@respx.mock
def test_search_pages_until_no_new_ids():
    def page(request):
        p = int(request.url.params["page"])
        assert request.url.params["krs"] == KRS
        assert request.url.params["signatureType"] == "B"
        assert request.url.params["from"] == "2001-01-01"
        assert request.url.params["to"] == "2026-10-12"
        items = {1: list(range(1, 51)), 2: [50, 51]}.get(p, [])
        return httpx.Response(200, json={"countPages": 100, "page": p, "list": [
            {"id": i, "dateOfPublication": "2020-01-01T00:00:00"} for i in items]})

    route = respx.get(SEARCH_URL).mock(side_effect=page)
    items = search_announcements(client(), KRS, date(2001, 1, 1), date(2026, 10, 12))
    assert [i["id"] for i in items] == list(range(1, 52))
    assert route.call_count == 2  # page 2 is short: no page 3


@respx.mock
def test_search_http_error_raises_without_body():
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(400, text="secret body"))
    with pytest.raises(msig.MsigError) as exc:
        search_announcements(client(), KRS, date(2001, 1, 1), date(2026, 10, 12))
    assert "secret body" not in str(exc.value)


def mock_msig(fixtures):
    search = json.loads((fixtures / "msig" / "search_0000047217.json").read_text("utf-8"))
    search_route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=search))

    def details(request):
        return httpx.Response(200, json=detail(fixtures, request.url.params["Id"]))

    details_route = respx.get(DETAILS_URL).mock(side_effect=details)
    return search_route, details_route


def add_bidder(db, nip, krs, name="FIRMA SP. Z O.O."):
    row = db.execute("insert into bidders(nip, krs, name, name_norm) values (%s,%s,%s,%s)"
                     " returning id", (nip, krs, name, name.lower())).fetchone()
    db.commit()
    return row["id"]


def db_dump(db):
    parts = []
    for table in ("board_links", "msig_announcements", "bidders", "jobs"):
        for r in db.execute(f"select row_to_json(t)::text j from {table} t"):
            parts.append(r["j"])
    db.commit()
    return "\n".join(parts)


@respx.mock
def test_sync_bidder_stores_links_without_personal_data(db, fixtures):
    bidder = add_bidder(db, "6791021905", KRS)
    search_route, details_route = mock_msig(fixtures)
    assert sync_bidder(db, client(), bidder, SECRET, today=date(2026, 10, 12)) is True
    assert details_route.call_count == len(IDS)

    def key(surname, given):
        return person_key(SECRET, surname, given, FAKE[surname])

    rows = {(r["person_key"], r["role"]): r for r in db.execute(
        "select * from board_links where bidder_id = %s", (bidder,))}
    db.commit()
    expected = {
        (key("NOWAKOWSKI", "JAN ADAM"), "board"): (date(2002, 8, 22), date(2020, 11, 23), 1972),
        (key("ZIELIŃSKI", "PIOTR MAREK"), "board"): (date(2002, 8, 22), date(2020, 11, 23), 1980),
        (key("WIŚNIEWSKI", "ROBERT"), "board"): (date(2002, 8, 22), date(2003, 12, 18), 1975),
        (key("LEWANDOWSKI", "KAROL JÓZEF"), "board"): (date(2003, 12, 18), date(2005, 3, 24),
                                                      1983),
        (key("ŁĄCKA", "ANNA MARIA"), "board"): (date(2020, 11, 23), None, 1965),
        (key("KOWALCZYK", "TOMASZ"), "board"): (date(2020, 11, 23), None, 2002),
        # Removed before any announcement listed the proxy: a one-day period.
        (key("DĄBROWSKA", "EWA"), "proxy"): (date(2002, 8, 22), date(2002, 8, 22), 1958),
        (key("WÓJCIK", "HANNA"), "supervisory"): (date(2003, 12, 18), date(2015, 12, 28), 1969),
        (key("MAZUR", "MARTA"), "supervisory"): (date(2003, 12, 18), date(2016, 12, 19), 1977),
        (key("KAMIŃSKI", "TADEUSZ"), "supervisory"): (date(2003, 12, 18), date(2015, 12, 28),
                                                     1961),
        (key("SZYMAŃSKA", "KATARZYNA"), "supervisory"): (date(2015, 12, 28), date(2016, 12, 15),
                                                        1985),
        (key("WOŹNIAK", "JOLANTA"), "supervisory"): (date(2015, 12, 28), date(2016, 12, 19),
                                                    1990),
        (key("KRAWCZYK", "MARTYNA"), "supervisory"): (date(2016, 12, 15), date(2016, 12, 19),
                                                     1991),
    }
    got = {k: (r["valid_from"], r["valid_to"], r["birth_year"]) for k, r in rows.items()}
    assert got == expected
    assert rows[(key("ŁĄCKA", "ANNA MARIA"), "board")]["source_id"] == "2300354"

    n = db.execute("select count(*) n from msig_announcements where bidder_id = %s",
                   (bidder,)).fetchone()["n"]
    checked = db.execute("select msig_checked_at from bidders where id = %s",
                         (bidder,)).fetchone()["msig_checked_at"]
    db.commit()
    assert n == len(IDS) and checked is not None

    # No raw PESEL (also not without its check digit) and no name of a person anywhere.
    dump = db_dump(db)
    for surname, pesel in FAKE.items():
        assert pesel not in dump and pesel[:10] not in dump
        assert surname not in dump
    for given in ("JAN ADAM", "PIOTR MAREK", "ANNA MARIA", "KAROL JÓZEF", "MARTA"):
        assert given not in dump

    # A re-check searches from the newest processed announcement and fetches no details
    # for announcements already processed.
    assert sync_bidder(db, client(), bidder, SECRET, today=date(2026, 11, 20)) is True
    assert details_route.call_count == len(IDS)
    assert search_route.calls.last.request.url.params["from"] == "2025-07-24"
    assert db.execute("select count(*) n from board_links").fetchone()["n"] == len(expected)
    db.commit()


@respx.mock
def test_sync_bidder_without_krs_is_marked_checked(db):
    bidder = add_bidder(db, "7010000005", None)
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(500))
    assert sync_bidder(db, client(), bidder, SECRET, today=date(2026, 10, 12)) is True
    assert route.call_count == 0
    assert db.execute("select msig_checked_at from bidders where id = %s",
                      (bidder,)).fetchone()["msig_checked_at"] is not None
    db.commit()


def test_person_key_column_rejects_raw_values(db):
    import psycopg

    bidder = add_bidder(db, "6791021905", KRS)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("insert into board_links(bidder_id, person_key, role, valid_from)"
                   " values (%s, %s, 'board', '2020-01-01')", (bidder, "72031501234"))
    db.rollback()


# --- shared pairs ---------------------------------------------------------------------


def test_shared_board_pairs(db):
    b1 = add_bidder(db, "6791021905", KRS)
    b2 = add_bidder(db, "5260250995", "0000019193")
    b3 = add_bidder(db, "7010000005", "0000000003")
    b4 = add_bidder(db, "1132853869", "0000000004")
    p1, p2, p3 = "a" * 64, "b" * 64, "c" * 64
    rows = [
        (b1, p1, "board", "2010-01-01", None),
        (b2, p1, "supervisory", "2015-01-01", "2018-01-01"),  # overlaps b1
        (b1, p2, "board", "2001-01-01", "2005-01-01"),
        (b3, p2, "proxy", "2006-01-01", None),  # no overlap with b1
        (b2, p2, "board", "2004-06-01", "2004-07-01"),  # overlaps b1, not b3
        (b4, p3, "board", "2020-01-01", None),  # shares with nobody
    ]
    for r in rows:
        db.execute("insert into board_links(bidder_id, person_key, role, valid_from, valid_to)"
                   " values (%s, %s, %s, %s, %s)", r)
    db.commit()
    pairs = {(r["a"], r["b"]): (r["n_shared"], r["n_concurrent"]) for r in db.execute(
        "select * from shared_board_pairs")}
    db.commit()
    assert pairs == {
        (b1, b2): (2, 2),
        (b1, b3): (1, 0),
        (b2, b3): (1, 0),
    }
    assert all(a < b for a, b in pairs)


# --- job and daily enqueue ------------------------------------------------------------


def ctx_with(db, secret):
    env = {"DATABASE_URL": "x"}
    if secret:
        env["PERSON_HMAC_SECRET"] = secret
    return Context(conn=db, settings=load_settings(env), client=client(), rawstore=None,
                   text_provider=None, decider=None)


@respx.mock
def test_handler_requires_secret(db):
    bidder = add_bidder(db, "6791021905", KRS)
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(RuntimeError, match="PERSON_HMAC_SECRET"):
        HANDLERS["msig_board"](ctx_with(db, None), {"bidder_id": bidder})
    assert route.call_count == 0


@respx.mock
def test_handler_syncs_bidder(db, fixtures):
    bidder = add_bidder(db, "6791021905", KRS)
    mock_msig(fixtures)
    HANDLERS["msig_board"](ctx_with(db, SECRET), {"bidder_id": bidder})
    assert db.execute("select count(*) n from board_links").fetchone()["n"] == 13
    db.commit()


@respx.mock
def test_handler_queues_continuation_after_time_limit(db, fixtures, monkeypatch):
    bidder = add_bidder(db, "6791021905", KRS)
    calls = {"n": 0}

    def page(request):
        calls["n"] += 1
        p = int(request.url.params["page"])
        return httpx.Response(200, json={"countPages": 100, "page": p, "list": [
            {"id": 1000 * p + i, "dateOfPublication": "2020-01-01T00:00:00"}
            for i in range(50)]})

    respx.get(SEARCH_URL).mock(side_effect=page)
    respx.get(DETAILS_URL).mock(side_effect=lambda r: httpx.Response(200, json={
        "id": int(r.url.params["Id"]), "dateOfPublication": "2020-01-01T00:00:00",
        "textInBody": "brak osób"}))
    monkeypatch.setattr(msig, "MSIG_TIME_LIMIT_S", -1)
    HANDLERS["msig_board"](ctx_with(db, SECRET), {"bidder_id": bidder})
    assert calls["n"] == 1  # stopped after the first page
    job = db.execute("select key, payload from jobs where kind = 'msig_board'").fetchone()
    db.commit()
    assert job["payload"] == {"bidder_id": bidder} and job["key"].startswith(f"{bidder}@more@")
    assert db.execute("select count(*) n from msig_announcements").fetchone()["n"] == 50
    db.commit()


def test_enqueue_due(db):
    due_new = add_bidder(db, "6791021905", KRS)
    due_old = add_bidder(db, "5260250995", "0000019193")
    fresh = add_bidder(db, "7010000005", "0000000003")
    add_bidder(db, "1132853869", None)
    queued = add_bidder(db, "9542583988", "0000000005")
    db.execute("update bidders set msig_checked_at = now() - interval '40 days' where id = %s",
               (due_old,))
    db.execute("update bidders set msig_checked_at = now() - interval '5 days' where id = %s",
               (fresh,))
    db.commit()
    enqueue(db, "msig_board", f"{queued}@2026-10-11", {"bidder_id": queued})
    assert enqueue_due(db, date(2026, 10, 12), limit=10) == 2
    keys = {r["key"] for r in db.execute("select key from jobs where kind = 'msig_board'")}
    db.commit()
    assert keys == {f"{due_new}@2026-10-12", f"{due_old}@2026-10-12", f"{queued}@2026-10-11"}
    # Running it again the same day adds nothing; the limit caps new jobs.
    assert enqueue_due(db, date(2026, 10, 12), limit=10) == 0


def test_enqueue_due_limit(db):
    for nip, krs in (("6791021905", KRS), ("5260250995", "0000019193")):
        add_bidder(db, nip, krs)
    assert enqueue_due(db, date(2026, 10, 12), limit=1) == 1


def test_daily_queues_msig_only_with_secret(db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setattr(cli, "today", lambda: date(2026, 10, 12))
    bidder = add_bidder(db, "6791021905", KRS)
    monkeypatch.delenv("PERSON_HMAC_SECRET", raising=False)
    assert cli.main(["daily"]) == 0
    assert db.execute("select count(*) n from jobs where kind = 'msig_board'").fetchone()["n"] == 0
    db.commit()
    monkeypatch.setenv("PERSON_HMAC_SECRET", SECRET)
    assert cli.main(["daily"]) == 0
    row = db.execute("select key, payload from jobs where kind = 'msig_board'").fetchone()
    db.commit()
    assert row["key"] == f"{bidder}@2026-10-12" and row["payload"] == {"bidder_id": bidder}
