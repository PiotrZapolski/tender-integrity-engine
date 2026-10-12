"""Testy parowe z listy kontrolnej OECD liczone na tabeli `pairs` i ofertach.

Para to wiersz `pairs` (zbudowany przez `tie.engine.graph.build_pairs`): dwie firmy `a < b`
w jednym rynku, które złożyły ofertę w co najmniej dwóch tych samych częściach. Części rynku
bierzemy z `lot_market` tego przebiegu. Wyniki trafiają do `test_results` z `level = 'pair'`
i kluczem encji `pair_key(a, b, market_id)` = `"<a>-<b>|<market_id>"` (ta sama para może
wystąpić w kilku rynkach).

- `3.1.6` rotacja (pary z `n_together >= 5`): `(1 - |a_wins - b_wins| / (a_wins + b_wins))`
  razy udział części wspólnych, w których wygrał ktoś z pary, `(a_wins + b_wins) / n_together`.
  Wysoka wartość = sygnał. Zapis także w `pairs.rotation`. Para z k członków kartelu, którzy
  wygrywają po kolei, ma wartość co najwyżej 2/k (pozostałe wygrane należą do reszty kartelu).
- `3.3.6` wzór cen: współczynnik zmienności (odchylenie standardowe z próby / średnia)
  stosunku cen w częściach wspólnych. Stosunek liczymy w kierunku "przegrany z pary / zwycięzca
  z pary" w częściach, w których wygrała dokładnie jedna firma z pary (min. 4 takie części).
  Kierunek wg zwycięzcy, a nie stały B/A: oferta osłonowa jest o stały procent droższa od
  oferty wyznaczonego zwycięzcy, a zwycięzcy w parze się zmieniają; stały B/A skakałby wtedy
  między 1.06 a 0.94 i ukrywał wzór. Ceny tylko z ofert ważnych (dokument `parsed`, bez
  `outside_bzp_range`), po przeliczeniu przez `price_scale`. Niski CV = sygnał: `value = cv`,
  percentyl odwrócony. Zapis w `pairs.ratio_cv`.
- `3.1.7` konsorcjum: część wygrana przez konsorcjum (`lots.consortium`), którego liderem jest
  jedna firma pary (`winner_bidder_id`), a druga występuje w nazwie konsorcjum (nazwa oferty
  zwycięskiej z dokumentu otwarcia lub nazwa lidera w BZP; dopasowanie po nazwie albo NIP),
  przy czym przed tą częścią obie firmy co najmniej 2 razy złożyły osobne oferty w tej samej
  części rynku. Wartość = liczba takich części; wynik tylko dla par z co najmniej jednym
  zdarzeniem, bez percentyla (zdarzenie rzadkie, liczba, nie rozkład).
- `3.2.11` wspólne dane: `shared_account or shared_address`, wartość 1 lub 0 dla każdej pary,
  bez percentyla.

Percentyl liczymy wyłącznie w obrębie rynku: `rank / n` (rank = liczba wartości nie większych,
dla testu odwróconego nie mniejszych), `null` gdy w rynku jest mniej niż 10 obserwacji testu;
`n_obs` = liczba obserwacji testu w rynku. Ponowne wywołanie w tym samym `run_id` nadpisuje
wyniki.
"""

from __future__ import annotations

import re
import statistics
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from tie.identity.names import _COMPANY_TOKENS, normalize_name

ROTATION = "3.1.6"
PRICE_PATTERN = "3.3.6"
CONSORTIUM = "3.1.7"
SHARED_DATA = "3.2.11"
TEST_IDS = (ROTATION, PRICE_PATTERN, CONSORTIUM, SHARED_DATA)

MIN_TOGETHER_ROTATION = 5
MIN_RATIO_LOTS = 4
MIN_SEPARATE_STARTS = 2
MIN_MARKET_OBS = 10
MAX_EVIDENCE_LOTS = 20
# A name core shorter than this is too generic to be looked up inside a consortium name.
MIN_CORE_CHARS = 8

_NIP_LIKE = re.compile(r"\d[\d\s-]{8,14}\d")


