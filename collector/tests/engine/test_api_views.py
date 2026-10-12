"""Widoki `api.*` (kontrakt docs/contracts/api-v1.md) i rola `tie_reader`.

Wiersze silnika (rynek, klaster, wyniki testów, sygnały, opisy) wstawiamy wprost SQL-em na
syntetyczny rynek z kartelem rotacyjnym, więc test nie zależy od modułów obliczeniowych.
"""

import json
from datetime import date
from decimal import Decimal
from itertools import pairwise

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tie.engine.oecd import seed_catalog

from .synth import make_market

ROTATION = {"members": 3, "pattern": "rotation", "share": 0.4}
CPV = "45233142-6"
PROVINCE = "PL22"
MARKET_ID = "cpv5_prov:45233:PL22"
CLUSTER_ID = "c1a55e7d00000001"
REVIEW_ID = "c1a55e7d00000002"
JDG_MEMBER_NAME = "Kowalski Jan Zakład Remontowy"
HIDDEN_TITLE = "Tytuł sygnału do przeglądu"
OLD_TITLE = "Stary tytuł z poprzedniej wersji dowodów"

# Kolumny i typy (udt_name) dokładnie wg kontraktu.
CONTRACT = {
    "stats": [
        ("procedures", "int4"), ("lots", "int4"), ("bids", "int4"), ("bidders", "int4"),
        ("signals_published", "int4"), ("parse_ok_share", "numeric"),
        ("oecd_checked", "int4"), ("oecd_partial", "int4"), ("updated_at", "timestamptz"),
    ],
    "signals": [
        ("signal_id", "text"), ("title", "text"), ("market_label", "text"),
        ("cpv_group", "text"), ("province", "text"), ("period_from", "date"),
        ("period_to", "date"), ("probability", "numeric"), ("strength", "int4"),
        ("oecd_items", "_text"), ("n_procedures", "int4"), ("total_value", "numeric"),
        ("facts", "jsonb"), ("strip", "jsonb"), ("members", "jsonb"),
    ],
    "signal_detail": [
        ("signal_id", "text"), ("summary", "text"), ("findings", "jsonb"),
        ("evidence", "jsonb"), ("alternatives", "_text"), ("graph", "jsonb"),
    ],
    "bidders": [
        ("bidder_id", "text"), ("display_name", "text"), ("nip", "text"), ("is_jdg", "bool"),
        ("krs", "text"), ("province", "text"), ("n_bids", "int4"), ("n_wins", "int4"),
        ("total_won", "numeric"), ("partners", "jsonb"), ("signal_ids", "_text"),
    ],
    "buyers": [
        ("buyer_id", "text"), ("name", "text"), ("province", "text"),
        ("n_procedures", "int4"), ("single_offer_share", "numeric"), ("signal_ids", "_text"),
    ],
    "search": [
        ("kind", "text"), ("id", "text"), ("label", "text"), ("nip", "text"),
        ("search_text", "text"),
    ],
    "oecd_catalog": [
        ("item", "text"), ("section", "text"), ("title", "text"), ("status", "text"),
        ("how", "text"),
    ],
}

_FOLD = str.maketrans("ĄĆĘŁŃÓŚŹŻąćęłńóśźż", "ACELNOSZZacelnoszz")


def _fold(text: str) -> str:
    return text.translate(_FOLD).lower()


def _bidder(db, bidder_id):
    return db.execute("select * from bidders where id = %s", (bidder_id,)).fetchone()


