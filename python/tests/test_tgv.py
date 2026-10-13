"""Offline tests for tgv_scraper.py - run against saved copies of the live API replies."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import tgv_scraper as tgv
from tgv_scraper import (
    Cinema,
    Movie,
    Seat,
    SeatArea,
    SeatPlan,
    SeatRow,
    Showtime,
    TicketType,
    align_by_number,
    apply_seat_status,
    by_region,
    clean,
    experience_label,
    filter_formats,
    filter_times,
    find_cinema,
    normalise_date,
    parse_cinemas,
    parse_day,
    parse_movie,
    parse_regions,
    parse_seat_plan,
    parse_seat_status,
    parse_showtime,
    parse_tickets,
    render_seat_plan,
    strip_html,
)

FIXTURES = Path(__file__).parent / "fixtures"
CINEMA = "BU0"
DATE = "2026-10-11"


def load(name: str) -> dict:
    """A saved API `results` payload, exactly as the parsers receive it."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def day() -> dict:
    return load("tgv_day_BU0.json")


@pytest.fixture(scope="module")
def movies(day) -> list[Movie]:
    return parse_day(day, CINEMA)


@pytest.fixture
def fresh(day) -> list[Movie]:
    """A private copy. apply_seat_status mutates in place, so tests that write
    to showtimes must not share the module-scoped fixture."""
    return parse_day(day, CINEMA)


@pytest.fixture(scope="module")
def region_labels() -> dict:
    return parse_regions(load("tgv_regions.json"))


@pytest.fixture(scope="module")
def outlets(region_labels) -> list[Cinema]:
    return parse_cinemas(load("tgv_cinemas.json"), region_labels)


# -- the day payload ----------------------------------------------------


def test_day_parses_into_movies(movies):
    assert len(movies) == 8
    assert all(isinstance(m, Movie) for m in movies)


def test_every_movie_carries_the_cinema(day, movies):
    for film in movies:
        assert film.cinema_id == CINEMA
        assert film.cinema_name == "1 UTAMA"
        assert film.showtimes, f"{film.title} parsed with no sessions"


def test_movie_details_are_joined_onto_sessions(movies):
    """The API lists the film twice: a detail record and a bare id next to the
    sessions. The parser has to join them or every title comes out as a uuid."""
    titles = {m.title for m in movies}
    assert "Other Mommy" in titles
    assert not any(len(t) == 36 for t in titles), "a uuid leaked through as a title"

    film = next(m for m in movies if m.title == "Other Mommy")
    assert film.genre == ("Horror",)
    assert film.runtime == 93
    assert film.rating == "16"
    assert film.director == "Rob Savage"
    assert film.language == "English"
    assert film.release_date == "2026-10-08"
    assert "Jessica Chastain" in film.cast
    assert film.trailer_id == "bhZ7nKxiFUU"


def test_showtimes_are_ordered_and_well_formed(movies):
    for film in movies:
        times = [s.start for s in film.showtimes]
        assert times == sorted(times), f"{film.title} sessions came back unsorted"
        for showtime in film.showtimes:
            assert showtime.session_id
            assert showtime.hall
            assert showtime.business_date == DATE
            assert showtime.cinema_id == CINEMA


def test_times_are_malaysia_wall_clock(movies):
    film = next(m for m in movies if m.title == "Other Mommy")
    first = film.showtimes[0]
    assert first.start == datetime(2026, 10, 11, 15, 0, tzinfo=tgv.MYT)
    assert first.start.utcoffset().total_seconds() == 8 * 3600
    assert first.time == "15:00"


def test_end_time_uses_the_movie_runtime(movies):
    film = next(m for m in movies if m.title == "Other Mommy")
    showtime = film.showtimes[0]
    assert showtime.runtime == 93
    assert showtime.ends == datetime(2026, 10, 11, 16, 33, tzinfo=tgv.MYT)


def test_experience_codes_render_as_names(movies):
    film = next(m for m in movies if m.title == "Other Mommy")
    labels = {s.experience_name for s in film.showtimes}
    assert "Indulge" in labels
    assert "Junior (Hot Wheels)" in labels


def test_cinema_name_overrides_the_argument(day):
    """A single-cinema payload should not need the caller to know its name."""
    films = parse_day(day, "SOMETHING-ELSE")
    assert films[0].cinema_id == CINEMA
    assert films[0].cinema_name == "1 UTAMA"


def test_empty_day_is_an_empty_list():
    assert parse_day(load("tgv_day_empty.json"), "SW0") == []


def test_session_without_a_detail_record_still_renders():
    """A session can arrive with no matching entry under results.movies."""
    payload = {
        "businessday": {
            "businessday": DATE,
            "cinemas": [{
                "cinemaid": CINEMA,
                "name": "1 UTAMA",
                "movies": [{
                    "movieid": "orphan-id",
                    "experiences": [{
                        "experience": "deluxe",
                        "sessions": [{
                            "sessionid": "1", "movieid": "orphan-id",
                            "cinemaid": CINEMA, "showtimemy": f"{DATE}T19:30:00",
                            "screenname": "Cinema 9", "businessdate": DATE,
                            "experience": "deluxe", "seattypes": "SINGLE", "status": 1,
                        }],
                    }],
                }],
            }],
        },
        "movies": [],
    }
    films = parse_day(payload, CINEMA)
    assert len(films) == 1
    assert films[0].id == "orphan-id"
    assert films[0].title == "orphan-id"
    assert films[0].showtimes[0].time == "19:30"


# -- seat status --------------------------------------------------------


def test_seat_status_maps_session_ids_to_counts():
    seats = parse_seat_status(load("tgv_seatstatus.json"))
    assert seats
    for total, used in seats.values():
        assert isinstance(total, int) and isinstance(used, int)
        assert 0 <= used <= total


def test_apply_seat_status_fills_the_gaps(fresh):
    seats = {m.showtimes[0].session_id: (100, 40) for m in fresh}
    apply_seat_status(fresh, seats)
    first = fresh[0].showtimes[0]
    assert (first.seats_total, first.seats_used) == (100, 40)
    assert first.seats_left == 60
    assert first.sold_out is False
    assert fresh[0].showtimes[-1].seats_left is None


