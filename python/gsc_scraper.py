#!/usr/bin/env python3
"""Showtimes, ticket prices and seat maps for GSC Cinemas Malaysia.

Single file, standard library only. No install, no dependencies.

    python gsc_scraper.py                          # movies now showing
    python gsc_scraper.py coming-soon
    python gsc_scraper.py advance-sales
    python gsc_scraper.py --all                    # every listing, counted
    python gsc_scraper.py --movies                 # the booking catalogue
    python gsc_scraper.py 235                      # one cinema, today
    python gsc_scraper.py 235 2026-10-14           # one cinema, one date
    python gsc_scraper.py --cinemas                # every outlet
    python gsc_scraper.py --dates                  # dates that have sessions
    python gsc_scraper.py --movie digger           # one film, every cinema
    python gsc_scraper.py --detail munafik-melawan-iblis
    python gsc_scraper.py --json                   # machine readable
    python gsc_scraper.py 235 --seats              # add seat availability
    python gsc_scraper.py 235 --prices             # add ticket prices
    python gsc_scraper.py 235 --seat-map --time 10:50AM
    python gsc_scraper.py --no-delay               # skip the polite pause

Also importable:

    import gsc_scraper as gsc
    gsc.listing("now-showing")
    gsc.movies()
    gsc.cinemas()
    gsc.regions()
    gsc.business_dates()
    gsc.day("235")
    gsc.schedule()
    gsc.movie("digger")
    gsc.detail("munafik-melawan-iblis")

How it works. gsc.com.my itself is a Nuxt app whose pages are server
rendered, which is what the editorial listings and the movie detail pages are
read from. Everything behind the "Buy Tickets" button lives on a separate host,
`epaymentwebapp.gsc.com.my`, which is an Angular app talking to a plain,
undocumented ASP.NET service at `epaymentapi.gsc.com.my/showtimews/service.asmx`.
That service takes no API key, no signature and no session token; the site's
own interceptor only adds an `Authorization` header when a member is signed in,
and browsing anonymously sends none at all. So this calls the same endpoints
the booking app calls, anonymously.

The service speaks XML rather than JSON, which is read here with
`xml.etree.ElementTree`.

Two name schemes meet in this file, and it is worth being explicit about which
is which:

- **The service** names an outlet by a numeric id (235 is GSC EkoCheras Mall)
  and calls it "Kuala Lumpur - EkoCheras Mall".
- **The website** names the same outlet "GSC EkoCheras Mall" and gives it a
  slug, `/cinema/gsc-ekocheras-mall`, plus a town and a list of amenities.

`cinemas()` returns the service's directory, which is what showtimes can be
queried against. `cinemas(with_details=True)` adds the website's slug, town and
amenities by joining the two on name, because the two never share an id.

Times: the service returns a business date plus a wall-clock time with no
offset, e.g. date 2026-10-14 and time 0015. These are Malaysia wall-clock and
are attached to a fixed UTC+08:00 here. Malaysia has no daylight saving, so the
offset is always +08:00.

Money: `tax` is deliberately ignored, for the same reason TGV's `taxInCents` is.
See `TicketType`.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta, timezone

# The public website.
SITE = "https://www.gsc.com.my"
# The booking app and the service behind it.
BOOKING = "https://epaymentwebapp.gsc.com.my"
API = "https://epaymentapi.gsc.com.my"
# Kept as BASE so code written against the old module keeps working.
BASE = SITE
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# Malaysia wall clock. No DST, so a fixed offset is exact.
MYT = timezone(timedelta(hours=8))

LISTINGS = {
    "now-showing": "/movies/now-showing/",
    "coming-soon": "/movies/coming-soon/",
    "advance-sales": "/movies/advance-sales/",
}
# Not a movie listing, so it lives outside LISTINGS: its parser is different.
CINEMA_PAGE = "/cinemas/gsc/"

ENDPOINTS = {
    # The booking catalogue: every film, every format variant, every date it
    # has sessions on.
    "movies": "/showtimews/service.asmx/getEpaymentMovie_ParentChild",
    # One film across every cinema, for one date.
    "movie_shows": "/showtimews/service.asmx/getShowTimesByMovie_ParentChild_V2",
    # One cinema, one date: the whole day.
    "day": "/showtimews/service.asmx/getShowTimesByLocation_ParentChild_V2",
    # The hall for one session: counts and the actual seat grid.
    "seat_plan": "/showtimews/service.asmx/getHallSeatStatus",
    # Ticket types for one session.
    "tickets": "/showtimews/service.asmx/getTicketPricingEpaySpecialV5",
    # The outlet directory, reached through the booking app's own proxy.
    "cinemas": "/api/GetCSOAllLocationV2",
}

# The outlet directory comes back as JSON through the app's proxy, so it gets
# its own base rather than the XML service root.
CINEMA_ENDPOINT = f"{BOOKING}{ENDPOINTS['cinemas']}"

# GSC uses these in place of real data, on the website's own pages.
PLACEHOLDERS = {"N/A", "NA", "-", "TBC", "TBA"}

# A face-value admission ticket is what an ordinary customer pays, and it is
# the figure `--prices` quotes. Everything else on a session (Children, OKU,
# Senior, Student) is a concession. Twin is face value too, but it seats two, so
# it counts as a bundle rather than a single seat.
STANDARD_TICKETS = {"ADULT", "TWIN"}
# The brand token the website prefixes its outlet names with. It says nothing
# about which outlet is meant, so it is dropped when the two directories are
# compared.
BRAND_NOISE = {"gsc", "aurum", "velvet"}

# The service reports a seat's state as a bare letter.
SEAT_FREE = "A"
SEAT_BOOKED = "B"
SEAT_BLOCKED = "D"
SEAT_UNKNOWN = "?"

# Town abbreviations GSC prints on the website where the service spells the
# town out. Used only when joining the two directories.
TOWN_ABBREVIATIONS = {
    "jb": ("johor", "bahru"),
    "kl": ("kuala", "lumpur"),
    "kk": ("kota", "kinabalu"),
    "bkt": ("bukit", "tinggi"),
}
# Words that carry no identity when comparing two outlet names.
NAME_NOISE = {"the", "mall", "shopping", "city", "of", "formerly", "cinema"}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def clean(value: str | None) -> str | None:
    """Strip whitespace and drop GSC's placeholder text.

    The website writes `N/A`, `NA`, `-`, `TBC` and `TBA` where it has no real
    value. Those become None rather than surviving as literal strings a caller
    has to filter. The service never sends placeholders, so applying the same
    rule to both is harmless.
    """
    if value is None:
        return None
    value = value.strip()
    return None if not value or value.upper() in PLACEHOLDERS else value


def split_codes(value: str | None) -> tuple[str, ...]:
    """'ENG,BM,CHI' -> ('ENG', 'BM', 'CHI'). Empty when there is nothing."""
    if not value:
        return ()
    parts = (clean(part) for part in value.split(","))
    return tuple(part for part in parts if part)


def as_list(value) -> list[str]:
    """Coerce a string, a list or None into a clean list of strings.

    The website is inconsistent about whether a multi-value field arrives as
    one comma-joined string or as a list, and both shapes show up on the same
    page, so every list field goes through here.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def strip_html(value: str | None) -> str | None:
    """Collapse a value that may hold HTML into plain text."""
    if not value:
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    text = (
        text.replace("&hellip;", "...")
        .replace("&amp;", "&")
        .replace("&nbsp;", " ")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return clean(re.sub(r"\s+", " ", text))


def slugify(title: str) -> str:
    """'(Rerun) La La Land' -> 'rerun-la-la-land'.

    The service has no slug of its own, so it is derived from the title using
    the website's convention, which is not the obvious one: punctuation is
    deleted rather than turned into a separator, so `Khong-Khaek 2` is
    `khongkhaek-2` while `Spider-Man: Brand New Day` is `spiderman-brand-new-day`
    and `Avengers:Endgame Encore` is `avengersendgame-encore`. Only runs of
    whitespace become a dash. Checked against every film on the saved
    now-showing page, this reproduces all of them.
    """
    text = "".join(ch for ch in title if ch.isalnum() or ch.isspace())
    slug = re.sub(r"\s+", "-", text.strip().casefold()).strip("-")
    return slug or "untitled"


def duration_minutes(value: str | None) -> int | None:
    """'1 hr 45 mins' -> 105. None when the runtime is unknown.

    Takes either shape the two sources use: the service's bare minute count
    ("105") or the website's prose ("1 hr 45 mins").
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text) or None
    match = re.search(r"(?:(\d+)\s*h(?:rs?)?\s*)?(?:(\d+)\s*m(?:ins?)?)?", text, re.I)
    if not match or not match.groups():
        return None
    total = int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
    return total or None


def loose_title(title: str) -> str:
    """'(InfinityVision) Avengers:Endgame Encore' -> 'avengersendgameencore'.

    GSC marks a film's format in the title as often as in the data, so one film
    exists under several titles that differ only by a bracketed prefix and
    punctuation. Searching on this form finds the film whichever of them the
    caller typed.
    """
    text = re.sub(r"\([^)]*\)", " ", title)
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def experience_label(code: str | None) -> str | None:
    """'IMAX' -> 'IMAX', '2D' -> 'Digital 2D'.

    The service sends a bare code in `type` and the human label in `type_desc`
    as "CODE - Label", so the label is the part after the dash. An unknown code
    still renders rather than being dropped.
    """
    if not code:
        return None
    code = code.strip()
    if not code:
        return None
    _, _, label = code.partition(" - ")
    return clean(label) or code


def session_start(on: str | None, clock: str | None) -> datetime | None:
    """Business date plus a bare wall clock -> an aware datetime.

    The service splits the two: `date="2026-10-14" time="0015"`, where the time
    is HHMM with no colon and no am/pm. Both halves are needed, because the
    date half is the calendar day while the business date may be the day before.
    """
    if not on or not clock:
        return None
    digits = re.sub(r"\D", "", clock)
    if len(digits) < 3:
        return None
    hour, minute = int(digits[:-2]), int(digits[-2:])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    try:
        day = Date.fromisoformat(on)
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=MYT)


def parse_clock(value: str | None) -> tuple[int, int] | None:
    """'18:00' or '1800' -> (18, 0). None passes straight through."""
    if value is None:
        return None
    match = re.fullmatch(r"(\d{1,2}):?(\d{2})", value.strip())
    if not match:
        raise SystemExit(f"bad time {value!r}, expected HH:MM")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise SystemExit(f"bad time {value!r}, expected HH:MM")
    return hour, minute


def normalise_date(value: str | Date | None) -> str:
    """Accept 'YYYY-MM-DD', a date, or nothing. Returns 'YYYY-MM-DD'."""
    if value is None:
        return datetime.now(MYT).date().isoformat()
    if isinstance(value, Date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return datetime.now(MYT).date().isoformat()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise SystemExit(f"bad date {value!r}, expected YYYY-MM-DD")


def looks_like_date(value: str | None) -> bool:
    """True when a positional argument is a date rather than a cinema name.

    The CLI takes an optional cinema and an optional date, and argparse cannot
    tell them apart by position alone. Deciding by shape works because every
    outlet id and name contains a letter and no date does.
    """
    if not value or not value.strip():
        return False
    try:
        normalise_date(value)
    except SystemExit:
        return False
    return True


def looks_like_clock(value: str | None) -> bool:
    """True when a `--time` value is a time of day.

    The service prints `timestr` in 12-hour form ("10:50AM") while the CLI
    documents 24-hour `HH:MM`, so both have to be recognised.
    """
    if not value:
        return False
    text = value.strip().upper().replace(" ", "")
    text = re.sub(r"(AM|PM)$", "", text)
    return bool(re.fullmatch(r"\d{1,2}:?\d{2}", text))


def clock_candidates(value: str) -> tuple[str, ...]:
    """Every spelling of one `--time` value worth matching against `timestr`.

    The service prints `timestr` in 12-hour form ("10:50AM") while the CLI
    documents 24-hour `HH:MM`, so both renderings are produced whichever side
    the user is reading times off.
    """
    text = value.strip().upper().replace(" ", "")
    found = [value.strip()]
    suffix = ""
    if text.endswith("AM") or text.endswith("PM"):
        text, suffix = text[:-2], text[-2:]
    digits = re.sub(r"\D", "", text)
    if len(digits) not in (3, 4):
        return tuple(found)
    hour, minute = int(digits[:-2]), int(digits[-2:])
    if suffix == "PM" and 1 <= hour <= 11:
        hour += 12
    if suffix == "AM" and hour == 12:
        hour = 0
    twelve = hour % 12 or 12
    found.append(f"{hour:02d}:{minute:02d}")
    found.append(f"{twelve}:{minute:02d}AM")
    found.append(f"{twelve}:{minute:02d} AM")
    found.append(f"{twelve}:{minute:02d}PM")
    found.append(f"{twelve}:{minute:02d} PM")
    return tuple(dict.fromkeys(f for f in found if f))


# --------------------------------------------------------------------------
# network
# --------------------------------------------------------------------------


def _ssl() -> ssl.SSLContext:
    """TLS context, using certifi's CA bundle if the system lacks one.

    Some Python installs (homebrew on macOS, slim containers) ship without a
    usable cert store, which makes every HTTPS call fail. certifi is used when
    it happens to be importable, but it is not required.
    """
    context = ssl.create_default_context()
    try:
        import certifi
    except ImportError:
        return context
    try:
        context.load_verify_locations(certifi.where())
    except OSError:
        pass
    return context


class Throttle:
    """Spaces requests out so a pool of workers still hits the site politely.

    Callers take a slot; the wait happens outside the lock so workers stay
    parallel, but the global rate stays at one request per `delay` seconds.
    """

    def __init__(self, delay: float = 0.25) -> None:
        self.delay = max(0.0, delay)
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def wait(self) -> None:
        if not self.delay:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.delay
        time.sleep(max(0.0, slot - now))


def _decode(response, path: str) -> str:
    body = response.read()
    if response.headers.get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    return body.decode("utf-8", "replace")


def fetch(path: str, timeout: float = 20.0, delay: float = 0.5, throttle=None) -> str:
    """GET a page from gsc.com.my and return the HTML.

    This is the website, not the booking service. Used for the editorial
    listings, the movie detail pages and the public outlet directory.
    """
    if throttle is not None:
        throttle.wait()
    elif delay:
        time.sleep(delay)  # be polite, this is someone's live site
    request = urllib.request.Request(
        f"{SITE}{path}",
        headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl()) as response:
            html = _decode(response, path)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise SystemExit(f"not found: {path}") from exc
        raise
    except ssl.SSLCertVerificationError as exc:
        raise SystemExit(
            "TLS verification failed: this Python has no usable CA store.\n"
            "  fix: pip install certifi"
        ) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            raise SystemExit(
                f"TLS error talking to gsc.com.my: {exc.reason}\n  fix: pip install certifi"
            ) from exc
        raise SystemExit(f"network error talking to gsc.com.my: {exc.reason}") from exc
    # Unknown pages sometimes come back 200 with a meta refresh to /not-found.
    if "not-found" in html[:400]:
        raise SystemExit(f"not found: {path}")
    return html


def request(path: str, params: dict | None = None, timeout: float = 25.0,
            throttle: "Throttle | None" = None) -> str:
    """Call one booking-service endpoint and return the raw body.

    The body is XML for everything under `showtimews` and JSON for the outlet
    directory, which the booking app proxies. There is nothing to send but the
    query: no key, no signature, no session token. The Origin and Referer the
    booking app sends are reproduced because they cost nothing and keep the
    request indistinguishable from normal browsing.
    """
    if throttle is not None:
        throttle.wait()
    if path.startswith("/api/") or path.startswith("http"):
        url = path if path.startswith("http") else f"{BOOKING}{path}"
        accept = "application/json, text/plain, */*"
    else:
        base = f"{API}{path}" if path.startswith("/") else path
        url = f"{base}?{urllib.parse.urlencode(params or {})}" if params is not None else base
        accept = "text/xml, application/xml, */*"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": accept,
            "Accept-Language": "en-MY,en;q=0.9",
            "Origin": BOOKING,
            "Referer": f"{BOOKING}/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl()) as response:
            return _decode(response, url)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"HTTP {exc.code} from {path}: {detail}") from exc
    except ssl.SSLCertVerificationError as exc:
        raise SystemExit(
            "TLS verification failed: this Python has no usable CA store.\n"
            "  fix: pip install certifi"
        ) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            raise SystemExit(f"TLS error talking to gsc.com.my: {exc.reason}") from exc
        raise SystemExit(f"network error talking to gsc.com.my: {exc.reason}") from exc


def xml_of(body: str, path: str) -> ET.Element:
    """Parse a service body, or say plainly that it was not XML."""
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise SystemExit(f"malformed XML from {path}: {exc}") from exc


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------


@dataclass
class Cinema:
    """One GSC outlet.

    `id` is the numeric id the booking service knows the outlet by, and is what
    every schedule query needs. `name` is the service's own name for it, which
    is prefixed with the town ("Kuala Lumpur - EkoCheras Mall").

    `slug`, `city` and `features` come from the website instead, and are only
    filled in when `cinemas(with_details=True)` could join the two directories
    on name. They are None otherwise rather than guessed.
    """

    id: str
    name: str
    slug: str | None = None
    city: str | None = None
    location: str | None = None  # region key, lower-cased
    region: str | None = None
    state: str | None = None
    address: str | None = None
    postcode: str | None = None
    latitude: str | None = None
    longitude: str | None = None
    telephone: str | None = None
    parking: str | None = None
    opening_hours: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    experiences: tuple[str, ...] = ()
    description: str | None = None

    @property
    def url(self) -> str:
        if self.slug:
            return f"{SITE}/cinema/{self.slug}/"
        return f"{SITE}{CINEMA_PAGE}"

    @property
    def town(self) -> str | None:
        """The town part of the service's name, e.g. 'Kepong'.

        This is always available and is not the same field as `city`: the
        website files some outlets under a state rather than a town, so `city`
        is whatever GSC labels and may say "Sarawak" where `town` says
        "Bintulu".
        """
        head, sep, _ = self.name.partition(" - ")
        return clean(head) if sep else (self.city if self.city else None)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "url": self.url,
            "city": self.city,
            "location": self.location,
            "region": self.region,
            "state": self.state,
            "address": self.address,
            "postcode": self.postcode,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "telephone": self.telephone,
            "parking": self.parking,
            "opening_hours": list(self.opening_hours),
            "features": list(self.features),
            "experiences": list(self.experiences),
            "description": self.description,
        }


@dataclass
class TicketType:
    """One kind of ticket GSC sells for a session: Adult, OKU, Twin...

    `price` is the advertised ticket price, read from the service's `price`
    field.

    The second money field, `tax`, is deliberately **not** used, for the same
    reason TGV's `taxInCents` is not. It does not reconcile with any Malaysian
    rate: it works out to exactly 20.00% of the ticket at one Selangor outlet
    and about 9.09% at another, both in Selangor, where cinema admission
    carries a flat 25% entertainment duty and movie tickets are exempt from
    service tax. Adding it inflated every price and made identical tickets
    read differently between outlets.
    """

    code: str
    description: str
    price: float | None = None
    # How many people this ticket admits. Twin seats two.
    seats: int = 1
    package: bool = False
    # Which seat category the price belongs to, matching the seat map.
    area_code: str | None = None
    sold_out: bool = False
    child_only: bool = False
    allocated_seating: bool = False
    # A plain adult seat sold at face value. Everything else on a session is a
    # concession or a bundle, and `price` on a showtime ignores those.
    standard: bool = False

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "description": self.description,
            "price": self.price,
            "seats": self.seats,
            "package": self.package,
            "area_code": self.area_code,
            "sold_out": self.sold_out,
            "child_only": self.child_only,
            "allocated_seating": self.allocated_seating,
            "standard": self.standard,
        }


@dataclass
class Showtime:
    """One screening of one film in one hall."""

    session_id: str
    movie_id: str
    cinema_id: str
    start: datetime | None
    hall: str | None = None
    hall_id: str | None = None
    business_date: str | None = None
    experience: str | None = None
    # The service's "CODE - Label" rendering of the format, e.g.
    # '2D - Digital 2D'. `experience` alone is only the code.
    experience_desc: str | None = None
    seat_types: tuple[str, ...] = ()
    status: int | None = None
    seats_total: int | None = None
    seats_used: int | None = None
    # --prices only, one entry per ticket type on sale for this session
    ticket_types: tuple[TicketType, ...] = ()
    # Filled in by the caller so ends() works without a back reference.
    runtime: int | None = None
    # The service's own 12-hour rendering, e.g. '10:50AM'. Not in to_dict;
    # `time` is the 24-hour form and is what the rest of the API speaks.
    time_text: str | None = None

    @property
    def experience_name(self) -> str | None:
        """The format as a display name, e.g. 'Digital 2D' for '2D'."""
        return experience_label(self.experience_desc or self.experience)

    @property
    def time(self) -> str | None:
        return self.start.strftime("%H:%M") if self.start else None

    @property
    def ends(self) -> datetime | None:
        """End time, if this Showtime knows the film's runtime."""
        if self.start is None or self.runtime is None:
            return None
        return self.start + timedelta(minutes=self.runtime)

    @property
    def seats_left(self) -> int | None:
        if self.seats_total is None or self.seats_used is None:
            return None
        return max(0, self.seats_total - self.seats_used)

    @property
    def sold_out(self) -> bool:
        left = self.seats_left
        return left is not None and left == 0

    @property
    def single_seat_price(self) -> float | None:
        """Cheapest standard ticket that admits exactly one person.

        This is the figure to compare across cinemas: it ignores Twin, which
        seats two, and it ignores concession tickets.
        """
        prices = [
            t.price for t in self.ticket_types
            if t.price is not None and t.standard and t.seats == 1
        ]
        return min(prices) if prices else None

    @property
    def is_bundle_only(self) -> bool:
        """True when the only standard tickets on sale seat more than one."""
        standard = [t for t in self.ticket_types if t.standard]
        return bool(standard) and all(t.seats > 1 for t in standard)

    @property
    def sold_out_promos(self) -> tuple[str, ...]:
        """Promotions the service has marked as no longer obtainable today."""
        return tuple(t.description for t in self.ticket_types if t.sold_out)

    @property
    def price(self) -> float | None:
        """Cheapest standard single-seat ticket price, in ringgit.

        A bundle covers two people, so quoting one here would compare a pair
        against a single seat. Concessions are excluded too. Without
        --prices this is None.
        """
        return self.single_seat_price or min(
            (t.price for t in self.ticket_types if t.price is not None and t.standard),
            default=None,
        )

    @property
    def price_bundle(self) -> float | None:
        """Cheapest multi-seat bundle price, if the hall sells one."""
        prices = [
            t.price for t in self.ticket_types
            if t.price is not None and t.standard and t.seats > 1
        ]
        return min(prices) if prices else None

    @property
    def price_range(self) -> str | None:
        """'RM25.00 - RM35.00' when the hall sells more than one single price."""
        prices = sorted({
            t.price for t in self.ticket_types
            if t.price is not None and t.standard and t.seats == 1
        })
        if not prices:
            return None
        if len(prices) == 1:
            return f"RM{prices[0]:.2f}"
        return f"RM{prices[0]:.2f} - RM{prices[-1]:.2f}"

    @property
    def per_person(self) -> float | None:
        """Cheapest standard price divided by how many people a ticket seats.

        Comparing the raw ticket price across cinemas can compare two people
        against one, so compare this instead.
        """
        prices = [
            t.price / t.seats
            for t in self.ticket_types
            if t.price is not None and t.standard and t.seats
        ]
        return min(prices) if prices else None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "movie_id": self.movie_id,
            "cinema_id": self.cinema_id,
            "hall_id": self.hall_id,
            "date": self.business_date,
            "start": self.start.isoformat() if self.start else None,
            "time": self.time,
            "ends": self.ends.isoformat() if self.ends else None,
            "hall": self.hall,
            "experience": self.experience,
            "experience_name": self.experience_name,
            "seat_types": list(self.seat_types),
            "seats_total": self.seats_total,
            "seats_used": self.seats_used,
            "seats_left": self.seats_left,
            "sold_out": self.sold_out,
            "status": self.status,
            "price": self.price,
            "price_range": self.price_range,
            "price_bundle": self.price_bundle,
            "per_person": self.per_person,
            "sold_out_promos": list(self.sold_out_promos),
            "ticket_types": [t.to_dict() for t in self.ticket_types],
        }


