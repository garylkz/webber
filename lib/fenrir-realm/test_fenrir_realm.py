"""Offline tests using selected real public JSON responses plus malformed variants."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

import pytest
import requests

import fenrir_realm_novels as fr


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Offline tests must not make network requests")
    monkeypatch.setattr(requests.sessions.Session, "request", fail)


@pytest.fixture
def sample():
    return json.loads((Path(__file__).parent / "fixtures" / "public_sample.json").read_text())


class Response:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return deepcopy(self.data)


class Session:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        endpoint = url.removeprefix(fr.API_URL)
        value = self.routes[endpoint]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, Response) else Response(value)


@pytest.fixture
def cache_root():
    # Keep test files inside the repo even when pytest's default temp root is not.
    root = fr.REPO_ROOT / "out" / "pytest-fenrir-realm"
    root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="cache-", dir=root))
    yield path
    shutil.rmtree(path)


@pytest.fixture
def setup_client(cache_root, sample):
    slug = sample["detail"]["slug"]
    session = Session({"/series": sample["catalogue"],
                       "/taxonomies/genres": sample["genres"],
                       "/taxonomies/tags": sample["tags"],
                       f"/series/{slug}": sample["detail"],
                       f"/series/{slug}/chapters": sample["chapters"]})
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    client = fr.Client(cache_dir=cache_root / "cache", session=session, clock=lambda: now[0])
    return client, session, now


def test_real_catalogue_detail_and_no_reading(sample, setup_client):
    client, session, _ = setup_client
    result = fr.search_novels("Atypical Profiler", per_page=1, client=client)
    novel = result["novels"][0]
    assert novel["title"] == sample["catalogue"]["data"][0]["title"]
    assert novel["description_scope"] == "catalogue_truncated"
    assert novel["url"].startswith(fr.BASE_URL + "/series/")
    assert not {"stats", "cover", "cover_data_url", "user"} & novel.keys()
    assert result["coverage"]["catalogue_exhaustive"] is False
    assert result["coverage"]["returned"] == 1
    detail = fr.get_novel(novel["slug"], client=client)
    assert detail["novel"]["description_scope"] == "full"
    assert "Yin Guihu" in detail["novel"]["description"]
    assert detail["novel"]["tags"]
    assert len(detail["requests"]) == 2
    assert detail["chapter_text_available"] is False
    assert "does not prove" in detail["chapter_text_unavailable_reason"]
    total = detail["coverage"]["chapters_total"]
    limited = fr.get_novel(novel["slug"], chapter_limit=1, client=client)
    assert len(limited["chapters"]) == 1
    assert limited["coverage"]["chapters_total"] == total
    assert limited["coverage"]["chapters_truncated"] is (total > 1)
    assert limited["latest_chapter"] == detail["latest_chapter"]
    with pytest.raises(fr.FenrirError):
        fr.get_novel(novel["slug"], chapter_limit=0, client=client)
    chapter = detail["chapters"][0]
    assert chapter["price"] == 0
    assert "is_locked" not in chapter
    assert "text" not in chapter and "readable" not in chapter
    assert chapter["url"] == novel["url"] + "/1"
    assert len(session.calls) == 3
    for _, kwargs in session.calls:
        assert kwargs["timeout"] == (10, 30)
        assert kwargs["allow_redirects"] is False


def test_html_prose_only():
    html = ('<p>Hero &amp; friend.</p><div class="recommendations"><p>Read Spam</p></div>'
            '<aside>Buy now</aside><script>bad()</script><div id="ad-banner">Ad</div>'
            '<iframe>tracking</iframe><p>Adventure<br>continues.</p>')
    assert fr.clean_description(html) == "Hero & friend.\nAdventure\ncontinues."
    assert fr.clean_description(None) == ""
    assert fr.clean_description("plain\ntext") == "plain\ntext"


def test_full_html_excludes_widgets(sample):
    data = deepcopy(sample["detail"])
    data["description_html"] = '<p>Actual plot</p><div class="related-novels">Not plot</div>'
    assert fr.parse_novel(data, full=True)["description"] == "Actual plot"


def test_chapter_parts_groups_and_unknown_price(sample):
    chapter = deepcopy(sample["chapters"][0])
    chapter.update(number=12, part=2, slug="volume-1/12-2", group={"slug": "volume-1"},
                   locked={"price": 5, "is_read_only": True})
    parsed = fr.parse_chapters([chapter], "novel")[0]
    assert parsed["url"] == fr.BASE_URL + "/series/novel/volume-1/12-2"
    assert parsed["price"] == 5 and parsed["is_read_only"] is True
    chapter["locked"] = None
    assert fr.parse_chapters([chapter], "novel")[0]["price"] is None
    chapter["slug"] = None
    chapter["group"]["slug"] = "../../other"
    with pytest.raises(fr.FenrirError):
        fr.parse_chapters([chapter], "novel")


@pytest.mark.parametrize("canonical,group,expected", [
    ("12-2", {"slug": "volume-1"}, "12-2"),
    ("volume-1/12-2", {"slug": "volume-1"}, "volume-1/12-2"),
    ("special-ending", None, "special-ending"),
    (None, {"slug": "volume-1"}, "volume-1/12-2"),
    ("", None, "12-2"),
    ("   ", None, "12-2"),
])
def test_chapter_canonical_slug_precedes_group_and_number(sample, canonical, group, expected):
    chapter = dict(sample["chapters"][0], number=12, part=2, slug=canonical, group=group)
    assert fr.parse_chapters([chapter], "novel")[0]["url"] == fr.BASE_URL + "/series/novel/" + expected


@pytest.mark.parametrize("canonical", [
    "../secret", "volume/../secret", ".", "..", "/12-2", "//example.com/12",
    "https://example.com/12", "12?redirect=evil", "12#fragment", "12//2",
    "volume/%2e%2e/12", "volume\\12", "12\n2", 12,
])
def test_unsafe_chapter_slugs_rejected(sample, canonical):
    chapter = dict(sample["chapters"][0], slug=canonical)
    with pytest.raises(fr.FenrirError, match="chapter slug"):
        fr.parse_chapters([chapter], "novel")


def test_taxonomies_filter_resolution_and_normalization(setup_client, sample):
    client, session, _ = setup_client
    genre, tag = sample["genres"][0], sample["tags"][0]
    result = fr.search_novels(genres=[genre["name"].upper(), genre["uuid"], genre["slug"]],
                              tags=[tag["slug"]], client=client)
    params = session.calls[-1][1]["params"]
    assert params["genres[]"] == [genre["uuid"]]
    assert params["tags[]"] == [tag["uuid"]]
    assert params["sort"] == "title"
    assert len(result["requests"]) == 3
    result = fr.search_novels(genres=[genre["slug"]], tags=[tag["name"]], client=client)
    assert all(r["source"] == "cache" for r in result["requests"])
    assert len(session.calls) == 3
    taxonomy = fr.get_taxonomies(client=client)
    assert taxonomy["genres"][0]["description"]
    assert not taxonomy["chapter_text_available"]


@pytest.mark.parametrize("ambiguous", [False, True])
def test_filter_errors_no_catalogue_request(setup_client, sample, ambiguous):
    client, session, _ = setup_client
    name = "not a genre"
    if ambiguous:
        row = deepcopy(sample["genres"][0])
        row["uuid"] = "other"
        session.routes["/taxonomies/genres"].append(row)
        name = row["name"]
    with pytest.raises(fr.FenrirError, match="Ambiguous" if ambiguous else "Unknown"):
        fr.search_novels(genres=[name], client=client)
    assert len(session.calls) == 1


def test_empty_results_and_stable_sort(setup_client, sample):
    client, session, _ = setup_client
    session.routes["/series"] = {"data": [], "meta": {"current_page": 1, "per_page": 20, "last_page": 1, "total": 0}}
    result = fr.search_novels(client=client)
    assert result["novels"] == [] and result["coverage"]["total"] == 0
    a = deepcopy(sample["catalogue"]["data"][0])
    b = deepcopy(a)
    a.update(title="Zebra", slug="a")
    b.update(title="apple", slug="b")
    session.routes["/series"]["data"] = [a, b]
    result = fr.search_novels(refresh=True, client=client)
    assert [n["title"] for n in result["novels"]] == ["apple", "Zebra"]


@pytest.mark.parametrize("kwargs", [{"page": 0}, {"page": True}, {"per_page": 101}, {"per_page": 0},
                                    {"search": " "}, {"search": "a\n"}, {"tags": [""]},
                                    {"genres": "Action"}, {"page": "1"}])
def test_invalid_search_before_network(setup_client, kwargs):
    client, session, _ = setup_client
    with pytest.raises(fr.FenrirError):
        fr.search_novels(client=client, **kwargs)
    assert not session.calls


@pytest.mark.parametrize("slug", ["../foo", "https://other/a", "a/b", "a?x=1", "a#b", "a%2fb", "", "a b", ".", None])
def test_invalid_slug_before_network(setup_client, slug):
    client, session, _ = setup_client
    with pytest.raises(fr.FenrirError):
        fr.get_novel(slug, client=client)
    assert not session.calls


def test_cache_ttl_refresh_provenance_and_atomic(setup_client):
    client, session, now = setup_client
    first = fr.search_novels(client=client)
    files = list(client.cache_dir.iterdir())
    assert len(files) == 1 and files[0].suffix == ".json"
    envelope = json.loads(files[0].read_text())
    identity = json.dumps(envelope["identity"], sort_keys=True, separators=(",", ":"))
    assert files[0].stem == hashlib.sha256(identity.encode()).hexdigest()
    now[0] += timedelta(hours=23, minutes=59)
    cached = fr.search_novels(client=client)
    assert cached["requests"][0]["source"] == "cache"
    assert cached["requests"][0]["fetched_at"] == first["requests"][0]["fetched_at"]
    assert len(session.calls) == 1
    now[0] += timedelta(minutes=1)
    assert fr.search_novels(client=client)["requests"][0]["source"] == "live"
    assert len(session.calls) == 2
    fr.search_novels(refresh=True, client=client)
    assert len(session.calls) == 3


@pytest.mark.parametrize("corruption", ["json", "params", "endpoint", "future", "naive", "data", "null"])
def test_unusable_cache_ignored(setup_client, corruption):
    client, session, now = setup_client
    fr.search_novels(client=client)
    path = next(client.cache_dir.glob("*.json"))
    envelope = json.loads(path.read_text())
    if corruption == "json":
        path.write_text("broken{")
    elif corruption == "null":
        path.write_text("null")
    else:
        if corruption == "params":
            envelope["identity"]["params"]["page"] = 99
        elif corruption == "endpoint":
            envelope["identity"]["endpoint"] = "https://foreign.invalid"
        elif corruption == "future":
            envelope["fetched_at"] = (now[0] + timedelta(seconds=1)).isoformat()
        elif corruption == "naive":
            envelope["fetched_at"] = "2026-01-01T00:00:00"
        elif corruption == "data":
            envelope["data"] = {"data": "oops"}
        path.write_text(json.dumps(envelope))
    assert fr.search_novels(client=client)["requests"][0]["source"] == "live"
    assert len(session.calls) == 2


def test_cache_keys_include_page_search_filters(setup_client, sample):
    client, session, _ = setup_client
    fr.search_novels(client=client)
    fr.search_novels(page=2, client=client)
    fr.search_novels(per_page=5, client=client)
    fr.search_novels(search="magic", client=client)
    fr.search_novels(genres=[sample["genres"][0]["slug"]], client=client)
    assert len(list(client.cache_dir.glob("*.json"))) == 6


@pytest.mark.parametrize("refresh", [False, True])
def test_no_stale_fallback_on_network_failure(setup_client, refresh):
    client, session, now = setup_client
    fr.search_novels(client=client)
    now[0] += timedelta(days=2)
    session.routes["/series"] = requests.Timeout("offline")
    with pytest.raises(fr.FenrirError, match="request failed"):
        fr.search_novels(refresh=refresh, client=client)


def test_cache_write_and_read_failure_preserves_live_success(setup_client, capsys):
    client, session, _ = setup_client
    client.cache_dir.write_text("not a directory")
    result = fr.search_novels(client=client)
    assert result["novels"] and result["requests"][0]["source"] == "live"
    assert "Warning:" in capsys.readouterr().err


def test_failed_atomic_write_preserves_previous_cache(setup_client, monkeypatch, capsys):
    client, _, _ = setup_client
    fr.search_novels(client=client)
    path = next(client.cache_dir.glob("*.json"))
    original = path.read_bytes()
    def fail(*args):
        raise OSError("no space")
    monkeypatch.setattr(fr.os, "replace", fail)
    assert fr.search_novels(refresh=True, client=client)["novels"]
    assert "no space" in capsys.readouterr().err
    assert path.read_bytes() == original
    assert list(client.cache_dir.iterdir()) == [path]


@pytest.mark.parametrize("data", [None, [], {"data": []}, {"data": {}, "meta": {}},
                                  {"data": [], "meta": {"current_page": True}}])
def test_bad_shapes_not_cached(setup_client, data):
    client, session, _ = setup_client
    session.routes["/series"] = data
    with pytest.raises(fr.FenrirError):
        fr.search_novels(client=client)
    assert not client.cache_dir.exists()


@pytest.mark.parametrize("status", [301, 403, 429, 503])
def test_http_errors(setup_client, status):
    client, session, _ = setup_client
    response = Response({})
    response.status_code = status
    session.routes["/series"] = response
    with pytest.raises(fr.FenrirError):
        fr.search_novels(client=client)


def test_invalid_json_and_detail_mismatch(setup_client, sample):
    client, session, _ = setup_client
    class InvalidJSON(Response):
        def json(self):
            raise ValueError("not JSON")
    session.routes["/series"] = InvalidJSON(None)
    with pytest.raises(fr.FenrirError, match="not JSON"):
        fr.search_novels(client=client)
    slug = sample["detail"]["slug"]
    session.routes[f"/series/{slug}"]["slug"] = "wrong"
    with pytest.raises(fr.FenrirError, match="does not match"):
        fr.get_novel(slug, client=client)


@pytest.mark.parametrize("argv", [["--detail", "foo", "--search", "x"], ["--taxonomies", "--genre", "Action"],
                                  ["--detail", "foo", "--page", "1"], ["--taxonomies", "--per-page", "20"],
                                  ["--page", "0"], ["--per-page", "101"], ["--detail", "https://foreign/x"]])
def test_cli_invalid_no_stdout(argv, capsys):
    assert fr.main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "Error:" in captured.err


def test_cli_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as error:
        fr.main(["--detail", "foo", "--taxonomies"])
    assert error.value.code != 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("argv,operation", [([], "browse"), (["--search", "magic", "--refresh"], "search"),
                                         (["--taxonomies"], "taxonomies"), (["--detail", "SLUG"], "detail")])
def test_cli_success(setup_client, sample, monkeypatch, capsys, argv, operation):
    client, _, _ = setup_client
    monkeypatch.setattr(fr, "Client", lambda: client)
    argv = [sample["detail"]["slug"] if arg == "SLUG" else arg for arg in argv]
    assert fr.main(argv) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["operation"] == operation
    assert not captured.err


def test_cli_live_error_is_stderr(setup_client, monkeypatch, capsys):
    client, session, _ = setup_client
    monkeypatch.setattr(fr, "Client", lambda: client)
    session.routes["/series"] = requests.ConnectionError("unreachable")
    assert fr.main([]) == 1
    captured = capsys.readouterr()
    assert not captured.out and "unreachable" in captured.err


def test_refresh_failure_never_uses_even_fresh_cache(setup_client):
    client, session, _ = setup_client
    fr.search_novels(client=client)
    session.routes["/series"] = requests.Timeout("offline")
    with pytest.raises(fr.FenrirError, match="offline"):
        fr.search_novels(refresh=True, client=client)
    assert fr.search_novels(client=client)["requests"][0]["source"] == "cache"


def test_normalized_filter_order_has_same_cache_key(setup_client, sample):
    client, session, _ = setup_client
    genres = [row["slug"] for row in sample["genres"]]
    fr.search_novels(genres=genres, client=client)
    fr.search_novels(genres=list(reversed(genres)), client=client)
    assert len(session.calls) == 2


@pytest.mark.parametrize("operation", [lambda c: fr.search_novels(refresh="yes", client=c),
                                       lambda c: fr.get_taxonomies(refresh=1, client=c),
                                       lambda c: fr.get_novel("novel", refresh=None, client=c)])
def test_invalid_refresh_before_requests(setup_client, operation):
    client, session, _ = setup_client
    with pytest.raises(fr.FenrirError, match="boolean"):
        operation(client)
    assert not session.calls


@pytest.mark.parametrize("parser,data", [(fr.parse_taxonomies, [None]),
                                         (fr.parse_taxonomies, [{"name": "Action"}]),
                                         (fr.parse_novel, {"slug": "novel"}),
                                         (lambda d: fr.parse_chapters(d, "novel"), {}),
                                         (lambda d: fr.parse_chapters(d, "novel"), [{"number": "../1"}])])
def test_additional_shape_errors(parser, data):
    with pytest.raises(fr.FenrirError):
        parser(data)
