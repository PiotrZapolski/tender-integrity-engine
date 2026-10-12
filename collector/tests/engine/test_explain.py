from dataclasses import dataclass, field

from psycopg.types.json import Jsonb

from tie.ai.providers import BudgetExceeded
from tie.engine.explain import (
    ALT_CAPACITY,
    ALT_COMMON_ESTIMATE,
    ALT_GROUP,
    ALT_SMALL_MARKET,
    allowed_numbers,
    explain_signal,
    numbers_ok,
    template_explanation,
)

A, B, C, OUTSIDER = 101, 102, 103, 999
KEYS = {A: "A", B: "B", C: "C"}
DASHES = (chr(0x2014), chr(0x2013))


@dataclass
class FakeDecision:
    probability: float = 0.95
    strength: float = 4.0
    per_item: dict = field(default_factory=lambda: {"3.1.6": 0.95, "3.3.7": 0.7, "3.1.1": 0.2})
    source: str = "jev"


class FakeProvider:
    def __init__(self, payload=None, exc=None):
        self.payload = payload
        self.exc = exc
        self.calls = 0
        self.last = None

    def complete_json(self, *, task, system, user):
        self.calls += 1
        self.last = {"task": task, "system": system, "user": user}
        if self.exc is not None:
            raise self.exc
        return self.payload


def lot_plan():
    """12 lots, members win in turn A, B, C; losers 6% and 7% above the winner."""
    order = [A, B, C]
    for i in range(12):
        year, month = 2021 + (2 + i) // 12, (2 + i) % 12 + 1
        winner = order[i % 3]
        losers = [m for m in order if m != winner]
        if i == 4:  # C did not bid in this lot, an outsider did
            losers = [A, OUTSIDER]
        base = 100000 + i * 1000
        offers = [(winner, base)] + [(loser, round(base * f, 2))
                                     for f, loser in zip((1.06, 1.07), losers)]
        yield i, f"{year}-{month:02d}-15", winner, base, offers


def rotation_evidence(with_offers=False):
    """Evidence package in the shape built by tie.engine.evidence.build_evidence."""
    lots = []
    for i, day, winner, base, offers in lot_plan():
        lot = {"id": 5000 + i, "date": day, "price": float(base), "est": round(base * 1.1, 2),
               "winner": KEYS[winner], "bidders": [KEYS[b] for b, _ in offers if b in KEYS],
               "n_offers": len(offers)}
        if with_offers:
            lot["offers"] = [{"bidder_id": b, "price": f"{p:.2f}"} for b, p in offers
                             if b in KEYS]
        lots.append(lot)
    return {
        "cluster_id": "c0ffee0123456789",
        "market": {"market_id": "cpv5_prov:45214:PL30", "cpv_group": "45214",
                   "province": "PL30", "level": "cpv5_prov", "n_lots": 140},
        "members": [{"key": "A", "bidder_id": A}, {"key": "B", "bidder_id": B},
                    {"key": "C", "bidder_id": C}],
        "n_lots": 27,
        "density": 1.0,
        "n_procedures": 27,
        "total_value": 2950000.0,
        "period": {"from": "2021-03-15", "to": "2024-06-30"},
        "oecd": {
            "3.1.1": {"level": "firm", "value": 0.33, "pct": 0.6, "n": 3,
                      "top": [{"e": "A", "v": 0.33, "p": 0.6}]},
            "3.1.6": {"level": "pair", "value": 0.93, "pct": 0.98, "n": 3,
                      "top": [{"e": "A-C", "v": 0.93, "p": 0.98},
                              {"e": "A-B", "v": 0.91, "p": 0.97}]},
            "3.3.7": {"level": "lot", "value": 2.4, "pct": 0.95, "n": 27, "mean": 2.5,
                      "high": 22},
        },
        "lots": lots,
    }


def no_dashes(exp):
    texts = [exp.title, exp.summary, *exp.alternatives, *(f["text"] for f in exp.findings),
             *(f["title"] for f in exp.findings)]
    return all(d not in t for t in texts for d in DASHES)


def test_template_for_rotation_cluster_has_lot_count_and_year():
    exp = template_explanation(rotation_evidence(), FakeDecision())
    assert exp.title == "3 firmy wygrywają przetargi na zmianę"
    assert exp.summary.startswith("3 firmy od 2021 roku wygrywają na zmianę: 27 przetargów razem.")
    assert "województwo wielkopolskie" in exp.summary
    assert "2 950 000 zł" in exp.summary
    assert "W ostatnich 12 wspólnych przetargach wygrywały: firma A 4, firma B 4, firma C 4." \
        in exp.summary
    assert [f["oecd"] for f in exp.findings] == ["3.1.6", "3.3.7"]
    assert exp.findings[0]["probability"] == 0.95
    assert exp.findings[0]["viz"] == "rotation"
    assert "0,93" in exp.findings[0]["text"] and "98%" in exp.findings[0]["text"]
    assert ALT_CAPACITY in exp.alternatives and ALT_SMALL_MARKET in exp.alternatives
    assert ALT_COMMON_ESTIMATE in exp.alternatives
    assert no_dashes(exp)


