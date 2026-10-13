#!/usr/bin/env python3
"""Get the list of movies now showing at GSC Cinemas (Malaysia).

Single file, standard library only. No install, no dependencies.

    python gsc_scraper.py                          # now showing
    python gsc_scraper.py coming-soon
    python gsc_scraper.py advance-sales
    python gsc_scraper.py cinemas                  # every GSC outlet
    python gsc_scraper.py --json                   # machine readable
    python gsc_scraper.py "munafik-melawan-iblis"  # detail for one movie
    python gsc_scraper.py --all                    # every listing, counted

Also importable:

    import gsc_scraper as gsc
    gsc.listing("now-showing")
    gsc.movie("verity")
    gsc.cinemas()

How it works: gsc.com.my is a Nuxt app, but the pages are server rendered, so
the movies are sitting in the HTML. We pull the page and read them out.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

BASE = "https://www.gsc.com.my"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
LISTINGS = {
    "now-showing": "/movies/now-showing/",
    "coming-soon": "/movies/coming-soon/",
    "advance-sales": "/movies/advance-sales/",
}
# Not a movie listing, so it lives outside LISTINGS: its parser is different.
CINEMAS = "/cinemas/gsc/"
# GSC uses these in place of real data.
PLACEHOLDERS = {"N/A", "NA", "-", "TBC", "TBA"}


def clean(value: str | None) -> str | None:
    """Strip whitespace and drop GSC's placeholder text."""
    if value is None:
        return None
    value = value.strip()
    return None if not value or value.upper() in PLACEHOLDERS else value


def split_codes(value: str | None) -> tuple[str, ...]:
    """'ENG,BM,CHI' -> ('ENG', 'BM', 'CHI')."""
    if not value:
        return ()
    parts = (clean(part) for part in value.split(","))
    return tuple(part for part in parts if part)


def duration_minutes(value: str | None) -> int | None:
    """'1 hr 45 mins' -> 105. None when the runtime is unknown."""
    if not value:
        return None
    match = re.search(r"(?:(\d+)\s*h(?:rs?)?\s*)?(?:(\d+)\s*m(?:ins?)?)?", value, re.I)
    if not match or not match.groups():
        return None
    total = int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
    return total or None


@dataclass
class Movie:
    title: str
    slug: str
    genre: str | None = None
    duration: str | None = None
    language: str | None = None
    subtitles: tuple[str, ...] = ()
    rating: str | None = None
    poster_url: str | None = None
    booking_url: str | None = None
    booking_id: int | None = None
    # detail page only
    synopsis: str | None = None
    director: str | None = None
    main_stars: tuple[str, ...] = ()
    trailer_url: str | None = None
    release_date: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"{BASE}/movie/{self.slug}/"

    @property
    def languages(self) -> tuple[str, ...]:
        """Audio codes as a tuple. GSC joins several with commas."""
        return split_codes(self.language)

    @property
    def runtime(self) -> int | None:
        return duration_minutes(self.duration)

    @property
    def trailer_id(self) -> str | None:
        if not self.trailer_url:
            return None
        match = re.search(r"(?:embed/|v=|youtu\.be/)([\w-]{6,})", self.trailer_url)
        return match.group(1) if match else None

    def to_dict(self) -> dict:
        data = {
            "title": self.title,
            "slug": self.slug,
            "url": self.url,
            "genre": self.genre,
            "duration": self.duration,
            "duration_minutes": self.runtime,
            "language": self.language,
            "languages": list(self.languages),
            "subtitles": list(self.subtitles),
            "rating": self.rating,
            "poster_url": self.poster_url,
            "booking_url": self.booking_url,
            "booking_id": self.booking_id,
        }
        if self.synopsis:
            data.update(
                synopsis=self.synopsis,
                director=self.director,
                main_stars=list(self.main_stars),
                trailer_url=self.trailer_url,
                trailer_id=self.trailer_id,
                release_date=self.release_date,
            )
        return data


@dataclass
class Cinema:
    """One GSC outlet, from /cinemas/gsc/."""

    slug: str
    name: str
    city: str | None = None
    features: tuple[str, ...] = ()

    @property
    def url(self) -> str:
        return f"{BASE}/cinema/{self.slug}/"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "slug": self.slug,
            "url": self.url,
            "city": self.city,
            "features": list(self.features),
        }


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


def fetch(path: str, timeout: float = 20.0, delay: float = 0.5) -> str:
    """GET a page from gsc.com.my and return the HTML."""
    if delay:
        time.sleep(delay)  # be polite, this is someone's live site
    request = urllib.request.Request(
        f"{BASE}{path}",
        headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl()) as response:
            body = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            html = body.decode("utf-8", "replace")
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
        raise
    # Unknown pages sometimes come back 200 with a meta refresh to /not-found.
    if "not-found" in html[:400]:
        raise SystemExit(f"not found: {path}")
    return html


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
    return BASE + url if url.startswith("/") else url


def parse_listing(html: str) -> list[Movie]:
    """Pull every movie card out of a listing page."""
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

        movies.append(
            Movie(
                title=title,
                slug=slug,
                genre=clean(chips.get("genre")),
                duration=clean(chips.get("duration")),
                language=clean(chips.get("language")),
                subtitles=split_codes(chips.get("subtitles")),
                rating=clean(chips.get("rating")),
                poster_url=_abs(_text(r'<div class="poster"[^>]*>\s*<img src="([^"]+)"', card)),
                booking_url=booking,
                booking_id=int(match.group(1)) if match else None,
            )
        )
    return movies


