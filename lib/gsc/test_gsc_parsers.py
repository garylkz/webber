"""Offline tests for gsc_scraper.py - run against saved copies of the live HTML."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gsc_scraper import (
    Cinema,
    Movie,
    by_city,
    clean,
    duration_minutes,
    parse_cinemas_page,
    parse_detail,
    parse_listing,
    split_codes,
)

FIXTURES = Path(__file__).parent / "fixtures"
PLACEHOLDERS = {"N/A", "NA", "-", "TBC", "TBA"}


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def now_showing() -> list[Movie]:
    return parse_listing(load("now_showing.html"))


@pytest.fixture(scope="module")
def coming_soon() -> list[Movie]:
    return parse_listing(load("coming_soon.html"))


@pytest.fixture(scope="module")
def advance_sales() -> list[Movie]:
    return parse_listing(load("advance_sales.html"))


@pytest.fixture(scope="module")
def detail() -> Movie:
    return parse_detail(load("movie_detail.html"), "munafik-melawan-iblis")


@pytest.fixture(scope="module")
def outlets() -> list[Cinema]:
    return parse_cinemas_page(load("cinemas.html"))


# -- listings ---------------------------------------------------------


@pytest.mark.parametrize("listing", ["now_showing", "coming_soon", "advance_sales"])
def test_listings_are_not_empty(listing, request):
    movies = request.getfixturevalue(listing)
    assert len(movies) > 10
    assert all(movie.title for movie in movies)


@pytest.mark.parametrize("listing", ["now_showing", "coming_soon", "advance_sales"])
def test_slugs_are_unique(listing, request):
    slugs = [movie.slug for movie in request.getfixturevalue(listing)]
    assert len(slugs) == len(set(slugs))


def test_now_showing_card_is_fully_parsed(now_showing):
    movie = next(m for m in now_showing if m.slug == "munafik-melawan-iblis")
    assert movie.title == "Munafik: Melawan Iblis"
    assert movie.genre == ("Horror",)
    assert movie.duration == "1 hr 45 mins"
    assert movie.runtime == 105
    assert movie.language == "BM"
    assert movie.subtitles == ("ENG",)
    assert movie.rating == "16"
    assert movie.poster_url.startswith("https://poster.gsc.com.my/")
    assert movie.booking_id == 6435
    assert movie.booking_url.endswith("showtime-by-movies?id=6435")


def test_url_is_absolute(now_showing):
    movie = now_showing[0]
    assert movie.url == f"https://www.gsc.com.my/movie/{movie.slug}/"


def test_every_slug_matches_its_url(now_showing):
    for movie in now_showing:
        assert movie.url.endswith(f"/{movie.slug}/")


def test_coming_soon_movies_have_no_booking_link(coming_soon):
    assert all(movie.booking_url is None for movie in coming_soon)
    assert all(movie.booking_id is None for movie in coming_soon)


def test_multi_valued_language_is_split(now_showing):
    """GSC sometimes joins several audio codes with commas."""
    movie = next(m for m in now_showing if m.slug == "terbang")
    assert movie.language == "BM,CAN,ENG,PUN"  # raw string preserved
    assert movie.languages == ("BM", "CAN", "ENG", "PUN")


def test_single_language_becomes_one_element_tuple(now_showing):
    movie = next(m for m in now_showing if m.slug == "munafik-melawan-iblis")
    assert movie.language == "BM"
    assert movie.languages == ("BM",)


@pytest.mark.parametrize("listing", ["now_showing", "coming_soon", "advance_sales"])
def test_languages_always_a_tuple(listing, request):
    for movie in request.getfixturevalue(listing):
        assert isinstance(movie.languages, tuple)
        assert all("," not in code for code in movie.languages)
        if movie.language:
            assert ",".join(movie.languages) == movie.language


def test_multi_valued_subtitles_are_split(now_showing):
    movie = next(m for m in now_showing if m.slug == "verity")
    assert movie.subtitles == ("BM", "CHI")


def test_placeholder_subtitles_become_empty_tuple(now_showing):
    """A chip reading "N/A" must not leak through as a one-element tuple."""
    movie = next(
        m
        for m in now_showing
        if m.slug == "project-sekai-6th-anniversary-thanks-festival-live"
    )
    assert movie.subtitles == ()
    assert "N/A" not in movie.subtitles


@pytest.mark.parametrize("listing", ["now_showing", "coming_soon", "advance_sales"])
def test_no_placeholder_strings_survive(listing, request):
    """No field on any listing should contain GSC's placeholder text."""
    for movie in request.getfixturevalue(listing):
        for name in ("duration", "language", "rating"):
            value = getattr(movie, name)
            assert value is None or value.upper() not in PLACEHOLDERS, (movie.slug, name, value)
        for value in movie.genre:
            assert value.upper() not in PLACEHOLDERS, (movie.slug, value)
        for code in movie.subtitles:
            assert code.upper() not in PLACEHOLDERS, (movie.slug, code)


