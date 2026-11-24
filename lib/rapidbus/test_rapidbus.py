"""Offline tests for rapidbus_buses: route lookup, the stop list, and the snapshot parser.

The snapshot fixture is a real answer for route U2000 with the driver ids
redacted and one stale bus (WVX0001) appended. The route table fixture is a
trimmed copy of the real one (the second SEWA KHAS kiosk id is made up).
"""

import datetime as dt
import json
import struct
from pathlib import Path

import pytest

import rapidbus_buses as r

FIXTURES = Path(__file__).parent / "fixtures"
TABLE = r.load_table(FIXTURES / "rapidbus_routes_sample.json")
SNAPSHOT = json.loads((FIXTURES / "rapidbus_snapshot_U2000.json").read_text())
NOW = dt.datetime(2026, 11, 23, 20, 12, 0, tzinfo=r.MYT)


def test_a_route_is_found_by_label_or_code():
    assert [x["code"] for x in r.find_routes(TABLE, "200")] == ["U2000"]
    assert [x["code"] for x in r.find_routes(TABLE, "u2000")] == ["U2000"]
    assert [x["code"] for x in r.find_routes(TABLE, " bas1 ")] == ["H1200"]


def test_the_os_suffix_is_optional():
    assert [x["code"] for x in r.find_routes(TABLE, "170")] == ["U1708"]
    assert [x["code"] for x in r.find_routes(TABLE, "170 (OS)")] == ["U1708"]


def test_a_label_can_name_several_routes():
    assert sorted(x["code"] for x in r.find_routes(TABLE, "sewa khas")) == ["S0008", "SK"]


def test_listing_and_searching_routes():
    assert r.list_routes(TABLE)["count"] == 5
    found = r.list_routes(TABLE, "bas")
    assert found["routes"] == [{"label": "BAS1", "code": "H1200", "region": "RKL",
                                "kiosk_ids": ["1459", "1466", "1468"], "stops": 0}]
    assert [x["code"] for x in r.list_routes(TABLE, "s0008")["routes"]] == ["S0008"]


def test_stops_are_split_by_direction_in_order():
    out = r.route_stops(TABLE, r.find_routes(TABLE, "200")[0])
    assert list(out["directions"]) == ["1", "2"]
    assert [s["stop_id"] for s in out["directions"]["2"]] == ["1003511", "1000887", "1003362"]
    assert out["directions"]["1"][0] == {"stop_id": "1003362", "name": "SL163 KOLEJ VOKASIONAL GOMBAK",
                                         "street": "JLN SG PUSU", "lat": 3.246681237351, "lon": 101.72831266652}


def test_the_snapshot_becomes_clean_bus_records():
    buses, dropped = r.parse_vehicles(SNAPSHOT, TABLE["stops"], NOW)
    assert dropped == 1  # WVX0001's fix is 14 minutes old
    assert [b["bus_no"] for b in buses] == ["WVA4165", "WVB4105", "WVB4108", "WVC6421"]
    b = buses[1]
    assert b == {
        "bus_no": "WVB4105", "lat": 3.169615, "lon": 101.6985116, "speed_kmh": 20.0, "heading": 187.0,
        "direction": "2", "gps_time": "2026-11-23T20:11:53+08:00", "age_seconds": 7,
        "at_stop": {"stop_id": "1000887", "name": "KL53 GRAND SEASONS HOTEL"},
        "at_terminus": None, "on_route": True, "accessible": True,
    }
    assert buses[3]["at_stop"] is None and buses[3]["at_terminus"] == "start"


def test_the_driver_id_is_never_passed_on():
    buses, _ = r.parse_vehicles(SNAPSHOT, TABLE["stops"], NOW)
    assert all("captain_id" not in b for b in buses)


def test_the_newest_fix_of_a_bus_wins_and_bad_ones_are_dropped():
    old = dict(SNAPSHOT["vehicles"][2], dt_gps="2026-11-23 20:10:00", latitude=1.0)
    broken = dict(SNAPSHOT["vehicles"][2], bus_no="WVZ1", latitude="")
    future = dict(SNAPSHOT["vehicles"][2], bus_no="WVZ2", dt_gps="2026-11-23 20:20:00")
    buses, dropped = r.parse_vehicles({"vehicles": [SNAPSHOT["vehicles"][2], old, broken, future]}, {}, NOW)
    assert [(b["bus_no"], b["lat"]) for b in buses] == [("WVB4105", 3.169615)]
    assert dropped == 2
    assert buses[0]["at_stop"] == {"stop_id": "1000887", "name": None}  # a stop the table does not know


