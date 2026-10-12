"""LLM fallback for "informacja z otwarcia ofert" documents.

When the deterministic parser cannot produce offers that pass the BZP cross-check, DeepSeek
reads the offers part of the document and returns structured JSON. The answer is validated
with pydantic; NIPs failing the checksum are dropped (`clean_nip`). The result is untrusted
until `tie.openings.crosscheck.cross_check` accepts it.
"""

from __future__ import annotations

import re
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
jednostki (godzina, km, sztuka); nie dodawaj zamawiającego jako oferenta; nie zgaduj NIP.
Gdy oferta ma kilka cen (np. zamówienie podstawowe, prawo opcji, łącznie), price to cena
łączna brutto.
Części zamówienia: nagłówki typu "Część nr 2", "Dla Części 3:", "Zadanie nr 4", "Pakiet 5",
"Część II" otwierają oferty danej części; lot_no to numer z nagłówka jako liczba arabska
(II = 2), bez przenumerowania. Wykonawca, który złożył ofertę na kilka części, występuje osobno
w każdej części z ceną tej części. W tabeli z kolumną ceny dla każdej części: jedna pozycja na
każdą wypełnioną cenę, pomiń puste i "-". Pomiń części, na które nie złożono ofert. Bez podziału
na części lot_no = null.
W sprostowaniu informacji z otwarcia ofert podaj tylko dane prawidłowe (po sprostowaniu)."""

# Matched against normalize_name(line): the offers table starts at the first such line.
_MARKERS = ("wykonawc", "oferent", "nazwa", "otwarto")
_BASES = ("total", "unit", "other")
# Matched against normalize_name(line): a part (lot) header such as "Dla Części 1:",
# "Część nr 2", "Zadanie nr 3", "Pakiet IV". The first offers table often follows its header
# directly, so the header must stay in the text sent to the model.
_LOT_HEADER = re.compile(r"\b(?:czesc|czesci|zadanie|zadania|pakiet|pakietu)\s+(?:nr\s+)?"
                         r"(?:\d+|[ivx]+)\b")
LOT_HEADER_LOOKBACK = 3  # lines above the first marker searched for a part header
# Characters of text sent to the model: per BZP lot, up to a cap. Multi-lot documents list
# every part's offers; 6000 characters cut them after two or three parts.
LLM_CHARS_PER_LOT = 6000
LLM_MAX_CHARS = 24000
_WIDE_SPACE = re.compile(r"[ \t]{2,}")
_BLANK_RUN = re.compile(r"\n{3,}")


class TextProvider(Protocol):
    def complete_json(self, *, task: str, system: str, user: str) -> dict: ...


def _to_decimal(value: Any) -> Decimal | None:
    """Number from the model: JSON number, "45.63", or a Polish amount like "38 930,50 zł"."""
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("boolean is not an amount")  # noqa: TRY004 - pydantic needs ValueError
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
        raise ValueError(f"not an amount: {value!r}")  # noqa: TRY004 - pydantic needs ValueError
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


def slice_offers_text(text: str, max_chars: int = LLM_CHARS_PER_LOT) -> str:
    """Text from the first line that starts the offers part, cut to `max_chars`.

    A part header up to `LOT_HEADER_LOOKBACK` lines above that line is kept (otherwise the
    offers of the first part lose their lot number). Without a marker line the whole text is
    used. Layout padding from `pdftotext -layout` (runs of spaces, blank lines) is squeezed
    first, so that `max_chars` holds offers rather than spaces.
    """
    lines = [_WIDE_SPACE.sub("  ", line).rstrip() for line in (text or "").splitlines()]
    norms = [normalize_name(line) for line in lines]
    start = next(
        (i for i, norm in enumerate(norms) if any(marker in norm for marker in _MARKERS)),
        0,
    )
    header = next(
        (i for i in range(max(0, start - LOT_HEADER_LOOKBACK), start)
         if _LOT_HEADER.search(norms[i])),
        None,
    )
    if header is not None:
        start = header
    return _BLANK_RUN.sub("\n\n", "\n".join(lines[start:]))[:max_chars]


def llm_max_chars(n_lots: int) -> int:
    return min(LLM_MAX_CHARS, LLM_CHARS_PER_LOT * max(1, n_lots))


def extract_with_llm(provider: TextProvider, text: str, n_lots: int = 1) -> LlmOpening:
    """Ask the model for structured offers. `n_lots` (lots of the procedure in BZP) sets how
    much text is sent. Raises pydantic.ValidationError on a bad answer; BudgetExceeded and
    provider errors propagate unchanged."""
    payload = provider.complete_json(task=TASK, system=SYSTEM_PROMPT,
                                     user=slice_offers_text(text, llm_max_chars(n_lots)))
    return LlmOpening.model_validate(payload)
