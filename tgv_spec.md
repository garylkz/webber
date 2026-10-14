# TGV Scraper — Functional Specification

Showtimes, ticket prices and seat maps for TGV Cinemas Malaysia, read from
TGV's own JSON API. This document describes what each function does. Nothing here
is specific to a language or runtime.

---

## The shared contract

The two scrapers in this repo are deliberately interchangeable: they expose the
same function names and emit the same JSON keys, so one consumer reads either
site's output. Where a function does the same job on both sites it is named the
same and behaves the same. This document covers what is common and what is
TGV-specific; `gsc_spec.md` covers the other side.

**Named for the same job on both sites:** `movies`, `detail`, `cinemas`,
`regions`, `business_dates`, `day`, `schedule`, `movie`, `tickets`,
`session_prices`, `seat_status`, `seat_plan`, `render_seat_plan`,
`filter_times`, `filter_formats`, `by_region`, `by_city`, `filter_cinemas`,
`find_cinema`, `normalise_date`, `parse_clock`, `looks_like_date`,
`experience_label`, `clean`, `split_codes`, `as_list`, `strip_html`,
`duration_minutes`, `seat_number`, `Throttle`, `show_movies`, `show_schedule`,
`show_cinemas`, and the `Movie`, `Cinema`, `Showtime`, `TicketType`, `Seat`,
`SeatRow`, `SeatArea` and `SeatPlan` records.

**Deliberately one-sided:**

| Name | Only | Why |
|---|---|---|
| `listing`, `parse_listing`, `parse_detail`, `parse_cinemas_page`, `join_directories` | GSC | GSC reads a website as well as a service. TGV has no server-rendered site behind the API. |
| `parse_movies`, `parse_movie_shows` | GSC | GSC publishes a catalogue and has a one-film lookup. TGV reports films only through sessions, so `tgv.movies()` is a union of day payloads. |
| `align_by_number`, `number_anchor` | TGV | TGV numbers each row independently, so a hall has to be realigned before it can be drawn. GSC's coordinates are already absolute. |

**Some fields changed shape on this side**, because the two sites disagree and
one of them was wrong:

| Field | Was | Now | Why |
|---|---|---|---|
| `Movie.cast` | `str` | `tuple[str, ...]` | TGV stores the cast as one comma-joined string. GSC returns a list. A tuple is what both can produce, and splitting it once here beats every caller splitting it again. |
| `Cinema.keyword` | `str` | `slug` | The Python scraper spelled it `keyword` and GSC spelled it `slug`. `keyword` is kept as an alias, so existing callers keep working. |
| `Movie.rating` | `"tba"` | `None` | `tba` is a placeholder, not a rating. Both scrapers now treat `N/A`, `NA`, `-`, `TBC` and `TBA` as absent, so a caller does not have to filter for placeholder strings on one site and not the other. |
| `Cinema.city`, `Cinema.state`, `Showtime.hall_id`, `Movie.duration`, `Movie.booking_url`, `Movie.booking_id` | absent | `None` | Filled in by the other scraper. They stay in the shared output so the key sets match. |

Everything else in this document is unchanged, and the notes below about TGV's
own quirks still stand.

---

## Concepts

| Term | Meaning |
|---|---|
| **Cinema** | One TGV outlet. Has an id (`BU0`), a name, a region, an address, coordinates, opening hours and the hall formats it offers. |
| **Region** | A grouping of cinemas, e.g. Klang Valley. Every cinema belongs to one. |
| **Movie** | One film. Has a title, slug, genre, runtime, rating, synopsis, artwork and the sessions it is showing. |
| **Showtime** | One screening: a movie, a hall, a start time, a date, a hall format, and optionally seat counts and ticket prices. |
| **Business date** | The day a session is filed under. Usually the calendar day, but a 00:30 show is filed under the previous day's business date. |
| **Hall format** | The cinema's tier for a hall — Deluxe, IMAX Laser, Indulge, Junior and so on. Each has a code and a display label. |
| **Hall area** | One priced part of a hall, e.g. SINGLE or TWIN. A single hall may contain several areas, each with its own layout and price list. |
| **Seat** | One seat, carrying the hall's grid coordinates so aisles are preserved. |
| **Session** | A live screening instance, identified by a session id. Prices and seat maps are fetched per session. |

