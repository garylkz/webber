# Meetup Scraper — Specification

Events from Meetup's unauthenticated frontend GraphQL endpoint. This document
describes what each function does. Nothing here is specific to a language or
runtime.

Available in [Python](../python/mu_scrapper.py) and
[TypeScript](../typescript/mu_scrapper.ts), with the same behaviour.

> The filename is misspelled. It says `scrapper` rather than `scraper`, and that
> is the name both implementations have always carried, so the spec is named to
> match rather than to be correct. Fixing it would break every command that
> invokes the tool.

---

## Usage

The CLI is the same in both languages. Substitute the runner and the extension:

```bash
cd python        # or: cd typescript
python mu_scrapper.py                                  # nearby events near KL
python mu_scrapper.py --query python --radius 10
python mu_scrapper.py --event-type ONLINE
python mu_scrapper.py --country sg --city "Singapore" --lat 1.3521 --lon 103.8198
python mu_scrapper.py --start-date 2026-08-20 --end-date 2026-09-20 --dump
```

`--dump` is how you get a whole country rather than one radius. For Malaysia it
sweeps 17 city anchors, dedupes by event id and drops anything whose venue is
outside the country, which is what stops a radius search from filling up with
Singapore. Other countries get a single anchor, the one you pass.

Event titles and venue names are set by whoever created the event and go straight
to your terminal, so they are stripped of escape sequences before printing.

Imported. Three filter fields are **required**, though every argument that builds
one marks them optional: `query` (`"*"` for all events), `lat` and `lon`. Meetup
rejects a filter missing any of the three, so a city name on its own is not
enough.

```python
import mu_scrapper as mu

filt = {"query": "*", "country": "my", "city": "Kuala Lumpur",
        "lat": 3.139, "lon": 101.6869, "radius": 25}
nodes, total = mu.search_events(filt, 50, 20)
events = mu.dump_country({"query": "*", "country": "my",
                          "lat": 3.139, "lon": 101.6869}, 500, 500, "my")
```

```ts
import { searchEvents, dumpCountry } from './mu_scrapper.ts';

const filter = { query: '*', country: 'my', city: 'Kuala Lumpur', lat: 3.139,
                 lon: 101.6869, radius: 25 };
const { nodes, totalCount } = await searchEvents(filter, 50, 20);
const events = await dumpCountry(
  { query: '*', country: 'my', lat: 3.139, lon: 101.6869 }, 500, 500, 'my');
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
| `searchEvents(filter, first, maxResults)` | `{nodes, totalCount}` | Paginate one anchor. `totalCount` is Meetup's own figure, which is usually larger than what was fetched. |
| `dumpCountry(filter, radius, cap, country)` | the events kept | Sweep every anchor, keep in-country events, dedupe, write. |
| `inCountry(venue, code)` | `bool` | Whether a venue is in a country. Falls back to a bounding box when Meetup omits the country. |
| `parseDate(value, end?)` | `str` | Accept `YYYY-MM-DD` or a full ISO datetime, return an ISO string with an offset. |
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
listed above — nothing more, because every field is one Meetup has to join for
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
- **Events are deduped by id.** The anchors overlap — an event in Johor Bahru is
  within range of more than one of them — and the id is the identity. Without
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

The default mode prints to the terminal instead:

```
location: Kuala Lumpur (3.139, 101.6869), radius 25 mi, query: <all>
total matching: 5 (fetched)

- Event Title
    2026-10-18T19:00:00+08:00  [PHYSICAL]  Group Name (group-urlname)  rsvps:2
    Venue Name (Petaling Jaya)
```

The count printed is **how many were fetched, not how many matched**. Meetup's own
`totalCount` is available from the same call and is usually larger; the sweep
prints it per anchor instead, since a single total would not add up across
seventeen searches that overlap.

---

## Safety

`sanitize` strips ANSI escape sequences and control characters from event titles,
venue names and group names before they are printed.

This is not cosmetic. Event titles and venue names are set by whoever created the
event, and they go straight to a terminal. An escape sequence in one would
otherwise be executed by the terminal — clearing the screen, repositioning the
cursor, or setting a colour that corrupts everything printed after it. Anything
untrusted that reaches a terminal goes through this first.

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

The two implementations agree on the printed output and on the dedupe rule. The
one thing to know when comparing runs is that **a sweep is not deterministic
across runs**: Meetup re-ranks events between calls, so two sweeps minutes apart
can differ by a couple of events. The Python implementation compared against
itself differs by the same amount, so a difference between the two is worth
looking at, and a difference between either and an earlier run of itself is not.