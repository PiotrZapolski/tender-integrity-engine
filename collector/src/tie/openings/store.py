"""Processing of one downloaded opening-of-offers document into validated `bids`.

Flow: raw bytes from the raw store, text extraction, the deterministic parser, and (when that
gives nothing that passes the BZP cross-check) the LLM fallback. Acceptance is per lot: the
offers of a lot are stored only when that lot passes `cross_check`; lots that fail get no
bids. Correctness beats coverage.

Document statuses returned and stored in `documents.status`:
- `parsed`: offers of at least one lot stored. `documents.error` is NULL or records what is
  worth knowing, in "; "-separated parts: `partial: <reasons>` (lots that failed, e.g.
  "partial: lot3:prices,lot5:winner") and `notes: <notes>` (relaxed rules used by the
  cross-check, e.g. "notes: bzp_partial,lot2:winner_by_price,lot_offset:1"),
- `mismatch`: offers found but every lot rejected by the cross-check (`documents.error` =
  reasons),
- `needs_review`: nothing to check (unreadable file, invalid LLM answer, no BZP facts).

When the document numbers its parts with a constant offset from BZP (e.g. "Zadanie nr 2..5"
continuing an earlier procedure, BZP lots 1..4), the shifted numbering is tried as well and
the one under which more lots pass is used (`lot_offset:<d>` note).

`BudgetExceeded` and provider (HTTP) errors propagate so the job goes back to the queue; the
transaction is rolled back and the document keeps its previous status.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass

import psycopg
from pydantic import ValidationError

from tie.bzp.store import upsert_bidder
from tie.ezam import documents as ezam_documents
from tie.money import clean_nip
from tie.openings.crosscheck import CheckResult, LotFacts, cross_check
from tie.openings.extract import extract_text
from tie.openings.llm import TextProvider, extract_with_llm
from tie.openings.parse import OfferRow, parse_opening
from tie.rawstore import RawStore

log = logging.getLogger(__name__)

# Raw store folder of each `documents.source`. e-Zamowienia files live in "ezam"; any other
# source uses its own name as the folder (e.g. "ezamawiajacy").
RAW_DIRS = {ezam_documents.SOURCE: ezam_documents.RAW_SOURCE}
PARSED = "parsed"
MISMATCH = "mismatch"
NEEDS_REVIEW = "needs_review"
MAX_ERROR_LEN = 500


def raw_dir(source: str) -> str:
    """Raw store folder holding the files of documents with this `documents.source`."""
    return RAW_DIRS.get(source, source)


@dataclass(frozen=True)
class _Lot:
    id: int
    lot_no: int
    facts: LotFacts
    winner_bidder_id: int | None

    @property
    def checkable(self) -> bool:
        """BZP has enough to verify offers: at least one offer and the min/max prices, or
        (`bzp_partial`) the winner price."""
        f = self.facts
        prices = (f.price_min is not None and f.price_max is not None) or (
            f.price_winner is not None)
        return f.n_offers is not None and f.n_offers >= 1 and prices


def _load_document(conn: psycopg.Connection, document_id: int) -> dict:
    row = conn.execute(
        "select d.id, d.procedure_id, d.source, d.file_name, d.raw_sha256, b.nip as buyer_nip"
        " from documents d"
        " join procedures p on p.ocds_id = d.procedure_id"
        " left join buyers b on b.id = p.buyer_id"
        " where d.id = %s",
        (document_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"document {document_id} not found")
    return row


def _load_lots(conn: psycopg.Connection, procedure_id: str) -> list[_Lot]:
    rows = conn.execute(
        "select l.id, l.lot_no, l.n_offers, l.n_rejected, l.price_min, l.price_max,"
        " l.price_winner,"
        " l.winner_bidder_id, b.nip as winner_nip, b.name_norm as winner_name_norm"
        " from lots l left join bidders b on b.id = l.winner_bidder_id"
        " where l.procedure_id = %s order by l.lot_no",
        (procedure_id,),
    ).fetchall()
    return [
        _Lot(
            id=r["id"],
            lot_no=r["lot_no"],
            facts=LotFacts(
                n_offers=r["n_offers"],
                price_min=r["price_min"],
                price_max=r["price_max"],
                price_winner=r["price_winner"],
                winner_nip=r["winner_nip"],
                winner_name_norm=r["winner_name_norm"],
                n_rejected=r["n_rejected"],
            ),
            winner_bidder_id=r["winner_bidder_id"],
        )
        for r in rows
    ]


def _finish(conn: psycopg.Connection, document_id: int, status: str,
            error: str | None = None) -> str:
    conn.execute(
        "update documents set status = %s, error = %s, updated_at = now() where id = %s",
        (status, error[:MAX_ERROR_LEN] if error else None, document_id),
    )
    log.info("opening document %s: %s %s", document_id, status, error or "")
    return status


def _ranks(rows: list[OfferRow]) -> list[int | None]:
    """Competition ranking by price ascending (ties share a rank); no price, no rank."""
    prices = [r.price for r in rows if r.price is not None]
    return [None if r.price is None else 1 + sum(p < r.price for p in prices) for r in rows]


def _bidder_by_nip(conn: psycopg.Connection, name: str, nip: str) -> int | None:
    """Known bidder with this NIP (names from BZP are kept), else a new one from the document.

    Without a name in the document (deterministic parser) an unknown NIP is left for the
    identity step instead of creating a bidder with no name.
    """
    row = conn.execute("select id from bidders where nip = %s", (nip,)).fetchone()
    if row is not None:
        return row["id"]
    if not name:
        return None
    return upsert_bidder(conn, name=name, nip=nip, city=None, province=None)


def _write_bids(conn: psycopg.Connection, document_id: int, lot: _Lot, rows: list[OfferRow],
                result: CheckResult, method: str) -> list[int]:
    """Write every offer of the lot, including the ones outside the BZP range (likely
    rejected, flagged in `outside_bzp_range`): cover bids are often rejected."""
    scale = result.scale
    outside = result.outside_range or [False] * len(rows)
    winner = result.winner
    ranks = _ranks(rows)
    ids: list[int] = []
    for i, row in enumerate(rows):
        bidder_id: int | None = None
        confidence: float | None = None
        if row.nip:
            bidder_id = _bidder_by_nip(conn, row.bidder_name, row.nip)
            confidence = 1.0 if bidder_id is not None else None
        elif i == winner:
            # Winner confirmed against BZP (name and price, or its unique price): it is the
            # BZP winner.
            bidder_id = lot.winner_bidder_id
        out = conn.execute(
            "insert into bids(lot_id, document_id, offer_no, bidder_id, bidder_name_raw,"
            " bidder_nip_raw, price, price_scale, is_winner, rank, parse_method,"
            " match_confidence, outside_bzp_range)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " on conflict (lot_id, offer_no, bidder_name_raw) do update set"
            "  document_id = excluded.document_id,"
            "  bidder_id = coalesce(excluded.bidder_id, bids.bidder_id),"
            "  match_confidence = case when excluded.bidder_id is null"
            "   then bids.match_confidence else excluded.match_confidence end,"
            "  bidder_nip_raw = excluded.bidder_nip_raw,"
            "  price = excluded.price,"
            "  price_scale = excluded.price_scale,"
            "  is_winner = excluded.is_winner,"
            "  rank = excluded.rank,"
            "  parse_method = excluded.parse_method,"
            "  outside_bzp_range = excluded.outside_bzp_range"
            " returning id",
            (lot.id, document_id, row.offer_no, bidder_id, row.bidder_name, row.nip,
             row.price, scale, i == winner, ranks[i], method, confidence, outside[i]),
        ).fetchone()
        ids.append(out["id"])
    return ids


def _store(conn: psycopg.Connection, document_id: int,
           checked: list[tuple[_Lot, list[OfferRow], CheckResult]], method: str) -> None:
    """Write the offers of every checked lot. The offers of a checked lot come from exactly
    one document, the last one that passed the cross-check: rows of these lots written
    earlier (by this or by another opening document of the procedure, e.g. a correction or a
    separate summary) that the current result does not have are deleted, so a lot never ends
    up with duplicate bids or two winners."""
    ids: list[int] = []
    lot_ids: list[int] = []
    for lot, rows, result in checked:
        ids.extend(_write_bids(conn, document_id, lot, rows, result, method))
        lot_ids.append(lot.id)
    conn.execute(
        "delete from bids where (lot_id = any(%s::bigint[]) or document_id = %s)"
        " and not (id = any(%s::bigint[]))",
        (lot_ids, document_id, ids),
    )


def _duplicate_keys(rows: list[OfferRow]) -> bool:
    keys = Counter((r.offer_no, r.bidder_name) for r in rows)
    return any(n > 1 for n in keys.values())


Checked = list[tuple[_Lot, list[OfferRow], CheckResult]]


@dataclass
class _LotsCheck:
    passed: Checked  # lots whose offers passed the cross-check
    reasons: list[str]  # why the other lots failed, plus lot numbers unknown to BZP
    notes: list[str]  # relaxed rules used by the passed lots


def _check_lots(groups: dict[int, list[OfferRow]], lots: list[_Lot]) -> _LotsCheck:
    """Cross-check every checkable lot on its own. Reasons and notes get a `lot<N>:` prefix
    when the procedure has several checkable lots or the document names unknown ones."""
    known = {lot.lot_no for lot in lots}
    targets = [lot for lot in lots if lot.checkable]
    prefixed = len(targets) > 1 or any(no not in known for no in groups)
    out = _LotsCheck(passed=[], reasons=[f"lot{no}:unknown" for no in sorted(groups)
                                         if no not in known], notes=[])
    for lot in targets:
        rows = groups.get(lot.lot_no, [])
        result = cross_check(rows, lot.facts)
        lot_reasons = list(result.reasons)
        if result.ok and _duplicate_keys(rows):
            lot_reasons.append("duplicate_offer")
        if lot_reasons:
            out.reasons.extend(f"lot{lot.lot_no}:{r}" if prefixed else r for r in lot_reasons)
        else:
            out.passed.append((lot, rows, result))
            out.notes.extend(f"lot{lot.lot_no}:{n}" if prefixed else n for n in result.notes)
    return out


def _check_lots_best_numbering(groups: dict[int, list[OfferRow]],
                               lots: list[_Lot]) -> _LotsCheck:
    """`_check_lots` as numbered by the document, and with the document's lot numbers shifted
    by a constant so that its lowest one is BZP's lowest (when all shifted numbers exist in
    BZP); the numbering under which more lots pass wins, ties keep the document's."""
    best = _check_lots(groups, lots)
    known = {lot.lot_no for lot in lots}
    if not groups or not known:
        return best
    offset = min(groups) - min(known)
    shifted = {no - offset: rows for no, rows in groups.items()}
    if offset == 0 or not set(shifted) <= known:
        return best
    alt = _check_lots(shifted, lots)
    if len(alt.passed) <= len(best.passed):
        return best
    alt.notes.append(f"lot_offset:{offset}")
    return alt


