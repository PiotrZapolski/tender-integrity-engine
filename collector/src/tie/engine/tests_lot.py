"""Lot-level price screens from the OECD checklist (section 3.3), computed per market.

- 3.3.7 relative distance: `(p2 - p1) / sd(losing valid offers)` for lots with 3+ valid
  offers (p1, p2 = the two lowest prices); a large gap to the winner with losers close to
  each other is the signal.
- 3.3.5 identical prices: 1 when at least two valid offers of the lot have the same price
  (within 0.01 PLN), else 0.
- 3.3.9 winning price vs estimate: `price_winner / estimated_value` from BZP (only when the
  estimate is positive); high is the signal.

Offers come only from opening documents that passed the cross-check
(`documents.status = 'parsed'`), prices are compared after `bids.price_scale` (BZP price =
document price * scale) and offers flagged `outside_bzp_range` (likely rejected) are left out:
these are screens of the price distribution of valid offers.

Percentiles are computed only within the lot's market (`lot_market` of the run): the
competition rank of the value among the market's observations divided by their number,
`(1 + count(values < v)) / n`, so high = unusual; `null` when the market has fewer than
`MIN_OBS` observations for the test (a small market alone must not make a signal).
"""

from __future__ import annotations

import statistics
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

MIN_OBS = 10
MIN_VALID_RD = 3
MIN_VALID_DUP = 2
PRICE_TOLERANCE = Decimal("0.01")
# Floor of the losers' standard deviation (one grosz): identical losing prices give a large,
# finite relative distance instead of a division by zero.
SD_FLOOR = 0.01
LOT_TESTS = ("3.3.5", "3.3.7", "3.3.9")


@dataclass
class Observation:
    """One observation of a test in a market. All observations of a (test, market) form the
    distribution for the percentile; only those with `store` are written."""

    test_id: str
    entity_key: str
    market_id: str
    value: float
    evidence: dict
    store: bool = True


def market_percentiles(values: list[float]) -> list[float | None]:
    """`rank / n` with competition ranking (ties share the lowest rank); all `None` when there
    are fewer than `MIN_OBS` values."""
    n = len(values)
    if n < MIN_OBS:
        return [None] * n
    ordered = sorted(values)
    return [(1 + bisect_left(ordered, v)) / n for v in values]


def write_results(conn: psycopg.Connection, run_id: int, level: str,
                  test_ids: tuple[str, ...], observations: list[Observation]) -> int:
    """Replace the run's rows of these tests with the observations (percentile and `n_obs`
    per test and market) and commit. Returns the number of rows written."""
    groups: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for obs in observations:
        groups[(obs.test_id, obs.market_id)].append(obs)
    rows = []
    for group in groups.values():
        pcts = market_percentiles([o.value for o in group])
        n_obs = len(group)
        for obs, pct in zip(group, pcts, strict=True):
            if obs.store:
                rows.append((run_id, obs.test_id, level, obs.entity_key, obs.market_id,
                             obs.value, pct, n_obs, Jsonb(obs.evidence)))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "delete from test_results where run_id = %s and level = %s"
                " and test_id = any(%s)",
                (run_id, level, list(test_ids)),
            )
            if rows:
                cur.executemany(
                    "insert into test_results(run_id, test_id, level, entity_key, market_id,"
                    " value, percentile, n_obs, evidence)"
                    " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    rows,
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


def _money(x: float | Decimal) -> float:
    return round(float(x), 2)


def _relative_distance(prices: list[Decimal]) -> tuple[float, dict]:
    s = sorted(float(p) for p in prices)
    p1, p2 = s[0], s[1]
    losers = s[1:]
    sd = statistics.stdev(losers)
    mean = statistics.fmean(losers)
    value = (p2 - p1) / max(sd, SD_FLOOR)
    return value, {
        "n_valid": len(s),
        "p1": _money(p1),
        "p2": _money(p2),
        "gap": _money(p2 - p1),
        "sd_losers": _money(sd),
        "cv_losers": round(sd / mean, 6) if mean > 0 else None,
    }


def _identical(prices: list[Decimal]) -> tuple[float, dict]:
    """Groups of adjacent sorted prices within the tolerance of their neighbour."""
    s = sorted(prices)
    groups: list[list[Decimal]] = [[s[0]]]
    for p in s[1:]:
        if p - groups[-1][-1] <= PRICE_TOLERANCE:
            groups[-1].append(p)
        else:
            groups.append([p])
    dups = [g for g in groups if len(g) > 1]
    return (1.0 if dups else 0.0), {
        "n_valid": len(s),
        "identical": [{"price": _money(g[0]), "count": len(g)} for g in dups],
    }


def compute_lot_tests(conn: psycopg.Connection, run_id: int) -> int:
    """Compute 3.3.5, 3.3.7 and 3.3.9 for every lot assigned to a market in this run and
    write them to `test_results` (level `lot`, `entity_key` = lot id). Replaces earlier rows
    of these tests in the run. Returns the number of rows written."""
    lots = conn.execute(
        "select lm.lot_id, lm.market_id, l.estimated_value, l.price_winner"
        " from lot_market lm join lots l on l.id = lm.lot_id"
        " where lm.run_id = %s order by lm.lot_id",
        (run_id,),
    ).fetchall()
    bids = conn.execute(
        "select b.lot_id, b.price * b.price_scale as price"
        " from bids b"
        " join lot_market lm on lm.lot_id = b.lot_id"
        " join documents d on d.id = b.document_id"
        " where lm.run_id = %s and d.status = 'parsed'"
        "  and b.price is not null and b.price > 0"
        "  and b.outside_bzp_range is not true",
        (run_id,),
    ).fetchall()
    prices: dict[int, list[Decimal]] = defaultdict(list)
    for b in bids:
        prices[b["lot_id"]].append(Decimal(b["price"]))

    observations: list[Observation] = []
    for lot in lots:
        key, market = str(lot["lot_id"]), lot["market_id"]
        valid = prices.get(lot["lot_id"], [])
        if len(valid) >= MIN_VALID_RD:
            value, evidence = _relative_distance(valid)
            observations.append(Observation("3.3.7", key, market, value, evidence))
        if len(valid) >= MIN_VALID_DUP:
            value, evidence = _identical(valid)
            observations.append(Observation("3.3.5", key, market, value, evidence))
        est, won = lot["estimated_value"], lot["price_winner"]
        if est is not None and est > 0 and won is not None and won > 0:
            observations.append(Observation(
                "3.3.9", key, market, float(won) / float(est),
                {"price_winner": _money(won), "estimated_value": _money(est)},
            ))
    return write_results(conn, run_id, "lot", LOT_TESTS, observations)


# Pure screens on a list of valid prices (value, evidence), shared with `tie.validation`,
# which scores the bids of gold cases in memory with exactly the engine's arithmetic.
relative_distance = _relative_distance
identical_prices = _identical
