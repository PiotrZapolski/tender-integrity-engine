"""BZP-only screens: collusion markers from BZP result notices alone.

Named offers from opening documents arrive slowly (the document host is rate limited), but
every BZP result notice already carries the winner, the number of offers and rejections,
the lowest, highest and winning price and the estimate. These screens use only that
(`lots` of the run's markets via `lot_market`, `procedures.buyer_id`, `bidder_accounts`,
`bidders.address`), so they cover every market as soon as BZP is loaded. Test ids start with
`bzp.` and never collide with the OECD ids of the co-bidder tests; each maps to one OECD item
(`OECD_ITEM`), under which it appears in the evidence package of a winner-based cluster.

Awarded lot = a lot of the run's markets with `lots.winner_bidder_id`. Lot date =
`contract_date`, else the procedure's publication day in Polish time (as in `markets`).

- `bzp.winner_rotation` (OECD 3.1.6 from winners, level `cluster`): for each buyer and market
  (= CPV group of the buyer's voivodeship) the awarded lots in date order. Group = the firms
  with at least `MIN_MEMBER_WINS` wins and at least `MEMBER_OF_TOP` of the top winner's wins;
  it qualifies with 2-5 members, at least `MIN_ROTATION_LOTS` lots won by the group, a share
  of the buyer's awarded lots of at least `MIN_COVERAGE` and no dominating member (largest
  member share of the group's wins at most `max_member_share(k)`). Turn statistic on the
  group's lots in date order: switches (consecutive awards to different members) plus
  windows of k consecutive awards won by k different members. The same members at several
  buyers of one market form one group; the percentile is the observed statistic against
  `PERMUTATIONS` draws of the winner labels permuted within each buyer (counts kept, so only
  the order is tested, as in 3.1.2). Value = coverage x balance (normalized entropy of the
  members' wins) x turn statistic / its maximum.
- `bzp.spread` (OECD 3.3.7 from min/max, levels `firm` and `cluster`): for lots with
  `n_offers >= 3`, no rejection (`n_rejected` 0 or unknown) and `price_max > price_min > 0`
  the spread `price_max / price_min` (equal prices are left out: in BZP they mostly mean the
  notice repeats one price). Firm: the median spread of its won lots (at least
  `MIN_FIRM_LOTS`), percentile among the firms of the market, narrow = high. Cluster: the
  median and the coefficient of variation of the spreads of the cluster's lots against
  `PERMUTATIONS` random samples of as many lots of the market; the percentile is the higher of
  "narrow" and "near-constant".
- `bzp.over_estimate` (OECD 3.3.9, level `firm`): median `price_winner / estimated_value` of
  the firm's won lots in the market (at least `MIN_FIRM_LOTS`), percentile among the firms of
  the market, high = signal.
- `bzp.shared_winners` (OECD 3.2.11, level `pair` and `cluster`): two firms that both win in
  the same market (no co-bidding needed) and share a bank account hash (`bidder_accounts`)
  or an address (case- and whitespace-insensitive). An account or address held by more than
  `MAX_SHARED_HOLDERS` firms (a bank's collective account, a virtual office) is ignored.
  Value 1, no percentile.

Winner-based clusters (`clusters`, `cluster_id` = `"w:" + cluster_key(market, members)`):
every rotation group and every connected group of 2-5 shared-data winners of a market (the
same members from both sources make one cluster). `n_lots` = awarded lots of the market won
by a member; for a cluster found only by rotation those are restricted to the rotation buyers
(`clusters.buyers`), otherwise the whole market (`buyers` null). `density` stays null. They
go through the usual evidence, decision and explanation steps (`tie.engine.decide` applies
their gates and caps them at `review`).

Percentiles of firm tests are computed only within the market and are null below `MIN_OBS`
firms. Everything is loaded with set-based SQL (one pass over the run's awarded lots through a
server-side cursor into numpy arrays); permutations are bounded by `PERMUTATIONS`.
"""

from __future__ import annotations

