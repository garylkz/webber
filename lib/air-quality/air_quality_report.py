#!/usr/bin/env python3
"""Report the air quality of a place now, and optionally of the next hours.

Usage:
    python air_quality_report.py                                # Petaling Jaya
    python air_quality_report.py --place "Kuala Lumpur"
    python air_quality_report.py --address "12 Jalan SS 2/24, 47300 Petaling Jaya, Selangor"
    python air_quality_report.py --lat 3.1 --lon 101.6 --hours 6

Prints one JSON object on stdout as its only output. The place is geocoded
with Open-Meteo, and its readings come from the Open-Meteo air quality API.
Inside Malaysia the official reading of the nearest DOE APIMS station with a
reading is added as official_value and official_band, with official_station
and official_distance_km. Errors go to stderr.
"""

import argparse
import json
import math
import re

import requests

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
# APIMS is the DOE ground-monitor network behind https://eqms.doe.gov.my/APIMS/main.
# This ArcGIS query returns the current reading of every station in one public GET.
APIMS_URL = ("https://eqms.doe.gov.my/api3/publicmapproxy/PUBLIC_DISPLAY/"
             "CAQM_MCAQM_Current_Reading/MapServer/0/query")
APIMS_PARAMS = {"f": "json", "outFields": "*", "returnGeometry": "false",
                "spatialRel": "esriSpatialRelIntersects", "where": "1=1"}
DEFAULT_PLACE = "Petaling Jaya"
MAX_HOURS = 48
POLLUTANTS = ("pm2_5", "pm10", "carbon_monoxide", "nitrogen_dioxide", "sulphur_dioxide", "ozone")
POSTCODE_RE = re.compile(r"^\d{5,6}\s+")

US_BANDS = ((50, "Good"), (100, "Moderate"), (150, "Unhealthy for sensitive groups"),
            (200, "Unhealthy"), (300, "Very Unhealthy"))
EU_BANDS = ((20, "Good"), (40, "Fair"), (60, "Moderate"), (80, "Poor"), (100, "Very Poor"))
ADVICE = (
    (50, "Air quality is good. Open the windows and do what you like outdoors."),
    (100, "Air quality is acceptable. Most people notice nothing. Unusually sensitive people "
          "should cut a long run short."),
    (150, "Sensitive groups should stay indoors. That means children, the elderly, and anyone "
          "with a heart or lung condition. Everyone else can go out as normal."),
    (200, "Everyone may feel effects. Children, the elderly, and anyone with a heart or lung "
          "condition should stay indoors. An N95 type mask cuts the particle load. Keep windows "
          "shut while PM2.5 is above 50 and run a HEPA filter indoors."),
    (300, "Everyone should stay indoors. An N95 type mask is not enough for a long time outdoors. "
          "Run a HEPA filter indoors, keep windows shut, and postpone any outdoor exercise."),
)
WORST_ADVICE = ("Stay indoors with the windows shut. Run a HEPA filter. A respirator of N95 type "
                "or better helps outside. Seek medical advice for a chest pain, a cough, or "
                "trouble breathing.")


# --- bands -------------------------------------------------------------------

def band(value, bands, worst):
    if value is None:
        return "no reading"
    for limit, name in bands:
        if value <= limit:
            return name
    return worst


def us_band(value):
    """The EPA band of a US AQI value."""
    return band(value, US_BANDS, "Hazardous")


def eu_band(value):
    """The EEA band of a European AQI value."""
    return band(value, EU_BANDS, "Extremely Poor")


def advice(value):
    """What to do at a US AQI value. The Malaysian API bands use the same numbers."""
    if value is None:
        return "The API sent no reading."
    return band(value, ADVICE, WORST_ADVICE)


# --- places ------------------------------------------------------------------

def address_candidates(address):
    """Names to geocode for a street address, whole address first, then down to the region.

    The geocoder reads a city or a district, so a full street address fails. A
    leading postcode such as 46300 is dropped from the shorter names.
    """
    parts = [p.strip() for p in address.split(",") if p.strip()]

    def clean(part):
        return POSTCODE_RE.sub("", part)

    names = [", ".join(parts)]
    if len(parts) > 3:
        names.append(", ".join(parts[-3:]))
    if len(parts) > 2:
        names.append(f"{clean(parts[-2])}, {parts[-1]}")
    if len(parts) > 1:
        names.append(clean(parts[-2]))
    if parts:
        names.append(clean(parts[-1]))
    seen = []
    for name in names:
        if name and name not in seen:
            seen.append(name)
    return seen


def parse_geocode(body):
    """An Open-Meteo geocoding response -> {latitude, longitude, label, country}, or None."""
    results = (body or {}).get("results") or []
    if not results:
        return None
    r = results[0]
    label = ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country")) if x)
    return {"latitude": r["latitude"], "longitude": r["longitude"], "label": label,
            "country": r.get("country") or ""}


