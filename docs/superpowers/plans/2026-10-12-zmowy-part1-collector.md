# zmowy.pl część 1: collector - plan implementacji

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Zbierać ogłoszenia o wyniku z BZP (cała Polska od 2021-01-01) oraz imienne listy oferentów z dokumentów "informacja z otwarcia ofert" z platformy e-Zamówienia, zapisywać je w Postgresie z kontrolą krzyżową i wzbogacać o dane z białej listy VAT, z codziennym przyrostem na serwerze Hetzner.

**Architecture:** Pakiet Pythona `tie` (katalog `collector/`) z małymi modułami: klient HTTP z limitami, magazyn surowych plików (zstd), kolejka zadań w Postgresie, parser ogłoszeń BZP, pobieranie dokumentów e-Zamówień, ekstrakcja tekstu (PDF, DOCX, OCR), parser deterministyczny + ekstrakcja przez DeepSeek, kontrola krzyżowa z BZP, tożsamość firm (NIP, Jev), biała lista VAT. Uruchamiane w Dockerze na głównym boxie, deploy przez GitHub Actions.

**Tech Stack:** Python 3.12, uv, psycopg 3, httpx, selectolax, zstandard, python-docx, pydantic 2, pytest, respx, ruff; Postgres 17 z `pg_trgm`; poppler-utils (`pdftotext`, `pdftoppm`), tesseract (`pol`); Docker Compose; supercronic; GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-12-tie-design.md`

## Global Constraints

- NIC nie uruchamiamy na laptopie właściciela: żadnego `pytest`, `uv`, `python`, `docker` lokalnie. Testy uruchamia wyłącznie GitHub Actions. Weryfikacja kroku "run test" = `git push` na gałąź `part1-collector` i `gh run watch --exit-status` (lub `gh run view --log-failed` przy błędzie).
- Brak myślników typograficznych (U+2014, U+2013) w kodzie, komentarzach, commitach i dokumentach. Używaj `-`.
- Sekrety (`JEV_API_KEY`, `DEEPSEEK_API_KEY`) tylko w zmiennych środowiskowych; nigdy w repo, nigdy w logach.
- User-Agent wszystkich zapytań: `TenderIntegrityEngine/0.1 (+https://github.com/PiotrZapolski/tender-integrity-engine)`.
- Limity tempa domyślne: `ezamowienia.gov.pl` 1 zapytanie/s, `wl-api.mf.gov.pl` 1 zapytanie/2 s, `api.typesafe.ai` 10/s, `api.deepseek.com` 5/s.
- JDG: przechowujemy nazwę w bazie (potrzebna do dopasowań), ale pole `is_jdg` musi być ustawione; warstwa publiczna (część 4) pokaże tylko NIP.
- Kwoty jako `Decimal` w Pythonie i `numeric(16,2)` w Postgresie. Daty jako `date`, czasy jako `timestamptz`.
- Model Jev przypięty: `jev-1.13.0`. DeepSeek: model z konfiguracji, domyślnie `deepseek-chat`, odpowiedź `response_format={"type":"json_object"}`.
- Każde wywołanie AI zapisuje wiersz w `ai_calls`; dzienny limit budżetu `AI_DAILY_BUDGET_USD` (domyślnie 5.0) blokuje kolejne wywołania wyjątkiem `BudgetExceeded`.
- Postępowania z jedną ofertą nie wymagają pobierania dokumentu otwarcia (oferent = zwycięzca z BZP).
- Commity kończą się linią `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Repozytoria i praca równoległa

- Backend (ten plan): publiczne repo `PiotrZapolski/tender-integrity-engine` ("Tender Integrity Engine", TIE). Silnik analityczny do przekazania instytucjom. Pakiet Pythona, komenda CLI, projekt Compose, baza i katalog serwera nazywają się `tie`; katalog kodu to `collector/`. User-Agent: `TenderIntegrityEngine/0.1 (+https://github.com/PiotrZapolski/tender-integrity-engine)`.
- Frontend zmowy.pl: osobne repo (część 4), czyta z bazy TIE przez widoki tylko do odczytu.
- Gałąź integracyjna: `part1-collector`. Każde zadanie robi osobny subagent w osobnym worktree na gałęzi `t<N>-<slug>` od aktualnego `part1-collector`; po zielonym CI i review Fable scala zadanie do `part1-collector`, usuwa worktree i gałąź.
- Fale (zadania w jednej fali idą równolegle):
  - Fala 1: Task 1.
  - Fala 2: Task 2, 3, 4, 6, 10.
  - Fala 3: Task 5, 7, 11, 13.
  - Fala 4: Task 8, 12.
  - Fala 5: Task 9, 14, 15.
  - Fala 6: Task 16, potem Task 17.
- Modele: Sonnet dla Task 1, 3, 10, 17 (opis jednoznaczny); Opus dla pozostałych (logika, dane, pieniądze, zewnętrzne API).
- Konflikty: `conftest.py` zmienia tylko Task 1 i Task 2; inne zadania dopisują fixture w swoich plikach testowych.

## Review Focus

1. Dzień BZP z więcej niż 500 ogłoszeniami (limit `PageSize`): fetcher musi dzielić zapytania (województwo, potem rodzaj zamówienia) i niczego nie zgubić; test w Task 7.
2. Ponowne przetworzenie tego samego ogłoszenia lub dokumentu (restart, backfill drugi raz): brak duplikatów, upsert idempotentny; testy w Task 8 i Task 9.
3. Cena w dokumencie otwarcia podana za jednostkę (roboczogodzina, km) a w BZP całkowita: kontrola krzyżowa ma rozpoznać stały mnożnik zamiast odrzucać; test w Task 12 (fixture Spytkowo, mnożnik 6152).
4. NIP zamawiającego w stopce dokumentu otwarcia nie może zostać uznany za oferenta; test w Task 11 (fixture Spytkowo, NIP 8451958301).
5. Awaria lub limit zewnętrznego API (HTTP 429/529/5xx, timeout): ponawianie z wydłużającą się przerwą, zadanie wraca do kolejki zamiast ginąć; testy w Task 4 i Task 5.

---

## Struktura plików

```
README.md                         opis projektu, źródła, status
LICENSE                           MIT (kod)
LICENSE-DATA.md                   CC BY 4.0 (dane pochodne)
.gitignore
.github/workflows/ci.yml          lint + testy + budowa obrazu
.github/workflows/deploy.yml      deploy na self-hosted runner (master)
collector/
  pyproject.toml
  Dockerfile
  crontab                         harmonogram supercronic
  src/tie/
    __init__.py
    config.py                     Settings z env
    db.py                         połączenie + migracje
    migrations/001_init.sql       schemat części 1
    rawstore.py                   magazyn surowych plików zstd
    http.py                       PoliteClient
    jobs.py                       kolejka zadań
    money.py                      parsowanie kwot i NIP
    bzp/parser.py                 parser ogłoszeń o wyniku
    bzp/fetch.py                  pobieranie dni z podziałem
    bzp/store.py                  zapis do bazy
    ezam/documents.py             lista i pobieranie dokumentów
    openings/extract.py           tekst z PDF/DOCX/OCR
    openings/parse.py             parser deterministyczny
    openings/crosscheck.py        kontrola krzyżowa z BZP
    openings/llm.py               ekstrakcja przez DeepSeek
    openings/store.py             zapis ofert
    ai/providers.py               TextProvider, DecisionProvider, budżet
    identity/bidders.py           tożsamość firm, Jev
    identity/whitelist.py         biała lista VAT
    handlers.py                   rejestr handlerów zadań
    cli.py                        komendy
  tests/
    conftest.py
    fixtures/...                  (już w repo)
    test_*.py
infra/
  docker-compose.yml
  docker-compose.prod.yml
docs/OPERATIONS.md
```

Fixtures (już skopiowane do `collector/tests/fixtures/`):

- `bzp/result_multilot.json`: 2026/BZP 00464943, ZBK Kraków, 6 części.
- `bzp/result_single.json`: 2026/BZP 00464944, Kielce, 1 oferta, postępowanie na e-Zamówieniach `ocds-148610-31180df3-a1c9-47cf-8061-b35e3d3d4483`.
- `bzp/result_cancelled.json`: 2026/BZP 00464946, unieważnienie.
- `bzp/result_consortium.json`: 2026/BZP 00465041, zwycięzca konsorcjum (lider NIP 8581157491).
- `bzp/result_spytkowo.json`: `ocds-148610-05afc816-7c33-4416-8a4f-8a804d3d4b32`, 8 ofert, min 239497.36, max 301571.04, zwycięzca MULTISERVICE NIP 6791021905.
- `bzp/result_kolbudy.json`: `ocds-148610-0446493b-6b68-4fa8-aa08-45d7d62c808c`, 1 oferta 213192.00.
- `ezam/documents_31180df3.json`, `ezam/tender_31180df3.json`: odpowiedzi API e-Zamówień.
- `openings/kolbudy_text.pdf`: 1 oferta, Reknica sp. z o.o., NIP 5932335316, 213 192,00 zł.
- `openings/spytkowo_unit_prices.pdf`: 8 ofert w cenie za roboczogodzinę: 45,63; 40,18; 49,02; 44,78; 39,85; 43,42; 41,82; 38,93; bez NIP oferentów; w stopce NIP zamawiającego 8451958301.
- `openings/rogow_docx.docx`: 4 oferty: 366 120,00; 525 960,00; 499 256,24; 770 901,55; bez NIP.
- `openings/kielce_scan.pdf`: skan bez warstwy tekstowej (wymaga OCR).
- `wl/batch_2.json`: odpowiedź zbiorczego zapytania białej listy VAT (`/api/search/nips/{nip1,nip2}`) dla dwóch NIP-ów: 6791021905 (prawdziwa spółka, KRS 0000047217, 4 konta) i 7010000005 (syntetyczna JDG, dane fikcyjne, krs null, 0 kont). Struktura: `result.entries[] = {identifier, subjects[]}`.

Każdy plik JSON w `bzp/` to jeden obiekt z API `mo-board/api/v1/notice` z polami m.in. `bzpNumber`, `noticeNumber`, `tenderId`, `publicationDate`, `organizationName`, `organizationNationalId`, `organizationProvince`, `orderType`, `cpvCode`, `procedureResult`, `contractors`, `htmlBody`.

---

### Task 1: Szkielet repo, README, CI

**Files:**
- Create: `README.md`, `LICENSE`, `LICENSE-DATA.md`, `.gitignore`, `.github/workflows/ci.yml`
- Create: `collector/pyproject.toml`, `collector/src/tie/__init__.py`, `collector/src/tie/config.py`
- Test: `collector/tests/test_config.py`, `collector/tests/conftest.py`

**Interfaces:**
- Produces: `tie.config.Settings` (dataclass, frozen) z polami: `database_url: str`, `raw_dir: Path`, `user_agent: str`, `jev_api_key: str | None`, `deepseek_api_key: str | None`, `deepseek_model: str`, `ai_daily_budget_usd: float`, `wl_daily_cap: int`; funkcja `load_settings(env: Mapping[str, str] | None = None) -> Settings`.

- [ ] **Step 1: README.md**

Treść (po polsku, bez myślników typograficznych), sekcje:
1. `# zmowy.pl` i jedno zdanie: "Otwarty radar zmów przetargowych w polskich zamówieniach publicznych."
2. "Co robimy": 4 punkty (zbieramy BZP i otwarcia ofert, budujemy graf współoferentów, liczymy sygnały z listy OECD, publikujemy z prawem do odpowiedzi). Zdanie: "To sygnał statystyczny, nie zarzut."
3. "Źródła danych" jako tabela: BZP (`https://ezamowienia.gov.pl/mo-board/api/v1/notice`), e-Zamówienia (`mp-readmodels/api/Search/GetTenderDocuments`, `Tender/DownloadDocument`), biała lista VAT (`https://wl-api.mf.gov.pl`), decyzje UOKiK (`https://uokik.gov.pl/decyzje-prezesa-uokik-w-sprawie-zmow-przetargowych`), orzeczenia KIO (`https://orzeczenia.uzp.gov.pl`).
4. "Metodologia": link do Wytycznych OECD 2025 rozdział 3 (`https://www.oecd.org/pl/publications/wytyczne-oecd-dotyczace-zwalczania-zmow-przetargowych-w-zamowieniach-publicznych-aktualizacja-2025_944084ad-pl/full-report/component-5.html`) i Zawiadomienia KE 2021/C 91/01 (`https://eur-lex.europa.eu/legal-content/PL/TXT/?uri=CELEX:52021XC0318(01)`), link do speca.
5. "Architektura": diagram tekstowy z sekcji 5 speca (skrócony).
6. "Status": tabela 5 części ze statusem (część 1 w toku).
7. "Licencje": kod MIT, dane pochodne CC BY 4.0, dane źródłowe to informacja publiczna.
8. "Kontakt": kontakt@zmowy.pl.

- [ ] **Step 2: LICENSE (MIT, rok 2026, "zmowy.pl contributors"), LICENSE-DATA.md (CC BY 4.0 z linkiem `https://creativecommons.org/licenses/by/4.0/deed.pl`), .gitignore**

```gitignore
__pycache__/
*.pyc
.venv/
.env
data/
.pytest_cache/
.ruff_cache/
node_modules/
```

- [ ] **Step 3: collector/pyproject.toml**

```toml
[project]
name = "tie"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "psycopg[binary]>=3.2",
  "httpx>=0.27",
  "selectolax>=0.3.21",
  "zstandard>=0.23",
  "python-docx>=1.1",
  "pydantic>=2.8",
]

[project.optional-dependencies]
dev = ["pytest>=8.3", "respx>=0.21", "ruff>=0.6"]

[project.scripts]
tie = "tie.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/tie"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"
```

- [ ] **Step 4: Write the failing test** `collector/tests/test_config.py`

```python
from pathlib import Path

from tie.config import load_settings


def test_defaults():
    s = load_settings({"DATABASE_URL": "postgresql://u:p@h/db"})
    assert s.database_url == "postgresql://u:p@h/db"
    assert s.raw_dir == Path("/data/raw")
    assert s.user_agent.startswith("TenderIntegrityEngine/0.1")
    assert s.jev_api_key is None
    assert s.deepseek_model == "deepseek-chat"
    assert s.ai_daily_budget_usd == 5.0
    assert s.wl_daily_cap == 90


def test_overrides():
    s = load_settings({
        "DATABASE_URL": "x", "RAW_DIR": "/tmp/r", "AI_DAILY_BUDGET_USD": "1.5",
        "JEV_API_KEY": "k", "WL_DAILY_CAP": "10",
    })
    assert s.raw_dir == Path("/tmp/r")
    assert s.ai_daily_budget_usd == 1.5
    assert s.jev_api_key == "k"
    assert s.wl_daily_cap == 10


def test_missing_database_url_raises():
    import pytest
    with pytest.raises(KeyError):
        load_settings({})
```

`collector/tests/conftest.py` (na razie tylko ścieżka do fixtures):

```python
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES
```

- [ ] **Step 5: CI workflow** `.github/workflows/ci.yml`

```yaml
name: ci
on:
  push:
  pull_request:
jobs:
  collector:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:17
        env:
          POSTGRES_USER: tie
          POSTGRES_PASSWORD: tie
          POSTGRES_DB: tie_test
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U tie" --health-interval 5s
          --health-timeout 5s --health-retries 10
    env:
      TEST_DATABASE_URL: postgresql://tie:tie@localhost:5432/tie_test
    defaults:
      run:
        working-directory: collector
    steps:
      - uses: actions/checkout@v4
      - run: sudo apt-get update && sudo apt-get install -y poppler-utils tesseract-ocr tesseract-ocr-pol
      - uses: astral-sh/setup-uv@v3
      - run: uv venv && uv pip install -e ".[dev]"
      - run: uv run ruff check src tests
      - run: uv run pytest -q
```

- [ ] **Step 6: Push and verify the test fails**

```bash
git checkout -b part1-collector
git add -A && git commit -m "chore: szkielet repo, README, CI"
git push -u origin part1-collector
gh run watch --exit-status
```
Expected: FAIL (`ModuleNotFoundError: tie.config` albo brak `load_settings`).

- [ ] **Step 7: Implementation** `collector/src/tie/config.py` (i pusty `__init__.py` z `__version__ = "0.1.0"`)

```python
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

USER_AGENT = "TenderIntegrityEngine/0.1 (+https://github.com/PiotrZapolski/tender-integrity-engine)"


@dataclass(frozen=True)
class Settings:
    database_url: str
    raw_dir: Path
    user_agent: str
    jev_api_key: str | None
    deepseek_api_key: str | None
    deepseek_model: str
    ai_daily_budget_usd: float
    wl_daily_cap: int


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    return Settings(
        database_url=e["DATABASE_URL"],
        raw_dir=Path(e.get("RAW_DIR", "/data/raw")),
        user_agent=e.get("USER_AGENT", USER_AGENT),
        jev_api_key=e.get("JEV_API_KEY") or None,
        deepseek_api_key=e.get("DEEPSEEK_API_KEY") or None,
        deepseek_model=e.get("DEEPSEEK_MODEL", "deepseek-chat"),
        ai_daily_budget_usd=float(e.get("AI_DAILY_BUDGET_USD", "5.0")),
        wl_daily_cap=int(e.get("WL_DAILY_CAP", "90")),
    )
```

- [ ] **Step 8: Push, verify PASS, commit**

```bash
git add -A && git commit -m "feat: konfiguracja z env"
git push && gh run watch --exit-status
```
Expected: PASS.

---

### Task 2: Baza danych i migracje

**Files:**
- Create: `collector/src/tie/db.py`, `collector/src/tie/migrations/001_init.sql`
- Modify: `collector/tests/conftest.py`
- Test: `collector/tests/test_db.py`

**Interfaces:**
- Consumes: `Settings.database_url`.
- Produces: `connect(url: str) -> psycopg.Connection` (autocommit=False, row_factory=dict_row); `migrate(conn) -> list[str]` (zwraca nazwy zastosowanych plików; idempotentne); fixture pytest `db` (połączenie do czystej bazy z migracjami, pomija testy, gdy brak `TEST_DATABASE_URL`).

- [ ] **Step 1: Write the failing test** `collector/tests/test_db.py`

```python
from tie.db import migrate

EXPECTED = {
    "schema_migrations", "buyers", "procedures", "lots", "bidders", "bids",
    "documents", "jobs", "ai_calls", "bidder_accounts", "wl_checks",
}


def test_tables_exist(db):
    rows = db.execute(
        "select table_name from information_schema.tables where table_schema='public'"
    ).fetchall()
    assert EXPECTED <= {r["table_name"] for r in rows}


def test_migrate_is_idempotent(db):
    assert migrate(db) == []


def test_pg_trgm_enabled(db):
    row = db.execute("select extname from pg_extension where extname='pg_trgm'").fetchone()
    assert row is not None
```

Dopisz do `conftest.py`:

```python
import os

import psycopg


@pytest.fixture
def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    from tie.db import connect, migrate
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute("drop schema public cascade")
        admin.execute("create schema public")
    conn = connect(url)
    migrate(conn)
    yield conn
    conn.close()
```

- [ ] **Step 2: Push, verify FAIL** (`gh run watch --exit-status`; brak `tie.db`).

- [ ] **Step 3: Implementation** `collector/src/tie/db.py`

```python
from __future__ import annotations

from importlib import resources

import psycopg
from psycopg.rows import dict_row


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=False, row_factory=dict_row)


def migrate(conn: psycopg.Connection) -> list[str]:
    conn.execute(
        "create table if not exists schema_migrations ("
        " name text primary key, applied_at timestamptz not null default now())"
    )
    done = {r["name"] for r in conn.execute("select name from schema_migrations").fetchall()}
    files = sorted(
        p for p in resources.files("tie.migrations").iterdir() if p.name.endswith(".sql")
    )
    applied = []
    for f in files:
        if f.name in done:
            continue
        conn.execute(f.read_text(encoding="utf-8"))
        conn.execute("insert into schema_migrations(name) values (%s)", (f.name,))
        applied.append(f.name)
    conn.commit()
    return applied
```

Dodaj pusty `collector/src/tie/migrations/__init__.py`.

`collector/src/tie/migrations/001_init.sql`:

```sql
create extension if not exists pg_trgm;

create table buyers (
  id bigserial primary key,
  nip text unique,
  regon text,
  name text not null,
  buyer_type text,
  province text,
  city text,
  created_at timestamptz not null default now()
);

create table procedures (
  ocds_id text primary key,
  bzp_number text not null,
  notice_number text not null unique,
  buyer_id bigint references buyers(id),
  title text not null,
  order_type text,
  mode text,
  cpv_main text,
  cpv_all text[] not null default '{}',
  is_below_eu boolean,
  platform text,
  platform_url text,
  published_at timestamptz not null,
  raw_sha256 text,
  updated_at timestamptz not null default now()
);

create table bidders (
  id bigserial primary key,
  nip text unique,
  regon text,
  krs text,
  name text not null,
  name_norm text not null,
  is_jdg boolean,
  city text,
  province text,
  address text,
  wl_checked_at timestamptz,
  created_at timestamptz not null default now()
);
create index bidders_name_trgm on bidders using gin (name_norm gin_trgm_ops);

create table lots (
  id bigserial primary key,
  procedure_id text not null references procedures(ocds_id) on delete cascade,
  lot_no int not null,
  cpv text,
  estimated_value numeric(16,2),
  outcome text not null,
  n_offers int,
  n_rejected int,
  price_min numeric(16,2),
  price_max numeric(16,2),
  price_winner numeric(16,2),
  contract_value numeric(16,2),
  contract_date date,
  winner_bidder_id bigint references bidders(id),
  consortium boolean not null default false,
  unique (procedure_id, lot_no)
);

create table documents (
  id bigserial primary key,
  procedure_id text not null references procedures(ocds_id) on delete cascade,
  source text not null,
  source_doc_id text not null,
  file_name text not null,
  doc_type text not null,
  raw_sha256 text,
  text_method text,
  status text not null default 'new',
  error text,
  updated_at timestamptz not null default now(),
  unique (source, source_doc_id)
);

create table bids (
  id bigserial primary key,
  lot_id bigint not null references lots(id) on delete cascade,
  document_id bigint references documents(id) on delete set null,
  offer_no int,
  bidder_id bigint references bidders(id),
  bidder_name_raw text not null,
  bidder_nip_raw text,
  price numeric(16,2),
  price_scale numeric(18,6) not null default 1,
  is_winner boolean not null default false,
  rank int,
  parse_method text not null,
  match_confidence real,
  unique (lot_id, offer_no, bidder_name_raw)
);

create table jobs (
  id bigserial primary key,
  kind text not null,
  key text not null,
  payload jsonb not null default '{}',
  status text not null default 'queued',
  attempts int not null default 0,
  max_attempts int not null default 8,
  run_after timestamptz not null default now(),
  locked_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  unique (kind, key)
);
create index jobs_ready on jobs (status, run_after);

create table ai_calls (
  id bigserial primary key,
  provider text not null,
  model text not null,
  task text not null,
  input_tokens int not null,
  output_tokens int not null,
  cost_usd numeric(12,6) not null,
  latency_ms int not null,
  ok boolean not null,
  created_at timestamptz not null default now()
);

create table bidder_accounts (
  bidder_id bigint not null references bidders(id) on delete cascade,
  account_hash text not null,
  primary key (bidder_id, account_hash)
);
create index bidder_accounts_hash on bidder_accounts (account_hash);

create table wl_checks (
  day date primary key,
  requests int not null default 0
);
```

- [ ] **Step 4: Push, verify PASS, commit** (`feat: schemat bazy i migracje`).

---

### Task 3: Magazyn surowych plików

**Files:**
- Create: `collector/src/tie/rawstore.py`
- Test: `collector/tests/test_rawstore.py`

**Interfaces:**
- Produces: `RawStore(root: Path)`; `put(data: bytes, *, source: str) -> str` (zwraca sha256 hex; zapis do `root/source/aa/bb/<sha>.zst`, atomowo, bez ponownego zapisu gdy istnieje); `get(sha256: str, *, source: str) -> bytes`; `exists(sha256, *, source) -> bool`.

- [ ] **Step 1: Write the failing test**

```python
import hashlib

import pytest

from tie.rawstore import RawStore


def test_put_get_roundtrip(tmp_path):
    rs = RawStore(tmp_path)
    data = "Zażółć gęślą jaźń".encode() * 100
    sha = rs.put(data, source="bzp")
    assert sha == hashlib.sha256(data).hexdigest()
    assert rs.exists(sha, source="bzp")
    assert rs.get(sha, source="bzp") == data
    path = tmp_path / "bzp" / sha[:2] / sha[2:4] / f"{sha}.zst"
    assert path.exists()
    assert path.stat().st_size < len(data)


def test_put_twice_is_noop(tmp_path):
    rs = RawStore(tmp_path)
    a = rs.put(b"x", source="ezam")
    mtime = (tmp_path / "ezam" / a[:2] / a[2:4] / f"{a}.zst").stat().st_mtime_ns
    assert rs.put(b"x", source="ezam") == a
    assert (tmp_path / "ezam" / a[:2] / a[2:4] / f"{a}.zst").stat().st_mtime_ns == mtime


def test_get_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        RawStore(tmp_path).get("0" * 64, source="bzp")
```

- [ ] **Step 2: Push, verify FAIL.**

- [ ] **Step 3: Implementation**

```python
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import zstandard


class RawStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, sha: str, source: str) -> Path:
        return self.root / source / sha[:2] / sha[2:4] / f"{sha}.zst"

    def exists(self, sha256: str, *, source: str) -> bool:
        return self._path(sha256, source).exists()

    def put(self, data: bytes, *, source: str) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = self._path(sha, source)
        if path.exists():
            return sha
        path.parent.mkdir(parents=True, exist_ok=True)
        compressed = zstandard.ZstdCompressor(level=10).compress(data)
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        with os.fdopen(fd, "wb") as f:
            f.write(compressed)
        os.replace(tmp, path)
        return sha

    def get(self, sha256: str, *, source: str) -> bytes:
        path = self._path(sha256, source)
        return zstandard.ZstdDecompressor().decompress(path.read_bytes())
```

- [ ] **Step 4: Push, verify PASS, commit** (`feat: magazyn surowych plikow zstd`).

---

### Task 4: Klient HTTP z limitami i ponawianiem

**Files:**
- Create: `collector/src/tie/http.py`
- Test: `collector/tests/test_http.py`

**Interfaces:**
- Produces: `PoliteClient(user_agent: str, rates: dict[str, float], *, max_retries: int = 5, base_delay: float = 1.0, sleep=time.sleep, timeout: float = 60.0)`; `get(url: str, **kw) -> httpx.Response`; `post_json(url: str, json: dict, headers: dict | None = None) -> httpx.Response`. Ponawia przy 429, 500, 502, 503, 504, 529 i `httpx.TransportError`; respektuje `Retry-After` (sekundy); przerwa `base_delay * 2**attempt`; po wyczerpaniu prób rzuca `HttpGiveUp(url, status)`. Dla 4xx innych niż 429 zwraca odpowiedź bez ponawiania. `rates` to minimalny odstęp w sekundach per host.

- [ ] **Step 1: Write the failing test**

```python
import httpx
import pytest
import respx

from tie.http import HttpGiveUp, PoliteClient


def make(sleeps):
    return PoliteClient("ua-test", {"example.com": 0.0}, base_delay=1.0, sleep=sleeps.append)


@respx.mock
def test_retries_on_429_with_retry_after():
    sleeps = []
    route = respx.get("https://example.com/a").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, text="ok"),
    ])
    r = make(sleeps).get("https://example.com/a")
    assert r.text == "ok"
    assert route.call_count == 2
    assert 7 in sleeps


@respx.mock
def test_backoff_on_503_then_give_up():
    sleeps = []
    respx.get("https://example.com/b").mock(return_value=httpx.Response(503))
    c = PoliteClient("ua", {}, max_retries=3, base_delay=1.0, sleep=sleeps.append)
    with pytest.raises(HttpGiveUp):
        c.get("https://example.com/b")
    assert [s for s in sleeps if s >= 1] == [1.0, 2.0, 4.0]


@respx.mock
def test_404_returned_without_retry():
    route = respx.get("https://example.com/c").mock(return_value=httpx.Response(404))
    r = make([]).get("https://example.com/c")
    assert r.status_code == 404 and route.call_count == 1


@respx.mock
def test_user_agent_sent():
    route = respx.get("https://example.com/d").mock(return_value=httpx.Response(200))
    make([]).get("https://example.com/d")
    assert route.calls.last.request.headers["User-Agent"] == "ua-test"


@respx.mock
def test_rate_limit_spacing():
    sleeps = []
    respx.get("https://slow.pl/x").mock(return_value=httpx.Response(200))
    times = iter([100.0, 100.0, 100.2, 100.2, 101.5, 101.5])
    c = PoliteClient("ua", {"slow.pl": 1.0}, sleep=sleeps.append, clock=lambda: next(times))
    c.get("https://slow.pl/x")
    c.get("https://slow.pl/x")
    assert any(abs(s - 0.8) < 1e-6 for s in sleeps)
```

- [ ] **Step 2: Push, verify FAIL.**

- [ ] **Step 3: Implementation**

```python
from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx

RETRY_STATUSES = {429, 500, 502, 503, 504, 529}


class HttpGiveUp(Exception):
    def __init__(self, url: str, status: int | None):
        super().__init__(f"giving up on {url} (status={status})")
        self.url = url
        self.status = status


class PoliteClient:
    def __init__(self, user_agent, rates, *, max_retries=5, base_delay=1.0,
                 sleep=time.sleep, timeout=60.0, clock=time.monotonic):
        self.rates = dict(rates)
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.sleep = sleep
        self.clock = clock
        self._last: dict[str, float] = {}
        self.client = httpx.Client(headers={"User-Agent": user_agent}, timeout=timeout,
                                   follow_redirects=True)

    def _wait_turn(self, host: str) -> None:
        gap = self.rates.get(host, 0.0)
        now = self.clock()
        last = self._last.get(host)
        if last is not None and gap > 0 and now - last < gap:
            self.sleep(gap - (now - last))
        self._last[host] = self.clock()

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        host = urlparse(url).hostname or ""
        status = None
        for attempt in range(self.max_retries + 1):
            self._wait_turn(host)
            try:
                r = self.client.request(method, url, **kw)
            except httpx.TransportError:
                r = None
            if r is not None and r.status_code not in RETRY_STATUSES:
                return r
            status = None if r is None else r.status_code
            if attempt == self.max_retries:
                break
            delay = self.base_delay * 2**attempt
            if r is not None and r.headers.get("Retry-After", "").isdigit():
                delay = float(r.headers["Retry-After"])
            self.sleep(delay)
        raise HttpGiveUp(url, status)

    def get(self, url: str, **kw) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post_json(self, url: str, json: dict, headers: dict | None = None) -> httpx.Response:
        return self.request("POST", url, json=json, headers=headers or {})
```

Uwaga: w `test_backoff_on_503_then_give_up` `max_retries=3` daje 4 próby i 3 przerwy: 1, 2, 4.

- [ ] **Step 4: Push, verify PASS, commit** (`feat: PoliteClient z limitami i ponawianiem`).

---

### Task 5: Kolejka zadań w Postgresie

**Files:**
- Create: `collector/src/tie/jobs.py`
- Test: `collector/tests/test_jobs.py`

**Interfaces:**
- Consumes: `connect`, fixture `db`.
- Produces: `enqueue(conn, kind: str, key: str, payload: dict | None = None, run_after: datetime | None = None) -> bool` (False, gdy (kind,key) już istnieje); `claim(conn, kinds: list[str], limit: int = 1) -> list[dict]` (`FOR UPDATE SKIP LOCKED`, ustawia `status='running'`, `locked_at=now()`, `attempts+1`); `complete(conn, job_id: int) -> None` (`status='done'`); `fail(conn, job_id: int, error: str, *, now: datetime | None = None) -> None` (jeśli `attempts >= max_attempts` to `status='failed'`, inaczej `status='queued'` i `run_after = now + 2**attempts minut`); `requeue_stale(conn, older_than: timedelta) -> int`. Każda funkcja robi `commit`.

- [ ] **Step 1: Write the failing test**

```python
from datetime import UTC, datetime, timedelta

from tie.jobs import claim, complete, enqueue, fail, requeue_stale


def test_enqueue_dedup(db):
    assert enqueue(db, "bzp_day", "2026-10-01") is True
    assert enqueue(db, "bzp_day", "2026-10-01") is False


def test_claim_complete(db):
    enqueue(db, "bzp_day", "d1", {"date": "2026-10-01"})
    [job] = claim(db, ["bzp_day"])
    assert job["payload"] == {"date": "2026-10-01"} and job["attempts"] == 1
    assert claim(db, ["bzp_day"]) == []
    complete(db, job["id"])
    row = db.execute("select status from jobs where id=%s", (job["id"],)).fetchone()
    assert row["status"] == "done"


def test_fail_backoff_then_failed(db):
    enqueue(db, "k", "x")
    db.execute("update jobs set max_attempts=2")
    db.commit()
    now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    [j] = claim(db, ["k"])
    fail(db, j["id"], "boom", now=now)
    row = db.execute("select status, run_after, last_error from jobs").fetchone()
    assert row["status"] == "queued" and row["last_error"] == "boom"
    assert row["run_after"] == now + timedelta(minutes=2)
    db.execute("update jobs set run_after=now()")
    db.commit()
    [j] = claim(db, ["k"])
    fail(db, j["id"], "boom2", now=now)
    assert db.execute("select status from jobs").fetchone()["status"] == "failed"


def test_future_job_not_claimed(db):
    enqueue(db, "k", "later", run_after=datetime.now(UTC) + timedelta(hours=1))
    assert claim(db, ["k"]) == []


def test_requeue_stale(db):
    enqueue(db, "k", "s")
    claim(db, ["k"])
    db.execute("update jobs set locked_at = now() - interval '2 hours'")
    db.commit()
    assert requeue_stale(db, timedelta(hours=1)) == 1
    assert claim(db, ["k"]) != []
```

- [ ] **Step 2: Push, verify FAIL.**

- [ ] **Step 3: Implementation**

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb


def enqueue(conn, kind, key, payload=None, run_after=None) -> bool:
    row = conn.execute(
        "insert into jobs(kind, key, payload, run_after) values (%s,%s,%s,coalesce(%s, now()))"
        " on conflict (kind, key) do nothing returning id",
        (kind, key, Jsonb(payload or {}), run_after),
    ).fetchone()
    conn.commit()
    return row is not None


def claim(conn, kinds, limit=1) -> list[dict]:
    rows = conn.execute(
        """
        update jobs set status='running', locked_at=now(), attempts=attempts+1
        where id in (
          select id from jobs
          where status='queued' and run_after <= now() and kind = any(%s)
          order by run_after, id
          for update skip locked
          limit %s)
        returning *
        """,
        (list(kinds), limit),
    ).fetchall()
    conn.commit()
    return rows


def complete(conn, job_id) -> None:
    conn.execute("update jobs set status='done', locked_at=null where id=%s", (job_id,))
    conn.commit()


def fail(conn, job_id, error, *, now=None) -> None:
    now = now or datetime.now(UTC)
    job = conn.execute("select attempts, max_attempts from jobs where id=%s",
                       (job_id,)).fetchone()
    if job["attempts"] >= job["max_attempts"]:
        conn.execute("update jobs set status='failed', locked_at=null, last_error=%s"
                     " where id=%s", (error[:2000], job_id))
    else:
        run_after = now + timedelta(minutes=2 ** job["attempts"])
        conn.execute("update jobs set status='queued', locked_at=null, last_error=%s,"
                     " run_after=%s where id=%s", (error[:2000], run_after, job_id))
    conn.commit()


def requeue_stale(conn, older_than) -> int:
    cur = conn.execute(
        "update jobs set status='queued', locked_at=null"
        " where status='running' and locked_at < now() - %s", (older_than,))
    conn.commit()
    return cur.rowcount
```

- [ ] **Step 4: Push, verify PASS, commit** (`feat: kolejka zadan w postgresie`).

---

### Task 6: Parser ogłoszenia o wyniku z BZP

**Files:**
- Create: `collector/src/tie/money.py`, `collector/src/tie/bzp/__init__.py`, `collector/src/tie/bzp/parser.py`
- Create: `collector/src/tie/identity/__init__.py`, `collector/src/tie/identity/names.py` (pełna specyfikacja i kod w Task 8, Step 3; zaimplementuj tutaj, bo Task 11 i 12 z niego korzystają równolegle z Task 8)
- Test: `collector/tests/test_money.py`, `collector/tests/test_bzp_parser.py`, `collector/tests/test_names.py` (przenieś tu `test_normalize` z Task 8)

**Interfaces:**
- Produces (`money.py`): `parse_amount(text: str) -> Decimal | None` (formaty: `32997,39 PLN`, `213 192,00 zł`, `1.162.074,00`, `45,63`, spacja twarda U+00A0; zwraca None, gdy brak części `,dd` lub `.dd`); `valid_nip(nip: str) -> bool` (10 cyfr, suma kontrolna wagi 6,5,7,2,3,4,5,6,7); `clean_nip(text: str) -> str | None` (usuwa `-`, spacje, prefiks `PL`; zwraca 10 cyfr tylko gdy `valid_nip`).
- Produces (`bzp/parser.py`):

```python
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
    outcome: str              # "umowa" | "uniewaznienie"
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
    order_type: str | None    # "Works" | "Supplies" | "Services"
    mode: str | None          # tekst z 3.1, np. "art. 275 pkt 1"
    cpv_all: list[str]        # same kody, np. ["45453000-7", ...]
    is_below_eu: bool | None
    platform_url: str | None
    platform: str | None      # "ezamowienia" | "platformazakupowa" | "ezamawiajacy" | "logintrade" | "e-propublico" | "other"
    lots: list[LotResult]

def parse_result_notice(record: dict) -> ResultNotice
```

Reguły parsowania (`htmlBody`):
1. Zamień HTML na listę linii: każdy element `h1`-`h4` z selectolax to jedna linia, `node.text(separator=" ")`, białe znaki zwinięte do jednej spacji, obcięte.
2. Pole = linia pasująca do `^(\d+(?:\.\d+)*\.?\))\s*(.*?):\s*(.*)$`; klucz to kod (np. `6.2.)`), wartość to grupa 3.
3. Bloki części: linie od `SEKCJA V ZAKOŃCZENIE POSTĘPOWANIA (dla części N)` do następnej takiej linii; gdy brak "(dla części", jeden blok od pierwszej linii zaczynającej się od `SEKCJA V ZAKOŃCZENIE` z `lot_no=1`.
4. CPV i wartość części: w sekcji IV pary `Część N` / `4.5.3.) Główny kod CPV` / `4.5.5.) Wartość części`; dla jednej części `4.5.3.)` i `4.3.1)` lub `4.5.5.)`.
5. `outcome`: wartość `5.1.)` zawiera "unieważnieniem" -> `"uniewaznienie"`, inaczej `"umowa"`.
6. Zwycięzca: `7.1.)` "Tak" -> consortium; nazwa z linii zaczynającej się od `7.3.1)` (wartość po ostatnim `: ` w obrębie nazwy pola, ucięta przed tekstem `Nazwy (firm`); NIP z `7.3.2)` przez `clean_nip`; miejscowość `7.3.4)`, województwo `7.3.6.)`.
7. `platform_url`: z surowego HTML regex `1\.6\.\) Adres strony internetowej prowadzonego postępowania:\s*</h3>\s*([^<\s]+)`; `platform` po domenie (`ezamowienia.gov.pl` -> `ezamowienia`, `platformazakupowa.pl`, `ezamawiajacy.pl`, `logintrade.net`, `e-propublico.pl`, reszta `other`).
8. Pola rekordu JSON: `bzpNumber`, `noticeNumber`, `tenderId`, `publicationDate`, `organizationName`, `organizationNationalId` (przez `clean_nip`), `organizationProvince`, `organizationCity`, `orderObject` (title), `orderType`, `isTenderAmountBelowEU`; REGON z `1.4)` gdy wartość zaczyna się od `REGON`.

- [ ] **Step 1: Write the failing tests**

`collector/tests/test_money.py`:

```python
from decimal import Decimal

