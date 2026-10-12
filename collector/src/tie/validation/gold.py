"""Gold set for the validation: UOKiK cases (`gold_cases`) and KIO rulings (`gold_kio`).

Helpers to read both tables, to place a UOKiK case in a market (sector -> CPV prefix,
region -> voivodeship code), to tell apart the firms named in a decision, and to show firm
names publicly: only legal entities with a capital company form keep their name, every other
firm (a sole trader, a civil or personal partnership whose name carries a person's name) is
shown as `NIP <nip>` when the NIP is known, otherwise not at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import psycopg

from tie.engine.markets import normalize_province
from tie.identity.names import normalize_name
from tie.money import clean_nip

# Sector of the curated decision list (`tie.gold.sources`) -> CPV prefix of its market.
# Matched as substrings of the normalized sector, first match wins; a case may override it
# with `source.cpv`.
SECTOR_CPV: tuple[tuple[str, str], ...] = (
    ("niepelnospraw", "60130"),   # special-purpose road passenger transport
    ("dowoz", "60130"),
    ("podklady kolejowe", "349"),  # railway equipment
    ("warzyw", "15300"),
    ("pieczyw", "15810"),
    ("wycinka drzew", "77211"),
    ("lesn", "77200"),
    ("zielen", "77310"),
    ("odpad", "90500"),
    ("utrzymanie drog", "45233"),
    ("drog", "45233"),
    ("wodociag", "45232"),
    ("kanalizac", "45232"),
    ("hosting", "724"),
    ("informatyk", "72"),
    ("drukar", "50313"),
    ("maszyny rolnicze", "16"),
    ("kartograf", "71354"),
    ("transport towar", "601"),
    ("holowan", "50118"),
    ("kurier", "64120"),
    ("remont", "45"),
    ("budow", "45"),
)

# Name tokens (after `normalize_name`) of legal entities whose name is not a person's name.
LEGAL_ENTITY_TOKENS = frozenset({"spzoo", "sa", "spoldzielnia", "fundacja", "stowarzyszenie",
                                 "instytut", "uniwersytet"})
# Tokens too generic to identify a firm when comparing names inside one decision.
_GENERIC = frozenset({"spzoo", "sa", "spj", "spk", "sc", "ska", "phu", "puh", "fhu", "ppuh",
                      "uslugi", "firma", "przedsiebiorstwo", "zaklad", "handlowo", "uslugowe",
                      "handlowe", "produkcyjno", "i"})
NIP_PREFIX = "NIP "


def sector_cpv(sector: str | None) -> str | None:
    s = normalize_name(sector or "")
    for needle, cpv in SECTOR_CPV:
        if needle in s:
            return cpv
    return None


def is_legal_entity(name: str) -> bool:
    n = normalize_name(name)
    return bool(set(n.split()) & LEGAL_ENTITY_TOKENS) or "przedsiebiorstwo panstwowe" in n


def public_firm_label(name: str | None, nip: str | None,
                      is_jdg: bool | None = None) -> str | None:
    """Public label of a firm: its name for a legal entity (and not a known sole trader),
    else `NIP <nip>`, else None (the firm is left out)."""
    if name and is_jdg is not True and is_legal_entity(name):
        return " ".join(name.split())
    nip = clean_nip(nip or "")
    return f"{NIP_PREFIX}{nip}" if nip else None


def _core(norm: str) -> set[str]:
    return {t for t in norm.split() if t not in _GENERIC}


def same_firm(a: str, b: str) -> bool:
    """Two normalized names of one decision denote the same firm: equal, or the distinctive
    tokens (at least two) of one are all in the other."""
    if not a or not b:
        return False
    if a == b:
        return True
    ca, cb = _core(a), _core(b)
    small, big = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
    return len(small) >= 2 and small <= big


@dataclass
class Firm:
    name: str
    nip: str | None = None

    @property
    def norm(self) -> str:
        return normalize_name(self.name)


@dataclass
class UokikCase:
    decision: str
    year: int | None
    sector: str | None
    region: str | None
    cpv: str | None
    province: str | None
    colluders: list[Firm]
    tenders: list[dict]
    patterns: list[str] = field(default_factory=list)

    @property
    def cutoff(self) -> date | None:
        """Lead-test cutoff: the decision date is not stored, the start of its year is."""
        return date(self.year, 1, 1) if self.year else None


@dataclass
class KioRuling:
    signature: str
    ruling_date: date | None
    ocds_id: str | None
    decision: str | None
    colluders: list[Firm]


def _firms(items) -> list[Firm]:
    out = []
    for c in items or []:
        if isinstance(c, dict) and (c.get("name") or "").strip():
            out.append(Firm(" ".join(str(c["name"]).split()), clean_nip(c.get("nip") or "")))
    return out


def case_from_payload(payload: dict) -> UokikCase:
    source = payload.get("source") or {}
    sector = source.get("sector")
    region = source.get("region")
    cpv = (str(source["cpv"]).replace(" ", "").split("-", 1)[0]
           if source.get("cpv") else sector_cpv(sector))
    return UokikCase(
        decision=str(payload.get("decision") or ""),
        year=payload.get("year"),
        sector=sector,
        region=region,
        cpv=cpv or None,
        province=normalize_province(region),
        colluders=_firms(payload.get("colluders")),
        tenders=[t for t in payload.get("tenders") or [] if isinstance(t, dict)],
        patterns=list(payload.get("pattern") or []),
    )


def load_uokik(conn: psycopg.Connection) -> list[UokikCase]:
    rows = conn.execute("select decision, payload from gold_cases order by decision").fetchall()
    conn.commit()
    return [case_from_payload({**r["payload"], "decision": r["decision"]}) for r in rows]


def load_kio(conn: psycopg.Connection) -> list[KioRuling]:
    rows = conn.execute(
        "select signature, ruling_date, ocds_id, decision, colluders from gold_kio"
        " order by signature").fetchall()
    conn.commit()
    return [KioRuling(r["signature"], r["ruling_date"], r["ocds_id"], r["decision"],
                      _firms(r["colluders"])) for r in rows]
