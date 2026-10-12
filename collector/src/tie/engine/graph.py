"""Co-bidder graph and Leiden clusters per market.

Input: lots assigned to markets for the run (`lot_market.run_id`) and their bids from opening
documents that passed the cross-check (`documents.status = 'parsed'`), with a known
`bidder_id`. Offers outside the BZP price range count too (co-occurrence, not prices).
The winner of a lot is the BZP winner (`lots.winner_bidder_id`), else the parsed bid marked
`is_winner`.

`build_pairs` writes `pairs`: for each market and each pair of firms that bid in the same lot
at least twice, `n_together`, the wins of each side in those lots, the wins of other firms,
and whether the two share a bank account (`bidder_accounts`) or an address (`bidders.address`
compared case- and whitespace-insensitively). `rotation` and `ratio_cv` are left null for the
pair tests.

`find_clusters` builds one weighted graph per market from `pairs` (edge weight `n_together`,
only pairs with `n_together >= min_together`), runs Leiden (`RBConfigurationVertexPartition`,
fixed seed, vertices and edges in a fixed order so the result is reproducible) and writes the
communities with 2-12 members to `clusters`:
- `n_lots`: lots of the market in which at least two members bid together;
- `density`: average share of those lots in which a given pair of members bid together,
  i.e. sum over the lots of C(c, 2) / (C(k, 2) * n_lots), where c is the number of members
  bidding in the lot and k the cluster size. 1.0 means every member bids in every one of the
  cluster's lots; firms that merely meet often in a busy market stay well below. For k = 2
  it is 1.0 by definition.
- `cluster_id`: `cluster_key(market_id, members)`, sha1 of the market and the sorted member
  ids, 16 hex characters, so the same firms in the same market keep their id across runs.

Both functions replace the rows of their `run_id` and commit.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict

import igraph as ig
import leidenalg
import psycopg

MIN_PAIR_LOTS = 2
MIN_CLUSTER_SIZE = 2
MAX_CLUSTER_SIZE = 12

# One row per (market, lot, bidder) taken into account.
_LOT_BIDDERS_SQL = """
select distinct lm.market_id, b.lot_id, b.bidder_id
from lot_market lm
join bids b on b.lot_id = lm.lot_id
join documents d on d.id = b.document_id
where lm.run_id = %(run_id)s and d.status = 'parsed' and b.bidder_id is not null
"""

_BUILD_PAIRS_SQL = f"""
with lb as ({_LOT_BIDDERS_SQL}),
lot_winner as (
  select l.id as lot_id,
         coalesce(l.winner_bidder_id, (
           select b.bidder_id from bids b join documents d on d.id = b.document_id
           where b.lot_id = l.id and b.is_winner and d.status = 'parsed'
             and b.bidder_id is not null
           order by b.id limit 1)) as winner
  from lots l
  where l.id in (select lot_id from lb)
),
together as (
  select x.market_id, x.bidder_id as a, y.bidder_id as b,
         count(*) as n_together,
         count(*) filter (where w.winner = x.bidder_id) as a_wins,
         count(*) filter (where w.winner = y.bidder_id) as b_wins,
         count(*) filter (
           where w.winner is not null and w.winner <> x.bidder_id and w.winner <> y.bidder_id
         ) as other_wins
  from lb x
  join lb y on y.lot_id = x.lot_id and y.market_id = x.market_id and x.bidder_id < y.bidder_id
  left join lot_winner w on w.lot_id = x.lot_id
  group by x.market_id, x.bidder_id, y.bidder_id
  having count(*) >= {MIN_PAIR_LOTS}
)
insert into pairs (run_id, a, b, market_id, n_together, a_wins, b_wins, other_wins,
                   shared_account, shared_address)
select %(run_id)s, t.a, t.b, t.market_id, t.n_together, t.a_wins, t.b_wins, t.other_wins,
       exists (
         select 1 from bidder_accounts aa
         join bidder_accounts ab on ab.account_hash = aa.account_hash
         where aa.bidder_id = t.a and ab.bidder_id = t.b
       ),
       coalesce(
         nullif(lower(regexp_replace(trim(ba.address), '\\s+', ' ', 'g')), '')
           = lower(regexp_replace(trim(bb.address), '\\s+', ' ', 'g')),
         false)