from tie.money import clean_nip, parse_amount, valid_nip


def test_amounts():
    assert parse_amount("32997,39 PLN") == Decimal("32997.39")
    assert parse_amount("213 192,00 zł") == Decimal("213192.00")
    assert parse_amount("213 192,00 zł") == Decimal("213192.00")
    assert parse_amount("1.162.074,00") == Decimal("1162074.00")
    assert parse_amount("45,63") == Decimal("45.63")
    assert parse_amount("2026") is None
    assert parse_amount("brak") is None


def test_nip():
    assert valid_nip("6791021905")
    assert valid_nip("5932335316")
    assert not valid_nip("6791021906")
    assert clean_nip("PL 679-102-19-05") == "6791021905"
    assert clean_nip("599136236") is None
```

`collector/tests/test_bzp_parser.py`:

```python
import json
from datetime import date
from decimal import Decimal

from tie.bzp.parser import parse_result_notice


def load(fixtures, name):
    return json.loads((fixtures / "bzp" / name).read_text(encoding="utf-8"))


def test_multilot(fixtures):
    n = parse_result_notice(load(fixtures, "result_multilot.json"))
    assert n.bzp_number == "2026/BZP 00464943"
    assert n.ocds_id == "ocds-148610-a4d0d5c2-66fb-47a0-ab8f-3f89c485600e"
    assert n.buyer_nip == "6751000311"
    assert n.buyer_regon == "350768365"
    assert n.platform == "ezamawiajacy"
    assert n.platform_url == "https://zbkkrakow.ezamawiajacy.pl/"
    assert n.order_type == "Works"
    assert "45453000-7" in n.cpv_all
    assert len(n.lots) == 6
    l1 = n.lots[0]
    assert l1.lot_no == 1 and l1.outcome == "umowa"
    assert l1.n_offers == 4 and l1.n_rejected == 0
    assert l1.price_min == Decimal("32997.39") and l1.price_max == Decimal("39308.97")
    assert l1.price_winner == Decimal("32997.39")
    assert l1.contract_date == date(2026, 9, 30)
    assert l1.estimated_value == Decimal("31494.02")
    assert l1.cpv == "45453000-7"
    assert l1.winner.nip == "6821618773" and l1.winner.name == "PRO-REM Łukasz Solarz"
    assert l1.winner.consortium is False
    l4 = n.lots[3]
    assert l4.n_offers == 8 and l4.winner.nip == "6762554220"


