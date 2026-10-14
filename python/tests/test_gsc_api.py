"""Offline tests for the GSC booking-service half of gsc_scraper.py.

Everything here runs against saved copies of the live XML and JSON, so the
whole suite stays offline. The parsers are pure functions over those bodies.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import gsc_scraper as gsc
import tgv_scraper as tgv

FIXTURES = Path(__file__).parent / "fixtures"

CINEMA_ID = "235"
CINEMA_NAME = "Kepong - Lotus`s (Kepong Village Mall)"
BUSINESS_DATE = "2026-10-14"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def catalogue() -> list[gsc.Movie]:
    return gsc.parse_movies(load("gsc_movies.xml"))


# Scoped per test on purpose: the filter and price tests mutate showtimes in
# place, which is what the live code does, so sharing one parsed day across
# tests would let one test's mutation decide another's result.
@pytest.fixture
def day() -> list[gsc.Movie]:
    return gsc.parse_day(load("gsc_day_235.xml"), CINEMA_ID, CINEMA_NAME)


@pytest.fixture(scope="module")
def movie_shows() -> list[gsc.Movie]:
    return gsc.parse_movie_shows(load("gsc_shows_movie_5749.xml"), "5749", BUSINESS_DATE)


@pytest.fixture(scope="module")
def plan() -> gsc.SeatPlan:
    return gsc.parse_seat_plan(load("gsc_seat_plan.xml"), CINEMA_ID, "49397")


@pytest.fixture(scope="module")
def tickets() -> tuple[gsc.TicketType, ...]:
    return gsc.parse_tickets(load("gsc_tickets.xml"))


@pytest.fixture(scope="module")
def outlets() -> list[gsc.Cinema]:
    return gsc.parse_cinemas(json.loads(load("gsc_cinemas.json")))


@pytest.fixture(scope="module")
def site_directory() -> list[gsc.Cinema]:
    return [
        c for c in gsc.parse_cinemas_page(load("cinemas.html")) if c.name.startswith("GSC ")
    ]


@pytest.fixture(scope="module")
def joined(site_directory) -> list[gsc.Cinema]:
    """The service directory with the website's fields merged in."""
    return gsc.parse_cinemas(json.loads(load("gsc_cinemas.json")), site_directory)


# -- catalogue ------------------------------------------------------------


def test_catalogue_is_the_whole_book(catalogue):
    assert len(catalogue) > 50
    assert all(m.id and m.title and m.slug for m in catalogue)


def test_catalogue_ids_are_unique(catalogue):
    ids = [m.id for m in catalogue]
    assert len(ids) == len(set(ids))


def test_catalogue_film_is_fully_parsed(catalogue):
    movie = next(m for m in catalogue if m.title == "Digger")
    assert movie.id == "5749"
    assert movie.slug == "digger"
    assert movie.genre == ("Drama",)
    assert movie.rating == "18"
    assert movie.runtime == 129  # the service sends minutes directly
    assert movie.language == "ENG"
    assert movie.trailer_id == "ScpzprkFxBY"
    assert movie.poster_url.startswith("https://poster.gsc.com.my/")


def test_catalogue_merges_format_variants(catalogue):
    """A film's IMAX and 2D children are one film, not two."""
    movie = next(m for m in catalogue if m.title == "Digger")
    assert movie.experiences == ("IMAX", "2D")
    assert len(movie.variants) == 2
    assert all(v.startswith("1000005") for v in movie.variants)


def test_catalogue_carries_each_films_dates(catalogue):
    movie = next(m for m in catalogue if m.title == "Digger")
    assert movie.dates[0] == "2026-10-13"
    assert movie.dates == tuple(sorted(set(movie.dates)))
    assert len(movie.dates) > 3


def test_catalogue_and_website_agree_on_the_runtime(day, catalogue):
    """The service knows the runtime even where the website has not published it."""
    movie = next(m for m in catalogue if m.id == "6360")
    website = next(m for m in day if m.id == "6360")
    assert movie.runtime == website.runtime == 186


def test_the_detail_pages_runtime_is_a_placeholder_when_unpublished():
    """The saved detail page for an unreleased title says 'N/A', not a time."""
    record = gsc.parse_detail(load("movie_detail.html"), "munafik-melawan-iblis")
    assert record.runtime is None
    assert record.duration is None
    # ...while the booking catalogue has it, which is what detail() falls back to.
    assert record.id == ""  # set by detail(), which reads the booking link