---

## Directories

| Function | Returns | Description |
|---|---|---|
| `regions()` | `{regionKey: regionLabel}` | Every TGV region. Keys are slugs like `klangvalley`; labels are display names like `Klang Valley`. |
| `cinemas()` | `Cinema[]` | Every outlet in Malaysia, sorted by name. Includes addresses, coordinates, opening hours and hall formats. |
| `cinemas(withDetails: false)` | `Cinema[]` | The lighter list — id and name only. One request instead of two, used when the caller only needs ids to query schedules. |

## Schedules

| Function | Returns | Description |
|---|---|---|
| `day(cinemaId, date?)` | `Movie[]` | Every movie and every session at one cinema on one date. Defaults to today. |
| `schedule(cinemaIds?, date?)` | `{cinemaId: Movie[]}` | The same across many cinemas. Cinemas are queried in parallel. With no ids given, every cinema in the country is swept. |
| `movie(title, date?)` | `[{cinemaId, movie}]` | Every session of one film across cinemas. Matches on the URL slug or the title. Fails if the film has no sessions. |
| `businessDates()` | `date[]` | Every date the API will answer for, ascending. This is the set across all cinemas, so it includes special and advance dates with nothing on them — check with `day()` before trusting one. |
| `movies(date?, cinemaIds?)` | `Movie[]` | Every film with sessions on a date, one record per film with all of its sessions. TGV has no catalogue endpoint, so this is a union of day payloads and costs one request per cinema. Unlike `day()`, a film seen in several cinemas is merged rather than repeated. |
| `detail(title, date?)` | `Movie` | One film as a single record: the first of `movie()`'s hits. TGV has no detail page separate from the schedule, so this needs no second call. |
| `cinemaExperiences(cinemaId)` | `string[]` | One outlet's hall formats, from the directory. One request. |

## Sessions

| Function | Returns | Description |
|---|---|---|
| `tickets(cinemaId, sessionId, areaCode, date?)` | `TicketType[]` | Every ticket type on sale for one session in one hall area. The lowest-level pricing call: one request, no batching, no filtering. |
| `sessionPrices(showtime)` | the showtime | Prices one screening and nothing else. Mutates and returns the showtime, so its price fields read the same afterwards. Loops the showtime's hall areas for you. |
| `seatStatus(cinemaId, sessionIds)` | `{sessionId: counts}` | Raw seat numbers for a batch of sessions: total, used, left, and percentage used. Answers "how many are left". |
| `seatPlan(cinemaId, showtime)` | `SeatPlan` | The full seat map for one session, including aisle positions. Answers "which ones". One request per session. |

## Filtering

| Function | Description |
|---|---|
| `filterTimes(movies, after?, before?)` | Keeps only showtimes inside an inclusive time window. The window is anchored on the **business date**, not the calendar day, so `--from 18:00` still catches a 00:30 show filed under yesterday. |
| `filterFormats(movies, formats?)` | Keeps only showtimes in the named hall format(s). Comma-separated. Matching is case-insensitive; an unknown format simply matches nothing. |
| `byRegion(cinemas, region)` | Narrows an already-fetched cinema list to one region, by region key or label. |
| `findCinema(cinemas, needle)` | Looks up one outlet by id, name or keyword, case-insensitively. Fails if the name is ambiguous, listing the candidates. |
| `looksLikeDate(value)` | True when a value is shaped like a date rather than a cinema name. |
| `parseClock(value)` | `'18:00'` or `'1800'` → `[18, 0]`. |
| `normaliseDate(value?)` | Accepts several date formats and a date object; returns `'YYYY-MM-DD'`. Defaults to today. |

