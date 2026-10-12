"""Tests A, B and C of the validation (spec section 9).

- A `screen_known_cartels`: the bids of each UOKiK case scored by the engine's screens and
  placed in the background of their market (`tie.validation.screens`). Rigged tenders and
  pairs of colluders against the non-rigged tenders and the other pairs of the same
  decisions (control).
- B `engine_on_our_data`: do the colluders of a UOKiK case or a KIO ruling, found among our
  bidders by NIP or normalized name, sit together in one engine cluster, and what did the
  engine decide for it (`signals`)? The engine run is the latest finished run whose `as_of`
  is before the decision (lead test); when there is none, the latest finished run, marked
  `lead = false`.
- C `false_alarms`: a random sample of clusters of the same markets (without known
  colluders) and the share of them above the review and publish thresholds.
"""

from __future__ import annotations

import random
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import psycopg

from tie.identity.names import normalize_name
from tie.validation.gold import Firm, KioRuling, UokikCase, same_firm
from tie.validation.screens import (
    LOT_SCREENS,
    PAIR_SCREENS,
    TOP_PCT,
    Backgrounds,
    Offer,
    Placement,
    Tender,
    lot_screens,
    pair_screens,
)

# A case counts as detected when at least this share of its evaluable rigged tenders is in
# the top 5%, or when at least one pair of colluders is.
CASE_TENDER_SHARE = 0.5
# Items whose per-item probability in a signal reaches this value are reported as fired.
ITEM_FIRED = 0.6


# --- Test A ----------------------------------------------------------------------------

@dataclass
class Unit:
    """One scored tender or pair of a case."""

    kind: str  # 'tender' | 'pair'
    rigged: bool
    placements: dict[str, Placement] = field(default_factory=dict)

    @property
    def best(self) -> float | None:
        pcts = [p.percentile for p in self.placements.values() if p.percentile is not None]
        return max(pcts) if pcts else None

    @property
    def evaluable(self) -> bool:
        return self.best is not None

    @property
    def top(self) -> bool:
        return self.best is not None and self.best >= TOP_PCT

    @property
    def fired(self) -> list[str]:
        return sorted(s for s, p in self.placements.items() if p.top)