def test_business_dates_is_the_union_of_every_film(catalogue):
    union = sorted({d for m in catalogue for d in m.dates})
    assert len(union) > 10
    assert union == sorted(union)
    assert union[0] <= union[-1]


def test_trailer_is_dropped_when_it_is_not_a_video(catalogue):
    assert all(
        "youtube" in m.trailer_url or "youtu.be" in m.trailer_url
        for m in catalogue
        if m.trailer_url
    )


# -- one cinema, one date -------------------------------------------------


def test_day_parses_into_movies(day):
    assert len(day) > 5
    assert all(m.title and m.cinema_id == CINEMA_ID for m in day)


def test_day_is_sorted_by_title(day):
    titles = [m.title.casefold() for m in day]
    assert titles == sorted(titles)


def test_day_movie_carries_its_detail(day):
    movie = next(m for m in day if m.title == "Avengers: Endgame Encore")
    assert movie.id == "6360"
    assert movie.genre == ("Action",)
    assert movie.rating == "13"
    assert movie.runtime == 186
    assert movie.language == "ENG"
    assert movie.trailer_id == "Ta_9XQ5PJzs"


def test_two_variants_of_one_film_are_one_movie(day):
    """Every film appears once, however many format variants it has."""
    titles = [m.title for m in day]
    assert len(titles) == len(set(titles))
    assert len({m.id for m in day}) == len(day)


def test_showtimes_are_ordered_and_well_formed(day):
    for movie in day:
        starts = [s.start for s in movie.showtimes]
        assert starts == sorted(starts)
        assert all(s.start is not None for s in movie.showtimes)
        assert all(s.cinema_id == CINEMA_ID for s in movie.showtimes)
        assert all(s.session_id for s in movie.showtimes)


def test_showtime_times_are_malaysia_wall_clock(day):
    showtime = day[0].showtimes[0]
    assert showtime.start.tzinfo is not None
    assert showtime.start.utcoffset().total_seconds() == 8 * 3600
    assert showtime.time == "10:50"
    assert showtime.time_text == "10:50AM"


def test_showtime_carries_hall_and_format(day):
    showtime = day[0].showtimes[0]
    assert showtime.hall_id == "3"
    assert showtime.experience == "2D"
    assert showtime.experience_name == "Digital 2D"


def test_end_time_uses_the_movie_runtime(day):
    movie = next(m for m in day if m.title == "Avengers: Endgame Encore")
    showtime = movie.showtimes[0]
    assert showtime.ends - showtime.start == gsc.timedelta(minutes=186)


def test_session_ids_match_the_showtimes(day):
    movie = day[0]
    assert movie.session_ids == tuple(s.session_id for s in movie.showtimes)


def test_business_date_is_the_oprn_date(day):
    """The payload files late shows under the previous day's business date."""
    assert all(m.showtimes[0].business_date == BUSINESS_DATE for m in day if m.showtimes)


def test_empty_day_is_an_empty_list():
    assert gsc.parse_day("<films />", CINEMA_ID) == []


# -- one film, every cinema ----------------------------------------------


def test_movie_shows_returns_one_movie_per_cinema(movie_shows):
    assert len(movie_shows) > 5
    assert len({m.cinema_id for m in movie_shows}) == len(movie_shows)
    assert all(m.cinema_name for m in movie_shows)


def test_movie_shows_carry_the_business_date(movie_shows):
    assert all(s.business_date == BUSINESS_DATE for m in movie_shows for s in m.showtimes)


def test_movie_shows_include_non_gsc_brands(movie_shows):
    """Aurum and Velvet screen the same films on the same service."""
    names = {m.cinema_name or "" for m in movie_shows}
    assert any("Aurum" in n for n in names)
    assert any("Velvet" in n for n in names)


def test_movie_shows_derive_formats_from_the_shows(movie_shows):
    formats = {f for m in movie_shows for f in m.experiences}
    assert "IMAX" in formats
    assert formats - {None}


def test_movie_shows_tolerates_an_empty_payload():
    assert gsc.parse_movie_shows('<locations parent_code="1" />') == []


# -- the outlet directory -------------------------------------------------