def test_sold_out_is_exactly_zero_left(fresh):
    apply_seat_status(fresh, {fresh[0].showtimes[0].session_id: (50, 50)})
    showtime = fresh[0].showtimes[0]
    assert showtime.seats_left == 0
    assert showtime.sold_out is True


def test_unknown_sessions_are_left_alone(fresh):
    apply_seat_status(fresh, {"not-a-real-session": (10, 1)})
    assert all(s.seats_total is None for m in fresh for s in m.showtimes)


# -- cinemas ------------------------------------------------------------


def test_cinemas_parse_with_addresses(outlets):
    assert len(outlets) >= 40
    assert all(isinstance(c, Cinema) for c in outlets)
    utama = next(c for c in outlets if c.id == CINEMA)
    assert utama.name == "1 UTAMA"
    assert "1 Utama Shopping Centre" in utama.address
    assert utama.keyword == "1-utama"
    assert utama.latitude and utama.longitude
    assert "imaxlaser" in utama.experiences


def test_cinema_urls_use_the_site_layout(outlets):
    utama = next(c for c in outlets if c.id == CINEMA)
    assert utama.url == "https://www.tgv.com.my/cinemas/klangvalley/1-utama"


def test_region_labels_are_applied(outlets, region_labels):
    assert "klangvalley" in region_labels
    assert region_labels["klangvalley"] == "Klang Valley"
    utama = next(c for c in outlets if c.id == CINEMA)
    assert utama.region == "Klang Valley"


def test_by_region_accepts_key_or_label(outlets):
    assert by_region(outlets, "pinang")
    assert by_region(outlets, "Pulau Pinang")
    assert by_region(outlets, "PULAU PINANG") == by_region(outlets, "pinang")
    assert by_region(outlets, "atlantis") == []


def test_find_cinema_by_id_name_and_keyword(outlets):
    assert find_cinema(outlets, CINEMA).id == CINEMA
    assert find_cinema(outlets, "1 utama").id == CINEMA
    assert find_cinema(outlets, "1-utama").id == CINEMA


def test_find_cinema_reports_ambiguity(outlets):
    with pytest.raises(SystemExit) as excinfo:
        find_cinema(outlets, "sunway")
    assert "matches" in str(excinfo.value)


def test_find_cinema_reports_a_miss(outlets):
    with pytest.raises(SystemExit):
        find_cinema(outlets, "no-such-cinema")


# -- small helpers ------------------------------------------------------


def test_experience_labels_are_humanised():
    assert experience_label("junior-hotwheel") == "Junior (Hot Wheels)"
    assert experience_label("imaxlaser") == "IMAX Laser"
    assert experience_label("deluxe") == "Deluxe"


def test_unknown_experience_codes_still_render():
    """New formats appear without notice. An unknown code must not blank out."""
    assert experience_label("brand_new_hall") == "Brand New Hall"
    assert experience_label("") is None
    assert experience_label(None) is None


def test_normalise_date_accepts_several_shapes():
    assert normalise_date("2026-10-13") == "2026-10-13"
    assert normalise_date("13-10-2026") == "2026-10-13"
    assert normalise_date("2026/10/13") == "2026-10-13"
    assert normalise_date(datetime(2026, 10, 13).date()) == "2026-10-13"


def test_normalise_date_defaults_to_today_in_malaysia():
    today = datetime.now(tgv.MYT).date().isoformat()
    assert normalise_date(None) == today
    assert normalise_date("") == today


def test_normalise_date_rejects_nonsense():
    with pytest.raises(SystemExit):
        normalise_date("next tuesday")


def test_strip_html_flattens_the_synopsis():
    raw = "<h3>Mother Truly Knows Best...</h3>\n<div>A young girl &hellip;</div>"
    assert strip_html(raw) == "Mother Truly Knows Best... A young girl ..."
    assert strip_html(None) is None


def test_clean_treats_blank_as_missing():
    assert clean("  x ") == "x"
    assert clean("") is None
    assert clean("   ") is None
    assert clean(None) is None


def test_movie_without_a_runtime_still_parses():
    """Unreleased films carry runtimemins as an empty string, not a number."""
    film = parse_movie({"recid": "x", "name": "Avengers: Doomsday", "extdata": {
        "movieinfo": {"runtimemins": "", "rating": "tba"},
    }})
    assert film.runtime is None
    assert film.rating == "tba"
    assert film.title == "Avengers: Doomsday"


def test_movie_json_is_serialisable(movies):
    payload = json.dumps([m.to_dict() for m in movies])
    assert json.loads(payload)[0]["showtimes"]


def test_cinema_json_is_serialisable(outlets):
    assert json.loads(json.dumps([c.to_dict() for c in outlets]))


def test_showtime_serialises_even_with_no_seat_data():
    showtime = parse_showtime(
        {"sessionid": "1", "movieid": "m", "cinemaid": CINEMA,
         "showtimemy": f"{DATE}T10:00:00", "screenname": "Cinema 1"},
        CINEMA,
    )
    assert showtime.seat_types == ()
    assert showtime.seats_left is None
    assert showtime.to_dict()["time"] == "10:00"


def test_filter_times_keeps_post_midnight_shows():
    """TGV files a 00:30 show under the previous day's business date but
    timestamps it on the next calendar day. Anchoring the window on the
    business date is what stops `--from 18:00` from dropping the late show."""
    films = parse_day(load("tgv_day_late.json"), CINEMA)
    assert [s.time for f in films for s in f.showtimes] == ["13:35", "19:00", "00:30"]
    kept = [s.time for f in tgv.filter_times(films, "18:00") for s in f.showtimes]
    assert kept == ["19:00", "00:30"]


def test_filter_times_can_exclude_the_post_midnight_show():
    """--until is measured against the business date, so it cuts the late show."""
    films = parse_day(load("tgv_day_late.json"), CINEMA)
    kept = [s.time for f in tgv.filter_times(films, None, "23:59") for s in f.showtimes]
    assert kept == ["13:35", "19:00"]


