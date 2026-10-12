from __future__ import annotations

import re
import unicodedata

_FORMS = [
    (r"spolka z ograniczona odpowiedzialnoscia", "spzoo"), (r"sp z o o", "spzoo"),
    (r"sp zoo", "spzoo"), (r"sp z oo", "spzoo"), (r"spolka akcyjna", "sa"), (r"\bs a\b", "sa"),
    (r"spolka jawna", "spj"), (r"\bsp j\b", "spj"), (r"spolka komandytowa", "spk"),
    (r"\bsp k\b", "spk"), (r"spolka cywilna", "sc"), (r"\bs c\b", "sc"),
]
_COMPANY_TOKENS = {"spzoo", "sa", "spj", "spk", "ska", "spp", "spoldzielnia", "fundacja",
                   "stowarzyszenie", "instytut", "uniwersytet"}


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").replace("ł", "l").replace("Ł", "L")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for pat, rep in _FORMS:
        s = re.sub(pat, rep, s)
    return re.sub(r"\s+", " ", s).strip()


def looks_like_company(name: str) -> bool:
    n = normalize_name(name)
    return bool(set(n.split()) & _COMPANY_TOKENS) or "przedsiebiorstwo panstwowe" in n
