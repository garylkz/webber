"""Live network tests for gsc_scraper.py.

Skipped by default. Run them with::

    pytest -m live
"""

from __future__ import annotations

import pytest

import gsc_scraper as gsc

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def now_showing() -> list:
    return gsc.listing("now-showing", delay=0)


def test_now_showing(now_showing):
    assert len(now_showing) > 5
    assert all(movie.slug and movie.title for movie in now_showing)
    assert any(movie.booking_id for movie in now_showing)


@pytest.mark.parametrize("name", ["coming-soon", "advance-sales"])
def test_other_listings(name):
    assert len(gsc.listing(name, delay=0)) > 5


def test_unknown_listing_name():
    with pytest.raises(SystemExit):
        gsc.listing("not-a-listing", delay=0)


def test_movie_detail(now_showing):
    slug = now_showing[0].slug
    detail = gsc.movie(slug, delay=0)
    assert detail.slug == slug
    assert detail.title
    assert detail.synopsis


def test_movie_accepts_bare_slug(now_showing):
    slug = now_showing[0].slug
    assert gsc.movie(f"/movie/{slug}/", delay=0).slug == slug


def test_unknown_slug_exits():
    with pytest.raises(SystemExit):
        gsc.movie("this-is-not-a-real-movie-12345", delay=0)


@pytest.mark.parametrize("bad", ["../../etc/passwd", "", "   ", "a/b/c"])
def test_invalid_slug_exits(bad):
    with pytest.raises(SystemExit):
        gsc.movie(bad, delay=0)


def test_cinemas():
    outlets = gsc.cinemas(delay=0)
    assert len(outlets) > 20
    assert all(cinema.slug and cinema.name for cinema in outlets)


def test_cinemas_by_city():
    outlets = gsc.cinemas(delay=0)
    kl = gsc.by_city(outlets, "Kuala Lumpur")
    assert kl
    assert all(cinema.city == "Kuala Lumpur" for cinema in kl)
