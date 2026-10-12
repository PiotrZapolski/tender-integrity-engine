-- Read-only API for the zmowy.pl frontend: schema `api`, exactly per docs/contracts/api-v1.md.
-- The frontend connects as `tie_reader`, which may only read the views in `api`; the views run
-- with the owner's privileges, so the reader never needs access to the tables behind them.
--
-- Privacy: names of bidders that may be natural persons never leave this layer. A bidder is
-- masked when `is_jdg` is true, or when it is unknown (`is_jdg` null) and the bidder has no
-- KRS number (sole traders are not in KRS); masked bidders appear as `NIP <nip>` everywhere,
-- and their names are also replaced inside generated texts (titles, summaries, findings,
-- alternatives). Raw `signals.evidence` and `test_results` are never exposed.
--
-- Only public signals are visible (api.signals, api.signal_detail, every `signal_ids` and
-- stats.signals_published): status `published` and `run_id` of the latest engine run with
-- status `done`.
--
-- The role is created without login. Operator, once per database (see docs/OPERATIONS.md):
--   alter role tie_reader with login password '<strong password>';

create schema if not exists api;

-- Helper functions (pure, no table access, so the reader can execute them inside the views).

create or replace function tie_province_name(p text) returns text
language sql immutable as $$
  select case regexp_replace(upper(btrim(p)), '^PL-?', '')
    when '' then null
    when '02' then 'dolnośląskie'
    when '04' then 'kujawsko-pomorskie'
    when '06' then 'lubelskie'
    when '08' then 'lubuskie'
    when '10' then 'łódzkie'
    when '12' then 'małopolskie'
    when '14' then 'mazowieckie'
    when '16' then 'opolskie'
    when '18' then 'podkarpackie'
    when '20' then 'podlaskie'
    when '22' then 'pomorskie'
    when '24' then 'śląskie'
    when '26' then 'świętokrzyskie'
    when '28' then 'warmińsko-mazurskie'
    when '30' then 'wielkopolskie'
    when '32' then 'zachodniopomorskie'
    else nullif(lower(btrim(p)), '')
  end
$$;

-- Short Polish name of the CPV division (first two digits of the code).
create or replace function tie_cpv_division(code text) returns text
language sql immutable as $$
  select case left(btrim(code), 2)
    when '03' then 'Produkty rolne i leśne'
    when '09' then 'Paliwa i energia'
    when '14' then 'Produkty górnictwa i metale'
    when '15' then 'Żywność i napoje'
    when '16' then 'Maszyny rolnicze'
    when '18' then 'Odzież i obuwie'
    when '19' then 'Skóra, tekstylia, tworzywa'
    when '22' then 'Druki'
    when '24' then 'Produkty chemiczne'
    when '30' then 'Sprzęt biurowy i komputerowy'
    when '31' then 'Urządzenia elektryczne i oświetlenie'
    when '32' then 'Sprzęt radiowy i telekomunikacyjny'
    when '33' then 'Sprzęt medyczny i farmaceutyki'
    when '34' then 'Sprzęt transportowy'
    when '35' then 'Sprzęt bezpieczeństwa i obronny'
    when '37' then 'Artykuły sportowe, zabawki, instrumenty'
    when '38' then 'Sprzęt laboratoryjny i precyzyjny'
    when '39' then 'Meble, wyposażenie, środki czystości'
    when '41' then 'Woda'
    when '42' then 'Maszyny przemysłowe'
    when '43' then 'Maszyny górnicze i budowlane'
    when '44' then 'Konstrukcje i materiały budowlane'
    when '45' then 'Roboty budowlane'
    when '48' then 'Oprogramowanie i systemy informatyczne'
    when '50' then 'Usługi naprawcze i konserwacyjne'
    when '51' then 'Usługi instalacyjne'
    when '55' then 'Usługi hotelarskie i gastronomiczne'
    when '60' then 'Usługi transportowe'
    when '63' then 'Usługi pomocnicze w transporcie'
    when '64' then 'Usługi pocztowe i telekomunikacyjne'
    when '65' then 'Usługi użyteczności publicznej'
    when '66' then 'Usługi finansowe i ubezpieczeniowe'
    when '70' then 'Usługi w zakresie nieruchomości'
    when '71' then 'Usługi architektoniczne i inżynieryjne'
    when '72' then 'Usługi informatyczne'
    when '73' then 'Usługi badawcze'
    when '75' then 'Usługi administracji publicznej'
    when '76' then 'Usługi dla przemysłu naftowego i gazowego'
    when '77' then 'Usługi rolnicze, leśne i ogrodnicze'
    when '79' then 'Usługi biznesowe'
    when '80' then 'Usługi edukacyjne i szkoleniowe'
    when '85' then 'Usługi zdrowotne i społeczne'
    when '90' then 'Usługi komunalne i odpady'
    when '92' then 'Usługi rekreacyjne, kulturalne i sportowe'
    when '98' then 'Inne usługi komunalne i osobiste'
  end
