#!/usr/bin/env python3
"""
Fetch one Rock Paper Shotgun article and print it as one JSON object:

  {"title", "author", "published", "url", "tags", "body", "body_file",
   "save_command"}

body is the article as Markdown. save_command is the shell command that saves
the article as a vault note; the agent runs it only after the user says yes.

Usage: rps_article.py <article url>

The body is also written to /tmp/rps-article-body.md, which the save command
reads with --body-file. The note follows pages/Articles Conventions.md: the
types articles and bookmarks, the site tags, the author, the publish time in
GMT+8, and the url.

Fetches the page with requests.
"""

import argparse
import json
import os
import re
import shlex
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

BODY_FILE = "/tmp/rps-article-body.md"
TIMEOUT = 30
GMT8 = timezone(timedelta(hours=8))
MAX_TAGS = 3

# Elements whose whole subtree is not article text.
SKIP_TAGS = {"script", "style", "figure", "aside", "iframe", "noscript", "svg", "button", "form", "video", "audio"}
SKIP_CLASS = re.compile(r"mpu|advert|poll|related|embed|newsletter|video")
BLOCK_TAGS = {"p", "h2", "h3", "h4", "li", "blockquote"}
VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "source", "wbr"}


def clean_url(raw):
    """Return the canonical article URL, or None when it is not an RPS article."""
    parts = urlsplit(raw.strip())
    host = parts.netloc.lower()
    if parts.scheme not in ("http", "https") or host not in ("rockpapershotgun.com", "www.rockpapershotgun.com"):
        return None
    if parts.path in ("", "/"):
        return None
    # Drop ?view=comments and any tracking query; the article is the path.
    return urlunsplit(("https", "www.rockpapershotgun.com", parts.path, "", ""))


def fetch(url):
    """Fetch the page, following redirects. An HTTP error status is a failure."""
    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as error:
        return None, f"Error fetching the article: {error}"
    return response.content.decode("utf-8", errors="replace"), None


class ArticleParser(HTMLParser):
    """Read the meta tags, the JSON-LD, and the body of an RPS article page."""

    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.meta = {}
        self.tags = []
        self.ld = []
        self.in_ld = False
        self.ld_text = []
        self.body_depth = 0
        self.skip_tag = None
        self.skip_count = 0
        self.blocks = []
        self.block = None
        self.links = []
        self.quote = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            if key == "article:tag" and attrs.get("content"):
                self.tags.append(attrs["content"])
            elif key and key not in self.meta:
                self.meta[key] = attrs.get("content", "")
            return
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.in_ld, self.ld_text = True, []
            return

        if not self.body_depth:
            if tag == "div" and "article_body_content" in (attrs.get("class") or ""):
                self.body_depth = 1
            return
        if tag == "div":
            self.body_depth += 1

        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_count += 1
            return
        if tag not in VOID_TAGS and (tag in SKIP_TAGS or SKIP_CLASS.search(attrs.get("class") or "")):
            self.skip_tag, self.skip_count = tag, 1
            return

        if tag == "blockquote":
            self.quote += 1
        elif tag in BLOCK_TAGS:
            self.start_block(tag)
        elif tag == "br" and self.block is not None:
            self.block["text"].append("\n")
        elif tag == "a" and self.block is not None:
            self.links.append((attrs.get("href") or "", len(self.block["text"])))
        elif tag in ("em", "i") and self.block is not None:
            self.block["text"].append("*")
        elif tag in ("strong", "b") and self.block is not None:
            self.block["text"].append("**")

    def handle_endtag(self, tag):
        if self.in_ld and tag == "script":
            self.in_ld = False
            self.ld.append("".join(self.ld_text))
            return
        if not self.body_depth:
            return
        if tag == "div":
            self.body_depth -= 1
            if not self.body_depth:
                self.end_block()
                return

        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_count -= 1
                if not self.skip_count:
                    self.skip_tag = None
            return

        if tag == "blockquote":
            self.quote = max(0, self.quote - 1)
        elif tag in BLOCK_TAGS:
            self.end_block()
        elif tag == "a" and self.links and self.block is not None:
            href, start = self.links.pop()
            text = "".join(self.block["text"][start:]).strip()
            del self.block["text"][start:]
            if text and href and not href.startswith(("#", "javascript:")):
                # Square brackets in link text would break the markdown link.
                text = text.replace("[", "(").replace("]", ")")
                self.block["text"].append(f"[{text}]({urljoin(self.base, href)})")
            else:
                self.block["text"].append(text)
        elif tag in ("em", "i") and self.block is not None:
            self.block["text"].append("*")
        elif tag in ("strong", "b") and self.block is not None:
            self.block["text"].append("**")

    def handle_data(self, data):
        if self.in_ld:
            self.ld_text.append(data)
        elif self.body_depth and not self.skip_tag and self.block is not None:
            self.block["text"].append(data)

    def start_block(self, tag):
        self.end_block()
        self.block = {"tag": tag, "quote": self.quote > 0, "text": []}

    def end_block(self):
        if self.block is None:
            return
        text = "".join(self.block["text"])
        text = "\n".join(re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n"))
        text = re.sub(r"\*\*\s*\*\*|(?<!\*)\*\s*\*(?!\*)", "", text).strip()
        if text:
            if self.block["tag"] in ("h2", "h3", "h4"):
                text = f"**{text.strip('*')}**"
            elif self.block["tag"] == "li":
                text = f"- {text}"
            if self.block["quote"]:
                text = "\n".join(f"> {line}" for line in text.split("\n"))
            self.blocks.append(text)
        self.block = None
        self.links = []


def author_of(ld_blocks):
    """Return the byline from the NewsArticle JSON-LD, joined when there are several."""
    for raw in ld_blocks:
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict) or "Article" not in str(item.get("@type", "")):
                continue
            people = item.get("author") or []
            people = people if isinstance(people, list) else [people]
            names = [p.get("name", "").strip() for p in people if isinstance(p, dict)]
            names = [n for n in names if n]
            if names:
                return ", ".join(names)
    return ""