@pytest.fixture
def world(db):
    """Rynek z kartelem rotacyjnym, JDG w kartelu, opublikowany sygnał i sygnał do przeglądu."""
    out = make_market(db, seed=1, n_firms=12, n_lots=80, cpv=CPV, province_code=PROVINCE,
                      cartel=ROTATION)
    seed_catalog(db)
    members = sorted(out["cartel"])
    jdg_member = members[0]
    db.execute(
        "update bidders set is_jdg = true, krs = null, name = %s where id = %s",
        (JDG_MEMBER_NAME, jdg_member),
    )
    for i, m in enumerate(members[1:], start=1):
        db.execute("update bidders set krs = %s where id = %s", (f"00001{i:05d}", m))
    outsider_jdg = out["jdg"][0]
    review_members = sorted(o for o in out["outsiders"] if o != outsider_jdg)[:2]

    run_id = db.execute(
        "insert into engine_runs(as_of, status, finished_at) values (%s, 'done', now())"
        " returning run_id",
        (out["as_of"],),
    ).fetchone()["run_id"]
    db.execute(
        "insert into markets(market_id, cpv_group, province, level, n_lots, run_id)"
        " values (%s, '45233', %s, 'cpv5_prov', %s, %s)",
        (MARKET_ID, PROVINCE, len(out["lots"]), run_id),
    )
    db.execute(
        "insert into lot_market(lot_id, market_id, run_id) select x, %s::text, %s::bigint"
        " from unnest(%s::bigint[]) x",
        (MARKET_ID, run_id, out["lots"]),
    )
    db.execute(
        "insert into clusters(run_id, cluster_id, market_id, members, n_lots, density)"
        " values (%s, %s, %s, %s, %s, 1.0), (%s, %s, %s, %s, 3, 1.0)",
        (run_id, CLUSTER_ID, MARKET_ID, members, len(out["cartel_lots"]),
         run_id, REVIEW_ID, MARKET_ID, review_members),
    )
    leaky = {"name": JDG_MEMBER_NAME, "bidder_id": jdg_member}
    db.execute(
        "insert into test_results(run_id, test_id, level, entity_key, market_id, value,"
        " percentile, n_obs, evidence) values"
        " (%s, '3.1.6', 'cluster', %s, %s, 0.93, 0.99, 40, %s),"
        " (%s, '3.1.1', 'firm', %s, %s, 0.30, 0.95, 12, %s)",
        (run_id, CLUSTER_ID, MARKET_ID, Jsonb(leaky),
         run_id, str(jdg_member), MARKET_ID, Jsonb(leaky)),
    )
    evidence = {
        "cluster_id": CLUSTER_ID,
        "members": [{"key": chr(65 + i), "bidder_id": m, "name": _bidder(db, m)["name"]}
                    for i, m in enumerate(members)],
    }
    db.execute(
        "insert into signals(signal_id, run_id, cluster_id, market_id, probability, strength,"
        " oecd, status, evidence_hash, evidence) values"
        " (%s, %s, %s, %s, 0.95, 4, %s, 'published', 'h-current', %s),"
        " (%s, %s, %s, %s, 0.70, 2, %s, 'review', 'h-review', %s)",
        (CLUSTER_ID, run_id, CLUSTER_ID, MARKET_ID,
         Jsonb({"3.1.6": 0.96, "3.3.6": 0.3, "3.2.11": 0.7}), Jsonb(evidence),
         REVIEW_ID, run_id, REVIEW_ID, MARKET_ID, Jsonb({"3.1.6": 0.7}), Jsonb({})),
    )
    rows = [{"key": chr(65 + i), "cells": ["win", "lose", None]} for i in range(3)]
    findings = [{
        "oecd": "3.1.6", "title": "Wygrywają na zmianę",
        "text": f"Firmy A, B i C, w tym {JDG_MEMBER_NAME}, wygrywają po kolei.",
        "probability": 0.96, "viz": "rotation", "data": {"rows": rows},
    }]
    db.execute(
        "insert into explanations(signal_id, evidence_hash, source, title, summary, findings,"
        " alternatives, created_at) values"
        " (%s, 'h-current', 'template', %s, %s, %s, %s, now()),"
        " (%s, 'h-old', 'template', %s, 'stary opis', '[]', '{}', now() - interval '1 day'),"
        " (%s, 'h-review', 'template', %s, 'opis', '[]', '{}', now())",
        (CLUSTER_ID, "3 firmy od 2023 roku wygrywają na zmianę",
         f"Trzy firmy, w tym {JDG_MEMBER_NAME.upper()}, startują razem i wygrywają po kolei.",
         Jsonb(findings), ["Mały rynek z niewieloma wykonawcami.",
                           f"Ograniczone moce firmy {JDG_MEMBER_NAME}."],
         CLUSTER_ID, OLD_TITLE, REVIEW_ID, HIDDEN_TITLE),
    )
    db.commit()
    return {
        **out,
        "members": members,
        "jdg_member": jdg_member,
        "jdg_member_nip": _bidder(db, jdg_member)["nip"],
        "outsider_jdg": outsider_jdg,
        "outsider_jdg_name": _bidder(db, outsider_jdg)["name"],
        "outsider_jdg_nip": _bidder(db, outsider_jdg)["nip"],
        "review_members": review_members,
        "run_id": run_id,
    }