$$;

-- Polish plural form for a count: 1 przetarg, 2 przetargi, 5 przetargów.
create or replace function tie_plural(n bigint, one text, few text, many text) returns text
language sql immutable as $$
  select case
    when n = 1 then one
    when n % 10 between 2 and 4 and n % 100 not between 12 and 14 then few
    else many
  end
$$;

-- Compact money label: 4,8 mln zł, 350 tys. zł.
create or replace function tie_money_short(v numeric) returns text
language sql immutable as $$
  select case
    when v is null then null
    when abs(v) >= 1000000000 then
      replace(to_char(round(v / 1000000000, 1), 'FM999999990.0'), '.', ',') || ' mld zł'
    when abs(v) >= 1000000 then
      replace(to_char(round(v / 1000000, 1), 'FM999999990.0'), '.', ',') || ' mln zł'
    when abs(v) >= 1000 then to_char(round(v / 1000), 'FM999999990') || ' tys. zł'
    else to_char(round(v), 'FM999999990') || ' zł'
  end
$$;

-- Replace every name (case-insensitive, literal match) with its mask.
create or replace function tie_mask_text(t text, names text[], masks text[]) returns text
language plpgsql immutable as $$
declare
  i int;
begin
  if t is null or names is null then
    return t;
  end if;
  for i in 1 .. coalesce(array_length(names, 1), 0) loop
    if names[i] is not null and length(btrim(names[i])) >= 3 then
      t := regexp_replace(
        t,
        regexp_replace(btrim(names[i]), '([.^$*+?()\[\]{}|\\])', '\\\1', 'g'),
        coalesce(masks[i], 'NIP'),
        'gi');
    end if;
  end loop;
  return t;
end
$$;

-- The same for JSON documents: names are matched in their JSON-escaped form, so the result
-- stays valid JSON; if it still cannot be parsed, the document is dropped rather than leaked.
create or replace function tie_mask_jsonb(j jsonb, names text[], masks text[]) returns jsonb
language plpgsql immutable as $$
declare
  enames text[];
  emasks text[];
begin
  if j is null or names is null or coalesce(array_length(names, 1), 0) = 0 then
    return j;
  end if;
  select array_agg(substr(to_json(u.n)::text, 2, length(to_json(u.n)::text) - 2) order by u.o),
         array_agg(substr(to_json(u.m)::text, 2, length(to_json(u.m)::text) - 2) order by u.o)
    into enames, emasks
    from unnest(names, masks) with ordinality as u(n, m, o);
  return tie_mask_text(j::text, enames, emasks)::jsonb;
exception when others then
  return null;
end
$$;

-- Indexes for the per-entity lookups the views do.
create index if not exists bids_bidder_id_idx on bids (bidder_id);
create index if not exists lots_winner_bidder_id_idx on lots (winner_bidder_id);
create index if not exists procedures_buyer_id_idx on procedures (buyer_id);
create index if not exists bidders_id_text_idx on bidders ((id::text));
create index if not exists buyers_id_text_idx on buyers ((id::text));
create index if not exists clusters_cluster_id_idx on clusters (cluster_id);

-- Internal helper views (schema public, not granted to the reader).

-- Public face of a bidder; the real name is deliberately not a column here.
create or replace view v_bidder_public as
select
  b.id as bidder_id,
  b.nip,
  b.is_jdg,
  (b.is_jdg is true or (b.is_jdg is null and b.krs is null)) as masked,
  case
    when b.is_jdg is true or (b.is_jdg is null and b.krs is null)
      then coalesce('NIP ' || b.nip, 'Wykonawca bez NIP')
    else b.name
  end as display_name,
  case when b.is_jdg is true or (b.is_jdg is null and b.krs is null) then null else b.krs end
    as krs,
  tie_province_name(b.province) as province
from bidders b;

