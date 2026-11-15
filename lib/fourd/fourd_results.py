#!/usr/bin/env python3
"""Malaysian 4D draw results from the operators' own sites.

Usage:
    python fourd_results.py                     # latest draw, every operator
    python fourd_results.py 2026-10-25          # one draw date
    python fourd_results.py --recent 10         # the last 10 draw dates
    python fourd_results.py --from 2024-01-01 --to 2024-12-31   # a date range
    python fourd_results.py --operator magnum   # one operator only
    python fourd_results.py --all --out all.json   # every draw ever, saved to a file

Prints a JSON object on stdout as its only output:
    {"draws": [{"operator", "draw_no", "draw_date", "draw_day", "first",
                "second", "third", "special", "consolation", "official"}]}

Covers Magnum 4D (magnum4d.my) and Da Ma Cai 1+3D (damacai.com.my). Sports
Toto is not covered: sportstoto.com.my refuses non-browser clients. Needs
`requests`. Errors go to stderr.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests

MAGNUM_BASE = "https://www.magnum4d.my"
DAMACAI_BASE = "https://www.damacai.com.my"
OPERATORS = ("magnum", "damacai")
USER_AGENT = "Mozilla/5.0"
# Da Ma Cai's result endpoint answers "Page Not Found" without this header,
# which its own page sends on every call.
DAMACAI_HEADERS = {"cookiesession": "421"}
MYT = timezone(timedelta(hours=8))
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
# How far back a Da Ma Cai "latest" walks before giving up. Draws are at most
# four days apart (Sun to Wed), so a week always finds one.
DAMACAI_LOOKBACK = 7
FETCH_ATTEMPTS = 3
# The oldest draw each operator still publishes.
MAGNUM_FIRST = "1985-04-25"
DAMACAI_FIRST = "2005-01-01"
# Da Ma Cai costs two requests per date, so a long range fetches a few dates
# at once rather than one after another.
DAMACAI_WORKERS = 4
# How often a long run reports how many Da Ma Cai dates it has done.
PROGRESS_EVERY = 50

Progress = Callable[[int, int], None]


@dataclass
class Draw:
    """One operator's result for one draw."""

    operator: str
    draw_no: str
    draw_date: str  # YYYY-MM-DD
    draw_day: str  # MON..SUN
    first: str
    second: str
    third: str
    special: list[str] = field(default_factory=list)
    consolation: list[str] = field(default_factory=list)
    official: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------- dates ----


def normalise_date(value: str) -> str:
    """YYYY-MM-DD, YYYYMMDD or DD/MM/YYYY -> YYYY-MM-DD. Raises ValueError."""
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"not a date: {value!r}")


def day_of(iso: str) -> str:
    return DAYS[date.fromisoformat(iso).weekday()]


def today_myt() -> str:
    return datetime.now(MYT).date().isoformat()


# --------------------------------------------------------------- parsers ----


def _number(value: object) -> str:
    """A 4D number as the operators print it, or "" for a blank slot."""
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{4}", text) else ""


def _numbers(values: list) -> list[str]:
    return [n for n in (_number(v) for v in values) if n]


def parse_magnum_draw(raw: dict) -> Draw:
    """One record off magnum4d.my's results JSON."""
    iso = normalise_date(raw.get("DrawDate", ""))
    return Draw(
        operator="magnum",
        draw_no=str(raw.get("DrawID", "")).strip(),
        draw_date=iso,
        draw_day=day_of(iso),
        first=_number(raw.get("FirstPrize")),
        second=_number(raw.get("SecondPrize")),
        third=_number(raw.get("ThirdPrize")),
        special=_numbers([raw.get(f"Special{i}") for i in range(1, 14)]),
        consolation=_numbers([raw.get(f"Console{i}") for i in range(1, 11)]),
        official=bool(raw.get("Official", True)),
    )


def parse_magnum(body: str) -> list[Draw]:
    """A /results/latest or /results/past/... body -> draws, newest first."""
    records = json.loads(body)
    draws = [parse_magnum_draw(r) for r in records if r.get("DrawDate")]
    return sorted(draws, key=lambda d: d.draw_date, reverse=True)


def parse_damacai_link(body: str) -> str:
    """The /callpassresult reply -> the signed URL that holds the draw."""
    return json.loads(body)["link"]


