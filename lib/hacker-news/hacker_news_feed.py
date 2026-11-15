#!/usr/bin/env python3
"""
Fetch the top stories from the Hacker News front page and print each one with
an article url and a comments url.

Output is one JSON object:

    {"page": 1, "count": 10, "stories": [
      {"title": "...", "url": "...", "comments_url": "..."}, ...]}

Every story keeps both its url and its comments_url. The agent that runs this
script reads the JSON and replies with each title, url, and comments_url.

Usage: hacker_news_feed.py [--page N]   (default: --page 1)

Hacker News shows 30 stories on each page. The script prints only the first 10
of them.

Uses the requests library to fetch the front page and parse the HTML.
"""

import argparse
import json
import re
import sys
from html import unescape

import requests

HN_BASE = "https://news.ycombinator.com"
OUT_FILE = "/tmp/hn-stories.json"
TIMEOUT = (10, 30)
MAX_STORIES = 10


def fail(message, status=1):
    """Print an error as JSON on stdout, where the agent reads, and exit."""
    print(json.dumps({"error": message}, ensure_ascii=False))
    sys.exit(status)


def fetch_hacker_news(page=1):
    """Fetch one page of Hacker News with certificate verification on."""
    try:
        response = requests.get(
            HN_BASE,
            params={"p": page} if page > 1 else None,
            timeout=TIMEOUT,
            headers={"User-Agent": "hacker-news-script/1.0"},
        )
    except requests.RequestException as error:
        fail(f"Error fetching Hacker News: {error}")

    if not response.ok:
        fail(f"Error fetching Hacker News: HTTP {response.status_code}")

    return response.text


def parse_stories(html):
    """Parse the HTML and extract story information."""
    stories = []

    # Hacker News puts the story id in the id attribute of the opening tr tag.
    pattern = r'<tr class="athing submission"[^>]*\bid="(\d+)"[^>]*>(.*?)</tr>'

    for row_id, row in re.findall(pattern, html, re.DOTALL):
        link = re.search(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', row)
        if not link:
            continue
        url, title = link.groups()
        # A link is either absolute or relative to the Hacker News host.
        if not url.startswith("http"):
            url = f"{HN_BASE}/{url.lstrip('/')}"
        stories.append(
            {
                "title": unescape(title).strip(),
                "url": url,
                "comments_url": f"{HN_BASE}/item?id={row_id}",
            }
        )

    return stories


def format_output(stories, page):
    """Format the stories as one JSON object. Every story keeps both URLs."""
    return json.dumps(
        {"page": page, "count": len(stories), "stories": stories},
        ensure_ascii=False,
        indent=2,
    )


def main():
    parser = argparse.ArgumentParser(description="Top Hacker News stories.")
    parser.add_argument("--page", type=int, default=1)
    page = max(parser.parse_args().page, 1)

    html = fetch_hacker_news(page)
    if not html:
        fail("Could not fetch HTML from Hacker News.")

    stories = parse_stories(html)[:MAX_STORIES]
    if not stories:
        fail("No stories found or an error occurred while parsing.")

    text = format_output(stories, page)
    # Persist the exact text so a wrong URL in the reply can be fixed by
    # running `cat /tmp/hn-stories.json` again.
    try:
        with open(OUT_FILE, "w") as handle:
            handle.write(text + "\n")
    except OSError:
        pass
    print(text)


if __name__ == "__main__":
    main()