def pair_key(a: int, b: int, market_id: str) -> str:
    """`test_results.entity_key` of a pair; `a < b`."""
    return f"{a}-{b}|{market_id}"


@dataclass
class _Result:
    test_id: str
    key: str
    market_id: str
    value: float
    evidence: dict
    percentile: float | None = None
    n_obs: int | None = None


@dataclass
class _Bid:
    price: float
    won: bool


@dataclass
class _Market:
    # bidder_id -> lot_id -> valid priced bid (lowest price if a firm has several offers)
    priced: dict[int, dict[int, _Bid]] = field(default_factory=lambda: defaultdict(dict))


def compute_pair_tests(conn: psycopg.Connection, run_id: int) -> int:
    """Compute 3.1.6, 3.3.6, 3.1.7 and 3.2.11 for every pair of `run_id`; returns the number
    of `test_results` rows written. Commits."""
    pairs = conn.execute(
        "select a, b, market_id, n_together, a_wins, b_wins, other_wins, shared_account,"
        " shared_address from pairs where run_id = %s order by market_id, a, b",
        (run_id,),
    ).fetchall()
    markets = _priced_bids(conn, run_id)

    results: list[_Result] = []
    rotation: dict[tuple[int, int, str], float] = {}
    ratio_cv: dict[tuple[int, int, str], float] = {}
    for p in pairs:
        a, b, mid = p["a"], p["b"], p["market_id"]
        key = pair_key(a, b, mid)

        rot = _rotation(p)
        if rot is not None:
            rotation[(a, b, mid)] = rot
            results.append(_Result(ROTATION, key, mid, rot, {
                "a": a, "b": b, "n_together": p["n_together"], "a_wins": p["a_wins"] or 0,
                "b_wins": p["b_wins"] or 0, "other_wins": p["other_wins"] or 0,
            }))

        market = markets.get(mid)
        if market is not None:
            cv = _ratio_cv(market, a, b)
            if cv is not None:
                ratio_cv[(a, b, mid)] = cv[0]
                results.append(_Result(PRICE_PATTERN, key, mid, cv[0], cv[1]))

        shared_account = bool(p["shared_account"])
        shared_address = bool(p["shared_address"])
        results.append(_Result(
            SHARED_DATA, key, mid, 1.0 if (shared_account or shared_address) else 0.0,
            {"a": a, "b": b, "shared_account": shared_account, "shared_address": shared_address},
        ))

    results.extend(_consortium_results(conn, run_id, pairs))

    _percentiles(results, ROTATION, higher_is_signal=True)
    _percentiles(results, PRICE_PATTERN, higher_is_signal=False)

    try:
        with conn.cursor() as cur:
            cur.execute(
                "delete from test_results where run_id = %s and level = 'pair'"
                " and test_id = any(%s)",
                (run_id, list(TEST_IDS)),
            )
            cur.execute(
                "update pairs set rotation = null, ratio_cv = null where run_id = %s", (run_id,)
            )
            cur.executemany(
                "insert into test_results(run_id, test_id, level, entity_key, market_id, value,"
                " percentile, n_obs, evidence) values (%s, %s, 'pair', %s, %s, %s, %s, %s, %s)",
                [(run_id, r.test_id, r.key, r.market_id, r.value, r.percentile, r.n_obs,
                  Jsonb(r.evidence)) for r in results],
            )
            updates = [
                (rotation.get(k), ratio_cv.get(k), run_id, k[0], k[1], k[2])
                for k in sorted(set(rotation) | set(ratio_cv))
            ]
            cur.executemany(
                "update pairs set rotation = %s, ratio_cv = %s"
                " where run_id = %s and a = %s and b = %s and market_id = %s",
                updates,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(results)


def _rotation(p: dict) -> float | None:
    n = p["n_together"] or 0
    if n < MIN_TOGETHER_ROTATION:
        return None
    a_wins, b_wins = p["a_wins"] or 0, p["b_wins"] or 0
    wins = a_wins + b_wins
    if wins == 0:
        return 0.0
    balance = 1.0 - abs(a_wins - b_wins) / wins
    return balance * min(1.0, wins / n)


def _priced_bids(conn: psycopg.Connection, run_id: int) -> dict[str, _Market]:
    """Valid priced bids of the lots of each market of the run."""
    rows = conn.execute(
        "select lm.market_id, b.lot_id, b.bidder_id, b.price * b.price_scale as price,"
        " (b.is_winner or l.winner_bidder_id = b.bidder_id) as won"
        " from lot_market lm"
        " join lots l on l.id = lm.lot_id"
        " join bids b on b.lot_id = lm.lot_id"
        " join documents d on d.id = b.document_id"
        " where lm.run_id = %s and d.status = 'parsed' and b.bidder_id is not null"
        " and b.price is not null and b.price > 0 and b.outside_bzp_range is not true",
        (run_id,),
    ).fetchall()
    markets: dict[str, _Market] = defaultdict(_Market)
    for r in rows:
        price = float(r["price"])
        lots = markets[r["market_id"]].priced[r["bidder_id"]]
        prev = lots.get(r["lot_id"])
        won = bool(r["won"])
        if prev is None:
            lots[r["lot_id"]] = _Bid(price, won)
        else:
            lots[r["lot_id"]] = _Bid(min(prev.price, price), prev.won or won)
    return markets


def _ratio_cv(market: _Market, a: int, b: int) -> tuple[float, dict] | None:
    bids_a, bids_b = market.priced.get(a), market.priced.get(b)
    if not bids_a or not bids_b:
        return None
    ratios: list[float] = []
    lots: list[int] = []
    for lot_id in sorted(bids_a.keys() & bids_b.keys()):
        x, y = bids_a[lot_id], bids_b[lot_id]
        if x.won == y.won:
            continue
        winner, loser = (x, y) if x.won else (y, x)
        ratios.append(loser.price / winner.price)
        lots.append(lot_id)
    if len(ratios) < MIN_RATIO_LOTS:
        return None
    mean = statistics.fmean(ratios)
    cv = statistics.stdev(ratios) / mean
    return cv, {
        "a": a, "b": b, "n_lots": len(ratios), "mean_ratio": round(mean, 4),
        "min_ratio": round(min(ratios), 4), "max_ratio": round(max(ratios), 4),
        "lots": lots[:MAX_EVIDENCE_LOTS],
    }


def _name_patterns(name: str | None) -> list[str]:
    """Normalized forms of a firm name to look up inside a consortium name."""
    norm = normalize_name(name or "")
    out = [norm] if len(norm) >= MIN_CORE_CHARS else []
    core = " ".join(w for w in norm.split() if w not in _COMPANY_TOKENS)
    if len(core) >= MIN_CORE_CHARS and core != norm:
        out.append(core)
    return out


def _contains(text_norm: str, pattern: str) -> bool:
    return f" {pattern} " in f" {text_norm} "


def _nips_in(text: str) -> set[str]:
    out = set()
    for m in _NIP_LIKE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) == 10:
            out.add(digits)
    return out


