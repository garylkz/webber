#!/usr/bin/env python3
"""Live positions of Rapid Bus buses on one route, from the MyRapid bus kiosk.

Usage:
    python rapidbus_buses.py 200                 # live buses on route 200
    python rapidbus_buses.py U2000               # the same, by the kiosk's route code
    python rapidbus_buses.py --routes            # every route in the saved table
    python rapidbus_buses.py --routes --search kjl
    python rapidbus_buses.py --stops 200         # a route's stops, from the saved table
    python rapidbus_buses.py T852 --map          # live buses, also drawn on a map under out/
    python rapidbus_buses.py T852 --format svg > t852.svg   # the answer as a map, not JSON
    python rapidbus_buses.py --stops T852 --format svg      # the route and its stops, no buses
    python rapidbus_buses.py --routes --format svg --save   # a table of routes, saved under out/

Prints one JSON value on stdout. Errors go to stderr with exit 1.

If the kiosk answers with its bot check, the call falls back to Malaysia's open
data feed of Rapid Bus positions (api.data.gov.my, GTFS Realtime), which needs
no bot check but carries fewer buses and older fixes. --source picks one.

The live call is one GET to the kiosk's AVL snapshot endpoint. The route table
(labels, codes, stops) cannot be fetched by a script, since the kiosk pages sit
behind Incapsula's bot check, so it ships beside this file as
rapidbus_routes.json, exported once from a browser. --routes and --stops read
only that file and make no request.
"""

import argparse
import datetime as dt
import json
import math
import re
import struct
from pathlib import Path

import requests

BASE = "https://myrapidbus.prasarana.com.my"
SNAPSHOT = BASE + "/avl/api/avl/route/{code}"
ROUTES_FILE = Path(__file__).resolve().parent / "rapidbus_routes.json"
PATHS_FILE = Path(__file__).resolve().parent / "rapidbus_paths.json"
MYT = dt.timezone(dt.timedelta(hours=8))
STALE_SECONDS = 300  # the kiosk drops a bus with no GPS fix for 5 minutes
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Accept": "application/json",
}
CODE_RE = re.compile(r"^[A-Z0-9]{2,8}$")
FEED_URL = "https://api.data.gov.my/gtfs-realtime/vehicle-position/prasarana/"
FEED_CATEGORIES = ("rapid-bus-mrtfeeder", "rapid-bus-kl")  # the feeders (T routes) first
FEED_NOTE = ("From Malaysia's open data feed, not the kiosk: it carries fewer buses, its fixes are 20 to 90 "
             "seconds older, and it has no direction or at-stop flags. An empty list does not mean no buses run.")


class BlockedError(Exception):
    """The site answered with its bot check instead of data."""


# --- the saved route table ---------------------------------------------------

def load_table(path: Path = ROUTES_FILE) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Route table not found: {path}") from None


def norm(label: str) -> str:
    """A label as typed by a person: case, spaces and the (OS) suffix do not matter."""
    return re.sub(r"\s+", " ", label.strip().upper())


def find_routes(table: dict, query: str) -> list[dict]:
    """Routes whose code or label is the query. A label may name several routes."""
    q = norm(query)
    by_code = [r for r in table["routes"] if r["code"] == q]
    if by_code:
        return by_code
    exact = [r for r in table["routes"] if norm(r["label"]) == q]
    if exact:
        return exact
    # "170" also finds "170 (OS)"
    return [r for r in table["routes"] if norm(r["label"]).removesuffix(" (OS)") == q]


def route_summary(route: dict) -> dict:
    return {"label": route["label"], "code": route["code"], "region": route["region"],
            "kiosk_ids": route["kiosk_ids"], "stops": len(route["stops"])}


def list_routes(table: dict, search: str | None = None) -> dict:
    routes = table["routes"]
    if search:
        s = norm(search)
        routes = [r for r in routes if s in norm(r["label"]) or s in r["code"]]
    return {"captured": table["captured"], "count": len(routes),
            "routes": [route_summary(r) for r in routes]}


def route_stops(table: dict, route: dict) -> dict:
    """A route's stops in order, split by direction as the kiosk numbers them."""
    stops = table["stops"]
    directions: dict[str, list] = {}
    for stop_id, direction in route["stops"]:
        s = stops[stop_id]
        directions.setdefault(direction, []).append(
            {"stop_id": stop_id, "name": s["name"], "street": s["street"], "lat": s["lat"], "lon": s["lon"]})
    return {**route_summary(route), "captured": table["captured"], "directions": directions}


