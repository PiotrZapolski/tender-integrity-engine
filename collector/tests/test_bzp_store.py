import json

from tie.bzp.parser import parse_result_notice
from tie.bzp.store import upsert_bidder, upsert_notice


def notice(fixtures, name):
    return parse_result_notice(json.loads((fixtures / "bzp" / name).read_text("utf-8")))


def test_upsert_multilot_idempotent(db, fixtures):
    n = notice(fixtures, "result_multilot.json")
    out = upsert_notice(db, n, "sha1")
    assert out == {"ocds_id": n.ocds_id, "lots": 6, "needs_opening": False}
    upsert_notice(db, n, "sha1")
    assert db.execute("select count(*) c from lots").fetchone()["c"] == 6
    assert db.execute("select count(*) c from procedures").fetchone()["c"] == 1
    b = db.execute("select * from bidders where nip='6821618773'").fetchone()
    assert b["is_jdg"] is True
    assert db.execute("select count(*) c from bidders where nip='9930454088'").fetchone()["c"] == 1


def test_spytkowo_enqueues_documents(db, fixtures):
    n = notice(fixtures, "result_spytkowo.json")
    out = upsert_notice(db, n, "sha2")
    assert out["needs_opening"] is True
    job = db.execute("select * from jobs where kind='ezam_docs'").fetchone()
    assert job["key"] == n.ocds_id


def test_single_offer_does_not_enqueue(db, fixtures):
    out = upsert_notice(db, notice(fixtures, "result_single.json"), "sha3")
    assert out["needs_opening"] is False
    assert db.execute("select count(*) c from jobs").fetchone()["c"] == 0


def test_consortium_flag(db, fixtures):
    upsert_notice(db, notice(fixtures, "result_consortium.json"), "sha4")
    lot = db.execute("select consortium from lots order by lot_no limit 1").fetchone()
    assert lot["consortium"] is True


def test_reupsert_overwrites_values(db, fixtures):
    n = notice(fixtures, "result_multilot.json")
    upsert_notice(db, n, "old")
    db.execute("update lots set n_offers = 99")
    db.commit()
    upsert_notice(db, n, "new")
    proc = db.execute("select * from procedures").fetchone()
    assert proc["raw_sha256"] == "new"
    assert proc["notice_number"] == n.notice_number
    assert proc["platform"] == "ezamawiajacy" and proc["order_type"] == "Works"
    assert proc["cpv_main"] == n.cpv_all[0]
    assert db.execute("select count(*) c from buyers").fetchone()["c"] == 1
    lot1 = db.execute("select * from lots where lot_no = 1").fetchone()
    assert lot1["n_offers"] == 4
    assert str(lot1["price_winner"]) == "32997.39"
    w = db.execute("select nip from bidders where id = %s", (lot1["winner_bidder_id"],)).fetchone()
    assert w["nip"] == "6821618773"


def test_cancelled_lots_have_no_winner(db, fixtures):
    n = notice(fixtures, "result_cancelled.json")
    upsert_notice(db, n, "sha5")
    rows = db.execute("select outcome, winner_bidder_id from lots").fetchall()
    assert rows and all(r["outcome"] == "uniewaznienie" for r in rows)
    assert all(r["winner_bidder_id"] is None for r in rows)
    assert db.execute("select count(*) c from bidders").fetchone()["c"] == 0


def test_upsert_bidder_by_nip_keeps_is_jdg(db):
    a = upsert_bidder(db, name="Jan Kowalski Usługi", nip="6791021905", city="Gliwice",
                      province="śląskie")
    db.execute("update bidders set is_jdg = false where id = %s", (a,))
    b = upsert_bidder(db, name="Jan Kowalski Usługi Remontowe", nip="6791021905", city=None,
                      province=None)
    db.commit()
    assert a == b
    row = db.execute("select * from bidders where id = %s", (a,)).fetchone()
    assert row["is_jdg"] is False
    assert row["name"] == "Jan Kowalski Usługi Remontowe"
    assert row["name_norm"] == "jan kowalski uslugi remontowe"
    assert row["city"] == "Gliwice" and row["province"] == "śląskie"


def test_upsert_bidder_without_nip(db):
    a = upsert_bidder(db, name="MULTISERVICE Sp. z o.o.", nip=None, city="Gliwice", province=None)
    b = upsert_bidder(db, name="Multiservice Sp. z o. o.", nip=None, city="Gliwice",
                      province=None)
    c = upsert_bidder(db, name="MULTISERVICE Sp. z o.o.", nip=None, city=None, province=None)
    d = upsert_bidder(db, name="MULTISERVICE Sp. z o.o.", nip=None, city=None, province=None)
    db.commit()
    assert a == b and c == d and a != c
    row = db.execute("select * from bidders where id = %s", (a,)).fetchone()
    assert row["nip"] is None and row["is_jdg"] is False
