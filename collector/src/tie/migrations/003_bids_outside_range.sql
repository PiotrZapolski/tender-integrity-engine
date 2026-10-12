-- True for offers accepted by the cross-check's rejected-offers rule whose price lies outside
-- the BZP min/max range (BZP counts only non-rejected offers there): likely rejected offers.
-- False for the other offers, null for bids stored before this column existed.
alter table bids add column outside_bzp_range boolean;
