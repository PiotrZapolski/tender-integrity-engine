"""Plain-language descriptions of signals.

Every signal gets a deterministic Polish description built from templates: a title, a short
summary, findings in the `api.signal_detail.findings` shape of `docs/contracts/api-v1.md`
(each with `viz` data) and a fixed list of other possible explanations for the pattern.
Numbers in the text come only from the evidence package.

Published signals may additionally get a rewrite of `title` and `summary` from DeepSeek
(task `signal_explain`). The model sees the evidence and the template and must not add
numbers: every number in its answer has to occur in the evidence (or in the template, whose
numbers are derived from the evidence). Any violation, provider error or exceeded budget
keeps the template. Findings and alternatives always come from the template.

Evidence package (built by `tie.engine.evidence.build_evidence`, Task 7). Keys used; every key
is optional and the listed alternatives are tolerated:

- `market`: `{"market_id", "cpv_group", "province", "level"}` (or the same keys top-level).
  `province` is a BZP code like `PL14`, a name, or `PL` / null for the whole country.
- `members`: `[{"key": "A", "bidder_id": 123, "n_bids"?, "n_wins"?}]` in display order; a
  plain list of bidder ids also works and missing keys become A, B, C... by position.
- `n_lots` (lots where at least two members bid together), `total_value`.
- `period`: `{"from", "to"}` ISO dates (or `[from, to]`, or `period_from` / `period_to`).
- `oecd` (alternatives `items`, `tests`): `{"3.1.6": result}` where `result` is
  `{"value", "pct" | "percentile", ...}` or a list of such results (the strongest is used).
- `lots` (alternatives `examples`, `sample_lots`): up to 20 example lots `{"id" | "lot_id",
  "date", "price" (winning price), "winner", "bidders", "offers"?, "teryt"?}`; `winner` and
  `bidders` hold member keys (`A`), bidder ids, or `inny` for a winner outside the cluster;
  `offers` is `[{"bidder_id" | "key", "price"}]`. When the lots carry no offers,
  `explain_signal` reads the members' valid offers from `bids` for the ladder.
- `pairs` (optional): `[{"a", "b", "n_together"}]` with bidder ids.

Decision (`tie.engine.decide.Decision`, Task 7): any object with `probability`, `strength`,
`per_item: dict[str, float]` and `source`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

TASK = "signal_explain"

Evidence = dict[str, Any]

MAX_FINDINGS = 5
MAX_ALTERNATIVES = 5
MAX_CELLS = 24
MAX_TITLE = 140
MAX_SUMMARY = 1000


class DecisionLike(Protocol):
    probability: float
    strength: float
    per_item: dict[str, float]
    source: str


class TextProvider(Protocol):
    def complete_json(self, *, task: str, system: str, user: str) -> dict: ...


@dataclass
class StoredDecision:
    """Decision rebuilt from a `signals` row."""

    probability: float
    strength: float
    per_item: dict[str, float] = field(default_factory=dict)
    source: str = "stored"


@dataclass
class Explanation:
    title: str
    summary: str
    findings: list[dict[str, Any]]
    alternatives: list[str]


# --- fixed texts -------------------------------------------------------------------------

PROVINCES = {
    "PL02": "dolnośląskie", "PL04": "kujawsko-pomorskie", "PL06": "lubelskie",
    "PL08": "lubuskie", "PL10": "łódzkie", "PL12": "małopolskie", "PL14": "mazowieckie",
    "PL16": "opolskie", "PL18": "podkarpackie", "PL20": "podlaskie", "PL22": "pomorskie",
    "PL24": "śląskie", "PL26": "świętokrzyskie", "PL28": "warmińsko-mazurskie",
    "PL30": "wielkopolskie", "PL32": "zachodniopomorskie",
}

ITEM_TITLES = {
    "3.1.1": "Ta sama firma często wygrywa",
    "3.1.2": "Podział zamawiających między firmy",
    "3.1.5": "Firma zawsze startuje i nigdy nie wygrywa",
    "3.1.6": "Wygrywanie na zmianę",
    "3.1.7": "Wspólna oferta firm, które wcześniej startowały osobno",
    "3.2.4": "Wspólne dane kontaktowe",
    "3.2.11": "Wspólny adres lub rachunek",
    "3.3.3": "Mniejsze obniżki niż w rynku",
    "3.3.5": "Identyczne ceny w ofertach",
    "3.3.6": "Stały układ cen między ofertami",
    "3.3.7": "Duża luka między zwycięzcą a resztą",
    "3.3.9": "Zwycięska cena wysoko ponad szacunek",
    "3.3.10": "Spadek cen po wejściu nowej firmy",
}

PATTERN_OF_ITEM = {
    "3.1.6": "rotation",
    "3.3.6": "cover", "3.3.7": "cover", "3.3.5": "cover", "3.1.5": "cover",
    "3.1.2": "split",
    "3.2.11": "shared", "3.2.4": "shared",
    "3.1.7": "consortium",
    "3.3.9": "prices", "3.3.3": "prices", "3.3.10": "prices", "3.3.1": "prices",
    "3.3.2": "prices", "3.3.4": "prices",
    "3.1.1": "dominance",
}

VIZ_OF_ITEM = {
    "3.1.6": "rotation", "3.1.5": "rotation", "3.1.1": "rotation", "3.1.2": "rotation",
    "3.3.6": "ladder", "3.3.7": "ladder", "3.3.5": "ladder", "3.3.9": "ladder",
    "3.3.3": "ladder", "3.3.10": "ladder",
}

ALT_SMALL_MARKET = ("Mały rynek: w tej branży i regionie działa niewiele firm, więc te same "
                    "firmy często spotykają się w przetargach.")
ALT_CAPACITY = ("Ograniczone moce: firma z pełnym portfelem zleceń składa droższą ofertę albo "
                "rezygnuje, więc wygrane mogą rozkładać się po kolei bez żadnej umowy.")
ALT_SPECIALISATION = ("Różna specjalizacja: firmy mogą być mocniejsze w innych rodzajach prac "
                      "i wygrywać tam, gdzie mają przewagę.")
ALT_COMMON_ESTIMATE = ("Wspólny kosztorys: firmy mogą liczyć ceny z tych samych katalogów "
                       "i cenników hurtowych, więc ich oferty wyglądają podobnie.")
ALT_SAME_SUPPLIER = ("Ten sam dostawca materiałów lub podwykonawca u kilku oferentów daje "
                     "podobną strukturę cen.")
ALT_DISTANCE = ("Odległość i koszty dojazdu: firmy startują głównie tam, gdzie mają bazę "
                "i sprzęt.")
ALT_GROUP = ("Firmy z jednej grupy kapitałowej lub rodzinnej mogą startować osobno, jeśli "
             "naprawdę ze sobą konkurują.")
ALT_SHARED_OFFICE = ("Wspólne biuro rachunkowe, ten sam budynek biurowy albo ten sam "
                     "pełnomocnik mogą dawać wspólne dane bez porozumienia co do cen.")
ALT_CONSORTIUM = ("Konsorcjum bywa jedynym sposobem, by spełnić warunki dużego zamówienia.")
ALT_ESTIMATE_LOW = ("Wartość szacunkowa zamawiającego mogła być zaniżona albo nieaktualna.")
ALT_COSTS_UP = ("Wzrost cen materiałów i pracy w okresie przetargów podnosi wszystkie oferty.")
ALT_ADVANTAGE = ("Firma może mieć przewagę kosztową, sprzęt lub doświadczenie, których brakuje "
                 "konkurentom.")

ALTERNATIVES = {
    "rotation": [ALT_SMALL_MARKET, ALT_CAPACITY, ALT_SPECIALISATION],
    "cover": [ALT_COMMON_ESTIMATE, ALT_SAME_SUPPLIER, ALT_SMALL_MARKET],
    "split": [ALT_DISTANCE, ALT_CAPACITY, ALT_SMALL_MARKET],
    "shared": [ALT_GROUP, ALT_SHARED_OFFICE],
    "consortium": [ALT_CONSORTIUM, ALT_CAPACITY],
    "prices": [ALT_ESTIMATE_LOW, ALT_COSTS_UP, ALT_SMALL_MARKET],
    "dominance": [ALT_ADVANTAGE, ALT_SMALL_MARKET],
    "together": [ALT_SMALL_MARKET, ALT_CAPACITY],
}

SYSTEM_PROMPT = """Piszesz krótkie opisy sygnałów statystycznych z polskich przetargów publicznych dla zwykłych czytelników.
Dostajesz dowody (JSON) i opis z szablonu. Przepisz tytuł i streszczenie prostym językiem polskim.
Zasady:
- Zwróć wyłącznie JSON: {"title": tekst, "summary": tekst}.
- title: jedno zdanie, najwyżej 90 znaków. summary: od 2 do 4 krótkich zdań.
- Nie dodawaj żadnych nowych liczb, dat ani kwot. Używaj tylko liczb z dowodów lub z szablonu i zapisuj je cyframi.
- Firmy nazywaj tylko literami z szablonu (firma A, firma B). Nie podawaj nazw firm, NIP ani nazwisk.
- To sygnał statystyczny, nie zarzut: nie pisz, że firmy zawarły zmowę; pisz, co widać w danych.
- Nie używaj długich myślników; używaj zwykłego łącznika "-" albo przecinka."""


# --- small helpers -----------------------------------------------------------------------

def _first(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n == 1:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def _firms(n: int) -> str:
    if n <= 0:
        return "Firmy"
    return f"{n} {_plural(n, 'firma', 'firmy', 'firm')}"


def _lots_word(n: int) -> str:
    return f"{n} {_plural(n, 'przetarg', 'przetargi', 'przetargów')}"


def _dec(x: float, digits: int) -> str:
    """Polish decimal: 0,93."""
    return f"{x:.{digits}f}".replace(".", ",")


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def _year(date_text: Any) -> str | None:
    if isinstance(date_text, str) and re.match(r"^\d{4}-\d{2}", date_text):
        return date_text[:4]
    return None


def _month_label(date_text: Any) -> str:
    if isinstance(date_text, str) and re.match(r"^\d{4}-\d{2}", date_text):
        return f"{date_text[5:7]}.{date_text[:4]}"
    return ""


def _no_dashes(text: str) -> str:
    return text.replace(chr(0x2014), "-").replace(chr(0x2013), "-")


def _id(value: Any) -> str | None:
    """Bidder ids compared as text: evidence may hold them as numbers or strings."""
    return None if value is None else str(value)


# --- evidence view -----------------------------------------------------------------------

@dataclass
class _Member:
    bidder_id: Any
    key: str
    n_bids: int | None
    n_wins: int | None


@dataclass
class _Result:
    value: float | None
    percentile: float | None
    signal: bool | None = None  # test verdict when the test stores one (3.3.10)


@dataclass
class _Lot:
    date: str | None
    winner: Any
    price: float | None
    teryt: str | None
    offers: list[tuple[Any, float | None]]


def _members(evidence: Evidence) -> list[_Member]:
    out = []
    for i, m in enumerate(evidence.get("members") or []):
        key = chr(ord("A") + i) if i < 26 else f"F{i + 1}"
        if isinstance(m, dict):
            out.append(_Member(
                bidder_id=_id(_first(m, "bidder_id", "id")),
                key=str(m.get("key") or key),
                n_bids=None if m.get("n_bids") is None else int(m["n_bids"]),
                n_wins=None if m.get("n_wins") is None else int(m["n_wins"]),
            ))
        else:
            out.append(_Member(bidder_id=_id(m), key=key, n_bids=None, n_wins=None))
    return out


def _best_result(raw: Any) -> _Result | None:
    if isinstance(raw, dict):
        signal = raw.get("signal")
        return _Result(_num(raw.get("value")), _num(_first(raw, "percentile", "pct")),
                       signal if isinstance(signal, bool) else None)
    if isinstance(raw, list):
        results = [r for r in (_best_result(x) for x in raw) if r is not None]
        if not results:
            return None
        return max(results, key=lambda r: (r.percentile if r.percentile is not None else -1.0,
                                           abs(r.value) if r.value is not None else -1.0))
    value = _num(raw)
    return None if value is None else _Result(value, None)


def _items(evidence: Evidence) -> dict[str, _Result]:
    raw = _first(evidence, "items", "oecd", "tests", default={})
    out: dict[str, _Result] = {}
    if isinstance(raw, dict):
        for item, res in raw.items():
            r = _best_result(res)
            if r is not None:
                out[str(item)] = r
    elif isinstance(raw, list):  # [{"item": "3.1.6", "value": ..., "percentile": ...}]
        grouped: dict[str, list] = {}
        for res in raw:
            if isinstance(res, dict) and res.get("item"):
                grouped.setdefault(str(res["item"]), []).append(res)
        for item, results in grouped.items():
            r = _best_result(results)
            if r is not None:
                out[item] = r
    return out


def _lots(evidence: Evidence) -> list[_Lot]:
    by_key = {m.key: m.bidder_id for m in _members(evidence)}

    def resolve(value: Any) -> str | None:
        """Member key (A, B) or bidder id to bidder id; other values (e.g. 'inny') kept."""
        value = _id(value)
        return by_key.get(value, value)

    out = []
    for lot in _first(evidence, "lots", "examples", "sample_lots", default=[]) or []:
        if not isinstance(lot, dict):
            continue
        offers = []
        for o in lot.get("offers") or []:
            if isinstance(o, dict):
                offers.append((resolve(_first(o, "bidder_id", "key", "id")), _num(o.get("price"))))
        for b in map(resolve, lot.get("bidders") or []):
            if not any(b == o[0] for o in offers):
                offers.append((b, None))
        winner = resolve(_first(lot, "winner", "winner_id", "winner_bidder_id", "winner_key"))
        out.append(_Lot(
            date=_first(lot, "date", "contract_date", "published_at"),
            winner=winner,
            price=_num(_first(lot, "price", "winner_price", "price_winner")),
            teryt=_first(lot, "teryt", "buyer_teryt"),
            offers=offers,
        ))
    out.sort(key=lambda lot: str(lot.date or ""))
    return out


def _period(evidence: Evidence) -> tuple[str | None, str | None]:
    p = evidence.get("period")
    if isinstance(p, dict):
        return _first(p, "from", "start"), _first(p, "to", "end")
    if isinstance(p, (list, tuple)) and len(p) == 2:
        return p[0], p[1]
    return evidence.get("period_from"), evidence.get("period_to")


def _market_text(evidence: Evidence) -> str | None:
    market = evidence.get("market") if isinstance(evidence.get("market"), dict) else {}
    cpv = _first(market, "cpv_group") or evidence.get("cpv_group")
    province = _first(market, "province") or evidence.get("province")
    if not cpv:
        return None
    text = f"zamówienia z grupy CPV {cpv}"
    if province and province != "PL":
        name = PROVINCES.get(str(province), str(province))
        return f"{text}, województwo {name}"
    return f"{text}, cała Polska"


# --- findings ----------------------------------------------------------------------------

def _rotation_data(members: list[_Member], lots: list[_Lot]) -> dict:
    lots = lots[-MAX_CELLS:]
    rows = []
    for m in members:
        cells = []
        for lot in lots:
            if lot.winner is not None and lot.winner == m.bidder_id:
                cells.append("win")
            elif any(o[0] == m.bidder_id for o in lot.offers):
                cells.append("lose")
            else:
                cells.append(None)
        rows.append({"key": m.key, "cells": cells})
    return {"rows": rows}


def _ladder_data(members: list[_Member], lots: list[_Lot]) -> dict:
    keys = {m.bidder_id: m.key for m in members}
    rows = []
    for lot in lots[-MAX_CELLS:]:
        win_price = lot.price
        if win_price is None:
            win_price = next((p for b, p in lot.offers if b == lot.winner and p), None)
        if not win_price:
            continue
        marks = [{"key": keys[b], "pct": round(p / win_price * 100, 1)}
                 for b, p in lot.offers if b in keys and p]
        if marks:
            rows.append({"label": _month_label(lot.date), "marks": marks})
    return {"rows": rows}


def graph_data(evidence: Evidence) -> dict:
    """`{"nodes": [{"key", "size"}], "edges": [{"a", "b", "w"}]}` for the cluster members."""
    members = _members(evidence)
    lots = _lots(evidence)
    keys = {m.bidder_id: m.key for m in members}
    nodes = []
    for m in members:
        size = m.n_bids
        if size is None:
            size = sum(1 for lot in lots if any(o[0] == m.bidder_id for o in lot.offers))
        nodes.append({"key": m.key, "size": size})
    weights: dict[tuple[str, str], int] = {}
    pairs = evidence.get("pairs") or []
    if pairs:
        for p in pairs:
            if not isinstance(p, dict):
                continue
            if _id(p.get("a")) not in keys or _id(p.get("b")) not in keys:
                continue
            a, b = sorted((keys[_id(p["a"])], keys[_id(p["b"])]))
            weights[(a, b)] = weights.get((a, b), 0) + int(p.get("n_together") or 0)
    else:
        for lot in lots:
            present = sorted({keys[o[0]] for o in lot.offers if o[0] in keys})
            for i, a in enumerate(present):
                for b in present[i + 1:]:
                    weights[(a, b)] = weights.get((a, b), 0) + 1
    edges = [{"a": a, "b": b, "w": w} for (a, b), w in sorted(weights.items()) if w > 0]
    return {"nodes": nodes, "edges": edges}


def _map_data(members: list[_Member], lots: list[_Lot]) -> dict:
    keys = {m.bidder_id: m.key for m in members}
    areas, seen = [], set()
    for lot in lots:
        if lot.teryt and lot.winner in keys and lot.teryt not in seen:
            seen.add(lot.teryt)
            areas.append({"teryt": str(lot.teryt), "key": keys[lot.winner]})
    return {"areas": areas}


def _item_text(item: str, r: _Result) -> str:
    v = r.value
    if item == "3.1.6":
        text = "Firmy startowały razem i wygrywały kolejno, raz jedna, raz druga."
        if v is not None:
            text += f" Wskaźnik rotacji: {_dec(v, 2)} (1 oznacza pełną zmianę)."
    elif item == "3.3.6":
        text = "Stosunek cen ofert tych firm prawie się nie zmienia między przetargami."
        if v is not None:
            text += f" Zmienność stosunku cen: {_dec(v * 100, 1)}%."
    elif item == "3.3.7":
        text = ("Zwycięska oferta jest wyraźnie tańsza od pozostałych, a przegrani składają "
                "oferty bardzo zbliżone do siebie.")
    elif item == "3.3.5":
        text = "W tych samych przetargach pojawiały się oferty z identyczną ceną."
    elif item == "3.1.5":
        text = "Co najmniej jedna z firm regularnie startuje i nigdy nie wygrywa."
        if v is not None:
            text += f" Starty bez wygranej: {round(v)}."
    elif item == "3.1.2":
        text = "Każda z firm wygrywa głównie u swoich zamawiających."
        if v is not None:
            text += f" Wskaźnik wyłączności: {_dec(v, 2)} (1 oznacza pełny podział)."
    elif item in ("3.2.11", "3.2.4"):
        text = ("Firmy, które składają konkurencyjne oferty, mają wspólny rachunek bankowy, "
                "ten sam adres lub te same dane kontaktowe.")
    elif item == "3.1.7":
        text = ("Firmy, które wcześniej startowały osobno, potem złożyły wspólną ofertę jako "
                "konsorcjum.")
        if v is not None and v >= 1:
            text += f" Takich przypadków: {round(v)}."
    elif item == "3.3.9":
        text = "Zwycięskie ceny są wysoko w stosunku do wartości szacunkowej zamawiającego."
        if v is not None:
            text += f" Zwycięska cena to {_pct(v)} szacunku."
    elif item == "3.3.3":
        text = ("Firmy z grupy wygrywają z mniejszą obniżką względem szacunku niż inne firmy "
                "w tym rynku.")
        if v is not None:
            text += f" Mediana zwycięskiej ceny: {_pct(v)} szacunku."
    elif item == "3.3.10":
        text = "Gdy w przetargach pojawiła się nowa firma, ceny wyraźnie spadły."
        if v is not None:
            text += f" Zmiana cen: o {_pct(abs(v))}."
    elif item == "3.1.1":
        text = "Jedna z firm wygrywa dużą część przetargów w tym rynku."
        if v is not None and 0 <= v <= 1:
            text += f" Udział wygranych: {_pct(v)}."
    else:
        text = f"Dane wskazują na punkt {item} listy kontrolnej OECD."
    if r.percentile is not None:
        text += (f" Mocniej niż w {round(r.percentile * 100)}% porównywalnych przypadków "
                 "w tym rynku.")
    return text


def _selected_items(items: dict[str, _Result], per_item: dict[str, float]) -> list[str]:
    def score(item: str) -> tuple:
        p = per_item.get(item)
        r = items[item]
        return (p if p is not None else -1.0,
                r.percentile if r.percentile is not None else -1.0,
                abs(r.value) if r.value is not None else -1.0)

    def strong(item: str) -> bool:
        p = per_item.get(item)
        if p is not None:
            return p >= 0.5
        r = items[item]
        if r.signal is False:
            return False
        if r.percentile is not None:
            return r.percentile >= 0.9
        return item in ("3.2.11", "3.2.4", "3.1.7") and (r.value or 0) >= 1

    ranked = sorted(items, key=lambda i: (tuple(-x for x in score(i)), i))
    chosen = [i for i in ranked if strong(i)]
    if not chosen and ranked:
        chosen = ranked[:1]
    return chosen[:MAX_FINDINGS]


def _finding(item: str, r: _Result, evidence: Evidence, per_item: dict[str, float]) -> dict:
    members = _members(evidence)
    lots = _lots(evidence)
    viz = VIZ_OF_ITEM.get(item, "graph")
    if item == "3.1.2" and any(lot.teryt for lot in lots):
        viz = "map"
    if viz == "rotation":
        data = _rotation_data(members, lots)
    elif viz == "ladder":
        data = _ladder_data(members, lots)
        if not data["rows"]:
            viz, data = "graph", graph_data(evidence)
    elif viz == "map":
        data = _map_data(members, lots)
    else:
        data = graph_data(evidence)
    p = per_item.get(item)
    return {
        "oecd": item,
        "title": ITEM_TITLES.get(item, f"Punkt OECD {item}"),
        "text": _item_text(item, r),
        "probability": None if p is None else round(float(p), 4),
        "viz": viz,
        "data": data,
    }


# --- template ----------------------------------------------------------------------------

def _title(pattern: str, n: int) -> str:
    firms = _firms(n)
    titles = {
        "rotation": f"{firms} wygrywają przetargi na zmianę",
        "cover": f"{firms} składają oferty w stałym układzie cen",
        "split": f"{firms} dzielą się zamawiającymi",
        "shared": f"{firms} konkurują, choć mają wspólne dane",
        "consortium": "Konkurenci startują razem jako konsorcjum",
        "prices": "Zwycięskie ceny wyraźnie ponad szacunki zamawiających",
        "dominance": "Jedna firma wygrywa dużą część przetargów",
    }
    return titles.get(pattern, f"{firms} często startują razem")


def _lead(pattern: str, n: int, year: str | None, n_lots: int | None) -> str:
    since = f" od {year} roku" if year else ""
    verbs = {
        "rotation": "wygrywają na zmianę",
        "cover": "składają oferty w powtarzalnym układzie cen",
        "split": "wygrywają każda u innych zamawiających",
    }
    verb = verbs.get(pattern, "startują w tych samych przetargach")
    tail = f": {_lots_word(n_lots)} razem." if n_lots else "."
    return f"{_firms(n)}{since} {verb}{tail}"


def _wins_sentence(members: list[_Member], lots: list[_Lot], n_lots: int | None) -> str | None:
    """Wins per member: totals when the evidence has them, else counted in the example lots."""
    if members and all(m.n_wins is not None for m in members):
        wins = ", ".join(f"firma {m.key} {m.n_wins}" for m in members)
        return f"Wygrane: {wins}."
    decided = [lot for lot in lots if lot.winner is not None]
    if not members or len(decided) < 2:
        return None
    ids = {m.bidder_id for m in members}
    parts = [f"firma {m.key} {sum(1 for lot in decided if lot.winner == m.bidder_id)}"
             for m in members]
    others = sum(1 for lot in decided if lot.winner not in ids)
    if others:
        parts.append(f"inne firmy {others}")
    scope = "We wszystkich" if n_lots == len(decided) else "W ostatnich"
    return f"{scope} {len(decided)} wspólnych przetargach wygrywały: {', '.join(parts)}."


def template_explanation(evidence: Evidence, decision: DecisionLike | None) -> Explanation:
    evidence = evidence or {}
    per_item = dict(getattr(decision, "per_item", None) or {})
    members = _members(evidence)
    items = _items(evidence)
    chosen = _selected_items(items, per_item)
    findings = [_finding(i, items[i], evidence, per_item) for i in chosen]
    patterns = [PATTERN_OF_ITEM.get(i, "together") for i in chosen] or ["together"]
    pattern = patterns[0]

    n = len(members)
    n_lots = evidence.get("n_lots")
    n_lots = int(n_lots) if isinstance(n_lots, (int, float)) and n_lots > 0 else None
    period_from, _ = _period(evidence)

    sentences = [_lead(pattern, n, _year(period_from), n_lots)]
    market = _market_text(evidence)
    if market:
        sentences.append(f"Rynek: {market}.")
    total = _num(evidence.get("total_value"))
    if total and total > 0:
        money = f"{round(total):,}".replace(",", " ")
        sentences.append(f"Łączna wartość tych zamówień: {money} zł.")
    wins = _wins_sentence(members, _lots(evidence), n_lots)
    if wins:
        sentences.append(wins)
    if findings:
        top = findings[0]
        sentences.append(f"Najmocniejszy sygnał: {top['title'].lower()} "
                         f"(punkt OECD {top['oecd']}).")

    alternatives: list[str] = []
    for p in patterns:
        for alt in ALTERNATIVES.get(p, ALTERNATIVES["together"]):
            if alt not in alternatives:
                alternatives.append(alt)

    return Explanation(
        title=_no_dashes(_title(pattern, n)),
        summary=_no_dashes(" ".join(sentences)),
        findings=findings,
        alternatives=alternatives[:MAX_ALTERNATIVES],
    )


# --- number guard ------------------------------------------------------------------------

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_THOUSANDS = re.compile(r"(?<=\d)[ \u00a0\u202f](?=\d{3}(?!\d))")
# Scalars under these keys are identifiers, not facts: their digits never allow a number.
_ID_KEYS = ("id", "nip", "regon", "krs", "hash", "url", "winner", "a", "b", "bidders",
            "members", "key")


def _canon(d: Decimal) -> str:
    return format(d.normalize(), "f")


def _numbers_in_text(text: str) -> list[str]:
    text = _THOUSANDS.sub("", text)
    out = []
    for raw in _NUMBER.findall(text):
        try:
            out.append(_canon(Decimal(raw.replace(",", "."))))
        except InvalidOperation:
            continue
    return out


def _variants(x: float) -> set[str]:
    out = set()
    for base in (x, abs(x), x * 100, abs(x) * 100):
        for digits in (0, 1, 2):
            out.add(_canon(Decimal(str(round(base, digits)))))
    out.add(_canon(Decimal(str(abs(x)))))
    return out


def _is_id_key(key: str) -> bool:
    k = key.lower()
    return k in _ID_KEYS or k.endswith(("_id", "_ids", "_hash", "_url", "_key"))


def allowed_numbers(evidence: Evidence, *texts: str) -> set[str]:
    """Canonical numbers present in the evidence (ids excluded) and in the given texts."""
    out: set[str] = set()

    def walk(node: Any, key: str = "") -> None:
        if key and _is_id_key(key) and not isinstance(node, (dict, list, tuple)):
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k))
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v, key)
        elif isinstance(node, bool) or node is None:
            return
        elif isinstance(node, (int, float, Decimal)):
            out.update(_variants(float(node)))
        elif isinstance(node, str):
            for n in _numbers_in_text(node):
                out.update(_variants(float(n)))

    walk(evidence)
    for t in texts:
        out.update(_numbers_in_text(t))
    return out


def numbers_ok(text: str, allowed: set[str]) -> bool:
    return all(n in allowed for n in _numbers_in_text(text))


def _template_texts(t: Explanation) -> list[str]:
    return [t.title, t.summary, *(f["text"] for f in t.findings)]


def _for_model(evidence: Evidence) -> Evidence:
    """Evidence without names: the model only needs keys and numbers."""
    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items()
                    if k not in ("name", "display_name", "bidder_name", "name_norm")}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node
    return strip(evidence)


def rewrite_with_model(provider: TextProvider, evidence: Evidence,
                       template: Explanation) -> tuple[str, str] | None:
    """DeepSeek rewrite of title and summary, or None when the answer is unusable."""
    user = json.dumps(
        {"dowody": _for_model(evidence),
         "szablon": {"title": template.title, "summary": template.summary,
                     "ustalenia": [f["text"] for f in template.findings]}},
        ensure_ascii=False, default=str,
    )
    try:
        answer = provider.complete_json(task=TASK, system=SYSTEM_PROMPT, user=user)
    except Exception as e:  # noqa: BLE001 - any provider failure keeps the template
        log.warning("signal_explain: provider failed (%s), keeping template", type(e).__name__)
        return None
    if not isinstance(answer, dict):
        return None
    title, summary = answer.get("title"), answer.get("summary")
    if not isinstance(title, str) or not isinstance(summary, str):
        log.warning("signal_explain: answer without title/summary, keeping template")
        return None
    title, summary = _no_dashes(" ".join(title.split())), _no_dashes(" ".join(summary.split()))
    if not title or not summary or len(title) > MAX_TITLE or len(summary) > MAX_SUMMARY:
        log.warning("signal_explain: answer empty or too long, keeping template")
        return None
    allowed = allowed_numbers(evidence, *_template_texts(template))
    if not numbers_ok(title, allowed) or not numbers_ok(summary, allowed):
        log.warning("signal_explain: answer has numbers outside the evidence, keeping template")
        return None
    return title, summary


# --- storage -----------------------------------------------------------------------------

def _per_item_from_oecd(oecd: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    if isinstance(oecd, dict):
        for item, v in oecd.items():
            p = _num(_first(v, "p", "probability", "noul")) if isinstance(v, dict) else _num(v)
            if p is not None:
                out[str(item)] = p
    elif isinstance(oecd, list):
        for v in oecd:
            if isinstance(v, dict) and v.get("item"):
                p = _num(_first(v, "p", "probability", "noul"))
                if p is not None:
                    out[str(v["item"])] = p
    return out


def with_member_offers(conn, evidence: Evidence) -> Evidence:
    """Copy of the evidence whose example lots carry the members' offers (for the ladder).

    The evidence package keeps only who bid; prices of the members' valid offers (documents
    after the cross-check, BZP scale applied, offers outside the BZP range left out) are read
    from `bids`. Lots that already have `offers` are left as they are.
    """
    lots = evidence.get("lots")
    if not isinstance(lots, list) or not lots:
        return evidence
    member_ids = []
    for m in evidence.get("members") or []:
        raw = m.get("bidder_id") if isinstance(m, dict) else m
        try:
            member_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    lot_ids: dict[int, int] = {}  # position in `lots` -> lot id
    for pos, lot in enumerate(lots):
        if isinstance(lot, dict) and not lot.get("offers"):
            try:
                lot_ids[pos] = int(_first(lot, "id", "lot_id"))
            except (TypeError, ValueError):
                continue
    if not member_ids or not lot_ids:
        return evidence
    rows = conn.execute(
        "select b.lot_id, b.bidder_id, b.price * b.price_scale as price"
        " from bids b join documents d on d.id = b.document_id and d.status = 'parsed'"
        " where b.lot_id = any(%s::bigint[]) and b.bidder_id = any(%s::bigint[])"
        "  and b.price is not null and b.outside_bzp_range is not true"
        " order by b.lot_id, b.bidder_id",
        (sorted(set(lot_ids.values())), member_ids),
    ).fetchall()
    offers: dict[int, list[dict]] = {}
    for r in rows:
        offers.setdefault(int(r["lot_id"]), []).append(
            {"bidder_id": int(r["bidder_id"]), "price": str(r["price"])})
    new_lots = [
        {**lot, "offers": offers[lot_ids[pos]]} if lot_ids.get(pos) in offers else lot
        for pos, lot in enumerate(lots)
    ]
    return {**evidence, "lots": new_lots}


def _hash(evidence: Evidence) -> str:
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def explain_signal(conn, provider: TextProvider | None, signal_id: str, *,
                   force: bool = False) -> str:
    """Write the explanation of a signal for its current evidence version; return its source.

    Every signal gets the template. Only `published` signals are sent to the provider (when
    one is given). An explanation already stored for the current `evidence_hash` is kept
    without calling the model again, unless `force` is set.
    """
    row = conn.execute(
        "select signal_id, status, probability, strength, oecd, evidence_hash, evidence"
        " from signals where signal_id = %s", (signal_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"signal {signal_id} not found")
    evidence = row["evidence"] or {}
    ev_hash = row["evidence_hash"] or _hash(evidence)

    if not force:
        existing = conn.execute(
            "select source from explanations where signal_id = %s and evidence_hash = %s",
            (signal_id, ev_hash),
        ).fetchone()
        if existing is not None:
            return existing["source"]

    decision = StoredDecision(
        probability=float(row["probability"] or 0.0),
        strength=float(row["strength"] or 0),
        per_item=_per_item_from_oecd(row["oecd"]),
    )
    template = template_explanation(with_member_offers(conn, evidence), decision)
    title, summary, source = template.title, template.summary, "template"
    if row["status"] == "published" and provider is not None:
        rewritten = rewrite_with_model(provider, evidence, template)
        if rewritten is not None:
            title, summary = rewritten
            source = "deepseek"

    conn.execute(
        "insert into explanations(signal_id, evidence_hash, source, title, summary, findings,"
        " alternatives) values (%s, %s, %s, %s, %s, %s, %s)"
        " on conflict (signal_id, evidence_hash) do update set source = excluded.source,"
        " title = excluded.title, summary = excluded.summary, findings = excluded.findings,"
        " alternatives = excluded.alternatives, created_at = now()",
        (signal_id, ev_hash, source, title, summary, Jsonb(template.findings),
         template.alternatives),
    )
    conn.commit()
    return source