def test_advance_sales_has_ratings(advance_sales):
    movie = next(m for m in advance_sales if m.slug == "jff-2026-tokyo-taxi")
    assert movie.rating == "16"
    assert movie.booking_id == 6343


def test_parse_listing_tolerates_foreign_html():
    """No cards means an empty list, not a crash."""
    assert parse_listing("<html><body><p>nope</p></body></html>") == []


# -- cinemas ----------------------------------------------------------


def test_cinemas_are_not_empty(outlets):
    assert len(outlets) > 20
    assert all(cinema.name and cinema.slug for cinema in outlets)


def test_cinema_slugs_are_unique(outlets):
    slugs = [cinema.slug for cinema in outlets]
    assert len(slugs) == len(set(slugs))


def test_cinema_card_is_fully_parsed(outlets):
    cinema = next(c for c in outlets if c.slug == "gsc-1-utama")
    assert cinema.name == "GSC 1 Utama"
    assert cinema.city == "Kuala Lumpur"
    assert cinema.features == (
        "4DX",
        "D-Box",
        "ScreenX",
        "Dolby Atmos",
        "BIG",
        "Play",
        "Prestige",
        "INFINITY VISION",
    )


def test_cinema_url_is_absolute(outlets):
    cinema = next(c for c in outlets if c.slug == "gsc-ioi-city-mall-west")
    assert cinema.url == "https://www.gsc.com.my/cinema/gsc-ioi-city-mall-west/"


def test_cinema_without_features_gets_empty_tuple(outlets):
    """The amenities row is missing on plain outlets; that must not become ("-",)."""
    cinema = next(c for c in outlets if c.slug == "gsc-1borneo")
    assert cinema.features == ()
    assert cinema.city == "Kota Kinabalu"


def test_cinema_features_are_always_tuples_without_commas(outlets):
    for cinema in outlets:
        assert isinstance(cinema.features, tuple)
        assert all("," not in feature for feature in cinema.features)


def test_non_gsc_brands_are_included(outlets):
    """The page mixes GSC with Aurum Theatre and Velvet, and all are cinemas."""
    names = {c.name for c in outlets}
    assert any(name.startswith("Aurum Theatre") for name in names)
    assert "Velvet Cinemas" in names


def test_by_city_filters_case_insensitively(outlets):
    kl = by_city(outlets, "kuala lumpur")
    assert len(kl) > 5
    assert {c.city for c in kl} == {"Kuala Lumpur"}
    assert by_city(outlets, "  KUALA LUMPUR ") == kl


def test_by_city_ignores_surrounding_whitespace(outlets):
    assert by_city(outlets, " Putrajaya ") == by_city(outlets, "Putrajaya")


def test_by_city_unknown_town_is_empty(outlets):
    assert by_city(outlets, "atlantis") == []


def test_cinema_to_dict_is_json_friendly(outlets):
    data = outlets[0].to_dict()
    assert set(data) == {
        "id", "name", "slug", "url", "city", "location", "region", "state", "address",
        "postcode", "latitude", "longitude", "telephone", "parking", "opening_hours",
        "features", "experiences", "description",
    }
    assert isinstance(data["features"], list)
    json.dumps(data)
    json.dumps([c.to_dict() for c in outlets])