@dataclass
class Movie:
    """A film.

    GSC files a film under a parent record and then one child per bookable
    variant, e.g. "Digger" and "(IMAX) Digger" are separate children of one
    parent. `id` is the parent, which is the film; each showtime carries the
    child it belongs to in `Showtime.movie_id`, which is what pricing is
    quoted against.
    """

    id: str = ""
    title: str = ""
    slug: str | None = None
    genre: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    rating: str | None = None
    runtime: int | None = None
    language: str | None = None
    subtitles: tuple[str, ...] = ()
    synopsis: str | None = None
    director: str | None = None
    cast: tuple[str, ...] = ()
    distributor: str | None = None
    release_date: str | None = None
    trailer_url: str | None = None
    poster_url: str | None = None
    banner_url: str | None = None
    experiences: tuple[str, ...] = ()
    session_ids: tuple[str, ...] = ()
    cinema_id: str | None = None
    cinema_name: str | None = None
    showtimes: tuple[Showtime, ...] = ()
    # The bookable variant codes, one per format (2D, IMAX, 4DX...).
    variants: tuple[str, ...] = ()
    # The service's parent and child codes, spelled out.
    parent_id: str | None = None
    # The runtime as the website writes it, e.g. '1 hr 45 mins'.
    duration: str | None = None
    booking_url: str | None = None
    booking_id: int | None = None
    # Set by the website's detail page only.
    still_url: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"{SITE}/movie/{self.slug}/" if self.slug else SITE

    @property
    def languages(self) -> tuple[str, ...]:
        """Audio codes as a tuple. GSC joins several with commas."""
        return split_codes(self.language)

    @property
    def trailer_id(self) -> str | None:
        """YouTube id from whatever trailer URL shape was stored."""
        if not self.trailer_url:
            return None
        match = re.search(r"(?:embed/|v=|youtu\.be/|/shorts/)([\w-]{6,})", self.trailer_url)
        return match.group(1) if match else None

    @property
    def times(self) -> tuple[str, ...]:
        return tuple(t for t in (s.time for s in self.showtimes) if t)

    @property
    def dates(self) -> tuple[str, ...]:
        """Every date this film has sessions on.

        Only the catalogue knows these: one film carries its whole run on the
        same payload, so this costs nothing extra. `business_dates()` is the
        union across every film.
        """
        return tuple(self.extra.get("dates", ()))

    @property
    def main_stars(self) -> tuple[str, ...]:
        """Alias of `cast`, the name the website's detail page used."""
        return self.cast

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "slug": self.slug,
            "url": self.url,
            "genre": list(self.genre),
            "tags": list(self.tags),
            "rating": self.rating,
            "runtime": self.runtime,
            "duration": self.duration,
            "language": self.language,
            "languages": list(self.languages),
            "subtitles": list(self.subtitles),
            "synopsis": self.synopsis,
            "director": self.director,
            "cast": list(self.cast),
            "distributor": self.distributor,
            "release_date": self.release_date,
            "trailer_url": self.trailer_url,
            "trailer_id": self.trailer_id,
            "poster_url": self.poster_url,
            "banner_url": self.banner_url,
            "experiences": list(self.experiences),
            "session_ids": list(self.session_ids),
            "cinema_id": self.cinema_id,
            "cinema_name": self.cinema_name,
            "booking_url": self.booking_url,
            "booking_id": self.booking_id,
            "showtimes": [s.to_dict() for s in self.showtimes],
        }


