"""Deterministic parser of "informacja z otwarcia ofert" documents.

Works on text from `tie.openings.extract.extract_text`. Extracts the budget, the offer prices
and the bidder NIPs (without the buyer's NIP); pairs them only when the counts match. Documents
it cannot pair are left for the LLM fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from tie.identity.names import normalize_name
from tie.money import _AMOUNT, clean_nip, parse_amount

FOOTER_LINES = 8
# Non-empty lines right after the last price line still belong to the last offer's table cell
# (e.g. the bidder's NIP printed under the price), so they are never treated as the footer.
LAST_OFFER_TAIL = 2

# Same amount syntax as tie.money, but not glued to other digits, dots or slashes, so case
# numbers like "ZP.271.12.2026" and digit runs like a NIP followed by an amount do not yield
# false amounts.
_AMOUNT_ISOLATED = re.compile(r"(?<![\d./])(?:" + _AMOUNT.pattern + r")(?![.,/]\d)")
_DATE = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b")
_TIME = re.compile(r"(?i)\bgodz\w*\.?\s*\d{1,2}[.:]\d{2}")
_NIP = re.compile(
    r"\b(?:PL)?\d{3}[- ]?\d{3}[- ]?\d{2}[- ]?\d{2}\b"
    r"|\b\d{3}-\d{2}-\d{2}-\d{3}\b"
    r"|\b\d{10}\b"
)
# Matched against normalize_name(line): lowercase, no diacritics, punctuation turned to spaces.
# Short keywords use word boundaries so names like "Elitelas" or "Łódź ul." are not excluded.
_EXCLUDED = re.compile(
    r"\bkwota\b|\bkapital\b|\bregon\b|\bkrs\b|\btel\b|\btelefon\b|\bfax\b|\bbdo\b"
    r"|\bstrona \d+|\bdz u\b|\bpoz\b|termin skladania"
)
_BUDGET_NOUN = re.compile(r"\bkwot")
_BUDGET_VERB = re.compile(r"przeznacz|zamierza")


@dataclass(frozen=True)
class OfferRow:
    offer_no: int | None
    bidder_name: str  # raw text, may be empty in the deterministic parser
    nip: str | None
    price: Decimal | None


@dataclass(frozen=True)
class ParsedOpening:
    budget: Decimal | None  # "kwota przeznaczona na realizację zamówienia"
    prices: list[Decimal] = field(default_factory=list)  # all offer prices in order
    nips: list[str] = field(default_factory=list)  # bidder NIPs in order, without the buyer's
    offers: list[OfferRow] = field(default_factory=list)  # only when len(nips) == len(prices)
    method: str = "deterministic"


def _amounts(line: str) -> list[Decimal]:
    """Amounts in a line, ignoring dates and clock times."""
    line = _TIME.sub(" ", _DATE.sub(" ", line))
    out: list[Decimal] = []
    for m in _AMOUNT_ISOLATED.finditer(line):
        value = parse_amount(m.group(0))
        if value is not None:
            out.append(value)
    return out


def _nips_in(line: str) -> list[str]:
    out: list[str] = []
    for m in _NIP.finditer(line):
        nip = clean_nip(m.group(0))
        if nip:
            out.append(nip)
    return out


def _is_budget_line(norm: str) -> bool:
    return bool(_BUDGET_NOUN.search(norm) and _BUDGET_VERB.search(norm))


def _find_budget(lines: list[str], norms: list[str]) -> tuple[Decimal | None, set[int]]:
    """First amount on the first budget line; if the line has none, the next non-empty line."""
    budget_lines: set[int] = set()
    for i, norm in enumerate(norms):
        if not _is_budget_line(norm):
            continue
        budget_lines.add(i)
        amounts = _amounts(lines[i])
        if amounts:
            return amounts[0], budget_lines
        nxt = next((j for j in range(i + 1, len(lines)) if lines[j].strip()), None)
        if nxt is not None:
            amounts = _amounts(lines[nxt])
            if amounts:
                budget_lines.add(nxt)
                return amounts[0], budget_lines
    return None, budget_lines


def parse_opening(text: str, *, buyer_nip: str | None = None) -> ParsedOpening:
    lines = (text or "").splitlines()
    norms = [normalize_name(line) for line in lines]
    budget, budget_lines = _find_budget(lines, norms)

    prices: list[Decimal] = []
    nip_hits: list[str] = []
    last_price_line = -1
    for i, line in enumerate(lines):
        if i in budget_lines or _EXCLUDED.search(norms[i]):
            continue
        nip_hits.extend(_nips_in(line))
        amounts = _amounts(line)
        if amounts:
            prices.append(amounts[-1])
            last_price_line = i

    non_empty = [i for i, line in enumerate(lines) if line.strip()]
    after_last_price = [i for i in non_empty if i > last_price_line][LAST_OFFER_TAIL:]
    footer = set(non_empty[-FOOTER_LINES:]) & set(after_last_price)
    skip = {nip for i in footer for nip in _nips_in(lines[i])}
    buyer = clean_nip(buyer_nip) if buyer_nip else None
    if buyer:
        skip.add(buyer)

    nips: list[str] = []
    for nip in nip_hits:
        if nip in skip or (nips and nips[-1] == nip):
            continue
        nips.append(nip)

    offers: list[OfferRow] = []
    if nips and len(nips) == len(prices):
        offers = [
            OfferRow(offer_no=i + 1, bidder_name="", nip=nip, price=price)
            for i, (nip, price) in enumerate(zip(nips, prices, strict=True))
        ]
    return ParsedOpening(budget=budget, prices=prices, nips=nips, offers=offers)