def test_parse_cinemas_page_tolerates_foreign_html():
    assert parse_cinemas_page("<html><body><p>nope</p></body></html>") == []


# -- details ----------------------------------------------------------


def test_detail_core_fields(detail):
    assert detail.title == "Munafik: Melawan Iblis"
    assert detail.slug == "munafik-melawan-iblis"
    assert detail.genre == ("Horror",)
    assert detail.language == "BM"
    assert detail.subtitles == ("ENG",)
    assert detail.rating == "16"
    assert detail.release_date == "01 October 2026"


def test_detail_synopsis_and_credits(detail):
    assert detail.synopsis.startswith("On his way home after helping a family")
    assert detail.director == "Guntur Soeharjanto"
    assert detail.main_stars == ("Arya Saloka", "Acha Septriasa", "Dimas Aditya")


def test_detail_trailer(detail):
    assert detail.trailer_url == "https://www.youtube.com/embed/cLvy25yo0Fg"
    assert detail.trailer_id == "cLvy25yo0Fg"


def test_detail_booking_link(detail):
    assert detail.booking_id == 6435


def test_detail_poster(detail):
    assert detail.poster_url == (
        "https://poster.gsc.com.my/2026/260923_MunafikMelawanIblis_big.jpg"
    )


def test_detail_cast_falls_back_to_credit_row():
    """With no JSON-LD actor array, the Cast credit row is used instead."""
    html = """
    <h1 class="movie-title">Fallback Film</h1>
    <div class="credits">
      <div class="credit"><span class="credit-label">Director</span> : Jane Doe</div>
      <div class="credit"><span class="credit-label">Cast</span> : Ann Lee, Bo Ng</div>
    </div>
    """
    result = parse_detail(html, "fallback-film")
    assert result.director == "Jane Doe"
    assert result.main_stars == ("Ann Lee", "Bo Ng")


def test_detail_cast_placeholder_becomes_empty_tuple():
    """A Cast row reading "N/A" must yield an empty tuple, not ("N/A",)."""
    html = """
    <h1 class="movie-title">No Cast</h1>
    <div class="credits">
      <div class="credit"><span class="credit-label">Cast</span> : N/A</div>
    </div>
    """
    assert parse_detail(html, "no-cast").main_stars == ()


def test_to_dict_is_json_friendly(detail, now_showing):
    data = detail.to_dict()
    for key in ("title", "slug", "url", "genre", "languages", "subtitles", "synopsis"):
        assert key in data
    json.dumps(data)
    json.dumps([m.to_dict() for m in now_showing])


# -- helpers ----------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1 hr 45 mins", 105),
        ("2 hrs 8 mins", 128),
        ("2 hrs 0 min", 120),
        ("45 mins", 45),
        ("0 hrs 0 min", None),
        ("N/A", None),
        (None, None),
    ],
)
def test_duration_minutes(value, expected):
    assert duration_minutes(value) == expected


def test_runtime_exposed_on_movie(now_showing):
    movie = next(m for m in now_showing if m.slug == "hope")
    assert movie.duration == "2 hrs 37 mins"
    assert movie.runtime == 157


@pytest.mark.parametrize(
    "value,expected",
    [
        ("ENG,BM,CHI", ("ENG", "BM", "CHI")),
        ("ENG", ("ENG",)),
        ("N/A", ()),
        ("", ()),
        (None, ()),
        ("ENG, N/A , BM", ("ENG", "BM")),
    ],
)
def test_split_codes(value, expected):
    assert split_codes(value) == expected


@pytest.mark.parametrize("value", ["N/A", "n/a", " TBC ", "-", "", "  ", None])
def test_clean_drops_placeholders(value):
    assert clean(value) is None


def test_clean_keeps_real_values():
    assert clean("  ENG  ") == "ENG"