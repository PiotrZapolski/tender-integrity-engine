"""LLM fallback for "informacja z otwarcia ofert" documents.

When the deterministic parser cannot produce offers that pass the BZP cross-check, DeepSeek
reads the offers part of the document and returns structured JSON. The answer is validated
with pydantic; NIPs failing the checksum are dropped (`clean_nip`). The result is untrusted
until `tie.openings.crosscheck.cross_check` accepts it.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, field_validator

from tie.identity.names import normalize_name
from tie.money import clean_nip, parse_amount

TASK = "opening_extract"

SYSTEM_PROMPT = """Jesteś parserem dokumentów "informacja z otwarcia ofert" z polskich przetargów publicznych.
Zwróć wyłącznie JSON: {"budget": liczba lub null, "offers": [{"offer_no": liczba lub null,
"lot_no": liczba lub null, "bidder_name": tekst, "nip": tekst lub null, "price": liczba lub null,
"price_basis": "total" | "unit" | "other"}]}.
Zasady: jedna pozycja na ofertę; przy konsorcjum bidder_name to lider (pierwszy wymieniony);
price to cena brutto oferty jako liczba z kropką dziesiętną; price_basis "unit" gdy cena dotyczy
jednostki (godzina, km, sztuka); nie dodawaj zamawiającego jako oferenta; nie zgaduj NIP."""  # noqa: E501

# Matched against normalize_name(line): the offers table starts at the first such line.
_MARKERS = ("wykonawc", "oferent", "nazwa", "otwarto")
_BASES = ("total", "unit", "other")


class TextProvider(Protocol):
    def complete_json(self, *, task: str, system: str, user: str) -> dict: ...


def _to_decimal(value: Any) -> Decimal | None:
    """Number from the model: JSON number, "45.63", or a Polish amount like "38 930,50 zł"."""
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("boolean is not an amount")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        out = Decimal(str(value))  # str() keeps 45.63 as 45.63, not the binary expansion
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            out = Decimal(s)
        except InvalidOperation:
            amount = parse_amount(s)
            if amount is None:
                raise ValueError(f"not an amount: {value!r}") from None
            out = amount
    else:
        raise ValueError(f"not an amount: {value!r}")
    if not out.is_finite():
        raise ValueError(f"not a finite amount: {value!r}")
    return out


class LlmOffer(BaseModel):
    offer_no: int | None = None
    lot_no: int | None = None
    bidder_name: str
    nip: str | None = None
    price: Decimal | None = None
    price_basis: Literal["total", "unit", "other"] = "other"

    @field_validator("bidder_name", mode="before")
    @classmethod
    def _name(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = " ".join(value.split())
            if not value:
                raise ValueError("empty bidder_name")
        return value

    @field_validator("nip", mode="before")
    @classmethod
    def _nip(cls, value: Any) -> str | None:
        if value is None or value == "":
            return None
        return clean_nip(str(value))

    @field_validator("price", mode="before")
    @classmethod
    def _price(cls, value: Any) -> Decimal | None:
        return _to_decimal(value)

    @field_validator("price_basis", mode="before")
    @classmethod
    def _basis(cls, value: Any) -> str:
        basis = str(value or "").strip().lower()
        return basis if basis in _BASES else "other"


class LlmOpening(BaseModel):
    budget: Decimal | None = None
    offers: list[LlmOffer] = Field(default_factory=list)

    @field_validator("budget", mode="before")
    @classmethod
    def _budget(cls, value: Any) -> Decimal | None:
        # The budget is informative only; an unreadable one must not reject the offers.
        try:
            return _to_decimal(value)
        except ValueError:
            return None


def slice_offers_text(text: str, max_chars: int = 6000) -> str:
    """Text from the first line that starts the offers part, cut to `max_chars`.

    Without such a line the whole text is used (still cut to `max_chars`).
    """
    lines = (text or "").splitlines()
    start = next(
        (i for i, line in enumerate(lines)
         if any(marker in normalize_name(line) for marker in _MARKERS)),
        0,
    )
    return "\n".join(lines[start:])[:max_chars]


def extract_with_llm(provider: TextProvider, text: str) -> LlmOpening:
    """Ask the model for structured offers. Raises pydantic.ValidationError on a bad answer;
    BudgetExceeded and provider errors propagate unchanged."""
    payload = provider.complete_json(task=TASK, system=SYSTEM_PROMPT,
                                     user=slice_offers_text(text))
    return LlmOpening.model_validate(payload)
