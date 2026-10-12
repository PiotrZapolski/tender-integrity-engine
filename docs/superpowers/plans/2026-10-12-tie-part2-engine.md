# TIE część 2: silnik analizy - plan implementacji

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Z danych części 1 (procedures, lots, bids, bidders, bidder_accounts) policzyć testy z listy kontrolnej OECD w obrębie rynków, zbudować graf współoferentów z klastrami, podjąć decyzję o publikacji sygnału przez Jev ze skalibrowanym prawdopodobieństwem i opublikować widoki `api.*` zgodne z `docs/contracts/api-v1.md`.

**Architecture:** Nowy podpakiet `tie.engine` czytający wyłącznie z Postgresa i zapisujący do tabel analitycznych (migracja 005). Obliczenia w pamięci (numpy, python-igraph + leidenalg) na poziomie rynku; każdy test to osobny moduł z czystą funkcją `compute(conn, run) -> list[TestResult]` zapisującą wiersze `test_results`. Decyzja i opisy w osobnych modułach. Widoki `api.*` jako SQL w migracji, odświeżane jako zwykłe widoki nad tabelami analitycznymi.

**Tech Stack:** Python 3.12, psycopg 3, numpy, python-igraph, leidenalg, pydantic 2, pytest; Jev (`tie.ai.providers.JevDecisions`), DeepSeek (`tie.ai.providers.DeepSeekText`).

**Spec:** `docs/superpowers/specs/2026-10-12-zmowy-design.md` (sekcje 8, 9, 10, 13) oraz kontrakt `docs/contracts/api-v1.md`.

## Global Constraints

- Wszystkie zasady z `.superpowers/sdd/2026-10-12-zmowy-part1-collector/global-constraints.md` obowiązują (testy tylko w CI, brak myślników typograficznych, sekrety tylko w env, User-Agent, commity z trailerem Opus), gałąź bazowa to `master`.
- Analiza używa tylko ofert z dokumentów, które przeszły kontrolę krzyżową (`documents.status = 'parsed'`), oraz zwycięzców i statystyk z BZP. Oferty z `bids.outside_bzp_range = true` wchodzą do testów współwystępowania i "zawsze przegrywa", ale NIE do testów rozkładu cen ofert ważnych.
- Ceny porównywane po przeliczeniu przez `bids.price_scale` (cena BZP = cena dokumentu * scale).
- Rynek = (grupa CPV 5 cyfr, województwo zamawiającego, okno 36 miesięcy kończące się na dacie przebiegu). Rynek z mniej niż 30 częściami łączymy z rynkiem nadrzędnym (grupa CPV 3 cyfry, to samo województwo); dalej z mniej niż 30: grupa 3 cyfry, cała Polska.
- Percentyl testu liczymy wyłącznie w obrębie rynku; wynik testu bez co najmniej 10 obserwacji w rynku ma `percentile = null`.
- Jev przypięty `jev-1.13.0`; arytmetyka w kodzie, do modelu idzie zwięzły pakiet liczb i percentyli (max 6000 znaków JSON).
- JDG: w tabelach analitycznych przechowujemy `bidder_id`; maskowanie nazw dzieje się w widokach `api.*` (`display_name = 'NIP ' || nip` gdy `is_jdg`).
- Żadnych nazwisk osób fizycznych w `api.*`.
- Koszt AI pod istniejącym budżetem dziennym.

## Review Focus

1. Rynek z 1-2 wykonawcami (np. usługi weterynaryjne): testy nie mogą generować sygnałów tylko dlatego, że rynek jest mały; percentyle w obrębie rynku i minimalne liczności. Test w Task 4.
2. Firma z jedną ofertą w historii: brak dzielenia przez zero, brak sygnału. Testy w Task 4 i 5.
3. Ponowny przebieg silnika: wyniki nadpisane w obrębie `run_id`, brak duplikatów sygnałów, stabilne `signal_id` dla tego samego klastra. Test w Task 7.
4. JDG w `api.*`: nazwa nigdy nie wycieka (także w `members`, `partners`, `search`, `evidence`). Test w Task 9.
5. Brak klucza Jev lub przekroczony budżet: silnik kończy się sygnałami ze statusem `review` z regresji zapasowej, nie wywraca się. Test w Task 7.

---

## Struktura plików

