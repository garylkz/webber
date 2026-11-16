#!/usr/bin/env python3
"""Anonymous Novelight catalogue, metadata and selected public chapter reader.

Standalone JSON CLI. Only requests is third party. Raw validated responses are
cached independently for 24 hours under the repository's out/novelight folder.
Search is the source's keyword search, not semantic or exhaustive discovery.
"""
import argparse
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

BASE_URL = "https://novelight.net"
REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "out" / "novelight"
TTL_SECONDS = 86400
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


class NovelightError(ValueError):
    """Invalid arguments, unavailable public data, or changed source markup."""


def _positive(value, label="page"):
    if type(value) is not int or value < 1:
        raise NovelightError(f"{label} must be a positive integer")
    return value


def _slug(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise NovelightError("slug must contain only letters, digits, underscores or hyphens")
    return value


def _link(href, pattern):
    url = urljoin(BASE_URL, href or "")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "novelight.net" or parts.fragment or not re.fullmatch(pattern, parts.path):
        raise NovelightError("Unexpected or unsafe source link")
    return url


class Node:
    def __init__(self, tag="", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def has(self, cls):
        return cls in (self.attrs.get("class") or "").split()

    def find(self, tag=None, cls=None, id=None):
        return [n for n in self.walk() if (tag is None or n.tag == tag)
                and (cls is None or n.has(cls)) and (id is None or n.attrs.get("id") == id)]

    def walk(self):
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.walk()

    def text(self):
        return _clean(self)


class DOM(HTMLParser):
    VOID = set("area base br col embed hr img input link meta param source track wbr".split())

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs)
        self.stack[-1].children.append(n)
        if tag not in self.VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _clean(node):
    parts = []

    def visit(n):
        if isinstance(n, str):
            parts.append(n)
            return
        identity = " ".join(str(n.attrs.get(k) or "") for k in ("class", "id")).lower()
        style = (n.attrs.get("style") or "").replace(" ", "").lower()
        if (n.tag in {"script", "style", "noscript", "iframe", "nav", "aside", "form", "button", "template"}
                or "hidden" in n.attrs or n.attrs.get("aria-hidden") == "true"
                or "display:none" in style or "visibility:hidden" in style
                or re.search(r"(?:^|[\s_-])(?:ads?|advert\w*|adsbygoogle|widget\w*|paywall|payment|login|comments?|navigation|recommend\w*|hidden)(?:$|[\s_-])", identity)):
            return
        block = n.tag in {"p", "div", "section", "br", "li", "h1", "h2", "blockquote", "hr"}
        if block:
            parts.append("\n")
        for child in n.children:
            visit(child)
        if block:
            parts.append("\n")
    visit(node)
    return "\n".join(s for s in (re.sub(r"\s+", " ", line).strip() for line in "".join(parts).splitlines()) if s)


def _one(nodes, label):
    if len(nodes) != 1:
        raise NovelightError(f"Malformed source: expected one {label}")
    return nodes[0]


def _title(node, cls="title"):
    text = _one(node.find(cls=cls), cls).text()
    if not text:
        raise NovelightError("Missing title")
    return text


def parse_taxonomies(html):
    root = DOM(html).root
    form = _one(root.find("form", "filter-container"), "filter form")
    result = {"genres": [], "tags": []}
    for label in form.find("label"):
        for inp in label.find("input"):
            kind = inp.attrs.get("name")
            if kind not in result:
                continue
            ident = inp.attrs.get("value", "")
            name = label.text()
            if not re.fullmatch(r"[0-9]+", ident) or not name:
                raise NovelightError("Malformed taxonomy")
            result[kind].append({"id": int(ident), "name": name})
    if not all(result.values()):
        raise NovelightError("Missing catalogue taxonomies")
    return result


def parse_catalogue(html, page=1):
    root = DOM(html).root
    grid = _one(root.find(cls="manga-grid-list"), "catalogue results container")
    novels = []
    for n in grid.children:
        if not isinstance(n, Node) or n.tag != "a" or not n.has("item"):
            continue
        url = _link(n.attrs.get("href"), r"/book/[A-Za-z0-9_-]+")
        novels.append({"slug": urlsplit(url).path.split("/")[-1], "url": url, "title": _title(n)})
    if not novels and grid.text() and not re.search(r"no .*(?:found|results)|nothing|not found", grid.text(), re.I):
        raise NovelightError("Malformed empty catalogue")
    pages = {page}
    for a in root.find("a"):
        href = a.attrs.get("href", "")
        parts = urlsplit(urljoin(BASE_URL + "/catalog/", href))
        if parts.scheme == "https" and parts.netloc == "novelight.net" and parts.path == "/catalog/":
            for p in parse_qs(parts.query).get("page", []):
                if p.isdecimal() and int(p) > 0:
                    pages.add(int(p))
    return {"novels": novels, "coverage": {"current_page": page, "next_page": page + 1 if page + 1 in pages else None,
            "pages": sorted(pages), "returned": len(novels), "catalogue_exhaustive": False,
            "scope": "One catalogue page; source page size is 30. Page links are not a total count."}}


def _chapters(container):
    result = []
    for n in container.children:
        if not isinstance(n, Node) or n.tag != "a" or not n.has("chapter"):
            continue
        url = _link(n.attrs.get("href"), r"/book/chapter/[1-9][0-9]*")
        info = n.find(cls="chapter-info")
        info = info[0] if info else Node()
        result.append({"id": int(url.rsplit("/", 1)[1]), "url": url, "title": _title(n),
                       "author": " ".join(x.text() for x in info.find(cls="author")),
                       "date": " ".join(x.text() for x in info.find(cls="date"))})
    return result


def parse_detail(html):
    root = DOM(html).root
    info = _one(root.find(id="information"), "information section")
    sections = info.find("section", "text-info")
    if not sections:
        raise NovelightError("Missing synopsis section")
    synopsis = sections[0].text()
    chapter_list = _one(root.find(id="all-chapters-list"), "chapter list")
    match = re.search(r"\bBOOK_ID\s*=\s*['\"]([1-9][0-9]*)['\"]", html)
    if not match:
        raise NovelightError("Missing book ID")
    pages = [int(n.attrs["value"]) for select in root.find("select", id="select-pagination-chapter")
             for n in select.find("option") if re.fullmatch(r"[1-9][0-9]*", n.attrs.get("value", ""))]
    fields = {}
    genres, tags, translators = [], [], []
    for sidebar in root.find(cls="second-information"):
        for mini in sidebar.find(cls="mini-info"):
            for item in mini.find(cls="item"):
                labels, values = item.find(cls="sub-header"), item.find(cls="info")
                if labels and values:
                    fields[labels[0].text()] = values[0].text()
    for kind, target in (("genres", genres), ("tags", tags)):
        for a in info.find("a") + [a for side in root.find(cls="second-information") for a in side.find("a")]:
            href = a.attrs.get("href", "")
            parts = urlsplit(urljoin(BASE_URL, href))
            if parts.scheme != "https" or parts.netloc != "novelight.net" or parts.path != "/catalog/":
                continue
            ids = parse_qs(parts.query).get(kind, [])
            for ident in ids:
                if ident.isdecimal():
                    row = {"id": int(ident), "name": a.text()}
                    if row not in target:
                        target.append(row)
    for group in info.find(cls="translators"):
        for a in group.find("a", "translator"):
            translators.append({"name": a.text(), "url": _link(a.attrs.get("href"), r"/team/[A-Za-z0-9_-]+")})
    title = _one(root.find("h1"), "novel heading").text()
    if not title:
        raise NovelightError("Missing novel title")
    return {"title": title, "description": synopsis,
            "description_scope": "full", "book_id": int(match[1]), "metadata": fields,
            "genres": genres, "tags": tags, "translators": translators,
            "chapters": _chapters(chapter_list), "chapter_pages": sorted(set(pages or [1]))}


def parse_chapter_page(data):
    if not isinstance(data, dict) or not isinstance(data.get("html"), str):
        raise NovelightError("Malformed chapter pagination response")
    root = DOM(data["html"]).root
    chapters = _chapters(root)
    if not chapters:
        raise NovelightError("Unavailable or malformed chapter list page")
    return chapters


def parse_wrapper(html):
    root = DOM(html).root
    _one(root.find(cls="chapter-text__place"), "chapter placeholder")
    header = _one(root.find("header", "header-chapter"), "chapter header")
    novel = _one(header.find("a", "header-title"), "novel link")
    title = _one(root.find("title"), "chapter title").text()
    if not title:
        raise NovelightError("Missing chapter title")
    links = []
    previous_url = next_url = None
    for a in root.find("a"):
        href = a.attrs.get("href", "")
        if re.fullmatch(r"/book/chapter/[1-9][0-9]*", href):
            url = _link(href, r"/book/chapter/[1-9][0-9]*")
            if url not in links:
                links.append(url)
            if a.find(cls="fa-angle-left"):
                previous_url = url
            if a.find(cls="fa-angle-right"):
                next_url = url
    return {"title": title, "novel_url": _link(novel.attrs.get("href"), r"/book/[A-Za-z0-9_-]+"),
            "novel_title": novel.text(), "previous_url": previous_url, "next_url": next_url,
            "navigation_urls": links}


def parse_reading(data):
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        raise NovelightError("Chapter unavailable: missing public content")
    root = DOM(data["content"]).root
    content = _one(root.find(cls="chapter-text"), "public chapter text")
    text = content.text()
    if not text or re.fullmatch(r"(?:loading|please (?:log ?in|sign in)|(?:this )?chapter (?:is )?(?:locked|unavailable))[.!\s]*", text, re.I):
        raise NovelightError("Chapter unavailable: no readable public prose")
    if re.search(r"(?:log ?in|sign in|pay|purchase|unlock).{0,35}(?:to (?:read|continue)|this chapter)|(?:chapter|content) is (?:locked|paid)", text, re.I):
        raise NovelightError("Chapter unavailable: login or payment required")
    return {"text": text, "text_available": True, "access": "public_anonymous", "scope": "This chapter only; other chapters may be unavailable."}


class Client:
    def __init__(self, cache_dir=CACHE_DIR, session=None):
        self.cache_dir = Path(cache_dir).resolve()
        try:
            self.cache_dir.relative_to(REPO_ROOT)
        except ValueError as exc:
            raise NovelightError("cache_dir must be inside the repository") from exc
        self.session = session or requests.Session()

    def fetch(self, path, params=None, *, ajax=False, referer=None, refresh=False, validate=None):
        if type(refresh) is not bool:
            raise NovelightError("refresh must be a boolean")
        if not re.fullmatch(r"/catalog/|/book/[A-Za-z0-9_-]+|/book/chapter/[1-9][0-9]*|/book/ajax/read-chapter/[1-9][0-9]*|/book/ajax/chapter-pagination", path):
            raise NovelightError("Unsupported request path")
        headers = dict(HEADERS)
        if ajax:
            _link(referer, r"/book/(?:[A-Za-z0-9_-]+|chapter/[1-9][0-9]*)")
            headers.update({"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*; q=0.01", "Referer": referer})
        params = params or {}
        identity = {"url": BASE_URL + path, "params": params, "headers": headers, "mode": "json" if ajax else "html"}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        cache = self.cache_dir / (key + ".json")
        now = datetime.now(timezone.utc)
        if not refresh:
            try:
                row = json.loads(cache.read_text())
                stamp = datetime.fromisoformat(row["fetched_at"])
                age = (now - stamp).total_seconds()
                if row["identity"] == identity and 0 <= age < TTL_SECONDS and isinstance(row["raw"], str):
                    raw = row["raw"]
                    value = json.loads(raw) if ajax else raw
                    parsed = validate(value)
                    return parsed, {"source": "cache", "url": identity["url"], "params": params, "fetched_at": row["fetched_at"]}
            except (OSError, ValueError, KeyError, TypeError):
                pass
        try:
            response = self.session.get(identity["url"], params=params, headers=headers, timeout=(10, 30), allow_redirects=False)
        except requests.RequestException as exc:
            raise NovelightError(f"Novelight request failed: {exc}") from exc
        if response.status_code != 200:
            raise NovelightError(f"Novelight HTTP {response.status_code}: unavailable (404 may mean page out of range)")
        raw = response.text
        try:
            value = json.loads(raw) if ajax else raw
            parsed = validate(value)
        except (ValueError, TypeError, KeyError) as exc:
            raise NovelightError(f"Invalid or unavailable Novelight response: {exc}") from exc
        stamp = now.isoformat()
        temp = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cache_dir, delete=False) as stream:
                temp = stream.name
                json.dump({"identity": identity, "fetched_at": stamp, "raw": raw}, stream)
            os.replace(temp, cache)
        except OSError as exc:
            print(f"Warning: could not write Novelight cache: {exc}", file=sys.stderr)
        finally:
            if temp and os.path.exists(temp):
                try:
                    os.unlink(temp)
                except OSError:
                    pass
        return parsed, {"source": "network", "url": identity["url"], "params": params, "fetched_at": stamp}


