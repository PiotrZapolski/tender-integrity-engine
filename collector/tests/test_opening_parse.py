from decimal import Decimal as D

from tie.openings.extract import extract_text
from tie.openings.parse import OfferRow, parse_opening


def text(fixtures, name):
    return extract_text((fixtures / "openings" / name).read_bytes(), name).text


def test_kolbudy_single_with_nip(fixtures):
    p = parse_opening(text(fixtures, "kolbudy_text.pdf"))
    assert p.budget == D("380000.00")
    assert p.prices == [D("213192.00")]
    assert p.nips == ["5932335316"]
    assert len(p.offers) == 1 and p.offers[0].nip == "5932335316"


def test_spytkowo_unit_prices_no_bidder_nips(fixtures):
    p = parse_opening(text(fixtures, "spytkowo_unit_prices.pdf"), buyer_nip="8451958301")
    assert p.prices == [D("45.63"), D("40.18"), D("49.02"), D("44.78"), D("39.85"),
                        D("43.42"), D("41.82"), D("38.93")]
    assert "8451958301" not in p.nips
    assert p.nips == []
    assert p.offers == []


def test_rogow_docx(fixtures):
    p = parse_opening(text(fixtures, "rogow_docx.docx"))
    assert p.budget == D("373000.00")
    assert p.prices == [D("366120.00"), D("525960.00"), D("499256.24"), D("770901.55")]
    assert p.offers == []


SYNTHETIC = """Testowo, dnia 01.10.2026 r.
INFORMACJA Z OTWARCIA OFERT
Otwarcie ofert nastapilo w dniu 1.10.2026 r. o godz. 10.15
Znak sprawy: ZP.271.12.2026
Zamawiajacy zamierza przeznaczyc na sfinansowanie zamowienia kwote:
150 000,00 zl brutto
1. Hotel Alfa Sp. z o.o., 90-001 Łódź ul. Piotrkowska 1, NIP 123-456-32-18, cena 120 000,00 zl
2. Beta Sp. j., NIP: PL5260250274
   NIP 5260250274, cena: 99 999,99 zl
Termin składania ofert: 30.09.2026 r.



Urząd Gminy Testowo, ul. Główna 1, 00-001 Testowo
NIP 5932335316
"""


def test_synthetic_pairs_and_excludes_footer_nip():
    p = parse_opening(SYNTHETIC)
    assert p.method == "deterministic"
    assert p.budget == D("150000.00")
    assert p.prices == [D("120000.00"), D("99999.99")]
    assert p.nips == ["1234563218", "5260250274"]
    assert p.offers == [
        OfferRow(offer_no=1, bidder_name="", nip="1234563218", price=D("120000.00")),
        OfferRow(offer_no=2, bidder_name="", nip="5260250274", price=D("99999.99")),
    ]


def test_buyer_nip_excluded_and_unpaired_counts_give_no_offers():
    doc = (
        "Zamawiajacy: Gmina Iksowo, NIP 845-195-83-01\n"
        "Kwota przeznaczona na realizacje zamowienia: 50 000,00 zl\n"
        "Oferta 1 Firma A NIP 1234563218 45 000,00 zl\n"
        "Oferta 2 Nowak Jan 48 000,00 zl\n"
    )
    p = parse_opening(doc, buyer_nip="PL8451958301")
    assert p.budget == D("50000.00")
    assert p.prices == [D("45000.00"), D("48000.00")]
    assert p.nips == ["1234563218"]
    assert p.offers == []


def test_empty_text():
    p = parse_opening("")
    assert p.budget is None
    assert p.prices == [] and p.nips == [] and p.offers == []
    assert p.method == "deterministic"