```
collector/src/tie/engine/
  __init__.py
  markets.py        rynki i przypisanie części
  oecd.py           katalog 51 punktów (stała) + seed tabeli
  tests_lot.py      testy cenowe na poziomie części (3.3.5, 3.3.7, 3.3.9)
  tests_firm.py     testy firmowe (3.1.1, 3.1.5, 3.3.3)
  tests_pair.py     testy parowe (3.1.6 rotacja, 3.3.6 wzór cen, 3.1.7 konsorcja, 3.2.11 wspólne dane)
  tests_market.py   testy rynkowe (3.1.2 podział terytorialny, 3.3.10 wejście nowego)
  graph.py          graf współoferentów i klastry Leiden
  evidence.py       pakiet dowodów klastra
  decide.py         decyzja Jev + regresja zapasowa
  explain.py        opisy (szablony + DeepSeek dla opublikowanych)
  run.py            orkiestracja przebiegu
collector/src/tie/migrations/005_engine.sql
collector/src/tie/migrations/006_api_views.sql
collector/tests/engine/ (testy, w tym generator syntetycznych rynków)
```

## Fale

- Fala A: Task 1 (schemat 005 + katalog OECD + generator syntetycznych rynków w testach).
- Fala B (równolegle): Task 2 (rynki), Task 3 (graf).
- Fala C (równolegle): Task 4 (testy części i firm), Task 5 (testy parowe), Task 6 (testy rynkowe).
- Fala D: Task 7 (dowody, decyzja, sygnały) i Task 8 (opisy) równolegle.
- Fala E: Task 9 (widoki api.* i rola tie_reader), Task 10 (orkiestracja, CLI, cron).

Modele: Opus dla wszystkich zadań poza Task 1 część katalogowa (Sonnet może przepisać 51 punktów, ale całość Task 1 robi Opus, bo zawiera generator syntetycznych rynków).

---

### Task 1: Schemat silnika, katalog OECD, generator syntetycznych rynków

**Files:** Create `collector/src/tie/migrations/005_engine.sql`, `collector/src/tie/engine/__init__.py`, `collector/src/tie/engine/oecd.py`, `collector/tests/engine/__init__.py`, `collector/tests/engine/synth.py`, `collector/tests/engine/test_oecd.py`, `collector/tests/engine/test_synth.py`. Modify `collector/pyproject.toml` (dependencies: `numpy>=2.0`, `python-igraph>=0.11`, `leidenalg>=0.10`).

**Interfaces (Produces):**
- Tabele (005): `engine_runs(run_id bigserial pk, started_at, finished_at, as_of date not null, params jsonb, status text)`; `markets(market_id text pk, cpv_group text, province text, level text, n_lots int, run_id bigint)`; `lot_market(lot_id bigint pk references lots, market_id text, run_id bigint)`; `test_results(run_id bigint, test_id text, level text check in ('lot','firm','pair','market','cluster'), entity_key text, market_id text, value double precision, percentile double precision, n_obs int, evidence jsonb, primary key (run_id, test_id, level, entity_key))`; `pairs(run_id, a bigint, b bigint, market_id text, n_together int, a_wins int, b_wins int, other_wins int, rotation double precision, ratio_cv double precision, shared_account bool, shared_address bool, primary key (run_id, a, b, market_id))` z warunkiem `a < b`; `clusters(run_id, cluster_id text, market_id text, members bigint[], n_lots int, density double precision, primary key (run_id, cluster_id))`; `signals(signal_id text pk, run_id bigint, cluster_id text, market_id text, probability double precision, strength int, oecd jsonb, status text check in ('published','review','hidden'), evidence_hash text, evidence jsonb, created_at, updated_at)`; `explanations(signal_id text, evidence_hash text, source text check in ('template','deepseek'), title text, summary text, findings jsonb, alternatives text[], created_at, primary key (signal_id, evidence_hash))`; `oecd_catalog(item text pk, section text, title text, status text check in ('A','C','N'), how text)`.
- `tie.engine.oecd.CATALOG: list[OecdItem]` (51 pozycji: `item`, `section`, `title`, `status`, `how`) przepisanych z tabel sekcji 8.1 speca (tytuł po polsku, status A/C/N, kolumna "Implementacja / powód" jako `how`; 3.4.1-3.4.9 osobno, każdy N z powodem "wypowiedzi nie są w danych publicznych"); `seed_catalog(conn) -> int` (upsert, zwraca 51).
- `tests/engine/synth.py`: `make_market(conn, *, seed: int, n_firms: int, n_lots: int, cpv: str, province_code: str, cartel: dict | None = None) -> dict` tworzy procedury, części, oferty (`documents.status='parsed'`) i firmy w bazie testowej; `cartel` opisuje wstrzykniętą zmowę: `{"members": 3, "pattern": "rotation"|"cover"|"split"|"shared_account", "share": 0.4}`; zwraca słownik z id firm kartelu i resztą. Konkurencja normalna: ceny log-normalne wokół wartości szacunkowej (sigma 0.12), liczba ofert 2-6, zwycięzca najtańszy. Rotacja: członkowie startują razem i wygrywają na zmianę; cover: przegrani członkowie zawsze o 5-8% drożej od zwycięzcy-członka; split: każdy członek wygrywa tylko u "swoich" zamawiających; shared_account: wspólny `bidder_accounts.account_hash`.