-- Signal with its cluster (the cluster row of the signal's run, else the latest one).
-- `is_public`: published and confirmed by the latest finished engine run; signals of clusters
-- that disappeared keep their row but drop out of the API.
create or replace view v_signal_cluster as
select
  s.signal_id,
  s.status,
  (s.status = 'published' and s.run_id is not distinct from (
    select max(r.run_id) from engine_runs r where r.status = 'done')) as is_public,
  s.probability,
  s.strength,
  s.oecd,
  s.evidence,
  s.evidence_hash,
  coalesce(c.market_id, s.market_id) as market_id,
  coalesce(c.members, '{}'::bigint[]) as members
from signals s
left join lateral (
  select c.market_id, c.members
  from clusters c
  where c.cluster_id = coalesce(s.cluster_id, s.signal_id)
  order by (c.run_id = s.run_id) desc nulls last, c.run_id desc
  limit 1
) c on true;

-- Members with their letter keys: the key from the evidence package when present, else the
-- position in members sorted by bidder_id (the same rule as tie.engine.evidence).
create or replace view v_signal_members as
select
  sc.signal_id,
  m.bidder_id,
  m.ord,
  coalesce(ek.key, chr(64 + m.ord::int)) as key
from v_signal_cluster sc
cross join lateral unnest(
  (select array_agg(x order by x) from unnest(sc.members) x)
) with ordinality as m(bidder_id, ord)
left join lateral (
  select e ->> 'key' as key
  from jsonb_array_elements(
    case when jsonb_typeof(sc.evidence -> 'members') = 'array'
      then sc.evidence -> 'members' else '[]'::jsonb end
  ) e
  where jsonb_typeof(e) = 'object'
    and e ->> 'bidder_id' = m.bidder_id::text
    and nullif(e ->> 'key', '') is not null
  limit 1
) ek on true;

-- Lots of the signal: lots of the cluster's market where at least two members bid (offers
-- from documents that passed the BZP cross-check), as in tie.engine.evidence.
create or replace view v_signal_lots as
select
  sc.signal_id,
  l.id as lot_id,
  l.procedure_id,
  l.lot_no,
  p.buyer_id,
  coalesce(l.contract_date, (p.published_at at time zone 'Europe/Warsaw')::date) as lot_date,
  l.winner_bidder_id,
  l.n_offers,
  coalesce(l.contract_value, l.price_winner) as value,
  p.title
from v_signal_cluster sc
join lateral (
  select b.lot_id
  from bids b
  join documents d on d.id = b.document_id and d.status = 'parsed'
  where b.bidder_id = any(sc.members)
  group by b.lot_id
  having count(distinct b.bidder_id) >= 2
) cl on true
join lot_market lm on lm.lot_id = cl.lot_id and lm.market_id = sc.market_id
join lots l on l.id = cl.lot_id
join procedures p on p.ocds_id = l.procedure_id;

-- Names to mask inside the texts of a signal: masked members, masked bidders and winners of
-- the signal's lots. Longer names first, so a name containing another one is replaced whole.
create or replace view v_signal_masks as
select sc.signal_id, m.names, m.masks
from v_signal_cluster sc
cross join lateral (
  select
    coalesce(array_agg(b.name order by length(b.name) desc, b.id), '{}'::text[]) as names,
    coalesce(array_agg(bp.display_name order by length(b.name) desc, b.id), '{}'::text[])
      as masks
  from bidders b
  join v_bidder_public bp on bp.bidder_id = b.id
  where bp.masked and b.id in (
    select unnest(sc.members)
    union
    select bb.bidder_id
    from bids bb join v_signal_lots sl on sl.lot_id = bb.lot_id
    where sl.signal_id = sc.signal_id
    union
    select sl.winner_bidder_id from v_signal_lots sl where sl.signal_id = sc.signal_id
  )
) m;

-- Current explanation of a signal: the one for the current evidence version, else the newest.
create or replace view v_signal_explanation as
select sc.signal_id, ex.title, ex.summary, ex.findings, ex.alternatives
from v_signal_cluster sc
join lateral (
  select e.title, e.summary, e.findings, e.alternatives
  from explanations e
  where e.signal_id = sc.signal_id
  order by (e.evidence_hash = sc.evidence_hash) desc nulls last, e.created_at desc
  limit 1
) ex on true;

-- Bidders with at least one bid: an offer from a cross-checked document or a BZP win.
create or replace view v_active_bidders as
select bp.*
from v_bidder_public bp
where exists (
    select 1 from bids b join documents d on d.id = b.document_id and d.status = 'parsed'
    where b.bidder_id = bp.bidder_id)
  or exists (select 1 from lots l where l.winner_bidder_id = bp.bidder_id);

