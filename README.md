# webber

A library of website scrapers swinging on the web: two Malaysian cinema chains
built to be interchangeable, plus three unrelated scrapers. See [LICENSE](LICENSE)
for licensing.

| Tool | Source | Python | TypeScript |
| --- | --- | :-: | :-: |
| GSC Cinemas `gsc_scraper` | server-rendered HTML + booking service | ✅ | ✅ |
| TGV Cinemas `tgv_scraper` | TGV JSON API | ✅ | ✅ |
| Goodreads `gr_scraper` | shelf RSS feed | ✅ | ✅ |
| Meetup `mu_scrapper` | public gql2 endpoint | ✅ | ✅ |
| YouTube `yt_rss` | channel RSS feed | ✅ | ✅ |
| YouTube `yt_rss` | channel RSS feed | ✅ | ✅ |

Every platform is implemented in every language, and **the two implementations
are interchangeable**: same source, same CLI flags, same output. The parsers
produce byte-identical records from the same saved bodies, so a consumer reads
either one. The differences are the ones the language forces:

| | Python | TypeScript |
| --- | --- | --- |
| Run it | `python gsc_scraper.py` | `node gsc_scraper.ts` |
| Needs | 3.9+, `requests` for Goodreads and Meetup | Node 22.6+, nothing to install |
| Build step | none | none, types are stripped at load |
| Network calls | synchronous | a Promise, so `await` every call |
| Field names | `showtime.session_id` | `showtime.sessionId` |
| JSON output | `to_dict()` | `toDict()` |

The last two rows are the only ones that matter when porting code: fields are
camelCase in TypeScript and snake_case in Python, and `to_dict()` / `toDict()`
emit the **same snake_case keys** on both, which is what the shared contract
pins. Read records from the properties you named and serialise with either
method.

The two cinema chains are undocumented and take **no API key, no signature and no
session token**. GSC's is an ASP.NET service at `epaymentapi.gsc.com.my` that speaks
XML; TGV's is a JSON API at `api.tgv.com.my`. Both are called the way a browser
calls them, anonymously.

All five are single-file. The two cinema scrapers and `yt_rss` are standard
library only. The Goodreads and Meetup scrapers came in from upstream and still
use `requests`; their TypeScript ports use `fetch` and need nothing.