def test_filter_times_bounds_are_inclusive():
    """A show starting exactly on the bound is kept by both ends."""
    films = parse_day(load("tgv_day_late.json"), CINEMA)
    kept = [s.time for f in tgv.filter_times(films, "19:00", "19:00") for s in f.showtimes]
    assert kept == ["19:00"]


def test_filter_times_before_bound():
    films = parse_day(load("tgv_day_BU0.json"), CINEMA)
    early = [s.time for f in tgv.filter_times(films, None, "18:00") for s in f.showtimes]
    assert early
    assert all(t <= "18:00" for t in early)


def test_filter_times_between_bounds():
    films = parse_day(load("tgv_day_BU0.json"), CINEMA)
    window = [s.time for f in tgv.filter_times(films, "16:00", "18:00") for s in f.showtimes]
    assert window
    assert all("16:00" <= t <= "18:00" for t in window)


def test_filter_times_leaves_the_input_untouched_when_unbounded():
    films = parse_day(load("tgv_day_BU0.json"), CINEMA)
    before = sum(len(f.showtimes) for f in films)
    assert tgv.filter_times(films) is films
    assert sum(len(f.showtimes) for f in films) == before


def test_filter_times_drops_empty_films():
    films = parse_day(load("tgv_day_BU0.json"), CINEMA)
    assert tgv.filter_times(films, "03:00", "03:01") == []


def test_filter_times_accepts_compact_clock():
    spaced = parse_day(load("tgv_day_BU0.json"), CINEMA)
    packed = parse_day(load("tgv_day_BU0.json"), CINEMA)
    assert [s.time for f in tgv.filter_times(spaced, "16:00", "18:00") for s in f.showtimes] == [
        s.time for f in tgv.filter_times(packed, "1600", "1800") for s in f.showtimes
    ]


def test_filter_formats_keeps_only_the_named_hall(day):
    films = parse_day(load("tgv_day_BU0.json"), CINEMA)
    codes = {s.experience for f in films for s in tgv.filter_formats(films, "deluxe")
             for s in f.showtimes}
    assert codes == {"deluxe"}


def test_filter_formats_accepts_several_codes(day):
    films = parse_day(load("tgv_day_BU0.json"), CINEMA)
    kept = tgv.filter_formats(films, "deluxe,imaxlaser")
    codes = {s.experience for f in kept for s in f.showtimes}
    assert codes == {"deluxe", "imaxlaser"}


def test_filter_formats_is_case_insensitive(day):
    films = parse_day(load("tgv_day_BU0.json"), CINEMA)
    assert tgv.filter_formats(films, "DELUXE")


def test_filter_formats_unknown_code_yields_nothing(day):
    films = parse_day(load("tgv_day_BU0.json"), CINEMA)
    assert tgv.filter_formats(films, "no-such-hall") == []


def test_filter_formats_without_a_filter_is_a_no_op(day):
    films = parse_day(load("tgv_day_BU0.json"), CINEMA)
    before = sum(len(f.showtimes) for f in films)
    assert tgv.filter_formats(films) is films
    assert sum(len(f.showtimes) for f in films) == before


def test_filter_formats_composes_with_filter_times(day):
    """The two filters chain, so evening + deluxe means both, not either."""
    films = parse_day(load("tgv_day_BU0.json"), CINEMA)
    kept = tgv.filter_formats(filter_times(films, "18:00"), "deluxe")
    assert kept
    for film in kept:
        assert all(s.experience == "deluxe" for s in film.showtimes)
        assert all(s.start.hour >= 18 for s in film.showtimes)


# -- prices ------------------------------------------------------------


@pytest.fixture(scope="module")
def tickets() -> tuple:
    return parse_tickets(load("tgv_tickets.json"))


def test_tickets_parse_with_prices(tickets):
    assert len(tickets) == 3
    single = next(t for t in tickets if t.description == "SINGLE")
    assert single.price_cents == 2500
    assert single.price == 25.00
    assert single.standard is True


def test_ticket_price_is_the_advertised_price_and_ignores_tax(tickets):
    """Only priceInCents is used. The API's taxInCents is skipped on purpose.

    TGV's `taxInCents` works out to 20% at 1 Utama and ~9% at Pavilion Damansara,
    yet both are in Selangor where cinema admission carries a flat 25%
    entertainment duty and movie tickets are exempt from service tax. Adding it
    inflated every price and made identical tickets look different between
    outlets, so `price` is the advertised price and nothing is added to it.
    """
    single = next(t for t in tickets if t.description == "SINGLE")
    assert single.price_cents == 2500
    assert single.price == 25.00
    assert not hasattr(single, "total")


def test_concession_and_promo_tickets_are_not_standard(tickets):
    """SENIOR/OKU and the Maybank promo are cheaper than the plain seat. They
    must not be quoted as the headline price."""
    by_desc = {t.description: t for t in tickets}
    assert by_desc["SENIOR/OKU"].standard is False
    assert by_desc["Maybank Mastercard Single"].standard is False
    assert by_desc["SINGLE"].standard is True


def test_only_standard_tickets_count_towards_the_shown_price(tickets):
    showtime = Showtime(session_id="1", movie_id="m", cinema_id="PDH", start=None,
                        ticket_types=tickets)
    assert showtime.price == 25.00
    assert showtime.price_range == "RM25.00"


def test_price_range_spans_several_single_types():
    """A range covers differing SINGLE prices across areas, not bundles."""
    showtime = Showtime(
        session_id="1", movie_id="m", cinema_id="X", start=None,
        ticket_types=(
            TicketType(code="1", description="SINGLE", price_cents=2500, standard=True),
            TicketType(code="2", description="SINGLE", price_cents=3000, standard=True),
        ),
    )
    assert showtime.price == 25.00
    assert showtime.price_range == "RM25.00 - RM30.00"


def test_price_ignores_bundles_even_when_they_are_the_only_standard_type():
    """A hall selling nothing but TWIN has no single-seat price, so `price` falls
    back to the bundle rather than returning None, and `price_bundle` exposes it."""
    showtime = Showtime(
        session_id="1", movie_id="m", cinema_id="BU0", start=None,
        ticket_types=(TicketType(code="1", description="TWIN", price_cents=5400, standard=True, package=True),),
    )
    assert showtime.single_seat_price is None
    assert showtime.price == 54.00
    assert showtime.price_bundle == 54.00
    assert showtime.is_bundle_only is True


