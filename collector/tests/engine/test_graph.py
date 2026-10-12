from datetime import UTC, date, datetime

from tie.engine.graph import build_pairs, cluster_key, find_clusters

from .synth import make_market

MARKET = "cpv5_prov:45233:14"


def _new_run(conn) -> int:
    row = conn.execute(
        "insert into engine_runs(as_of) values (%s) returning run_id", (date(2026, 1, 1),)
    ).fetchone()
    conn.commit()
    return row["run_id"]


def _assign_all(conn, run_id: int, market_id: str = MARKET) -> None:
    """Put every lot into one market (Task 2's assign_markets does this in production)."""
    conn.execute(
        "insert into markets(market_id, cpv_group, province, level, n_lots, run_id)"
        " select %s, '45233', '14', 'cpv5_prov', count(*), %s from lots"
        " on conflict (market_id) do update set n_lots = excluded.n_lots, run_id = excluded.run_id",
        (market_id, run_id),
    )
    conn.execute(
        "insert into lot_market(lot_id, market_id, run_id) select id, %s, %s from lots"
        " on conflict (lot_id) do update set market_id = excluded.market_id,"
        " run_id = excluded.run_id",
        (market_id, run_id),
    )
    conn.commit()


def _clusters(conn, run_id: int) -> list[dict]:
    return conn.execute(
        "select cluster_id, market_id, members, n_lots, density from clusters"
        " where run_id = %s order by cluster_id",
        (run_id,),
    ).fetchall()


def _small_market(conn) -> dict:
    """Four lots by hand: A, B, C bid; the lot of the unparsed document must be ignored."""
    buyer = conn.execute(
        "insert into buyers(nip, name, province) values ('1111111111', 'Gmina', '14')"
        " returning id"
    ).fetchone()["id"]
    ids = {}
    for key, address in (("A", "ul. Polna 1, Radom"), ("B", "  UL. POLNA 1,   radom "),
                         ("C", "ul. Leśna 2, Radom")):
        ids[key] = conn.execute(
            "insert into bidders(nip, name, name_norm, is_jdg, address)"
            " values (%s, %s, %s, false, %s) returning id",
            (f"52100000{ord(key)}", f"Firma {key}", f"firma {key.lower()}", address),
        ).fetchone()["id"]
    conn.execute(
        "insert into bidder_accounts(bidder_id, account_hash) values (%s, 'h-ac'), (%s, 'h-ac'),"
        " (%s, 'h-b')",
        (ids["A"], ids["C"], ids["B"]),
    )
    spec = [  # (bidders, winner, document status)
        (("A", "B", "C"), "A", "parsed"),
        (("A", "B"), "B", "parsed"),
        (("A", "C"), "C", "parsed"),
        (("A", "B"), "A", "new"),
    ]
    for k, (bidders, winner, status) in enumerate(spec, start=1):
        ocds = f"ocds-graph-{k}"
        conn.execute(
            "insert into procedures(ocds_id, bzp_number, notice_number, buyer_id, title,"
            " cpv_main, published_at) values (%s, %s, %s, %s, 'T', '45233000-9', %s)",
            (ocds, f"BZP {k}", f"N/{k}", buyer, datetime(2025, 1, k, tzinfo=UTC)),
        )
        lot = conn.execute(
            "insert into lots(procedure_id, lot_no, cpv, outcome, n_offers, winner_bidder_id)"
            " values (%s, 1, '45233000-9', 'umowa', %s, %s) returning id",
            (ocds, len(bidders), ids[winner]),
        ).fetchone()["id"]
        doc = conn.execute(
            "insert into documents(procedure_id, source, source_doc_id, file_name, doc_type,"
            " status) values (%s, 'test', %s, 'o.pdf', 'opening', %s) returning id",
            (ocds, f"d{k}", status),
        ).fetchone()["id"]
        for n, b in enumerate(bidders, start=1):
            conn.execute(
                "insert into bids(lot_id, document_id, offer_no, bidder_id, bidder_name_raw,"
                " price, is_winner, parse_method) values (%s, %s, %s, %s, %s, %s, %s, 'test')",
                (lot, doc, n, ids[b], f"Firma {b}", 1000 + n, b == winner),
            )
    conn.commit()
    return ids


