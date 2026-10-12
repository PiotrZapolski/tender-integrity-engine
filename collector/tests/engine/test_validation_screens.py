"""Validation test A: engine screens on gold tenders against the background of our data."""

from decimal import Decimal

import pytest

from tie.engine.tests_lot import identical_prices, relative_distance
from tie.validation.checks import screen_known_cartels
from tie.validation.gold import case_from_payload, public_firm_label, same_firm, sector_cpv
from tie.validation.screens import (
    Backgrounds,
    Offer,
    Tender,
    lot_screens,
    market_levels,
    pair_screens,
    percentile_in,
)

from .synth import AS_OF, make_market

VALID_NIP = "1234567883"


def _tender(key, prices, bidders, winner=0):
    return Tender(key, [Offer(b, Decimal(str(p)), i == winner)
                        for i, (b, p) in enumerate(zip(bidders, prices, strict=True))])


def test_percentile_counts_ties_against_the_gold_value():
    ordered = [float(i) for i in range(20)]
    assert percentile_in(19.5, ordered, True) == 1.0
    assert percentile_in(19.0, ordered, True) == 0.95
    assert percentile_in(-1.0, ordered, False) == 1.0
    assert percentile_in(0.0, ordered, False) == 0.95
    binary = [0.0] * 18 + [1.0] * 2
    assert percentile_in(1.0, binary, True) == 0.9
    assert percentile_in(1.0, [0.0] * 9, True) is None


def test_lot_screens_reuse_engine_arithmetic():
    prices = [Decimal(100000), Decimal(130000), Decimal(130500), Decimal(131000)]
    out = lot_screens(_tender(1, prices, [1, 2, 3, 4]))
    assert out["3.3.7"] == relative_distance(prices)[0] == pytest.approx(60.0)
    assert out["3.3.5"] == identical_prices(prices)[0] == 0.0
    dup = lot_screens(_tender(2, [100, 100, 120], [1, 2, 3]))
    assert dup["3.3.5"] == 1.0
    two = lot_screens(_tender(3, [100, 120], [1, 2]))
    assert "3.3.7" not in two and two["3.3.5"] == 0.0
    # Offers outside the BZP range and without a price are not price observations.
    t = _tender(4, [100, 130, 131], [1, 2, 3])
    t.offers[2].valid = False
    t.offers.append(Offer(5, None))
    assert "3.3.7" not in lot_screens(t)


def test_pair_screens_rotation_and_price_pattern():
    tenders = []
    for i in range(6):
        w = 100000 + 5000 * i
        winner = 1 if i % 2 == 0 else 2
        loser = 2 if winner == 1 else 1
        tenders.append(_tender(i, [w, round(w * 1.3, 2)], [winner, loser]))
    tenders.append(_tender(6, [90000, 95000], [1, 3]))
    out = pair_screens(tenders)
    assert set(out) == {(1, 2)}
    # 1 and 2 met 6 times: 3 wins each and the 7th tender without 2 does not count.
    assert out[(1, 2)]["3.1.6"] == pytest.approx(1.0)
    assert out[(1, 2)]["3.3.6"] == pytest.approx(0.0, abs=1e-6)


def test_market_levels_fine_to_coarse():
    assert market_levels("60130", "PL06") == [
        ("cpv5_prov", "60130", "PL06"), ("cpv3_prov", "601", "PL06"), ("cpv3_pl", "601", None),
        ("cpv2_prov", "60", "PL06"), ("cpv2_pl", "60", None)]
    assert market_levels("45", None) == [("cpv2_pl", "45", None)]
    assert market_levels("", "PL06") == []


def test_sector_mapping_and_public_names():
    assert sector_cpv("dowóz uczniów") == "60130"
    assert sector_cpv("przewóz osób niepełnosprawnych") == "60130"
    assert sector_cpv("remonty i budowy") == "45"
    assert sector_cpv("wycinka drzew") == "77211"
    assert sector_cpv("usługi kosmiczne") is None

    assert public_firm_label("ALFA-BUD  Sp. z o.o.", None) == "ALFA-BUD Sp. z o.o."
    assert public_firm_label("Usługi Transportowe Jan Kowalski", VALID_NIP) == f"NIP {VALID_NIP}"
    assert public_firm_label("Usługi Transportowe Jan Kowalski", None) is None
    assert public_firm_label("Kowalski i Wspólnicy sp.j.", None) is None
    assert public_firm_label("Anna Nowak s.c.", VALID_NIP) == f"NIP {VALID_NIP}"
    assert public_firm_label("Nowak sp. z o.o.", VALID_NIP, is_jdg=True) == f"NIP {VALID_NIP}"

    assert same_firm("puh mateusz lal", "mateusz lal puh")
    assert not same_firm("uslugi transportowe lal mieczyslaw", "puh mateusz lal")


