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
  during summer time) and `tie stats` every 30 minutes.
- `worker` runs `tie worker`. Keep exactly one `worker` replica: the budget and dedup logic
  assume a single worker. Do not use `--scale worker=N`.
- `migrate` runs `tie migrate` once on every `up`, before `worker` and `cron` start.

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

The same works for `needs_review` documents. Bids that fail during identity matching are
logged (`identity: bid <id> skipped`) and retried by the next `identity` job; they never
block the other bids. Each failed run increments `bids.identity_failures`; after 3 failed
runs the bid is excluded from matching (`bids_identity_excluded` in `tie stats`). A rejected
Jev key (HTTP 401/403), the AI budget, a network outage, or a batch in which every new bid
failed stops the job without counting anything. After fixing the cause (for example a Jev
API change), give the excluded bids another chance:

```sql
update bids set identity_failures = 0 where bidder_id is null;
```

## Backup

- Postgres is backed up automatically by the host's `/opt/backup` (auto-discovered).
- Raw BZP JSON under `/var/www/tie/data/raw` can be excluded from the backup: it is
  re-downloadable from the source. PDFs fetched from tender platforms must stay in the backup.

## Keys

- API keys (`JEV_API_KEY`, `DEEPSEEK_API_KEY`) live only in `/var/www/tie/infra/.env` on the
  server. Never commit them and never log them.
- Rotate the Jev key after the hackathon.
- Never edit tracked files on the server; change the repo and redeploy.