def _catalogue(client, params, refresh):
    def validate(html):
        return {**parse_catalogue(html, params["page"]), "taxonomies": parse_taxonomies(html)}
    return client.fetch("/catalog/", params, refresh=refresh, validate=validate)


def _params(search=None, genres=(), tags=(), page=1):
    _positive(page)
    if search is not None and (not isinstance(search, str) or not search.strip() or any(ord(c) < 32 for c in search)):
        raise NovelightError("search must be nonempty text without control characters")
    for kind, values in (("genres", genres), ("tags", tags)):
        if not isinstance(values, (list, tuple)):
            raise NovelightError(f"{kind} must be a list or tuple of names or IDs")
        for v in values:
            if isinstance(v, bool) or not isinstance(v, (int, str)) or not str(v).strip() or any(ord(c) < 32 for c in str(v)):
                raise NovelightError(f"Invalid {kind} filter")
    params = {"ordering": "title", "page": page}
    if search is not None:
        params["search"] = search.strip()
    return params


def _resolve(values, options, label):
    result = []
    for value in values:
        value = str(value).strip()
        matches = [o["id"] for o in options if (o["id"] == int(value) if value.isdecimal() else o["name"].casefold() == value.casefold())]
        if len(matches) != 1:
            raise NovelightError(f"Unknown or ambiguous {label}: {value}")
        result.append(matches[0])
    return sorted(set(result))