def _gold_payload(decision="TEST-1/2026", sector="dowóz uczniów", region="lubelskie"):
    colluders = [{"name": "Alfa Trans sp. z o.o.", "nip": None},
                 {"name": "Beta Bus sp. z o.o.", "nip": None}]
    tenders = []
    for i in range(6):
        w = 100000 + 5000 * i
        alfa_wins = i % 2 == 0
        tenders.append({
            "buyer": f"Gmina {i}", "subject": f"Dowóz {i}", "date": None, "year": 2020,
            "rigged": True,
            "bids": [
                {"bidder": "Alfa Trans sp. z o.o.", "price": w if alfa_wins else round(w * 1.3, 2),
                 "won": alfa_wins},
                {"bidder": "BETA BUS Sp. z o.o.", "price": round(w * 1.3, 2) if alfa_wins else w,
                 "won": not alfa_wins},
                {"bidder": "Gamma Kom sp. z o.o.", "price": round(w * 1.305, 2), "won": False},
                {"bidder": "Delta sp. z o.o.", "price": round(w * 1.31, 2), "won": False},
            ],
        })
    # Three competitive control tenders: a fourth one won by the same firm would already be
    # the 3.3.6 pattern (constant loser/winner ratio) for its pairs.
    for i in range(3):
        tenders.append({
            "buyer": f"Gmina K{i}", "subject": f"Dowóz K{i}", "rigged": False,
            "bids": [{"bidder": "Epsilon sp. z o.o.", "price": 100000, "won": True},
                     {"bidder": "Zeta sp. z o.o.", "price": 104000, "won": False},
                     {"bidder": "Eta sp. z o.o.", "price": 109000, "won": False},
                     {"bidder": "Theta sp. z o.o.", "price": 115000, "won": False}],
        })
    tenders.append({"buyer": "Gmina S", "subject": "Jedna oferta", "rigged": False,
                    "bids": [{"bidder": "Epsilon sp. z o.o.", "price": 50000, "won": True}]})
    return {"decision": decision, "year": 2026, "colluders": colluders, "tenders": tenders,
            "pattern": ["cover_bids", "rotation"], "notes": "",
            "source": {"sector": sector, "region": region}}


def test_known_cartel_lands_in_top_five_percent_of_its_market(db):
    make_market(db, seed=7, n_firms=14, n_lots=120, cpv="60130000-8", province_code="PL06")
    cases = [
        case_from_payload(_gold_payload()),
        case_from_payload(_gold_payload("TEST-2/2026", sector="usługi kosmiczne")),
        case_from_payload(_gold_payload("TEST-3/2026", sector="usługi kurierskie")),
    ]
    metrics, per_case = screen_known_cartels(cases, Backgrounds(db, AS_OF))

    case = per_case["TEST-1/2026"]
    assert case["detected"] is True and case["reason"] is None
    assert case["market"] == {"level": "cpv5_prov", "cpv_group": "60130", "province": "PL06"}
    assert case["tenders"] == {"n": 6, "evaluable": 6, "top5": 6, "share": 1.0}
    assert case["pairs"]["n"] == 1 and case["pairs"]["top5"] == 1
    assert {"3.3.7", "3.3.6"} <= set(case["oecd_items"])
    assert case["percentile"] >= 0.95
    assert case["control_tenders"]["evaluable"] == 3 and case["control_tenders"]["top5"] == 0
    assert case["control_pairs"]["top5"] == 0

    assert per_case["TEST-2/2026"]["detected"] is None
    assert per_case["TEST-2/2026"]["reason"] == "no_market_for_sector"
    assert per_case["TEST-3/2026"]["detected"] is None
    assert per_case["TEST-3/2026"]["reason"] == "background_too_small"

    assert metrics["cases"] == 3 and metrics["cases_evaluable"] == 1
    assert metrics["cases_detected"] == 1
    assert metrics["rigged_units"]["share"] == 1.0
    assert metrics["control_tenders"]["share"] == 0.0
    assert metrics["items"]["3.3.7"]["rigged"]["share"] == 1.0
