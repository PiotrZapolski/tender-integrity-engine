"""Opening-of-offers documents from the Marketplanet platform (`*.ezamawiajacy.pl`).

Every buyer has its own subdomain. The public procedure page (no login) lists the procedure
documents and folders; the standard folder "Informacja z otwarcia ofert" holds the opening
documents (art. 222 ust. 5 Pzp). Download tokens on the page are bound to the HTTP session,
so a download first loads the page (the client keeps the session cookie) and then posts to
the file's `/repository/download/<token>` URL, as the "Pobierz" button does.

BZP field 1.6 often points only at the buyer's home page; only links to a procedure
(`/pn/<buyer>/demand/<id>/notice/public/details`) can be followed, see `procedure_url`.

robots.txt of every host is read before the first request to it and obeyed (Disallow,
Crawl-delay). On 2026-10-12 the subdomains had no robots.txt (404).

`sync_documents` stores opening/selection/cancellation documents in `documents`
(`source="ezamawiajacy"`) and queues `ezamawiajacy_download` for each opening document;
`download_document` stores the file in the raw store (folder `ezamawiajacy`) and queues
`opening_parse`.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import psycopg
from selectolax.lexbor import LexborHTMLParser

from tie.ezam.documents import classify
from tie.http import PoliteClient
from tie.jobs import enqueue
from tie.rawstore import RawStore

log = logging.getLogger(__name__)

PLATFORM = "ezamawiajacy"
DOMAIN = "ezamawiajacy.pl"
SOURCE = "ezamawiajacy"
RAW_SOURCE = "ezamawiajacy"
DOCS_JOB = "ezamawiajacy_docs"
DOWNLOAD_JOB = "ezamawiajacy_download"
PARSE_JOB = "opening_parse"
STORED_TYPES = ("opening", "selection", "cancellation")
# Minimal gap between two requests to any *.ezamawiajacy.pl host (one platform behind them).
MIN_GAP_S = 5.0
ROBOTS_TTL_S = 6 * 3600

_DEMAND_PATH = re.compile(r"/pn/[^/]+/demand/\d+/notice/public/details/?")
_NOTICE_PATH = re.compile(r"/pn/[^/]+/demand/notice/public/\d+/details/?")
_ROW_ID = re.compile(r"^(\w*)_(\d+)$")
_DATA_CALL = re.compile(r"""\$\("#([^"]+)"\)\.data\(\{(.*?)\}\)""", re.S)
_DATA_PAIR = re.compile(r"(\w+):\s*'([^']*)'")


class RobotsDisallowed(Exception):
    """robots.txt of the host does not allow our user agent to fetch the URL."""


@dataclass(frozen=True)
class Row:
    """One row of a document list: a file (`download_url`) or a folder (`folder_url`)."""

    row_id: str
    folder: str
    item_id: str
    name: str
    folder_url: str | None
    download_url: str | None

    @property
    def is_folder(self) -> bool:
        return self.folder_url is not None


def procedure_url(platform_url: str | None) -> str | None:
    """The public procedure page for a BZP field 1.6 URL, or None when it does not name one."""
    if not platform_url:
        return None
    u = urlparse(platform_url.strip())
    host = (u.hostname or "").lower()
    if not host.endswith("." + DOMAIN):
        return None
    if not (_DEMAND_PATH.fullmatch(u.path) or _NOTICE_PATH.fullmatch(u.path)):
        return None
    return f"https://{host}{u.path}"


def parse_rows(html: str) -> list[Row]:
    """File and folder rows of a procedure page, in page order.

    Rows without a positive numeric id (the "up" row, the generated notice) are skipped.
    """
    data: dict[str, dict[str, str]] = {}
    for m in _DATA_CALL.finditer(html):
        data[m.group(1)] = dict(_DATA_PAIR.findall(m.group(2)))
    rows: list[Row] = []
    for tr in LexborHTMLParser(html).css("tr.fileDataRow"):
        row_id = tr.attributes.get("id") or ""
        m = _ROW_ID.match(row_id)
        if not m:
            continue
        cell = tr.css_first("td.text.long")
        name = re.sub(r"\s+", " ", cell.text(separator=" ")).strip() if cell else ""
        d = data.get(row_id, {})
        folder_url = d.get("enterFolderUrl") or None
        download_url = None if folder_url else (d.get("downloadPostUrl") or None)
        if not (folder_url or download_url):
            continue
        rows.append(Row(row_id=row_id, folder=m.group(1), item_id=m.group(2), name=name,
                        folder_url=folder_url, download_url=download_url))
    return rows


_robots: dict[str, tuple[float, RobotFileParser]] = {}


def _robots_for(client: PoliteClient, host: str) -> RobotFileParser:
    cached = _robots.get(host)
    if cached is not None and time.monotonic() - cached[0] < ROBOTS_TTL_S:
        return cached[1]
    r = client.get(f"https://{host}/robots.txt")
    rp = RobotFileParser()
    if r.status_code == 200:
        rp.parse(r.text.splitlines())
    elif r.status_code in (401, 403):
        rp.disallow_all = True
    elif 400 <= r.status_code < 500:
        rp.allow_all = True
    else:
        raise RuntimeError(f"robots.txt of {host}: HTTP {r.status_code}")
    _robots[host] = (time.monotonic(), rp)
    return rp


def check_robots(client: PoliteClient, url: str) -> None:
    """Raise RobotsDisallowed unless robots.txt of the host allows `url`; apply Crawl-delay."""
    host = (urlparse(url).hostname or "").lower()
    agent = client.client.headers.get("User-Agent", "*")
    rp = _robots_for(client, host)
    if not rp.can_fetch(agent, url):
        raise RobotsDisallowed(url)
    delay = rp.crawl_delay(agent)
    if delay and float(delay) > client.rates.get(host, MIN_GAP_S):
        client.rates[host] = float(delay)