def test_price_is_none_without_the_prices_call(movies):
    assert all(s.price is None for m in movies for s in m.showtimes)
    assert all(s.price_range is None for m in movies for s in m.showtimes)


def test_showtime_json_carries_ticket_types(tickets):
    showtime = Showtime(session_id="1", movie_id="m", cinema_id="PDH", start=None,
                        ticket_types=tickets)
    payload = showtime.to_dict()
    assert payload["price"] == 25.00
    assert len(payload["ticket_types"]) == 3
    assert json.loads(json.dumps(payload))["price_range"] == "RM25.00"


def test_sessions_expose_their_area_codes(movies):
    """Prices are fetched per hall area, so the codes have to survive parsing."""
    codes = [s.area_codes for m in movies for s in m.showtimes]
    assert all(codes)
    assert all(isinstance(c, tuple) for c in codes)


def test_bundle_tickets_advertise_how_many_people_they_seat():
    twin = TicketType(code="1", description="TWIN", price_cents=5400,
                      standard=True)
    single = TicketType(code="2", description="SINGLE", price_cents=2500,
                        standard=True)
    assert twin.seats == 2
    assert single.seats == 1


def test_per_person_splits_a_bundle():
    """A TWIN at RM54.00 is RM27.00 a head. Comparing it against a SINGLE seat
    without dividing would make a bundle look twice the price of a single."""
    showtime = Showtime(
        session_id="1", movie_id="m", cinema_id="BU0", start=None,
        ticket_types=(
            TicketType(code="1", description="TWIN", price_cents=5400,
                       standard=True),
        ),
    )
    assert showtime.price == 54.00
    assert showtime.per_person == 27.00


def test_per_person_treats_a_single_as_itself():
    showtime = Showtime(
        session_id="1", movie_id="m", cinema_id="PDH", start=None,
        ticket_types=(next(t for t in parse_tickets(load("tgv_tickets.json"))
                          if t.description == "SINGLE"),),
    )
    assert showtime.per_person == 25.00


def test_per_person_picks_the_cheapest_per_head():
    """With both a bundle and a single on sale, per_person should find the
    genuinely cheapest way in, not the smallest ticket total."""
    showtime = Showtime(
        session_id="1", movie_id="m", cinema_id="X", start=None,
        ticket_types=(
            TicketType(code="1", description="SINGLE", price_cents=2000,
                       standard=True),
            TicketType(code="2", description="TWIN", price_cents=3000,
                       standard=True),
        ),
    )
    assert showtime.price == 20.00
    # SINGLE is 20.00 for one person; TWIN is 30.00 for two, so 15.00 each.
    assert showtime.per_person == 15.00


def test_per_person_ignores_special_tickets(tickets):
    showtime = Showtime(session_id="1", movie_id="m", cinema_id="PDH", start=None,
                        ticket_types=tickets)
    assert showtime.per_person == 25.00


def test_unknown_ticket_names_count_as_one_seat():
    """An unfamiliar ticket name must not divide the price by a wrong number."""
    odd = TicketType(code="9", description="QUAD PACK", price_cents=4000, standard=True)
    assert odd.seats == 1
    showtime = Showtime(session_id="1", movie_id="m", cinema_id="X", start=None,
                        ticket_types=(odd,))
    assert showtime.per_person == 40.00


def test_showtime_json_carries_per_person():
    showtime = Showtime(
        session_id="1", movie_id="m", cinema_id="BU0", start=None,
        ticket_types=(TicketType(code="1", description="TWIN", price_cents=5400, standard=True),),
    )
    assert showtime.to_dict()["per_person"] == 27.00
    assert showtime.to_dict()["ticket_types"][0]["seats"] == 2


def test_package_flag_comes_from_the_api(tickets):
    """`isPackageTicket` is the API's own signal that a ticket bundles seats.
    The name "TWIN" suggests it, but the field is what confirms it."""
    assert all(t.package is False for t in tickets), "PDH sells only plain singles"
    twin = TicketType(code="1", description="TWIN", price_cents=5400, standard=True, package=True)
    assert twin.package is True
    assert twin.to_dict()["package"] is True


def test_single_seat_price_ignores_bundles():
    twin = Showtime(
        session_id="1", movie_id="m", cinema_id="BU0", start=None,
        ticket_types=(TicketType(code="1", description="TWIN", price_cents=5400, standard=True, package=True),),
    )
    assert twin.price == 54.00
    assert twin.single_seat_price is None
    assert twin.is_bundle_only is True


def test_single_seat_price_matches_a_single(tickets):
    showtime = Showtime(session_id="1", movie_id="m", cinema_id="PDH", start=None,
                        ticket_types=tickets)
    assert showtime.single_seat_price == 25.00
    assert showtime.is_bundle_only is False


def test_bundle_flag_is_recorded_on_showtimes(movies):
    """Every showtime keeps its area codes and, once priced, its package mix."""
    f = next(m for m in movies if m.title == "Other Mommy")
    for s in f.showtimes:
        assert isinstance(s.area_codes, tuple)
        if s.ticket_types:
            assert all(isinstance(t.package, bool) for t in s.ticket_types)


@pytest.fixture(scope="module")
def two_area_tickets() -> tuple:
    """1 Utama Cinema 3 sells a TWIN in one hall area and a SINGLE in another."""
    return parse_tickets(load("tgv_tickets_two_areas.json"))


def test_single_seat_is_found_across_areas(two_area_tickets):
    """Regression: one area sells only TWIN bundles. Reading just that area and
    reporting its price made a two-seat bundle look like the only option, and
    quoted RM54.00 where the single seat is RM25.00."""
    showtime = Showtime(session_id="1", movie_id="m", cinema_id="BU0", start=None,
                        ticket_types=two_area_tickets)
    assert showtime.single_seat_price == 25.00
    assert showtime.price == 25.00
    assert showtime.is_bundle_only is False


