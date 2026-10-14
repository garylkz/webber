# GSC Scraper — Specification

Movies, showtimes, ticket prices and seat maps for GSC Cinemas Malaysia. This
document describes what each function does. Nothing here is specific to a
language or runtime.

GSC has no public JSON API, and everything behind its "Buy Tickets" button was
long assumed to be unreachable. It is not. The website is a Nuxt app whose
pages are server rendered, and the booking app behind it is an Angular app on a
separate host calling a plain ASP.NET service:

| Host | What it is |
|---|---|
| `www.gsc.com.my` | The website. Server rendered HTML. |
| `epaymentwebapp.gsc.com.my` | The booking app, an Angular SPA, and its proxy. |
| `epaymentapi.gsc.com.my` | The booking service itself. |

The service takes **no API key, no signature and no session token**. The site's
own interceptor only attaches an `Authorization` header when a member is signed
in, and browsing anonymously sends none at all, so these endpoints can be called
the way a browser calls them. It speaks **XML**, not JSON.

So this scraper reads two sources: the website for the editorial listings and a
movie's credits, and the booking service for everything a session needs.

---

## Usage

Both languages expose the same CLI and the same importable functions. For the
cinema pair the commands are identical; substitute `python` / `.py` for `node`
/ `.ts`: `gsc_scraper` takes `cinema` / `date` / `--movie` / `--seats` /
`--seat-map` / `--prices`; `tgv_scraper` takes `BU0` / `--cinemas` /
`--movie` / `--seats`. The specs (`docs/gsc_spec.md`, `docs/tvg_spec.md`)
describe the behaviour once; this section confirms the language-neutral
interface exists in both implementations.

---

## Concepts