def test_single_on_ezamowienia(fixtures):
    n = parse_result_notice(load(fixtures, "result_single.json"))
    assert n.platform == "ezamowienia"
    assert n.ocds_id == "ocds-148610-31180df3-a1c9-47cf-8061-b35e3d3d4483"
    assert len(n.lots) == 1
    assert n.lots[0].n_offers == 1
    assert n.lots[0].price_min == Decimal("499998.96")
    assert n.lots[0].winner.name.startswith("GE Medical Systems Polska")
    assert n.lots[0].cpv == "33112340-3"


def test_cancelled(fixtures):
    n = parse_result_notice(load(fixtures, "result_cancelled.json"))
    assert all(lot.outcome == "uniewaznienie" for lot in n.lots)
    assert all(lot.winner is None for lot in n.lots)


def test_consortium(fixtures):
    n = parse_result_notice(load(fixtures, "result_consortium.json"))
    w = n.lots[0].winner
    assert w.consortium is True and w.nip == "8581157491"
    assert w.name.startswith("Anka-T FHU")


def test_spytkowo(fixtures):
    n = parse_result_notice(load(fixtures, "result_spytkowo.json"))
    lot = n.lots[0]
    assert lot.n_offers == 8
    assert lot.price_min == Decimal("239497.36") and lot.price_max == Decimal("301571.04")
    assert lot.winner.nip == "6791021905"