def _get(client: PoliteClient, url: str):
    check_robots(client, url)
    r = client.get(url)
    r.raise_for_status()
    return r


def _store(conn: psycopg.Connection, ocds_id: str, docs: list[tuple[str, str, str]]) -> None:
    try:
        for doc_id, file_name, doc_type in docs:
            conn.execute(
                "insert into documents(procedure_id, source, source_doc_id, file_name, doc_type)"
                " values (%s, %s, %s, %s, %s)"
                " on conflict (source, source_doc_id) do update set"
                "  file_name = excluded.file_name,"
                "  doc_type = excluded.doc_type,"
                "  updated_at = now()",
                (ocds_id, SOURCE, doc_id, file_name, doc_type),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def sync_documents(conn: psycopg.Connection, client: PoliteClient, ocds_id: str,
                   url: str | None = None) -> int:
    """Store the opening/selection/cancellation documents of a procedure; returns the number
    of opening documents.

    `url` is the BZP field 1.6 URL (default: `procedures.platform_url`). Files listed on the
    procedure page are classified by name; files in an opening folder are opening documents
    unless their name says selection or cancellation. For each opening document a
    `ezamawiajacy_download` job is queued (key: `source_doc_id`, `<host>:<file id>`).
    Upsert does not overwrite `status` or `raw_sha256`.
    """
    if url is None:
        row = conn.execute("select platform_url from procedures where ocds_id = %s",
                           (ocds_id,)).fetchone()
        conn.commit()
        url = row["platform_url"] if row else None
    page_url = procedure_url(url)
    if page_url is None:
        log.info("ezamawiajacy %s: no procedure page in %r", ocds_id, url)
        return 0
    try:
        r = _get(client, page_url)
    except RobotsDisallowed:
        log.warning("ezamawiajacy %s: robots.txt disallows %s", ocds_id, page_url)
        return 0
    page_url = str(r.url)
    host = (urlparse(page_url).hostname or "").lower()

    found: list[tuple[Row, str, str]] = []  # (row, page it is listed on, doc_type)
    for row in parse_rows(r.text):
        if not row.is_folder:
            found.append((row, page_url, classify(row.name, row.name)))
        elif classify(row.name, "") == "opening":
            folder_page = urljoin(page_url, row.folder_url)
            try:
                fr = _get(client, folder_page)
            except RobotsDisallowed:
                log.warning("ezamawiajacy %s: robots.txt disallows %s", ocds_id, folder_page)
                continue
            for f in parse_rows(fr.text):
                if f.is_folder or f.folder != row.folder:
                    continue
                doc_type = classify(f.name, f.name)
                found.append((f, folder_page, "opening" if doc_type == "other" else doc_type))

    docs: dict[str, tuple[Row, str, str]] = {}
    for row, listed_on, doc_type in found:
        if doc_type in STORED_TYPES:
            docs[f"{host}:{row.item_id}"] = (row, listed_on, doc_type)
    _store(conn, ocds_id, [(doc_id, row.name, t) for doc_id, (row, _, t) in docs.items()])

    openings = 0
    for doc_id, (row, listed_on, doc_type) in docs.items():
        if doc_type != "opening":
            continue
        openings += 1
        enqueue(conn, DOWNLOAD_JOB, doc_id, {"ocds_id": ocds_id, "doc_id": doc_id,
                                              "page_url": listed_on, "row_id": row.row_id})
    return openings


def download_document(
    conn: psycopg.Connection,
    client: PoliteClient,
    rawstore: RawStore,
    ocds_id: str,
    doc_id: str,
    page_url: str,
    row_id: str,
) -> str | None:
    """Download one document, store it raw and queue `opening_parse`; returns the sha256.

    Loads `page_url` for a fresh session-bound token of row `row_id`, then posts to it.
    Returns None (document status `blocked`) when robots.txt disallows the page or the file.
    """
    row = conn.execute(
        "select id from documents where source = %s and source_doc_id = %s",
        (SOURCE, doc_id),
    ).fetchone()
    conn.commit()
    if row is None:
        raise LookupError(f"document {doc_id} not found")
    document_id = row["id"]

    try:
        page = _get(client, page_url)
        match = [r for r in parse_rows(page.text) if r.row_id == row_id and not r.is_folder]
        if not match:
            raise LookupError(f"document {doc_id}: row {row_id} not on {page_url}")
        file_url = urljoin(str(page.url), match[0].download_url)
        check_robots(client, file_url)
    except RobotsDisallowed as e:
        conn.execute(
            "update documents set status = 'blocked', error = %s, updated_at = now()"
            " where id = %s",
            (f"robots.txt disallows {e}", document_id),
        )
        conn.commit()
        log.warning("ezamawiajacy %s: robots.txt disallows %s", doc_id, e)
        return None

    r = client.request("POST", file_url)
    r.raise_for_status()
    # A token the server does not know gives HTTP 200 with an HTML "file not found" page.
    if r.headers.get("content-type", "").startswith("text/html"):
        raise ValueError(f"document {doc_id}: HTML instead of a file from {file_url}")
    if not r.content:
        raise ValueError(f"empty file for document {doc_id}")
    sha = rawstore.put(r.content, source=RAW_SOURCE)

    try:
        conn.execute(
            "update documents set raw_sha256 = %s, status = 'downloaded', error = null,"
            " updated_at = now() where id = %s",
            (sha, document_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    enqueue(conn, PARSE_JOB, doc_id, {"document_id": document_id})
    return sha