@dataclass
class Seat:
    """One seat in a hall.

    `row` and `column` are the service's own grid coordinates, which are
    absolute across the hall: an aisle is simply a column with no seat in it.
    `number` is the printed label the customer reads, and `id` is the booking
    identifier the service issues.
    """

    id: str
    row: int
    column: int
    number: str | None = None
    status: str = SEAT_FREE
    category: int | None = None
    seat_type: str | None = None
    priority: int = 0

    @property
    def state(self) -> str:
        """Normalised state: 'A' free, 'B' booked, 'D' blocked, '?' unknown."""
        return self.status if self.status in SEAT_MARKS else SEAT_UNKNOWN

    @property
    def free(self) -> bool:
        return self.state == SEAT_FREE

    @property
    def booked(self) -> bool:
        return self.state == SEAT_BOOKED

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "number": self.number,
            "row": self.row,
            "column": self.column,
            "status": self.status,
            "state": self.state,
            "category": self.category,
            "seat_type": self.seat_type,
            "free": self.free,
            "booked": self.booked,
        }


@dataclass
class SeatRow:
    """One row of a hall area. `index` is the service's grid row."""

    index: int
    name: str | None = None
    seats: tuple[Seat, ...] = ()

    @property
    def columns(self) -> tuple[int, ...]:
        return tuple(sorted(seat.column for seat in self.seats))

    @property
    def free_seats(self) -> tuple[Seat, ...]:
        return tuple(s for s in self.seats if s.free)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "seats": [s.to_dict() for s in self.seats],
        }


@dataclass
class SeatArea:
    """One part of a hall, e.g. a section.

    GSC divides a hall into sections rather than priced areas, but the shape is
    the same as TGV's: a group of rows that can be drawn against one ruler.
    """

    number: int
    code: str | None = None
    description: str | None = None
    seat_count: int = 0
    row_count: int = 0
    column_count: int = 0
    rows: tuple[SeatRow, ...] = ()

    @property
    def label(self) -> str:
        return self.description or self.code or f"section {self.number}"

    @property
    def occupied_rows(self) -> tuple[SeatRow, ...]:
        """Rows that actually hold seats, in service order."""
        return tuple(row for row in self.rows if row.seats)

    @property
    def seats(self) -> tuple[Seat, ...]:
        return tuple(seat for row in self.rows for seat in row.seats)

    @property
    def booked(self) -> tuple[Seat, ...]:
        return tuple(s for s in self.seats if s.booked)

    @property
    def free(self) -> tuple[Seat, ...]:
        return tuple(s for s in self.seats if s.free)

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "code": self.code,
            "description": self.description,
            "seat_count": self.seat_count,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "rows": [r.to_dict() for r in self.rows],
        }


@dataclass
class SeatPlan:
    """The whole hall for one session, with the screen's position.

    The counts are the service's own availability figure, taken from the hall
    booking block rather than from counting seat states. That block is the
    number a customer-facing "seats left" would show, and counting the grid
    does not reproduce it: on the captured hall the states explain 6 of the 7
    seats the service holds back. `blocked` is the count of seats the grid does
    mark as unavailable, and `reported_total`/`reported_booked` keep the hall
    element's own numbers, which mean something else entirely and are kept
    only so the discrepancy stays visible.
    """

    cinema_id: str | None = None
    session_id: str | None = None
    hall_id: str | None = None
    hall_name: str | None = None
    areas: tuple[SeatArea, ...] = ()
    boundary_left: float = 0.0
    boundary_right: float = 0.0
    screen_start: float = 0.0
    screen_width: float = 0.0
    seats_total: int | None = None
    seats_booked: int | None = None
    seats_blocked: int | None = None
    # Seat category name -> seats still available, from the hall booking block.
    available: dict = field(default_factory=dict)
    # Seat category id -> {name, seats_taken}, i.e. how many seats one ticket
    # of that category occupies.
    categories: dict = field(default_factory=dict)
    reported_total: int | None = None
    reported_booked: int | None = None
    # The hall's formats, one line each, when the service describes them.
    experiences: tuple[str, ...] = ()

    @property
    def first_column(self) -> int:
        """Leftmost column any area uses, so one ruler fits every grid."""
        columns = [seat.column for area in self.areas for seat in area.seats]
        return min(columns) if columns else 0

    @property
    def last_column(self) -> int:
        columns = [seat.column for area in self.areas for seat in area.seats]
        return max(columns) if columns else 0

    @property
    def booked(self) -> tuple[Seat, ...]:
        return tuple(s for area in self.areas for s in area.booked)

    @property
    def free(self) -> tuple[Seat, ...]:
        return tuple(s for area in self.areas for s in area.free)

    @property
    def seats(self) -> tuple[Seat, ...]:
        return tuple(seat for area in self.areas for seat in area.seats)

    @property
    def seats_left(self) -> int | None:
        """Seats the service will still sell for this session."""
        if not self.available:
            return None
        return sum(self.available.values())

    @property
    def hall_rows(self) -> tuple[tuple[SeatArea, SeatRow], ...]:
        """Every row of the hall, screen end first, paired with its section.

        GSC numbers rows from the back of the hall towards the screen: the
        indices ascend while the row letters descend, ending on `A` at the
        highest index. Descending index therefore puts the screen end first.
        Ordering by the raw index draws the hall upside down.
        """
        rows = [
            (area, row)
            for area in self.areas
            for row in area.occupied_rows
        ]
        return tuple(sorted(rows, key=lambda pair: pair[1].index, reverse=True))

    def to_dict(self) -> dict:
        return {
            "cinema_id": self.cinema_id,
            "session_id": self.session_id,
            "hall_id": self.hall_id,
            "hall_name": self.hall_name,
            "seats_total": self.seats_total,
            "seats_left": self.seats_left,
            "seats_booked": self.seats_booked,
            "seats_blocked": self.seats_blocked,
            "available": dict(self.available),
            "areas": [a.to_dict() for a in self.areas],
        }


SEAT_MARKS = {SEAT_FREE: None, SEAT_BOOKED: "X", SEAT_BLOCKED: "-"}


def seat_number(seat: Seat) -> str | None:
    """A seat's printed number.

    The other scraper needs this to realign a hall whose rows are numbered
    independently; GSC's coordinates are already absolute so nothing here calls
    it, but both modules expose it so a caller can ask either the same question.
    """
    if seat.number is not None:
        return seat.number
    text = re.sub(r"\D", "", str(seat.id))
    return text or None


# --------------------------------------------------------------------------
# parsers: every one is a pure function over a saved body
# --------------------------------------------------------------------------


def parse_cinemas(payload, directory: list | None = None) -> list[Cinema]:
    """The outlet directory payload -> Cinema records, sorted by name.

    `directory` is the optional list parsed from the website's outlet page. It
    is joined on name to attach `slug`, `city` and `features`; see
    `join_directories`.
    """
    records = payload.get("Response", {}).get("Body", {}).get("Location", []) if isinstance(
        payload, dict
    ) else payload or []

    found: list[Cinema] = []
    seen: set[str] = set()
    for record in records:
        cid = clean(record.get("OutletId"))
        name = clean(record.get("OutletName"))
        # The service sends a "Please Select Cinema" placeholder row twice, once
        # per region, which is not an outlet.
        if not cid or not name or cid in seen or name.casefold().startswith("please select"):
            continue
        seen.add(cid)
        region = clean(record.get("Region"))
        address = clean(record.get("Address"))
        postcode = None
        state = clean(record.get("StateDesc"))
        if address:
            tail = address.rsplit(",", 1)
            if len(tail) == 2 and re.fullmatch(r"\d{5}", tail[1].strip()):
                postcode = tail[1].strip()
        found.append(
            Cinema(
                id=cid,
                name=name,
                location=region.casefold() if region else None,
                region=region,
                state=state,
                address=address,
                postcode=postcode,
            )
        )
    found.sort(key=lambda c: c.name)
    if directory:
        join_directories(found, directory)
    return found