```

- [ ] **Step 2: Push, verify FAIL.**

- [ ] **Step 3: Implementation** `money.py`:

```python
from __future__ import annotations

import re
from decimal import Decimal

_AMOUNT = re.compile(r"(\d{1,3}(?:[  .]\d{3})+|\d+)([,.]\d{2})(?!\d)")
_WEIGHTS = (6, 5, 7, 2, 3, 4, 5, 6, 7)


def parse_amount(text: str) -> Decimal | None:
    m = _AMOUNT.search(text or "")
    if not m:
        return None
    whole = re.sub(r"[  .]", "", m.group(1))
    return Decimal(f"{whole}.{m.group(2)[1:]}")


def valid_nip(nip: str) -> bool:
    if not re.fullmatch(r"\d{10}", nip or ""):
        return False
    s = sum(int(d) * w for d, w in zip(nip[:9], _WEIGHTS, strict=True)) % 11
    return s != 10 and s == int(nip[9])


def clean_nip(text: str) -> str | None:
    digits = re.sub(r"\D", "", re.sub(r"(?i)^\s*PL", "", text or ""))
    return digits if valid_nip(digits) else None
```

`bzp/parser.py`: zaimplementuj dataclassy z sekcji Interfaces i funkcję `parse_result_notice` według reguł 1-8. Pomocnicze funkcje prywatne: `_lines(html) -> list[str]`, `_fields(lines) -> list[tuple[str, str]]` (kod, wartość, w kolejności), `_lot_blocks(lines) -> list[tuple[int, list[str]]]`, `_section_iv(lines) -> dict[int, tuple[str | None, Decimal | None]]`, `_winner(fields) -> Winner | None`, `_platform(url) -> str | None`. `published_at` przez `datetime.fromisoformat(record["publicationDate"].replace("Z", "+00:00"))` z obcięciem ułamków sekund do 6 cyfr. Zwycięzca tylko gdy `outcome == "umowa"` i w bloku jest `7.3.1)`.

- [ ] **Step 4: Push, verify PASS.** Jeśli test nie przechodzi z powodu różnic w formacie HTML, popraw parser (nie test); wartości oczekiwane pochodzą wprost z fixtures.

- [ ] **Step 5: Commit** (`feat: parser ogloszen o wyniku BZP`).

---

### Task 7: Pobieranie dni z BZP z podziałem

**Files:**
- Create: `collector/src/tie/bzp/fetch.py`
- Test: `collector/tests/test_bzp_fetch.py`

**Interfaces:**
- Consumes: `PoliteClient`, `RawStore`.
- Produces: `BZP_URL = "https://ezamowienia.gov.pl/mo-board/api/v1/notice"`; `PROVINCES = ["PL02","PL04",...,"PL32"]` (16 kodów co 2); `ORDER_TYPES = ["Works","Supplies","Services"]`; `fetch_day(client, day: date, *, page_size: int = 500) -> list[dict]` (zwraca unikalne rekordy po `noticeNumber`). Algorytm: zapytanie bez filtrów; jeśli wynik ma mniej niż `page_size` rekordów, koniec. W przeciwnym razie zapytania per województwo; jeśli któreś zwróci `page_size`, dla niego zapytania per `OrderType`; jeśli nadal `page_size`, loguje ostrzeżenie `bzp_partition_saturated` z parametrami. Do wyniku dokłada też rekordy z zapytania bez filtrów (union, dedup). Parametry: `NoticeType=TenderResultNotice`, `PublicationDateFrom=YYYY-MM-DD`, `PublicationDateTo=YYYY-MM-DD`, `PageSize`, opcjonalnie `OrganizationProvince`, `OrderType`. `store_day(rawstore, day, records) -> str` zapisuje JSON listy rekordów (posortowanej po `noticeNumber`, `ensure_ascii=False`) jako surowy plik `source="bzp"` i zwraca sha.

- [ ] **Step 1: Write the failing test**

```python
import json
from datetime import date

import httpx
import respx

from tie.bzp.fetch import BZP_URL, fetch_day, store_day
from tie.http import PoliteClient
from tie.rawstore import RawStore


def rec(n, prov="PL14", otype="Works"):
    return {"noticeNumber": f"2026/BZP {n:08d}/01", "organizationProvince": prov,
            "orderType": otype}


def client():
    return PoliteClient("ua", {}, sleep=lambda s: None)


@respx.mock
def test_small_day_single_call():
    route = respx.get(BZP_URL).mock(return_value=httpx.Response(200, json=[rec(1), rec(2)]))
    out = fetch_day(client(), date(2026, 10, 1), page_size=5)
    assert len(out) == 2 and route.call_count == 1
    q = route.calls.last.request.url.params
    assert q["NoticeType"] == "TenderResultNotice"
    assert q["PublicationDateFrom"] == "2026-10-01" == q["PublicationDateTo"]


