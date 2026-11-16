#!/usr/bin/env python3
"""Public Fenrir Realm novel metadata, as an importable module and JSON CLI.

Use search_novels(search=None, genres=(), tags=(), page=1, per_page=20),
get_novel(slug), or get_taxonomies(), optionally passing client=Client(...).
Client accepts a repo-local cache_dir and an injectable requests-compatible session.
No chapter text endpoint is used. Search is source keyword search, not semantic
matching; use full detail descriptions to reason about candidate plots.
"""

import argparse
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import os

import requests


BASE_URL = "https://fenrirealm.com"
API_URL = BASE_URL + "/api/new/v2"
REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "out" / "fenrir-realm"
TTL_SECONDS = 24 * 60 * 60
TIMEOUT = (10, 30)
READING_REASON = (
    "Protected chapter text is not supported. Public chapter metadata, including "
    "a zero price or unlocked flag, does not prove that chapter text is readable. "
    "No chapter text is fetched."
)


class FenrirError(ValueError):
    """Invalid input, unavailable source, or unexpected public API data."""


def _slug(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise FenrirError("slug must be a single name containing letters, digits, '_' or '-'")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise FenrirError(f"{label} must be nonempty text without control characters")
    return value.strip()


def _integer(value, label, minimum, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise FenrirError(f"{label} must be an integer in {minimum}..{maximum or 'unbounded'}")
    return value


def _object(value, label):
    if not isinstance(value, dict):
        raise FenrirError(f"Unexpected {label}: expected an object")
    return value


def _array(value, label):
    if not isinstance(value, list):
        raise FenrirError(f"Unexpected {label}: expected an array")
    return value


class _Description(HTMLParser):
    """Keep prose, excluding non-description widgets and executable content."""

    VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "embed"}
    BLOCK = {"p", "div", "section", "li", "br", "h1", "h2", "h3", "blockquote"}
    OMIT = {"script", "style", "iframe", "noscript", "aside", "nav", "button", "form"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        identity = ((attrs.get("class") or "") + " " + (attrs.get("id") or "")).casefold()
        widget = re.search(r"(?:^|[\s_-])(ad|ads|advertisement|advertising|promo|recommendations?|recommended|related)(?:$|[\s_-])", identity)
        omit = (self.stack and self.stack[-1][1]) or tag in self.OMIT or bool(widget) or "adsbygoogle" in identity
        if not omit and tag in self.BLOCK:
            self.parts.append("\n")
        if tag not in self.VOID:
            self.stack.append((tag, bool(omit)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                omitted = self.stack[index][1]
                del self.stack[index:]
                if not omitted and tag in self.BLOCK:
                    self.parts.append("\n")
                break

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)


def clean_description(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise FenrirError("Unexpected description: expected text")
    parser = _Description()
    parser.feed(value)
    return "\n".join(line for line in (re.sub(r"\s+", " ", s).strip() for s in "".join(parser.parts).splitlines()) if line)


def parse_taxonomies(data):
    result = []
    for row in _array(data, "taxonomies"):
        row = _object(row, "taxonomy")
        item = {key: _text(row.get(key), f"taxonomy {key}") for key in ("uuid", "name", "slug")}
        item["description"] = clean_description(row.get("description"))
        result.append(item)
    return sorted(result, key=lambda item: (item["name"].casefold(), item["uuid"]))


def parse_novel(data, *, full=False):
    row = _object(data, "novel")
    slug = _slug(row.get("slug"))
    result = {
        "uuid": _text(row.get("uuid"), "novel uuid"),
        "title": _text(row.get("title"), "novel title"),
        "slug": slug,
        "url": f"{BASE_URL}/series/{slug}",
        "description": clean_description((row.get("description_html") or row.get("description")) if full else row.get("description")),
        "description_scope": "full" if full else "catalogue_truncated",
        "genres": parse_taxonomies(row.get("genres", [])),
    }
    for key in ("status", "type"):
        value = row.get(key)
        if value is not None and not isinstance(value, str):
            raise FenrirError(f"Unexpected novel {key}")
        result[key] = value
    if full:
        if row.get("alt_title") is not None and not isinstance(row["alt_title"], str):
            raise FenrirError("Unexpected novel alt_title")
        result["alt_title"] = row.get("alt_title")
        result["tags"] = parse_taxonomies(row.get("tags", []))
        result["sub_tags"] = parse_taxonomies(row.get("sub_tags", []))
    return result


def parse_catalogue(data):
    row = _object(data, "catalogue")
    novels = [parse_novel(item) for item in _array(row.get("data"), "catalogue data")]
    meta = _object(row.get("meta"), "catalogue meta")
    coverage = {key: _integer(meta.get(key), key, 0 if key == "total" else 1)
                for key in ("current_page", "per_page", "last_page", "total")}
    coverage.update({"returned": len(novels), "catalogue_exhaustive": False,
                     "scope": "One API page only; descriptions are truncated. No full catalogue crawl."})
    return sorted(novels, key=lambda n: (n["title"].casefold(), n["slug"])), coverage


def _chapter_number(value, label, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or not re.fullmatch(r"\d+(?:\.\d+)?", str(value)):
        raise FenrirError(f"Unexpected chapter {label}")
    return value


def parse_chapters(data, slug):
    _slug(slug)
    result = []
    for row in _array(data, "chapters"):
        row = _object(row, "chapter")
        number = _chapter_number(row.get("number"), "number")
        part = _chapter_number(row.get("part"), "part", nullable=True)
        group = row.get("group")
        group_slug = _slug(_object(group, "chapter group").get("slug")) if group is not None else None
        path = f"{number}" + (f".{part}" if part is not None else "")
        locked = row.get("locked")
        price = None
        read_only = None
        if locked is not None:
            locked = _object(locked, "chapter locked")
            price = locked.get("price")
            if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0):
                raise FenrirError("Unexpected chapter price")
            read_only = locked.get("is_read_only")
            if read_only is not None and not isinstance(read_only, bool):
                raise FenrirError("Unexpected chapter is_read_only")
        title = row.get("title") or row.get("name") or ""
        if not isinstance(title, str):
            raise FenrirError("Unexpected chapter title")
        result.append({"number": number, "part": part, "title": title.strip(),
                       "url": f"{BASE_URL}/series/{slug}/" + (group_slug + "/" if group_slug else "") + path,
                       "price": price, "is_read_only": read_only})
    return result


def _normalized(params):
    return {key: sorted(set(value)) if isinstance(value, (list, tuple)) else value
            for key, value in sorted(params.items())}


class Client:
    def __init__(self, *, cache_dir=CACHE_DIR, session=None, clock=None):
        self.cache_dir = Path(cache_dir).resolve()
        if not self.cache_dir.is_relative_to(REPO_ROOT):
            raise FenrirError("cache_dir must be inside the repository")
        self.session = session if session is not None else requests.Session()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _get(self, endpoint, params, validate, refresh=False):
        if type(refresh) is not bool:
            raise FenrirError("refresh must be a boolean")
        params = _normalized(params)
        url = API_URL + endpoint
        identity = {"endpoint": url, "params": params}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        path = self.cache_dir / (key + ".json")
        now = self.clock()
        if not refresh:
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                saved = datetime.fromisoformat(cached["fetched_at"])
                age = (now - saved).total_seconds()
                if cached["identity"] == identity and saved.tzinfo is not None and 0 <= age < TTL_SECONDS:
                    parsed = validate(cached["data"])
                    return parsed, self._provenance(url, params, "cache", cached["fetched_at"])
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
                print(f"Warning: ignoring cache {path.name}: {exc}", file=sys.stderr)
        try:
            response = self.session.get(url, params=params, timeout=TIMEOUT, allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200:
                raise FenrirError(f"HTTP {response.status_code}; redirects are not followed")
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise FenrirError(f"Public API request failed for {url}: {exc}") from exc
        parsed = validate(data)
        fetched_at = self.clock().astimezone(timezone.utc).isoformat()
        temp_path = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cache_dir,
                                             prefix=".cache-", suffix=".tmp", delete=False) as handle:
                temp_path = Path(handle.name)
                json.dump({"identity": identity, "fetched_at": fetched_at, "data": data}, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except (OSError, ValueError, TypeError) as exc:
            print(f"Warning: could not write cache: {exc}", file=sys.stderr)
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError as exc:
                    print(f"Warning: could not remove cache temporary file: {exc}", file=sys.stderr)
        return parsed, self._provenance(url, params, "live", fetched_at)

    @staticmethod
    def _provenance(url, params, source, fetched_at):
        return {"url": url, "params": params, "source": source, "fetched_at": fetched_at}


def _base_result(operation, provenance):
    return {"operation": operation, "requests": provenance, "chapter_text_available": False,
            "chapter_text_unavailable_reason": READING_REASON}


def _filters(values, label):
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise FenrirError(f"{label} must be a list or tuple of names, slugs or UUIDs")
    return [_text(value, label) for value in values]


def _resolve(values, taxonomy, label):
    resolved = set()
    for value in values:
        matches = {item["uuid"] for item in taxonomy
                   if value.casefold() in {item[key].casefold() for key in ("uuid", "name", "slug")}}
        if len(matches) != 1:
            raise FenrirError(f"{'Unknown' if not matches else 'Ambiguous'} {label} filter: {value}")
        resolved.update(matches)
    return sorted(resolved)


def search_novels(search=None, *, genres=(), tags=(), page=1, per_page=20, refresh=False, client=None):
    """Browse/search one page. Resolve filters exactly; never claim exhaustive matching."""
    if search is not None:
        search = _text(search, "search")
    _integer(page, "page", 1)
    _integer(per_page, "per_page", 1, 100)
    genres, tags = _filters(genres, "genre"), _filters(tags, "tag")
    client = client if client is not None else Client()
    params = {"page": page, "per_page": per_page, "sort": "title"}
    provenance = []
    if search is not None:
        params["search"] = search
    for label, values in (("genres", genres), ("tags", tags)):
        if values:
            taxonomy, request = client._get(f"/taxonomies/{label}", {}, parse_taxonomies, refresh)
            provenance.append(request)
            params[label + "[]"] = _resolve(values, taxonomy, label)
    (novels, coverage), request = client._get("/series", params, parse_catalogue, refresh)
    provenance.append(request)
    result = _base_result("search" if search is not None else "browse", provenance)
    result.update({"novels": novels, "coverage": coverage})
    return result


def get_novel(slug, *, refresh=False, client=None):
    """Full public description and chapter metadata, not protected chapter text."""
    _slug(slug)
    client = client if client is not None else Client()
    def validate_detail(data):
        novel = parse_novel(data, full=True)
        if novel["slug"] != slug:
            raise FenrirError("Unexpected detail slug does not match requested novel")
        return novel
    novel, detail_request = client._get(f"/series/{slug}", {}, validate_detail, refresh)
    chapters, chapter_request = client._get(f"/series/{slug}/chapters", {}, lambda data: parse_chapters(data, slug), refresh)
    result = _base_result("detail", [detail_request, chapter_request])
    result.update({"novel": novel, "chapters": chapters,
                   "coverage": {"scope": "One novel and its public chapter list; no chapter text.",
                                "chapters_returned": len(chapters), "catalogue_exhaustive": False}})
    return result


def get_taxonomies(*, refresh=False, client=None):
    client = client if client is not None else Client()
    provenance = []
    result = _base_result("taxonomies", provenance)
    for label in ("genres", "tags"):
        result[label], request = client._get(f"/taxonomies/{label}", {}, parse_taxonomies, refresh)
        provenance.append(request)
    result["coverage"] = {"scope": "Public genre and tag taxonomy lists, not a novel catalogue.", "catalogue_exhaustive": False}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search")
    parser.add_argument("--genre", action="append", default=[])
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--page", type=int, default=None)
    parser.add_argument("--per-page", type=int, default=None)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--detail", metavar="SLUG")
    operation.add_argument("--taxonomies", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    try:
        if (args.detail is not None or args.taxonomies) and (args.search is not None or args.genre or args.tag or args.page is not None or args.per_page is not None):
            raise FenrirError("Search, genre, tag and pagination parameters cannot accompany detail or taxonomies")
        if args.detail is not None:
            result = get_novel(args.detail, refresh=args.refresh)
        elif args.taxonomies:
            result = get_taxonomies(refresh=args.refresh)
        else:
            result = search_novels(args.search, genres=args.genre, tags=args.tag,
                                   page=args.page if args.page is not None else 1,
                                   per_page=args.per_page if args.per_page is not None else 20,
                                   refresh=args.refresh)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except FenrirError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
