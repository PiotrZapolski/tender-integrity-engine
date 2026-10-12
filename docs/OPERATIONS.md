# Operations

Production runs on one box under `/var/www/tie/`, as a Docker Compose project named `tie`
(services `db`, `migrate`, `worker`, `cron`). Server time is UTC.

## First run

1. Create the env file on the server from the template and fill in the keys:
   `cp infra/.env.example /var/www/tie/infra/.env`, then edit it (set a real
   `POSTGRES_PASSWORD` and the same password inside `DATABASE_URL`).
2. Make the raw files directory writable for the container user (`nobody`, uid 65534):
   `mkdir -p /var/www/tie/data/raw && chown 65534:65534 /var/www/tie/data/raw`.
3. Register a GitHub Actions self-hosted runner for the repo with the label `tie`
   (workflows use `runs-on: [self-hosted, tie]`). The runner user must be able to run
   `docker compose` and write to `/var/www/tie/`.
4. Deploy: a push to `master` (or a manual run of the `deploy` workflow) syncs the code to
   `/var/www/tie/`, runs `docker compose up -d --build` and prints `tie stats`. The sync
   excludes `infra/.env` and `data/`, so secrets and raw files survive deploys.
5. First backfill, from `/var/www/tie/infra`:
   `docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm worker tie backfill --since 2021-01-01`.
   Start with the newest days if you want to watch costs first.

## Daily operation

- `cron` (supercronic, `collector/crontab`) runs `tie daily` at 23:00 UTC (01:00 in Poland
  during summer time), `tie engine-run` at 02:30 UTC and `tie stats` every 30 minutes.
- Workers are split by job kind so one slow host never blocks the rest:
  - `worker-ezam`: `bzp_day`, `ezam_docs`, `ezam_download` (the only process talking to
    ezamowienia.gov.pl, so its 1 s gap holds). Keep one replica.
  - `worker-parse` (3 replicas): `opening_parse` (text extraction, OCR, DeepSeek). Jobs are
    claimed with `FOR UPDATE SKIP LOCKED`; the daily AI budget can be overshot by at most the
    calls in flight (3).
  - `worker`: ezamawiajacy, identity (Jev), whitelist, MSiG, engine. Keep one replica: each
    external host must be served by exactly one process so per-host rate limits hold.
- `migrate` runs `tie migrate` once on every `up`, before `worker` and `cron` start.

## Engine

The analysis engine (markets, OECD tests, co-bidder clusters, signals, explanations) runs
once a day. At 02:30 UTC `cron` runs `tie engine-run`, which only queues an `engine_run`
job (payload `{"as_of": "<today>", "full": false}`); the single `worker` runs it, so the
engine shares the worker's CPU and memory limits and the AI budget. While a long backfill
is queued the job waits behind the older ready jobs.

Each run gets a new row in `engine_runs` and goes through: OECD catalog seed, markets,
pairs, clusters, lot, firm, pair and market tests, signals (Jev decision, or the fallback
regression with status at most `review` when `JEV_API_KEY` is missing or the budget is
spent), and explanations of published signals that have none for their current evidence
(DeepSeek when `DEEPSEEK_API_KEY` is set, otherwise the template). A step that fails marks
the run `failed` with the step and the error in `params`, and the job is retried with the
usual backoff. A second run on the same day is safe: it gets a new `run_id`, signals keep
their `signal_id`, and unchanged evidence is not sent to Jev again. "Unchanged" is judged on
a coarse decision package (members, OECD items present, percentiles rounded to 0.05), so
the daily slide of the 36-month window does not trigger new Jev calls and new explanations.

Publication switch `ENGINE_MAX_STATUS` (in `infra/.env`, default `review`): the highest
status the engine may give a signal (`hidden`, `review` or `published`). With the default
nothing is published and `api.signals` stays empty; every candidate lands in `review`. Set
`ENGINE_MAX_STATUS=published` only after the thresholds were calibrated on the gold set and
a sample of `review` signals was checked by a person, then redeploy. Even then a signal is
published only when Jev (never the fallback) gives a combined probability of at least 0.91
from its three strongest OECD items, the strongest item at least 0.8 and at least two items
at least 0.6. The value used is recorded in `engine_runs.params->>'max_status'`.

Minimum evidence gate (applied after the thresholds, it only lowers a status):

- `ENGINE_MIN_LOTS` (in `infra/.env`, default 8): a cluster with fewer common lots
  (`clusters.n_lots`), or whose lots come from fewer than 2 different buyers, is at most
  `hidden`. The value used is recorded in `engine_runs.params->>'min_lots'`.
- Strong and weak OECD items: a cluster is at most `review` unless at least one strong item
  fired, i.e. is present in the evidence, has a decision probability of at least 0.6 and,
  for 3.3.10, no `signal = false` flag. Strong items: 3.1.6 (rotation), 3.3.6 (price
  pattern), 3.2.11 (shared account or address), 3.1.2 (territorial split), 3.3.10 (price
  drop after entry), 3.3.5 (identical prices). Weak items: 3.1.1 (frequent winner), 3.3.3
  (small discounts), 3.3.9 (price above estimate), 3.1.5 (always bids, never wins), 3.3.7
  (gap between winner and losers). Weak items alone never publish a signal, even with
  `ENGINE_MAX_STATUS=published`.

