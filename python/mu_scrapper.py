"""Scrape Meetup events from the public gql2 endpoint (no login / API key).

Merges the old mtp_events.py (nearby upcoming events) and mtp_dump.py
(country-wide JSON dump) into one tool. It calls Meetup's unauthenticated
frontend GraphQL interface (https://www.meetup.com/gql2), method 2a in
notes/topics/meetup-scraping.md.

Two modes:
  default (list)   print nearby upcoming events, newest relevance first.
  --dump           scan a whole country via geo anchors and write JSON.

Defaults to Kuala Lumpur, Malaysia (country code "my"), 25 mi radius, which
covers KL and the close parts of Selangor. The underlying EventSearchFilter
supports: query, country, city, state, zip, lat/lon, radius, eventType
(PHYSICAL|ONLINE|HYBRID), isHappeningNow, and a start/end date range.

Examples:
  python mu_scrapper.py                          # nearby upcoming events near KL
  python mu_scrapper.py --query python           # keyword filter
  python mu_scrapper.py --radius 10 --event-type ONLINE
  python mu_scrapper.py --country sg --city "Singapore" --lat 1.3521 --lon 103.8198
  python mu_scrapper.py --start-date 2026-08-20 --end-date 2026-09-20 --dump
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0"}
URL = "https://www.meetup.com/gql2"
TIMEOUT = 20
DELAY = 0.3
PAGE = 50
OUT_DIR = Path(__file__).resolve().parent / "out"

DEFAULT = {"city": "Kuala Lumpur", "country": "my", "lat": 3.139, "lon": 101.6869}
DEFAULT_LIST_RADIUS = 25
DEFAULT_DUMP_RADIUS = 500
DEFAULT_LIST_MAX = 20
DEFAULT_DUMP_MAX = 500

ANCHORS = [
    ("Kuala Lumpur", 3.139, 101.6869), ("George Town (Penang)", 5.4141, 100.3288),
    ("Johor Bahru", 1.4927, 103.7418), ("Ipoh", 4.5975, 101.0901),
    ("Malacca", 2.1896, 102.2501), ("Kuantan", 3.8077, 103.3260),
    ("Kota Bharu", 6.1256, 102.2381), ("Kuala Terengganu", 5.3303, 103.1407),
    ("Alor Setar", 6.1244, 100.3678), ("Seremban", 2.7258, 101.9424),
    ("Kota Kinabalu", 5.9804, 116.0735), ("Kuching", 1.5535, 110.3593),
    ("Miri", 4.3995, 113.9914), ("Bintulu", 3.1865, 113.5473),
    ("Sandakan", 5.8394, 118.1179), ("Tawau", 4.2448, 117.8910),
    ("Labuan", 5.2831, 115.2308),
]

# Discard an event unless its venue country is the target code. When the
# country is missing, fall back to these bounding boxes for Malaysia.
MY_BOXES = ((1.0, 6.7, 99.6, 104.6), (0.85, 7.36, 109.4, 119.4))

ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize(text):
    """Strip escape sequences and control chars from untrusted strings
    before printing them to the terminal."""
    return CTRL_RE.sub("", ANSI_RE.sub("", text or ""))


QUERY = """query($filter: EventSearchFilter!, $first: Int!, $after: String) {
  eventSearch(filter: $filter, first: $first, after: $after) {
    totalCount
    pageInfo { endCursor hasNextPage }
    edges {
      node {
        id
        title
        dateTime
        endTime
        duration
        eventType
        isOnline
        status
        eventUrl
        venue { name city state country lat lon }
        group { name urlname }
        rsvps { totalCount }
      }
    }
  }
}"""


def fetch_page(filt, first, after):
    r = requests.post(URL, json={"query": QUERY, "variables": {"filter": filt, "first": first, "after": after}},
                      headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError as exc:
        raise RuntimeError(f"non-JSON response (HTTP {r.status_code})") from exc
    if "errors" in data:
        raise RuntimeError(data["errors"][0]["message"][:300])
    return data["data"]["eventSearch"]


def search_events(filt, first, max_results):
    """Paginate one geo anchor. Return (nodes, total_count)."""
    page = fetch_page(filt, first, None)
    nodes, cursor = [], page["pageInfo"]["endCursor"]
    while page["edges"] and len(nodes) < max_results:
        nodes.extend(e["node"] for e in page["edges"])
        if not page["pageInfo"]["hasNextPage"] or len(nodes) >= max_results:
            break
        time.sleep(DELAY)
        page = fetch_page(filt, first, cursor)
        cursor = page["pageInfo"]["endCursor"]
    return nodes[:max_results], page["totalCount"]


def in_country(venue, code):
    if not venue:
        return False
    country = (venue.get("country") or "").upper()
    if country:
        return country == code.upper()
    if code.upper() == "MY":
        lat, lon = venue.get("lat"), venue.get("lon")
        if lat is None or lon is None:
            return False
        return any(lo <= lat <= hi and lo2 <= lon <= hi2 for lo, hi, lo2, hi2 in MY_BOXES)
    return False


def parse_date(value, end=False):
    """Accept YYYY-MM-DD (treated as local midnight / end-of-day) or a full
    ISO 8601 datetime. Return an ISO string with a timezone offset, which is
    what Meetup's ZonedDateTime scalar requires."""
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T23:59:59" if end else "T00:00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat()