def parse_damacai(body: str) -> Draw | None:
    """A Da Ma Cai draw blob -> its 1+3D draw, or None if it holds none."""
    raw = json.loads(body)
    if not raw or not raw.get("drawDate"):
        return None
    iso = normalise_date(raw["drawDate"])
    return Draw(
        operator="damacai",
        draw_no=str(raw.get("drawNo", "")).strip(),
        draw_date=iso,
        draw_day=day_of(iso),
        first=_number(raw.get("p1")),
        second=_number(raw.get("p2")),
        third=_number(raw.get("p3")),
        special=_numbers(raw.get("starterList") or []),
        consolation=_numbers(raw.get("consolidateList") or []),
        # Older draws carry "C" where newer ones spell it out.
        official=str(raw.get("status", "")).upper() in ("C", "COMPLETED"),
    )


# --------------------------------------------------------------- network ----


_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT


def fetch_text(url: str, headers: dict | None = None, timeout: int = 15) -> str | None:
    """GET one URL as text. A 404 is None; any other failure exits.

    Da Ma Cai's blob host now and then stalls on a request it answers at once
    when asked again, so a timeout is retried before giving up.
    """
    where = url.split("?")[0]
    for attempt in range(FETCH_ATTEMPTS):
        try:
            resp = _session.get(url, headers=headers, timeout=timeout)
        except requests.Timeout as exc:
            if attempt == FETCH_ATTEMPTS - 1:
                raise SystemExit(f"timed out fetching {where}") from exc
            continue
        except requests.RequestException as exc:
            raise SystemExit(f"network error fetching {where}: {exc}") from exc
        if resp.status_code == 404:
            return None
        if not resp.ok:
            raise SystemExit(f"HTTP {resp.status_code} from {where}")
        return resp.content.decode("utf-8-sig", "replace")
    return None


def magnum_latest() -> Draw | None:
    body = fetch_text(f"{MAGNUM_BASE}/results/latest")
    draws = parse_magnum(body) if body else []
    return draws[0] if draws else None


def magnum_recent(count: int, on_or_before: str | None = None) -> list[Draw]:
    """The last `count` Magnum draws on or before a date (default: today)."""
    day = on_or_before or today_myt()
    body = fetch_text(f"{MAGNUM_BASE}/results/past/between-dates/null/{day}/{count}")
    return parse_magnum(body) if body else []


def magnum_draw(day: str) -> Draw | None:
    """The Magnum draw held on exactly this date, if there was one."""
    return next((d for d in magnum_recent(1, day) if d.draw_date == day), None)


def magnum_between(start: str, end: str) -> list[Draw]:
    """Every Magnum draw from `start` to `end`, both inclusive, newest first."""
    # At most one draw a day, so the day count is a safe upper bound.
    limit = date.fromisoformat(end).toordinal() - date.fromisoformat(start).toordinal() + 1
    if limit < 1:
        return []
    body = fetch_text(f"{MAGNUM_BASE}/results/past/between-dates/{start}/{end}/{limit}")
    return parse_magnum(body) if body else []


def damacai_draw(day: str) -> Draw | None:
    """The Da Ma Cai draw held on exactly this date, if there was one."""
    if day < DAMACAI_FIRST:
        return None
    link = fetch_text(
        f"{DAMACAI_BASE}/callpassresult?pastdate={day.replace('-', '')}",
        headers=DAMACAI_HEADERS,
    )
    if not link or not link.lstrip().startswith("{"):
        raise SystemExit("Da Ma Cai did not return a result link")
    body = fetch_text(parse_damacai_link(link))
    return parse_damacai(body) if body else None


def damacai_latest(today: str | None = None) -> Draw | None:
    start = date.fromisoformat(today or today_myt())
    for back in range(DAMACAI_LOOKBACK + 1):
        found = damacai_draw((start - timedelta(days=back)).isoformat())
        if found:
            return found
    return None


# ---------------------------------------------------------------- queries ----


def draws_on(day: str, operators: tuple[str, ...] = OPERATORS) -> list[Draw]:
    """Every chosen operator's draw on one date. Empty on a non-draw day."""
    fetchers = {"magnum": magnum_draw, "damacai": damacai_draw}
    return [d for d in (fetchers[op](day) for op in operators) if d]


def latest(operators: tuple[str, ...] = OPERATORS) -> list[Draw]:
    """Each chosen operator's most recent published draw."""
    out = []
    if "magnum" in operators:
        found = magnum_latest()
        if found:
            out.append(found)
    if "damacai" in operators:
        found = damacai_latest()
        if found:
            out.append(found)
    return out


def recent(
    count: int, operators: tuple[str, ...] = OPERATORS, progress: Progress | None = None
) -> list[Draw]:
    """The last `count` draw dates, for every chosen operator.

    The draw calendar comes from Magnum, which lists past draws by count. The
    operators draw on the same Wed, Sat and Sun, so Da Ma Cai is then asked for
    each of those dates. Some special draws are Magnum's alone; Da Ma Cai simply
    has nothing on those dates.
    """
    return _with_damacai(magnum_recent(count), operators, progress)