-- api.stats (1 row)
create or replace view api.stats as
select
  (select count(*) from procedures)::int as procedures,
  (select count(*) from lots)::int as lots,
  (select count(*) from bids b join documents d on d.id = b.document_id
    where d.status = 'parsed')::int as bids,
  (select count(*) from (
    select b.bidder_id from bids b join documents d on d.id = b.document_id
    where d.status = 'parsed' and b.bidder_id is not null
    union
    select l.winner_bidder_id from lots l where l.winner_bidder_id is not null) x)::int
    as bidders,
  (select count(*) from v_signal_cluster where is_public)::int as signals_published,
  (select round(
     count(*) filter (where status = 'parsed')::numeric
     / nullif(count(*) filter (where status in ('parsed', 'mismatch', 'needs_review')), 0),
     4)
   from documents where doc_type = 'opening') as parse_ok_share,
  (select count(*) from oecd_catalog where status = 'A')::int as oecd_checked,
  (select count(*) from oecd_catalog where status = 'C')::int as oecd_partial,
  (select max(finished_at) from engine_runs where status = 'done') as updated_at;

-- api.signals (one row per published signal, ranked)
create or replace view api.signals as
select
  sc.signal_id,
  coalesce(
    tie_mask_text(ex.title, ms.names, ms.masks),
    'Możliwa zmowa przetargowa: ' || mk.label
  ) as title,
  mk.label as market_label,
  mk.cpv_group,
  mk.province,
  agg.period_from,
  agg.period_to,
  round(sc.probability::numeric, 4) as probability,
  sc.strength,
  coalesce(nullif(oi.from_decision, '{}'::text[]), oi.from_findings) as oecd_items,
  agg.n_procedures::int as n_procedures,
  coalesce(agg.total_value, 0) as total_value,
  jsonb_build_array(
    jsonb_build_object(
      'value', agg.n_procedures::text,
      'label', tie_plural(agg.n_procedures, 'przetarg razem', 'przetargi razem',
                          'przetargów razem')),
    jsonb_build_object(
      'value', mem.n::text,
      'label', tie_plural(mem.n, 'firma w grupie', 'firmy w grupie', 'firm w grupie')),
    jsonb_build_object(
      'value', tie_money_short(coalesce(agg.total_value, 0)),
      'label', 'łączna wartość')
  ) as facts,
  st.strip,
  mem.members