def published_of(meta):
    """Return the publish time in GMT+8 as YYYY-MM-DDTHH:mm, or an empty string."""
    raw = meta.get("article:published_time", "")
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(GMT8).strftime("%Y-%m-%dT%H:%M")


def tag_of(name):
    """Turn a site tag such as Itch.io into a vault tag such as itch-io."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def main():
    args = argparse.ArgumentParser(description="Fetch one Rock Paper Shotgun article.")
    args.add_argument("url", help="a rockpapershotgun.com article url")
    args = args.parse_args()

    url = clean_url(args.url)
    if not url:
        print("Only rockpapershotgun.com article URLs are supported.", file=sys.stderr)
        sys.exit(1)

    html, error = fetch(url)
    if error:
        print(error, file=sys.stderr)
        sys.exit(1)
    parser = ArticleParser(url)
    parser.feed(html)
    parser.close()

    title = (parser.meta.get("og:title") or "").strip()
    body = "\n\n".join(parser.blocks)
    if not title or not body:
        print("Could not find the article text on that page.", file=sys.stderr)
        sys.exit(1)
    # A slash cannot sit in a file name, so the note title drops it.
    note_title = re.sub(r"\s*[/\\]\s*", " ", title)
    author = author_of(parser.ld)
    published = published_of(parser.meta)
    tags = []
    for name in parser.tags:
        tag = tag_of(name)
        if tag and tag not in tags:
            tags.append(tag)
    tags = tags[:MAX_TAGS]

    try:
        with open(BODY_FILE, "w") as handle:
            handle.write(body + "\n")
    except OSError as error:
        print(f"Could not write {BODY_FILE}: {error}", file=sys.stderr)
        sys.exit(1)

    command = ["python3", ".agents/scripts/vault-create.py", "--types", "articles", "bookmarks"]
    if tags:
        command += ["--tags", *tags]
    command += ["--title", note_title, "--url", url]
    if author:
        command += ["--set", f"author={author}"]
    if published:
        command += ["--set", f"published-on={published}"]
    command += ["--body-file", BODY_FILE]

    print(json.dumps({
        "title": title,
        "author": author or None,
        "published": published or None,
        "url": url,
        "tags": tags,
        "body": body,
        "body_file": BODY_FILE,
        "save_command": shlex.join(command),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
