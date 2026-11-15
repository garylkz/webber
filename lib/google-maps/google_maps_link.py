#!/usr/bin/env python3
"""Expand a short Google Maps link into a full google.com/maps/place link.

Usage:
    python google_maps_link.py https://maps.app.goo.gl/XXXX

Prints a JSON object on stdout as its only output:
    {"short_url": ..., "url": ..., "kind": ...}

kind is place (a named place), place_id, coordinates, query, or redirect (the
redirect as it came). The script reads only the redirect, so it never loads the
map page. Errors go to stderr.
"""

import argparse
import json
import re
import urllib.parse

import requests

ALLOWED_HOSTS = {"maps.app.goo.gl", "goo.gl"}
MAPS_ORIGIN = "https://maps.google.com"
COORDINATES_RE = re.compile(r"^-?\d+\.\d+, ?-?\d+\.\d+$")
SEARCH_RE = re.compile(r"(maps\.google\.com/?\?q=|google\.com/maps/search/?\?q=|google\.com/maps/?\?q=)")


def check_short_url(url: str) -> str:
    """Refuse anything but an http(s) goo.gl link, so the script fetches no other host."""
    parts = urllib.parse.urlparse(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in {"http", "https"} or host not in ALLOWED_HOSTS:
        raise SystemExit(f"Not a short Google Maps link: {url}")
    if host == "goo.gl" and not parts.path.startswith("/maps"):
        raise SystemExit(f"Not a short Google Maps link: {url}")
    return url


def redirect_of(url: str, timeout: int = 20) -> str | None:
    """The Location header of one request, without following it. None when there is none."""
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.head(url, headers=headers, allow_redirects=False, timeout=timeout)
        if resp.status_code == 405:  # some servers refuse HEAD
            resp = requests.get(url, headers=headers, allow_redirects=False, timeout=timeout, stream=True)
            resp.close()
    except requests.RequestException as e:
        raise SystemExit(f"Could not reach {url}: {e}") from e
    location = resp.headers.get("Location")
    if not location:
        return None
    return urllib.parse.urljoin(MAPS_ORIGIN + "/", location) if location.startswith("/") else location


def first_query(url: str) -> str:
    """The first q= value of a URL's query string, decoded."""
    values = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("q")
    return values[0].strip() if values else ""


def classify(location: str) -> tuple[str, str]:
    """Turn a redirect target into (full url, kind)."""
    if "/maps/place/" in location:
        return location, "place"
    if SEARCH_RE.search(location):
        query = first_query(location)
        if query.startswith("place_id:"):
            return "https://www.google.com/maps/place/?q=" + urllib.parse.quote_plus(query, safe=":"), "place_id"
        if COORDINATES_RE.match(query):
            return "https://www.google.com/maps/place/@" + query.replace(" ", ""), "coordinates"
        if query:
            return "https://www.google.com/maps/place/" + urllib.parse.quote_plus(query, safe=",:"), "query"
    return location, "redirect"


def expand(short_url: str) -> dict:
    location = redirect_of(check_short_url(short_url))
    if location is None:
        raise SystemExit("No redirect found (not a short link?)")
    url, kind = classify(location)
    return {"short_url": short_url, "url": url, "kind": kind}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="a short link such as https://maps.app.goo.gl/XXXX")
    args = parser.parse_args()
    print(json.dumps(expand(args.url), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
