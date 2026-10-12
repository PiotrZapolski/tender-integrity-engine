-- Fixes to the api.* helper views of 006 (006 itself stays untouched, it may already be
-- applied). Both views are replaced in place with the same columns, so every api.* view built
-- on them picks the new definition up without being recreated.

-- Privacy: a bidder is masked when `is_jdg` is true, or when it has no KRS number and its
-- status is not confirmed by the whitelist check yet (`is_jdg` unknown, or `wl_checked_at`
-- null). The name heuristic at creation (`looks_like_company`) sets `is_jdg = false` for a
-- sole trader whose name contains a company word ("Usługi Remontowe Jan Kowalski"); such a
-- bidder stays masked until the whitelist confirms it is in KRS.
create or replace view v_bidder_public as
select
  b.id as bidder_id,
  b.nip,
  b.is_jdg,
  (b.is_jdg is true
    or (b.krs is null and (b.is_jdg is null or b.wl_checked_at is null))) as masked,
  case
    when b.is_jdg is true
      or (b.krs is null and (b.is_jdg is null or b.wl_checked_at is null))
      then coalesce('NIP ' || b.nip, 'Wykonawca bez NIP')
    else b.name
  end as display_name,
  case
    when b.is_jdg is true
      or (b.krs is null and (b.is_jdg is null or b.wl_checked_at is null))
      then null
    else b.krs
  end as krs,
  tie_province_name(b.province) as province
from bidders b;

-- `is_public`: published and refreshed by the latest finished engine run or by a later one.
-- A run in progress rewrites `signals.run_id` cluster by cluster; with `>=` the signals it has
-- already refreshed stay visible next to those it has not reached yet, and stale signals (of
-- clusters the latest finished run no longer found) drop out.
create or replace view v_signal_cluster as
select
  s.signal_id,
  s.status,
  (s.status = 'published' and s.run_id >= (
    select max(r.run_id) from engine_runs r where r.status = 'done')) is true as is_public,
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
