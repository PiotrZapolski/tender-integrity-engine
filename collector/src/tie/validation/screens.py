"""Engine screens on in-memory tenders and their place in a background distribution.

Test A of the validation (spec section 9) scores the bids described in UOKiK decisions with
the lot and pair screens the engine computes on our data, using the engine's own pure
functions (`tie.engine.tests_lot`, `tie.engine.tests_pair`), and places every value against
the distribution of the same screen in our database for the same market.

Screens:
- lot `3.3.7` relative distance (3+ valid prices) and `3.3.5` identical prices (2+);
- pair `3.1.6` rotation (5+ common tenders) and `3.3.6` coefficient of variation of the
  loser/winner price ratio (4+ tenders won by exactly one firm of the pair, low = signal).
`3.3.9` needs the buyer's estimate, which the decisions do not give, so it is left out.

Background: lots of our database with offers from cross-checked opening documents
(`documents.status = 'parsed'`), prices after `price_scale`, offers `outside_bzp_range` only
for co-occurrence, in the 36-month window ending at `as_of` (as the engine's markets). The
market of a gold case is tried from fine to coarse: CPV 5 digits in the province, CPV 3 in
the province, CPV 3 in Poland, CPV 2 in the province, CPV 2 in Poland. Per screen the first
level with at least 30 lots and 10 observations is used, else the coarsest level with 10
observations, else the value has no percentile.

Percentile of a gold value `v` among `n` background values: the share of background values
that are strictly less suspicious, `count(b < v) / n` (for `3.3.6`: `count(b > v) / n`).
Ties count against the gold value, so a binary screen (`3.3.5`) reaches the top 5% only when
fewer than 5% of the background lots show it. Top 5% = percentile >= 0.95.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Hashable, Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import combinations

import psycopg

from tie.engine.graph import MIN_PAIR_LOTS
from tie.engine.markets import cpv_group, normalize_province, window_start
from tie.engine.tests_lot import (
    MIN_OBS,
    MIN_VALID_DUP,
    MIN_VALID_RD,
    identical_prices,
    relative_distance,
)
from tie.engine.tests_pair import PricedBid, PricedBids, price_ratio_cv, rotation_index

LOT_SCREENS = ("3.3.5", "3.3.7")
PAIR_SCREENS = ("3.1.6", "3.3.6")
HIGHER_IS_SIGNAL = {"3.3.5": True, "3.3.7": True, "3.1.6": True, "3.3.6": False}
TOP_SHARE = 0.05
TOP_PCT = 1.0 - TOP_SHARE
# Same minimum market size as the engine (`tie.engine.markets.assign_markets`).
MIN_MARKET_LOTS = 30
WINDOW_MONTHS = 36


@dataclass
class Offer:
    bidder: Hashable | None
    price: Decimal | None
    won: bool = False
    # False for offers outside the BZP price range: co-occurrence only, not prices.
    valid: bool = True


@dataclass
class Tender:
    key: Hashable
    offers: list[Offer] = field(default_factory=list)
    # Winner of the lot (BZP); None means the bidder of the only offer marked `won`.
    winner: Hashable | None = None

    def valid_prices(self) -> list[Decimal]:
        return [Decimal(o.price) for o in self.offers
                if o.valid and o.price is not None and o.price > 0]

    def lot_winner(self) -> Hashable | None:
        if self.winner is not None:
            return self.winner
        won = sorted({o.bidder for o in self.offers if o.won and o.bidder is not None},
                     key=str)
        return won[0] if won else None


def lot_screens(tender: Tender) -> dict[str, float]:
    """`3.3.7` and `3.3.5` of one tender (engine arithmetic), where computable."""
    prices = tender.valid_prices()
    out: dict[str, float] = {}
    if len(prices) >= MIN_VALID_RD:
        out["3.3.7"] = relative_distance(prices)[0]
    if len(prices) >= MIN_VALID_DUP:
        out["3.3.5"] = identical_prices(prices)[0]
    return out


def pair_screens(tenders: Iterable[Tender]) -> dict[tuple, dict[str, float]]:
    """`3.1.6` and `3.3.6` of every pair of bidders that met in at least two tenders.

    Pairs are `(a, b)` with `a < b` (bidder keys must be mutually comparable); a pair without
    enough common tenders for either screen maps to an empty dict."""
    together: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0])
    priced = PricedBids()
    for index, tender in enumerate(tenders):
        winner = tender.lot_winner()
        bidders = sorted({o.bidder for o in tender.offers if o.bidder is not None})
        for a, b in combinations(bidders, 2):
            row = together[(a, b)]
            row[0] += 1
            row[1] += winner == a
            row[2] += winner == b
        for o in tender.offers:
            if o.bidder is None or not o.valid or o.price is None or o.price <= 0:
                continue
            won = bool(o.won) or (winner is not None and o.bidder == winner)
            lots = priced.priced[o.bidder]
            prev = lots.get(index)
            price = float(o.price)
            lots[index] = (PricedBid(price, won) if prev is None
                           else PricedBid(min(prev.price, price), prev.won or won))
    out: dict[tuple, dict[str, float]] = {}
    for (a, b), (n, a_wins, b_wins) in together.items():
        if n < MIN_PAIR_LOTS:
            continue
        screens: dict[str, float] = {}
        rot = rotation_index({"n_together": n, "a_wins": a_wins, "b_wins": b_wins})
        if rot is not None:
            screens["3.1.6"] = rot
        cv = price_ratio_cv(priced, a, b)
        if cv is not None:
            screens["3.3.6"] = cv[0]
        out[(a, b)] = screens
    return out


def percentile_in(value: float, ordered: list[float], higher_is_signal: bool) -> float | None:
    """Share of the (sorted) background that is strictly less suspicious than `value`;
    None with fewer than `MIN_OBS` background values."""
    n = len(ordered)
    if n < MIN_OBS:
        return None
    if higher_is_signal:
        return bisect_left(ordered, value) / n
    return (n - bisect_right(ordered, value)) / n


@dataclass
class Background:
    level: str
    cpv_group: str
    province: str | None
    n_lots: int
    values: dict[str, list[float]]


@dataclass
class Placement:
    screen: str
    value: float
    percentile: float | None
    n_obs: int
    level: str | None
    cpv_group: str | None
    province: str | None

    @property
    def top(self) -> bool:
        return self.percentile is not None and self.percentile >= TOP_PCT

    def as_dict(self) -> dict:
        return {"value": round(self.value, 6),
                "pct": None if self.percentile is None else round(self.percentile, 4),
                "n": self.n_obs, "level": self.level, "cpv_group": self.cpv_group,
                "province": self.province}


def background_from_tenders(tenders: list[Tender], level: str, group: str,
                            province: str | None) -> Background:
    values: dict[str, list[float]] = defaultdict(list)
    for tender in tenders:
        for screen, value in lot_screens(tender).items():
            values[screen].append(value)
    for screens in pair_screens(tenders).values():
        for screen, value in screens.items():
            values[screen].append(value)
    return Background(level, group, province, len(tenders),
                      {k: sorted(v) for k, v in values.items()})


def market_levels(cpv: str, province: str | None) -> list[tuple[str, str, str | None]]:
    """Candidate markets of a CPV prefix (2-5 digits) and province, fine to coarse."""
    out: list[tuple[str, str, str | None]] = []
    if province and len(cpv) >= 5:
        out.append(("cpv5_prov", cpv[:5], province))
    if province and len(cpv) >= 3:
        out.append(("cpv3_prov", cpv[:3], province))
    if len(cpv) >= 3:
        out.append(("cpv3_pl", cpv[:3], None))
    if province and len(cpv) >= 2:
        out.append(("cpv2_prov", cpv[:2], province))
    if len(cpv) >= 2:
        out.append(("cpv2_pl", cpv[:2], None))
    return out


_BACKGROUND_SQL = """
with dated as (
  select l.id, l.winner_bidder_id,
         replace(coalesce(nullif(btrim(l.cpv), ''), nullif(btrim(p.cpv_main), '')), ' ', '')
           as cpv,
         bu.province,
         coalesce(l.contract_date, (p.published_at at time zone 'Europe/Warsaw')::date) as day
  from lots l
  join procedures p on p.ocds_id = l.procedure_id
  left join buyers bu on bu.id = p.buyer_id
)
select x.id as lot_id, x.cpv, x.province, x.winner_bidder_id, b.bidder_id,
       b.price * b.price_scale as price, b.is_winner,
       coalesce(b.outside_bzp_range, false) as outside
