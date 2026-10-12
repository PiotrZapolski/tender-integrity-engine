# Validation: the UOKiK gold set

Before the engine's signals go public they are checked against cases where bid rigging is a
proven fact. The first source of such cases is the decisions of the President of UOKiK on
bid rigging (zmowa przetargowa, art. 6 ust. 1 pkt 7 of the Polish competition act). Each
decision describes concrete tenders: the buyer, the subject, every offer with its price, who
won, and which firms colluded. Turned into structured data, these cases form the gold set
(spec section 9, `docs/superpowers/specs/2026-10-12-zmowy-design.md`).

## What is in it

- `collector/src/tie/gold/sources.py`: `UOKIK_DECISIONS`, a curated list of 40 decisions from
  2015-2026 (number, PDF URL, year, sector, region, short note). Sources: the press release
  "Decyzje Prezesa UOKiK w sprawie zmów przetargowych" (uokik.gov.pl, the four seed decisions
  RGD-10/2023, RGD-11/2023, RGD-12/2023, RLU-7/2023) and the decision database
  decyzje.uokik.gov.pl, section "Porozumienia ograniczające konkurencję". Every URL was
  checked on 2026-10-12: HTTP 200, a PDF with a text layer, an operative part citing art. 6
  ust. 1 pkt 7. Sectors: school transport, construction and renovation, roads, forestry, tree
  felling, waste, IT, food supplies, railway sleepers and others; most regions are covered.
- `tie gold-extract`: downloads each decision (raw copy in RawStore under `gold`), extracts
  the text (`pdftotext`, OCR fallback), splits it into overlapping chunks of about 12 000
  characters and asks DeepSeek (task `gold_extract`) for strict JSON per chunk. The answers are
  validated with pydantic and merged: tenders by buyer + subject, bids by bidder, colluders by
  name. The result goes to the `gold_cases` table (migration `004_gold_cases.sql`), one row per
  decision.
- `tie gold-export PATH`: writes all stored cases into one JSON file. The committed copy lives
  at `validation/gold/uokik.json`.

Shape of one case (`payload` in `gold_cases`, an element of `cases` in the export):

```json
{
  "decision": "RLU-7/2023",
  "year": 2023,
  "colluders": [{"name": "Usługi Transportowe Skiba Marcin", "nip": null}],
  "tenders": [{
    "buyer": "Gmina Gorzków",
    "subject": "Dowóz uczniów do szkoły w roku szkolnym 2019/2020",
    "date": "2019-07-26",
    "year": 2019,
    "bids": [
      {"bidder": "TACHO-SYSTEM Marek Pilip", "price": 88944.0, "won": false},
      {"bidder": "Usługi Transportowe Skiba Marcin", "price": 98100.0, "won": true}
    ],
    "rigged": true
  }],
  "pattern": ["bid_suppression", "cover_bids"],
  "notes": "Najtańsza oferta wycofana, umowę zawarto z droższym uczestnikiem zmowy.",
  "source": {"url": "https://decyzje.uokik.gov.pl/...", "sector": "dowóz uczniów",
             "region": "lubelskie", "sha256": "...", "text_method": "pdftext",
             "chunks": 9, "failed_chunks": []}
}
```

`pattern` values: `rotation` (firms win in turns), `cover_bids` (sham offers),
`market_split` (tenders or areas divided), `bid_suppression` (offer withdrawn, documents not
completed, or no offer), `other`. `decision` and `year` come from the curated list, not from the
model. A chunk with an invalid answer is skipped and listed in `source.failed_chunks`; such
decisions deserve a manual look.

## Running the extraction on the server

Everything runs on the production box, never on a laptop. `DEEPSEEK_API_KEY` must be set in
`infra/.env`. From `/var/www/tie/infra`:

```sh
# one decision first, to check the output and the cost
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm worker tie gold-extract --only RLU-7/2023

# then the whole list (decisions already in gold_cases are skipped; --force re-extracts)
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm worker tie gold-extract

# export into the raw volume (host path /var/www/tie/data/raw/gold-export/uokik.json)
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm worker tie gold-export /data/raw/gold-export/uokik.json
```

The command prints a JSON summary (`extracted`, `skipped`, `failed`, `budget_exceeded`) and
exits with 1 when something failed or the budget stopped it. Every model call is a row in
`ai_calls` with task `gold_extract` and counts against `AI_DAILY_BUDGET_USD`; when the budget is
reached the run stops and the next run continues with the remaining decisions. A decision of
40-120 pages takes 5-15 calls; the whole list costs well under 1 USD at DeepSeek prices.
Downloads from decyzje.uokik.gov.pl are spaced by at least 2 seconds.

The export file is data, not code: copy it from the server and commit it to
`validation/gold/uokik.json` through a normal branch and pull request (the repository is the
source of truth). Check by hand a sample of cases against the PDFs before relying on them,
especially decisions with `failed_chunks` or tenders without prices.

## How the tests use it

- Test A (offers in market context): for each rigged tender of the gold set, the bids are
  scored by the engine against the market they belong to (sector, region, year). Pass: the
  tender and the colluding pairs are in the top 5% of risk of their market. Gate: below 60%
  the engine is improved before the public launch.
- Test B (full pipeline, cases from 2021 on): the tenders of recent decisions are matched to
  our BZP data; the full pipeline runs on data published before the decision date only (lead
  test), and we check whether the named firms land in the published clusters.
- Test C (false alarms): a control group of random firms from the same markets as the gold
  cases, plus a manual review of the 50 strongest signals. The gold set defines the markets
  and gives the positive examples against which the thresholds are calibrated.

Results, including failures, go to the "Sprawdziliśmy się" page.

## Legal note

UOKiK decisions are public official documents: they are published by the President of UOKiK
under art. 31 pkt 15 and art. 31b of the act of 16 February 2007 on competition and consumer
protection, in a version without business secrets or other protected information. Official
documents are not subject to copyright (art. 4 of the Polish copyright act). The repository
keeps only the list of URLs, a short excerpt used as a test fixture and the structured data
derived from the decisions; the PDFs are downloaded at runtime and are not committed. Names of
sole traders appear in the decisions themselves; the public layer of zmowy.pl follows the
project rule of showing only the NIP of a sole trader.
