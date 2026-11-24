---
id: rapidbus
type: module-design
status: draft
title: Rapid Bus Live Positions
parent: webber-architecture
---

# Rapid Bus Live Positions: Specification

Where the buses on one Rapid Bus route are right now, read from the MyRapid
bus kiosk (`https://myrapidbus.prasarana.com.my/kiosk`), plus the route's
stops and a searchable list of routes.

Implemented in [rapidbus_buses.py](rapidbus_buses.py).

---

## Usage

```bash
lib/.venv/bin/python lib/rapidbus/rapidbus_buses.py 200              # live buses on route 200
lib/.venv/bin/python lib/rapidbus/rapidbus_buses.py U2000            # the same, by code
lib/.venv/bin/python lib/rapidbus/rapidbus_buses.py --stops T789     # stops, no request
lib/.venv/bin/python lib/rapidbus/rapidbus_buses.py T852 --map       # live, plus a map under out/
lib/.venv/bin/python lib/rapidbus/rapidbus_buses.py T852 --source feed  # the open data feed, not the kiosk
lib/.venv/bin/python lib/rapidbus/rapidbus_buses.py --routes --search kjl
```

`--format svg` answers with a picture instead of JSON, for a live route, `--stops`
or `--routes` (below). `--source` is `auto` (the default: the kiosk, and the open data feed if the kiosk
answers with its bot check), `kiosk` or `feed`.

## Output

Live mode:

```json
{"route": "200", "code": "U2000", "region": "RKL", "source": "kiosk", "fetched_at": "2026-11-23T20:12:00+08:00",
 "count": 4, "dropped_stale": 1,
 "next_stop_method": "estimated from straight lines between stops; distance_m is as the crow flies",
 "buses": [{"bus_no": "WVB4105", "lat": 3.169615, "lon": 101.6985116, "speed_kmh": 20.0,
            "heading": 187.0, "direction": "2", "gps_time": "2026-11-23T20:11:53+08:00",
            "age_seconds": 7, "at_stop": {"stop_id": "1000887", "name": "KL53 GRAND SEASONS HOTEL"},
            "at_terminus": null, "on_route": true, "accessible": true,
            "next_stop": {"stop_id": "1000477", "name": "KL69 PASAR CHOW KIT", "distance_m": 524}}]}
```

- `source` is `kiosk` or `open data feed`. A feed answer also has a `note` saying
  what is missing (below); without it, an empty list could be read as "no buses".
- `direction` is the kiosk's `1` or `2`, the same numbers `--stops` uses.
- `at_terminus` is `start`, `end` or null.
- `on_route` says whether the bus is on the route's drawn path.
- `accessible` is the kiosk's "disabled friendly" flag.
- `next_stop` is an estimate (below), or null when the bus is over 500 m from
  every line between stops (going to or from the depot, say) or the route
  lists no stops. `on_route` is null on routes whose snapshot leaves it out,
  such as the T feeders.

`--stops` gives the route's summary and `directions`: for each direction, its
stops in order (`stop_id`, `name`, `street`, `lat`, `lon`). `--routes` gives
`label`, `code`, `region`, `kiosk_ids` and the stop count of each route.

## Answering as JSON or SVG

`--format json` (the default) prints JSON as above. `--format svg` prints an
SVG on stdout instead:

| Asked for | The SVG is |
|---|---|
| a route | the live map: tiles, road path, stops, buses with next stops |
| a route with `--stops` | the same map with no buses ("N stops, no live buses") |
| `--routes` (with optional `--search`) | a table of labels, codes and stop counts, three columns |

`--save` writes the SVG to `out/rapidbus_<name>_<time>.svg` and prints
`{"format": "svg", "file": ..., "bytes": ...}` instead. The MCP tool always
saves: a map with its tiles is about 1 MB, too much to return as text.
`--map` still means JSON plus a saved map, and does not go with `--format svg`.
A route list or stops map needs no request; only the tiles are fetched.

## The source

The kiosk page has three parts, found by reading it in a browser:

1. `/kiosk` lists about 490 routes in a select box: a numeric kiosk id and the
   label signed on the bus (`200`, `T789`, `KJL6`, `170 (OS)`).
2. `/kiosk?route=<id>` writes the route's data into the page's script: its
   region (`RKL`, `RPG`, `RKN`), its AVL route code (`U2000`), the drawn path,
   the terminus polygons and the stop list (`var bstp`).
3. Live positions come from `GET /avl/api/avl/route/<code>`, a JSON snapshot
   of the route's buses, then a Pusher websocket at `/avl` on channel
   `avl.route.<code>`. The kiosk drops a bus whose fix is over 5 minutes old.

