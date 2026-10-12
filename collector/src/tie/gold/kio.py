"""Gold labels from KIO rulings on exclusion of bidders for collusion.

Second validation source next to UOKiK decisions: rulings of the National Appeal Chamber
(Krajowa Izba Odwoławcza, KIO) that decide on art. 108 ust. 1 pkt 5 Pzp ("zamawiający może
stwierdzić, na podstawie wiarygodnych przesłanek, że wykonawca zawarł z innymi wykonawcami
porozumienie mające na celu zakłócenie konkurencji"). The Pzp of 2019 applies from 2021, so
every such ruling falls inside our BZP window.

Source: the public database https://orzeczenia.uzp.gov.pl (ASP.NET, no official API):
- search: POST /Home/GetResults with the search form fields (Phrase, Kind=KIO, KIO.Art,
  KIO.DecT, Dt, Pg, Srt), answers an HTML fragment with links /Home/Details/{id};
- metadata: GET /Home/Details/{id} (buyer, city, outcome "oddalone"/"uwzględnione");
- full text: GET /Home/ContentHtml/{id}?Kind=KIO (plain HTML, UTF-8, no charset header);
  /Home/PdfContent/{id}?Kind=KIO serves the same text as PDF.

SEED is the curated list (research of 2026-10-12, see the t22 report). `expected` is our
reading of the ruling; DeepSeek extracts the fields again from the text and the run reports
the rulings where both disagree.

Decision semantics:
- upheld: KIO confirmed the collusion ground (dismissed the appeal of an excluded bidder, or
  ordered the exclusion demanded by a competitor) - a positive label for the colluders;
- overturned: KIO found the collusion not proven (annulled the exclusion, or dismissed the
  appeal demanding it) - a hard negative;
- other: the ruling does not decide on the merits of art. 108 ust. 1 pkt 5 (self-cleaning,
  formal grounds, withdrawn charge).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from selectolax.lexbor import LexborHTMLParser

from tie.ai.providers import Budget, BudgetExceeded, DeepSeekText
from tie.config import Settings
from tie.db import connect
from tie.http import HttpGiveUp, PoliteClient

log = logging.getLogger(__name__)

BASE_URL = "https://orzeczenia.uzp.gov.pl"
HOST = "orzeczenia.uzp.gov.pl"
# At most one request every 3 seconds to the UZP ruling database.
RATE_S = 3.0
TASK = "gold_kio"

# Matching a ruling to our procedures by buyer name and subject (pg_trgm similarity).
MIN_BUYER_SIM = 0.5
MIN_SUBJECT_SIM = 0.3
# Our procedures are result notices, published after the appeal (or after the repeated
# evaluation KIO ordered), so the window reaches further after the ruling than before it.
WINDOW_BEFORE_DAYS = 90
WINDOW_AFTER_DAYS = 400

Decision = Literal["upheld", "overturned", "other"]


def details_url(doc_id: int) -> str:
    return f"{BASE_URL}/Home/Details/{doc_id}"


def content_url(doc_id: int) -> str:
    return f"{BASE_URL}/Home/ContentHtml/{doc_id}?Kind=KIO"


def normalize_signature(signature: str) -> str:
    """'kio  2258/21' -> 'KIO 2258/21'."""
    s = re.sub(r"\s+", " ", (signature or "").strip().upper())
    return re.sub(r"^KIO\s*", "KIO ", s)


@dataclass(frozen=True)
class SeedRuling:
    signature: str
    ruling_date: date
    doc_id: int
    expected: Literal["upheld", "overturned"]
    joined: tuple[str, ...] = ()

    @property
    def signatures(self) -> tuple[str, ...]:
        return (self.signature, *self.joined)

    @property
    def url(self) -> str:
        return details_url(self.doc_id)


def _seed(signature: str, day: str, doc_id: int, expected: str,
          *joined: str) -> SeedRuling:
    return SeedRuling(signature, date.fromisoformat(day), doc_id, expected, joined)


UP, OV = "upheld", "overturned"

SEED: tuple[SeedRuling, ...] = (
    # upheld: exclusion for collusion confirmed or ordered by KIO
    _seed("KIO 2254/21", "2021-09-06", 15646, UP, "KIO 2255/21", "KIO 2258/21"),
    _seed("KIO 2378/21", "2021-10-01", 15733, UP),
    _seed("KIO 506/22", "2022-03-11", 16729, UP, "KIO 507/22"),
    _seed("KIO 757/22", "2022-04-07", 16944, UP, "KIO 807/22"),
    _seed("KIO 2977/22", "2022-11-24", 18888, UP),
    _seed("KIO 434/23", "2023-03-02", 19159, UP),
    _seed("KIO 1902/23", "2023-07-18", 26604, UP),
    _seed("KIO 1203/23", "2023-07-28", 19591, UP),
    _seed("KIO 2128/23", "2023-08-04", 26530, UP),
    _seed("KIO 2512/23", "2023-09-07", 19649, UP),
    _seed("KIO 3849/23", "2024-01-15", 24520, UP),
    _seed("KIO 505/24", "2024-03-04", 23645, UP),
    _seed("KIO 741/24", "2024-03-19", 23820, UP),
    _seed("KIO 1349/24", "2024-05-09", 22900, UP),
    _seed("KIO 1607/24", "2024-05-29", 23141, UP),
    _seed("KIO 1725/24", "2024-06-17", 22680, UP, "KIO 1747/24", "KIO 1773/24"),
    _seed("KIO 2717/24", "2024-08-28", 21946, UP, "KIO 2720/24"),
    _seed("KIO 3076/24", "2024-09-19", 20268, UP),
    _seed("KIO 3524/24", "2024-10-22", 20695, UP),
    _seed("KIO 3667/24", "2024-10-31", 20818, UP),
    _seed("KIO 4930/24", "2025-01-20", 23973, UP),
    _seed("KIO 4921/24", "2025-01-23", 24014, UP),
    _seed("KIO 618/25", "2025-03-17", 25540, UP, "KIO 619/25"),
    _seed("KIO 2678/25", "2025-08-18", 30127, UP, "KIO 2686/25", "KIO 2701/25"),
    _seed("KIO 3461/25", "2025-09-29", 30743, UP),
    _seed("KIO 4219/25", "2025-11-17", 31540, UP),
    _seed("KIO 4309/25", "2025-11-21", 31645, UP),
    _seed("KIO 4751/25", "2025-12-16", 32047, UP),
    _seed("KIO 1512/26", "2026-06-22", 35262, UP),
    _seed("KIO 3136/26", "2026-08-26", 36348, UP, "KIO 3160/26"),
    _seed("KIO 4225/26", "2026-09-25", 36476, UP),
    # overturned: exclusion annulled, or the demand to exclude dismissed as not proven
    _seed("KIO 1083/21", "2021-06-04", 15390, OV),
    _seed("KIO 3185/21", "2021-11-18", 15997, OV),
    _seed("KIO 3745/21", "2022-01-19", 16180, OV),
    _seed("KIO 201/22", "2022-02-21", 16463, OV),
    _seed("KIO 279/22", "2022-03-15", 16528, OV),
    _seed("KIO 676/22", "2022-03-25", 16872, OV),
    _seed("KIO 1401/22", "2022-06-09", 17479, OV),
    _seed("KIO 1368/22", "2022-06-13", 17454, OV),
    _seed("KIO 1457/22", "2022-06-23", 17529, OV),
    _seed("KIO 1795/22", "2022-08-01", 17836, OV),
    _seed("KIO 599/23", "2023-04-04", 19251, OV, "KIO 619/23", "KIO 622/23"),
    _seed("KIO 2459/23", "2023-09-07", 26231, OV),
    _seed("KIO 3044/23", "2023-10-30", 19684, OV),
    _seed("KIO 3569/23", "2023-12-13", 19718, OV),
    _seed("KIO 3718/23", "2024-01-03", 24424, OV),
    _seed("KIO 3799/23", "2024-01-09", 19724, OV, "KIO 3805/23"),
    _seed("KIO 3894/23", "2024-01-18", 24570, OV, "KIO 3901/23"),
    _seed("KIO 1132/24", "2024-04-23", 23518, OV),
    _seed("KIO 1348/24", "2024-05-09", 22899, OV),
    _seed("KIO 2457/24", "2024-08-02", 22031, OV),
    _seed("KIO 4385/24", "2024-12-13", 21665, OV, "KIO 4411/24"),
    _seed("KIO 424/25", "2025-03-04", 25357, OV),
    _seed("KIO 1747/25", "2025-06-06", 29248, OV, "KIO 1749/25"),
    _seed("KIO 2244/25", "2025-06-30", 29546, OV),
    _seed("KIO 3819/25", "2025-10-28", 31265, OV),
    _seed("KIO 4227/25", "2025-11-20", 31619, OV, "KIO 4232/25", "KIO 4233/25",
          "KIO 4235/25", "KIO 4236/25", "KIO 4237/25"),
    _seed("KIO 4472/25", "2025-12-01", 31782, OV, "KIO 4651/25"),
    _seed("KIO 271/26", "2026-03-13", 33503, OV),
    _seed("KIO 1225/26", "2026-04-16", 34095, OV),
    _seed("KIO 526/26", "2026-04-20", 34137, OV),
    _seed("KIO 1198/26", "2026-04-20", 34160, OV),
    _seed("KIO 1104/26", "2026-05-11", 34495, OV),
    _seed("KIO 2341/26", "2026-06-16", 35173, OV),
    _seed("KIO 2273/26", "2026-07-06", 35519, OV),
    _seed("KIO 3031/26", "2026-08-04", 36024, OV, "KIO 3032/26", "KIO 3034/26",
          "KIO 3044/26"),
    _seed("KIO 3750/26", "2026-09-23", 36334, OV, "KIO 3771/26"),
)


def find_seed(signature: str, seeds: tuple[SeedRuling, ...] = SEED) -> SeedRuling | None:
    wanted = normalize_signature(signature)
    return next((s for s in seeds if wanted in s.signatures), None)


# --- fetch and text --------------------------------------------------------------------

def fetch_ruling_html(client: PoliteClient, doc_id: int) -> str:
    r = client.get(content_url(doc_id))
    if r.status_code != 200:
        raise RuntimeError(f"KIO ruling {doc_id}: HTTP {r.status_code}")
    # The server sends text/html without a charset; the documents are UTF-8.
    return r.content.decode("utf-8", errors="replace")


_LEAF_BLOCKS = "h1, h2, h3, h4, h5, h6, p"
_BLOCKS = _LEAF_BLOCKS + ", li"


def html_to_text(html: str) -> str:
    """One line per paragraph or heading, whitespace (and nbsp) collapsed."""
    lines = []
    for node in LexborHTMLParser(html).css(_BLOCKS):
        # KIO documents wrap paragraphs in <li>; take the inner paragraphs only once.
        if node.tag == "li" and node.css_first(_LEAF_BLOCKS) is not None:
            continue
        text = re.sub(r"\s+", " ", node.text(separator=" ").replace("\xa0", " ")).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


_KEYWORDS = re.compile(r"108 ust\. ?1 pkt ?5|porozumieni|zmow", re.IGNORECASE)
_GAP = "\n[...]\n"


def excerpt(text: str, max_chars: int = 24000, head: int = 14000, tail: int = 5000,
            window: int = 600) -> str:
    """The part of a long ruling worth sending to the model: the head (operative part,
    parties, the appellant's charges), the tail (the Chamber's conclusion) and windows
    around the collusion keywords in between, within `max_chars`."""
    if len(text) <= max_chars:
        return text
    start, end = text[:head], text[len(text) - tail:]
    middle = text[head:len(text) - tail]
    budget = max_chars - len(start) - len(end) - 2 * len(_GAP)
    spans: list[list[int]] = []
    for m in _KEYWORDS.finditer(middle):
        lo, hi = max(0, m.start() - window), min(len(middle), m.end() + window)
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = hi
        else:
            spans.append([lo, hi])
    parts: list[str] = []
    for lo, hi in spans:
        piece = middle[lo:hi]
        cost = len(piece) + (len(_GAP) if parts else 0)
        if cost > budget:
            break
        parts.append(piece)
        budget -= cost
    if not parts:
        return start + _GAP + end
    return start + _GAP + _GAP.join(parts) + _GAP + end


# --- extraction ------------------------------------------------------------------------

def _blank_to_none(v):
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


class Colluder(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1)
    nip: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("nip", mode="before")
    @classmethod
    def _nip(cls, v):
        if v is None:
            return None
        digits = re.sub(r"\D", "", str(v))
        return digits if len(digits) == 10 else None


_BZP = re.compile(r"(\d{4})\s*/\s*BZP\s*(\d{8})", re.IGNORECASE)
_OCDS = re.compile(r"ocds-148610-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class KioExtraction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    signature: str | None = None
    ruling_date: date | None = None
    buyer: str | None = None
    subject: str | None = None
    procedure_ref: str | None = None
    bzp_number: str | None = None
    ocds_id: str | None = None
    appellant: str | None = None
    appellant_role: Literal["excluded_bidder", "competitor", "other"] = "other"
    colluders: list[Colluder] = Field(default_factory=list)
    decision: Decision
    reasoning: str | None = None

    @field_validator("signature", "buyer", "subject", "procedure_ref", "appellant",
                     "reasoning", mode="before")
    @classmethod
    def _strings(cls, v):
        return _blank_to_none(v)

    @field_validator("ruling_date", mode="before")
    @classmethod
    def _date(cls, v):
        v = _blank_to_none(v)
        if not isinstance(v, str):
            return v
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y"):
            try:
                return datetime.strptime(v, fmt).date()
            except ValueError:
                continue
        return None

    @field_validator("bzp_number", mode="before")
    @classmethod
    def _bzp(cls, v):
        m = _BZP.search(v) if isinstance(v, str) else None
        return f"{m.group(1)}/BZP {m.group(2)}" if m else None

    @field_validator("ocds_id", mode="before")
    @classmethod
    def _ocds(cls, v):
        m = _OCDS.search(v.lower()) if isinstance(v, str) else None
        return m.group(0) if m else None

    @field_validator("appellant_role", "decision", mode="before")
    @classmethod
    def _lower(cls, v):
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("colluders", mode="before")
    @classmethod
    def _colluders(cls, v):
        return [] if v is None else v


SYSTEM_PROMPT = """\
Jesteś analitykiem orzeczeń Krajowej Izby Odwoławczej (KIO). Dostajesz tekst (lub fragmenty)
wyroku KIO. Interesuje nas wyłącznie przesłanka wykluczenia z art. 108 ust. 1 pkt 5 ustawy
Pzp: zawarcie przez wykonawcę z innymi wykonawcami porozumienia mającego na celu zakłócenie
konkurencji (zmowa przetargowa), także w związku z art. 111 pkt 4 i art. 226 ust. 1 pkt 2
lit. a Pzp.

