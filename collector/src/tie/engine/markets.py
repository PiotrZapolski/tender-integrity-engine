"""Markets: CPV group x buyer voivodeship x rolling window, with fallback levels.

Every lot whose date (``contract_date``, else the procedure's ``published_at`` in Polish time)
falls in the window ``(as_of - window_months, as_of]`` gets exactly one market. Levels are tried
in order:

- ``cpv5_prov``: 5-digit CPV group in the buyer's voivodeship, kept when it has at least
  ``min_lots`` lots in the window;
- ``cpv3_prov``: lots left over from too small ``cpv5_prov`` markets, pooled by 3-digit CPV group
  in the same voivodeship, kept when the pool has at least ``min_lots`` lots;
- ``cpv3_pl``: everything still left, pooled by 3-digit CPV group across Poland. Final level,
  no minimum. Lots whose buyer has no known voivodeship go straight here.

The lot CPV is ``lots.cpv``, else ``procedures.cpv_main``; lots with neither are not assigned.
``lot_market`` holds the assignment of the latest run only (``lot_id`` is its primary key), so
each call replaces it as a whole. ``markets`` rows are upserted; rows of markets that no longer
exist keep their old ``run_id`` and are never touched again.
"""

from __future__ import annotations

import calendar
import re
from collections import Counter
from datetime import date

import psycopg
from psycopg.rows import tuple_row

LEVEL_CPV5_PROV = "cpv5_prov"
LEVEL_CPV3_PROV = "cpv3_prov"
LEVEL_CPV3_PL = "cpv3_pl"

PROVINCE_CODES = frozenset(f"PL{n:02d}" for n in range(2, 33, 2))
_PROVINCE_NAMES = {
    "dolnośląskie": "PL02",
    "kujawsko-pomorskie": "PL04",
    "lubelskie": "PL06",
    "lubuskie": "PL08",
    "łódzkie": "PL10",
    "małopolskie": "PL12",
    "mazowieckie": "PL14",
    "opolskie": "PL16",
    "podkarpackie": "PL18",
    "podlaskie": "PL20",
    "pomorskie": "PL22",
    "śląskie": "PL24",
    "świętokrzyskie": "PL26",
    "warmińsko-mazurskie": "PL28",
    "wielkopolskie": "PL30",
    "zachodniopomorskie": "PL32",
}
_SPACES = re.compile(r"\s+")

_ELIGIBLE_LOTS = """
with dated as (
  select l.id,
         coalesce(nullif(btrim(l.cpv), ''), nullif(btrim(p.cpv_main), '')) as cpv,
         b.province,
         coalesce(l.contract_date, (p.published_at at time zone 'Europe/Warsaw')::date) as day
  from lots l
  join procedures p on p.ocds_id = l.procedure_id
  left join buyers b on b.id = p.buyer_id
)
select id, cpv, province from dated
where day > %(start)s and day <= %(as_of)s
order by id
"""

_UPSERT_MARKET = (
    "insert into markets(market_id, cpv_group, province, level, n_lots, run_id)"
    " values (%s, %s, %s, %s, %s, %s)"
    " on conflict (market_id) do update set"
    "  cpv_group = excluded.cpv_group, province = excluded.province,"
    "  level = excluded.level, n_lots = excluded.n_lots, run_id = excluded.run_id"
)


def cpv_group(cpv: str | None, digits: int) -> str | None:
    """First ``digits`` digits of a CPV code like ``45233140-2``; None when not available."""
    if not cpv or digits < 1:
        return None
    code = _SPACES.sub("", cpv).split("-", 1)[0]
    if not code.isdigit() or len(code) < digits:
        return None
    return code[:digits]


def normalize_province(value: str | None) -> str | None:
    """Voivodeship code ``PLnn`` from a code or a Polish name; None when unknown."""
    if not value:
        return None
    text = _SPACES.sub(" ", value).strip()
    if text.upper() in PROVINCE_CODES:
        return text.upper()
    name = text.lower().removeprefix("województwo ").strip()
    return _PROVINCE_NAMES.get(name)


def market_id(level: str, group: str, province: str | None) -> str:
    return f"{level}:{group}:{province or 'PL'}"


def window_start(as_of: date, months: int) -> date:
    """``as_of`` shifted back by ``months`` calendar months (day clamped to month end)."""
    index = as_of.year * 12 + as_of.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(as_of.day, calendar.monthrange(year, month)[1]))


def assign_markets(
    conn: psycopg.Connection,
    run_id: int,
    as_of: date,
    *,
    window_months: int = 36,
    min_lots: int = 30,
) -> int:
    """Write ``markets`` and ``lot_market`` for ``run_id``; return the number of assigned lots."""
    start = window_start(as_of, window_months)
    with conn.cursor(row_factory=tuple_row) as cur:
        rows = cur.execute(_ELIGIBLE_LOTS, {"start": start, "as_of": as_of}).fetchall()

    lots: list[tuple[int, str | None, str, str | None]] = []
    for lot_id, cpv, province in rows:
        g3 = cpv_group(cpv, 3)
        if g3 is None:
            continue
        lots.append((lot_id, cpv_group(cpv, 5), g3, normalize_province(province)))

    markets: dict[str, tuple[str, str, str | None]] = {}
    sizes: Counter[str] = Counter()
    assignment: list[tuple[int, str]] = []

    def put(lot_id: int, level: str, group: str, province: str | None) -> None:
        mid = market_id(level, group, province)
        markets[mid] = (level, group, province)
        sizes[mid] += 1
        assignment.append((lot_id, mid))

    cpv5_sizes = Counter((g5, prov) for _, g5, _, prov in lots if g5 and prov)
    rest = []
    for lot_id, g5, g3, prov in lots:
        if g5 and prov and cpv5_sizes[(g5, prov)] >= min_lots:
            put(lot_id, LEVEL_CPV5_PROV, g5, prov)
        else:
            rest.append((lot_id, g3, prov))

    cpv3_sizes = Counter((g3, prov) for _, g3, prov in rest if prov)
    for lot_id, g3, prov in rest:
        if prov and cpv3_sizes[(g3, prov)] >= min_lots:
            put(lot_id, LEVEL_CPV3_PROV, g3, prov)
        else:
            put(lot_id, LEVEL_CPV3_PL, g3, None)

    with conn.cursor() as cur:
        cur.execute("delete from lot_market")
        cur.executemany(
            _UPSERT_MARKET,
            [
                (mid, group, prov, level, sizes[mid], run_id)
                for mid, (level, group, prov) in sorted(markets.items())
            ],
        )
        with cur.copy("copy lot_market (lot_id, market_id, run_id) from stdin") as copy:
            for lot_id, mid in assignment:
                copy.write_row((lot_id, mid, run_id))
    conn.commit()
    return len(assignment)
