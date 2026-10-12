import json
import logging
from datetime import date

import httpx
import pytest
import respx

from tie.bzp.fetch import BZP_URL, ORDER_TYPES, PROVINCES, fetch_day, store_day
from tie.http import PoliteClient
from tie.rawstore import RawStore


def rec(n, prov="PL14", otype="Works"):
    return {"noticeNumber": f"2026/BZP {n:08d}/01", "organizationProvince": prov,
            "orderType": otype}


def client():
    return PoliteClient("ua", {}, sleep=lambda s: None)


def test_constants():
    assert BZP_URL == "https://ezamowienia.gov.pl/mo-board/api/v1/notice"
    assert len(PROVINCES) == 16
    assert PROVINCES[0] == "PL02" and PROVINCES[-1] == "PL32"
    # The real API enum (ENUM.002) uses "Delivery" for supplies; "Supplies" returns HTTP 400.
    assert ORDER_TYPES == ["Works", "Delivery", "Services"]


@respx.mock
def test_small_day_single_call():
    route = respx.get(BZP_URL).mock(return_value=httpx.Response(200, json=[rec(1), rec(2)]))
    out = fetch_day(client(), date(2026, 10, 1), page_size=5)
    assert len(out) == 2 and route.call_count == 1
    q = route.calls.last.request.url.params
    assert q["NoticeType"] == "TenderResultNotice"
    assert q["PublicationDateFrom"] == "2026-10-01" == q["PublicationDateTo"]
    assert q["PageSize"] == "5"
    assert "OrganizationProvince" not in q and "OrderType" not in q


@respx.mock
def test_saturated_day_splits_by_province_then_order_type(caplog):
    def handler(request):
        p = request.url.params
        if "OrganizationProvince" not in p:
            return httpx.Response(200, json=[rec(i) for i in range(3)])
        if p["OrganizationProvince"] == "PL14" and "OrderType" not in p:
            return httpx.Response(200, json=[rec(i) for i in range(3)])
        if p["OrganizationProvince"] == "PL14":
            base = {"Works": 10, "Delivery": 20, "Services": 30}[p["OrderType"]]
            return httpx.Response(200, json=[rec(base + i) for i in range(2)])
        if p["OrganizationProvince"] == "PL02":
            return httpx.Response(200, json=[rec(100, "PL02")])
        return httpx.Response(200, json=[])

    route = respx.get(BZP_URL).mock(side_effect=handler)
    with caplog.at_level(logging.WARNING, logger="tie.bzp.fetch"):
        out = fetch_day(client(), date(2026, 10, 1), page_size=3)
    numbers = {r["noticeNumber"] for r in out}
    assert len(numbers) == len(out)
    assert {"2026/BZP 00000010/01", "2026/BZP 00000031/01", "2026/BZP 00000100/01"} <= numbers
    assert "2026/BZP 00000000/01" in numbers
    # 1 unfiltered + 16 provinces + 3 order types for the saturated province only
    assert route.call_count == 1 + 16 + 3
    sent_types = [c.request.url.params.get("OrderType") for c in route.calls]
    assert sorted(t for t in sent_types if t) == ["Delivery", "Services", "Works"]
    assert "bzp_partition_saturated" not in caplog.text


@respx.mock
def test_saturated_order_type_logs_warning(caplog):
    def handler(request):
        p = request.url.params
        prov = p.get("OrganizationProvince")
        if prov is None or prov == "PL14":
            otype = p.get("OrderType", "Works")
            offset = {"Works": 0, "Delivery": 100, "Services": 200}[otype]
            return httpx.Response(200, json=[rec(offset + i, "PL14", otype) for i in range(2)])
        return httpx.Response(200, json=[])

    respx.get(BZP_URL).mock(side_effect=handler)
    with caplog.at_level(logging.WARNING, logger="tie.bzp.fetch"):
        out = fetch_day(client(), date(2026, 10, 1), page_size=2)
    assert len(out) == 6
    saturated = [r for r in caplog.records if "bzp_partition_saturated" in r.getMessage()]
    assert len(saturated) == 3
    msg = saturated[0].getMessage()
    assert "2026-10-01" in msg and "PL14" in msg and "Works" in msg


@respx.mock
def test_http_error_raises():
    respx.get(BZP_URL).mock(return_value=httpx.Response(400, json={"errors": {}}))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_day(client(), date(2026, 10, 1), page_size=5)


def test_store_day(tmp_path):
    rs = RawStore(tmp_path)
    sha = store_day(rs, date(2026, 10, 1), [rec(2), rec(1)])
    data = json.loads(rs.get(sha, source="bzp"))
    assert [r["noticeNumber"] for r in data] == ["2026/BZP 00000001/01", "2026/BZP 00000002/01"]


def test_store_day_keeps_polish_characters(tmp_path):
    rs = RawStore(tmp_path)
    r = rec(1) | {"organizationName": "Gmina Kołbudy"}
    sha = store_day(rs, date(2026, 10, 1), [r])
    raw = rs.get(sha, source="bzp")
    assert "Kołbudy".encode() in raw
    assert store_day(rs, date(2026, 10, 1), [r]) == sha
