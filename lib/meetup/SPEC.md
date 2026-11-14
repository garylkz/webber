---
id: meetup
type: module-design
status: draft
title: Meetup Scraper
parent: webber-architecture
---

# Meetup Scraper - Specification

Events from Meetup's unauthenticated frontend GraphQL endpoint. This document
describes what each function does.

Implemented in [meetup_scraper.py](meetup_scraper.py).

---

## Usage

The CLI:

```bash
cd lib/meetup
python meetup_scraper.py                                  # nearby events near KL
python meetup_scraper.py --query python --radius 10
python meetup_scraper.py --event-type ONLINE
python meetup_scraper.py --country sg --city "Singapore" --lat 1.3521 --lon 103.8198
python meetup_scraper.py --start-date 2026-08-20 --end-date 2026-09-20 --dump
```

`--dump` is how you get a whole country rather than one radius. For Malaysia it
sweeps 17 city anchors, dedupes by event id and drops anything whose venue is
outside the country, which is what stops a radius search from filling up with
Singapore. Other countries get a single anchor, the one you pass.

Event titles and venue names are set by whoever created the event. The default
mode prints the raw gql2 nodes as-is, and the human-facing error lines, which
carry Meetup's own messages, go through `sanitize` before they reach stderr.

Imported. Three filter fields are **required**, though every argument that builds
one marks them optional: `query` (`"*"` for all events), `lat` and `lon`. Meetup
rejects a filter missing any of the three, so a city name on its own is not
enough.

```python
import meetup_scraper as mu

filt = {"query": "*", "country": "my", "city": "Kuala Lumpur",
        "lat": 3.139, "lon": 101.6869, "radius": 25}
nodes, total = mu.search_events(filt, 50, 20)
mu.dump_country({"query": "*", "country": "my",
                 "lat": 3.139, "lon": 101.6869}, 500, 500, "my")
```

---

## Concepts

| Term | Meaning |
|---|---|
| **Anchor** | A city and its coordinates. A search radiates out from one; a country-wide sweep radiates out from all of them in turn. |
| **Filter** | The `EventSearchFilter` object Meetup's search takes: query text, city, country, coordinates, radius, event type, and a date range. Every field is optional. |
| **Node** | One event as the query below asks for it. |
| **RSVP count** | How many people have said they are going. It is the only measure of popularity the endpoint returns. |
| **Country check** | Discarding an event whose venue is not in the target country. Meetup's radius search ignores borders, so a search centred on Johor Bahru returns Singaporean events. |

---

## Modes

Two, and they are different jobs rather than two settings of one.

| Mode | What it does |
|---|---|
| default | Lists nearby upcoming events, one anchor, newest first as Meetup orders them. |
| `--dump` | Sweeps every anchor for a country, dedupes, and writes the whole result to `out/`. |

---

## Functions

| Function | Returns | Description |
|---|---|---|
| `search_events(filt, first, max_results)` | `(nodes, total_count)` | Paginate one anchor. `total_count` is Meetup's own figure, which is usually larger than what was fetched. |
| `dump_country(filt, radius, cap, country)` | `None` | Sweep every anchor, keep in-country events, dedupe, write, and print the JSON summary. |
| `in_country(venue, code)` | `bool` | Whether a venue is in a country. Falls back to a bounding box when Meetup omits the country. |
| `parse_date(value, end=False)` | `str` | Accept `YYYY-MM-DD` or a full ISO datetime, return an ISO string with an offset. |
| `sanitize(text)` | `str` | Strip escape sequences and control characters from untrusted text. |

### Required filter fields

**`query`, `lat` and `lon` must be present in every filter, even though every
argument that builds one marks them optional.** Meetup declares them non-null, so
a filter missing any of the three is rejected outright with a GraphQL error
naming the field:

| Missing | Rejected as |
|---|---|
| `query` | `Field 'query' has coerced Null value for NonNull type 'String!'` |
| `lat` or `lon` | `Field 'lat' has coerced Null value for NonNull type 'Float!'` |

`query` is `"*"` for every event, which is what the CLI defaults to. `city` alone
is not a location: the coordinates are what the search actually radiates from,
and the city name only ranks the results within it.

---

## The query

One GraphQL document, posted to `https://www.meetup.com/gql2` as
`{query, variables}`. It asks for `eventSearch` and selects exactly the fields
listed above - nothing more, because every field is one Meetup has to join for
and the endpoint is unauthenticated.

There is no login and no API key. That is the entire basis of this tool: the
`gql2` endpoint is Meetup's own frontend interface, reachable without an account
because the website uses it before anyone signs in.

**Pagination is by cursor.** Each page carries a `pageInfo` with an
`endCursor` and a `hasNextPage`, and the next page is fetched with that cursor
passed as `after`. There is no offset and no page number. A short delay sits
between pages, not before the first one, since the first page has no cursor to
wait on.

---

## Searching

The default mode filters by a query string, a city, and a radius in miles around
coordinates. Defaults are Kuala Lumpur (`my`, 3.139, 101.6869) and 25 miles, which
covers the city and the close parts of Selangor.

