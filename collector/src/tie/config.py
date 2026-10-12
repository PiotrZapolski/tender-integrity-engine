from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

USER_AGENT = "TenderIntegrityEngine/0.1 (+https://github.com/PiotrZapolski/tender-integrity-engine)"


@dataclass(frozen=True)
class Settings:
    database_url: str
    raw_dir: Path
    user_agent: str
    jev_api_key: str | None
    deepseek_api_key: str | None
    deepseek_model: str
    ai_daily_budget_usd: float
    wl_daily_cap: int
    # HMAC key for MSiG person keys (tie.identity.msig). No default value: msig_board jobs
    # refuse to run and `tie daily` does not queue them while it is unset.
    person_hmac_secret: str | None = None
    msig_daily_cap: int = 500


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    return Settings(
        database_url=e["DATABASE_URL"],
        raw_dir=Path(e.get("RAW_DIR", "/data/raw")),
        user_agent=e.get("USER_AGENT", USER_AGENT),
        jev_api_key=e.get("JEV_API_KEY") or None,
        deepseek_api_key=e.get("DEEPSEEK_API_KEY") or None,
        deepseek_model=e.get("DEEPSEEK_MODEL", "deepseek-chat"),
        ai_daily_budget_usd=float(e.get("AI_DAILY_BUDGET_USD", "5.0")),
        wl_daily_cap=int(e.get("WL_DAILY_CAP", "90")),
        person_hmac_secret=e.get("PERSON_HMAC_SECRET") or None,
        msig_daily_cap=int(e.get("MSIG_DAILY_CAP", "500")),
    )