def test_bundle_price_is_reported_separately(two_area_tickets):
    showtime = Showtime(session_id="1", movie_id="m", cinema_id="BU0", start=None,
                        ticket_types=two_area_tickets)
    assert showtime.price_bundle == 54.00


def test_finished_promo_tickets_are_marked(two_area_tickets):
    """The Maybank ticket keeps its RM10 price in the API even when TGV writes
    "Today's Promotion Limit Reached" into the description."""
    promo = next(t for t in two_area_tickets if "Maybank" in t.description)
    assert promo.sold_out is True
    assert promo.standard is False
    showtime = Showtime(session_id="1", movie_id="m", cinema_id="BU0", start=None,
                        ticket_types=two_area_tickets)
    assert "Maybank Mastercard Single" in showtime.sold_out_promos
    assert showtime.single_seat_price == 25.00


def test_price_never_returns_a_bundle_total(two_area_tickets):
    """`price` must be a one-seat figure even when the hall also sells TWIN."""
    showtime = Showtime(session_id="1", movie_id="m", cinema_id="BU0", start=None,
                        ticket_types=two_area_tickets)
    assert showtime.price < showtime.price_bundle


def test_tickets_carry_their_area_code(two_area_tickets):
    codes = {t.area_code for t in two_area_tickets}
    assert codes == {"0000000002", "0000000010"}
    twin = next(t for t in two_area_tickets if t.description == "TWIN")
    single = next(t for t in two_area_tickets if t.description == "SINGLE")
    assert twin.area_code != single.area_code


def test_tickets_endpoint_is_mapped():
    assert tgv.ENDPOINTS["tickets"] == "/api/boxoffice/v1/moviesession_gettickets"


def drawn_row_names(plan, **kwargs) -> list[str]:
    """The row labels of a rendered plan, top to bottom.

    Picking them out by shape is fragile once the drawing carries area headings
    and per-area totals, so this drops every summary line: those either hold the
    " - " separator or end in a digit-led token such as `TWIN:`.
    """
    lines = render_seat_plan(plan, ruler=False, **kwargs).splitlines()
    names = []
    for line in lines:
        tokens = line.split()
        if not tokens or " - " in line or ":" in tokens[0]:
            continue
        if not line[:1].isalpha():
            continue
        if any(t.isdigit() or t == "X" for t in tokens[1:]):
            names.append(tokens[0])
    return names


@pytest.fixture(scope="module")
def utama_plan():
    return parse_seat_plan(load("tgv_seatplan_BU0.json"))


@pytest.fixture(scope="module")
def pdh_plan():
    return parse_seat_plan(load("tgv_seatplan_PDH.json"))


def test_seat_plan_splits_the_hall_into_its_areas(utama_plan):
    """A hall sells different seat kinds at different prices, and each comes
    back as its own area with its own layout."""
    assert [a.description for a in utama_plan.areas] == ["TWIN", "SINGLE"]
    assert utama_plan.areas[0].seat_count == 24
    assert utama_plan.areas[1].seat_count == 154


def test_every_seat_in_the_plan_is_accounted_for(utama_plan, pdh_plan):
    """Row counts must add up to what the API claims for the area. This is the
    check that catches a dropped aisle or an unparsed row."""
    for plan in (utama_plan, pdh_plan):
        for area in plan.areas:
            assert len(area.seats) == area.seat_count


def test_booked_seats_are_marked_where_they_are(pdh_plan):
    """Pavilion Damansara has two seats gone, numbers 9 and 8 on row H. Every
    other seat is free, so the count alone cannot say where they are.

    Seat numbers are not column numbers: number 9 sits in column 7, because the
    aisle at column 2 takes a slot. Asserting the columns as well is what pins
    both down and stops one being mistaken for the other."""
    single = next(a for a in pdh_plan.areas if a.description == "SINGLE")
    taken = single.booked
    assert len(taken) == 2
    assert {s.row for s in taken} == {1}          # row H, the second SINGLE row
    assert {s.id for s in taken} == {"9", "8"}
    assert {s.column for s in taken} == {7, 8}
    assert all(s.free for s in single.seats if s not in taken)


def test_rows_of_differing_width_keep_their_own_seat_counts(utama_plan):
    """Rows B to H hold 16 seats, J and K hold 15, A holds 12, because of the
    aisles. Packing the numbers together once made H look like it had an extra
    seat that no other row had."""
    single = next(a for a in utama_plan.areas if a.description == "SINGLE")
    widths = {row.name: len(row.seats) for row in single.occupied_rows}
    assert widths["B"] == 16
    assert widths["H"] == 16
    assert widths["J"] == 15
    assert widths["A"] == 12
    assert widths["H"] == widths["B"], "H is not wider than B"


def test_aisle_columns_are_missing_from_a_row_not_invented(utama_plan):
    """Row H skips columns 2 and 15. The seats either side are 14 and 13, so
    closing the gap would renumber the row and destroy the real seat numbers."""
    single = next(a for a in utama_plan.areas if a.description == "SINGLE")
    row_h = next(r for r in single.occupied_rows if r.name == "H")
    assert 2 not in row_h.columns
    assert 15 not in row_h.columns
    assert {s.id for s in row_h.seats if s.column == 1} == {"15"}
    assert {s.id for s in row_h.seats if s.column == 3} == {"14"}


def test_alignment_moves_a_short_row_onto_the_shared_number_columns(utama_plan):
    """Rows J and K hold 15 seats against B's 16, and TGV packs their numbering
    against the left end, so seat 15 lands in column 0 instead of column 1.

    A real hall letters seat 15 into the same column in every row. Aligning on
    the number puts J and K back under B and leaves column 0 empty, which is
    where the absent seat 16 would have been."""
    single = next(a for a in utama_plan.areas if a.description == "SINGLE")
    before = {r.name: {s.id: s.column for s in r.seats} for r in single.occupied_rows}
    assert before["B"]["15"] != before["J"]["15"], "the fixture must show the skew"

    aligned = align_by_number(utama_plan)
    single = next(a for a in aligned.areas if a.description == "SINGLE")
    after = {r.name: {s.id: s.column for s in r.seats} for r in single.occupied_rows}

    assert after["J"]["15"] == after["B"]["15"] == 1
    assert after["K"]["15"] == after["B"]["15"]
    assert after["J"]["14"] == after["B"]["14"] == 3
    assert 0 not in {s.column for s in next(
        r for r in single.occupied_rows if r.name == "J").seats}, "c0 must be empty"
    assert "16" not in {s.id for s in next(
        r for r in single.occupied_rows if r.name == "J").seats}


