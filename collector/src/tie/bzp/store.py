"""Zapis ogłoszeń o wyniku z BZP do Postgresa (idempotentny upsert).

`upsert_notice` zapisuje kupującego, postępowanie, części i zwycięzców w jednej transakcji,
zatwierdza ją, a potem (gdy trzeba) dodaje zadanie pobrania dokumentów z e-Zamówień.
`upsert_bidder` nie zatwierdza transakcji: robi to wywołujący.
"""

from __future__ import annotations

import psycopg

from tie.bzp.parser import LotResult, ResultNotice
from tie.identity.names import looks_like_company, normalize_name
from tie.jobs import enqueue

DOCS_JOB = "ezam_docs"
DOCS_PLATFORM = "ezamowienia"


def upsert_bidder(
    conn: psycopg.Connection,
    *,
    name: str,
    nip: str | None,
    city: str | None,
    province: str | None,
) -> int:
    """Znajdź lub utwórz wykonawcę; zwraca `bidders.id`.

    Z NIP: upsert po `nip` (nazwa nadpisywana, miasto i województwo tylko gdy podane).
    Bez NIP: dopasowanie po `(name_norm, city)` wśród wierszy bez NIP.
    `is_jdg` ustawiane wyłącznie przy tworzeniu wiersza.
    """
    name_norm = normalize_name(name)
    is_jdg = not looks_like_company(name)
    if nip:
        row = conn.execute(
            "insert into bidders(nip, name, name_norm, is_jdg, city, province)"
            " values (%s, %s, %s, %s, %s, %s)"
            " on conflict (nip) do update set"
            "  name = excluded.name,"
            "  name_norm = excluded.name_norm,"
            "  city = coalesce(excluded.city, bidders.city),"
            "  province = coalesce(excluded.province, bidders.province)"
            " returning id",
            (nip, name, name_norm, is_jdg, city, province),
        ).fetchone()
        return row["id"]

    row = conn.execute(
        "select id from bidders where nip is null and name_norm = %s"
        " and coalesce(city, '') = coalesce(%s, '') order by id limit 1",
        (name_norm, city),
    ).fetchone()
    if row is not None:
        if province:
            conn.execute(
                "update bidders set province = %s where id = %s and province is null",
                (province, row["id"]),
            )
        return row["id"]
    row = conn.execute(
        "insert into bidders(nip, name, name_norm, is_jdg, city, province)"
        " values (null, %s, %s, %s, %s, %s) returning id",
        (name, name_norm, is_jdg, city, province),
    ).fetchone()
    return row["id"]


def _upsert_buyer(conn: psycopg.Connection, notice: ResultNotice) -> int:
    if notice.buyer_nip:
        row = conn.execute(
            "insert into buyers(nip, regon, name, province, city)"
            " values (%s, %s, %s, %s, %s)"
            " on conflict (nip) do update set"
            "  name = excluded.name,"
            "  regon = coalesce(excluded.regon, buyers.regon),"
            "  province = coalesce(excluded.province, buyers.province),"
            "  city = coalesce(excluded.city, buyers.city)"
            " returning id",
            (notice.buyer_nip, notice.buyer_regon, notice.buyer_name, notice.buyer_province,
             notice.buyer_city),
        ).fetchone()
        return row["id"]

    row = conn.execute(
        "select id from buyers where nip is null and name = %s"
        " and coalesce(city, '') = coalesce(%s, '') order by id limit 1",
        (notice.buyer_name, notice.buyer_city),
    ).fetchone()
    if row is not None:
        conn.execute(
            "update buyers set"
            "  regon = coalesce(%s, regon),"
            "  province = coalesce(%s, province)"
            " where id = %s",
            (notice.buyer_regon, notice.buyer_province, row["id"]),
        )
        return row["id"]
    row = conn.execute(
        "insert into buyers(nip, regon, name, province, city)"
        " values (null, %s, %s, %s, %s) returning id",
        (notice.buyer_regon, notice.buyer_name, notice.buyer_province, notice.buyer_city),
    ).fetchone()
    return row["id"]