@respx.mock
def test_saturated_day_splits_by_province_then_order_type():
    def handler(request):
        p = request.url.params
        if "OrganizationProvince" not in p:
            return httpx.Response(200, json=[rec(i) for i in range(3)])
        if p["OrganizationProvince"] == "PL14" and "OrderType" not in p:
            return httpx.Response(200, json=[rec(i) for i in range(3)])
        if p["OrganizationProvince"] == "PL14":
            base = {"Works": 10, "Supplies": 20, "Services": 30}[p["OrderType"]]
            return httpx.Response(200, json=[rec(base + i) for i in range(2)])
        if p["OrganizationProvince"] == "PL02":
            return httpx.Response(200, json=[rec(100, "PL02")])
        return httpx.Response(200, json=[])

    respx.get(BZP_URL).mock(side_effect=handler)
    out = fetch_day(client(), date(2026, 10, 1), page_size=3)
    numbers = {r["noticeNumber"] for r in out}
    assert len(numbers) == len(out)
    assert {"2026/BZP 00000010/01", "2026/BZP 00000031/01", "2026/BZP 00000100/01"} <= numbers
    assert "2026/BZP 00000000/01" in numbers


def test_store_day(tmp_path):
    rs = RawStore(tmp_path)
    sha = store_day(rs, date(2026, 10, 1), [rec(2), rec(1)])
    data = json.loads(rs.get(sha, source="bzp"))
    assert [r["noticeNumber"] for r in data] == ["2026/BZP 00000001/01", "2026/BZP 00000002/01"]
```

- [ ] **Step 2: Push, verify FAIL.**

- [ ] **Step 3: Implementation**

```python
from __future__ import annotations

import json
import logging
from datetime import date

BZP_URL = "https://ezamowienia.gov.pl/mo-board/api/v1/notice"
PROVINCES = [f"PL{n:02d}" for n in range(2, 33, 2)]
ORDER_TYPES = ["Works", "Supplies", "Services"]
log = logging.getLogger(__name__)


def _query(client, day, page_size, **extra) -> list[dict]:
    params = {"NoticeType": "TenderResultNotice", "PublicationDateFrom": day.isoformat(),
              "PublicationDateTo": day.isoformat(), "PageSize": page_size, **extra}
    r = client.get(BZP_URL, params=params)
    r.raise_for_status()
    return r.json()


def fetch_day(client, day: date, *, page_size: int = 500) -> list[dict]:
    seen: dict[str, dict] = {}

    def add(rows):
        for r in rows:
            seen.setdefault(r["noticeNumber"], r)

    base = _query(client, day, page_size)
    add(base)
    if len(base) < page_size:
        return list(seen.values())
    for prov in PROVINCES:
        rows = _query(client, day, page_size, OrganizationProvince=prov)
        add(rows)
        if len(rows) < page_size:
            continue
        for ot in ORDER_TYPES:
            sub = _query(client, day, page_size, OrganizationProvince=prov, OrderType=ot)
            add(sub)
            if len(sub) >= page_size:
                log.warning("bzp_partition_saturated day=%s prov=%s type=%s", day, prov, ot)
    return list(seen.values())


def store_day(rawstore, day: date, records: list[dict]) -> str:
    rows = sorted(records, key=lambda r: r["noticeNumber"])
    return rawstore.put(json.dumps(rows, ensure_ascii=False).encode(), source="bzp")
```

- [ ] **Step 4: Push, verify PASS, commit** (`feat: pobieranie dnia BZP z podzialem`).

---

### Task 8: Zapis ogłoszeń do bazy i handler dnia

**Files:**
- Create: `collector/src/tie/bzp/store.py`
- Uses (powstaje w Task 6): `collector/src/tie/identity/names.py`; poniższa specyfikacja `names.py` jest kanoniczna dla Task 6, a test `test_normalize` mieszka w `test_names.py`
- Test: `collector/tests/test_bzp_store.py`

**Interfaces:**
- Consumes: `ResultNotice`, `LotResult`, `Winner`, `enqueue`.
- Produces (`identity/names.py`): `normalize_name(name: str) -> str` (małe litery, bez polskich znaków, bez cudzysłowów i interpunkcji, formy prawne sprowadzone do tokenów: `spolka z ograniczona odpowiedzialnoscia`/`sp z o o`/`sp zoo` -> `spzoo`, `spolka akcyjna`/`s a` -> `sa`, `spolka jawna`/`sp j` -> `spj`, `spolka komandytowa`/`sp k` -> `spk`, `spolka cywilna`/`s c` -> `sc`; zwinięte spacje); `looks_like_company(name: str) -> bool` (True gdy po normalizacji zawiera token z: `spzoo`, `sa`, `spj`, `spk`, `ska`, `spp`, `spoldzielnia`, `fundacja`, `stowarzyszenie`, `instytut`, `uniwersytet`, `przedsiebiorstwo panstwowe`).
- Produces (`bzp/store.py`): `upsert_notice(conn, notice: ResultNotice, raw_sha256: str) -> dict` zwraca `{"ocds_id": str, "lots": int, "needs_opening": bool}`; `upsert_bidder(conn, *, name: str, nip: str | None, city: str | None, province: str | None) -> int` (po NIP, gdy jest; bez NIP po `(name_norm, city)`; ustawia `is_jdg = not looks_like_company(name)` tylko przy tworzeniu). `needs_opening` = postępowanie na platformie `ezamowienia` i co najmniej jedna część ma `n_offers >= 2`. Po zapisie, gdy `needs_opening`, `enqueue(conn, "ezam_docs", ocds_id, {"ocds_id": ocds_id})`. Ponowny zapis tego samego ogłoszenia nadpisuje wartości (upsert), nie dubluje wierszy.
- Uwaga: zwycięzca konsorcjum zapisuje się jako lider (nazwa i NIP z 7.3.x); `lots.consortium = true`.

- [ ] **Step 1: Write the failing test**

```python
import json

from tie.bzp.parser import parse_result_notice
from tie.bzp.store import upsert_notice
from tie.identity.names import looks_like_company, normalize_name


def notice(fixtures, name):
    return parse_result_notice(json.loads((fixtures / "bzp" / name).read_text("utf-8")))


def test_normalize():
    assert normalize_name('MULTISERVICE Sp. z o.o.') == "multiservice spzoo"
    assert normalize_name("„STEKOP” S.A.") == "stekop sa"
    assert looks_like_company("Multi Projekt Sp. z o.o.")
    assert not looks_like_company("PRO-REM Łukasz Solarz")


def test_upsert_multilot_idempotent(db, fixtures):
    n = notice(fixtures, "result_multilot.json")
    out = upsert_notice(db, n, "sha1")
    assert out == {"ocds_id": n.ocds_id, "lots": 6, "needs_opening": False}
    upsert_notice(db, n, "sha1")
    assert db.execute("select count(*) c from lots").fetchone()["c"] == 6
    assert db.execute("select count(*) c from procedures").fetchone()["c"] == 1
    b = db.execute("select * from bidders where nip='6821618773'").fetchone()
    assert b["is_jdg"] is True
    assert db.execute("select count(*) c from bidders where nip='9930454088'").fetchone()["c"] == 1


def test_spytkowo_enqueues_documents(db, fixtures):
    n = notice(fixtures, "result_spytkowo.json")
    out = upsert_notice(db, n, "sha2")
    assert out["needs_opening"] is True
    job = db.execute("select * from jobs where kind='ezam_docs'").fetchone()
    assert job["key"] == n.ocds_id


def test_single_offer_does_not_enqueue(db, fixtures):
    out = upsert_notice(db, notice(fixtures, "result_single.json"), "sha3")
    assert out["needs_opening"] is False
    assert db.execute("select count(*) c from jobs").fetchone()["c"] == 0


def test_consortium_flag(db, fixtures):
    upsert_notice(db, notice(fixtures, "result_consortium.json"), "sha4")
    lot = db.execute("select consortium from lots order by lot_no limit 1").fetchone()
    assert lot["consortium"] is True
```

- [ ] **Step 2: Push, verify FAIL.**

- [ ] **Step 3: Implementation.** `identity/names.py`:

```python
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
```

`bzp/store.py`: `upsert_bidder` z `insert ... on conflict (nip) do update set name=excluded.name, city=coalesce(excluded.city, bidders.city), province=coalesce(...)` dla NIP; bez NIP `select id from bidders where nip is null and name_norm=%s and coalesce(city,'')=coalesce(%s,'')` i insert gdy brak. Kupujący: upsert po `nip` (gdy brak NIP po `(name, city)` analogicznie). `procedures`: `insert ... on conflict (ocds_id) do update` wszystkich pól i `updated_at=now()`. `lots`: `insert ... on conflict (procedure_id, lot_no) do update`. Wszystko w jednej transakcji, `commit` na końcu, potem `enqueue`.

- [ ] **Step 4: Push, verify PASS, commit** (`feat: zapis ogloszen BZP i normalizacja nazw`).

---

### Task 9: Dokumenty z e-Zamówień

**Files:**
- Create: `collector/src/tie/ezam/__init__.py`, `collector/src/tie/ezam/documents.py`
- Test: `collector/tests/test_ezam_documents.py`

**Interfaces:**
- Consumes: `PoliteClient`, `RawStore`, `enqueue`.
- Produces: `DOCS_URL = "https://ezamowienia.gov.pl/mp-readmodels/api/Search/GetTenderDocuments"`; `DOWNLOAD_URL = "https://ezamowienia.gov.pl/mp-readmodels/api/Tender/DownloadDocument/{ocds}/{doc}"`; `classify(name: str, file_name: str) -> str` zwraca `"opening"` (regex `otwarci|zestawienie ofert|zbiorcze zestawienie` po normalizacji bez polskich znaków, małe litery), `"selection"` (`wybor|wyborze|najkorzystniejsz`), `"cancellation"` (`uniewaznien`), inaczej `"other"`; `list_documents(client, ocds_id) -> list[dict]` (pola `objectId`, `name`, `fileName`, `tenderDocumentState`; pomija stan inny niż `Published`); `sync_documents(conn, client, ocds_id) -> int` (zapisuje do `documents` wszystkie dokumenty typu opening/selection/cancellation z `source="ezamowienia"`, `source_doc_id=objectId`, upsert; dla typu `opening` robi `enqueue(conn, "ezam_download", objectId, {"ocds_id":..., "doc_id":...})`; zwraca liczbę dokumentów opening); `download_document(conn, client, rawstore, ocds_id, doc_id) -> str` (pobiera plik, zapisuje surowy plik `source="ezam"`, ustawia `documents.raw_sha256`, `status='downloaded'`, `enqueue(conn, "opening_parse", doc_id, {"document_id": id})`; zwraca sha).

- [ ] **Step 1: Write the failing test**

```python
import json

import httpx
import respx

from tie.bzp.parser import parse_result_notice
from tie.bzp.store import upsert_notice
from tie.ezam.documents import (DOCS_URL, DOWNLOAD_URL, classify, download_document,
                                  sync_documents)
from tie.http import PoliteClient
from tie.rawstore import RawStore

OCDS = "ocds-148610-31180df3-a1c9-47cf-8061-b35e3d3d4483"


def test_classify():
    assert classify("ZBIORCZE ZESTAWIENIE OFERT", "INFORMACJA Z OTWARCIA OFERT.pdf") == "opening"
    assert classify("Informacja", "Informacja_z_otwarcia_ofert.pdf") == "opening"
    assert classify("INFORMACJA O WYBORZE OFERTY NAJKORZYSTNIEJSZEJ", "x.pdf") == "selection"
    assert classify("Zawiadomienie o unieważnieniu postępowania", "u.pdf") == "cancellation"
    assert classify("SWZ", "SWZ.pdf") == "other"


