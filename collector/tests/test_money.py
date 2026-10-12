from decimal import Decimal

from tie.money import clean_nip, parse_amount, valid_nip


def test_amounts():
    assert parse_amount("32997,39 PLN") == Decimal("32997.39")
    assert parse_amount("213 192,00 zł") == Decimal("213192.00")
    assert parse_amount("213 192,00 zł") == Decimal("213192.00")
    assert parse_amount("1.162.074,00") == Decimal("1162074.00")
    assert parse_amount("45,63") == Decimal("45.63")
    assert parse_amount("2026") is None
    assert parse_amount("brak") is None


def test_nip():
    assert valid_nip("6791021905")
    assert valid_nip("5932335316")
    assert not valid_nip("6791021906")
    assert clean_nip("PL 679-102-19-05") == "6791021905"
    assert clean_nip("599136236") is None