# --- live positions ----------------------------------------------------------

def fetch_snapshot(code: str, timeout: int = 20) -> dict:
    """The kiosk's AVL snapshot for one route code. Raises BlockedError on the bot check."""
    try:
        resp = requests.get(SNAPSHOT.format(code=code), headers=HEADERS, timeout=timeout,
                            allow_redirects=False)
    except requests.RequestException as e:
        raise SystemExit(f"Could not reach {BASE}: {e}") from e
    ctype = resp.headers.get("Content-Type", "")
    if resp.status_code in (301, 302, 403) or "json" not in ctype:
        raise BlockedError(f"HTTP {resp.status_code}, {ctype or 'no content type'}")
    if resp.status_code != 200:
        raise SystemExit(f"{BASE} answered HTTP {resp.status_code} for route {code}")
    try:
        data = resp.json()
    except ValueError:
        raise BlockedError("the answer was not JSON") from None
    if not isinstance(data.get("vehicles"), list):
        raise SystemExit(f"Unexpected snapshot for route {code}: no vehicles list")
    return data


# --- the open data feed ------------------------------------------------------
# api.data.gov.my publishes Rapid Bus positions as GTFS Realtime, a protobuf
# message. Only the few fields used here are decoded, by hand, so the tool needs
# no protobuf library. FeedMessage.entity (2) -> FeedEntity.vehicle (4) ->
# VehiclePosition: trip (1: trip_id 1, route_id 5), position (2: lat 1, lon 2,
# bearing 3, speed 5), timestamp (5), vehicle (8: id 1, label 2, plate 3).