@respx.mock
def test_sync_and_download(db, fixtures, tmp_path):
    rec = json.loads((fixtures / "bzp" / "result_single.json").read_text("utf-8"))
    upsert_notice(db, parse_result_notice(rec), "sha")
    docs = json.loads((fixtures / "ezam" / "documents_31180df3.json").read_text("utf-8"))
    respx.get(DOCS_URL).mock(return_value=httpx.Response(200, json=docs))
    client = PoliteClient("ua", {}, sleep=lambda s: None)
    assert sync_documents(db, client, OCDS) == 1
    assert sync_documents(db, client, OCDS) == 1
    rows = db.execute("select doc_type, count(*) c from documents group by doc_type").fetchall()
    counts = {r["doc_type"]: r["c"] for r in rows}
    assert counts["opening"] == 1 and counts["selection"] == 1
    job = db.execute("select * from jobs where kind='ezam_download'").fetchone()
    doc_id = job["payload"]["doc_id"]
    assert doc_id == f"{OCDS}_17"
    pdf = (fixtures / "openings" / "kielce_scan.pdf").read_bytes()
    respx.get(DOWNLOAD_URL.format(ocds=OCDS, doc=doc_id)).mock(
        return_value=httpx.Response(200, content=pdf, headers={"Content-Type": "application/pdf"}))
    sha = download_document(db, client, RawStore(tmp_path), OCDS, doc_id)
    row = db.execute("select status, raw_sha256 from documents where source_doc_id=%s",
                     (doc_id,)).fetchone()
    assert row["status"] == "downloaded" and row["raw_sha256"] == sha
    assert db.execute("select count(*) c from jobs where kind='opening_parse'").fetchone()["c"] == 1
```

- [ ] **Step 2: Push, verify FAIL.**

- [ ] **Step 3: Implementation** według Interfaces. `list_documents`: `client.get(DOCS_URL, params={"tenderId": ocds_id})`, `raise_for_status`, filtr `tenderDocumentState == "Published"`. `classify` normalizuje przez `identity.names.normalize_name` na złączeniu `name + " " + file_name`. Upsert `documents` przez `on conflict (source, source_doc_id) do update set file_name=excluded.file_name, doc_type=excluded.doc_type, updated_at=now()` (bez nadpisywania `status` i `raw_sha256`).

- [ ] **Step 4: Push, verify PASS, commit** (`feat: dokumenty z e-Zamowien`).

---

### Task 10: Ekstrakcja tekstu (PDF, DOCX, OCR)

**Files:**
- Create: `collector/src/tie/openings/__init__.py`, `collector/src/tie/openings/extract.py`
- Test: `collector/tests/test_extract.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class Extracted: text: str; method: str` (`"pdftext" | "docx" | "ocr"`); `extract_text(data: bytes, file_name: str) -> Extracted`. PDF: `pdftotext -layout - -` przez `subprocess.run(input=data, capture_output=True, timeout=60)`; gdy wynik po usunięciu białych znaków ma mniej niż 200 znaków, OCR: zapis do katalogu tymczasowego, `pdftoppm -r 300 -png file.pdf page`, dla każdej strony `tesseract page-N.png - -l pol --psm 6`, złączenie stron `\n\f\n`. DOCX: `python-docx`, akapity i komórki tabel w kolejności dokumentu (wiersz tabeli = komórki złączone ` | `). Inne rozszerzenia: `UnsupportedDocument`.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from tie.openings.extract import UnsupportedDocument, extract_text


def read(fixtures, name):
    return (fixtures / "openings" / name).read_bytes()


def test_text_pdf(fixtures):
    e = extract_text(read(fixtures, "kolbudy_text.pdf"), "kolbudy_text.pdf")
    assert e.method == "pdftext"
    assert "5932335316" in e.text and "213 192,00" in e.text


def test_docx(fixtures):
    e = extract_text(read(fixtures, "rogow_docx.docx"), "x.docx")
    assert e.method == "docx"
    assert "366 120,00" in e.text and "770 901,55" in e.text


def test_scan_uses_ocr(fixtures):
    e = extract_text(read(fixtures, "kielce_scan.pdf"), "scan.pdf")
    assert e.method == "ocr"
    assert len(e.text.strip()) > 200
    assert "ofert" in e.text.lower()


def test_unsupported():
    with pytest.raises(UnsupportedDocument):
        extract_text(b"x", "a.xlsx")
```

- [ ] **Step 2: Push, verify FAIL.**

- [ ] **Step 3: Implementation** według Interfaces (moduły `subprocess`, `tempfile`, `pathlib`, `io`, `docx.Document`). Iteracja po elementach DOCX w kolejności: `for child in doc.element.body.iterchildren()`; `w:p` -> akapit, `w:tbl` -> tabela.

- [ ] **Step 4: Push, verify PASS, commit** (`feat: ekstrakcja tekstu z PDF, DOCX i OCR`).

---

### Task 11: Parser deterministyczny dokumentu otwarcia

**Files:**
- Create: `collector/src/tie/openings/parse.py`
- Test: `collector/tests/test_opening_parse.py`

**Interfaces:**
- Consumes: `parse_amount`, `clean_nip`, `Extracted.text`.
- Produces:

```python
@dataclass(frozen=True)
class OfferRow:
    offer_no: int | None
    bidder_name: str          # surowy tekst, może być pusty w parserze deterministycznym
    nip: str | None
    price: Decimal | None

@dataclass(frozen=True)
class ParsedOpening:
    budget: Decimal | None    # "kwota przeznaczona na realizację zamówienia"
    prices: list[Decimal]     # wszystkie ceny ofert w kolejności
    nips: list[str]           # NIP-y oferentów w kolejności (bez NIP zamawiającego)
    offers: list[OfferRow]    # wypełnione tylko gdy len(nips) == len(prices) > 0
    method: str               # "deterministic"

def parse_opening(text: str, *, buyer_nip: str | None = None) -> ParsedOpening
```

Reguły:
1. Budżet: pierwsza kwota w linii zawierającej (po normalizacji) `kwota` i (`przeznacz` lub `zamierza`).
2. Linie wykluczone z cen i NIP-ów: zawierające `kwota`, `kapital`, `regon`, `krs`, `tel`, `fax`, `bdo`, `strona \d+`, `dz u`, `poz `, `termin skladania`, oraz linia budżetu.
3. NIP-y: wszystkie `clean_nip` z dopasowań `\b(?:PL)?\d{3}[- ]?\d{3}[- ]?\d{2}[- ]?\d{2}\b` i `\b\d{10}\b` w liniach niewykluczonych, z pominięciem `buyer_nip` i NIP-ów występujących w ostatnich 8 liniach dokumentu (stopka), duplikaty kolejne usunięte.
4. Ceny: w każdej linii niewykluczonej ostatnia kwota pasująca do `parse_amount`, o ile linia nie jest datą (`\d{2}\.\d{2}\.\d{4}`) bez innych kwot.
5. `offers`: gdy `len(nips) == len(prices) > 0`, pary w kolejności wystąpienia, `offer_no = i+1`, `bidder_name = ""`.

- [ ] **Step 1: Write the failing test**

```python
from decimal import Decimal as D

from tie.openings.extract import extract_text
from tie.openings.parse import parse_opening


def text(fixtures, name):
    return extract_text((fixtures / "openings" / name).read_bytes(), name).text


def test_kolbudy_single_with_nip(fixtures):
    p = parse_opening(text(fixtures, "kolbudy_text.pdf"))
    assert p.budget == D("380000.00")
    assert p.prices == [D("213192.00")]
    assert p.nips == ["5932335316"]
    assert len(p.offers) == 1 and p.offers[0].nip == "5932335316"


def test_spytkowo_unit_prices_no_bidder_nips(fixtures):
    p = parse_opening(text(fixtures, "spytkowo_unit_prices.pdf"), buyer_nip="8451958301")
    assert p.prices == [D("45.63"), D("40.18"), D("49.02"), D("44.78"), D("39.85"),
                        D("43.42"), D("41.82"), D("38.93")]
    assert "8451958301" not in p.nips
    assert p.nips == []
    assert p.offers == []


def test_rogow_docx(fixtures):
    p = parse_opening(text(fixtures, "rogow_docx.docx"))
    assert p.budget == D("373000.00")
    assert p.prices == [D("366120.00"), D("525960.00"), D("499256.24"), D("770901.55")]
    assert p.offers == []
```

- [ ] **Step 2: Push, verify FAIL.**

- [ ] **Step 3: Implementation** według reguł (normalizacja linii do porównań przez `identity.names.normalize_name`).

- [ ] **Step 4: Push, verify PASS, commit** (`feat: deterministyczny parser otwarcia ofert`).

---

### Task 12: Kontrola krzyżowa z BZP

**Files:**
- Create: `collector/src/tie/openings/crosscheck.py`
- Test: `collector/tests/test_crosscheck.py`

**Interfaces:**
- Consumes: `OfferRow`.
- Produces:

```python
@dataclass(frozen=True)
class LotFacts:
    n_offers: int | None
    price_min: Decimal | None
    price_max: Decimal | None
    price_winner: Decimal | None
    winner_nip: str | None
    winner_name_norm: str | None

@dataclass(frozen=True)
class CheckResult:
    ok: bool
    scale: Decimal            # mnożnik: cena BZP = cena dokumentu * scale
    reasons: list[str]        # puste gdy ok

def cross_check(offers: list[OfferRow], facts: LotFacts) -> CheckResult
```

Reguły: (a) `len(offers) == facts.n_offers`, inaczej powód `count`; (b) ceny: `dmin, dmax` z ofert; gdy `|dmin - price_min| <= 0.01` i `|dmax - price_max| <= 0.01` to `scale=1`; inaczej `k1 = price_min/dmin`, `k2 = price_max/dmax`, gdy `|k1-k2|/k1 <= 0.005` to `scale = round((k1+k2)/2, 6)`, inaczej powód `prices`; (c) zwycięzca: jeśli `winner_nip` i któraś oferta ma NIP, wymagane `winner_nip` wśród NIP-ów ofert, a cena tej oferty * scale w odległości 0.5% od `price_winner`; jeśli brak NIP-ów, wymagane, by któraś oferta miała `normalize_name(bidder_name)` z podobieństwem tokenów (Jaccard >= 0.5) do `winner_name_norm`; inaczej powód `winner`. Brak `n_offers` lub cen w BZP to powód `bzp_incomplete`.

- [ ] **Step 1: Write the failing test**

```python
from decimal import Decimal as D

from tie.openings.crosscheck import LotFacts, cross_check
from tie.openings.parse import OfferRow

SPY = [("MONSTER GROUP SP Z O.O.", "45.63"), ("GS 5 Sp. z o.o.", "40.18"),
       ("STEKOP S.A.", "49.02"), ("Spółdzielnia WULKAN", "44.78"),
       ("Neptun Security Sp. z o.o.", "39.85"), ("GrupaAlfa Sp. z o.o. Sp. k.", "43.42"),
       ("MICROS GROUP Sp. z o.o.", "41.82"), ("MULTISERVICE Sp. z o.o.", "38.93")]


def rows(pairs, nips=None):
    return [OfferRow(i + 1, n, (nips or {}).get(n), D(p)) for i, (n, p) in enumerate(pairs)]


FACTS = LotFacts(8, D("239497.36"), D("301571.04"), D("239497.36"), "6791021905",
                 "multiservice spzoo")


def test_unit_prices_scale_detected():
    r = cross_check(rows(SPY), FACTS)
    assert r.ok, r.reasons
    assert r.scale == D("6152.000000")


def test_count_mismatch():
    r = cross_check(rows(SPY[:7]), FACTS)
    assert not r.ok and "count" in r.reasons


def test_winner_by_nip():
    facts = LotFacts(1, D("213192.00"), D("213192.00"), D("213192.00"), "5932335316", None)
    r = cross_check([OfferRow(1, "", "5932335316", D("213192.00"))], facts)
    assert r.ok and r.scale == 1


def test_wrong_winner_nip():
    facts = LotFacts(1, D("213192.00"), D("213192.00"), D("213192.00"), "6791021905", None)
    r = cross_check([OfferRow(1, "", "5932335316", D("213192.00"))], facts)
    assert not r.ok and "winner" in r.reasons


def test_inconsistent_prices():
    bad = rows([(n, p) for n, p in SPY[:-1]] + [("MULTISERVICE Sp. z o.o.", "30.00")])
    assert "prices" in cross_check(bad, FACTS).reasons
```

- [ ] **Step 2: Push, verify FAIL.**

- [ ] **Step 3: Implementation** według reguł (Decimal; `normalize_name` z `identity.names`).

- [ ] **Step 4: Push, verify PASS, commit** (`feat: kontrola krzyzowa otwarcia z BZP`).

---

### Task 13: Dostawcy AI i budżet

**Files:**
- Create: `collector/src/tie/ai/__init__.py`, `collector/src/tie/ai/providers.py`
- Test: `collector/tests/test_ai_providers.py`

