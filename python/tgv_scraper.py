#!/usr/bin/env python3
"""Showtimes for TGV Cinemas Malaysia, read straight from TGV's own JSON API.

Single file, standard library only. No install, no dependencies.

    python tgv_scraper.py                          # today, every cinema
    python tgv_scraper.py BU0                      # one cinema, today
    python tgv_scraper.py BU0 2026-10-13           # one cinema, one date
    python tgv_scraper.py --cinemas                # the outlet directory
    python tgv_scraper.py --cinemas --region klangvalley
    python tgv_scraper.py --dates                  # dates that have sessions
    python tgv_scraper.py --movie other-mommy      # one movie, every cinema
    python tgv_scraper.py --json                   # machine readable
    python tgv_scraper.py --seats                  # add seat availability
    python tgv_scraper.py --no-delay               # skip the polite pause

Also importable:

    import tgv_scraper as tgv
    tgv.cinemas()
    tgv.regions()
    tgv.business_dates()
    tgv.day("BU0")
    tgv.schedule()
    tgv.movie("other-mommy")

Prices are opt-in and cost one call per session per hall area. Price a single
screening you picked, rather than the whole day:

    late = next(s for f in tgv.day("BU0") for s in f.showtimes if s.time == "21:45")
    tgv.session_prices(late).price

How it works: tgv.com.my is an Angular app that reads a JSON API on
api.tgv.com.my. Every endpoint is a plain POST with a JSON body, or a plain GET,
and none of them want an API key, a signature or a session token. The only
auth-shaped header the app sends is x-mvcsession, which is the logged-in user's
token and is sent empty when browsing anonymously. So this calls the same
endpoints the site itself calls, anonymously.

Times: TGV returns showtimes as naive local strings such as
"2026-10-11T17:45:00". They are Malaysia wall-clock and are attached to a fixed
UTC+08:00 offset here. Malaysia has no daylight saving, so the offset is always
+08:00.
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
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta, timezone

BASE = "https://api.tgv.com.my"
SITE = "https://www.tgv.com.my"
CDN = "https://d9s3e2ooa0od7.cloudfront.net"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# Malaysia wall clock. No DST, so a fixed offset is exact.
MYT = timezone(timedelta(hours=8))

ENDPOINTS = {
    "cinemas": "/api/cinemas/v1/getcinemas",
    "regions": "/api/cinemas/v1/getstatecinema",
    "business_dates": "/api/boxoffice/v1/moviesession_getsessionbusinessdates",
    "day": "/api/boxoffice/v1/moviesession_get",
    "seat_status": "/api/boxoffice/v1/moviesession_getseatstatus",
    "seat_plan": "/api/boxoffice/v1/moviesession_getseatplan",
    "movie_cinemas": "/api/boxoffice/v1/moviesession_getmoviecinemas",
    "tickets": "/api/boxoffice/v1/moviesession_gettickets",
}

# TGV reports money in cents, as integers. Only `priceInCents` is the ticket
# price and is used; `taxInCents` does not reconcile with any Malaysian rate and
# is ignored (see TicketType).
CENT = 100

# A session sells several ticket types at once: the plain seats, then a long
# tail of discounts (SENIOR/OKU, Maybank Mastercard promos, member and card
# rates). The plain seat is what an ordinary customer pays, so it is the one
# `--prices` quotes. Everything else stays available on the showtime's
# `ticket_types` for callers who want the full list.
STANDARD_TICKETS = {"SINGLE", "TWIN", "COUPLE", "DOUBLE", "SOLO"}

# TGV flags a finished promo inside the ticket's longDescription rather than
# with a boolean, e.g. "RM10 ticket with Maybank Mastercard debit card\n
# Today's Promotion Limit Reached". The ticket keeps its listed price either
# way, so it has to be filtered out explicitly.
SOLD_OUT_RE = re.compile(
    r"(promotion\s+limit\s+reached|limit\s+reached|sold\s*out|not\s+available)", re.I
)

# How many people one ticket seats, from the ticket description. The API does
# confirm a bundle via `isPackageTicket`, but it never says how many seats are
# in it, so the count still comes from the name. Unlisted descriptions count as
# 1 so an unfamiliar ticket name never inflates the per-person figure.
TICKET_SEATS = {"SINGLE": 1, "SOLO": 1, "TWIN": 2, "COUPLE": 2, "DOUBLE": 2}

# The API keys every cinema format by a lowercase code. These are just the ones
# seen in the wild; unknown codes are title cased rather than dropped.
EXPERIENCE_LABELS = {
    "deluxe": "Deluxe",
    "beanie": "Beanie",
    "infinity": "Infinity",
    "indy": "Indy",
    "imax": "IMAX",
    "imaxlaser": "IMAX Laser",
    "onyx": "Onyx",
    "indulge": "Indulge",
    "indulge-bed": "Indulge Bed",
    "indulge-coway-berex": "Indulge (Coway Berex)",
    "junior": "Junior",
    "junior-hotwheel": "Junior (Hot Wheels)",
    "premium": "Premium",
    "vip": "VIP",
}


def experience_label(code: str | None) -> str | None:
    """'junior-hotwheel' -> 'Junior (Hot Wheels)'. Unknown codes still render."""
    if not code:
        return None
    key = code.strip().lower()
    if key in EXPERIENCE_LABELS:
        return EXPERIENCE_LABELS[key]
    return re.sub(r"[-_]+", " ", key).strip().title() or None


def clean(value: str | None) -> str | None:
    """Strip whitespace and turn empty strings into None."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def strip_html(value: str | None) -> str | None:
    """TGV stores synopsis as HTML. Collapse it to plain text."""
    if not value:
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    text = text.replace("&hellip;", "...").replace("&amp;", "&")
    text = text.replace("&nbsp;", " ").replace("&quot;", '"').replace("&#39;", "'")
    return clean(re.sub(r"\s+", " ", text))


def as_list(value) -> list[str]:
    """TGV is inconsistent: genre is a list, tags a list, but both can be null."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(item).strip() for item in value if str(item).strip()]


def parse_time(value: str | None) -> datetime | None:
    """'2026-10-11T17:45:00' -> an aware datetime in UTC+08:00."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=MYT)
    except ValueError:
        return None


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


def request(path: str, payload: dict | None = None, timeout: float = 25.0,
            throttle: Throttle | None = None) -> dict:
    """Call one TGV API endpoint and return the decoded body.

    A payload of None sends GET, anything else sends POST with that JSON body.
    """
    if throttle is not None:
        throttle.wait()
    url = f"{BASE}{path}"
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-MY,en;q=0.9",
        # The API is public but the site calls it same-origin. Sending the
        # real Origin/Referer costs nothing and keeps us indistinguishable
        # from normal browsing.
        "Origin": SITE,
        "Referer": f"{SITE}/",
        # The site's own header for an anonymous visitor.
        "x-mvcsession": "",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl()) as response:
            body = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
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
            raise SystemExit(f"TLS error talking to api.tgv.com.my: {exc.reason}") from exc
        raise SystemExit(f"network error talking to api.tgv.com.my: {exc.reason}") from exc

    try:
        return json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"non-JSON reply from {path}") from exc


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


