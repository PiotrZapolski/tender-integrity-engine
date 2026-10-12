-- Validation and calibration of the engine on the gold set (part 3b, `tie validate`).
-- One row per run: `params` = proposed thresholds and gate parameters, `metrics` = results
-- of tests A, B, C and the gate, `cases` = public per-case rows (firm names already masked:
-- only legal entities keep their name, other firms appear as `NIP <nip>` or not at all).
create table if not exists calibration_runs (
  id bigserial primary key,
  run_at timestamptz not null default now(),
  params jsonb not null default '{}',
  metrics jsonb not null default '{}',
  cases jsonb not null default '[]'
);
create index if not exists calibration_runs_run_at on calibration_runs (run_at desc, id desc);

-- api.validation: the latest calibration run for the page "Sprawdziliśmy się".
-- One row `kind = 'summary'` (metrics and params of the run, case columns null) and one row
-- `kind = 'case'` per gold case (UOKiK decision or KIO ruling).
create or replace view api.validation as
with latest as (
  select id, run_at, params, metrics, cases
  from calibration_runs
  order by run_at desc, id desc
  limit 1
)
select l.id as calibration_id, l.run_at, 'summary'::text as kind, 0 as position,
       null::text as source, null::text as case_key, null::text as name, null::int as year,
       null::text as sector, null::text as region, null::text as decision,
       null::boolean as detected, null::text as status, null::text as reason,
       null::text[] as oecd_items, null::double precision as percentile,
       null::double precision as probability, null::jsonb as firms, null::jsonb as detail,
       l.metrics, l.params
from latest l
union all
select l.id, l.run_at, 'case', c.ord::int,
       c.x ->> 'source', c.x ->> 'key', c.x ->> 'name', (c.x ->> 'year')::int,
       c.x ->> 'sector', c.x ->> 'region', c.x ->> 'decision',
       (c.x ->> 'detected')::boolean, c.x ->> 'status', c.x ->> 'reason',
       coalesce(array(select jsonb_array_elements_text(c.x -> 'oecd_items')), '{}'),
       (c.x ->> 'percentile')::double precision, (c.x ->> 'probability')::double precision,
       coalesce(c.x -> 'firms', '[]'::jsonb),
       jsonb_build_object('tenders', c.x -> 'tenders', 'pairs', c.x -> 'pairs',
                          'market', c.x -> 'market', 'patterns', c.x -> 'patterns',
                          'engine', c.x -> 'engine'),
       null::jsonb, null::jsonb
from latest l
cross join lateral jsonb_array_elements(l.cases) with ordinality as c(x, ord);

grant select on api.validation to tie_reader;
