---
id: hacker-news
type: module-design
title: Hacker News agent tools
parent: webber-architecture
status: draft
---

## Responsibility

Lets an agent show the owner what is on Hacker News and what people say about
a story. Two scripts, used in that order:

- [hacker_news_feed.py](hacker_news_feed.py) lists the stories on one front page.
- [hacker_news_comments.py](hacker_news_comments.py) fetches one story's comment thread, for an
  agent to summarize.

Nothing imports either one.

## hacker_news_feed.py

Reads the front page HTML and prints the first 10 stories of the requested page
(`--page N`, default 1). It is a feed lister and follows that contract in
`webber-architecture`.

```json
{"page": 1, "count": 10, "stories": [
  {"title": "...", "url": "...", "comments_url": "..."}]}
```

`comments_url` is the story's `item?id=` page on news.ycombinator.com, which is
what `hacker_news_comments.py` takes. The cache copy is `/tmp/hn-stories.json`.

## hacker_news_comments.py

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

It started as a POSIX shell copy of the owner's global `hn-comments.sh`, using
`curl` and `jq`, and was ported to Python with `requests`. The port printed the
same JSON as the shell script on every live thread it was checked against.

### Testing

`test_hacker_news.py` runs the id parser, the HTML cleaner, the comment and
depth limits and the moved-thread check, over Hacker News item 1 in
`fixtures/` and small made-up trees. It never touches the network.