def _parsed_error(check: _LotsCheck) -> str | None:
    parts = []
    if check.reasons:
        parts.append("partial: " + ",".join(check.reasons))
    if check.notes:
        parts.append("notes: " + ",".join(check.notes))
    return "; ".join(parts) or None


def _llm_groups(llm_offers, lots: list[_Lot], buyer_nip: str | None
                ) -> dict[int, list[OfferRow]]:
    """LLM offers per lot number; offers without lot_no go to lot 1 (single-lot procedure:
    all offers go to its only lot). The buyer listed as a bidder is dropped."""
    single = lots[0].lot_no if len(lots) == 1 else None
    groups: dict[int, list[OfferRow]] = defaultdict(list)
    for offer in llm_offers:
        if buyer_nip and offer.nip == buyer_nip:
            continue
        lot_no = single if single is not None else (offer.lot_no or 1)
        group = groups[lot_no]
        offer_no = offer.offer_no if offer.offer_no is not None else len(group) + 1
        group.append(OfferRow(offer_no=offer_no, bidder_name=offer.bidder_name,
                              nip=offer.nip, price=offer.price))
    return dict(groups)


def _process(conn: psycopg.Connection, rawstore: RawStore, provider: TextProvider,
             document_id: int) -> str:
    doc = _load_document(conn, document_id)
    if not doc["raw_sha256"]:
        return _finish(conn, document_id, NEEDS_REVIEW, "raw_missing")
    try:
        data = rawstore.get(doc["raw_sha256"], source=raw_dir(doc["source"]))
    except FileNotFoundError:
        return _finish(conn, document_id, NEEDS_REVIEW, "raw_missing")
    try:
        extracted = extract_text(data, doc["file_name"])
    except Exception as e:  # noqa: BLE001 - any failure here is a property of the file
        return _finish(conn, document_id, NEEDS_REVIEW, f"extract: {type(e).__name__}: {e}")
    conn.execute("update documents set text_method = %s where id = %s",
                 (extracted.method, document_id))
    if not extracted.text.strip():
        return _finish(conn, document_id, NEEDS_REVIEW, "no_text")

    lots = _load_lots(conn, doc["procedure_id"])
    targets = [lot for lot in lots if lot.checkable]
    if not targets:
        return _finish(conn, document_id, NEEDS_REVIEW, "bzp_incomplete")

    buyer_nip = clean_nip(doc["buyer_nip"]) if doc["buyer_nip"] else None
    parsed = parse_opening(extracted.text, buyer_nip=buyer_nip)
    if len(targets) == 1 and targets[0].facts.n_offers >= 2 and parsed.offers:
        check = _check_lots({targets[0].lot_no: list(parsed.offers)}, targets)
        if check.passed and not check.reasons:
            _store(conn, document_id, check.passed, "deterministic")
            return _finish(conn, document_id, PARSED, _parsed_error(check))

    try:
        llm = extract_with_llm(provider, extracted.text, n_lots=len(lots))
    except ValidationError as e:
        return _finish(conn, document_id, NEEDS_REVIEW,
                       f"llm_invalid: {e.error_count()} errors")
    check = _check_lots_best_numbering(_llm_groups(llm.offers, lots, buyer_nip), lots)
    if not check.passed:
        return _finish(conn, document_id, MISMATCH, ",".join(check.reasons))
    _store(conn, document_id, check.passed, "llm")
    return _finish(conn, document_id, PARSED, _parsed_error(check))


def process_opening_document(conn: psycopg.Connection, rawstore: RawStore,
                             provider: TextProvider, document_id: int) -> str:
    """Process one downloaded opening document; returns its new status
    (`parsed`, `mismatch` or `needs_review`) and commits."""
    try:
        status = _process(conn, rawstore, provider, document_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return status
