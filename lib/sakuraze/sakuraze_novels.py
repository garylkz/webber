#!/usr/bin/env python3
"""Anonymous Sakuraze discovery and selected public chapters, with a 24h local cache.

search_novels(search=None, genres=(), tags=(), page=1, per_page=20),
get_novel(slug), get_taxonomies(), get_chapter(slug, number) accept refresh and
an optional client=Client(). Search is local substring matching, not semantic
ranking. Genre filters use any, tag filters use all, with exact names/known IDs.
Only requests is a third-party dependency.
"""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urljoin, urlsplit
from uuid import UUID

import requests

BASE_URL = "https://sakuraze.vercel.app"
PROJECT = "hlzjslwrhabsxdskinwd"
SUPABASE_URL = f"https://{PROJECT}.supabase.co"
API_URL = SUPABASE_URL + "/rest/v1/"
REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / "out" / "sakuraze"
TTL_SECONDS = 86400
CACHE_VERSION = 1
TIMEOUT = (10, 30)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
SELECT = ("id,slug,title,alternative_title,description,author_id,original_author,"
          "novel_type,type,country_of_origin,year_of_release,status,translation_status,"
          "is_locked,is_nsfw,is_visible,approval_status,updated_at,"
          "author:profiles(username,display_name),novel_genres:novel_genres(genres(name)),"
          "novel_tags:novel_tags(tags(name))")
SCOPE = {"select": SELECT, "is_visible": "eq.true", "approval_status": "eq.approved", "is_nsfw": "eq.false"}
SNAPSHOT_PARAMS = dict(SCOPE, order="updated_at.desc", limit=500)


class SakurazeError(ValueError):
    """Bad input, unavailable source, or unsupported public response."""


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise SakurazeError(f"{label} must be nonempty text without control characters")
    return value.strip()


