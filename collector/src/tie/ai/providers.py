"""AI providers behind a daily USD budget.

Every call (successful or not) writes one row into `ai_calls`. Before each call the
budget is checked against today's sum of `cost_usd`; when it is reached the call is
refused with BudgetExceeded and no HTTP request is made.
"""

from __future__ import annotations

import json
import logging
import time
from decimal import Decimal
from typing import Any

from tie.http import HttpGiveUp, PoliteClient

log = logging.getLogger(__name__)

# USD per 1M tokens (input, output)
PRICES: dict[tuple[str, str], tuple[float, float]] = {
    ("deepseek", "deepseek-chat"): (0.30, 1.20),
    ("jev", "jev-1.13.0"): (0.042, 0.0),
}


class BudgetExceeded(Exception):
    pass


def cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    price = PRICES.get((provider, model))
    if price is None:
        log.warning("no price for %s/%s, recording cost 0", provider, model)
        return 0.0
    price_in, price_out = price
    return (input_tokens / 1e6) * price_in + (output_tokens / 1e6) * price_out


class Budget:
    def __init__(self, conn, daily_usd: float):
        self.conn = conn
        self.daily_usd = daily_usd

    def spent_today(self) -> float:
        row = self.conn.execute(
            "select coalesce(sum(cost_usd), 0) as spent from ai_calls"
            " where created_at >= date_trunc('day', now())"
        ).fetchone()
        return float(row["spent"])

    def check(self) -> None:
        spent = self.spent_today()
        if spent >= self.daily_usd:
            raise BudgetExceeded(
                f"AI daily budget reached: spent {spent:.6f} USD of {self.daily_usd} USD"
            )

    def record(self, *, provider: str, model: str, task: str, input_tokens: int,
               output_tokens: int, latency_ms: int, ok: bool) -> float:
        cost = cost_usd(provider, model, input_tokens, output_tokens)
        self.conn.execute(
            "insert into ai_calls(provider, model, task, input_tokens, output_tokens,"
            " cost_usd, latency_ms, ok) values (%s, %s, %s, %s, %s, %s, %s, %s)",
            (provider, model, task, input_tokens, output_tokens,
             Decimal(f"{cost:.6f}"), latency_ms, ok),
        )
        # Commit right away so spending survives a rollback of the caller's work.
        self.conn.commit()
        return cost


def _post(client: PoliteClient, budget: Budget, *, provider: str, model: str, task: str,
          url: str, api_key: str, payload: dict) -> tuple[dict[str, Any], int]:
    """POST payload, return (parsed JSON, latency_ms). On HTTP failure records ok=False
    and raises RuntimeError. The API key never appears in the error message."""
    headers = {"Authorization": f"Bearer {api_key}"}
    start = time.monotonic()
    try:
        r = client.post_json(url, json=payload, headers=headers)
    except HttpGiveUp as e:
        latency_ms = int((time.monotonic() - start) * 1000)
        budget.record(provider=provider, model=model, task=task, input_tokens=0,
                      output_tokens=0, latency_ms=latency_ms, ok=False)
        raise RuntimeError(f"{provider} request failed: status={e.status}") from e
    latency_ms = int((time.monotonic() - start) * 1000)
    if r.status_code >= 400:
        budget.record(provider=provider, model=model, task=task, input_tokens=0,
                      output_tokens=0, latency_ms=latency_ms, ok=False)
        raise RuntimeError(f"{provider} request failed: HTTP {r.status_code}: {r.text[:500]}")
    try:
        data = r.json()
    except ValueError as e:
        budget.record(provider=provider, model=model, task=task, input_tokens=0,
                      output_tokens=0, latency_ms=latency_ms, ok=False)
        raise RuntimeError(f"{provider} returned non-JSON body") from e
    if not isinstance(data, dict):
        budget.record(provider=provider, model=model, task=task, input_tokens=0,
                      output_tokens=0, latency_ms=latency_ms, ok=False)
        raise RuntimeError(f"{provider} returned unexpected JSON body")
    return data, latency_ms


class DeepSeekText:
    """TextProvider: chat completion with a JSON object response."""

    URL = "https://api.deepseek.com/chat/completions"
    PROVIDER = "deepseek"

    def __init__(self, client: PoliteClient, api_key: str, model: str, budget: Budget):
        self.client = client
        self.api_key = api_key
        self.model = model
        self.budget = budget

    def complete_json(self, *, task: str, system: str, user: str) -> dict:
        self.budget.check()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        data, latency_ms = _post(self.client, self.budget, provider=self.PROVIDER,
                                 model=self.model, task=task, url=self.URL,
                                 api_key=self.api_key, payload=payload)
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        try:
            result = json.loads(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError) as e:
            self.budget.record(provider=self.PROVIDER, model=self.model, task=task,
                               input_tokens=input_tokens, output_tokens=output_tokens,
                               latency_ms=latency_ms, ok=False)
            raise RuntimeError(f"deepseek returned no valid JSON content: {e}") from e
        self.budget.record(provider=self.PROVIDER, model=self.model, task=task,
                           input_tokens=input_tokens, output_tokens=output_tokens,
                           latency_ms=latency_ms, ok=True)
        return result


class JevDecisions:
    """DecisionProvider: Jev by TypeSafe AI, answers typed questions about a state."""

    URL = "https://api.typesafe.ai/v1/systemone"
    MODEL = "jev-1.13.0"
    PROVIDER = "jev"

    def __init__(self, client: PoliteClient, api_key: str, budget: Budget):
        self.client = client
        self.api_key = api_key
        self.budget = budget

    def decide(self, *, task: str, state: Any, questions: dict) -> dict:
        self.budget.check()
        payload = {"model": self.MODEL, "state": state, "questions": questions}
        data, latency_ms = _post(self.client, self.budget, provider=self.PROVIDER,
                                 model=self.MODEL, task=task, url=self.URL,
                                 api_key=self.api_key, payload=payload)
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        answers = data.get("answers")
        ok = isinstance(answers, dict)
        self.budget.record(provider=self.PROVIDER, model=self.MODEL, task=task,
                           input_tokens=input_tokens, output_tokens=output_tokens,
                           latency_ms=latency_ms, ok=ok)
        if not ok:
            raise RuntimeError("jev response has no answers object")
        return answers