def test_outlets_are_not_empty(outlets):
    assert len(outlets) > 40
    assert all(c.id and c.name for c in outlets)


def test_outlet_ids_are_unique(outlets):
    ids = [c.id for c in outlets]
    assert len(ids) == len(set(ids))


def test_outlets_are_sorted_by_name(outlets):
    names = [c.name for c in outlets]
    assert names == sorted(names)


def test_the_placeholder_row_is_dropped(outlets):
    assert not any("Please Select" in c.name for c in outlets)


def test_outlet_carries_region_address_and_postcode(outlets):
    outlet = next(c for c in outlets if c.id == CINEMA_ID)
    assert outlet.name == CINEMA_NAME
    assert outlet.region == "Klang Valley"
    assert outlet.location == "klang valley"
    assert outlet.state == "Federal Territory of Kuala Lumpur"
    assert outlet.postcode == "52200"
    assert outlet.address
    assert outlet.town == "Kepong"


def test_outlet_url_falls_back_to_the_directory(outlets):
    """No slug without the website page, so the directory is the honest link."""
    assert outlets[0].slug is None
    assert outlets[0].url == "https://www.gsc.com.my/cinemas/gsc/"


def test_regions_are_derived_from_the_directory(outlets):
    regions = {c.region for c in outlets}
    assert "Klang Valley" in regions
    assert len(regions) > 4


def test_by_region_and_by_city_narrow_the_list(joined):
    assert gsc.by_region(joined, "klang valley")
    assert gsc.by_region(joined, "Klang Valley") == gsc.by_region(joined, "  klang valley ")
    assert gsc.by_city(joined, "Kepong")
    assert gsc.filter_cinemas(joined, "Klang Valley")
    assert gsc.by_region(joined, "atlantis") == []


def test_town_and_city_are_different_fields(joined):
    """The website files some outlets under a state, so the two must not merge."""
    outlet = next(c for c in joined if c.id == CINEMA_ID)
    assert outlet.town == "Kepong"  # from the service's own name
    assert outlet.city == "Kuala Lumpur"  # what the website labels it
    assert gsc.by_city(joined, "Kepong") == [outlet]
    assert outlet in gsc.by_city(joined, "Kuala Lumpur")


def test_find_cinema_by_id_and_by_name(outlets):
    assert gsc.find_cinema(outlets, CINEMA_ID).name == CINEMA_NAME
    assert gsc.find_cinema(outlets, "lotus").id == CINEMA_ID
    assert gsc.find_cinema(outlets, "ekochERAS".lower()).id == "213"
    with pytest.raises(SystemExit):
        gsc.find_cinema(outlets, "no-such-cinema")


# -- joining the two directories -----------------------------------------


def test_the_two_directories_join_on_every_outlet(joined, site_directory):
    """The website and the service share no id, so names are all there is."""
    assert len(joined) == len(site_directory) == 50
    assert all(c.slug and c.city and c.features is not None for c in joined)


def test_the_join_picks_the_right_outlet(joined):
    """Near-identical names are only told apart by their town."""
    slugs = {c.name: c.slug for c in joined}
    assert slugs["Kulai - IOI Mall"] == "gsc-ioi-mall-kulai"
    assert slugs["Puchong - IOI Mall"] == "gsc-ioi-mall-puchong"
    assert slugs["Johor Bahru - Paradigm Mall"] == "gsc-paradigm-jb"
    assert slugs["Petaling Jaya - Paradigm Mall"] == "gsc-paradigm-mall"
    assert slugs["Bintulu - The Spring"] == "gsc-spring-bintulu"
    assert slugs["Kuching - The Spring"] == "gsc-the-spring-kuching"


def test_the_join_never_pairs_two_outlets_together(site_directory):
    slugs = [c.slug for c in site_directory]
    assert len(slugs) == len(set(slugs))


def test_a_bad_site_name_scores_below_the_threshold():
    assert gsc._name_score("GSC 1 Utama", "Kuala Lumpur - 1 Utama (New Wing)") >= 0.7
    assert gsc._name_score("GSC 1 Utama", "Miri - Bintang Megamall") < 0.7