from v_signal_cluster sc
left join markets m on m.market_id = sc.market_id
cross join lateral (
  select
    coalesce(m.cpv_group, nullif(split_part(sc.market_id, ':', 2), '')) as cpv_group,
    tie_province_name(coalesce(
      m.province, nullif(nullif(split_part(sc.market_id, ':', 3), ''), 'PL'))) as province
) mk0
cross join lateral (
  select
    mk0.cpv_group,
    mk0.province,
    concat_ws(' · ',
      coalesce(tie_cpv_division(mk0.cpv_group), 'CPV ' || mk0.cpv_group),
      coalesce(mk0.province, 'cała Polska')) as label
) mk
left join v_signal_explanation ex on ex.signal_id = sc.signal_id
left join v_signal_masks ms on ms.signal_id = sc.signal_id
cross join lateral (
  select
    min(sl.lot_date) as period_from,
    max(sl.lot_date) as period_to,
    count(distinct sl.procedure_id) as n_procedures,
    sum(sl.value) as total_value
  from v_signal_lots sl
  where sl.signal_id = sc.signal_id
) agg
cross join lateral (
  select coalesce(
    jsonb_agg(jsonb_build_object('winner', t.key) order by t.lot_date, t.lot_id),
    '[]'::jsonb) as strip
  from (
    select sl.lot_date, sl.lot_id, sm.key
    from v_signal_lots sl
    left join v_signal_members sm
      on sm.signal_id = sl.signal_id and sm.bidder_id = sl.winner_bidder_id
    where sl.signal_id = sc.signal_id
    order by sl.lot_date desc, sl.lot_id desc
    limit 24
  ) t
) st
cross join lateral (
  select
    count(*) as n,
    coalesce(
      jsonb_agg(jsonb_build_object(
        'key', sm.key,
        'bidder_id', sm.bidder_id::text,
        'display_name', bp.display_name,
        'nip', bp.nip) order by sm.key),
      '[]'::jsonb) as members
  from v_signal_members sm
  join v_bidder_public bp on bp.bidder_id = sm.bidder_id
  where sm.signal_id = sc.signal_id
) mem
cross join lateral (
  select
    coalesce((
      select array_agg(k order by string_to_array(k, '.')::int[])
      from jsonb_each(
        case when jsonb_typeof(sc.oecd) = 'object' then sc.oecd else '{}'::jsonb end) e(k, v)
      where k ~ '^\d+(\.\d+)+$'
        and case jsonb_typeof(v)
          when 'number' then (v #>> '{}')::numeric >= 0.5
          when 'boolean' then v = 'true'::jsonb
          when 'null' then false
          else true
        end
    ), '{}'::text[]) as from_decision,
    coalesce((
      select array_agg(fi.k order by string_to_array(fi.k, '.')::int[])
      from (
        select distinct f ->> 'oecd' as k
        from jsonb_array_elements(
          case when jsonb_typeof(ex.findings) = 'array' then ex.findings else '[]'::jsonb end) f
        where f ->> 'oecd' ~ '^\d+(\.\d+)+$'
      ) fi
    ), '{}'::text[]) as from_findings
) oi
where sc.is_public
order by sc.probability desc nulls last, sc.strength desc nulls last, sc.signal_id;

-- api.signal_detail (one row per published signal)
create or replace view api.signal_detail as
select
  sc.signal_id,
  tie_mask_text(ex.summary, ms.names, ms.masks) as summary,
  coalesce(tie_mask_jsonb(ex.findings, ms.names, ms.masks), '[]'::jsonb) as findings,
  ev.evidence,
  coalesce(
    (select array_agg(tie_mask_text(a.t, ms.names, ms.masks) order by a.o)
     from unnest(ex.alternatives) with ordinality as a(t, o)),
    '{}'::text[]) as alternatives,
  g.graph
from v_signal_cluster sc
left join v_signal_explanation ex on ex.signal_id = sc.signal_id
left join v_signal_masks ms on ms.signal_id = sc.signal_id
cross join lateral (
  select coalesce(jsonb_agg(jsonb_build_object(
      'date', to_char(sl.lot_date, 'YYYY-MM-DD'),
      'buyer', bu.name,
      'subject', tie_mask_text(
        sl.title || case when sl.lot_no > 1 then ', część ' || sl.lot_no else '' end,
        ms.names, ms.masks),
      'winner_key', sm.key,
      'n_offers', coalesce(sl.n_offers, (select count(*) from bids b where b.lot_id = sl.lot_id)),
      'bzp_url', 'https://ezamowienia.gov.pl/mp-client/search/list/' || sl.procedure_id,
      'opening_url', (
        select 'https://ezamowienia.gov.pl/mp-readmodels/api/Tender/DownloadDocument/'
               || d.procedure_id || '/' || d.source_doc_id
        from documents d
        where d.procedure_id = sl.procedure_id and d.doc_type = 'opening'
          and d.status = 'parsed' and d.source = 'ezamowienia'
        order by d.id
        limit 1)
    ) order by sl.lot_date, sl.lot_id), '[]'::jsonb) as evidence
  from v_signal_lots sl
  left join buyers bu on bu.id = sl.buyer_id
  left join v_signal_members sm
    on sm.signal_id = sl.signal_id and sm.bidder_id = sl.winner_bidder_id
  where sl.signal_id = sc.signal_id
) ev
cross join lateral (
  with ml as (
    select distinct sm.key, b.lot_id
    from v_signal_members sm
    join bids b on b.bidder_id = sm.bidder_id
    join documents d on d.id = b.document_id and d.status = 'parsed'
    join v_signal_lots sl on sl.lot_id = b.lot_id and sl.signal_id = sm.signal_id
    where sm.signal_id = sc.signal_id
  ),
  nodes as (select key, count(*) as size from ml group by key),
  edges as (
    select x.key as a, y.key as b, count(*) as w
    from ml x join ml y on y.lot_id = x.lot_id and x.key < y.key
    group by x.key, y.key
  )
  select jsonb_build_object(
    'nodes', coalesce(
      (select jsonb_agg(jsonb_build_object('key', key, 'size', size) order by key) from nodes),
      '[]'::jsonb),
    'edges', coalesce(
      (select jsonb_agg(jsonb_build_object('a', a, 'b', b, 'w', w) order by a, b) from edges),
      '[]'::jsonb)
  ) as graph
) g
where sc.is_public;

-- api.bidders (one row per bidder with at least one bid)
create or replace view api.bidders as
select
  ab.bidder_id::text as bidder_id,
  ab.display_name,
  ab.nip,
  ab.is_jdg,
  ab.krs,
  ab.province,
  st.n_bids::int as n_bids,
  st.n_wins::int as n_wins,
  st.total_won,
  pt.partners,
  sg.signal_ids
from v_active_bidders ab
cross join lateral (
  select
    count(*) as n_bids,
    count(*) filter (where l.winner_bidder_id = ab.bidder_id) as n_wins,
    coalesce(sum(coalesce(l.contract_value, l.price_winner))
      filter (where l.winner_bidder_id = ab.bidder_id), 0) as total_won
  from (
    select b.lot_id
    from bids b join documents d on d.id = b.document_id and d.status = 'parsed'
    where b.bidder_id = ab.bidder_id
    union
    select l2.id from lots l2 where l2.winner_bidder_id = ab.bidder_id
  ) x
  join lots l on l.id = x.lot_id
) st
cross join lateral (
  select coalesce(jsonb_agg(jsonb_build_object(
      'bidder_id', p.bidder_id::text,
      'display_name', pp.display_name,
      'n_together', p.n) order by p.n desc, p.bidder_id), '[]'::jsonb) as partners
  from (
    select b2.bidder_id, count(distinct b2.lot_id) as n
    from bids b1
    join documents d1 on d1.id = b1.document_id and d1.status = 'parsed'
    join bids b2 on b2.lot_id = b1.lot_id and b2.bidder_id <> b1.bidder_id
    join documents d2 on d2.id = b2.document_id and d2.status = 'parsed'
    where b1.bidder_id = ab.bidder_id
    group by b2.bidder_id
    order by n desc, b2.bidder_id
    limit 10
  ) p
  join v_bidder_public pp on pp.bidder_id = p.bidder_id
) pt
cross join lateral (
  select coalesce(array_agg(sc.signal_id order by sc.signal_id), '{}'::text[]) as signal_ids
  from v_signal_cluster sc
  where sc.is_public and ab.bidder_id = any(sc.members)
) sg;

-- api.buyers (one row per contracting authority)
create or replace view api.buyers as
select
  b.id::text as buyer_id,
  b.name,
  tie_province_name(b.province) as province,
  st.n_procedures::int as n_procedures,
  st.single_offer_share,
  sg.signal_ids
from buyers b
cross join lateral (
  select
    count(distinct p.ocds_id) as n_procedures,
    round(
      count(l.id) filter (where l.n_offers = 1)::numeric
      / nullif(count(l.id) filter (where l.n_offers is not null), 0),
      4) as single_offer_share
  from procedures p
  left join lots l on l.procedure_id = p.ocds_id
  where p.buyer_id = b.id
) st
cross join lateral (
  select coalesce(array_agg(distinct sc.signal_id order by sc.signal_id), '{}'::text[])
    as signal_ids
  from procedures p
  join lots l on l.procedure_id = p.ocds_id
  join lot_market lm on lm.lot_id = l.id
  join v_signal_cluster sc on sc.market_id = lm.market_id and sc.is_public
  where p.buyer_id = b.id
    and (
      select count(distinct bb.bidder_id)
      from bids bb join documents dd on dd.id = bb.document_id and dd.status = 'parsed'
      where bb.lot_id = l.id and bb.bidder_id = any(sc.members)
    ) >= 2
) sg;

-- api.search (for the search box)
create or replace view api.search as
select
  'bidder'::text as kind,
  ab.bidder_id::text as id,
  ab.display_name as label,
  ab.nip,
  lower(translate(
    ab.display_name || ' ' || coalesce(ab.nip, ''),
    'ĄĆĘŁŃÓŚŹŻąćęłńóśźż', 'ACELNOSZZacelnoszz')) as search_text
from v_active_bidders ab
union all
select
  'buyer'::text,
  bu.id::text,
  bu.name,
  bu.nip,
  lower(translate(
    bu.name || ' ' || coalesce(bu.nip, ''),
    'ĄĆĘŁŃÓŚŹŻąćęłńóśźż', 'ACELNOSZZacelnoszz'))
from buyers bu;

-- api.oecd_catalog (51 rows, seeded by tie.engine.oecd.seed_catalog)
create or replace view api.oecd_catalog as
select item, section, title, status, how
from oecd_catalog
order by string_to_array(item, '.')::int[];

-- Read-only role for the frontend (no login here; the operator sets a password).
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'tie_reader') then
    create role tie_reader nologin;
  end if;
end
$$;

grant usage on schema api to tie_reader;
grant select on all tables in schema api to tie_reader;
alter default privileges in schema api grant select on tables to tie_reader;