def _rows(db, view):
    return db.execute(f"select * from api.{view}").fetchall()


def test_views_match_contract_columns_and_types(db, world):
    views = {r["table_name"] for r in db.execute(
        "select table_name from information_schema.views where table_schema = 'api'")}
    assert views == set(CONTRACT)
    for view, columns in CONTRACT.items():
        got = db.execute(
            "select column_name, udt_name from information_schema.columns"
            " where table_schema = 'api' and table_name = %s order by ordinal_position",
            (view,),
        ).fetchall()
        assert [(r["column_name"], r["udt_name"]) for r in got] == columns, view


def test_signals_has_one_published_row_with_contract_fields(db, world):
    rows = _rows(db, "signals")
    assert len(rows) == 1
    s = rows[0]
    n = len(world["cartel_lots"])
    assert s["signal_id"] == CLUSTER_ID
    assert s["title"] == "3 firmy od 2023 roku wygrywają na zmianę"
    assert s["market_label"] == "Roboty budowlane · pomorskie"
    assert s["cpv_group"] == "45233"
    assert s["province"] == "pomorskie"
    assert isinstance(s["period_from"], date) and s["period_from"] <= s["period_to"]
    assert s["probability"] == Decimal("0.95")
    assert s["strength"] == 4
    assert s["oecd_items"] == ["3.1.6", "3.2.11"]
    assert s["n_procedures"] == n
    expected_total = db.execute(
        "select sum(coalesce(contract_value, price_winner)) s from lots where id = any(%s)",
        (world["cartel_lots"],),
    ).fetchone()["s"]
    assert s["total_value"] == expected_total

    assert len(s["facts"]) == 3
    assert all(set(f) == {"value", "label"} and isinstance(f["value"], str)
               for f in s["facts"])
    assert s["facts"][0] == {"value": str(n), "label": "przetargów razem"}
    assert s["facts"][1] == {"value": "3", "label": "firmy w grupie"}

    assert len(s["strip"]) == min(24, n)
    winners = [x["winner"] for x in s["strip"]]
    assert set(winners) <= {"A", "B", "C"}
    # Rotacja: kolejne części wygrywa kolejny członek.
    assert all(a != b for a, b in pairwise(winners))

    members = s["members"]
    assert [m["key"] for m in members] == ["A", "B", "C"]
    assert [m["bidder_id"] for m in members] == [str(m) for m in world["members"]]
    assert all(set(m) == {"key", "bidder_id", "display_name", "nip"} for m in members)
    assert members[0]["display_name"] == f"NIP {world['jdg_member_nip']}"
    assert members[0]["nip"] == world["jdg_member_nip"]
    assert members[1]["display_name"] == _bidder(db, world["members"][1])["name"]


