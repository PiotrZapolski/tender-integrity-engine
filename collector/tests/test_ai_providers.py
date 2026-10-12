import json

import httpx
import pytest
import respx

from tie.ai.providers import Budget, BudgetExceeded, DeepSeekText, JevDecisions
from tie.http import PoliteClient


def client():
    return PoliteClient("ua", {}, sleep=lambda s: None)


@respx.mock
def test_deepseek_json_and_cost(db):
    respx.post(DeepSeekText.URL).mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": '{"offers": []}'}}],
        "usage": {"prompt_tokens": 1000000, "completion_tokens": 0}}))
    ds = DeepSeekText(client(), "k", "deepseek-chat", Budget(db, 5.0))
    assert ds.complete_json(task="t", system="s", user="u") == {"offers": []}
    row = db.execute("select * from ai_calls").fetchone()
    assert float(row["cost_usd"]) == pytest.approx(0.30) and row["ok"] is True


@respx.mock
def test_budget_blocks(db):
    b = Budget(db, 0.10)
    b.record(provider="deepseek", model="deepseek-chat", task="t", input_tokens=1000000,
             output_tokens=0, latency_ms=1, ok=True)
    with pytest.raises(BudgetExceeded):
        DeepSeekText(client(), "k", "deepseek-chat", b).complete_json(
            task="t", system="s", user="u")


@respx.mock
def test_jev_decide(db):
    route = respx.post(JevDecisions.URL).mock(return_value=httpx.Response(200, json={
        "model": "jev-1.13.0", "answers": {"same": {"type": "noul", "noul": 0.92}},
        "usage": {"input_tokens": 441, "output_tokens": 57}}))
    j = JevDecisions(client(), "k", Budget(db, 5.0))
    ans = j.decide(task="identity", state={"a": "x"},
                   questions={"same": {"type": "noul", "instructions": "?"}})
    assert ans["same"]["noul"] == 0.92
    body = route.calls.last.request.read().decode()
    assert '"model":"jev-1.13.0"' in body.replace(" ", "")
    assert route.calls.last.request.headers["Authorization"] == "Bearer k"


@respx.mock
def test_deepseek_request_shape(db):
    route = respx.post(DeepSeekText.URL).mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": '{"a": 1}'}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}))
    DeepSeekText(client(), "k", "deepseek-chat", Budget(db, 5.0)).complete_json(
        task="t", system="sys", user="usr")
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer k"
    body = json.loads(req.read())
    assert body["model"] == "deepseek-chat"
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0
    assert body["messages"] == [{"role": "system", "content": "sys"},
                                {"role": "user", "content": "usr"}]


@respx.mock
def test_jev_cost_recorded(db):
    respx.post(JevDecisions.URL).mock(return_value=httpx.Response(200, json={
        "model": "jev-1.13.0", "answers": {}, "usage": {"input_tokens": 2000000,
                                                         "output_tokens": 100}}))
    JevDecisions(client(), "k", Budget(db, 5.0)).decide(task="x", state={}, questions={})
    row = db.execute("select * from ai_calls").fetchone()
    assert row["provider"] == "jev" and row["model"] == "jev-1.13.0" and row["task"] == "x"
    assert row["input_tokens"] == 2000000 and row["output_tokens"] == 100
    assert float(row["cost_usd"]) == pytest.approx(0.084)


@respx.mock
def test_http_error_recorded_and_raised(db):
    respx.post(JevDecisions.URL).mock(return_value=httpx.Response(401, json={"error": "no"}))
    with pytest.raises(RuntimeError):
        JevDecisions(client(), "k", Budget(db, 5.0)).decide(task="x", state={}, questions={})
    row = db.execute("select * from ai_calls").fetchone()
    assert row["ok"] is False and float(row["cost_usd"]) == 0


@respx.mock
def test_give_up_recorded_and_raised(db):
    respx.post(DeepSeekText.URL).mock(return_value=httpx.Response(503))
    c = PoliteClient("ua", {}, sleep=lambda s: None, max_retries=1)
    with pytest.raises(RuntimeError):
        DeepSeekText(c, "k", "deepseek-chat", Budget(db, 5.0)).complete_json(
            task="t", system="s", user="u")
    row = db.execute("select * from ai_calls").fetchone()
    assert row["ok"] is False


def test_unknown_model_costs_zero(db):
    cost = Budget(db, 5.0).record(provider="x", model="y", task="t", input_tokens=10**6,
                                  output_tokens=10**6, latency_ms=1, ok=True)
    assert cost == 0.0
    row = db.execute("select * from ai_calls").fetchone()
    assert float(row["cost_usd"]) == 0