| Term | Meaning |
|---|---|
| **Listing** | One of the website's three shelves of films: now showing, coming soon, advance sales. |
| **Catalogue** | The booking service's list of every film, one record each. |
| **Parent / variant** | GSC files a film once and then once per bookable format, so "Digger" and "(IMAX) Digger" are two records under one parent. The parent is the film; each variant is what a session actually books. |
| **Movie** | One film. Carries its variants, and the sessions it has, when the result of a schedule query. |
| **Showtime** | One screening: a film, a hall, a start time, a hall format, and optionally seat counts and ticket prices. |
| **Business date** | The day a session is filed under. Usually the calendar day, but a 00:15 show is filed under the previous day's business date. |
| **Hall format** | The cinema's tier for a hall: Digital 2D, IMAX, 4DX, GETHA, CABIN. Each has a code and a display label. |
| **Outlet** | One cinema. Has a numeric booking id (`235` is GSC Lotus's Kepong) and a name prefixed with its town. |
| **Region** | A sales region: Klang Valley, Northern, Southern, East Coast, East Malaysia. |
| **Town** | The town in the service's own name, e.g. "Kepong". **Not** the same field as the website's `city`, which is sometimes a state. |
| **Placeholder** | Text standing in for a value the site has not published: `N/A`, `NA`, `-`, `TBC`, `TBA`. Treated as absent. |

---

## The service

| Method | Query | Returns |
|---|---|---|
| `getEpaymentMovie_ParentChild` | `includeChild=true` | Every film, its variants, and every date it has sessions on. |
| `getShowTimesByMovie_ParentChild_V2` | `parentid`, `oprndate` | One film, at every cinema that has it, for one date. |
| `getShowTimesByLocation_ParentChild_V2` | `locationid`, `oprndate` | One cinema, one date: the whole day. |
| `getHallSeatStatus` | `locationid`, `hallid`, `showdate`, `showtime` | One hall: the seat grid, the counts, and what is still sellable. |
| `getTicketPricingEpaySpecialV5` | `locationid`, `hallid`, `filmid`, `showdate`, `showtime` | Ticket types and prices for one session. |
| `GetCSOAllLocationV2` | — | The outlet directory. Reached through the booking app's proxy, as JSON. |

Two of these are worth calling out because they shape the API above them:

- **One film across the country is one request.** `getShowTimesByMovie...`
  returns every cinema at once, so `movie()` never has to sweep.
- **Seats and prices have no batch form.** One request per session, so both are
  opt-in and both run after filtering.

---

## Listings

| Function | Returns | Description |
|---|---|---|
| `listing(name)` | `Movie[]` | The website's shelf by name: `now-showing`, `coming-soon` or `advance-sales`. Fails on an unknown name, listing the valid ones. |
| `parse_listing(html)` | `Movie[]` | Pulls every movie card out of saved listing HTML. No network. |
| `detail(slug)` | `Movie` | The website's full record for one film: synopsis, director, cast, trailer, release date. |
| `parse_detail(html, slug)` | `Movie` | Reads one film's detail page from saved HTML. No network. |
| `movies()` | `Movie[]` | The booking service's whole catalogue, one record per film. One request. |
| `parse_movies(xml)` | `Movie[]` | The same, from a saved payload. No network. |
| `fetch(path)` | HTML | Fetches one website page and returns it as text, for parsing yourself. |
| `request(path, params)` | text | Calls one booking-service endpoint and returns the raw body. |

**`listing()` is the one function with no counterpart on the other scraper, and
there is a reason.** Which shelf a film sits on is an editorial decision GSC's
website makes. The service has no such category and no endpoint that reports
one, so the catalogue from `movies()` is not divided this way and inventing a
TGV equivalent would mean guessing.

---

## Outlets

| Function | Returns | Description |
|---|---|---|
| `cinemas()` | `Cinema[]` | Every outlet in Malaysia, sorted by name. One request. |
| `cinemas(with_details=False)` | `Cinema[]` | The lighter list. One request either way; the details are what the second one adds. |
| `parse_cinemas(payload, directory?)` | `Cinema[]` | The directory payload. No network. |
| `parse_cinemas_page(html)` | `Cinema[]` | The website's outlet page. No network. |
| `join_directories(outlets, directory)` | `int` | Merges the two on name. No network. |
| `by_city(outlets, city)` | `Cinema[]` | Narrows an outlet list to one town. |
| `by_region(outlets, region)` | `Cinema[]` | Narrows to one region, by key or label. |
| `filter_cinemas(outlets, where)` | `Cinema[]` | Either of the two above, whichever matches. |
| `find_cinema(outlets, needle)` | `Cinema` | Looks up one outlet by id, slug or name. Fails if the name is ambiguous, listing the candidates. |
| `regions()` | `{key: label}` | Every region. Derived from the directory, one request. |

### Two directories, one outlet list

This is the fiddliest part of the whole scraper, and it is worth being explicit
about.

The two sources share **no id at all** and **almost no name**:

| | Service | Website |
|---|---|---|
| Name | `Kepong - Lotus\`s (Kepong Village Mall)` | `GSC Lotus's Kepong` |
| Id | `235` | none, only the slug `gsc-lotuss-kepong` |
| Has | region, address, postcode, state | slug, town, amenities |

`cinemas()` returns the service's list, because showtimes can only be queried
against its ids. `cinemas(with_details=True)` then joins the website's page onto
it to attach `slug`, `city` and `features`, matching on name.

The matcher is deliberately conservative. Candidates are scored on token
overlap, taken strongest-first with each side used once, and anything below the
threshold is left unjoined rather than guessed. The town token is what breaks
the near-identical pairs: `GSC IOI Mall Kulai` and `GSC IOI Mall Puchong` share
every token but the town, and `GSC Spring Bintulu` would otherwise take
`GSC The Spring, Kuching`'s outlet. The website glues some words together where
the service splits them (`TropicanaGardens`), so both sides are cut the same way
before comparing. On the current directory this joins **50 of 50** outlets, each
to the right one.

An outlet the join cannot resolve keeps its service name and leaves `slug`,
`city` and `features` as `None`. `find_cinema()` still finds it, because it also
matches on the slug, which is what the website's own name becomes.

**The directory is the 50 GSC outlets that take online payment.** Aurum Theatre
and Velvet screen the same films on the same service and do turn up in schedule
results, but they are not in the directory, so `schedule()` cannot reach them by
id. `movie()` will, because it asks the service for one film rather than walking
the directory.

---

## Schedules

| Function | Returns | Description |
|---|---|---|
| `day(cinema_id, date?, seats?, prices?, after?, before?, formats?)` | `Movie[]` | Every film and session at one outlet on one date. |
| `schedule(cinema_ids?, date?, ...)` | `{cinema_id: Movie[]}` | The same across many outlets, in parallel. With no ids given, every outlet is swept. |
| `movie(slug_or_title, date?, ...)` | `[(cinema_id, Movie)]` | Every session of one film. Fails if it has none. |
| `business_dates()` | `date[]` | Every date the service will answer for. One request, the catalogue. |
| `cinema_experiences(cinema_id, date?)` | `{code: label}` | One outlet's hall formats, read off its day payload. |

A film is matched by id, by slug, by exact title, or by a loose form that
ignores a bracketed format prefix, because one film exists under several titles
that differ only by `"(InfinityVision) "` or `"(4DX) "`. Every variant with
sessions is returned rather than only the first catalogue match, so a search for
`Avengers: Endgame Encore` also finds the `(InfinityVision)` showings.

**Ordering guarantee:** filters are applied *before* seats and prices are
fetched, never after. Both cost a request per session, so narrowing the result
afterwards would still pay for every screening that was thrown away.

---

## Sessions

| Function | Returns | Description |
|---|---|---|
| `tickets(cinema_id, showtime)` | `TicketType[]` | Every ticket type on one session. One request. The lowest-level pricing call. |
| `session_prices(showtime)` | the showtime | Prices one screening and nothing else. Mutates and returns the showtime, so its price fields read the same afterwards. |
| `seat_status(cinema_id, showtimes)` | `{session_id: counts}` | Remaining seats for one or more sessions. One request each. |
| `seat_plan(cinema_id, showtime)` | `SeatPlan` | The full seat map for one session, aisles included. One request. |
| `render_seat_plan(plan)` | text | Draws a hall as fixed-width text. |
| `seat_number(seat)` | `str \| None` | A seat's printed number. |

`day(..., prices=True)` and `day(..., seats=True)` are the bulk paths and loop
the above over the showtimes that survived filtering. A failed session is
reported on stderr and left without a price, so it prints as a missing price
rather than a missing screening.

---

## Filtering

| Function | Description |
|---|---|
| `filter_times(movies, after?, before?)` | Keeps only showtimes inside an inclusive time window. The window is anchored on the **business date**, not the calendar day, so `--from 18:00` still catches a 00:15 show filed under yesterday. |
| `filter_formats(movies, formats?)` | Keeps only showtimes in the named hall formats. Comma-separated. Case-insensitive; an unknown format matches nothing. |
| `normalise_date(value?)` | Accepts several date formats and a date object; returns `YYYY-MM-DD`. Defaults to today, Malaysia time. |
| `parse_clock(value)` | `'18:00'` or `'1800'` → `[18, 0]`. |
| `looks_like_date(value)` | True when a value is shaped like a date rather than an outlet name. |
| `looks_like_clock(value)` | True for `HH:MM` or `h:mmAM`/`PM`. |
| `clock_candidates(value)` | Every spelling of one `--time` value worth matching, in both 24- and 12-hour form. |

---

## Records

### Movie

`id`, `slug`, `url`, `rating`, `runtime`, `synopsis`, `director`,
`release_date`, `trailer_url`, `poster_url`, `language` and `variants` read
straight from the service. `slug` is **derived from the title**, because the
service has no slug of its own; the derivation reproduces the website's
convention exactly, punctuation deleted rather than turned into a separator, so
`Khong-Khaek 2` is `khongkhaek-2` while `Spider-Man: Brand New Day` is
`spiderman-brand-new-day`.

`genre`, `subtitles`, `tags`, `experiences` and `cast` are lists. `language` is
the raw string, with `languages` as the split form.

`synopsis`, `director`, `cast`, `release_date` and `trailer_url` come from the
**website's detail page**, because the service carries none of them. Where the
website has not published a field it writes `N/A`, and an unreleased title's
running time is one of them, so `detail()` backfills from the catalogue rather
than leaving a gap the service could have filled.

`booking_url` and `booking_id` are the website's own links into its ticket
flow, and `booking_id` is the same parent code the catalogue uses, so the two
records join on it.

### Cinema

`id` is the numeric booking id and is what every schedule query needs. `name` is
the service's own name, prefixed with the town. `town` is that town on its own
and is always available; `city` is the website's label, which is sometimes a
state instead. `slug`, `city` and `features` need the website page and are
`None` without it.

### Showtime

`experience` is the format code, `experience_name` its display label read from
`type_desc`. `movie_id` is the **bookable variant** code, not the film's own
id, and it is what prices are quoted against.

### TicketType

A session sells several ticket types at once. `Adult` and `Twin` are face
value; `Children`, `OKU`, `Senior` and `Student` are concessions. `standard`
marks the face-value ones and is what every price field on a showtime is
computed from. `seats` is how many people a ticket admits, so `Twin` is 2.

### Seats

Three separate notions of "how many seats" arrive in one payload and they do
not agree, so they are kept apart rather than reconciled by guesswork:

| Field | Meaning | Where it comes from |
|---|---|---|
| `seats_total` | Physical seats in the hall | counted from the grid |
| `seats_blocked` | Seats the grid marks `D` | counted from the grid |
| `seats_left` | Seats the service will still sell | the hall booking block |
| `seats_booked` | The difference | derived |
| `reported_total` | *Not* the seat count | the hall element's `maximumseats` |

`seats_left` prefers the service's own availability block because that is the
figure the site shows, and counting the seat states does not reproduce it: on
the captured hall the states account for 6 of the 7 seats the service holds
back. `maximumseats` is kept as `reported_total` and deliberately not used,
because on that same hall it reads 12 for a 134-seat room.

Seat states normalise to `A` free, `B` booked, `D` blocked, `?` unknown, so one
renderer reads either scraper's seats. `number` is the printed label and `id` the
booking identifier; they are different and both are kept.

---

## Data conventions

**Times are Malaysia wall-clock.** The service splits a session's time into a
calendar date and a bare `HHMM`, with no offset. They are attached to a fixed
UTC+08:00 here. Malaysia has no daylight saving, so the offset is always +08:00.
The service's own 12-hour rendering is kept as `time_text`; `time` is the
24-hour form the rest of the API speaks.

**Money is in ringgit.** The service sends `price` as a decimal string.

**The tax field is deliberately ignored.** Every ticket carries a second money
field, `tax`, and it does not reconcile with any Malaysian rate: it works out to
exactly 20.00% of the price at one Selangor outlet and about 9.09% at another,
both in Selangor, where cinema admission carries a flat 25% entertainment duty
and movie tickets are exempt from service tax. Adding it inflated every price
and made identical tickets read differently between outlets.

**Placeholder text is treated as absent.** `N/A`, `NA`, `-`, `TBC` and `TBA`
become `None` rather than surviving as literal strings a caller has to filter.

**URLs are made absolute.** Protocol-relative links get `https:` and
site-relative links get the base host.

**A 404 is a clean error, and so is a soft one.** Some missing website pages
return HTTP 200 with a meta refresh to a not-found page. Both that and a genuine
404 report "not found" for the requested path.

**Requests are throttled.** A shared throttle spaces calls one per interval, so
a pool of parallel workers still hits the site at the global rate. The pause can
be dropped with a flag only if you have a reason.

---

## Errors

| Condition | Behaviour |
|---|---|
| Unknown listing name | Rejected, listing the three valid names. |
| Malformed movie slug | Rejected before any request is made. |
| Malformed date or time | Rejected with a message naming the expected format. |
| Unknown or ambiguous outlet | Rejected; an ambiguous name lists the candidates. |
| A film has no sessions | Reported, with a hint if filters were in play. |
| Page not found, either directly or via a soft redirect | Reported as not found, with the path. |
| HTTP error from the service | Reported with the status code and the response body. |
| Malformed or non-XML reply | Reported as such, rather than parsed optimistically. |
| Network error or timeout | Reported with the reason. |
| TLS verification failure | Reported with the fix. |

Every fatal condition raises one error type, so the CLI can report it as a
single line rather than a traceback.

---

## Not implemented

| Missing | Why |
|---|---|
| Concessions and food-and-bundle pricing | The pricing response carries them and they are parsed out of the same call, but there is no function or record for them. They are admission's neighbour, not part of it. |
| Favourites, member benefits, rewards | On the booking app, behind a login. Browsing anonymously reaches none of it. |
| Hall booking | A whole-hall hire product the service has endpoints for, which is a different product from buying seats. |

Getting those would need the authenticated side of the booking app, which is a
different job with different terms.

---

## The shared contract

Both scrapers in this repo expose the same function names and emit the same
JSON keys, so one consumer reads either site's output. This document and
[`tgv_spec.md`](tgv_spec.md) describe the shared surface once each, with the
differences listed where they occur. The Python offline suite pins the key sets, and the
TypeScript ports under `typescript/` emit the same six sets and produce
byte-identical records from the same saved bodies.