import logging
import math
import time
import zlib
from array import array
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import psycopg
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from tie.engine.graph import cluster_key
from tie.engine.tests_firm import firm_key
from tie.engine.tests_lot import market_percentiles
from tie.engine.tests_market import _mid_rank
from tie.engine.tests_pair import pair_key

log = logging.getLogger(__name__)

TEST_ROTATION = "bzp.winner_rotation"
TEST_SPREAD = "bzp.spread"
TEST_OVER_ESTIMATE = "bzp.over_estimate"
TEST_SHARED = "bzp.shared_winners"
BZP_TESTS = (TEST_ROTATION, TEST_SPREAD, TEST_OVER_ESTIMATE, TEST_SHARED)
# OECD item each BZP-only test stands for (catalog `how` text and evidence key).
OECD_ITEM = {
    TEST_ROTATION: "3.1.6",
    TEST_SPREAD: "3.3.7",
    TEST_OVER_ESTIMATE: "3.3.9",
    TEST_SHARED: "3.2.11",
}
# A BZP-only test counts as a strong item of the decision gate only from this percentile.
STRONG_MIN_PCT = {TEST_ROTATION: 0.99}
CLUSTER_PREFIX = "w:"

MIN_ROTATION_LOTS = 6
MIN_GROUP = 2
MAX_GROUP = 5
MIN_MEMBER_WINS = 2
MEMBER_OF_TOP = 0.5
MIN_COVERAGE = 0.6
PERMUTATIONS = 500
MIN_SPREAD_OFFERS = 3
MIN_FIRM_LOTS = 3
MIN_OBS = 10
LOW_SPREAD_QUANTILE = 0.1
MAX_SHARED_HOLDERS = 5
MAX_EVIDENCE_ROWS = 20
MAX_SEQUENCE = 24

_POPCOUNT = np.array([i.bit_count() for i in range(1 << MAX_GROUP)], dtype=np.int64)

_LOTS_SQL = """
select lm.market_id, l.id, p.buyer_id,
       coalesce(l.contract_date, (p.published_at at time zone 'Europe/Warsaw')::date) as day,
       l.winner_bidder_id,
       case when l.n_offers >= %(min_offers)s and coalesce(l.n_rejected, 0) = 0
                 and l.price_min > 0 and l.price_max > l.price_min
            then (l.price_max / l.price_min)::float8 end as spread,
       case when l.estimated_value > 0 and l.price_winner > 0
            then (l.price_winner / l.estimated_value)::float8 end as over
from lot_market lm
join lots l on l.id = lm.lot_id
join procedures p on p.ocds_id = l.procedure_id
where lm.run_id = %(run_id)s and l.winner_bidder_id is not null
order by lm.market_id, p.buyer_id nulls last, day, l.id
"""

_SHARED_SQL = r"""
with w as (
  select lm.market_id, l.winner_bidder_id as bidder_id, count(*) as wins
  from lot_market lm join lots l on l.id = lm.lot_id
  where lm.run_id = %(run_id)s and l.winner_bidder_id is not null
  group by lm.market_id, l.winner_bidder_id
),
winners as (select distinct bidder_id from w),
acc as (
  select a.bidder_id, a.account_hash
  from bidder_accounts a join winners x on x.bidder_id = a.bidder_id
),
acc_ok as (
  select account_hash from bidder_accounts
  where account_hash in (select account_hash from acc)
  group by account_hash
  having count(distinct bidder_id) between 2 and %(max_holders)s
),
by_account as (
  select distinct x.bidder_id as a, y.bidder_id as b
  from acc x
  join acc_ok k on k.account_hash = x.account_hash
  join acc y on y.account_hash = x.account_hash and x.bidder_id < y.bidder_id
),
addr as (
  select b.id, lower(regexp_replace(btrim(b.address), '\s+', ' ', 'g')) as norm
  from bidders b join winners x on x.bidder_id = b.id
  where nullif(btrim(b.address), '') is not null
),
addr_all as (
  select lower(regexp_replace(btrim(b.address), '\s+', ' ', 'g')) as norm
  from bidders b
  where nullif(btrim(b.address), '') is not null
    and lower(regexp_replace(btrim(b.address), '\s+', ' ', 'g')) in (select norm from addr)
),
addr_ok as (
  select norm from addr_all group by norm having count(*) between 2 and %(max_holders)s
),
by_address as (
  select x.id as a, y.id as b
  from addr x
  join addr_ok k on k.norm = x.norm
  join addr y on y.norm = x.norm and x.id < y.id
),
linked as (
  select a, b, bool_or(acc) as shared_account, bool_or(adr) as shared_address
  from (select a, b, true as acc, false as adr from by_account
        union all
        select a, b, false, true from by_address) u
  group by a, b
)
select wa.market_id, k.a, k.b, k.shared_account, k.shared_address,
       wa.wins as a_wins, wb.wins as b_wins
from linked k
join w wa on wa.bidder_id = k.a
join w wb on wb.bidder_id = k.b and wb.market_id = wa.market_id
order by wa.market_id, k.a, k.b
"""


