"""Scrape nationwide movie showtimes from TGV Cinemas Malaysia.

This script uses TGV's public boxoffice/movies web API. It does not use a
login or a token. It uses these endpoints:
  - getmoviepagemovielist                 -> list of movie categories (nowselling, comingsoon, ...).
  - movielist/{listkey}                   -> movies in a category (recid, title, movie info).
  - getcinemas                            -> nationwide cinema directory.
  - moviesession_getlite                  -> nationwide showtimes for one movie/date.
  - moviesession_getseatstatus            -> aggregate seat occupancy for one session.
  - moviesession_getseatplan              -> full per-seat map for one session.
  - cart_create + moviesession_gettickets -> ticket pricing for one session.
  - search_screenings() / search_screenings_for_group()
                                           -> find screenings by movie/date/time/location.
                                              They attach pricing and best-seat recommendations.

Run with no arguments to scrape nationwide showtimes. Run `seats <cinemaid> <sessionid>`
to look up the live seat map for one showtime. The cinemaid and sessionid come from
a prior scrape, or from search_screenings().
"""

import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

API_BASE = "https://api.tgv.com.my/api"
HEADERS = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
REQUEST_DELAY = 0.3  # seconds between requests; this avoids overloading TGV's servers
DAYS_AHEAD = 7  # scrape today plus the next 6 days of showtimes
OUT_DIR = Path(__file__).resolve().parent / "out"