def test_the_bot_check_is_an_error_not_an_empty_route(monkeypatch):
    class Resp:
        status_code = 302
        headers = {"Content-Type": "text/html"}

    monkeypatch.setattr(r.requests, "get", lambda *a, **k: Resp())
    with pytest.raises(SystemExit, match="bot check"):
        r.live(TABLE, "200", NOW, source="kiosk")


def test_live_output(monkeypatch):
    monkeypatch.setattr(r, "fetch_snapshot", lambda code: SNAPSHOT)
    out = r.live(TABLE, "200", NOW)
    assert {k: out[k] for k in ("route", "code", "region", "count", "dropped_stale")} == {
        "route": "200", "code": "U2000", "region": "RKL", "count": 4, "dropped_stale": 1}


def test_an_ambiguous_or_unknown_route_makes_no_request(monkeypatch):
    def boom(code):
        raise AssertionError("no request expected")

    monkeypatch.setattr(r, "fetch_snapshot", boom)
    with pytest.raises(SystemExit, match="names 2 routes"):
        r.live(TABLE, "SEWA KHAS", NOW)
    with pytest.raises(SystemExit, match="Unknown route"):
        r.live(TABLE, "no such route!", NOW)


def test_an_unlisted_code_is_tried_as_given(monkeypatch):
    seen = []
    monkeypatch.setattr(r, "fetch_snapshot", lambda code: seen.append(code) or {"vehicles": []})
    out = r.live(TABLE, "t9990", NOW)
    assert seen == ["T9990"] and out["count"] == 0 and out["route"] is None


# --- next stop ---------------------------------------------------------------
# A made-up loop along the equator: A -> B -> C eastward (direction 1), then
# C -> D -> A back westward a little north (direction 2). 0.001 deg is about 111 m.

LOOP = {"label": "X", "code": "X1", "region": "RKL", "kiosk_ids": [],
        "stops": [["A", "1"], ["B", "1"], ["C", "1"], ["C", "2"], ["D", "2"], ["A", "2"]]}
LOOP_STOPS = {
    "A": {"name": "A", "lat": 0.0, "lon": 0.0},
    "B": {"name": "B", "lat": 0.0, "lon": 0.01},
    "C": {"name": "C", "lat": 0.0, "lon": 0.02},
    "D": {"name": "D", "lat": 0.003, "lon": 0.01},
}


def bus(lat, lon, direction="1", speed=30.0, heading=90.0):
    return {"lat": lat, "lon": lon, "direction": direction, "speed_kmh": speed, "heading": heading}


def nxt(b):
    found = r.next_stop(b, LOOP, LOOP_STOPS)
    return found and found["name"]


def test_the_next_stop_is_the_end_of_the_nearest_line():
    assert nxt(bus(0.0, 0.004)) == "B"
    assert nxt(bus(0.0001, 0.015)) == "C"
    assert r.next_stop(bus(0.0, 0.004), LOOP, LOOP_STOPS)["distance_m"] == 667


def test_a_bus_at_a_stop_heads_for_the_one_after():
    assert nxt(bus(0.0, 0.01)) == "C"


def test_a_bus_at_the_terminus_starts_the_other_direction():
    # at C, stopped, still labelled direction 1: the next stop is D, not C
    assert nxt(bus(0.0, 0.02, direction="1", speed=0.0)) == "D"


def test_heading_beats_a_stale_direction():
    # on the A-B line but driving west, labelled direction 1: it is on the way back
    assert nxt(bus(0.0001, 0.005, direction="1", heading=270.0)) == "A"
    assert nxt(bus(0.0001, 0.005, direction="1", heading=90.0)) == "B"


def test_far_from_the_route_or_no_stops_means_no_estimate():
    assert nxt(bus(0.05, 0.05)) is None
    assert r.next_stop(bus(0.0, 0.0), dict(LOOP, stops=[]), LOOP_STOPS) is None


