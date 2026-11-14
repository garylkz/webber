---
id: rps
type: module-design
title: Rock Paper Shotgun agent tools
parent: webber-architecture
status: draft
---

## Responsibility

Lets an agent show the owner the latest Rock Paper Shotgun articles and read
one in full. Two scripts, used in that order:

- [rps_feed.py](rps_feed.py) lists articles from the RSS feed.
- [rps_article.py](rps_article.py) fetches one article as Markdown.

Nothing imports either one.

## rps_feed.py

The feed is sorted newest first and printed as a window: `--start N` is the
position to begin at (1 is the newest) and `--count M` how many to print;
without `--count` it prints every article from `--start` on. It is a feed
lister and follows that contract in `webber-architecture`.

```json
{"order": "newest first", "start": 1, "next_start": 11, "total": 20, "count": 10,
 "articles": [{"title": "...", "url": "...", "comments_url": "..."}]}
```

`total` is the number of articles in the feed and `count` the number printed.
`next_start` is the `--start` for the next batch; when it is past `total`, there
are no more articles. `comments_url` is the article URL with `?view=comments`.
The cache copy is `/tmp/rps-articles.json`.

## rps_article.py

Takes one article URL from the feed and prints it as one JSON object:

```json
{"title": "...", "author": "...", "published": "2026-11-08T19:00", "url": "...",
 "tags": ["pc"], "body": "...", "body_file": "/tmp/rps-article-body.md",
 "save_command": "python3 .agents/scripts/vault-create.py ..."}
```

- **Only rockpapershotgun.com article URLs are accepted.** The query string
  (`?view=comments`, tracking) is dropped, since the article is the path.
- `body` is the article text as Markdown, with ads, polls, embeds and related
  links left out. It is also written to `/tmp/rps-article-body.md`.
- `published` is in GMT+8. `author` and `published` are `null` when the page
  has none. At most 3 of the site's tags are kept.
- **`save_command` saves the article as a note in the owner's vault, and the
  script never runs it.** The agent runs it only after the owner says yes, from
  the vault root, because the command's path to `vault-create.py` is relative
  to the vault, not to webber.

Errors go to stderr with a non-zero exit, unlike the feed listers.

### Departures from the shared conventions

- **It fetches with `curl` through `subprocess`, not with `requests`,** and uses
  only the standard library otherwise. It started as a copy of
  `~/.agents/scripts/rps-article.py`, which the global `rps-article` agent skill
  runs.
- **The two copies are independent and already differ:** this one prints JSON
  only (the global one also has a `--format text` mode). A fix made to one does
  not reach the other.
