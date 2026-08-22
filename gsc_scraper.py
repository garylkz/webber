"""Scrape nationwide movie showtimes from Golden Screen Cinemas (GSC) Malaysia.

This script uses GSC's public showtime web service. It does not use a login or
a token:
  - getEpaymentMovie_ParentChild        -> list of now-showing movies + dates available.
  - getShowTimesByMovie_ParentChild_V2  -> full nationwide showtimes for one movie/date.
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests

API_BASE = "https://epaymentapi.gsc.com.my/showtimews/service.asmx"
HEADERS = {"User-Agent": "Mozilla/5.0"}
REQUEST_DELAY = 0.3  # seconds between requests; this avoids overloading GSC's servers
OUT_DIR = Path(__file__).resolve().parent / "out"


def parse_xml(content: bytes) -> ET.Element:
    """Parse remote XML but refuse documents with a DTD or entity
    declarations. stdlib ElementTree blocks external entities, but entity
    expansion (billion laughs) can still exhaust memory otherwise."""
    lowered = content.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("Refusing XML with DTD/entity declarations")
    return ET.fromstring(content)


def get_movies(session):
    resp = session.get(
        f"{API_BASE}/getEpaymentMovie_ParentChild",
        params={"includeChild": "true", "parent": ""},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    root = parse_xml(resp.content)

    movies = []
    for parent in root.findall("parent"):
        dates = sorted({
            show.get("opsdate")
            for child in parent.findall("child")
            for show in child.findall("show")
        })
        movies.append({
            "code": parent.get("code"),
            "title": parent.get("title"),
            "dates": dates,
        })
    return movies


def get_showtimes(session, parent_code, oprndate):
    resp = session.get(
        f"{API_BASE}/getShowTimesByMovie_ParentChild_V2",
        params={"parentid": parent_code, "oprndate": oprndate},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    root = parse_xml(resp.content)

    cinemas = []
    for location in root.findall("location"):
        formats = []
        for child in location.findall("child"):
            shows = [dict(show.attrib) for show in child.findall("show")]
            formats.append({
                "format_code": child.get("code"),
                "title": child.get("title"),
                "duration": child.get("duration"),
                "language": child.get("lang"),
                "rating": child.get("rating"),
                "genre": child.get("genre"),
                "shows": shows,
            })
        cinemas.append({
            "location_id": location.get("id"),
            "name": location.get("name"),
            "address": location.get("address"),
            "region": location.get("region"),
            "formats": formats,
        })
    return cinemas


def get_seat_map(session, location_id, hall_id, showdate, showtime):
    """Real-time seat map for one screening. `showtime` is 24h "HHMM" (e.g. "2045").
    It matches the `time` field on a `show` entry from get_showtimes()."""
    resp = session.get(
        f"{API_BASE}/getHallSeatStatus",
        params={
            "locationid": location_id,
            "hallid": hall_id,
            "showdate": showdate,
            "showtime": showtime,
        },
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    root = parse_xml(resp.content)

    seats = []
    for row in root.findall(".//row"):
        for col in row.findall("col"):
            seats.append({
                "row": row.get("value"),
                "col": col.get("value"),
                "id": col.get("id"),
                "status": col.get("status"),  # "A" = available, "B" = booked/unavailable
                "category": col.get("seatcategory"),
                "type": col.get("type"),
                "x": int(col.get("x") or 0),   # left-right position within the hall
                "y": int(row.get("y") or 0),   # row depth: smaller y = nearer the screen
            })

    return {
        "hall_no": root.get("no"),
        "max_seats_per_booking": root.get("maximumseats"),
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

    A smaller `y` means the seat is nearer to the screen. This holds true for
    GSC's seat-map data, where the row depth numbers outward from the screen.

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


def get_ticket_pricing(session, location_id, hall_id, film_id, showdate, showtime):
    """Ticket prices for one screening, broken down by ticket/seat type.

    `film_id` is the format's `format_code` from get_showtimes(). For example,
    use the IMAX variant's code, not the movie's overall `code`. `seatcategory`
    on each ticket type matches the `category` field on seats from
    get_seat_map(). Use it to look up the cost of a given seat.
    """
    resp = session.get(
        f"{API_BASE}/getTicketPricingEpaySpecialV5",
        params={
            "locationid": location_id,
            "hallid": hall_id,
            "filmid": film_id,
            "showdate": showdate,
            "showtime": showtime,
        },
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    root = parse_xml(resp.content)

    ticket_data = root.find("ticketdata")
    tickets = []
    for ticket in ticket_data.findall("ticket"):
        tickets.append({
            "type": ticket.get("type"),
            "price": float(ticket.get("price")),
            "tax": float(ticket.get("tax")),
            "surcharge": float(ticket.get("surcharge")),
            "seatcategory": ticket.get("seatcategory"),  # matches a seat's "category" field
            "seats_taken": int(ticket.get("seats_taken")),  # e.g. "Twin"/couple seats use 2
        })

    return {
        "available": ticket_data.get("code") == "0",
        "message": ticket_data.get("msg"),
        "tickets": tickets,
    }


def _find_available_blocks(seat_map, block_size, max_seat_gap=3):
    """Find each distinct contiguous block of `block_size` available seats.

    The seats must be physically adjacent. Adjacent means consecutive seat
    numbers and a normal seat-to-seat gap in `x`. A block never bridges across
    a center aisle. One row can yield several overlapping blocks; the function
    returns each block once."""
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


def recommend_seats_for_group(seat_map, group_size, top_n=5, ideal_row_fraction=0.6, max_seat_gap=3):
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


def recommend_seats_near(seat_map, target_seat, party_size=1, top_n=5, max_seat_gap=3):
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
    measurement.
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
    """Accept "HHMM" (API format, e.g. "2045") or "HH:MM" (e.g. "20:45")."""
    time_str = time_str.replace(":", "")
    return int(time_str[:2]) * 60 + int(time_str[2:])


def _find_matching_screenings(session, movie_name_pattern, date, target_time,
                               location_pattern=None, time_window_minutes=30):
    """Search screenings by movie name, date, time window, and cinema location.

    - `movie_name_pattern` / `location_pattern`: regex, case-insensitive. A
      plain substring (e.g. "Odyssey" or "TRX") works fine as-is, since it is
      a valid regex too. Use real regex syntax only when you need more than
      "contains".
    - `target_time`: "HH:MM" or "HHMM". It matches screenings within
      +/- `time_window_minutes` of this time on the given `date`.

    Return raw screening matches (movie/format/cinema/hall identifiers)
    without price or seat data. See search_screenings() and
    search_screenings_for_group().
    """
    target_minutes = _time_to_minutes(target_time)
    movie_re = re.compile(movie_name_pattern, re.IGNORECASE)
    location_re = re.compile(location_pattern, re.IGNORECASE) if location_pattern else None

    matches = []
    for movie in get_movies(session):
        if not movie_re.search(movie["title"]) or date not in movie["dates"]:
            continue

        cinemas = get_showtimes(session, movie["code"], date)
        time.sleep(REQUEST_DELAY)

        for cinema in cinemas:
            if location_re and not location_re.search(cinema["name"]):
                continue

            for fmt in cinema["formats"]:
                for show in fmt["shows"]:
                    if abs(_time_to_minutes(show["time"]) - target_minutes) > time_window_minutes:
                        continue
                    matches.append({
                        "movie_title": movie["title"],
                        "movie_code": movie["code"],
                        "format_title": fmt["title"],
                        "format_code": fmt["format_code"],
                        "cinema_name": cinema["name"],
                        "location_id": cinema["location_id"],
                        "address": cinema["address"],
                        "date": date,
                        "time": show["time"],
                        "timestr": show["timestr"],
                        "hall_id": show["hid"],
                        "hall_name": show["hname"],
                    })
    return matches


def search_screenings(session, movie_name_pattern, date, target_time,
                       location_pattern=None, time_window_minutes=30, top_n_seats=3):
    """Search screenings, then attach ticket pricing and the best single seat for each.

    See _find_matching_screenings() for the search parameters. Return one
    entry per matching screening with `pricing` (from get_ticket_pricing) and
    `best_seat` (from recommend_seats).
    """
    screenings = _find_matching_screenings(
        session, movie_name_pattern, date, target_time, location_pattern, time_window_minutes
    )

    results = []
    for screening in screenings:
        pricing = get_ticket_pricing(
            session, screening["location_id"], screening["hall_id"],
            screening["format_code"], screening["date"], screening["time"],
        )
        time.sleep(REQUEST_DELAY)

        seat_map = get_seat_map(
            session, screening["location_id"], screening["hall_id"],
            screening["date"], screening["time"],
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
    (from recommend_seats_for_group). `best_block` is None for screenings
    that do not have `group_size` contiguous free seats in any single row.
    """
    screenings = _find_matching_screenings(
        session, movie_name_pattern, date, target_time, location_pattern, time_window_minutes
    )

    results = []
    for screening in screenings:
        pricing = get_ticket_pricing(
            session, screening["location_id"], screening["hall_id"],
            screening["format_code"], screening["date"], screening["time"],
        )
        time.sleep(REQUEST_DELAY)

        seat_map = get_seat_map(
            session, screening["location_id"], screening["hall_id"],
            screening["date"], screening["time"],
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
    movies = get_movies(session)
    print(f"Found {len(movies)} movies now showing.")

    result = []
    for i, movie in enumerate(movies, 1):
        print(f"[{i}/{len(movies)}] {movie['title']} ({len(movie['dates'])} dates)")
        showtimes_by_date = {}
        for oprndate in movie["dates"]:
            showtimes_by_date[oprndate] = get_showtimes(session, movie["code"], oprndate)
            time.sleep(REQUEST_DELAY)
        result.append({
            "code": movie["code"],
            "title": movie["title"],
            "showtimes_by_date": showtimes_by_date,
        })
    return result


def main():
    session = requests.Session()
    data = scrape_all(session)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUT_DIR / f"gsc_showtimes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(data)} movies' showtimes to {output_path}")


if __name__ == "__main__":
    main()