from dated x
join bids b on b.lot_id = x.id
join documents d on d.id = b.document_id and d.status = 'parsed'
where x.day > %(start)s and x.day <= %(as_of)s and x.cpv like %(prefix)s
order by x.id, b.id
"""


class Backgrounds:
    """Background distributions of the screens per market, loaded lazily and cached."""

    def __init__(self, conn: psycopg.Connection, as_of: date,
                 window_months: int = WINDOW_MONTHS) -> None:
        self.conn = conn
        self.as_of = as_of
        self.start = window_start(as_of, window_months)
        self._rows: dict[str, list[tuple[str | None, str | None, Tender]]] = {}
        self._markets: dict[tuple, Background] = {}

    def _tenders(self, prefix2: str) -> list[tuple[str | None, str | None, Tender]]:
        if prefix2 not in self._rows:
            lots: dict[int, tuple[str | None, str | None, Tender]] = {}
            for r in self.conn.execute(_BACKGROUND_SQL, {
                    "start": self.start, "as_of": self.as_of, "prefix": prefix2 + "%"}):
                if r["lot_id"] not in lots:
                    lots[r["lot_id"]] = (r["cpv"], normalize_province(r["province"]),
                                         Tender(r["lot_id"], winner=r["winner_bidder_id"]))
                won = bool(r["is_winner"]) or (r["winner_bidder_id"] is not None
                                               and r["winner_bidder_id"] == r["bidder_id"])
                lots[r["lot_id"]][2].offers.append(Offer(
                    r["bidder_id"], None if r["price"] is None else Decimal(r["price"]), won,
                    valid=not r["outside"]))
            self.conn.commit()
            self._rows[prefix2] = list(lots.values())
        return self._rows[prefix2]

    def market(self, level: str, group: str, province: str | None) -> Background:
        key = (level, group, province)
        if key not in self._markets:
            digits = len(group)
            tenders = [
                t for cpv, prov, t in self._tenders(group[:2])
                if cpv_group(cpv, digits) == group and (province is None or prov == province)
            ]
            self._markets[key] = background_from_tenders(tenders, level, group, province)
        return self._markets[key]

    def place(self, screen: str, value: float, cpv: str | None,
              province: str | None) -> Placement:
        chosen: Background | None = None
        coarsest: Background | None = None
        for level in market_levels(cpv or "", province):
            bg = self.market(*level)
            n = len(bg.values.get(screen, ()))
            if n < MIN_OBS:
                continue
            coarsest = bg
            if bg.n_lots >= MIN_MARKET_LOTS:
                chosen = bg
                break
        chosen = chosen or coarsest
        if chosen is None:
            return Placement(screen, value, None, 0, None, None, None)
        ordered = chosen.values[screen]
        return Placement(screen, value,
                         percentile_in(value, ordered, HIGHER_IS_SIGNAL[screen]),
                         len(ordered), chosen.level, chosen.cpv_group, chosen.province)
