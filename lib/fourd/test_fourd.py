"""Offline tests for fourd_results.py - run against saved copies of the live replies."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import fourd_results as fourd
from fourd_results import (
    Draw,
    day_of,
    normalise_date,
    parse_damacai,
    parse_damacai_link,
    parse_magnum,
    sort_draws,
)

FIXTURES = Path(__file__).parent / "fixtures"
KEYS = {
    "operator", "draw_no", "draw_date", "draw_day", "first", "second",
    "third", "special", "consolation", "official",
}


def body(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8-sig")


# ---------------------------------------------------------------- dates ----


@pytest.mark.parametrize("value", ["2026-10-25", "20261025", "25/10/2026", " 2026-10-25 "])
def test_normalise_date_accepts_every_shape(value):
    assert normalise_date(value) == "2026-10-25"


@pytest.mark.parametrize("value", ["", "2026-13-01", "31/02/2026", "yesterday"])
def test_normalise_date_refuses_nonsense(value):
    with pytest.raises(ValueError):
        normalise_date(value)


def test_day_of():
    assert day_of("2026-10-25") == "SUN"
    assert day_of("2026-10-28") == "WED"


# --------------------------------------------------------------- magnum ----


def test_parse_magnum_past_is_newest_first():
    draws = parse_magnum(body("fourd_magnum_past.json"))
    assert [d.draw_date for d in draws] == ["2026-10-25", "2026-10-24", "2026-10-21"]
    assert all(d.operator == "magnum" for d in draws)


def test_parse_magnum_draw_fields():
    draw = parse_magnum(body("fourd_magnum_past.json"))[0]
    assert draw.draw_no == "440/26"
    assert draw.draw_day == "SUN"
    assert (draw.first, draw.second, draw.third) == ("3719", "0831", "5411")
    assert draw.special[0] == "6681"
    assert len(draw.special) == 10
    assert draw.consolation[0] == "7995"
    assert len(draw.consolation) == 10
    assert draw.official is True


def test_parse_magnum_latest():
    [draw] = parse_magnum(body("fourd_magnum_latest.json"))
    assert (draw.draw_no, draw.draw_date, draw.first) == ("442/26", "2026-10-28", "8243")


def test_parse_magnum_drops_blank_slots():
    raw = json.loads(body("fourd_magnum_latest.json"))
    raw[0]["Special3"] = "----"
    raw[0]["Console10"] = ""
    [draw] = parse_magnum(json.dumps(raw))
    assert len(draw.special) == 9
    assert len(draw.consolation) == 9
    assert "----" not in draw.special


def test_parse_magnum_empty():
    assert parse_magnum("[]") == []


# -------------------------------------------------------------- damacai ----


def test_parse_damacai_link():
    link = parse_damacai_link(body("fourd_damacai_link.json"))
    assert link.startswith("https://prddmcremt1.blob.core.windows.net/drawresult/DrawDate/20261025.json?")


def test_parse_damacai_draw_fields():
    draw = parse_damacai(body("fourd_damacai_20261025.json"))
    assert draw is not None
    assert draw.operator == "damacai"
    assert (draw.draw_no, draw.draw_date, draw.draw_day) == ("6150/26", "2026-10-25", "SUN")
    assert (draw.first, draw.second, draw.third) == ("2963", "8780", "4243")
    assert draw.special[0] == "2664"
    assert draw.consolation[0] == "9681"
    assert len(draw.special) == len(draw.consolation) == 10
    assert draw.official is True


def test_parse_damacai_old_status_letter_is_official():
    draw = parse_damacai(body("fourd_damacai_20050101.json"))
    assert draw is not None
    assert (draw.draw_no, draw.draw_date) == ("2382/05", "2005-01-01")
    assert draw.official is True


def test_parse_damacai_unfinished_draw_is_not_official():
    raw = json.loads(body("fourd_damacai_20261025.json"))
    raw["status"] = "PENDING"
    assert parse_damacai(json.dumps(raw)).official is False


def test_parse_damacai_empty():
    assert parse_damacai("{}") is None


# --------------------------------------------------------------- output ----


def test_to_dict_keys_are_pinned():
    for draw in [*parse_magnum(body("fourd_magnum_past.json")),
                 parse_damacai(body("fourd_damacai_20261025.json"))]:
        assert set(draw.to_dict()) == KEYS


def test_sort_draws_newest_date_then_operator():
    mag = parse_magnum(body("fourd_magnum_past.json"))
    dmc = parse_damacai(body("fourd_damacai_20261025.json"))
    ordered = sort_draws([mag[2], dmc, mag[1], mag[0]])
    assert [(d.draw_date, d.operator) for d in ordered] == [
        ("2026-10-25", "magnum"),
        ("2026-10-25", "damacai"),
        ("2026-10-24", "magnum"),
        ("2026-10-21", "magnum"),
    ]


def test_draws_on_skips_operators_with_no_draw(monkeypatch):
    dmc = parse_damacai(body("fourd_damacai_20261025.json"))
    monkeypatch.setattr(fourd, "magnum_draw", lambda day: None)
    monkeypatch.setattr(fourd, "damacai_draw", lambda day: dmc)
    assert fourd.draws_on("2026-10-25") == [dmc]


def test_recent_uses_magnum_calendar_for_damacai(monkeypatch):
    mag = parse_magnum(body("fourd_magnum_past.json"))
    asked: list[str] = []

    def fake_damacai(day: str) -> Draw | None:
        asked.append(day)
        return None

    monkeypatch.setattr(fourd, "magnum_recent", lambda count: mag[:count])
    monkeypatch.setattr(fourd, "damacai_draw", fake_damacai)
    assert fourd.recent(2, ("damacai",)) == []
    assert asked == ["2026-10-25", "2026-10-24"]


def test_between_asks_damacai_only_for_magnum_dates(monkeypatch):
    mag = parse_magnum(body("fourd_magnum_past.json"))
    dmc = parse_damacai(body("fourd_damacai_20261025.json"))
    asked: list[str] = []

    def fake_damacai(day: str) -> Draw | None:
        asked.append(day)
        return dmc if day == dmc.draw_date else None

    monkeypatch.setattr(fourd, "magnum_between", lambda start, end: mag)
    monkeypatch.setattr(fourd, "damacai_draw", fake_damacai)
    got = fourd.between("2026-10-20", "2026-10-25")
    assert sorted(asked) == ["2026-10-21", "2026-10-24", "2026-10-25"]
    assert [(d.draw_date, d.operator) for d in got] == [
        ("2026-10-25", "magnum"),
        ("2026-10-25", "damacai"),
        ("2026-10-24", "magnum"),
        ("2026-10-21", "magnum"),
    ]


def test_damacai_before_first_draw_makes_no_request(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("should not fetch")

    monkeypatch.setattr(fourd, "fetch_text", boom)
    assert fourd.damacai_draw("2004-12-31") is None


def test_magnum_between_empty_range_makes_no_request(monkeypatch):
    monkeypatch.setattr(fourd, "fetch_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert fourd.magnum_between("2026-10-25", "2026-10-01") == []


def test_fetch_all_covers_magnum_first_draw_to_today(monkeypatch):
    seen = {}
    monkeypatch.setattr(fourd, "today_myt", lambda: "2026-10-29")
    monkeypatch.setattr(fourd, "between", lambda start, end, ops, progress: seen.update(
        start=start, end=end, ops=ops) or [])
    assert fourd.fetch_all() == []
    assert seen == {"start": "1985-04-25", "end": "2026-10-29", "ops": fourd.OPERATORS}


def test_progress_counts_only_damacai_dates(monkeypatch):
    mag = parse_magnum(body("fourd_magnum_past.json"))
    old = parse_magnum(body("fourd_magnum_latest.json"))[0]
    old.draw_date = "2004-12-29"
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(fourd, "damacai_draw", lambda day: None)
    fourd._with_damacai([*mag, old], fourd.OPERATORS, lambda done, total: calls.append((done, total)))
    assert calls == [(1, 3), (2, 3), (3, 3)]


def test_save_json_matches_cli_output(tmp_path):
    draws = [*parse_magnum(body("fourd_magnum_past.json")), parse_damacai(body("fourd_damacai_20261025.json"))]
    path = fourd.save_json(draws, tmp_path / "nested" / "all.json")
    assert path.read_text(encoding="utf-8") == fourd.to_json(draws) + "\n"
    saved = json.loads(path.read_text(encoding="utf-8"))["draws"]
    assert [(d["draw_date"], d["operator"]) for d in saved][:2] == [("2026-10-25", "magnum"), ("2026-10-25", "damacai")]


# ----------------------------------------------------------------- live ----


@pytest.mark.live
def test_live_latest_has_both_operators():
    draws = fourd.latest()
    assert {d.operator for d in draws} == {"magnum", "damacai"}
    assert all(len(d.first) == 4 for d in draws)


@pytest.mark.live
def test_live_known_draw():
    draws = fourd.draws_on("2026-10-25")
    assert [(d.operator, d.first) for d in draws] == [("magnum", "3719"), ("damacai", "2963")]


@pytest.mark.live
def test_live_non_draw_day_is_empty():
    assert fourd.draws_on("2026-10-26") == []
