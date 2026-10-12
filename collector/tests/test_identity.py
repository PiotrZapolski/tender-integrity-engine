import json
from datetime import date

import httpx
import respx

from tie.bzp.store import upsert_bidder
from tie.http import PoliteClient
from tie.identity.bidders import resolve_unmatched_bids
from tie.identity.whitelist import WL_URL, enrich_bidders


class FakeJev:
    def __init__(self, choice, p):
        self.choice, self.p = choice, p

    def decide(self, *, task, state, questions):
        probs = {k: 0.0 for k in questions["match"]["criteria"]}
        probs[self.choice] = self.p
        return {"match": {"type": "choice", "choice": self.choice, "probabilities": probs,
                          "confidence": self.p}}


def seed_bid(db, raw_name):
    db.execute("insert into procedures(ocds_id, bzp_number, notice_number, title, published_at)"
               " values ('o','b','n','t', now())")
    lot = db.execute("insert into lots(procedure_id, lot_no, outcome) values ('o',1,'umowa')"
                     " returning id").fetchone()["id"]
    db.execute("insert into bids(lot_id, offer_no, bidder_name_raw, parse_method)"
               " values (%s,1,%s,'llm')", (lot, raw_name))
    db.commit()


def test_resolve_match(db):
    bid_id = upsert_bidder(db, name="MULTISERVICE Sp. z o.o.", nip="6791021905",
                           city="Gliwice", province="PL24")
    db.commit()
    seed_bid(db, "MULTISERVICE SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ")
    assert resolve_unmatched_bids(db, FakeJev("0", 0.93)) == 1
    row = db.execute("select bidder_id, match_confidence from bids").fetchone()
    assert row["bidder_id"] == bid_id and row["match_confidence"] > 0.9


def test_resolve_none_creates_new(db):
    upsert_bidder(db, name="MULTISERVICE Sp. z o.o.", nip="6791021905", city="Gliwice",
                  province="PL24")
    db.commit()
    seed_bid(db, "MULTISERWIS Jan Kowalski")
    assert resolve_unmatched_bids(db, FakeJev("none", 0.9)) == 0
    row = db.execute("select b.nip from bids x join bidders b on b.id=x.bidder_id").fetchone()
    assert row["nip"] is None


@respx.mock
def test_whitelist_enrich(db, fixtures):
    upsert_bidder(db, name="MULTISERVICE Sp. z o.o.", nip="6791021905", city=None, province=None)
    upsert_bidder(db, name="Usługi Remontowe Jan Testowy", nip="7010000005", city=None,
                  province=None)
    db.commit()
    route = respx.get(url__startswith=WL_URL.format(nips="")).mock(
        return_value=httpx.Response(
            200, json=json.loads((fixtures / "wl" / "batch_2.json").read_text("utf-8"))))
    n = enrich_bidders(db, PoliteClient("ua", {}, sleep=lambda s: None), daily_cap=10,
                       today=date(2026, 10, 12))
    assert n == 2
    co = db.execute("select * from bidders where nip='6791021905'").fetchone()
    assert co["krs"] == "0000047217" and co["is_jdg"] is False
    jdg = db.execute("select * from bidders where nip='7010000005'").fetchone()
    assert jdg["is_jdg"] is True
    assert db.execute("select count(*) c from bidder_accounts").fetchone()["c"] == 4
    assert db.execute("select requests from wl_checks").fetchone()["requests"] == 1
    assert route.call_count == 1
    assert "6791021905" in str(route.calls.last.request.url)


@respx.mock
def test_whitelist_daily_cap(db):
    upsert_bidder(db, name="A Sp. z o.o.", nip="6791021905", city=None, province=None)
    db.commit()
    db.execute("insert into wl_checks(day, requests) values ('2026-10-12', 10)")
    db.commit()
    n = enrich_bidders(db, PoliteClient("ua", {}, sleep=lambda s: None), daily_cap=10,
                       today=date(2026, 10, 12))
    assert n == 0
