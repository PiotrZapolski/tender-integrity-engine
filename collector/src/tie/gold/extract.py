"""UOKiK decision PDF -> structured bid-rigging case (the validation gold set).

`tie gold-extract` downloads each decision from `tie.gold.sources.UOKIK_DECISIONS` (raw copy in
RawStore, source "gold"), extracts its text, and asks DeepSeek for strict JSON: the colluding
firms, every tender described with all bids and prices, and the collusion pattern. Decisions
are long (20-120 pages), so the text is split into overlapping chunks, each chunk is extracted
on its own and the results are merged (tenders by buyer + subject, bids by bidder). The case
is validated with pydantic and stored in `gold_cases`; `tie gold-export` writes all cases to
one JSON file (validation/gold/uokik.json).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError, field_serializer, field_validator

from tie.ai.providers import BudgetExceeded
from tie.gold.sources import Decision
from tie.http import PoliteClient
from tie.identity.names import normalize_name
from tie.money import clean_nip
from tie.openings.extract import extract_text
from tie.openings.llm import _to_decimal
from tie.rawstore import RawStore

log = logging.getLogger(__name__)

TASK = "gold_extract"
RAW_SOURCE = "gold"
# About 4k tokens of Polish text per call: the answer (all bids of all tenders in the chunk)
# stays well below the model's output limit.
CHUNK_CHARS = 12000
# Offer tables cut by a chunk border appear whole in one of the two chunks.
CHUNK_OVERLAP = 1500
MAX_NOTES = 4000
# Polite gap in seconds between two downloads from the UOKiK servers.
GOLD_RATES: dict[str, float] = {"decyzje.uokik.gov.pl": 2.0, "uokik.gov.pl": 2.0}
EXPORT_SOURCE = ("UOKiK decisions on bid rigging (art. 6 ust. 1 pkt 7 uokik),"
                 " structured by tie gold-extract")

PATTERNS = ("rotation", "cover_bids", "market_split", "bid_suppression", "other")
Pattern = Literal["rotation", "cover_bids", "market_split", "bid_suppression", "other"]

SYSTEM_PROMPT = """Jesteś analitykiem decyzji Prezesa UOKiK w sprawach zmów przetargowych (art. 6 ust. 1 pkt 7 uokik).
Dostajesz fragment jednej decyzji. Zwróć wyłącznie JSON: {"decision": tekst, "year": liczba,
"colluders": [{"name": tekst, "nip": tekst lub null}], "tenders": [{"buyer": tekst,
"subject": tekst, "date": "RRRR-MM-DD" lub null, "year": liczba lub null, "bids": [{"bidder": tekst,
"price": liczba lub null, "won": true lub false}], "rigged": true lub false}],
"pattern": ["rotation" | "cover_bids" | "market_split" | "bid_suppression" | "other"], "notes": tekst}.
Zasady: tenders to tylko postępowania opisane w tym fragmencie; buyer to zamawiający, subject to
przedmiot zamówienia (z numerem części, jeśli oferty dotyczą części); bids to wszystkie oferty
złożone w postępowaniu, także firm spoza zmowy; price to cena brutto oferty jako liczba z kropką
dziesiętną; won true tylko dla oferty, z którą zawarto umowę (po rezygnacji tańszej oferty jest to
oferta droższa); rigged true, gdy decyzja ustala, że w tym postępowaniu doszło do zmowy;
colluders to przedsiębiorcy uznani w decyzji za uczestników zmowy; nip tylko, gdy jest w tekście;
date to data ogłoszenia postępowania lub złożenia ofert; pattern: rotation (wygrywanie na zmianę),
cover_bids (oferty pozorne), market_split (podział zamówień lub rynku), bid_suppression (wycofanie
oferty, nieuzupełnienie dokumentów lub niezłożenie oferty), other (inny mechanizm); notes to jedno
lub dwa zdania o mechanizmie zmowy opisanym w tym fragmencie. Gdy fragment nie opisuje przetargów
ani uczestników, zwróć puste listy. Nie zgaduj brakujących danych."""


class GoldExtractError(RuntimeError):
    """A decision could not be turned into a case (download, text or model answer)."""


class TextProvider(Protocol):
    def complete_json(self, *, task: str, system: str, user: str) -> dict: ...


def _collapse(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def _lenient_int(value: Any) -> int | None:
    """Year from the model: 2019, "2019" or "2019 r."; anything else becomes None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        m = re.search(r"\b(\d{4})\b", value)
        return int(m.group(1)) if m else None
    return None