def test_template_is_deterministic():
    assert template_explanation(rotation_evidence(), FakeDecision()) == template_explanation(
        rotation_evidence(), FakeDecision())


def test_rotation_viz_has_row_per_member():
    exp = template_explanation(rotation_evidence(), FakeDecision())
    data = exp.findings[0]["data"]
    assert [r["key"] for r in data["rows"]] == ["A", "B", "C"]
    for row in data["rows"]:
        assert len(row["cells"]) == 12
        assert set(row["cells"]) <= {"win", "lose", None}
    rows = {r["key"]: r["cells"] for r in data["rows"]}
    assert rows["A"][0] == "win" and rows["B"][0] == "lose" and rows["C"][0] == "lose"
    assert rows["B"][4] == "win" and rows["C"][4] is None
    assert sum(c == "win" for r in rows.values() for c in r) == 12


def test_ladder_viz_marks_pct_of_winning_price():
    exp = template_explanation(rotation_evidence(with_offers=True), FakeDecision())
    assert exp.findings[1]["viz"] == "ladder"
    ladder = exp.findings[1]["data"]
    assert len(ladder["rows"]) == 12
    first = ladder["rows"][0]
    assert first["label"] == "03.2021"
    assert {m["key"]: m["pct"] for m in first["marks"]} == {"A": 100.0, "B": 106.0, "C": 107.0}


def test_price_finding_without_offer_prices_falls_back_to_graph():
    exp = template_explanation(rotation_evidence(), FakeDecision())
    assert exp.findings[1]["oecd"] == "3.3.7"
    assert exp.findings[1]["viz"] == "graph"


def test_shared_data_pattern_uses_graph_and_own_alternatives():
    ev = rotation_evidence()
    ev["oecd"] = {"3.2.11": {"level": "pair", "value": 1, "pct": None, "n": 1}}
    exp = template_explanation(ev, None)
    assert exp.title == "3 firmy konkurują, choć mają wspólne dane"
    assert exp.findings[0]["oecd"] == "3.2.11"
    assert exp.findings[0]["viz"] == "graph"
    graph = exp.findings[0]["data"]
    assert graph["nodes"] == [{"key": "A", "size": 12}, {"key": "B", "size": 12},
                              {"key": "C", "size": 11}]
    edges = {(e["a"], e["b"]): e["w"] for e in graph["edges"]}
    assert edges == {("A", "B"): 12, ("A", "C"): 11, ("B", "C"): 11}
    assert ALT_GROUP in exp.alternatives
    assert ALT_CAPACITY not in exp.alternatives
    assert no_dashes(exp)


def test_item_whose_test_says_no_signal_is_not_a_finding():
    ev = rotation_evidence()
    ev["oecd"]["3.3.10"] = {"level": "cluster", "value": -0.02, "pct": 0.99, "n": 1,
                            "signal": False}
    exp = template_explanation(ev, None)
    assert [f["oecd"] for f in exp.findings] == ["3.1.6", "3.3.7"]


def test_number_guard():
    allowed = allowed_numbers(rotation_evidence())
    assert numbers_ok("3 firmy od 2021 roku, 27 przetargów, wskaźnik 0,93, 98%.", allowed)
    assert numbers_ok("Łącznie 2 950 000 zł.", allowed)
    assert not numbers_ok("Aż 137 przetargów.", allowed)
    assert not numbers_ok("Firma 101 wygrywa.", allowed)  # bidder ids are not facts
    assert not numbers_ok("Część 5003.", allowed)  # nor lot ids


# --- database: explain_signal ------------------------------------------------------------

def seed_bids(db):
    """Procedure, parsed opening document, bidders, the 12 lots and their offers."""
    db.execute("insert into procedures(ocds_id, bzp_number, notice_number, title, published_at)"
               " values ('ocds-t8', 'BZP-1', 'N-1', 'Remonty', '2021-01-01')")
    doc = db.execute(
        "insert into documents(procedure_id, source, source_doc_id, file_name, doc_type, status)"
        " values ('ocds-t8', 'ezamowienia', 'd1', 'o.pdf', 'opening', 'parsed') returning id"
    ).fetchone()["id"]
    for bidder_id in (A, B, C, OUTSIDER):
        db.execute("insert into bidders(id, nip, name, name_norm, is_jdg)"
                   " values (%s, %s, %s, %s, false)",
                   (bidder_id, f"77800{bidder_id:05d}", f"Firma {bidder_id}", f"firma {bidder_id}"))
    for i, day, winner, base, offers in lot_plan():
        db.execute("insert into lots(id, procedure_id, lot_no, outcome, price_winner,"
                   " contract_date, winner_bidder_id) values (%s, 'ocds-t8', %s, 'awarded',"
                   " %s, %s, %s)", (5000 + i, i + 1, base, day, winner))
        for no, (bidder_id, price) in enumerate(offers):
            # A's offer in lot 1 lies outside the BZP price range: not a valid offer
            outside = i == 1 and bidder_id == A
            db.execute("insert into bids(lot_id, document_id, offer_no, bidder_id,"
                       " bidder_name_raw, price, is_winner, parse_method, outside_bzp_range)"
                       " values (%s, %s, %s, %s, %s, %s, %s, 'table', %s)",
                       (5000 + i, doc, no + 1, bidder_id, f"Firma {bidder_id}", price,
                        bidder_id == winner, outside))
    db.commit()