Zwróć jeden obiekt JSON z kluczami:
- "signature": sygnatura akt, np. "KIO 1234/24" (pierwsza, gdy sprawy połączone);
- "ruling_date": data wyroku RRRR-MM-DD;
- "buyer": pełna nazwa zamawiającego;
- "subject": przedmiot zamówienia (krótko, słowami z tekstu);
- "procedure_ref": numer ogłoszenia lub postępowania tak, jak podano w tekście (BZP, TED
  "2023/S ...", znak sprawy zamawiającego), albo null;
- "bzp_number": numer ogłoszenia w Biuletynie Zamówień Publicznych, np.
  "2023/BZP 00123456/01", albo null;
- "ocds_id": identyfikator postępowania "ocds-148610-...", jeśli występuje, albo null;
- "appellant": odwołujący;
- "appellant_role": "excluded_bidder" gdy odwołujący kwestionuje własne wykluczenie lub
  odrzucenie swojej oferty z powodu zmowy, "competitor" gdy odwołujący żąda wykluczenia
  innych wykonawców za zmowę, w pozostałych przypadkach "other";
- "colluders": lista wykonawców, którym zarzucono zmowę (art. 108 ust. 1 pkt 5), każdy jako
  {"name": nazwa z tekstu, "nip": NIP jeśli podany w tekście, inaczej null}; nie zgaduj NIP;
