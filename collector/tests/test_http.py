import json

import httpx
import pytest
import respx

from tie.http import HttpGiveUp, PoliteClient


def make(sleeps):
    return PoliteClient("ua-test", {"example.com": 0.0}, base_delay=1.0, sleep=sleeps.append)


@respx.mock
def test_retries_on_429_with_retry_after():
    sleeps = []
    route = respx.get("https://example.com/a").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, text="ok"),
    ])
    r = make(sleeps).get("https://example.com/a")
    assert r.text == "ok"
    assert route.call_count == 2
    assert 7 in sleeps


@respx.mock
def test_backoff_on_503_then_give_up():
    sleeps = []
    respx.get("https://example.com/b").mock(return_value=httpx.Response(503))
    c = PoliteClient("ua", {}, max_retries=3, base_delay=1.0, sleep=sleeps.append)
    with pytest.raises(HttpGiveUp):
        c.get("https://example.com/b")
    assert [s for s in sleeps if s >= 1] == [1.0, 2.0, 4.0]


@respx.mock
def test_404_returned_without_retry():
    route = respx.get("https://example.com/c").mock(return_value=httpx.Response(404))
    r = make([]).get("https://example.com/c")
    assert r.status_code == 404 and route.call_count == 1


@respx.mock
def test_user_agent_sent():
    route = respx.get("https://example.com/d").mock(return_value=httpx.Response(200))
    make([]).get("https://example.com/d")
    assert route.calls.last.request.headers["User-Agent"] == "ua-test"


@respx.mock
def test_rate_limit_spacing():
    sleeps = []
    respx.get("https://slow.pl/x").mock(return_value=httpx.Response(200))
    times = iter([100.0, 100.0, 100.2, 100.2, 101.5, 101.5])
    c = PoliteClient("ua", {"slow.pl": 1.0}, sleep=sleeps.append, clock=lambda: next(times))
    c.get("https://slow.pl/x")
    c.get("https://slow.pl/x")
    assert any(abs(s - 0.8) < 1e-6 for s in sleeps)


@respx.mock
def test_rate_limit_shared_by_subdomains():
    sleeps = []
    respx.get("https://a.platform.pl/x").mock(return_value=httpx.Response(200))
    respx.get("https://b.platform.pl/x").mock(return_value=httpx.Response(200))
    respx.get("https://other.pl/x").mock(return_value=httpx.Response(200))
    times = iter([100.0, 100.0, 101.0, 101.0, 101.0, 101.0])
    c = PoliteClient("ua", {"platform.pl": 5.0}, sleep=sleeps.append, clock=lambda: next(times))
    c.get("https://a.platform.pl/x")
    c.get("https://b.platform.pl/x")
    assert sleeps == [4.0]
    c.get("https://other.pl/x")
    assert sleeps == [4.0]


@respx.mock
def test_transport_error_retried_then_give_up_with_none_status():
    sleeps = []
    route = respx.get("https://example.com/e").mock(side_effect=httpx.ConnectError("boom"))
    c = PoliteClient("ua", {}, max_retries=2, base_delay=0.5, sleep=sleeps.append)
    with pytest.raises(HttpGiveUp) as exc:
        c.get("https://example.com/e")
    assert exc.value.status is None
    assert exc.value.url == "https://example.com/e"
    assert route.call_count == 3
    assert sleeps == [0.5, 1.0]


@respx.mock
def test_post_json_sends_body_and_headers():
    route = respx.post("https://example.com/p").mock(return_value=httpx.Response(200, json={}))
    c = make([])
    r = c.post_json("https://example.com/p", {"a": 1}, headers={"X-Key": "k"})
    assert r.status_code == 200
    req = route.calls.last.request
    assert req.headers["X-Key"] == "k"
    assert req.headers["User-Agent"] == "ua-test"
    assert json.loads(req.content) == {"a": 1}


@respx.mock
def test_retry_after_capped_by_default():
    sleeps = []
    respx.get("https://example.com/f").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "86400"}),
        httpx.Response(200, text="ok"),
    ])
    r = make(sleeps).get("https://example.com/f")
    assert r.text == "ok"
    assert 300.0 in sleeps
    assert 86400 not in sleeps


def waf_client(sleeps):
    return PoliteClient("ua", {}, base_delay=1.0, sleep=sleeps.append,
                        retry_statuses_by_host={"ezamowienia.gov.pl": {403}})


@respx.mock
def test_403_retried_on_configured_host():
    sleeps = []
    route = respx.get("https://ezamowienia.gov.pl/x").mock(side_effect=[
        httpx.Response(403),
        httpx.Response(200, text="ok"),
    ])
    r = waf_client(sleeps).get("https://ezamowienia.gov.pl/x")
    assert r.status_code == 200 and r.text == "ok"
    assert route.call_count == 2
    assert sleeps == [1.0]


@respx.mock
def test_403_on_other_host_returned_without_retry():
    sleeps = []
    route = respx.get("https://example.com/forbidden").mock(return_value=httpx.Response(403))
    r = waf_client(sleeps).get("https://example.com/forbidden")
    assert r.status_code == 403 and route.call_count == 1
    assert sleeps == []


def test_handlers_configure_ezamowienia_waf_retry():
    from tie.handlers import RATES, RETRY_STATUSES_BY_HOST

    assert RATES["ezamowienia.gov.pl"] == 1.0
    assert RATES["ezamawiajacy.pl"] == 5.0
    assert RETRY_STATUSES_BY_HOST == {"ezamowienia.gov.pl": {403}}
