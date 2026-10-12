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

The harness lives in `collector/src/tie/validation/` and runs as `tie validate`. It reads
`gold_cases` (UOKiK) and `gold_kio` (KIO rulings, `tie gold-kio`), the engine tables of the
latest finished runs (`clusters`, `signals`) and the bids in our database. It never calls an
AI provider and never changes a signal.

- Test A (screens on known cartels, `checks.screen_known_cartels`): every tender of every
  UOKiK case is scored with the engine's own lot screens (`3.3.7` relative distance,
  `3.3.5` identical prices) and every pair of firms that met in at least two tenders of the
  decision with the pair screens (`3.1.6` rotation, `3.3.6` constant loser/winner price
  ratio). The functions are the engine's (`tie.engine.tests_lot`, `tie.engine.tests_pair`),
  applied to the prices from the decision. `3.3.9` needs the buyer's estimate, which the
  decisions do not give, so it is not part of test A. Each value is placed against the same
  screen computed over our bids (cross-checked opening documents only, prices after
  `price_scale`, 36-month window ending at `--as-of`) in the case's market: the sector maps
  to a CPV prefix (`gold.SECTOR_CPV`, or `source.cpv` in the payload) and the region to a
  voivodeship. Markets are tried from fine to coarse (CPV 5 digits + voivodeship, CPV 3 +
  voivodeship, CPV 3 Poland, CPV 2 + voivodeship, CPV 2 Poland); per screen the first market
  with at least 30 lots and 10 observations is used, else the coarsest with 10 observations.
  Percentile = share of the background strictly less suspicious than the gold value (ties
  count against it). A tender or pair is a hit when its best screen is in the top 5%
  (percentile >= 0.95). Rigged tenders and pairs of two colluders are the positives; the
  non-rigged tenders and the other pairs of the same decisions are the control. Gate: at
  least 60% of the evaluable positives in the top 5%, with at least 20 evaluable.
  A case counts as detected when half of its evaluable rigged tenders, or one pair of its
  colluders, is a hit.
- Test B (the full engine on our data, `checks.engine_on_our_data`): the colluders of each
  UOKiK case and each KIO ruling are looked up among our bidders by NIP or by normalized
  name. The engine run used is the latest finished run whose `as_of` is before the decision
  (KIO: the ruling date; UOKiK: 1 January of the decision year, the exact date is not
  stored), so only earlier data counts (`lead = true`); without such a run the latest run is
  used and `lead = false`. Hit = at least two colluders in one engine cluster; the report
  gives the cluster's probability, status and decision source (`jev` or `fallback`) from
  `signals`. Misses carry a reason: `no_engine_run`, `colluders_not_in_our_data`,
  `never_bid_together`, `not_in_same_cluster`, `cluster_without_signal`. KIO rulings with
  `decision = 'overturned'` are reported separately: a hit there is a false alarm candidate.
- Test C (false alarms, `checks.false_alarms`): a random sample (`--sample`, fixed `--seed`)
  of the latest run's clusters in the markets of the gold set (CPV prefixes of the UOKiK
  cases, markets of the procedures KIO rulings link to), without any known colluder; the
  share at or above the review and the publish threshold. When no cluster falls in those
  markets, the sample is drawn from all markets (`scope = all_markets`). The manual review of
  the 50 strongest signals is a separate step.

Results, including failures, go to the "Sprawdziliśmy się" page.

## Calibration

`propose_thresholds` uses the probabilities of the test C sample. The current defaults of
`tie.engine.decide.upsert_signals` (publish 0.91, review 0.6) are floors, so a calibration can
only make the engine stricter:

- `review_threshold`: the lowest value on a 0.01 grid with at most 10% of the control
  clusters at or above it;
- `publish_threshold`: the lowest value with at most 1% of the control clusters at or above
  it, and at least `review_threshold + 0.05`.

