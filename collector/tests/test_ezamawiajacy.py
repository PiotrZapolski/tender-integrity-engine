import json
from dataclasses import replace

import httpx
import pytest
import respx

from tie.bzp.parser import parse_result_notice
from tie.bzp.store import upsert_notice
from tie.http import PoliteClient
from tie.platforms import ezamawiajacy
from tie.platforms.ezamawiajacy import (
    DOWNLOAD_JOB,
    check_robots,
    download_document,
    parse_rows,
    procedure_url,
    sync_documents,
)
from tie.rawstore import RawStore

HOST = "wodypolskie.ezamawiajacy.pl"
DEMAND_URL = f"https://{HOST}/pn/pgwwp/demand/316881/notice/public/details"
NOTICE_PATH = "/pn/pgwwp/demand/notice/public/231740/details"
NOTICE_URL = f"https://{HOST}{NOTICE_PATH}"
FOLDER_URL = f"{NOTICE_URL}?folder=0004&"
OPENING_TOKEN = "17918096246486E6VU8qFcoXRwOHdn9b"  # token of the file in the folder fixture
DOC_ID = f"{HOST}:4095893"
OPENING_NAME = "Informacja z otwarcia ofert K.ROZ.270.131.2026.pdf"


@pytest.fixture(autouse=True)
def fresh_robots():
    ezamawiajacy._robots.clear()
    yield
    ezamawiajacy._robots.clear()


def client():
    return PoliteClient("TIE-test/1.0", {}, sleep=lambda s: None)


def page(fixtures, name):
    return (fixtures / "ezamawiajacy" / name).read_text("utf-8")


def platform(fixtures, *, robots=None, file_response=None):
    """respx side effect imitating the platform host."""
    details = page(fixtures, "details_231740.html")
    folder = page(fixtures, "details_231740_folder0004.html")
    pdf = (fixtures / "openings" / "kielce_scan.pdf").read_bytes()

    def handle(request: httpx.Request) -> httpx.Response:
        path, query = request.url.path, request.url.query.decode()
        if path == "/robots.txt":
            if handle.robots is None:
                return httpx.Response(404)
            return httpx.Response(200, text=handle.robots)
        if request.method == "GET" and path == "/pn/pgwwp/demand/316881/notice/public/details":
            return httpx.Response(302, headers={"Location": NOTICE_URL})
        if request.method == "GET" and path == NOTICE_PATH:
            return httpx.Response(200, text=folder if "folder=0004" in query else details,
                                  headers={"Content-Type": "text/html;charset=UTF-8"})
        if request.method == "POST" and path == f"/repository/download/{OPENING_TOKEN}":
            return file_response or httpx.Response(
                200, content=pdf, headers={"Content-Type": "application/pdf"})
        return httpx.Response(404, text="<html>Strona nieodnaleziona</html>",
                              headers={"Content-Type": "text/html"})

    handle.robots = robots
    return handle


def upsert_procedure(db, fixtures, url=DEMAND_URL):
    rec = json.loads((fixtures / "bzp" / "result_multilot.json").read_text("utf-8"))
    notice = replace(parse_result_notice(rec), platform_url=url)
    return notice, upsert_notice(db, notice, "sha")


def test_procedure_url():
    assert procedure_url(DEMAND_URL) == DEMAND_URL
    assert procedure_url(f"http://{HOST}{NOTICE_PATH}?x=1") == NOTICE_URL
    zus = "https://zus.ezamawiajacy.pl/pn/zus/demand/304217/notice/public/details"
    assert procedure_url(zus) == zus
    for url in (None, "", "https://zbkkrakow.ezamawiajacy.pl/",
                "https://polin.ezamawiajacy.pl/servlet/HomeServlet",
                "https://plonsk.ezamawiajacy.pl/app/login",
                "https://ezamawiajacy.pl.evil.com/pn/a/demand/1/notice/public/details",
                "https://platformazakupowa.pl/transakcja/123"):
        assert procedure_url(url) is None


def test_parse_rows_procedure_page(fixtures):
    rows = parse_rows(page(fixtures, "details_231740.html"))
    files = [r for r in rows if not r.is_folder]
    folders = [r for r in rows if r.is_folder]
    assert [(f.row_id, f.name) for f in files] == [("_2293370", "Ogłoszenie o zamówieniu.pdf")]
    assert files[0].download_url == "/repository/download/1791809602960nQb9zXYvIsMz7Q26o0z"
    assert [(f.folder, f.name) for f in folders] == [
        ("0002", "Kwota przeznaczona na sfinansowanie zamówienia"),
        ("0004", "Informacja z otwarcia ofert"),
        ("0003", "Informacje o wynikach"),
    ]
    assert folders[1].folder_url == f"{NOTICE_PATH}?folder=0004&"


def test_parse_rows_folder_page(fixtures):
    rows = [r for r in parse_rows(page(fixtures, "details_231740_folder0004.html"))
            if r.folder == "0004"]
    assert len(rows) == 1
    assert rows[0].row_id == "0004_4095893" and rows[0].item_id == "4095893"
    assert rows[0].name == OPENING_NAME
    assert rows[0].download_url == f"/repository/download/{OPENING_TOKEN}"


