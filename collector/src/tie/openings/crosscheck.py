"""Cross-check of offers read from an opening document against the BZP result notice.

Nothing from an opening document enters the analysis unless it passes this check: the offer
count, the min/max price (with detection of unit prices, where BZP holds totals) and the winner
(by NIP, or by name similarity when the document has no NIPs) must agree with BZP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from tie.identity.names import normalize_name
from tie.openings.parse import OfferRow

PRICE_TOLERANCE = Decimal("0.01")  # absolute, PLN: document prices equal BZP prices
SCALE_TOLERANCE = Decimal("0.005")  # relative: k1 and k2 must agree within 0.5%
WINNER_PRICE_TOLERANCE = Decimal("0.005")  # relative: winner's price within 0.5% of BZP
NAME_SIMILARITY = 0.5  # token Jaccard threshold for winner name matching
SCALE_QUANTUM = Decimal("0.000001")

# Legal-form tokens produced by normalize_name. They are dropped before the Jaccard comparison
# so that "abc spzoo" and "xyz spzoo" do not look similar only because of the legal form.
_LEGAL_FORMS = {"spzoo", "sa", "spj", "spk", "ska", "spp", "sc"}


@dataclass(frozen=True)
class LotFacts:
    n_offers: int | None
    price_min: Decimal | None
    price_max: Decimal | None
    price_winner: Decimal | None
    winner_nip: str | None
    winner_name_norm: str | None


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    scale: Decimal  # multiplier: BZP price = document price * scale
    reasons: list[str] = field(default_factory=list)  # empty when ok


def _nip_digits(nip: str | None) -> str | None:
    digits = re.sub(r"\D", "", re.sub(r"(?i)^\s*PL", "", nip or ""))
    return digits or None


def _tokens(name: str | None) -> set[str]:
    tokens = set(normalize_name(name or "").split())
    core = tokens - _LEGAL_FORMS
    return core or tokens


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _detect_scale(prices: list[Decimal], facts: LotFacts) -> Decimal | None:
    """Scale such that BZP price = document price * scale, or None when prices disagree."""
    dmin, dmax = min(prices), max(prices)
    if (abs(dmin - facts.price_min) <= PRICE_TOLERANCE
            and abs(dmax - facts.price_max) <= PRICE_TOLERANCE):
        return Decimal(1)
    if dmin <= 0 or dmax <= 0 or facts.price_min <= 0 or facts.price_max <= 0:
        return None
    k1 = facts.price_min / dmin
    k2 = facts.price_max / dmax
    if abs(k1 - k2) / k1 > SCALE_TOLERANCE:
        return None
    return ((k1 + k2) / 2).quantize(SCALE_QUANTUM, rounding=ROUND_HALF_UP)


def _price_matches(price: Decimal | None, scale: Decimal | None, facts: LotFacts) -> bool:
    """Winner price check; skipped when BZP has no winner price or the scale is unknown."""
    if facts.price_winner is None or scale is None:
        return True
    if price is None:
        return False
    limit = WINNER_PRICE_TOLERANCE * abs(facts.price_winner)
    return abs(price * scale - facts.price_winner) <= limit


def _winner_ok(offers: list[OfferRow], facts: LotFacts, scale: Decimal | None) -> bool:
    winner_nip = _nip_digits(facts.winner_nip)
    offer_nips = [_nip_digits(o.nip) for o in offers]
    if winner_nip and any(offer_nips):
        return any(
            nip == winner_nip and _price_matches(o.price, scale, facts)
            for o, nip in zip(offers, offer_nips, strict=True)
        )
    winner_tokens = _tokens(facts.winner_name_norm)
    if not winner_tokens:
        return False
    return any(
        _jaccard(_tokens(o.bidder_name), winner_tokens) >= NAME_SIMILARITY
        and _price_matches(o.price, scale, facts)
        for o in offers
    )


def cross_check(offers: list[OfferRow], facts: LotFacts) -> CheckResult:
    reasons: list[str] = []

    bzp_prices = facts.price_min is not None and facts.price_max is not None
    if facts.n_offers is None or not bzp_prices:
        reasons.append("bzp_incomplete")

    if facts.n_offers is not None and len(offers) != facts.n_offers:
        reasons.append("count")

    scale: Decimal | None = None
    if bzp_prices:
        prices = [o.price for o in offers]
        if not prices or any(p is None for p in prices):
            reasons.append("prices")
        else:
            scale = _detect_scale(prices, facts)
            if scale is None:
                reasons.append("prices")

    if not _winner_ok(offers, facts, scale):
        reasons.append("winner")

    return CheckResult(ok=not reasons, scale=scale if scale is not None else Decimal(1),
                       reasons=reasons)
