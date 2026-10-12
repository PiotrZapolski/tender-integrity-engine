"""Order of the VAT whitelist queue: the daily MF cap decides which firms get bank account
hashes and addresses first, so winners come first (most won lots), then bidders seen in
bids, then the rest."""

from tie.identity.whitelist import _pending


def _bidder(db, name, nip, checked=False):
    return db.execute(
        "insert into bidders(nip, name, name_norm, wl_checked_at)"
        " values (%s, %s, %s, case when %s then now() end) returning id",
        (nip, name, name.lower(), checked),
    ).fetchone()["id"]


def _lots(db, tag, winner, n):
    db.execute(
        "insert into procedures(ocds_id, bzp_number, notice_number, title, published_at)"
        " values (%s, %s, %s, 't', now())", (tag, tag, tag))
    ids = []
    for k in range(n):
        ids.append(db.execute(
            "insert into lots(procedure_id, lot_no, outcome, winner_bidder_id)"
            " values (%s, %s, 'umowa', %s) returning id", (tag, k + 1, winner),
        ).fetchone()["id"])
    return ids


def test_pending_orders_by_wins_then_bids_then_rest(db):
    rest = _bidder(db, "Reszta", "1000000001")
    bids_only = _bidder(db, "Tylko oferty", "1000000002")
    one_win = _bidder(db, "Jedna wygrana", "1000000003")
    three_wins = _bidder(db, "Trzy wygrane", "1000000004")
    three_wins_bids = _bidder(db, "Trzy wygrane i oferty", "1000000005")
    checked = _bidder(db, "Sprawdzony", "1000000006", checked=True)
    no_nip = _bidder(db, "Bez NIP", None)

    _lots(db, "p-one", one_win, 1)
    _lots(db, "p-three", three_wins, 3)
    lots = _lots(db, "p-three-bids", three_wins_bids, 3)
    _lots(db, "p-checked", checked, 5)
    _lots(db, "p-nonip", no_nip, 4)
    for offer_no, bidder in enumerate((bids_only, three_wins_bids), start=1):
        db.execute(
            "insert into bids(lot_id, offer_no, bidder_id, bidder_name_raw, parse_method)"
            " values (%s, %s, %s, 'x', 'synth')", (lots[0], offer_no, bidder))
    db.commit()

    order = [r["id"] for r in _pending(db, 10)]
    assert order == [three_wins_bids, three_wins, one_win, bids_only, rest]
    assert [r["id"] for r in _pending(db, 2)] == [three_wins_bids, three_wins]
