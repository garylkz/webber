---
name: tgv-showtimes
description: Looks up TGV Cinemas Malaysia films, showtimes, seat availability, seat maps and ticket prices. Use when the user asks what is showing at a TGV cinema, when or where a film plays at TGV, how many seats are left, or what a TGV ticket costs. For GSC cinemas use the gsc-showtimes skill instead.
---

# TGV Cinemas

Run the scraper from the webber repo root. It prints JSON on stdout and nothing
else; progress and errors go to stderr.

```sh
lib/.venv/bin/python lib/tgv/tgv_cinema.py <args>
```

## Pick the command

| The user wants | Args |
| --- | --- |
| One cinema's sessions | `<cinema> [YYYY-MM-DD]` |
| Where and when one film plays | `--movie <slug-or-title> [<cinema>] [YYYY-MM-DD]`, optionally `--region <region>` |
| Find a cinema or its id | `--cinemas`, optionally `--region <region>` |
| The regions | `--regions` |
| Which dates can be queried | `--dates` |
| Seat map of one session | `<cinema> <date> --seat-map --time HH:MM` |
| Every cinema in the country | (no cinema): one request per cinema, so prefer a cinema, a film or a region |

- `<cinema>` is the id (`BU0`), a name (`"1 Utama"`) or its keyword. An
  ambiguous name fails and lists the candidates: pick one and rerun.
- The date defaults to today, Malaysia time. Use `YYYY-MM-DD`.
- `--movie` takes the cinema and the date in either order. Without a cinema it
  sweeps every outlet, so add one or a `--region` when the user named a place.

## Narrow first, then add seats or prices

Add `--from HH:MM`, `--until HH:MM` and `--format <codes>` (e.g. `deluxe`,
`imaxlaser`, `indulge`) whenever the user's question allows. Filters run before
seats and prices are fetched. `--seats` costs one request per cinema;
`--prices` costs one request per session per hall area, so an unfiltered day
with `--prices` is slow. Ask for them only when the user needs them.
`--per-person` (with `--prices`) quotes per person, which is what to use when
comparing cinemas.

## Reading the output

- A film is one record with `showtimes`; each showtime has `time` (24-hour),
  `hall`, `experience_name` (the hall format) and, when asked for,
  `seats_left` and `price`.
- `price` is the cheapest ordinary single seat in ringgit. `price_bundle` is a
  multi-seat bundle such as `TWIN`, not the cost of one person. Concessions and
  bank promos are in `ticket_types`; promos that have run out for the day are in
  `sold_out_promos` and are never quoted.
- `seats_left` is remaining seats, not the hall size.
- A show after midnight belongs to the previous day's schedule (the business
  date), so a 00:30 show is listed under the evening before.
- `--dates` includes advance dates with nothing on yet; an empty day there is
  normal.
- `null` means TGV has not published the value (e.g. an unreleased film's
  rating or runtime).

When you answer, list films with their times, and add hall format, seats or
price only when the user asked about them.

## Failures

A bad date, time or cinema exits with a one-line message on stderr: fix the
argument and rerun. A network or API error is reported as such; say so rather
than reporting an empty schedule. A film with no sessions is an error, not an
empty list: try another date or region.

How the scraper works and why is in [SPEC.md](SPEC.md).
