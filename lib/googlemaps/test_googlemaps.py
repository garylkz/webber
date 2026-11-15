"""Offline tests for googlemaps_link: the redirect classifier and the host check."""

import pytest

import googlemaps_link as g


def test_a_place_redirect_is_kept():
    loc = "https://www.google.com/maps/place/Taiwanese+House/@3.1,101.6,17z/data=!4m6"
    assert g.classify(loc) == (loc, "place")


def test_a_place_id_query():
    loc = "https://maps.google.com?q=place_id:ChIJabc123&ftid=0x1:0x2"
    assert g.classify(loc) == ("https://www.google.com/maps/place/?q=place_id:ChIJabc123", "place_id")


def test_coordinates_drop_the_space():
    loc = "https://maps.google.com?q=3.1073,+101.6067&ftid=0x1"
    assert g.classify(loc) == ("https://www.google.com/maps/place/@3.1073,101.6067", "coordinates")


def test_a_text_query_becomes_a_place_search():
    loc = "https://www.google.com/maps/search/?q=Al+Safuan%2C+Petaling+Jaya"
    assert g.classify(loc) == ("https://www.google.com/maps/place/Al+Safuan,+Petaling+Jaya", "query")


def test_anything_else_is_the_redirect_as_it_came():
    loc = "https://www.google.com/maps/dir/a/b"
    assert g.classify(loc) == (loc, "redirect")
    assert g.classify("https://maps.google.com?q=") == ("https://maps.google.com?q=", "redirect")


def test_only_goo_gl_links_are_fetched():
    assert g.check_short_url("https://maps.app.goo.gl/AbC123") == "https://maps.app.goo.gl/AbC123"
    assert g.check_short_url("https://goo.gl/maps/AbC123")
    for bad in ("https://example.com/x", "file:///etc/passwd", "https://goo.gl/other", "ftp://maps.app.goo.gl/x"):
        with pytest.raises(SystemExit):
            g.check_short_url(bad)


def test_a_path_only_redirect_is_made_absolute(monkeypatch):
    class Resp:
        status_code = 302
        headers = {"Location": "/maps/place/X"}

    monkeypatch.setattr(g.requests, "head", lambda *a, **k: Resp())
    assert g.redirect_of("https://maps.app.goo.gl/x") == "https://maps.google.com/maps/place/X"