def test_live_output_carries_the_estimate(monkeypatch):
    monkeypatch.setattr(r, "fetch_snapshot", lambda code: SNAPSHOT)
    out = r.live(TABLE, "200", NOW)
    assert out["next_stop_method"].startswith("estimated")
    assert all("next_stop" in b for b in out["buses"])


# --- the map -----------------------------------------------------------------

def loop_result(*buses):
    return {"route": "X", "code": "X1", "fetched_at": "2026-11-23T22:00:00+08:00", "buses": list(buses)}


def map_bus(bus_no, lat, lon, speed=30.0, heading=90.0):
    b = dict(bus(lat, lon, speed=speed, heading=heading), bus_no=bus_no, age_seconds=9)
    b["next_stop"] = r.next_stop(b, LOOP, LOOP_STOPS)
    return b


def test_the_map_is_valid_svg_with_tiles_route_and_buses():
    import xml.etree.ElementTree as ET

    asked = []
    res = loop_result(map_bus("WAB1", 0.0, 0.004), map_bus("FAR1", 0.05, 0.05))
    svg = r.render_map(res, LOOP, LOOP_STOPS, lambda z, x, y: asked.append((z, x, y)) or b"PNG")
    ns = {"s": "http://www.w3.org/2000/svg"}
    root = ET.fromstring(svg)
    assert len(root.findall(".//s:image", ns)) == len(asked) > 0
    assert len({z for z, _, _ in asked}) == 1
    assert len(root.findall("s:polyline", ns)) == 2  # one line per direction
    texts = " ".join(t.text or "" for t in root.iter("{http://www.w3.org/2000/svg}text"))
    assert "WAB1 → B (667 m)" in texts
    assert "off the route, not drawn: FAR1" in texts
    assert "OpenStreetMap contributors" in texts


def test_missing_tiles_are_noted_not_fatal():
    svg = r.render_map(loop_result(map_bus("WAB1", 0.0, 0.004)), LOOP, LOOP_STOPS, lambda z, x, y: None)
    assert "<image" not in svg and "tiles missing)" in svg


def test_labels_of_buses_on_the_same_spot_do_not_overlap():
    import re as re_

    res = loop_result(*(map_bus(f"WAB{i}", 0.0, 0.004) for i in range(3)))
    svg = r.render_map(res, LOOP, LOOP_STOPS, lambda z, x, y: None)
    ys = [float(y) for y in re_.findall(r'y="([\d.]+)" font-size="13" font-weight="bold"', svg)]
    assert len(ys) == 3 and min(abs(a - b) for i, a in enumerate(ys) for b in ys[i + 1:]) >= 30
    assert svg.count("<line") >= 2  # leader lines for the two that moved


LOOP_PATH = [[0.0, 0.0], [0.0005, 0.01], [0.0, 0.02], [0.003, 0.01], [0.0, 0.0]]


def test_a_known_road_path_replaces_the_straight_lines():
    svg = r.render_map(loop_result(map_bus("WAB1", 0.0, 0.004)), LOOP, LOOP_STOPS, lambda z, x, y: None, LOOP_PATH)
    assert svg.count("<polyline") == 2 and f'stroke="{r.ROUTE_COLOUR}" stroke-width="6"' in svg
    assert "arrows show the way it runs" in svg and "Road path unknown" not in svg
    assert svg.count('d="M-4,3 L0,-4 L4,3"') >= 2


def test_without_a_path_the_map_says_so():
    svg = r.render_map(loop_result(map_bus("WAB1", 0.0, 0.004)), LOOP, LOOP_STOPS, lambda z, x, y: None)
    assert svg.count("<polyline") == 2 and "Road path unknown" in svg


def test_arrows_are_spaced_along_the_line_and_point_its_way():
    marks = r.arrows_along([(0, 0), (300, 0), (300, 300)], every=100)
    assert [(round(x), round(y)) for x, y, _ in marks] == [(50, 0), (150, 0), (250, 0), (300, 50), (300, 150), (300, 250)]
    assert [round(a) for _, _, a in marks] == [90, 90, 90, 180, 180, 180]  # east, then down the screen


