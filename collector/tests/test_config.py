from pathlib import Path

import pytest

from tie.config import engine_max_status, engine_min_lots, load_settings


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


def test_engine_max_status_defaults_to_review_and_validates():
    assert load_settings({"DATABASE_URL": "x"}).engine_max_status == "review"
    assert engine_max_status({}) == "review"
    assert engine_max_status({"ENGINE_MAX_STATUS": ""}) == "review"
    assert engine_max_status({"ENGINE_MAX_STATUS": " Published "}) == "published"
    assert load_settings({"DATABASE_URL": "x", "ENGINE_MAX_STATUS": "hidden"}
                         ).engine_max_status == "hidden"
    with pytest.raises(ValueError):
        engine_max_status({"ENGINE_MAX_STATUS": "public"})


def test_engine_min_lots_defaults_to_8_and_validates():
    assert load_settings({"DATABASE_URL": "x"}).engine_min_lots == 8
    assert engine_min_lots({}) == 8
    assert engine_min_lots({"ENGINE_MIN_LOTS": ""}) == 8
    assert engine_min_lots({"ENGINE_MIN_LOTS": " 12 "}) == 12
    assert load_settings({"DATABASE_URL": "x", "ENGINE_MIN_LOTS": "3"}).engine_min_lots == 3
    with pytest.raises(ValueError):
        engine_min_lots({"ENGINE_MIN_LOTS": "many"})
    with pytest.raises(ValueError):
        engine_min_lots({"ENGINE_MIN_LOTS": "-1"})