def _varint(b: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        c = b[i]
        i += 1
        value |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            return value, i


def proto_fields(b: bytes) -> list[tuple[int, object]]:
    """The (field number, value) pairs of one protobuf message: ints, or bytes for the rest."""
    i, out = 0, []
    while i < len(b):
        key, i = _varint(b, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(b, i)
        elif wire == 1:
            value, i = b[i:i + 8], i + 8
        elif wire == 5:
            value, i = b[i:i + 4], i + 4
        elif wire == 2:
            size, i = _varint(b, i)
            value, i = b[i:i + size], i + size
        else:
            raise ValueError(f"wire type {wire}")
        if i > len(b):
            raise ValueError("truncated message")
        out.append((number, value))
    return out


def _field(b: bytes, number: int):
    return next((v for n, v in proto_fields(b) if n == number), None)


def _text(b: bytes, number: int) -> str:
    v = _field(b, number)
    return v.decode("utf-8", "replace").strip() if isinstance(v, bytes) else ""


def feed_snapshot(data: bytes, wanted: set[str]) -> dict:
    """The feed's vehicles whose route id is in `wanted`, shaped like a kiosk snapshot.

    The feed's speed is already km/h (a bus doing 29 reads 29). It has no
    direction, stop or terminus flags, so those are left out.
    """
    vehicles = []
    try:
        for number, entity in proto_fields(data):
            veh = _field(entity, 4) if number == 2 else None
            if veh is None:
                continue
            trip, pos, desc = _field(veh, 1) or b"", _field(veh, 2) or b"", _field(veh, 8) or b""
            if _text(trip, 5).upper() not in wanted:
                continue
            p = {n: struct.unpack("<f", v)[0] for n, v in proto_fields(pos) if isinstance(v, bytes) and len(v) == 4}
            stamp = _field(veh, 5)
            vehicles.append({
                "bus_no": _text(desc, 2) or _text(desc, 3) or _text(desc, 1),
                "latitude": p.get(1), "longitude": p.get(2), "angle": p.get(3), "speed": p.get(5),
                "dt_gps": dt.datetime.fromtimestamp(stamp, MYT).strftime("%Y-%m-%d %H:%M:%S")
                          if isinstance(stamp, int) else None,
            })
    except (IndexError, ValueError, struct.error, OverflowError, OSError) as e:
        raise SystemExit(f"The open data feed answer could not be decoded ({type(e).__name__}: {e})") from None
    return {"vehicles": vehicles}


def fetch_feed(wanted: set[str], timeout: int = 20) -> dict:
    """Open data feed positions for a route: the first category that has any of its buses."""
    snapshot = {"vehicles": []}
    for category in FEED_CATEGORIES:
        try:
            resp = requests.get(FEED_URL, params={"category": category}, timeout=timeout)
        except requests.RequestException as e:
            raise SystemExit(f"Could not reach the open data feed: {e}") from e
        if resp.status_code != 200:
            raise SystemExit(f"The open data feed answered HTTP {resp.status_code} for {category}")
        snapshot = feed_snapshot(resp.content, wanted)
        if snapshot["vehicles"]:
            break
    return snapshot


def parse_time(value) -> dt.datetime | None:
    """The kiosk's 'YYYY-MM-DD HH:MM:SS', which is Malaysia time."""
    if not isinstance(value, str):
        return None
    try:
        t = dt.datetime.fromisoformat(value.strip().replace(" ", "T"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=MYT)


def to_float(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def direction_of(value) -> str | None:
    """The snapshot says "02" where the stop list says "2"; use the stop list's form."""
    text = str(value).strip() if value is not None else ""
    return str(int(text)) if text.isdigit() else (text or None)


def parse_vehicles(snapshot: dict, stops: dict, now: dt.datetime) -> tuple[list[dict], int]:
    """Clean bus records, newest fix per bus, and how many stale or broken ones were dropped.

    The driver's id (captain_id) is left out on purpose.
    """
    buses: dict[str, dict] = {}
    dropped = 0
    for v in snapshot.get("vehicles", []):
        bus_no = str(v.get("bus_no") or "").strip().upper()
        gps = parse_time(v.get("dt_gps"))
        lat, lon = to_float(v.get("latitude")), to_float(v.get("longitude"))
        if not bus_no or gps is None or lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
            dropped += 1
            continue
        age = (now - gps).total_seconds()
        if age > STALE_SECONDS or age < -60:
            dropped += 1
            continue
        if bus_no in buses and buses[bus_no]["_gps"] > gps:
            continue
        stop_id = v.get("busstop_id")
        stop = stops.get(str(stop_id)) if stop_id else None
        buses[bus_no] = {
            "_gps": gps,
            "bus_no": bus_no,
            "lat": lat,
            "lon": lon,
            "speed_kmh": to_float(v.get("speed")),
            "heading": to_float(v.get("angle")),
            "direction": direction_of(v.get("dir")),
            "gps_time": gps.isoformat(),
            "age_seconds": max(0, round(age)),
            "at_stop": {"stop_id": str(stop_id), "name": stop["name"] if stop else None} if stop_id else None,
            "at_terminus": v.get("is_inside_endstop"),
            "on_route": v.get("is_inside_shape") == 1 if v.get("is_inside_shape") is not None else None,
            "accessible": v.get("accessibility") == 1 if v.get("accessibility") is not None else None,
        }
    out = sorted(buses.values(), key=lambda b: b["bus_no"])
    for b in out:
        del b["_gps"]
    return out, dropped


# --- next stop, estimated ----------------------------------------------------
# The kiosk does not say which stop a bus goes to next. It is estimated by
# drawing a straight line from each stop to the next, in the order of the bus's
# direction, and taking the end of the line the bus is nearest to.

EARTH_M = 6_371_000
OFF_ROUTE_M = 500    # farther than this from every line: no estimate
AT_STOP_M = 40       # this close to a stop, the bus is at it and heads for the one after
OTHER_DIR_M = 100    # a line of the other direction counts as this much farther away
AGAINST_M = 300      # so does a line pointing away from a moving bus's heading
MOVING_KMH = 3       # below this the heading is not trusted


def to_xy(lat: float, lon: float, lat0: float) -> tuple[float, float]:
    """Metres on a flat map around lat0, good enough over a city."""
    return (math.radians(lon) * EARTH_M * math.cos(math.radians(lat0)), math.radians(lat) * EARTH_M)


def project(p, a, b) -> tuple[float, float]:
    """(distance from p to the segment a-b, how far along it 0..1)."""
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy), t


def bearing(a, b) -> float:
    """Compass bearing from a to b, in degrees."""
    return math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360


def stop_sequence(route: dict, direction: str) -> list[str]:
    """The stop ids a bus in this direction passes, in order, then the first two
    of the other direction, since a bus that finishes one direction starts the
    other. A terminus both lists share is not repeated."""
    ours = [sid for sid, d in route["stops"] if d == direction]
    if not ours:
        return []
    others = [sid for sid, d in route["stops"] if d != direction]
    if others and others[0] == ours[-1]:
        others = others[1:]
    return ours + others[:2]


def next_stop(bus: dict, route: dict, stops: dict) -> dict | None:
    """The stop the bus is heading for, and how far away it is in a straight line.

    Every line between consecutive stops of either direction is a candidate. The
    snapshot's direction is often stale (it seems to change only at a
    terminus), so it only tips a close call; a moving bus's heading counts more.
    """
    lat0 = bus["lat"]
    p = to_xy(bus["lat"], bus["lon"], lat0)
    xy = {sid: to_xy(stops[sid]["lat"], stops[sid]["lon"], lat0) for sid, _ in route["stops"] if sid in stops}
    moving = bus.get("heading") is not None and (bus.get("speed_kmh") or 0) >= MOVING_KMH
    best = None  # (score, distance, sequence, index of the line's end)
    for direction in dict.fromkeys(d for _, d in route["stops"]):
        seq = [sid for sid in stop_sequence(route, direction) if sid in xy]
        for i in range(1, len(seq)):
            a, b = xy[seq[i - 1]], xy[seq[i]]
            dist, _ = project(p, a, b)
            score = dist + (0 if direction == bus["direction"] else OTHER_DIR_M)
            if moving and a != b:
                diff = abs(bearing(a, b) - bus["heading"]) % 360
                score += AGAINST_M if min(diff, 360 - diff) > 90 else 0
            if best is None or score < best[0]:
                best = (score, dist, seq, i)
    if best is None or best[1] > OFF_ROUTE_M:
        return None
    _, _, seq, i = best
    if math.dist(p, xy[seq[i]]) < AT_STOP_M and i + 1 < len(seq):
        i += 1
    sid = seq[i]
    return {"stop_id": sid, "name": stops[sid]["name"], "distance_m": round(math.dist(p, xy[sid]))}


def resolve_route(table: dict, query: str) -> dict:
    """The one route a query names, or an error that lists the candidates."""
    routes = find_routes(table, query)
    if not routes:
        if not CODE_RE.match(norm(query)):
            raise SystemExit(f"Unknown route: {query} (see --routes)")
        # a code the saved table does not know yet: try it as given
        routes = [{"label": None, "code": norm(query), "region": None, "kiosk_ids": [], "stops": []}]
    codes = sorted({r["code"] for r in routes})
    if len(codes) > 1:
        raise SystemExit(json.dumps({
            "error": f"{query} names {len(codes)} routes; ask again with one code",
            "routes": [route_summary(r) for r in routes]}, ensure_ascii=False))
    return routes[0]


def live(table: dict, query: str, now: dt.datetime | None = None, draw: bool = False,
         source: str = "auto") -> dict:
    route = resolve_route(table, query)
    used, note = "kiosk", None
    snapshot = None
    if source != "feed":
        try:
            snapshot = fetch_snapshot(route["code"])
        except BlockedError as e:
            if source == "kiosk":
                raise SystemExit(f"The MyRapid site answered with its bot check, not data ({e}). "
                                 "It blocks bursts of requests; try again in a few minutes, "
                                 "or use --source feed.") from None
    if snapshot is None:
        # the feed names a route by its label (T852) on the feeders and by its code (U2000) elsewhere
        wanted = {x.upper() for x in (route["code"], route["label"]) if x}
        snapshot, used, note = fetch_feed(wanted), "open data feed", FEED_NOTE
    now = now or dt.datetime.now(MYT)
    buses, dropped = parse_vehicles(snapshot, table["stops"], now)
    for bus in buses:
        bus["next_stop"] = next_stop(bus, route, table["stops"])
    result = {
        "route": route["label"],
        "code": route["code"],
        "region": route["region"],
        "source": used,
        "fetched_at": now.isoformat(timespec="seconds"),
        "count": len(buses),
        "dropped_stale": dropped,
        "next_stop_method": "estimated from straight lines between stops; distance_m is as the crow flies",
        "buses": buses,
    }
    if note:
        result["note"] = note
    if draw:
        result["map"] = save_map(result, route, table["stops"])
    return result


# --- the map -----------------------------------------------------------------
# An SVG: OpenStreetMap tiles embedded as images, the route drawn as lines
# between its stops, and each bus as an arrow with its plate and next stop.

OUT_DIR = Path(__file__).resolve().parents[2] / "out"  # repo-root out/
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_DIR = OUT_DIR / "rapidbus-tiles"
TILE_MAX_AGE = 7 * 24 * 3600  # OSM asks clients to cache tiles; a week is plenty for streets
TILE_HEADERS = {"User-Agent": "webber-rapidbus/0.1 (personal map renderer)"}
MAP_W, MAP_H = 1000, 760   # the map area, below a header
HEADER_H = 64
ROUTE_COLOUR = "#1a56db"
DIR_COLOURS = {"1": "#1b7f6b", "2": "#6a41e2"}
STOP_LABEL_LIMIT = 40  # label every stop only on routes this short


def world_px(lat: float, lon: float, z: int) -> tuple[float, float]:
    """Web Mercator pixel of a point at zoom z, as OSM tiles use."""
    scale = 256 * 2 ** z
    s = math.sin(math.radians(max(-85.0, min(85.0, lat))))
    return (lon + 180) / 360 * scale, (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * scale


def fit_zoom(points: list[tuple[float, float]], width: int, height: int, pad: int = 40) -> int:
    """The closest zoom (up to 17) at which every point fits in the frame."""
    for z in range(17, 2, -1):
        xs, ys = zip(*(world_px(lat, lon, z) for lat, lon in points))
        if max(xs) - min(xs) <= width - 2 * pad and max(ys) - min(ys) <= height - 2 * pad:
            return z
    return 3


def fetch_tile(z: int, x: int, y: int) -> bytes | None:
    """One OSM tile, from the local cache when it is under a week old. None when it cannot be had."""
    path = TILE_DIR / str(z) / str(x) / f"{y}.png"
    try:
        if path.is_file() and dt.datetime.now().timestamp() - path.stat().st_mtime < TILE_MAX_AGE:
            return path.read_bytes()
    except OSError:
        pass
    try:
        resp = requests.get(TILE_URL.format(z=z, x=x, y=y), headers=TILE_HEADERS, timeout=20)
    except requests.RequestException:
        return None
    if resp.status_code != 200 or not resp.headers.get("Content-Type", "").startswith("image/"):
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
    except OSError:
        pass
    return resp.content


def esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def load_paths(path: Path = PATHS_FILE) -> dict:
    """Route code to its road path, [[lat, lon], ...]. Empty when the file is missing."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))["paths"]
    except (FileNotFoundError, ValueError, KeyError):
        return {}


def arrows_along(line: list[tuple[float, float]], every: float = 140) -> list[tuple[float, float, float]]:
    """(x, y, bearing) points spaced along a drawn line, to show which way it runs."""
    marks, run = [], every / 2
    for (x1, y1), (x2, y2) in zip(line, line[1:]):
        seg = math.hypot(x2 - x1, y2 - y1)
        while seg and run <= seg:
            f = run / seg
            marks.append((x1 + f * (x2 - x1), y1 + f * (y2 - y1), math.degrees(math.atan2(x2 - x1, y1 - y2))))
            run += every
        run -= seg
    return marks


def render_map(result: dict, route: dict, stops: dict, tile_source=fetch_tile, path: list | None = None) -> str:
    """The SVG text of a live result: tiles, the route, its stops and its buses.

    Buses with no next stop (off the route) stay off the map and are named in
    the header, so one bus at the depot does not shrink the route to a dot.
    """
    import base64

    route_stops = [(sid, d) for sid, d in route["stops"] if sid in stops]
    on_map = [b for b in result["buses"] if b.get("next_stop") or not route_stops]
    off_map = [b for b in result["buses"] if b not in on_map]
    points = [(stops[sid]["lat"], stops[sid]["lon"]) for sid, _ in route_stops]
    points += [(b["lat"], b["lon"]) for b in on_map]
    points += [(lat, lon) for lat, lon in path or []]
    if not points:
        raise SystemExit(f"Nothing to draw for route {result['code']}: no stops and no buses")
    z = fit_zoom(points, MAP_W, MAP_H)
    pxs = [world_px(lat, lon, z) for lat, lon in points]
    cx = (min(p[0] for p in pxs) + max(p[0] for p in pxs)) / 2
    cy = (min(p[1] for p in pxs) + max(p[1] for p in pxs)) / 2
    left, top = cx - MAP_W / 2, cy - MAP_H / 2

    def xy(lat, lon):
        wx, wy = world_px(lat, lon, z)
        return round(wx - left, 1), round(wy - top + HEADER_H, 1)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{MAP_W}" height="{MAP_H + HEADER_H}" '
           f'viewBox="0 0 {MAP_W} {MAP_H + HEADER_H}" font-family="Helvetica, Arial, sans-serif">',
           f'<rect width="{MAP_W}" height="{MAP_H + HEADER_H}" fill="#f2efe9"/>',
           f'<svg x="0" y="{HEADER_H}" width="{MAP_W}" height="{MAP_H}">']
    # tiles
    missing = 0
    for tx in range(int(left // 256), int((left + MAP_W) // 256) + 1):
        for ty in range(int(top // 256), int((top + MAP_H) // 256) + 1):
            if not 0 <= ty < 2 ** z:
                continue
            png = tile_source(z, tx % 2 ** z, ty)
            if png is None:
                missing += 1
                continue
            data = base64.b64encode(png).decode("ascii")
            out.append(f'<image x="{round(tx * 256 - left, 1)}" y="{round(ty * 256 - top, 1)}" '
                       f'width="256" height="256" href="data:image/png;base64,{data}"/>')
    out.append(f'<rect width="{MAP_W}" height="{MAP_H}" fill="#ffffff" opacity="0.45"/></svg>')
    # the route: its road path when known, else one straight-lined chain of stops per direction
    if path:
        line = [xy(lat, lon) for lat, lon in path]
        points = " ".join("%s,%s" % p for p in line)
        # a white casing under a strong blue line, so it stands out from OSM's roads and parks
        out.append(f'<polyline points="{points}" fill="none" stroke="#ffffff" stroke-width="10" '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')
        out.append(f'<polyline points="{points}" fill="none" stroke="{ROUTE_COLOUR}" stroke-width="6" '
                   f'stroke-linejoin="round" stroke-linecap="round"/>')
        for ax, ay, angle in arrows_along(line):
            out.append(f'<path d="M-4,3 L0,-4 L4,3" fill="none" stroke="#ffffff" stroke-width="1.8" '
                       f'transform="translate({ax:.1f},{ay:.1f}) rotate({angle:.0f})"/>')
    for direction in ([] if path else dict.fromkeys(d for _, d in route_stops)):
        line = " ".join("%s,%s" % xy(stops[sid]["lat"], stops[sid]["lon"]) for sid, d in route_stops if d == direction)
        colour = DIR_COLOURS.get(direction, "#555555")
        out.append(f'<polyline points="{line}" fill="none" stroke="{colour}" stroke-width="4" '
                   f'stroke-linejoin="round" stroke-linecap="round" opacity="0.85"/>')
    label = len(dict.fromkeys(sid for sid, _ in route_stops)) <= STOP_LABEL_LIMIT
    # buses: markers first, then labels, each label moved down or up until it
    # clears the ones already placed, with a leader line when it had to move
    placed: list[tuple[float, float, float, float]] = []
    labels, markers = [], []
    for b in sorted(on_map, key=lambda b: xy(b["lat"], b["lon"])[1]):
        x, y = xy(b["lat"], b["lon"])
        moving = (b.get("speed_kmh") or 0) >= MOVING_KMH
        fill = "#e8590c" if moving else "#868e96"
        heading = b.get("heading") or 0
        nxt = b.get("next_stop")
        caption = b["bus_no"] + (f" → {nxt['name']} ({nxt['distance_m']} m)" if nxt else "")
        speed = f"{b['speed_kmh']:.0f} km/h" if b.get("speed_kmh") is not None else ""
        markers.append(f'<g transform="translate({x},{y})"><title>{esc(caption)}, {esc(speed)}</title>'
                   f'<circle r="11" fill="{fill}" stroke="#ffffff" stroke-width="2.5"/>'
                   f'<path d="M0,-7 L5,4 L0,1.5 L-5,4 Z" fill="#ffffff" transform="rotate({heading:.0f})"/></g>')
        width = 7.6 * len(caption) + 10
        for shift in (0, 34, -34, 68, -68, 102, -102, 136, -136, 170):
            box = (x + 15, y - 22 + shift, x + 15 + width, y + 11 + shift)
            if not any(box[0] < o[2] and o[0] < box[2] and box[1] < o[3] and o[1] < box[3] for o in placed):
                break
        placed.append(box)
        labels.append((x, y, shift, caption, f'{speed}, GPS {b["age_seconds"]} s old'))
    # stops: a name is drawn only where it clears the bus labels and the names
    # already drawn; every stop keeps its name as a hover title
    for sid in dict.fromkeys(sid for sid, _ in route_stops):
        s = stops[sid]
        x, y = xy(s["lat"], s["lon"])
        out.append(f'<circle cx="{x}" cy="{y}" r="4.5" fill="#ffffff" stroke="#333333" stroke-width="1.5">'
                   f'<title>{esc(s["name"])}</title></circle>')
        box = (x + 6, y - 6, x + 8 + 6 * len(s["name"]), y + 6)
        if label and not any(box[0] < o[2] and o[0] < box[2] and box[1] < o[3] and o[1] < box[3] for o in placed):
            placed.append(box)
            out.append(f'<text x="{x + 7}" y="{y + 3.5}" font-size="10" fill="#333333" stroke="#ffffff" '
                       f'stroke-width="3" paint-order="stroke">{esc(s["name"])}</text>')
    out.extend(markers)
    for x, y, shift, caption, detail in labels:
        if shift:
            out.append(f'<line x1="{x}" y1="{y}" x2="{x + 15}" y2="{y + shift - 4}" stroke="#1a1a1a" '
                       f'stroke-width="1"/>')
        out.append(f'<text x="{x + 15}" y="{y - 8 + shift}" font-size="13" font-weight="bold" fill="#1a1a1a" '
                   f'stroke="#ffffff" stroke-width="4" paint-order="stroke">{esc(caption)}</text>')
        out.append(f'<text x="{x + 15}" y="{y + 7 + shift}" font-size="11" fill="#444444" stroke="#ffffff" '
                   f'stroke-width="3.5" paint-order="stroke">{esc(detail)}</text>')
    # header and attribution
    when = result["fetched_at"][:16].replace("T", " ")
    title = f"Rapid Bus {result['route'] or result['code']} ({result['code']})"
    is_live = result.get("live", True)
    if is_live:
        sub = f"{len(result['buses'])} buses at {when} MYT"
        if result.get("source", "kiosk") != "kiosk":
            sub += " (open data feed: partial, older fixes)"
        if off_map:
            sub += f"; off the route, not drawn: {', '.join(b['bus_no'] for b in off_map)}"
        sub += ". Next stops are estimates."
    else:
        sub = f"{len(dict.fromkeys(sid for sid, _ in route_stops))} stops, no live buses"
    sub += ("" if path or not route_stops else " Road path unknown: stops joined by straight lines.")
    out.append(f'<rect width="{MAP_W}" height="{HEADER_H}" fill="#ffffff"/>'
               f'<text x="16" y="28" font-size="20" font-weight="bold" fill="#1a1a1a">{esc(title)}</text>'
               f'<text x="16" y="50" font-size="12.5" fill="#555555">{esc(sub)}</text>')
    legend_x = MAP_W - 300
    if path:
        out.append(f'<line x1="{legend_x}" y1="24" x2="{legend_x + 22}" y2="24" stroke="{ROUTE_COLOUR}" '
                   f'stroke-width="6"/><text x="{legend_x + 28}" y="28" font-size="12" fill="#333333">'
                   f'route, arrows show the way it runs</text>')
    for i, (direction, colour) in enumerate(DIR_COLOURS.items()):
        if not path and any(d == direction for _, d in route_stops):
            out.append(f'<line x1="{legend_x + i * 90}" y1="24" x2="{legend_x + i * 90 + 22}" y2="24" '
                       f'stroke="{colour}" stroke-width="4"/><text x="{legend_x + i * 90 + 28}" y="28" '
                       f'font-size="12" fill="#333333">direction {direction}</text>')
    if is_live:
        out.append(f'<circle cx="{legend_x + 10}" cy="46" r="6" fill="#e8590c"/><text x="{legend_x + 22}" y="50" '
                   f'font-size="12" fill="#333333">moving</text><circle cx="{legend_x + 100}" cy="46" r="6" '
                   f'fill="#868e96"/><text x="{legend_x + 112}" y="50" font-size="12" fill="#333333">stopped</text>')
    note = "Map data © OpenStreetMap contributors" + (f" ({missing} tiles missing)" if missing else "")
    out.append(f'<text x="{MAP_W - 8}" y="{MAP_H + HEADER_H - 8}" font-size="11" text-anchor="end" fill="#333333" '
               f'stroke="#ffffff" stroke-width="3" paint-order="stroke">{esc(note)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def render_routes(listing: dict) -> str:
    """The route list (from list_routes) as an SVG table: label, code and stop count, three columns."""
    rows = listing["routes"]
    col_w, row_h, top, cols = 330, 20, 70, 3
    per_col = max(1, -(-len(rows) // cols))
    width, height = 16 + cols * col_w, top + per_col * row_h + 16
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}" font-family="Helvetica, Arial, sans-serif">',
           f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
           f'<text x="16" y="28" font-size="20" font-weight="bold" fill="#1a1a1a">Rapid Bus routes ({len(rows)})</text>',
           f'<text x="16" y="50" font-size="12.5" fill="#555555">label as signed on the bus, kiosk code, '
           f'number of stops. Table captured {esc(listing.get("captured", ""))}.</text>']
    for i, row in enumerate(rows):
        x, y = 16 + (i // per_col) * col_w, top + (i % per_col) * row_h + 14
        if i % 2 == 0:
            out.append(f'<rect x="{x - 6}" y="{y - 14}" width="{col_w - 12}" height="{row_h}" fill="#f4f6f8"/>')
        stops = f'{row["stops"]} stops' if row["stops"] else "no stops saved"
        out.append(f'<text x="{x}" y="{y}" font-size="13" font-weight="bold" fill="#1a1a1a">{esc(row["label"])}</text>'
                   f'<text x="{x + 130}" y="{y}" font-size="13" fill="#1a56db">{esc(row["code"])}</text>'
                   f'<text x="{x + 205}" y="{y}" font-size="12" fill="#555555">{esc(stops)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def stops_map(route: dict, stops: dict, tile_source=fetch_tile) -> str:
    """A map of a route's road path and stops, with no buses."""
    stamp = dt.datetime.now(MYT).isoformat(timespec="seconds")
    result = {"route": route["label"], "code": route["code"], "fetched_at": stamp, "buses": [], "live": False}
    return render_map(result, route, stops, tile_source, load_paths().get(route["code"]))


def save_svg(svg: str, name: str, when: str) -> str:
    """Write SVG text to out/rapidbus_<name>_<time>.svg and return its path."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = when[:19].replace("-", "").replace(":", "").replace("T", "_")
    file = OUT_DIR / f"rapidbus_{re.sub(r'[^A-Za-z0-9]+', '-', name).strip('-')}_{stamp}.svg"
    file.write_text(svg, encoding="utf-8")
    return str(file)


def save_map(result: dict, route: dict, stops: dict) -> str:
    """Write the live map under out/ and return its path."""
    svg = render_map(result, route, stops, path=load_paths().get(result["code"]))
    return save_svg(svg, result["route"] or result["code"], result["fetched_at"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("route", nargs="?", help="route label (200, T789, KJL6) or kiosk code (U2000)")
    parser.add_argument("--routes", action="store_true", help="list routes from the saved table")
    parser.add_argument("--search", help="with --routes, only labels or codes containing this")
    parser.add_argument("--stops", action="store_true", help="the route's stops instead of live buses")
    parser.add_argument("--map", action="store_true",
                        help="also draw the live buses on a street map, saved as an SVG under out/ (JSON output only)")
    parser.add_argument("--format", choices=("json", "svg"), default="json",
                        help="answer as JSON (default) or as an SVG: a live map, a stops map, or a table of routes")
    parser.add_argument("--save", action="store_true",
                        help="with --format svg, save the SVG under out/ and print its path as JSON, "
                             "instead of printing the SVG")
    parser.add_argument("--source", choices=("auto", "kiosk", "feed"), default="auto",
                        help="live data from the kiosk, the open data feed, or the kiosk with the feed as fallback")
    args = parser.parse_args()

    svg_out = args.format == "svg"
    if args.save and not svg_out:
        parser.error("--save goes with --format svg")
    if svg_out and args.map:
        parser.error("--map saves a map beside JSON; with --format svg the answer is the map")
    name = when = None
    if args.routes:
        if args.route or args.stops or args.map:
            parser.error("--routes takes no route, --stops or --map")
        result = list_routes(load_table(), args.search)
        if svg_out:
            result, name, when = render_routes(result), "routes", dt.datetime.now(MYT).isoformat()
    elif args.search:
        parser.error("--search goes with --routes")
    elif not args.route:
        parser.error("give a route, or --routes")
    elif args.stops and args.map:
        parser.error("--map draws live buses; it does not go with --stops")
    elif args.stops:
        table = load_table()
        if svg_out:
            route = resolve_route(table, args.route)
            result = stops_map(route, table["stops"])
            name, when = f"{route['label'] or route['code']}-stops", dt.datetime.now(MYT).isoformat()
        else:
            routes = find_routes(table, args.route)
            if not routes:
                raise SystemExit(f"Unknown route: {args.route} (see --routes)")
            result = [route_stops(table, r) for r in routes]
            result = result[0] if len(result) == 1 else result
    else:
        table = load_table()
        result = live(table, args.route, draw=args.map, source=args.source)
        if svg_out:
            route = resolve_route(table, args.route)
            name, when = result["route"] or result["code"], result["fetched_at"]
            result = render_map(result, route, table["stops"], path=load_paths().get(route["code"]))
    if svg_out and args.save:
        print(json.dumps({"format": "svg", "file": save_svg(result, name, when), "bytes": len(result.encode("utf-8"))}))
    elif svg_out:
        print(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
