from decimal import Decimal as D

from tie.openings.crosscheck import LotFacts, cross_check
from tie.openings.parse import OfferRow

SPY = [("MONSTER GROUP SP Z O.O.", "45.63"), ("GS 5 Sp. z o.o.", "40.18"),
       ("STEKOP S.A.", "49.02"), ("Spółdzielnia WULKAN", "44.78"),
       ("Neptun Security Sp. z o.o.", "39.85"), ("GrupaAlfa Sp. z o.o. Sp. k.", "43.42"),
       ("MICROS GROUP Sp. z o.o.", "41.82"), ("MULTISERVICE Sp. z o.o.", "38.93")]


def rows(pairs, nips=None):
    return [OfferRow(i + 1, n, (nips or {}).get(n), D(p)) for i, (n, p) in enumerate(pairs)]


FACTS = LotFacts(8, D("239497.36"), D("301571.04"), D("239497.36"), "6791021905",
                 "multiservice spzoo")


def test_unit_prices_scale_detected():
    r = cross_check(rows(SPY), FACTS)
    assert r.ok, r.reasons
    assert r.scale == D("6152.000000")


def test_count_mismatch():
    r = cross_check(rows(SPY[:7]), FACTS)
    assert not r.ok and "count" in r.reasons


def test_winner_by_nip():
    facts = LotFacts(1, D("213192.00"), D("213192.00"), D("213192.00"), "5932335316", None)
    r = cross_check([OfferRow(1, "", "5932335316", D("213192.00"))], facts)
    assert r.ok and r.scale == 1


def test_wrong_winner_nip():
    facts = LotFacts(1, D("213192.00"), D("213192.00"), D("213192.00"), "6791021905", None)
    r = cross_check([OfferRow(1, "", "5932335316", D("213192.00"))], facts)
    assert not r.ok and "winner" in r.reasons


def test_inconsistent_prices():
    bad = rows([(n, p) for n, p in SPY[:-1]] + [("MULTISERVICE Sp. z o.o.", "30.00")])
    assert "prices" in cross_check(bad, FACTS).reasons