def filter_times(movies: list[Movie], after: str | None = None,
                 before: str | None = None) -> list[Movie]:
    """Keep only the showtimes inside a window. Both bounds are inclusive.

    The window is anchored on the business date, not on the clock alone. TGV
    files a 00:30 show under the previous day's business date but timestamps it
    on the next calendar day, so `--from 18:00` still picks those up: you get
    everything from 18:00 that evening onwards, late shows included.
    """
    low, high = parse_clock(after), parse_clock(before)
    if low is None and high is None:
        return movies

    def keep(showtime: Showtime) -> bool:
        if showtime.start is None:
            return False
        anchor = Date.fromisoformat(showtime.business_date) if showtime.business_date else showtime.start.date()
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

    `--format deluxe` is the budget tier, `--format imaxlaser` the premium one.
    Comma-separate for several: `--format deluxe,indulge`. Matching is on the
    experience code, case-insensitively, and unknown codes simply return
    nothing rather than erroring.
    """
    if not wanted:
        return movies
    codes = {code.strip().casefold() for code in wanted.split(",") if code.strip()}
    for film in movies:
        film.showtimes = tuple(
            s for s in film.showtimes
            if (s.experience or "").casefold() in codes
        )
    return [film for film in movies if film.showtimes]


def _results(body: dict, path: str) -> dict:
    """Unwrap the API's {success, error, results} envelope."""
    if not body.get("success", True):
        raise SystemExit(f"API error from {path}: {body.get('error')}")
    return body.get("results") or {}


@dataclass
class Cinema:
    """One TGV outlet."""

    id: str
    name: str
    keyword: str | None = None
    location: str | None = None  # region key, e.g. "klangvalley"
    region: str | None = None  # region label, e.g. "Klang Valley"
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
    status: int = 1

    @property
    def url(self) -> str:
        if self.location and self.keyword:
            return f"{SITE}/cinemas/{self.location}/{self.keyword}"
        return f"{SITE}/cinemas"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "location": self.location,
            "region": self.region,
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
    """One kind of ticket TGV sells for a session: SINGLE, TWIN, and so on.

    `price` is the advertised ticket price, taken from the API's `priceInCents`.
    That is the figure TGV publishes per hall (1 Utama IMAX reads RM35, which
    matches the published price for that hall) and the one to compare across
    cinemas.

    The API's second money field, `taxInCents`, is deliberately **not** used.
    It is not a tax in the Malaysian sense and it does not reconcile: Malaysia
    levies 8% service tax on services (movie tickets are exempt from it) and
    25% Selangor entertainment duty on cinema admission, yet this field works
    out to exactly 20.00% at 1 Utama and about 9.09% at Pavilion Damansara
    Heights, both of which are in Selangor. Adding it inflated every price and
    made identical tickets look different between outlets.
    """

    code: str
    description: str
    price_cents: int | None = None
    child_only: bool = False
    allocated_seating: bool = False
    # True when the ticket bundles more than one seat (TWIN, COUPLE). This is
    # the API's own `isPackageTicket` flag, so it does not depend on guessing
    # from the description. Compare `per_person`, not `price`, for these.
    package: bool = False
    # Which hall area this price came from. A session has several, each with its
    # own price list, and the same ticket type can differ between them.
    area_code: str | None = None
    # TGV marks a promo as used up in its longDescription, e.g. "RM10 ticket
    # with Maybank Mastercard debit card\nToday's Promotion Limit Reached".
    # Such a ticket is still listed with its price, so it must not be quoted.
    sold_out: bool = False
    # A plain SINGLE/TWIN/COUPLE seat sold at face value. Everything else on a
    # session (senior/OKU, Maybank promos, member rates) is a discounted or
    # special ticket, and `price`/`price_range` deliberately ignore those so the
    # quoted figure is what an ordinary walk-in pays.
    standard: bool = False

    @property
    def price(self) -> float | None:
        """Advertised ticket price in ringgit, from the API's `priceInCents`."""
        return self.price_cents / CENT if self.price_cents is not None else None

    @property
    def seats(self) -> int:
        """How many people this ticket admits. Bundles seat more than one."""
        return TICKET_SEATS.get(self.description.upper(), 1)

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
    """One screening of one movie in one hall."""

    session_id: str
    movie_id: str
    cinema_id: str
    start: datetime | None
    hall: str | None = None
    business_date: str | None = None
    experience: str | None = None
    seat_types: tuple[str, ...] = ()
    status: int = 1
    seats_total: int | None = None
    seats_used: int | None = None
    # Needed to ask for prices; TGV prices tickets per hall area.
    area_codes: tuple[str, ...] = ()
    # --prices only, one entry per ticket type TGV sells for this session
    ticket_types: tuple = ()

    @property
    def experience_name(self) -> str | None:
        return experience_label(self.experience)

    @property
    def single_seat_price(self) -> float | None:
        """Cheapest standard ticket that admits exactly one person.

        This is the figure to compare across cinemas: it ignores TWIN and other
        bundles, which seat two, and ignores concession and promo tickets.
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
        """Promo tickets TGV has marked as no longer obtainable today."""
        return tuple(
            t.description for t in self.ticket_types if t.sold_out
        )

    @property
    def price(self) -> float | None:
        """Cheapest standard single-seat ticket price, in ringgit.

        This is TGV's advertised price, straight from the API's `priceInCents`.
        Bundles (TWIN, COUPLE) seat two people, so they are excluded: quoting
        one here would compare a pair against a single seat. Concession and
        finished promo tickets are excluded too. `price_bundle` has the pair
        price if you want it. Without --prices this is None.
        """
        return self.single_seat_price or (
            min((t.price for t in self.ticket_types
                 if t.price is not None and t.standard), default=None)
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
        """'RM25.00 - RM35.00' when the session sells more than one single price."""
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

        TGV sells bundles (TWIN, COUPLE, DOUBLE) alongside plain SINGLE seats,
        sometimes exclusively. Comparing the raw ticket price across cinemas
        then compares two people against one, so compare this instead.
        """
        prices = [
            t.price / t.seats
            for t in self.ticket_types
            if t.price is not None and t.standard and t.seats
        ]
        return min(prices) if prices else None

    @property
    def time(self) -> str | None:
        return self.start.strftime("%H:%M") if self.start else None

    @property
    def ends(self) -> datetime | None:
        """End time, if this Showtime knows the movie's runtime."""
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

    # Filled in by the caller so ends_at() works without a back reference.
    runtime: int | None = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "movie_id": self.movie_id,
            "cinema_id": self.cinema_id,
            "date": self.business_date,
            "start": self.start.isoformat() if self.start else None,
            "time": self.time,
            "hall": self.hall,
            "experience": self.experience,
            "experience_name": self.experience_name,
            "seat_types": list(self.seat_types),
            "seats_total": self.seats_total,
            "seats_used": self.seats_used,
            "seats_left": self.seats_left,
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
    """A film, with every session found for it in one cinema on one date."""

    id: str
    title: str
    slug: str | None = None
    genre: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    rating: str | None = None
    runtime: int | None = None
    language: str | None = None
    subtitles: tuple[str, ...] = ()
    synopsis: str | None = None
    director: str | None = None
    cast: str | None = None
    distributor: str | None = None
    release_date: str | None = None
    trailer_url: str | None = None
    poster_url: str | None = None
    banner_url: str | None = None
    experiences: tuple[str, ...] = ()
    session_ids: tuple[str, ...] = ()  # the HO codes TGV books against
    showtimes: tuple[Showtime, ...] = ()
    cinema_id: str | None = None
    cinema_name: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"{SITE}/movies/details/{self.slug}" if self.slug else SITE

    @property
    def trailer_id(self) -> str | None:
        """YouTube id from whatever trailer URL shape TGV stored."""
        if not self.trailer_url:
            return None
        match = re.search(
            r"(?:embed/|v=|youtu\.be/|/shorts/)([\w-]{6,})", self.trailer_url
        )
        return match.group(1) if match else None

    @property
    def times(self) -> tuple[str, ...]:
        return tuple(t for t in (s.time for s in self.showtimes) if t)

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
            "language": self.language,
            "subtitles": list(self.subtitles),
            "synopsis": self.synopsis,
            "director": self.director,
            "cast": self.cast,
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
            "showtimes": [s.to_dict() for s in self.showtimes],
        }


def _asset_url(movie: dict, key: str) -> str | None:
    """Build a CDN url for one of a movie's named assets (poster, banner...)."""
    for asset in movie.get("assets") or []:
        if asset.get("assetkey") != key:
            continue
        info = (asset.get("extdata") or {}).get("fileinfo") or {}
        path = info.get("filepath")
        if path:
            return f"{CDN}/{path.lstrip('/')}"
    return None


