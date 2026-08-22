#!/usr/bin/env python3
"""Convert a YouTube channel URL into its RSS feed URL.

Usage:
    python yt_rss.py <channel-url> [--fetch]
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

FEEDS_URL = "https://www.youtube.com/feeds/videos.xml"
ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}

CHANNEL_RE = re.compile(r"(?:youtube\.com|youtu\.be)/(channel|user)/([^/?]+)")
HANDLE_RE = re.compile(r"(?:youtube\.com)/(@[^/?]+)")
CHANNEL_ID_RE = re.compile(r'"channelId":"(UC[\w-]{22})"')
EXTERNAL_ID_RE = re.compile(r'"externalId":"(UC[\w-]{22})"')
CANONICAL_RE = re.compile(r'rel="canonical" href="[^"]*?/channel/(UC[\w-]{22})"')


def rss_for_channel(channel_id: str) -> str:
    return f"{FEEDS_URL}?channel_id={channel_id}"


def normalize_url(url: str) -> str:
    """Force https for bare hosts, then refuse non-http(s) schemes and
    non-YouTube hosts. This blocks file:// and SSRF-style fetches."""
    if not urllib.parse.urlparse(url).scheme:
        url = "https://" + url
    parts = urllib.parse.urlparse(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ALLOWED_SCHEMES:
        raise SystemExit(f"Refusing URL with scheme {parts.scheme!r}: {url}")
    if host not in ALLOWED_HOSTS:
        raise SystemExit(f"Refusing to fetch non-YouTube host {host!r}: {url}")
    return url


def fetch_page(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def channel_id_from_url(url: str) -> str | None:
    m = CHANNEL_RE.search(url)
    if m:
        kind, ident = m.group(1), m.group(2)
        if kind == "channel":
            return ident
        return ident  # user name works as ?user=... via feed, but resolve anyway

    # Resolve handle or user URL by scraping the page for the channel id.
    page_url = normalize_url(url)
    try:
        html = fetch_page(page_url)
    except urllib.error.URLError as e:
        raise SystemExit(f"Failed to fetch {page_url}: {e}") from e

    ids = CHANNEL_ID_RE.findall(html)
    if not ids:
        ids = EXTERNAL_ID_RE.findall(html)
    if not ids:
        ids = CANONICAL_RE.findall(html)
    if not ids:
        return None
    return max(set(ids), key=ids.count)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="YouTube channel URL (channel, user, or @handle)")
    parser.add_argument("--fetch", action="store_true", help="Also fetch and print the feed XML")
    args = parser.parse_args()

    channel_id = channel_id_from_url(args.url)
    if not channel_id:
        raise SystemExit("Could not determine the channel ID from that URL.")

    rss_url = rss_for_channel(channel_id)
    print(rss_url)

    if args.fetch:
        try:
            print(fetch_page(rss_url))
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Feed fetch failed: HTTP {e.code}") from e


if __name__ == "__main__":
    main()