A label does not map to a code by any rule (`151` is `U1510`, `BAS1` is
`H1200`, `SEWA KHAS` is `SK`). Many labels repeat in the select box (`BAS1`
eight times), almost always with the same code; only `SEWA KHAS` has two
codes (`SK`, `S0008`). The repeats often differ only in whether the page
lists stops at all.

The November 2026 export found 493 kiosk entries, all in region `RKL` (the
kiosk lists no Penang or Kuantan routes), which merge into 368 routes with
6,491 stops. 74 of them list no stops.

## Design

- **The route table is saved, not fetched.** The kiosk pages sit behind
  Incapsula's bot check, which a script does not pass, so the table of
  labels, codes and stops ships as [rapidbus_routes.json](rapidbus_routes.json),
  one entry per label and code with every kiosk id behind it and the longest
  stop list among them. It was exported once from a browser (below). `--routes` and `--stops` read
  only that file. This departs from the other tools, which fetch everything
  they show.
- **One request per live call.** Live mode makes one GET to the snapshot
  endpoint and never follows a redirect. Incapsula also guards that endpoint
  and starts answering a plain client with its bot check (a 302 to a
  "Loading" page) after a short burst of requests. That answer is never shown
  as a route with no buses: in `auto` the call falls back to the open data feed
  (below), and with `--source kiosk` it is an error saying so.
- **A label that names several routes is an error** that lists their codes,
  so one call never fans out to several requests. A code the table does not
  know is tried as given, so a new route works before the table is
  refreshed.
- **The driver's id is dropped.** The snapshot carries `captain_id`, which
  looks like the driver's staff number. The tool never passes it on. It also
  leaves out `trip_no`, `trip_rev_kind`, `engine_status` and `source`, whose
  meaning is not known.
- **Clean records.** A record with no plate, a missing or out-of-range
  coordinate, an unreadable time, a fix over 5 minutes old or over a minute in
  the future is dropped and counted in `dropped_stale`. When a bus appears
  twice, its newest fix wins. Times are Malaysia time (UTC+8).

## The open data feed

Malaysia's open data portal publishes Rapid Bus positions as a GTFS Realtime
feed: `https://api.data.gov.my/gtfs-realtime/vehicle-position/prasarana/`,
with `?category=rapid-bus-mrtfeeder` (the T feeders) or `rapid-bus-kl`. It has
no bot check, so it is the fallback when the kiosk blocks the script. The
answer is a protobuf message; the few fields used are decoded by hand in
`proto_fields` and `feed_snapshot`, so no protobuf library is needed.

- **Route names differ.** The feeder feed names a route by its label (`T852`),
  the other feed by its code (`U2000`), so a bus matches if its route id is
  either. The two categories are asked in turn until one has the route's buses.
- **Speed is already km/h**, not metres per second as the GTFS Realtime
  standard says: a moving bus reads 29, and the kiosk agrees.
- **Fewer buses, older fixes.** In a November 2026 comparison on T852 the feed
  had 3 of the kiosk's 6 buses, and its fixes ran 20 to 90 seconds behind
  (a bus the kiosk showed at 17 km/h was stopped in the feed). Where both had
  a bus, the positions matched to the last digit.
- **Less detail.** No direction, at-stop or at-terminus flag, and no on-route
  flag. The next-stop estimate works from position and heading alone, and a
  feed map says "open data feed: partial, older fixes" in its header.

## Next stop

The kiosk does not say which stop a bus heads for, so the tool estimates it
from the saved stop list, without a request:

1. Each direction's stops, in order, make a chain of straight lines. A chain
   carries on into the first stops of the other direction, since a bus that
   finishes one direction starts the other.
2. Each line is scored by the bus's distance to it. A line of the other
   direction counts as 100 m farther, and for a bus moving at 3 km/h or more,
   a line pointing over 90 degrees away from its heading counts as 300 m
   farther.
3. The lowest score wins and the line's end is the next stop. If the bus is
   within 40 m of that stop, it is at it, and the stop after is the next one.

The snapshot's `dir` counts for little because it is often stale: on T852 a
bus labelled direction 1 was driving into the terminus that ends direction 2.
The label seems to change only at a terminus. On route 200 a bus labelled direction 2
(northbound) was driving south, and the snapshot named a northbound stop as
the one it was at; the estimate followed the heading to the southbound stop
ahead. Where the road between two stops
bends far from the straight line, the estimate can name the wrong stop.
`distance_m` is as the crow flies, so shorter than the drive. The route's real
path, which the kiosk pages carry, would remove both errors but would mean
re-exporting the table.

## Map

`--map` also draws the live result as an SVG, `out/rapidbus_<route>_<time>.svg`
at the repo root, and adds its path to the JSON as `map`. SVG keeps the tool
free of an imaging library; it opens in any browser.