def parse_cinemas(payload: dict, regions: dict | None = None) -> list[Cinema]:
    """Turn a getcinemas payload into Cinema records.

    `regions` is the optional {region key: label} map from getstatecinema, used
    to give each outlet a readable region name.
    """
    labels = regions or {}
    found: list[Cinema] = []
    seen: set[str] = set()

    for record in payload.get("cinemas") or []:
        extdata = record.get("extdata") or {}
        info = extdata.get("cinema") or {}
        meta = info.get("meta") or {}
        cid = clean(record.get("itemkey"))
        name = clean(record.get("name")) or clean(meta.get("name"))
        if not cid or not name or cid in seen:
            continue
        seen.add(cid)
        location = clean(info.get("location"))
        found.append(
            Cinema(
                id=cid,
                name=name,
                keyword=clean(info.get("keyword")),
                location=location,
                region=labels.get(location),
                address=clean(info.get("address")),
                postcode=clean(info.get("addressPostcode")),
                latitude=clean(info.get("latitude")),
                longitude=clean(info.get("longitude")),
                telephone=clean(info.get("telephone")),
                parking=clean(info.get("parkinginfo")),
                opening_hours=tuple(
                    h for h in as_list(info.get("openingHours")) if h
                ),
                features=tuple(as_list(info.get("features"))),
                experiences=tuple(as_list(extdata.get("exp"))),
                description=strip_html(meta.get("description")),
                status=record.get("status", 1),
            )
        )
    return found


def parse_regions(payload: dict) -> dict:
    """getstatecinema -> {region key: region label}.

    The payload lists every region with its cinemas repeated, including one
    "all" bucket. Only the labels are kept, so a region's label is unambiguous
    even though its cinema list is not.
    """
    labels: dict[str, str] = {}
    for group in payload or []:
        value = clean(group.get("value"))
        label = clean(group.get("label"))
        if value and label and value != "all":
            labels[value] = label
    return labels


def parse_movie(record: dict) -> Movie:
    """Turn one entry of the getcinemas/moviesession payload into a Movie."""
    info = ((record.get("extdata") or {}).get("movieinfo")) or {}
    trailer = info.get("movieTrailer") or None
    if trailer and "youtube.com" not in trailer and "youtu.be" not in trailer:
        trailer = None  # drop anything that is not a YouTube link
    return Movie(
        id=record.get("recid") or "",
        title=clean(record.get("name")) or "Untitled",
        slug=clean(record.get("itemkey"))
        or clean((record.get("extdata") or {}).get("folderkey")),
        genre=tuple(as_list(info.get("genre"))),
        tags=tuple(as_list((record.get("extdata") or {}).get("tags"))),
        rating=clean(info.get("rating")),
        runtime=info.get("runtimemins") if isinstance(info.get("runtimemins"), int) else None,
        language=clean(info.get("lang")),
        subtitles=tuple(as_list(info.get("subtitlelang"))),
        synopsis=strip_html(info.get("synopsis")),
        director=clean(info.get("director")),
        cast=clean(info.get("cast")),
        distributor=clean(info.get("distributor")),
        release_date=clean(info.get("releasedatemy")),
        trailer_url=trailer,
        poster_url=_asset_url(record, "poster"),
        banner_url=_asset_url(record, "banner-desktop"),
        experiences=tuple(as_list((record.get("extdata") or {}).get("exp"))),
        session_ids=tuple(as_list(info.get("hocodes"))),
    )


def parse_showtime(session: dict, cinema_id: str, runtime: int | None = None) -> Showtime:
    return Showtime(
        session_id=str(session.get("sessionid") or ""),
        movie_id=session.get("movieid") or "",
        cinema_id=clean(session.get("cinemaid")) or cinema_id,
        start=parse_time(session.get("showtimemy")),
        hall=clean(session.get("screenname")),
        business_date=clean(session.get("businessdate")),
        experience=clean(session.get("experience")),
        seat_types=tuple(
            t.strip()
            for t in (session.get("seattypes") or "").split("|")
            if t.strip()
        ),
        status=session.get("status", 1),
        runtime=runtime,
        area_codes=tuple(
            (session.get("extdata") or {}).get("areacategorycodes") or []
        ),
    )


def parse_tickets(payload: dict) -> tuple[TicketType, ...]:
    """gettickets -> the ticket types on sale for one session.

    Price comes from the API's `priceInCents` integer-cent field; `taxInCents` is
deliberately not read (see TicketType). `isAvailableFor-
    LoyaltyMembersOnly` and friends are not filtered on: TGV already excludes
    what a walk-in cannot buy.
    """
    found: list[TicketType] = []
    for row in payload.get("tickets") or []:
        # Only priceInCents is read. taxInCents is skipped on purpose: see
        # TicketType for why it does not reconcile with any Malaysian rate.
        price = row.get("priceInCents")
        found.append(
            TicketType(
                code=str(row.get("ticketTypeCode") or ""),
                description=clean(row.get("description")) or "TICKET",
                price_cents=price if isinstance(price, int) else None,
                child_only=bool(row.get("isChildOnlyTicket")),
                allocated_seating=bool(row.get("isAllocatableSeating")),
                package=bool(row.get("isPackageTicket")),
                area_code=clean(row.get("areaCategoryCode")),
                sold_out=bool(
                    SOLD_OUT_RE.search(row.get("longDescription") or "")
                    or SOLD_OUT_RE.search(row.get("description") or "")
                ),
                standard=(clean(row.get("description")) or "").strip().upper()
                in STANDARD_TICKETS,
            )
        )
    return tuple(found)


def parse_seat_status(payload: dict) -> dict[str, tuple[int, int]]:
    """getseatstatus -> {session id: (seats_total, seats_used)}."""
    results: dict[str, tuple[int, int]] = {}
    for row in (payload.get("seatstatuslist") or []):
        sid = row.get("sessionid")
        if sid is None:
            continue
        results[str(sid)] = (
            row.get("seatstotal") or 0,
            row.get("seatsused") or 0,
        )
    return results


# The API reports a seat's state as a bare integer. These are the values seen in
# the wild; anything else is rendered as `?` rather than guessed at, because a
# mislabelled seat is worse than an unreadable one.
SEAT_FREE = 0
SEAT_BOOKED = 1


@dataclass
class Seat:
    """One seat in a hall.

    `row` and `column` are the API's own zero-based grid coordinates, which is
    what carries the hall's shape: columns with no seat in them are aisles, and
    dropping them is what makes a grid look one seat wider on some rows than on
    others. `id` is the number TGV prints on the booking page.
    """

    id: str
    row: int
    column: int
    status: int = SEAT_FREE
    priority: int = 0
    seat_style: int = 0
    seats_in_group: tuple = ()

    @property
    def free(self) -> bool:
        return self.status == SEAT_FREE

    @property
    def booked(self) -> bool:
        return self.status == SEAT_BOOKED

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "row": self.row,
            "column": self.column,
            "status": self.status,
            "free": self.free,
        }


@dataclass
class SeatRow:
    """One row of a hall area. `index` is the API's rowIndexZeroBased."""

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
    """One priced part of a hall, e.g. SINGLE or TWIN.

    A hall sells several kinds of seat at different prices, and TGV returns each
    as its own area with its own layout. The areas share one coordinate space,
    so their columns line up and can be drawn against a single ruler.
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
        return self.description or self.code or f"area {self.number}"

    @property
    def occupied_rows(self) -> tuple[SeatRow, ...]:
        """Rows that actually hold seats, in API order.

        Every area reports a full `rowCount`, but the rows of a second area come
        back as empty placeholders with no name. Drawing those produces blank
        lines and a phantom row count, so they are dropped here.
        """
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


def seat_number(seat: Seat) -> int | None:
    """The seat's printed number, or None if the id is not a plain integer."""
    try:
        return int(str(seat.id).strip())
    except (TypeError, ValueError):
        return None


