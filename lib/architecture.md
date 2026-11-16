---
id: webber-architecture
type: architecture-design
status: draft
title: webber architecture
parent: webber-goal
---

## Topology

Everything lives under `lib/`, one folder per tool (`lib/<tool>/`; the tools
are this spec's children in the graph). A folder holds the tool's code, its `SPEC.md`, its tests and
its `fixtures/`. A tool an agent runs directly may also carry a `SKILL.md`:
the commands and how to read the output, with the design left to `SPEC.md`
(today `gsc`, `tgv`, `youtube`, `fenrir-realm`, `sakuraze` and `novelight`).
One folder is not a scraper: `lib/mcp/` serves the scripts as MCP tools by
running them as subprocesses, so it imports none of them (see `mcp`). A folder holds one or more scripts, and each
script is a single file that is both an importable module and a CLI
(`python lib/<tool>/<script>.py ...`). The folders are not Python packages:
there is no `__init__.py`, no build step and no shared library module.

**No module imports another.** A helper that two scrapers both need, such as
GSC's and TGV's throttle and date and clock parsing, is written into each file
on purpose, so any one file can be copied out and run on its own. That
duplication is accepted. Keeping the shared names matching is the cinema
contract's job (below), not a shared module's.

## Naming

One name per tool, used everywhere, so a tool's folder, spec, scripts, tests
and skill can be found from any one of them.

- **Folder:** the source's name as people say it, lowercase (`gsc`, `fourd`,
  `goodreads`, `rps`). A name of two words takes a hyphen (`hacker-news`,
  `air-quality`, `google-maps`). A common short form is fine (`rps`); an
  invented one is not.
- **Spec id:** the folder name. The two root specs keep `webber-goal` and
  `webber-architecture`.
- **Script:** `<source>_<output>`: the folder name with its hyphen written as
  an underscore, since a Python module name cannot hold a hyphen, then a short
  noun for what the script returns (`gsc_cinema.py`, `fourd_results.py`,
  `goodreads_shelf.py`, `hacker_news_feed.py`, `air_quality_report.py`). The
  prefix stays even inside its folder: every folder is on the import path, so
  two `feed.py` files would collide, and a copied file still says which source
  it reads. Every script is Python.
- **MCP tool:** the script's name without `.py`, so a tool and its script
  match (`fourd_results`, `rps_feed`). The `cinema_*` tools are the exception:
  each serves both `gsc` and `tgv` through its `chain` argument.
- **Tests:** `test_<folder>.py`, or `test_<folder>_<topic>.py` when a tool has
  several (`test_gsc_api.py`, `test_gsc_parsers.py`). Tests that hit the real
  site are `test_<folder>_live.py`.
- **Saved responses:** `fixtures/` inside the tool's folder.
- **Skill name:** `<folder>-<job>` (`gsc-showtimes`, `youtube-rss`).

## Shared conventions

These hold for every tool and carry the detail behind the goal's principles
(`webber-goal`: JSON on stdout, anonymous access only, one file per tool).
The module specs describe only where a tool departs from them.

- **Output.** Every script, Python or shell, prints its result as one JSON
  value on stdout and has no text output mode. A run that writes its result to a
  file prints a JSON summary instead (`{"draws": N, "file": ...}`). Progress,
  warnings and errors go to stderr, so stdout holds only JSON; the feed listers
  are the exception (below). Records are dataclasses or dicts with snake_case
  keys.
- **Files.** A tool that saves a bulk result writes it under `out/` at the repo
  root, which is git-ignored. The feed listers are the exception (below).
- **Politeness.** Tools that fan out (the cinema sweeps, the 4D history, the Meetup
  country sweep) throttle themselves to a global rate, so parallel workers don't
  add up to a burst.
- **HTTP.** Every tool makes its calls through `requests`, which is the only
  third-party dependency. Nothing uses `urllib.request` or shells out to `curl`.
  One client means one way to handle errors and timeouts, and `requests` brings
  its own CA bundle, so HTTPS works on an install that has no system
  certificate store.
- **Errors.** Bad arguments are rejected before any request is made. A source
  that can't be reached is reported as such, never shown as an empty result.

## The cinema contract

`gsc` and `tgv` are interchangeable on purpose. They expose
the same function names for the same jobs and emit the same JSON key sets for
the six shared records (`Movie`, `Cinema`, `Showtime`, `TicketType`, `Seat`,
`SeatPlan`), so one consumer reads either chain's output. Where a name exists on
only one side, the reason is recorded. `tgv`'s "The shared contract"
section owns the full list of names and deliberate asymmetries.
`lib/gsc/test_gsc_api.py` enforces the contract by pinning the key sets
and the one-sided names. A field added to one scraper and not the other fails
the suite.

No other tool takes part in this contract. Each one has its own record shape.

## Agent news tools

`hacker-news` and `rps` let an agent show the owner the latest items from a news
source and then go deeper into one (a Hacker News comment thread, a full Rock
Paper Shotgun article). They are agent tools, not library scrapers: their
output is for a person, through the agent, and nothing imports them.

The two **feed listers**, `hacker_news_feed.py` and `rps_feed.py`, share one contract:

- **Every item carries `title`, `url` and `comments_url`, and both URLs are
  always present.** The agent's reply shows each item with both links.
- **Each writes its exact JSON to a fixed file under `/tmp`**, overwritten on
  every run, not `out/`: it is a scratch copy the agent re-reads if it garbles
  a URL, instead of fetching the site again. A failed write is ignored.
- **Errors go to stdout as JSON, `{"error": "..."}`, not to stderr**, with a
  non-zero exit, so an agent reading stdout sees why there is no result, in the
  one place it reads.
- **No tests.** Both feed parsers are small enough that a broken page shows up
  straight away in the agent's reply. *(Inferred, unconfirmed.)*

## Testing

- Each tool's offline tests sit in its folder and run its parsers, as pure
  functions, over saved real responses in that folder's `fixtures/`. They never
  touch the network and are the default `pytest` run from `lib/`, where
  `conftest.py` puts every tool folder on the import path.
- Tests marked `live` hit the real sites (`pytest -m live`). They pick dates from
  the site's own list of trading days, because a cinema's national run rolls over
  around midnight Malaysia time.
- `goodreads`, `meetup` and `rps` have no offline tests, and in `hacker-news`
  only `hacker_news_comments.py` has them.

## Known limitations

- **The sources are undocumented and can change without notice.** GSC's booking
  service, TGV's API, Magnum and Da Ma Cai's endpoints, Northport's enquiry
  pages and Meetup's gql2 endpoint all belong to someone else, and none of them
  is a published API. A change of shape shows up in the `live` tests. The
  expected fix is to re-capture the fixtures and update the parser, so the
  offline suite then pins the new shape.
