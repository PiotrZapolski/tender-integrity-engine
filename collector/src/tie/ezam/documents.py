"""Dokumenty postępowania z platformy e-Zamówienia.

`list_documents` pobiera listę opublikowanych dokumentów, `classify` rozpoznaje typ
(otwarcie ofert, wybór, unieważnienie, inne), `sync_documents` zapisuje istotne dokumenty
w tabeli `documents` i kolejkuje pobranie informacji z otwarcia ofert, a
`download_document` pobiera plik do magazynu surowych danych i kolejkuje jego parsowanie.
"""

from __future__ import annotations

import re

import psycopg

from tie.http import PoliteClient
from tie.identity.names import normalize_name
from tie.jobs import enqueue
from tie.rawstore import RawStore

DOCS_URL = "https://ezamowienia.gov.pl/mp-readmodels/api/Search/GetTenderDocuments"
DOWNLOAD_URL = "https://ezamowienia.gov.pl/mp-readmodels/api/Tender/DownloadDocument/{ocds}/{doc}"

SOURCE = "ezamowienia"
RAW_SOURCE = "ezam"
DOWNLOAD_JOB = "ezam_download"
PARSE_JOB = "opening_parse"
STORED_TYPES = ("opening", "selection", "cancellation")

# Dopasowania na tekście po normalize_name (małe litery, bez polskich znaków).
_OPENING_STRONG = re.compile(r"\bz otwarci|zestawienie ofert|zbiorcze zestawienie")
_OPENING_WEAK = re.compile(r"otwarci")
# Słowo "otwarcia" pojawia się też w zmianach terminów i odpowiedziach na pytania
# ("nowe terminy skladania i otwarcia ofert"); takie dokumenty to nie informacja z otwarcia.
_OPENING_NOISE = re.compile(
    r"\btermin|skladani|odpowiedz|pytani|wyjasnien|\bzmian|przedluz|ogloszeni|\bswz\b"
)
_SELECTION = re.compile(r"\bwybor|najkorzystniejsz")
_CANCELLATION = re.compile(r"uniewaznien")


def classify(name: str, file_name: str) -> str:
    """Zwraca "opening", "selection", "cancellation" albo "other"."""
    text = normalize_name(f"{name or ''} {file_name or ''}")
    if _OPENING_STRONG.search(text) or (
        _OPENING_WEAK.search(text) and not _OPENING_NOISE.search(text)
    ):
        return "opening"
    if _SELECTION.search(text):
        return "selection"
    if _CANCELLATION.search(text):
        return "cancellation"
    return "other"


def list_documents(client: PoliteClient, ocds_id: str) -> list[dict]:
    """Opublikowane dokumenty postępowania (pomija usunięte i inne stany niż Published)."""
    r = client.get(DOCS_URL, params={"tenderId": ocds_id})
    r.raise_for_status()
    data = r.json() or []
    if not isinstance(data, list):
        raise ValueError(f"unexpected documents payload for {ocds_id}: {type(data).__name__}")
    out = []
    for d in data:
        if not isinstance(d, dict) or not d.get("objectId"):
            continue
        if d.get("tenderDocumentState") != "Published":
            continue
        out.append({
            "objectId": d["objectId"],
            "name": d.get("name") or "",
            "fileName": d.get("fileName") or "",
            "tenderDocumentState": d["tenderDocumentState"],
        })
    return out


def sync_documents(conn: psycopg.Connection, client: PoliteClient, ocds_id: str) -> int:
    """Zapisz dokumenty typu opening/selection/cancellation; zwraca liczbę dokumentów opening.

    Upsert nie nadpisuje `status` ani `raw_sha256`. Dla każdego dokumentu opening po
    zatwierdzeniu transakcji dodaje zadanie `ezam_download` (klucz `objectId`).
    """
    docs = list_documents(client, ocds_id)
    openings: list[str] = []
    try:
        for d in docs:
            doc_type = classify(d["name"], d["fileName"])
            if doc_type not in STORED_TYPES:
                continue
            conn.execute(
                "insert into documents(procedure_id, source, source_doc_id, file_name, doc_type)"
                " values (%s, %s, %s, %s, %s)"
                " on conflict (source, source_doc_id) do update set"
                "  file_name = excluded.file_name,"
                "  doc_type = excluded.doc_type,"
                "  updated_at = now()",
                (ocds_id, SOURCE, d["objectId"], d["fileName"] or d["name"], doc_type),
            )
            if doc_type == "opening":
                openings.append(d["objectId"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    for doc_id in openings:
        enqueue(conn, DOWNLOAD_JOB, doc_id, {"ocds_id": ocds_id, "doc_id": doc_id})
    return len(openings)


def download_document(
    conn: psycopg.Connection,
    client: PoliteClient,
    rawstore: RawStore,
    ocds_id: str,
    doc_id: str,
) -> str:
    """Pobierz plik dokumentu, zapisz go surowo i zakolejkuj `opening_parse`; zwraca sha256."""
    row = conn.execute(
        "select id from documents where source = %s and source_doc_id = %s",
        (SOURCE, doc_id),
    ).fetchone()
    conn.commit()
    if row is None:
        raise LookupError(f"document {doc_id} not found")
    document_id = row["id"]

    r = client.get(DOWNLOAD_URL.format(ocds=ocds_id, doc=doc_id))
    r.raise_for_status()
    if not r.content:
        raise ValueError(f"empty file for document {doc_id}")
    sha = rawstore.put(r.content, source=RAW_SOURCE)

    try:
        conn.execute(
            "update documents set raw_sha256 = %s, status = 'downloaded', error = null,"
            " updated_at = now() where id = %s",
            (sha, document_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    enqueue(conn, PARSE_JOB, doc_id, {"document_id": document_id})
    return sha