def search_novels(search=None, genres=(), tags=(), page=1, refresh=False, client=None):
    params = _params(search, genres, tags, page)
    client = client or Client()
    provenance = []
    if genres or tags:
        base, p = _catalogue(client, _params(), refresh)
        provenance.append(p)
        for kind, values in (("genres", genres), ("tags", tags)):
            if values:
                params[kind] = _resolve(values, base["taxonomies"][kind], kind)
    data, p = _catalogue(client, params, refresh)
    provenance.append(p)
    return {"source": "novelight", "query": params, "novels": data["novels"], "coverage": data["coverage"], "provenance": provenance}


def get_taxonomies(refresh=False, client=None):
    data, p = _catalogue(client or Client(), _params(), refresh)
    return {"source": "novelight", **data["taxonomies"], "provenance": [p]}


def get_novel(slug, chapter_page=1, refresh=False, client=None):
    _slug(slug)
    _positive(chapter_page, "chapter_page")
    client = client or Client()
    url = BASE_URL + "/book/" + slug
    data, p = client.fetch("/book/" + slug, refresh=refresh, validate=parse_detail)
    provenance = [p]
    if chapter_page not in data["chapter_pages"]:
        raise NovelightError("Chapter page out of range")
    if chapter_page != 1:
        data["chapters"], p = client.fetch("/book/ajax/chapter-pagination", {"book_id": data["book_id"], "page": chapter_page},
                                           ajax=True, referer=url, refresh=refresh, validate=parse_chapter_page)
        provenance.append(p)
    pages = data.pop("chapter_pages")
    data["chapter_coverage"] = {"current_page": chapter_page, "pages": pages, "next_page": chapter_page + 1 if chapter_page + 1 in pages else None,
                                "returned": len(data["chapters"]), "scope": "One chapter-list page, not the whole book."}
    return {"source": "novelight", "slug": slug, "url": url, **data, "provenance": provenance}