def parse_cinemas(html: str) -> list[Cinema]:
    """Pull every cinema card out of /cinemas/gsc/.

    Each card carries a link, a name, a town, and an optional comma-joined list
    of formats and amenities. GSC files a couple of outlets under a state rather
    than a town (GSC Spring Bintulu is listed as "Sarawak"), so `city` is
    whatever GSC says and is not guaranteed to be a locality.
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
                slug=slug,
                name=name,
                city=_text(r'twi-location[^>]*>(.*?)</li>', card),
                # The amenities row is absent on plain single-screen outlets.
                features=split_codes(_text(r'twi-experience[^>]*>(.*?)</li>', card)),
            )
        )
    return found


def _slug(href: str | None) -> str | None:
    match = re.search(r"/movie/([^/?#]+)", href or "")
    return match.group(1) if match else None


def _slug_path(href: str | None, section: str) -> str | None:
    match = re.search(rf"/{section}/([^/?#]+)", href or "")
    return match.group(1) if match else None


def parse_detail(html: str, slug: str) -> Movie:
    """Read one movie's detail page.

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

    return Movie(
        title=data.get("name") or _text(r'class="movie-title"[^>]*>(.*?)</h1>', html) or slug,
        slug=slug,
        genre=clean(genre or specs("Genre")),
        duration=clean(data.get("duration")) or specs("Running Time"),
        language=clean(data.get("inLanguage") or specs("Language")),
        subtitles=split_codes(specs("Subtitles")),
        rating=clean(data.get("contentRating") or specs("Classification")),
        poster_url=_abs(data.get("image")),
        booking_url=booking,
        booking_id=int(match.group(1)) if match else None,
        synopsis=clean(data.get("description")),
        director=credit("Director"),
        main_stars=tuple(
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


def listing(name: str, **kwargs) -> list[Movie]:
    """Fetch a listing by name: now-showing, coming-soon, advance-sales."""
    if name not in LISTINGS:
        raise SystemExit(f"unknown listing {name!r}, pick one of {', '.join(LISTINGS)}")
    return parse_listing(fetch(LISTINGS[name], **kwargs))


def movie(slug: str, **kwargs) -> Movie:
    """Fetch the detail record for one movie."""
    slug = slug.strip().strip("/").removeprefix("movie/").rstrip("/")
    if not slug or "/" in slug:
        raise SystemExit(f"bad slug: {slug!r}")
    return parse_detail(fetch(f"/movie/{slug}/", **kwargs), slug)


def cinemas(**kwargs) -> list[Cinema]:
    """Fetch every GSC outlet in Malaysia, as shown on /cinemas/gsc/."""
    return parse_cinemas(fetch(CINEMAS, **kwargs))


def by_city(outlets: list[Cinema], city: str) -> list[Cinema]:
    """Narrow an already-fetched cinema list to one town, case-insensitively."""
    wanted = city.strip().casefold()
    return [c for c in outlets if (c.city or "").casefold() == wanted]


def show(movies: list[Movie]) -> None:
    for i, m in enumerate(movies, 1):
        rating = m.rating or "unrated"
        runtime = f"{m.runtime} min" if m.runtime else "TBA"
        subs = ", ".join(m.subtitles) or "-"
        print(f"{i:2}. {m.title}")
        print(f"    {m.genre} | {runtime} | {rating} | audio {m.language} | subs {subs}")


def show_cinemas(outlets: list[Cinema]) -> None:
    for i, c in enumerate(outlets, 1):
        features = ", ".join(c.features) or "-"
        print(f"{i:2}. {c.name}")
        print(f"    {c.city} | {features}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List movies now showing at GSC Cinemas Malaysia.",
        epilog=(
            "examples:  python gsc_scraper.py --json    "
            "python gsc_scraper.py verity    python gsc_scraper.py cinemas"
        ),
    )
    parser.add_argument(
        "name",
        nargs="?",
        default="now-showing",
        help="a listing name, 'cinemas', or a movie slug for detail",
    )
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--all", action="store_true", help="fetch every listing")
    parser.add_argument("--city", help="with 'cinemas', only show this town")
    parser.add_argument("--no-delay", action="store_true", help="skip the polite pause")
    args = parser.parse_args()

    kwargs = {"delay": 0.0 if args.no_delay else 0.5}

    if args.name == "cinemas":
        outlets = cinemas(**kwargs)
        if args.city:
            outlets = by_city(outlets, args.city)
        if args.json:
            print(json.dumps([c.to_dict() for c in outlets], indent=2, ensure_ascii=False))
            return
        label = f" in {args.city}" if args.city else ""
        print(f"\ncinemas{label}: {len(outlets)} outlets\n")
        show_cinemas(outlets)
        return

    if args.all:
        names = list(LISTINGS)
    elif args.name in LISTINGS:
        names = [args.name]
    else:
        names = []

    if names:
        results = {name: listing(name, **kwargs) for name in names}
        if args.json:
            print(
                json.dumps(
                    {name: [m.to_dict() for m in movies] for name, movies in results.items()},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return
        for name, movies in results.items():
            print(f"\n{name}: {len(movies)} movies\n")
            show(movies)
        return

    detail = movie(args.name, **kwargs)
    if args.json:
        print(json.dumps(detail.to_dict(), indent=2, ensure_ascii=False))
        return
    for key, value in detail.to_dict().items():
        print(f"{key:18} {value}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - keep the CLI readable
        sys.exit(f"error: {exc}")