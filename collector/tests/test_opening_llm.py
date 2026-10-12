import io
import json
from decimal import Decimal as D

from docx import Document

from tie.bzp.parser import parse_result_notice
from tie.bzp.store import upsert_bidder, upsert_notice
from tie.openings.llm import SYSTEM_PROMPT, LlmOpening, extract_with_llm, slice_offers_text
from tie.openings.store import process_opening_document
from tie.rawstore import RawStore

SPY_OCDS = "ocds-148610-05afc816-7c33-4416-8a4f-8a804d3d4b32"
KOL_OCDS = "ocds-148610-0446493b-6b68-4fa8-aa08-45d7d62c808c"

SPY_NAMES = ["MONSTER GROUP SP Z O.O.", "GS 5 Sp. z o.o.", "STEKOP S.A.",
             "Spółdzielnia WULKAN w Olsztynie", "Neptun Security Sp. z o.o.",
             "GrupaAlfa Sp. z o.o. Sp. k.", "MICROS GROUP Sp. z o.o.", "MULTISERVICE Sp. z o.o."]
SPY_PRICES = ["45.63", "40.18", "49.02", "44.78", "39.85", "43.42", "41.82", "38.93"]


class FakeProvider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.last = None

    def complete_json(self, *, task, system, user):
        self.calls += 1
        self.last = {"task": task, "system": system, "user": user}
        return self.payload


def insert_doc(db, rs, data, file_name, ocds):
    sha = rs.put(data, source="ezam")
    row = db.execute(
        "insert into documents(procedure_id, source, source_doc_id, file_name, doc_type,"
        " raw_sha256, status) values (%s,'ezamowienia',%s,%s,'opening',%s,'downloaded')"
        " returning id", (ocds, ocds + "_9", file_name, sha)).fetchone()
    db.commit()
    return row["id"]


def setup_doc(db, fixtures, tmp_path, notice_file, pdf_file, ocds):
    rec = json.loads((fixtures / "bzp" / notice_file).read_text("utf-8"))
    upsert_notice(db, parse_result_notice(rec), "sha")
    rs = RawStore(tmp_path)
    doc_id = insert_doc(db, rs, (fixtures / "openings" / pdf_file).read_bytes(), pdf_file, ocds)
    return rs, doc_id


def spy_offers(lot_no=None):
    return [{"offer_no": i + 1, "lot_no": lot_no, "bidder_name": n, "nip": None,
             "price": float(p), "price_basis": "unit"}
            for i, (n, p) in enumerate(zip(SPY_NAMES, SPY_PRICES))]


def doc_row(db, doc_id):
    return db.execute("select * from documents where id=%s", (doc_id,)).fetchone()


def docx_bytes(lines):
    doc = Document()
    for line in lines:
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_slice():
    t = "Nagłówek\nKwota 1\nWykonawca/siedziba Cena\n1 Firma 10,00"
    assert slice_offers_text(t).startswith("Wykonawca/siedziba")


def test_slice_without_marker_and_limit():
    assert slice_offers_text("abc\ndef") == "abc\ndef"
    assert len(slice_offers_text("Oferent " + "x" * 10000)) == 6000
    assert slice_offers_text("Wstęp\nOTWARTO oferty\nreszta", max_chars=10) == "OTWARTO of"


def test_extract_with_llm_validates_and_cleans_nip():
    fake = FakeProvider({"budget": "380000.00", "offers": [
        {"offer_no": 1, "lot_no": None, "bidder_name": " Alfa Sp. z o.o. ",
         "nip": "PL 593-233-53-16", "price": 213192.1, "price_basis": "total"},
        {"offer_no": "2", "lot_no": 1, "bidder_name": "Beta", "nip": "1234567890",
         "price": "38 930,50 zł", "price_basis": "UNIT"},
        {"offer_no": None, "lot_no": None, "bidder_name": "Gamma", "nip": None,
         "price": None, "price_basis": "per month"}]})
    out = extract_with_llm(fake, "Nagłówek\nWykonawca Cena\n1 Alfa 213 192,10")
    assert isinstance(out, LlmOpening)
    assert fake.last["task"] == "opening_extract"
    assert fake.last["system"] == SYSTEM_PROMPT
    assert fake.last["user"].startswith("Wykonawca Cena")
    assert out.budget == D("380000.00")
    a, b, c = out.offers
    assert a.nip == "5932335316" and a.bidder_name == "Alfa Sp. z o.o."
    assert a.price == D("213192.1") and a.price_basis == "total"
    assert b.nip is None and b.offer_no == 2 and b.lot_no == 1
    assert b.price == D("38930.50") and b.price_basis == "unit"
    assert c.offer_no is None and c.price is None and c.price_basis == "other"


def test_system_prompt_exact():
    assert SYSTEM_PROMPT.startswith("Jesteś parserem dokumentów \"informacja z otwarcia ofert\"")
    assert SYSTEM_PROMPT.endswith("nie zgaduj NIP.")
    assert len(SYSTEM_PROMPT.splitlines()) == 7


