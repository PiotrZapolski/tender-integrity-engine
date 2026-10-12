# Tender Integrity Engine (TIE)

Open analytical engine for screening bid-rigging risk in Polish public procurement.

The public presentation layer, zmowy.pl, lives in a separate repository. TIE is the engine
that institutions (contracting authorities, audit bodies, competition and procurement
regulators) and researchers can run themselves, on their own infrastructure, against open data.

This is a statistical signal, not an accusation.

## What it does

- Collects Polish public procurement data: BZP notices and bid-opening records from
  e-Zamowienia, enriched with VAT white list data (Ministry of Finance). KRS persons (board
  members, partners) from Monitor Sadowy i Gospodarczy (MSiG) follow in part 1b.
- Builds a co-bidder graph: which companies bid together, against each other, or in
  patterns that never overlap with real competition.
- Computes risk signals from the OECD bid-rigging indicator list, at the level of a
  procedure, a pair of firms, a firm and a market over time.
- Produces evidence packs for every signal and supports publication with a right of reply
  for the entities concerned.

A signal means "this pattern deserves a closer look". It does not establish that any
agreement took place.

## Data sources

| Source | Endpoint | Part |
|---|---|---|
| BZP (Biuletyn Zamowien Publicznych) | `https://ezamowienia.gov.pl/mo-board/api/v1/notice` | 1 |
| e-Zamowienia (tender documents, bid-opening records) | `mp-readmodels/api/Search/GetTenderDocuments`, `Tender/DownloadDocument` | 1 |
| VAT white list (Ministry of Finance): REGON, KRS number, hashed bank accounts | `https://wl-api.mf.gov.pl` | 1 |
| KRS persons via Monitor Sadowy i Gospodarczy (MSiG) | planned | 1b |
| UOKiK decisions on bid rigging | `https://uokik.gov.pl/decyzje-prezesa-uokik-w-sprawie-zmow-przetargowych` | 3 |
| KIO rulings (National Appeals Chamber) | `https://orzeczenia.uzp.gov.pl` | 3 |

All requests identify themselves with the User-Agent
`TenderIntegrityEngine/0.1 (+https://github.com/PiotrZapolski/tender-integrity-engine)` and
respect conservative per-host rate limits.

## Methodology

- [OECD Guidelines for Fighting Bid Rigging in Public Procurement, 2025 update, chapter 3 (Polish)](https://www.oecd.org/pl/publications/wytyczne-oecd-dotyczace-zwalczania-zmow-przetargowych-w-zamowieniach-publicznych-aktualizacja-2025_944084ad-pl/full-report/component-5.html)
- [European Commission Notice 2021/C 91/01 on tools to fight collusion in public procurement](https://eur-lex.europa.eu/legal-content/PL/TXT/?uri=CELEX:52021XC0318(01))
- [Design spec of the project (Polish)](docs/superpowers/specs/2026-10-12-zmowy-design.md)

## Architecture

```
   BZP API          e-Zamowienia          VAT white list (MF)
        \                |                      /
         v               v                     v
   +-----------------------------------------------------------+
   | collector (Python): job queue in Postgres, rate limits    |
   +-----------------------------------------------------------+
        | raw files (zstd)              | structured data
        v                               v
   /data/raw (disk)            Postgres: buyers, procedures, lots,
                                         bidders, bids, documents
                                          |
   +-----------------------------------------------------------+
   | engine (Python): markets, OECD tests, Leiden graph,       |
   |  evidence packs, Jev decisions, DeepSeek explanations     |
   +-----------------------------------------------------------+
                                          |
                               Postgres: markets, tests, clusters,
                                         signals, explanations
                                          |
                      read-only views -> presentation layer (zmowy.pl)
```

Components communicate only through Postgres tables and views. Every layer can be
recomputed from the layer below it.

## Run it yourself

Requirements: a Linux host with Docker Compose. Everything runs in containers: Postgres, a
one-shot `migrate`, one `worker` and a `cron` that queues the daily jobs.

```sh
git clone https://github.com/PiotrZapolski/tender-integrity-engine
cd tender-integrity-engine/infra
cp .env.example .env        # set POSTGRES_PASSWORD (and the same password in DATABASE_URL)
mkdir -p data/raw && sudo chown 65534:65534 data/raw   # raw files, written as user nobody
docker compose up -d --build
docker compose run --rm worker tie backfill --since 2024-01-01
docker compose run --rm worker tie stats
```

Settings in `infra/.env`: `DEEPSEEK_API_KEY` (LLM fallback for opening documents the
deterministic parser cannot read), `JEV_API_KEY` (matching bidders without NIP),
`AI_DAILY_BUDGET_USD` (hard daily cap on AI spend, default 5.0) and `WL_DAILY_CAP` (VAT
white list requests per day, default 90). BZP notices are collected without any key; jobs
that need a missing key fail and can be requeued once the key is set. Keep exactly one
`worker`. Day-to-day operation, monitoring and recovery recipes are in
[docs/OPERATIONS.md](docs/OPERATIONS.md).

## Status

| Part | Scope | Status |
|---|---|---|
| 1 | Collector: BZP 2021+, bid-opening records from e-Zamowienia, VAT white list | in progress |
| 1b | KRS persons (board members, partners) from MSiG | planned |
| 2 | Engine: OECD test catalogue, tests, graph, Jev decision | planned |
| 3 | Validation against UOKiK and KIO cases, threshold calibration | planned |
| 4 | Web application zmowy.pl (separate repository) | planned |
| 5 | More platforms: ezamawiajacy.pl (procedure links from BZP); platformazakupowa.pl and logintrade block file downloads in robots.txt | in progress |

## Licenses

- Code: [MIT](LICENSE).
- Derived data (signals, graphs, evidence packs): [CC BY 4.0](LICENSE-DATA.md).
- Source data is public information published by Polish public bodies and keeps its
  original terms.

## Contact

kontakt@zmowy.pl

## Po polsku

Tender Integrity Engine (TIE) to otwarty silnik analityczny do wykrywania ryzyka zmów
przetargowych w polskich zamówieniach publicznych. Zbiera dane z BZP i e-Zamówień, buduje
graf współoferentów i liczy sygnały z listy wskaźników OECD. Publiczna strona zmowy.pl
jest osobnym repozytorium, a TIE to silnik, który instytucje mogą uruchomić u siebie.
Wynik to sygnał statystyczny, nie zarzut. Kontakt: kontakt@zmowy.pl.
