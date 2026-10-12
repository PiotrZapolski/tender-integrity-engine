"""Market and cluster tests of the OECD checklist.

- 3.1.2 territorial split (level `cluster`): buyer x member matrix of the wins of the cluster
  members in their market, counted only over contested lots (2 or more offers: a one-offer
  lot is no allocation) and only at buyers with at least 2 such member-won lots (a buyer
  with one lot is exclusive by definition). Value = share of those member wins that happened
  at buyers where only one member ever wins (exclusivity index, buyers weighted by the
  number of lots). High = signal.
  Percentile: the observed index against the same index after reshuffling the winner labels
  among the same member-won lots (buyers and win counts kept), so a cluster that simply works
  for few buyers does not look like a split. Written only when at least two members win and the
  wins are spread over at least two buyers.
- 3.3.10 price drop after a new entrant (levels `cluster` and `market`): for a firm from outside
  the cluster that bids for the first time in a lot where members bid, the median of
  `price_winner / estimated_value` over the member lots in the 6 months before vs the 6 months
  from the entry. Value = relative change of the median (negative = drop); a drop of more than
  10% is a signal. The `market` level is the same over all lots of the market and firms bidding
  in the market for the first time. An entry counts only when the 6 months before it are fully
  covered by data (otherwise every firm looks new at the start of the data) and both windows
  have at least 3 lots. With several entries the row keeps the largest drop. Percentile: the
  entry change against the same before/after change taken at every other lot date of the series
  (placebo dates), so a usual 6-month price swing does not look like an entry effect.

Participation in a lot = a bid from a parsed opening document (offers outside the BZP range
included, this is co-occurrence) or being the BZP winner (one-offer lots have no document).
Prices come from BZP (`lots.price_winner`, `lots.estimated_value`).
"""

from __future__ import annotations

import calendar
import zlib
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from statistics import median

import numpy as np
import psycopg
from psycopg.types.json import Jsonb

TEST_SPLIT = "3.1.2"
TEST_ENTRY = "3.3.10"
MIN_OBS = 10  # below this many observations the percentile is null
ENTRY_WINDOW_MONTHS = 6
ENTRY_MIN_LOTS = 3
ENTRY_SIGNAL_DROP = -0.10
SPLIT_DRAWS = 999
MAX_EVIDENCE_ROWS = 20
SPLIT_MIN_OFFERS = 2
SPLIT_MIN_BUYER_LOTS = 2


@dataclass(frozen=True)
class _Lot:
    lot_id: int
    market_id: str
    buyer_id: int | None
    day: date
    winner: int | None
    ratio: float | None
    n_offers: int | None = None


def _add_months(d: date, months: int) -> date:
    total = d.year * 12 + d.month - 1 + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _mid_rank(sample: np.ndarray, value: float, *, higher_is_signal: bool) -> float:
    """Share of the sample below `value` (ties count half); flipped when low is the signal."""
    tol = 1e-9
    below = float(np.sum(sample < value - tol))
    above = float(np.sum(sample > value + tol))
    equal = len(sample) - below - above
    if not higher_is_signal:
        below = above
    return (below + 0.5 * equal) / len(sample)


def _load(conn: psycopg.Connection, run_id: int):
    lots: dict[int, _Lot] = {}
    for r in conn.execute(
        "select lm.lot_id, lm.market_id, p.buyer_id, p.published_at::date as day,"
        " l.winner_bidder_id, l.price_winner, l.estimated_value, l.n_offers"
        " from lot_market lm join lots l on l.id = lm.lot_id"
        " join procedures p on p.ocds_id = l.procedure_id"
        " where lm.run_id = %s",
        (run_id,),
    ).fetchall():
        ratio = None
        if r["price_winner"] is not None and r["estimated_value"] and r["estimated_value"] > 0:
            ratio = float(r["price_winner"] / r["estimated_value"])
        lots[r["lot_id"]] = _Lot(
            r["lot_id"], r["market_id"], r["buyer_id"], r["day"], r["winner_bidder_id"], ratio,
            r["n_offers"],
        )

    bidders: dict[int, set[int]] = defaultdict(set)
    for r in conn.execute(
        "select b.lot_id, b.bidder_id from bids b"
        " join documents d on d.id = b.document_id"
        " join lot_market lm on lm.lot_id = b.lot_id"
        " where lm.run_id = %s and d.status = 'parsed' and b.bidder_id is not null",
        (run_id,),
    ).fetchall():
        bidders[r["lot_id"]].add(r["bidder_id"])
    for lot in lots.values():
        if lot.winner is not None:
            bidders[lot.lot_id].add(lot.winner)

    clusters = conn.execute(
        "select cluster_id, market_id, members from clusters where run_id = %s"
        " order by cluster_id",
        (run_id,),
    ).fetchall()
    return lots, bidders, clusters


