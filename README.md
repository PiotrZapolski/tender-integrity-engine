# Tender Integrity Engine (TIE)

Open analytical engine for screening bid-rigging risk in Polish public procurement.

The public presentation layer, zmowy.pl, lives in a separate repository. TIE is the engine
that institutions (contracting authorities, audit bodies, competition and procurement
regulators) and researchers can run themselves, on their own infrastructure, against open data.

This is a statistical signal, not an accusation.

## What it does

- Collects Polish public procurement data: BZP notices and bid-opening records from
  e-Zamowienia, enriched with company registry and VAT white list data.
- Builds a co-bidder graph: which companies bid together, against each other, or in
  patterns that never overlap with real competition.
- Computes risk signals from the OECD bid-rigging indicator list, at the level of a
  procedure, a pair of firms, a firm and a market over time.
- Produces evidence packs for every signal and supports publication with a right of reply
  for the entities concerned.

A signal means "this pattern deserves a closer look". It does not establish that any
agreement took place.

## Data sources

| Source | Endpoint |
|---|---|
| BZP (Biuletyn Zamowien Publicznych) | `https://ezamowienia.gov.pl/mo-board/api/v1/notice` |
| e-Zamowienia (tender documents, bid-opening records) | `mp-readmodels/api/Search/GetTenderDocuments`, `Tender/DownloadDocument` |
| VAT white list (Ministry of Finance) | `https://wl-api.mf.gov.pl` |
| UOKiK decisions on bid rigging | `https://uokik.gov.pl/decyzje-prezesa-uokik-w-sprawie-zmow-przetargowych` |
| KIO rulings (National Appeals Chamber) | `https://orzeczenia.uzp.gov.pl` |

All requests identify themselves with the User-Agent
`TenderIntegrityEngine/0.1 (+https://github.com/PiotrZapolski/tender-integrity-engine)` and
respect conservative per-host rate limits.

## Methodology

- [OECD Guidelines for Fighting Bid Rigging in Public Procurement, 2025 update, chapter 3 (Polish)](https://www.oecd.org/pl/publications/wytyczne-oecd-dotyczace-zwalczania-zmow-przetargowych-w-zamowieniach-publicznych-aktualizacja-2025_944084ad-pl/full-report/component-5.html)
- [European Commission Notice 2021/C 91/01 on tools to fight collusion in public procurement](https://eur-lex.europa.eu/legal-content/PL/TXT/?uri=CELEX:52021XC0318(01))
- [Design spec of the project (Polish)](docs/superpowers/specs/2026-10-12-zmowy-design.md)

## Architecture

```
   BZP API     e-Zamowienia     procurement platforms     KRS / CEIDG
        \            |                   |                   /
         v           v                   v                  v
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

## Status

| Part | Scope | Status |
|---|---|---|
| 1 | Collector: BZP 2021+, bid-opening records from e-Zamowienia, KRS | in progress |
| 2 | Engine: OECD test catalogue, tests, graph, Jev decision | planned |
| 3 | Validation against UOKiK and KIO cases, threshold calibration | planned |
| 4 | Web application zmowy.pl (separate repository) | planned |
| 5 | More platforms: platformazakupowa.pl, ezamawiajacy.pl, logintrade | planned |

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