class GoldColluder(BaseModel):
    name: str
    nip: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: Any) -> Any:
        value = _collapse(value)
        if value == "":
            raise ValueError("empty colluder name")
        return value

    @field_validator("nip", mode="before")
    @classmethod
    def _nip(cls, value: Any) -> str | None:
        if value is None or value == "":
            return None
        return clean_nip(str(value))


class GoldBid(BaseModel):
    bidder: str
    price: Decimal | None = None
    won: bool = False

    @field_validator("bidder", mode="before")
    @classmethod
    def _bidder(cls, value: Any) -> Any:
        value = _collapse(value)
        if value == "":
            raise ValueError("empty bidder")
        return value

    @field_validator("price", mode="before")
    @classmethod
    def _price(cls, value: Any) -> Decimal | None:
        return _to_decimal(value)

    @field_validator("won", mode="before")
    @classmethod
    def _won(cls, value: Any) -> Any:
        return False if value is None else value

    @field_serializer("price", when_used="json")
    def _price_json(self, value: Decimal | None) -> float | None:
        # JSON number in the stored payload and the export; Decimal stays in Python.
        return None if value is None else float(value)


class GoldTender(BaseModel):
    buyer: str = ""
    subject: str = ""
    date: str | None = None
    year: int | None = None
    bids: list[GoldBid] = Field(default_factory=list)
    rigged: bool = False

    @field_validator("buyer", "subject", mode="before")
    @classmethod
    def _text(cls, value: Any) -> Any:
        return "" if value is None else _collapse(value)

    @field_validator("date", mode="before")
    @classmethod
    def _date(cls, value: Any) -> str | None:
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    @field_validator("year", mode="before")
    @classmethod
    def _year(cls, value: Any) -> int | None:
        return _lenient_int(value)

    @field_validator("bids", mode="before")
    @classmethod
    def _bids(cls, value: Any) -> Any:
        return [] if value is None else value

    @field_validator("rigged", mode="before")
    @classmethod
    def _rigged(cls, value: Any) -> Any:
        return False if value is None else value


