"""Offline tests for the MCP server: argument checks, command lines, protocol.

Nothing here runs a scraper or touches the network. Argument and protocol tests
talk to the server through the SDK's in-memory client, with `run` replaced so
no script starts. Two tests start the real server process, over stdio and HTTP,
and only list its tools.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import anyio
import pytest
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

import mcp_server as m

SERVER = Path(m.__file__)
REAL_RUN = m.run  # the no_scripts fixture replaces m.run in every test


def argv(tool, **args):
    """The command line a tool would run, with the script path relative to lib/."""
    cmd = getattr(m, f"build_{tool}")(args)
    return [str(Path(cmd[1]).relative_to(m.LIB))] + cmd[2:]


def ask(coro_fn, server=None):
    """Run coro_fn(client) against an in-memory server and return its result."""
    async def main():
        async with Client(server or m.make_server()) as client:
            return await coro_fn(client)
    return anyio.run(main)


def call(tool, **args):
    return ask(lambda client: client.call_tool(tool, args))


def listed(server=None):
    return ask(lambda client: client.list_tools(), server).tools


@pytest.fixture(autouse=True)
def no_scripts(monkeypatch):
    """A call that gets past every check returns this instead of starting a script."""
    monkeypatch.setattr(m, "run", lambda cmd: json.dumps({"ran": cmd[1:]}))


def rejects(tool, **args):
    result = call(tool, **args)
    assert result.is_error, f"{tool} {args} was accepted: {result.content[0].text}"
    return result.content[0].text


# --- the tool list -----------------------------------------------------------

def test_the_tool_names_are_pinned():
    assert sorted(t.name for t in listed()) == sorted([
        "cinema_list", "cinema_showtimes", "cinema_seat_map", "gsc_films",
        "fourd_results", "northport_enquiry", "meetup_events", "goodreads_shelf",
        "youtube_rss", "hacker_news_feed", "hacker_news_comments", "rps_feed", "rps_article",
        "air_quality_report", "google_maps_link", "youtube_channel",
    ])


def test_every_tool_is_read_only_and_described():
    for tool in listed():
        assert tool.description and tool.annotations.read_only_hint is True
        schema = tool.input_schema
        for key in schema.get("required", ()):
            assert key in schema["properties"]
        for rule in schema["properties"].values():
            assert rule.get("description")


def test_every_script_a_tool_runs_exists():
    for tool in listed():
        schema = tool.input_schema
        sample = {}
        for key in schema.get("required", ()):
            rule = schema["properties"][key]
            sample[key] = rule["enum"][0] if "enum" in rule else {
                "user_id": "1", "item": "1", "time": "19:30",
                "url": "https://www.rockpapershotgun.com/x" if tool.name == "rps_article" else "u",
            }.get(key, "x")
        cmd = getattr(m, f"build_{tool.name}")(sample)
        assert Path(cmd[1]).is_file(), cmd[1]


# --- argument checks ---------------------------------------------------------

def test_missing_arguments_are_rejected():
    rejects("cinema_list", chain="gsc")
    rejects("cinema_seat_map", chain="gsc", cinema="235")


def test_types_enums_and_patterns_are_checked():
    rejects("cinema_list", chain="mbo", what="cinemas")
    rejects("cinema_showtimes", chain="gsc", date="14/10/2026")
    rejects("cinema_showtimes", chain="gsc", date="x2026-10-14y")
    rejects("hacker_news_feed", page="2")
    rejects("hacker_news_feed", page=True)
    rejects("hacker_news_feed", page=0)
    rejects("goodreads_shelf", user_id="abc")
    rejects("rps_article", url="https://example.com/x")


def test_a_positional_cannot_pass_as_an_option():
    rejects("cinema_showtimes", chain="gsc", cinema="--all")
    rejects("northport_enquiry", kind="container", value="-h")
    rejects("youtube_rss", url="--fetch")


def test_a_valid_call_reaches_the_script():
    result = call("hacker_news_feed", page=2)
    assert not result.is_error
    assert json.loads(result.content[0].text) == {"ran": [str(m.LIB / "hacker-news/hacker_news_feed.py"), "--page=2"]}


def test_option_values_use_the_equals_form():
    assert argv("meetup_events", query="--dump") == ["meetup/meetup_events.py", "--query=--dump"]


# --- command lines -----------------------------------------------------------

def test_cinema_list():
    assert argv("cinema_list", chain="gsc", what="cinemas", region="Klang Valley", city="Petaling Jaya") == [
        "gsc/gsc_cinema.py", "--cinemas", "--region=Klang Valley", "--city=Petaling Jaya"]
    assert argv("cinema_list", chain="tgv", what="regions") == ["tgv/tgv_cinema.py", "--regions"]
    rejects("cinema_list", chain="tgv", what="movies")
    rejects("cinema_list", chain="tgv", what="cinemas", city="Klang")
    rejects("cinema_list", chain="gsc", what="dates", region="Klang Valley")


def test_cinema_showtimes():
    assert argv("cinema_showtimes", chain="gsc", cinema="235", date="2026-10-14",
                prices=True, per_person=True, from_time="18:00") == [
        "gsc/gsc_cinema.py", "--from=18:00", "--prices", "--per-person", "235", "2026-10-14"]
    assert argv("cinema_showtimes", chain="tgv", movie="other-mommy", seats=True) == [
        "tgv/tgv_cinema.py", "--movie=other-mommy", "--seats"]
    assert argv("cinema_showtimes", chain="gsc", date="2026-10-14") == ["gsc/gsc_cinema.py", "2026-10-14"]
    rejects("cinema_showtimes", chain="gsc", per_person=True)
    rejects("cinema_showtimes", chain="gsc", region="Klang Valley")
    rejects("cinema_showtimes", chain="tgv", movie="x", region="Klang Valley")


def test_cinema_seat_map():
    assert argv("cinema_seat_map", chain="tgv", cinema="BU0", date="2026-10-14", time="19:30",
                raw_columns=True) == [
        "tgv/tgv_cinema.py", "--seat-map", "--time=19:30", "--raw-columns", "BU0", "2026-10-14"]
    rejects("cinema_seat_map", chain="gsc", cinema="235", time="19:30", raw_columns=True)


def test_gsc_films():
    assert argv("gsc_films") == ["gsc/gsc_cinema.py"]
    assert argv("gsc_films", listing="coming-soon") == ["gsc/gsc_cinema.py", "--listing=coming-soon"]
    assert argv("gsc_films", listing="all") == ["gsc/gsc_cinema.py", "--all"]
    assert argv("gsc_films", detail="digger") == ["gsc/gsc_cinema.py", "--detail=digger"]
    rejects("gsc_films", listing="all", detail="digger")


def test_fourd_results():
    assert argv("fourd_results") == ["fourd/fourd_results.py"]
    assert argv("fourd_results", date="2026-10-25", operator="magnum") == [
        "fourd/fourd_results.py", "--operator=magnum", "2026-10-25"]
    assert argv("fourd_results", from_date="2024-01-01", to_date="2024-12-31") == [
        "fourd/fourd_results.py", "--from=2024-01-01", "--to=2024-12-31"]
    rejects("fourd_results", date="2026-10-25", recent=3)
    rejects("fourd_results", to_date="2024-12-31")


def test_the_small_tools():
    assert argv("northport_enquiry", kind="bl", value="SOMEBL123") == [
        "northport/northport_enquiry.py", "bl", "SOMEBL123"]
    assert argv("meetup_events", lat=1.35, radius=10, happening_now=True) == [
        "meetup/meetup_events.py", "--lat=1.35", "--radius=10", "--happening-now"]
    assert argv("goodreads_shelf", user_id="1123497") == ["goodreads/goodreads_shelf.py", "1123497", "read"]
    assert argv("youtube_rss", url="https://youtube.com/@x", fetch=True) == [
        "youtube/youtube_rss.py", "--fetch", "https://youtube.com/@x"]
    assert argv("youtube_channel", name="科學漫聊") == ["youtube/youtube_channel.py", "科學漫聊"]
    rejects("youtube_channel", name="--help")
    assert argv("hacker_news_feed", page=2) == ["hacker-news/hacker_news_feed.py", "--page=2"]
    assert argv("hacker_news_comments", item="https://news.ycombinator.com/item?id=42") == [
        "hacker-news/hacker_news_comments.py", "https://news.ycombinator.com/item?id=42"]
    assert argv("rps_feed", start=2, count=5) == ["rps/rps_feed.py", "--start", "2", "--count", "5"]
    assert argv("google_maps_link", url="https://maps.app.goo.gl/AbC") == [
        "google-maps/google_maps_link.py", "https://maps.app.goo.gl/AbC"]
    rejects("google_maps_link", url="https://example.com/x")


def test_air_quality():
    assert argv("air_quality_report") == ["air-quality/air_quality_report.py"]
    assert argv("air_quality_report", place="Kuala Lumpur", hours=6) == [
        "air-quality/air_quality_report.py", "--place=Kuala Lumpur", "--hours=6"]
    assert argv("air_quality_report", lat=-3.5, lon=101.6) == [
        "air-quality/air_quality_report.py", "--lat=-3.5", "--lon=101.6"]
    rejects("air_quality_report", place="Kuala Lumpur", lat=3.1, lon=101.6)
    rejects("air_quality_report", place="Kuala Lumpur", address="Jalan Gasing, Petaling Jaya")
    rejects("air_quality_report", lat=3.1)
    rejects("air_quality_report", hours=49)
    rejects("air_quality_report", place="--note")


# --- results -----------------------------------------------------------------

def test_a_rule_between_arguments_is_a_json_tool_error():
    assert json.loads(rejects("fourd_results", to_date="2024-12-31")) == {"error": "to_date needs from_date"}


def test_a_failed_run_reports_stdout_json_or_stderr(monkeypatch):
    class Done:
        def __init__(self, code, out, err):
            self.returncode, self.stdout, self.stderr = code, out, err

    def text(result):
        return result.content[0].text

    monkeypatch.setattr(m, "run", REAL_RUN)
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: Done(1, '{"error": "feed down"}\n', ""))
    assert text(m.outcome(m.build_rps_feed, {})) == '{"error": "feed down"}'
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: Done(1, "", "Could not reach the site.\n"))
    result = m.outcome(m.build_rps_feed, {})
    assert result.is_error and json.loads(text(result)) == {"error": "Could not reach the site."}
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: Done(0, '{"ok": 1}\n', "progress\n"))
    result = m.outcome(m.build_rps_feed, {})
    assert not result.is_error and text(result) == '{"ok": 1}'


# --- protocol ----------------------------------------------------------------

def test_tools_can_be_limited_by_group():
    assert sorted(t.name for t in listed(m.make_server({"fourd", "rps"}))) == [
        "fourd_results", "rps_article", "rps_feed"]
    done = subprocess.run([sys.executable, str(SERVER), "--tools", "bogus"], input="", capture_output=True, text=True)
    assert done.returncode == 2 and "unknown group" in done.stderr


def test_the_real_server_over_stdio():
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER), "--tools", "rps"])
    names = ask(lambda client: client.list_tools(), params).tools
    assert sorted(t.name for t in names) == ["rps_article", "rps_feed"]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_the_real_server_over_http():
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(SERVER), "--http", "--port", str(port), "--tools", "fourd"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        names = ask(lambda client: client.list_tools(), f"http://127.0.0.1:{port}/mcp").tools
        assert [t.name for t in names] == ["fourd_results"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# --- ready-made Markdown for the feeds ---------------------------------------

HN = {"page": 1, "count": 2, "stories": [
    {"title": "One", "url": "https://a.example/1", "comments_url": "https://news.ycombinator.com/item?id=1"},
    {"title": "Two", "url": "https://a.example/2", "comments_url": "https://news.ycombinator.com/item?id=2"}]}
RPS = {"order": "newest", "start": 3, "next_start": 5, "total": 40, "articles": [
    {"title": "Three", "url": "https://www.rockpapershotgun.com/three",
     "comments_url": "https://www.rockpapershotgun.com/three?view=comments"},
    {"title": "Four", "url": "https://www.rockpapershotgun.com/four",
     "comments_url": "https://www.rockpapershotgun.com/four?view=comments"}]}


def test_the_feeds_render_both_links_on_their_own_lines():
    assert m.render_hacker_news_feed(HN).splitlines() == [
        "Hacker News, page 1:", "",
        "1. One", "   Article: https://a.example/1", "   Comments: https://news.ycombinator.com/item?id=1",
        "2. Two", "   Article: https://a.example/2", "   Comments: https://news.ycombinator.com/item?id=2"]
    lines = m.render_rps_feed(RPS).splitlines()
    assert lines[2:5] == ["3. Three", "   Article: https://www.rockpapershotgun.com/three",
                          "   Comments: https://www.rockpapershotgun.com/three?view=comments"]
    assert lines[-1] == "More: start=5"


def test_a_feed_call_returns_markdown_and_keeps_the_json(monkeypatch):
    monkeypatch.setattr(m, "run", lambda cmd: json.dumps(HN))
    result = call("hacker_news_feed")
    assert not result.is_error
    assert result.content[0].text == m.render_hacker_news_feed(HN)
    assert result.structured_content == HN


def test_an_unexpected_shape_falls_back_to_the_json(monkeypatch):
    monkeypatch.setattr(m, "run", lambda cmd: '{"odd": true}')
    result = call("rps_feed")
    assert not result.is_error and result.content[0].text == '{"odd": true}'


def test_a_channel_renders_its_handle_and_url_on_their_own_lines():
    long = "@Alexalexalexalexalexalexalex"
    found = {"status": "found", "name": "Alex Meyers", "channel": "@AlexMeyersVids",
             "subscribers": "3.92M subscribers", "url": "https://www.youtube.com/@AlexMeyersVids"}
    assert m.render_youtube_channel(found).splitlines() == [
        "Found: Alex Meyers", "   Handle: @AlexMeyersVids", "   Subscribers: 3.92M subscribers",
        "   URL: https://www.youtube.com/@AlexMeyersVids"]
    ambiguous = {"status": "ambiguous", "query": "alex", "candidates": [
        {"name": "alex", "channel": long, "subscribers": "1.18M subscribers", "url": "https://www.youtube.com/" + long}]}
    lines = m.render_youtube_channel(ambiguous).splitlines()
    assert lines[0].startswith('More than one channel matches "alex"')
    assert lines[2:] == ["1. alex", f"   Handle: {long}", "   Subscribers: 1.18M subscribers",
                         f"   URL: https://www.youtube.com/{long}"]
    assert m.render_youtube_channel({"status": "odd"}) is None
