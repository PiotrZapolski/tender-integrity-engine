"""Decyzja o sygnale klastra: Jev ze skalibrowanym prawdopodobieństwem per punkt OECD,
regresja logistyczna jako wariant zapasowy, zapis sygnałów z progami publikacji.

Arytmetyka zostaje w kodzie: Jev dostaje pakiet dowodów (`tie.engine.evidence`) jako `state`,
ale tylko z punktami obecnymi (`present_items`: z percentylem albo flaga/liczba zdarzeń > 0),
i dla każdego z nich pytanie `noul` z liczbami tego punktu, plus `score` siły sygnału
w 5 poziomach. Łączne prawdopodobieństwo liczymy w kodzie, z 3 najsilniejszych punktów:
`p = 1 - prod(1 - p_i * w_i)`, `w_i` = 1 dla punktów A i 0.6 dla C.

Bramka publikacji (ochrona przed fałszywym alarmem): status `published` wymaga decyzji Jev
z `p >= publish_threshold`, najsilniejszego punktu z `p_i >= 0.8` i co najmniej dwóch punktów
z `p_i >= 0.6`. Ponad to status nigdy nie przekracza `ENGINE_MAX_STATUS` (domyślnie `review`,
więc bez jawnej zgody operatora nic nie jest publikowane).

Błędy Jev: odpowiedź 4xx inna niż 429 (zły klucz, zły kształt zapytania, zła nazwa modelu)
wyłącza Jev do końca przebiegu, tak samo przekroczony budżet i kilka kolejnych błędów innego
rodzaju; reszta klastrów idzie regresją zapasową, a powód trafia do
`engine_runs.params.jev_disabled_reason`.

Wiersz `signals`: `signal_id` = `cluster_id`, `oecd` = mapa punkt -> prawdopodobieństwo
(dla Jev noul, dla regresji wejście testu 0..1), `evidence` = pakiet dowodów z dodatkowym
kluczem `decision` (`source`, `strength`), który nie wchodzi do `evidence_hash`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from tie.ai.providers import BudgetExceeded, ProviderHttpError
from tie.config import ENGINE_STATUSES, engine_max_status
from tie.engine.evidence import CHECKED, build_evidence, evidence_hash, present_items
from tie.engine.oecd import CATALOG

log = logging.getLogger(__name__)

TASK = "signal_decide"
ITEM_WEIGHTS = {"A": 1.0, "C": 0.6}
STRENGTH_KEY = "strength"
STRENGTH_LEVELS = [
    "brak sygnału",
    "słaby sygnał",
    "umiarkowany sygnał",
    "silny sygnał",
    "bardzo silny sygnał",
]
# Łączne prawdopodobieństwo z tylu najsilniejszych punktów (więcej słabych punktów nie
# może dać razem wysokiego wyniku).
TOP_ITEMS = 3
# Bramka publikacji: najsilniejszy punkt co najmniej PUBLISH_MAX_ITEM i co najmniej
# PUBLISH_MIN_ITEMS punktów z prawdopodobieństwem co najmniej PUBLISH_ITEM_FLOOR.
PUBLISH_MAX_ITEM = 0.8
PUBLISH_ITEM_FLOOR = 0.6
PUBLISH_MIN_ITEMS = 2
# Regresja zapasowa: stałe wagi na wejściach testów (percentyl 0..1), wyraz wolny.
FALLBACK_WEIGHTS = {
    "3.1.6": 2.0,
    "3.3.6": 2.0,
    "3.3.7": 1.5,
    "3.1.2": 1.5,
    "3.2.11": 2.5,
    "3.1.5": 1.0,
    "3.3.9": 0.8,
    "3.3.10": 1.0,
}
FALLBACK_BIAS = -4.0
# Testy zero-jedynkowe bez percentyla: wejściem jest sama wartość.
BINARY_ITEMS = {"3.2.11", "3.3.5"}
# 3.3.10 bez percentyla: sygnałem jest spadek cen o więcej niż 10% po wejściu nowego oferenta.
ENTRY_DROP = -0.10
SOURCE_JEV = "jev"
SOURCE_FALLBACK = "fallback"
SOURCE_EMPTY = "empty"
# Po tylu kolejnych błędach innych niż HTTP (sieć, zła odpowiedź) Jev jest wyłączany.
MAX_CONSECUTIVE_ERRORS = 5
MAX_BODY_LOG = 500

_TITLES = {c.item: c.title for c in CATALOG}
_HOW = {c.item: c.how for c in CATALOG}


class Decider(Protocol):
    def decide(self, *, task: str, state: Any, questions: dict) -> dict: ...


@dataclass(frozen=True)
class Decision:
    probability: float
    strength: float
    per_item: dict[str, float]
    source: str


def question_key(item: str) -> str:
    return "p_" + item.replace(".", "_")


def _noul_question(item: str) -> dict:
    status = "sprawdzany automatycznie" if CHECKED[item] == "A" else "sprawdzany częściowo"
    return {
        "type": "noul",
        "instructions": {
            "pytanie": (
                f"Czy liczby w polu `liczby` wskazują, że firmy z klastra (pole `members` stanu)"
                f" wykazują sygnał zmowy przetargowej z listy OECD {item}:"
                f" \"{_TITLES[item]}\"?"
            ),
            "jak_liczymy": f"{_HOW[item]} (punkt {status})",
            "objasnienie": (
                "`pct` to percentyl w obrębie tego samego rynku (1.0 = wartość najbardziej"
                " podejrzana w rynku, null = za mało obserwacji), `value` to wynik testu,"
                " `n` to liczba ocenianych encji, `top` to najsilniejsze encje (klucz członka"
                " albo para członków)."
            ),
            "liczby": None,
        },
    }


# Szablony pytań: `noul` dla każdego punktu A/C katalogu, `score` siły sygnału.
# `build_questions` wybiera punkty obecne w dowodach i wstawia ich liczby w pole `liczby`.
QUESTIONS: dict[str, dict] = {
    **{question_key(item): _noul_question(item) for item in CHECKED},
    STRENGTH_KEY: {
        "type": "score",
        "instructions": (
            "Jak silny jest łączny sygnał zmowy przetargowej w tym klastrze firm, biorąc pod"
            " uwagę wszystkie liczby i percentyle w stanie (`oecd`, `lots`, `n_lots`)?"
        ),
        "criteria": STRENGTH_LEVELS,
    },
}


def build_questions(evidence: dict) -> dict:
    """Pytania dla pakietu dowodów: noul dla każdego punktu w `evidence["oecd"]` + siła."""
    questions: dict[str, dict] = {}
    for item, numbers in evidence.get("oecd", {}).items():
        if item not in CHECKED:
            continue
        template = QUESTIONS[question_key(item)]
        questions[question_key(item)] = {
            **template,
            "instructions": {**template["instructions"], "liczby": numbers},
        }
    questions[STRENGTH_KEY] = QUESTIONS[STRENGTH_KEY]
    return questions


def _weighted(item: str, p: float) -> float:
    return p * ITEM_WEIGHTS.get(CHECKED.get(item, ""), 0.0)


def combine(per_item: dict[str, float]) -> float:
    """`p = 1 - prod(1 - p_i * w_i)` po `TOP_ITEMS` najsilniejszych punktach,
    `w_i` = 1 dla punktów A i 0.6 dla C."""
    contributions = sorted((_weighted(item, p) for item, p in per_item.items()), reverse=True)
    keep = 1.0
    for x in contributions[:TOP_ITEMS]:
        keep *= 1.0 - x
    return 1.0 - keep


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _noul(answers: dict, key: str) -> float:
    answer = answers.get(key)
    if not isinstance(answer, dict) or answer.get("noul") is None:
        raise ValueError(f"jev answer {key!r} has no noul")
    value = float(answer["noul"])
    if math.isnan(value):
        raise ValueError(f"jev answer {key!r} is NaN")
    return _clip(value, 0.0, 1.0)


def decide_cluster(decider: Decider, evidence: dict) -> Decision:
    """Decyzja Jev dla pakietu dowodów. Model dostaje tylko punkty obecne
    (`present_items`). Błędy dostawcy i niepełne odpowiedzi przechodzą wyżej jako wyjątki
    (wtedy `upsert_signals` używa regresji zapasowej)."""
    items = present_items(evidence)
    if not items:
        return Decision(probability=0.0, strength=1.0, per_item={}, source=SOURCE_EMPTY)
    state = {**evidence, "oecd": items}
    questions = build_questions(state)
    answers = decider.decide(task=TASK, state=state, questions=questions)
    if not isinstance(answers, dict):
        raise TypeError("jev answers is not an object")
    per_item = {item: round(_noul(answers, question_key(item)), 4) for item in items}
    probability = combine(per_item)
    score = (answers.get(STRENGTH_KEY) or {}).get("score")
    try:
        # Poziomy score są numerowane od 0; siła sygnału to 1..5.
        strength = _clip(float(score) + 1.0, 1.0, 5.0)
    except (TypeError, ValueError):
        strength = 1.0 + 4.0 * probability
    return Decision(probability=round(probability, 6), strength=round(strength, 4),
                    per_item=per_item, source=SOURCE_JEV)


def _fallback_input(item: str, numbers: dict) -> float:
    # Test z własną flagą sygnału (3.3.10: spadek cen o więcej niż 10%) i flagą `false`.
    if numbers.get("signal") is False:
        return 0.0
    pct = numbers.get("pct")
    if pct is not None:
        return _clip(float(pct), 0.0, 1.0)
    value = numbers.get("value")
    if value is None:
        return 0.0
    if item in BINARY_ITEMS:
        return _clip(float(value), 0.0, 1.0)
    if item == "3.3.10":
        return 1.0 if float(value) <= ENTRY_DROP else 0.0
    # Bez percentyla (za mało obserwacji w rynku) test nie daje sygnału.
    return 0.0


def fallback_decision(evidence: dict) -> Decision:
    """Regresja logistyczna na percentylach testów (stałe wagi `FALLBACK_WEIGHTS`)."""
    per_item: dict[str, float] = {}
    z = FALLBACK_BIAS
    for item, numbers in evidence.get("oecd", {}).items():
        if item not in FALLBACK_WEIGHTS:
            continue
        x = _fallback_input(item, numbers)
        per_item[item] = round(x, 4)
        z += FALLBACK_WEIGHTS[item] * x
    probability = 1.0 / (1.0 + math.exp(-z))
    return Decision(probability=round(probability, 6),
                    strength=round(1.0 + 4.0 * probability, 4),
                    per_item=per_item, source=SOURCE_FALLBACK)


def passes_publish_gate(decision: Decision) -> bool:
    """Najsilniejszy punkt co najmniej 0.8 i co najmniej dwa punkty co najmniej 0.6."""
    values = list(decision.per_item.values())
    return (max(values, default=0.0) >= PUBLISH_MAX_ITEM
            and sum(1 for v in values if v >= PUBLISH_ITEM_FLOOR) >= PUBLISH_MIN_ITEMS)


def cap_status(status: str, max_status: str) -> str:
    """Status nie wyższy niż `max_status` (kolejność hidden < review < published)."""
    if ENGINE_STATUSES.index(status) > ENGINE_STATUSES.index(max_status):
        return max_status
    return status


def status_for(decision: Decision, publish_threshold: float, review_threshold: float,
               max_status: str = "published") -> str:
    """Status wg progów i bramki publikacji; tylko decyzja Jev może zostać opublikowana,
    a wynik nigdy nie przekracza `max_status`."""
    if (decision.source == SOURCE_JEV and decision.probability >= publish_threshold
            and passes_publish_gate(decision)):
        status = "published"
    elif decision.probability >= review_threshold:
        status = "review"
    else:
        status = "hidden"
    return cap_status(status, max_status)


def _stored_decision(row: dict) -> Decision | None:
    meta = (row.get("evidence") or {}).get("decision") or {}
    if meta.get("source") != SOURCE_JEV or row.get("probability") is None:
        return None
    return Decision(probability=float(row["probability"]),
                    strength=float(meta.get("strength") or row.get("strength") or 1.0),
                    per_item={k: float(v) for k, v in (row.get("oecd") or {}).items()},
                    source=SOURCE_JEV)


def _upsert(conn: psycopg.Connection, run_id: int, cluster: dict, evidence: dict, ehash: str,
            decision: Decision, status: str) -> None:
    stored = {**evidence, "decision": {"source": decision.source,
                                       "strength": decision.strength}}
    conn.execute(
        "insert into signals(signal_id, run_id, cluster_id, market_id, probability, strength,"
        " oecd, status, evidence_hash, evidence)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " on conflict (signal_id) do update set"
        "  run_id = excluded.run_id, cluster_id = excluded.cluster_id,"
        "  market_id = excluded.market_id, probability = excluded.probability,"
        "  strength = excluded.strength, oecd = excluded.oecd, status = excluded.status,"
        "  evidence_hash = excluded.evidence_hash, evidence = excluded.evidence,"
        "  updated_at = now()",
        (cluster["cluster_id"], run_id, cluster["cluster_id"], cluster["market_id"],
         decision.probability, round(_clip(decision.strength, 1.0, 5.0)),
         Jsonb(decision.per_item), status, ehash, Jsonb(stored)),
    )


def _record_jev_disabled(conn: psycopg.Connection, run_id: int, reason: str) -> None:
    conn.execute(
        "update engine_runs set params = params || jsonb_build_object("
        "'jev_disabled_reason', %s::text) where run_id = %s",
        (reason, run_id),
    )
    conn.commit()


def _http_error_disables_jev(status: int) -> bool:
    """Każde 4xx poza 429 dotyczy zapytania albo klucza, nie klastra: dalsze pytania w tym
    przebiegu skończyłyby się tak samo."""
    return 400 <= status < 500 and status != 429


def upsert_signals(conn: psycopg.Connection, run_id: int, decider: Decider | None, *,
                   publish_threshold: float = 0.91, review_threshold: float = 0.6,
                   max_status: str | None = None) -> dict:
    """Sygnał dla każdego klastra przebiegu `run_id`; zwraca liczniki.

    `max_status` (domyślnie z `ENGINE_MAX_STATUS`, czyli `review`) ogranicza status z góry.
    Gdy `evidence_hash` sygnału się nie zmienił, a poprzednia decyzja pochodziła z Jev,
    model nie jest pytany ponownie. Bez decidera, po `BudgetExceeded` albo błędzie dostawcy
    decyduje regresja zapasowa, a status jest co najwyżej `review`. Każdy klaster jest
    zatwierdzany osobno, żeby zapłacona decyzja nie przepadła przy późniejszym błędzie.
    """
    if max_status is None:
        max_status = engine_max_status()
    if max_status not in ENGINE_STATUSES:
        raise ValueError(f"unknown max status {max_status!r}")
    counts: dict[str, Any] = {"clusters": 0, "published": 0, "review": 0, "hidden": 0,
                              "jev_calls": 0, "reused": 0, "fallback": 0}
    clusters = conn.execute(
        "select cluster_id, market_id from clusters where run_id = %s order by cluster_id",
        (run_id,),
    ).fetchall()
    jev_off = decider is None
    consecutive_errors = 0

    def disable(reason: str) -> None:
        nonlocal jev_off
        jev_off = True
        counts["jev_disabled_reason"] = reason
        _record_jev_disabled(conn, run_id, reason)

    for cluster in clusters:
        evidence = build_evidence(conn, run_id, cluster["cluster_id"])
        ehash = evidence_hash(evidence)
        previous = conn.execute(
            "select probability, strength, oecd, evidence_hash, evidence from signals"
            " where signal_id = %s",
            (cluster["cluster_id"],),
        ).fetchone()
        decision = None
        if previous is not None and previous["evidence_hash"] == ehash:
            decision = _stored_decision(previous)
            if decision is not None:
                counts["reused"] += 1
        if decision is None and not jev_off:
            try:
                decision = decide_cluster(decider, evidence)
                consecutive_errors = 0
                if decision.source == SOURCE_JEV:
                    counts["jev_calls"] += 1
            except BudgetExceeded:
                log.warning("AI budget exceeded, fallback decisions for the rest of run %s",
                            run_id)
                disable("budget exceeded")
            except ProviderHttpError as e:
                if _http_error_disables_jev(e.status):
                    # The message carries the start of the response body (never the key).
                    log.error("jev HTTP %s for cluster %s, Jev off for the rest of run %s: %s",
                              e.status, cluster["cluster_id"], run_id,
                              str(e)[:MAX_BODY_LOG])
                    disable(f"HTTP {e.status}")
                else:
                    log.warning("jev error for cluster %s: HTTP %s", cluster["cluster_id"],
                                e.status)
                    consecutive_errors += 1
            except Exception as e:  # noqa: BLE001 - other provider failure or bad answer
                log.warning("jev decision failed for cluster %s: %s", cluster["cluster_id"],
                            type(e).__name__)
                consecutive_errors += 1
            if not jev_off and consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log.error("jev failed %s times in a row, Jev off for the rest of run %s",
                          consecutive_errors, run_id)
                disable(f"{consecutive_errors} consecutive errors")
        if decision is None:
            decision = fallback_decision(evidence)
            counts["fallback"] += 1
        status = status_for(decision, publish_threshold, review_threshold, max_status)
        _upsert(conn, run_id, cluster, evidence, ehash, decision, status)
        conn.commit()
        counts["clusters"] += 1
        counts[status] += 1
    if decider is not None and counts["jev_calls"] == 0 and counts["fallback"] > 0:
        log.warning("engine run %s: a Jev decider is configured but made no decision"
                    " (%s fallback decisions)", run_id, counts["fallback"])
        counts["warning"] = "decider configured, 0 jev calls"
    return counts