def get_chapter(chapter_id, refresh=False, client=None):
    _positive(chapter_id, "chapter_id")
    client = client or Client()
    path = f"/book/chapter/{chapter_id}"
    data, p = client.fetch(path, refresh=refresh, validate=parse_wrapper)
    reading, q = client.fetch(f"/book/ajax/read-chapter/{chapter_id}", ajax=True, referer=BASE_URL + path, refresh=refresh, validate=parse_reading)
    return {"source": "novelight", "id": chapter_id, "url": BASE_URL + path, **data, **reading, "provenance": [p, q]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--detail", metavar="SLUG")
    modes.add_argument("--taxonomies", action="store_true")
    modes.add_argument("--chapter", type=int, metavar="POSITIVE_ID")
    parser.add_argument("--search")
    parser.add_argument("--genre", action="append", default=[])
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--page", type=int, default=None)
    parser.add_argument("--chapter-page", type=int, default=None)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    if (args.detail is not None or args.taxonomies or args.chapter is not None) and (args.search is not None or args.genre or args.tag or args.page is not None):
        parser.error("search, genre, tag and page are only allowed in browse mode")
    if args.chapter_page is not None and args.detail is None:
        parser.error("chapter-page is only allowed with detail")
    try:
        if args.detail is not None:
            data = get_novel(args.detail, chapter_page=args.chapter_page if args.chapter_page is not None else 1, refresh=args.refresh)
        elif args.taxonomies:
            data = get_taxonomies(refresh=args.refresh)
        elif args.chapter is not None:
            data = get_chapter(args.chapter, refresh=args.refresh)
        else:
            data = search_novels(args.search, args.genre, args.tag, args.page if args.page is not None else 1, refresh=args.refresh)
        print(json.dumps(data, ensure_ascii=False))
        return 0
    except (NovelightError, requests.RequestException) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