def print_events(nodes, city, lat, lon, radius, query):
    print(f"location: {city} ({lat}, {lon}), radius {radius} mi, query: {query if query != '*' else '<all>'}")
    print(f"total matching: {len(nodes)} (fetched)")
    print()
    for n in nodes:
        venue = n.get("venue")
        venue_s = sanitize(f"{venue['name']} ({venue['city']})") if venue else "online/unknown"
        print(f"- {sanitize(n['title'])}")
        print(f"    {sanitize(n['dateTime'])}  [{sanitize(n['eventType'])}]  {sanitize(n['group']['name'])} "
              f"({sanitize(n['group']['urlname'])})  rsvps:{n['rsvps']['totalCount']}")
        print(f"    {venue_s}")


def dump_country(filt, radius, cap, country):
    """Scan every anchor and keep only events inside `country`. Dedupe by id."""
    anchors = ANCHORS if country.upper() == "MY" else [("", filt["lat"], filt["lon"])]
    seen = {}
    summary = []

    for name, lat, lon in anchors:
        anchor_filt = dict(filt, lat=lat, lon=lon, radius=radius)
        if name:
            anchor_filt["city"] = name
        page = fetch_page(anchor_filt, PAGE, None)
        total = page["totalCount"]
        collected = []
        nodes = [e["node"] for e in page["edges"]]
        cursor = page["pageInfo"]["endCursor"]
        while nodes and len(collected) < cap:
            for n in nodes:
                if not in_country(n.get("venue"), country):
                    continue
                if n["id"] not in seen:
                    seen[n["id"]] = n
                    collected.append(n)
            if not page["pageInfo"]["hasNextPage"] or len(collected) >= cap:
                break
            time.sleep(DELAY)
            page = fetch_page(anchor_filt, PAGE, cursor)
            nodes = [e["node"] for e in page["edges"]]
            cursor = page["pageInfo"]["endCursor"]
        summary.append((name or f"{lat:.4f},{lon:.4f}", total, len(collected)))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"mtp_events_{country}_{ts}.json"
    out_path.write_text(json.dumps(list(seen.values()), ensure_ascii=False, indent=2), encoding="utf-8")

    for name, total, got in summary:
        print(f"{name}: {total} matching, {got} new in-country unique")
    print(f"\ntotal unique events in {country.upper()}: {len(seen)}")
    print(f"wrote: {out_path}")


def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--query", default="*", help="keyword filter (default: '*' = all events)")
    p.add_argument("--country", default=DEFAULT["country"], help="ISO country code (default: my)")
    p.add_argument("--city", default=DEFAULT["city"], help="city name (default: Kuala Lumpur)")
    p.add_argument("--state", default=None, help="state/province name (e.g. Selangor)")
    p.add_argument("--zip", default=None, help="postal code")
    p.add_argument("--lat", type=float, default=DEFAULT["lat"], help="latitude")
    p.add_argument("--lon", type=float, default=DEFAULT["lon"], help="longitude")
    p.add_argument("--radius", type=float, default=None,
                   help="search radius in miles (list default 25, dump default 500)")
    p.add_argument("--event-type", choices=["PHYSICAL", "ONLINE", "HYBRID"], default=None,
                   help="filter by event type")
    p.add_argument("--happening-now", action="store_true", help="only events happening right now")
    p.add_argument("--start-date", default=None,
                   help="start of date range, YYYY-MM-DD or ISO datetime (local time)")
    p.add_argument("--end-date", default=None,
                   help="end of date range, YYYY-MM-DD or ISO datetime (local time)")
    p.add_argument("--max", type=int, default=None,
                   help="max results (list default 20, dump default 500 per anchor)")
    p.add_argument("--dump", action="store_true",
                   help="country-wide scan: iterate geo anchors and write JSON to out/")
    return p


def main():
    args = build_parser().parse_args()

    filt = {"query": args.query, "city": args.city, "country": args.country,
            "lat": args.lat, "lon": args.lon}
    if args.state:
        filt["state"] = args.state
    if args.zip:
        filt["zip"] = args.zip
    if args.event_type:
        filt["eventType"] = args.event_type
    if args.happening_now:
        filt["isHappeningNow"] = True
    if args.start_date:
        filt["startDateRange"] = parse_date(args.start_date, end=False)
    if args.end_date:
        filt["endDateRange"] = parse_date(args.end_date, end=True)

    if args.dump:
        radius = args.radius if args.radius is not None else DEFAULT_DUMP_RADIUS
        cap = args.max if args.max is not None else DEFAULT_DUMP_MAX
        dump_country(filt, radius, cap, args.country)
        return

    radius = args.radius if args.radius is not None else DEFAULT_LIST_RADIUS
    max_results = args.max if args.max is not None else DEFAULT_LIST_MAX
    filt["radius"] = radius

    nodes, _ = search_events(filt, min(PAGE, max_results), max_results)
    print_events(nodes, args.city, args.lat, args.lon, radius, args.query)


if __name__ == "__main__":
    main()