- "decision": jak KIO rozstrzygnęła zarzut zmowy:
  "upheld" gdy KIO potwierdziła zmowę (oddaliła odwołanie wykluczonego wykonawcy w tym
  zakresie albo nakazała wykluczenie lub odrzucenie oferty z powodu zmowy),
  "overturned" gdy KIO uznała zmowę za niewykazaną (nakazała unieważnienie wykluczenia albo
  oddaliła żądanie wykluczenia za zmowę),
  "other" gdy wyrok nie rozstrzyga merytorycznie zarzutu zmowy (np. dotyczy tylko
  samooczyszczenia, odrzucenia odwołania, umorzenia, innej podstawy wykluczenia);
- "reasoning": jedno lub dwa zdania uzasadnienia decyzji po polsku.

Używaj tylko informacji z tekstu. Brakujące wartości ustaw na null.
"""


class TextProvider(Protocol):
    def complete_json(self, *, task: str, system: str, user: str) -> dict: ...


def extract_ruling(provider: TextProvider, text: str, *, signature: str) -> KioExtraction:
    user = f"Sygnatura: {signature}\n\nTekst wyroku KIO:\n{excerpt(text)}"
    data = provider.complete_json(task=TASK, system=SYSTEM_PROMPT, user=user)
    return KioExtraction.model_validate(data)


# --- matching --------------------------------------------------------------------------

@dataclass(frozen=True)
class ProcedureMatch:
    ocds_id: str
    method: Literal["ocds", "bzp", "buyer_subject"]
    score: float


def match_procedure(conn: psycopg.Connection, ext: KioExtraction,
                    ruling_date: date | None) -> ProcedureMatch | None:
    """Link a ruling to one of our procedures: by OCDS id, then by BZP number, then by the
    most similar buyer name and subject among procedures published around the ruling."""
    if ext.ocds_id:
        row = conn.execute("select ocds_id from procedures where ocds_id = %s",
                           (ext.ocds_id,)).fetchone()
        if row:
            return ProcedureMatch(row["ocds_id"], "ocds", 1.0)
    if ext.bzp_number:
        row = conn.execute(
            "select ocds_id from procedures where bzp_number = %(n)s"
            " or notice_number like %(n)s || '/%%' order by published_at limit 1",
            {"n": ext.bzp_number}).fetchone()
        if row:
            return ProcedureMatch(row["ocds_id"], "bzp", 1.0)
    if not (ext.buyer and ext.subject):
        return None
    row = conn.execute(
        "select p.ocds_id, similarity(lower(b.name), lower(%(buyer)s)) as bs,"
        " similarity(lower(p.title), lower(%(subject)s)) as ts"
        " from procedures p join buyers b on b.id = p.buyer_id"
        " where similarity(lower(b.name), lower(%(buyer)s)) >= %(min_b)s"
        "  and similarity(lower(p.title), lower(%(subject)s)) >= %(min_t)s"
        "  and (%(day)s::date is null or p.published_at between"
        "       %(day)s::date - %(before)s::int and %(day)s::date + %(after)s::int)"
        " order by bs + ts desc, p.published_at limit 1",
        {"buyer": ext.buyer, "subject": ext.subject, "min_b": MIN_BUYER_SIM,
         "min_t": MIN_SUBJECT_SIM, "day": ruling_date, "before": WINDOW_BEFORE_DAYS,
         "after": WINDOW_AFTER_DAYS}).fetchone()
    if not row:
        return None
    return ProcedureMatch(row["ocds_id"], "buyer_subject",
                          round((float(row["bs"]) + float(row["ts"])) / 2, 4))


# --- store and run ---------------------------------------------------------------------

def store_ruling(conn: psycopg.Connection, seed: SeedRuling, ext: KioExtraction,
                 match: ProcedureMatch | None) -> None:
    payload = {
        "doc_id": seed.doc_id,
        "signatures": list(seed.signatures),
        "content_url": content_url(seed.doc_id),
        "expected": seed.expected,
        "extraction": ext.model_dump(mode="json"),
        "match": None if match is None else {"ocds_id": match.ocds_id, "method": match.method,
                                             "score": match.score},
    }
    conn.execute(
        "insert into gold_kio(signature, ruling_date, buyer, procedure_ref, ocds_id,"
        " bzp_number, colluders, decision, url, payload)"
        " values (%(signature)s, %(ruling_date)s, %(buyer)s, %(procedure_ref)s, %(ocds_id)s,"
        " %(bzp_number)s, %(colluders)s, %(decision)s, %(url)s, %(payload)s)"
        " on conflict (signature) do update set ruling_date = excluded.ruling_date,"
        " buyer = excluded.buyer, procedure_ref = excluded.procedure_ref,"
        " ocds_id = excluded.ocds_id, bzp_number = excluded.bzp_number,"
        " colluders = excluded.colluders, decision = excluded.decision,"
        " url = excluded.url, payload = excluded.payload, updated_at = now()",
        {"signature": seed.signature, "ruling_date": seed.ruling_date, "buyer": ext.buyer,
         "procedure_ref": ext.procedure_ref,
         "ocds_id": match.ocds_id if match else None, "bzp_number": ext.bzp_number,
         "colluders": Jsonb([c.model_dump() for c in ext.colluders]),
         "decision": ext.decision, "url": seed.url, "payload": Jsonb(payload)})
    conn.commit()


def run(conn: psycopg.Connection, client: PoliteClient, provider: TextProvider, *,
        only: str | None = None, seeds: tuple[SeedRuling, ...] = SEED) -> dict:
    """Fetch, extract, match and store the seed rulings (or the one named by `only`).
    A failing ruling is logged and counted; the run goes on. The AI budget stops it."""
    selected = seeds if only is None else tuple(
        s for s in seeds if normalize_signature(only) in s.signatures)
    summary: dict = {"selected": len(selected), "processed": 0, "failed": 0, "matched": 0,
                     "upheld": 0, "overturned": 0, "other": 0, "failures": [],
                     "expected_mismatch": [], "stopped": None}
    for seed in selected:
        try:
            text = html_to_text(fetch_ruling_html(client, seed.doc_id))
            if not text:
                raise RuntimeError(f"KIO ruling {seed.doc_id}: empty text")
            ext = extract_ruling(provider, text, signature=seed.signature)
            match = match_procedure(conn, ext, seed.ruling_date)
            store_ruling(conn, seed, ext, match)
        except BudgetExceeded:
            conn.rollback()
            log.warning("gold_kio: AI budget reached, stopping before %s", seed.signature)
            summary["stopped"] = "budget"
            break
        except (ValidationError, RuntimeError, HttpGiveUp, psycopg.Error, OSError) as e:
            conn.rollback()
            log.warning("gold_kio %s failed: %s", seed.signature, e)
            summary["failed"] += 1
            summary["failures"].append(seed.signature)
            continue
        summary["processed"] += 1
        summary[ext.decision] += 1
        summary["matched"] += match is not None
        if ext.decision != seed.expected:
            summary["expected_mismatch"].append(seed.signature)
        log.info("gold_kio %s: decision=%s expected=%s match=%s", seed.signature,
                 ext.decision, seed.expected, match.method if match else None)
    return summary


def cli_main(settings: Settings, only: str | None) -> int:
    """`tie gold-kio [--only SIGNATURE]`: prints the run summary as JSON."""
    if not settings.deepseek_api_key:
        log.error("gold-kio: DEEPSEEK_API_KEY is not set")
        return 2
    if only is not None and find_seed(only) is None:
        log.error("gold-kio: %r is not in the curated list", only)
        return 2
    rates = {HOST: RATE_S, "api.deepseek.com": 0.2}
    with (PoliteClient(settings.user_agent, rates) as client,
          connect(settings.database_url) as conn,
          # Own autocommit connection: Budget commits each `ai_calls` row at once.
          psycopg.connect(settings.database_url, autocommit=True,
                          row_factory=dict_row) as budget_conn):
        provider = DeepSeekText(client, settings.deepseek_api_key, settings.deepseek_model,
                                Budget(budget_conn, settings.ai_daily_budget_usd))
        summary = run(conn, client, provider, only=only)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["failed"] == 0 and summary["stopped"] is None else 1