def test_build_pairs_counts_and_shared_data(db):
    ids = _small_market(db)
    run_id = _new_run(db)
    _assign_all(db, run_id)

    assert build_pairs(db, run_id) == 2
    assert build_pairs(db, run_id) == 2  # rerun replaces, no duplicates
    rows = db.execute(
        "select a, b, market_id, n_together, a_wins, b_wins, other_wins, shared_account,"
        " shared_address from pairs where run_id = %s order by a, b",
        (run_id,),
    ).fetchall()
    got = {(r["a"], r["b"]): r for r in rows}
    a, b, c = ids["A"], ids["B"], ids["C"]
    ab = got[(min(a, b), max(a, b))]
    ac = got[(min(a, c), max(a, c))]
    assert set(got) == {(min(a, b), max(a, b)), (min(a, c), max(a, c))}  # B-C met once
    assert (ab["n_together"], ab["other_wins"]) == (2, 0)
    assert sorted([ab["a_wins"], ab["b_wins"]]) == [1, 1]
    assert ab["market_id"] == MARKET
    assert ab["shared_address"] is True and ab["shared_account"] is False
    assert (ac["n_together"], ac["a_wins"], ac["b_wins"], ac["other_wins"]) == (2, 1, 1, 0)
    assert ac["shared_account"] is True and ac["shared_address"] is False


def test_rotation_cartel_is_one_cluster(db):
    m = make_market(db, seed=1, n_firms=12, n_lots=80, cpv="45233000-9", province_code="14",
                    cartel={"members": 3, "pattern": "rotation", "share": 0.4})
    run_id = _new_run(db)
    _assign_all(db, run_id)
    build_pairs(db, run_id)
    find_clusters(db, run_id)

    cartel = sorted(m["cartel"])
    clusters = _clusters(db, run_id)
    with_members = [c for c in clusters if set(c["members"]) & set(cartel)]
    assert len(with_members) == 1, clusters
    found = with_members[0]
    assert sorted(found["members"]) == cartel
    assert found["n_lots"] >= len(m["cartel_lots"])
    assert found["density"] > 0.8
    assert found["cluster_id"] == cluster_key(MARKET, cartel)
    for c in clusters:
        assert 2 <= len(c["members"]) <= 12


def test_no_cartel_gives_no_dense_triple(db):
    make_market(db, seed=1, n_firms=12, n_lots=80, cpv="45233000-9", province_code="14")
    run_id = _new_run(db)
    _assign_all(db, run_id)
    build_pairs(db, run_id)
    find_clusters(db, run_id)

    for c in _clusters(db, run_id):
        if len(c["members"]) >= 3:
            assert not (c["density"] > 0.8 and c["n_lots"] >= 20), c


def test_cluster_id_stable_between_runs(db):
    make_market(db, seed=1, n_firms=12, n_lots=80, cpv="45233000-9", province_code="14",
                cartel={"members": 3, "pattern": "rotation", "share": 0.4})
    first = _new_run(db)
    _assign_all(db, first)
    build_pairs(db, first)
    n_first = find_clusters(db, first)
    assert n_first >= 1
    assert find_clusters(db, first) == n_first  # rerun in the same run replaces
    assert len(_clusters(db, first)) == n_first

    second = _new_run(db)
    _assign_all(db, second)
    build_pairs(db, second)
    find_clusters(db, second)

    def ids(run_id):
        return [(c["cluster_id"], sorted(c["members"])) for c in _clusters(db, run_id)]

    assert ids(first) == ids(second)
    assert all(len(c["cluster_id"]) == 16 for c in _clusters(db, first))


def test_cluster_key_order_independent():
    assert cluster_key("m", [3, 1, 2]) == cluster_key("m", [1, 2, 3])
    assert cluster_key("m", [1, 2, 3]) != cluster_key("n", [1, 2, 3])
    assert len(cluster_key("m", [1, 2])) == 16