def get_cinemas(session):
    resp = session.post(
        f"{API_BASE}/cinemas/v1/getcinemas",
        json={"startrowindex": 0, "maxrows": 200},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()

    cinemas = {}
    for c in resp.json()["results"]["cinemas"]:
        info = c.get("extdata", {}).get("cinema", {})
        cinemas[c["itemkey"]] = {
            "name": c["name"],
            "address": info.get("address"),
            "state": info.get("location"),
            "latitude": info.get("latitude"),
            "longitude": info.get("longitude"),
        }
    return cinemas


def get_movies(session, listkey="nowselling"):
    resp = session.get(
        f"{API_BASE}/movies/v1/movielist/{listkey}", headers=HEADERS, timeout=15
    )
    resp.raise_for_status()

    movies = []
    for m in resp.json()["results"]["movies"]:
        info = m.get("extdata", {}).get("movieinfo", {})
        movies.append({
            "code": m["recid"],
            "title": m["name"],
            "itemkey": m["itemkey"],
            "rating": info.get("rating"),
            "genre": info.get("genre"),
            "language": info.get("lang"),
            "duration": info.get("runtimemins"),
            "release_date": info.get("releasedatemy"),
        })
    return movies


def get_showtimes(session, movie_code, businessday, cinema_ids):
    resp = session.post(
        f"{API_BASE}/boxoffice/v1/moviesession_getlite",
        json={"businessday": businessday, "movieid": movie_code, "cinemaid": cinema_ids},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()

    cinemas = resp.json()["results"]["cinemas"]
    return {cid: data["experiences"] for cid, data in cinemas.items() if data["experiences"]}


def get_seat_occupancy(session, cinemaid, sessionid):
    """Occupancy totals for one session. It gives the headline numbers only, no seat map."""
    resp = session.post(
        f"{API_BASE}/boxoffice/v1/moviesession_getseatstatus",
        json={"cinemaid": cinemaid, "sessionid": [sessionid]},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["results"]["seatstatuslist"][0]


def get_seat_plan(session, cinemaid, sessionid):
    """Raw per-seat hall layout for one session, as returned by the API.

    The layout is areas -> rows -> seats. Each seat has a row/column position
    and a status of 0 (available) or 1 (sold). Use get_seat_map() to get the
    flattened version for the seat-scoring functions below.
    """
    resp = session.post(
        f"{API_BASE}/boxoffice/v1/moviesession_getseatplan",
        json={"cinemaid": cinemaid, "sessionid": sessionid},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["results"]["seatlayout"]


def print_seat_map(seatlayout):
    for area in seatlayout["areas"]:
        print(f"\n-- Area {area['number']}: {area['description']} ({area['numberOfSeats']} seats) --")
        for row in area["rows"]:
            if not row["seats"]:
                continue
            seats = sorted(row["seats"], key=lambda s: s["position"]["columnIndex"])
            line = " ".join("X" if s["status"] else "." for s in seats)
            label = row["physicalName"] or "?"
            print(f"{label:>3} {line}")
    print("\n(. = available, X = sold)")


def _row_depth_rank(row_name):
    """Alphabetical rank of a row letter: A=1, B=2, ..., Z=26, AA=27, ...

    The rank acts as a "distance from screen" axis. Unlike GSC's seat-status
    API, TGV's seat plan gives no explicit row-depth coordinate. It gives only
    a row letter and a per-area rowIndex that resets/overlaps across areas.

    This uses the standard cinema convention. The nearest row to the screen is
    Row A. Later letters move toward the back. Every TGV hall inspected
    matched this rule (couple/sofa seats sit in the last-lettered row). This
    rule does not apply to exotic layouts.
    """
    if not re.fullmatch(r"[A-Za-z]+", row_name or ""):
        return 0
    rank = 0
    for ch in row_name.upper():
        rank = rank * 26 + (ord(ch) - ord("A") + 1)
    return rank


def get_seat_map(session, cinemaid, sessionid):
    """Fetch the seat plan and flatten it into the row/col/id/status/x/y shape.
    recommend_seats() and the other seat functions use this shape.

    `status` has two values: "A" for available and "B" for booked. `x` is the
    seat's columnIndex. It stays consistent left-to-right across a hall's
    areas. For example, TWIN couple seats sit centered in the same column range
    as the SINGLE seats behind/around them. `y` is the row's alphabetical rank;
    see _row_depth_rank(). `category` is the areaCategoryCode. It matches a
    ticket's `seatcategory` field from get_ticket_pricing().
    """
    seatlayout = get_seat_plan(session, cinemaid, sessionid)

    seats = []
    for area in seatlayout["areas"]:
        for row in area["rows"]:
            row_name = row["physicalName"]
            if not row_name or not row["seats"]:
                continue
            y = _row_depth_rank(row_name)
            for seat in row["seats"]:
                seats.append({
                    "row": row_name,
                    "col": seat["id"],
                    "id": seat["id"],
                    "status": "B" if seat["status"] else "A",
                    "category": area["areaCategoryCode"],
                    "type": area["description"],
                    "x": seat["position"]["columnIndex"],
                    "y": y,
                })

    return {
        "cinemaid": cinemaid,
        "sessionid": sessionid,
        "total_seats": len(seats),
        "available_seats": sum(1 for s in seats if s["status"] == "A"),
        "seats": seats,
    }


def _seat_geometry(seat_map, ideal_row_fraction):
    """Shared reference points used to score each x/y position in the hall."""
    all_seats = seat_map["seats"]
    xs = [s["x"] for s in all_seats]
    ys = [s["y"] for s in all_seats]
    center_x = (min(xs) + max(xs)) / 2
    min_y, max_y = min(ys), max(ys)
    ideal_y = min_y + ideal_row_fraction * (max_y - min_y)
    return {
        "center_x": center_x,
        "ideal_y": ideal_y,
        "max_x_offset": max(abs(x - center_x) for x in xs) or 1,
        "max_y_offset": max(abs(ideal_y - min_y), abs(max_y - ideal_y)) or 1,
    }


def _score_position(x, y, geometry):
    centeredness = 1 - abs(x - geometry["center_x"]) / geometry["max_x_offset"]
    distance_quality = 1 - abs(y - geometry["ideal_y"]) / geometry["max_y_offset"]
    centeredness = max(0, min(1, centeredness))
    distance_quality = max(0, min(1, distance_quality))
    overall = 0.5 * centeredness + 0.5 * distance_quality
    return centeredness, distance_quality, overall


def recommend_seats(seat_map, top_n=5, ideal_row_fraction=0.6):
    """Rank available seats from a get_seat_map() result by viewing quality.

    The cinema-viewing rules of thumb used here:
      - Horizontal centering matters most. Off-center seats cause "keystoning"
        (the image looks skewed/stretched toward the far side).
      - The front few rows are too close (constant eye/neck movement, distorted
        edges of frame). The very back row is too far (screen looks small, more
        exposure to hallway/door noise). The best row sits at about 55-70% of
        the way back from the screen. `ideal_row_fraction` controls this spot.
        0 = front row, 1 = back row. The default 0.6 suits standard/2D halls.
        Try ~0.5 for IMAX/large-format screens where the image already fills
        peripheral vision from the middle of the hall.

    A smaller `y` means the seat is nearer to the screen. See _row_depth_rank()
    for the caveat. It infers the rank from row letters, not from a coordinate
    that TGV's API provides.

    Return each recommended seat with its own centering/distance scores plus
    an overall score in [0, 1], best seat first.
    """
    available = [s for s in seat_map["seats"] if s["status"] == "A"]
    if not available:
        return {"best_recommended_seat": None, "ranked_seats": []}

    geometry = _seat_geometry(seat_map, ideal_row_fraction)

    ranked = []
    for seat in available:
        centeredness, distance_quality, overall = _score_position(seat["x"], seat["y"], geometry)
        ranked.append({
            "seat": f"{seat['row']}{seat['col']}",
            "row": seat["row"],
            "col": seat["col"],
            "id": seat["id"],
            "how_centered": round(centeredness, 3),  # 1.0 = dead center
            "distance_from_screen": round(distance_quality, 3),  # 1.0 = ideal row depth
            "overall_score": round(overall, 3),
        })

    ranked.sort(key=lambda s: s["overall_score"], reverse=True)
    return {
        "best_recommended_seat": ranked[0],
        "ranked_seats": ranked[:top_n],
    }


def get_user_session(session, cinemaid):
    """Create an anonymous cart session scoped to `cinemaid`. get_ticket_pricing()
    requires the returned usersessionid."""
    resp = session.post(
        f"{API_BASE}/boxoffice/v1/cart_create",
        json={"cinemaid": cinemaid},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["results"]["usersessionid"]


def get_ticket_pricing(session, cinemaid, sessionid, seat_categories, usersessionid):
    """Ticket prices for one screening, broken down by ticket/seat type.

    `seat_categories` is an iterable of areaCategoryCode values. Pass each
    distinct `category` from a get_seat_map() result to get full pricing for
    the hall. `seatcategory` on each returned ticket matches that same field,
    so you can look up the cost of a given seat. `usersessionid` comes from
    get_user_session().
    """
    resp = session.post(
        f"{API_BASE}/boxoffice/v1/moviesession_gettickets",
        json={
            "cinemaid": cinemaid,
            "sessionid": sessionid,
            "areacategorycodes": ",".join(seat_categories),
            "usersessionid": usersessionid,
            "usetemplateuser": True,
        },
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()

    tickets = []
    for t in resp.json()["results"]["tickets"]:
        package_tickets = (t.get("packageContent") or {}).get("tickets") or []
        tickets.append({
            "type": t["description"],
            "price": t["priceInCents"] / 100,
            "tax": t["taxInCents"] / 100,
            "surcharge": (t.get("surchargeAmount") or 0) / 100,
            "seatcategory": t["areaCategoryCode"],  # matches a seat's "category" field
            "seats_taken": sum(p["quantity"] for p in package_tickets) or 1,  # e.g. Twin/couple seats use 2
        })
    return {"tickets": tickets}


def _find_available_blocks(seat_map, block_size, max_seat_gap=1):
    """Find each distinct contiguous block of `block_size` available seats.

    The seats must be physically adjacent. Adjacent means consecutive seat
    numbers and a normal seat-to-seat gap in `x`. A block never bridges across
    a center aisle. One row can yield several overlapping blocks; the function
    returns each block once.

    `max_seat_gap` defaults to 1, that is strictly consecutive columnIndex
    values. TGV's seat plan uses unit column increments within a row and a gap
    of 2+ for aisles. Unlike GSC's pixel-scale x coordinates, this plan has no
    slack to allow.
    """
    seats_by_row = {}
    for seat in seat_map["seats"]:
        seats_by_row.setdefault(seat["row"], []).append(seat)

    raw_blocks = []
    for row_seats in seats_by_row.values():
        row_seats.sort(key=lambda s: s["x"])

        run = []
        for seat in row_seats:
            is_adjacent_to_run = run and (seat["x"] - run[-1]["x"]) <= max_seat_gap
            if seat["status"] == "A" and (not run or is_adjacent_to_run):
                run.append(seat)
            else:
                run = [seat] if seat["status"] == "A" else []

            if len(run) >= block_size:
                for start in range(len(run) - block_size + 1):
                    raw_blocks.append(run[start:start + block_size])

    blocks, seen_seat_sets = [], set()
    for block in raw_blocks:
        key = tuple(s["id"] for s in block)
        if key in seen_seat_sets:  # the same window can appear in overlapping runs
            continue
        seen_seat_sets.add(key)
        blocks.append(block)
    return blocks


def recommend_seats_for_group(seat_map, group_size, top_n=5, ideal_row_fraction=0.6, max_seat_gap=1):
    """Find the best contiguous block of `group_size` available seats in one row.

    Individually "best" seats usually sit as scattered leftovers next to
    already-booked seats. They do not suit a group that wants to sit side by
    side. This instead finds every unbroken run of available, physically
    adjacent seats within each row. It then scores each possible
    `group_size`-wide window using the block's center position. It uses the
    same centering/distance logic as recommend_seats().

    Return the best block first, plus up to `top_n` alternatives. If no row
    has enough contiguous free seats, `best_recommended_block` is None. The
    group will need to split up or pick a different showtime.
    """
    geometry = _seat_geometry(seat_map, ideal_row_fraction)
    blocks = _find_available_blocks(seat_map, group_size, max_seat_gap)

    if not blocks:
        return {"best_recommended_block": None, "ranked_blocks": [], "group_size": group_size}

    ranked = []
    for block in blocks:
        avg_x = sum(s["x"] for s in block) / len(block)
        row_y = block[0]["y"]
        centeredness, distance_quality, overall = _score_position(avg_x, row_y, geometry)

        ranked.append({
            "row": block[0]["row"],
            "seats": [f"{s['row']}{s['col']}" for s in block],
            "seat_ids": [s["id"] for s in block],
            "how_centered": round(centeredness, 3),
            "distance_from_screen": round(distance_quality, 3),
            "overall_score": round(overall, 3),
        })

    ranked.sort(key=lambda b: b["overall_score"], reverse=True)
    return {
        "best_recommended_block": ranked[0],
        "ranked_blocks": ranked[:top_n],
        "group_size": group_size,
    }


def _parse_seat_label(label):
    """Split a seat label like "D13" into ("D", 13). It tolerates multi-letter
    rows and zero-padded seat numbers ("D01" -> ("D", 1))."""
    match = re.match(r"^\s*([A-Za-z]+)\s*0*(\d+)\s*$", label)
    if not match:
        raise ValueError(f"Could not parse seat label: {label!r}")
    return match.group(1).upper(), int(match.group(2))


def _find_seat(seat_map, seat_label):
    row, col = _parse_seat_label(seat_label)
    for seat in seat_map["seats"]:
        if seat["row"].upper() == row and int(seat["col"]) == col:
            return seat
    raise ValueError(f"Seat {seat_label!r} not found in this hall's seat map")


def recommend_seats_near(seat_map, target_seat, party_size=1, top_n=5, max_seat_gap=1):
    """Find seat(s) closest to `target_seat`. For example, a friend who
    already booked a seat, and you want to sit as near them as possible.

    `target_seat` is a seat label such as "D13". It only needs to exist in the
    hall's seat map. It is fine if its status is already "B"/booked. That is
    the expected case here.

    - `party_size=1`: ranks every available seat by straight-line distance
      to the target seat. The distance uses the seat grid's own x/y units. A
      seat in the same row and right next to them scores best. A seat one row
      in front or behind scores about the same as a seat one over.
    - `party_size>1`: instead finds contiguous blocks of `party_size` adjacent
      seats. It uses the same adjacency rule as recommend_seats_for_group. It
      ranks each block by its *average* distance to the target. The distance
      is the average across all seats in the block, not just its nearest seat.
      A block that grazes the target on one end ranks below a block that is
      close as a whole.

    Return the closest option first, plus up to `top_n` alternatives. Distance
    is in seat-grid units, not meters. Treat it as a ranking, not a
    measurement. The `y` axis is an inferred row-letter rank (see
    _row_depth_rank()), so cross-row distances are only approximate.
    """
    target = _find_seat(seat_map, target_seat)

    if party_size == 1:
        available = [s for s in seat_map["seats"] if s["status"] == "A" and s["id"] != target["id"]]
        ranked = [
            {
                "seat": f"{s['row']}{s['col']}",
                "row": s["row"],
                "col": s["col"],
                "id": s["id"],
                "distance": round(((s["x"] - target["x"]) ** 2 + (s["y"] - target["y"]) ** 2) ** 0.5, 3),
            }
            for s in available
        ]
        ranked.sort(key=lambda s: s["distance"])
        return {
            "target_seat": target_seat,
            "best_recommended_seat": ranked[0] if ranked else None,
            "ranked_seats": ranked[:top_n],
        }

    blocks = _find_available_blocks(seat_map, party_size, max_seat_gap)
    if not blocks:
        return {"target_seat": target_seat, "best_recommended_block": None, "ranked_blocks": [], "group_size": party_size}

    ranked = []
    for block in blocks:
        distances = [((s["x"] - target["x"]) ** 2 + (s["y"] - target["y"]) ** 2) ** 0.5 for s in block]
        ranked.append({
            "row": block[0]["row"],
            "seats": [f"{s['row']}{s['col']}" for s in block],
            "seat_ids": [s["id"] for s in block],
            "distance": round(sum(distances) / len(distances), 3),
            "closest_seat_distance": round(min(distances), 3),
        })

    ranked.sort(key=lambda b: b["distance"])
    return {
        "target_seat": target_seat,
        "best_recommended_block": ranked[0],
        "ranked_blocks": ranked[:top_n],
        "group_size": party_size,
    }


def _time_to_minutes(time_str):
    """Accept "HHMM" (e.g. "2045") or "HH:MM" (e.g. "20:45")."""
    time_str = time_str.replace(":", "")
    return int(time_str[:2]) * 60 + int(time_str[2:])


def _showtime_to_minutes(showtimemy):
    """Extract HH:MM from an ISO datetime like "2026-07-24T00:50:00" (TGV's
    `showtimemy` format). Convert it to minutes after midnight."""
    return _time_to_minutes(showtimemy[11:16])


def _find_matching_screenings(session, movie_name_pattern, date, target_time,
                               location_pattern=None, time_window_minutes=30,
                               cinemas=None):
    """Search screenings by movie name, date, time window, and cinema location.

    - `movie_name_pattern` / `location_pattern`: regex, case-insensitive. A
      plain substring (e.g. "Odyssey" or "Pavilion") works fine as-is, since
      it is a valid regex too. Use real regex syntax only when you need more
      than "contains". `location_pattern` matches against the cinema's name
      and address.
    - `target_time`: "HH:MM" or "HHMM". It matches screenings within
      +/- `time_window_minutes` of this time on the given `date`.
    - `cinemas`: pass a get_cinemas() result in to avoid re-fetching the
      cinema directory on every call, e.g. when searching several dates/times
      in a loop.

    Return raw screening matches (movie/experience/cinema/session identifiers)
    without price or seat data. See search_screenings() and
    search_screenings_for_group().
    """
    if cinemas is None:
        cinemas = get_cinemas(session)
    cinema_ids = list(cinemas.keys())

    target_minutes = _time_to_minutes(target_time)
    movie_re = re.compile(movie_name_pattern, re.IGNORECASE)
    location_re = re.compile(location_pattern, re.IGNORECASE) if location_pattern else None

    matches = []
    for movie in get_movies(session):
        if not movie_re.search(movie["title"]):
            continue

        by_cinema = get_showtimes(session, movie["code"], date, cinema_ids)
        time.sleep(REQUEST_DELAY)

        for cid, experiences in by_cinema.items():
            cinema = cinemas.get(cid, {})
            if location_re and not location_re.search(f"{cinema.get('name', '')} {cinema.get('address', '')}"):
                continue

            for exp in experiences:
                for show in exp["sessions"]:
                    if abs(_showtime_to_minutes(show["showtimemy"]) - target_minutes) > time_window_minutes:
                        continue
                    matches.append({
                        "movie_title": movie["title"],
                        "movie_code": movie["code"],
                        "experience": exp["experience"],
                        "cinema_id": cid,
                        "cinema_name": cinema.get("name"),
                        "address": cinema.get("address"),
                        "date": date,
                        "time": show["showtimemy"][11:16],
                        "sessionid": show["sessionid"],
                        "screenname": show["screenname"],
                    })
    return matches


def search_screenings(session, movie_name_pattern, date, target_time,
                       location_pattern=None, time_window_minutes=30, top_n_seats=3):
    """Search screenings, then attach ticket pricing and the best single seat for each.

    See _find_matching_screenings() for the search parameters. Return one
    entry per matching screening with `pricing` (from get_ticket_pricing())
    and `best_seat` (from recommend_seats()).
    """
    screenings = _find_matching_screenings(
        session, movie_name_pattern, date, target_time, location_pattern, time_window_minutes
    )

    results = []
    for screening in screenings:
        seat_map = get_seat_map(session, screening["cinema_id"], screening["sessionid"])
        time.sleep(REQUEST_DELAY)

        categories = sorted({s["category"] for s in seat_map["seats"]})
        usersessionid = get_user_session(session, screening["cinema_id"])
        pricing = get_ticket_pricing(
            session, screening["cinema_id"], screening["sessionid"], categories, usersessionid
        )
        time.sleep(REQUEST_DELAY)

        recommendation = recommend_seats(seat_map, top_n=top_n_seats)

        results.append({
            **screening,
            "available_seats": seat_map["available_seats"],
            "total_seats": seat_map["total_seats"],
            "pricing": pricing["tickets"],
            "best_seat": recommendation["best_recommended_seat"],
            "alternative_seats": recommendation["ranked_seats"],
        })
    return results


def search_screenings_for_group(session, movie_name_pattern, date, target_time, group_size,
                                 location_pattern=None, time_window_minutes=30, top_n_seats=3):
    """Do the same search as search_screenings(), but recommend a seat block for a group.

    Return one entry per matching screening with `pricing` and `best_block`
    (from recommend_seats_for_group()). `best_block` is None for screenings
    that do not have `group_size` contiguous free seats in any single row.
    """
    screenings = _find_matching_screenings(
        session, movie_name_pattern, date, target_time, location_pattern, time_window_minutes
    )

    results = []
    for screening in screenings:
        seat_map = get_seat_map(session, screening["cinema_id"], screening["sessionid"])
        time.sleep(REQUEST_DELAY)

        categories = sorted({s["category"] for s in seat_map["seats"]})
        usersessionid = get_user_session(session, screening["cinema_id"])
        pricing = get_ticket_pricing(
            session, screening["cinema_id"], screening["sessionid"], categories, usersessionid
        )
        time.sleep(REQUEST_DELAY)

        recommendation = recommend_seats_for_group(seat_map, group_size, top_n=top_n_seats)

        results.append({
            **screening,
            "available_seats": seat_map["available_seats"],
            "total_seats": seat_map["total_seats"],
            "pricing": pricing["tickets"],
            "best_block": recommendation["best_recommended_block"],
            "alternative_blocks": recommendation["ranked_blocks"],
        })
    return results


def scrape_all(session):
    cinemas = get_cinemas(session)
    cinema_ids = list(cinemas.keys())
    print(f"Found {len(cinema_ids)} cinemas nationwide.")

    movies = get_movies(session)
    print(f"Found {len(movies)} movies now showing.")

    dates = [
        (datetime.now() + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(DAYS_AHEAD)
    ]

    result = []
    for i, movie in enumerate(movies, 1):
        print(f"[{i}/{len(movies)}] {movie['title']}")
        showtimes_by_date = {}
        for businessday in dates:
            by_cinema = get_showtimes(session, movie["code"], businessday, cinema_ids)
            if by_cinema:
                showtimes_by_date[businessday] = [
                    {"cinema_id": cid, **cinemas.get(cid, {}), "experiences": experiences}
                    for cid, experiences in by_cinema.items()
                ]
            time.sleep(REQUEST_DELAY)
        result.append({
            "code": movie["code"],
            "title": movie["title"],
            "rating": movie["rating"],
            "genre": movie["genre"],
            "language": movie["language"],
            "duration": movie["duration"],
            "showtimes_by_date": showtimes_by_date,
        })
    return result


def main():
    session = requests.Session()

    if len(sys.argv) > 1 and sys.argv[1] == "seats":
        if len(sys.argv) != 4:
            print("Usage: tgv_scraper.py seats <cinemaid> <sessionid>")
            sys.exit(1)
        cinemaid, sessionid = sys.argv[2], sys.argv[3]

        status = get_seat_occupancy(session, cinemaid, sessionid)
        print(f"{status['seatsused']}/{status['seatstotal']} seats sold ({status['usedpercentage']}%)")

        seatlayout = get_seat_plan(session, cinemaid, sessionid)
        print_seat_map(seatlayout)
        return

    data = scrape_all(session)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUT_DIR / f"tgv_showtimes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(data)} movies' showtimes to {output_path}")


if __name__ == "__main__":
    main()