def between(
    start: str, end: str, operators: tuple[str, ...] = OPERATORS, progress: Progress | None = None
) -> list[Draw]:
    """Every draw from `start` to `end`, both inclusive, for every chosen operator.

    The calendar comes from Magnum, as in `recent`.
    """
    return _with_damacai(magnum_between(start, end), operators, progress)


def fetch_all(operators: tuple[str, ...] = OPERATORS, progress: Progress | None = None) -> list[Draw]:
    """Every draw each chosen operator still publishes, up to today.

    Magnum's whole history is one request. Da Ma Cai's is two requests per
    date since 2005, a few thousand in all, so this takes a while; pass
    `progress(done, total)` to follow it.
    """
    return between(MAGNUM_FIRST, today_myt(), operators, progress)


def _with_damacai(
    calendar: list[Draw], operators: tuple[str, ...], progress: Progress | None = None
) -> list[Draw]:
    """Magnum's draws (if chosen) plus Da Ma Cai's on the same dates (if chosen)."""
    out = [d for d in calendar if "magnum" in operators]
    if "damacai" in operators:
        days = [m.draw_date for m in calendar if m.draw_date >= DAMACAI_FIRST]
        with ThreadPoolExecutor(DAMACAI_WORKERS) as pool:
            for done, found in enumerate(pool.map(damacai_draw, days), 1):
                if found:
                    out.append(found)
                if progress:
                    progress(done, len(days))
    return sort_draws(out)


def to_json(draws: list[Draw]) -> str:
    """Draws as the CLI prints them: {"draws": [...]}, newest first, indent 2."""
    return json.dumps({"draws": [d.to_dict() for d in sort_draws(draws)]}, ensure_ascii=False, indent=2)


def save_json(draws: list[Draw], path: str | Path) -> Path:
    """Write draws to a JSON file, creating its folder if needed. Returns the path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(to_json(draws) + "\n", encoding="utf-8")
    return target


def sort_draws(draws: list[Draw]) -> list[Draw]:
    """Newest date first, then in OPERATORS order."""
    return sorted(draws, key=lambda d: (_neg_date(d.draw_date), OPERATORS.index(d.operator)))


def _neg_date(iso: str) -> int:
    return -date.fromisoformat(iso).toordinal()


# -------------------------------------------------------------------- cli ----


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("date", nargs="?", help="draw date as YYYY-MM-DD (default: latest draw)")
    parser.add_argument("--recent", type=int, metavar="N", help="the last N draw dates instead")
    parser.add_argument("--from", dest="start", metavar="DATE", help="every draw from this date on")
    parser.add_argument("--to", dest="end", metavar="DATE", help="with --from, the last date (default: today)")
    parser.add_argument("--all", action="store_true", help="every draw ever published (slow for Da Ma Cai)")
    parser.add_argument("--operator", choices=(*OPERATORS, "all"), default="all", help="one operator only")
    parser.add_argument("--out", metavar="FILE", help="write the JSON to this file; stdout gets a JSON summary")
    args = parser.parse_args(argv)

    operators = OPERATORS if args.operator == "all" else (args.operator,)
    if sum(x is not None for x in (args.date, args.recent, args.start, args.all or None)) > 1:
        parser.error("give one of: a date, --recent, --from, --all")
    if args.end and not args.start:
        parser.error("--to needs --from")
    if args.recent is not None and args.recent < 1:
        parser.error("--recent must be at least 1")

    def parse_day(value: str) -> str:
        try:
            return normalise_date(value)
        except ValueError as exc:
            parser.error(str(exc))

    def report(done: int, total: int) -> None:
        if done % PROGRESS_EVERY == 0 or done == total:
            print(f"damacai: {done}/{total} dates", file=sys.stderr)

    if args.all:
        draws = fetch_all(operators, report)
    elif args.start:
        start, end = parse_day(args.start), parse_day(args.end) if args.end else today_myt()
        if start > end:
            parser.error("--from is after --to")
        draws = between(start, end, operators, report)
    elif args.recent:
        draws = recent(args.recent, operators, report)
    elif args.date:
        day = parse_day(args.date)
        draws = draws_on(day, operators)
        if not draws:
            print(f"no draw on {day}", file=sys.stderr)
    else:
        draws = latest(operators)

    if args.out:
        path = save_json(draws, args.out)
        print(json.dumps({"draws": len(draws), "file": str(path)}))
    else:
        print(to_json(draws))


if __name__ == "__main__":
    main()