def _upsert_procedure(
    conn: psycopg.Connection, notice: ResultNotice, buyer_id: int, raw_sha256: str
) -> None:
    conn.execute(
        "insert into procedures(ocds_id, bzp_number, notice_number, buyer_id, title,"
        " order_type, mode, cpv_main, cpv_all, is_below_eu, platform, platform_url,"
        " published_at, raw_sha256, updated_at)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())"
        " on conflict (ocds_id) do update set"
        "  bzp_number = excluded.bzp_number,"
        "  notice_number = excluded.notice_number,"
        "  buyer_id = excluded.buyer_id,"
        "  title = excluded.title,"
        "  order_type = excluded.order_type,"
        "  mode = excluded.mode,"
        "  cpv_main = excluded.cpv_main,"
        "  cpv_all = excluded.cpv_all,"
        "  is_below_eu = excluded.is_below_eu,"
        "  platform = excluded.platform,"
        "  platform_url = excluded.platform_url,"
        "  published_at = excluded.published_at,"
        "  raw_sha256 = excluded.raw_sha256,"
        "  updated_at = now()",
        (
            notice.ocds_id,
            notice.bzp_number,
            notice.notice_number,
            buyer_id,
            notice.title,
            notice.order_type,
            notice.mode,
            notice.cpv_all[0] if notice.cpv_all else None,
            list(notice.cpv_all),
            notice.is_below_eu,
            notice.platform,
            notice.platform_url,
            notice.published_at,
            raw_sha256,
        ),
    )


def _upsert_lot(conn: psycopg.Connection, ocds_id: str, lot: LotResult) -> None:
    winner = lot.winner
    winner_id = None
    if winner is not None:
        # Konsorcjum: zapisujemy lidera (nazwa i NIP z 7.3.x).
        winner_id = upsert_bidder(
            conn, name=winner.name, nip=winner.nip, city=winner.city, province=winner.province
        )
    conn.execute(
        "insert into lots(procedure_id, lot_no, cpv, estimated_value, outcome, n_offers,"
        " n_rejected, price_min, price_max, price_winner, contract_value, contract_date,"
        " winner_bidder_id, consortium)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " on conflict (procedure_id, lot_no) do update set"
        "  cpv = excluded.cpv,"
        "  estimated_value = excluded.estimated_value,"
        "  outcome = excluded.outcome,"
        "  n_offers = excluded.n_offers,"
        "  n_rejected = excluded.n_rejected,"
        "  price_min = excluded.price_min,"
        "  price_max = excluded.price_max,"
        "  price_winner = excluded.price_winner,"
        "  contract_value = excluded.contract_value,"
        "  contract_date = excluded.contract_date,"
        "  winner_bidder_id = excluded.winner_bidder_id,"
        "  consortium = excluded.consortium",
        (
            ocds_id,
            lot.lot_no,
            lot.cpv,
            lot.estimated_value,
            lot.outcome,
            lot.n_offers,
            lot.n_rejected,
            lot.price_min,
            lot.price_max,
            lot.price_winner,
            lot.contract_value,
            lot.contract_date,
            winner_id,
            bool(winner and winner.consortium),
        ),
    )


def _needs_opening(notice: ResultNotice) -> bool:
    return notice.platform == DOCS_PLATFORM and any(
        lot.n_offers is not None and lot.n_offers >= 2 for lot in notice.lots
    )


def upsert_notice(conn: psycopg.Connection, notice: ResultNotice, raw_sha256: str) -> dict:
    """Zapisz ogłoszenie o wyniku; ponowny zapis nadpisuje wartości, nie dubluje wierszy.

    Zwraca `{"ocds_id": str, "lots": int, "needs_opening": bool}`. Gdy `needs_opening`,
    po zatwierdzeniu transakcji dodaje zadanie `ezam_docs` z kluczem `ocds_id`.

    Ogłoszenie bez `tenderId` (puste lub brak) rzuca `ValueError` przed jakimkolwiek zapisem:
    inaczej wszystkie takie ogłoszenia nadpisywałyby jeden wiersz `procedures('')`. Handler
    `bzp_day` loguje taki rekord i go pomija.
    """
    if not (notice.ocds_id or "").strip():
        raise ValueError(f"notice {notice.notice_number}: empty tenderId, not stored")
    try:
        buyer_id = _upsert_buyer(conn, notice)
        _upsert_procedure(conn, notice, buyer_id, raw_sha256)
        for lot in notice.lots:
            _upsert_lot(conn, notice.ocds_id, lot)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    needs_opening = _needs_opening(notice)
    if needs_opening:
        enqueue(conn, DOCS_JOB, notice.ocds_id, {"ocds_id": notice.ocds_id})
    return {"ocds_id": notice.ocds_id, "lots": len(notice.lots), "needs_opening": needs_opening}
