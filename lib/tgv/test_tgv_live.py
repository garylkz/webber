"""Live network tests for tgv_scraper.py.

Skipped by default. Run them with::

    pytest -m live

These hit api.tgv.com.my anonymously, the same way the website does. Keep the
call count low: the whole file makes about a dozen requests.
"""

from __future__ import annotations

import pytest

import tgv_scraper as tgv

pytestmark = pytest.mark.live

NET = {"delay": 0.0}


@pytest.fixture(scope="module")
def cinemas() -> list[tgv.Cinema]:
    return tgv.cinemas(**NET)


@pytest.fixture(scope="module")
def busy_date() -> str:
    """A date that is certainly trading, rather than whatever today is.

    A national run rolls over around midnight Malaysia time, so an outlet
    queried in the first minutes of a new day legitimately has nothing on it.
    Picking from the API's own list of business dates keeps these tests
    meaningful at any hour.
    """
    dates = tgv.business_dates(**NET)
    today = tgv.normalise_date(None)
    return today if today in dates else dates[0]


@pytest.fixture(scope="module")
def today(busy_date) -> list[tgv.Movie]:
    return tgv.day("BU0", busy_date, **NET)


def test_cinemas(cinemas):
    assert len(cinemas) > 30
    assert all(c.id and c.name for c in cinemas)
    assert any(c.id == "BU0" for c in cinemas)


def test_cinemas_carry_addresses(cinemas):
    utama = next(c for c in cinemas if c.id == "BU0")
    assert utama.address
    assert utama.region
    assert utama.experiences


def test_lightweight_cinema_list_matches_the_detailed_one(cinemas):
    lite = tgv.cinemas(with_details=False, **NET)
    assert len(lite) >= len(cinemas) - 2  # a couple of outlets may be listed but inactive
    assert {c.id for c in lite} >= {c.id for c in cinemas}


def test_regions():
    labels = tgv.regions(**NET)
    assert "klangvalley" in labels
    assert all(isinstance(v, str) for v in labels.values())


def test_business_dates_are_sorted_iso_days():
    dates = tgv.business_dates(**NET)
    assert dates
    assert dates == sorted(dates)
    assert all(tgv.normalise_date(d) == d for d in dates)


def test_day_returns_sessions(today):
    assert today
    assert all(m.title for m in today)
    assert all(m.showtimes for m in today)
    assert all(s.session_id for m in today for s in m.showtimes)


def test_day_respects_the_cinema(today):
    assert all(m.cinema_id == "BU0" for m in today)


def test_seat_status(today):
    ids = [s.session_id for m in today for s in m.showtimes][:10]
    seats = tgv.seat_status("BU0", ids, **NET)
    assert seats
    for row in seats.values():
        assert row["seats_total"] is not None
        assert row["seats_left"] == row["seats_total"] - row["seats_used"]


def test_day_with_seats(today, busy_date):
    movies = tgv.day("BU0", busy_date, seats=True, **NET)
    assert movies
    filled = [s for m in movies for s in m.showtimes if s.seats_total is not None]
    assert filled, "no session came back with seat counts"
    assert all(s.seats_left is not None for s in filled)


def test_schedule_spans_cinemas(busy_date):
    """The national run is one request per cinema, so keep it to a few."""
    data = tgv.schedule(cinema_ids=["BU0", "SW0", "GUR"], on=busy_date, **NET)
    assert set(data) == {"BU0", "SW0", "GUR"}
    assert any(data.values())


def test_movie_lookup(today, busy_date):
    film = today[0]
    found = tgv.movie(film.slug, on=busy_date, cinema_ids=["BU0", "SW0", "GUR"], **NET)
    assert found
    assert all(m.slug == film.slug for _, m in found)


def test_unknown_movie_exits():
    with pytest.raises(SystemExit):
        tgv.movie("this-is-not-a-real-movie-12345", cinema_ids=["BU0"], **NET)


def test_a_date_with_nothing_on_it_is_empty():
    assert tgv.day("SW0", "2020-01-01", **NET) == []