def test_paths_file_missing_means_no_paths(tmp_path):
    assert r.load_paths(tmp_path / "none.json") == {}


# --- the open data feed ------------------------------------------------------
# A small protobuf encoder builds feed answers, so no binary fixture is needed.

def _enc_varint(n):
    out = b""
    while True:
        low, n = n & 0x7F, n >> 7
        out += bytes([low | (0x80 if n else 0)])
        if not n:
            return out


def _msg(number, payload):
    return _enc_varint(number << 3 | 2) + _enc_varint(len(payload)) + payload


def _entity(route, plate, lat, lon, bearing, speed, when):
    trip = _msg(1, b"trip-1") + _msg(5, route.encode())
    pos = b"".join(_enc_varint(n << 3 | 5) + struct.pack("<f", v) for n, v in ((1, lat), (2, lon), (3, bearing), (5, speed)))
    veh = _msg(1, trip) + _msg(2, pos) + _enc_varint(5 << 3) + _enc_varint(int(when)) + _msg(8, _msg(1, b"id") + _msg(2, plate.encode()))
    return _msg(2, _msg(1, b"e1") + _msg(4, veh))


WHEN = NOW.timestamp() - 40
FEED = (_msg(1, b"\x0a\x032.0")
        + _entity("T852", "VAF5762", 3.17349, 101.66237, 270.0, 29.0, WHEN)
        + _entity("U2000", "WVB4105", 3.1696, 101.6985, 187.0, 20.0, WHEN)
        + _entity("T852", "VG6550", 3.14734, 101.65964, 55.0, 0.0, WHEN - 30))


def test_the_feed_gives_the_wanted_routes_buses_in_snapshot_shape():
    snap = r.feed_snapshot(FEED, {"T852"})
    assert [v["bus_no"] for v in snap["vehicles"]] == ["VAF5762", "VG6550"]
    v = snap["vehicles"][0]
    assert (round(v["latitude"], 4), round(v["longitude"], 4), v["angle"], v["speed"]) == (3.1735, 101.6624, 270.0, 29.0)
    assert v["dt_gps"] == "2026-11-23 20:11:20"


def test_feed_buses_pass_through_the_same_parser():
    buses, dropped = r.parse_vehicles(r.feed_snapshot(FEED, {"T852"}), {}, NOW)
    assert dropped == 0 and [b["bus_no"] for b in buses] == ["VAF5762", "VG6550"]
    b = buses[0]
    assert b["speed_kmh"] == 29.0 and b["age_seconds"] == 40
    assert b["direction"] is None and b["at_stop"] is None and b["at_terminus"] is None


def test_a_broken_feed_answer_is_an_error():
    with pytest.raises(SystemExit, match="could not be decoded"):
        r.feed_snapshot(FEED[:-5], {"T852"})
    with pytest.raises(SystemExit, match="could not be decoded"):
        r.feed_snapshot(b"\xff\xff\xff", {"T852"})


def test_the_feed_is_asked_category_by_category_until_the_route_shows(monkeypatch):
    asked = []

    class Resp:
        def __init__(self, content):
            self.status_code, self.content = 200, content

    def fake_get(url, params=None, **k):
        asked.append(params["category"])
        return Resp(b"" if params["category"] == "rapid-bus-mrtfeeder" else FEED)

    monkeypatch.setattr(r.requests, "get", fake_get)
    snap = r.fetch_feed({"U2000"})
    assert asked == ["rapid-bus-mrtfeeder", "rapid-bus-kl"]
    assert [v["bus_no"] for v in snap["vehicles"]] == ["WVB4105"]


def _blocked(code):
    raise r.BlockedError("HTTP 302, text/html")


def test_a_blocked_kiosk_falls_back_to_the_feed(monkeypatch):
    monkeypatch.setattr(r, "fetch_snapshot", _blocked)
    seen = []
    monkeypatch.setattr(r, "fetch_feed", lambda wanted: seen.append(wanted) or r.feed_snapshot(FEED, wanted))
    out = r.live(TABLE, "200", NOW)
    assert seen == [{"U2000", "200"}]
    assert out["source"] == "open data feed" and out["note"].startswith("From Malaysia's open data feed")
    assert [b["bus_no"] for b in out["buses"]] == ["WVB4105"] and "next_stop" in out["buses"][0]