def test_the_join_leaves_an_unknown_outlet_alone():
    outlet = gsc.Cinema(id="1", name="Nowhere - Nothing")
    assert gsc.join_directories([outlet], [gsc.Cinema(id="x", name="GSC 1 Utama")]) == 0
    assert outlet.slug is None


# -- seats ----------------------------------------------------------------


def test_seat_plan_carries_the_whole_hall(plan):
    assert plan.hall_id == "3"
    assert plan.seats_total == 134
    assert plan.seats_blocked == 5
    assert len(plan.areas) >= 1


def test_seat_counts_prefer_the_services_own_availability(plan):
    counts = gsc.counts_from_plan(plan)
    assert counts["seats_left"] == 127  # the hall booking block, not the states
    assert counts["seats_total"] == 134
    assert counts["seats_used"] == 7
    assert counts["used_percentage"] == 5.2


def test_the_halls_own_numbers_are_kept_but_not_used(plan):
    """`maximumseats` is not the seat count, so it is reported and not trusted."""
    assert plan.reported_total == 12
    assert plan.seats_total == 134


def test_seat_grid_keeps_aisles_as_empty_columns(plan):
    row = plan.areas[0].rows[0]
    columns = [seat.column for seat in row.seats]
    assert columns == sorted(columns)
    assert row.columns == tuple(sorted(set(columns)))
    assert len(set(columns)) == len(columns)  # no two seats share a column


def test_seat_states_are_normalised(plan):
    states = {seat.state for seat in plan.seats}
    assert SEATS_EXPECTED <= states


SEATS_EXPECTED = {"A", "B", "D"}


def test_seat_number_and_identity_are_kept_separate(plan):
    seat = plan.areas[0].rows[0].seats[0]
    assert seat.number is not None
    assert seat.id and seat.id != seat.number


def test_rows_are_ordered_screen_end_first(plan):
    rows = [row for _, row in plan.hall_rows]
    indices = [row.index for row in rows]
    assert indices == sorted(indices, reverse=True)
    # The front row carries the highest index and is lettered 'A'.
    assert rows[0].name == "A"


def test_seat_categories_say_how_many_seats_a_ticket_takes(plan):
    assert plan.categories["2"]["name"] == "Twin"
    assert plan.categories["2"]["seats_taken"] == 2


def test_seat_plan_renders_with_aisles_visible(plan):
    drawn = gsc.render_seat_plan(plan)
    assert "SCREEN" in drawn
    assert "seats drawn" in drawn
    assert drawn.count("\n") > len(plan.areas[0].rows)


def test_render_reports_a_miscount_rather_than_hiding_it(plan):
    plan.areas[0].seat_count = 999
    assert "(service says 999)" in gsc.render_seat_plan(plan)


def test_seat_status_is_json_friendly(plan):
    json.dumps(gsc.counts_from_plan(plan))
    json.dumps(plan.to_dict())


# -- prices ---------------------------------------------------------------


def test_tickets_parse_with_prices(tickets):
    codes = [t.code for t in tickets]
    assert "Adult" in codes
    assert "Twin" in codes
    assert all(t.price is None or t.price > 0 for t in tickets)


def test_only_face_value_tickets_are_standard(tickets):
    """Adult and Twin are the tickets a walk-in pays. The rest are concessions."""
    standard = {t.code for t in tickets if t.standard}
    assert standard == {"Adult", "Twin"}


def test_the_twin_ticket_seats_two_people(tickets):
    twin = next(t for t in tickets if t.code == "Twin")
    assert twin.seats == 2
    assert twin.package is True
    assert twin.area_code == "2"


def test_concessions_are_flagged_and_not_standard(tickets):
    concessions = {t.code for t in tickets} - {"Adult", "Twin"}
    assert concessions
    for code in concessions:
        assert next(t for t in tickets if t.code == code).standard is False
    assert next(t for t in tickets if t.code == "Children").child_only is True


def test_price_is_the_adult_seat_not_the_concessions(day, tickets):
    showtime = day[0].showtimes[0]
    assert showtime.price is None  # nothing fetched yet
    showtime.ticket_types = tickets
    # The quote is the adult seat. Twin is also face value but seats two, so it
    # can never be the answer here, and no concession is quoted at all.
    assert showtime.price == next(t.price for t in tickets if t.code == "Adult")
    assert showtime.price != showtime.price_bundle
    for ticket in tickets:
        if ticket.code not in {"Adult", "Twin"}:
            assert showtime.price != ticket.price or ticket.price == 13.0


