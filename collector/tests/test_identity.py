import json
from datetime import date

import httpx
import pytest
import respx

from tie.ai.providers import BudgetExceeded, ProviderHttpError
from tie.bzp.store import upsert_bidder
from tie.handlers import HANDLERS, Context
from tie.http import HttpGiveUp, PoliteClient
from tie.identity.bidders import resolve_unmatched_bids
from tie.identity.names import normalize_name
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


WL_COMPANY = "MULTISERVICE SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ"


def seed_bids(db, rows):
    """rows: (bidder_name_raw, bidder_nip_raw); returns bid ids in order."""
    db.execute("insert into procedures(ocds_id, bzp_number, notice_number, title, published_at)"
               " values ('o','b','n','t', now())")
    lot = db.execute("insert into lots(procedure_id, lot_no, outcome) values ('o',1,'umowa')"
                     " returning id").fetchone()["id"]
    ids = [db.execute("insert into bids(lot_id, offer_no, bidder_name_raw, bidder_nip_raw,"
                      " parse_method) values (%s,%s,%s,%s,'deterministic') returning id",
                      (lot, i + 1, name, nip)).fetchone()["id"]
           for i, (name, nip) in enumerate(rows)]
    db.commit()
    return ids


def wl_mock(fixtures):
    return respx.get(url__startswith=WL_URL.format(nips="")).mock(
        return_value=httpx.Response(
            200, json=json.loads((fixtures / "wl" / "batch_2.json").read_text("utf-8"))))


@respx.mock
def test_nameless_bid_with_nip_gets_placeholder_then_whitelist_name(db, fixtures):
    upsert_bidder(db, name="Usługi Remontowe Jan Testowy", nip="7010000005", city=None,
                  province=None)
    db.commit()
    seed_bids(db, [("", "679-102-19-05")])
    assert resolve_unmatched_bids(db, FakeJev("none", 0.9)) == 0
    b = db.execute("select * from bidders where nip='6791021905'").fetchone()
    assert b["name"] == "NIP 6791021905"
    assert b["name_norm"] == normalize_name("NIP 6791021905")
    assert db.execute("select bidder_id from bids").fetchone()["bidder_id"] == b["id"]

    wl_mock(fixtures)
    assert enrich_bidders(db, PoliteClient("ua", {}, sleep=lambda s: None), daily_cap=10,
                          today=date(2026, 10, 12)) == 2
    b = db.execute("select * from bidders where nip='6791021905'").fetchone()
    assert b["name"] == WL_COMPANY and b["name_norm"] == normalize_name(WL_COMPANY)
    assert b["is_jdg"] is False
    # A real name is never overwritten by the whitelist name.
    jdg = db.execute("select * from bidders where nip='7010000005'").fetchone()
    assert jdg["name"] == "Usługi Remontowe Jan Testowy"
    assert jdg["name_norm"] == normalize_name("Usługi Remontowe Jan Testowy")


@respx.mock
def test_whitelist_fills_empty_name(db, fixtures):
    db.execute("insert into bidders(nip, name, name_norm, is_jdg) values"
               " ('6791021905', '', '', true)")
    db.commit()
    wl_mock(fixtures)
    enrich_bidders(db, PoliteClient("ua", {}, sleep=lambda s: None), daily_cap=10,
                   today=date(2026, 10, 12))
    b = db.execute("select * from bidders where nip='6791021905'").fetchone()
    assert b["name"] == WL_COMPANY and b["name_norm"] == normalize_name(WL_COMPANY)


POISON = "MULTISERVICE Sp. z o.o"
GOOD = "MULTISERVICE SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ"


class PoisonJev(FakeJev):
    """Raises for the bidder names in `poison`, answers like FakeJev for the rest."""

    def __init__(self, choice, p, poison=(POISON,), error=None):
        super().__init__(choice, p)
        self.poison = set(poison)
        self.error = error or RuntimeError("jev request failed: HTTP 422: bad state")
        self.calls = []

    def decide(self, *, task, state, questions):
        self.calls.append(state["oferent"])
        if state["oferent"] in self.poison:
            raise self.error
        return super().decide(task=task, state=state, questions=questions)


def seed_known_bidder(db):
    bidder_id = upsert_bidder(db, name="MULTISERVICE Sp. z o.o.", nip="6791021905",
                              city="Gliwice", province="PL24")
    db.commit()
    return bidder_id


def test_poison_bid_is_skipped_and_others_resolved(db, caplog):
    known = seed_known_bidder(db)
    poison_id, good_id = seed_bids(db, [(POISON, None), (GOOD, None)])
    jev = PoisonJev("0", 0.93)
    skip: set[int] = set()
    assert resolve_unmatched_bids(db, jev, skip=skip) == 1
    rows = {r["id"]: r for r in db.execute("select * from bids").fetchall()}
    assert rows[poison_id]["bidder_id"] is None
    assert rows[good_id]["bidder_id"] == known
    assert skip == {poison_id}
    assert f"bid {poison_id}" in caplog.text
    # Not retried in the same run.
    assert resolve_unmatched_bids(db, jev, skip=skip) == 0
    assert jev.calls == [POISON, GOOD]


