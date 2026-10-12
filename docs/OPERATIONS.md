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
## Backup

- Postgres is backed up automatically by the host's `/opt/backup` (auto-discovered).
- Raw BZP JSON under `/var/www/tie/data/raw` can be excluded from the backup: it is
  re-downloadable from the source. PDFs fetched from tender platforms must stay in the backup.

## Keys

- API keys (`JEV_API_KEY`, `DEEPSEEK_API_KEY`) live only in `/var/www/tie/infra/.env` on the
  server. Never commit them and never log them.
- Rotate the Jev key after the hackathon.
- Never edit tracked files on the server; change the repo and redeploy.