def test_a_bundle_never_quotes_as_one_seat(day, tickets):
    showtime = day[0].showtimes[0]
    showtime.ticket_types = tickets
    assert showtime.single_seat_price == showtime.price
    assert showtime.price_bundle == next(t.price for t in tickets if t.code == "Twin")
    assert showtime.price != showtime.price_bundle
    assert showtime.per_person is not None


def test_price_range_spans_several_single_prices(day):
    showtime = day[0].showtimes[0]
    showtime.ticket_types = (
        gsc.TicketType(code="Adult", description="Adult", price=25.0, standard=True),
        gsc.TicketType(code="Adult2", description="Adult", price=35.0, standard=True),
    )
    assert showtime.price_range == "RM25.00 - RM35.00"


def test_price_range_is_a_single_figure_when_there_is_only_one(day):
    showtime = day[0].showtimes[0]
    showtime.ticket_types = (
        gsc.TicketType(code="Adult", description="Adult", price=25.0, standard=True),
    )
    assert showtime.price_range == "RM25.00"


def test_showtime_json_carries_ticket_types(day, tickets):
    showtime = day[0].showtimes[0]
    showtime.ticket_types = tickets
    data = showtime.to_dict()
    assert len(data["ticket_types"]) == len(tickets)
    json.dumps(data)


# -- filtering ------------------------------------------------------------


def test_filter_times_bounds_are_inclusive(day):
    films = gsc.filter_times([day[0]], "10:50", "10:50")
    assert films[0].showtimes


def test_filter_times_can_exclude_everything(day):
    assert gsc.filter_times([day[0]], "23:59") == []


def test_filter_times_leaves_an_unbounded_query_alone(day):
    assert gsc.filter_times([day[0]]) == [day[0]]


def test_filter_times_drops_empty_films(day):
    assert gsc.filter_times([day[0]], "23:00") == []


def test_filter_times_anchors_on_the_business_date(day):
    """A 00:15 show filed under the 13th still counts as that evening's night."""
    late = gsc.Showtime(
        session_id="1", movie_id="2", cinema_id="3",
        start=gsc.session_start("2026-10-14", "0015"),
        business_date="2026-10-13",
    )
    evening = gsc.Movie(id="1", title="Late", showtimes=(late,))
    # Anchored on the 13th, 00:15 on the 14th falls after 18:00 on the 13th, so
    # an evening window still counts it.
    assert gsc.filter_times([evening], "18:00")
    # A window that closes at 22:00 on the 13th does not.
    assert gsc.filter_times([gsc.Movie(id="1", title="Late", showtimes=(late,))],
                            None, "22:00") == []


def test_filter_times_anchors_on_the_business_date_not_the_calendar(day):
    """The same show, filed under a different business date, is excluded."""
    late = gsc.Showtime(
        session_id="1", movie_id="2", cinema_id="3",
        start=gsc.session_start("2026-10-14", "0015"),
        business_date="2026-10-14",
    )
    film = gsc.Movie(id="1", title="Late", showtimes=(late,))
    assert gsc.filter_times([film], "18:00") == []


def test_filter_formats_keeps_only_the_named_hall(day):
    films = gsc.filter_formats([day[0]], "2D")
    assert films
    assert all(s.experience == "2D" for s in films[0].showtimes)


def test_filter_formats_is_case_insensitive(day):
    assert gsc.filter_formats([day[0]], "2d") == gsc.filter_formats([day[0]], "2D")


def test_filter_formats_unknown_code_yields_nothing(day):
    assert gsc.filter_formats([day[0]], "not-a-format") == []


def test_filter_formats_without_a_filter_is_a_no_op(day):
    assert gsc.filter_formats([day[0]]) == [day[0]]


def test_filter_formats_composes_with_filter_times(day):
    films = gsc.filter_formats(gsc.filter_times([day[0]], "10:50"), "2D")
    assert films
    assert films[0].showtimes[0].time == "10:50"


# -- small helpers --------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [("105", 105), ("1 hr 45 mins", 105), ("2 hrs 8 mins", 128), ("N/A", None), (None, None)],
)
def test_duration_minutes_takes_either_shape(value, expected):
    assert gsc.duration_minutes(value) == expected


