---
id: hackernews
type: module-design
title: Hacker News agent tools
parent: webber-architecture
status: draft
---

## Responsibility

Lets an agent show the owner what is on Hacker News and what people say about
a story. Two scripts, used in that order:

- [hackernews_feed.py](hackernews_feed.py) lists the stories on one front page.
- [hackernews_comments.sh](hackernews_comments.sh) fetches one story's comment thread, for an
  agent to summarize.

Nothing imports either one.

## hackernews_feed.py

Reads the front page HTML and prints the first 10 stories of the requested page
(`--page N`, default 1). It is a feed lister and follows that contract in
`webber-architecture`.

```json
{"page": 1, "count": 10, "stories": [
  {"title": "...", "url": "...", "comments_url": "..."}]}
```

`comments_url` is the story's `item?id=` page on news.ycombinator.com, which is
what `hackernews_comments.sh` takes. The cache copy is `/tmp/hn-stories.json`.

## hackernews_comments.sh

Takes a `comments_url` or a bare item id and prints the thread as one JSON
object: the story's id, title, points and URLs, and a tree of comments, each
with its text and its replies. It reads the Algolia HN API
(`hn.algolia.com/api/v1/items/<id>`), which returns the whole tree in one call.

- **The thread is bounded for an agent's context:** at most 80 comments, 4
  levels deep, each cut to 700 characters (ending in ` [...]`). `shown` and
  `max_depth` in the output say how much was kept.
- **A moved thread is followed.** When the only top-level comment says
  "Comments moved to item?id=X", it fetches X instead and sets `moved_from` to
  the original id.
- **No comments is not an error:** `comments` is empty and `note` is
  `No comments yet.`

Errors go to stderr with a non-zero exit, unlike the feed listers.

### Departures from the shared conventions

- **It is a POSIX shell script that calls `curl` and `jq`, not Python with
  `requests`.** It started as a copy of `~/.agents/scripts/hn-comments.sh`,
  which the global `hn-comments` agent skill runs.
- **The two copies are independent and already differ:** this one prints JSON
  only (the global one also has a `--format text` mode). A fix made to one does
  not reach the other.
