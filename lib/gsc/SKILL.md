---
name: gsc-showtimes
description: Looks up GSC Cinemas Malaysia films, showtimes, seat availability, seat maps and ticket prices. Use when the user asks what is showing at a GSC cinema, when or where a film plays at GSC, how many seats are left, or what a GSC ticket costs. For TGV cinemas use the tgv-showtimes skill instead.
---

# GSC Cinemas

Run the scraper from the webber repo root. It prints JSON on stdout and nothing
else; progress and errors go to stderr.

```sh
lib/.venv/bin/python lib/gsc/gsc_cinema.py <args>
```

## Pick the command

| The user wants | Args |
| --- | --- |
| What is showing now / coming soon | (none), or `--listing coming-soon`, `--listing advance-sales`, `--all` |
| One cinema's sessions | `<cinema> [YYYY-MM-DD]` |
| Where and when one film plays | `--movie <slug-or-title> [<cinema>] [YYYY-MM-DD]` |
| A film's synopsis, director, cast | `--detail <slug>` |
| Find a cinema or its id | `--cinemas`, optionally `--city <town>` or `--region <region>` |
| Which dates can be queried | `--dates` |
| Seat map of one session | `<cinema> [date] --seat-map --time <HH:MM or 10:50AM>` |

- `<cinema>` is the numeric id (`235`), the slug, or a name (`"1 Utama"`). An
  ambiguous name fails and lists the candidates: pick one and rerun.
- The date defaults to today, Malaysia time. Use `YYYY-MM-DD`.
- `--movie` takes the cinema and the date in either order, and `--region` or
  `--city` narrows it to part of the country.

## Narrow first, then add seats or prices

Add `--from HH:MM`, `--until HH:MM` and `--format <codes>` (e.g. `imax,4dx`)
whenever the user's question allows. Filters run before seats and prices are
fetched, and `--seats` and `--prices` each cost one extra request per session,
so an unfiltered day with `--prices` is slow. Ask for them only when the user
needs them. `--per-person` (with `--prices`) quotes per person, which is what to
use when comparing cinemas.

## Reading the output

- A film is one record with `showtimes`; each showtime has `time` (24-hour),
  `hall`, `experience_name` (the hall format) and, when asked for,
  `seats_left` and `price`.
- `price` is the cheapest ordinary single seat in ringgit. `price_bundle` is a
  pair seat (`Twin`), not the cost of one person. Concessions are in
  `ticket_types`.
- `seats_left` is what GSC will still sell. Ignore `reported_total`; it is not
  the hall size.
- A show after midnight belongs to the previous day's schedule (the business
  date), so a 00:15 show is listed under the evening before.
- `null` means GSC has not published the value (placeholders such as `N/A` and
  `TBA` become `null`).

When you answer, list films with their times, and add hall format, seats or
price only when the user asked about them.

## Failures

A bad date, time or cinema exits with a one-line message on stderr: fix the
argument and rerun. A network or site error is reported as such; say so rather
than reporting an empty schedule. A film with no sessions on that date is an
error, not an empty list: try another date from `--dates`.

How the scraper works and why is in [SPEC.md](SPEC.md).