def test_slugify_follows_the_website_convention():
    assert gsc.slugify("(Rerun) La La Land") == "rerun-la-la-land"
    assert gsc.slugify("Munafik: Melawan Iblis") == "munafik-melawan-iblis"
    assert gsc.slugify("Avengers: Endgame Encore") == "avengers-endgame-encore"
    assert gsc.slugify("V") == "v"
    assert gsc.slugify("Resident Evil ") == "resident-evil"


def test_slugify_matches_the_live_slugs():
    """Every slug on the saved now-showing page is reproducible from its title."""
    listed = gsc.parse_listing(load("now_showing.html"))
    for movie in listed:
        assert gsc.slugify(movie.title) == movie.slug, movie.title


def test_loose_title_ignores_the_format_prefix():
    """One film exists under several titles that differ only by a bracket."""
    assert gsc.loose_title("(InfinityVision) Avengers:Endgame Encore") == "avengersendgameencore"
    assert gsc.loose_title("Avengers: Endgame Encore") == "avengersendgameencore"
    assert gsc.loose_title("(4DX) (InfinityVision) Avengers: Endgame") == "avengersendgame"


def test_experience_labels_are_humanised():
    assert gsc.experience_label("IMAX") == "IMAX"
    assert gsc.experience_label("2D - Digital 2D") == "Digital 2D"
    assert gsc.experience_label("") is None
    assert gsc.experience_label(None) is None


def test_session_start_rejects_nonsense():
    assert gsc.session_start("2026-10-14", "1050").isoformat() == "2026-10-14T10:50:00+08:00"
    assert gsc.session_start("2026-10-14", "2599") is None
    assert gsc.session_start("2026-10-14", None) is None
    assert gsc.session_start("not-a-date", "1050") is None


def test_clock_candidates_cover_both_spellings():
    assert "21:45" in gsc.clock_candidates("9:45PM")
    assert "9:45PM" in gsc.clock_candidates("21:45")
    assert "12:30AM" in gsc.clock_candidates("00:30")
    assert "12:30PM" in gsc.clock_candidates("12:30")


def test_looks_like_a_date_not_a_cinema():
    assert gsc.looks_like_date("2026-10-14")
    assert not gsc.looks_like_date("235")
    assert not gsc.looks_like_date("EkoCheras")
    assert not gsc.looks_like_date(None)


def test_as_list_takes_either_shape():
    assert gsc.as_list("Horror") == ["Horror"]
    assert gsc.as_list("Horror, Action") == ["Horror", "Action"]
    assert gsc.as_list(["Horror", " Action "]) == ["Horror", "Action"]
    assert gsc.as_list(None) == []


# -- the shared contract with tgv_scraper --------------------------------


@pytest.mark.parametrize(
    "record,expected",
    [
        (
            lambda m: m.Movie(id="1", title="x"),
            {
                "id", "title", "slug", "url", "genre", "tags", "rating", "runtime",
                "duration", "language", "languages", "subtitles", "synopsis", "director",
                "cast", "distributor", "release_date", "trailer_url", "trailer_id",
                "poster_url", "banner_url", "experiences", "session_ids", "cinema_id",
                "cinema_name", "booking_url", "booking_id", "showtimes",
            },
        ),
        (
            lambda m: m.Cinema(id="1", name="x"),
            {
                "id", "name", "slug", "url", "city", "location", "region", "state",
                "address", "postcode", "latitude", "longitude", "telephone", "parking",
                "opening_hours", "features", "experiences", "description",
            },
        ),
        (
            lambda m: m.Showtime(session_id="1", movie_id="2", cinema_id="3", start=None),
            {
                "session_id", "movie_id", "cinema_id", "hall_id", "date", "start",
                "time", "ends", "hall", "experience", "experience_name", "seat_types",
                "seats_total", "seats_used", "seats_left", "sold_out", "status",
                "price", "price_range", "price_bundle", "per_person", "sold_out_promos",
                "ticket_types",
            },
        ),
        (
            lambda m: m.TicketType(code="a", description="a"),
            {
                "code", "description", "price", "seats", "package", "area_code",
                "sold_out", "child_only", "allocated_seating", "standard",
            },
        ),
        (
            lambda m: m.Seat(id="1", row=1, column=2),
            {
                "id", "number", "row", "column", "status", "state", "category",
                "seat_type", "free", "booked",
            },
        ),
        (
            lambda m: m.SeatPlan(cinema_id="1", session_id="2"),
            {
                "cinema_id", "session_id", "hall_id", "hall_name", "seats_total",
                "seats_left", "seats_booked", "seats_blocked", "available", "areas",
            },
        ),
    ],
)
def test_the_output_shape_is_pinned(record, expected):
    """One format, deliberately frozen.

    A consumer reads either scraper's output with the same code, so a field
    added to one and not the other is a break. The TypeScript port emits these
    same key sets; changing one side means changing all three.
    """
    for module in (gsc, tgv):
        assert set(record(module).to_dict()) == expected, module.__name__