def test_spytkowo_llm_path(db, fixtures, tmp_path):
    rs, doc_id = setup_doc(db, fixtures, tmp_path, "result_spytkowo.json",
                           "spytkowo_unit_prices.pdf", SPY_OCDS)
    names = ["MONSTER GROUP SP Z O.O.", "GS 5 Sp. z o.o.", "STEKOP S.A.",
             "Spółdzielnia WULKAN w Olsztynie", "Neptun Security Sp. z o.o.",
             "GrupaAlfa Sp. z o.o. Sp. k.", "MICROS GROUP Sp. z o.o.", "MULTISERVICE Sp. z o.o."]
    prices = ["45.63", "40.18", "49.02", "44.78", "39.85", "43.42", "41.82", "38.93"]
    fake = FakeProvider({"budget": None, "offers": [
        {"offer_no": i + 1, "lot_no": None, "bidder_name": n, "nip": None, "price": float(p),
         "price_basis": "unit"} for i, (n, p) in enumerate(zip(names, prices))]})
    assert process_opening_document(db, rs, fake, doc_id) == "parsed"
    assert fake.calls == 1
    bids = db.execute("select * from bids order by offer_no").fetchall()
    assert len(bids) == 8
    assert bids[7]["is_winner"] is True and bids[7]["rank"] == 1
    assert bids[0]["price_scale"] == D("6152.000000")
    assert all(b["parse_method"] == "llm" for b in bids)
    # extra checks
    assert sum(b["is_winner"] for b in bids) == 1
    assert [b["rank"] for b in bids] == [7, 3, 8, 6, 2, 5, 4, 1]
    assert bids[0]["price"] == D("45.63") and bids[0]["bidder_name_raw"] == names[0]
    winner_id = db.execute("select winner_bidder_id from lots").fetchone()["winner_bidder_id"]
    assert bids[7]["bidder_id"] == winner_id
    assert all(b["bidder_id"] is None for b in bids[:7])
    assert all(b["document_id"] == doc_id for b in bids)
    doc = doc_row(db, doc_id)
    assert doc["status"] == "parsed" and doc["text_method"] == "pdftext" and doc["error"] is None
    # reprocessing is idempotent
    assert process_opening_document(db, rs, fake, doc_id) == "parsed"
    assert db.execute("select count(*) c from bids").fetchone()["c"] == 8


def test_mismatch_when_llm_wrong(db, fixtures, tmp_path):
    rs, doc_id = setup_doc(db, fixtures, tmp_path, "result_spytkowo.json",
                           "spytkowo_unit_prices.pdf", SPY_OCDS)
    fake = FakeProvider({"budget": None, "offers": [
        {"offer_no": 1, "lot_no": None, "bidder_name": "X", "nip": None, "price": 1.0,
         "price_basis": "total"}]})
    assert process_opening_document(db, rs, fake, doc_id) == "mismatch"
    assert db.execute("select count(*) c from bids").fetchone()["c"] == 0
    err = db.execute("select error from documents where id=%s", (doc_id,)).fetchone()["error"]
    assert "count" in err
    assert doc_row(db, doc_id)["status"] == "mismatch"


def test_deterministic_path_skips_llm(db, fixtures, tmp_path):
    rec = json.loads((fixtures / "bzp" / "result_kolbudy.json").read_text("utf-8"))
    upsert_notice(db, parse_result_notice(rec), "sha")
    db.execute("update lots set n_offers = 2, price_min = 213192.00, price_max = 250000.00,"
               " price_winner = 213192.00")
    db.commit()
    rs = RawStore(tmp_path)
    data = docx_bytes([
        "INFORMACJA Z OTWARCIA OFERT",
        "1. Alfa Sp. z o.o., NIP 593-233-53-16, cena 213 192,00 zl",
        "2. Beta Sp. j., NIP 526-025-02-74, cena 250 000,00 zl",
    ])
    doc_id = insert_doc(db, rs, data, "otwarcie.docx", KOL_OCDS)
    fake = FakeProvider({"budget": None, "offers": []})
    assert process_opening_document(db, rs, fake, doc_id) == "parsed"
    assert fake.calls == 0
    bids = db.execute("select * from bids order by offer_no").fetchall()
    assert [b["bidder_nip_raw"] for b in bids] == ["5932335316", "5260250274"]
    assert all(b["parse_method"] == "deterministic" for b in bids)
    assert all(b["price_scale"] == D(1) for b in bids)
    winner_id = db.execute("select winner_bidder_id from lots").fetchone()["winner_bidder_id"]
    assert bids[0]["is_winner"] is True and bids[0]["rank"] == 1
    assert bids[0]["bidder_id"] == winner_id
    assert bids[1]["is_winner"] is False and bids[1]["rank"] == 2
    # no name in the document and an unknown NIP: identity resolution fills it in later
    assert bids[1]["bidder_id"] is None
    doc = doc_row(db, doc_id)
    assert doc["status"] == "parsed" and doc["text_method"] == "docx"