**Testy:** `test_oecd.py`: 51 pozycji, unikalne `item`, liczby A=14, C=12, N=25, `seed_catalog` idempotentny. `test_synth.py`: `make_market(seed=1, n_firms=12, n_lots=80, cartel={"members":3,"pattern":"rotation","share":0.4})` tworzy 80 części, każda z 2-6 ofertami, zwycięzca ma `is_winner`, członkowie kartelu wygrywają razem co najmniej 25 części, deterministyczne przy tym samym seed.

- [ ] Kroki: test (czerwony w CI) -> implementacja -> zielone CI -> commit.

---

### Task 2: Rynki i przypisanie części

**Files:** Create `collector/src/tie/engine/markets.py`, `collector/tests/engine/test_markets.py`.

**Interfaces:** `cpv_group(cpv: str | None, digits: int) -> str | None` (pierwsze `digits` cyfr kodu CPV); `assign_markets(conn, run_id: int, as_of: date, *, window_months: int = 36, min_lots: int = 30) -> int` (zapisuje `markets` i `lot_market` dla części z `contract_date` lub `published_at` w oknie; poziomy `cpv5_prov`, `cpv3_prov`, `cpv3_pl`; zwraca liczbę przypisanych części). `market_id` = `f"{level}:{group}:{province or 'PL'}"`.

**Testy:** części z rzadkiej grupy 5-cyfrowej (mniej niż 30) trafiają do `cpv3_prov`; 30+ zostaje w `cpv5_prov`; części poza oknem nie są przypisane; ponowne wywołanie w tym samym `run_id` nie dubluje.

---

### Task 3: Graf współoferentów i klastry

**Files:** Create `collector/src/tie/engine/graph.py`, `collector/tests/engine/test_graph.py`.

**Interfaces:** `build_pairs(conn, run_id) -> int` (dla każdego rynku i każdej pary firm, które złożyły ofertę w tej samej części: `n_together`, wygrane każdej strony w tych częściach, wygrane innych firm, `shared_account` i `shared_address` z `bidder_accounts`/`bidders.address`; tylko pary z `n_together >= 2`); `find_clusters(conn, run_id, *, min_together: int = 3, resolution: float = 1.0, seed: int = 42) -> int` (graf per rynek, krawędzie z wagą `n_together` dla par `n_together >= min_together`, Leiden `RBConfigurationVertexPartition` z ustalonym seed; klastry o 2-12 członkach zapisywane z `density` i `n_lots` = liczba części, w których co najmniej dwóch członków startowało razem). `cluster_id` = sha1 z posortowanych `bidder_id` członków i `market_id`, obcięte do 16 znaków (stabilne między przebiegami).

**Testy:** rynek syntetyczny z rotacją: kartel (3 firmy) wychodzi jako jeden klaster dokładnie z tymi członkami; rynek bez kartelu przy seed 1 nie daje klastra zawierającego 3 losowe firmy z `density` powyżej 0.8 i `n_lots >= 20`; `cluster_id` identyczny w dwóch przebiegach.

---

### Task 4: Testy części i firm

**Files:** Create `collector/src/tie/engine/tests_lot.py`, `collector/src/tie/engine/tests_firm.py`, `collector/tests/engine/test_tests_lot_firm.py`.

**Interfaces:** `compute_lot_tests(conn, run_id) -> int` i `compute_firm_tests(conn, run_id) -> int`, każde zapisuje `test_results` z `percentile` w obrębie rynku (rank / n, null gdy `n_obs < 10`). Testy:
- `3.3.7` (część): odległość względna `(p2 - p1) / sd(przegranych ważnych)` dla części z 3+ ofertami ważnymi; wysoka wartość = sygnał.
- `3.3.5` (część): czy co najmniej dwie oferty mają identyczną cenę (po przeliczeniu, tolerancja 0.01 zł).
- `3.3.9` (część): `price_winner / estimated_value`; wysoka = sygnał (tylko gdy wartość szacunkowa > 0).
- `3.1.1` (firma w rynku): udział wygranych firmy w częściach rynku (min. 10 części w rynku).
- `3.1.5` (firma w rynku): liczba startów bez żadnej wygranej (sygnał od 8 startów, 0 wygranych).
- `3.3.3` (firma w rynku): mediana `price_winner / estimated_value` dla wygranych firmy na tle rynku.

