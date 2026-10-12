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

## api.validation (latest calibration run, page "Sprawdziliśmy się")
One row `kind = 'summary'` with the run's `metrics` and `params` (case columns null), then one
row `kind = 'case'` per gold case (order by `position`). Written by `tie validate`
(docs/VALIDATION.md). Firm names are already masked: legal entities only, other firms as
`NIP <nip>` or left out.

| column | type | meaning |
|---|---|---|
| calibration_id | int8 | id of the calibration run |
| run_at | timestamptz | when the run was made |
| kind | text | `summary` or `case` |
| position | int4 | 0 for the summary, then case order |
| source | text | `uokik` (decision of the President of UOKiK) or `kio` (KIO ruling) |
| case_key | text | decision number (`RLU-7/2023`) or signature (`KIO 2378/21`) |
| name | text | display name of the case |
| year | int4 | year of the decision or ruling |
| sector | text | sector (UOKiK only) |
| region | text | region (UOKiK only) |
| decision | text | `upheld` (collusion proven), `overturned` (KIO: not proven), `other` |
| detected | bool | did the engine flag it; null = could not be evaluated |
| status | text | `detected`, `missed` or `not_evaluable` |
| reason | text | why not evaluated or missed (e.g. `background_too_small`, `not_in_same_cluster`) |
| oecd_items | text[] | OECD items that fired |
| percentile | float8 | UOKiK: median percentile of the rigged tenders in their market |
| probability | float8 | engine probability of the cluster holding the colluders |
| firms | jsonb | array of public firm labels |
| detail | jsonb | `tenders`, `pairs`, `market`, `patterns`, `engine` (test details) |
| metrics | jsonb | summary row only: results of tests A, B, C, calibration, gate |
| params | jsonb | summary row only: proposed thresholds and gate parameters |
