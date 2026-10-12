"""Wzbogacanie wykonawców danymi z białej listy podatników VAT (MF).

Zapytanie zbiorcze o maksymalnie 30 NIP-ów; MF pozwala na 100 zapytań wyszukujących dziennie,
więc każde zapytanie najpierw rezerwuje miejsce w `wl_checks` (licznik na dzień, zatwierdzany
od razu). Numery kont zapisujemy wyłącznie jako sha256 (hex) z samych cyfr numeru.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date

import psycopg

from tie.http import PoliteClient
from tie.identity.names import PLACEHOLDER_PREFIX, normalize_name

log = logging.getLogger(__name__)

WL_URL = "https://wl-api.mf.gov.pl/api/search/nips/{nips}"
BATCH_SIZE = 30
_NIP_WEIGHTS = (6, 5, 7, 2, 3, 4, 5, 6, 7)


class WhitelistError(Exception):
    pass


def valid_nip(nip: str) -> bool:
    if not re.fullmatch(r"\d{10}", nip or ""):
        return False
    check = sum(int(d) * w for d, w in zip(nip, _NIP_WEIGHTS)) % 11
    return check == int(nip[9])


def account_hash(number: str) -> str:
    return hashlib.sha256(re.sub(r"\D", "", number).encode("ascii")).hexdigest()


def _reserve_request(conn: psycopg.Connection, today: date, daily_cap: int) -> bool:
    """Atomowo zwiększ licznik zapytań na `today`; False, gdy limit wyczerpany."""
    conn.execute(
        "insert into wl_checks(day, requests) values (%s, 0) on conflict (day) do nothing",
        (today,),
    )
    row = conn.execute(
        "update wl_checks set requests = requests + 1"
        " where day = %s and requests < %s returning requests",
        (today, daily_cap),
    ).fetchone()
    conn.commit()
    return row is not None


def _pending(conn: psycopg.Connection, limit: int) -> list[dict]:
    """Wykonawcy do sprawdzenia, w kolejności ważności dla silnika (dzienny limit MF decyduje,
    kto dostanie rachunki i adres pierwszy): najpierw najwięcej wygranych części
    (`lots.winner_bidder_id`), potem obecni w ofertach (`bids`), potem reszta; w obrębie
    grupy rosnąco po `id`."""
    rows = conn.execute(
        "with wins as ("
        "  select l.winner_bidder_id as bidder_id, count(*) as n from lots l"
        "  where l.winner_bidder_id is not null group by l.winner_bidder_id)"
        " select b.id, b.nip from bidders b"
        " left join wins w on w.bidder_id = b.id"
        " where b.nip is not null and b.wl_checked_at is null"
        " order by coalesce(w.n, 0) desc,"
        "  exists (select 1 from bids x where x.bidder_id = b.id) desc, b.id"
        " limit %s",
        (limit,),
    ).fetchall()
    conn.commit()
    return rows


def _apply_subject(conn: psycopg.Connection, bidder_id: int, subject: dict) -> None:
    """Zapisz dane z MF. Nazwę (i `name_norm`) bierzemy z MF tylko, gdy obecna jest pusta albo
    zastępcza (`NIP <nip>`); prawdziwej nazwy z BZP lub dokumentu nigdy nie nadpisujemy."""
    krs = subject.get("krs") or None
    address = subject.get("workingAddress") or subject.get("residenceAddress") or None
    wl_name = (subject.get("name") or "").strip() or None
    # In UPDATE every SET expression sees the old row, so both columns test the old name.
    fill = ("(%(name)s::text is not null"
            " and (btrim(name) = '' or name = %(prefix)s::text || coalesce(nip, '')))")
    conn.execute(
        "update bidders set regon = coalesce(%(regon)s, regon), krs = %(krs)s,"
        " is_jdg = %(is_jdg)s, address = coalesce(%(address)s, address),"
        f" name = case when {fill} then %(name)s::text else name end,"
        f" name_norm = case when {fill} then %(name_norm)s::text else name_norm end,"
        " wl_checked_at = now() where id = %(id)s",
        {
            "regon": subject.get("regon") or None,
            "krs": krs,
            "is_jdg": krs is None,
            "address": address,
            "name": wl_name,
            "name_norm": normalize_name(wl_name or ""),
            "prefix": PLACEHOLDER_PREFIX,
            "id": bidder_id,
        },
    )
    for number in subject.get("accountNumbers") or []:
        if not re.sub(r"\D", "", number or ""):
            continue
        conn.execute(
            "insert into bidder_accounts(bidder_id, account_hash) values (%s, %s)"
            " on conflict do nothing",
            (bidder_id, account_hash(number)),
        )


def enrich_bidders(
    conn: psycopg.Connection, client: PoliteClient, *, daily_cap: int, today: date,
    limit: int = 300,
) -> int:
    """Sprawdź wykonawców z NIP na białej liście; zwraca liczbę wzbogaconych."""
    rows = _pending(conn, limit)
    invalid = [r["id"] for r in rows if not valid_nip(r["nip"])]
    if invalid:
        # Błędny NIP psuje całe zapytanie zbiorcze i nigdy nie da wyniku: oznaczamy jako
        # sprawdzony bez wzbogacenia.
        conn.execute("update bidders set wl_checked_at = now() where id = any(%s)", (invalid,))
        conn.commit()
        log.warning("whitelist: %d bidders with invalid NIP skipped", len(invalid))
    rows = [r for r in rows if valid_nip(r["nip"])]

    enriched = 0
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]
        if not _reserve_request(conn, today, daily_cap):
            log.info("whitelist: daily cap %d reached for %s", daily_cap, today)
            break
        by_nip = {r["nip"]: r["id"] for r in batch}
        url = WL_URL.format(nips=",".join(by_nip))
        r = client.get(url, params={"date": today.isoformat()})
        if r.status_code != 200:
            raise WhitelistError(f"whitelist HTTP {r.status_code}: {r.text[:300]}")
        try:
            entries = r.json()["result"]["entries"] or []
        except (ValueError, KeyError, TypeError) as exc:
            raise WhitelistError("whitelist response has no result.entries") from exc

        try:
            for entry in entries:
                bidder_id = by_nip.get(str(entry.get("identifier") or ""))
                subjects = entry.get("subjects") or []
                if bidder_id is None or not subjects:
                    continue
                _apply_subject(conn, bidder_id, subjects[0])
                enriched += 1
            # Wszystkie z paczki (także bez danych w odpowiedzi) są już sprawdzone.
            conn.execute(
                "update bidders set wl_checked_at = now()"
                " where id = any(%s) and wl_checked_at is null",
                (list(by_nip.values()),),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return enriched
