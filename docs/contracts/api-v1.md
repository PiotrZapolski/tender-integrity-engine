# TIE read API contract v1 (for the zmowy.pl frontend)

The engine (part 2) publishes read-only SQL views in schema `api` of the TIE database.
The frontend connects with a read-only role (`tie_reader`) and never touches other schemas.
All money values are numeric PLN, all dates ISO `YYYY-MM-DD`, ids are text.
Bidders that are sole traders (`is_jdg = true`) are exposed only by NIP: `display_name` is
`NIP <nip>` and `name` is null.

## api.stats (1 row)
| column | type | meaning |
|---|---|---|
| procedures | int | procedures in the database |
| lots | int | lots |
| bids | int | named offers read from opening documents |
| bidders | int | distinct bidders |
| signals_published | int | signals above the publication threshold |
| parse_ok_share | numeric | share of opening documents that passed the BZP cross-check |
| oecd_checked | int | OECD checklist items checked automatically (A) |
| oecd_partial | int | OECD items checked partially (C) |
| updated_at | timestamptz | last engine run |

## api.signals (one row per published signal, ranked)
| column | type | meaning |
|---|---|---|
| signal_id | text | stable id (slug-safe) |
| title | text | plain-language headline (Polish) |
| market_label | text | e.g. "Remonty szkół · powiat kaliski" |
| cpv_group | text | CPV group code |
| province | text | voivodeship name |
| period_from, period_to | date | time span of evidence |
| probability | numeric | calibrated probability 0..1 (Jev) |
| strength | int | 1..5 |
| oecd_items | text[] | e.g. {"3.1.6","3.3.6"} |
| n_procedures | int | procedures in evidence |
| total_value | numeric | sum of contract values in evidence |
| facts | jsonb | 3 items `[{"value":"27","label":"przetargów razem"}]` |
| strip | jsonb | rotation strip: up to 24 items `{"winner":"A"}` in time order |
| members | jsonb | `[{"key":"A","bidder_id":"...","display_name":"...","nip":"..."}]` |

## api.signal_detail (one row per signal)
| column | type | meaning |
|---|---|---|
| signal_id | text | |
| summary | text | 2-4 sentence plain-language summary |
| findings | jsonb | `[{"oecd":"3.1.6","title":"...","text":"...","probability":0.94,"viz":"rotation"|"ladder"|"map"|"graph","data":{...}}]` |
| evidence | jsonb | `[{"date":"2023-05-16","buyer":"...","subject":"...","winner_key":"B","n_offers":3,"bzp_url":"...","opening_url":"..."}]` |
| alternatives | text[] | other possible explanations |
| graph | jsonb | `{"nodes":[{"key":"A","size":27}],"edges":[{"a":"A","b":"B","w":27}]}` |

`viz` data shapes: rotation `{"rows":[{"key":"A","cells":["win","lose",null,...]}]}`;
ladder `{"rows":[{"label":"03.2021","marks":[{"key":"A","pct":100.0}]}]}` (pct of winning price);
map `{"areas":[{"teryt":"3007","key":"A"}]}`; graph as `graph`.

## api.bidders (one row per bidder with at least one bid)
| column | type | meaning |
|---|---|---|
| bidder_id | text | |
| display_name | text | company name or `NIP <nip>` for JDG |
| nip | text | |
| is_jdg | bool | |
| krs | text | null for JDG |
| province | text | |
| n_bids, n_wins | int | |
| total_won | numeric | |
| partners | jsonb | top co-bidders `[{"bidder_id","display_name","n_together"}]` |
| signal_ids | text[] | |

## api.buyers (one row per contracting authority)
| column | type | meaning |
|---|---|---|
| buyer_id | text | |
| name | text | |
| province | text | |
| n_procedures | int | |
| single_offer_share | numeric | share of lots with exactly one offer |
| signal_ids | text[] | |

## api.search (for the search box)
| column | type | meaning |
|---|---|---|
| kind | text | `bidder` or `buyer` |
| id | text | |
| label | text | display name |
| nip | text | |
| search_text | text | lowercased, ascii-folded name + NIP (use `ilike`) |

## api.oecd_catalog (51 rows)
| column | type | meaning |
|---|---|---|
| item | text | e.g. "3.3.6" |
| section | text | "3.1".."3.5" |
| title | text | Polish text of the OECD item |
| status | text | `A`, `C` or `N` |
| how | text | how we check it, or why we cannot |
