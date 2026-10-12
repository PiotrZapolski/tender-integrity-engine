-- Gold labels from rulings of the National Appeal Chamber (KIO) on exclusion of bidders
-- for collusion (art. 108 ust. 1 pkt 5 Pzp). decision: 'upheld' (KIO confirmed the
-- collusion ground), 'overturned' (KIO found it not proven), 'other'.
create table if not exists gold_kio (
  signature text primary key,
  ruling_date date,
  buyer text,
  procedure_ref text,
  ocds_id text,
  bzp_number text,
  colluders jsonb not null default '[]',
  decision text,
  url text,
  payload jsonb not null default '{}',
  updated_at timestamptz not null default now()
);
create index if not exists gold_kio_ocds on gold_kio (ocds_id);
