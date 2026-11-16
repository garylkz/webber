#!/usr/bin/env python3
"""Find a YouTube channel by a creator's name.

Usage:
    python youtube_channel.py "alex meyers"

Prints one JSON object on stdout as its only output:
    {"status": "found", "name": ..., "channel": "@handle", "subscribers": ..., "url": ...}
    {"status": "ambiguous", "query": ..., "candidates": [{"name": ..., "channel": ..., "subscribers": ..., "url": ...}]}

The name is searched on YouTube, channels only. A channel is picked only when
the match is clear: its name or handle equals the name (ignoring case, spaces
and punctuation), and it has at least 5 times the subscribers of every other
close result. Otherwise the close results come back as ambiguous. Traditional
and Simplified Chinese count as the same, through macOS's own converter. When
nothing comes close, the error goes to stderr with exit 1.
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.parse

import requests

SEARCH_URL = "https://www.youtube.com/results"
CHANNELS_ONLY = "EgIQAg%3D%3D"  # YouTube's search filter for channels
INITIAL_DATA_RE = re.compile(r"var ytInitialData = (\{.*?\});</script>", re.S)
CJK_RE = re.compile(r"[㐀-鿿]")
COUNT_RE = re.compile(r"([\d.]+)\s*([KMB]?)")
MULTIPLIER = {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}
TOP_RESULTS = 5
CLEAR_LEAD = 5  # the pick needs this many times the subscribers of every other close result


def search_page(name: str, timeout: int = 30) -> str:
    """The HTML of one YouTube channel search."""
    url = f"{SEARCH_URL}?search_query={urllib.parse.quote_plus(name)}&sp={CHANNELS_ONLY}"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"}, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise SystemExit(f"Could not search YouTube: {e}") from e
    return resp.text


def parse_channels(html: str) -> list[dict]:
    """Every channel result in a search page, in page order: [{name, channel, subscribers}].

    channel is the @handle, or the UC channel id when the channel has no handle.
    """
    m = INITIAL_DATA_RE.search(html)
    if not m:
        return []
    channels = []

    def walk(node):
        if isinstance(node, dict):
            c = node.get("channelRenderer")
            if c:
                texts = [c.get("subscriberCountText", {}).get("simpleText", ""),
                         c.get("videoCountText", {}).get("simpleText", "")]
                path = c.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("canonicalBaseUrl", "")
                handle = urllib.parse.unquote(path.lstrip("/")) if path.startswith("/@") else c.get("channelId", "")
                channels.append({"name": c.get("title", {}).get("simpleText", ""), "channel": handle,
                                 "subscribers": next((t for t in texts if "subscriber" in t), "")})
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(m.group(1)))
    return channels


def simplified_chinese(texts: list[str]) -> dict:
    """{text: its Simplified form}, through macOS's Hant-Hans transform. Empty when not on macOS."""
    if sys.platform != "darwin" or not any(CJK_RE.search(t) for t in texts):
        return {}
    script = ('ObjC.import("Foundation"); function run(argv) { return JSON.stringify(JSON.parse(argv[0]).map('
              's => ObjC.unwrap($(s).stringByApplyingTransformReverse("Hant-Hans", false)))); }')
    try:
        done = subprocess.run(["osascript", "-l", "JavaScript", "-e", script, json.dumps(texts)],
                              capture_output=True, text=True, timeout=20)
        return dict(zip(texts, json.loads(done.stdout)))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}


def subscriber_count(text: str) -> float:
    """'3.93M subscribers' -> 3930000.0; 0 when there is no number."""
    m = COUNT_RE.match(text)
    return float(m.group(1)) * MULTIPLIER[m.group(2)] if m else 0


def channel_url(channel: str) -> str:
    return "https://www.youtube.com/" + (channel if channel.startswith("@") else "channel/" + channel)


def choose(query: str, channels: list[dict], simplified: dict | None = None) -> dict | None:
    """The found or ambiguous result for a query, or None when nothing comes close. Pure."""
    simplified = simplified or {}

    def key(s):
        return re.sub(r"[\W_]+", "", simplified.get(s, s).casefold())

    def keys(c):
        return [key(c["name"]), key(c["channel"])]

    q = key(query)
    close = [c for c in channels[:TOP_RESULTS] if q and any(k and (q in k or k in q) for k in keys(c))]
    close.sort(key=lambda c: subscriber_count(c["subscribers"]), reverse=True)
    exact = [c for c in close if q in keys(c)]
    if exact:
        top = exact[0]
        others = [subscriber_count(c["subscribers"]) for c in close if c is not top]
        if all(subscriber_count(top["subscribers"]) >= CLEAR_LEAD * max(n, 1) for n in others):
            return {"status": "found", **top, "url": channel_url(top["channel"])}
    if close:
        return {"status": "ambiguous", "query": query,
                "candidates": [{**c, "url": channel_url(c["channel"])} for c in close]}
    return None


def find_channel(name: str) -> dict:
    channels = parse_channels(search_page(name))[:TOP_RESULTS]
    texts = [name] + [t for c in channels for t in (c["name"], c["channel"])]
    result = choose(name, channels, simplified_chinese(texts))
    if result is None:
        raise SystemExit(f'No channel found for "{name}".')
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", help="the creator or channel name, in any language")
    args = parser.parse_args()
    print(json.dumps(find_channel(args.name), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