def geocode(names, timeout=15):
    """Geocode each name in turn and keep the first hit, or None."""
    for name in names:
        try:
            resp = requests.get(GEOCODE_URL, params={"name": name, "count": 1, "language": "en",
                                                     "format": "json"}, timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException:
            continue
        hit = parse_geocode(resp.json())
        if hit:
            return hit
    return None


def in_malaysia(country, lat, lon):
    """The geocoder's country when there is one; for a raw point, a loose box over both peninsulas.

    The box also takes Singapore and Brunei, but a raw point is the rare case.
    """
    if country:
        return country == "Malaysia"
    return 0.3 <= lat <= 7.5 and 99.5 <= lon <= 119.5


# --- the official Malaysian reading ------------------------------------------

def api_value(attributes):
    """A station's API as a float, or None when it has no reading yet."""
    try:
        return float(attributes.get("API"))
    except (TypeError, ValueError):
        return None


def nearest_station(body, lat, lon):
    """The reading of the nearest APIMS station that has one, or None.

    Just after the hour a station can still be waiting for its reading, so a
    station without one is passed over for the next nearest. The station's name
    and distance go with the reading, so a farther station is plain to see.
    """
    best, best_d = None, None
    for feature in (body or {}).get("features") or []:
        a = feature.get("attributes") or {}
        if a.get("LATITUDE") is None or a.get("LONGITUDE") is None or api_value(a) is None:
            continue
        dy = (a["LATITUDE"] - lat) * 111.32
        dx = (a["LONGITUDE"] - lon) * 111.32 * math.cos(math.radians((a["LATITUDE"] + lat) / 2))
        d = math.hypot(dx, dy)
        if best_d is None or d < best_d:
            best, best_d = a, d
    if best is None:
        return None
    return {"official_value": api_value(best), "official_band": best.get("CLASS") or "n/a",
            "official_station": best.get("STATION_LOCATION") or best.get("STATION_ID") or "unknown",
            "official_distance_km": round(best_d, 1)}


def official_reading(lat, lon, timeout=20):
    """The nearest station's reading, or None when APIMS does not answer or has no reading."""
    try:
        resp = requests.get(APIMS_URL, params=APIMS_PARAMS, timeout=timeout)
        resp.raise_for_status()
        return nearest_station(resp.json(), lat, lon)
    except (requests.RequestException, ValueError):
        return None


# --- the report --------------------------------------------------------------

def air_params(lat, lon, hours):
    params = {"latitude": lat, "longitude": lon, "current": ",".join(POLLUTANTS + ("us_aqi", "european_aqi")),
              "timezone": "auto"}
    if hours:
        # One forecast day ends at local midnight, so up to 48 hours from now needs 3.
        params.update(hourly="us_aqi", forecast_days=3)
    else:
        params["forecast_days"] = 1
    return params


def build_report(air, label, hours=0, official=None):
    """The report from an Open-Meteo air quality response. Pure: no request."""
    current = air["current"]
    report = {
        "label": label,
        "latitude": air.get("latitude"),
        "longitude": air.get("longitude"),
        "time": current.get("time"),
        "timezone": air.get("timezone"),
        "us_aqi": current.get("us_aqi"),
        "us_aqi_band": us_band(current.get("us_aqi")),
        "european_aqi": current.get("european_aqi"),
        "european_aqi_band": eu_band(current.get("european_aqi")),
        "pollutant_unit": (air.get("current_units") or {}).get("pm2_5"),
        "pollutants": {name: current.get(name) for name in POLLUTANTS},
        "advice": advice(current.get("us_aqi")),
    }
    if official:
        report.update(official)
    if hours:
        times = (air.get("hourly") or {}).get("time") or []
        values = (air.get("hourly") or {}).get("us_aqi") or []
        ahead = [{"time": t, "us_aqi": v, "us_aqi_band": us_band(v)}
                 for t, v in zip(times, values) if t >= (current.get("time") or "")][:hours]
        if ahead:
            report["hourly"] = ahead
    return report


def read_air(lat, lon, hours, timeout=20):
    try:
        resp = requests.get(AIR_URL, params=air_params(lat, lon, hours), timeout=timeout)
        resp.raise_for_status()
        air = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise SystemExit(f"No answer from the air quality API: {e}") from e
    if not isinstance(air.get("current"), dict):
        raise SystemExit("The air quality API sent no current reading.")
    return air


def report_for(place=None, address=None, lat=None, lon=None, hours=0):
    if lat is not None:
        point = {"latitude": lat, "longitude": lon, "label": "the given point", "country": ""}
    else:
        names = address_candidates(address) if address else [place or DEFAULT_PLACE]
        point = geocode(names)
        if point is None:
            if address:
                raise SystemExit(f"The address does not name a place the geocoder knows: {address}")
            raise SystemExit(f"No place called {names[0]!r}. Give a city or a district name.")
    official = None
    if in_malaysia(point["country"], point["latitude"], point["longitude"]):
        official = official_reading(point["latitude"], point["longitude"])
    air = read_air(point["latitude"], point["longitude"], hours)
    return build_report(air, point["label"], hours, official)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--place", help="a city or district, such as 'Kuala Lumpur'")
    where.add_argument("--address", help="a street address; it is narrowed down until the geocoder knows it")
    where.add_argument("--lat", type=float, help="latitude; needs --lon")
    parser.add_argument("--lon", type=float, help="longitude; needs --lat")
    parser.add_argument("--hours", type=int, default=0, help=f"also the US AQI of the next N hours, at most {MAX_HOURS}")
    args = parser.parse_args()
    if (args.lat is None) != (args.lon is None):
        parser.error("--lat and --lon must be given together")
    if args.lon is not None and (args.place or args.address):
        parser.error("give --place, --address, or --lat/--lon, not more than one")
    if not 0 <= args.hours <= MAX_HOURS:
        parser.error(f"--hours must be from 0 to {MAX_HOURS}")
    report = report_for(args.place, args.address, args.lat, args.lon, args.hours)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
