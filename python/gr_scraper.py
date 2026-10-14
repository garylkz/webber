"""Scrape a Goodreads user's bookshelf through the public RSS endpoint.

Goodreads retired its official API. No new API keys exist. The HTML shelf
page (/review/list/{id}) requires a login. The RSS endpoint
(/review/list_rss/{id}?shelf={shelf}) stays public. It supports pagination:

  - shelf values: read, currently-reading, to-read (custom shelves too).
  - paginate with &per_page=200&page={n}. Stop when a page is empty.
  - remove duplicates by review GUID. Goodreads ignores per_page above 200,
    so page in chunks.

Each item carries the book title, cover/author/avg-rating in the description
block. It also carries the user's own rating, read-at date, date added,
shelves, and review text.

Run with no arguments to scrape a default user's read shelf. Run `search
<name>` to look up a user ID by people search first. IDs also appear in
profile URLs as /user/show/{id}-{slug}.
"""

import json
import re
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0"}
DEFAULT_USER_ID = "1123497"
MAX_PAGES = 50
OUT_DIR = Path(__file__).resolve().parent / "out"


def clean(s):
    return re.sub(r"<!\[CDATA\[|\]\]>", "", s or "").strip()


def parse_item(raw):
    g = re.search(r"<guid[^>]*>(.*?)</guid>", raw, re.S)
    t = re.search(r"<title>(.*?)</title>", raw, re.S)
    d = re.search(r"<description>(.*?)</description>", raw, re.S)
    desc = clean(d.group(1)) if d else ""
    fields = {}
    for key in ("author", "name", "average rating", "book published",
                "rating", "read at", "date added", "shelves", "review"):
        m = re.search(rf"{re.escape(key)}:\s*(.*?)(?:<br|$)", desc, re.S)
        if m:
            fields[key] = m.group(1).strip()
    m = re.search(r"href=\"(https://www\.goodreads\.com/book/show/\d+[^\"]*)\"", desc)
    if m:
        fields["book_url"] = m.group(1).split("?")[0]
    return {"guid": clean(g.group(1)) if g else "", "title": clean(t.group(1)) if t else "?", **fields}


def scrape(session, uid, shelf="read"):
    """Fetch every page of a user's shelf. Remove duplicates by review GUID."""
    if not uid.isdigit():
        raise ValueError(f"User ID must be numeric, got {uid!r}")
    seen, pages = [], 0
    for page in range(1, MAX_PAGES + 1):
        query = urllib.parse.urlencode({"shelf": shelf, "per_page": 200, "page": page})
        url = f"https://www.goodreads.com/review/list_rss/{uid}?{query}"
        resp = session.get(url, headers=UA, timeout=30)
        resp.raise_for_status()
        items = re.findall(r"<item>(.*?)</item>", resp.text, re.S)
        if not items:
            break
        pages = page
        for raw in items:
            row = parse_item(raw)
            if row["guid"] and any(x["guid"] == row["guid"] for x in seen):
                continue
            seen.append(row)
    return seen, pages


def find_user(session, name):
    """Resolve a name to a Goodreads user ID through the public people search."""
    resp = session.get(
        f"https://www.goodreads.com/search?q={requests.utils.quote(name)}&search_type=people",
        headers=UA, timeout=30,
    )
    resp.raise_for_status()
    ids = sorted(set(re.findall(r"/user/show/(\d+)-[a-zA-Z0-9_]+", resp.text)))
    return ids


def main():
    session = requests.Session()

    if len(sys.argv) > 1 and sys.argv[1] == "search":
        if len(sys.argv) != 3:
            print("Usage: gr_scraper.py search <name>")
            sys.exit(1)
        ids = find_user(session, sys.argv[2])
        if not ids:
            print("No users found.")
            sys.exit(1)
        for uid in ids[:10]:
            print(uid)
        return

    uid = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_USER_ID
    shelf = sys.argv[2] if len(sys.argv) > 2 else "read"
    if not uid.isdigit():
        print("Usage: gr_scraper.py <numeric-user-id> [shelf]")
        sys.exit(1)

    books, pages = scrape(session, uid, shelf)
    print(f"Fetched {len(books)} unique books from user {uid} shelf '{shelf}' ({pages} page(s)).")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUT_DIR / f"gr_books_{uid}_{shelf}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(books, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(books)} books to {output_path}")


if __name__ == "__main__":
    main()