def test_signal_detail_uses_current_explanation_and_masks_texts(db, world):
    rows = _rows(db, "signal_detail")
    assert [r["signal_id"] for r in rows] == [CLUSTER_ID]
    d = rows[0]
    mask = f"NIP {world['jdg_member_nip']}"
    assert mask in d["summary"]
    assert "stary opis" not in d["summary"]
    assert d["findings"][0]["viz"] == "rotation"
    assert len(d["findings"][0]["data"]["rows"]) == 3
    assert mask in d["findings"][0]["text"]
    assert d["alternatives"][0] == "Mały rynek z niewieloma wykonawcami."
    assert d["alternatives"][1] == f"Ograniczone moce firmy {mask}."

    n = len(world["cartel_lots"])
    assert len(d["evidence"]) == n
    keys = {"date", "buyer", "subject", "winner_key", "n_offers", "bzp_url", "opening_url"}
    for e in d["evidence"]:
        assert set(e) == keys
        assert e["winner_key"] in {"A", "B", "C"}
        assert e["n_offers"] >= 3
        assert e["bzp_url"].startswith("https://ezamowienia.gov.pl/")
        assert e["buyer"].startswith("Gmina Testowa")
    assert [e["date"] for e in d["evidence"]] == sorted(e["date"] for e in d["evidence"])

    graph = d["graph"]
    assert graph["nodes"] == [{"key": k, "size": n} for k in "ABC"]
    assert graph["edges"] == [{"a": a, "b": b, "w": n}
                              for a, b in (("A", "B"), ("A", "C"), ("B", "C"))]


def test_jdg_name_never_appears_in_any_api_view(db, world):
    forbidden = [
        JDG_MEMBER_NAME, world["outsider_jdg_name"], "kowalski", "jan testowy",
        HIDDEN_TITLE, OLD_TITLE,
    ]
    forbidden = {_fold(f) for f in forbidden}
    views = [r["table_name"] for r in db.execute(
        "select table_name from information_schema.views where table_schema = 'api'")]
    assert len(views) == len(CONTRACT)
    seen = []
    for view in views:
        for row in _rows(db, view):
            text = _fold(json.dumps(row, default=str, ensure_ascii=False))
            seen.append(text)
            for f in forbidden:
                assert f not in text, (view, f)
    everything = "\n".join(seen)
    # Maskowanie zadziałało, a nie zniknęły wiersze: obie JDG są widoczne po NIP.
    assert f"nip {world['jdg_member_nip']}" in everything
    assert f"nip {world['outsider_jdg_nip']}" in everything


def test_bidders_mask_jdg_and_unknown_status_without_krs(db, world):
    outsiders = [o for o in world["outsiders"]
                 if o != world["outsider_jdg"] and o not in world["review_members"]]
    unknown_no_krs, unknown_with_krs = outsiders[0], outsiders[1]
    db.execute("update bidders set is_jdg = null, krs = null where id = %s", (unknown_no_krs,))
    db.execute("update bidders set is_jdg = null, krs = '0000999999' where id = %s",
               (unknown_with_krs,))
    db.commit()
    rows = {r["bidder_id"]: r for r in _rows(db, "bidders")}
    assert len(rows) == 12

    jdg = rows[str(world["jdg_member"])]
    assert jdg["display_name"] == f"NIP {world['jdg_member_nip']}"
    assert jdg["is_jdg"] is True and jdg["krs"] is None
    assert jdg["province"] == "pomorskie"
    n = len(world["cartel_lots"])
    assert jdg["n_bids"] == n
    wins = db.execute("select count(*) c, sum(contract_value) s from lots"
                      " where winner_bidder_id = %s", (world["jdg_member"],)).fetchone()
    assert jdg["n_wins"] == wins["c"]
    assert jdg["total_won"] == wins["s"]
    assert jdg["signal_ids"] == [CLUSTER_ID]
    top = {p["bidder_id"]: p["n_together"] for p in jdg["partners"][:2]}
    assert top == {str(m): n for m in world["members"][1:]}

    member = rows[str(world["members"][1])]
    assert member["display_name"] == _bidder(db, world["members"][1])["name"]
    assert member["krs"] is not None
    # Partner JDG widoczny w `partners` tylko jako NIP.
    assert {"bidder_id": str(world["jdg_member"]),
            "display_name": f"NIP {world['jdg_member_nip']}",
            "n_together": n} in member["partners"]

    assert rows[str(unknown_no_krs)]["display_name"].startswith("NIP ")
    assert rows[str(unknown_with_krs)]["display_name"] == _bidder(db, unknown_with_krs)["name"]
    # Sygnał w przeglądzie nie jest publiczny.
    for r in world["review_members"]:
        assert rows[str(r)]["signal_ids"] == []