**Ordering guarantee:** filters are applied *before* seats and prices are fetched,
never after. A price costs one request per session per hall area, so narrowing the
result afterwards would still pay for every screening that was thrown away.

---

## Movies

| Field | Description |
|---|---|
| `title`, `slug` | Display name and URL slug. The slug is what `movie()` matches on. |
| `url` | Canonical page on tgv.com.my. |
| `trailerId` | YouTube id, extracted from whatever trailer URL shape was stored. Non-YouTube links are dropped entirely. |
| `times` | Every showtime for this movie at this cinema, as `HH:MM`. |
| `genre`, `tags`, `subtitles`, `experiences` | Lists. The API is inconsistent about whether these arrive as arrays, strings or null; they are normalised to lists. |
| `rating`, `runtime`, `language`, `releaseDate` | Descriptive fields, as TGV reports them. |
| `synopsis` | Stored as HTML, collapsed to plain text. |
| `posterUrl`, `bannerUrl` | Built from the CDN path TGV stores. |
| `cast` | Names as a list. TGV stores one comma-joined string, so it is split here. |
| `language`, `languages` | The raw language string and the same value split into a list of codes. |
| `cinemaId`, `cinemaName`, `showtimes` | Set when a movie is the result of a schedule query. Cleared by `movies()`, whose records span the country. |

## Showtimes

| Field | Description |
|---|---|
| `time` | Start time as `HH:MM`, Malaysia wall-clock. |
| `ends` | Start plus the movie's runtime, when the runtime is known. |
| `hall`, `experience`, `experienceName` | Hall name, hall format code, and its display label. Unknown codes still render as a title-cased name. |
| `seatsLeft`, `soldOut` | Remaining seats, and whether the hall is full. |
| `sessionId` | Identifies this screening. Prices and seat maps are fetched per session id. |
| `hallId` | Always null here. TGV prices per hall area and does not send the hall's own id; the other scraper fills it in. |

`Seat.state` normalises the API's integer seat status to the same letters the
other scraper uses: `A` free, `B` booked, `?` unknown. The raw integer is kept
as `Seat.status`, and `number` is the printed seat number.

## Prices

A session sells several ticket types at once: plain seats, then a long tail of
discounts — senior and OKU rates, bank card promos, member rates. The plain seat
is what an ordinary customer pays, and it is the figure quoted everywhere below.

| Field | Description |
|---|---|
| `price` | Cheapest standard ticket seating one person. Falls back to the cheapest standard ticket of any size when the hall sells only bundles. |
| `singleSeatPrice` | Cheapest standard ticket admitting exactly one person. Bundles excluded. |
| `priceBundle` | Cheapest multi-seat bundle. |
| `priceRange` | `'RM25.00 - RM35.00'` when one session sells more than one single-seat price. |
| `perPerson` | Cheapest standard price divided by how many people the ticket seats. Use this when comparing across cinemas, since a raw ticket price can compare two people against one. |
| `ticketTypes` | Every ticket on sale, including concessions and promos, for callers who want the full list. |
| `soldOutPromos` | Promotions TGV has marked as no longer obtainable today. |

**Ticket fields:** code, description, price, how many people it admits, whether it
bundles multiple seats, whether it is a child-only or allocated-seating ticket,
which hall area it came from, whether it is a plain face-value seat, and whether
its promotion has run out.

**Prices are opt-in.** They cost one request per session per hall area, so they
are never fetched unless asked for, and a single failed area is reported rather
than allowed to fail the session — the remaining areas may still price.

---

## Seats

| Function | Description |
|---|---|
| `renderSeatPlan(plan, options?)` | Draws a hall as fixed-width text: blank for an aisle, the seat number when free, `X` when taken. |
| `alignByNumber(plan)` | Returns a copy where each seat's column comes from its printed number, and reports how many seats had to move. |
| `numberAnchor(area)` | Seat number → column, taken from the area's fullest row. That row spans the hall, so it fixes where each number sits. |
| `seatNumber(seat)` | A seat's printed number, or null when the id is not a plain integer. |

