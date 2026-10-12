"""Parser ogłoszeń o wyniku postępowania z BZP (rekord JSON z polem htmlBody)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from tie.money import clean_nip, parse_amount


@dataclass(frozen=True)
class Winner:
    name: str
    nip: str | None
    city: str | None
    province: str | None
    consortium: bool


@dataclass(frozen=True)
class LotResult:
    lot_no: int
    cpv: str | None
    estimated_value: Decimal | None
    outcome: str  # "umowa" | "uniewaznienie"
    n_offers: int | None
    n_rejected: int | None
    price_min: Decimal | None
    price_max: Decimal | None
    price_winner: Decimal | None
    contract_value: Decimal | None
    contract_date: date | None
    winner: Winner | None


@dataclass(frozen=True)
class ResultNotice:
    bzp_number: str
    notice_number: str
    ocds_id: str
    published_at: datetime
    buyer_name: str
    buyer_nip: str | None
    buyer_regon: str | None
    buyer_province: str | None
    buyer_city: str | None
    title: str
    order_type: str | None  # "Works" | "Supplies" | "Services"
    mode: str | None  # tekst z 3.1, np. "art. 275 pkt 1"
    cpv_all: list[str]
    is_below_eu: bool | None
    platform_url: str | None
    platform: str | None
    lots: list[LotResult]


_HEADINGS = {"h1", "h2", "h3", "h4"}
_FIELD = re.compile(r"^(\d+(?:\.\d+)*\.?\))\s*(.*?):\s*(.*)$")
_LOT_HEADER = re.compile(r"^SEKCJA V ZAKOŃCZENIE POSTĘPOWANIA \(dla części (\d+)\)")
_LOT_LINE = re.compile(r"^Część (\d+)$")
_CPV = re.compile(r"\d{8}-\d")
_INT = re.compile(r"\d+")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PLATFORM_URL = re.compile(
    r"1\.6\.\) Adres strony internetowej prowadzonego postępowania:\s*</h3>\s*([^<\s]+)"
)
_FRACTION = re.compile(r"(\.\d{6})\d+")
_PLATFORMS = (
    ("ezamowienia.gov.pl", "ezamowienia"),
    ("platformazakupowa.pl", "platformazakupowa"),
    ("ezamawiajacy.pl", "ezamawiajacy"),
    ("logintrade.net", "logintrade"),
    ("e-propublico.pl", "e-propublico"),
)
_ORDER_TYPES = {"Works": "Works", "Supplies": "Supplies", "Delivery": "Supplies",
                "Services": "Services"}


def _lines(html: str) -> list[str]:
    tree = HTMLParser(html or "")
    root = tree.root
    if root is None:
        return []
    out: list[str] = []
    for node in root.traverse():
        if node.tag in _HEADINGS:
            text = re.sub(r"\s+", " ", node.text(separator=" ")).strip()
            if text:
                out.append(text)
    return out


def _fields(lines: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in lines:
        m = _FIELD.match(line)
        if m:
            out.append((m.group(1), m.group(3).strip()))
    return out


def _first(fields: list[tuple[str, str]], code: str) -> str | None:
    for k, v in fields:
        if k == code:
            return v
    return None


def _section(lines: list[str], start: str, stop: str) -> list[str]:
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith(stop) and inside:
            break
        if line.startswith(start):
            inside = True
            continue
        if inside:
            out.append(line)
    return out


def _lot_blocks(lines: list[str]) -> list[tuple[int, list[str]]]:
    blocks: list[tuple[int, list[str]]] = []
    current: list[str] | None = None
    for line in lines:
        m = _LOT_HEADER.match(line)
        if m:
            current = []
            blocks.append((int(m.group(1)), current))
            continue
        if current is not None:
            current.append(line)
    if blocks:
        return blocks
    for i, line in enumerate(lines):
        if line.startswith("SEKCJA V ZAKOŃCZENIE"):
            return [(1, lines[i + 1:])]
    return []


def _cpv_code(value: str | None) -> str | None:
    if not value:
        return None
    m = _CPV.search(value)
    return m.group(0) if m else None


def _section_iv(lines: list[str]) -> dict[int, tuple[str | None, Decimal | None]]:
    sec = _section(lines, "SEKCJA IV", "SEKCJA V")
    out: dict[int, tuple[str | None, Decimal | None]] = {}
    lot: int | None = None
    cpv: str | None = None
    value: Decimal | None = None
    single_value: Decimal | None = None
    for line in sec:
        m = _LOT_LINE.match(line)
        if m:
            if lot is not None:
                out[lot] = (cpv, value)
            lot, cpv, value = int(m.group(1)), None, None
            continue
        f = _FIELD.match(line)
        if not f:
            continue
        code, val = f.group(1), f.group(3)
        if code == "4.5.3.)" and cpv is None:
            cpv = _cpv_code(val)
        elif code == "4.5.5.)" and value is None:
            value = parse_amount(val)
        elif code == "4.3.1)" and single_value is None:
            single_value = parse_amount(val)
    if lot is not None:
        out[lot] = (cpv, value)
    else:
        out[1] = (cpv, single_value if single_value is not None else value)
    return out


def _int(value: str | None) -> int | None:
    if not value:
        return None
    m = _INT.search(value)
    return int(m.group(0)) if m else None


def _amount(value: str | None) -> Decimal | None:
    return parse_amount(value) if value else None


def _date(value: str | None) -> date | None:
    if not value:
        return None
    m = _DATE.search(value)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(0))
    except ValueError:
        return None


def _winner_name(line: str) -> str:
    m = _FIELD.match(line)
    name = m.group(3) if m else line.split(": ", 1)[-1]
    cut = name.find("Nazwy (firm")
    if cut >= 0:
        name = name[:cut]
    return name.strip()


def _winner(lines: list[str], fields: list[tuple[str, str]]) -> Winner | None:
    name_line = next((ln for ln in lines if ln.startswith("7.3.1)")), None)
    if name_line is None:
        return None
    name = _winner_name(name_line)
    if not name:
        return None
    consortium = (_first(fields, "7.1.)") or "").strip().lower().startswith("tak")
    nip_raw = _first(fields, "7.3.2)")
    return Winner(
        name=name,
        nip=clean_nip(nip_raw) if nip_raw else None,
        city=_first(fields, "7.3.4)") or None,
        province=_first(fields, "7.3.6.)") or None,
        consortium=consortium,
    )


def _platform(url: str | None) -> str | None:
    if not url:
        return None
    host = (urlparse(url).hostname or url).lower()
    for domain, name in _PLATFORMS:
        if host == domain or host.endswith("." + domain):
            return name
    return "other"


def _lot(lot_no: int, lines: list[str], iv: dict[int, tuple[str | None, Decimal | None]]
         ) -> LotResult:
    fields = _fields(lines)
    cpv, estimated = iv.get(lot_no, (None, None))
    outcome = "uniewaznienie" if "unieważnieniem" in (_first(fields, "5.1.)") or "") else "umowa"
    winner = _winner(lines, fields) if outcome == "umowa" else None
    return LotResult(
        lot_no=lot_no,
        cpv=cpv,
        estimated_value=estimated,
        outcome=outcome,
        n_offers=_int(_first(fields, "6.1.)")),
        n_rejected=_int(_first(fields, "6.1.6.)")),
        price_min=_amount(_first(fields, "6.2.)")),
        price_max=_amount(_first(fields, "6.3.)")),
        price_winner=_amount(_first(fields, "6.4.)")),
        contract_value=_amount(_first(fields, "8.2.)")),
        contract_date=_date(_first(fields, "8.1.)")),
        winner=winner,
    )


def _published_at(value: str) -> datetime:
    return datetime.fromisoformat(_FRACTION.sub(r"\1", value.replace("Z", "+00:00")))


def _mode(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+ustawy\s*$", "", value).strip() or None


def _regon(value: str | None) -> str | None:
    if not value or not value.strip().upper().startswith("REGON"):
        return None
    digits = re.sub(r"\D", "", value)
    return digits or None


def parse_result_notice(record: dict) -> ResultNotice:
    html = record.get("htmlBody") or ""
    lines = _lines(html)
    fields = _fields(lines)
    iv = _section_iv(lines)
    lots = [_lot(no, block, iv) for no, block in _lot_blocks(lines)]

    m = _PLATFORM_URL.search(html)
    platform_url = m.group(1) if m else None
    nip_raw = record.get("organizationNationalId")
    order_type = record.get("orderType")

    return ResultNotice(
        bzp_number=record["bzpNumber"],
        notice_number=record["noticeNumber"],
        ocds_id=record["tenderId"],
        published_at=_published_at(record["publicationDate"]),
        buyer_name=record.get("organizationName") or "",
        buyer_nip=clean_nip(nip_raw) if nip_raw else None,
        buyer_regon=_regon(_first(fields, "1.4)")),
        buyer_province=record.get("organizationProvince") or None,
        buyer_city=record.get("organizationCity") or None,
        title=record.get("orderObject") or "",
        order_type=_ORDER_TYPES.get(order_type, order_type) if order_type else None,
        mode=_mode(_first(fields, "3.1.)")),
        cpv_all=_CPV.findall(record.get("cpvCode") or ""),
        is_below_eu=record.get("isTenderAmountBelowEU"),
        platform_url=platform_url,
        platform=_platform(platform_url),
        lots=lots,
    )
