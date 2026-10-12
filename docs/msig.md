# MSiG board links

Module `collector/src/tie/identity/msig.py`, job kind `msig_board`, migration
`008_msig_persons.sql`. This file records how the MSiG API behaves (checked with curl on
2026-10-12, 1 request per 2 s, our User-Agent) and what the data can and cannot tell.

**Coverage note:** MSiG coverage of KRS entries ends around 2025-11: the KRS act change
(Dz. U. 2025 poz. 1556, in force 2025-11-29) abolished their announcement in MSiG (details in
"Completeness" below). Links reflect board, owners and proxies up to about November 2025, so
the re-check period defaults to 180 days (`MSIG_RECHECK_DAYS`), not 30.

## Why MSiG

The official KRS API masks names and PESEL of board members. MSiG (Monitor Sądowy i
Gospodarczy) announcements of KRS entries (type B, chapter "XV. WPISY DO KRAJOWEGO REJESTRU
SĄDOWEGO") quote the entry text unmasked, including PESEL. We use it only to find bidders
that share a person; no name or PESEL is stored.

## API

Base `https://wyszukiwarka-msig.ms.gov.pl/api` (found via `GET /home/getapiurl`). No key, no
login, no bot protection.

### `GET /Monitor/Search`

Params: `krs` (10 digits), `signatureType=B` (KRS entries), `from`, `to` (`YYYY-MM-DD`, both
required; without `signatureType` the API answers 444 "Typ syng./sprawy - Wartość domyślna:
A i B"), `page` (from 1).

```json
{"countPages": 100, "page": 1, "list": [
  {"id": 2300354, "sequenceNumber": 1, "monitorNumber": "232/2020",
   "dateOfPublication": "2020-11-27T00:00:00",
   "entityName": "MULTISERVICE SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ.",
   "signatureOfCase": null, "signatureKRS": "[OL.VIII NS-REJ.KRS/11735/20/755]"}]}
```

- `pageSize` is ignored: a page always has up to 50 items. `countPages` is always 100 and
  means nothing; pages past 100 work.
- Consecutive pages overlap by one item (page 2 starts with the last item of page 1). The
  module deduplicates ids and stops at a short page or a page with no new id.
- Sorted by publication date, ascending.
- `textInBody` together with `krs` gives HTTP 400, so we cannot pre-filter announcements
  that mention board members; every new announcement of a bidder is fetched once.

### `GET /Monitor/SearchCount`

Same params; returns a bare integer: the number of 50-item Search pages (not the number of
announcements). For KRS 0000047217 it returns 2 for 63 announcements.

### `GET /Monitor/Detalis?Id=<id>` (sic)

```json
{"entityName": "...", "numberOfNotice": "774914", "krs": "0000047217", "nip": null,
 "page": 505, "chapterName": "XV. WPISY DO KRAJOWEGO REJESTRU SĄDOWEGO/2. Wpisy kolejne",
 "monitorNumber": "142/2025", "dateOfPublication": "2025-07-24T00:00:00",
 "textInPosition": "Poz. 774914. MULTISERVICE ... KRS 0000047217 ...",
 "textInBody": "W dniu 30.06.2025 dokonano wpisu do rejestru KRS nr 70 następującej treści: ...",
 "id": 18606798, "idNext": 0, "idPrevious": 0}
```

`textInBody` is OCR-like text of the printed issue: non-breaking spaces (` `), spaces
inside words and dates ("23.1 1.2020", "17 .07 .2026", "ZIELIŃS KI"), page headers inserted
mid-text, older entries (2002 to 2005) with a different field numbering.

### `GET /Monitor/Download?id=<id>`

Returns the whole MSiG issue as a PDF (for example 108 pages, 2.5 MB). Not used: Detalis has
the text.

## Where board members appear

Inside `textInBody`, by KRS section:

| Section | Meaning | Role stored |
|---|---|---|
| `Dz. 2. Rub. 1.` | Organ uprawniony do reprezentacji (ZARZĄD, or partners of sp.j./sp.k.) | `board` |
| `Dz. 2. Rub. 2.` | Organ nadzoru (RADA NADZORCZA) | `supervisory` |
| `Dz. 2. Rub. 3.` | Prokurenci | `proxy` |
| `Dz. 1. Rub. 7.` | Dane wspólników (shareholders, partners) | `shareholder` |

A bare `Rub. N` keeps the last `Dz.`; `PRub.` is a subsection, not a section. Persons are
listed as `<no> 1. NAZWISKO 2. IMIONA 3. PESEL 5. FUNKCJA 6. ...` (before about 2005:
`4. FUNKCJA 5. ...`, sometimes with the `4.` lost). Operations: `wpisać:` (add),
`wykreślić:` (remove; also removing the whole organ followed by its members), `zmienić:` or
`(dla pozycji: 1. X 2. Y 3. PESEL)` followed by field changes (the person stays, e.g. a new
share count). Shareholders that are companies (no given names, no PESEL) are skipped. A
section we ignore although it holds PESEL: `Dz. 5 Rub. 1` (curator).

The entry date ("W dniu DD.MM.YYYY dokonano wpisu", falls back to the publication date) is
the `valid_from` / `valid_to`; the entry number ("rejestru KRS nr N") orders entries of one
day.

## Stored data and privacy

- `board_links(bidder_id, person_key, role, birth_year, valid_from, valid_to, source_id)`.
- `person_key` = HMAC-SHA256(`PERSON_HMAC_SECRET`, PESEL). PESEL is unique and stable;
  names are not (surname changes, "MARTA MARIA" vs "MARTA" for the same PESEL, OCR breaks
  words), so the name is used only to recognise an entry while parsing and is never stored.
  The same person in several roles or companies has one key. A check constraint allows only
  64 hex chars.
- PESEL is validated (checksum and date) before use. Entries without a valid PESEL (missing,
  e.g. foreigners, or failing the checks) are skipped and counted in the job log.
- A removal without an earlier addition (the first entry predates MSiG coverage) becomes a
  one-day period `[entry date, entry date]`.
- `msig_announcements` lists processed announcement ids per bidder (no personal data), so a
  re-check (every 180 days by default, `MSIG_RECHECK_DAYS`, queued by `tie daily`) fetches
  only new announcements and searches from the newest processed publication date.
- `shared_board_pairs(a, b, n_shared, n_concurrent)`: pairs of bidders with `a < b`, persons
  shared at any time and persons whose periods overlap, in any roles (board, supervisory,
  proxy, shareholder). No person appears in it. Persons must
  never be exposed in `api.*` views; only this pair-level fact may be.
- Nothing raw goes into logs, exceptions, `jobs.last_error` or the raw file store.

## Completeness: MSiG stopped carrying KRS entries in late 2025

`SearchCount` for `signatureType=B` (50-item pages):

| Period | Pages | About announcements |
|---|---|---|
| 2024 (whole year) | 30 035 | 1.5 million |
| 2025 (whole year) | 30 440 | 1.5 million |
| 2025-10 | 1 203 | 60 000 |
| 2025-11 | 1 622 | 81 000 |
| 2025-12 | 357 | 18 000 |
| 2026-01 | 71 | 3 500 |
| 2026-02 | 32 | 1 600 |
| 2026-03 | 15 | 750 |
| 2026-06 | 6 | 300 |
| 2026-09 | 4 | 200 |
| 2026-01-01 to 2026-10-12 | 161 | 8 000 |

`SearchCount` and `Search` agree in both years: for 2024-07-25 the count is 299, page 299 has
44 items and page 300 is empty; for 2026-09 the count is 4, page 4 has 32 items and page 5
is empty. The earlier "inconsistency" (30 440 for 2025 against 5 for part of August 2026)
was real data, not a counter bug: position numbers confirm it ("Poz. 774914" in July 2025,
"Poz. 7870" in September 2026).

Cause: the act of 26 September 2025 amending the KRS act (Dz. U. 2025 poz. 1556), in force
since 29 November 2025, abolished the duty to announce KRS entries in MSiG. A trickle of
2026 entries still appears (a sample from September 2026: a financial statement note, a
deletion from the register and a court curator, all entered in July to September 2026), but
at about 0.5 percent of the earlier volume.

Consequences:

- MSiG is a historical source: board composition is reliable up to about November 2025.
  Board changes after that date do not appear, so open periods (`valid_to is null`) may be
  stale and new members of 2026 are missing.
- Companies registered after late 2025 have no MSiG board data at all.
- A fresher source of names would be the KRS itself (full extracts are behind bot
  protection, and the KRS API masks persons), so for now treat `shared_board_pairs` as
  "shared members until late 2025".
