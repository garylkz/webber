---
id: air-quality
type: module-design
status: draft
title: Air Quality Report
parent: webber-architecture
---

# Air Quality Report: Specification

The air quality of a place now, and optionally of the next hours, as one JSON
object. It started as the owner's global `air-quality.sh` agent script and was
moved here in Python.

Implemented in [air_quality_report.py](air_quality_report.py).

---

## Usage

```bash
lib/.venv/bin/python lib/air-quality/air_quality_report.py                       # Petaling Jaya
lib/.venv/bin/python lib/air-quality/air_quality_report.py --place "Kuala Lumpur"
lib/.venv/bin/python lib/air-quality/air_quality_report.py --address "Jalan Gasing, 46000 Petaling Jaya, Selangor"
lib/.venv/bin/python lib/air-quality/air_quality_report.py --lat 3.1 --lon 101.6 --hours 6
```

Give at most one of `--place`, `--address` or `--lat`/`--lon`. `--hours` is 0
to 48.

## Sources

- **Open-Meteo geocoding** turns a place name into a point, a label
  (name, state, country) and a country.
- **Open-Meteo air quality** gives the current US AQI, European AQI and six
  pollutants for the grid point, and the hourly US AQI with `--hours`. Outside
  Malaysia this is the whole report. It is a model value, not a street monitor.
- **DOE APIMS** (the ground-monitor network behind
  https://eqms.doe.gov.my/APIMS/main) gives the current reading of every
  Malaysian station in one public ArcGIS query. The nearest station with a
  reading gives `official_value` and `official_band` from its `API` and `CLASS`,
  and its name and distance as `official_station` and `official_distance_km`.
  Just after the hour a station can still be waiting for its reading, and it is
  then passed over for the next nearest. It is asked only when the
  geocoder says the place is in Malaysia, or, for a raw point, when the point
  falls in a loose box over both peninsulas. If APIMS does not answer, the two
  keys are left out and the rest of the report stands.

## Output

```json
{"label": "Petaling Jaya, Selangor, Malaysia", "latitude": 3.1, "longitude": 101.6,
 "time": "2026-11-16T00:00", "timezone": "Asia/Kuala_Lumpur",
 "us_aqi": 85, "us_aqi_band": "Moderate", "european_aqi": 60, "european_aqi_band": "Moderate",
 "pollutant_unit": "μg/m³",
 "pollutants": {"pm2_5": 28.1, "pm10": 30.2, "carbon_monoxide": 412.0,
                "nitrogen_dioxide": 14.3, "sulphur_dioxide": 6.1, "ozone": 52.0},
 "advice": "Air quality is acceptable. ...",
 "official_value": 55.0, "official_band": "Moderate",
 "official_station": "Petaling Jaya, SELANGOR", "official_distance_km": 2.4,
 "hourly": [{"time": "2026-11-16T00:00", "us_aqi": 85, "us_aqi_band": "Moderate"}]}
```

The four `official_` keys appear only inside Malaysia, and only when some
station has a reading. `hourly`
appears only with `--hours`. Any reading may be `null`, and its band is then
`no reading`.

## Functions

- `us_band`, `eu_band`: the EPA and EEA bands of a value.
- `advice`: what to do at a US AQI value. The Malaysian API bands use the same
  numbers.
- `address_candidates`: a street address split into names to geocode, from the
  whole address down to the region, with a leading postcode dropped. The
  geocoder reads a city or a district, so a full street address fails.
- `parse_geocode`, `nearest_station`, `build_report`: pure parsers of the three
  responses, tested offline.
- `report_for`: the whole run, with requests.

## Departures from the global script

- JSON only. The global script's `--format markdown` report is left to the
  agent, as webber prints no text output.
- `--address` replaces `--note`. webber cannot read the owner's vault, so the
  caller reads a note's `address` property and passes it here.

## Testing

`test_air_quality.py` runs the bands, the address splitter and the three parsers
over real responses for Petaling Jaya in `fixtures/`. The APIMS fixture is cut
down to the fields the parser reads, so it holds no station staff details.
