-- Validation gold set: one row per UOKiK bid-rigging decision, structured by DeepSeek
-- (`tie gold-extract`). `payload` follows tie.gold.extract.GoldCase.
create table gold_cases (
  decision text primary key,
  payload jsonb not null,
  extracted_at timestamptz not null default now()
);