def _name_tokens(value: str, expand: bool = False) -> set[str]:
    """Words that identify an outlet, for joining the two directories.

    Glued words are split first: the service writes `TropicanaGardens` where
    the website writes `Tropicana Gardens`, and `MyTOWN` where the website
    writes `MyTown`, so both sides are cut the same way before comparing. The
    brand token is dropped, since the website prefixes every name with it and
    the service prefixes none.
    """
    text = value.replace("`", "'").replace("’", "'")
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    words = [w for w in re.findall(r"[a-z0-9]+", text.casefold()) if w not in NAME_NOISE and len(w) > 1]
    words = [w for w in words if w not in BRAND_NOISE]
    if expand:
        words = [t for w in words for t in TOWN_ABBREVIATIONS.get(w, [w])]
    return set(words)


def _name_parts(value: str) -> list[tuple[str, str]]:
    """Split 'Johor Bahru - AEON Dato Onn' into (town, site) candidates.

    The service is inconsistent about spacing around the dash, so both splits
    are tried and the caller takes whichever matches better.
    """
    parts: list[tuple[str, str]] = []
    for pattern in (r"^(.*?)\s+-\s+(.+)$", r"^(.*?)\s*-\s*(.+)$"):
        match = re.match(pattern, value)
        if match:
            parts.append((match.group(1), match.group(2)))
    return parts or [("", value)]


def _name_score(site_name: str, service_name: str) -> float:
    """How well a website outlet name and a service outlet name agree.

    1.0 means one name is a subset of the other's tokens, plus a bonus when the
    town agrees. The bonus is what separates `GSC Spring Bintulu` from
    `GSC The Spring, Kuching`, which are otherwise near-identical.
    """
    site = _name_tokens(site_name, expand=True)
    if not site:
        return 0.0
    best = 0.0
    for town, place in _name_parts(service_name):
        tokens = _name_tokens(place)
        if not tokens:
            continue
        score = 2 * len(site & tokens) / (len(site) + len(tokens))
        if site <= tokens or tokens <= site:
            score += 0.5
        if site & _name_tokens(town, expand=True):
            score += 0.5
        best = max(best, score)
    return best


# Below this, two names are not the same outlet. Every pair the two
# directories actually share scores at least 0.7, and the weakest real pair
# scores 0.9.
JOIN_THRESHOLD = 0.7


def join_directories(outlets: list[Cinema], directory: list[Cinema],
                     threshold: float = JOIN_THRESHOLD) -> int:
    """Attach slug, city and features from the website to service outlets.

    The two directories share no id, so they are matched on name. Candidates
    are taken strongest-first and each side is used once, which is what keeps
    the near-identical names apart: `GSC IOI Mall Kulai` and `GSC IOI Mall
    Puchong` both share tokens with the other, and only the town token tells
    them from each other.

    Mutates `outlets` and returns how many were joined. Unmatched outlets keep
    their service values and leave slug, city and features as None.
    """
    candidates = sorted(
        (
            (_name_score(site.name, service.name), si, oi)
            for si, site in enumerate(directory)
            for oi, service in enumerate(outlets)
        ),
        key=lambda item: -item[0],
    )
    taken_sites: set[int] = set()
    taken_outlets: set[int] = set()
    joined = 0
    for score, si, oi in candidates:
        if score < threshold:
            break
        if si in taken_sites or oi in taken_outlets:
            continue
        taken_sites.add(si)
        taken_outlets.add(oi)
        site, service = directory[si], outlets[oi]
        service.slug = site.slug
        service.city = site.city or service.town
        service.features = site.features
        joined += 1
    return joined


def parse_movies(xml: str) -> list[Movie]:
    """The catalogue payload -> one Movie per film.

    One `<parent>` is a film and its `<child>` records are the bookable format
    variants of it, e.g. "Digger" and "(IMAX) Digger". The parent becomes the
    Movie; the children become its `variants` and its `experiences`.
    """
    root = xml_of(xml, "movies")
    movies: list[Movie] = []
    for parent in root.findall("parent"):
        code = clean(parent.get("code"))
        title = clean(parent.get("title"))
        if not code or not title:
            continue
        children = parent.findall("child")
        first = children[0] if children else parent
        trailer = clean(first.get("trailer_url"))
        if trailer and "youtube" not in trailer and "youtu.be" not in trailer:
            trailer = None
        variants = tuple(
            clean(child.get("code")) for child in children if clean(child.get("code"))
        )
        experiences = tuple(
            dict.fromkeys(
                clean(child.get("film_type")) for child in children if clean(child.get("film_type"))
            )
        )
        dates = [
            clean(show.get("opsdate"))
            for child in children
            for show in child.findall("show")
        ]
        movies.append(
            Movie(
                id=code,
                title=title,
                slug=slugify(title),
                genre=tuple(as_list(first.get("genre"))),
                rating=clean(first.get("rating")),
                runtime=duration_minutes(first.get("duration")),
                language=clean(first.get("lang")),
                trailer_url=trailer,
                poster_url=clean(first.get("thumb_big")) or clean(first.get("thumb_medium")),
                banner_url=clean(first.get("stills")),
                experiences=experiences,
                variants=variants,
                parent_id=code,
                extra={"dates": list(dict.fromkeys(d for d in dates if d))},
            )
        )
    return movies


def _showtime(element, cinema_id: str, cinema_name: str | None,
              business_date: str | None, film_id: str | None = None) -> Showtime:
    """One `<show>` element -> a Showtime.

    The show element does not name the film, so the bookable variant code is
    passed in from the `<child>` that encloses it. That code is what pricing is
    quoted against.
    """
    hall = clean(element.get("hname"))
    return Showtime(
        session_id=clean(element.get("id")) or "",
        movie_id=film_id or clean(element.get("movieid")) or "",
        cinema_id=cinema_id,
        start=session_start(element.get("date"), element.get("time")),
        hall=hall,
        hall_id=clean(element.get("hid")),
        business_date=business_date,
        experience=clean(element.get("type")),
        experience_desc=clean(element.get("type_desc")),
        seat_types=tuple(as_list(element.get("seattypes"))),
        time_text=clean(element.get("timestr")),
    )


def parse_day(xml: str, cinema_id: str, cinema_name: str | None = None) -> list[Movie]:
    """The one-cinema, one-date payload -> movies carrying their sessions.

    Films arrive under `<oprn>`, which carries the business date, then a
    `<parent>` per film and a `<child>` per bookable variant with its `<show>`
    elements. Children of one parent are merged into one Movie, because a film
    playing in 2D and in IMAX at the same cinema is still one film.
    """
    root = xml_of(xml, "day")
    business_date = None
    movies: list[Movie] = []
    by_parent: dict[str, Movie] = {}

    for oprn in root.findall("oprn"):
        business_date = clean(oprn.get("date")) or business_date
        for parent in oprn.findall("parent"):
            code = clean(parent.get("code"))
            if not code:
                continue
            movie = by_parent.get(code)
            for child in parent.findall("child"):
                child_code = clean(child.get("code"))
                showtimes = [
                    _showtime(show, cinema_id, cinema_name, business_date, child_code)
                    for show in child.findall("show")
                ]
                showtimes.sort(key=lambda s: (s.start is None, s.start))
                if movie is None:
                    trailer = clean(child.get("trailer_url"))
                    if trailer and "youtube" not in trailer and "youtu.be" not in trailer:
                        trailer = None
                    movie = Movie(
                        id=code,
                        title=clean(parent.get("title")) or code,
                        slug=slugify(clean(parent.get("title")) or code),
                        genre=tuple(as_list(child.get("genre"))),
                        rating=clean(child.get("rating")),
                        runtime=duration_minutes(child.get("duration")),
                        language=clean(child.get("lang")),
                        trailer_url=trailer,
                        poster_url=clean(child.get("thumb_big")),
                        parent_id=code,
                        cinema_id=cinema_id,
                        cinema_name=cinema_name,
                    )
                    by_parent[code] = movie
                    movies.append(movie)
                elif movie.runtime is None:
                    movie.runtime = duration_minutes(child.get("duration"))
                movie.variants = tuple(
                    dict.fromkeys(movie.variants + ((child_code,) if child_code else ()))
                )
                # The day payload carries no format on the child, but each show
                # names the hall format it is in, which is the same thing.
                movie.experiences = tuple(
                    dict.fromkeys(
                        movie.experiences
                        + tuple(
                            clean(show.get("type")) for show in child.findall("show")
                        )
                    )
                )
                movie.showtimes = tuple(
                    sorted(
                        movie.showtimes + tuple(showtimes),
                        key=lambda s: (s.start is None, s.start),
                    )
                )

    for movie in movies:
        movie.cinema_id = cinema_id
        if movie.cinema_name is None:
            movie.cinema_name = cinema_name
        for showtime in movie.showtimes:
            showtime.runtime = movie.runtime
        movie.session_ids = tuple(s.session_id for s in movie.showtimes if s.session_id)
    movies.sort(key=lambda m: m.title.casefold())
    return movies


def parse_movie_shows(xml: str, parent_id: str | None = None,
                      on: str | None = None) -> list[Movie]:
    """The one-film payload -> one Movie per cinema showing it.

    The service answers for every cinema at once, so a national lookup of one
    film is a single request rather than one per outlet. The result is the same
    shape `day()` produces, which is what `movie()` returns.

    `on` is the business date that was asked for. The payload does not echo it
    back, and it cannot be recovered from the shows: a 00:15 screening is
    filed under one business date and timestamped on the next calendar day.
    """
    root = xml_of(xml, "movie_shows")
    parent_code = clean(root.get("parent_code"))
    parent_title = clean(root.get("parent_title"))
    found: list[Movie] = []
    for location in root.findall("location"):
        cid = clean(location.get("id"))
        name = clean(location.get("name"))
        if not cid:
            continue
        wrapper = ET.Element("films")
        oprn = ET.SubElement(wrapper, "oprn")
        if on:
            oprn.set("date", on)
        children = location.findall("child")
        if not children:
            continue
        # The payload drops the <parent> level, so it is put back for `parse_day`.
        parent = ET.SubElement(oprn, "parent")
        if parent_code:
            parent.set("code", parent_code)
        if parent_title:
            parent.set("title", parent_title)
        for child in children:
            parent.append(child)
        found.extend(parse_day(ET.tostring(wrapper, encoding="unicode"), cid, name))
    return found