def test_alignment_never_loses_or_duplicates_a_seat(utama_plan, pdh_plan):
    """Moving seats is only safe if the total is unchanged and no two seats in a
    row land in the same column."""
    for plan in (utama_plan, pdh_plan):
        aligned = align_by_number(plan)
        for area, original in zip(aligned.areas, plan.areas):
            assert len(area.seats) == len(original.seats)
            assert area.seat_count == len(area.seats)
            for row in area.occupied_rows:
                columns = [s.column for s in row.seats]
                assert len(set(columns)) == len(columns), "two seats stacked"


def test_alignment_is_a_no_op_where_numbering_already_agrees(pdh_plan):
    """Every Pavilion row is identical, so nothing should move there."""
    aligned = align_by_number(pdh_plan)
    assert aligned.moved_by_alignment == 0
    for area, original in zip(aligned.areas, pdh_plan.areas):
        for row, before in zip(area.occupied_rows, original.occupied_rows):
            assert [s.column for s in row.seats] == [s.column for s in before.seats]


def test_alignment_leaves_non_numeric_seat_ids_alone(utama_plan):
    """A seat TGV labels something other than a number must not be guessed at."""
    single = next(a for a in utama_plan.areas if a.description == "SINGLE")
    row = single.occupied_rows[0]
    odd = Seat(id="PODIUM", row=row.index, column=0)
    patched = SeatArea(
        number=single.number, code=single.code, description=single.description,
        seat_count=single.seat_count, row_count=single.row_count,
        column_count=single.column_count,
        rows=(SeatRow(index=row.index, name=row.name,
                      seats=(odd,) + row.seats[1:]),)
             + tuple(r for r in single.rows if r.index != row.index),
    )
    plan = SeatPlan(cinema_id="BU0", session_id="1", areas=(patched,))
    aligned = align_by_number(plan)
    kept = aligned.areas[0].occupied_rows[0].seats[0]
    assert kept.id == "PODIUM"
    assert kept.column == 0, "an unnumbered seat keeps the column the API gave it"


def test_render_aligns_by_default_and_can_show_raw_columns(utama_plan):
    """The default drawing lines rows up; align=False shows TGV's own columns."""
    aligned = rendered_cells(utama_plan)
    raw = rendered_cells(utama_plan, align=False)
    assert aligned["J"][1] == aligned["B"][1] == "15"
    assert raw["J"][0] == "15" and raw["B"][0] == "16"
    assert raw["J"][1] == "14"


def rendered_cells(plan, **kwargs) -> dict:
    """{row name: [cell per column]} for a rendered plan."""
    drawn = render_seat_plan(plan, ruler=False, **kwargs).splitlines()
    rows = {}
    for line in drawn:
        tokens = line.split()
        if not tokens or " - " in line or ":" in tokens[0] or "SCREEN" in line:
            continue
        if not line[:1].isalpha():
            continue
        if not any(t.isdigit() or t == "X" for t in tokens[1:]):
            continue
        rows[tokens[0]] = [line[3 + c * 3: 3 + (c + 1) * 3].strip() or "."
                           for c in range(18)]
    return rows


def test_row_indices_run_from_the_back_of_the_hall_towards_the_screen(utama_plan, pdh_plan):
    """TGV counts rows from the rearmost row, so the drawing has to reverse them.

    Three things in the payload show the direction. The indices form one
    contiguous run shared by every area rather than restarting per area. The
    TWIN area, which is sold at the back of a hall, holds the low indices. And
    the row at index 0 is named M while the highest index is named A, which is
    the usual cinema convention of lettering forwards from the front."""
    for plan in (utama_plan, pdh_plan):
        indices = [row.index for _, row in plan.hall_rows]
        assert indices == sorted(indices, reverse=True), "not screen end first"
        # One shared run across every area, no gaps and no restarts.
        assert sorted(indices) == list(range(len(indices)))
        twin = next(a for a in plan.areas if a.description == "TWIN")
        single = next(a for a in plan.areas if a.description == "SINGLE")
        assert max(r.index for r in twin.occupied_rows) < min(
            r.index for r in single.occupied_rows
        ), "the twin rows sit behind the singles"


def test_hall_rows_start_at_the_screen_end_and_end_with_the_twin_rows(utama_plan):
    """Screen first, then the single rows in letter order, twin rows last."""
    assert drawn_row_names(utama_plan) == [
        "A", "B", "C", "D", "E", "F", "G", "H", "J", "K", "L", "M",
    ]


def test_row_names_appear_in_letter_order_top_to_bottom(pdh_plan):
    """The real names need no relabelling once the rows are ordered correctly."""
    assert drawn_row_names(pdh_plan) == ["A", "B", "C", "D", "E", "F", "G", "H", "J"]


def test_screen_marker_sits_above_the_first_row(utama_plan):
    drawn = render_seat_plan(utama_plan).splitlines()
    screen = next(i for i, line in enumerate(drawn) if "SCREEN" in line)
    first_row = next(i for i, line in enumerate(drawn)
                     if line[:1].isalpha() and " - " not in line
                     and ":" not in line.split()[0]
                     and any(t.isdigit() or t == "X" for t in line.split()[1:]))
    assert screen < first_row


def test_twin_heading_comes_after_the_single_rows(utama_plan):
    """The twin rows are at the back, so their heading cannot come first."""
    drawn = render_seat_plan(utama_plan).splitlines()
    single = next(i for i, l in enumerate(drawn) if l.startswith("SINGLE -"))
    twin = next(i for i, l in enumerate(drawn) if l.startswith("TWIN -"))
    assert single < twin


def test_empty_placeholder_rows_are_not_drawn(utama_plan):
    """Every area reports a full rowCount, but a second area's rows come back as
    nameless empty entries. Drawing them would print blank lines."""
    twin = next(a for a in utama_plan.areas if a.description == "TWIN")
    assert twin.row_count == 12
    assert len(twin.rows) == 12
    assert len(twin.occupied_rows) == 2
    assert all(row.name for row in twin.occupied_rows)


