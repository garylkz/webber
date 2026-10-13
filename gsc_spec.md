# GSC Scraper — Functional Specification

Movies now showing, coming soon, on advance sale, and the outlet directory for
GSC Cinemas Malaysia. This document describes what each function does. Nothing
here is specific to a language or runtime.

GSC has no public JSON API. The `static/movie-lists.json` the frontend reads is
not deployed, and the state bundle the app ships is 8.6 MB of obfuscated
JavaScript rather than JSON. The pages are server-rendered, so everything is read
out of the HTML — about 57 KB per listing, and far more stable than the bundle.

---

## Concepts

| Term | Meaning |
|---|---|
| **Listing** | One of three pages of movies: now showing, coming soon, or advance sales. |
| **Movie** | One film, with whatever the listing card carries and, from the detail page, its credits. |
| **Booking id** | The numeric id GSC's ticket flow uses. Found in the booking link's query string. |
| **Cinema** | One GSC outlet, with a name, a slug, a town, and a list of formats and amenities. |
| **Placeholder** | Text GSC substitutes when it has no real value: `N/A`, `NA`, `-`, `TBC`, `TBA`. Treated as absent. |

---

## Listings

| Function | Returns | Description |
|---|---|---|
| `listing(name)` | `Movie[]` | Fetches one listing by name: `now-showing`, `coming-soon` or `advance-sales`. Fails on an unknown name, listing the valid ones. |
| `parse_listing(html)` | `Movie[]` | Pulls every movie card out of saved listing HTML. No network. |
| `movie(slug)` | `Movie` | Fetches the detail record for one movie. The slug is tidied first, so a full URL path or one with stray slashes still resolves. |
| `parse_detail(html, slug)` | `Movie` | Reads one movie's detail page from saved HTML. No network. |
| `cinemas()` | `Cinema[]` | Every GSC outlet in Malaysia. |
| `parse_cinemas(html)` | `Cinema[]` | Parses a saved cinema-list page. No network. |
| `fetch(path)` | HTML | Fetches one page and returns it as text, for parsing yourself. |
| `by_city(cinemas, city)` | `Cinema[]` | Narrows an already-fetched outlet list to one town, case-insensitively. |
| `show(movies)` | — | Prints a listing with numbering, runtime, rating, audio and subtitles. |
| `show_cinemas(outlets)` | — | Prints the numbered outlet directory with town and features. |

**Duplicate cards are dropped.** A slug already seen on the page is skipped, so a
movie promoted in two placements on the same listing appears once.

---

## Listing cards

Each card on a listing page carries five chips, and **the icon beside a chip is
what says what it means** — there is no label. The parser keys off the icon, so
genre, duration, language, subtitles and rating are never confused for each other
when the order changes.

| Field | Meaning |
|---|---|
| `title` | Display name. |
| `slug` | URL segment. Also the join key for the detail page. |
| `url` | Canonical movie page. |
| `genre` | Single genre string as GSC renders it. |
| `duration` | Runtime as GSC writes it, e.g. `1 hr 45 mins`. |
| `language` | Raw audio string, sometimes comma-joined, e.g. `BM,CAN,ENG,PUN`. |
| `languages` | The same value split into a list. Use this in your own code. |
| `runtime` | Runtime in minutes. `null` when unknown. |
| `subtitles` | Subtitle codes as a list. |
| `rating` | Classification. |
| `poster_url` | Absolute poster image URL. |
| `booking_url` | Absolute link into GSC's ticket flow. |
| `booking_id` | The numeric id from that link, or `null` if the link carries none. |

## Detail fields

Only present when the detail page was read; the same record shape carries them
as `null` when it came from a listing.

| Field | Meaning |
|---|---|
| `synopsis` | Plot summary. |
| `director` | Director credit. |
| `main_stars` | Cast names as a list. |
| `trailer_url` | Trailer link. |
| `trailer_id` | YouTube id extracted from that link, whatever URL shape it uses. |
| `release_date` | Release date as GSC states it. |

## Cinemas

| Field | Meaning |
|---|---|
| `name`, `slug`, `url` | Outlet identity and canonical page. |
| `city` | Whatever GSC labels the location — usually a town, but not always. |
| `features` | Formats and amenities as a list. Empty for plain single-screen outlets, where GSC omits the row entirely. |

---

## How detail is read

The detail page embeds a **schema.org/Movie structured-data block**, which is the
richest and cleanest source. Everything falls back to the visible specs table
below it when the structured block is missing a field. Attributes on the embedded
block appear in any order, so it is found by its type rather than its position.

Details worth knowing:

- **Genre may be a list** in the structured block and a string elsewhere; both are
  normalised to one comma-joined string.
- **Cast may be a single object** rather than a list; it is normalised. When the
  structured block has no cast, the visible credit row is used instead.
- **Trailer may be absent** or present as an object holding an embed URL. A
  trailer link that is not a recognisable video link yields no id rather than a
  wrong one.
- **Title falls back three times**: the structured block, then the visible page
  heading, then the slug. A movie is never nameless.

---

## Data conventions

**Placeholder text is treated as absent.** `N/A`, `NA`, `-`, `TBC` and `TBA` all
become `null` rather than surviving as literal strings a caller has to filter.

**Runtime is parsed from prose.** `1 hr 45 mins` becomes `105` minutes. A duration
with no recognisable hours or minutes yields `null`; `0` is also treated as
unknown rather than a zero-length film.

**Comma-joined codes are split.** GSC renders multiple audio or subtitle codes as
one string. The raw value is kept and the split list is provided alongside it, so
nothing is lost.

**URLs are made absolute.** Protocol-relative links get `https:` and site-relative
links get the base host, so callers never have to resolve them.

**A 404 is a clean error, and so is a soft one.** Some missing pages return HTTP
200 with a meta refresh to a not-found page. Both that and a genuine 404 report
"not found" for the requested path, rather than returning HTML that parses to
nothing.

**Requests are throttled.** Each fetch waits before it goes out, because this is
someone's live site. The pause can be dropped with a flag only if you have a
reason.

---

## Errors

| Condition | Behaviour |
|---|---|
| Unknown listing name | Rejected, listing the three valid names. |
| Malformed movie slug | Rejected before any request is made. |
| Page not found, either directly or via a soft redirect | Reported as not found, with the path. |
| TLS verification failure | Reported with the fix. |
| Network error or timeout | Reported with the reason. |

Every fatal condition raises one error type, so the CLI can report it as a single
line rather than a traceback.

---

## Not implemented

| Missing | Why |
|---|---|
| Showtimes | GSC does not publish a schedule on any server-rendered page; sessions only exist inside its booking flow behind a cinema and date selection. |
| Seat maps | Same reason — nothing server-rendered to read them from. |
| Prices | Same reason. |

Getting those would need the booking endpoints behind that flow, which is a
different job with different terms. The listing and directory surfaces are what
GSC renders publicly, and that is what this covers.