def is_winner_cluster(cluster_id: str | None) -> bool:
    """True for a winner-based cluster of this module (`w:` prefix)."""
    return bool(cluster_id) and str(cluster_id).startswith(CLUSTER_PREFIX)


def winner_cluster_id(market_id: str, members) -> str:
    return CLUSTER_PREFIX + cluster_key(market_id, members)


def max_member_share(k: int) -> float:
    """Largest share of the group's wins one member may have (0.7 for pairs, else 0.6)."""
    return max(0.6, 1.4 / k)


@dataclass
class _Data:
    markets: list[str]
    mk: np.ndarray
    lot: np.ndarray
    buyer: np.ndarray
    day: np.ndarray
    winner: np.ndarray
    spread: np.ndarray
    over: np.ndarray

    @property
    def n(self) -> int:
        return len(self.lot)


@dataclass
class _BuyerRun:
    buyer: int
    awarded: int
    rows: np.ndarray  # row indices of the group's lots, in date order
    labels: np.ndarray  # member index (0..k-1) of each of those lots


@dataclass
class _Cluster:
    mk: int
    members: tuple[int, ...]
    sources: set[str] = field(default_factory=set)
    buyers: set[int] | None = field(default_factory=set)

    def add(self, source: str, buyers: set[int] | None) -> None:
        self.sources.add(source)
        if buyers is None or self.buyers is None:
            self.buyers = None
        else:
            self.buyers |= buyers


def _load(conn: psycopg.Connection, run_id: int) -> _Data:
    index: dict[str, int] = {}
    mk, day = array("i"), array("i")
    lot, buyer, winner = array("q"), array("q"), array("q")
    spread, over = array("d"), array("d")
    nan = math.nan
    with conn.cursor(name="bzp_only_lots", row_factory=tuple_row) as cur:
        cur.itersize = 20_000
        cur.execute(_LOTS_SQL, {"run_id": run_id, "min_offers": MIN_SPREAD_OFFERS})
        for market_id, lot_id, buyer_id, d, winner_id, s, o in cur:
            i = index.get(market_id)
            if i is None:
                i = index[market_id] = len(index)
            mk.append(i)
            lot.append(lot_id)
            buyer.append(-1 if buyer_id is None else buyer_id)
            day.append(d.toordinal())
            winner.append(winner_id)
            spread.append(nan if s is None else s)
            over.append(nan if o is None else o)
    return _Data(
        markets=list(index),
        mk=np.array(mk, dtype=np.int32),
        lot=np.array(lot, dtype=np.int64),
        buyer=np.array(buyer, dtype=np.int64),
        day=np.array(day, dtype=np.int32),
        winner=np.array(winner, dtype=np.int64),
        spread=np.array(spread, dtype=np.float64),
        over=np.array(over, dtype=np.float64),
    )