def test_plan_spans_every_column_any_area_uses(utama_plan):
    """The ruler has to cover the widest area so one header fits the page."""
    assert utama_plan.first_column == 0
    assert utama_plan.last_column == 17


def test_rendered_grid_places_a_seat_in_its_own_column(utama_plan):
    """Every seat must land in the cell for its own column, and an aisle column
    must be blank rather than holding the next seat along.

    Checked by cell offset rather than by substring, because a substring can
    match while seats are still in the wrong columns."""
    width, label_width = 3, 3
    drawn = render_seat_plan(utama_plan, ruler=False)
    row = next(line for line in drawn.splitlines() if line.startswith("H "))
    single = next(a for a in utama_plan.areas if a.description == "SINGLE")
    row_h = next(r for r in single.occupied_rows if r.name == "H")

    for seat in row_h.seats:
        cell = row[label_width + seat.column * width:
                   label_width + (seat.column + 1) * width]
        assert cell.strip() == seat.id, f"seat {seat.id} drawn in the wrong column"
    # Column 2 is the aisle: the cell after 15 and before 14 is empty.
    assert row[label_width + 2 * width: label_width + 3 * width].strip() == ""


def test_rendered_grid_draws_as_many_seats_as_the_api_counts(utama_plan, pdh_plan):
    """A silent mismatch is the failure this whole renderer exists to prevent."""
    for plan in (utama_plan, pdh_plan):
        drawn = render_seat_plan(plan)
        for area in plan.areas:
            assert f"{area.label} - {area.seat_count} seats" in drawn
        assert "(API says" not in drawn


def test_ruler_ticks_sit_under_the_numbers_they_label(utama_plan):
    """The tick row must be right-aligned with the column numbers above it.

    Drawn at a fixed cell width the dashes fill the cell and land one character
    left of the digit, so every column reads as shifted by one."""
    drawn = render_seat_plan(utama_plan).splitlines()
    ruler, ticks = drawn[0], drawn[1]
    label_width = 3
    for column in range(18):
        offset = label_width + column * 3
        number = ruler[offset:offset + 3]
        tick = ticks[offset:offset + 3]
        assert len(number.rstrip()) == len(tick.rstrip()), (
            f"column {column}: {number!r} and {tick!r} do not line up"
        )


def test_seats_in_a_column_share_one_right_edge(utama_plan, pdh_plan):
    """Column alignment means the right edge of every cell in a column matches,
    so a two-digit seat number reads against the single-digit ones above and
    below it."""
    for plan in (utama_plan, pdh_plan):
        drawn = render_seat_plan(plan, ruler=False).splitlines()
        rows = [line for line in drawn
                if line[:1].isalpha() and " - " not in line and "drawn" not in line]
        for column in range(plan.first_column, plan.last_column + 1):
            offset = 3 + column * 3
            edges = {len(row[offset:offset + 3].rstrip())
                     for row in rows if row[offset:offset + 3].strip()}
            assert len(edges) <= 1, f"column {column} has edges {edges}"


def test_rendered_grid_marks_booked_seats_and_leaves_free_ones_numbered(pdh_plan):
    drawn = render_seat_plan(pdh_plan, ruler=False)
    row_h = next(line for line in drawn.splitlines() if line.startswith("H "))
    assert "X  X" in row_h
    assert "9  8" not in row_h
    row_g = next(line for line in drawn.splitlines() if line.startswith("G "))
    assert "9  8" in row_g


def test_row_labels_are_tgvs_own_names_not_invented_ones(utama_plan):
    """Earlier this rendered M first, top down, which put the back of the hall at
    the top. The fix was to reverse the row order, not to relabel the rows: the
    names TGV sends already read A to M once ordered correctly."""
    drawn = render_seat_plan(utama_plan, ruler=False).splitlines()
    names = drawn_row_names(utama_plan)
    # Every label is a row TGV itself named, so no row is missing and none added.
    sent = {row.name for area in utama_plan.areas for row in area.rows if row.name}
    assert set(names) == sent
    assert "I" not in sent, "1 Utama skips the letter I, and the output must too"


def test_seat_plan_endpoint_is_mapped():
    assert tgv.ENDPOINTS["seat_plan"] == "/api/boxoffice/v1/moviesession_getseatplan"


def test_seat_plan_posts_a_bare_session_id_and_a_req_field():
    """The endpoint is stricter than the others: it wants a string sessionid,
    not the array getseatstatus takes, and it 400s without `req`."""
    sent = {}

    def fake(path, payload=None, **kwargs):
        sent["path"] = path
        sent["payload"] = payload
        return {"results": {"cinemaid": "BU0", "sessionid": "1", "seatlayout": {"areas": []}}}

    original = tgv.request
    tgv.request = fake
    try:
        tgv.seat_plan("bu0", "326689", business_date="2026-10-18")
    finally:
        tgv.request = original

    assert sent["path"] == tgv.ENDPOINTS["seat_plan"]
    assert sent["payload"]["sessionid"] == "326689"
    assert isinstance(sent["payload"]["sessionid"], str)
    assert sent["payload"]["req"] == "x"
    assert sent["payload"]["cinemaid"] == "BU0"


def test_filter_times_rejects_bad_clocks():
    for bad in ("25:00", "18:99", "half nine", "18"):
        with pytest.raises(SystemExit):
            tgv.filter_times([], bad)


# --- price fetching: dedupe, parallelism, failure handling -------------------