# --- 3.1.2 territorial split --------------------------------------------------------------


def _exclusivity(starts: np.ndarray, sizes: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """Exclusivity index for each row of `labels` (winner labels of lots sorted by buyer).

    `starts` are the offsets of each buyer's run of lots, `sizes` their lengths. A buyer is
    exclusive when all its lots carry one label; the index weights buyers by their lots.
    """
    labels = np.atleast_2d(labels)
    lo = np.minimum.reduceat(labels, starts, axis=1)
    hi = np.maximum.reduceat(labels, starts, axis=1)
    return ((lo == hi) * sizes).sum(axis=1) / sizes.sum()


def _contested(lot: _Lot, bidders) -> bool:
    return max(lot.n_offers or 0, len(bidders.get(lot.lot_id, ()))) >= SPLIT_MIN_OFFERS


def _split_result(cluster, member_lots: list[_Lot], bidders) -> dict | None:
    """3.1.2 for one cluster; `member_lots` = lots of its market where a member takes part
    (a bid or the win)."""
    members = sorted(cluster["members"] or [])
    member_set = set(members)
    won = [lot for lot in member_lots
           if lot.winner in member_set and lot.buyer_id is not None and _contested(lot, bidders)]
    per_buyer: dict[int, int] = defaultdict(int)
    for lot in won:
        per_buyer[lot.buyer_id] += 1
    won = [lot for lot in won if per_buyer[lot.buyer_id] >= SPLIT_MIN_BUYER_LOTS]
    winners = sorted({lot.winner for lot in won})
    buyers = sorted({lot.buyer_id for lot in won})
    if len(winners) < 2 or len(buyers) < 2:
        return None

    won.sort(key=lambda lot: (lot.buyer_id, lot.lot_id))
    m_index = {m: i for i, m in enumerate(winners)}
    labels = np.array([m_index[lot.winner] for lot in won])
    buyer_of = np.array([lot.buyer_id for lot in won])
    starts = np.flatnonzero(np.r_[True, buyer_of[1:] != buyer_of[:-1]])
    sizes = np.diff(np.r_[starts, len(won)])
    value = float(_exclusivity(starts, sizes, labels)[0])

    n_obs = len(won)
    percentile = None
    if n_obs >= MIN_OBS:
        rng = np.random.default_rng(zlib.crc32(cluster["cluster_id"].encode("utf-8")))
        shuffled = rng.permuted(np.tile(labels, (SPLIT_DRAWS, 1)), axis=1)
        percentile = _mid_rank(
            _exclusivity(starts, sizes, shuffled), value, higher_is_signal=True
        )

    # Starts: share of member bids placed at buyers where only one member ever bids.
    bids_at: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for lot in member_lots:
        if lot.buyer_id is None:
            continue
        for m in bidders.get(lot.lot_id, set()) & member_set:
            bids_at[lot.buyer_id][m] += 1
    n_starts = sum(sum(c.values()) for c in bids_at.values())
    excl_starts = sum(sum(c.values()) for c in bids_at.values() if len(c) == 1)

    matrix: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for lot in won:
        matrix[lot.buyer_id][lot.winner] += 1
    rows = sorted(matrix.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))
    evidence = {
        "members": members,
        "member_wins": n_obs,
        "n_buyers": len(buyers),
        "exclusive_buyers": sum(1 for c in matrix.values() if len(c) == 1),
        "start_exclusivity": round(excl_starts / n_starts, 4) if n_starts else None,
        "buyers": [
            {"buyer_id": b, "wins": {str(m): n for m, n in sorted(c.items())}}
            for b, c in rows[:MAX_EVIDENCE_ROWS]
        ],
    }
    return {"value": value, "percentile": percentile, "n_obs": n_obs, "evidence": evidence}


# --- 3.3.10 new entrant -------------------------------------------------------------------


def _window_change(days: list[date], ratios: list[float], t: date) -> dict | None:
    """Relative change of the median ratio: 6 months from `t` vs the 6 months before `t`.

    `days` is sorted and `ratios` follows it.
    """
    lo = bisect_left(days, _add_months(t, -ENTRY_WINDOW_MONTHS))
    mid = bisect_left(days, t)
    hi = bisect_left(days, _add_months(t, ENTRY_WINDOW_MONTHS))
    before = ratios[lo:mid]
    after = ratios[mid:hi]
    if len(before) < ENTRY_MIN_LOTS or len(after) < ENTRY_MIN_LOTS:
        return None
    m_before = median(before)
    m_after = median(after)
    if m_before <= 0:
        return None
    return {
        "change": (m_after - m_before) / m_before,
        "median_before": m_before,
        "median_after": m_after,
        "n_before": len(before),
        "n_after": len(after),
    }


