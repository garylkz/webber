---
id: fourd
type: module-design
status: draft
title: 4D Results Scraper
parent: webber-architecture
---

# 4D Results Scraper: Specification

Malaysian 4D draw results, read from the operators' own sites. This document
describes what each function does.

Implemented in [fourd_results.py](fourd_results.py).

---

## Usage

The CLI:

```bash
cd lib/fourd
python fourd_results.py                       # latest draw, every operator
python fourd_results.py 2026-10-25            # one draw date
python fourd_results.py --recent 10           # the last 10 draw dates
python fourd_results.py --operator magnum     # one operator only
python fourd_results.py --from 2024-01-01 --to 2024-12-31   # any past range
python fourd_results.py --from 2026-10-01     # from a date up to today
python fourd_results.py --all --out out/fourd_all.json   # every draw, to a file
```

A date, `--recent`, `--from` and `--all` are alternatives; give at most one.
`--to` needs `--from`. `--operator` and `--out` combine with any of them.

`--out FILE` writes exactly what would have gone to stdout, creating the folder
if needed, and prints a JSON summary on stdout instead:
`{"draws": N, "file": "FILE"}`. (`out/` is
already in webber's `.gitignore`.)

Imported:

```python
import fourd_results as fourd

draws = fourd.draws_on("2026-10-25")
history = fourd.recent(20)
year = fourd.between("2024-01-01", "2024-12-31")
fourd.save_json(fourd.fetch_all(), "out/fourd_all.json")
```

---

## Coverage

| Operator | Source | History from |
|---|---|---|
| Magnum 4D | `www.magnum4d.my` JSON endpoints | 25 Apr 1985 (draw `001/85`) |
| Da Ma Cai 1+3D | `www.damacai.com.my` result link, then a signed Azure blob | 1 Jan 2005 (draw `2382/05`) |
| Sports Toto | not covered | |

**Sports Toto is left out on purpose.** `www.sportstoto.com.my` answers any
non-browser client with an Akamai "Access Denied" page. Getting past that would
mean defeating bot detection, which this library does not do.

Neither covered source takes an API key or a login.

---

## Concepts

| Term | Meaning |
|---|---|
| **Draw** | One operator's result on one date: 1st, 2nd and 3rd prize, 10 special numbers and 10 consolation numbers. |
| **Draw number** | The operator's own id, `NNN/YY` for Magnum and `NNNN/YY` for Da Ma Cai. The two counters are unrelated. |
| **Draw day** | Regular draws are on Wednesday, Saturday and Sunday. Special draws fall on other days, usually a Tuesday. Most are shared, but not all: since 2005, 75 Magnum draw dates (62 of them Tuesdays, 14 in early 2021) have no Da Ma Cai draw. |
| **Official** | The operator marks the result final. Magnum sends `Official: true`; Da Ma Cai sends `status` of `COMPLETED` (or `C` on older draws). |

---

## Record

`to_dict()` emits these snake_case keys:

| Key | Type | Notes |
|---|---|---|
| `operator` | `str` | `magnum` or `damacai` |
| `draw_no` | `str` | As the operator prints it |
| `draw_date` | `str` | `YYYY-MM-DD` |
| `draw_day` | `str` | `MON` to `SUN`, computed from the date |
| `first`, `second`, `third` | `str` | Four digits, leading zeros kept |
| `special` | `list[str]` | Usually 10 |
| `consolation` | `list[str]` | Usually 10 |
| `official` | `bool` | See above |

Numbers stay strings: `0831` is a different number from `831`. Blank slots
(empty, or the `----` an operator uses for a slot it did not fill) are dropped
from `special` and `consolation`, so a list can be shorter than 10.

The CLI prints `{"draws": [...]}`, newest date first, then Magnum before Da Ma
Cai on the same date.

---

## Functions

| Function | Returns | Description |
|---|---|---|
| `draws_on(day, operators=OPERATORS)` | `list[Draw]` | Every chosen operator's draw on exactly that date. Empty on a non-draw day. |
| `latest(operators=OPERATORS)` | `list[Draw]` | Each operator's most recent published draw. |
| `recent(count, operators=OPERATORS, progress=None)` | `list[Draw]` | The last `count` draw dates, every chosen operator. |
| `between(start, end, operators=OPERATORS, progress=None)` | `list[Draw]` | Every draw from `start` to `end`, both inclusive. |
| `magnum_between(start, end)` | `list[Draw]` | Every Magnum draw in the range, in one request. |
| `fetch_all(operators=OPERATORS, progress=None)` | `list[Draw]` | Every draw each operator still publishes: `between` from Magnum's first draw to today. |
| `to_json(draws)` | `str` | Pure. The CLI's output: `{"draws": [...]}`, newest first, indent 2. |
| `save_json(draws, path)` | `Path` | Writes `to_json` plus a newline to a file, creating its folder. |
| `magnum_draw(day)` | `Draw` or `None` | One Magnum draw. |
| `magnum_recent(count, on_or_before=None)` | `list[Draw]` | The last `count` Magnum draws on or before a date. |
| `magnum_latest()` | `Draw` or `None` | Magnum's newest draw. |
| `damacai_draw(day)` | `Draw` or `None` | One Da Ma Cai draw. |
| `damacai_latest(today=None)` | `Draw` or `None` | Walks back up to 7 days for Da Ma Cai's newest draw. |
| `parse_magnum(body)` | `list[Draw]` | Pure. A Magnum results body, newest first. |
| `parse_damacai(body)` | `Draw` or `None` | Pure. A Da Ma Cai draw blob. |
| `parse_damacai_link(body)` | `str` | Pure. The signed blob URL out of `/callpassresult`. |
| `normalise_date(value)` | `str` | Pure. `YYYY-MM-DD`, `YYYYMMDD` or `DD/MM/YYYY` to `YYYY-MM-DD`. |
| `sort_draws(draws)` | `list[Draw]` | Pure. Newest date first, then operator order. |

---

## Endpoints

### Magnum

| Call | Reply |
|---|---|
| `GET /results/latest` | A one-element array: the newest draw. |
| `GET /results/past/between-dates/null/{YYYY-MM-DD}/{n}` | The last `n` draws on or before the date, newest first. |
| `GET /results/past/between-dates/{from}/{to}/{n}` | Up to `n` draws in the range, both ends inclusive, newest first. `magnum_between` passes the number of days in the range, which can never be fewer than the draws in it. |
| `GET /draw-dates/available-for-year` | Upcoming draw dates. Not used. |

The date must be ISO. Any other shape gets `[]` with a 200, not an error.
`magnum_draw` asks for one draw on or before the date and keeps it only if the
date matches, which is how a non-draw day comes back empty.

### Da Ma Cai

Two requests per draw:

1. `GET /callpassresult?pastdate={YYYYMMDD}` with header `cookiesession: 421`
   returns `{"link": "https://prddmcremt1.blob.core.windows.net/..."}`. Without
   the header the site answers "Page Not Found" with a 200. The site's own page
   sends that fixed value on every call.
2. `GET` the link. It is a read-only signed URL valid for about an hour. A
   date with no draw is a `404 BlobNotFound`, which becomes `None`.

The link endpoint answers for any date, so it says nothing about whether a draw
happened; only the blob does. The blob may start with a UTF-8 BOM.

Da Ma Cai's own fields map as: `p1`/`p2`/`p3` to the prizes, `starterList` to
`special`, `consolidateList` to `consolation`. The blob also carries 3D, 3+3D
and jackpot data, which this scraper ignores.

---

## History

Any past date or range back to each operator's first draw can be fetched:
Magnum from 25 Apr 1985, Da Ma Cai from 1 Jan 2005.

`recent(count)` and `between(start, end)` take their calendar from Magnum,
because only Magnum lists past draws. The operators draw on the same dates, so
Da Ma Cai is then asked for each of them. Consequences:

- `--operator damacai` with `--recent` or `--from` still makes one Magnum
  request, for the calendar.
- Magnum returns any count or range in one request.
- Da Ma Cai costs two requests per date. They run 4 dates at a time, so a year
  (about 160 draws) is around 320 requests.
- Before 1 Jan 2005 Da Ma Cai has no data, so dates before that are skipped
  without a request and a range reaching back further holds Magnum alone there.

`recent`, `between` and `fetch_all` take an optional `progress(done, total)`
callback, called after each Da Ma Cai date. The CLI uses it to print
`damacai: N/M dates` on stderr every 50 dates.

### Bulk

`--all` (or `fetch_all()`) is the whole archive. Measured in October 2026:

| Part | Size | Time |
|---|---|---|
| Magnum, 1985 to now | 6,834 draws, one request (about 9 MB) | about 2 s |
| Da Ma Cai, 2005 to now | 3,690 dates asked, 3,615 draws found, two requests each | about 7 min |

Pretty-printed, a draw is about 600 bytes: Magnum alone is about 4 MB, both
operators together (10,449 draws) about 6.2 MB.

Da Ma Cai is only asked about Magnum's dates, so a Da Ma Cai draw on a day
Magnum did not draw would be missed. None has been seen, but the scraper cannot
rule it out.

---

## Errors

| Condition | Behaviour |
|---|---|
| Date not in a known shape | Refused, naming the value. |
| More than one of a date, `--recent`, `--from` | Refused. |
| `--to` without `--from` | Refused. |
| `--from` after `--to` | Refused. |
| `--recent` below 1 | Refused. |
| Unknown `--operator` | Refused, listing the choices. |
| No draw on the date | `{"draws": []}`, and `no draw on {date}` on stderr. |
| A request times out | Retried, up to 3 attempts in all, then reported. |
| Any other HTTP error or network failure | Reported with the status or reason. |

---

## Notes

**Not in the shared contract** that the two cinema scrapers hold to. Its parsers
are checked by `lib/fourd/test_fourd.py` over saved fixtures, and that suite
pins the record's key set.