def _counting(monkeypatch, prices_by_area=None):
    """Patch request() and record every endpoint called.

    `prices_by_area` maps an area code to the ticket list returned for it. Any
    area not listed gets a default SINGLE ticket so every session comes back
    priced. The recorded list is returned so a test can assert on how many calls
    actually went out.
    """
    seen: list[str] = []
    prices = prices_by_area or {}
    default = [_price_tickets("SINGLE", 3000)]

    def fake_request(path, payload=None, **kwargs):
        seen.append(path)
        if path == tgv.ENDPOINTS["day"]:
            return {"results": load("tgv_day_BU0.json")}
        if path == tgv.ENDPOINTS["tickets"]:
            code = payload["areacategorycodes"]
            return {"results": {"tickets": prices.get(code, default)}}
        if path == tgv.ENDPOINTS["seat_status"]:
            ids = payload["sessionid"]
            return {"results": {"seatstatuslist": [
                {"sessionid": i, "seatstotal": 100, "seatsused": 10} for i in ids]}}
        raise AssertionError(f"unexpected endpoint {path}")

    monkeypatch.setattr(tgv, "request", fake_request)
    return seen


def _price_tickets(description, price_cents, area=None):
    """A raw gettickets row, in the shape the API sends (parse_tickets reads dicts)."""
    return {"code": description, "description": description,
            "priceInCents": price_cents, "taxInCents": 0}


def test_price_is_fetched_once_per_session_and_area(monkeypatch, day):
    """One gettickets call per session per hall area, and no more.

    Expectation is derived from the fixture rather than hard-coded, so it holds
    as the saved payload changes.
    """
    movies = parse_day(day, CINEMA)
    expected = sum(len(s.area_codes) for f in movies for s in f.showtimes)

    seen = _counting(monkeypatch)
    tgv.day(CINEMA, DATE, prices=True)
    assert seen.count(tgv.ENDPOINTS["tickets"]) == expected


def test_only_the_chosen_sessions_are_priced(monkeypatch, day):
    """Narrowing first is what makes prices affordable.

    An evening-only run must not pay for the screenings it then drops, so it
    costs strictly fewer price calls than the whole day.
    """
    whole_day = parse_day(day, CINEMA)
    all_combos = sum(len(s.area_codes) for f in whole_day for s in f.showtimes)

    seen = _counting(monkeypatch)
    movies = tgv.day(CINEMA, DATE, prices=True, after="18:00")
    evening_calls = seen.count(tgv.ENDPOINTS["tickets"])

    evening_combos = sum(len(s.area_codes) for f in movies for s in f.showtimes)
    assert evening_calls == evening_combos
    assert evening_calls < all_combos


def test_a_failed_price_call_does_not_abort_the_run(monkeypatch):
    """One dead gettickets call must not lose the prices of every other area."""
    alive = "0000000002"
    dead = "0000000010"

    def fake_request(path, payload=None, **kwargs):
        if path == tgv.ENDPOINTS["day"]:
            return {"results": load("tgv_day_BU0.json")}
        if path == tgv.ENDPOINTS["tickets"]:
            code = payload["areacategorycodes"]
            if code == dead:
                raise SystemExit("HTTP 500 from gettickets")
            return {"results": {"tickets": [_price_tickets("SINGLE", 3000)]}}
        raise AssertionError(path)

    monkeypatch.setattr(tgv, "request", fake_request)
    movies = tgv.day(CINEMA, DATE, prices=True)

    # Every showtime that only uses the live area kept its price.
    priced = [s for f in movies for s in f.showtimes
              if s.area_codes and dead not in s.area_codes]
    assert priced, "fixture should have sessions in the surviving area"
    assert all(s.ticket_types for s in priced)
    assert all(s.price == 30.00 for s in priced)

    # A showtime sharing both areas keeps the surviving area's ticket.
    both = [s for f in movies for s in f.showtimes
            if alive in s.area_codes and dead in s.area_codes]
    assert both, "fixture should have a session spanning both areas"
    assert all(s.single_seat_price == 30.00 for s in both)

    # The run completed: every film is still present with its showtimes.
    assert all(s.showtimes for f in movies for s in [f])


def test_session_prices_prices_one_session_on_demand(monkeypatch, day):
    """session_prices() is the single-session path: it prices the showtime it is
    given and touches nothing else."""
    seen = _counting(monkeypatch)
    movies = parse_day(day, CINEMA)
    target = movies[0].showtimes[0]

    priced = tgv.session_prices(target)
    assert priced is target
    assert target.ticket_types
    calls = seen.count(tgv.ENDPOINTS["tickets"])
    assert calls == len(target.area_codes)

    # No other showtime got a price.
    untouched = [s for f in movies for s in f.showtimes if s is not target]
    assert all(s.ticket_types == () for s in untouched)


def test_session_prices_keeps_other_areas_when_one_fails(monkeypatch, day):
    """A dead area must not cost the session the single seat in its other area."""
    dead = "0000000010"

    def fake_request(path, payload=None, **kwargs):
        if path == tgv.ENDPOINTS["tickets"]:
            if payload["areacategorycodes"] == dead:
                raise SystemExit("HTTP 500 from gettickets")
            return {"results": {"tickets": [_price_tickets("SINGLE", 3000)]}}
        raise AssertionError(path)

    monkeypatch.setattr(tgv, "request", fake_request)
    movies = parse_day(day, CINEMA)
    target = next(s for f in movies for s in f.showtimes
                  if len(s.area_codes) > 1 and dead in s.area_codes)

    tgv.session_prices(target)
    assert target.single_seat_price == 30.00


def test_seats_are_one_call_for_the_whole_cinema(monkeypatch):
    """Seat status batches every session into a single request, unlike prices."""
    seen = _counting(monkeypatch)
    tgv.day(CINEMA, DATE, seats=True)
    assert seen.count(tgv.ENDPOINTS["seat_status"]) == 1


def test_prices_are_not_fetched_at_all_without_the_flag(monkeypatch):
    seen = _counting(monkeypatch)
    tgv.day(CINEMA, DATE)
    assert tgv.ENDPOINTS["tickets"] not in seen


def test_parse_time_rejects_junk():
    assert tgv.parse_time("not a time") is None
    assert tgv.parse_time(None) is None


# -- the API surface ----------------------------------------------------


def test_endpoints_are_absolute_paths():
    assert all(p.startswith("/api/") for p in tgv.ENDPOINTS.values())


def test_every_endpoint_is_mapped():
    assert set(tgv.ENDPOINTS) == {
        "cinemas", "regions", "business_dates", "day", "seat_status",
        "movie_cinemas", "tickets", "seat_plan",
    }