def test_identity_job_completes_despite_poison_bid(db):
    known = seed_known_bidder(db)
    poison_id, good_id = seed_bids(db, [(POISON, None), (GOOD, None)])
    ctx = Context(conn=db, settings=None, client=PoliteClient("ua", {}, sleep=lambda s: None),
                  rawstore=None, text_provider=None, decider=PoisonJev("0", 0.93))
    HANDLERS["identity"](ctx, {})
    rows = {r["id"]: r for r in db.execute("select * from bids").fetchall()}
    assert rows[poison_id]["bidder_id"] is None
    assert rows[good_id]["bidder_id"] == known


def failures(db) -> dict[int, int]:
    rows = db.execute("select id, identity_failures from bids").fetchall()
    db.commit()
    return {r["id"]: r["identity_failures"] for r in rows}


def test_all_bids_failing_raises(db):
    seed_known_bidder(db)
    ids = seed_bids(db, [(POISON, None), (GOOD, None)])
    with pytest.raises(RuntimeError, match="all 2 bids failed"):
        resolve_unmatched_bids(db, PoisonJev("0", 0.93, poison=(POISON, GOOD)))
    # An outage must not mark good bids as poison.
    assert failures(db) == {ids[0]: 0, ids[1]: 0}


def test_failed_bid_counted_per_run_and_excluded_after_three(db):
    known = seed_known_bidder(db)
    poison_id, good_id = seed_bids(db, [(POISON, None), (GOOD, None)])
    jev = PoisonJev("0", 0.93)
    for run in range(1, 4):
        # A separate run (identity job) starts with an empty skip set.
        resolve_unmatched_bids(db, jev, skip=set())
        assert failures(db)[poison_id] == run
    assert jev.calls == [POISON, GOOD, POISON, POISON]
    assert resolve_unmatched_bids(db, jev, skip=set()) == 0
    assert jev.calls == [POISON, GOOD, POISON, POISON]
    rows = {r["id"]: r for r in db.execute("select * from bids").fetchall()}
    assert rows[poison_id]["bidder_id"] is None
    assert rows[good_id]["bidder_id"] == known
    # The identity job sees nothing left to do.
    ctx = Context(conn=db, settings=None, client=PoliteClient("ua", {}, sleep=lambda s: None),
                  rawstore=None, text_provider=None, decider=jev)
    HANDLERS["identity"](ctx, {})
    assert jev.calls == [POISON, GOOD, POISON, POISON]


def test_previously_failed_batch_increments_without_raising(db):
    seed_known_bidder(db)
    ids = seed_bids(db, [(POISON, None), (GOOD, None)])
    db.execute("update bids set identity_failures = 1")
    db.commit()
    jev = PoisonJev("0", 0.93, poison=(POISON, GOOD))
    assert resolve_unmatched_bids(db, jev, skip=set()) == 0
    assert failures(db) == {ids[0]: 2, ids[1]: 2}


def test_fresh_bids_come_first(db):
    seed_known_bidder(db)
    old_id, fresh_id = seed_bids(db, [(POISON, None), (GOOD, None)])
    db.execute("update bids set identity_failures = 1 where id = %s", (old_id,))
    db.commit()
    jev = PoisonJev("0", 0.93)
    assert resolve_unmatched_bids(db, jev, limit=1, skip=set()) == 1
    assert jev.calls == [GOOD]


def http_error(status):
    return ProviderHttpError("jev", status, f"jev request failed: HTTP {status}: no")


@pytest.mark.parametrize("error", [
    BudgetExceeded("limit"),
    RuntimeError("jev request failed: status=503"),
    http_error(401),
    http_error(403),
])
def test_budget_and_transport_errors_propagate(db, error):
    if type(error) is RuntimeError:
        error.__cause__ = HttpGiveUp("https://jev", 503)
    seed_known_bidder(db)
    ids = seed_bids(db, [(POISON, None), (GOOD, None)])
    db.execute("update bids set identity_failures = 1")
    db.commit()
    jev = PoisonJev("0", 0.93, error=error)
    with pytest.raises(type(error)):
        resolve_unmatched_bids(db, jev)
    assert jev.calls == [POISON]
    assert db.execute("select count(*) c from bids where bidder_id is not null"
                      ).fetchone()["c"] == 0
    assert failures(db) == {ids[0]: 1, ids[1]: 1}


def test_other_http_error_is_per_bid(db):
    seed_known_bidder(db)
    poison_id, _ = seed_bids(db, [(POISON, None), (GOOD, None)])
    jev = PoisonJev("0", 0.93, error=http_error(422))
    assert resolve_unmatched_bids(db, jev, skip=set()) == 1
    assert failures(db)[poison_id] == 1