def add_lot2(db):
    zeta = upsert_bidder(db, name="Zeta Sp. z o.o.", nip="5260250274", city=None, province=None)
    db.execute(
        "insert into lots(procedure_id, lot_no, outcome, n_offers, price_min, price_max,"
        " price_winner, winner_bidder_id) values (%s, 2, 'umowa', 2, 100.00, 200.00, 100.00, %s)",
        (SPY_OCDS, zeta))
    db.commit()
    return zeta


def test_multilot_groups_by_lot(db, fixtures, tmp_path):
    rs, doc_id = setup_doc(db, fixtures, tmp_path, "result_spytkowo.json",
                           "spytkowo_unit_prices.pdf", SPY_OCDS)
    zeta = add_lot2(db)
    fake = FakeProvider({"budget": None, "offers": spy_offers(None) + [
        {"offer_no": 1, "lot_no": 2, "bidder_name": "Zeta Sp. z o.o.", "nip": "526-025-02-74",
         "price": 100.0, "price_basis": "total"},
        {"offer_no": 2, "lot_no": 2, "bidder_name": "Omega", "nip": None, "price": 200.0,
         "price_basis": "total"}]})
    assert process_opening_document(db, rs, fake, doc_id) == "parsed"
    lot2 = db.execute(
        "select b.* from bids b join lots l on l.id = b.lot_id where l.lot_no = 2"
        " order by b.offer_no").fetchall()
    assert len(lot2) == 2
    assert lot2[0]["is_winner"] is True and lot2[0]["bidder_id"] == zeta
    assert lot2[0]["match_confidence"] == 1.0
    assert lot2[1]["is_winner"] is False and lot2[1]["rank"] == 2
    assert db.execute("select count(*) c from bids").fetchone()["c"] == 10
    name = db.execute("select name from bidders where id=%s", (zeta,)).fetchone()["name"]
    assert name == "Zeta Sp. z o.o."


def test_multilot_all_or_nothing(db, fixtures, tmp_path):
    rs, doc_id = setup_doc(db, fixtures, tmp_path, "result_spytkowo.json",
                           "spytkowo_unit_prices.pdf", SPY_OCDS)
    add_lot2(db)
    fake = FakeProvider({"budget": None, "offers": spy_offers(1) + [
        {"offer_no": 1, "lot_no": 2, "bidder_name": "Zeta Sp. z o.o.", "nip": None,
         "price": 100.0, "price_basis": "total"}]})
    assert process_opening_document(db, rs, fake, doc_id) == "mismatch"
    assert db.execute("select count(*) c from bids").fetchone()["c"] == 0
    assert "lot2:count" in doc_row(db, doc_id)["error"]


def test_unknown_lot_is_mismatch(db, fixtures, tmp_path):
    rs, doc_id = setup_doc(db, fixtures, tmp_path, "result_spytkowo.json",
                           "spytkowo_unit_prices.pdf", SPY_OCDS)
    add_lot2(db)
    fake = FakeProvider({"budget": None, "offers": spy_offers(1) + [
        {"offer_no": 1, "lot_no": 7, "bidder_name": "Zeta Sp. z o.o.", "nip": None,
         "price": 100.0, "price_basis": "total"}]})
    assert process_opening_document(db, rs, fake, doc_id) == "mismatch"
    assert "lot7:unknown" in doc_row(db, doc_id)["error"]
    assert db.execute("select count(*) c from bids").fetchone()["c"] == 0


def test_invalid_llm_output_needs_review(db, fixtures, tmp_path):
    rs, doc_id = setup_doc(db, fixtures, tmp_path, "result_spytkowo.json",
                           "spytkowo_unit_prices.pdf", SPY_OCDS)
    fake = FakeProvider({"budget": None, "offers": "brak"})
    assert process_opening_document(db, rs, fake, doc_id) == "needs_review"
    doc = doc_row(db, doc_id)
    assert doc["status"] == "needs_review" and doc["error"].startswith("llm_invalid")
    assert db.execute("select count(*) c from bids").fetchone()["c"] == 0


def test_unsupported_file_needs_review(db, fixtures, tmp_path):
    rec = json.loads((fixtures / "bzp" / "result_spytkowo.json").read_text("utf-8"))
    upsert_notice(db, parse_result_notice(rec), "sha")
    rs = RawStore(tmp_path)
    doc_id = insert_doc(db, rs, b"plain text", "otwarcie.txt", SPY_OCDS)
    fake = FakeProvider({"budget": None, "offers": []})
    assert process_opening_document(db, rs, fake, doc_id) == "needs_review"
    assert fake.calls == 0
    assert doc_row(db, doc_id)["error"].startswith("extract")


def test_budget_exceeded_propagates(db, fixtures, tmp_path):
    from tie.ai.providers import BudgetExceeded

    class Broke:
        def complete_json(self, *, task, system, user):
            raise BudgetExceeded("limit")

    rs, doc_id = setup_doc(db, fixtures, tmp_path, "result_spytkowo.json",
                           "spytkowo_unit_prices.pdf", SPY_OCDS)
    try:
        process_opening_document(db, rs, Broke(), doc_id)
    except BudgetExceeded:
        pass
    else:
        raise AssertionError("BudgetExceeded was swallowed")
    assert doc_row(db, doc_id)["status"] == "downloaded"