Other filters: `--state`, `--zip`, `--event-type` (`PHYSICAL`, `ONLINE`,
`HYBRID`), `--happening-now`, and `--start-date` / `--end-date`.

`--start-date` and `--end-date` take either a bare date or a full ISO datetime.
**A bare date means local midnight, or end-of-day when it is the upper bound**,
so `--start-date X --end-date X` means that one whole day rather than an empty
range.

---

## The country sweep

`--dump` is how you get a whole country rather than one radius, because a radius
search centred anywhere only returns what that radius reaches.

- **Malaysia radiates from 17 anchors**, one per city with a real meetup scene,
  each with its own coordinates and its own `city` in the filter. Set `--city` on
  each request so Meetup ranks the local events first.
- **Any other country radiates from one anchor**, the one given on the command
  line. There is no built-in list for anywhere else.
- **Each anchor paginates** until the cap or the end.
- **Events are deduped by id.** The anchors overlap - an event in Johor Bahru is
  within range of more than one of them - and the id is the identity. Without
  this the sweep returns the same evening's meetup seventeen times.
- **Every event is then checked against the country**, and the ones outside it
  dropped. This is what stops a Malaysian sweep from filling with Singaporean
  events, since radius search does not respect a border.

### The country check

1. If the venue names a country, that decides it, case-insensitively.
2. If it does not and the target is Malaysia, the venue's coordinates are checked
   against two bounding boxes: the peninsula, and Borneo. The gap between them is
   the South China Sea, and nothing in Malaysia falls in it.
3. If it does not and the target is anything else, the event is dropped. There is
   no bounding box for a country this tool has not been given anchors for.

So the check is a filter on top of the sweep, not part of the radius search. Meetup
will happily return an event in another country from a Malaysian anchor, and only
this discards it.

---

## Output

`--dump` writes every kept event to `out/mtp_events_{country}_{timestamp}.json`,
where the timestamp is `YYYYMMDD_HHMMSS`. The file name keeps `mtp`, the prefix of
the two scripts this one was merged from. `out/` is git-ignored.

The default mode prints one JSON object to the terminal instead:

```json
{
  "location": {"city": "Kuala Lumpur", "lat": 3.139, "lon": 101.6869, "radius": 25},
  "query": "*",
  "total": 5,
  "events": [
    {
      "id": "...",
      "title": "Event Title",
      "dateTime": "2026-10-18T19:00:00+08:00",
      "eventType": "PHYSICAL",
      "group": {"name": "Group Name", "urlname": "group-urlname"},
      "rsvps": {"totalCount": 2},
      "venue": {"name": "Venue Name", "city": "Petaling Jaya", "country": "my"},
      "eventUrl": "...",
      "status": "...",
      "isOnline": false
    }
  ]
}
```

`total` is **how many were fetched, not how many matched**, and `events` carries
the raw gql2 nodes untouched. Meetup's own `totalCount` is available from the
same call and is usually larger; the sweep prints it summed in `total` of its
summary instead.

The `--dump` mode writes the same out/ file it always did and prints a JSON
summary to stdout rather than the old per-anchor lines:

```json
{"total": 0, "new": 0, "file": "out/mtp_events_my_20261018_190000.json"}
```

`total` is the sum of Meetup's own `totalCount` across the anchors, `new` is the
number of unique in-country events kept, and `file` is where they were written.

---

## Safety

`sanitize` strips ANSI escape sequences and control characters from untrusted
text before it is printed. The default mode puts the raw gql2 nodes into its JSON
output as-is, and the error lines that carry Meetup's own messages through to
stderr go through `sanitize` first.

This is not cosmetic. Titles and error messages come from other people, and they
reach a terminal stderr. An escape sequence in one would otherwise be executed by
the terminal - clearing the screen, repositioning the cursor, or setting a colour
that corrupts everything printed after it. Anything untrusted that reaches a
terminal goes through this first.

---

## Errors

| Condition | Behaviour |
|---|---|
| Unknown option | Rejected by the argument parser. |
| `--event-type` outside the three choices | Rejected. |
| Non-numeric value for a numeric option | Rejected, naming what was expected. |
| HTTP error from the endpoint | Reported with the status code. |
| Non-JSON response | Reported as such, rather than parsed optimistically. |
| The response carries GraphQL `errors` | Reported with Meetup's own message, truncated. |
| No `eventSearch` in the response | Reported rather than treated as empty. |
| A network error or timeout | Reported with the reason. |

**A sweep is not all-or-nothing.** A failure part-way through stops that run, but
the events already collected have been written out by the time it does, so a long
sweep interrupted at the sixteenth anchor keeps the first fifteen.

---

## Notes

**This scraper is not in the shared contract** that the two cinema scrapers hold
to. It has no counterpart on either of them and does not need one: it reads one
site, and it returns its own event shape rather than the `Movie`/`Cinema`/
`Showtime` records that contract pins.

The one thing to know when comparing runs is that **a sweep is not deterministic
across runs**: Meetup re-ranks events between calls, so two sweeps minutes apart
can differ by a couple of events. A difference between a run and an earlier run
of itself is not worth chasing.