**Why alignment is needed.** TGV numbers each row independently, so in a row with
fewer seats the numbering packs against one end and the same seat number lands in
a different column in a different row. Real halls do not work that way: seat 15 is
in the same place in every row, and a short row simply has an empty space where the
missing number would be. Aligning restores that, and makes a short front row or a
mid-hall aisle visible instead of looking like a miscount.

**Rendering rules.** Every cell is the same width and a column with no seat in it
is left blank rather than closed up, because that is what makes aisles visible. The
drawing ends with a count of seats drawn per area, and prints the API's own figure
when the two disagree, so a miscount surfaces immediately rather than being argued
about.

**Row order is screen-end first.** TGV numbers rows from the back of the hall
towards the screen, which the payload proves three ways: the indices form one
contiguous run shared across areas rather than restarting per area; the rear-sold
TWIN area holds the low indices; and the lowest-indexed row is named `M` while the
highest is named `A`. Ordering by raw index draws the hall upside down.

**Empty rows are dropped.** Every area reports a full row count, but a second
area's rows arrive as nameless placeholders. Drawing them produces blank lines and
a phantom row count.

**Unknown seat states render as `?`** rather than being guessed at. A mislabelled
seat is worse than an unreadable one.

---

## Output

| Function | Description |
|---|---|
| `showMovies(movies, label?, perPerson?)` | Prints one cinema's films and their sessions. `perPerson` quotes the per-person figure instead of the ticket price. |
| `showSchedule(data, perPerson?)` | Prints a whole schedule, one section per cinema. |
| `showCinemas(cinemas)` | Prints the numbered outlet directory with region and hall formats. |

When a session is printed, the single-seat price is quoted and a bundle's pair price
is shown alongside it in brackets — showing only the bundle total would read as the
cost of one seat.

---

## Errors

| Condition | Behaviour |
|---|---|
| Malformed date or time argument | Rejected with a message naming the expected format. |
| Unknown or ambiguous cinema | Rejected; an ambiguous name lists the candidate outlets. |
| HTTP error from an endpoint | Reported with the status code and the response body. |
| Network error or timeout | Reported with the reason. |
| API returns `success: false` | Reported with the API's own error message. |
| Non-JSON response | Reported as such, rather than parsed optimistically. |
| A film has no sessions | Reported, with a hint if filters were in play. |
| One hall area fails to price | Reported for that area; the rest of the session still prices. |

Every fatal condition raises one error type, so a caller can distinguish "the
arguments were wrong" from "the site was unreachable" from "there was genuinely
nothing there". Argument errors are a distinct subtype, which is what lets a date
be probed without it being treated as a failure.

---

## Data conventions

**Times are Malaysia wall-clock.** The API returns naive local timestamps with no
offset. They are attached to a fixed `+08:00`. Malaysia has no daylight saving, so
that offset is exact year-round.

**Money is in cents.** The API reports integer cents. Prices are converted to
ringgit on read.

**`taxInCents` is deliberately ignored.** Every ticket carries a second money
field. It is not a tax in the Malaysian sense and does not reconcile: it works out
to exactly 20.00% at one Selangor cinema and about 9.09% at another, both in the
same state. Malaysia levies 8% service tax on services (movie tickets are exempt)
and 25% Selangor entertainment duty on admission, neither of which matches. Adding
it inflated every price and made identical tickets look different between outlets.

**Finished promotions are filtered out.** TGV marks an exhausted promotion inside
the ticket's description text rather than with a flag, and the ticket keeps its
listed price either way, so it has to be recognised explicitly.

**Requests are throttled.** A shared throttle spaces calls so a pool of parallel
workers still hits the site at one request per interval. The rate holds globally,
not per worker.

**Authentication is anonymous.** The site sends a session header that is empty for
a logged-out visitor, and that is what is sent here. No key, signature or token is
required by any endpoint.