def test_the_kiosk_answer_has_no_note(monkeypatch):
    monkeypatch.setattr(r, "fetch_snapshot", lambda code: SNAPSHOT)
    out = r.live(TABLE, "200", NOW)
    assert out["source"] == "kiosk" and "note" not in out


def test_source_feed_never_touches_the_kiosk(monkeypatch):
    def boom(code):
        raise AssertionError("no kiosk request expected")

    monkeypatch.setattr(r, "fetch_snapshot", boom)
    monkeypatch.setattr(r, "fetch_feed", lambda wanted: r.feed_snapshot(FEED, wanted))
    assert r.live(TABLE, "200", NOW, source="feed")["source"] == "open data feed"


def test_source_kiosk_does_not_fall_back(monkeypatch):
    monkeypatch.setattr(r, "fetch_snapshot", _blocked)
    monkeypatch.setattr(r, "fetch_feed", lambda wanted: pytest.fail("no feed request expected"))
    with pytest.raises(SystemExit, match="--source feed"):
        r.live(TABLE, "200", NOW, source="kiosk")


def test_a_feed_map_says_where_it_came_from():
    res = dict(loop_result(map_bus("WAB1", 0.0, 0.004)), source="open data feed")
    svg = r.render_map(res, LOOP, LOOP_STOPS, lambda z, x, y: None)
    assert "open data feed: partial" in svg
    assert "open data feed" not in r.render_map(loop_result(map_bus("WAB1", 0.0, 0.004)), LOOP, LOOP_STOPS, lambda z, x, y: None)


# --- answering as SVG --------------------------------------------------------

def test_the_route_list_is_an_svg_table():
    import xml.etree.ElementTree as ET

    root = ET.fromstring(r.render_routes(r.list_routes(TABLE)))
    texts = [t.text for t in root.iter("{http://www.w3.org/2000/svg}text")]
    assert "Rapid Bus routes (5)" in texts
    assert "200" in texts and "U2000" in texts and "no stops saved" in texts


def test_a_stops_map_has_the_stops_and_no_buses():
    import xml.etree.ElementTree as ET

    route = r.find_routes(TABLE, "200")[0]
    svg = r.stops_map(route, TABLE["stops"], lambda z, x, y: None)
    root = ET.fromstring(svg)
    texts = " ".join(t.text or "" for t in root.iter("{http://www.w3.org/2000/svg}text"))
    assert "4 stops, no live buses" in texts and "moving" not in texts and "Next stops are estimates" not in texts
    assert len(root.findall(".//{http://www.w3.org/2000/svg}circle")) == 4  # the shared end stop once


def test_an_svg_is_saved_under_out_with_a_clean_name(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "OUT_DIR", tmp_path / "out")
    path = r.save_svg("<svg/>", "170 (OS)", "2026-11-24T21:30:05+08:00")
    assert path == str(tmp_path / "out" / "rapidbus_170-OS_20261124_213005.svg")
    assert Path(path).read_text() == "<svg/>"


def run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr(r, "load_table", lambda *a: TABLE)
    monkeypatch.setattr("sys.argv", ["rapidbus_buses.py", *argv])
    r.main()
    return capsys.readouterr().out


def test_the_cli_prints_an_svg_or_saves_it(monkeypatch, capsys, tmp_path):
    out = run_cli(monkeypatch, capsys, "--routes", "--format", "svg")
    assert out.startswith("<svg") and out.rstrip().endswith("</svg>")
    monkeypatch.setattr(r, "OUT_DIR", tmp_path)
    saved = json.loads(run_cli(monkeypatch, capsys, "--routes", "--search", "bas", "--format", "svg", "--save"))
    assert saved["format"] == "svg" and Path(saved["file"]).read_text().startswith("<svg") and saved["bytes"] > 100
    assert json.loads(run_cli(monkeypatch, capsys, "--routes"))["count"] == 5  # json stays the default


def test_the_cli_rejects_mixed_up_format_options(monkeypatch, capsys):
    for argv in (["200", "--save"], ["200", "--format", "svg", "--map"]):
        with pytest.raises(SystemExit):
            run_cli(monkeypatch, capsys, *argv)