**Testy:** rynek z cover bids (pattern "cover") daje wysoki percentyl `3.3.7` dla części kartelu i niski dla reszty; firma z 0 wygranych w 10 startach dostaje `3.1.5`; firma z 1 ofertą nie dostaje żadnego wyniku firmowego; rynek z 2 firmami i 40 częściami daje `percentile` liczony tylko w tym rynku (Review Focus 1).

---

### Task 5: Testy parowe

**Files:** Create `collector/src/tie/engine/tests_pair.py`, `collector/tests/engine/test_tests_pair.py`.

**Interfaces:** `compute_pair_tests(conn, run_id) -> int` na podstawie `pairs` i ofert:
- `3.1.6` rotacja: dla par z `n_together >= 5`: `rotation = 1 - |a_wins - b_wins| / (a_wins + b_wins)` pomnożone przez udział części, w których wygrał ktoś z pary; wysoka = sygnał. Zapis także w `pairs.rotation`.
- `3.3.6` wzór cen: współczynnik zmienności stosunku cen B/A w częściach wspólnych (min. 4 części); niski CV = sygnał (zapisz `value = cv`, percentyl odwrócony). Zapis w `pairs.ratio_cv`.
- `3.1.7` konsorcja: para, która startowała osobno co najmniej 2 razy, a potem wygrała razem jako konsorcjum (z `lots.consortium` i nazwy lidera; wartość = liczba takich zdarzeń).
- `3.2.11` wspólne dane: `shared_account or shared_address` (wartość 1/0, bez percentyla).

**Testy:** wzorzec "rotation" daje `3.1.6` > 0.8 dla par kartelu; "cover" daje `3.3.6` CV < 0.03 dla par kartelu i > 0.05 dla par losowych; "shared_account" daje `3.2.11 = 1` tylko dla par kartelu.

---

### Task 6: Testy rynkowe

**Files:** Create `collector/src/tie/engine/tests_market.py`, `collector/tests/engine/test_tests_market.py`.

**Interfaces:** `compute_market_tests(conn, run_id) -> int`:
- `3.1.2` podział terytorialny (klaster): dla członków klastra macierz zamawiający x wygrane; indeks wyłączności = udział zamawiających, u których wygrywa wyłącznie jeden członek, ważony liczbą części; wysoki = sygnał. Zapis `level='cluster'`.
- `3.3.10` wejście nowego (rynek i klaster): dla firmy spoza klastra, która pierwszy raz startuje w części, gdzie startują członkowie, porównaj medianę `price_winner / estimated_value` 6 miesięcy przed i 6 miesięcy po w częściach z udziałem członków; spadek > 10% = sygnał (wartość = względna zmiana).

**Testy:** wzorzec "split" daje `3.1.2` > 0.8 dla klastra kartelu; brak nowego wejścia daje brak wyniku `3.3.10`; syntetyczne wejście z obniżką cen o 20% daje wartość około -0.2.

---

### Task 7: Pakiet dowodów, decyzja, sygnały

**Files:** Create `collector/src/tie/engine/evidence.py`, `collector/src/tie/engine/decide.py`, `collector/tests/engine/test_decide.py`.

**Interfaces:**
- `build_evidence(conn, run_id, cluster_id) -> dict` (rynek, członkowie z `bidder_id`, liczba części, okres, dla każdego punktu OECD A/C wynik i percentyl z `test_results` dla klastra, jego par i członków, maks. 20 przykładowych części z ceną i zwycięzcą; JSON maks. 6000 znaków) i `evidence_hash(evidence) -> str` (sha256 kanonicznego JSON).
- `QUESTIONS`: dla każdego punktu OECD obecnego w dowodach pytanie `noul` (instrukcja po polsku z treścią punktu i polem z liczbami), plus `strength` jako `score` z 5 poziomami ("brak sygnału" do "bardzo silny sygnał").
- `decide_cluster(decider, evidence) -> Decision(probability: float, strength: float, per_item: dict[str, float], source: str)`: `probability` = maksimum z `per_item` ważone siłą (dokładna formuła: `p = 1 - prod(1 - p_i * w_i)` gdzie `w_i` = 1 dla punktów A i 0.6 dla C), `source = "jev"`.
- `fallback_decision(evidence) -> Decision`: regresja logistyczna na percentylach (wagi stałe w kodzie: 3.1.6 2.0, 3.3.6 2.0, 3.3.7 1.5, 3.1.2 1.5, 3.2.11 2.5, 3.1.5 1.0, 3.3.9 0.8, 3.3.10 1.0, bias -4.0), `source = "fallback"`.
- `upsert_signals(conn, run_id, decider, *, publish_threshold: float = 0.91, review_threshold: float = 0.6) -> dict` (dla każdego klastra: dowody, decyzja Jev; przy braku decidera, `BudgetExceeded` lub błędzie providera: fallback i status co najwyżej `review`; `signal_id` = `cluster_id`; gdy `evidence_hash` się nie zmienił, nie woła modelu ponownie; status `published` / `review` / `hidden` wg progów; zwraca liczniki).

