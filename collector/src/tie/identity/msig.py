"""Board links of bidders from MSiG (Monitor Sądowy i Gospodarczy) announcements.

MSiG announcements of KRS entries (type B, chapter "XV. WPISY DO KRAJOWEGO REJESTRU
SĄDOWEGO") quote the entry text with names and PESEL of board members, which the official
KRS API masks. For each bidder with a KRS number we list its announcements, fetch the text of
the new ones and parse the sections:

- Dz. 1 Rub. 7 (wspólnicy) -> role `shareholder`,
- Dz. 2 Rub. 1 (organ uprawniony do reprezentacji) -> role `board`,
- Dz. 2 Rub. 2 (organ nadzoru) -> role `supervisory`,
- Dz. 2 Rub. 3 (prokurenci) -> role `proxy`,

with the operations `wpisać` (add), `wykreślić` (remove) and `zmienić` or a
"(dla pozycji: ...)" reference (the member stays, one of their fields changes). Entries
without a valid PESEL (missing, bad checksum or date) are skipped and counted.

Privacy (binding): names and PESEL live only in memory while an announcement is parsed.
Nothing raw is stored, logged or put into an exception message. A person is stored as
`person_key` = HMAC-SHA256(PERSON_HMAC_SECRET, PESEL) with the birth year and the role; the
name is used only to recognise an entry while parsing. Research notes, formats and the
completeness caveat: docs/msig.md.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import psycopg

from tie.http import PoliteClient
from tie.jobs import enqueue

log = logging.getLogger(__name__)

API = "https://wyszukiwarka-msig.ms.gov.pl/api/Monitor"
SEARCH_URL = f"{API}/Search"
DETAILS_URL = f"{API}/Detalis"  # sic: the endpoint name has this typo
# Search ignores `pageSize` and always returns pages of 50; consecutive pages overlap by one
# item, so ids are deduplicated and paging stops at a short page or one with nothing new.
PAGE_SIZE = 50
MAX_PAGES = 400
HISTORY_START = date(2001, 1, 1)
# One msig_board job works at most this long, then queues a continuation for the same bidder.
MSIG_TIME_LIMIT_S = 900
# MSiG stopped carrying KRS entries on 2025-11-29 (docs/msig.md): re-checks rarely find
# anything new, so they are rare.
RECHECK_DAYS = 180
JOB_KIND = "msig_board"

ROLES = {(1, 7): "shareholder", (2, 1): "board", (2, 2): "supervisory", (2, 3): "proxy"}

_LETTERS = "A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźż"
_SECTION_RE = re.compile(
    rf"(?<![{_LETTERS}])(?:Dz\.\s*(\d{{1,2}})\s*\.?\s*)?Rub\.\s*(\d{{1,2}})(?!\d)")
_OP_RE = re.compile(rf"(?<![{_LETTERS}])(wpisać|wykreślić|zmienić)(?![{_LETTERS}])")
_OPS = {"wpisać": "add", "wykreślić": "remove", "zmienić": "keep"}
# "1. NAZWISKO 2. IMIONA 3. PESEL": names never contain digits or parentheses, which keeps a
# match inside one entry; the PESEL may be split by an OCR space.
_PERSON_RE = re.compile(
    r"(?<![\d.])1\.\s*([^\d()]+?)\s+2\.\s*([^\d()]+?)\s+3\.\s*((?:\d\s?){10}\d)(?!\d)")
# The same entry shape with no PESEL after "3." (or no field 3 at all): counted, not stored.
_NO_PESEL_RE = re.compile(
    r"(?<![\d.])1\.\s*([^\d()]+?)\s+2\.\s*([^\d()]+?)\s+(?:3\.\s*(?=[^\d\s])|[4-6]\.)")
_FOR_POSITION_RE = re.compile(r"dla\s+pozycji\s*:?\s*$")
_ENTRY_DATE_RE = re.compile(r"W\s*dniu\s*([\d\s.]{8,16}?)\s*dokonano")
_ENTRY_NO_RE = re.compile(r"rejestru\s+KRS\s+nr\s*(\d+)")
_PESEL_WEIGHTS = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)
# PESEL month offset -> century.
_CENTURY = {0: 1900, 20: 2000, 40: 2100, 60: 2200, 80: 1800}


class MsigError(Exception):
    pass


@dataclass(frozen=True)
class PersonEntry:
    role: str
    op: str  # add | remove | keep
    # Raw personal data: memory only, never in repr, logs or the database.
    surname: str = field(repr=False)
    given: str = field(repr=False)
    pesel: str = field(repr=False)


@dataclass
class Announcement:
    source_id: str
    published: date
    entry_date: date
    entry_no: int | None
    persons: list[PersonEntry]
    # Person entries in a parsed section skipped for lack of a valid PESEL.
    no_valid_pesel: int = 0


# --- PESEL, names, keys ---------------------------------------------------------------


def _pesel_date(pesel: str) -> date | None:
    yy, mm, dd = int(pesel[0:2]), int(pesel[2:4]), int(pesel[4:6])
    offset = (mm - 1) // 20 * 20 if mm else -1
    century = _CENTURY.get(offset)
    if century is None:
        return None
    try:
        return date(century + yy, mm - offset, dd)
    except ValueError:
        return None


def valid_pesel(pesel: str | None) -> bool:
    if not pesel or not re.fullmatch(r"\d{11}", pesel):
        return False
    total = sum(int(d) * w for d, w in zip(pesel, _PESEL_WEIGHTS))
    if (10 - total % 10) % 10 != int(pesel[10]):
        return False
    return _pesel_date(pesel) is not None


def birth_year(pesel: str) -> int:
    born = _pesel_date(pesel)
    if born is None:
        raise ValueError("invalid PESEL date")
    return born.year


def person_key(secret: str | None, pesel: str) -> str:
    """HMAC of the PESEL alone: unique and stable, unlike names (surname changes, OCR)."""
    if not secret:
        raise ValueError("PERSON_HMAC_SECRET is required to compute person keys")
    if not valid_pesel(pesel):
        raise ValueError("invalid PESEL")
    return hmac.new(secret.encode(), pesel.encode(), hashlib.sha256).hexdigest()


# --- parser ---------------------------------------------------------------------------


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\u00a0", " "))


def _clean_name(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" .,;:-")


def _entry_date(body: str) -> date | None:
    m = _ENTRY_DATE_RE.search(body)
    if not m:
        return None
    try:
        return datetime.strptime(re.sub(r"\s", "", m.group(1)), "%d.%m.%Y").date()
    except ValueError:
        return None


def _sections(body: str) -> list[tuple[int, int | None, int]]:
    """(start offset, Dz., Rub.) of every section; a bare "Rub. N" keeps the last Dz."""
    out: list[tuple[int, int | None, int]] = []
    dz: int | None = None
    for m in _SECTION_RE.finditer(body):
        if m.group(1):
            dz = int(m.group(1))
        out.append((m.start(), dz, int(m.group(2))))
    return out


def _section_at(sections: list[tuple[int, int | None, int]],
                pos: int) -> tuple[int, str] | None:
    """(section start, role) of the parsed section containing `pos`, else None."""
    current = None
    for start, dz, rub in sections:
        if start > pos:
            break
        current = (start, dz, rub)
    if current is None or (current[1], current[2]) not in ROLES:
        return None
    return current[0], ROLES[(current[1], current[2])]


def _is_person_name(surname: str, given: str) -> bool:
    # Uppercase only (lowercase means prose such as addresses), and the given names must have
    # letters (a company shareholder has "2. ********" or similar).
    return (bool(surname) and bool(re.search(r"[A-ZĄĆĘŁŃÓŚŹŻ]", given))
            and not re.search(r"[a-ząćęłńóśźż]", surname + given))


def _persons(body: str) -> tuple[list[PersonEntry], int]:
    sections = _sections(body)
    persons: list[PersonEntry] = []
    skipped = 0
    for m in _NO_PESEL_RE.finditer(body):
        if (_section_at(sections, m.start()) is not None
                and _is_person_name(_clean_name(m.group(1)), _clean_name(m.group(2)))):
            skipped += 1
    for m in _PERSON_RE.finditer(body):
        found = _section_at(sections, m.start())
        if found is None:
            continue
        section_start, role = found
        surname, given = _clean_name(m.group(1)), _clean_name(m.group(2))
        if not _is_person_name(surname, given):
            continue
        pesel = re.sub(r"\s", "", m.group(3))
        if not valid_pesel(pesel):
            skipped += 1
            continue
        prefix = body[section_start:m.start()]
        ops = list(_OP_RE.finditer(prefix))
        op = _OPS[ops[-1].group(1)] if ops else "add"
        if _FOR_POSITION_RE.search(prefix.rstrip(" (")):
            op = "keep"
        persons.append(PersonEntry(role, op, surname, given, pesel))
    return persons, skipped


def parse_announcement(doc: dict) -> Announcement:
    """Parse a Detalis response into board events; raw values stay inside the result."""
    body = _normalize_text(doc.get("textInBody") or "")
    published = date.fromisoformat(str(doc["dateOfPublication"])[:10])
    entry_no = _ENTRY_NO_RE.search(body)
    persons, skipped = _persons(body)
    return Announcement(
        source_id=str(doc["id"]),
        published=published,
        entry_date=_entry_date(body) or published,
        entry_no=int(entry_no.group(1)) if entry_no else None,
        persons=persons,
        no_valid_pesel=skipped,
    )


# --- HTTP -----------------------------------------------------------------------------


def _get_json(client: PoliteClient, url: str, params: dict, what: str):
    r = client.get(url, params=params)
    if r.status_code != 200:
        # Never include the body: it may quote personal data.
        raise MsigError(f"MSiG HTTP {r.status_code} for {what}")
    try:
        return r.json()
    except ValueError as exc:
        raise MsigError(f"MSiG returned no JSON for {what}") from exc


def _search_page(client: PoliteClient, krs: str, since: date, until: date,
                 page: int) -> list[dict]:
    data = _get_json(client, SEARCH_URL, {
        "krs": krs, "signatureType": "B", "from": since.isoformat(),
        "to": until.isoformat(), "page": page,
    }, f"search krs={krs} page={page}")
    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise MsigError(f"MSiG search krs={krs} page={page}: no list")
    return items


def _pages(client: PoliteClient, krs: str, since: date, until: date):
    """Yield the new (not seen on earlier pages) items of each page."""
    seen: set[int] = set()
    for page in range(1, MAX_PAGES + 1):
        items = _search_page(client, krs, since, until, page)
        new = [it for it in items if it.get("id") is not None and it["id"] not in seen]
        seen.update(it["id"] for it in new)
        if new:
            yield new
        if not new or len(items) < PAGE_SIZE:
            return
    log.warning("msig: krs %s has more than %d search pages", krs, MAX_PAGES)


def search_announcements(client: PoliteClient, krs: str, since: date,
                         until: date) -> list[dict]:
    return [it for page in _pages(client, krs, since, until) for it in page]


def fetch_details(client: PoliteClient, source_id: str) -> dict:
    data = _get_json(client, DETAILS_URL, {"Id": source_id}, f"details id={source_id}")
    if not isinstance(data, dict) or "dateOfPublication" not in data:
        raise MsigError(f"MSiG details id={source_id}: unexpected response")
    data.setdefault("id", source_id)
    return data


# --- storage --------------------------------------------------------------------------


def apply_announcement(conn: psycopg.Connection, bidder_id: int, ann: Announcement,
                       secret: str) -> None:
    """Apply the events of one announcement (in the caller's transaction)."""
    day = ann.entry_date
    for p in ann.persons:
        key = person_key(secret, p.pesel)
        open_row = conn.execute(
            "select valid_from from board_links where bidder_id = %s and person_key = %s"
            " and role = %s and valid_to is null order by valid_from desc limit 1",
            (bidder_id, key, p.role)).fetchone()
        if p.op in ("add", "keep"):
            if open_row is None:
                conn.execute(
                    "insert into board_links(bidder_id, person_key, role, birth_year,"
                    " valid_from, source_id) values (%s, %s, %s, %s, %s, %s)"
                    " on conflict do nothing",
                    (bidder_id, key, p.role, birth_year(p.pesel), day, ann.source_id))
        elif open_row is not None:
            conn.execute(
                "update board_links set valid_to = greatest(%s, valid_from)"
                " where bidder_id = %s and person_key = %s and role = %s and valid_from = %s",
                (day, bidder_id, key, p.role, open_row["valid_from"]))
        else:
            # Removed, but no earlier announcement listed the person (e.g. the first entry
            # predates MSiG coverage): all we know is membership on the removal day.
            conn.execute(
                "insert into board_links(bidder_id, person_key, role, birth_year, valid_from,"
                " valid_to, source_id) values (%s, %s, %s, %s, %s, %s, %s)"
                " on conflict do nothing",
                (bidder_id, key, p.role, birth_year(p.pesel), day, day, ann.source_id))
    conn.execute(
        "insert into msig_announcements(bidder_id, source_id, published, entry_date, n_persons)"
        " values (%s, %s, %s, %s, %s) on conflict do nothing",
        (bidder_id, ann.source_id, ann.published, ann.entry_date, len(ann.persons)))


def _mark_checked(conn: psycopg.Connection, bidder_id: int) -> None:
    conn.execute("update bidders set msig_checked_at = now() where id = %s", (bidder_id,))
    conn.commit()


def sync_bidder(conn: psycopg.Connection, client: PoliteClient, bidder_id: int,
                secret: str, *, today: date, deadline: float | None = None) -> bool:
    """Process new MSiG announcements of one bidder; False when stopped at `deadline`.

    Each search page is fetched, parsed and committed as a unit, in entry order, together
    with its `msig_announcements` rows, so a continuation resumes where this run stopped.
    """
    if not secret:
        raise ValueError("PERSON_HMAC_SECRET is required")
    row = conn.execute("select krs from bidders where id = %s", (bidder_id,)).fetchone()
    krs = (row["krs"] or "").strip() if row else ""
    conn.commit()
    if not re.fullmatch(r"\d{10}", krs):
        if row is not None:
            _mark_checked(conn, bidder_id)
        return True
    done_rows = conn.execute(
        "select source_id, published from msig_announcements where bidder_id = %s",
        (bidder_id,)).fetchall()
    conn.commit()
    done = {r["source_id"] for r in done_rows}
    since = max((r["published"] for r in done_rows), default=HISTORY_START)
    processed = persons = skipped = 0
    for items in _pages(client, krs, since, today):
        todo = [str(it["id"]) for it in items if str(it["id"]) not in done]
        anns = [parse_announcement(fetch_details(client, sid)) for sid in todo]
        anns.sort(key=lambda a: (a.entry_date, a.entry_no or 0, int(a.source_id)))
        try:
            for ann in anns:
                apply_announcement(conn, bidder_id, ann, secret)
                persons += len(ann.persons)
                skipped += ann.no_valid_pesel
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        done.update(todo)
        processed += len(anns)
        if deadline is not None and time.monotonic() >= deadline:
            log.info("msig: bidder %s stopped at the time limit after %d announcements",
                     bidder_id, processed)
            return False
    _mark_checked(conn, bidder_id)
    log.info("msig: bidder %s: %d new announcements, %d person entries,"
             " %d entries skipped without a valid PESEL", bidder_id, processed, persons, skipped)
    return True


def run_job(conn: psycopg.Connection, client: PoliteClient, secret: str | None,
            payload: dict) -> None:
    """Handler body of the `msig_board` job (payload: {"bidder_id": int})."""
    if not secret:
        raise RuntimeError("PERSON_HMAC_SECRET not set")
    bidder_id = int(payload["bidder_id"])
    deadline = time.monotonic() + MSIG_TIME_LIMIT_S
    now = datetime.now(UTC)
    if not sync_bidder(conn, client, bidder_id, secret, today=now.date(), deadline=deadline):
        key = f"{bidder_id}@more@{now.isoformat()}"
        enqueue(conn, JOB_KIND, key, {"bidder_id": bidder_id})
        log.info("msig: continuation %s queued", key)


def enqueue_due(conn: psycopg.Connection, today: date, limit: int,
                recheck_days: int = RECHECK_DAYS) -> int:
    """Queue msig_board jobs for bidders with a KRS not checked in `recheck_days` days."""
    rows = conn.execute(
        "select b.id from bidders b"
        " where b.krs is not null"
        " and (b.msig_checked_at is null"
        f"      or b.msig_checked_at < now() - interval '{int(recheck_days)} days')"
        " and not exists (select 1 from jobs j where j.kind = %s"
        "   and j.status in ('queued', 'running') and j.payload->>'bidder_id' = b.id::text)"
        " order by b.msig_checked_at nulls first,"
        " exists (select 1 from bids x where x.bidder_id = b.id) desc, b.id"
        " limit %s",
        (JOB_KIND, limit)).fetchall()
    conn.commit()
    return sum(enqueue(conn, JOB_KIND, f"{r['id']}@{today.isoformat()}", {"bidder_id": r["id"]})
               for r in rows)