def parse_seat_plan(xml: str, cinema_id: str | None = None,
                    session_id: str | None = None) -> SeatPlan:
    """The hall payload -> a SeatPlan.

    The grid is complete: every row is present and every seat carries its own
    absolute column, so an aisle is simply a column with no seat in it and the
    layout can be drawn as it arrives, with no alignment pass needed.

    Three separate notions of "how many seats" arrive in one payload and they do
    not agree, so they are kept apart:

    - the grid itself, which is the physical seat count,
    - the seat states, `A` free, `B` booked, `D` blocked,
    - the hall booking block, which is what the service still sells.

    The booking block is what `seats_left` reports, because it is the figure
    the site itself shows, and counting the states does not reproduce it: on
    the captured hall they account for 6 of the 7 seats the service holds back.
    """
    root = xml_of(xml, "seat_plan")
    areas: list[SeatArea] = []

    for section in root.findall("section"):
        rows: list[SeatRow] = []
        for row in section.findall("row"):
            seats = tuple(
                Seat(
                    id=clean(col.get("id")) or "",
                    row=int(col.get("y") or row.get("y") or 0),
                    column=int(col.get("x") or 0),
                    number=clean(col.get("value")),
                    status=clean(col.get("status")) or SEAT_FREE,
                    category=int(col.get("seatcategory"))
                    if (col.get("seatcategory") or "").isdigit() else None,
                    seat_type=clean(col.get("type")),
                    priority=int(col.get("priority") or 0),
                )
                for col in row.findall("col")
                if col.get("id") is not None
            )
            rows.append(SeatRow(index=int(row.get("y") or 0), name=clean(row.get("value")),
                                seats=seats))
        rows.sort(key=lambda r: r.index)
        columns = {seat.column for row in rows for seat in row.seats}
        areas.append(
            SeatArea(
                number=int(section.get("value") or 0),
                code=clean(section.get("value")),
                seat_count=sum(len(row.seats) for row in rows),
                row_count=len(rows),
                column_count=len(columns),
                rows=tuple(rows),
            )
        )

    available = {
        clean(tkt.get("name")) or SEAT_FREE: int(tkt.get("qty") or 0)
        for tkt in root.findall("hallbooking/tkt")
    }
    categories = {
        clean(item.get("id")): {
            "name": clean(item.get("name")),
            "seats_taken": int(item.get("seats_taken") or 1),
            "seat_type": clean(next(iter(item.findall("seat")), ET.Element("seat")).get("type")),
        }
        for item in root.findall("mappings/category")
        if clean(item.get("id"))
    }
    experiences = tuple(
        f"{clean(exp.get('name'))}: {clean(exp.get('description'))}".rstrip(": ")
        for exp in root.findall("experience/exp_img")
        if clean(exp.get("name"))
    )

    plan = SeatPlan(
        cinema_id=cinema_id,
        session_id=session_id,
        hall_id=clean(root.get("no")),
        areas=tuple(areas),
        available=available,
        categories=categories,
        reported_total=int(root.get("maximumseats") or 0) or None,
        reported_booked=int(root.get("hbooked") or 0),
        experiences=experiences,
    )
    plan.seats_total = len(plan.seats) or None
    plan.seats_blocked = len([s for s in plan.seats if s.state == SEAT_BLOCKED]) or None
    left = plan.seats_left
    if plan.seats_total is not None:
        plan.seats_booked = max(0, plan.seats_total - (left if left is not None else 0))
    return plan


def parse_seat_counts(xml: str) -> dict:
    """The hall payload reduced to just the counts, for `seat_status`."""
    return counts_from_plan(parse_seat_plan(xml))


def counts_from_plan(plan: SeatPlan) -> dict:
    """{session id: counts} for one hall, the shape `seat_status` returns."""
    total, left = plan.seats_total, plan.seats_left
    used = plan.seats_booked
    if left is None and used is None:
        used = len(plan.booked) or None
    return {
        "seats_total": total,
        "seats_used": used,
        "seats_left": left if left is not None else (max(0, total - used) if
                                                    (total is not None and used is not None)
                                                    else None),
        "seats_blocked": plan.seats_blocked,
        "used_percentage": round(used / total * 100, 1) if (total and used is not None)
        else None,
    }


def parse_tickets(xml: str) -> tuple[TicketType, ...]:
    """The pricing payload -> the ticket types on sale for one session.

    `price` is the advertised price and `tax` is deliberately not read, for
    the reason given on `TicketType`. The food-and-bundle block the service
    attaches to the same response is ignored here: it is concessions, not
    admission.
    """
    root = xml_of(xml, "tickets")
    found: list[TicketType] = []
    for ticket in root.findall("ticketdata/ticket"):
        price = ticket.get("price")
        try:
            amount = float(price) if price not in (None, "") else None
        except ValueError:
            amount = None
        code = clean(ticket.get("id")) or ""
        seats = int(ticket.get("seats_taken") or 1)
        category = clean(ticket.get("seatcategory"))
        found.append(
            TicketType(
                code=code,
                description=clean(ticket.get("type")) or code,
                price=amount,
                seats=seats,
                package=bool(ticket.get("seats_taken")) and seats > 1,
                area_code=category,
                child_only=code.casefold() == "children",
                standard=code.upper() in STANDARD_TICKETS,
            )
        )
    return tuple(found)


def parse_listing(html: str) -> list[Movie]:
    """Pull every movie card out of a website listing page."""
    cards = re.split(r'<div class="movie-poster-card"', html)[1:]
    movies: list[Movie] = []
    seen: set[str] = set()

    for card in cards:
        slug = _slug(_text(r'<a href="(/movie/[^"]+)"[^>]*class="card-link"', card))
        title = _text(r'class="title-text"[^>]*>(.*?)</span>', card)
        if not slug or not title or slug in seen:
            continue
        seen.add(slug)

        # Each chip carries an icon that says what it means.
        chips: dict[str, str] = {}
        icons = {
            "i-tag.svg": "genre",
            "i-clock.svg": "duration",
            "i-chat.svg": "language",
            "i-keyboard.svg": "subtitles",
            "i-lpf.svg": "rating",
        }
        for icon_src, chip_text in re.findall(
            r'<span class="chip"[^>]*>.*?src="([^"]+)"[^>]*>(.*?)</span>', card, re.S
        ):
            for icon, key in icons.items():
                if icon in icon_src and (value := clean(re.sub(r"<[^>]+>", "", chip_text))):
                    chips.setdefault(key, value)

        booking = _abs(_text(r'<a href="([^"]+)"[^>]*class="btn-buy"', card))
        match = re.search(r"[?&]id=(\d+)", booking or "")
        duration = clean(chips.get("duration"))

        movies.append(
            Movie(
                title=title,
                slug=slug,
                genre=tuple(as_list(chips.get("genre"))),
                runtime=duration_minutes(duration),
                duration=duration,
                language=clean(chips.get("language")),
                subtitles=split_codes(chips.get("subtitles")),
                rating=clean(chips.get("rating")),
                poster_url=_abs(_text(r'<div class="poster"[^>]*>\s*<img src="([^"]+)"', card)),
                booking_url=booking,
                booking_id=int(match.group(1)) if match else None,
                id=str(match.group(1)) if match else "",
                parent_id=str(match.group(1)) if match else None,
            )
        )
    return movies


def parse_cinemas_page(html: str) -> list[Cinema]:
    """Pull every outlet card out of the website's /cinemas/gsc/ page.

    Each card carries a link, a name, a town, and an optional comma-joined list
    of formats and amenities. GSC files a couple of outlets under a state
    rather than a town (GSC Spring Bintulu is listed as "Sarawak"), so `city`
    is whatever GSC says and is not guaranteed to be a locality.

    The page is not only GSC-branded: it also lists Aurum Theatre and Velvet
    Cinemas, so `join_directories` ignores anything not called "GSC ...".
    """
    cards = re.split(r'<div class="card-info ratio ratio-4x3"', html)[1:]
    found: list[Cinema] = []
    seen: set[str] = set()

    for card in cards:
        slug = _slug_path(_text(r'<a href="(/cinema/[^"]+)"', card), "cinema")
        name = _text(r'class="font-m fw-bold mb-2"[^>]*>(.*?)</div>', card)
        if not slug or not name or slug in seen:
            continue
        seen.add(slug)
        found.append(
            Cinema(
                id=slug,
                name=name,
                slug=slug,
                city=_text(r'twi-location[^>]*>(.*?)</li>', card),
                # The amenities row is absent on plain single-screen outlets.
                features=split_codes(_text(r'twi-experience[^>]*>(.*?)</li>', card)),
            )
        )
    return found


def parse_detail(html: str, slug: str) -> Movie:
    """Read one movie's detail page from the website.

    The page embeds a schema.org/Movie JSON-LD block, which is the richest and
    cleanest source. Falls back to the visible specs table for anything missing.
    """
    data: dict = {}
    # Attributes come in any order, e.g. <script data-n-head="ssr" type="application/ld+json">.
    for raw in re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S
    ):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for item in parsed if isinstance(parsed, list) else [parsed]:
            if isinstance(item, dict) and item.get("@type") == "Movie":
                data = item
                break
        if data:
            break

    def specs(label: str) -> str | None:
        match = re.search(
            rf'class="spec-label"[^>]*>\s*{label}\s*</span>\s*'
            rf'<span class="spec-value"[^>]*>\s*:?\s*([^<]*)',
            html,
            re.I,
        )
        return clean(match.group(1)) if match else None

    def credit(label: str) -> str | None:
        match = re.search(
            rf'class="credit-label"[^>]*>\s*{label}\s*</span>\s*:\s*([^<]*)', html, re.I
        )
        return clean(match.group(1)) if match else None

    genre = data.get("genre")
    if isinstance(genre, list):
        genre = ", ".join(str(item) for item in genre if item)
    booking = _abs(_text(r'<a href="([^"]+)"[^>]*class="cta-book"', html))
    match = re.search(r"[?&]id=(\d+)", booking or "")
    actors = data.get("actor") or []
    if isinstance(actors, dict):
        actors = [actors]
    trailer = data.get("trailer") or {}
    duration = clean(data.get("duration")) or specs("Running Time")
    title = data.get("name") or _text(r'class="movie-title"[^>]*>(.*?)</h1>', html) or slug

    return Movie(
        title=title,
        slug=slug,
        genre=tuple(as_list(genre or specs("Genre"))),
        runtime=duration_minutes(duration),
        duration=duration,
        language=clean(data.get("inLanguage") or specs("Language")),
        subtitles=split_codes(specs("Subtitles")),
        rating=clean(data.get("contentRating") or specs("Classification")),
        poster_url=_abs(data.get("image")),
        booking_url=booking,
        booking_id=int(match.group(1)) if match else None,
        synopsis=clean(data.get("description")),
        director=credit("Director"),
        cast=tuple(
            name
            for name in (
                item.get("name") if isinstance(item, dict) else item for item in actors
            )
            if name
        )
        or split_codes(credit("Cast")),
        trailer_url=trailer.get("embedUrl") if isinstance(trailer, dict) else None,
        release_date=clean(data.get("dateCreated") or specs("Release Date")),
    )


def _text(pattern: str, html: str, group: int = 1) -> str | None:
    match = re.search(pattern, html, re.I | re.S)
    if not match:
        return None
    return clean(re.sub(r"<[^>]+>", " ", match.group(group)))


