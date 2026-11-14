#!/usr/bin/env python3
"""Convert a YouTube channel URL into its RSS feed URL.

Usage:
    python yt_rss.py <channel-url> [--fetch]

Prints a JSON object on stdout as its only output:
    {"channel_url": ..., "channel_id": ..., "feed_url": ...}

With --fetch the feed is fetched and the result gains a "videos" key
holding the entries of the feed. Errors go to stderr.
"""

import argparse
import json
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET

import requests

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
    """Fetch one page as text. Raises requests.HTTPError on a non-2xx status."""
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.content.decode("utf-8", errors="replace")


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
    except requests.RequestException as e:
        raise SystemExit(f"Failed to fetch {page_url}: {e}") from e

    ids = CHANNEL_ID_RE.findall(html)
    if not ids:
        ids = EXTERNAL_ID_RE.findall(html)
    if not ids:
        ids = CANONICAL_RE.findall(html)
    if not ids:
        return None
    return max(set(ids), key=ids.count)


def parse_feed(xml: str) -> list[dict]:
    """A feeds/videos.xml Atom body -> [{video_id, title, url, published}]."""
    atom = "{http://www.w3.org/2005/Atom}"
    yt = "{http://www.youtube.com/xml/schemas/2015}"
    videos = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    for entry in root.findall(f"{atom}entry"):
        link = entry.find(f"{atom}link")
        videos.append({
            "video_id": (entry.findtext(f"{yt}videoId") or "").strip(),
            "title": (entry.findtext(f"{atom}title") or "").strip(),
            "url": (link.get("href") or "").strip() if link is not None else "",
            "published": (entry.findtext(f"{atom}published") or "").strip(),
        })
    return videos


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="YouTube channel URL (channel, user, or @handle)")
    parser.add_argument("--fetch", action="store_true", help="Also fetch the feed and add its videos")
    args = parser.parse_args()

    channel_id = channel_id_from_url(args.url)
    if not channel_id:
        raise SystemExit("Could not determine the channel ID from that URL.")

    rss_url = rss_for_channel(channel_id)
    result = {
        "channel_url": args.url,
        "channel_id": channel_id,
        "feed_url": rss_url,
    }

    if args.fetch:
        try:
            result["videos"] = parse_feed(fetch_page(rss_url))
        except requests.HTTPError as e:
            raise SystemExit(f"Feed fetch failed: HTTP {e.response.status_code}") from e
        except requests.RequestException as e:
            raise SystemExit(f"Feed fetch failed: {e}") from e

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
