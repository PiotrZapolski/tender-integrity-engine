"""Synthetic procurement markets for engine tests.

`make_market` writes a complete market into the test database: buyers of one province,
firms, single-lot procedures of one CPV code, lots with BZP facts, one parsed opening
document per procedure and its bids. Optionally a cartel is injected. Everything is driven by
`numpy.random.default_rng(seed)`, so the same arguments always produce the same market
(serial ids aside); identifiers are namespaced by the arguments, so several markets fit in
one database.

Normal competition: estimated value log-normal around 400k PLN, 2-6 offers per lot drawn
from the non-cartel firms (weighted, some firms are more active), prices log-normal around
the estimated value with sigma 0.12, the cheapest offer wins.

Cartel lots (`share` of all lots, picked at random) have prices inflated by `inflation`
(default 1.10) and a winner chosen by the cartel, always the cheapest offer:
- rotation: all members bid together (plus 0-3 outsiders) and win in turn, in date order;
- cover: all members bid together, a random member wins, the other members bid 5-8% above it;
- split: each member owns two buyers; a member bids only at its own buyers and wins there,
  the other members stay away (1-5 outsiders lose);
- shared_account: all members bid together, a random member wins; members share one
  `bidder_accounts.account_hash` (every firm also has an account of its own).
Cartel members never bid in normal lots.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import numpy as np
import psycopg

from tie.identity.names import normalize_name

PATTERNS = ("rotation", "cover", "split", "shared_account")
SIGMA = 0.12
MIN_BIDS = 2
MAX_BIDS = 6
START = date(2023, 3, 1)
SPAN_DAYS = 1000
CONTRACT_LAG_DAYS = 14
AS_OF = date(2026, 1, 1)
CENT = Decimal("0.01")
_NIP_WEIGHTS = (6, 5, 7, 2, 3, 4, 5, 6, 7)


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _nip(seed_text: str) -> str:
    """Deterministic NIP with a valid check digit."""
    base = int(_h(seed_text), 16) % 900_000_000 + 100_000_000
    while True:
        digits = str(base)
        check = sum(int(d) * w for d, w in zip(digits, _NIP_WEIGHTS, strict=True)) % 11
        if check != 10:
            return digits + str(check)
        base = base + 1 if base < 999_999_999 else 100_000_000


def _money(x: float | Decimal) -> Decimal:
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


def _validate(n_firms: int, n_lots: int, cartel: dict | None) -> tuple[str | None, int, float]:
    if n_lots < 1:
        raise ValueError("n_lots must be at least 1")
    if cartel is None:
        if n_firms < MIN_BIDS:
            raise ValueError("a market needs at least 2 firms")
        return None, 0, 0.0
    pattern = cartel.get("pattern")
    if pattern not in PATTERNS:
        raise ValueError(f"unknown cartel pattern {pattern!r}, expected one of {PATTERNS}")
    members = int(cartel.get("members", 3))
    share = float(cartel.get("share", 0.4))
    if members < 2:
        raise ValueError("a cartel needs at least 2 members")
    if pattern != "split" and members > MAX_BIDS:
        raise ValueError(f"pattern {pattern} allows at most {MAX_BIDS} members")
    if n_firms - members < MIN_BIDS:
        raise ValueError("a market needs at least 2 firms outside the cartel")
    if not 0.0 <= share <= 1.0:
        raise ValueError("cartel share must be between 0 and 1")
    return pattern, members, share


def make_market(
    conn: psycopg.Connection,
    *,
    seed: int,
    n_firms: int,
    n_lots: int,
    cpv: str,
    province_code: str,
    cartel: dict | None = None,
) -> dict:
    """Create a synthetic market and commit it.

    `cartel`: `{"members": 3, "pattern": "rotation"|"cover"|"split"|"shared_account",
    "share": 0.4}`, optionally `"inflation": 1.10` (price level of cartel lots vs estimate).

    Returns `{"firms", "cartel", "outsiders", "jdg", "buyers", "lots", "procedures",
    "cartel_lots", "own_buyers", "account_hash", "pattern", "cpv", "province", "as_of"}`:
    bidder, buyer and lot ids are lists in creation order (`lots` in date order),
    `own_buyers` maps each split member to its buyer ids (empty otherwise), `account_hash` is
    the account shared by a `shared_account` cartel (None otherwise), `as_of` is a run date
    whose 36-month window covers every lot.
    """
    pattern, n_members, share = _validate(n_firms, n_lots, cartel)
    inflation = float((cartel or {}).get("inflation", 1.10))
    namespace = "|".join(
        [str(seed), cpv, province_code, str(n_firms), str(n_lots),
         repr(sorted((cartel or {}).items()))]
    )
    tag = _h(namespace)[:8]
    rng = np.random.default_rng(seed)

    member_idx = (
        sorted(int(i) for i in rng.choice(n_firms, n_members, replace=False))
        if n_members else []
    )
    outsider_idx = [i for i in range(n_firms) if i not in member_idx]
    jdg_idx = outsider_idx[-1] if len(outsider_idx) >= 3 else None
    # Activity weights of outsiders: a few firms bid often, most rarely.
    weights = 1.0 / np.sqrt(np.arange(1, len(outsider_idx) + 1))
    weights = rng.permutation(weights)
    weights = weights / weights.sum()

    n_buyers = max(8, 2 * n_members + 4)
    n_cartel = int(share * n_lots + 0.5) if pattern else 0
    cartel_k = (
        {int(k) for k in rng.choice(n_lots, n_cartel, replace=False)} if n_cartel else set()
    )

    try:
        with conn.cursor() as cur:
            buyers = []
            for j in range(n_buyers):
                row = cur.execute(
                    "insert into buyers(nip, name, buyer_type, province, city)"
                    " values (%s, %s, %s, %s, %s) returning id",
                    (_nip(f"{namespace}:buyer:{j}"), f"Gmina Testowa {j + 1} {tag}",
                     "administracja samorzadowa", province_code, f"Miasto {j + 1} {tag}"),
                ).fetchone()
                buyers.append(row["id"])

            firms: list[int] = []
            names: list[str] = []
            nips: list[str] = []
            for i in range(n_firms):
                if i == jdg_idx:
                    name = f"Usługi Remontowe Jan Testowy {tag}"
                else:
                    name = f"Firma Testowa {i + 1:02d} {tag} sp. z o.o."
                nip = _nip(f"{namespace}:firm:{i}")
                row = cur.execute(
                    "insert into bidders(nip, name, name_norm, is_jdg, city, province, address)"
                    " values (%s, %s, %s, %s, %s, %s, %s) returning id",
                    (nip, name, normalize_name(name), i == jdg_idx, f"Miasto {tag}",
                     province_code, f"ul. Testowa {i + 1}, Miasto {tag}"),
                ).fetchone()
                firms.append(row["id"])
                names.append(name)
                nips.append(nip)
                cur.execute(
                    "insert into bidder_accounts(bidder_id, account_hash) values (%s, %s)",
                    (row["id"], _h(f"{namespace}:account:{i}")),
                )
            account_hash = None
            if pattern == "shared_account":
                account_hash = _h(f"{namespace}:cartel-account")
                for i in member_idx:
                    cur.execute(
                        "insert into bidder_accounts(bidder_id, account_hash) values (%s, %s)",
                        (firms[i], account_hash),
                    )

            own_buyer_pos = (
                {m: [2 * pos, 2 * pos + 1] for pos, m in enumerate(member_idx)}
                if pattern == "split" else {}
            )

            lots: list[int] = []
            procedures: list[str] = []
            cartel_lots: list[int] = []
            turn = 0
            for k in range(n_lots):
                estimate = Decimal(max(10_000, int(round(
                    400_000 * float(np.exp(rng.normal(0.0, 0.8))), -2))))
                is_cartel = k in cartel_k

                if not is_cartel:
                    n = min(int(rng.integers(MIN_BIDS, MAX_BIDS + 1)), len(outsider_idx))
                    picked = [outsider_idx[int(p)] for p in
                              rng.choice(len(outsider_idx), n, replace=False, p=weights)]
                    raw = float(estimate) * np.exp(rng.normal(0.0, SIGMA, n))
                    prices = [_money(float(x)) for x in raw]
                    winner = int(np.argmin(raw))
                    buyer_pos = int(rng.integers(n_buyers))
                else:
                    if pattern == "split":
                        owner = member_idx[turn % n_members]
                        extra = int(rng.integers(1, MAX_BIDS))
                        in_cartel = [owner]
                        buyer_pos = own_buyer_pos[owner][int(rng.integers(2))]
                    else:
                        extra = int(rng.integers(0, MAX_BIDS - n_members + 1))
                        in_cartel = list(member_idx)
                        if pattern == "rotation":
                            owner = member_idx[turn % n_members]
                        else:
                            owner = member_idx[int(rng.integers(n_members))]
                        buyer_pos = int(rng.integers(n_buyers))
                    extra = min(extra, len(outsider_idx))
                    outs = []
                    if extra:
                        outs = [outsider_idx[int(p)] for p in
                                rng.choice(len(outsider_idx), extra, replace=False, p=weights)]
                    picked = in_cartel + outs
                    n = len(picked)
                    raw = sorted(float(x) for x in
                                 float(estimate) * inflation * np.exp(rng.normal(0.0, SIGMA, n)))
                    winner = picked.index(owner)
                    # The cartel's pick gets the lowest price, the others the rest.
                    rest = iter(raw[1:])
                    prices = [_money(raw[0]) if pos == winner else _money(next(rest))
                              for pos in range(n)]
                    if pattern == "cover":
                        for pos, firm_i in enumerate(picked):
                            if pos != winner and firm_i in member_idx:
                                bump = Decimal(f"{float(rng.uniform(1.05, 1.08)):.6f}")
                                prices[pos] = _money(prices[winner] * bump)
                    turn += 1

                # The winner is strictly the cheapest after rounding to grosze.
                for pos in range(n):
                    if pos != winner and prices[pos] <= prices[winner]:
                        prices[pos] = prices[winner] + CENT
                order = sorted(range(n), key=lambda p: (prices[p], p))
                rank = {p: r + 1 for r, p in enumerate(order)}
                offer_no = [int(x) + 1 for x in rng.permutation(n)]

                published = datetime.combine(
                    START + timedelta(days=k * SPAN_DAYS // n_lots), datetime.min.time(),
                    tzinfo=UTC,
                ) + timedelta(hours=10)
                ocds_id = f"ocds-synth-{tag}-{k + 1:05d}"
                cur.execute(
                    "insert into procedures(ocds_id, bzp_number, notice_number, buyer_id, title,"
                    " order_type, mode, cpv_main, cpv_all, is_below_eu, platform, published_at)"
                    " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (ocds_id, f"2025/BZP {tag}{k + 1:05d}", f"SYN/{tag}/{k + 1:05d}",
                     buyers[buyer_pos], f"Zamówienie testowe {k + 1} ({cpv})", "Services",
                     "Tryb podstawowy", cpv, [cpv], True, "ezamowienia", published),
                )
                winner_price = prices[winner]
                lot = cur.execute(
                    "insert into lots(procedure_id, lot_no, cpv, estimated_value, outcome,"
                    " n_offers, n_rejected, price_min, price_max, price_winner, contract_value,"
                    " contract_date, winner_bidder_id, consortium)"
                    " values (%s, 1, %s, %s, 'umowa', %s, 0, %s, %s, %s, %s, %s, %s, false)"
                    " returning id",
                    (ocds_id, cpv, estimate, n, min(prices), max(prices), winner_price,
                     winner_price, published.date() + timedelta(days=CONTRACT_LAG_DAYS),
                     firms[picked[winner]]),
                ).fetchone()
                doc = cur.execute(
                    "insert into documents(procedure_id, source, source_doc_id, file_name,"
                    " doc_type, text_method, status)"
                    " values (%s, 'synth', %s, 'informacja_z_otwarcia_ofert.pdf', 'opening',"
                    " 'synth', 'parsed') returning id",
                    (ocds_id, f"{tag}-{k + 1:05d}"),
                ).fetchone()
                cur.executemany(
                    "insert into bids(lot_id, document_id, offer_no, bidder_id, bidder_name_raw,"
                    " bidder_nip_raw, price, price_scale, is_winner, rank, parse_method,"
                    " match_confidence, outside_bzp_range)"
                    " values (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s, 'synth', 1.0, false)",
                    [(lot["id"], doc["id"], offer_no[pos], firms[f], names[f], nips[f],
                      prices[pos], pos == winner, rank[pos]) for pos, f in enumerate(picked)],
                )
                lots.append(lot["id"])
                procedures.append(ocds_id)
                if is_cartel:
                    cartel_lots.append(lot["id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "firms": firms,
        "cartel": [firms[i] for i in member_idx],
        "outsiders": [firms[i] for i in outsider_idx],
        "jdg": [firms[jdg_idx]] if jdg_idx is not None else [],
        "buyers": buyers,
        "lots": lots,
        "procedures": procedures,
        "cartel_lots": cartel_lots,
        "own_buyers": {firms[m]: [buyers[p] for p in pos] for m, pos in own_buyer_pos.items()},
        "account_hash": account_hash,
        "pattern": pattern,
        "cpv": cpv,
        "province": province_code,
        "as_of": AS_OF,
    }
