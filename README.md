# webber

Cinema scrapers for Malaysia. MIT licensed — see [LICENSE](LICENSE).

| Implementation | Site | Source |
| --- | --- | --- |
| `python/gsc_scraper.py` | [GSC Cinemas](https://www.gsc.com.my) | server-rendered HTML |
| `python/tgv_scraper.py` | [TGV Cinemas](https://www.tgv.com.my) | TGV's own JSON API |
| `typescript/tgv_scraper.ts` | [TGV Cinemas](https://www.tgv.com.my) | TGV's own JSON API |

The Python scripts are single-file and dependency-free — standard library only,
nothing to install. `tgv_scraper.ts` is a TypeScript port of the TGV script
covering the same ground; see [`tgv_spec.md`](tgv_spec.md) for what each function
does, written independently of any language.

---

# TGV Cinemas

`tgv_scraper.py` reads TGV's own JSON API on `api.tgv.com.my`. It is
**undocumented and takes no API key**: every endpoint is a plain `POST` with a JSON
body (or a plain `GET`), and the only auth-shaped header the site sends is
`x-mvcsession`, which carries the logged-in user's token and is sent **empty** when
browsing anonymously. This calls the same endpoints the website calls, anonymously.

```bash
cd python
python tgv_scraper.py                          # today, every cinema
python tgv_scraper.py BU0                      # one cinema, today
python tgv_scraper.py "1 Utama"                # by name, or by keyword
python tgv_scraper.py BU0 2026-10-13           # one cinema, one date
python tgv_scraper.py --cinemas                # the outlet directory
python tgv_scraper.py --cinemas --region pinang
python tgv_scraper.py --dates                  # dates that have sessions
python tgv_scraper.py --movie other-mommy      # one film, every cinema
python tgv_scraper.py --movie other-mommy BU0  # one film, one cinema
python tgv_scraper.py --movie other-mommy --region klangvalley
python tgv_scraper.py --seats                  # add seat availability
python tgv_scraper.py --prices                 # add ticket prices
python tgv_scraper.py --prices --per-person    # quote per person, not per ticket
python tgv_scraper.py --json                   # machine readable
python tgv_scraper.py --workers 8              # more parallel cinemas
python tgv_scraper.py --no-delay               # skip the polite pause
```

Narrow a schedule down:

```bash
cd python
python tgv_scraper.py BU0 2026-10-18 --from 18:00            # evening only
python tgv_scraper.py BU0 2026-10-18 --until 18:00            # matinee only
python tgv_scraper.py BU0 2026-10-18 --format deluxe          # budget halls only
python tgv_scraper.py BU0 2026-10-18 --format imaxlaser       # IMAX only
python tgv_scraper.py BU0 2026-10-18 --from 18:00 --format deluxe --seats
```

`--from` and `--until` are inclusive, accept `HH:MM` or `HHMM`, and anchor on the
business date so post-midnight shows on a late session day count as evening. `--format`
matches TGV's experience codes and takes a comma-separated list. The filters chain, so
`--from 18:00 --format deluxe` means both conditions, not either.

Filters run **before** seats and prices are fetched, not after. A price is one request
per session per hall area, so narrowing first is what keeps a query cheap: an evening
run does not pay for the matinees it then discards. `--movie` also takes an optional
cinema and an optional date in either order, so `--movie slug BU0 2026-10-18` targets one
outlet instead of sweeping all 44.

```
1 UTAMA [BU0] 2026-10-11: 8 movies

  Other Mommy  (Horror | 93 min | 16 | English)
      15:00  Cinema 4  233 seats
      17:45  INDULGE 2  24 seats
      19:15  Junior  96 seats
```

Or import it:

```python
import tgv_scraper as tgv

tgv.cinemas()                          # every outlet with addresses
tgv.cinemas(with_details=False)        # id and name only, one request
tgv.regions()                          # {'klangvalley': 'Klang Valley', ...}
tgv.business_dates()                   # every date the API will answer for

# price one screening you picked, without pricing the rest of the day
films = tgv.day("BU0", "2026-10-18")
late = next(s for f in films for s in f.showtimes if s.time == "21:45")
tgv.session_prices(late).price        # 30.0

# or the raw call for one session in one hall area
tgv.tickets("BU0", late.session_id, "0000000002", "2026-10-18")

# one cinema, one date
for film in tgv.day("BU0", "2026-10-11", seats=True):
    print(film.title, film.genre, film.rating, film.times)
    for showtime in film.showtimes:
        print("   ", showtime.time, showtime.hall, showtime.experience_name, showtime.seats_left)

# one date, every cinema
for cid, films in tgv.schedule(on="2026-10-11").items():
    print(cid, len(films), "movies")

# one film, every cinema it is playing in
for cid, film in tgv.movie("other-mommy"):
    print(cid, film.cinema_name, film.times)
```

## TGV API

| Function | Endpoint | Returns |
| --- | --- | --- |
| `cinemas()` | `POST /api/cinemas/v1/getcinemas` | `list[Cinema]`, full directory |
| `cinemas(with_details=False)` | `GET /api/cinemas/v1/getstatecinema` | `list[Cinema]`, id and name only |
| `regions()` | `GET /api/cinemas/v1/getstatecinema` | `{region key: label}` |
| `business_dates()` | `POST /api/boxoffice/v1/moviesession_getsessionbusinessdates` | `list[str]`, ISO days |
| `day(cinema_id, on, seats)` | `POST /api/boxoffice/v1/moviesession_get` | `list[Movie]` with sessions |
| `schedule(on, seats)` | the same, once per cinema | `{cinema id: list[Movie]}` |
| `movie(slug, on)` | the same, once per cinema, filtered | `[(cinema id, Movie)]` |
| `seat_status(cinema_id, ids)` | `POST /api/boxoffice/v1/moviesession_getseatstatus` | `{session id: counts}` |
| `tickets(cinema_id, sid, area, date)` | `POST /api/boxoffice/v1/moviesession_gettickets` | `tuple[TicketType, ...]` for one session in one area |
| `session_prices(showtime)` | the same, once per area | the showtime, priced |
| `parse_tickets(payload)` | none, on saved JSON | `tuple[TicketType, ...]` |
| `filter_times(movies, after, before)` | none, in memory | Showtimes inside an `HH:MM` window |
| `filter_formats(movies, codes)` | none, in memory | Showtimes in the named hall formats |
| `request(path, payload)` | any of the above | raw decoded body |

`Movie` fields: `id`, `title`, `slug`, `url`, `genre`, `tags`, `rating`, `runtime`,
`language`, `subtitles`, `synopsis`, `director`, `cast`, `distributor`,
`release_date`, `trailer_url`, `trailer_id`, `poster_url`, `banner_url`,
`experiences`, `session_ids`, `cinema_id`, `cinema_name`, `showtimes`.

`Cinema` fields: `id`, `name`, `url`, `location` (region key), `region` (label),
`address`, `postcode`, `latitude`, `longitude`, `telephone`, `parking`,
`opening_hours`, `features`, `experiences`, `description`.

`Showtime` fields: `session_id`, `movie_id`, `cinema_id`, `date`, `start` (an aware
`datetime`), `time`, `hall`, `experience`, `experience_name`, `seat_types`,
`seats_total`, `seats_used`, `seats_left`, `sold_out`, `status`, `ends`, plus
`price`, `price_range` and `ticket_types` with `--prices`.

Notes:

- **Times** come back as naive Malaysia wall clock (`2026-10-11T17:45:00`). They are
  attached to a fixed UTC+08:00 here, which is always correct because Malaysia has no
  daylight saving.
- **The same film appears twice** in a `moviesession_get` payload: a full detail
  record under `results.movies`, and a bare id next to the sessions under
  `results.businessday.cinemas[].movies[]`. The parser joins them on the id, so
  titles and runtimes are not lost.
- **`runtimemins` is an empty string**, not `null`, for unreleased films. `runtime`
  is `None` for those.
- **`seatstatus` is `null`** on the day payload, so seat counts need the extra
  `getseatstatus` call. Pass `seats=True` to do it; it is one call per cinema.
  The counts are **remaining** seats, not hall capacity: the API returns
  `seats_total` and `seats_used`, and `Showtime.seats_left` is the difference.

- **Prices** need `--prices`. `gettickets` takes a single session id plus one of
  the session's `areacategorycodes`, so unlike seats it costs **one call per
  showtime per hall area**, which is why it is opt-in. Only the showtimes that
  survive the `--from`/`--until`/`--format` filters are priced, so narrow first.
  A failed area is reported on stderr and does not abort the run or cost the
  session its other areas. `session_prices(showtime)` is the single-session
  version if you already know which screening you want. Two things to know:
  - **Only `priceInCents` is read; `taxInCents` is ignored.** `priceInCents` is
    TGV's advertised ticket price and matches published listings (1 Utama IMAX
    reads RM35). `taxInCents` does not reconcile with any Malaysian rate: it
    works out to exactly 20% at 1 Utama and about 9% at Pavilion Damansara
    Heights, yet both outlets are in Selangor, where cinema admission carries a
    flat 25% entertainment duty and movie tickets are exempt from service tax.
    Adding it inflated every price and made identical tickets look different
    between outlets (1 Utama and Pavilion Damansara Deluxe both read RM25, not
    RM30 and RM27.27).
  - A session sells several types at once. Alongside the plain seat there are
    `SENIOR/OKU` concessions and bank promos such as `Maybank Mastercard
    Single`, which can be less than half the face price. `--prices` shows only
    the standard `SINGLE`/`TWIN`/`COUPLE` seat so the number is what an ordinary
    customer pays. Every type is still on `Showtime.ticket_types`, each flagged
    with `standard`.
  - **A session has several hall areas, each with its own price list.** The
    areas do not necessarily sell the same thing: 1 Utama's Cinema 3 sells a
    `TWIN` bundle in one area and a `SINGLE` seat in another. **Every** area is
    fetched, because reading only the first would report the pair price and
    hide the single seat. `Showtime.price` is therefore always the cheapest
    single seat; the bundle, if sold, is `Showtime.price_bundle`, and each
    `TicketType` records its `area_code`.
  - **TGV marks a finished promo in the ticket's `longDescription`** with text
    like "Today's Promotion Limit Reached", while still listing its price.
    Those tickets get `sold_out=True` and are excluded from every price.
    `Showtime.sold_out_promos` lists them.
- **`experience` codes** (`imaxlaser`, `indulge`, `junior-hotwheel`, `onyx`, ...) are
  mapped to display names by `experience_label()`. Unknown codes are title cased
  rather than dropped, so a new format still renders.
- **`business_dates()`** includes special and advance dates with no sessions yet.
  Check with `day()` before trusting a date.

Bad input exits with a message rather than raising, so the CLI stays readable:

```python
tgv.day("BU0", "next tuesday")   # SystemExit: bad date 'next tuesday', expected YYYY-MM-DD
tgv.find_cinema(tgv.cinemas(), "sunway")
# SystemExit: 'sunway' matches 5 cinemas: SWP SUNWAY PUTRA, SW0 SUNWAY PYRAMID, ...
```

## Politeness

`tgv_scraper.py` rate-limits to one request every 0.25s. A national run is one
request per cinema (44 calls) and goes out in parallel, but the throttle holds the
*global* rate, so `--workers 8` does not mean eight times the traffic. Drop the
delay with `--no-delay` only if you have a reason to.

## TypeScript

`typescript/tgv_scraper.ts` is a port of the above: same API, same CLI flags. It
runs directly on Node 22.6+ — types are stripped at load, so there is no build step
and no dependencies.

```bash
cd typescript
node tgv_scraper.ts                          # today, every cinema
node tgv_scraper.ts BU0 --seats              # one cinema, seats left
node tgv_scraper.ts BU0 --seat-map --time 13:30
node tgv_scraper.ts --prices --per-person
node tgv_scraper.ts --json --movie other-mommy
```

Imported, every network call is a promise:

```ts
import { day, sessionPrices } from './tgv_scraper.ts';

const films = await day('BU0', '2026-10-18');
const late = films.flatMap((f) => f.showtimes).find((s) => s.time === '21:45');
(await sessionPrices(late)).price;
```

Optional type check, no build output:

```bash
cd typescript
npx tsc --noEmit tgv_scraper.ts
```

Function-by-function behaviour is in [`tgv_spec.md`](tgv_spec.md).

---

# GSC Cinemas

`gsc_scraper.py` parses the **server-rendered HTML** of `gsc.com.my`. No JavaScript
engine, no headless browser, no private API, no dependencies.

```bash
cd python
python gsc_scraper.py                     # movies now showing
python gsc_scraper.py coming-soon
python gsc_scraper.py advance-sales
python gsc_scraper.py cinemas             # every GSC outlet
python gsc_scraper.py cinemas --city "Kuala Lumpur"
python gsc_scraper.py --all               # all three movie listings
python gsc_scraper.py --json              # machine readable
python gsc_scraper.py verity              # detail for one movie
python gsc_scraper.py --no-delay          # skip the polite 0.5s pause
```

```
now-showing: 47 movies

 1. (Rerun) La La Land
    Musical | 128 min | 13 | audio ENG | subs BM
```

Or import it:

```python
import gsc_scraper as gsc

for movie in gsc.listing("now-showing"):
    print(movie.title, movie.rating, movie.booking_id)

detail = gsc.movie("munafik-melawan-iblis")
print(detail.director, detail.main_stars, detail.trailer_id)

for cinema in gsc.by_city(gsc.cinemas(), "Kuala Lumpur"):
    print(cinema.name, cinema.features)
```

## GSC API

| Function | Returns |
| --- | --- |
| `listing(name)` | `list[Movie]` for `now-showing`, `coming-soon`, `advance-sales` |
| `movie(slug)` | `Movie` with detail fields filled in |
| `cinemas()` | `list[Cinema]`, every GSC outlet in Malaysia |
| `by_city(outlets, city)` | Filter a cinema list to one town, case-insensitively |
| `fetch(path)` | Raw HTML, if you want to parse it yourself |
| `parse_listing(html)` / `parse_detail(html, slug)` | Parse saved HTML, no network |
| `parse_cinemas(html)` | Parse saved cinema-list HTML, no network |
| `show(movies)` / `show_cinemas(outlets)` | Print the human-readable listing |

`Movie` fields: `title`, `slug`, `url`, `genre`, `duration`, `runtime`, `language`,
`languages`, `subtitles`, `rating`, `poster_url`, `booking_url`, `booking_id`, plus
`synopsis`, `director`, `main_stars`, `trailer_url`, `trailer_id`, `release_date` from
`movie()`.

`Cinema` fields: `name`, `slug`, `url`, `city`, `features`.

Notes:

- `language` is the raw string GSC renders, sometimes comma-joined
  (`"BM,CAN,ENG,PUN"`). `languages` is the same value normalised to a tuple. Use
  `languages` in your own code.
- `runtime` is minutes (`105`); `duration` is GSC's raw string (`"1 hr 45 mins"`).
- The cinema page is not only GSC-branded: it also lists **Aurum Theatre** and **Velvet
  Cinemas** under `/cinema/` slugs, so filter on `name` if you need GSC proper.
- GSC files a couple of outlets under a state rather than a town (GSC Spring Bintulu is
  listed as "Sarawak"), so `city` is whatever GSC says.
- GSC has no public JSON API. The `static/movie-lists.json` the app reads is not
  deployed, and the Nuxt state bundle is 8.6 MB of obfuscated JavaScript rather than
  JSON. Server-rendered HTML is ~57 KB and far more stable, so that is what this
  parses.
- Detail fields come from the `schema.org/Movie` JSON-LD block each movie page embeds,
  falling back to the visible specs table.

---

# certifi

Both Python scripts work with no dependencies *provided your Python has a CA store*.
Some installs (Homebrew Python on macOS, slim containers) ship without one, and every
HTTPS call then fails with `CERTIFICATE_VERIFY_FAILED`. If you hit that:

```bash
pip install certifi
```

Both pick up `certifi`'s bundle automatically when it is importable. That is the only
situation where you need it, and it is not a hard requirement. The TypeScript
implementation has no such issue: TLS trust comes from the runtime.

# Tests

```bash
cd python
.venv/bin/python -m pytest          # offline, runs against tests/fixtures/
.venv/bin/python -m pytest -m live  # also hits the real sites
```

172 offline tests and 26 live ones. The fixtures were captured from the live sources;
if either site changes shape the offline suite is what fails first, which is
intentional.

The TypeScript implementation has no test suite yet. Its parsers are pure functions
over saved JSON, mirroring the Python ones, so the fixtures under `python/tests/fixtures/`
are the natural starting point.

If the live run fails with a TLS error, run it with a Python that has `certifi` (see
above), e.g. `python3 -m pytest -m live` instead of the venv one.

Offline tests never touch the network: `tgv_scraper.py` exposes `parse_day()`,
`parse_cinemas()`, `parse_regions()`, `parse_movie()`, `parse_showtime()` and
`parse_seat_status()` as pure functions over saved JSON, the same way
`gsc_scraper.py` exposes its `parse_*` functions over saved HTML.