def _slug(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise SakurazeError("slug must contain only letters, digits, '_' or '-'")
    return value


def _integer(value, label, minimum=1, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise SakurazeError(f"{label} must be an integer in {minimum}..{maximum or 'unbounded'}")
    return value


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value.lower():
            raise ValueError()
    except (ValueError, AttributeError):
        raise SakurazeError("Unexpected UUID") from None
    return value.lower()


def _object(value):
    if not isinstance(value, dict):
        raise SakurazeError("Expected a response object")
    return value


def _array(value):
    if not isinstance(value, list):
        raise SakurazeError("Expected a response array")
    return value


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.omit = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])
        if tag in {"script", "style", "iframe", "noscript"}:
            self.omit.append(tag)
        if not self.omit and tag in {"p", "div", "br", "li", "h1", "h2", "blockquote"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.omit:
            self.omit.remove(tag)
        if not self.omit and tag in {"p", "div", "li", "blockquote"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.omit:
            self.parts.append(data)


def clean_text(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SakurazeError("Expected HTML text")
    parser = _HTML()
    parser.feed(value)
    return "\n".join(part for part in (re.sub(r"\s+", " ", line).strip()
                                     for line in "".join(parser.parts).splitlines()) if part)


def _names(row, label):
    singular = "genres" if label == "novel_genres" else "tags"
    result = set()
    for join in _array(row.get(label)):
        item = _object(_object(join).get(singular))
        result.add(_text(item.get("name"), singular))
    return sorted(result, key=str.casefold)


def parse_novel(row):
    row = _object(row)
    if row.get("is_visible") is not True or row.get("approval_status") != "approved" or row.get("is_nsfw") is not False:
        raise SakurazeError("Novel response violates the public approved non-NSFW scope")
    if type(row.get("is_locked")) is not bool:
        raise SakurazeError("Unexpected novel lock state")
    slug = _slug(row.get("slug"))
    novel = {"id": _uuid(row.get("id")), "slug": slug, "title": _text(row.get("title"), "title"),
             "url": f"{BASE_URL}/novel/{slug}", "description": clean_text(row.get("description")),
             "genres": _names(row, "novel_genres"), "tags": _names(row, "novel_tags"),
             "is_locked": row["is_locked"]}
    for key in ("alternative_title", "original_author", "novel_type", "type", "country_of_origin",
                "status", "translation_status", "updated_at"):
        value = row.get(key)
        if value is not None and not isinstance(value, str):
            raise SakurazeError(f"Unexpected novel {key}")
        novel[key] = value
    year = row.get("year_of_release")
    if year is not None and (type(year) not in (int, str)):
        raise SakurazeError("Unexpected year_of_release")
    novel["year_of_release"] = year
    author = row.get("author")
    authors = [novel["original_author"]] if novel["original_author"] else []
    if author is not None:
        author = _object(author)
        for key in ("display_name", "username"):
            if author.get(key) is not None:
                authors.append(_text(author[key], "author"))
    novel["authors"] = list(dict.fromkeys(authors))
    return novel


def _novels(data):
    novels = [parse_novel(row) for row in _array(data)]
    if len({n["id"] for n in novels}) != len(novels) or len({n["slug"] for n in novels}) != len(novels):
        raise SakurazeError("Duplicate novels in response")
    return novels


def _config(data):
    data = _object(data)
    if data.get("url") != SUPABASE_URL:
        raise SakurazeError("Unexpected public API project")
    key = data.get("key")
    try:
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", key):
            raise ValueError()
        payload = key.split(".")[1]
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if decoded.get("role") != "anon" or decoded.get("ref") != PROJECT:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise SakurazeError("Invalid frontend anonymous configuration") from None
    return {"url": SUPABASE_URL, "key": key}


class Client:
    """Read-only allowlisted requests; injectable session and UTC clock for tests."""

    def __init__(self, *, cache_dir=CACHE_DIR, session=None, clock=None):
        self.cache_dir = Path(cache_dir).resolve()
        self._check_cache_dir()
        self.session = session if session is not None else requests.Session()
        # Do not inherit browser/account cookies or environment credentials.
        if isinstance(self.session, requests.Session):
            self.session.trust_env = False
            self.session.auth = None
            self.session.headers.clear()
            self.session.params.clear()
            self.session.cookies.clear()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _check_cache_dir(self):
        if not self.cache_dir.resolve().is_relative_to(REPO_ROOT):
            raise SakurazeError("cache_dir must be inside the repository")

    @staticmethod
    def _identity(method, url, params=None, body=None):
        return {"version": CACHE_VERSION, "method": method, "url": url, "params": params, "body": body}

    def _path(self, identity):
        self._check_cache_dir()
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        path = self.cache_dir / (digest + ".json")
        if not path.resolve().is_relative_to(REPO_ROOT):
            raise SakurazeError("cache path must be inside the repository")
        return path

    def _cached(self, identity, validate):
        try:
            entry = _object(json.loads(self._path(identity).read_text(encoding="utf-8")))
            _object(entry.get("headers"))
            saved = datetime.fromisoformat(entry["fetched_at"])
            age = (self.clock() - saved).total_seconds()
            if entry["identity"] == identity and saved.tzinfo is not None and 0 <= age < TTL_SECONDS:
                return validate(entry["data"], entry.get("headers", {})), entry
        except (OSError, ValueError, TypeError, KeyError, OverflowError):
            pass
        return None

    def _save(self, identity, data, headers):
        entry = {"identity": identity, "fetched_at": self.clock().astimezone(timezone.utc).isoformat(),
                 "data": data, "headers": headers}
        temporary = None
        try:
            path = self._path(identity)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cache_dir,
                                             prefix=".cache-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(entry, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except (OSError, ValueError, TypeError):
            print("Warning: could not write Sakuraze cache; using live response", file=sys.stderr)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    print("Warning: could not remove Sakuraze cache temporary file", file=sys.stderr)
        return entry

    def _http(self, method, url, *, headers=None, params=None, body=None):
        try:
            if isinstance(self.session, requests.Session):
                self.session.cookies.clear()
            response = self.session.request(method, url, headers=dict(HEADERS, **(headers or {})),
                                            params=params, json=body, timeout=TIMEOUT, allow_redirects=False)
            if response.status_code != 200:
                raise SakurazeError(f"Public source returned HTTP {response.status_code}; redirects are not followed")
            return response
        except requests.RequestException:
            # Do not include exception text, headers or response bodies: they may contain the key.
            raise SakurazeError("Public source request failed") from None

    def _public_config(self, refresh):
        identity = self._identity("GET", BASE_URL + "/#public-config")
        cached = None if refresh else self._cached(identity, lambda data, headers: _config(data))
        if cached:
            return cached[0]
        root = self._http("GET", BASE_URL + "/")
        parser = _HTML()
        parser.feed(root.text)
        assets = []
        for src in parser.scripts:
            url = urljoin(BASE_URL + "/", src)
            parsed = urlsplit(url)
            if (parsed.scheme == "https" and parsed.netloc == urlsplit(BASE_URL).netloc
                    and not parsed.query and not parsed.fragment
                    and re.fullmatch(r"/assets/[A-Za-z0-9_.-]+\.js", parsed.path)):
                assets.append(url)
        # The application entrypoint must be unambiguous. Never scan arbitrary scripts.
        if len(set(assets)) != 1:
            raise SakurazeError("Could not identify a unique same-origin frontend asset")
        script = self._http("GET", assets[0]).text
        urls = set(re.findall(r'\bms\s*=\s*["\']([^"\']+)["\']', script))
        keys = set(re.findall(r'\bus\s*=\s*["\']([^"\']+)["\']', script))
        if urls != {SUPABASE_URL} or len(keys) != 1:
            raise SakurazeError("Could not discover expected anonymous frontend configuration")
        config = _config({"url": urls.pop(), "key": keys.pop()})
        self._save(identity, config, {})
        return config

    def _read(self, method, endpoint, params, body, validate, refresh=False):
        if type(refresh) is not bool:
            raise SakurazeError("refresh must be a boolean")
        allowed = False
        if method == "GET" and endpoint == "novels" and body is None:
            allowed = params == SNAPSHOT_PARAMS or (
                isinstance(params, dict) and set(params) == set(SCOPE) | {"slug"}
                and all(params.get(k) == v for k, v in SCOPE.items())
                and isinstance(params.get("slug"), str) and params["slug"].startswith("eq.")
                and _slug(params["slug"][3:]))
        elif method == "GET" and endpoint == "genres":
            allowed = params == {"select": "*", "order": "name"} and body is None
        elif method == "POST" and params is None and isinstance(body, dict):
            if endpoint == "rpc/get_chapter_list_for_reader" and set(body) == {"p_novel_id"}:
                allowed = bool(_uuid(body["p_novel_id"]))
            elif endpoint == "rpc/get_chapter_for_reader" and set(body) == {"p_novel_id", "p_chapter_number", "p_preview_chars"}:
                allowed = (bool(_uuid(body["p_novel_id"])) and bool(_integer(body["p_chapter_number"], "chapter"))
                           and type(body["p_preview_chars"]) is int and body["p_preview_chars"] == 1600)
        if not allowed:
            raise SakurazeError("Request is outside the read-only endpoint allowlist")
        url = API_URL + endpoint
        identity = self._identity(method, url, params, body)
        cached = None if refresh else self._cached(identity, validate)
        if cached:
            parsed, entry = cached
            source = "cache"
        else:
            config = self._public_config(refresh)
            response = self._http(method, url, headers={"apikey": config["key"],
                                  "Authorization": "Bearer " + config["key"], "Prefer": "count=exact"},
                                  params=params, body=body)
            try:
                data = response.json()
            except ValueError:
                raise SakurazeError("Public source returned invalid JSON") from None
            headers = {"content-range": response.headers.get("Content-Range", "")}
            parsed = validate(data, headers)
            entry = self._save(identity, data, headers)
            source = "live"
        provenance = {"url": url, "method": method, "params": params, "body": body,
                      "source": source, "fetched_at": entry["fetched_at"], "cache_ttl_seconds": TTL_SECONDS}
        return parsed, provenance


def _snapshot(data, headers):
    novels = _novels(data)
    if len(novels) > 500:
        raise SakurazeError("Snapshot exceeds requested cap")
    value = headers.get("content-range", "")
    if not isinstance(value, str):
        raise SakurazeError("Unexpected Content-Range type")
    match = re.fullmatch(r"(?:(\d+)-(\d+)|\*)/(\d+|\*)", value)
    if value and match is None:
        raise SakurazeError("Malformed snapshot Content-Range")
    total = None
    if match:
        total = int(match[3]) if match[3] != "*" else None
        if match[1] is not None and (int(match[1]) != 0 or int(match[2]) + 1 != len(novels)):
            raise SakurazeError("Unexpected snapshot Content-Range")
    if total is not None and total < len(novels):
        raise SakurazeError("Source count is smaller than snapshot")
    coverage = {"snapshot_count": len(novels), "source_count": total, "snapshot_cap": 500,
                "partial": total is None or len(novels) < total,
                "scope": "Bounded visible, approved, non-NSFW snapshot; filtering and pagination are local."}
    return novels, coverage


def _genres(data, headers):
    result = []
    for row in _array(data):
        row = _object(row)
        result.append({"id": _uuid(row.get("id")), "name": _text(row.get("name"), "genre")})
    if len({r["id"] for r in result}) != len(result):
        raise SakurazeError("Duplicate genre IDs")
    return sorted(result, key=lambda row: (row["name"].casefold(), row["id"]))


def _tags(novels):
    return [{"id": None, "name": name} for name in sorted({t for n in novels for t in n["tags"]}, key=str.casefold)]


def _filters(values, label):
    if not isinstance(values, (list, tuple)):
        raise SakurazeError(f"{label} must be a list or tuple")
    return [_text(v, label) for v in values]


def _resolve(values, taxonomy, label):
    result = set()
    for value in values:
        matches = [row for row in taxonomy if value.casefold() in {row["name"].casefold(), (row["id"] or "").casefold()}]
        if len(matches) != 1:
            raise SakurazeError(f"{'Unknown' if not matches else 'Ambiguous'} {label} filter: {value}")
        result.add(matches[0]["name"].casefold())
    return result


def _result(operation, requests_log, **data):
    return {"source": "sakuraze", "source_url": BASE_URL, "operation": operation, "requests": requests_log, **data}


def search_novels(search=None, *, genres=(), tags=(), page=1, per_page=20, refresh=False, client=None):
    """Casefolded substring across title, summary, authors, genres and tags."""
    if search is not None:
        search = _text(search, "search")
    _integer(page, "page")
    _integer(per_page, "per_page", maximum=100)
    genres, tags = _filters(genres, "genre"), _filters(tags, "tag")
    client = client if client is not None else Client()
    (novels, coverage), request = client._read("GET", "novels", SNAPSHOT_PARAMS, None, _snapshot, refresh)
    requests_log = [request]
    taxonomy = []
    if genres:
        taxonomy, request = client._read("GET", "genres", {"select": "*", "order": "name"}, None, _genres, refresh)
        requests_log.append(request)
    genre_names = _resolve(genres, taxonomy, "genre")
    tag_names = _resolve(tags, _tags(novels), "tag")
    matches = []
    for novel in novels:
        haystack = "\n".join([novel["title"], novel["alternative_title"] or "", novel["description"],
                               *novel["authors"], *novel["genres"], *novel["tags"]]).casefold()
        if search is not None and search.casefold() not in haystack:
            continue
        if genre_names and not genre_names.intersection(g.casefold() for g in novel["genres"]):
            continue
        if not tag_names.issubset(t.casefold() for t in novel["tags"]):
            continue
        matches.append(novel)
    coverage.update({"matched_count": len(matches), "page": page, "per_page": per_page,
                     "returned": len(matches[(page - 1) * per_page:page * per_page])})
    return _result("search" if search is not None else "browse", requests_log,
                   novels=matches[(page - 1) * per_page:page * per_page], coverage=coverage,
                   filter_semantics={"search": "casefold substring", "genres": "any exact name or known ID",
                                     "tags": "all exact names; source joins do not expose tag IDs"})


def get_taxonomies(*, refresh=False, client=None):
    client = client if client is not None else Client()
    genres, gp = client._read("GET", "genres", {"select": "*", "order": "name"}, None, _genres, refresh)
    (novels, coverage), np = client._read("GET", "novels", SNAPSHOT_PARAMS, None, _snapshot, refresh)
    return _result("taxonomies", [gp, np], genres=genres, tags=_tags(novels), coverage=coverage,
                   tags_scope="Distinct names from snapshot only; tag IDs are not exposed by source joins.")


def _detail(client, slug, refresh):
    def validate(data, headers):
        novels = _novels(data)
        if len(novels) != 1 or novels[0]["slug"] != slug:
            raise SakurazeError("Public novel not found or detail response mismatched")
        return novels[0]
    return client._read("GET", "novels", dict(SCOPE, slug="eq." + slug), None, validate, refresh)


def _chapter_metadata(row, novel_id, slug):
    row = _object(row)
    if _uuid(row.get("novel_id")) != novel_id:
        raise SakurazeError("Chapter novel ID mismatch")
    number = _integer(row.get("chapter_number"), "chapter number")
    if type(row.get("is_premium")) is not bool:
        raise SakurazeError("Unexpected premium state")
    cost = row.get("coin_cost")
    if cost is not None:
        _integer(cost, "coin cost", 0)
    title = row.get("title")
    if not isinstance(title, str):
        raise SakurazeError("Unexpected chapter title")
    result = {"id": _uuid(row.get("id")), "title": title.strip(),
              "chapter_number": number, "is_premium": row["is_premium"], "coin_cost": cost,
              "url": f"{BASE_URL}/novel/{slug}/chapter/{number}"}
    for key in ("scheduled_free_at", "created_at"):
        value = row.get(key)
        if value is not None and not isinstance(value, str):
            raise SakurazeError(f"Unexpected chapter {key}")
        result[key] = value
    return result


def get_novel(slug, *, refresh=False, client=None):
    _slug(slug)
    client = client if client is not None else Client()
    novel, np = _detail(client, slug, refresh)
    def validate(data, headers):
        chapters = [_chapter_metadata(row, novel["id"], slug) for row in _array(data)]
        if len({c["chapter_number"] for c in chapters}) != len(chapters):
            raise SakurazeError("Duplicate chapter numbers")
        return sorted(chapters, key=lambda c: c["chapter_number"])
    chapters, cp = client._read("POST", "rpc/get_chapter_list_for_reader", None,
                                 {"p_novel_id": novel["id"]}, validate, refresh)
    return _result("detail", [np, cp], novel=novel, chapters=chapters,
                   coverage={"chapters_returned": len(chapters), "scope": "One public novel and reader chapter list."})


def get_chapter(slug, number, *, refresh=False, client=None):
    """Return only an explicitly free full chapter or explicitly labelled public preview."""
    _slug(slug)
    _integer(number, "chapter")
    client = client if client is not None else Client()
    novel, np = _detail(client, slug, refresh)
    if novel["is_locked"]:
        raise SakurazeError("Novel is locked; no chapter text request made")
    def validate(data, headers):
        data = _object(data)
        access, full = data.get("access_status"), data.get("is_full_content")
        if data.get("success") is not True or not ((access == "free" and full is True) or (access == "preview" and full is False)):
            raise SakurazeError("Chapter unavailable or unsupported access flags")
        row = _object(data.get("chapter"))
        chapter = _chapter_metadata(row, novel["id"], slug)
        if chapter["chapter_number"] != number or not isinstance(row.get("content"), str) or not row["content"].strip():
            raise SakurazeError("Chapter number mismatch or unavailable content")
        text = clean_text(row["content"])
        if not text:
            raise SakurazeError("Chapter unavailable: no readable public text")
        chapter.update({"access_status": access, "is_full_content": full,
                        "content_html": row["content"], "text": text})
        return chapter
    chapter, cp = client._read("POST", "rpc/get_chapter_for_reader", None,
                              {"p_novel_id": novel["id"], "p_chapter_number": number, "p_preview_chars": 1600}, validate, refresh)
    return _result("chapter", [np, cp], novel={k: novel[k] for k in ("id", "slug", "title", "url")}, chapter=chapter)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search")
    parser.add_argument("--genre", action="append", default=[])
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--page", type=int)
    parser.add_argument("--per-page", type=int)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--detail", metavar="SLUG")
    modes.add_argument("--taxonomies", action="store_true")
    modes.add_argument("--chapter", type=int, metavar="NUMBER")
    parser.add_argument("--novel", metavar="SLUG")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args(argv)
    try:
        other_mode = args.detail is not None or args.taxonomies or args.chapter is not None
        if other_mode and (args.search is not None or args.genre or args.tag or args.page is not None or args.per_page is not None):
            raise SakurazeError("Search filters and pagination cannot accompany other modes")
        if (args.novel is not None) != (args.chapter is not None):
            raise SakurazeError("--novel is required for, and only permitted with, --chapter")
        if args.detail is not None:
            result = get_novel(args.detail, refresh=args.refresh)
        elif args.taxonomies:
            result = get_taxonomies(refresh=args.refresh)
        elif args.chapter is not None:
            result = get_chapter(args.novel, args.chapter, refresh=args.refresh)
        else:
            result = search_novels(args.search, genres=args.genre, tags=args.tag,
                                   page=args.page if args.page is not None else 1,
                                   per_page=args.per_page if args.per_page is not None else 20, refresh=args.refresh)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except SakurazeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
