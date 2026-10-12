"""`tie validate`: run tests A, B and C, propose thresholds, store and report.

The run is written to `calibration_runs` (params = proposed thresholds and gate parameters,
metrics = results of the three tests and the gate, cases = public per-case rows) and read
by the frontend through `api.validation` (page "Sprawdziliśmy się").

Calibration (`propose_thresholds`): the probabilities of the control clusters of test C
decide the thresholds; the defaults of `tie.engine.decide.upsert_signals` are floors, so a
calibration can only make the engine stricter:
- `review_threshold`: the lowest value on a 0.01 grid (not below the default) with at most
  `REVIEW_FP_BUDGET` of the control clusters at or above it;
- `publish_threshold`: the same with `PUBLISH_FP_BUDGET`, and at least `review + 0.05`.
With fewer than `MIN_CONTROL` control clusters the defaults stay (`basis = "defaults"`).
`engine_max_status` stays `review`: only a person who has read the report may change
`ENGINE_MAX_STATUS` on the server.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, date, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from tie.engine import decide
from tie.validation.checks import (
    done_runs,
    engine_on_our_data,
    false_alarms,
    match_bidders,
    pick_run,
    positive_probabilities,
    screen_known_cartels,
)
from tie.validation.gold import (
    Firm,
    KioRuling,
    UokikCase,
    load_kio,
    load_uokik,
    public_firm_label,
)
from tie.validation.screens import TOP_SHARE, Backgrounds

_SIGNAL_DEFAULTS = inspect.signature(decide.upsert_signals).parameters
DEFAULT_PUBLISH = float(_SIGNAL_DEFAULTS["publish_threshold"].default)
DEFAULT_REVIEW = float(_SIGNAL_DEFAULTS["review_threshold"].default)
REVIEW_FP_BUDGET = 0.10
PUBLISH_FP_BUDGET = 0.01
MIN_CONTROL = 30
PUBLISH_GAP = 0.05
MAX_THRESHOLD = 0.99
GRID = [round(0.5 + 0.01 * i, 2) for i in range(50)]
# Spec gate: test A below 60% means the engine is improved before the public launch.
TEST_A_MIN_SHARE = 0.60
TEST_A_MIN_EVALUABLE = 20
DEFAULT_SAMPLE = 200
DEFAULT_SEED = 20261012
ENGINE_MAX_STATUS = "review"


def _share(values: list[float], t: float) -> float:
    return sum(1 for v in values if v >= t) / len(values) if values else 0.0


def propose_thresholds(control: list[float], positives: list[float], *,
                       default_publish: float = DEFAULT_PUBLISH,
                       default_review: float = DEFAULT_REVIEW) -> dict:
    """Thresholds from the control probabilities (test C), recall on the positives (B)."""
    if len(control) < MIN_CONTROL:
        review, publish, basis = default_review, default_publish, "defaults"
    else:
        review = next((t for t in GRID if t >= default_review
                       and _share(control, t) <= REVIEW_FP_BUDGET), MAX_THRESHOLD)
        floor = max(default_publish, review + PUBLISH_GAP)
        publish = next((t for t in GRID if t >= floor - 1e-9
                        and _share(control, t) <= PUBLISH_FP_BUDGET), MAX_THRESHOLD)
        publish = min(max(publish, round(floor, 2)), MAX_THRESHOLD)
        basis = "control_clusters"
    return {
        "review_threshold": round(review, 2),
        "publish_threshold": round(publish, 2),
        "basis": basis,
        "control_n": len(control),
        "positives_n": len(positives),
        "control_share_review": round(_share(control, review), 4) if control else None,
        "control_share_publish": round(_share(control, publish), 4) if control else None,
        "recall_review": round(_share(positives, review), 4) if positives else None,
        "recall_publish": round(_share(positives, publish), 4) if positives else None,
    }


def gate_params() -> dict:
    return {
        "top_share": TOP_SHARE,
        "test_a_min_share": TEST_A_MIN_SHARE,
        "test_a_min_evaluable": TEST_A_MIN_EVALUABLE,
        "review_fp_budget": REVIEW_FP_BUDGET,
        "publish_fp_budget": PUBLISH_FP_BUDGET,
        "min_control": MIN_CONTROL,
        "publish_max_item": decide.PUBLISH_MAX_ITEM,
        "publish_item_floor": decide.PUBLISH_ITEM_FLOOR,
        "publish_min_items": decide.PUBLISH_MIN_ITEMS,
        "top_items": decide.TOP_ITEMS,
    }


def evaluate_gate(test_a: dict, calibration: dict) -> dict:
    rigged = test_a["rigged_units"]
    if rigged["evaluable"] < TEST_A_MIN_EVALUABLE:
        a = "insufficient_data"
    else:
        a = "pass" if (rigged["share"] or 0.0) >= TEST_A_MIN_SHARE else "fail"
    if calibration["control_n"] < MIN_CONTROL:
        c = "insufficient_data"
    else:
        c = ("pass" if (calibration["control_share_publish"] or 0.0) <= PUBLISH_FP_BUDGET
             else "fail")
    return {
        "test_a": a,
        "test_a_share": rigged["share"],
        "test_a_evaluable": rigged["evaluable"],
        "test_c": c,
        "ready_for_human_review": a == "pass" and c == "pass",
        "engine_max_status": ENGINE_MAX_STATUS,
        "note": ("ENGINE_MAX_STATUS stays 'review' until a person approves publication"
                 " after reading this report."),
    }


def _firm_labels(conn: psycopg.Connection, firms: list[Firm]) -> list[str]:
    """Public labels of the named firms; `is_jdg` from our bidders when we know the firm."""
    jdg: dict[str, bool | None] = {}
    nips = sorted({f.nip for f in firms if f.nip})
    if nips:
        jdg = {r["nip"]: r["is_jdg"] for r in conn.execute(
            "select nip, is_jdg from bidders where nip = any(%s)", (nips,))}
        conn.commit()
    labels = []
    for f in firms:
        label = public_firm_label(f.name, f.nip, jdg.get(f.nip) if f.nip else None)
        if label and label not in labels:
            labels.append(label)
    return labels


def _engine_view(entry: dict) -> dict:
    keys = ("in_cluster", "probability", "status", "decision_source", "lead", "run_as_of",
            "matched", "co_bid_lots", "reason")
    return {k: entry.get(k) for k in keys}


def uokik_row(conn: psycopg.Connection, case: UokikCase, a: dict, b: dict) -> dict:
    detected = a["detected"]
    return {
        "source": "uokik",
        "key": case.decision,
        "name": f"Decyzja Prezesa UOKiK {case.decision}",
        "year": case.year,
        "sector": case.sector,
        "region": case.region,
        "decision": "upheld",
        "detected": detected,
        "status": "not_evaluable" if detected is None else ("detected" if detected else "missed"),
        "reason": a["reason"],
        "oecd_items": a["oecd_items"],
        "percentile": a["percentile"],
        "probability": b["probability"],
        "tenders": a["tenders"],
        "pairs": a["pairs"],
        "market": a["market"],
        "patterns": case.patterns,
        "engine": _engine_view(b),
        "firms": _firm_labels(conn, case.colluders),
    }


def kio_row(conn: psycopg.Connection, ruling: KioRuling, b: dict) -> dict:
    evaluable = b["run_id"] is not None and b["matched"] >= 2
    detected = b["in_cluster"] if evaluable else None
    return {
        "source": "kio",
        "key": ruling.signature,
        "name": f"Wyrok KIO {ruling.signature.removeprefix('KIO ').strip()}",
        "year": ruling.ruling_date.year if ruling.ruling_date else None,
        "sector": None,
        "region": None,
        "decision": ruling.decision,
        "detected": detected,
        "status": "not_evaluable" if detected is None else ("detected" if detected else "missed"),
        "reason": b["reason"],
        "oecd_items": b["oecd_items"],
        "percentile": None,
        "probability": b["probability"],
        "tenders": None,
        "pairs": None,
        "market": None,
        "patterns": [],
        "engine": {**_engine_view(b), "linked_procedure": ruling.ocds_id is not None},
        "firms": _firm_labels(conn, ruling.colluders),
    }


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def run_validation(conn: psycopg.Connection, *, as_of: date, sample: int = DEFAULT_SAMPLE,
                   seed: int = DEFAULT_SEED, store: bool = True) -> dict:
    """Run the whole validation and return the report (stored in `calibration_runs`)."""
    cases = load_uokik(conn)
    rulings = load_kio(conn)

    metrics_a, per_case_a = screen_known_cartels(cases, Backgrounds(conn, as_of))
    metrics_b, per_case_b, per_kio = engine_on_our_data(conn, cases, rulings, DEFAULT_REVIEW)

    known: set[int] = set()
    for firms in [c.colluders for c in cases] + [r.colluders for r in rulings]:
        known |= set(match_bidders(conn, firms))
    latest, _ = pick_run(done_runs(conn), None)
    metrics_c, control = false_alarms(
        conn, latest, {c.cpv for c in cases if c.cpv},
        sorted({r.ocds_id for r in rulings if r.ocds_id}), known, sample=sample, seed=seed,
        publish_threshold=DEFAULT_PUBLISH, review_threshold=DEFAULT_REVIEW)

    calibration = propose_thresholds(control, positive_probabilities(per_case_b, per_kio))
    gate = evaluate_gate(metrics_a, calibration)
    params = {
        "publish_threshold": calibration["publish_threshold"],
        "review_threshold": calibration["review_threshold"],
        "basis": calibration["basis"],
        "current": {"publish_threshold": DEFAULT_PUBLISH, "review_threshold": DEFAULT_REVIEW},
        "engine_max_status": ENGINE_MAX_STATUS,
        "gate": gate_params(),
        "as_of": as_of.isoformat(),
        "sample": sample,
        "seed": seed,
    }
    metrics = {
        "test_a": metrics_a,
        "test_b": metrics_b,
        "test_c": metrics_c,
        "calibration": calibration,
        "gate": gate,
    }
    rows = ([uokik_row(conn, c, per_case_a[c.decision], per_case_b[c.decision]) for c in cases]
            + [kio_row(conn, r, per_kio[r.signature]) for r in rulings])
    report = _jsonable({"params": params, "metrics": metrics, "cases": rows})
    report["calibration_run_id"] = None
    report["run_at"] = datetime.now(UTC).isoformat()
    if store:
        row = conn.execute(
            "insert into calibration_runs(params, metrics, cases) values (%s, %s, %s)"
            " returning id, run_at",
            (Jsonb(report["params"]), Jsonb(report["metrics"]), Jsonb(report["cases"]))
        ).fetchone()
        conn.commit()
        report["calibration_run_id"] = row["id"]
        report["run_at"] = row["run_at"].isoformat()
    return report


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def _num(x: Any) -> str:
    return "-" if x is None else str(x)


def markdown_summary(report: dict) -> str:
    """Short human summary of a report (no person names: firms are not listed)."""
    p, m = report["params"], report["metrics"]
    a, b, c = m["test_a"], m["test_b"], m["test_c"]
    cal, gate = m["calibration"], m["gate"]
    lines = [
        "# Walidacja silnika TIE",
        "",
        (f"Przebieg kalibracji {_num(report.get('calibration_run_id'))}, {report['run_at']},"
        f" dane do {p['as_of']}."),
        "",
        "## Bramka",
        "",
        (f"- Test A: {gate['test_a']} (udział w top 5%: {_pct(gate['test_a_share'])},"
        f" ocenione jednostki: {gate['test_a_evaluable']}, próg {_pct(TEST_A_MIN_SHARE)},"
        f" minimum {TEST_A_MIN_EVALUABLE})"),
        f"- Test C: {gate['test_c']}",
        "- Gotowe do przeglądu przez człowieka: "
        + ("tak" if gate["ready_for_human_review"] else "nie"),
        f"- ENGINE_MAX_STATUS: {gate['engine_max_status']} (zmienia tylko człowiek)",
        "",
        "## Test A: sita na znanych kartelach (UOKiK)",
        "",
        "| grupa | jednostek | ocenionych | w top 5% | udział |",
        "|---|---|---|---|---|",
    ]
    for label, key in (("przetargi ze zmową", "rigged_tenders"), ("pary zmowy", "rigged_pairs"),
                       ("razem (bramka)", "rigged_units"),
                       ("przetargi kontrolne", "control_tenders"),
                       ("pary kontrolne", "control_pairs")):
        r = a[key]
        lines.append(f"| {label} | {r['n']} | {r['evaluable']} | {r['top5']} |"
                     f" {_pct(r['share'])} |")
    lines += ["", "| punkt OECD | zmowa: top 5% | kontrola: top 5% | lift |", "|---|---|---|---|"]
    for item, r in a["items"].items():
        lines.append(f"| {item} | {_pct(r['rigged']['share'])} ({r['rigged']['evaluable']}) |"
                     f" {_pct(r['control']['share'])} ({r['control']['evaluable']}) |"
                     f" {_num(r['lift'])} |")
    pos = b["positives"]
    lines += [
        "",
        "## Test B: pełny silnik na naszych danych",
        "",
        f"- Przebiegi silnika: {b['engine_runs']}",
        (f"- Sprawy pozytywne (UOKiK + KIO utrzymane): {pos['n']}, ocenione {pos['evaluable']},"
        f" w jednym klastrze {pos['in_cluster']} (recall {_pct(pos['recall'])}),"
        f" z prawdopodobieństwem od progu przeglądu {pos['flagged']},"
        f" z wyprzedzeniem {pos['lead_hits']}"),
        (f"- KIO uchylone (negatywne): ocenione {b['kio_overturned']['evaluable']},"
        f" w jednym klastrze {b['kio_overturned']['in_cluster']}"),
        f"- Powody: {json.dumps(pos['reasons'], ensure_ascii=False, sort_keys=True)}",
        "",
        "## Test C: fałszywe alarmy",
        "",
    ]
    if c.get("run_id") is None:
        lines.append("- Brak zakończonego przebiegu silnika.")
    else:
        lines += [
            (f"- Klastry w próbie: {c['sampled']} z {c['candidates']} ({c['scope']}),"
            f" z sygnałem {c['with_signal']}"),
            (f"- Od progu przeglądu: {_pct(c['share_review'])}, od progu publikacji:"
            f" {_pct(c['share_publish'])}"),
        ]
    lines += [
        "",
        "## Proponowane parametry",
        "",
        f"- review_threshold: {p['review_threshold']} (obecnie {p['current']['review_threshold']})",
        (f"- publish_threshold: {p['publish_threshold']}"
        f" (obecnie {p['current']['publish_threshold']})"),
        (f"- podstawa: {p['basis']}, klastrów kontrolnych {cal['control_n']},"
        f" trafień B {cal['positives_n']}"),
        "",
        "## Sprawy",
        "",
        "| sprawa | rok | sektor | wykryta | punkty OECD | percentyl | silnik |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in report["cases"]:
        detected = {True: "tak", False: "nie", None: "nie do oceny"}[row["detected"]]
        engine = row["engine"]["reason"] or "-"
        lines.append(f"| {row['key']} | {_num(row['year'])} | {row['sector'] or '-'} |"
                     f" {detected} | {', '.join(row['oecd_items']) or '-'} |"
                     f" {_num(row['percentile'])} | {engine} |")
    return "\n".join(lines) + "\n"