**Interfaces:**
- Consumes: `PoliteClient`, tabela `ai_calls`.
- Produces:

```python
class BudgetExceeded(Exception): ...

PRICES = {  # USD za 1 mln tokenów (wejście, wyjście)
    ("deepseek", "deepseek-chat"): (0.30, 1.20),
    ("jev", "jev-1.13.0"): (0.042, 0.0),
}

class Budget:
    def __init__(self, conn, daily_usd: float): ...
    def check(self) -> None            # rzuca BudgetExceeded gdy suma cost_usd dziś >= daily_usd
    def record(self, *, provider, model, task, input_tokens, output_tokens, latency_ms, ok) -> float

class DeepSeekText:  # TextProvider
    URL = "https://api.deepseek.com/chat/completions"
    def __init__(self, client, api_key: str, model: str, budget: Budget): ...
    def complete_json(self, *, task: str, system: str, user: str) -> dict

class JevDecisions:  # DecisionProvider
    URL = "https://api.typesafe.ai/v1/systemone"
    MODEL = "jev-1.13.0"
    def __init__(self, client, api_key: str, budget: Budget): ...
    def decide(self, *, task: str, state, questions: dict) -> dict   # zwraca "answers"
```

`DeepSeekText.complete_json`: `budget.check()`, POST z `{"model", "messages":[system,user], "response_format":{"type":"json_object"}, "temperature":0}`, nagłówek `Authorization: Bearer`, `json.loads(choices[0].message.content)`, `budget.record` z `usage.prompt_tokens`/`completion_tokens`. `JevDecisions.decide`: POST `{"model": MODEL, "state": state, "questions": questions}`, zwraca `resp["answers"]`, zapis `usage.input_tokens`/`output_tokens`. Błąd HTTP (status >= 400 po ponowieniach PoliteClient) rejestrowany jako `ok=False` i rzucany `RuntimeError`.

- [ ] **Step 1: Write the failing test**

```python
import httpx
import pytest
import respx

from tie.ai.providers import Budget, BudgetExceeded, DeepSeekText, JevDecisions
from tie.http import PoliteClient


def client():
    return PoliteClient("ua", {}, sleep=lambda s: None)


@respx.mock
def test_deepseek_json_and_cost(db):
    respx.post(DeepSeekText.URL).mock(return_value=httpx.Response(200, json={
        "choices": [{"message": {"content": '{"offers": []}'}}],
        "usage": {"prompt_tokens": 1000000, "completion_tokens": 0}}))
    ds = DeepSeekText(client(), "k", "deepseek-chat", Budget(db, 5.0))
    assert ds.complete_json(task="t", system="s", user="u") == {"offers": []}
    row = db.execute("select * from ai_calls").fetchone()
    assert float(row["cost_usd"]) == pytest.approx(0.30) and row["ok"] is True


@respx.mock
def test_budget_blocks(db):
    b = Budget(db, 0.10)
    b.record(provider="deepseek", model="deepseek-chat", task="t", input_tokens=1000000,
             output_tokens=0, latency_ms=1, ok=True)
    with pytest.raises(BudgetExceeded):
        DeepSeekText(client(), "k", "deepseek-chat", b).complete_json(
            task="t", system="s", user="u")


@respx.mock
def test_jev_decide(db):
    route = respx.post(JevDecisions.URL).mock(return_value=httpx.Response(200, json={
        "model": "jev-1.13.0", "answers": {"same": {"type": "noul", "noul": 0.92}},
        "usage": {"input_tokens": 441, "output_tokens": 57}}))
    j = JevDecisions(client(), "k", Budget(db, 5.0))
    ans = j.decide(task="identity", state={"a": "x"},
                   questions={"same": {"type": "noul", "instructions": "?"}})
    assert ans["same"]["noul"] == 0.92
    body = route.calls.last.request.read().decode()
    assert '"model":"jev-1.13.0"' in body.replace(" ", "")
    assert route.calls.last.request.headers["Authorization"] == "Bearer k"
```

- [ ] **Step 2: Push, verify FAIL.**

- [ ] **Step 3: Implementation** według Interfaces. Koszt: `(in/1e6)*price_in + (out/1e6)*price_out`; nieznany model -> koszt 0 i ostrzeżenie w logu. Suma dzienna: `select coalesce(sum(cost_usd),0) from ai_calls where created_at >= date_trunc('day', now())`.

- [ ] **Step 4: Push, verify PASS, commit** (`feat: dostawcy AI z budzetem`).

---

### Task 14: Ekstrakcja ofert przez DeepSeek, zapis ofert, handler parsowania

**Files:**
- Create: `collector/src/tie/openings/llm.py`, `collector/src/tie/openings/store.py`
- Test: `collector/tests/test_opening_llm.py`

**Interfaces:**
- Consumes: `DeepSeekText`, `ParsedOpening`, `OfferRow`, `cross_check`, `LotFacts`, `extract_text`, `parse_opening`, `RawStore`, `upsert_bidder`.
- Produces (`llm.py`): `SYSTEM_PROMPT: str`; `class LlmOffer(pydantic.BaseModel): offer_no: int | None; lot_no: int | None; bidder_name: str; nip: str | None; price: Decimal | None; price_basis: Literal["total","unit","other"]`; `class LlmOpening(BaseModel): budget: Decimal | None; offers: list[LlmOffer]`; `slice_offers_text(text: str, max_chars: int = 6000) -> str` (od pierwszej linii zawierającej po normalizacji `wykonawc`, `oferent`, `nazwa` lub `otwarto` do końca; obcięte do `max_chars`); `extract_with_llm(provider, text: str) -> LlmOpening` (zadanie `opening_extract`; walidacja pydantic; NIP przez `clean_nip`).
- Produces (`store.py`): `process_opening_document(conn, rawstore, provider, document_id: int) -> str` zwraca status dokumentu: `"parsed"`, `"mismatch"` albo `"needs_review"`. Przebieg: wczytaj dokument i surowe bajty; `extract_text`; zapisz `documents.text_method`; `parse_opening` (z `buyer_nip`); gdy procedura ma jedną część z `n_offers >= 2` i `offers` niepuste, `cross_check`; gdy nie ok albo `offers` puste, `extract_with_llm` i `cross_check` dla ofert z LLM (dla wielu części grupuj po `lot_no`, oferty bez `lot_no` przypisz do części 1); gdy ok, zapisz `bids` (upsert po `(lot_id, offer_no, bidder_name_raw)`; `bidder_id` przez `upsert_bidder` gdy jest NIP; `price_scale` z wyniku; `is_winner` dla oferty zwycięzcy; `rank` po cenie rosnąco; `parse_method` `"deterministic"` lub `"llm"`) i status `parsed`; gdy nadal nie ok, status `mismatch` z `documents.error` = powody złączone przecinkiem. `BudgetExceeded` propaguje się w górę (zadanie wraca do kolejki).

`SYSTEM_PROMPT` (dokładnie):

```
Jesteś parserem dokumentów "informacja z otwarcia ofert" z polskich przetargów publicznych.
Zwróć wyłącznie JSON: {"budget": liczba lub null, "offers": [{"offer_no": liczba lub null,
"lot_no": liczba lub null, "bidder_name": tekst, "nip": tekst lub null, "price": liczba lub null,
"price_basis": "total" | "unit" | "other"}]}.
Zasady: jedna pozycja na ofertę; przy konsorcjum bidder_name to lider (pierwszy wymieniony);
price to cena brutto oferty jako liczba z kropką dziesiętną; price_basis "unit" gdy cena dotyczy
jednostki (godzina, km, sztuka); nie dodawaj zamawiającego jako oferenta; nie zgaduj NIP.
```

- [ ] **Step 1: Write the failing test**

```python
import json
from decimal import Decimal as D

from tie.bzp.parser import parse_result_notice
from tie.bzp.store import upsert_notice
from tie.openings.llm import slice_offers_text
from tie.openings.store import process_opening_document
from tie.rawstore import RawStore

SPY_OCDS = "ocds-148610-05afc816-7c33-4416-8a4f-8a804d3d4b32"


class FakeProvider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def complete_json(self, *, task, system, user):
        self.calls += 1
        return self.payload


def setup_doc(db, fixtures, tmp_path, notice_file, pdf_file, ocds):
    rec = json.loads((fixtures / "bzp" / notice_file).read_text("utf-8"))
    upsert_notice(db, parse_result_notice(rec), "sha")
    rs = RawStore(tmp_path)
    sha = rs.put((fixtures / "openings" / pdf_file).read_bytes(), source="ezam")
    row = db.execute(
        "insert into documents(procedure_id, source, source_doc_id, file_name, doc_type,"
        " raw_sha256, status) values (%s,'ezamowienia',%s,%s,'opening',%s,'downloaded')"
        " returning id", (ocds, ocds + "_9", pdf_file, sha)).fetchone()
    db.commit()
    return rs, row["id"]


def test_slice():
    t = "Nagłówek\nKwota 1\nWykonawca/siedziba Cena\n1 Firma 10,00"
    assert slice_offers_text(t).startswith("Wykonawca/siedziba")


def test_spytkowo_llm_path(db, fixtures, tmp_path):
    rs, doc_id = setup_doc(db, fixtures, tmp_path, "result_spytkowo.json",
                           "spytkowo_unit_prices.pdf", SPY_OCDS)
    names = ["MONSTER GROUP SP Z O.O.", "GS 5 Sp. z o.o.", "STEKOP S.A.",
             "Spółdzielnia WULKAN w Olsztynie", "Neptun Security Sp. z o.o.",
             "GrupaAlfa Sp. z o.o. Sp. k.", "MICROS GROUP Sp. z o.o.", "MULTISERVICE Sp. z o.o."]
    prices = ["45.63", "40.18", "49.02", "44.78", "39.85", "43.42", "41.82", "38.93"]
    fake = FakeProvider({"budget": None, "offers": [
        {"offer_no": i + 1, "lot_no": None, "bidder_name": n, "nip": None, "price": float(p),
         "price_basis": "unit"} for i, (n, p) in enumerate(zip(names, prices))]})
    assert process_opening_document(db, rs, fake, doc_id) == "parsed"
    assert fake.calls == 1
    bids = db.execute("select * from bids order by offer_no").fetchall()
    assert len(bids) == 8
    assert bids[7]["is_winner"] is True and bids[7]["rank"] == 1
    assert bids[0]["price_scale"] == D("6152.000000")
    assert all(b["parse_method"] == "llm" for b in bids)


def test_mismatch_when_llm_wrong(db, fixtures, tmp_path):
    rs, doc_id = setup_doc(db, fixtures, tmp_path, "result_spytkowo.json",
                           "spytkowo_unit_prices.pdf", SPY_OCDS)
    fake = FakeProvider({"budget": None, "offers": [
        {"offer_no": 1, "lot_no": None, "bidder_name": "X", "nip": None, "price": 1.0,
         "price_basis": "total"}]})
    assert process_opening_document(db, rs, fake, doc_id) == "mismatch"
    assert db.execute("select count(*) c from bids").fetchone()["c"] == 0
    err = db.execute("select error from documents where id=%s", (doc_id,)).fetchone()["error"]
    assert "count" in err
```

- [ ] **Step 2: Push, verify FAIL.**

- [ ] **Step 3: Implementation** według Interfaces.

- [ ] **Step 4: Push, verify PASS, commit** (`feat: ekstrakcja ofert przez LLM i zapis ofert`).

---

### Task 15: Tożsamość firm (Jev) i biała lista VAT

**Files:**
- Create: `collector/src/tie/identity/bidders.py`, `collector/src/tie/identity/whitelist.py`
- Test: `collector/tests/test_identity.py`