With fewer than 30 control clusters the defaults stay (`basis = "defaults"`). The recall of
the test B hits at both thresholds is reported next to them. The gate parameters of the
publish gate (`PUBLISH_MAX_ITEM`, `PUBLISH_ITEM_FLOOR`, `PUBLISH_MIN_ITEMS`, `TOP_ITEMS`) are
stored with the run. `ready_for_human_review` is true only when test A and test C both pass.
`engine_max_status` stays `review`: the proposal is never applied automatically, a person reads
the report and only then may set `ENGINE_MAX_STATUS=published` and new thresholds on the
server through the repository.

Each run is one row in `calibration_runs` (migration `010_calibration.sql`): `params`
(proposed thresholds, current thresholds, gate parameters, `as_of`, sample, seed), `metrics`
(tests A, B, C, calibration, gate) and `cases` (public per-case rows). The view
`api.validation` exposes the latest run (contract in `docs/contracts/api-v1.md`).

## Running the validation on the server

From `/var/www/tie/infra`, after `gold-extract`, `gold-kio` and at least one engine run:

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm worker \
  tie validate --write-report /data/raw/validation/report.json
```

Options: `--as-of YYYY-MM-DD` (end of the background window, default today), `--sample N`
(control clusters for test C, default 200), `--seed N`. The command prints a markdown
summary, stores a `calibration_runs` row and, with `--write-report`, writes the JSON report and
the same summary as `report.md` next to it (host path `/var/www/tie/data/raw/validation/`).

## Reading the report

The JSON report has `params`, `metrics`, `cases`, `calibration_run_id` and `run_at`.

- `metrics.gate`: `test_a` and `test_c` are `pass`, `fail` or `insufficient_data`;
  `test_a_share` is the share of evaluable positives in the top 5% (spec gate 60%).
  `insufficient_data` is the normal state while the backfill is young: few bids per market
  give no background (`reason = background_too_small` per case).
- `metrics.test_a.items`: per screen, the share of positives and of controls in the top 5%
  and their ratio (`lift`). A screen with lift near 1 does not separate cartels from the rest
  and should lose weight in the fallback regression; a high lift supports a higher weight.
- `metrics.test_a.levels`: how often each market level was used; many `cpv2_pl` placements
  mean the comparison is coarse.
- `metrics.test_b`: `positives` (UOKiK + upheld KIO) with `evaluable`, `in_cluster` (hits),
  `flagged` (hit with probability at or above the review threshold), `lead_hits` and the
  reasons of misses; `kio_overturned` the same for rulings where collusion was not proven.
- `metrics.test_c`: `share_review` and `share_publish` of the control sample at the current
  thresholds; `metrics.calibration` the proposed thresholds with the control shares and the
  recall at them.
- `cases[]` (one per UOKiK decision and KIO ruling, shaped for the frontend): `name`, `year`,
  `sector`, `detected` (true, false, or null when not evaluable), `status`, `reason`,
  `oecd_items` (screens in the top 5% for UOKiK, items with probability >= 0.6 in the
  engine's signal for KIO), `percentile` (median best percentile of the rigged tenders),
  `probability`, `tenders`/`pairs` counts, `market`, `engine` (test B) and `firms`.
  Firm names are public only for legal entities with a capital company form (sp. z o.o.,
  S.A., cooperative and the like) that our data does not mark as a sole trader; every other
  firm appears as `NIP <nip>`, or not at all without a NIP. Names of persons never appear.

## Legal note

UOKiK decisions are public official documents: they are published by the President of UOKiK
under art. 31 pkt 15 and art. 31b of the act of 16 February 2007 on competition and consumer
protection, in a version without business secrets or other protected information. Official
documents are not subject to copyright (art. 4 of the Polish copyright act). The repository
keeps only the list of URLs, a short excerpt used as a test fixture and the structured data
derived from the decisions; the PDFs are downloaded at runtime and are not committed. Names of
sole traders appear in the decisions themselves; the public layer of zmowy.pl follows the
project rule of showing only the NIP of a sole trader.
