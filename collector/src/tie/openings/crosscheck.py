"""Cross-check of offers read from an opening document against the BZP result notice.

Nothing from an opening document enters the analysis unless it passes this check: the offer
count, the min/max price (with detection of unit prices, where BZP holds totals) and the winner
(by NIP, or by name similarity when the document has no NIPs) must agree with BZP.

BZP computes the lowest and highest price (fields 6.2/6.3) over the non-rejected offers only,
while the opening document lists every offer. When BZP reports rejected offers and the min/max
of all offers do not match, the check accepts a scale under which the BZP min and max are both
offer prices and at most `n_rejected` offers lie outside the BZP range (`outside_range`).

Relaxed rules, each recorded in `CheckResult.notes` (seen on real documents):
- `winner_by_price`: the winner is not found by NIP or name (e.g. BZP spells it "SYLVA SP.
  ZO.O" or adds the street to the name), but exactly one offer has the BZP winner price and
  no NIP contradicts it; that offer is the winner.
- `winner_unscaled`: the scale comes from BZP min/max given net while the winner price is
  gross (scale about 1/1.23), so the winner's document price equals the BZP winner price
  without the scale.
- `bzp_no_winner`: BZP has no winner at all (cancelled lot); count and min/max must agree.
- `bzp_partial`: BZP lacks the lowest or the highest price but has the winner price; the
  count, the price bound that is present (at scale 1), the winner price among the offers and
  the winner identity are checked.
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
# The short ones are fragments of misspelled forms that normalize_name leaves split, e.g.
# "SP. ZO.O" -> "sp zo o", "Spółka z o.o." -> "spolka z o o".
_LEGAL_FORMS = {"spzoo", "sa", "spj", "spk", "ska", "spp", "sc",
                "sp", "spolka", "z", "o", "zo", "oo"}


@dataclass(frozen=True)
class LotFacts:
    n_offers: int | None
    price_min: Decimal | None
    price_max: Decimal | None
    price_winner: Decimal | None
    winner_nip: str | None
    winner_name_norm: str | None
    n_rejected: int | None = None


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    scale: Decimal  # multiplier: BZP price = document price * scale
    reasons: list[str] = field(default_factory=list)  # empty when ok
    # Per offer, when the rejected-offers rule accepted the prices: True for offers whose
    # scaled price lies outside the BZP min/max range (likely the rejected ones). None when
    # the exact rule was used.
    outside_range: list[bool] | None = None
    winner: int | None = None  # index of the BZP winner's offer, when found
    notes: list[str] = field(default_factory=list)  # relaxed rules used (module docstring)


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


def _outside(scaled: Decimal, facts: LotFacts) -> bool:
    return (scaled < facts.price_min - PRICE_TOLERANCE
            or scaled > facts.price_max + PRICE_TOLERANCE)


def _detect_scale_with_rejected(prices: list[Decimal], facts: LotFacts
                                ) -> tuple[Decimal, list[bool]] | None:
    """Rejected-offers rule: (scale, outside flags per offer), or None.

    Candidate scales: 1 first, then price_min / p for each offer price p. A scale is accepted
    when the BZP min and max both equal a scaled offer price (within PRICE_TOLERANCE) and at
    most `n_rejected` scaled offer prices lie outside [price_min, price_max].
    """
    if not facts.n_rejected or facts.n_rejected <= 0:
        return None
    candidates = [Decimal(1)]
    if facts.price_min > 0:
        candidates.extend(facts.price_min / p for p in prices if p > 0)
    seen: set[Decimal] = set()
    for k in candidates:
        if k in seen:
            continue
        seen.add(k)
        scaled = [p * k for p in prices]
        if not any(abs(s - facts.price_min) <= PRICE_TOLERANCE for s in scaled):
            continue
        if not any(abs(s - facts.price_max) <= PRICE_TOLERANCE for s in scaled):
            continue
        outside = [_outside(s, facts) for s in scaled]
        if sum(outside) <= facts.n_rejected:
            scale = k if k == 1 else k.quantize(SCALE_QUANTUM, rounding=ROUND_HALF_UP)
            return scale, outside
    return None


def _partial_prices(prices: list[Decimal], facts: LotFacts) -> list[bool] | None:
    """`bzp_partial` price check at scale 1: outside flags per offer, or None on failure.

    The BZP winner price must be an offer price; each BZP bound that is present must be an
    offer price, with at most `n_rejected` offers beyond the bounds."""
    tol = PRICE_TOLERANCE
    if not any(abs(p - facts.price_winner) <= tol for p in prices):
        return None
    for bound in (facts.price_min, facts.price_max):
        if bound is not None and not any(abs(p - bound) <= tol for p in prices):
            return None
    outside = [
        (facts.price_min is not None and p < facts.price_min - tol)
        or (facts.price_max is not None and p > facts.price_max + tol)
        for p in prices
    ]
    if sum(outside) > (facts.n_rejected or 0):
        return None
    return outside


def _price_matches(price: Decimal | None, scale: Decimal | None, facts: LotFacts) -> bool:
    """Winner price check; skipped when BZP has no winner price or the scale is unknown.

    The document price matches when, scaled or not, it is within 0.5% of the BZP winner
    price (unscaled: BZP min/max given net, the winner price gross)."""
    if facts.price_winner is None or scale is None:
        return True
    if price is None:
        return False
    limit = WINNER_PRICE_TOLERANCE * abs(facts.price_winner)
    return (abs(price * scale - facts.price_winner) <= limit
            or abs(price - facts.price_winner) <= limit)


def _has_winner(facts: LotFacts) -> bool:
    return (facts.price_winner is not None or _nip_digits(facts.winner_nip) is not None
            or bool(_tokens(facts.winner_name_norm)))


def _identity_candidates(offers: list[OfferRow], facts: LotFacts,
                         scale: Decimal | None) -> list[int]:
    """Offers that are the BZP winner by NIP (when both sides have NIPs) or else by name,
    with a matching price."""
    winner_nip = _nip_digits(facts.winner_nip)
    offer_nips = [_nip_digits(o.nip) for o in offers]
    if winner_nip and any(offer_nips):
        return [i for i, (o, nip) in enumerate(zip(offers, offer_nips, strict=True))
                if nip == winner_nip and _price_matches(o.price, scale, facts)]
    winner_tokens = _tokens(facts.winner_name_norm)
    if not winner_tokens:
        return []
    return [i for i, o in enumerate(offers)
            if _jaccard(_tokens(o.bidder_name), winner_tokens) >= NAME_SIMILARITY
            and _price_matches(o.price, scale, facts)]


def find_winner(offers: list[OfferRow], facts: LotFacts,
                scale: Decimal | None) -> tuple[int | None, list[str]]:
    """(index of the BZP winner's offer or None, notes). Several identity candidates: the one
    whose price is closest to the BZP winner price."""
    pw = facts.price_winner
    cands = _identity_candidates(offers, facts, scale)
    if cands:
        if pw is None or scale is None:
            return cands[0], []

        def gap(i: int) -> Decimal:
            p = offers[i].price
            return min(abs(p * scale - pw), abs(p - pw))

        best = min(cands, key=gap)
        price = offers[best].price
        scaled_ok = abs(price * scale - pw) <= WINNER_PRICE_TOLERANCE * abs(pw)
        return best, ([] if scaled_ok else ["winner_unscaled"])
    if pw is None or scale is None:
        return None, []
    scaled_tol = PRICE_TOLERANCE * max(Decimal(1), scale)
    exact = [i for i, o in enumerate(offers)
             if o.price is not None
             and (abs(o.price * scale - pw) <= scaled_tol or abs(o.price - pw) <= PRICE_TOLERANCE)]
    if len(exact) != 1:
        return None, []
    winner_nip = _nip_digits(facts.winner_nip)
    nip = _nip_digits(offers[exact[0]].nip)
    if winner_nip and nip and nip != winner_nip:
        return None, []
    return exact[0], ["winner_by_price"]


def cross_check(offers: list[OfferRow], facts: LotFacts) -> CheckResult:
    reasons: list[str] = []
    notes: list[str] = []

    full = facts.price_min is not None and facts.price_max is not None
    partial = not full and facts.price_winner is not None
    if facts.n_offers is None or not (full or partial):
        reasons.append("bzp_incomplete")

    if facts.n_offers is not None and len(offers) != facts.n_offers:
        reasons.append("count")

    prices = [o.price for o in offers]
    priced = bool(prices) and all(p is not None for p in prices)
    scale: Decimal | None = None
    outside_range: list[bool] | None = None
    if full:
        if not priced:
            reasons.append("prices")
        else:
            scale = _detect_scale(prices, facts)
            if scale is None:
                found = _detect_scale_with_rejected(prices, facts)
                if found is not None:
                    scale, outside_range = found
            if scale is None:
                reasons.append("prices")
    elif partial:
        notes.append("bzp_partial")
        outside = _partial_prices(prices, facts) if priced else None
        if outside is None:
            reasons.append("prices")
        else:
            scale = Decimal(1)
            outside_range = outside if any(outside) else None

    winner: int | None = None
    if _has_winner(facts):
        winner, winner_notes = find_winner(offers, facts, scale)
        if winner is None:
            reasons.append("winner")
        notes.extend(winner_notes)
    elif full and scale is not None:
        notes.append("bzp_no_winner")

    return CheckResult(ok=not reasons, scale=scale if scale is not None else Decimal(1),
                       reasons=reasons, outside_range=outside_range, winner=winner,
                       notes=notes)
