"""Decyzja o sygnale klastra: Jev ze skalibrowanym prawdopodobieństwem per punkt OECD,
regresja logistyczna jako wariant zapasowy, zapis sygnałów z progami publikacji.

Arytmetyka zostaje w kodzie: Jev dostaje pakiet dowodów (`tie.engine.evidence`) jako `state`
i dla każdego punktu OECD obecnego w dowodach pytanie `noul` z liczbami tego punktu, plus
`score` siły sygnału w 5 poziomach. Łączne prawdopodobieństwo liczymy w kodzie:
`p = 1 - prod(1 - p_i * w_i)`, `w_i` = 1 dla punktów A i 0.6 dla C.

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
from tie.engine.evidence import CHECKED, build_evidence, evidence_hash
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
# Odpowiedź z tymi statusami HTTP dotyczy klucza, nie klastra: nie pytamy dalej w tym przebiegu.
AUTH_STATUSES = frozenset({401, 403})

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


def combine(per_item: dict[str, float]) -> float:
    """`p = 1 - prod(1 - p_i * w_i)`, `w_i` = 1 dla punktów A i 0.6 dla C."""
    keep = 1.0
    for item, p in per_item.items():
        keep *= 1.0 - p * ITEM_WEIGHTS.get(CHECKED.get(item, ""), 0.0)
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
    """Decyzja Jev dla pakietu dowodów. Błędy dostawcy i niepełne odpowiedzi przechodzą
    wyżej jako wyjątki (wtedy `upsert_signals` używa regresji zapasowej)."""
    questions = build_questions(evidence)
    items = [k for k in questions if k != STRENGTH_KEY]
    if not items:
        return Decision(probability=0.0, strength=1.0, per_item={}, source=SOURCE_EMPTY)
    answers = decider.decide(task=TASK, state=evidence, questions=questions)
    if not isinstance(answers, dict):
        raise TypeError("jev answers is not an object")
    per_item = {
        item: round(_noul(answers, question_key(item)), 4)
        for item in evidence["oecd"] if item in CHECKED
    }
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


def status_for(decision: Decision, publish_threshold: float, review_threshold: float) -> str:
    """Status wg progów; tylko decyzja Jev może zostać opublikowana."""
    if decision.source == SOURCE_JEV and decision.probability >= publish_threshold:
        return "published"
    if decision.probability >= review_threshold:
        return "review"
    return "hidden"


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


def upsert_signals(conn: psycopg.Connection, run_id: int, decider: Decider | None, *,
                   publish_threshold: float = 0.91, review_threshold: float = 0.6) -> dict:
    """Sygnał dla każdego klastra przebiegu `run_id`; zwraca liczniki.

    Gdy `evidence_hash` sygnału się nie zmienił, a poprzednia decyzja pochodziła z Jev,
    model nie jest pytany ponownie. Bez decidera, po `BudgetExceeded` albo błędzie dostawcy
    decyduje regresja zapasowa, a status jest co najwyżej `review`. Każdy klaster jest
    zatwierdzany osobno, żeby zapłacona decyzja nie przepadła przy późniejszym błędzie.
    """
    counts = {"clusters": 0, "published": 0, "review": 0, "hidden": 0,
              "jev_calls": 0, "reused": 0, "fallback": 0}
    clusters = conn.execute(
        "select cluster_id, market_id from clusters where run_id = %s order by cluster_id",
        (run_id,),
    ).fetchall()
    jev_off = decider is None
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
                if decision.source == SOURCE_JEV:
                    counts["jev_calls"] += 1
            except BudgetExceeded:
                log.warning("AI budget exceeded, fallback decisions for the rest of run %s",
                            run_id)
                jev_off = True
            except ProviderHttpError as e:
                log.warning("jev error for cluster %s: HTTP %s", cluster["cluster_id"],
                            e.status)
                jev_off = e.status in AUTH_STATUSES
            except Exception as e:  # noqa: BLE001 - other provider failure or bad answer
                log.warning("jev decision failed for cluster %s: %s", cluster["cluster_id"],
                            type(e).__name__)
        if decision is None:
            decision = fallback_decision(evidence)
            counts["fallback"] += 1
        status = status_for(decision, publish_threshold, review_threshold)
        _upsert(conn, run_id, cluster, evidence, ehash, decision, status)
        conn.commit()
        counts["clusters"] += 1
        counts[status] += 1
    return counts