def _price(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        price = Decimal(str(value))
    except ArithmeticError:
        return None
    return price if price.is_finite() and price > 0 else None


class _FirmIndex:
    """Integer key per firm of one decision; spellings of one firm share the key."""

    def __init__(self) -> None:
        self.norms: list[str] = []

    def key(self, name: str) -> int:
        norm = normalize_name(name)
        for i, known in enumerate(self.norms):
            if same_firm(norm, known):
                return i
        self.norms.append(norm)
        return len(self.norms) - 1


def case_units(case: UokikCase, backgrounds: Backgrounds) -> list[Unit]:
    """Scored tenders and pairs of one UOKiK case."""
    firms = _FirmIndex()
    colluders = {firms.key(c.name) for c in case.colluders}
    tenders: list[tuple[bool, Tender]] = []
    for i, t in enumerate(case.tenders):
        offers = [Offer(firms.key(b["bidder"]), _price(b.get("price")), bool(b.get("won")))
                  for b in t.get("bids") or []
                  if isinstance(b, dict) and (b.get("bidder") or "").strip()]
        tenders.append((bool(t.get("rigged")), Tender(i, offers)))

    def place(screens: dict[str, float]) -> dict[str, Placement]:
        return {s: backgrounds.place(s, v, case.cpv, case.province)
                for s, v in sorted(screens.items())}

    units = [Unit("tender", rigged, place(lot_screens(t))) for rigged, t in tenders]
    for (a, b), screens in sorted(pair_screens([t for _, t in tenders]).items()):
        if screens:
            units.append(Unit("pair", a in colluders and b in colluders, place(screens)))
    return units


def _rate(units: list[Unit]) -> dict:
    evaluable = [u for u in units if u.evaluable]
    top = sum(1 for u in evaluable if u.top)
    return {"n": len(units), "evaluable": len(evaluable), "top5": top,
            "share": round(top / len(evaluable), 4) if evaluable else None}


def _levels(units: list[Unit]) -> dict[str, int]:
    return dict(Counter(p.level or "none" for u in units for p in u.placements.values()))


def case_summary(case: UokikCase, units: list[Unit]) -> dict:
    rigged_t = [u for u in units if u.kind == "tender" and u.rigged]
    rigged_p = [u for u in units if u.kind == "pair" and u.rigged]
    ev_t = [u for u in rigged_t if u.evaluable]
    ev_p = [u for u in rigged_p if u.evaluable]
    if case.cpv is None:
        detected, reason = None, "no_market_for_sector"
    elif not ev_t and not ev_p:
        detected = None
        reason = ("no_rigged_tender_with_prices" if not any(u.placements for u in rigged_t)
                  and not rigged_p else "background_too_small")
    else:
        share_t = sum(u.top for u in ev_t) / len(ev_t) if ev_t else 0.0
        detected = share_t >= CASE_TENDER_SHARE or any(u.top for u in ev_p)
        reason = None
    bests = [u.best for u in (ev_t or ev_p)]
    placements = [p for u in rigged_t + rigged_p for p in u.placements.values()
                  if p.level is not None]
    market = None
    if placements:
        level = Counter((p.level, p.cpv_group, p.province) for p in placements).most_common(1)
        market = dict(zip(("level", "cpv_group", "province"), level[0][0], strict=True))
    return {
        "detected": detected,
        "reason": reason,
        "oecd_items": sorted({s for u in rigged_t + rigged_p for s in u.fired}),
        "percentile": round(statistics.median(bests), 4) if bests else None,
        "tenders": _rate(rigged_t),
        "pairs": _rate(rigged_p),
        "control_tenders": _rate([u for u in units if u.kind == "tender" and not u.rigged]),
        "control_pairs": _rate([u for u in units if u.kind == "pair" and not u.rigged]),
        "market": market,
        "levels": _levels(units),
    }


def _item_rates(units: list[Unit]) -> dict[str, dict]:
    out = {}
    for screen in (*LOT_SCREENS, *PAIR_SCREENS):
        row = {}
        for label, rigged in (("rigged", True), ("control", False)):
            scored = [u.placements[screen] for u in units
                      if u.rigged == rigged and screen in u.placements
                      and u.placements[screen].percentile is not None]
            top = sum(1 for p in scored if p.top)
            row[label] = {"evaluable": len(scored), "top5": top,
                          "share": round(top / len(scored), 4) if scored else None}
        r, c = row["rigged"]["share"], row["control"]["share"]
        row["lift"] = round(r / c, 3) if r is not None and c else None
        out[screen] = row
    return out


def screen_known_cartels(cases: list[UokikCase], backgrounds: Backgrounds) -> tuple[dict, dict]:
    """Test A: (metrics, per-case summaries keyed by decision)."""
    all_units: list[Unit] = []
    per_case: dict[str, dict] = {}
    for case in cases:
        units = case_units(case, backgrounds) if case.cpv else []
        all_units.extend(units)
        per_case[case.decision] = case_summary(case, units)
    rigged = [u for u in all_units if u.rigged]
    metrics = {
        "cases": len(cases),
        "cases_evaluable": sum(1 for s in per_case.values() if s["detected"] is not None),
        "cases_detected": sum(1 for s in per_case.values() if s["detected"]),
        "rigged_units": _rate(rigged),
        "rigged_tenders": _rate([u for u in rigged if u.kind == "tender"]),
        "rigged_pairs": _rate([u for u in rigged if u.kind == "pair"]),
        "control_tenders": _rate([u for u in all_units if u.kind == "tender" and not u.rigged]),
        "control_pairs": _rate([u for u in all_units if u.kind == "pair" and not u.rigged]),
        "items": _item_rates(all_units),
        "levels": _levels(all_units),
    }
    return metrics, per_case


# --- Test B ----------------------------------------------------------------------------

def done_runs(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute(
        "select run_id, as_of from engine_runs where status = 'done'"
        " order by as_of desc, run_id desc").fetchall()
    conn.commit()
    return rows


def pick_run(runs: list[dict], cutoff: date | None) -> tuple[dict | None, bool]:
    """Latest finished run before `cutoff` (lead test), else the latest one."""
    if not runs:
        return None, False
    if cutoff is not None:
        for run in runs:
            if run["as_of"] < cutoff:
                return run, True
    return runs[0], False


def match_bidders(conn: psycopg.Connection, firms: list[Firm]) -> dict[int, str]:
    """Our bidder ids of the named firms: {bidder_id: 'nip' | 'name'}."""
    nips = sorted({f.nip for f in firms if f.nip})
    norms = sorted({f.norm for f in firms if f.norm})
    if not nips and not norms:
        return {}
    out: dict[int, str] = {}
    for r in conn.execute(
            "select id, nip, name_norm from bidders where nip = any(%s) or name_norm = any(%s)"
            " order by id", (nips, norms)):
        by_nip = r["nip"] is not None and r["nip"] in nips
        out[r["id"]] = "nip" if by_nip else "name"
    conn.commit()
    return out


def _co_bid_lots(conn: psycopg.Connection, ids: list[int], cutoff: date | None) -> int:
    row = conn.execute(
        "select count(*) as n from ("
        " select b.lot_id from bids b"
        " join documents d on d.id = b.document_id and d.status = 'parsed'"
        " join lots l on l.id = b.lot_id"
        " join procedures p on p.ocds_id = l.procedure_id"
        " where b.bidder_id = any(%s) and (%s::date is null or p.published_at < %s::date)"
        " group by b.lot_id having count(distinct b.bidder_id) >= 2) x",
        (ids, cutoff, cutoff)).fetchone()
    conn.commit()
    return row["n"]


def _procedure_bidders(conn: psycopg.Connection, ocds_id: str) -> set[int]:
    rows = conn.execute(
        "select distinct b.bidder_id from bids b join lots l on l.id = b.lot_id"
        " where l.procedure_id = %s and b.bidder_id is not null", (ocds_id,)).fetchall()
    conn.commit()
    return {r["bidder_id"] for r in rows}


def _best_cluster(conn: psycopg.Connection, run_id: int, ids: list[int]) -> dict | None:
    """The cluster of the run holding most of `ids` (at least two), with its signal."""
    rows = conn.execute(
        "select c.cluster_id, c.market_id, c.members, s.probability, s.status, s.oecd,"
        " s.run_id as signal_run, s.evidence -> 'decision' ->> 'source' as source"
        " from clusters c left join signals s on s.signal_id = c.cluster_id"
        " where c.run_id = %s and c.members && %s::bigint[] order by c.cluster_id",
        (run_id, ids)).fetchall()
    conn.commit()
    best = None
    for r in rows:
        together = len(set(r["members"] or []) & set(ids))
        if together >= 2 and (best is None or together > best[0]):
            best = (together, r)
    if best is None:
        return None
    return {**best[1], "together": best[0]}


def check_engine(conn: psycopg.Connection, runs: list[dict], firms: list[Firm],
                 cutoff: date | None, ocds_id: str | None = None) -> dict:
    """Test B for one gold entry: are its colluders together in an engine cluster?"""
    matched = match_bidders(conn, firms)
    ids = sorted(matched)
    run, lead = pick_run(runs, cutoff)
    out = {
        "cutoff": cutoff.isoformat() if cutoff else None,
        "named": len(firms),
        "matched": len(ids),
        "matched_by": dict(Counter(matched.values())),
        "run_id": run["run_id"] if run else None,
        "run_as_of": run["as_of"].isoformat() if run else None,
        "lead": lead,
        "co_bid_lots": _co_bid_lots(conn, ids, cutoff if lead else None) if len(ids) >= 2 else 0,
        "in_cluster": False,
        "cluster_id": None,
        "probability": None,
        "status": None,
        "decision_source": None,
        "oecd_items": [],
        "reason": None,
    }
    if ocds_id:
        out["procedure_bidders_matched"] = len(_procedure_bidders(conn, ocds_id) & set(ids))
    if run is None:
        out["reason"] = "no_engine_run"
        return out
    if len(ids) < 2:
        out["reason"] = "colluders_not_in_our_data"
        return out
    cluster = _best_cluster(conn, run["run_id"], ids)
    if cluster is None:
        out["reason"] = ("never_bid_together" if out["co_bid_lots"] == 0
                         else "not_in_same_cluster")
        return out
    out.update({
        "in_cluster": True,
        "cluster_id": cluster["cluster_id"],
        "together": cluster["together"],
        "probability": (None if cluster["probability"] is None
                        else round(float(cluster["probability"]), 6)),
        "status": cluster["status"],
        "decision_source": cluster["source"],
        "oecd_items": sorted(k for k, v in (cluster["oecd"] or {}).items()
                             if v is not None and float(v) >= ITEM_FIRED),
        "reason": "ok" if cluster["probability"] is not None else "cluster_without_signal",
    })
    return out


def _b_rate(entries: list[dict], review_threshold: float) -> dict:
    evaluable = [e for e in entries if e["run_id"] is not None and e["matched"] >= 2]
    hits = [e for e in evaluable if e["in_cluster"]]
    flagged = [e for e in hits if (e["probability"] or 0.0) >= review_threshold]
    return {
        "n": len(entries), "evaluable": len(evaluable), "in_cluster": len(hits),
        "flagged": len(flagged), "lead_hits": sum(1 for e in hits if e["lead"]),
        "recall": round(len(hits) / len(evaluable), 4) if evaluable else None,
        "reasons": dict(Counter(e["reason"] for e in entries)),
    }


def engine_on_our_data(conn: psycopg.Connection, cases: list[UokikCase],
                       rulings: list[KioRuling], review_threshold: float
                       ) -> tuple[dict, dict, dict]:
    """Test B: (metrics, per UOKiK decision, per KIO signature)."""
    runs = done_runs(conn)
    uokik = {c.decision: check_engine(conn, runs, c.colluders, c.cutoff) for c in cases}
    kio = {r.signature: {**check_engine(conn, runs, r.colluders, r.ruling_date, r.ocds_id),
                         "decision": r.decision, "linked": r.ocds_id is not None}
           for r in rulings}
    positives = list(uokik.values()) + [e for e in kio.values() if e["decision"] == "upheld"]
    metrics = {
        "engine_runs": len(runs),
        "positives": _b_rate(positives, review_threshold),
        "uokik": _b_rate(list(uokik.values()), review_threshold),
        "kio_upheld": _b_rate([e for e in kio.values() if e["decision"] == "upheld"],
                              review_threshold),
        "kio_upheld_linked": _b_rate(
            [e for e in kio.values() if e["decision"] == "upheld" and e["linked"]],
            review_threshold),
        "kio_overturned": _b_rate([e for e in kio.values() if e["decision"] == "overturned"],
                                  review_threshold),
    }
    return metrics, uokik, kio


def positive_probabilities(uokik: dict, kio: dict) -> list[float]:
    entries = list(uokik.values()) + [e for e in kio.values() if e["decision"] == "upheld"]
    return [e["probability"] for e in entries if e["in_cluster"] and e["probability"] is not None]


# --- Test C ----------------------------------------------------------------------------

def _market_group(market_id: str | None) -> str:
    parts = (market_id or "").split(":")
    return parts[1] if len(parts) >= 2 else ""


def _same_market(group: str, prefixes: set[str]) -> bool:
    return any(group and (group.startswith(p[:3]) or p.startswith(group)) for p in prefixes)


def false_alarms(conn: psycopg.Connection, run: dict | None, cpv_prefixes: set[str],
                 linked_procedures: list[str], exclude: set[int], *, sample: int, seed: int,
                 publish_threshold: float, review_threshold: float) -> tuple[dict, list[float]]:
    """Test C: (metrics, probabilities of the sampled clusters that have a signal)."""
    if run is None:
        return {"run_id": None, "reason": "no_engine_run", "clusters": 0, "sampled": 0}, []
    clusters = conn.execute(
        "select c.cluster_id, c.market_id, c.members, s.probability, s.status"
        " from clusters c left join signals s on s.signal_id = c.cluster_id"
        " where c.run_id = %s order by c.cluster_id", (run["run_id"],)).fetchall()
    markets: set[str] = set()
    if linked_procedures:
        markets = {r["market_id"] for r in conn.execute(
            "select distinct lm.market_id from lot_market lm join lots l on l.id = lm.lot_id"
            " where l.procedure_id = any(%s)", (linked_procedures,))}
    conn.commit()
    pool = [c for c in clusters if not (set(c["members"] or []) & exclude)]
    same = [c for c in pool if c["market_id"] in markets
            or _same_market(_market_group(c["market_id"]), cpv_prefixes)]
    scope = "gold_markets" if same else "all_markets"
    candidates = same or pool
    picked = random.Random(seed).sample(candidates, min(sample, len(candidates)))
    probs = [float(c["probability"]) for c in picked if c["probability"] is not None]

    def share(t: float) -> float | None:
        return round(sum(1 for p in probs if p >= t) / len(probs), 4) if probs else None

    metrics = {
        "run_id": run["run_id"],
        "scope": scope,
        "clusters": len(clusters),
        "candidates": len(candidates),
        "sampled": len(picked),
        "with_signal": len(probs),
        "statuses": dict(Counter(c["status"] or "none" for c in picked)),
        "share_review": share(review_threshold),
        "share_publish": share(publish_threshold),
        "median_probability": round(statistics.median(probs), 4) if probs else None,
    }
    return metrics, probs