def test_buyers_and_search(db, world):
    buyers = {r["buyer_id"]: r for r in _rows(db, "buyers")}
    assert len(buyers) == len(world["buyers"])
    cartel_buyers = {r["buyer_id"] for r in db.execute(
        "select distinct p.buyer_id from lots l join procedures p on p.ocds_id = l.procedure_id"
        " where l.id = any(%s)", (world["cartel_lots"],))}
    for bid, b in buyers.items():
        assert b["province"] == "pomorskie"
        assert b["single_offer_share"] == Decimal(0)
        assert b["signal_ids"] == ([CLUSTER_ID] if int(bid) in cartel_buyers else [])
    assert sum(b["n_procedures"] for b in buyers.values()) == 80

    nip = world["jdg_member_nip"]
    hits = db.execute("select * from api.search where search_text ilike %s",
                      (f"%{nip}%",)).fetchall()
    assert [(h["kind"], h["label"]) for h in hits] == [("bidder", f"NIP {nip}")]
    gminy = db.execute("select * from api.search where search_text ilike %s",
                       ("%gmina testowa%",)).fetchall()
    assert len(gminy) == len(world["buyers"]) and {h["kind"] for h in gminy} == {"buyer"}
    assert db.execute("select count(*) c from api.search where search_text ilike %s",
                      ("%kowalski%",)).fetchone()["c"] == 0


def test_stats_and_oecd_catalog(db, world):
    stats = _rows(db, "stats")
    assert len(stats) == 1
    s = stats[0]
    n_bids = db.execute("select count(*) c from bids").fetchone()["c"]
    assert (s["procedures"], s["lots"], s["bids"], s["bidders"]) == (80, 80, n_bids, 12)
    assert s["signals_published"] == 1
    assert s["parse_ok_share"] == Decimal(1)
    assert (s["oecd_checked"], s["oecd_partial"]) == (14, 12)
    assert s["updated_at"] is not None

    catalog = _rows(db, "oecd_catalog")
    assert len(catalog) == 51
    assert catalog[0]["item"] == "3.1.1"
    assert {r["status"] for r in catalog} == {"A", "C", "N"}


def test_mask_text_is_literal_and_case_insensitive(db):
    got = db.execute(
        "select tie_mask_text(%s, %s, %s) t",
        ("Wygrała JAN (KOWALSKI) S.C. oraz Jan (Kowalski) s.c.",
         ["Jan (Kowalski) s.c."], ["NIP 1234567890"]),
    ).fetchone()["t"]
    assert got == "Wygrała NIP 1234567890 oraz NIP 1234567890"
    untouched = db.execute(
        "select tie_mask_text('Jan Kowalski sc', array['Jan (Kowalski) s.c.'], array['X']) t"
    ).fetchone()["t"]
    assert untouched == "Jan Kowalski sc"


def test_reader_role_reads_only_api(db, world):
    role = db.execute("select rolcanlogin from pg_roles where rolname = 'tie_reader'").fetchone()
    assert role is not None and role["rolcanlogin"] is False
    db.execute("set role tie_reader")
    try:
        for view in CONTRACT:
            db.execute(f"select * from api.{view}").fetchall()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute("select * from bidders").fetchall()
    finally:
        db.rollback()
        db.execute("reset role")
        db.commit()
