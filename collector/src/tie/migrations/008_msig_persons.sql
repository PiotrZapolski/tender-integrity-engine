-- Board, supervisory board, proxy and shareholder links of bidders, parsed from MSiG
-- announcements of KRS entries (tie.identity.msig). Privacy: no name or PESEL of a person is
-- ever stored. A person is only `person_key` = HMAC-SHA256(PERSON_HMAC_SECRET, PESEL), plus
-- the birth year. Persons must never be exposed in public (api.*) views; only the fact that
-- two bidders share a person (shared_board_pairs) may be published.

alter table bidders add column msig_checked_at timestamptz;

create table board_links (
  bidder_id bigint not null references bidders(id) on delete cascade,
  -- Hex HMAC digest only: the check rejects anything that could be a raw name or PESEL.
  person_key text not null check (person_key ~ '^[0-9a-f]{64}$'),
  role text not null check (role in ('board', 'supervisory', 'proxy', 'shareholder')),
  birth_year int,
  valid_from date not null,
  -- Null while the person is still listed (as far as MSiG shows, see docs/msig.md).
  valid_to date,
  -- MSiG announcement id that created the row.
  source_id text,
  primary key (bidder_id, person_key, role, valid_from)
);
create index board_links_person on board_links (person_key);

-- MSiG announcements already processed per bidder (no personal data), so a re-check only
-- fetches new announcements.
create table msig_announcements (
  bidder_id bigint not null references bidders(id) on delete cascade,
  source_id text not null,
  published date not null,
  entry_date date,
  n_persons int not null default 0,
  processed_at timestamptz not null default now(),
  primary key (bidder_id, source_id)
);

-- Pairs of bidders (a < b) sharing at least one person (board, supervisory board, proxy or
-- shareholder, in any combination of roles).
-- n_shared counts shared persons at any time, n_concurrent those whose periods overlap.
create view shared_board_pairs as
select x.bidder_id as a,
       y.bidder_id as b,
       count(distinct x.person_key)::int as n_shared,
       (count(distinct x.person_key) filter (
          where daterange(x.valid_from, x.valid_to, '[]')
                && daterange(y.valid_from, y.valid_to, '[]')))::int as n_concurrent
from board_links x
join board_links y on y.person_key = x.person_key and x.bidder_id < y.bidder_id
group by x.bidder_id, y.bidder_id;
