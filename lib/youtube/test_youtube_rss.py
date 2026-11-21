"""Offline tests for youtube_rss: resolving a @handle URL to its own channel id."""

import youtube_rss as r

OWNER = "UCHnyfMqiRRG1u-2MsSQLbXA"
OTHER = "UC1Umuj" + "a" * 17
URL = "https://www.youtube.com/@veritasium"


def resolve(monkeypatch, html):
    monkeypatch.setattr(r, "fetch_page", lambda url: html)
    return r.channel_id_from_url(URL)


def test_the_canonical_link_beats_a_recommended_channel_id(monkeypatch):
    html = f'"channelId":"{OTHER}" <link rel="canonical" href="https://www.youtube.com/channel/{OWNER}">'
    assert resolve(monkeypatch, html) == OWNER


def test_the_external_id_beats_a_recommended_channel_id(monkeypatch):
    html = f'"channelId":"{OTHER}" "externalId":"{OWNER}"'
    assert resolve(monkeypatch, html) == OWNER


def test_channel_id_is_the_last_resort(monkeypatch):
    assert resolve(monkeypatch, f'"channelId":"{OWNER}"') == OWNER


def test_a_page_with_no_id_resolves_to_none(monkeypatch):
    assert resolve(monkeypatch, "<html></html>") is None
