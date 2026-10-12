import json
from datetime import date
from decimal import Decimal

from tie.bzp.parser import parse_result_notice


def load(fixtures, name):
    return json.loads((fixtures / "bzp" / name).read_text(encoding="utf-8"))


def test_multilot(fixtures):
    n = parse_result_notice(load(fixtures, "result_multilot.json"))
    assert n.bzp_number == "2026/BZP 00464943"
    assert n.ocds_id == "ocds-148610-a4d0d5c2-66fb-47a0-ab8f-3f89c485600e"
    assert n.buyer_nip == "6751000311"
    assert n.buyer_regon == "350768365"
    assert n.platform == "ezamawiajacy"
    assert n.platform_url == "https://zbkkrakow.ezamawiajacy.pl/"
    assert n.order_type == "Works"
    assert "45453000-7" in n.cpv_all
    assert len(n.lots) == 6
    l1 = n.lots[0]
    assert l1.lot_no == 1 and l1.outcome == "umowa"
    assert l1.n_offers == 4 and l1.n_rejected == 0
    assert l1.price_min == Decimal("32997.39") and l1.price_max == Decimal("39308.97")
    assert l1.price_winner == Decimal("32997.39")
    assert l1.contract_date == date(2026, 9, 30)
    assert l1.estimated_value == Decimal("31494.02")
    assert l1.cpv == "45453000-7"
    assert l1.winner.nip == "6821618773" and l1.winner.name == "PRO-REM Łukasz Solarz"
    assert l1.winner.consortium is False
    l4 = n.lots[3]
    assert l4.n_offers == 8 and l4.winner.nip == "6762554220"


def test_single_on_ezamowienia(fixtures):
    n = parse_result_notice(load(fixtures, "result_single.json"))
    assert n.platform == "ezamowienia"
    assert n.ocds_id == "ocds-148610-31180df3-a1c9-47cf-8061-b35e3d3d4483"
    assert len(n.lots) == 1
    assert n.lots[0].n_offers == 1
    assert n.lots[0].price_min == Decimal("499998.96")
    assert n.lots[0].winner.name.startswith("GE Medical Systems Polska")
    assert n.lots[0].cpv == "33112340-3"


def test_cancelled(fixtures):
    n = parse_result_notice(load(fixtures, "result_cancelled.json"))
    assert all(lot.outcome == "uniewaznienie" for lot in n.lots)
    assert all(lot.winner is None for lot in n.lots)


def test_consortium(fixtures):
    n = parse_result_notice(load(fixtures, "result_consortium.json"))
    w = n.lots[0].winner
    assert w.consortium is True and w.nip == "8581157491"
    assert w.name.startswith("Anka-T FHU")


def test_spytkowo(fixtures):
    n = parse_result_notice(load(fixtures, "result_spytkowo.json"))
    lot = n.lots[0]
    assert lot.n_offers == 8
    assert lot.price_min == Decimal("239497.36") and lot.price_max == Decimal("301571.04")
    assert lot.winner.nip == "6791021905"