def _abs(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith("//"):
        return "https:" + url
    if url.startswith(f"http{BOOKING}") or url.startswith(f"http{SITE}"):
        return url
    return SITE + url if url.startswith("/") else url


def _slug(href: str | None) -> str | None:
    match = re.search(r"/movie/([^/?#]+)", href or "")
    # Slugs with an accent in them are percent-encoded in the href.
    return urllib.parse.unquote(match.group(1)) if match else None


def _slug_path(href: str | None, section: str) -> str | None:
    match = re.search(rf"/{section}/([^/?#]+)", href or "")
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# filtering
# --------------------------------------------------------------------------


def filter_times(movies: list[Movie], after: str | None = None,
                 before: str | None = None) -> list[Movie]:
    """Keep only the showtimes inside a window. Both bounds are inclusive.

    The window is anchored on the business date, not on the clock alone. GSC
    files a 00:15 show under the previous day's business date but timestamps it
    on the next calendar day, so `--from 18:00` still picks those up: you get
    everything from 18:00 that evening onwards, late shows included.
    """
    low, high = parse_clock(after), parse_clock(before)
    if low is None and high is None:
        return movies

    def keep(showtime: Showtime) -> bool:
        if showtime.start is None:
            return False
        anchor = (
            Date.fromisoformat(showtime.business_date)
            if showtime.business_date
            else showtime.start.date()
        )
        midnight = datetime(anchor.year, anchor.month, anchor.day, tzinfo=MYT)
        if low is not None and showtime.start < midnight + timedelta(hours=low[0], minutes=low[1]):
            return False
        if high is not None and showtime.start > midnight + timedelta(hours=high[0], minutes=high[1]):
            return False
        return True

    for film in movies:
        film.showtimes = tuple(s for s in film.showtimes if keep(s))
    return [film for film in movies if film.showtimes]


def filter_formats(movies: list[Movie], wanted: str | None = None) -> list[Movie]:
    """Keep only showtimes in the named hall format(s).

    `--format imax` is the premium tier, `--format 2d` the ordinary one.
    Comma-separate for several: `--format imax,4dx`. Matching is on the format
    code, case-insensitively, and unknown codes simply return nothing rather
    than erroring.
    """
    if not wanted:
        return movies
    codes = {code.strip().casefold() for code in wanted.split(",") if code.strip()}
    for film in movies:
        film.showtimes = tuple(
            s for s in film.showtimes if (s.experience or "").casefold() in codes
        )
    return [film for film in movies if film.showtimes]


def apply_seat_status(movies: list[Movie], seats: dict) -> list[Movie]:
    """Fold seat counts onto the showtimes, in place."""
    for movie in movies:
        for showtime in movie.showtimes:
            counts = seats.get(showtime.session_id)
            if counts:
                showtime.seats_total = counts.get("seats_total")
                showtime.seats_used = counts.get("seats_used")
    return movies


def by_region(outlets: list[Cinema], region: str) -> list[Cinema]:
    """Narrow an already-fetched outlet list to one region.

    Accepts either the region key ("klang valley") or its label ("Klang
    Valley"), case-insensitively.
    """
    wanted = region.strip().casefold()
    return [
        c for c in outlets
        if wanted in {(c.region or "").casefold(), (c.location or "").casefold()}
    ]


def by_city(outlets: list[Cinema], city: str) -> list[Cinema]:
    """Narrow an already-fetched outlet list to one town, case-insensitively.

    Matches the town in the service's own name, which every outlet has, as well
    as the website's `city` label when the two directories were joined.
    """
    wanted = city.strip().casefold()
    return [
        c for c in outlets
        if wanted in {(c.city or "").casefold(), (c.town or "").casefold()}
    ]


def filter_cinemas(outlets: list[Cinema], where: str) -> list[Cinema]:
    """Narrow an outlet list by region or town, whichever matches."""
    return by_region(outlets, where) or by_city(outlets, where)


def find_cinema(outlets: list[Cinema], needle: str) -> Cinema:
    """Look one outlet up by id, slug or name, case-insensitively.

    Punctuation is ignored, so "GSC 1 Utama" finds the outlet the website calls
    `GSC 1 Utama` even though the service, which is what an id has to come from,
    calls it `Petaling Jaya - 1 Utama (New Wing)`. Its slug is what the website
    name becomes, so matching on that bridges the two naming schemes.
    """
    key = needle.strip().casefold()
    squashed = _squash(needle)
    for c in outlets:
        if c.id.casefold() == key or (c.slug or "").casefold() == key:
            return c
    for c in outlets:
        if c.name.casefold() == key or _squash(c.name) == squashed:
            return c
    hits = [c for c in outlets if key in c.name.casefold() or squashed in _squash(c.name)]
    if not hits:
        hits = [c for c in outlets if c.slug and squashed in _squash(c.slug)]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise SystemExit(
            f"{needle!r} matches {len(hits)} cinemas: "
            + ", ".join(f"{c.id} {c.name}" for c in hits[:8])
        )
    raise SystemExit(f"no cinema matching {needle!r}")


def _squash(value: str) -> str:
    """A name with everything but its letters and digits removed."""
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------


def _options(kwargs: dict) -> tuple[Throttle, float]:
    throttle = kwargs.pop("throttle", None) or Throttle(kwargs.get("delay", 0.0))
    return throttle, kwargs.get("timeout", 25.0)


def movies(**kwargs) -> list[Movie]:
    """The whole booking catalogue: every film, one record each.

    One request. Each film carries the dates it has sessions on in
    `business_dates()` terms, and its bookable format variants in `variants`.
    """
    throttle, timeout = _options(kwargs)
    body = request(ENDPOINTS["movies"], {"includeChild": "true"}, timeout=timeout,
                   throttle=throttle)
    return parse_movies(body)


def listing(name: str, **kwargs) -> list[Movie]:
    """The website's editorial listing: now-showing, coming-soon, advance-sales.

    This is the one function GSC has that TGV does not, and it has no
    counterpart in the booking service: which shelf a film sits on is an
    editorial decision the website makes, not a property of a session. The
    catalogue from `movies()` is the service's own view and is not divided this
    way.
    """
    if name not in LISTINGS:
        raise SystemExit(f"unknown listing {name!r}, pick one of {', '.join(LISTINGS)}")
    options = {k: kwargs[k] for k in ("timeout", "delay", "throttle") if k in kwargs}
    return parse_listing(fetch(LISTINGS[name], **options))


def detail(slug: str, enrich: bool = True, **kwargs) -> Movie:
    """The website's record for one film, with synopsis and credits.

    The booking service carries no synopsis, director or cast, so the detail
    page is still the only place those come from. Its booking link carries the
    same parent code the catalogue uses, so the two records join on it.

    The website leaves fields it has not published as `N/A`, and an unreleased
    title's running time is one of them, while the booking service already knows
    it. So with `enrich` the record is backfilled from the catalogue, one extra
    request, and anything the catalogue does not know either stays None.
    """
    slug = slug.strip().strip("/").removeprefix("movie/").rstrip("/")
    if not slug or "/" in slug:
        raise SystemExit(f"bad slug: {slug!r}")
    options = {k: kwargs[k] for k in ("timeout", "delay", "throttle") if k in kwargs}
    movie = parse_detail(fetch(f"/movie/{slug}/", **options), slug)
    if movie.booking_id:
        movie.id = str(movie.booking_id)
        movie.parent_id = movie.id

    if enrich and movie.id:
        from_service = _service_film(movie, **kwargs)
        for name in ("runtime", "language", "rating", "genre", "poster_url",
                     "trailer_url", "slug"):
            if getattr(movie, name) in (None, (), "") and getattr(from_service, name):
                setattr(movie, name, getattr(from_service, name))
        movie.experiences = from_service.experiences
        movie.variants = from_service.variants
    return movie


def _service_film(movie: Movie, **kwargs) -> Movie:
    """The catalogue's record for one film, matched on its booking id."""
    throttle, timeout = _options(kwargs)
    for candidate in movies(throttle=throttle, timeout=timeout):
        if candidate.id == movie.id:
            return candidate
    return movie


def cinemas(with_details: bool = True, **kwargs) -> list[Cinema]:
    """Every GSC outlet in Malaysia, sorted by name.

    with_details=True adds the website's slug, town and amenities, which costs
    one more request for the outlet page. with_details=False returns the
    lighter id and name list only, in one request.
    """
    throttle, timeout = _options(kwargs)
    body = request(ENDPOINTS["cinemas"], timeout=timeout, throttle=throttle)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"non-JSON reply from {ENDPOINTS['cinemas']}") from exc

    directory = None
    if with_details:
        page = fetch(CINEMA_PAGE, timeout=timeout, throttle=throttle)
        directory = [c for c in parse_cinemas_page(page) if c.name.startswith("GSC ")]
    return parse_cinemas(payload, directory)


def regions(**kwargs) -> dict:
    """{region key: region label} for every GSC region.

    Derived from the outlet directory rather than a dedicated endpoint: the
    service tags each outlet with a region but publishes no list of its own.
    """
    throttle, timeout = _options(kwargs)
    labels: dict[str, str] = {}
    for cinema in cinemas(with_details=False, throttle=throttle, timeout=timeout):
        if cinema.location and cinema.region:
            labels[cinema.location] = cinema.region
    return dict(sorted(labels.items(), key=lambda kv: kv[1]))


def business_dates(**kwargs) -> list[str]:
    """Every date the service will answer for, ascending.

    The union of every date any film has a session on, so it reaches past today
    into advance sales, and it includes dates only a handful of cinemas run.
    One request, the same one `movies()` makes.
    """
    found: set[str] = set()
    for movie in movies(**kwargs):
        found.update(movie.dates)
    return sorted(found)


def day(cinema_id: str, on: str | Date | None = None, seats: bool = False,
        prices: bool = False, after: str | None = None, before: str | None = None,
        formats: str | None = None, **kwargs) -> list[Movie]:
    """Every movie and every session at one cinema on one date.

    One request for the day itself. `seats` adds remaining-seat counts and
    costs one further request per session, because the service has no batched
    seat call; `prices` costs one further request per session.

    `after`, `before` and `formats` narrow the result. They are applied to the
    parsed day **before** seats and prices are fetched, not after. That
    ordering is the whole point: a seat or price lookup is a request per
    screening, so filtering afterwards still pays for every one you discarded.
    """
    throttle, timeout = _options(kwargs)
    cid = str(cinema_id).strip()
    if not cid:
        raise SystemExit("day() needs a cinema id")
    body = request(
        ENDPOINTS["day"],
        {"locationid": cid, "oprndate": normalise_date(on)},
        timeout=timeout,
        throttle=throttle,
    )
    found = filter_formats(filter_times(parse_day(body, cid), after, before), formats)
    if seats:
        apply_seat_status(found, _seat_status(cid, found, throttle, timeout))
    if prices:
        _attach_prices(found, throttle, timeout, kwargs.get("workers", 8))
    return found


def _seat_status(cinema_id: str, movies: list[Movie], throttle: Throttle,
                 timeout: float, workers: int = 8) -> dict:
    """Seat counts for every session still in the batch.

    The service answers one session at a time, so this is one request per
    showtime. That is why it is opt-in and why the filters run first: a
    narrowed query only pays for the screenings it kept.
    """
    jobs = [s for film in movies for s in film.showtimes if s.session_id]
    if not jobs:
        return {}

    def one(showtime: Showtime) -> tuple[str, dict | None]:
        try:
            plan = seat_plan(cinema_id, showtime, throttle=throttle, timeout=timeout)
        except SystemExit as exc:
            print(f"gsc: seat lookup failed, {showtime.time}: {exc}", file=sys.stderr)
            return showtime.session_id, None
        return showtime.session_id, counts_from_plan(plan)

    workers = max(1, min(workers, len(jobs)))
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for sid, counts in pool.map(one, jobs):
            if counts:
                results[sid] = counts
    return results


def _counts_from_plan(plan: SeatPlan) -> dict:
    total = plan.seats_total
    booked = plan.seats_booked or 0
    left = sum(plan.available.values()) if plan.available else None
    if left is None and total is not None:
        left = max(0, total - booked)
    return {
        "seats_total": total,
        "seats_used": booked,
        "seats_left": left,
        "used_percentage": round(booked / total * 100, 1) if total else None,
    }


