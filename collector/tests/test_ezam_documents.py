import json

import httpx
import respx

from tie.bzp.parser import parse_result_notice
from tie.bzp.store import upsert_notice
from tie.ezam.documents import (
    DOCS_URL,
    DOWNLOAD_URL,
    classify,
    download_document,
    list_documents,
    sync_documents,
)
from tie.http import PoliteClient
from tie.rawstore import RawStore

OCDS = "ocds-148610-31180df3-a1c9-47cf-8061-b35e3d3d4483"


def test_classify():
    assert classify("ZBIORCZE ZESTAWIENIE OFERT", "INFORMACJA Z OTWARCIA OFERT.pdf") == "opening"
    assert classify("Informacja", "Informacja_z_otwarcia_ofert.pdf") == "opening"
    assert classify("INFORMACJA O WYBORZE OFERTY NAJKORZYSTNIEJSZEJ", "x.pdf") == "selection"
    assert classify("Zawiadomienie o unieważnieniu postępowania", "u.pdf") == "cancellation"
    assert classify("SWZ", "SWZ.pdf") == "other"


@respx.mock
def test_sync_and_download(db, fixtures, tmp_path):
    rec = json.loads((fixtures / "bzp" / "result_single.json").read_text("utf-8"))
    upsert_notice(db, parse_result_notice(rec), "sha")
    docs = json.loads((fixtures / "ezam" / "documents_31180df3.json").read_text("utf-8"))
    respx.get(DOCS_URL).mock(return_value=httpx.Response(200, json=docs))
    client = PoliteClient("ua", {}, sleep=lambda s: None)
    assert sync_documents(db, client, OCDS) == 1
    assert sync_documents(db, client, OCDS) == 1
    rows = db.execute("select doc_type, count(*) c from documents group by doc_type").fetchall()
    counts = {r["doc_type"]: r["c"] for r in rows}
    assert counts["opening"] == 1 and counts["selection"] == 1
    job = db.execute("select * from jobs where kind='ezam_download'").fetchone()
    doc_id = job["payload"]["doc_id"]
    assert doc_id == f"{OCDS}_17"
    pdf = (fixtures / "openings" / "kielce_scan.pdf").read_bytes()
    respx.get(DOWNLOAD_URL.format(ocds=OCDS, doc=doc_id)).mock(
        return_value=httpx.Response(200, content=pdf, headers={"Content-Type": "application/pdf"}))
    sha = download_document(db, client, RawStore(tmp_path), OCDS, doc_id)
    row = db.execute("select status, raw_sha256 from documents where source_doc_id=%s",
                     (doc_id,)).fetchone()
    assert row["status"] == "downloaded" and row["raw_sha256"] == sha
    assert db.execute("select count(*) c from jobs where kind='opening_parse'").fetchone()["c"] == 1


def test_classify_ignores_deadline_changes():
    name = "OGŁOSZENIE O ZMIANIE - NOWE TERMINY SKŁADANIA I OTWARCIA OFERT.pdf"
    assert classify("OGŁOSZENIE O ZMIANIE TERMINÓW", name) == "other"
    assert classify("ODPOWIEDZI NA PYTANIA I NOWE TREMINY SKŁADANIA I OTWARCIA OFERT",
                    "ODPOWIEDZI NA PYTANIA.pdf") == "other"
    assert classify("Otwarcie ofert", "otwarcie.pdf") == "opening"


@respx.mock
def test_list_documents_skips_not_published(fixtures):
    docs = json.loads((fixtures / "ezam" / "documents_31180df3.json").read_text("utf-8"))
    route = respx.get(DOCS_URL).mock(return_value=httpx.Response(200, json=docs))
    out = list_documents(PoliteClient("ua", {}, sleep=lambda s: None), OCDS)
    assert route.calls.last.request.url.params["tenderId"] == OCDS
    ids = {d["objectId"] for d in out}
    assert len(out) == len(docs) - 2
    assert f"{OCDS}_3" not in ids and f"{OCDS}_4" not in ids
    assert all(d["tenderDocumentState"] == "Published" for d in out)