**Testy:** fake decider z odpowiedziami Jev (format `{"x": {"type":"noul","noul":0.9}}`) daje `published` dla klastra kartelu; brak decidera daje `review` z `source="fallback"` (Review Focus 5); drugi przebieg bez zmiany dowodów nie wywołuje decidera (licznik wywołań fake = 0) i nie dubluje sygnałów (Review Focus 3).

---

### Task 8: Opisy sygnałów

**Files:** Create `collector/src/tie/engine/explain.py`, `collector/tests/engine/test_explain.py`.

**Interfaces:** `template_explanation(evidence, decision) -> Explanation(title, summary, findings, alternatives)`: deterministyczne zdania po polsku z liczbami z dowodów (np. "3 firmy od 2021 roku wygrywają na zmianę: 27 przetargów razem."), `findings` w kształcie z `api-v1.md` (`viz` rotation/ladder/graph z danymi), `alternatives` z listy stałych wyjaśnień zależnych od wzoru (mały rynek, ograniczone moce, wspólny kosztorys). `explain_signal(conn, provider, signal_id) -> str` dla `published`: DeepSeek (zadanie `signal_explain`) dostaje dowody i szablon, zwraca JSON `{title, summary}` w prostym polszczyźnie bez nowych liczb (walidacja: każda liczba w tekście musi występować w dowodach, inaczej zostaje szablon); zapis w `explanations` z `source`.

**Testy:** szablon dla klastra rotacji zawiera liczbę części i lata; walidacja odrzuca odpowiedź DeepSeek z liczbą spoza dowodów (zostaje `template`); dane `viz` rotation mają wiersz na członka.

---

### Task 9: Widoki api.* i rola tylko do odczytu

**Files:** Create `collector/src/tie/migrations/006_api_views.sql`, `collector/tests/engine/test_api_views.py`.

**Interfaces:** schemat `api` z widokami dokładnie wg `docs/contracts/api-v1.md` (`stats`, `signals`, `signal_detail`, `bidders`, `buyers`, `search`, `oecd_catalog`); tylko sygnały `published` w `api.signals`/`api.signal_detail`; `display_name` maskowany dla JDG wszędzie; rola `tie_reader` (bez logowania w migracji; `grant usage on schema api`, `grant select on all tables in schema api`), instrukcja nadania hasła w OPERATIONS.md.

**Testy:** po syntetycznym przebiegu z kartelem `api.signals` ma 1 wiersz z polami z kontraktu; JDG-member ma `display_name = 'NIP ...'` i nazwa nie występuje w żadnej kolumnie żadnego widoku `api.*` (test serializuje wszystkie wiersze wszystkich widoków do tekstu i szuka nazwy) (Review Focus 4); `api.oecd_catalog` ma 51 wierszy.

---

### Task 10: Orkiestracja, CLI, harmonogram

**Files:** Create `collector/src/tie/engine/run.py`; Modify `collector/src/tie/cli.py`, `collector/src/tie/handlers.py`, `collector/crontab`, `docs/OPERATIONS.md`; Test `collector/tests/engine/test_run.py`.

**Interfaces:** `run_engine(ctx, as_of: date, *, full: bool = False) -> dict` (nowy `engine_runs`, kolejno: seed katalogu, rynki, pary, klastry, testy części/firm/par/rynku, sygnały, opisy dla opublikowanych bez opisu; zapis statusu i liczników; całość w jednym `run_id`); CLI `tie engine-run [--as-of YYYY-MM-DD] [--full]`; handler `engine_run`; crontab: codziennie 02:30 UTC `tie engine-run`.

**Testy:** pełny przebieg na rynku syntetycznym z kartelem kończy się statusem `done`, 1 opublikowanym sygnałem (fake decider), opisem `template`; drugi przebieg tego samego dnia działa (nowy `run_id`, ten sam `signal_id`).

---

## Po części 2

Walidacja (część 3b): testy A/B/C na zbiorze wzorcowym z części 3a, kalibracja `publish_threshold`, strona "Sprawdziliśmy się".
