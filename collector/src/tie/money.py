from __future__ import annotations

import re
from decimal import Decimal

_AMOUNT = re.compile(r"(\d{1,3}(?:[  .]\d{3})+|\d+)([,.]\d{2})(?!\d)")
_WEIGHTS = (6, 5, 7, 2, 3, 4, 5, 6, 7)


def parse_amount(text: str) -> Decimal | None:
    m = _AMOUNT.search(text or "")
    if not m:
        return None
    whole = re.sub(r"[  .]", "", m.group(1))
    return Decimal(f"{whole}.{m.group(2)[1:]}")


def valid_nip(nip: str) -> bool:
    if not re.fullmatch(r"\d{10}", nip or ""):
        return False
    s = sum(int(d) * w for d, w in zip(nip[:9], _WEIGHTS, strict=True)) % 11
    return s != 10 and s == int(nip[9])


def clean_nip(text: str) -> str | None:
    digits = re.sub(r"\D", "", re.sub(r"(?i)^\s*PL", "", text or ""))
    return digits if valid_nip(digits) else None