def insert_signal(db, *, status="published", signal_id="sig1", evidence_hash="hash1",
                  evidence=None):
    db.execute(
        "insert into signals(signal_id, run_id, cluster_id, market_id, probability, strength,"
        " oecd, status, evidence_hash, evidence) values (%s, 1, %s, %s, 0.95, 4, %s, %s, %s, %s)"
        " on conflict (signal_id) do update set evidence_hash = excluded.evidence_hash,"
        " evidence = excluded.evidence, status = excluded.status",
        (signal_id, signal_id, "cpv5_prov:45214:PL30",
         Jsonb({"3.1.6": 0.95, "3.3.7": 0.7, "3.1.1": 0.2}), status, evidence_hash,
         Jsonb(evidence or rotation_evidence())),
    )
    db.commit()


def explanation_rows(db, signal_id="sig1"):
    return db.execute("select * from explanations where signal_id = %s"
                      " order by created_at, evidence_hash", (signal_id,)).fetchall()


def test_deepseek_answer_with_number_outside_evidence_keeps_template(db):
    insert_signal(db)
    provider = FakeProvider({"title": "3 firmy wygrywają na zmianę",
                             "summary": "Od 2021 roku aż 137 przetargów wygrały te firmy."})
    assert explain_signal(db, provider, "sig1") == "template"
    assert provider.calls == 1
    assert provider.last["task"] == "signal_explain"
    [row] = explanation_rows(db)
    assert row["source"] == "template"
    assert row["evidence_hash"] == "hash1"
    assert row["title"] == "3 firmy wygrywają przetargi na zmianę"
    assert "27 przetargów" in row["summary"]
    assert row["findings"][0]["viz"] == "rotation"
    assert ALT_CAPACITY in row["alternatives"]


def test_ladder_is_built_from_valid_member_bids(db):
    seed_bids(db)
    insert_signal(db, status="review")
    assert explain_signal(db, None, "sig1") == "template"
    [row] = explanation_rows(db)
    ladder = row["findings"][1]
    assert ladder["oecd"] == "3.3.7" and ladder["viz"] == "ladder"
    rows = ladder["data"]["rows"]
    assert len(rows) == 12
    assert {m["key"]: m["pct"] for m in rows[0]["marks"]} == {"A": 100.0, "B": 106.0, "C": 107.0}
    # lot 1: A's offer outside the BZP range is left out
    assert {m["key"]: m["pct"] for m in rows[1]["marks"]} == {"B": 100.0, "C": 107.0}
    # lot 4: the outsider's offer is not a member mark
    assert {m["key"] for m in rows[4]["marks"]} == {"A", "B"}


def test_valid_deepseek_rewrite_is_saved_and_not_repeated(db):
    ev = rotation_evidence()
    ev["members"][2]["name"] = "Jan Kowalski Usługi Remontowe"
    insert_signal(db, evidence=ev)
    title = "Trzy firmy " + chr(0x2014) + " wygrywają po kolei"
    provider = FakeProvider({"title": title,
                             "summary": "Od 2021 roku 3 firmy wygrywają na zmianę w 27 "
                                        "przetargach. Każda ma po 4 wygrane w ostatnich 12."})
    assert explain_signal(db, provider, "sig1") == "deepseek"
    [row] = explanation_rows(db)
    assert row["source"] == "deepseek"
    assert row["title"] == "Trzy firmy - wygrywają po kolei"
    assert row["summary"].startswith("Od 2021 roku 3 firmy")
    assert row["findings"][0]["oecd"] == "3.1.6"
    # bidder names never go to the model
    assert "Kowalski" not in provider.last["user"]
    assert "27" in provider.last["user"]

    assert explain_signal(db, provider, "sig1") == "deepseek"
    assert provider.calls == 1

    insert_signal(db, evidence_hash="hash2", evidence=ev)
    assert explain_signal(db, provider, "sig1") == "deepseek"
    assert provider.calls == 2
    assert [r["evidence_hash"] for r in explanation_rows(db)] == ["hash1", "hash2"]


def test_signal_in_review_gets_template_without_model(db):
    insert_signal(db, status="review")
    provider = FakeProvider({"title": "x", "summary": "y"})
    assert explain_signal(db, provider, "sig1") == "template"
    assert provider.calls == 0
    assert explanation_rows(db)[0]["source"] == "template"


def test_provider_failure_or_no_provider_keeps_template(db):
    insert_signal(db)
    provider = FakeProvider(exc=BudgetExceeded("limit"))
    assert explain_signal(db, provider, "sig1") == "template"
    assert provider.calls == 1

    insert_signal(db, signal_id="sig2")
    assert explain_signal(db, None, "sig2") == "template"