def anchor_from_row(row: SeatRow) -> dict[int, int]:
    """seat number -> column, read off one row."""
    anchor: dict[int, int] = {}
    for seat in row.seats:
        number = seat_number(seat)
        if number is not None and number not in anchor:
            anchor[number] = seat.column
    return anchor


def number_anchor(area: SeatArea) -> dict[int, int]:
    """seat number -> column, taken from the fullest row of this area.

    The fullest row is the one that spans the hall, so it fixes where each
    number sits. Ties go to the row nearest the screen, which is arbitrary but
    deterministic. An empty area has no anchor and gets an empty map.
    """
    rows = area.occupied_rows
    if not rows:
        return {}
    fullest = max(rows, key=lambda row: (len(row.seats), -row.index))
    return anchor_from_row(fullest)


def hall_anchor(plan: SeatPlan) -> dict[int, int]:
    """seat number -> column for the whole hall, from its single fullest row.

    Both areas in a hall share one column grid: TGV gives them identical
    `left`, `width` and `columnCount`. So the fullest row anywhere in the hall
    fixes where every number sits, and the other area can be lined up on it
    rather than keeping its own private lettering.

    The anchor is then extrapolated past both ends, because a hall's rows do not
    all carry the same numbers. At Pavilion Damansara the SINGLE rows run 3 to 15
    while the TWIN row runs 1 to 10, so seats 1 and 2 have no entry in the anchor
    row at all. They are not stray data: the numbering is a straight run, so they
    belong one and two columns beyond the anchor row's lowest number.
    """
    rows = [row for area in plan.areas for row in area.occupied_rows]
    if not rows:
        return {}
    fullest = max(rows, key=lambda row: (len(row.seats), -row.index))
    anchor = anchor_from_row(fullest)
    # Only extend as far as the numbers actually in use. Beyond that is guesswork
    # about seats that do not exist, and the aisle in the anchor row already
    # shows where the run may not continue unbroken.
    used = [
        number
        for row in rows
        for number in (seat_number(s) for s in row.seats)
        if number is not None
    ]
    return _extrapolate(anchor, low=min(used), high=max(used))


def _extrapolate(anchor: dict[int, int], low: int, high: int) -> dict[int, int]:
    """Extend a number -> column map outwards, at its own spacing, to cover
    `low` through `high`.

    Only the two ends are extended, never the middle: a gap in the middle is an
    aisle, and inventing a column there would put a seat where the hall has a
    walkway.

    The step comes from the anchor's own lowest and highest numbers, so the run
    continues in whichever direction that row was lettered. Stopping at the real
    numbering means a two-seat shortfall is filled with two seats and not eight.
    """
    numbers = sorted(anchor)
    if len(numbers) < 2:
        return dict(anchor)

    out = dict(anchor)
    edge_low, edge_high = numbers[0], numbers[-1]
    low_step = anchor[edge_low] - anchor[edge_low + 1]
    high_step = anchor[edge_high] - anchor[edge_high - 1]

    for step, edge, target, direction in (
        (low_step, edge_low, low, -1),
        (high_step, edge_high, high, 1),
    ):
        if step == 0:
            continue
        number = edge
        column = anchor[edge]
        while number != target:
            number += direction
            column += step
            if column < 0 or number in out:
                break
            out[number] = column
    return out


def _area_fits_hall(area: SeatArea, hall: dict[int, int]) -> bool:
    """Can the hall anchor place every row of this area without a collision?

    A row fits when each of its numbers appears in the hall anchor and the
    resulting columns are all distinct. Anything else means either an unlettered
    number or two seats in one place, so the caller keeps the area's own
    lettering instead.
    """
    if not hall:
        return False
    for row in area.occupied_rows:
        columns: list[int] = []
        for seat in row.seats:
            number = seat_number(seat)
            if number is None or number not in hall:
                return False
            columns.append(hall[number])
        if len(set(columns)) != len(columns):
            return False
    return True


def align_by_number(plan: SeatPlan, shared: bool = True) -> SeatPlan:
    """Return a copy where each seat's column comes from its number.

    TGV gives every row its own columns, and in a row with fewer seats the
    numbering packs against one end, so the same number lands in a different
    column in different rows. Real halls do not work that way: seat 15 is in the
    same place in every row, and a row short of one seat simply has an empty
    space where the missing number would be.

    With `shared`, the anchor is the hall's single fullest row, so the TWIN rows
    line up on the same columns as the SINGLE rows above them. The two areas
    share one grid but do not always share a numbering: where the hall anchor
    cannot place an area without stacking two seats in one column, that area
    falls back to its own map and is named in `conflicting_areas`. An area is
    placed from one map throughout, never a blend of the two. Seats the API
    labels non-numerically keep the column they arrived with.
    """
    moved = 0
    conflicts: list[str] = []
    hall = hall_anchor(plan) if shared else {}
    areas: list[SeatArea] = []

    for area in plan.areas:
        use_hall = shared and _area_fits_hall(area, hall)
        anchor = hall if use_hall else number_anchor(area)
        rows: list[SeatRow] = []
        for row in area.rows:
            seats: list[Seat] = []
            taken: set[int] = set()
            for seat in row.seats:
                number = seat_number(seat)
                column = anchor.get(number) if number is not None else None
                if column is None or column in taken:
                    # Unknown number, or this row already has a seat in that
                    # column: keep the seat where the API put it rather than
                    # stacking two seats in one place.
                    column = seat.column
                    if column in taken:
                        column = -1
                else:
                    if column != seat.column:
                        moved += 1
                    taken.add(column)
                seats.append(
                    Seat(
                        id=seat.id,
                        row=seat.row,
                        column=column,
                        status=seat.status,
                        priority=seat.priority,
                        seat_style=seat.seat_style,
                        seats_in_group=seat.seats_in_group,
                    )
                )
            rows.append(SeatRow(index=row.index, name=row.name, seats=tuple(seats)))
        if shared and hall and not use_hall:
            conflicts.append(area.label)
        areas.append(
            SeatArea(
                number=area.number,
                code=area.code,
                description=area.description,
                seat_count=area.seat_count,
                row_count=area.row_count,
                column_count=area.column_count,
                rows=tuple(rows),
            )
        )
    aligned = SeatPlan(
        cinema_id=plan.cinema_id,
        session_id=plan.session_id,
        areas=tuple(areas),
        boundary_left=plan.boundary_left,
        boundary_right=plan.boundary_right,
        screen_start=plan.screen_start,
        screen_width=plan.screen_width,
    )
    aligned.moved_by_alignment = moved
    aligned.conflicting_areas = tuple(conflicts)
    return aligned