The outcome is stored per signal in `signals.evidence->'decision'->>'gate'`: `ok`,
`min_lots`, `min_buyers` or `weak_only`; run counts carry `"gates": {"min_lots": n, ...}`
for the non-`ok` outcomes. `signals.evidence` also has the market at the top level
(`market_id`, `market_label` such as `CPV 45210 · wielkopolskie`, and `n_buyers`):

```sql
select status, evidence->'decision'->>'gate' gate, evidence->>'market_label' market,
       (evidence->>'n_lots')::int n_lots, (evidence->>'n_buyers')::int n_buyers,
       probability, oecd
from signals order by probability desc limit 20;
```

Jev errors: an HTTP 4xx other than 429 (rejected key, wrong request shape, unknown model)
turns Jev off for the rest of the run after the first call; the error and the start of the
response body are logged once, the remaining clusters get the fallback, and the reason is
stored in `engine_runs.params->>'jev_disabled_reason'` (also after a spent budget or 5
consecutive other errors). If a Jev key is configured but a run made no Jev decision, the
run counts carry `"warning": "decider configured, 0 jev calls"`:

```sql
select run_id, params->>'jev_disabled_reason' jev_off, params->'counts'->'signals' signals
from engine_runs order by run_id desc limit 5;
```

Pruning: after each successful run `test_results`, `pairs` and `clusters` keep only the
rows of the last 2 `done` runs, and `explanations` drops rows of an older evidence version
once the signal has an explanation for its current one. The deleted row counts are in
`params->'counts'->'prune'`. `signals.evidence` is self-contained, so the API does not need
the pruned rows.

Run it by hand, from `/var/www/tie/infra`:

- queue a run for the worker (as cron does): `... run --rm worker tie engine-run`;
- for another date or flagged as a full recompute: `tie engine-run --as-of 2026-10-01 --full`
  (`--full` is only recorded in `params` for now; every run recomputes all markets);
- run it in the foreground and print the counters as JSON:
  `docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm worker tie engine-run --now`.

Only one run works at a time (Postgres advisory lock); a second one fails with "another
engine run is in progress" and its job is retried later. A run still `running` after 12
hours (worker killed) is marked `failed` by the next run. Check the latest runs:

```sql
select run_id, as_of, status, started_at, finished_at - started_at took,
       params->'counts' counts, params->>'failed_step' step, params->>'error' error
from engine_runs order by run_id desc limit 10;
```

Signals by status: `select status, count(*) from signals group by 1;`.

## Read-only role for the frontend

The zmowy.pl frontend reads the engine's results through the views in schema `api` as the
role `tie_reader`. Migration 006 creates `tie_reader` without login. Once per database,
from `/var/www/tie/infra`, give it a password:
`docker compose -f docker-compose.yml -f docker-compose.prod.yml exec db psql -U tie -d tie -c "alter role tie_reader with login password '<strong password>'"`.
The frontend connects as `tie_reader` and reads only schema `api`; it cannot read the
tables behind the views. Names of bidders that may be natural persons are masked in `api.*`
(shown as `NIP <nip>`) until the whitelist check confirms a KRS entry.

## Monitoring

`tie stats` prints counts, AI spend and the share of `mismatch` rows. Run it on demand:
`docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm worker tie stats`.
Logs: `docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f worker cron`.

## AI costs

Every AI call writes a row to the `ai_calls` table. The daily limit is `AI_DAILY_BUDGET_USD`
(default 5.0); once it is reached further calls fail with `BudgetExceeded` until the next day.
Spend per day:
`select date_trunc('day', created_at) d, sum(cost_usd) from ai_calls group by 1 order by 1 desc;`

## Recipes

Run SQL from `/var/www/tie/infra` with:
`docker compose -f docker-compose.yml -f docker-compose.prod.yml exec db psql -U tie tie -c "<sql>"`.

Check AI spend today (UTC day, the same window the budget uses), per provider and task:

```sql
select provider, task, count(*) calls, sum(cost_usd) usd, count(*) filter (where not ok) failed
from ai_calls where created_at >= date_trunc('day', now())
group by 1, 2 order by usd desc;
```

Requeue failed jobs of one kind after fixing the cause (for example a missing
`DEEPSEEK_API_KEY` makes every `opening_parse` job fail, a missing `JEV_API_KEY` every
`identity` job). Add the key to `infra/.env`, redeploy, then:

```sql
update jobs set status = 'queued', attempts = 0, run_after = now(), locked_at = null,
  last_error = null
where status = 'failed' and kind = 'opening_parse';
```

Running `tie backfill` again does not help: jobs are deduplicated on `(kind, key)` whatever
their status, so failed jobs must be requeued with the statement above.

Re-parse documents rejected by the cross-check (`status = 'mismatch'`), for example after a
parser fix. The raw file is already in the raw store, so nothing is downloaded again; each
document gets a new `opening_parse` job with a fresh key:

