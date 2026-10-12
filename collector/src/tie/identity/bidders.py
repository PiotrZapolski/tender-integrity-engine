"""Dopasowanie oferentów z dokumentów otwarcia do znanych wykonawców.

Oferty (`bids`) bez `bidder_id` dopasowujemy tak:
- z NIP w ofercie: deterministycznie po NIP (bez AI),
- bez NIP: kandydaci z pg_trgm (`similarity(name_norm, ...) >= 0.45`, tylko wykonawcy z NIP),
  a decyzję podejmuje Jev (pytanie typu `choice`). Gdy wybrany kandydat ma prawdopodobieństwo
  >= `threshold`, wiążemy ofertę z nim; inaczej tworzymy nowego wykonawcę bez NIP.

Oferta z NIP, ale bez nazwy (parser deterministyczny), dla nieznanego NIP tworzy wykonawcę
o nazwie zastępczej `NIP <nip>` (`placeholder_name`); biała lista podmienia ją na nazwę z MF.

Każda oferta to osobna transakcja (decider sam zatwierdza wiersz w `ai_calls`). Błąd jednej
oferty (np. odrzucony stan w Jev) nie zatrzymuje pozostałych: oferta jest logowana, pomijana
i dopisywana do zbioru `skip`, więc w tym samym przebiegu nie wraca. Na końcu paczki ofertom,
które się nie udały, zwiększamy `bids.identity_failures`; kolejny przebieg (następne zadanie
`identity`) spróbuje ich znowu, ale po `MAX_IDENTITY_FAILURES` nieudanych przebiegach oferta
wypada z dopasowania (reset licznika: docs/OPERATIONS.md). Nowe oferty (licznik 0) idą
pierwsze, więc trwale błędne oferty nie zasłaniają reszty.

Przerywają (bez zwiększania liczników) tylko błędy systemowe: `BudgetExceeded`, utrata
połączenia z bazą, wyczerpane ponowienia HTTP (`HttpGiveUp`), odrzucony klucz (HTTP 401/403),
a także sytuacja, gdy w paczce były co najmniej dwie nowe oferty i żadna z nich się nie udała
(awaria nie może oznaczyć dobrych ofert jako błędnych). Paczka samych ofert, które już
wcześniej się nie udały, tylko zwiększa liczniki i nie przerywa.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

import psycopg

from tie.ai.providers import BudgetExceeded, ProviderHttpError
from tie.bzp.store import upsert_bidder
from tie.http import HttpGiveUp
from tie.identity.names import normalize_name, placeholder_name

log = logging.getLogger(__name__)

# A bid that failed in this many separate identity runs is no longer selected.
MAX_IDENTITY_FAILURES = 3
# Filter shared by candidate selection here and the remaining-bids count in tie.handlers.
UNMATCHED_SQL = f"bidder_id is null and identity_failures < {MAX_IDENTITY_FAILURES}"
# Rejected API key: every bid would fail the same way.
AUTH_STATUSES = frozenset({401, 403})
MIN_SIMILARITY = 0.45
NONE_CHOICE = "none"
MATCH_INSTRUCTIONS = (
    "Który kandydat to ten sam wykonawca co `oferent`? Wybierz none, jeśli żaden."
)


class Decider(Protocol):
    def decide(self, *, task: str, state: Any, questions: dict) -> dict: ...


def candidates(
    conn: psycopg.Connection, name: str, province: str | None, limit: int = 5
) -> list[dict]:
    """Wykonawcy z NIP o nazwie podobnej do `name`, od najbardziej podobnych.

    Gdy `province` podane, tylko wykonawcy z tego samego województwa.
    """
    name_norm = normalize_name(name)
    if not name_norm:
        return []
    # `%%` (operator pg_trgm, domyślny próg 0.3) pozwala użyć indeksu gin_trgm;
    # właściwy próg 0.45 sprawdza `similarity`.
    return conn.execute(
        "select id, name, nip, city, province, similarity(name_norm, %(n)s) as sim"
        " from bidders"
        " where nip is not null and name_norm %% %(n)s"
        "  and similarity(name_norm, %(n)s) >= %(min)s"
        "  and (%(prov)s::text is null or province = %(prov)s::text)"
        " order by sim desc, id"
        " limit %(limit)s",
        {"n": name_norm, "min": MIN_SIMILARITY, "prov": province, "limit": limit},
    ).fetchall()


def _label(c: dict) -> str:
    return f"{c['name']}, {c['city'] or '-'}, NIP {c['nip']}"


def _prob(probabilities: dict, key: str) -> float | None:
    value = probabilities.get(key)
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _decide(decider: Decider, raw_name: str, cands: list[dict],
            threshold: float) -> tuple[dict | None, float | None]:
    """Zwraca (wybrany kandydat albo None, match_confidence)."""
    keys = [str(i) for i in range(len(cands))]
    questions = {
        "match": {
            "type": "choice",
            "instructions": MATCH_INSTRUCTIONS,
            "criteria": {**{k: None for k in keys}, NONE_CHOICE: None},
        }
    }
    state = {"oferent": raw_name, "kandydaci": {k: _label(c) for k, c in zip(keys, cands)}}
    answers = decider.decide(task="identity", state=state, questions=questions)
    answer = (answers or {}).get("match") or {}
    choice = str(answer.get("choice") or NONE_CHOICE)
    probabilities = answer.get("probabilities") or {}
    if choice in keys:
        p = _prob(probabilities, choice)
        if p is not None and p >= threshold:
            return cands[int(choice)], p
    return None, _prob(probabilities, NONE_CHOICE)


def _nip_digits(raw: str | None) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    return digits if len(digits) == 10 else None


def _resolve_by_nip(conn: psycopg.Connection, raw_name: str, nip: str) -> tuple[int, bool]:
    """Zwraca (bidder_id, czy wykonawca już istniał).

    Bez nazwy w ofercie nowy wykonawca dostaje nazwę zastępczą `NIP <nip>`.
    """
    row = conn.execute("select id from bidders where nip = %s", (nip,)).fetchone()
    if row is not None:
        return row["id"], True
    name = raw_name.strip() or placeholder_name(nip)
    return upsert_bidder(conn, name=name, nip=nip, city=None, province=None), False


def _systemic(exc: BaseException) -> bool:
    """Błąd, który dotyczy wszystkich ofert (budżet, baza, sieć, klucz), a nie jednej oferty."""
    fatal = (BudgetExceeded, psycopg.OperationalError, psycopg.InterfaceError, HttpGiveUp)
    if isinstance(exc, ProviderHttpError) and exc.status in AUTH_STATUSES:
        return True
    return isinstance(exc, fatal) or isinstance(exc.__cause__, HttpGiveUp)


def resolve_unmatched_bids(
    conn: psycopg.Connection, decider: Decider, *, threshold: float = 0.85, limit: int = 200,
    skip: set[int] | None = None,
) -> int:
    """Uzupełnij `bids.bidder_id` dla ofert bez wykonawcy.

    Zwraca liczbę ofert dopasowanych do istniejących wykonawców. Oferty z `skip` są pomijane;
    id ofert, których nie udało się przetworzyć, są do `skip` dopisywane (ten sam zbiór
    przekazuj w kolejnych wywołaniach jednego przebiegu), a ich `identity_failures` rośnie o 1
    na końcu paczki.
    """
    if skip is None:
        skip = set()
    bids = conn.execute(
        "select id, bidder_name_raw, bidder_nip_raw, identity_failures from bids"
        f" where {UNMATCHED_SQL} and not (id = any(%s::bigint[]))"
        " order by identity_failures, id limit %s",
        (list(skip), limit),
    ).fetchall()
    conn.commit()

    matched = 0
    failed: list[int] = []
    # Ta sama nazwa w kilku ofertach w jednym przebiegu: jedna decyzja AI.
    seen: dict[str, tuple[int, float | None, bool]] = {}
    for bid in bids:
        raw_name = bid["bidder_name_raw"]
        try:
            nip = _nip_digits(bid["bidder_nip_raw"])
            if nip is not None:
                bidder_id, existed = _resolve_by_nip(conn, raw_name, nip)
                confidence: float | None = 1.0
            elif raw_name in seen:
                bidder_id, confidence, existed = seen[raw_name]
            else:
                cands = candidates(conn, raw_name, None)
                chosen, confidence = (None, None)
                if cands:
                    chosen, confidence = _decide(decider, raw_name, cands, threshold)
                if chosen is not None:
                    bidder_id, existed = chosen["id"], True
                else:
                    bidder_id = upsert_bidder(conn, name=raw_name, nip=None, city=None,
                                              province=None)
                    existed = False
                seen[raw_name] = (bidder_id, confidence, existed)
            conn.execute(
                "update bids set bidder_id = %s, match_confidence = %s where id = %s",
                (bidder_id, confidence, bid["id"]),
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if _systemic(exc):
                raise
            failed.append(bid["id"])
            skip.add(bid["id"])
            log.exception("identity: bid %s skipped", bid["id"])
            continue
        if existed:
            matched += 1
    log.info("identity: %d bids processed, %d matched to existing bidders, %d skipped",
             len(bids), matched, len(failed))
    fresh = [b["id"] for b in bids if b["identity_failures"] == 0]
    if len(fresh) >= 2 and set(fresh) <= set(failed):
        # Every fresh bid failing points at a systemic problem (API contract, key), not at
        # data; counters stay untouched so an outage does not mark good bids as poison.
        raise RuntimeError(f"identity: all {len(fresh)} bids failed"
                           f" ({len(bids)} bids in batch)")
    if failed:
        conn.execute(
            "update bids set identity_failures = identity_failures + 1"
            " where id = any(%s::bigint[])", (failed,))
        conn.commit()
    return matched