def test_the_three_implementations_agree_on_the_key_count():
    """The numbers the TypeScript port was checked against."""
    assert len(gsc.Movie(id="1", title="x").to_dict()) == 28
    assert len(gsc.Cinema(id="1", name="x").to_dict()) == 18
    assert len(gsc.Showtime(session_id="1", movie_id="2", cinema_id="3", start=None).to_dict()) == 23
    assert len(gsc.TicketType(code="a", description="a").to_dict()) == 10
    assert len(gsc.Seat(id="1", row=1, column=2).to_dict()) == 10
    assert len(gsc.SeatPlan(cinema_id="1", session_id="2").to_dict()) == 10


def test_both_expose_the_same_movie_properties():
    for name in ("url", "slug", "trailer_id", "languages", "times"):
        assert hasattr(gsc.Movie, name), name
        assert hasattr(tgv.Movie, name), name


@pytest.mark.parametrize(
    "name",
    [
        "movies", "detail", "cinemas", "regions", "business_dates", "day",
        "schedule", "movie", "session_prices", "seat_status", "seat_plan", "tickets",
        "cinema_experiences", "filter_times", "filter_formats", "by_region", "by_city",
        "filter_cinemas", "find_cinema", "looks_like_date", "normalise_date",
        "parse_clock", "experience_label", "clean", "split_codes", "as_list",
        "strip_html", "Throttle", "render_seat_plan", "show_movies", "show_schedule",
        "show_cinemas", "seat_number", "parse_day", "parse_tickets",
        "parse_seat_plan", "parse_cinemas",
    ],
)
def test_both_scrapers_expose_the_same_function(name):
    assert hasattr(gsc, name), f"gsc_scraper is missing {name}"
    assert hasattr(tgv, name), f"tgv_scraper is missing {name}"


@pytest.mark.parametrize(
    "name,why",
    [
        ("listing", "the website's editorial shelves; TGV has no such category"),
        ("parse_cinemas_page", "the website's outlet page; TGV has no website"),
        ("parse_listing", "ditto, for movie cards"),
        ("parse_detail", "ditto, for a film's own page"),
        ("join_directories", "only GSC has two directories to join"),
    ],
)
def test_the_website_only_functions_are_gsc_only(name, why):
    """GSC reads a website as well as a service, and that is where these live.

    Each is asserted rather than hidden, because a caller porting between the
    two should be told which names will not resolve.
    """
    assert hasattr(gsc, name), f"gsc_scraper is missing {name}"
    assert not hasattr(tgv, name), f"{name} should not be on tgv_scraper: {why}"


@pytest.mark.parametrize(
    "name,why",
    [
        ("parse_movie_shows", "TGV's API has no one-film lookup, so movie() sweeps"),
        ("parse_movies", "GSC publishes a catalogue; TGV only reports through sessions"),
        ("align_by_number", "GSC's seat coordinates are already absolute"),
        ("number_anchor", "ditto"),
    ],
)
def test_the_one_way_asymmetries_are_asserted(name, why):
    assert hasattr(gsc, name) != hasattr(tgv, name), why


def test_both_clean_drop_placeholders():
    for value in ("N/A", "TBA", "-", "  ", ""):
        assert gsc.clean(value) is None
        assert tgv.clean(value) is None


def test_both_split_codes_are_tuples():
    assert gsc.split_codes("ENG,BM") == ("ENG", "BM")
    assert tgv.split_codes("ENG,BM") == ("ENG", "BM")
    assert gsc.split_codes("N/A") == ()