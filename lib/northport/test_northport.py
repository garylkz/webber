"""Offline tests for northport_scraper.py - run against saved enquiry fragments."""

from __future__ import annotations

from pathlib import Path

import pytest

import northport_scraper as northport
from northport_scraper import parse_booking, parse_rows

FIXTURES = Path(__file__).parent / "fixtures"


def body(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_rows_maps_labels_to_values():
    rows = parse_rows(body("northport_container.html"))
    assert rows["Container Number"] == "ABCD1234567"
    assert rows["Size & Type"] == "40 HC"


def test_parse_rows_handles_line_break_and_tagged_label():
    rows = parse_rows(body("northport_container.html"))
    assert rows["Status"] == "IN YARD"
    assert rows["Vessel"] == "MV EXAMPLE"


def test_parse_rows_first_repeated_label_wins():
    assert parse_rows(body("northport_container.html"))["Status"] == "IN YARD"


def test_parse_rows_empty():
    assert parse_rows("") == {}
    assert parse_rows("<p>No record found</p>") == {}


def test_parse_booking_header_and_containers():
    booking = parse_booking(body("northport_booking.html"))
    assert booking["Booking Ref"] == "BK0001"
    assert booking["Line"] == "EXAMPLE LINE"
    assert booking["Containers"] == [
        {"Container Number": "WXYZ7654321", "Size": "20 GP", "Status": "GATED IN"},
        {"Container Number": "WXYZ7654322", "Size": "20 GP", "Status": "LOADED"},
    ]


def test_parse_booking_without_container_list():
    assert parse_booking("<p>Booking Ref : <span>BK1</span></p>") == {
        "Booking Ref": "BK1",
        "Containers": [],
    }


def test_post_form_uppercases_and_strips(monkeypatch):
    seen = {}

    class Resp:
        ok = True
        content = b"<p>A : <span>b</span></p>"

    def fake_post(url, data, headers, timeout):
        seen.update(url=url, data=data)
        return Resp()

    monkeypatch.setattr(northport._session, "post", fake_post)
    assert northport.get_by_container(" abcd1234567 ") == {"A": "b"}
    assert seen["url"] == northport.BASE_URL + "query_contN4.php"
    assert seen["data"] == {"cont_num_N4": "ABCD1234567"}


def test_post_form_http_error_exits(monkeypatch):
    class Resp:
        ok = False
        status_code = 503

    monkeypatch.setattr(northport._session, "post", lambda *a, **k: Resp())
    with pytest.raises(SystemExit):
        northport.get_by_bl("X")