def schedule(cinema_ids: list[str] | None = None, on: str | Date | None = None,
             seats: bool = False, prices: bool = False, workers: int = 4,
             after: str | None = None, before: str | None = None,
             formats: str | None = None, **kwargs) -> dict:
    """{cinema id: movies} for one date across many cinemas.

    Each cinema is one request, so a national run is one request per outlet.
    They go out in parallel but are still rate limited by the throttle.
    prices=True adds one more request per session on top, which is a lot, so
    it is off by default.

    `after`, `before` and `formats` are applied per cinema before seats and
    prices are fetched, so a filtered run never pays for screenings it drops.
    """
    throttle, timeout = _options(kwargs)
    when = normalise_date(on)

    if cinema_ids is None:
        cinema_ids = [c.id for c in cinemas(with_details=False, throttle=throttle,
                                           timeout=timeout)]
    else:
        cinema_ids = [str(c).strip() for c in cinema_ids]

    def one(cid: str) -> tuple[str, list[Movie]]:
        body = request(ENDPOINTS["day"], {"locationid": cid, "oprndate": when},
                       timeout=timeout, throttle=throttle)
        found = filter_formats(filter_times(parse_day(body, cid), after, before), formats)
        if seats:
            apply_seat_status(found, _seat_status(cid, found, throttle, timeout))
        if prices:
            _attach_prices(found, throttle, timeout, workers)
        return cid, found

    workers = max(1, min(workers, len(cinema_ids) or 1))
    results: dict[str, list[Movie]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for cid, found in pool.map(one, cinema_ids):
            results[cid] = found
    return {cid: results[cid] for cid in cinema_ids if results.get(cid)}


def movie(slug_or_title: str, on: str | Date | None = None, seats: bool = False,
          prices: bool = False, cinema_ids: list[str] | None = None,
          workers: int = 4, after: str | None = None, before: str | None = None,
          formats: str | None = None, **kwargs) -> list:
    """Every session of one film, as [(cinema id, Movie)].

    The service has no movie-centric session lookup by title, so the film is
    resolved in the catalogue first and then asked for directly. That is one
    request to resolve the film and one for its sessions, which is cheaper than
    walking every cinema the way a title search would have to, and it returns
    every cinema at once rather than only the ones you guessed.

    A film can be filed under several titles that differ only by a bracketed
    format prefix, so a title is matched loosely too. Every variant that has
    sessions is returned; if none does, the film is reported as having none.

    Pass `cinema_ids` to stay on one or a few outlets. Left as None it sweeps
    the whole country, and with `prices=True` that is a price lookup per
    session at every outlet.
    """
    throttle, timeout = _options(kwargs)
    when = normalise_date(on)
    catalogue = movies(throttle=throttle, timeout=timeout)

    wanted = slug_or_title.strip().casefold()
    slug = wanted.replace(" ", "-")
    loose = loose_title(slug_or_title)
    hits = [
        m for m in catalogue
        if m.id == wanted
        or (m.slug or "") == slug
        or m.title.casefold() == wanted
        or loose_title(m.title) == loose
    ]
    if not hits:
        raise SystemExit(_no_sessions(slug_or_title, when, after, before, formats))

    if cinema_ids is not None:
        targets = [str(c).strip() for c in cinema_ids]
        # The day payload carries no outlet name, so the directory is asked for
        # the names. One request, and only when the caller narrowed to a few
        # outlets.
        names = {
            c.id: c.name
            for c in cinemas(with_details=False, throttle=throttle, timeout=timeout)
        }
        found = []
        for cid in targets:
            body = request(ENDPOINTS["day"], {"locationid": cid, "oprndate": when},
                           timeout=timeout, throttle=throttle)
            found.extend(
                film
                for film in parse_day(body, cid, names.get(cid))
                if any(film.id == hit.id or loose_title(film.title) == loose_title(hit.title)
                       for hit in hits)
            )
    else:
        # Ask for every variant and keep the ones that have sessions, rather
        # than betting on the first catalogue match.
        found = []
        for hit in hits:
            found.extend(
                parse_movie_shows(
                    request(ENDPOINTS["movie_shows"],
                            {"parentid": hit.id, "oprndate": when},
                            timeout=timeout, throttle=throttle),
                    hit.id,
                    when,
                )
            )

    found = [m for m in found if filter_formats(filter_times([m], after, before), formats)]
    found = [m for m in found if m.showtimes]
    if not found:
        raise SystemExit(_no_sessions(slug_or_title, when, after, before, formats))

    for film in found:
        if seats:
            apply_seat_status([film], _seat_status(film.cinema_id, [film], throttle, timeout))
        if prices:
            _attach_prices([film], throttle, timeout, workers)
    return [(f.cinema_id or "", f) for f in found]


def _no_sessions(wanted: str, when: str, after, before, formats) -> str:
    hint = " with those --from/--until/--format filters" if (after or before or formats) else ""
    return f"no sessions for {wanted!r} on {when}{hint}"


def _seat_params(cinema_id: str, showtime: Showtime) -> dict:
    """The query the hall endpoints want: which hall, which day, which time."""
    return {
        "locationid": str(cinema_id).strip(),
        "hallid": str(showtime.hall_id or ""),
        "showdate": showtime.start.date().isoformat() if showtime.start else "",
        "showtime": showtime.start.strftime("%H%M") if showtime.start else "",
    }


def seat_plan(cinema_id: str, showtime: "Showtime | str", **kwargs) -> SeatPlan:
    """The full seat map for one session.

    The day payload carries no seat coordinates at all, so this is the only way
    to see which seats are taken. One request per session. Pass a Showtime, or
    a session id with the hall, date and time spelled out.
    """
    throttle, timeout = _options(kwargs)
    if isinstance(showtime, Showtime):
        session_id = showtime.session_id
        params = _seat_params(cinema_id, showtime)
    else:
        session_id = str(showtime)
        params = {
            "locationid": str(cinema_id).strip(),
            "hallid": str(kwargs.get("hall_id") or ""),
            "showdate": normalise_date(kwargs.get("showdate")),
            "showtime": re.sub(r"\D", "", str(kwargs.get("showtime") or "")),
        }
    if not session_id:
        raise SystemExit("seat_plan needs a session id")
    body = request(ENDPOINTS["seat_plan"], params, timeout=timeout, throttle=throttle)
    plan = parse_seat_plan(body, str(cinema_id).strip(), session_id)
    plan.hall_name = kwargs.get("hall_name") if isinstance(showtime, str) else showtime.hall
    return plan


def seat_status(cinema_id: str, showtimes, **kwargs) -> dict:
    """Seat counts for one or more sessions: total, used, left, percentage.

    The service answers one session at a time, so this is one request per
    showtime. A failed session is reported on stderr and left out of the
    result rather than taking the run down.
    """
    throttle, timeout = _options(kwargs)
    if isinstance(showtimes, Showtime):
        showtimes = [showtimes]
    out: dict[str, dict] = {}
    for showtime in showtimes or []:
        try:
            plan = seat_plan(cinema_id, showtime, throttle=throttle, timeout=timeout)
        except SystemExit as exc:
            print(f"gsc: seat lookup failed, {showtime.time}: {exc}", file=sys.stderr)
            continue
        out[showtime.session_id] = _counts_from_plan(plan)
    return out


def tickets(cinema_id: str, showtime: "Showtime | str", hall_id: str | None = None,
            film_id: str | None = None, on: str | Date | None = None, **kwargs) -> tuple:
    """Every ticket type on sale for one session.

    The lowest-level pricing call: a single request, no batching and no
    filtering. Prefer `session_prices()` unless the ids are already to hand.

        gsc.tickets("235", late)

    Pass a Showtime and the hall, film and time are read off it. Otherwise
    pass the ids: hall, film, and the date and time.
    """
    throttle, timeout = _options(kwargs)
    if isinstance(showtime, Showtime):
        hall_id = hall_id or showtime.hall_id
        film_id = film_id or showtime.movie_id
        on = on or (showtime.start.date() if showtime.start else None)
        when = normalise_date(on)
        clock = showtime.start.strftime("%H%M") if showtime.start else ""
    else:
        when = normalise_date(on)
        clock = re.sub(r"\D", "", str(kwargs.get("showtime") or ""))
    if not (hall_id and film_id and clock):
        raise SystemExit("tickets needs a hall id, a film id and a show time")
    body = request(
        ENDPOINTS["tickets"],
        {
            "locationid": str(cinema_id).strip(),
            "hallid": str(hall_id),
            "filmid": str(film_id),
            "showdate": when,
            "showtime": clock,
        },
        timeout=timeout,
        throttle=throttle,
    )
    return parse_tickets(body)


def session_prices(showtime: Showtime, **kwargs) -> Showtime:
    """Price one screening and return it.

    This is the on-demand path: you pick the session, this prices that session
    and nothing else. `day(..., prices=True)` and friends are the bulk path
    and loop this logic over every showtime they kept.

        films = gsc.day("235", "2026-10-14")
        late = next(s for f in films for s in f.showtimes if s.time == "22:00")
        gsc.session_prices(late).price

    Mutates and returns the showtime, so `showtime.price` reads the same
    afterwards. The result is the cheapest standard adult seat;
    `price_bundle` has the pair price if the hall sells one.
    """
    try:
        showtime.ticket_types = tickets(showtime.cinema_id, showtime, **kwargs)
    except SystemExit as exc:
        print(f"gsc: price lookup failed, {showtime.time}: {exc}", file=sys.stderr)
        showtime.ticket_types = ()
    return showtime


def _attach_prices(movies: list[Movie], throttle: Throttle, timeout: float,
                   workers: int = 8) -> None:
    """Fill in ticket prices on every showtime, in place.

    Prices are only fetched for the showtimes still in `movies`, so a narrowed
    query (`--from`, `--until`, `--format`, or one film at one cinema) costs
    only the showtimes it kept. Filter first, then call this.

    One request per showtime. Those go out in parallel; the throttle still
    holds the *global* rate, so the request count is unchanged and a run stops
    being serial.

    A failed call does not take the run down with it. That showtime is left
    without a price and reported on stderr, so it prints as a missing price
    rather than a missing screening.
    """
    jobs = [s for film in movies for s in film.showtimes if s.session_id]
    if not jobs:
        return

    workers = max(1, min(workers, len(jobs)))

    def one(showtime: Showtime) -> tuple[int, tuple]:
        return id(showtime), session_prices(
            showtime, throttle=throttle, timeout=timeout
        ).ticket_types

    collected: dict[int, list] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for sid, found in pool.map(one, jobs):
            collected.setdefault(sid, []).extend(found)

    for film in movies:
        for showtime in film.showtimes:
            showtime.ticket_types = tuple(collected.get(id(showtime), ()))


def cinema_experiences(cinema_id: str, on: str | Date | None = None, **kwargs) -> dict:
    """{format code: label} for one cinema, from its day payload.

    The service publishes no cinema directory with formats on it, but every day
    payload repeats that cinema's formats in its filter block, so this is one
    request. `day()` and `schedule()` fold the same block into each movie's
    `experiences`.
    """
    throttle, timeout = _options(kwargs)
    body = request(
        ENDPOINTS["day"],
        {"locationid": str(cinema_id).strip(), "oprndate": normalise_date(on)},
        timeout=timeout,
        throttle=throttle,
    )
    root = xml_of(body, "day")
    found: dict[str, str] = {}
    for group in root.findall("filters/group"):
        if group.get("name") != "Experiences":
            continue
        for item in group.findall("type"):
            code = clean(item.get("code"))
            if code:
                found[code] = clean(item.get("name")) or code
    return found


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _seat_cell(seat: Seat | None, width: int) -> str:
    """One fixed-width cell. Blank for an aisle, the seat number when free, `X`
    when taken and `-` when blocked. The width is constant so every column
    lines up down the grid."""
    if seat is None:
        return " " * width
    mark = SEAT_MARKS.get(seat.state)
    return (mark if mark is not None else seat.number or seat.id).rjust(width)


def render_seat_plan(plan: SeatPlan, width: int = 3, ruler: bool = True) -> str:
    """Draw a hall as fixed-width text.

    Every cell is `width` characters, and a column with no seat in it is left
    blank rather than closed up. That is the whole point: halls genuinely
    differ row to row, with aisles mid-hall, and packing the numbers together
    makes a wide row look like it has an extra seat. Rendering the column
    positions is what makes the aisles visible.

    GSC's grid is already absolute, so the columns can be drawn as they arrive.
    The drawing ends with a count of seats drawn per section, and prints the
    service's own figure when the two disagree, so a miscount surfaces
    immediately rather than being argued about.
    """
    first, last = plan.first_column, plan.last_column
    columns = list(range(first, last + 1))
    label_width = max(
        (len(row.name or "") for area in plan.areas for row in area.rows), default=1
    ) + 2

    out: list[str] = []
    if ruler and columns:
        out.append(" " * label_width + "".join(str(c).rjust(width) for c in columns))
        # Ticks are as wide as the label they sit under and right-aligned to
        # match it, so every column reads as sitting on its own digit.
        out.append(
            " " * label_width
            + "".join(("-" * len(str(c))).rjust(width) for c in columns).rstrip()
        )

    screen = "=" * (label_width + len(columns) * width)
    out.append(f"{'':>{len(screen) - 1}}SCREEN".rjust(len(screen) - 6))

    drawn_for: dict[int, int] = {}
    for area, row in plan.hall_rows:
        if area.number not in drawn_for:
            taken = len(area.booked)
            drawn_for[area.number] = 0
            if out and out[-1] != screen:
                out.append("")
            out.append(
                f"{area.label} - {area.seat_count} seats, {taken} taken, "
                f"{area.seat_count - taken} free"
            )
        by_column = {seat.column: seat for seat in row.seats}
        out.append(
            (row.name or str(row.index)).ljust(label_width)
            + "".join(_seat_cell(by_column.get(c), width) for c in columns)
        )
        drawn_for[area.number] += len(row.seats)

    for area in plan.areas:
        counted = drawn_for.get(area.number, 0)
        if not counted:
            continue
        out.append(
            f"{area.label}: {counted} seats drawn"
            + ("" if counted == area.seat_count else f" (service says {area.seat_count})")
        )
    return "\n".join(out)


# --------------------------------------------------------------------------
# printing
# --------------------------------------------------------------------------


def _session_line(showtime: Showtime, per_person: bool = False) -> str:
    parts = [showtime.time or showtime.time_text or "??:??", showtime.hall or "-"]
    left = showtime.seats_left
    if left is not None:
        parts.append("SOLD OUT" if left == 0 else f"{left} seats")
    if per_person and showtime.per_person is not None:
        parts.append(f"RM{showtime.per_person:.2f}/person")
        return "  ".join(parts)
    # Quote the single-seat price. A bundle covers two people, so showing its
    # full total here would read as the cost of one seat.
    single = showtime.single_seat_price
    if single is not None:
        parts.append(f"RM{single:.2f}")
        if showtime.price_bundle is not None:
            parts.append(f"(pair RM{showtime.price_bundle:.2f})")
    elif showtime.price_range:
        parts.append(showtime.price_range)
    return "  ".join(parts)


def show_movies(movies: list[Movie], cinema_label: str | None = None,
                per_person: bool = False) -> None:
    """Print one cinema's films and their sessions."""
    if cinema_label:
        print(f"\n=== {cinema_label}\n")
    for film in movies:
        bits = [
            bit for bit in (
                ", ".join(film.genre),
                f"{film.runtime} min" if film.runtime else None,
                film.rating,
                film.language,
            ) if bit
        ]
        print(f"  {film.title}" + (f"  ({' | '.join(bits)})" if bits else ""))
        for showtime in film.showtimes:
            print(f"      {_session_line(showtime, per_person)}")
        if not film.showtimes:
            subs = ", ".join(film.subtitles) or "-"
            audio = film.language or "-"
            print(f"      {audio} audio | {subs} subs | {film.slug}")


def show_listing(movies: list[Movie], label: str | None = None) -> None:
    """Print a website listing, numbered.

    A listing carries no sessions, so each film gets one summary line rather
    than the session lines `show_movies` prints.
    """
    if label:
        print(f"\n{label}: {len(movies)} movies\n")
    for i, m in enumerate(movies, 1):
        runtime = f"{m.runtime} min" if m.runtime else "TBA"
        subs = ", ".join(m.subtitles) or "-"
        print(f"{i:2}. {m.title}")
        print(
            f"    {' | '.join(m.genre) or '-'} | {runtime} | {m.rating or 'unrated'} "
            f"| audio {m.language or '-'} | subs {subs}"
        )


def show_schedule(schedule_data: dict, per_person: bool = False) -> None:
    """Print a whole schedule, one section per cinema."""
    for cid, films in schedule_data.items():
        show_movies(films, f"{films[0].cinema_name or cid} [{cid}]" if films else cid,
                    per_person)


def show_cinemas(outlets: list[Cinema]) -> None:
    """Print the numbered outlet directory with region and amenities."""
    for i, c in enumerate(outlets, 1):
        formats = ", ".join(c.features) or "-"
        location = c.region or c.city or "-"
        print(f"{i:2}. {c.name} [{c.id}]")
        print(f"    {location} | {formats}")


# Kept as the name this module used before it grew sessions.
show = show_listing


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Showtimes, prices and seat maps for GSC Cinemas Malaysia.",
        epilog=(
            "examples:  python gsc_scraper.py --json    "
            "python gsc_scraper.py 235 2026-10-14 --prices    "
            "python gsc_scraper.py --movie digger"
        ),
    )
    parser.add_argument("cinema", nargs="?", help="cinema id, slug or name (default: now showing)")
    parser.add_argument("date", nargs="?", help="date as YYYY-MM-DD (default: today, MYT)")
    parser.add_argument("--cinemas", action="store_true", help="list every outlet instead")
    parser.add_argument("--movies", action="store_true", help="list the booking catalogue")
    parser.add_argument("--dates", action="store_true", help="list dates that have sessions")
    parser.add_argument("--regions", action="store_true", help="list regions instead")
    parser.add_argument("--listing", metavar="NAME",
                        help="the website's listing: now-showing, coming-soon, advance-sales")
    parser.add_argument("--all", action="store_true", help="fetch every website listing")
    parser.add_argument("--movie", metavar="SLUG", help="every session of one film")
    parser.add_argument("--detail", metavar="SLUG",
                        help="the website's full record for one film")
    parser.add_argument("--region", help="with --cinemas, only this region")
    parser.add_argument("--city", help="with --cinemas, only this town")
    parser.add_argument("--from", dest="earliest", metavar="HH:MM",
                        help="only showtimes at or after this time")
    parser.add_argument("--until", dest="latest", metavar="HH:MM",
                        help="only showtimes at or before this time")
    parser.add_argument("--format", metavar="CODES",
                        help="only these hall formats, e.g. imax or imax,4dx")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--seats", action="store_true", help="include seat availability")
    parser.add_argument("--seat-map", action="store_true",
                        help="draw the seat map for one session (needs a cinema and --time)")
    parser.add_argument("--prices", action="store_true",
                        help="include ticket prices (one extra call per session)")
    parser.add_argument("--per-person", action="store_true",
                        help="with --prices, quote per person instead of per ticket")
    parser.add_argument("--workers", type=int, default=4, help="parallel cinema requests")
    parser.add_argument("--time", metavar="HH:MM",
                        help="with --seat-map, which session to draw")
    parser.add_argument("--no-delay", action="store_true", help="skip the polite pause")
    args = parser.parse_args()

    delay = 0.0 if args.no_delay else 0.25
    throttle = Throttle(delay)
    net = {"delay": delay, "throttle": throttle}

    if args.regions:
        found = regions(**net)
        if args.json:
            print(json.dumps(found, indent=2, ensure_ascii=False))
        else:
            print()
            for key, label in found.items():
                print(f"  {key:16} {label}")
        return

    if args.dates:
        found = business_dates(**net)
        if args.json:
            print(json.dumps(found, indent=2, ensure_ascii=False))
        else:
            print(f"\ndates with sessions: {len(found)}\n")
            print("  " + "  ".join(found))
        return

    if args.cinemas:
        outlets = cinemas(**net)
        if args.region:
            outlets = by_region(outlets, args.region)
        if args.city:
            outlets = by_city(outlets, args.city)
        if args.json:
            print(json.dumps([c.to_dict() for c in outlets], indent=2, ensure_ascii=False))
        else:
            label = f" in {args.region or args.city}" if (args.region or args.city) else ""
            print(f"\ncinemas{label}: {len(outlets)} outlets\n")
            show_cinemas(outlets)
        return

    if args.movies:
        found = movies(**net)
        if args.json:
            print(json.dumps([m.to_dict() for m in found], indent=2, ensure_ascii=False))
        else:
            print(f"\ncatalogue: {len(found)} films\n")
            for i, m in enumerate(found, 1):
                formats = ", ".join(m.experiences) or "-"
                print(f"{i:2}. {m.title} [{m.id}]")
                print(f"    {', '.join(m.genre) or '-'} | {m.runtime or 'TBA'} min "
                      f"| {m.rating or 'unrated'} | {formats}")
        return

    if args.detail:
        found = detail(args.detail, delay=delay, throttle=throttle)
        if args.json:
            print(json.dumps(found.to_dict(), indent=2, ensure_ascii=False))
            return
        for key, value in found.to_dict().items():
            print(f"{key:18} {value}")
        return

    if args.movie:
        # `--movie` takes an optional cinema and an optional date, in either
        # order of appearance, and argparse cannot tell a date from a cinema
        # name, so decide by shape rather than by position:
        #   --movie slug                      every cinema, today
        #   --movie slug 2026-10-18           every cinema, that date
        #   --movie slug 235                  one cinema, today
        #   --movie slug 235 2026-10-18       one cinema, that date
        movie_cinema, movie_on = args.cinema, args.date
        if args.date is None and args.cinema and looks_like_date(args.cinema):
            movie_on, movie_cinema = args.cinema, None
        elif args.date is not None and looks_like_date(args.date):
            movie_on = args.date
        elif args.date is not None:
            movie_cinema, movie_on = args.date, args.cinema

        if movie_cinema is not None:
            target = find_cinema(cinemas(with_details=False, **net), movie_cinema)
            cinema_ids = [target.id]
        elif args.region or args.city:
            outlets = cinemas(**net)
            narrowed = filter_cinemas(outlets, args.region or args.city)
            if not narrowed:
                raise SystemExit(f"no cinemas in {args.region or args.city!r}")
            cinema_ids = [c.id for c in narrowed]
        else:
            cinema_ids = None

        found = movie(args.movie, movie_on, seats=args.seats, prices=args.prices,
                      cinema_ids=cinema_ids, workers=args.workers,
                      after=args.earliest, before=args.latest, formats=args.format, **net)
        if args.json:
            print(json.dumps([{"cinema_id": cid, **m.to_dict()} for cid, m in found],
                             indent=2, ensure_ascii=False))
        else:
            where = "every cinema" if cinema_ids is None else f"{len(cinema_ids)} cinema(s)"
            print(f"\n{args.movie} on {normalise_date(movie_on)}, {where}: {len(found)} cinemas\n")
            for cid, m in found:
                show_movies([m], f"{m.cinema_name or cid} [{cid}]", args.per_person)
        return

    if args.seat_map:
        if not args.cinema or not args.time:
            raise SystemExit("--seat-map needs a cinema and --time HH:MM")
        if not looks_like_clock(args.time):
            raise SystemExit(f"bad time {args.time!r}, expected HH:MM or h:mmAM/PM")
        target = find_cinema(cinemas(with_details=False, **net), args.cinema)
        when = normalise_date(args.date)
        wanted = clock_candidates(args.time)
        matches = [
            (film, s)
            for film in day(target.id, when, **net)
            for s in film.showtimes
            if s.time in wanted or (s.time_text or "").upper() in {w.upper() for w in wanted}
        ]
        if not matches:
            raise SystemExit(f"no session at {args.time} on {when} at {target.name}")
        for film, showtime in matches:
            plan = seat_plan(target.id, showtime, **net)
            print(f"\n{target.name} [{target.id}] {when} {showtime.time} "
                  f"{showtime.hall or ''} {film.title} (session {showtime.session_id})")
            print(render_seat_plan(plan))
        return

    if args.listing or args.all:
        names = list(LISTINGS) if args.all else [args.listing or "now-showing"]
        if args.json:
            print(
                json.dumps(
                    {name: [m.to_dict() for m in listing(name, delay=delay)]
                     for name in names},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return
        for name in names:
            show_listing(listing(name, delay=delay), name)
        return

    if args.cinema:
        target = find_cinema(cinemas(with_details=False, **net), args.cinema)
        when = normalise_date(args.date)
        # Filters go in, not out: a seat or price call is a request per
        # session, so narrowing after the fact still pays for every discarded
        # screening.
        found = day(target.id, when, seats=args.seats, prices=args.prices,
                    after=args.earliest, before=args.latest, formats=args.format, **net)
        if args.json:
            print(json.dumps([m.to_dict() for m in found], indent=2, ensure_ascii=False))
        else:
            print(f"\n{target.name} [{target.id}] {when}: {len(found)} movies\n")
            show_movies(found, None, args.per_person)
        return

    # No arguments at all: the website's front page listing, which is what a
    # bare invocation has always meant here. A national schedule sweep is one
    # request per outlet, so that has to be asked for.
    show_listing(listing("now-showing", delay=delay), "now-showing")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - keep the CLI readable
        sys.exit(f"error: {exc}")