def _bounds(*keys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Start and end offsets of the runs of equal keys (arrays already sorted by the keys)."""
    n = len(keys[0])
    if n == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    change = np.zeros(n - 1, dtype=bool)
    for k in keys:
        change |= k[1:] != k[:-1]
    cuts = np.flatnonzero(change) + 1
    return np.r_[0, cuts], np.r_[cuts, n]


# --- bzp.winner_rotation ------------------------------------------------------------------


def turns(labels: np.ndarray, k: int) -> np.ndarray:
    """Turn statistic of each row of `labels` (member indices 0..k-1 in date order):
    switches between consecutive awards plus windows of k consecutive awards won by k
    different members."""
    labels = np.atleast_2d(labels)
    m = labels.shape[1]
    switches = (labels[:, 1:] != labels[:, :-1]).sum(axis=1)
    if m < k:
        return switches
    bits = np.left_shift(np.int64(1), labels.astype(np.int64))
    acc = bits[:, : m - k + 1].copy()
    for j in range(1, k):
        acc |= bits[:, j: m - k + 1 + j]
    return switches + (_POPCOUNT[acc] == k).sum(axis=1)


def max_turns(m: int, k: int) -> int:
    return max(m - 1, 0) + max(m - k + 1, 0)


def pick_group(winners: np.ndarray) -> list[int] | None:
    """Rotation group of one buyer's awarded lots (winner ids), or None."""
    uniq, counts = np.unique(winners, return_counts=True)
    if len(uniq) < MIN_GROUP:
        return None
    floor = max(MIN_MEMBER_WINS, MEMBER_OF_TOP * int(counts.max()))
    keep = counts >= floor
    k = int(keep.sum())
    if not MIN_GROUP <= k <= MAX_GROUP:
        return None
    wins = counts[keep]
    total = int(wins.sum())
    if total < MIN_ROTATION_LOTS or total / len(winners) < MIN_COVERAGE:
        return None
    if wins.max() / total > max_member_share(k):
        return None
    return sorted(int(x) for x in uniq[keep])


def _rotation_runs(d: _Data) -> dict[tuple[int, tuple[int, ...]], list[_BuyerRun]]:
    """Rotation groups per (market, members), each with its buyers' runs."""
    out: dict[tuple[int, tuple[int, ...]], list[_BuyerRun]] = defaultdict(list)
    starts, ends = _bounds(d.mk, d.buyer)
    big = (ends - starts) >= MIN_ROTATION_LOTS
    for s, e in zip(starts[big].tolist(), ends[big].tolist(), strict=True):
        buyer = int(d.buyer[s])
        if buyer < 0:
            continue
        w = d.winner[s:e]
        members = pick_group(w)
        if members is None:
            continue
        member_arr = np.array(members, dtype=np.int64)
        mask = np.isin(w, member_arr)
        rows = np.flatnonzero(mask) + s
        labels = np.searchsorted(member_arr, w[mask])
        out[(int(d.mk[s]), tuple(members))].append(_BuyerRun(buyer, e - s, rows, labels))
    return out


def _balance(wins: list[int]) -> float:
    total = sum(wins)
    if len(wins) < 2 or total == 0:
        return 0.0
    h = -sum(w / total * math.log(w / total) for w in wins if w > 0)
    return h / math.log(len(wins))


def rotation_result(cluster_id: str, members: tuple[int, ...],
                    runs: list[_BuyerRun], d: _Data) -> dict:
    """Value, percentile and evidence of one rotation group (permutations within buyers)."""
    k = len(members)
    observed = 0
    maximum = 0
    simulated = np.zeros(PERMUTATIONS, dtype=np.float64)
    rng = np.random.default_rng(zlib.crc32(cluster_id.encode("utf-8")))
    wins = [0] * k
    awarded = 0
    buyers = []
    for run in sorted(runs, key=lambda r: r.buyer):
        observed += int(turns(run.labels, k)[0])
        maximum += max_turns(len(run.labels), k)
        draws = rng.permuted(np.tile(run.labels, (PERMUTATIONS, 1)), axis=1)
        simulated += turns(draws, k)
        counts = np.bincount(run.labels, minlength=k)
        for i in range(k):
            wins[i] += int(counts[i])
        awarded += run.awarded
        buyers.append({
            "buyer_id": run.buyer,
            "awarded": run.awarded,
            "group_lots": len(run.labels),
            "wins": {str(members[i]): int(counts[i]) for i in range(k) if counts[i]},
            "sequence": [int(x) for x in run.labels[-MAX_SEQUENCE:]],
            "from": _iso(int(d.day[run.rows[0]])),
            "to": _iso(int(d.day[run.rows[-1]])),
        })
    n_lots = sum(wins)
    coverage = n_lots / awarded if awarded else 0.0
    balance = _balance(wins)
    rate = observed / maximum if maximum else 0.0
    percentile = _mid_rank(simulated, float(observed), higher_is_signal=True)
    buyers.sort(key=lambda b: (-b["group_lots"], b["buyer_id"]))
    evidence = {
        "oecd": OECD_ITEM[TEST_ROTATION],
        "members": list(members),
        "n_lots": n_lots,
        "awarded": awarded,
        "coverage": round(coverage, 4),
        "balance": round(balance, 4),
        "wins": {str(m): w for m, w in zip(members, wins, strict=True)},
        "turns": observed,
        "max_turns": maximum,
        "expected_turns": round(float(simulated.mean()), 3),
        "draws": PERMUTATIONS,
        "n_buyers": len(buyers),
        "buyers": buyers[:MAX_EVIDENCE_ROWS],
    }
    return {"value": coverage * balance * rate, "percentile": percentile, "n_obs": n_lots,
            "evidence": evidence}


def _iso(ordinal: int) -> str:
    return date.fromordinal(ordinal).isoformat()


# --- firm screens: bzp.spread and bzp.over_estimate ---------------------------------------


def _firm_screens(d: _Data, values: np.ndarray, test_id: str, *,
                  narrow_is_signal: bool) -> tuple[list[tuple], dict[int, np.ndarray]]:
    """Firm rows of one screen and the sorted valid values of each market (for references).

    A row: (test_id, 'firm', entity_key, market_id, value, percentile, n_obs, evidence).
    """
    valid = np.flatnonzero(~np.isnan(values))
    by_market: dict[int, np.ndarray] = {}
    if len(valid) == 0:
        return [], by_market
    vals = values[valid]
    mks = d.mk[valid]
    order = np.lexsort((vals, mks))
    mks_sorted, vals_sorted = mks[order], vals[order]
    for s, e in zip(*_bounds(mks_sorted), strict=True):
        by_market[int(mks_sorted[s])] = vals_sorted[s:e]

    winners = d.winner[valid]
    order = np.lexsort((vals, winners, mks))
    mk_o, w_o, v_o = mks[order], winners[order], vals[order]
    rows: list[tuple] = []
    starts, ends = _bounds(mk_o, w_o)
    sizes = ends - starts
    per_market: dict[int, list[tuple[int, float, dict]]] = defaultdict(list)
    for s, e in zip(starts[sizes >= MIN_FIRM_LOTS].tolist(),
                    ends[sizes >= MIN_FIRM_LOTS].tolist(), strict=True):
        market = int(mk_o[s])
        x = v_o[s:e]
        median = float(np.median(x))
        mean = float(x.mean())
        ref = by_market[market]
        ev = {"bidder_id": int(w_o[s]), "n_lots": e - s, "oecd": OECD_ITEM[test_id],
              "median": round(median, 6), "market_median": round(float(np.median(ref)), 6)}
        if narrow_is_signal:
            low = float(np.quantile(ref, LOW_SPREAD_QUANTILE))
            ev["cv"] = round(float(x.std(ddof=1)) / mean, 6) if mean > 0 and e - s > 1 else None
            ev["low_share"] = round(float((x <= low).mean()), 4)
        else:
            ev["above_estimate_share"] = round(float((x > 1.0).mean()), 4)
        per_market[market].append((int(w_o[s]), median, ev))
    for market, firms in per_market.items():
        values_m = [(-v if narrow_is_signal else v) for _, v, _ in firms]
        pcts = market_percentiles(values_m)
        market_id = d.markets[market]
        for (bidder, value, ev), pct in zip(firms, pcts, strict=True):
            rows.append((test_id, "firm", firm_key(bidder, market_id), market_id, value, pct,
                         len(firms), ev))
    return rows, by_market


def spread_cluster_result(cluster_id: str, spreads: np.ndarray,
                          reference: np.ndarray | None) -> dict | None:
    """bzp.spread of a cluster's lots against random samples of its market's lots."""
    x = spreads[~np.isnan(spreads)]
    if len(x) < MIN_FIRM_LOTS:
        return None
    median = float(np.median(x))
    cv = float(x.std(ddof=1) / x.mean()) if len(x) > 1 and x.mean() > 0 else 0.0
    evidence = {"oecd": OECD_ITEM[TEST_SPREAD], "n_lots": len(x),
                "median": round(median, 6), "cv": round(cv, 6)}
    percentile = None
    if reference is not None and len(reference) >= MIN_OBS:
        rng = np.random.default_rng(zlib.crc32(("spread|" + cluster_id).encode("utf-8")))
        sample = reference[rng.integers(0, len(reference), size=(PERMUTATIONS, len(x)))]
        medians = np.median(sample, axis=1)
        means = sample.mean(axis=1)
        cvs = sample.std(axis=1, ddof=1) / np.where(means > 0, means, np.nan)
        cvs = np.nan_to_num(cvs, nan=np.inf)
        p_narrow = _mid_rank(medians, median, higher_is_signal=False)
        p_const = _mid_rank(cvs, cv, higher_is_signal=False)
        percentile = max(p_narrow, p_const)
        evidence.update({"pct_narrow": round(p_narrow, 4), "pct_constant": round(p_const, 4),
                         "market_median": round(float(np.median(reference)), 6),
                         "draws": PERMUTATIONS})
    return {"value": median, "percentile": percentile, "n_obs": len(x),
            "evidence": evidence}


# --- bzp.shared_winners -------------------------------------------------------------------


def _shared_pairs(conn: psycopg.Connection, run_id: int) -> list[tuple]:
    with conn.cursor(row_factory=tuple_row) as cur:
        return cur.execute(_SHARED_SQL, {"run_id": run_id,
                                         "max_holders": MAX_SHARED_HOLDERS}).fetchall()


def _components(pairs: list[tuple[int, int]]) -> list[list[int]]:
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    groups: dict[int, list[int]] = defaultdict(list)
    for x in list(parent):
        groups[find(x)].append(x)
    return sorted(sorted(g) for g in groups.values())


# --- entry point --------------------------------------------------------------------------


def _write(conn: psycopg.Connection, run_id: int, rows: list[tuple],
           clusters: list[tuple]) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("delete from test_results where run_id = %s and test_id = any(%s)",
                        (run_id, list(BZP_TESTS)))
            cur.execute("delete from clusters where run_id = %s and cluster_id like %s",
                        (run_id, CLUSTER_PREFIX + "%"))
            with cur.copy(
                "copy test_results (run_id, test_id, level, entity_key, market_id, value,"
                " percentile, n_obs, evidence) from stdin"
            ) as copy:
                copy.set_types(["int8", "text", "text", "text", "text", "float8", "float8",
                                "int4", "jsonb"])
                for test_id, level, key, market_id, value, pct, n_obs, ev in rows:
                    copy.write_row((
                        run_id, test_id, level, key, market_id,
                        None if value is None else float(value),
                        None if pct is None else float(pct),
                        None if n_obs is None else int(n_obs), Jsonb(ev)))
            cur.executemany(
                "insert into clusters (run_id, cluster_id, market_id, members, n_lots,"
                " density, buyers) values (%s, %s, %s, %s, %s, null, %s)",
                [(run_id, *c) for c in clusters],
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def compute_bzp_only(conn: psycopg.Connection, run_id: int) -> dict:
    """Compute the BZP-only screens and winner-based clusters of `run_id`.

    Replaces the run's `bzp.*` rows of `test_results` and its `w:` clusters, commits and
    returns counters with the seconds spent in each step (`seconds`)."""
    seconds: dict[str, float] = {}
    clock = time.monotonic()

    def lap(step: str) -> None:
        nonlocal clock
        now = time.monotonic()
        seconds[step] = round(now - clock, 3)
        log.info("bzp_only run %s: %s took %.3fs", run_id, step, now - clock)
        clock = now

    d = _load(conn, run_id)
    lap("load")

    rows: list[tuple] = []
    groups: dict[tuple[int, tuple[int, ...]], _Cluster] = {}

    def cluster_of(mk: int, members: tuple[int, ...]) -> _Cluster:
        key = (mk, members)
        if key not in groups:
            groups[key] = _Cluster(mk, members)
        return groups[key]

    runs = _rotation_runs(d)
    for (mk, members), buyer_runs in sorted(runs.items()):
        market_id = d.markets[mk]
        cid = winner_cluster_id(market_id, members)
        result = rotation_result(cid, members, buyer_runs, d)
        rows.append((TEST_ROTATION, "cluster", cid, market_id, result["value"],
                     result["percentile"], result["n_obs"], result["evidence"]))
        cluster_of(mk, members).add(TEST_ROTATION, {r.buyer for r in buyer_runs})
    lap("rotation")

    market_index = {m: i for i, m in enumerate(d.markets)}
    shared = _shared_pairs(conn, run_id)
    conn.commit()
    by_market: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for market_id, a, b, acc, adr, a_wins, b_wins in shared:
        rows.append((TEST_SHARED, "pair", pair_key(a, b, market_id), market_id, 1.0, None,
                     None, {"oecd": OECD_ITEM[TEST_SHARED], "a": a, "b": b,
                            "shared_account": bool(acc), "shared_address": bool(adr),
                            "a_wins": int(a_wins), "b_wins": int(b_wins)}))
        if market_id in market_index:
            by_market[market_index[market_id]].append((a, b))
    oversized = 0
    for mk, pairs in sorted(by_market.items()):
        for members in _components(pairs):
            if not MIN_GROUP <= len(members) <= MAX_GROUP:
                oversized += 1
                continue
            cluster_of(mk, tuple(members)).add(TEST_SHARED, None)
    lap("shared_winners")

    over_rows, _ = _firm_screens(d, d.over, TEST_OVER_ESTIMATE, narrow_is_signal=False)
    rows.extend(over_rows)
    lap("over_estimate")
    spread_rows, spread_ref = _firm_screens(d, d.spread, TEST_SPREAD, narrow_is_signal=True)
    rows.extend(spread_rows)
    lap("spread_firms")

    # Lots of each cluster: awarded lots of its market won by a member (rotation-only
    # clusters: at the rotation buyers).
    member_ids = sorted({m for c in groups.values() for m in c.members})
    lots_of: dict[tuple[int, int], list[int]] = defaultdict(list)
    if member_ids:
        for i in np.flatnonzero(np.isin(d.winner, np.array(member_ids, dtype=np.int64))):
            lots_of[(int(d.mk[i]), int(d.winner[i]))].append(int(i))
    cluster_rows: list[tuple] = []
    for (mk, members), c in sorted(groups.items()):
        idx = sorted(i for m in members for i in lots_of.get((mk, m), ()))
        if c.buyers is not None:
            idx = [i for i in idx if int(d.buyer[i]) in c.buyers]
        market_id = d.markets[mk]
        cid = winner_cluster_id(market_id, members)
        cluster_rows.append((cid, market_id, list(members), len(idx),
                             None if c.buyers is None else sorted(c.buyers)))
        if TEST_SHARED in c.sources:
            rows.append((TEST_SHARED, "cluster", cid, market_id, 1.0, None, None,
                         {"oecd": OECD_ITEM[TEST_SHARED], "members": list(members)}))
        spread = spread_cluster_result(cid, d.spread[np.array(idx, dtype=np.int64)],
                                       spread_ref.get(mk))
        if spread is not None:
            spread["evidence"]["members"] = list(members)
            rows.append((TEST_SPREAD, "cluster", cid, market_id, spread["value"],
                         spread["percentile"], spread["n_obs"], spread["evidence"]))
    lap("clusters")

    _write(conn, run_id, rows, cluster_rows)
    lap("write")
    counts = {
        "lots": d.n,
        "markets": len(d.markets),
        "rotation_groups": len(runs),
        "shared_pairs": len(shared),
        "shared_oversized": oversized,
        "clusters": len(cluster_rows),
        "rows": len(rows),
        "seconds": seconds,
    }
    return counts
