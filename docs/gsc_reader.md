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