from together t
join bidders ba on ba.id = t.a
join bidders bb on bb.id = t.b
"""


def cluster_key(market_id: str, members) -> str:
    """Stable cluster id: sha1 of the market and the sorted member ids, 16 hex characters."""
    text = market_id + "|" + ",".join(str(m) for m in sorted(int(m) for m in members))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def build_pairs(conn: psycopg.Connection, run_id: int) -> int:
    """Write co-bidder pairs of the run (replacing earlier ones); returns the number written."""
    try:
        conn.execute("delete from pairs where run_id = %s", (run_id,))
        cur = conn.execute(_BUILD_PAIRS_SQL, {"run_id": run_id})
        written = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return written


def _lot_bidders(conn: psycopg.Connection, run_id: int) -> dict[str, dict[int, set[int]]]:
    lots: dict[str, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
    for r in conn.execute(_LOT_BIDDERS_SQL, {"run_id": run_id}):
        lots[r["market_id"]][r["lot_id"]].add(r["bidder_id"])
    return lots


def _communities(
    edges: list[tuple[int, int, int]], *, resolution: float, seed: int
) -> list[list[int]]:
    """Leiden communities (as sorted bidder ids) of one market's weighted graph."""
    nodes = sorted({a for a, _, _ in edges} | {b for _, b, _ in edges})
    index = {bidder: i for i, bidder in enumerate(nodes)}
    graph = ig.Graph(n=len(nodes), edges=[(index[a], index[b]) for a, b, _ in edges])
    graph.es["weight"] = [float(w) for _, _, w in edges]
    partition = leidenalg.find_partition(
        graph,
        leidenalg.RBConfigurationVertexPartition,
        weights="weight",
        resolution_parameter=resolution,
        seed=seed,
    )
    return [sorted(nodes[v] for v in community) for community in partition]


def _cohesion(members: list[int], lots: dict[int, set[int]]) -> tuple[int, float]:
    """(n_lots, density) of a cluster over the lots of its market, see the module docstring."""
    member_set = set(members)
    k = len(members)
    n_lots = 0
    pair_lots = 0
    for bidders in lots.values():
        c = len(bidders & member_set)
        if c >= 2:
            n_lots += 1
            pair_lots += c * (c - 1) // 2
    if n_lots == 0:
        return 0, 0.0
    return n_lots, pair_lots / (k * (k - 1) // 2 * n_lots)


def find_clusters(
    conn: psycopg.Connection,
    run_id: int,
    *,
    min_together: int = 3,
    resolution: float = 1.0,
    seed: int = 42,
) -> int:
    """Write Leiden clusters of the run (replacing earlier ones); returns the number written."""
    edges_by_market: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    for r in conn.execute(
        "select market_id, a, b, n_together from pairs"
        " where run_id = %s and n_together >= %s order by market_id, a, b",
        (run_id, min_together),
    ):
        edges_by_market[r["market_id"]].append((r["a"], r["b"], r["n_together"]))

    lots = _lot_bidders(conn, run_id) if edges_by_market else {}
    rows = []
    for market_id in sorted(edges_by_market):
        for members in _communities(
            edges_by_market[market_id], resolution=resolution, seed=seed
        ):
            if not MIN_CLUSTER_SIZE <= len(members) <= MAX_CLUSTER_SIZE:
                continue
            n_lots, density = _cohesion(members, lots.get(market_id, {}))
            rows.append(
                (run_id, cluster_key(market_id, members), market_id, members, n_lots, density)
            )

    try:
        conn.execute("delete from clusters where run_id = %s", (run_id,))
        with conn.cursor() as cur:
            cur.executemany(
                "insert into clusters (run_id, cluster_id, market_id, members, n_lots, density)"
                " values (%s, %s, %s, %s, %s, %s)",
                rows,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)
