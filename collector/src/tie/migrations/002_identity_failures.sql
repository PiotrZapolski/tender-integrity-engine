-- Number of identity runs in which matching this bid failed; after 3 the bid is excluded
-- from identity matching until an operator resets the counter (docs/OPERATIONS.md).
alter table bids add column identity_failures smallint not null default 0;
