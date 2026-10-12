from decimal import Decimal as D

from tie.identity.names import normalize_name
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


def test_winner_index_and_no_notes_on_plain_match():
    r = cross_check(rows(SPY), FACTS)
    assert r.winner == 7 and r.notes == []


# Real lot (document 46): BZP names the winner "SYLVA SP. ZO.O", without a NIP.
PELLET = [("SYLVA Spółka z o.o.", "164721.60"),
          ("Bio-Agro Produkt Urbański Kwiasowski Spółka Komandytowa", "174856.80"),
          ("WĘGLOPASZ Spółka z ograniczoną odpowiedzialnością", "182728.80")]


def pellet_facts(name):
    return LotFacts(3, D("164721.60"), D("182728.80"), D("164721.60"), None,
                    normalize_name(name))


def test_winner_name_with_broken_legal_form():
    r = cross_check(rows(PELLET), pellet_facts("SYLVA SP. ZO.O"))
    assert r.ok, r.reasons
    assert r.winner == 0 and r.notes == []


def test_winner_by_unique_price_when_names_differ():
    r = cross_check(rows(PELLET), pellet_facts("Zupelnie Inna Nazwa"))
    assert r.ok, r.reasons
    assert r.winner == 0 and r.notes == ["winner_by_price"]


def test_winner_price_shared_by_two_offers_is_not_enough():
    two = [PELLET[0], ("Druga Firma", "164721.60"), PELLET[2]]
    r = cross_check(rows(two), pellet_facts("Zupelnie Inna Nazwa"))
    assert not r.ok and r.reasons == ["winner"]


def test_winner_by_price_refused_on_nip_conflict():
    facts = LotFacts(3, D("164721.60"), D("182728.80"), D("164721.60"), "5932335316",
                     "inna firma")
    offers = rows(PELLET, nips={PELLET[0][0]: "5260250274", PELLET[1][0]: "7532450747"})
    r = cross_check(offers, facts)
    assert not r.ok and r.reasons == ["winner"]


def test_winner_price_unscaled_when_bzp_min_max_are_net():
    # Real lot (document 109): BZP min/max are net (gross / 1.23), the winner price is gross.
    offers = [OfferRow(1, "PTM Soft", "6792846140", D("35999.64")),
              OfferRow(2, "Vavatech sp. z o.o.", "5262845994", D("36604.80")),
              OfferRow(3, "Inna", "8992651436", D("80000.00")),
              OfferRow(4, "WebReklama", "1180183823", D("194832.00"))]
    facts = LotFacts(4, D("29268.00"), D("158400.00"), D("36604.80"), "5262845994", None)
    r = cross_check(offers, facts)
    assert r.ok, r.reasons
    assert r.winner == 1 and r.notes == ["winner_unscaled"]
    assert abs(r.scale - D("0.813")) < D("0.0001")


def test_lot_without_winner_in_bzp():
    # Real lot (document 89): cancelled procedure, BZP has offers and prices but no winner.
    offers = [OfferRow(1, "KESON", "7343636675", D("635815.63")),
              OfferRow(2, "MURINO", "6772544654", D("610505.69")),
              OfferRow(3, "SAMSON", "6762091547", D("648210.00")),
              OfferRow(4, "ZELIAS", "9930085432", D("695524.55"))]
    facts = LotFacts(4, D("610505.69"), D("695524.55"), None, None, None)
    r = cross_check(offers, facts)
    assert r.ok, r.reasons
    assert r.winner is None and r.notes == ["bzp_no_winner"]


def test_lot_without_winner_still_needs_prices():
    offers = [OfferRow(1, "A", None, D("10.00")), OfferRow(2, "B", None, D("30.00"))]
    facts = LotFacts(2, D("10.00"), D("20.00"), None, None, None)
    assert cross_check(offers, facts).reasons == ["prices"]


# Real lot (document 42): BZP has no lowest price, but the offer count, the highest price and
# the winner (price and NIP).
BAMBINO = [("TRONUS POLSKA", "5272680141", "1069345.76"), ("PHU BMS", "5811172950", "356984.31"),
           ("Zoneo", "7752667756", "189000.00"), ("ALTARE", "6572982438", "381530.85"),
           ("Moje Bambino Sp. z o.o", "7251933448", "259777.11")]


def bambino_rows():
    return [OfferRow(i + 1, n, nip, D(p)) for i, (n, nip, p) in enumerate(BAMBINO)]


def bambino_facts(**kw):
    base = dict(n_offers=5, price_min=None, price_max=D("1069345.76"),
                price_winner=D("259777.11"), winner_nip="7251933448", winner_name_norm=None,
                n_rejected=0)
    base.update(kw)
    return LotFacts(**base)


def test_bzp_partial_accepted_with_possible_checks():
    r = cross_check(bambino_rows(), bambino_facts())
    assert r.ok, r.reasons
    assert r.notes == ["bzp_partial"] and r.winner == 4 and r.scale == 1


def test_bzp_partial_only_min_and_winner():
    facts = bambino_facts(price_min=D("189000.00"), price_max=None)
    r = cross_check(bambino_rows(), facts)
    assert r.ok, r.reasons
    assert r.notes == ["bzp_partial"]


def test_bzp_partial_count_mismatch():
    r = cross_check(bambino_rows()[:4], bambino_facts())
    assert not r.ok and "count" in r.reasons


def test_bzp_partial_winner_price_not_among_offers():
    r = cross_check(bambino_rows(), bambino_facts(price_winner=D("259000.00")))
    assert not r.ok and "prices" in r.reasons


def test_bzp_partial_bound_not_among_offers():
    r = cross_check(bambino_rows(), bambino_facts(price_max=D("999999.99")))
    assert not r.ok and "prices" in r.reasons


def test_bzp_partial_offer_above_max_allowed_only_when_rejected():
    facts = bambino_facts(price_max=D("381530.85"))
    assert "prices" in cross_check(bambino_rows(), facts).reasons
    r = cross_check(bambino_rows(), bambino_facts(price_max=D("381530.85"), n_rejected=1))
    assert r.ok, r.reasons
    assert r.outside_range == [True, False, False, False, False]


def test_bzp_partial_needs_winner_price():
    facts = bambino_facts(price_winner=None)
    r = cross_check(bambino_rows(), facts)
    assert not r.ok and "bzp_incomplete" in r.reasons
