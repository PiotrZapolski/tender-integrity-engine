create extension if not exists pg_trgm;

create table buyers (
  id bigserial primary key,
  nip text unique,
  regon text,
  name text not null,
  buyer_type text,
  province text,
  city text,
  created_at timestamptz not null default now()
);

create table procedures (
  ocds_id text primary key,
  bzp_number text not null,
  notice_number text not null unique,
  buyer_id bigint references buyers(id),
  title text not null,
  order_type text,
  mode text,
  cpv_main text,
  cpv_all text[] not null default '{}',
  is_below_eu boolean,
  platform text,
  platform_url text,
  published_at timestamptz not null,
  raw_sha256 text,
  updated_at timestamptz not null default now()
);

create table bidders (
  id bigserial primary key,
  nip text unique,
  regon text,
  krs text,
  name text not null,
  name_norm text not null,
  is_jdg boolean,
  city text,
  province text,
  address text,
  wl_checked_at timestamptz,
  created_at timestamptz not null default now()
);
create index bidders_name_trgm on bidders using gin (name_norm gin_trgm_ops);

create table lots (
  id bigserial primary key,
  procedure_id text not null references procedures(ocds_id) on delete cascade,
  lot_no int not null,
  cpv text,
  estimated_value numeric(16,2),
  outcome text not null,
  n_offers int,
  n_rejected int,
  price_min numeric(16,2),
  price_max numeric(16,2),
  price_winner numeric(16,2),
  contract_value numeric(16,2),
  contract_date date,
  winner_bidder_id bigint references bidders(id),
  consortium boolean not null default false,
  unique (procedure_id, lot_no)
);

create table documents (
  id bigserial primary key,
  procedure_id text not null references procedures(ocds_id) on delete cascade,
  source text not null,
  source_doc_id text not null,
  file_name text not null,
  doc_type text not null,
  raw_sha256 text,
  text_method text,
  status text not null default 'new',
  error text,
  updated_at timestamptz not null default now(),
  unique (source, source_doc_id)
);

create table bids (
  id bigserial primary key,
  lot_id bigint not null references lots(id) on delete cascade,
  document_id bigint references documents(id) on delete set null,
  offer_no int,
  bidder_id bigint references bidders(id),
  bidder_name_raw text not null,
  bidder_nip_raw text,
  price numeric(16,2),
  price_scale numeric(18,6) not null default 1,
  is_winner boolean not null default false,
  rank int,
  parse_method text not null,
  match_confidence real,
  unique (lot_id, offer_no, bidder_name_raw)
);

create table jobs (
  id bigserial primary key,
  kind text not null,
  key text not null,
  payload jsonb not null default '{}',
  status text not null default 'queued',
  attempts int not null default 0,
  max_attempts int not null default 8,
  run_after timestamptz not null default now(),
  locked_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  unique (kind, key)
);
create index jobs_ready on jobs (status, run_after);

create table ai_calls (
  id bigserial primary key,
  provider text not null,
  model text not null,
  task text not null,
  input_tokens int not null,
  output_tokens int not null,
  cost_usd numeric(12,6) not null,
  latency_ms int not null,
  ok boolean not null,
  created_at timestamptz not null default now()
);

create table bidder_accounts (
  bidder_id bigint not null references bidders(id) on delete cascade,
  account_hash text not null,
  primary key (bidder_id, account_hash)
);
create index bidder_accounts_hash on bidder_accounts (account_hash);

create table wl_checks (
  day date primary key,
  requests int not null default 0
);
