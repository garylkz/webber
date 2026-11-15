# webber

A personal toolkit of small scrapers. Each one reads a public site the way an
anonymous browser would and prints JSON, for the command line, for other code,
and for AI agents.

| Folder | Tool | What it reads |
| --- | --- | --- |
| `lib/gsc/` | `gsc_scraper.py` | GSC Cinemas: films, showtimes, seats, prices |
| `lib/tgv/` | `tgv_scraper.py` | TGV Cinemas: the same, through the same interface |
| `lib/fourd/` | `fourd_scraper.py` | Magnum 4D and Da Ma Cai results |
| `lib/northport/` | `northport_scraper.py` | Northport container, B/L, vessel and booking enquiries |
| `lib/meetup/` | `meetup_scraper.py` | Meetup events near a city or across a country |
| `lib/goodread/` | `goodread_scraper.py` | A Goodreads user's shelf |
| `lib/youtube/` | `youtube_rss.py` | A YouTube channel's RSS feed URL |
| `lib/hackernews/` | `hackernews_feed.py`, `hackernews_comments.sh` | Hacker News front page and comment threads |
| `lib/rps/` | `rps_feed.py`, `rps_article.py` | Rock Paper Shotgun feed and articles |
| `lib/airquality/` | `airquality_report.py` | Air quality of a place, with the official Malaysian reading |
| `lib/googlemaps/` | `googlemaps_link.py` | A short Google Maps link expanded to a full place link |
| `lib/mcp/` | `mcp_server.py` | Not a scraper: serves all of the above as MCP tools |

## Setup

```bash
python3 -m venv lib/.venv
lib/.venv/bin/pip install requests "mcp==2.3.0"
```

`requests` is the scrapers' only Python dependency; `mcp` is for the MCP server alone. `hackernews_comments.sh` and `rps_article.py`
also need `curl`, and `hackernews_comments.sh` needs `jq`.

## Run

From the repo root, for example:

```bash
lib/.venv/bin/python lib/gsc/gsc_scraper.py --movie digger
lib/.venv/bin/python lib/youtube/youtube_rss.py "https://youtube.com/@handle"
```

Every tool prints JSON on stdout; `--help` lists its options.

## MCP

`lib/mcp/mcp_server.py` serves the tools to MCP clients over stdio, built on the
official `mcp` SDK. Point a client at the venv's Python:

```json
{"mcpServers": {"webber": {"command": "/path/to/webber/lib/.venv/bin/python",
                           "args": ["/path/to/webber/lib/mcp/mcp_server.py"]}}}
```

Add `"--tools", "cinema,fourd"` to the args to serve only some groups. For a
client that connects instead of starting the server, run it with `--http` and
point the client at `http://127.0.0.1:8766/mcp` (local only, no auth). The tool
list is in [`lib/mcp/SPEC.md`](lib/mcp/SPEC.md).

## Tests

```bash
cd lib
.venv/bin/python -m pytest          # offline, against saved responses
.venv/bin/python -m pytest -m live  # also hits the real sites
```

## Docs

- [`lib/goal-and-requirements.md`](lib/goal-and-requirements.md): what webber is for.
- [`lib/architecture.md`](lib/architecture.md): how the tools are built and the
  rules they share.
- `lib/<tool>/SPEC.md`: each tool's design.
- `lib/<tool>/SKILL.md` (gsc, tgv, youtube): how an agent runs the tool.
- [`lib/mcp/SPEC.md`](lib/mcp/SPEC.md): the MCP server.
