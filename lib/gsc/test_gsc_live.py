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
    detail = gsc.detail(slug, delay=0)
    assert detail.slug == slug
    assert detail.title
    assert detail.synopsis
    assert detail.director


def test_movie_accepts_bare_slug(now_showing):
    slug = now_showing[0].slug
    assert gsc.detail(f"/movie/{slug}/", delay=0).slug == slug


def test_unknown_slug_exits():
    with pytest.raises(SystemExit):
        gsc.detail("this-is-not-a-real-movie-12345", delay=0)


@pytest.mark.parametrize("bad", ["../../etc/passwd", "", "   ", "a/b/c"])
def test_invalid_slug_exits(bad):
    with pytest.raises(SystemExit):
        gsc.detail(bad, delay=0)


def test_cinemas():
    outlets = gsc.cinemas(delay=0)
    assert len(outlets) > 20
    assert all(cinema.slug and cinema.name for cinema in outlets)


def test_cinemas_by_city():
    """A town matches either the service's own town or the website's label."""
    outlets = gsc.cinemas(delay=0)
    kl = gsc.by_city(outlets, "Kuala Lumpur")
    assert kl
    for cinema in kl:
        assert "Kuala Lumpur" in {cinema.town or "", cinema.city or ""}


def test_find_cinema_accepts_the_websites_name():
    """The service calls it 'Petaling Jaya - 1 Utama (New Wing)'."""
    outlets = gsc.cinemas(delay=0)
    assert gsc.find_cinema(outlets, "gsc 1 utama").slug == "gsc-1-utama"
    assert gsc.find_cinema(outlets, "1 Utama (New Wing)").slug == "gsc-1-utama"


# -- the booking service --------------------------------------------------


def test_catalogue():
    catalogue = gsc.movies(delay=0)
    assert len(catalogue) > 20
    assert all(f.id and f.title and f.slug for f in catalogue)
    assert all(f.dates for f in catalogue)


def test_business_dates_come_from_the_catalogue():
    dates = gsc.business_dates(delay=0)
    assert dates == sorted(dates)
    assert len(dates) > 3
    assert dates[0] <= gsc.normalise_date(None)


def test_regions_come_from_the_outlet_directory():
    regions = gsc.regions(delay=0)
    assert regions
    assert "klang valley" in regions
    assert regions["klang valley"] == "Klang Valley"


def test_cinemas_carry_ids_and_a_region():
    outlets = gsc.cinemas(delay=0)
    assert len(outlets) > 40
    assert all(c.id.isdigit() for c in outlets)
    assert all(c.region for c in outlets)
    # with_details=True also joins the website on, so slugs are filled in.
    assert all(c.slug for c in outlets)


def test_day_returns_films_and_sessions():
    outlet = gsc.find_cinema(gsc.cinemas(delay=0), "gsc 1 utama")
    films = gsc.day(outlet.id, "2026-10-14", delay=0)
    assert films
    assert all(f.title for f in films)
    showtimes = [s for f in films for s in f.showtimes]
    assert showtimes
    assert all(s.session_id and s.hall_id and s.start for s in showtimes)
    assert all(s.cinema_id == outlet.id for s in showtimes)


def test_day_defaults_to_today():
    """Today means today in Malaysia, whatever the machine's own timezone is.

    Not asserted to be non-empty: a national run rolls over around midnight,
    so an outlet queried in the first minutes of a new day legitimately has
    nothing on it yet. The explicit date is compared instead.
    """
    outlet = gsc.find_cinema(gsc.cinemas(delay=0), "gsc 1 utama")
    today = gsc.normalise_date(None)
    assert gsc.day(outlet.id, delay=0) == gsc.day(outlet.id, today, delay=0)
    assert gsc.day(outlet.id, "2026-10-14", delay=0)


def test_day_can_be_priced():
    outlet = gsc.find_cinema(gsc.cinemas(delay=0), "gsc 1 utama")
    films = gsc.day(outlet.id, "2026-10-14", prices=True, after="10:00", before="14:00", delay=0)
    priced = [s for f in films for s in f.showtimes if s.ticket_types]
    assert priced
    assert all(s.price is not None for s in priced)
    assert all(s.movie_id for s in priced)


def test_day_can_be_seated():
    outlet = gsc.find_cinema(gsc.cinemas(delay=0), "gsc 1 utama")
    films = gsc.day(outlet.id, "2026-10-14", seats=True, after="10:00", before="14:00", delay=0)
    seated = [s for f in films for s in f.showtimes if s.seats_left is not None]
    assert seated
    assert all(s.seats_left >= 0 for s in seated)


def test_seat_plan_draws_a_hall():
    outlet = gsc.find_cinema(gsc.cinemas(delay=0), "gsc 1 utama")
    films = gsc.day(outlet.id, "2026-10-14", after="10:00", before="14:00", delay=0)
    showtime = next(s for f in films for s in f.showtimes)
    plan = gsc.seat_plan(outlet.id, showtime, delay=0)
    assert plan.seats_total
    assert plan.seats
    drawn = gsc.render_seat_plan(plan)
    assert "SCREEN" in drawn
    assert "seats drawn" in drawn


def test_movie_searches_every_cinema_in_one_call():
    hits = gsc.movie("digger", "2026-10-14", delay=0)
    assert hits
    for cid, film in hits:
        assert cid
        assert film.showtimes
        assert film.title


def test_movie_accepts_a_bracketed_title():
    """One film exists under several titles differing only by a format prefix."""
    assert gsc.movie("(InfinityVision) Avengers:Endgame Encore", "2026-10-14", delay=0)


def test_movie_can_be_narrowed_to_one_cinema():
    outlet = gsc.find_cinema(gsc.cinemas(delay=0), "gsc 1 utama")
    hits = gsc.movie("digger", "2026-10-14", cinema_ids=[outlet.id], delay=0)
    assert all(cid == outlet.id for cid, _ in hits)


def test_movie_with_no_sessions_exits():
    with pytest.raises(SystemExit):
        gsc.movie("no-such-film-12345", "2026-10-14", delay=0)


def test_schedule_covers_the_country():
    data = gsc.schedule(on="2026-10-14", cinema_ids=["235", "356"], delay=0)
    assert set(data) <= {"235", "356"}
    assert any(films for films in data.values())


def test_cinema_experiences():
    formats = gsc.cinema_experiences("235", "2026-10-14", delay=0)
    assert formats
    assert all(code and label for code, label in formats.items())


def test_detail_is_backfilled_from_the_catalogue():
    """The website leaves an unpublished runtime as N/A; the service knows it."""
    record = gsc.detail("munafik-melawan-iblis", delay=0)
    assert record.synopsis
    assert record.director
    assert record.cast
    assert record.runtime


def test_loose_title_matches_a_bracketed_variant():
    assert gsc.loose_title("(InfinityVision) Avengers:Endgame Encore") == (
        gsc.loose_title("Avengers: Endgame Encore")
    )