**Interfaces:**
- Consumes: `JevDecisions`, `PoliteClient`, `normalize_name`, tabele `bidders`, `bids`, `bidder_accounts`, `wl_checks`.
- Produces (`bidders.py`): `candidates(conn, name: str, province: str | None, limit: int = 5) -> list[dict]` (bidders z NIP, `similarity(name_norm, %s) >= 0.45`, sortowane malejąco, opcjonalnie ten sam `province`); `resolve_unmatched_bids(conn, decider, *, threshold: float = 0.85, limit: int = 200) -> int` (dla `bids` z `bidder_id is null`: kandydaci; jedno wywołanie `decider.decide(task="identity", state={"oferent": bidder_name_raw, "kandydaci": {str(i): "<name>, <city>, NIP <nip>"}}, questions={"match": {"type":"choice","instructions":"Który kandydat to ten sam wykonawca co `oferent`? Wybierz none, jeśli żaden.","criteria": {"0":None,...,"none":None}}})`; gdy wybór != `none` i `probabilities[wybór] >= threshold`, ustaw `bidder_id` i `match_confidence`; inaczej utwórz nowego biddera bez NIP przez `upsert_bidder`, `match_confidence` = prawdopodobieństwo `none`; zwraca liczbę dopasowanych do istniejących).
- Produces (`whitelist.py`): `WL_URL = "https://wl-api.mf.gov.pl/api/search/nips/{nips}"` (do 30 NIP-ów rozdzielonych przecinkiem; limit MF: 100 zapytań wyszukujących dziennie); `enrich_bidders(conn, client, *, daily_cap: int, today: date, limit: int = 300) -> int` (bidders z NIP i `wl_checked_at is null`, najpierw ci występujący w `bids`, w paczkach po 30; przed każdym zapytaniem sprawdź i zwiększ `wl_checks.requests` dla `today`, stop przy `daily_cap`; dla każdego `entries[i]` dopasuj biddera po `identifier`; zapisz `regon`, `krs`, `is_jdg = krs is null`, `address = workingAddress or residenceAddress`, `wl_checked_at=now()`; konta: `bidder_accounts` z `sha256(numer)` hex; wpis z pustym `subjects` oznacza tylko `wl_checked_at`). Zwraca liczbę wzbogaconych.

- [ ] **Step 1: Write the failing test**

```python
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
    upsert_bidder(db, name="Usługi Remontowe Jan Testowy", nip="7010000005", city=None, province=None)
    db.commit()
    route = respx.get(url__startswith="https://wl-api.mf.gov.pl/api/search/nips/").mock(
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
```

- [ ] **Step 2: Push, verify FAIL.**

- [ ] **Step 3: Implementation** według Interfaces. Parametr zapytania WL: `params={"date": today.isoformat()}`.

- [ ] **Step 4: Push, verify PASS, commit** (`feat: tozsamosc firm przez Jev i biala lista VAT`).

---

### Task 16: Handlery zadań, worker i CLI

**Files:**
- Create: `collector/src/tie/handlers.py`, `collector/src/tie/cli.py`
- Test: `collector/tests/test_cli.py`

**Interfaces:**
- Consumes: wszystkie poprzednie moduły.
- Produces (`handlers.py`): `class Context` (pola: `conn`, `settings`, `client: PoliteClient`, `rawstore`, `text_provider`, `decider`) z fabryką `Context.from_settings(settings)` (klient z limitami z Global Constraints; providery tylko gdy klucze są ustawione, inaczej `None`); `HANDLERS: dict[str, Callable[[Context, dict], None]]` z kluczami: `"bzp_day"` (payload `{"date"}`: `fetch_day`, `store_day`, `parse_result_notice` + `upsert_notice` dla każdego rekordu; błąd parsowania pojedynczego rekordu logowany i pomijany, liczony), `"ezam_docs"` (`sync_documents`), `"ezam_download"` (`download_document`), `"opening_parse"` (`process_opening_document`; brak `text_provider` = rzuca `RuntimeError("DEEPSEEK_API_KEY not set")`), `"identity"` (`resolve_unmatched_bids`), `"whitelist"` (`enrich_bidders`); `run_once(ctx, kinds: list[str] | None = None) -> bool` (claim jednego zadania, wywołanie handlera, `complete` albo `fail` z `repr(exc)`; zwraca False gdy brak zadań).
- Produces (`cli.py`): `main(argv: list[str] | None = None) -> int` z podkomendami: `migrate`; `backfill --since YYYY-MM-DD [--until YYYY-MM-DD]` (enqueue `bzp_day` dla każdego dnia, klucz = data, od najnowszego do najstarszego przez `run_after` rosnące co 1 s); `daily` (enqueue `bzp_day` dla wczoraj i przedwczoraj z kluczem `<data>@<dzisiejsza data>` żeby wymusić ponowne pobranie, plus `identity` i `whitelist` z kluczem dzisiejszej daty); `worker [--kinds a,b] [--max-jobs N]` (pętla `run_once`, `requeue_stale` co 100 zadań, sen 5 s gdy pusto, wyjście po N zadaniach); `stats` (wypisuje JSON: liczba procedur, części, ofert, dokumentów wg statusu, zadań wg statusu i rodzaju, koszt AI dziś).

- [ ] **Step 1: Write the failing test**

```python
import json
from datetime import date

from tie import cli
from tie.handlers import HANDLERS


def test_handlers_registered():
    assert {"bzp_day", "ezam_docs", "ezam_download", "opening_parse", "identity",
            "whitelist"} <= set(HANDLERS)


def test_backfill_enqueues_days(db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db.info.dsn)
    assert cli.main(["backfill", "--since", "2026-10-01", "--until", "2026-10-03"]) == 0
    keys = [r["key"] for r in db.execute(
        "select key from jobs where kind='bzp_day' order by run_after").fetchall()]
    assert keys == ["2026-10-03", "2026-10-02", "2026-10-01"]


def test_daily_enqueues(db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db.info.dsn)
    monkeypatch.setattr(cli, "today", lambda: date(2026, 10, 12))
    assert cli.main(["daily"]) == 0
    kinds = {r["kind"] for r in db.execute("select kind from jobs").fetchall()}
    assert kinds == {"bzp_day", "identity", "whitelist"}
    payloads = [r["payload"] for r in db.execute(
        "select payload from jobs where kind='bzp_day'").fetchall()]
    assert {"date": "2026-10-11"} in payloads and {"date": "2026-10-10"} in payloads


def test_stats(db, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", db.info.dsn)
    assert cli.main(["stats"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["procedures"] == 0 and "jobs" in out
```

Uwaga: `db.info.dsn` nie zawiera hasła; w `conftest.py` udostępnij też `os.environ["TEST_DATABASE_URL"]` i w tych testach używaj `monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])` (zastąp `db.info.dsn`, jeśli połączenie bez hasła nie działa w CI).

- [ ] **Step 2: Push, verify FAIL.**

- [ ] **Step 3: Implementation** według Interfaces (`argparse`; `today()` jako funkcja modułowa zwracająca `date.today()`; logowanie `logging.basicConfig(level=INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")`).

- [ ] **Step 4: Push, verify PASS, commit** (`feat: handlery, worker i CLI`).

---

### Task 17: Docker, Compose, harmonogram, deploy, dokumentacja operacyjna

**Files:**
- Create: `collector/Dockerfile`, `collector/crontab`, `infra/docker-compose.yml`, `infra/docker-compose.prod.yml`, `infra/.env.example`, `.github/workflows/deploy.yml`, `docs/OPERATIONS.md`
- Modify: `.github/workflows/ci.yml` (job `image`: `docker build collector`)

**Interfaces:**
- Consumes: komendy CLI z Task 16.
- Produces: obraz `tie-collector`; usługi Compose `db` (postgres:17, wolumen `tie_pg`), `migrate` (jednorazowo `tie migrate`), `worker` (`tie worker`, `cpus: 4`, `mem_limit: 8g`), `cron` (supercronic z `crontab`); katalog surowych plików montowany `/var/www/tie/data/raw:/data/raw`.

- [ ] **Step 1: Dockerfile**

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      poppler-utils tesseract-ocr tesseract-ocr-pol curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-amd64
RUN curl -fsSL "$SUPERCRONIC_URL" -o /usr/local/bin/supercronic && chmod +x /usr/local/bin/supercronic
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY crontab /app/crontab
USER nobody
CMD ["tie", "worker"]
```

- [ ] **Step 2: crontab** (czas serwera UTC; 23:00 UTC = 01:00 w Polsce latem)

```
0 23 * * * tie daily
*/30 * * * * tie stats
```

- [ ] **Step 3: infra/docker-compose.yml**

```yaml
name: tie
services:
  db:
    image: postgres:17
    environment:
      POSTGRES_USER: tie
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: tie
    volumes: ["tie_pg:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U tie"]
      interval: 5s
      retries: 20
    restart: unless-stopped
  migrate:
    build: ../collector
    env_file: .env
    command: ["tie", "migrate"]
    depends_on: { db: { condition: service_healthy } }
  worker:
    build: ../collector
    env_file: .env
    command: ["tie", "worker"]
    volumes: ["${RAW_HOST_DIR:-./data/raw}:/data/raw"]
    depends_on: { migrate: { condition: service_completed_successfully } }
    cpus: 4
    mem_limit: 8g
    restart: unless-stopped
  cron:
    build: ../collector
    env_file: .env
    command: ["supercronic", "/app/crontab"]
    depends_on: { migrate: { condition: service_completed_successfully } }
    restart: unless-stopped
volumes:
  tie_pg:
```

`infra/docker-compose.prod.yml`:

```yaml
services:
  worker:
    volumes: ["/var/www/tie/data/raw:/data/raw"]
```

`infra/.env.example`:

```
POSTGRES_PASSWORD=change-me
DATABASE_URL=postgresql://tie:change-me@db:5432/tie
RAW_DIR=/data/raw
JEV_API_KEY=
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-chat
AI_DAILY_BUDGET_USD=5.0
WL_DAILY_CAP=90
```

- [ ] **Step 4: .github/workflows/deploy.yml**

```yaml
name: deploy
on:
  push:
    branches: [master]
  workflow_dispatch:
jobs:
  deploy:
    runs-on: [self-hosted, tie]
    steps:
      - uses: actions/checkout@v4
      - name: sync code
        run: rsync -a --delete --exclude .git --exclude infra/.env --exclude data ./ /var/www/tie/
      - name: up
        working-directory: /var/www/tie/infra
        run: docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
      - name: stats
        working-directory: /var/www/tie/infra
        run: docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm worker tie stats
```

Dopisz do `ci.yml` job:

```yaml
  image:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker build -t tie-collector collector
      - run: docker run --rm tie-collector tie --help
```

- [ ] **Step 5: docs/OPERATIONS.md**: sekcje "Pierwsze uruchomienie" (utworzenie `/var/www/tie/infra/.env` z `.env.example` na serwerze, rejestracja runnera z etykietą `tie`, `docker compose run --rm worker tie backfill --since 2021-01-01`), "Codzienna praca" (cron 23:00 UTC), "Podgląd" (`tie stats`), "Koszty AI" (tabela `ai_calls`, limit `AI_DAILY_BUDGET_USD`), "Backup" (Postgres automatycznie przez `/opt/backup`; surowe JSON BZP do wykluczenia z backupu, PDF w backupie), "Klucze" (tylko `.env` na serwerze; rotacja klucza Jev po hackathonie).

- [ ] **Step 6: Push, verify CI PASS (oba joby), commit** (`feat: docker, compose, deploy i dokumentacja operacyjna`).

---

## Po zakończeniu części 1 (Fable, nie subagent)

1. Przegląd całej gałęzi (`git diff master...part1-collector`), raport review.
2. Rejestracja runnera `tie` na głównym boxie i plik `.env` na serwerze (klucze Jev i DeepSeek) za zgodą właściciela.
3. PR `part1-collector` -> `master`. Merge odpala deploy na prod, więc wymaga zgody właściciela.
4. Pierwszy backfill od najnowszych dni, obserwacja kosztów i odsetka `mismatch` przez `tie stats`.
5. Część 1b (osobny plan): powiązania osobowe z MSiG. API `https://wyszukiwarka-msig.ms.gov.pl/api/Monitor/Search` (GET, bez klucza; parametry `krs`, `signatureType=B`, `from`, `to`, `page`, `pageSize`) zwraca ogłoszenia "XV. WPISY DO KRS" z niemaskowanymi nazwiskami i PESEL członków zarządu, rady nadzorczej i prokurentów. Zasady z analizy w projekcie firmy365 (`docs/04-dane-osobowe.md`): tempo 600 ms, identyfikujący User-Agent, PESEL przechowywany wyłącznie jako HMAC (plus rok urodzenia), nigdy nie publikowany; przed budową rozstrzygnąć kompletność danych (niespójne `SearchCount` w 2026). Biała lista VAT (konta bankowe, adresy) wchodzi już w części 1.
