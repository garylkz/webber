# webber

A library of website scrapers swinging on the web: two Malaysian cinema chains
built to be interchangeable, plus three unrelated scrapers. 

| Tool | Source | Python | TypeScript |
| --- | --- | :-: | :-: |
| `gsc_scraper` | server-rendered HTML + booking service | ✅ | ✅ |
| `tgv_scraper` | TGV JSON API | ✅ | ✅ |
| `gr_scraper` | shelf RSS feed | ✅ | ✅ |
| `meetup_scrapper` | public gql2 endpoint | ✅ | ✅ |
| `yt_rss` | channel RSS feed | ✅ | ✅ |
| `fourd_scraper` | Magnum 4D + Da Ma Cai | ✅ | ✅ |
| `northport_scraper` | Northport e-NP2 CAS-N4 enquiry (HTML fragments) | ✅ | ✅ |

Every platform is implemented in every language, and **the two implementations
are interchangeable**: same source, same CLI flags, same output. The parsers
produce byte-identical records from the same saved bodies, so a consumer reads
either one. The differences are the ones the language forces:

| | Python | TypeScript |
| --- | --- | --- |
| Run it | `python gsc_scraper.py` | `node gsc_scraper.ts` |
| Runtimes | Python 3.9+ | Node 22.6+ |
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

All seven are single-file. The two cinema scrapers and `yt_rss` are standard
library only. The Goodreads and Meetup scrapers came in from upstream and still
use `requests`, and `fourd_scraper` and `northport_scraper` use it too; their TypeScript ports use
`fetch` and need nothing.

**Every one of these has a TypeScript port under `typescript/`.** The ports
cover the same ground and emit the same JSON, with no dependencies and no build
step. See [TypeScript](#typescript) below.


> Further details in `docs/tvg_reader.md` and `docs/gsc_reader.md`; specs in `docs/*_spec.md`.


## Tests

```bash
cd python
.venv/bin/python -m pytest          # offline, runs against tests/fixtures/
.venv/bin/python -m pytest -m live  # also hits the real sites
```

347 offline tests and 47 live ones. The fixtures were captured from the live sources;
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
