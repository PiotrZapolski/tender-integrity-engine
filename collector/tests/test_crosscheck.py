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


def test_bzp_incomplete():
    facts = LotFacts(None, None, D("301571.04"), None, None, "multiservice spzoo")
    r = cross_check(rows(SPY), facts)
    assert not r.ok and "bzp_incomplete" in r.reasons


def test_winner_name_mismatch():
    facts = LotFacts(8, D("239497.36"), D("301571.04"), D("239497.36"), None, "inna firma spzoo")
    r = cross_check(rows(SPY), facts)
    assert not r.ok and r.reasons == ["winner"]


# Real lot: BZP counts 5 offers, 2 rejected, and gives min/max over the non-rejected ones.
REJ_PRICES = ["633627.10", "89999.10", "184500.00", "68809.78", "120552.30"]
REJ_NIPS = ["5272680141", "7532450747", "7471927438", "8982252580", "1182286740"]


def rejected_rows():
    return [OfferRow(i + 1, "", nip, D(p))
            for i, (nip, p) in enumerate(zip(REJ_NIPS, REJ_PRICES, strict=True))]


def rejected_facts(n_rejected):
    return LotFacts(5, D("89999.10"), D("184500.00"), D("89999.10"), "7532450747", None,
                    n_rejected=n_rejected)


def test_rejected_offers_outside_bzp_range_accepted():
    r = cross_check(rejected_rows(), rejected_facts(2))
    assert r.ok, r.reasons
    assert r.scale == 1
    assert r.outside_range == [True, False, False, True, False]


def test_more_offers_outside_range_than_rejected():
    r = cross_check(rejected_rows(), rejected_facts(1))
    assert not r.ok and r.reasons == ["prices"]


def test_rejected_rule_not_used_without_rejected_offers():
    assert cross_check(rejected_rows(), rejected_facts(None)).reasons == ["prices"]
    assert cross_check(rejected_rows(), rejected_facts(0)).reasons == ["prices"]


def test_rejected_rule_with_unit_prices():
    # Unit prices; the cheapest offer (rejected) is below the BZP range after scaling by 100.
    offers = [OfferRow(1, "", "7532450747", D("900.00")), OfferRow(2, "", None, D("1200.00")),
              OfferRow(3, "", None, D("500.00"))]
    facts = LotFacts(3, D("90000.00"), D("120000.00"), D("90000.00"), "7532450747", None,
                     n_rejected=1)
    r = cross_check(offers, facts)
    assert r.ok, r.reasons
    assert r.scale == 100
    assert r.outside_range == [False, False, True]


def test_exact_rule_marks_nothing_outside():
    assert cross_check(rows(SPY), FACTS).outside_range is None
