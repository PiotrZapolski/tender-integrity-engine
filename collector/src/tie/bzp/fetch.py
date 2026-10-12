"""Fetch one publication day of BZP result notices, partitioning around the page cap.

The board API ignores pagination parameters and returns at most `PageSize` records per
query, so a saturated day is split by organisation province and then by order type.
"""

from __future__ import annotations

import json
import logging
from datetime import date

BZP_URL = "https://ezamowienia.gov.pl/mo-board/api/v1/notice"
PROVINCES = [f"PL{n:02d}" for n in range(2, 33, 2)]
# Values of the API dictionary ENUM.002 / SL.MO.042. Supplies are "Delivery";
# sending "Supplies" makes the API answer HTTP 400.
ORDER_TYPES = ["Works", "Delivery", "Services"]
log = logging.getLogger(__name__)


def _query(client, day: date, page_size: int, **extra) -> list[dict]:
    params = {"NoticeType": "TenderResultNotice", "PublicationDateFrom": day.isoformat(),
              "PublicationDateTo": day.isoformat(), "PageSize": page_size, **extra}
    r = client.get(BZP_URL, params=params)
    r.raise_for_status()
    return r.json()


def fetch_day(client, day: date, *, page_size: int = 500) -> list[dict]:
    """Return unique (by `noticeNumber`) TenderResultNotice records published on `day`."""
    seen: dict[str, dict] = {}

    def add(rows: list[dict]) -> None:
        for r in rows:
            seen.setdefault(r["noticeNumber"], r)

    base = _query(client, day, page_size)
    add(base)
    if len(base) < page_size:
        return list(seen.values())
    for prov in PROVINCES:
        rows = _query(client, day, page_size, OrganizationProvince=prov)
        add(rows)
        if len(rows) < page_size:
            continue
        for ot in ORDER_TYPES:
            sub = _query(client, day, page_size, OrganizationProvince=prov, OrderType=ot)
            add(sub)
            if len(sub) >= page_size:
                log.warning("bzp_partition_saturated day=%s prov=%s type=%s page_size=%s",
                            day.isoformat(), prov, ot, page_size)
    return list(seen.values())


def store_day(rawstore, day: date, records: list[dict]) -> str:
    """Store the day's records as one raw JSON file (source "bzp") and return its sha256."""
    rows = sorted(records, key=lambda r: r["noticeNumber"])
    return rawstore.put(json.dumps(rows, ensure_ascii=False).encode("utf-8"), source="bzp")
