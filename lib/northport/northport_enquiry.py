#!/usr/bin/env python3
"""Northport (Port Klang) e-NP2 CAS-N4 enquiries: container, B/L, vessel, booking.

Usage:
    python northport_enquiry.py container ABCD1234567   # one container
    python northport_enquiry.py bl SOMEBL123            # one bill of lading
    python northport_enquiry.py vessel MV-EXAMPLE       # one vessel or SCN
    python northport_enquiry.py booking BOOKING123      # one booking, with its containers

Prints a JSON object on stdout as its only output: the portal's own labels
mapped to their values. A booking also carries "Containers", a list of
{"Container Number", ...} objects. An unknown number gives an empty object.

The portal answers each enquiry with an HTML fragment, not JSON, made of
`<p>Label : <span>value</span></p>` rows. Needs `requests`. Errors go to stderr.
"""

from __future__ import annotations

import argparse
import json
import re
from html import unescape

import requests

BASE_URL = "https://apps.northport.com.my/e-NP2/"
USER_AGENT = "Mozilla/5.0"
TIMEOUT = 30
# The portal answers its own page's XHR calls; the header marks ours as one.
HEADERS = {"X-Requested-With": "XMLHttpRequest"}

_ROW = re.compile(r"<p[^>]*>\s*(.*?)\s*:\s*(?:<br>\s*)?<span>(.*?)</span>", re.S)
_CHILD = re.compile(
    r"<div class='div_query_output_parent'[^>]*>.*?<span>(.*?)</span>.*?"
    r"<div id='divvvv\d+' class='div_query_output_child'>(.*?)</div>",
    re.S,
)
_TAG = re.compile(r"<[^>]+>")

# --------------------------------------------------------------- parsers ----


def parse_rows(html: str) -> dict[str, str]:
    """An enquiry fragment -> {label: value}. The first of a repeated label wins."""
    out: dict[str, str] = {}
    for label, value in _ROW.findall(html):
        label = unescape(_TAG.sub("", label)).strip()
        out.setdefault(label, unescape(value).strip())
    return out


def parse_booking(html: str) -> dict:
    """A booking fragment -> its header rows plus a "Containers" list."""
    header, _, rest = html.partition("Container List")
    containers = [
        {"Container Number": unescape(no).strip(), **parse_rows(body)}
        for no, body in _CHILD.findall(rest)
    ]
    return {**parse_rows(header), "Containers": containers}


# --------------------------------------------------------------- network ----


_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT


def post_form(endpoint: str, field: str, value: str) -> str:
    """POST one form field to an enquiry page and return the fragment. Failure exits."""
    url = BASE_URL + endpoint
    try:
        resp = _session.post(
            url, data={field: value.strip().upper()}, headers=HEADERS, timeout=TIMEOUT
        )
    except requests.Timeout as exc:
        raise SystemExit(f"timed out fetching {url}") from exc
    except requests.RequestException as exc:
        raise SystemExit(f"network error fetching {url}: {exc}") from exc
    if not resp.ok:
        raise SystemExit(f"HTTP {resp.status_code} from {url}")
    return resp.content.decode("utf-8", "replace")


def get_by_container(container_no: str) -> dict[str, str]:
    return parse_rows(post_form("query_contN4.php", "cont_num_N4", container_no))


def get_by_bl(bl_no: str) -> dict[str, str]:
    return parse_rows(post_form("query_contblN4.php", "cont_bl_N4", bl_no))


def get_by_vessel(scn_or_vessel_name: str) -> dict[str, str]:
    return parse_rows(post_form("query_vesselN4.php", "vsl_num_N4", scn_or_vessel_name))


def get_by_booking(booking_ref: str) -> dict:
    return parse_booking(post_form("query_ebkref_N4.php", "ebkref_num_N4", booking_ref))


QUERIES = {
    "container": get_by_container,
    "bl": get_by_bl,
    "vessel": get_by_vessel,
    "booking": get_by_booking,
}


def to_json(result: dict) -> str:
    """A result as the CLI prints it: indent 2."""
    return json.dumps(result, ensure_ascii=False, indent=2)


# -------------------------------------------------------------------- cli ----


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", choices=tuple(QUERIES), help="what the value identifies")
    parser.add_argument("value", help="container number, B/L, vessel name or SCN, or booking reference")
    args = parser.parse_args(argv)
    print(to_json(QUERIES[args.kind](args.value)))


if __name__ == "__main__":
    main()
