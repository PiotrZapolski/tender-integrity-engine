-- Analysis engine (part 2): runs, markets, test results, co-bidder pairs and clusters,
-- signals with their explanations, and the OECD checklist catalog shown on "Co sprawdzamy".
-- run_id columns are plain bigints (no foreign key): results of one run are replaced as a
-- whole by the engine, and old runs may be pruned without touching the published signals.

create table engine_runs (
  run_id bigserial primary key,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  as_of date not null,
  params jsonb not null default '{}',
  status text not null default 'running'
);

-- Market = (CPV group, buyer province, 36-month window); level says which fallback was used.
create table markets (
  market_id text primary key,
  cpv_group text,
  province text,
  level text,
  n_lots int,
  run_id bigint
);

create table lot_market (
  lot_id bigint primary key references lots(id) on delete cascade,
  market_id text not null,
  run_id bigint
);
create index lot_market_market on lot_market (market_id);

create table test_results (
  run_id bigint not null,
  test_id text not null,
  level text not null check (level in ('lot', 'firm', 'pair', 'market', 'cluster')),
  entity_key text not null,
  market_id text,
  value double precision,
  percentile double precision,
  n_obs int,
  evidence jsonb,
  primary key (run_id, test_id, level, entity_key)
);
create index test_results_market on test_results (run_id, market_id);

create table pairs (
  run_id bigint not null,
  a bigint not null,
  b bigint not null,
  market_id text not null,
  n_together int,
  a_wins int,
  b_wins int,
  other_wins int,
  rotation double precision,
  ratio_cv double precision,
  shared_account bool,
  shared_address bool,
  primary key (run_id, a, b, market_id),
  check (a < b)
);

create table clusters (
  run_id bigint not null,
  cluster_id text not null,
  market_id text,
  members bigint[],
  n_lots int,
  density double precision,
  primary key (run_id, cluster_id)
);

create table signals (
  signal_id text primary key,
  run_id bigint,
  cluster_id text,
  market_id text,
  probability double precision,
  strength int,
  oecd jsonb,
  status text not null check (status in ('published', 'review', 'hidden')),
  evidence_hash text,
  evidence jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index signals_status on signals (status);

create table explanations (
  signal_id text not null,
  evidence_hash text not null,
  source text not null check (source in ('template', 'deepseek')),
  title text,
  summary text,
  findings jsonb,
  alternatives text[],
  created_at timestamptz not null default now(),
  primary key (signal_id, evidence_hash)
);

create table oecd_catalog (
  item text primary key,
  section text not null,
  title text not null,
  status text not null check (status in ('A', 'C', 'N')),
  how text not null
);
