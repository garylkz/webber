#!/usr/bin/env python3
"""Fetch a Hacker News comment thread and print it as one JSON object.

Usage:
    python hacker_news_comments.py <item url | item id>

The JSON holds the id, title, points, url, comments_url, the number of
top-level comments, how many comments are shown, the depth limit, and the
comment tree: each comment has its text and its replies. When the only
top-level comment says "Comments moved to item?id=X", it fetches X instead and
sets moved_from to the original id (null otherwise).

Uses the Algolia HN API, which returns the whole comment tree as JSON. Errors
go to stderr with a non-zero exit.
"""

import argparse
import json
import re

import requests

API = "https://hn.algolia.com/api/v1/items"
MAX_COMMENTS = 80
MAX_DEPTH = 4
MAX_CHARS = 700

ID_RE = re.compile(r"id=(\d+)")
LINK_RE = re.compile(r'<a [^>]*href="([^"]+)"[^>]*>.*?</a>')
TAG_RE = re.compile(r"<[^>]+>")
ENTITY_RE = re.compile(r"&(#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")
NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": " "}
MOVED_RE = re.compile(r"comments moved to [\s\S]*?item\?id=([0-9]+)", re.I)


def resolve_id(item: str) -> str | None:
    """The item id of a comments url or a bare id. A url's last id= wins."""
    found = ID_RE.findall(item)
    if found:
        return found[-1]
    return item if item.isdigit() else None


def _entity(m: re.Match) -> str:
    e = m.group(1)
    if e[:2] in ("#x", "#X"):
        return chr(int(e[2:], 16))
    if e.startswith("#"):
        return chr(int(e[1:]))
    return NAMED.get(e, f"&{e};")


def clean(text: str | None) -> str:
    """Comment HTML to plain text: paragraphs to newlines, links to their href, no tags, entities decoded."""
    text = (text or "").replace("<p>", "\n")
    text = LINK_RE.sub(lambda m: m.group(1), text)
    text = TAG_RE.sub("", text)
    text = ENTITY_RE.sub(_entity, text)
    return text.strip()


def trim_body(text: str) -> str:
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS].rstrip() + " [...]"
    return text


def with_text(node: dict) -> list[dict]:
    return [c for c in node.get("children") or [] if (c.get("text") or "") != ""]


def moved_target(item: dict) -> str | None:
    """The new id when the only top-level comment says the comments moved, else None."""
    top = with_text(item)
    if len(top) != 1:
        return None
    m = MOVED_RE.search(clean(top[0].get("text")))
    return m.group(1) if m else None


def comment_tree(node: dict) -> tuple[list[dict], int]:
    """The comments under node in reading order, at most MAX_COMMENTS in all and MAX_DEPTH levels deep."""
    shown = 0

    def walk(n: dict, depth: int) -> list[dict]:
        nonlocal shown
        out = []
        for child in with_text(n):
            if shown >= MAX_COMMENTS:
                break
            shown += 1
            replies = walk(child, depth + 1) if depth + 1 < MAX_DEPTH else []
            out.append({"text": trim_body(clean(child.get("text"))), "replies": replies})
        return out

    return walk(node, 0), shown


def build(item: dict, item_id: str, moved_from: str | None = None) -> dict:
    """The thread's JSON from an Algolia item. Pure."""
    comments, shown = comment_tree(item)
    out = {
        "id": int(item_id),
        "title": item.get("title"),
        "points": item.get("points"),
        "url": item.get("url"),
        "comments_url": f"https://news.ycombinator.com/item?id={item_id}",
        "moved_from": int(moved_from) if moved_from else None,
        "top_level_comments": len(item.get("children") or []),
        "shown": shown,
        "max_depth": MAX_DEPTH,
        "comments": comments,
    }
    if not comments:
        out["note"] = "No comments yet."
    return out


def fetch(item_id: str) -> dict:
    try:
        resp = requests.get(f"{API}/{item_id}", timeout=(10, 60))
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise SystemExit(f"Error fetching thread: {e}") from e
    if not isinstance(data, dict):
        raise SystemExit("Error fetching thread: the API did not return a JSON object")
    return data


def thread(item: str) -> dict:
    item_id = resolve_id(item)
    if item_id is None:
        raise SystemExit("Could not resolve an item id from the argument.")
    data = fetch(item_id)
    moved_from = None
    target = moved_target(data)
    if target and target != item_id:
        moved_from, item_id = item_id, target
        data = fetch(item_id)
    return build(data, item_id, moved_from)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("item", help="a news.ycombinator.com/item?id= url or a numeric item id")
    args = parser.parse_args()
    print(json.dumps(thread(args.item), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