- **Background.** OpenStreetMap's standard tiles, embedded in the file as
  PNG data, so the SVG stands alone (about 1 MB). The zoom is the closest at
  which every stop and drawn bus fits the 1000 x 760 map. Tiles are cached
  under `out/rapidbus-tiles/` for a week, as OSM's tile policy asks, and the
  map carries its attribution. A tile that cannot be fetched is left blank and
  counted on the map; it never fails the run.
- **Route.** The route's road path from
  [rapidbus_paths.json](rapidbus_paths.json), with arrows showing the way it
  runs. A route with no saved path falls back to one straight line per
  direction through its stops, and the header says so. On routes of up to 40
  stops each stop is named, unless the name would overlap
  a bus label or another name; every stop has its name as a hover title.
- **Buses.** An arrow turned to the bus's heading, orange when moving at 3
  km/h or more, grey when not, labelled with the plate, next stop, speed and
  GPS age. Labels of buses on the same spot stack, with a leader line.
- **Off the route.** A bus with no next-stop estimate is not drawn, so one
  bus at the depot does not shrink the route; the header names it.

## Road paths

Each kiosk route page carries its road path as `rawShapes`: one line under key
`02` covering the whole trip, out and back (T852: 212 points, route 200: 562).
[rapidbus_paths.json](rapidbus_paths.json) holds one per route code,
`{"captured", "source", "tolerance_m", "paths": {code: [[lat, lon], ...]}}`,
taken from the kiosk entry with the longest path and simplified
(Douglas-Peucker) to stay within 5 m of the original, with coordinates rounded
to 5 decimals (about 1 m). It is exported from the browser the same way as the
route table (below) and is used only for drawing; the next-stop estimate does
not use it yet.

## Refreshing the route table

Open `https://myrapidbus.prasarana.com.my/kiosk` in a browser and run this in
its console. It fetches one route page every 1.5 seconds (about 20 minutes)
and builds the table into `window.TABLE`:

```js
function grab(h, name) {
  const k = 'var ' + name + ' = ', i = h.indexOf(k);
  if (i < 0) return null;
  const s = i + k.length;
  return JSON.parse(h.slice(s, h.indexOf('];', s) + 1));
}
(async () => {
  const opts = [...document.querySelectorAll('#route option')].filter(o => o.value);
  const routes = {}, stops = {};
  for (const o of opts) {
    const h = await fetch('/kiosk?route=' + o.value).then(r => r.text());
    const code = (h.match(/var no_route = [^;]*\?\s*'([^']*)'/) || [])[1];
    const region = (h.match(/var prm = '([^']*)'/) || [])[1];
    const bstp = grab(h, 'bstp') || [];
    for (const s of bstp) stops[s.stop_id] = {name: s.stop_name.trim(), street: s.street_name.trim(), lat: s.lat, lon: s.lng};
    if (!code) continue;
    const key = o.text.trim() + '|' + code;
    const e = routes[key] ||= {label: o.text.trim(), code, region, kiosk_ids: [], stops: []};
    e.kiosk_ids.push(o.value);
    if (bstp.length > e.stops.length) e.stops = bstp.map(s => [String(s.stop_id), String(s.dr)]);
    await new Promise(r => setTimeout(r, 1500));
  }
  window.TABLE = {captured: new Date().toISOString().slice(0, 10), source: location.href,
                  routes: Object.values(routes), stops};
})();
```

Then `copy(JSON.stringify(TABLE))` and save it over `rapidbus_routes.json`.
The table keeps every stop any kiosk entry lists; the November 2026 file
holds only the stops of the merged routes' chosen lists.

## Testing

`test_rapidbus.py` runs route lookup, the stop list and the snapshot parser
over `fixtures/`: a real U2000 snapshot (driver ids redacted, one stale bus
appended) and a trimmed route table. The bot-check answer is simulated by
replacing `requests.get`. The next-stop tests use a made-up loop route, and
the map tests render it with a stand-in tile source and parse the SVG. Nothing
in it touches the network.

## Not covered

- **A route id the feed names differently** from its kiosk label and code.
  Such a route returns no buses from the feed.
- **Arrival times.** The next stop comes without an arrival time. The kiosk calls `POST /website/getBusstopEta` only for
  one route (`U8210`), in rough 5/10/15/30-minute bands. Untested.
- **Route paths and terminus polygons**, which are in the route pages but are
  not needed to say where a bus is.
- **The websocket stream.** One snapshot per call is enough for an agent.
- **A live feed call in the tests.** The decoder is tested on messages the
  tests encode themselves; the real feed was checked by hand.