@dataclass
class SeatPlan:
    """The whole hall for one session: every area, and the screen's position."""

    cinema_id: str | None = None
    session_id: str | None = None
    areas: tuple[SeatArea, ...] = ()
    boundary_left: float = 0.0
    boundary_right: float = 0.0
    screen_start: float = 0.0
    screen_width: float = 0.0
    # How many seats align_by_number had to move. 0 on a plan it did not touch.
    moved_by_alignment: int = 0
    # Areas the hall-wide anchor could not letter without stacking two seats in
    # one column, so they kept their own numbering. Empty when every area lined
    # up on the shared grid.
    conflicting_areas: tuple[str, ...] = ()

    @property
    def first_column(self) -> int:
        """Leftmost column any area uses. Shared across areas, so one ruler
        fits every grid on the page."""
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
    def hall_rows(self) -> tuple[tuple[SeatArea, SeatRow], ...]:
        """Every row of the hall, screen end first, paired with its area.

        TGV numbers rows from the back of the hall towards the screen, and the
        payload proves it three ways: the indices form one contiguous run shared
        by all areas rather than restarting per area; the TWIN area, which is
        sold at the back, holds the low indices; and at index 0 the row is named
        `M` while at the highest index it is named `A`.

        So descending index puts the screen end first, and the row names then
        read A, B, C downwards on their own, with the TWIN rows falling at the
        bottom where the low indices put them. Ordering by the raw index instead
        draws the hall upside down.
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
            "areas": [a.to_dict() for a in self.areas],
        }


def parse_seat_plan(payload: dict) -> SeatPlan:
    """getseatplan -> a SeatPlan.

    The endpoint validates its body strictly: `sessionid` must be a bare string
    (getseatstatus takes an array instead), and `req` must be present. Rows
    arrive as a fixed-length list padded with nameless empty entries, and each
    seat carries its own grid position, so the aisle columns are recoverable.
    """
    layout = payload.get("seatlayout") or {}
    areas: list[SeatArea] = []

    for area in layout.get("areas") or []:
        rows: list[SeatRow] = []
        for row in area.get("rows") or []:
            seats = tuple(
                Seat(
                    id=str(seat.get("id")),
                    row=(seat.get("position") or {}).get("rowIndex", row.get("rowIndexZeroBased")),
                    column=(seat.get("position") or {}).get("columnIndex", 0),
                    status=seat.get("status", SEAT_FREE),
                    priority=seat.get("priority", 0),
                    seat_style=seat.get("seatStyle", 0),
                    seats_in_group=tuple(seat.get("seatsInGroup") or ()),
                )
                for seat in (row.get("seats") or [])
                if seat.get("id") is not None
            )
            rows.append(
                SeatRow(
                    index=row.get("rowIndexZeroBased", 0),
                    name=clean(row.get("physicalName")),
                    seats=seats,
                )
            )

        areas.append(
            SeatArea(
                number=area.get("number", 0),
                code=clean(area.get("areaCategoryCode")),
                description=clean(area.get("description")),
                seat_count=area.get("numberOfSeats", 0),
                row_count=area.get("rowCount", 0),
                column_count=area.get("columnCount", 0),
                rows=tuple(sorted(rows, key=lambda r: r.index)),
            )
        )

    return SeatPlan(
        cinema_id=clean(payload.get("cinemaid")),
        session_id=clean(payload.get("sessionid")),
        areas=tuple(areas),
        boundary_left=layout.get("boundaryLeft", 0.0),
        boundary_right=layout.get("boundaryRight", 0.0),
        screen_start=layout.get("screenStart", 0.0),
        screen_width=layout.get("screenWidth", 0.0),
    )


def parse_day(payload: dict, cinema_id: str, cinema_name: str | None = None) -> list[Movie]:
    """Turn one moviesession_get payload into movies carrying their sessions.

    The payload nests the same film twice: a detail record under
    `results.movies`, and a bare id under
    `results.businessday.cinemas[].movies[]` next to the sessions. They are
    joined on the movie id.
    """
    details = {
        record.get("recid"): record
        for record in (payload.get("movies") or [])
        if record.get("recid")
    }

    movies: list[Movie] = []
    seen: set[str] = set()

    for cinema in (payload.get("businessday") or {}).get("cinemas") or []:
        cinema_id = clean(cinema.get("cinemaid")) or cinema_id
        cinema_name = clean(cinema.get("name")) or cinema_name

        for entry in cinema.get("movies") or []:
            movie_id = entry.get("movieid") or ""
            record = details.get(movie_id)
            if not record:
                # Sessions with no detail record still deserve to show up.
                record = {"recid": movie_id, "name": movie_id, "extdata": {}}
            movie = parse_movie(record)

            showtimes: list[Showtime] = []
            for group in entry.get("experiences") or []:
                for session in group.get("sessions") or []:
                    showtimes.append(
                        parse_showtime(session, cinema_id, movie.runtime)
                    )
            showtimes.sort(key=lambda s: (s.start is None, s.start))

            movie.cinema_id = cinema_id
            movie.cinema_name = cinema_name
            movie.showtimes = tuple(showtimes)

            if movie_id in seen:
                # Same film listed twice under one cinema, e.g. two blocs.
                existing = next(m for m in movies if m.id == movie_id)
                existing.showtimes = tuple(
                    sorted(existing.showtimes + movie.showtimes, key=lambda s: (s.start is None, s.start))
                )
                continue
            seen.add(movie_id)
            movies.append(movie)

    movies.sort(key=lambda m: m.title.casefold())
    return movies


def apply_seat_status(
    movies: list[Movie], seats: dict[str, tuple[int, int]]
) -> list[Movie]:
    """Fold getseatstatus numbers onto the showtimes, in place."""
    for movie in movies:
        for showtime in movie.showtimes:
            if showtime.session_id in seats:
                showtime.seats_total, showtime.seats_used = seats[showtime.session_id]
    return movies


def regions(**kwargs) -> dict:
    """{region key: region label} for every TGV region."""
    throttle = kwargs.pop("throttle", None) or Throttle(kwargs.get("delay", 0.0))
    body = request(ENDPOINTS["regions"], timeout=kwargs.get("timeout", 25.0), throttle=throttle)
    return parse_regions(_results(body, "regions"))


def cinemas(with_details: bool = True, **kwargs) -> list[Cinema]:
    """Every TGV outlet in Malaysia.

    with_details=True adds addresses, coordinates and formats, which costs one
    more request for the region labels. with_details=False returns the lighter
    id and name list.
    """
    throttle = kwargs.pop("throttle", None) or Throttle(kwargs.get("delay", 0.0))
    timeout = kwargs.get("timeout", 25.0)

    if not with_details:
        body = request(ENDPOINTS["regions"], timeout=timeout, throttle=throttle)
        found: dict[str, Cinema] = {}
        for group in _results(body, "regions"):
            for record in group.get("cinemas") or []:
                cid = clean(record.get("cinemaid"))
                if cid and cid not in found:
                    found[cid] = Cinema(id=cid, name=clean(record.get("name")) or cid)
        return sorted(found.values(), key=lambda c: c.name)

    body = request(
        ENDPOINTS["cinemas"],
        {"startrowindex": 0, "maxrows": 500},
        timeout=timeout,
        throttle=throttle,
    )
    labels = parse_regions(
        _results(request(ENDPOINTS["regions"], timeout=timeout, throttle=throttle), "regions")
    )
    found = parse_cinemas(_results(body, "cinemas"), labels)
    return sorted(found, key=lambda c: c.name)


def business_dates(**kwargs) -> list[str]:
    """Every date the API will answer for, ascending.

    This is the set of business dates across all cinemas, so it includes a few
    special and advance dates that have no sessions today. Check with `day()`
    before trusting a date to have anything on it.
    """
    throttle = kwargs.pop("throttle", None) or Throttle(kwargs.get("delay", 0.0))
    body = request(
        ENDPOINTS["business_dates"], {}, timeout=kwargs.get("timeout", 25.0), throttle=throttle
    )
    return sorted(_results(body, "business_dates").get("businessdates") or [])


def day(cinema_id: str, on: str | Date | None = None, seats: bool = False,
        prices: bool = False, after: str | None = None, before: str | None = None,
        formats: str | None = None, workers: int = 8, **kwargs) -> list[Movie]:
    """Every movie and every session at one cinema on one date.

    seats adds remaining-seat counts (one extra call). prices adds ticket
    prices (one extra call per session per hall area, so it is opt-in).

    `after`, `before` and `formats` narrow the result. They are applied to the
    parsed day **before** seats and prices are fetched, not after. That ordering
    is the whole point: a price costs one request per session per area, so
    filtering afterwards still pays for every screening you just threw away.
    """
    throttle = kwargs.pop("throttle", None) or Throttle(kwargs.get("delay", 0.0))
    timeout = kwargs.get("timeout", 25.0)
    when = normalise_date(on)
    cid = str(cinema_id).strip().upper()

    body = request(
        ENDPOINTS["day"],
        {"cinemaid": cid, "businessdate": when},
        timeout=timeout,
        throttle=throttle,
    )
    movies = parse_day(_results(body, "day"), cid)
    movies = filter_formats(filter_times(movies, after, before), formats)
    if seats:
        apply_seat_status(movies, _seat_status(cid, movies, throttle, timeout))
    if prices:
        _attach_prices(movies, throttle, timeout, workers)
    return movies


def _seat_status(cinema_id: str, movies: list[Movie], throttle: Throttle, timeout: float) -> dict:
    """One getseatstatus call covering every session in the batch."""
    ids = [s.session_id for m in movies for s in m.showtimes if s.session_id]
    if not ids:
        return {}
    body = request(
        ENDPOINTS["seat_status"],
        {"cinemaid": cinema_id, "sessionid": ids},
        timeout=timeout,
        throttle=throttle,
    )
    return parse_seat_status(_results(body, "seat_status"))


def tickets(cinema_id: str, session_id: str, area_code: str,
            business_date: str | Date | None = None, **kwargs) -> tuple[TicketType, ...]:
    """Every ticket type on sale for one session in one hall area.

    The lowest-level pricing call: a single `gettickets` request, no batching and
    no filtering. TGV takes one session id and one area code at a time, so a
    session with several hall areas needs one call per area.

        tgv.tickets("BU0", "327053", "0000000002", "2026-10-18")

    Prefer `session_prices()` unless you know the area code already; it loops the
    areas for you and hands back a priced `Showtime`.
    """
    body = request(
        ENDPOINTS["tickets"],
        {
            "cinemaid": str(cinema_id).strip().upper(),
            "sessionid": str(session_id),
            "businessdate": normalise_date(business_date),
            "req": "x",
            "areacategorycodes": area_code,
        },
        timeout=kwargs.get("timeout", 25.0),
        throttle=kwargs.get("throttle"),
    )
    out = []
    for ticket in parse_tickets(_results(body, "tickets")):
        ticket.area_code = area_code
        out.append(ticket)
    return tuple(out)


def session_prices(showtime: Showtime, **kwargs) -> Showtime:
    """Price one screening and return it.

    This is the on-demand path: you pick the session, this prices that session
    and nothing else. `day(..., prices=True)` and friends are the bulk path and
    loop this logic over every showtime they kept.

        films = tgv.day("BU0", "2026-10-18")
        late = [s for f in films for s in f.showtimes if s.time == "21:45"][0]
        tgv.session_prices(late).price       # 30.0

    Mutates and returns the showtime, so `showtime.price` reads the same
    afterwards. The result is the cheapest standard single seat;
    `price_bundle` has the pair price if the hall sells one.
    """
    throttle = kwargs.get("throttle")
    timeout = kwargs.get("timeout", 25.0)
    collected: list[TicketType] = []
    for code in showtime.area_codes:
        if not showtime.session_id:
            continue
        try:
            collected.extend(tickets(showtime.cinema_id, showtime.session_id, code,
                                     showtime.business_date, throttle=throttle, timeout=timeout))
        except SystemExit as exc:
            # One dead area must not cost the session its other areas, which may
            # hold the single seat. Reported, then move on.
            print(f"tgv: price lookup failed, {showtime.time} area {code}: {exc}",
                  file=sys.stderr)
    showtime.ticket_types = tuple(collected)
    return showtime


def _attach_prices(movies: list[Movie], throttle: Throttle, timeout: float,
                   workers: int = 8) -> None:
    """Fill in ticket prices on every showtime, in place.

    Prices only get fetched for the showtimes still in `movies`, so a narrowed
    query (`--from`, `--until`, `--format`, or one film at one cinema) costs
    only the showtimes it kept. Filter first, then call this.

    Costs one request per session per hall area, which is why it is opt-in.
    Those go out in parallel: the throttle still holds the *global* rate, so the
    request count is unchanged and a run stops being serial.

    A failed call does not take the run down with it. That showtime is left
    without a price and reported on stderr, so it prints as a missing price
    rather than a missing screening.
    """
    jobs: list[Showtime] = [
        showtime
        for film in movies
        for showtime in film.showtimes
        if showtime.session_id and showtime.area_codes
    ]
    if not jobs:
        return

    workers = max(1, min(workers, len(jobs)))
    failed: list[str] = []
    lock = threading.Lock()

    def one(showtime: Showtime) -> tuple[int, tuple[TicketType, ...]]:
        try:
            priced = session_prices(showtime, throttle=throttle, timeout=timeout)
            return id(showtime), priced.ticket_types
        except SystemExit as exc:
            # request() reports network and HTTP problems as SystemExit. Losing
            # one area's price list must not abort the rest of the run.
            with lock:
                failed.append(f"{showtime.time}: {exc}")
            return id(showtime), ()

    collected: dict[int, list[TicketType]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for sid, found in pool.map(one, jobs):
            collected.setdefault(sid, []).extend(found)

    for film in movies:
        for showtime in film.showtimes:
            showtime.ticket_types = tuple(collected.get(id(showtime), ()))

    for problem in failed:
        print(f"tgv: price lookup failed, {problem}", file=sys.stderr)


def schedule(cinema_ids: list[str] | None = None, on: str | Date | None = None,
             seats: bool = False, prices: bool = False, workers: int = 4,
             after: str | None = None, before: str | None = None,
             formats: str | None = None, **kwargs) -> dict[str, list[Movie]]:
    """{cinema id: movies} for one date across many cinemas.

    Each cinema is one request, so a national run is 45 calls. They go out in
    parallel but are still rate limited by the throttle. prices=True adds one
    more request per session per area on top, which is a lot, so it is off by
    default.

    `after`, `before` and `formats` are applied per cinema before seats and
    prices are fetched, so a filtered run never pays for screenings it drops.
    """
    throttle = Throttle(kwargs.get("delay", 0.25))
    timeout = kwargs.get("timeout", 25.0)
    when = normalise_date(on)

    if cinema_ids is None:
        cinema_ids = [c.id for c in cinemas(with_details=False, throttle=throttle, timeout=timeout)]
    else:
        cinema_ids = [str(c).strip().upper() for c in cinema_ids]

    def one(cid: str) -> tuple[str, list[Movie]]:
        body = request(
            ENDPOINTS["day"],
            {"cinemaid": cid, "businessdate": when},
            timeout=timeout,
            throttle=throttle,
        )
        movies = parse_day(_results(body, "day"), cid)
        movies = filter_formats(filter_times(movies, after, before), formats)
        if seats:
            apply_seat_status(movies, _seat_status(cid, movies, throttle, timeout))
        if prices:
            _attach_prices(movies, throttle, timeout, workers)
        return cid, movies

    workers = max(1, min(workers, len(cinema_ids) or 1))
    results: dict[str, list[Movie]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for cid, movies in pool.map(one, cinema_ids):
            results[cid] = movies
    return {cid: results[cid] for cid in cinema_ids if results.get(cid)}


def movie(slug_or_title: str, on: str | Date | None = None, seats: bool = False,
          prices: bool = False, cinema_ids: list[str] | None = None,
          workers: int = 4, after: str | None = None, before: str | None = None,
          formats: str | None = None, **kwargs) -> list[tuple[str, Movie]]:
    """Every session of one film, across cinemas, as [(cinema id, Movie)].

    The API has no movie-centric session lookup that returns times, so this
    walks the same cinema-by-cinema day payload and keeps the matches.

    Pass `cinema_ids` to stay on one or a few outlets. Left as None it sweeps
    every cinema in the country, and with `prices=True` that is a price lookup
    per session per area at all 45 of them.
    """
    throttle = Throttle(kwargs.get("delay", 0.25))
    timeout = kwargs.get("timeout", 25.0)
    wanted = slug_or_title.strip().casefold().replace(" ", "-")
    when = normalise_date(on)

    if cinema_ids is None:
        cinema_ids = [c.id for c in cinemas(with_details=False, throttle=throttle, timeout=timeout)]
    else:
        cinema_ids = [str(c).strip().upper() for c in cinema_ids]

    def one(cid: str) -> tuple[str, list[Movie]]:
        body = request(
            ENDPOINTS["day"],
            {"cinemaid": cid, "businessdate": when},
            timeout=timeout,
            throttle=throttle,
        )
        return cid, parse_day(_results(body, "day"), cid)

    def hits(film: Movie) -> bool:
        return film.slug == wanted or film.title.casefold() == slug_or_title.strip().casefold()

    found: list[tuple[str, Movie]] = []
    workers = max(1, min(workers, len(cinema_ids) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for cid, movies in pool.map(one, cinema_ids):
            for film in movies:
                if not hits(film):
                    continue
                # Narrow before enriching: a price call per session per area is
                # the single most expensive thing this script does. Both filters
                # rewrite film.showtimes in place as well as returning a list.
                filter_formats(filter_times([film], after, before), formats)
                if not film.showtimes:
                    continue
                if seats:
                    apply_seat_status([film], _seat_status(cid, [film], throttle, timeout))
                if prices:
                    _attach_prices([film], throttle, timeout, workers)
                found.append((cid, film))

    if not found:
        hint = ""
        if after or before or formats:
            hint = " with those --from/--until/--format filters"
        raise SystemExit(f"no sessions for {slug_or_title!r} on {when}{hint}")
    return found


def seat_status(cinema_id: str, session_ids: list[str], **kwargs) -> dict:
    """Raw {session id: {seats_total, seats_used, used_percentage}}."""
    throttle = kwargs.pop("throttle", None) or Throttle(kwargs.get("delay", 0.0))
    body = request(
        ENDPOINTS["seat_status"],
        {"cinemaid": str(cinema_id).strip().upper(), "sessionid": [str(s) for s in session_ids]},
        timeout=kwargs.get("timeout", 25.0),
        throttle=throttle,
    )
    rows = {
        str(r.get("sessionid")): r
        for r in (_results(body, "seat_status").get("seatstatuslist") or [])
    }
    return {
        sid: {
            "seats_total": row.get("seatstotal"),
            "seats_used": row.get("seatsused"),
            "seats_left": max(0, (row.get("seatstotal") or 0) - (row.get("seatsused") or 0)),
            "used_percentage": row.get("usedpercentage"),
        }
        for sid, row in rows.items()
    }


def seat_plan(cinema_id: str, showtime: Showtime | str, **kwargs) -> SeatPlan:
    """The full seat map for one session, aisle positions included.

    `getseatstatus` answers "how many are left"; this answers "which ones". One
    call per session, and it is the only way to see the actual grid, because the
    day payload carries counts but never seat coordinates.

        late = [s for f in tgv.day("BU0", "2026-10-18") for s in f.showtimes][-1]
        plan = tgv.seat_plan("BU0", late)
        print(tgv.render_seat_plan(plan))

    The body is stricter than the other endpoints: `sessionid` is a bare string
    here, not the array `getseatstatus` wants, and `req` must be sent or the API
    answers 400 with two validation errors at once.
    """
    throttle = kwargs.get("throttle")
    session_id = showtime if isinstance(showtime, str) else showtime.session_id
    if not session_id:
        raise SystemExit("seat_plan needs a session id")
    on = None if isinstance(showtime, str) else showtime.business_date
    body = request(
        ENDPOINTS["seat_plan"],
        {
            "cinemaid": str(cinema_id).strip().upper(),
            "sessionid": str(session_id),
            "businessdate": normalise_date(on),
            "req": "x",
        },
        timeout=kwargs.get("timeout", 25.0),
        throttle=throttle,
    )
    return parse_seat_plan(_results(body, "seat_plan"))


SEAT_MARKS = {SEAT_FREE: None, SEAT_BOOKED: "X"}


def _seat_cell(seat: Seat | None, width: int) -> str:
    """One fixed-width cell. Blank for an aisle, the seat number when free, `X`
    when taken. The width is constant so every column lines up down the grid."""
    if seat is None:
        return " " * width
    mark = SEAT_MARKS.get(seat.status)
    return (mark if mark else seat.id).rjust(width)


def render_seat_plan(plan: SeatPlan, width: int = 3, ruler: bool = True,
                     align: bool = True) -> str:
    """Draw a hall as fixed-width text.

    With `align`, seat numbers are laid out on a single column map per area, the
    way a real hall is lettered, so a short row shows a gap where its missing
    number would be instead of sliding every number sideways. Pass False to see
    the raw per-row columns TGV returned instead.

    Every cell is `width` characters, and a column with no seat in it is left
    blank rather than closed up. That is the whole point: TGV's rows genuinely
    differ in length (a short front row, aisles mid-hall), and packing the
    numbers together makes a wide row look like it has an extra seat. Rendering
    the column positions is what makes the aisles visible.

    Row order is the API's own, top of the grid first. TGV's `physicalName` for
    that end is unknowable from this payload: there is no screen-facing field,
    only a screen position for the whole hall. Row labels therefore stay as TGV
    numbers them, and `relabel=True` is available when you want plain A, B, C
    downwards instead.
    """
    if align:
        plan = align_by_number(plan)
    first, last = plan.first_column, plan.last_column
    columns = list(range(first, last + 1))
    label_width = max(
        (len(row.name or "") for area in plan.areas for row in area.rows), default=1
    ) + 2

    out: list[str] = []
    if ruler and columns:
        out.append(" " * label_width + "".join(str(c).rjust(width) for c in columns))
        # Ticks are as wide as the label they sit under and right-aligned to
        # match it. A fixed-width dash fills the whole cell and lands one
        # character left of the digit, which reads as every column being shifted.
        out.append(
            " " * label_width
            + "".join(("-" * len(str(c))).rjust(width) for c in columns)
            .rstrip()
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
                + ("  (own numbering, this hall's areas disagree)"
                   if area.label in plan.conflicting_areas else "")
            )
        by_column = {seat.column: seat for seat in row.seats}
        out.append(
            (row.name or str(row.index)).ljust(label_width)
            + "".join(_seat_cell(by_column.get(c), width) for c in columns)
        )
        drawn_for[area.number] += len(row.seats)

    # Proof the drawing matches the payload, so a miscount shows up here rather
    # than being argued about.
    for area in plan.areas:
        counted = drawn_for.get(area.number, 0)
        if not counted:
            continue
        out.append(
            f"{area.label}: {counted} seats drawn"
            + ("" if counted == area.seat_count else f" (API says {area.seat_count})")
        )
    return "\n".join(out)


def by_region(outlets: list[Cinema], region: str) -> list[Cinema]:
    """Narrow an already-fetched cinema list to one region.

    Accepts either the region key ("klangvalley") or its label ("Klang Valley"),
    case-insensitively.
    """
    wanted = region.strip().casefold()
    return [
        c for c in outlets
        if wanted in {(c.region or "").casefold(), (c.location or "").casefold()}
    ]


def find_cinema(outlets: list[Cinema], needle: str) -> Cinema:
    """Look one outlet up by id, name or keyword, case-insensitively."""
    key = needle.strip().casefold()
    for c in outlets:
        if c.id.casefold() == key or (c.keyword or "").casefold() == key:
            return c
    for c in outlets:
        if c.name.casefold() == key:
            return c
    hits = [c for c in outlets if key in c.name.casefold()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise SystemExit(
            f"{needle!r} matches {len(hits)} cinemas: "
            + ", ".join(f"{c.id} {c.name}" for c in hits[:8])
        )
    raise SystemExit(f"no cinema matching {needle!r}")


def _session_line(showtime: Showtime, per_person: bool = False) -> str:
    parts = [showtime.time or "??:??", showtime.hall or "-"]
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
    if cinema_label:
        print(f"\n=== {cinema_label}\n")
    for film in movies:
        bits = [b for b in (", ".join(film.genre), f"{film.runtime} min" if film.runtime else None,
                            film.rating, film.language) if b]
        print(f"  {film.title}" + (f"  ({' | '.join(bits)})" if bits else ""))
        for showtime in film.showtimes:
            print(f"      {_session_line(showtime, per_person)}")


def show_schedule(schedule_data: dict[str, list[Movie]],
                  per_person: bool = False) -> None:
    for cid, movies in schedule_data.items():
        show_movies(movies, f"{movies[0].cinema_name or cid} [{cid}]" if movies else cid,
                    per_person)


def show_cinemas(outlets: list[Cinema]) -> None:
    for i, c in enumerate(outlets, 1):
        formats = ", ".join(c.experiences) or "-"
        location = c.region or c.location or "-"
        print(f"{i:2}. {c.name} [{c.id}]")
        print(f"    {location} | {formats}")


def _looks_like_date(value: str | None) -> bool:
    """True when a positional argument is a date rather than a cinema name.

    The CLI takes an optional cinema and an optional date for `--movie`, and
    argparse cannot tell them apart by position alone. Deciding by shape works
    because every cinema id and name contains a letter and no date does.
    """
    if not value:
        return False
    value = value.strip()
    if not value:
        return False
    try:
        normalise_date(value)
    except SystemExit:
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Showtimes for TGV Cinemas Malaysia.",
        epilog=(
            "examples:  python tgv_scraper.py BU0    "
            "python tgv_scraper.py --cinemas    python tgv_scraper.py --movie other-mommy"
        ),
    )
    parser.add_argument("cinema", nargs="?", help="cinema id, name or keyword (default: all cinemas)")
    parser.add_argument("date", nargs="?", help="date as YYYY-MM-DD (default: today, MYT)")
    parser.add_argument("--cinemas", action="store_true", help="list every outlet instead")
    parser.add_argument("--regions", action="store_true", help="list regions instead")
    parser.add_argument("--dates", action="store_true", help="list dates that have sessions")
    parser.add_argument("--movie", metavar="SLUG", help="every session of one film")
    parser.add_argument("--region", help="with --cinemas, only this region")
    parser.add_argument("--from", dest="earliest", metavar="HH:MM",
                        help="only showtimes at or after this time")
    parser.add_argument("--until", dest="latest", metavar="HH:MM",
                        help="only showtimes at or before this time")
    parser.add_argument("--format", metavar="CODES",
                        help="only these hall formats, e.g. deluxe or deluxe,indulge")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--seats", action="store_true", help="include seat availability")
    parser.add_argument("--seat-map", action="store_true",
                        help="draw the seat map for one session (needs a cinema, "
                             "a date and --time)")
    parser.add_argument("--prices", action="store_true",
                        help="include ticket prices (one extra call per showtime)")
    parser.add_argument("--per-person", action="store_true",
                        help="with --prices, quote per person instead of per ticket")
    parser.add_argument("--workers", type=int, default=4, help="parallel cinema requests")
    parser.add_argument("--time", metavar="HH:MM",
                        help="with --seat-map, which session to draw")
    parser.add_argument("--raw-columns", action="store_true",
                        help="with --seat-map, draw TGV's per-row columns "
                             "instead of aligning seats by number")
    parser.add_argument("--no-delay", action="store_true", help="skip the polite pause")
    args = parser.parse_args()

    delay = 0.0 if args.no_delay else 0.25
    throttle = Throttle(delay)
    net = {"delay": delay, "throttle": throttle}

    if args.regions:
        found = regions(**{**net, "throttle": throttle})
        if args.json:
            print(json.dumps(found, indent=2, ensure_ascii=False))
        else:
            print()
            for key, label in sorted(found.items(), key=lambda kv: kv[1]):
                print(f"  {key:16} {label}")
        return

    if args.dates:
        found = business_dates(**{**net, "throttle": throttle})
        if args.json:
            print(json.dumps(found, indent=2, ensure_ascii=False))
        else:
            print(f"\ndates with sessions: {len(found)}\n")
            print("  " + "  ".join(found))
        return

    if args.cinemas:
        outlets = cinemas(**{**net, "throttle": throttle})
        if args.region:
            outlets = by_region(outlets, args.region)
        if args.json:
            print(json.dumps([c.to_dict() for c in outlets], indent=2, ensure_ascii=False))
        else:
            label = f" in {args.region}" if args.region else ""
            print(f"\ncinemas{label}: {len(outlets)} outlets\n")
            show_cinemas(outlets)
        return

    if args.movie:
        # `--movie` accepts an optional cinema and date, in either order of
        # appearance, and argparse cannot tell a date from a cinema name, so
        # decide by shape rather than by position:
        #   --movie slug                      every cinema, today
        #   --movie slug 2026-10-18           every cinema, that date
        #   --movie slug BU0                  one cinema, today
        #   --movie slug BU0 2026-10-18       one cinema, that date
        # The old rule keyed off `not value.isalpha()`, so `BU0` was read as a
        # date, the cinema was silently dropped, and the run went national.
        movie_cinema, movie_on = args.cinema, args.date
        if args.date is None and args.cinema and _looks_like_date(args.cinema):
            movie_on, movie_cinema = args.cinema, None
        elif args.date is not None and _looks_like_date(args.date):
            movie_on = args.date
        elif args.date is not None:
            # Second positional was a cinema, so swap them.
            movie_on, movie_cinema = movie_cinema, args.date
        if movie_cinema is not None:
            target = find_cinema(cinemas(with_details=False, **net), movie_cinema)
            cinema_ids = [target.id]
        elif args.region:
            # Needs the full directory: with_details=False carries no location,
            # so by_region would silently match nothing.
            cinema_ids = [c.id for c in by_region(cinemas(**net), args.region)]
            if not cinema_ids:
                raise SystemExit(f"no cinemas in region {args.region!r}")
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
        target = find_cinema(cinemas(with_details=False, **net), args.cinema)
        when = normalise_date(args.date)
        wanted = args.time.strip()
        matches = [
            (film, s)
            for film in day(target.id, when, **net)
            for s in film.showtimes
            if s.time == wanted
        ]
        if not matches:
            raise SystemExit(f"no session at {wanted} on {when} at {target.name}")
        for film, showtime in matches:
            plan = seat_plan(target.id, showtime, **{**net, "throttle": throttle})
            print(f"\n{target.name} [{target.id}] {when} {showtime.time} "
                  f"{showtime.hall or ''} {film.title} (session {showtime.session_id})")
            print(render_seat_plan(plan, align=not args.raw_columns))
        return

    on = normalise_date(args.date)

    if args.cinema:
        target = find_cinema(cinemas(with_details=False, **{**net, "throttle": throttle}), args.cinema)
        # Filters go in, not out: prices cost one call per session per area, so
        # narrowing after the fact still pays for every discarded screening.
        movies = day(target.id, on, seats=args.seats, prices=args.prices,
                     after=args.earliest, before=args.latest, formats=args.format, **net)
        if args.json:
            print(json.dumps([m.to_dict() for m in movies], indent=2, ensure_ascii=False))
        else:
            print(f"\n{target.name} [{target.id}] {on}: {len(movies)} movies\n")
            show_movies(movies, None, args.per_person)
        return

    everything = schedule(on=on, seats=args.seats, prices=args.prices,
                          workers=args.workers, after=args.earliest,
                          before=args.latest, formats=args.format, **net)
    if args.json:
        print(json.dumps({cid: [m.to_dict() for m in movies] for cid, movies in everything.items()},
                         indent=2, ensure_ascii=False))
    else:
        total = sum(len(movies) for movies in everything.values())
        print(f"\n{on}: {len(everything)} cinemas, {total} movie slots\n")
        show_schedule(everything, args.per_person)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - keep the CLI readable
        sys.exit(f"error: {exc}")