def _entry_result(lots: list[_Lot], bidders, exclude: set[int]) -> dict | None:
    """Largest relative drop of the winner/estimate median around first entries into `lots`."""
    if not lots:
        return None
    lots = sorted(lots, key=lambda lot: (lot.day, lot.lot_id))
    data_start = lots[0].day
    first_seen: dict[int, date] = {}
    for lot in lots:
        for firm in bidders.get(lot.lot_id, ()):
            first_seen.setdefault(firm, lot.day)

    entries: dict[date, list[int]] = defaultdict(list)
    for firm, day in first_seen.items():
        if firm in exclude or _add_months(day, -ENTRY_WINDOW_MONTHS) < data_start:
            continue
        entries[day].append(firm)
    if not entries:
        return None

    priced = [lot for lot in lots if lot.ratio is not None]
    days = [lot.day for lot in priced]
    ratios = [lot.ratio for lot in priced]
    events = []
    for day in sorted(entries):
        change = _window_change(days, ratios, day)
        if change is not None:
            events.append((change["change"], day, sorted(entries[day]), change))
    if not events:
        return None
    events.sort(key=lambda e: (e[0], e[1]))
    value, day, entrants, best = events[0]

    placebo = []
    for t in sorted(set(days)):
        if _add_months(t, -ENTRY_WINDOW_MONTHS) < data_start:
            continue
        change = _window_change(days, ratios, t)
        if change is not None:
            placebo.append(change["change"])
    percentile = None
    if len(placebo) >= MIN_OBS:
        percentile = _mid_rank(np.array(placebo), value, higher_is_signal=False)

    evidence = {
        "entry_date": day.isoformat(),
        "entrants": entrants,
        "median_before": round(best["median_before"], 4),
        "median_after": round(best["median_after"], 4),
        "n_before": best["n_before"],
        "n_after": best["n_after"],
        "signal": value < ENTRY_SIGNAL_DROP,
        "n_placebo": len(placebo),
        "events": [
            {"date": d.isoformat(), "entrants": firms, "change": round(v, 4)}
            for v, d, firms, _ in events[:MAX_EVIDENCE_ROWS]
        ],
    }
    return {
        "value": value,
        "percentile": percentile,
        "n_obs": best["n_before"] + best["n_after"],
        "evidence": evidence,
    }


# --- entry point --------------------------------------------------------------------------


def compute_market_tests(conn: psycopg.Connection, run_id: int) -> int:
    """Compute 3.1.2 and 3.3.10 for `run_id`, replacing earlier rows of this run.

    Commits; returns the number of `test_results` rows written.
    """
    lots, bidders, clusters = _load(conn, run_id)
    by_market: dict[str, list[_Lot]] = defaultdict(list)
    for lot in lots.values():
        by_market[lot.market_id].append(lot)
    # (market, bidder) -> lots of that market where the bidder takes part: each cluster only
    # looks at its members' lots instead of scanning its whole market.
    lots_of: dict[tuple[str, int], list[_Lot]] = defaultdict(list)
    for lot in lots.values():
        for firm in bidders.get(lot.lot_id, ()):
            lots_of[(lot.market_id, firm)].append(lot)

    rows = []
    for cluster in clusters:
        market_id = cluster["market_id"]
        members = set(cluster["members"] or [])
        seen: dict[int, _Lot] = {}
        for m in members:
            for lot in lots_of.get((market_id, m), ()):
                seen[lot.lot_id] = lot
        member_lots = [seen[k] for k in sorted(seen)]
        split = _split_result(cluster, member_lots, bidders)
        if split:
            rows.append((TEST_SPLIT, "cluster", cluster["cluster_id"], market_id, split))
        entry = _entry_result(member_lots, bidders, members)
        if entry:
            entry["evidence"]["members"] = sorted(members)
            rows.append((TEST_ENTRY, "cluster", cluster["cluster_id"], market_id, entry))

    for market_id in sorted(by_market):
        entry = _entry_result(by_market[market_id], bidders, set())
        if entry:
            rows.append((TEST_ENTRY, "market", market_id, market_id, entry))

    try:
        with conn.cursor() as cur:
            cur.execute(
                "delete from test_results where run_id = %s and test_id = any(%s)",
                (run_id, [TEST_SPLIT, TEST_ENTRY]),
            )
            cur.executemany(
                "insert into test_results(run_id, test_id, level, entity_key, market_id, value,"
                " percentile, n_obs, evidence) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (run_id, test_id, level, key, market_id, r["value"], r["percentile"],
                     r["n_obs"], Jsonb(r["evidence"]))
                    for test_id, level, key, market_id, r in rows
                ],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)
