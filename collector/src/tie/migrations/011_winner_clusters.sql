-- Winner-based clusters of the BZP-only screens (tie.engine.bzp_only): `cluster_id` starts
-- with `w:` and the cluster's lots are the lots of its market won by a member. `buyers`
-- restricts those lots to the given contracting authorities (a group found by buyer-level
-- winner rotation); null means the whole market (co-bidder clusters, shared-data groups).
alter table clusters add column if not exists buyers bigint[];
