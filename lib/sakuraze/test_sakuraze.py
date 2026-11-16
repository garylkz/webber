"""Offline tests. Run with --basetemp=out/pytest-sakuraze from repo root."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
import requests

import sakuraze_novels as s

FIXTURES = Path(__file__).parent / "fixtures"
NOVEL_ID = "9aaa3eab-5377-4e50-a250-0ae6a0825711"
CHAPTER_ID = "f102c857-d38c-4431-af16-554b914ff8b7"
GENRE_ID = "2be5bd94-e46c-41fa-abd5-576430504a01"
NOW = datetime(2026, 11, 10, tzinfo=timezone.utc)
SLUG = "the-demonic-cult-knows-how-to-communicate"


def token(role="anon", ref=s.PROJECT):
    # Synthetic test token, not a saved frontend JWT or credential.
    payload = base64.urlsafe_b64encode(json.dumps({"role": role, "ref": ref}).encode()).decode().rstrip("=")
    return "test." + payload + ".signature"


def chapter(number=1):
    return {"id": CHAPTER_ID, "title": "", "chapter_number": number, "is_premium": number > 50,
            "coin_cost": 15 if number > 50 else 0, "novel_id": NOVEL_ID,
            "scheduled_free_at": None, "created_at": "2026-11-09T02:27:33.389729+00:00"}


def chapter_response(number=1):
    return {"success": True, "access_status": "preview" if number > 50 else "free",
            "is_full_content": number <= 50,
            "chapter": dict(chapter(number), content="<p>A public <b>plot</b> clue &amp; evidence.</p>")}


class Response:
    def __init__(self, data=None, *, text="", status=200, headers=None):
        self.data, self.text, self.status_code = data, text, status
        self.headers = headers or {}

    def json(self):
        return deepcopy(self.data)


class Session:
    def __init__(self, novels):
        self.novels = deepcopy(novels)
        self.calls = []
        self.override = {}
        self.fail = False
        self.genres = [{"id": GENRE_ID, "name": "Martial Arts"}]

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] == s.TIMEOUT
        assert kwargs["headers"]["User-Agent"] == s.HEADERS["User-Agent"]
        assert kwargs["headers"]["Accept-Language"] == "en-US,en;q=0.9"
        if self.fail:
            raise requests.ConnectionError("secret should never appear: " + token())
        if url in self.override:
            response = self.override[url]
            return response(method, kwargs) if callable(response) else response
        if url == s.BASE_URL + "/":
            assert "apikey" not in kwargs["headers"]
            return Response(text='<script type="module" src="/assets/index-test.js"></script>')
        if url == s.BASE_URL + "/assets/index-test.js":
            assert "Authorization" not in kwargs["headers"]
            return Response(text=f'const ms="{s.SUPABASE_URL}",us="{token()}";')
        assert url.startswith(s.API_URL)
        assert kwargs["headers"]["apikey"] == token()
        assert kwargs["headers"]["Authorization"] == "Bearer " + token()
        if url == s.API_URL + "novels":
            params = kwargs["params"]
            assert all(params[k] == v for k, v in s.SCOPE.items())
            if "slug" in params:
                return Response([r for r in self.novels if "eq." + r["slug"] == params["slug"]])
            return Response(self.novels, headers={"Content-Range": f"0-{len(self.novels)-1}/{len(self.novels)}"})
        if url == s.API_URL + "genres":
            return Response(self.genres)
        if url == s.API_URL + "rpc/get_chapter_list_for_reader":
            assert method == "POST" and kwargs["json"] == {"p_novel_id": NOVEL_ID}
            return Response([chapter(51), chapter(1)])
        if url == s.API_URL + "rpc/get_chapter_for_reader":
            assert method == "POST"
            body = kwargs["json"]
            assert set(body) == {"p_novel_id", "p_chapter_number", "p_preview_chars"}
            assert body["p_preview_chars"] == 1600
            return Response(chapter_response(body["p_chapter_number"]))
        pytest.fail("Unexpected endpoint")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Offline test attempted network")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


@pytest.fixture
def novels():
    return json.loads((FIXTURES / "novels.json").read_text())


@pytest.fixture
def env(tmp_path, novels):
    assert tmp_path.resolve().is_relative_to(s.REPO_ROOT), "Use --basetemp=out/pytest-sakuraze"
    session = Session(novels)
    clock = [NOW]
    client = s.Client(cache_dir=tmp_path / "cache", session=session, clock=lambda: clock[0])
    return client, session, clock


def test_real_fixture_and_cleaning(novels):
    parsed = s._novels(novels)
    assert parsed[0]["id"] == NOVEL_ID
    assert parsed[0]["url"] == s.BASE_URL + "/novel/" + SLUG
    assert "author" not in parsed[0] and "cover" not in parsed[0]
    assert s.clean_text('<p>First &amp; second</p><script>bad</script><style>bad</style><p>Next<br>line</p>') == "First & second\nNext\nline"


def test_snapshot_search_filters_pagination_cache(env):
    client, session, _ = env
    for index, row in enumerate(session.novels):
        row["novel_genres"] = [{"genres": {"name": "Martial Arts" if index == 0 else "Fantasy"}}]
        row["novel_tags"] = [{"tags": {"name": name}} for name in (["Clever", "Clue"] if index == 0 else ["Clue"])]
    result = s.search_novels("DEMONIC", genres=[GENRE_ID], tags=["clever", "clue"], client=client)
    assert len(result["novels"]) == 1
    assert result["coverage"]["source_count"] == 2
    assert result["coverage"]["partial"] is False
    assert all(p["source"] == "live" for p in result["requests"])
    count = len(session.calls)
    session.fail = True
    for query in ("fish", "nothing matches", "DEMONIC"):
        s.search_novels(query, client=client)
    result = s.search_novels(page=2, per_page=1, client=client)
    assert len(result["novels"]) == 1
    assert result["requests"][0]["source"] == "cache"
    assert s.search_novels(page=100, client=client)["novels"] == []
    assert len(session.calls) == count
    assert token() not in json.dumps(result)


@pytest.mark.parametrize("field,value", [("title", "Needle"), ("alternative_title", "Needle"),
                                        ("description", "<p>Needle</p>"), ("original_author", "Needle")])
def test_search_fields(env, field, value):
    client, session, _ = env
    session.novels[0][field] = value
    assert len(s.search_novels("nEeDlE", client=client)["novels"]) == 1


def test_taxonomies_and_unknown_ambiguous(env):
    client, session, _ = env
    result = s.get_taxonomies(client=client)
    assert result["genres"] == session.genres
    assert result["tags"] and all(t["id"] is None for t in result["tags"])
    with pytest.raises(s.SakurazeError, match="Unknown"):
        s.search_novels(genres=["not-a-genre"], client=client)
    with pytest.raises(s.SakurazeError, match="Unknown"):
        s.search_novels(tags=["not-a-tag"], client=client)
    session.genres.append({"id": CHAPTER_ID, "name": "MARTIAL ARTS"})
    with pytest.raises(s.SakurazeError, match="Ambiguous"):
        s.search_novels(genres=["Martial Arts"], refresh=True, client=client)


def test_detail_chapter_free_preview_and_no_network_reuse(env):
    client, session, _ = env
    detail = s.get_novel(SLUG, client=client)
    assert [c["chapter_number"] for c in detail["chapters"]] == [1, 51]
    assert detail["chapters"][0]["title"] == ""
    for number in (1, 51):
        result = s.get_chapter(SLUG, number, client=client)
        c = result["chapter"]
        assert c["is_full_content"] is (number == 1)
        assert c["access_status"] == ("free" if number == 1 else "preview")
        assert c["text"] == "A public plot clue & evidence."
        assert c["url"] == f"{s.BASE_URL}/novel/{SLUG}/chapter/{number}"
        assert "content_html" in c
        assert token() not in json.dumps(result)
    count = len(session.calls)
    session.fail = True
    s.get_chapter(SLUG, 1, client=client)
    s.get_chapter(SLUG, 51, client=client)
    s.get_novel(SLUG, client=client)
    assert len(session.calls) == count


def test_locked_blocks_text_rpc(env):
    client, session, _ = env
    session.novels[0]["is_locked"] = True
    with pytest.raises(s.SakurazeError, match="locked"):
        s.get_chapter(SLUG, 1, client=client)
    assert not any(url.endswith("rpc/get_chapter_for_reader") for _, url, _ in session.calls)
    assert s.get_novel(SLUG, client=client)["novel"]["is_locked"] is True


@pytest.mark.parametrize("change", [
    {"success": False, "message": "not available"}, {"success": 1},
    {"access_status": "paid"}, {"access_status": "preview", "is_full_content": True},
    {"is_full_content": "true"}, {"access_status": "free", "is_full_content": False},
])
def test_unsupported_chapter_flags_never_cached(env, change):
    client, session, _ = env
    response = chapter_response()
    response.update(change)
    session.override[s.API_URL + "rpc/get_chapter_for_reader"] = Response(response)
    with pytest.raises(s.SakurazeError, match="unavailable|unsupported"):
        s.get_chapter(SLUG, 1, client=client)
    identity = client._identity("POST", s.API_URL + "rpc/get_chapter_for_reader", None,
                                {"p_novel_id": NOVEL_ID, "p_chapter_number": 1, "p_preview_chars": 1600})
    assert not client._path(identity).exists()


@pytest.mark.parametrize("number", [1, 51])
@pytest.mark.parametrize("html", ["<p><br></p>", "<script>not prose</script>"])
def test_markup_only_chapters_rejected_and_not_cached(env, number, html):
    client, session, _ = env
    response = chapter_response(number)
    response["chapter"]["content"] = html
    session.override[s.API_URL + "rpc/get_chapter_for_reader"] = Response(response)
    with pytest.raises(s.SakurazeError, match="no readable public text"):
        s.get_chapter(SLUG, number, client=client)
    identity = client._identity("POST", s.API_URL + "rpc/get_chapter_for_reader", None,
                               {"p_novel_id": NOVEL_ID, "p_chapter_number": number, "p_preview_chars": 1600})
    assert not client._path(identity).exists()


@pytest.mark.parametrize("number", [1, 51])
def test_markup_only_cached_chapter_is_revalidated(env, number):
    client, session, _ = env
    s.get_chapter(SLUG, number, client=client)
    identity = client._identity("POST", s.API_URL + "rpc/get_chapter_for_reader", None,
                               {"p_novel_id": NOVEL_ID, "p_chapter_number": number, "p_preview_chars": 1600})
    path = client._path(identity)
    entry = json.loads(path.read_text())
    entry["data"]["chapter"]["content"] = "<p><br></p>"
    path.write_text(json.dumps(entry))
    session.fail = True
    with pytest.raises(s.SakurazeError, match="request failed"):
        s.get_chapter(SLUG, number, client=client)
    session.fail = False
    result = s.get_chapter(SLUG, number, client=client)
    assert result["requests"][-1]["source"] == "live"
    assert result["chapter"]["text"]


@pytest.mark.parametrize("field,value", [("chapter_number", 2), ("novel_id", CHAPTER_ID),
                                        ("id", "bad"), ("content", None), ("is_premium", "false")])
def test_chapter_response_identity_and_types(env, field, value):
    client, session, _ = env
    response = chapter_response()
    response["chapter"][field] = value
    session.override[s.API_URL + "rpc/get_chapter_for_reader"] = Response(response)
    with pytest.raises(s.SakurazeError):
        s.get_chapter(SLUG, 1, client=client)


@pytest.mark.parametrize("field,value", [("is_visible", False), ("is_nsfw", True),
                                        ("approval_status", "pending"), ("is_visible", 1),
                                        ("id", "bad"), ("slug", "../unsafe"), ("is_locked", None)])
def test_public_scope_fail_closed(env, field, value):
    client, session, _ = env
    session.novels[0][field] = value
    with pytest.raises(s.SakurazeError):
        s.search_novels(client=client)
    assert not client._path(client._identity("GET", s.API_URL + "novels", s.SNAPSHOT_PARAMS)).exists()


def test_missing_or_wrong_detail(env):
    client, session, _ = env
    with pytest.raises(s.SakurazeError, match="not found"):
        s.get_novel("missing", client=client)
    session.override[s.API_URL + "novels"] = Response(session.novels)
    with pytest.raises(s.SakurazeError, match="not found"):
        s.get_novel(SLUG, client=client)


@pytest.mark.parametrize("age", [86400, 86401, -1])
def test_expired_future_cache_no_stale_fallback(env, age):
    client, session, clock = env
    s.search_novels(client=client)
    clock[0] = NOW + timedelta(seconds=age)
    session.fail = True
    with pytest.raises(s.SakurazeError, match="request failed"):
        s.search_novels(client=client)


@pytest.mark.parametrize("corrupt", ["json", "identity", "data", "date", "headers"])
def test_corrupt_cache_is_ignored(env, corrupt):
    client, session, _ = env
    s.search_novels(client=client)
    path = client._path(client._identity("GET", s.API_URL + "novels", s.SNAPSHOT_PARAMS))
    entry = json.loads(path.read_text())
    if corrupt == "json":
        path.write_text("{")
    else:
        key, value = {"identity": ("identity", {}), "data": ("data", {}),
                      "date": ("fetched_at", "invalid"), "headers": ("headers", [])}[corrupt]
        entry[key] = value
        path.write_text(json.dumps(entry))
    before = len(session.calls)
    result = s.search_novels(client=client)
    assert len(session.calls) == before + 1  # cached config, no bootstrap
    assert result["requests"][0]["source"] == "live"


def test_refresh_and_config_cache(env):
    client, session, _ = env
    s.search_novels(client=client)
    before = len(session.calls)
    s.get_chapter(SLUG, 1, client=client)
    assert len(session.calls) == before + 2  # new endpoints, config already local
    before = len(session.calls)
    s.search_novels(refresh=True, client=client)
    assert len(session.calls) == before + 3


def test_cache_write_failure_keeps_live(env, monkeypatch, capsys):
    client, _, _ = env
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(s.os, "replace", fail)
    assert len(s.search_novels(client=client)["novels"]) == 2
    assert "Warning" in capsys.readouterr().err
    assert not list(client.cache_dir.glob("*.tmp"))


@pytest.mark.parametrize("range_header,partial,total", [("0-1/999", True, 999), ("0-1/*", True, None),
                                                       ("", True, None), ("0-1/2", False, 2)])
def test_honest_coverage(novels, range_header, partial, total):
    _, coverage = s._snapshot(novels, {"content-range": range_header})
    assert coverage["partial"] is partial and coverage["source_count"] == total


@pytest.mark.parametrize("header", ["1-2/3", "0-5/6", "0-1/1"])
def test_mismatched_content_range(novels, header):
    with pytest.raises(s.SakurazeError):
        s._snapshot(novels, {"content-range": header})


@pytest.mark.parametrize("root", [
    '<script src="https://evil.example/assets/index.js"></script>',
    '<script src="//evil.example/assets/index.js"></script>',
    '<script src="/elsewhere/index.js"></script>',
    '<script src="/assets/../index.js"></script>',
    '<script src="/assets/one.js?secret=1"></script>',
    '<script src="/assets/one.js"></script><script src="/assets/two.js"></script>',
])
def test_unsafe_bootstrap_scripts(env, root):
    client, session, _ = env
    session.override[s.BASE_URL + "/"] = Response(text=root)
    with pytest.raises(s.SakurazeError, match="asset"):
        s.search_novels(client=client)
    assert len(session.calls) == 1


@pytest.mark.parametrize("key", [token("service_role"), token(ref="wrong"), "not-a-token", "x.W10.x"])
def test_invalid_keys_fail_closed(env, key, capsys):
    client, session, _ = env
    session.override[s.BASE_URL + "/assets/index-test.js"] = Response(text=f'ms="{s.SUPABASE_URL}";us="{key}"')
    with pytest.raises(s.SakurazeError) as exc:
        s.search_novels(client=client)
    assert key not in str(exc.value)
    assert key not in capsys.readouterr().err
    assert not any(url.startswith(s.API_URL) for _, url, _ in session.calls)


@pytest.mark.parametrize("status", [301, 302, 401, 404, 500])
def test_no_redirects_or_error_body_leaks(env, status):
    client, session, _ = env
    session.override[s.BASE_URL + "/"] = Response(text=token(), status=status)
    with pytest.raises(s.SakurazeError, match=f"HTTP {status}") as exc:
        s.search_novels(client=client)
    assert token() not in str(exc.value)
    assert len(session.calls) == 1


@pytest.mark.parametrize("method,endpoint,params,body", [
    ("POST", "rpc/unlock_chapter", None, {}), ("PATCH", "novels", {}, {}),
    ("GET", "https://evil.example/", {}, None), ("GET", "novels", {}, None),
    ("POST", "rpc/get_chapter_for_reader", None,
     {"p_novel_id": NOVEL_ID, "p_chapter_number": 1, "p_preview_chars": 9999}),
    ("POST", "rpc/get_chapter_for_reader", None,
     {"p_novel_id": NOVEL_ID, "p_chapter_number": 1, "p_preview_chars": 1600, "bypass": True}),
])
def test_allowlist_before_any_network(env, method, endpoint, params, body):
    client, session, _ = env
    with pytest.raises(s.SakurazeError):
        client._read(method, endpoint, params, body, lambda d, h: d)
    assert session.calls == []


@pytest.mark.parametrize("kwargs", [{"page": 0}, {"page": True}, {"page": 1.5}, {"per_page": 101},
                                    {"per_page": 0}, {"search": ""}, {"genres": "x"}, {"tags": [1]},
                                    {"refresh": 1}])
def test_invalid_search_before_network(env, kwargs):
    client, session, _ = env
    with pytest.raises(s.SakurazeError):
        s.search_novels(client=client, **kwargs)
    assert not session.calls


@pytest.mark.parametrize("slug,number", [("../escape", 1), ("safe", 0), ("safe", True), ("safe", "1"), ("safe", 1.5)])
def test_invalid_chapter_before_network(env, slug, number):
    client, session, _ = env
    with pytest.raises(s.SakurazeError):
        s.get_chapter(slug, number, client=client)
    assert not session.calls


def test_cache_paths_and_version(env):
    client, _, _ = env
    with pytest.raises(s.SakurazeError):
        s.Client(cache_dir=s.REPO_ROOT.parent)
    first = client._identity("GET", s.API_URL + "novels", {"x": 1})
    second = dict(first, method="POST")
    assert client._path(first) != client._path(second)
    assert client._path(first) != client._path(dict(first, version=99))
    assert client._path(first) != client._path(dict(first, body={"x": 1}))
    assert client._path(first) == client._path(dict(reversed(list(first.items()))))


@pytest.mark.parametrize("args", [
    ["--detail", SLUG, "--search", "x"], ["--taxonomies", "--page", "1"],
    ["--chapter", "1", "--novel", SLUG, "--tag", "x"], ["--chapter", "1"],
    ["--novel", SLUG], ["--chapter", "0", "--novel", SLUG], ["--page", "0"],
    ["--per-page", "101"], ["--detail", "../bad"],
])
def test_cli_invalid(args, capsys):
    assert s.main(args) == 1
    output = capsys.readouterr()
    assert output.out == "" and "Error:" in output.err


def test_cli_exclusive(capsys):
    with pytest.raises(SystemExit) as exc:
        s.main(["--detail", SLUG, "--taxonomies"])
    assert exc.value.code == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("args,operation", [([], "browse"), (["--search", "demonic"], "search"),
                                         (["--detail", SLUG], "detail"), (["--taxonomies"], "taxonomies"),
                                         (["--chapter", "51", "--novel", SLUG], "chapter")])
def test_cli_json_modes(env, monkeypatch, capsys, args, operation):
    client, _, _ = env
    monkeypatch.setattr(s, "Client", lambda: client)
    assert s.main(args) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["operation"] == operation
    assert not output.err


def test_multi_genre_any_tag_all(env):
    client, session, _ = env
    session.genres = [{"id": GENRE_ID, "name": "Martial Arts"}, {"id": CHAPTER_ID, "name": "Fantasy"}]
    for index, row in enumerate(session.novels):
        row["novel_genres"] = [{"genres": {"name": "Martial Arts" if index == 0 else "Fantasy"}}]
        row["novel_tags"] = [{"tags": {"name": name}} for name in (["One", "Two"] if index == 0 else ["One"])]
    assert len(s.search_novels(genres=[GENRE_ID, CHAPTER_ID], client=client)["novels"]) == 2
    assert len(s.search_novels(tags=["One", "Two"], client=client)["novels"]) == 1


def test_refresh_failure_does_not_return_cached_result(env):
    client, session, _ = env
    s.search_novels(client=client)
    session.fail = True
    with pytest.raises(s.SakurazeError) as exc:
        s.search_novels(refresh=True, client=client)
    assert token() not in str(exc.value)
    assert "secret" not in str(exc.value)


def test_config_wrong_project_url(env):
    client, session, _ = env
    session.override[s.BASE_URL + "/assets/index-test.js"] = Response(text=f'ms="https://evil.example";us="{token()}"')
    with pytest.raises(s.SakurazeError, match="configuration"):
        s.search_novels(client=client)
    assert len(session.calls) == 2


def test_cache_semantic_corruption_is_revalidated(env):
    client, session, _ = env
    s.search_novels(client=client)
    path = client._path(client._identity("GET", s.API_URL + "novels", s.SNAPSHOT_PARAMS))
    entry = json.loads(path.read_text())
    entry["data"][0]["is_nsfw"] = True
    path.write_text(json.dumps(entry))
    result = s.search_novels(client=client)
    assert result["requests"][0]["source"] == "live"
    assert len(result["novels"]) == 2


def test_session_removes_account_state(env):
    client, _, _ = env
    session = requests.Session()
    session.headers["Authorization"] = "personal"
    session.params["token"] = "personal"
    session.auth = ("personal", "password")
    session.cookies.set("session", "personal")
    clean = s.Client(cache_dir=client.cache_dir, session=session)
    assert clean.session.auth is None
    assert clean.session.trust_env is False
    assert not clean.session.cookies and not clean.session.headers and not clean.session.params


def test_snapshot_cap_and_malformed_range(novels):
    with pytest.raises(s.SakurazeError, match="Malformed"):
        s._snapshot(novels, {"content-range": "invalid"})
    with pytest.raises(s.SakurazeError):
        s._snapshot(novels, {"content-range": None})
    rows = []
    for index in range(501):
        row = deepcopy(novels[0])
        row["id"] = f"00000000-0000-0000-0000-{index:012x}"
        row["slug"] = f"novel-{index}"
        rows.append(row)
    with pytest.raises(s.SakurazeError, match="cap"):
        s._snapshot(rows, {"content-range": "0-500/501"})