class GoldExtraction(BaseModel):
    """What the model returns for one chunk of a decision."""

    decision: str | None = None
    year: int | None = None
    colluders: list[GoldColluder] = Field(default_factory=list)
    tenders: list[GoldTender] = Field(default_factory=list)
    pattern: list[Pattern] = Field(default_factory=list)
    notes: str = ""

    @field_validator("year", mode="before")
    @classmethod
    def _year(cls, value: Any) -> int | None:
        return _lenient_int(value)

    @field_validator("colluders", "tenders", mode="before")
    @classmethod
    def _lists(cls, value: Any) -> Any:
        return [] if value is None else value

    @field_validator("pattern", mode="before")
    @classmethod
    def _pattern(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return value  # let pydantic reject it
        out: list[str] = []
        for item in value:
            name = str(item).strip().lower()
            name = name if name in PATTERNS else "other"
            if name not in out:
                out.append(name)
        return out

    @field_validator("notes", mode="before")
    @classmethod
    def _notes(cls, value: Any) -> str:
        return "" if value is None else " ".join(str(value).split())


class GoldCase(GoldExtraction):
    """One decision, merged from all chunks; `decision` and `year` come from the source list.

    `source` holds provenance added by the pipeline (URL, sector, region, raw sha256, text
    method, number of chunks and the chunks whose answer was invalid)."""

    decision: str
    year: int
    source: dict[str, Any] | None = None


def compact_text(text: str) -> str:
    """Fewer tokens, same content: runs of spaces become two (columns stay apart), page
    breaks become newlines, at most one empty line in a row."""
    lines = [re.sub(r"[ \t]{2,}", "  ", line).rstrip()
             for line in (text or "").replace("\f", "\n").splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


def chunk_text(text: str, max_chars: int = CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split `text` into chunks of at most `max_chars`, cut at line ends where possible.

    Each chunk after the first starts at a line beginning inside the last `overlap` characters
    of the previous chunk, so a table cut by the border is complete in one of them. A line
    longer than the room left is cut hard. Blank text gives no chunks.
    """
    if overlap >= max_chars:
        raise ValueError("overlap must be smaller than max_chars")
    text = text or ""
    if not text.strip():
        return []
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start, n = 0, len(text)
    while True:
        end = min(start + max_chars, n)
        if end < n:
            # Cut after the last newline, but past the overlap so the next start moves forward.
            cut = text.rfind("\n", start + overlap + 1, end)
            if cut != -1:
                end = cut + 1
        chunks.append(text[start:end])
        if end >= n:
            return chunks
        nxt = end - overlap
        newline = text.find("\n", nxt, end - 1)
        start = newline + 1 if newline != -1 else nxt


def _user_message(decision: Decision, i: int, n: int, chunk: str) -> str:
    return (f"Decyzja Prezesa UOKiK nr {decision.id} ({decision.year}), fragment {i}/{n}.\n\n"
            f"{chunk}")


def _merge_bid(bids: list[GoldBid], bid: GoldBid) -> None:
    """Same bidder (normalized name) with a compatible price is one offer; a second, different
    price of the same bidder is kept as another offer (for example another lot)."""
    key = normalize_name(bid.bidder)
    for existing in bids:
        if normalize_name(existing.bidder) != key:
            continue
        if existing.price is None or bid.price is None or existing.price == bid.price:
            if existing.price is None:
                existing.price = bid.price
            existing.won = existing.won or bid.won
            return
    bids.append(bid.model_copy())


def merge_extractions(decision: Decision, parts: list[GoldExtraction]) -> GoldCase:
    colluders: list[GoldColluder] = []
    colluder_by_key: dict[str, GoldColluder] = {}
    tenders: list[GoldTender] = []
    tender_by_key: dict[tuple[str, str], GoldTender] = {}
    patterns: list[str] = []
    notes: list[str] = []
    for part in parts:
        for colluder in part.colluders:
            key = normalize_name(colluder.name)
            known = colluder_by_key.get(key)
            if known is None:
                known = colluder.model_copy()
                colluders.append(known)
                colluder_by_key[key] = known
            elif known.nip is None and colluder.nip:
                known.nip = colluder.nip
        for tender in part.tenders:
            key = (normalize_name(tender.buyer), normalize_name(tender.subject))
            target = tender_by_key.get(key)
            if target is None:
                target = tender.model_copy(update={"bids": []})
                tenders.append(target)
                tender_by_key[key] = target
            target.rigged = target.rigged or tender.rigged
            target.date = target.date or tender.date
            if target.year is None:
                target.year = tender.year
            for bid in tender.bids:
                _merge_bid(target.bids, bid)
        for pattern in part.pattern:
            if pattern not in patterns:
                patterns.append(pattern)
        if part.notes and part.notes not in notes:
            notes.append(part.notes)
    return GoldCase(decision=decision.id, year=decision.year, colluders=colluders,
                    tenders=tenders, pattern=patterns, notes=" ".join(notes)[:MAX_NOTES])


def extract_case(provider: TextProvider, decision: Decision, text: str, *,
                 max_chars: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> GoldCase:
    """Structured case from the decision text, one model call per chunk.

    A chunk whose answer is invalid (pydantic) or unusable (provider error) is skipped and
    listed in `source["failed_chunks"]`; when no chunk succeeds GoldExtractError is raised.
    BudgetExceeded propagates unchanged."""
    chunks = chunk_text(compact_text(text), max_chars=max_chars, overlap=overlap)
    if not chunks:
        raise GoldExtractError(f"{decision.id}: the decision has no text")
    parts: list[GoldExtraction] = []
    failed: list[int] = []
    for i, chunk in enumerate(chunks, start=1):
        try:
            payload = provider.complete_json(task=TASK, system=SYSTEM_PROMPT,
                                             user=_user_message(decision, i, len(chunks), chunk))
            parts.append(GoldExtraction.model_validate(payload))
        except BudgetExceeded:
            raise
        except (ValidationError, RuntimeError) as e:
            failed.append(i)
            log.warning("gold %s: chunk %d/%d skipped: %s", decision.id, i, len(chunks),
                        str(e)[:300])
    if not parts:
        raise GoldExtractError(f"{decision.id}: no valid answer for any of {len(chunks)} chunks")
    case = merge_extractions(decision, parts)
    case.source = {"url": decision.url, "sector": decision.sector, "region": decision.region,
                   "chunks": len(chunks), "failed_chunks": failed}
    return case


def download_decision(client: PoliteClient, rawstore: RawStore,
                      decision: Decision) -> tuple[bytes, str]:
    """PDF bytes and their sha256; the raw file is kept in RawStore under source "gold"."""
    r = client.get(decision.url)
    if r.status_code != 200:
        raise GoldExtractError(f"{decision.id}: HTTP {r.status_code} for {decision.url}")
    data = r.content
    if not data.startswith(b"%PDF"):
        raise GoldExtractError(f"{decision.id}: the response is not a PDF")
    return data, rawstore.put(data, source=RAW_SOURCE)


def store_case(conn: psycopg.Connection, case: GoldCase) -> None:
    conn.execute(
        "insert into gold_cases(decision, payload) values (%s, %s)"
        " on conflict (decision) do update set payload = excluded.payload, extracted_at = now()",
        (case.decision, Jsonb(case.model_dump(mode="json"))))
    conn.commit()


def load_cases(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute("select payload from gold_cases order by decision").fetchall()
    conn.commit()
    return [r["payload"] for r in rows]


def extract_decision(conn: psycopg.Connection, client: PoliteClient, rawstore: RawStore,
                     provider: TextProvider, decision: Decision) -> GoldCase:
    data, sha = download_decision(client, rawstore, decision)
    extracted = extract_text(data, "decision.pdf")
    case = extract_case(provider, decision, extracted.text)
    case.source.update({"sha256": sha, "text_method": extracted.method})
    store_case(conn, case)
    return case


def run_gold_extract(conn: psycopg.Connection, client: PoliteClient, rawstore: RawStore,
                     provider: TextProvider, decisions, *, force: bool = False) -> dict:
    """Extract every decision not yet in `gold_cases` (all of them with `force`).

    A failing decision is logged and the run goes on; the AI daily budget stops the run
    (`budget_exceeded`), the remaining decisions are picked up by the next run."""
    if force:
        done: set[str] = set()
    else:
        done = {r["decision"] for r in conn.execute("select decision from gold_cases")}
        conn.commit()
    summary: dict[str, Any] = {"extracted": [], "skipped": [], "failed": {},
                               "budget_exceeded": False}
    for decision in decisions:
        if decision.id in done:
            summary["skipped"].append(decision.id)
            continue
        try:
            case = extract_decision(conn, client, rawstore, provider, decision)
        except BudgetExceeded as e:
            log.warning("gold-extract: %s, stopping before %s", e, decision.id)
            summary["budget_exceeded"] = True
            break
        except (psycopg.OperationalError, psycopg.InterfaceError):
            raise  # the database is gone
        except Exception as e:
            conn.rollback()
            log.exception("gold-extract: %s failed", decision.id)
            summary["failed"][decision.id] = f"{type(e).__name__}: {e}"[:500]
            continue
        log.info("gold-extract: %s tenders=%d colluders=%d chunks=%d failed_chunks=%s",
                 decision.id, len(case.tenders), len(case.colluders), case.source["chunks"],
                 case.source["failed_chunks"])
        summary["extracted"].append(decision.id)
    return summary


def export_gold(conn: psycopg.Connection, path: Path) -> int:
    """Write all stored cases to `path` as one UTF-8 JSON document; returns the count."""
    cases = load_cases(conn)
    doc = {"source": EXPORT_SOURCE,
           "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
           "count": len(cases), "cases": cases}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
    return len(cases)