```sql
insert into jobs(kind, key, payload)
select 'opening_parse', source_doc_id || '@reparse-' || to_char(now(), 'YYYYMMDDHH24MI'),
       jsonb_build_object('document_id', id)
from documents where status = 'mismatch' and doc_type = 'opening'
on conflict (kind, key) do nothing;
```

Run it only after the fix is deployed (the worker picks the jobs up at once). The key of
the original job is the bare `source_doc_id`, and keys are unique per kind whatever the job
status, so the `@reparse-<timestamp>` suffix is required; running the statement again a
minute later queues another round. To re-parse only the documents rejected on prices (for
example after the rejected-offers rule, where BZP min/max cover only non-rejected offers),
add `and error like '%prices%'`. Watch the outcome:

```sql
select status, count(*) from documents where doc_type = 'opening' group by 1 order by 2 desc;
select count(*) filter (where outside_bzp_range) outside, count(*) bids from bids;
```

The same works for `needs_review` documents. After a parser release that changes what can
be read (for example per-lot acceptance, the `bzp_partial` rule, `.doc`/`.odt` support or
the OCR orientation fix), re-queue both kinds in one go:

```sql
insert into jobs(kind, key, payload)
select 'opening_parse', source_doc_id || '@reparse-' || to_char(now(), 'YYYYMMDDHH24MI'),
       jsonb_build_object('document_id', id)
from documents
where status in ('mismatch', 'needs_review') and doc_type = 'opening'
  and coalesce(error, '') <> 'raw_missing'
on conflict (kind, key) do nothing;
```

Each re-parsed document makes at most one DeepSeek call (`AI_DAILY_BUDGET_USD` still
applies). `parsed` documents with `error` starting with `partial:` have bids for the lots
that passed and none for the lots listed after `partial:` (`partial` in `tie stats`); the
`notes:` part names the relaxed cross-check rules that were used (`bzp_partial`,
`winner_by_price`, `winner_unscaled`, `bzp_no_winner`, `lot_offset:<d>`). Watch the result:

```sql
select status, split_part(error, ':', 1) kind, count(*) from documents
where doc_type = 'opening' group by 1, 2 order by 1, 3 desc;
```

Bids that fail during identity matching are
logged (`identity: bid <id> skipped`) and retried by the next `identity` job; they never
block the other bids. Each failed run increments `bids.identity_failures`; after 3 failed
runs the bid is excluded from matching (`bids_identity_excluded` in `tie stats`). A rejected
Jev key (HTTP 401/403), the AI budget, a network outage, or a batch in which every new bid
failed stops the job without counting anything. After fixing the cause (for example a Jev
API change), give the excluded bids another chance:

```sql
update bids set identity_failures = 0 where bidder_id is null;
```

## Opening documents from ezamawiajacy.pl

Procedures on the Marketplanet platform (`procedures.platform = 'ezamawiajacy'`) get an
`ezamawiajacy_docs` job when BZP field 1.6 links to the procedure page
(`/pn/<buyer>/demand/<id>/notice/public/details`) and a lot has at least 2 offers. A link to
the buyer's home page only cannot be followed and is skipped. All `*.ezamawiajacy.pl` hosts
share one gap of 5 s between requests; robots.txt of each host is read first and obeyed
(a disallowed download leaves the document with `status = 'blocked'`). Files go to the raw
store folder `ezamawiajacy`.

Procedures stored before this collector existed need the jobs queued once:

```sql
insert into jobs(kind, key, payload)
select 'ezamawiajacy_docs', p.ocds_id, jsonb_build_object('ocds_id', p.ocds_id)
from procedures p
where p.platform = 'ezamawiajacy'
  and p.platform_url ~ '/pn/[^/]+/demand/(\d+/notice/public|notice/public/\d+)/details/?$'
  and exists (select 1 from lots l where l.procedure_id = p.ocds_id and l.n_offers >= 2)
on conflict (kind, key) do nothing;
```

The job reads the URL from `procedures.platform_url` when the payload has none.

## Backup

- Postgres is backed up automatically by the host's `/opt/backup` (auto-discovered).
- Raw BZP JSON under `/var/www/tie/data/raw` can be excluded from the backup: it is
  re-downloadable from the source. PDFs fetched from tender platforms must stay in the backup.

## Keys

- API keys (`JEV_API_KEY`, `DEEPSEEK_API_KEY`) live only in `/var/www/tie/infra/.env` on the
  server. Never commit them and never log them.
- Rotate the Jev key after the hackathon.
- `PERSON_HMAC_SECRET` keys the MSiG person hashes (`board_links.person_key`, see
  `docs/msig.md`). Generate it once (`openssl rand -hex 32`), keep it only in `.env` and in
  the owner's password manager. While it is unset, `tie daily` queues no `msig_board` jobs.
  Changing it changes every key: after a change run
  `truncate board_links, msig_announcements; update bidders set msig_checked_at = null;`
  and let the daily jobs rebuild the links.
- Never edit tracked files on the server; change the repo and redeploy.