def _consortium_results(
    conn: psycopg.Connection, run_id: int, pairs: list[dict]
) -> list[_Result]:
    """3.1.7: consortium lots won by a pair that bid separately at least twice before."""
    partners: dict[tuple[int, str], set[int]] = defaultdict(set)
    for p in pairs:
        partners[(p["a"], p["market_id"])].add(p["b"])
        partners[(p["b"], p["market_id"])].add(p["a"])
    if not partners:
        return []

    won = conn.execute(
        "select lm.market_id, l.id as lot_id, l.winner_bidder_id as leader, p.published_at,"
        " w.name as leader_name,"
        " coalesce((select array_agg(b.bidder_name_raw) from bids b"
        "   where b.lot_id = l.id and (b.is_winner or b.bidder_id = l.winner_bidder_id)),"
        "   '{}') as raw_names"
        " from lot_market lm"
        " join lots l on l.id = lm.lot_id"
        " join procedures p on p.ocds_id = l.procedure_id"
        " join bidders w on w.id = l.winner_bidder_id"
        " where lm.run_id = %s and l.consortium",
        (run_id,),
    ).fetchall()

    candidates: list[tuple[dict, int]] = []
    partner_ids: set[int] = set()
    for lot in won:
        if partners.get((lot["leader"], lot["market_id"])):
            partner_ids |= partners[(lot["leader"], lot["market_id"])]
    if not partner_ids:
        return []
    firms = {
        r["id"]: r for r in conn.execute(
            "select id, name, nip from bidders where id = any(%s)", (sorted(partner_ids),)
        ).fetchall()
    }
    for lot in won:
        texts = [t for t in [*(lot["raw_names"] or []), lot["leader_name"]] if t]
        if not texts:
            continue
        norms = [normalize_name(t) for t in texts]
        nips = set().union(*(_nips_in(t) for t in texts))
        for partner in sorted(partners.get((lot["leader"], lot["market_id"]), ())):
            firm = firms.get(partner)
            if firm is None:
                continue
            named = any(_contains(n, pat) for n in norms for pat in _name_patterns(firm["name"]))
            if named or (firm["nip"] and firm["nip"] in nips):
                candidates.append((lot, partner))
    if not candidates:
        return []

    # One query for the parsed offers of every firm involved in a candidate, then the
    # "separate starts before the consortium lot" count per candidate in Python.
    involved = sorted({lot["leader"] for lot, _ in candidates} | {p for _, p in candidates})
    starts: dict[tuple[str, int], dict[int, Any]] = defaultdict(dict)
    for r in conn.execute(
        "select distinct lm.market_id, b.lot_id, b.bidder_id, p.published_at"
        " from bids b"
        " join documents d on d.id = b.document_id and d.status = 'parsed'"
        " join lot_market lm on lm.lot_id = b.lot_id and lm.run_id = %s"
        " join lots l on l.id = b.lot_id"
        " join procedures p on p.ocds_id = l.procedure_id"
        " where b.bidder_id = any(%s)",
        (run_id, involved),
    ).fetchall():
        starts[(r["market_id"], r["bidder_id"])][r["lot_id"]] = r["published_at"]

    events: dict[tuple[int, int, str], list[dict]] = defaultdict(list)
    for lot, partner in candidates:
        leader, mid, before = lot["leader"], lot["market_id"], lot["published_at"]
        mine, theirs = starts.get((mid, leader), {}), starts.get((mid, partner), {})
        separate = 0
        if before is not None:
            separate = sum(1 for lot_id in mine.keys() & theirs.keys()
                           if mine[lot_id] is not None and mine[lot_id] < before)
        if separate >= MIN_SEPARATE_STARTS:
            a, b = sorted((leader, partner))
            events[(a, b, lot["market_id"])].append(
                {"lot_id": lot["lot_id"], "leader": leader, "separate_before": separate}
            )

    return [
        _Result(CONSORTIUM, pair_key(a, b, mid), mid, float(len(ev)), {
            "a": a, "b": b, "events": len(ev), "lots": ev[:MAX_EVIDENCE_LOTS],
        })
        for (a, b, mid), ev in sorted(events.items())
    ]


def _percentiles(results: list[_Result], test_id: str, *, higher_is_signal: bool) -> None:
    by_market: dict[str, list[_Result]] = defaultdict(list)
    for r in results:
        if r.test_id == test_id:
            by_market[r.market_id].append(r)
    for group in by_market.values():
        n = len(group)
        values = sorted(r.value for r in group)
        for r in group:
            r.n_obs = n
            if n < MIN_MARKET_OBS:
                r.percentile = None
            elif higher_is_signal:
                r.percentile = bisect_right(values, r.value) / n
            else:
                r.percentile = (n - bisect_left(values, r.value)) / n


# Pure pair screens shared with `tie.validation` (gold cases and background pairs in memory):
# `rotation_index(row)` takes a mapping with `n_together`, `a_wins`, `b_wins`;
# `price_ratio_cv(priced, a, b)` takes a `PricedBids` filled per bidder and lot.
PricedBids = _Market
PricedBid = _Bid
rotation_index = _rotation
price_ratio_cv = _ratio_cv