def test_bzp_procedure_link_enqueues_docs_job(db, fixtures):
    notice, out = upsert_procedure(db, fixtures)
    assert out["needs_opening"] is True
    job = db.execute("select * from jobs where kind='ezamawiajacy_docs'").fetchone()
    assert job["key"] == notice.ocds_id
    assert job["payload"] == {"ocds_id": notice.ocds_id, "url": DEMAND_URL}


def test_bzp_home_page_link_does_not_enqueue(db, fixtures):
    _, out = upsert_procedure(db, fixtures, url="https://zbkkrakow.ezamawiajacy.pl/")
    assert out["needs_opening"] is False
    assert db.execute("select count(*) c from jobs").fetchone()["c"] == 0


@respx.mock
def test_sync_and_download(db, fixtures, tmp_path):
    notice, _ = upsert_procedure(db, fixtures)
    route = respx.route(host=HOST).mock(side_effect=platform(fixtures))
    c = client()
    assert sync_documents(db, c, notice.ocds_id, DEMAND_URL) == 1
    assert sync_documents(db, c, notice.ocds_id) == 1  # url from procedures.platform_url
    docs = db.execute("select * from documents").fetchall()
    assert [(d["source"], d["source_doc_id"], d["file_name"], d["doc_type"]) for d in docs] == [
        ("ezamawiajacy", DOC_ID, OPENING_NAME, "opening")]
    jobs = db.execute("select * from jobs where kind = %s", (DOWNLOAD_JOB,)).fetchall()
    assert len(jobs) == 1 and jobs[0]["key"] == DOC_ID
    payload = jobs[0]["payload"]
    assert payload == {"ocds_id": notice.ocds_id, "doc_id": DOC_ID, "page_url": FOLDER_URL,
                       "row_id": "0004_4095893"}
    queries = [call.request.url.query.decode() for call in route.calls]
    assert not any("folder=0002" in q or "folder=0003" in q for q in queries)
    robots = [call for call in route.calls if call.request.url.path == "/robots.txt"]
    assert len(robots) == 1  # cached per host

    rs = RawStore(tmp_path)
    sha = download_document(db, c, rs, notice.ocds_id, DOC_ID, payload["page_url"],
                            payload["row_id"])
    post = route.calls.last.request
    assert post.method == "POST" and post.url.path == f"/repository/download/{OPENING_TOKEN}"
    assert route.calls[-2].request.url.query.decode() == "folder=0004&"  # fresh token first
    row = db.execute("select status, raw_sha256 from documents").fetchone()
    assert row["status"] == "downloaded" and row["raw_sha256"] == sha
    assert rs.get(sha, source="ezamawiajacy") == (fixtures / "openings" / "kielce_scan.pdf"
                                                  ).read_bytes()
    parse = db.execute("select * from jobs where kind = 'opening_parse'").fetchone()
    assert parse["key"] == DOC_ID


@respx.mock
def test_download_rejects_html_answer(db, fixtures, tmp_path):
    notice, _ = upsert_procedure(db, fixtures)
    html = httpx.Response(200, text="<HTML>Podany plik nie został znaleziony</HTML>",
                          headers={"Content-Type": "text/html;charset=utf-8"})
    respx.route(host=HOST).mock(side_effect=platform(fixtures, file_response=html))
    c = client()
    sync_documents(db, c, notice.ocds_id, DEMAND_URL)
    with pytest.raises(ValueError, match="HTML instead of a file"):
        download_document(db, c, RawStore(tmp_path), notice.ocds_id, DOC_ID, FOLDER_URL,
                          "0004_4095893")
    assert db.execute("select status from documents").fetchone()["status"] == "new"


@respx.mock
def test_robots_disallow_stops_sync(fixtures):
    route = respx.route(host=HOST).mock(
        side_effect=platform(fixtures, robots="User-agent: *\nDisallow: /pn/\n"))
    assert sync_documents(None, client(), "ocds-x", DEMAND_URL) == 0
    assert [call.request.url.path for call in route.calls] == ["/robots.txt"]


@respx.mock
def test_robots_disallow_marks_download_blocked(db, fixtures, tmp_path):
    notice, _ = upsert_procedure(db, fixtures)
    handler = platform(fixtures)
    route = respx.route(host=HOST).mock(side_effect=handler)
    c = client()
    sync_documents(db, c, notice.ocds_id, DEMAND_URL)
    ezamawiajacy._robots.clear()
    handler.robots = "User-agent: *\nDisallow: /repository/\n"
    assert download_document(db, c, RawStore(tmp_path), notice.ocds_id, DOC_ID, FOLDER_URL,
                             "0004_4095893") is None
    assert not any(call.request.method == "POST" for call in route.calls)
    row = db.execute("select status, error from documents").fetchone()
    assert row["status"] == "blocked" and "robots.txt" in row["error"]


@respx.mock
def test_robots_crawl_delay_raises_gap(fixtures):
    respx.route(host=HOST).mock(
        side_effect=platform(fixtures, robots="User-agent: *\nCrawl-delay: 30\n"))
    c = client()
    check_robots(c, NOTICE_URL)
    assert c.rates[HOST] == 30.0


@respx.mock
def test_robots_specific_agent_rules(fixtures):
    respx.route(host=HOST).mock(side_effect=platform(
        fixtures, robots="User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow:\n"))
    check_robots(client(), NOTICE_URL)  # our agent is not GPTBot: allowed
