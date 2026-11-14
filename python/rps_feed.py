#!/usr/bin/env python3
"""
Fetch the latest articles from the Rock Paper Shotgun RSS feed and print each
one with a clickable article link and a clickable comments link.

Output is one JSON object:

    {"order": "newest first", "start": 1, "next_start": 11, "total": 20,
     "count": 10, "articles": [
      {"title": "...", "url": "...", "comments_url": "..."}, ...]}

Every article keeps both its url and its comments_url. The agent that runs this
script reads the JSON and replies with each title, url, and comments_url.

Usage: rps-feed.py [--start N] [--count M]   (default: every article)

The feed is sorted newest first. --start N is the position to begin at, where 1
is the newest article. --count M is how many articles to print. Without
--count, it prints every article from --start to the end. total is the number of
articles in the feed, and next_start is the --start for the next batch; when
next_start is past total, there are no more articles.

Uses the requests library to fetch the feed.
"""

import json
import sys
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

FEED_URL = "https://www.rockpapershotgun.com/feed"
OUT_FILE = "/tmp/rps-articles.json"
TIMEOUT = 30


def fetch_feed():
    """Fetch the feed XML as bytes, or None after printing why it failed."""
    try:
        response = requests.get(
            FEED_URL, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"}
        )
    except requests.RequestException as error:
        print(f"Error fetching Rock Paper Shotgun: {error}")
        return None
    if not response.ok:
        print(f"Error fetching Rock Paper Shotgun: HTTP {response.status_code}")
        return None
    return response.content


def parse_articles(xml_bytes):
    """Parse the feed and extract article information."""
    articles = []
    for item in ET.fromstring(xml_bytes).iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        try:
            published = parsedate_to_datetime(item.findtext("pubDate")).astimezone()
        except (TypeError, ValueError):
            continue
        articles.append(
            {"title": title, "link": link, "published": published}
        )
    return articles


def format_output(articles, start, total):
    """Format the articles as one JSON object. Every article keeps both URLs."""
    return json.dumps(
        {
            "order": "newest first",
            "start": start,
            "next_start": start + len(articles),
            "total": total,
            "count": len(articles),
            "articles": [
                {
                    "title": article["title"],
                    "url": article["link"],
                    "comments_url": f"{article['link']}?view=comments",
                }
                for article in articles
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def parse_args(argv):
    """Return (start, count) from --start N and --count M.

    The default start is 1. The default count is None, which means every article.
    """
    usage = (
        "Usage: rps-feed.py [--start N] [--count M], where N and M are 1 or more"
        " (1 is the newest article; without --count, every article is shown)."
    )
    values = {"--start": 1, "--count": None}
    seen = set()
    args = list(argv)
    while args:
        flag = args.pop(0)
        if flag not in values or flag in seen or not args or not args[0].isdigit() or int(args[0]) < 1:
            print(usage)
            sys.exit(2)
        values[flag] = int(args.pop(0))
        seen.add(flag)
    return values["--start"], values["--count"]


def main():
    start, count = parse_args(sys.argv[1:])

    xml_bytes = fetch_feed()
    if not xml_bytes:
        print("Could not fetch the feed from Rock Paper Shotgun.")
        return
    try:
        articles = parse_articles(xml_bytes)
    except ET.ParseError as error:
        print(f"Error parsing the feed: {error}")
        return
    if not articles:
        print("No articles found or an error occurred while parsing.")
        return

    articles = sorted(articles, key=lambda a: a["published"], reverse=True)
    total = len(articles)
    if start > total:
        print(f"No articles at position {start}; the feed has {total}.")
        return
    end = total if count is None else start - 1 + count
    articles = articles[start - 1 : end]
    text = format_output(articles, start, total)
    # Persist the exact text so a wrong URL in the reply can be fixed by
    # running `cat /tmp/rps-articles.json` again.
    try:
        with open(OUT_FILE, "w") as handle:
            handle.write(text + "\n")
    except OSError:
        pass
    print(text)


if __name__ == "__main__":
    main()
