from pathlib import Path

import pytest

from tie.config import load_settings


def test_defaults():
    s = load_settings({"DATABASE_URL": "postgresql://u:p@h/db"})
    assert s.database_url == "postgresql://u:p@h/db"
    assert s.raw_dir == Path("/data/raw")
    assert s.user_agent.startswith("TenderIntegrityEngine/0.1")
    assert s.jev_api_key is None
    assert s.deepseek_model == "deepseek-chat"
    assert s.ai_daily_budget_usd == 5.0
    assert s.wl_daily_cap == 90


def test_overrides():
    s = load_settings({
        "DATABASE_URL": "x", "RAW_DIR": "/tmp/r", "AI_DAILY_BUDGET_USD": "1.5",
        "JEV_API_KEY": "k", "WL_DAILY_CAP": "10",
    })
    assert s.raw_dir == Path("/tmp/r")
    assert s.ai_daily_budget_usd == 1.5
    assert s.jev_api_key == "k"
    assert s.wl_daily_cap == 10


def test_missing_database_url_raises():
    with pytest.raises(KeyError):
        load_settings({})
