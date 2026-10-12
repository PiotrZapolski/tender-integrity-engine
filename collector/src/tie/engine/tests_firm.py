"""Firm-level screens from the OECD checklist, computed for each firm within each market.

- 3.1.1 the same supplier wins often: the firm's share of the market's lots with a winner
  (only in markets with at least `MIN_MARKET_LOTS` such lots).
- 3.1.5 always bids, never wins: the number of starts of a firm with no win, stored only when
  the firm has at least `NEVER_WIN_STARTS` starts, 0 wins and a cover-bid shape: in at least
  half of its starts its valid offer (cross-checked document, not outside the BZP range,
  after `price_scale`) is within `COVER_GAP` (10%) of the winning price. A firm that simply
  loses by a wide margin is an honest loser, not a cover bidder. The other firms of the
  market get value 0 and still count in the distribution for the percentile.
- 3.3.3 smaller discounts than usual: the median `price_winner / estimated_value` over the
  firm's wins (at least `MIN_WINS_RATIO` wins with a positive estimate), against the market.

A start is a lot where the firm has an offer in a cross-checked opening document
(`documents.status = 'parsed'`; offers outside the BZP price range count too, as "always
loses" uses all offers) or where BZP names it the winner; wins come from BZP
(`lots.winner_bidder_id`). Only firms with at least `MIN_STARTS` starts in the market get any
result: a firm with a single offer gives no signal and no division by zero.

`entity_key` is `"<bidder_id>@<market_id>"` (a firm can be in several markets);
`evidence.bidder_id` holds the id. Percentiles as in `tests_lot`: within the market only,
`null` below `MIN_OBS` firms.
"""

from __future__ import annotations

import statistics
from collections import defaultdict

import psycopg

from tie.engine.tests_lot import Observation, write_results

MIN_STARTS = 2
MIN_MARKET_LOTS = 10
NEVER_WIN_STARTS = 8
# A losing offer at most this much above the winning price has the shape of a cover bid.
COVER_GAP = 0.10
MIN_WINS_RATIO = 2
FIRM_TESTS = ("3.1.1", "3.1.5", "3.3.3")


def firm_key(bidder_id: int, market_id: str) -> str:
    return f"{bidder_id}@{market_id}"


def compute_firm_tests(conn: psycopg.Connection, run_id: int) -> int:
    """Compute 3.1.1, 3.1.5 and 3.3.3 for every firm in every market of this run and write
    them to `test_results` (level `firm`). Replaces earlier rows of these tests in the run.
    Returns the number of rows written."""
    lots = conn.execute(
        "select lm.market_id, l.id, l.winner_bidder_id, l.price_winner, l.estimated_value"
        " from lot_market lm join lots l on l.id = lm.lot_id"
        " where lm.run_id = %s order by l.id",
        (run_id,),
    ).fetchall()
    bid_starts = conn.execute(
        "select lm.market_id, b.bidder_id, b.lot_id,"
        "  case when b.outside_bzp_range is not true and b.price > 0"
        "   then b.price * b.price_scale end as price"
        " from bids b"
        " join lot_market lm on lm.lot_id = b.lot_id"
        " join documents d on d.id = b.document_id"
        " where lm.run_id = %s and d.status = 'parsed' and b.bidder_id is not null",
        (run_id,),
    ).fetchall()

    starts: dict[str, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
    wins: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    win_ratios: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    market_ratios: dict[str, list[float]] = defaultdict(list)
    won_lots: dict[str, int] = defaultdict(int)
    # (market, bidder) -> lot -> lowest valid offer price
    offers: dict[tuple[str, int], dict[int, float]] = defaultdict(dict)
    for b in bid_starts:
        starts[b["market_id"]][b["bidder_id"]].add(b["lot_id"])
        if b["price"] is not None:
            prices = offers[(b["market_id"], b["bidder_id"])]
            price = float(b["price"])
            prices[b["lot_id"]] = min(price, prices.get(b["lot_id"], price))
    winning_price = {lot["id"]: float(lot["price_winner"]) for lot in lots
                     if lot["price_winner"] is not None and lot["price_winner"] > 0}
    for lot in lots:
        market, winner = lot["market_id"], lot["winner_bidder_id"]
        est, price = lot["estimated_value"], lot["price_winner"]
        ratio = (float(price) / float(est)
                 if est is not None and est > 0 and price is not None and price > 0 else None)
        if ratio is not None:
            market_ratios[market].append(ratio)
        if winner is None:
            continue
        won_lots[market] += 1
        starts[market][winner].add(lot["id"])
        wins[market][winner] += 1
        if ratio is not None:
            win_ratios[market][winner].append(ratio)

    observations: list[Observation] = []
    for market in sorted(starts):
        n_won = won_lots[market]
        market_median = (statistics.median(market_ratios[market])
                         if market_ratios[market] else None)
        for bidder in sorted(starts[market]):
            n_starts = len(starts[market][bidder])
            if n_starts < MIN_STARTS:
                continue
            key = firm_key(bidder, market)
            n_wins = wins[market].get(bidder, 0)
            base = {"bidder_id": bidder, "starts": n_starts, "wins": n_wins}
            if n_won >= MIN_MARKET_LOTS:
                observations.append(Observation(
                    "3.1.1", key, market, n_wins / n_won, {**base, "market_lots_won": n_won},
                ))
            never = n_wins == 0 and n_starts >= NEVER_WIN_STARTS
            close = 0
            if never:
                close = sum(
                    1 for lot_id, p in offers.get((market, bidder), {}).items()
                    if lot_id in winning_price and p <= winning_price[lot_id] * (1 + COVER_GAP)
                )
                never = 2 * close >= n_starts
            observations.append(Observation(
                "3.1.5", key, market, float(n_starts) if never else 0.0,
                {**base, "close_losses": close} if never else base, store=never,
            ))
            ratios = win_ratios[market].get(bidder, [])
            if len(ratios) >= MIN_WINS_RATIO:
                observations.append(Observation(
                    "3.3.3", key, market, statistics.median(ratios),
                    {**base, "wins_with_estimate": len(ratios),
                     "market_median": (round(market_median, 6)
                                       if market_median is not None else None)},
                ))
    return write_results(conn, run_id, "firm", FIRM_TESTS, observations)
