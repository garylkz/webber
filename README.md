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

## Setup

```bash
python3 -m venv lib/.venv
lib/.venv/bin/pip install requests
```

`requests` is the only Python dependency. `hackernews_comments.sh` and `rps_article.py`
also need `curl`, and `hackernews_comments.sh` needs `jq`.

## Run

From the repo root, for example:

```bash
lib/.venv/bin/python lib/gsc/gsc_scraper.py --movie digger
lib/.venv/bin/python lib/youtube/youtube_rss.py "https://youtube.com/@handle"
```

Every tool prints JSON on stdout; `--help` lists its options.

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
