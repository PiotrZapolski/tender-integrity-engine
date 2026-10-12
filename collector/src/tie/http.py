"""Polite HTTP client: per-host rate limits, retries with exponential backoff and Retry-After."""

from __future__ import annotations

import time
from typing import Self
from urllib.parse import urlparse

import httpx

RETRY_STATUSES = {429, 500, 502, 503, 504, 529}


class HttpGiveUp(Exception):
    def __init__(self, url: str, status: int | None):
        super().__init__(f"giving up on {url} (status={status})")
        self.url = url
        self.status = status


class PoliteClient:
    """Synchronous httpx wrapper used by every collector that calls public APIs.

    `rates` maps a host to the minimal gap in seconds between two requests to it.
    Retries on RETRY_STATUSES and httpx.TransportError; other responses (including
    4xx other than 429) are returned as is. After `max_retries` retries raises HttpGiveUp.
    """

    def __init__(self, user_agent: str, rates: dict[str, float], *, max_retries: int = 5,
                 base_delay: float = 1.0, sleep=time.sleep, timeout: float = 60.0,
                 clock=time.monotonic):
        self.rates = dict(rates)
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.sleep = sleep
        self.clock = clock
        self._last: dict[str, float] = {}
        self.client = httpx.Client(headers={"User-Agent": user_agent}, timeout=timeout,
                                   follow_redirects=True)

    def _wait_turn(self, host: str) -> None:
        gap = self.rates.get(host, 0.0)
        now = self.clock()
        last = self._last.get(host)
        if last is not None and gap > 0 and now - last < gap:
            self.sleep(gap - (now - last))
        self._last[host] = self.clock()

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        host = urlparse(url).hostname or ""
        status = None
        for attempt in range(self.max_retries + 1):
            self._wait_turn(host)
            try:
                r = self.client.request(method, url, **kw)
            except httpx.TransportError:
                r = None
            if r is not None and r.status_code not in RETRY_STATUSES:
                return r
            status = None if r is None else r.status_code
            if attempt == self.max_retries:
                break
            delay = self.base_delay * 2**attempt
            if r is not None:
                retry_after = r.headers.get("Retry-After", "").strip()
                if retry_after.isdigit():
                    delay = float(retry_after)
            self.sleep(delay)
        raise HttpGiveUp(url, status)

    def get(self, url: str, **kw) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post_json(self, url: str, json: dict, headers: dict | None = None) -> httpx.Response:
        return self.request("POST", url, json=json, headers=headers or {})

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
