"""Pakiet dowodów klastra: zwięzły JSON liczb i percentyli dla modelu decyzyjnego.

Pakiet zawiera wyłącznie `bidder_id` członków (nigdy nazw firm: JDG maskują widoki `api.*`),
rynek, liczbę części, okres, wyniki testów OECD (A/C) z `test_results` dla klastra, jego par,
członków i części oraz do 20 przykładowych części. Wszystko, co trafia do pakietu, jest
deterministyczne i niezależne od `run_id`, więc `evidence_hash` zmienia się tylko wtedy, gdy
zmieniły się dane. Kanoniczny JSON pakietu ma najwyżej 6000 znaków.

Kształt pakietu:

    {
      "cluster_id": "...", "market": {"market_id", "cpv_group", "province", "level", "n_lots"},
      "members": [{"key": "A", "bidder_id": 123}, ...],      # posortowane po bidder_id
      "n_lots": 30, "density": 1.0, "n_procedures": 30, "total_value": 1234567.89,
      "period": {"from": "2023-03-15", "to": "2025-11-20"},
      "oecd": {
        "3.1.6": {"level": "pair", "value": 0.93, "pct": 0.98, "n": 3,
                  "top": [{"e": "A-B", "v": 0.93, "p": 0.98}, ...]},     # do 3 najsilniejszych
        "3.3.7": {"level": "lot", "value": 2.1, "pct": 0.95, "n": 30, "mean": 2.3, "high": 25}
      },
      "lots": [{"id": 1, "date": "2025-01-02", "price": 1.0, "est": 1.0, "winner": "A",
                "bidders": ["A", "B"], "n_offers": 3}, ...]          # do 20, rosnąco po dacie
    }

Znaczenie pól OECD: `value` i `pct` to wynik i percentyl w rynku najsilniejszej encji
(dla części: mediany po częściach klastra), `n` liczba encji z wynikiem, `top` encje
(`klaster`, `rynek`, klucz członka `A` albo pary `A-B`), dla części `high` to liczba części
z percentylem co najmniej 0.9, a `mean` średnia wartość. `winner` w części to klucz członka,
`inny` dla zwycięzcy spoza klastra albo null.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg

from tie.engine.oecd import CATALOG

MAX_CHARS = 6000
MAX_LOTS = 20
MAX_TOP = 3
HIGH_PCT = 0.9
# Punkty OECD sprawdzane automatycznie (A) lub częściowo (C): tylko one trafiają do dowodów.
CHECKED = {c.item: c.status for c in CATALOG if c.status in ("A", "C")}
# Testy, w których niższa wartość oznacza silniejszy sygnał (gdy brak percentyla).
LOWER_IS_STRONGER = {"3.3.6", "3.3.10"}
OTHER_WINNER = "inny"


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def evidence_hash(evidence: dict) -> str:
    """sha256 kanonicznego JSON pakietu (klucze posortowane, bez spacji)."""
    return hashlib.sha256(canonical_json(evidence).encode("utf-8")).hexdigest()


def _key(i: int) -> str:
    """A, B, ..., Z, AA, AB, ... dla kolejnych członków."""
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


def _num(x: Any, digits: int = 4) -> float | None:
    if x is None:
        return None
    return round(float(x), digits)


def _ids(entity_key: str, market_id: str | None) -> list[int]:
    """Liczby całkowite z klucza encji po usunięciu z niego identyfikatora rynku.

    Klucze encji testów firmowych i parowych zawierają `bidder_id` (np. `123`, `123:456`,
    `123|cpv5_prov:45210:30`); kolejność liczb w kluczu to kolejność identyfikatorów.
    """
    if market_id:
        entity_key = entity_key.replace(market_id, " ")
    return [int(t) for t in re.findall(r"\d+", entity_key)]


def _strength(test_id: str, pct: float | None, value: float | None) -> tuple[float, float]:
    """Klucz sortowania encji od najsilniejszego sygnału: najpierw percentyl, potem wartość."""
    v = 0.0 if value is None else float(value)
    if test_id in LOWER_IS_STRONGER:
        v = -v
    return (-1.0 if pct is None else float(pct), v)


def _cluster_lots(conn: psycopg.Connection, market_id: str, members: list[int]) -> list[dict]:
    """Części rynku, w których co najmniej dwóch członków złożyło ofertę (z dokumentów
    po kontroli krzyżowej), od najstarszej."""
    return conn.execute(
        "with mb as ("
        " select b.lot_id, array_agg(distinct b.bidder_id order by b.bidder_id) as member_ids"
        " from bids b"
        " join documents d on d.id = b.document_id and d.status = 'parsed'"
        " join lot_market lm on lm.lot_id = b.lot_id and lm.market_id = %(market)s"
        " where b.bidder_id = any(%(members)s::bigint[])"
        " group by b.lot_id having count(distinct b.bidder_id) >= 2)"
        " select l.id as lot_id, l.procedure_id, l.price_winner, l.estimated_value,"
        "  l.contract_value, l.winner_bidder_id, l.n_offers, mb.member_ids,"
        "  coalesce(l.contract_date, p.published_at::date) as day"
        " from mb join lots l on l.id = mb.lot_id"
        " join procedures p on p.ocds_id = l.procedure_id"
        " order by day, l.id",
        {"market": market_id, "members": members},
    ).fetchall()


def _test_rows(conn: psycopg.Connection, run_id: int, cluster_id: str,
               market_id: str) -> list[dict]:
    return conn.execute(
        "select test_id, level, entity_key, market_id, value, percentile, n_obs"
        " from test_results"
        " where run_id = %(run)s and test_id = any(%(items)s) and ("
        "  (level = 'cluster' and position(%(cid)s in entity_key) > 0)"
        "  or (level = 'market' and (entity_key = %(market)s or market_id = %(market)s))"
        "  or (level in ('pair', 'firm', 'lot')"
        "      and (market_id = %(market)s or market_id is null)))"
        " order by test_id, level, entity_key",
        {"run": run_id, "items": list(CHECKED), "cid": cluster_id, "market": market_id},
    ).fetchall()


def _oecd(rows: list[dict], market_id: str, key_of: dict[int, str],
          lot_ids: set[int]) -> dict[str, dict]:
    """Wyniki testów przypisane do klastra, zagregowane per punkt OECD."""
    entities: dict[str, list[tuple[str, str, float | None, float | None]]] = {}
    lots: dict[str, list[tuple[float | None, float | None]]] = {}
    for r in rows:
        test_id, level = r["test_id"], r["level"]
        value, pct = _num(r["value"]), _num(r["percentile"])
        ids = _ids(r["entity_key"], r["market_id"] or market_id)
        if level == "cluster":
            label = "klaster"
        elif level == "market":
            label = "rynek"
        elif level == "firm":
            if not ids or ids[0] not in key_of:
                continue
            label = key_of[ids[0]]
        elif level == "pair":
            if len(ids) < 2 or ids[0] == ids[1] or not (ids[0] in key_of and ids[1] in key_of):
                continue
            label = "-".join(sorted((key_of[ids[0]], key_of[ids[1]])))
        else:  # lot
            if not ids or ids[0] not in lot_ids:
                continue
            lots.setdefault(test_id, []).append((value, pct))
            continue
        entities.setdefault(test_id, []).append((level, label, value, pct))

    out: dict[str, dict] = {}
    for test_id, ents in entities.items():
        ents.sort(key=lambda e: _strength(test_id, e[3], e[2]), reverse=True)
        level, _, value, pct = ents[0]
        out[test_id] = {
            "level": level, "value": value, "pct": pct, "n": len(ents),
            "top": [{"e": e[1], "v": e[2], "p": e[3]} for e in ents[:MAX_TOP]],
        }
    for test_id, vals in lots.items():
        if test_id in out:
            continue
        values = [v for v, _ in vals if v is not None]
        pcts = [p for _, p in vals if p is not None]
        out[test_id] = {
            "level": "lot",
            "value": _num(statistics.median(values)) if values else None,
            "pct": _num(statistics.median(pcts)) if pcts else None,
            "n": len(vals),
            "mean": _num(statistics.fmean(values)) if values else None,
            "high": sum(1 for p in pcts if p >= HIGH_PCT),
        }
    return dict(sorted(out.items()))


def _lot_entry(r: dict, key_of: dict[int, str]) -> dict:
    winner = r["winner_bidder_id"]
    return {
        "id": r["lot_id"],
        "date": r["day"].isoformat() if isinstance(r["day"], date) else None,
        "price": _num(r["price_winner"], 2),
        "est": _num(r["estimated_value"], 2),
        "winner": None if winner is None else key_of.get(winner, OTHER_WINNER),
        "bidders": [key_of[m] for m in r["member_ids"] if m in key_of],
        "n_offers": r["n_offers"],
    }


def _fit(evidence: dict) -> dict:
    """Przytnij pakiet do MAX_CHARS: najpierw listy `top` do 1, potem najstarsze części."""
    def fits() -> bool:
        return len(canonical_json(evidence)) <= MAX_CHARS

    if fits():
        return evidence
    for item in evidence["oecd"].values():
        if "top" in item:
            item["top"] = item["top"][:1]
    while evidence["lots"] and not fits():
        evidence["lots"].pop(0)
    if not fits():
        for item in evidence["oecd"].values():
            item.pop("top", None)
    if not fits():
        raise ValueError(f"evidence package does not fit in {MAX_CHARS} characters")
    return evidence


def build_evidence(conn: psycopg.Connection, run_id: int, cluster_id: str) -> dict:
    """Pakiet dowodów klastra `cluster_id` z przebiegu `run_id` (kształt w opisie modułu)."""
    cluster = conn.execute(
        "select cluster_id, market_id, members, n_lots, density from clusters"
        " where run_id = %s and cluster_id = %s",
        (run_id, cluster_id),
    ).fetchone()
    if cluster is None:
        raise LookupError(f"cluster {cluster_id} not found in run {run_id}")
    market_id = cluster["market_id"]
    members = sorted({int(m) for m in (cluster["members"] or [])})
    key_of = {bidder_id: _key(i) for i, bidder_id in enumerate(members)}

    market = conn.execute(
        "select market_id, cpv_group, province, level, n_lots from markets where market_id = %s",
        (market_id,),
    ).fetchone()
    lots = _cluster_lots(conn, market_id, members)
    days = [r["day"] for r in lots if r["day"] is not None]
    total = sum(
        (r["contract_value"] if r["contract_value"] is not None else r["price_winner"])
        or Decimal(0)
        for r in lots
    )
    rows = _test_rows(conn, run_id, cluster_id, market_id)

    evidence = {
        "cluster_id": cluster_id,
        "market": {
            "market_id": market_id,
            "cpv_group": market["cpv_group"] if market else None,
            "province": market["province"] if market else None,
            "level": market["level"] if market else None,
            "n_lots": market["n_lots"] if market else None,
        },
        "members": [{"key": key_of[m], "bidder_id": m} for m in members],
        "n_lots": cluster["n_lots"] if cluster["n_lots"] is not None else len(lots),
        "density": _num(cluster["density"]),
        "n_procedures": len({r["procedure_id"] for r in lots}),
        "total_value": _num(total, 2),
        "period": {
            "from": min(days).isoformat() if days else None,
            "to": max(days).isoformat() if days else None,
        },
        "oecd": _oecd(rows, market_id, key_of, {r["lot_id"] for r in lots}),
        "lots": [_lot_entry(r, key_of) for r in lots[-MAX_LOTS:]],
    }
    return _fit(evidence)
