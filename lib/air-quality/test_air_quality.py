"""Offline tests for air_quality_report: bands, places, the nearest station and the report.

The fixtures are real responses for Petaling Jaya: the Open-Meteo geocoder, the
Open-Meteo air quality API with six hours ahead, and a few APIMS stations cut
down to the fields the parser reads.
"""

import json
from pathlib import Path

import pytest

import air_quality_report as a

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


GEOCODE = load("air_quality_geocode_petaling_jaya.json")
AIR = load("air_quality_air_petaling_jaya.json")
APIMS = load("air_quality_apims_sample.json")


# --- bands -------------------------------------------------------------------

@pytest.mark.parametrize("value, band", [
    (None, "no reading"), (0, "Good"), (50, "Good"), (51, "Moderate"), (100, "Moderate"),
    (101, "Unhealthy for sensitive groups"), (151, "Unhealthy"), (201, "Very Unhealthy"), (301, "Hazardous"),
])
def test_us_bands(value, band):
    assert a.us_band(value) == band


@pytest.mark.parametrize("value, band", [
    (None, "no reading"), (20, "Good"), (21, "Fair"), (41, "Moderate"), (61, "Poor"),
    (81, "Very Poor"), (101, "Extremely Poor"),
])
def test_eu_bands(value, band):
    assert a.eu_band(value) == band


def test_advice_follows_the_us_numbers():
    assert a.advice(None) == "The API sent no reading."
    assert a.advice(40).startswith("Air quality is good.")
    assert a.advice(120).startswith("Sensitive groups should stay indoors.")
    assert a.advice(400) == a.WORST_ADVICE


# --- places ------------------------------------------------------------------

def test_an_address_is_narrowed_to_names_the_geocoder_knows():
    assert a.address_candidates("12, Jalan SS 2/24, 47300 Petaling Jaya, Selangor") == [
        "12, Jalan SS 2/24, 47300 Petaling Jaya, Selangor",
        "Jalan SS 2/24, 47300 Petaling Jaya, Selangor",
        "Petaling Jaya, Selangor",
        "Petaling Jaya",
        "Selangor",
    ]
    assert a.address_candidates("Petaling Jaya") == ["Petaling Jaya"]


def test_the_geocoder_answer():
    point = a.parse_geocode(GEOCODE)
    assert point["label"] == "Petaling Jaya, Selangor, Malaysia"
    assert point["country"] == "Malaysia"
    assert isinstance(point["latitude"], float)
    assert a.parse_geocode({"generationtime_ms": 0.1}) is None


def test_malaysia_by_country_or_by_box():
    assert a.in_malaysia("Malaysia", 0, 0)
    assert not a.in_malaysia("Singapore", 1.3, 103.8)
    assert a.in_malaysia("", 3.1, 101.6)
    assert not a.in_malaysia("", 51.5, -0.1)


# --- the official reading ----------------------------------------------------

def test_the_nearest_station_is_petaling_jaya():
    point = a.parse_geocode(GEOCODE)
    reading = a.nearest_station(APIMS, point["latitude"], point["longitude"])
    nearest = APIMS["features"][0]["attributes"]
    assert nearest["STATION_LOCATION"].startswith("Petaling Jaya")
    assert reading == {"official_value": float(nearest["API"]), "official_band": nearest["CLASS"]}


def test_no_station_or_a_bad_value():
    assert a.nearest_station({"features": []}, 3, 101) is None
    odd = {"features": [{"attributes": {"API": "n/a", "CLASS": None, "LATITUDE": 3, "LONGITUDE": 101}}]}
    assert a.nearest_station(odd, 3, 101) == {"official_value": None, "official_band": "n/a"}


# --- the report --------------------------------------------------------------

def test_the_report_keeps_every_reading():
    report = a.build_report(AIR, "Petaling Jaya, Selangor, Malaysia", hours=6,
                            official={"official_value": 55.0, "official_band": "Moderate"})
    current = AIR["current"]
    assert report["us_aqi"] == current["us_aqi"] and report["us_aqi_band"] == a.us_band(current["us_aqi"])
    assert report["european_aqi_band"] == a.eu_band(current["european_aqi"])
    assert report["pollutants"] == {name: current[name] for name in a.POLLUTANTS}
    assert report["pollutant_unit"] == AIR["current_units"]["pm2_5"]
    assert report["advice"] == a.advice(current["us_aqi"])
    assert report["official_value"] == 55.0 and report["official_band"] == "Moderate"
    assert len(report["hourly"]) == 6
    assert all(h["time"] >= current["time"] for h in report["hourly"])
    assert report["hourly"][0]["us_aqi_band"] == a.us_band(report["hourly"][0]["us_aqi"])


def test_no_hours_and_no_official_reading():
    report = a.build_report(AIR, "the given point")
    assert "hourly" not in report and "official_value" not in report


def test_the_hourly_request_asks_for_three_days():
    assert a.air_params(3, 101, 6)["forecast_days"] == 3
    assert a.air_params(3, 101, 0) == {**a.air_params(3, 101, 0), "forecast_days": 1}
    assert "hourly" not in a.air_params(3, 101, 0)