**Every one of these has a TypeScript port under `typescript/`.** The ports
cover the same ground and emit the same JSON, with no dependencies and no build
step. See [TypeScript](#typescript) below.

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
| `movies(on, cinema_ids)` | the day endpoint, once per cinema | `list[Movie]`, one record per film |
| `detail(slug, on)` | the day endpoint, once per cinema | the first of `movie()`'s hits |
| `cinema_experiences(cinema_id)` | the cinema directory | `list[str]`, hall formats |
| `day(cinema_id, on, seats)` | `POST /api/boxoffice/v1/moviesession_get` | `list[Movie]` with sessions |
| `schedule(on, seats)` | the same, once per cinema | `{cinema id: list[Movie]}` |
| `movie(slug, on)` | the same, once per cinema, filtered | `[(cinema id, Movie)]` |
| `seat_status(cinema_id, ids)` | `POST /api/boxoffice/v1/moviesession_getseatstatus` | `{session id: counts}` |
| `tickets(cinema_id, sid, area, date)` | `POST /api/boxoffice/v1/moviesession_gettickets` | `tuple[TicketType, ...]` for one session in one area |
| `session_prices(showtime)` | the same, once per area | the showtime, priced |
| `parse_tickets(payload)` | none, on saved JSON | `tuple[TicketType, ...]` |
| `filter_times(movies, after, before)` | none, in memory | Showtimes inside an `HH:MM` window |
| `filter_formats(movies, codes)` | none, in memory | Showtimes in the named hall formats |
| `by_city(cinemas, town)` | none, in memory | Outlets in one town |
| `filter_cinemas(cinemas, where)` | none, in memory | Outlets in one region or town |
| `request(path, payload)` | any of the above | raw decoded body |

`Movie` fields: `id`, `title`, `slug`, `url`, `genre`, `tags`, `rating`, `runtime`,
`duration`, `language`, `languages`, `subtitles`, `synopsis`, `director`, `cast`,
`distributor`, `release_date`, `trailer_url`, `trailer_id`, `poster_url`,
`banner_url`, `experiences`, `session_ids`, `cinema_id`, `cinema_name`,
`booking_url`, `booking_id`, `showtimes`.

`Cinema` fields: `id`, `name`, `slug`, `url`, `city`, `location` (region key),
`region` (label), `state`, `address`, `postcode`, `latitude`, `longitude`,
`telephone`, `parking`, `opening_hours`, `features`, `experiences`,
`description`.

`Showtime` fields: `session_id`, `movie_id`, `cinema_id`, `hall_id`, `date`,
`start` (an aware `datetime`), `time`, `ends`, `hall`, `experience`,
`experience_name`, `seat_types`, `seats_total`, `seats_used`, `seats_left`,
`sold_out`, `status`, plus
`price`, `price_range` and `ticket_types` with `--prices`.

Notes:

- **Times** come back as naive Malaysia wall clock (`2026-10-11T17:45:00`). They are
  attached to a fixed UTC+08:00 here, which is always correct because Malaysia has no
  daylight saving.
- **`cast` is a list.** The API stores it as one comma-joined string, which is
  split here rather than left for every caller to split again.
- **`rating` can be a placeholder.** An unreleased film's `rating` is `tba`, which
  now becomes `None` like every other placeholder. See
  [Shared contract (see specs)](#parity-with-tgv).
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

Every Python script has a TypeScript port under `typescript/`, same CLI flags and
same output. They run directly on Node 22.6+ — types are stripped at load, so
there is no build step and no dependencies.

```bash
cd typescript
node tgv_scraper.ts                          # today, every cinema
node tgv_scraper.ts BU0 --seats              # one cinema, seats left
node tgv_scraper.ts BU0 --seat-map --time 13:30
node tgv_scraper.ts --prices --per-person
node tgv_scraper.ts --json --movie other-mommy

node gsc_scraper.ts 235 2026-10-14 --prices   # the GSC equivalent
node gsc_scraper.ts --movie digger

node gr_scraper.ts 1123497 to-read             # Goodreads
node mu_scrapper.ts --query python             # Meetup
node yt_rss.ts "https://youtube.com/@handle"   # YouTube RSS
```

Imported, every network call is a promise:

```ts
import { day, sessionPrices } from './tgv_scraper.ts';

const films = await day('BU0', '2026-10-18');
const late = films.flatMap((f) => f.showtimes).find((s) => s.time === '21:45');
(await sessionPrices(late)).price;
```

**The ports emit the same JSON keys as the Python scripts**, all six record
types, and the parsers produce byte-identical output from the same saved bodies.
They have **no test suite of their own yet**; their parsers are pure functions
over the fixtures under `python/tests/fixtures/`, which is where a suite would
start.

Two notes on the ports. The two cinema scrapers are async where Python is sync,
so every network call returns a Promise, and the field names are camelCase
(`showtime.sessionId`, not `session_id`) while `toDict()` emits the snake_case
keys the contract pins. `gsc_scraper.ts` carries a small XML parser, because Node
has none built in and GSC's booking service speaks XML.

Optional type check, no build output, needs `typescript` installed:

```bash
cd typescript
npx tsc --noEmit *.ts
```

Function-by-function behaviour is in [`tgv_spec.md`](tgv_spec.md) and
[`gsc_spec.md`](gsc_spec.md).

---

# GSC Cinemas

`gsc_scraper.py` reads **two** sources, because GSC is two systems.

The website at `gsc.com.my` is a Nuxt app whose pages are server rendered. That
is where the editorial listings and a film's credits come from.

Everything behind the "Buy Tickets" button was long assumed to be out of reach.
It is not. The button points at a separate host, `epaymentwebapp.gsc.com.my`,
which is an Angular app calling a plain ASP.NET service at
`epaymentapi.gsc.com.my`. **The service takes no API key, no signature and no
session token**: the site's own interceptor only attaches an `Authorization`
header when a member is signed in, and browsing anonymously sends none at all.
It speaks XML, not JSON.

So this script has showtimes, ticket prices and seat maps for GSC too, and it
exposes the same functions as the TGV one.

```bash
cd python
python gsc_scraper.py                          # movies now showing
python gsc_scraper.py coming-soon
python gsc_scraper.py advance-sales
python gsc_scraper.py --all                    # all three website listings
python gsc_scraper.py --movies                 # the booking catalogue
python gsc_scraper.py 235                      # one cinema, today
python gsc_scraper.py 235 2026-10-14           # one cinema, one date
python gsc_scraper.py --cinemas                # every outlet
python gsc_scraper.py --cinemas --city "Kepong"
python gsc_scraper.py --regions                # the sales regions
python gsc_scraper.py --dates                  # dates that have sessions
python gsc_scraper.py --movie digger           # one film, every cinema
python gsc_scraper.py --detail munafik-melawan-iblis
python gsc_scraper.py --json                   # machine readable
python gsc_scraper.py 235 --prices             # add ticket prices
python gsc_scraper.py 235 --seats              # add seat availability
python gsc_scraper.py 235 --seat-map --time 10:50AM
python gsc_scraper.py --no-delay               # skip the polite pause
```

```
Kepong - Lotus`s (Kepong Village Mall) [235] 2026-10-14: 9 movies

  Avengers: Endgame Encore  (Action | 186 min | 13 | ENG)
      10:50  3  127 seats  RM13.00  (pair RM16.00)
  Once Upon A Time in The Middle East  (Drama | 141 min | 16 | MAN)
      11:00  1  164 seats  RM13.00  (pair RM16.00)
      16:25  1  164 seats  RM13.00  (pair RM16.00)
```

Narrow it down:

```bash
cd python
python gsc_scraper.py 235 2026-10-14 --from 18:00          # evening only
python gsc_scraper.py 235 2026-10-14 --until 18:00          # matinee only
python gsc_scraper.py 235 2026-10-14 --format 2D            # ordinary halls only
python gsc_scraper.py 235 2026-10-14 --format imax,4dx
python gsc_scraper.py 235 2026-10-14 --from 18:00 --prices
python gsc_scraper.py 235 2026-10-14 --seat-map --time 22:35
```

`--from` and `--until` are inclusive, accept `HH:MM` or `HHMM`, and anchor on the
business date so post-midnight shows on a late session day count as evening.
`--format` matches GSC's hall format codes and takes a comma-separated list.
Filters run **before** seats and prices are fetched, so a narrowed run never pays
for the sessions it discards.

Or import it:

```python
import gsc_scraper as gsc

gsc.listing("now-showing")                # the website's shelves
gsc.detail("munafik-melawan-iblis")        # synopsis, director, cast
gsc.movies()                              # the booking catalogue, one request
gsc.cinemas()                             # every outlet, with slugs and towns
gsc.regions()
gsc.business_dates()

# one cinema, one date
for film in gsc.day("235", "2026-10-14", prices=True):
    print(film.title, film.genre, film.rating, film.times)
    for showtime in film.showtimes:
        print("   ", showtime.time, showtime.hall, showtime.experience_name,
              showtime.seats_left, showtime.price)

# one date, every cinema
for cid, films in gsc.schedule(on="2026-10-14").items():
    print(cid, len(films), "movies")

# one film, every cinema it plays in
for cid, film in gsc.movie("digger"):
    print(cid, film.cinema_name, film.times)

# the seat map for one screening
plan = gsc.seat_plan("235", film.showtimes[0])
print(gsc.render_seat_plan(plan))
```

## GSC API

| Function | Endpoint | Returns |
| --- | --- | --- |
| `listing(name)` | website, `/movies/now-showing/` and two others | `list[Movie]`, the editorial shelves |
| `detail(slug)` | website, `/movie/<slug>/` | `Movie` with credits, backfilled from the catalogue |
| `cinemas()` | `GET epaymentwebapp/api/GetCSOAllLocationV2` | `list[Cinema]`, the outlet directory |
| `movies()` | `getEpaymentMovie_ParentChild` | `list[Movie]`, the whole catalogue, one request |
| `business_dates()` | the same | `list[str]`, ISO days |
| `day(cinema_id, on, ...)` | `getShowTimesByLocation_ParentChild_V2` | `list[Movie]` with sessions |
| `schedule(on, ...)` | the same, once per outlet | `{cinema id: list[Movie]}` |
| `movie(slug, on)` | `getShowTimesByMovie_ParentChild_V2` | `[(cinema id, Movie)]`, every cinema at once |
| `seat_status(cinema_id, showtimes)` | `getHallSeatStatus` | `{session id: counts}`, one request each |
| `seat_plan(cinema_id, showtime)` | the same | `SeatPlan`, the grid and the counts |
| `tickets(cinema_id, showtime)` | `getTicketPricingEpaySpecialV5` | `tuple[TicketType, ...]` for one session |
| `session_prices(showtime)` | the same | the showtime, priced |
| `cinema_experiences(cinema_id, on)` | the day payload's filter block | `{code: label}` |
| `fetch(path)` / `request(path, params)` | any of the above | raw HTML / raw body |

`Movie` fields: `id`, `title`, `slug`, `url`, `genre`, `tags`, `rating`,
`runtime`, `duration`, `language`, `languages`, `subtitles`, `synopsis`,
`director`, `cast`, `distributor`, `release_date`, `trailer_url`, `trailer_id`,
`poster_url`, `banner_url`, `experiences`, `session_ids`, `cinema_id`,
`cinema_name`, `booking_url`, `booking_id`, `showtimes`, plus `variants`,
`parent_id`, `dates` and `main_stars` as attributes.

`Cinema` fields: `id`, `name`, `slug`, `url`, `city`, `location`, `region`,
`state`, `address`, `postcode`, `latitude`, `longitude`, `telephone`, `parking`,
`opening_hours`, `features`, `experiences`, `description`, plus `town`.

`Showtime` fields: `session_id`, `movie_id`, `cinema_id`, `hall_id`, `date`,
`start` (an aware `datetime`), `time`, `ends`, `hall`, `experience`,
`experience_name`, `seat_types`, `seats_total`, `seats_used`, `seats_left`,
`sold_out`, `status`, `price`, `price_range`, `price_bundle`, `per_person`,
`sold_out_promos`, `ticket_types`.

Notes:

- **One film across the country is one request.** `getShowTimesByMovie...`
  returns every cinema at once, so `--movie` never sweeps the directory. On a
  popular film that is 53 cinemas in a single call.
- **Seats and prices have no batch form**, one request per session, which is why
  both are opt-in and why the filters run first.
- **A film is filed once per bookable format.** "Digger" and "(IMAX) Digger" are
  two `<child>` records under one parent. The parent is the `Movie`; each showtime
  carries the child it belongs to in `movie_id`, which is what prices are quoted
  against. A film playing in 2D and in IMAX at one cinema is still one `Movie`.
- **Titles carry their format.** The same film exists as "Avengers: Endgame
  Encore" and "(InfinityVision) Avengers:Endgame Encore", so a title search
  matches loosely and returns every variant that has sessions.
- **`slug` is derived from the title**, because the service has none. It follows
  the website's convention, which deletes punctuation rather than turning it
  into a separator, and reproduces every slug on the live now-showing page.
- **Three different seat counts arrive in one payload and disagree.**
  `seats_total` counts the grid, `seats_blocked` counts the seats it marks `D`,
  and `seats_left` is the service's own availability block, which is the figure
  the site shows and the one reported. The hall element's `maximumseats` is
  *not* the seat count, reads 12 for a 134-seat room, and is kept as
  `reported_total` only so the discrepancy stays visible.
- **`tax` is deliberately ignored**, exactly as TGV's `taxInCents` is. It does
  not reconcile with any Malaysian rate: 20.00% of the price at one Selangor
  outlet and about 9.09% at another, both in Selangor, where admission carries a
  flat 25% entertainment duty and movie tickets are exempt from service tax.
- **`Adult` and `Twin` are face value**; `Children`, `OKU`, `Senior` and
  `Student` are concessions. `standard` marks the face-value ones, and `Twin`
  seats two, so `price` quotes the single seat and `price_bundle` the pair.
- **The website has no JSON API.** Its `static/movie-lists.json` is not deployed
  and the Nuxt state bundle is 8.6 MB of obfuscated JavaScript. Server-rendered
  HTML is ~57 KB and far more stable, so that is what the listings and detail
  pages are read from.
- **The booking service has no synopsis, director or cast**, so the detail page
  is still where those come from. It leaves unpublished fields as `N/A` and an
  unreleased title's running time is one of them, so `detail()` backfills from
  the catalogue.

### Two directories, one outlet list

The booking service and the website share no id and almost no name:

| | Service | Website |
|---|---|---|
| Name | `Kepong - Lotus\`s (Kepong Village Mall)` | `GSC Lotus's Kepong` |
| Id | `235` | none, only the slug `gsc-lotuss-kepong` |
| Has | region, address, postcode, state | slug, town, amenities |

`cinemas()` returns the service's list, because showtimes can only be queried
against its ids. `cinemas(with_details=True)` joins the website's page onto it
to attach `slug`, `city` and `features`, matching on name. The matcher is
conservative: candidates are scored on token overlap, taken strongest-first
with each side used once, and anything below the threshold is left unjoined
rather than guessed. The town token is what separates the near-identical pairs
(`GSC IOI Mall Kulai` against `GSC IOI Mall Puchong`). On the current directory
it joins **50 of 50** outlets, each to the right one.

`find_cinema()` matches on id, slug or name, ignoring punctuation, so
`"GSC 1 Utama"` finds the outlet the service calls
`Petaling Jaya - 1 Utama (New Wing)`.

The directory holds the 50 GSC outlets that take online payment. **Aurum Theatre
and Velvet screen the same films on the same service** and do appear in schedule
results, but they are not in the directory, so `schedule()` cannot reach them by
id. `movie()` can, because it asks the service for one film rather than walking
the directory.

---

## Shared contract (see specs)

The two scrapers are built to be interchangeable. They expose the same function
names, take the same arguments for the same job, and emit **the same JSON keys**
for every record, so one consumer reads either site's output. The Python offline
suite pins those key sets, and the TypeScript port emits the same six.

| | GSC | TGV |
|---|---|---|
| Catalogue | `movies()`, one request | `movies()`, one request per cinema |
| One film | `movie()` → `[(cinema, Movie)]`, one request | `movie()` → `[(cinema, Movie)]`, a sweep |
| One film, one record | `detail()`, page + catalogue | `detail()`, no second call |
| Day | `day()` | `day()` |
| Sweep | `schedule()` | `schedule()` |
| Prices | `session_prices()`, one call per session | one call per session **per hall area** |
| Seats | `seat_plan()`, one call per session | `seat_status()`, one call per **batch** |
| Outlet formats | `cinema_experiences()`, off a day payload | on the outlet record, one request |
| Hall realignment | not needed | `align_by_number()` |
| Editorial shelves | `listing()` | none, and none needed |

Three differences are structural rather than a naming choice, and are asserted
in the test suite so nobody "fixes" them by accident:

- `listing()` exists only on GSC. Which shelf a film sits on is an editorial
  decision the website makes; TGV has no such category and no endpoint that
  reports one.
- `align_by_number()` exists only on TGV. TGV numbers each row independently,
  so a hall has to be realigned before it can be drawn. GSC's coordinates are
  already absolute.
- `align_by_number()` and its helpers being TGV-only is why GSC's seat renderer
  takes no `align` option.

`tgv_spec.md` and `gsc_spec.md` both open with the shared contract and list what
differs.

Function-by-function behaviour is in [`gsc_spec.md`](gsc_spec.md).

---

# certifi

The four dependency-free Python scripts work with nothing installed *provided your
Python has a CA store*. Some installs (Homebrew Python on macOS, slim containers)
ship without one, and every HTTPS call then fails with `CERTIFICATE_VERIFY_FAILED`.
If you hit that:

```bash
pip install certifi
```

They pick up `certifi`'s bundle automatically when it is importable. That is the only
situation where you need it, and it is not a hard requirement. `gr_scraper.py` and
`mu_scrapper.py` need `requests`, which bundles its own CA store, so they are
unaffected. The TypeScript implementations have no such issue: TLS trust comes
from the Node runtime.

# Tests

```bash
cd python
.venv/bin/python -m pytest          # offline, runs against tests/fixtures/
.venv/bin/python -m pytest -m live  # also hits the real sites
```

317 offline tests and 44 live ones. The fixtures were captured from the live sources;
if either site changes shape the offline suite is what fails first, which is
intentional.

`tests/test_gsc_api.py` is the parity suite. It checks that both scrapers expose
the same function names, that they emit the same JSON keys, and it **pins those
key sets** so a field added to one and not the other fails the build. It also
asserts the one-sided functions, with the reason, so nobody "fixes" a deliberate
asymmetry by accident.

The TypeScript implementations have no test suite yet. Their parsers are pure
functions over the fixtures under `python/tests/fixtures/`, mirroring the Python
ones, so those fixtures are the natural starting point. Their output has been
checked against the Python ones for all six record types and is byte-identical
from the same saved bodies.

If the live run fails with a TLS error, run it with a Python that has `certifi` (see
above), e.g. `python3 -m pytest -m live` instead of the venv one.

Offline tests never touch the network. Both modules expose their parsers as pure
functions over saved bodies: `tgv_scraper.py` has `parse_day()`, `parse_cinemas()`,
`parse_regions()`, `parse_movie()`, `parse_showtime()` and `parse_seat_status()`
over saved JSON, and `gsc_scraper.py` has `parse_movies()`, `parse_day()`,
`parse_movie_shows()`, `parse_seat_plan()`, `parse_tickets()`, `parse_cinemas()`
over saved XML and JSON, plus `parse_listing()`, `parse_detail()` and
`parse_cinemas_page()` over saved HTML.

A caveat worth knowing when writing live tests: a national run rolls over around
midnight Malaysia time, so an outlet queried in the first minutes of a new day
legitimately has nothing on it. Both live suites pick a date from the site's own
list of trading days rather than assuming today is one of them.