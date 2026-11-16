---
id: youtube
type: module-design
status: draft
title: YouTube Tools
parent: webber-architecture
---

# YouTube Tools: Specification

Two scripts about YouTube channels. This document describes what each function
does.

- [youtube_rss.py](youtube_rss.py) turns a channel URL into the channel's
  official RSS feed URL.
- [youtube_channel.py](youtube_channel.py) finds a channel by a creator's
  name. Its section is at the end of this document.

---

## Usage

The CLI:

```bash
cd lib/youtube
python youtube_rss.py "https://youtube.com/@handle"
python youtube_rss.py "https://youtube.com/@handle" --fetch
```

A `/channel/UC...` URL carries its own id and needs no request. A handle does not,
so that is the one case where a page is fetched, and the id is read out of it.

Imported:

```python
import youtube_rss as yt

feed = yt.rss_for_channel("UC...")
id_ = yt.channel_id_from_url("https://youtube.com/@handle")
```

---

## Concepts

| Term | Meaning |
|---|---|
| **Channel id** | YouTube's stable identifier for a channel, always beginning `UC` and 24 characters long. It never changes, where a handle or a vanity name can be changed by its owner. |
| **Handle** | The `@name` form a channel can set, e.g. `@veritasium`. A display name, not an id, and it can be changed or given up. |
| **Feed URL** | `https://www.youtube.com/feeds/videos.xml?channel_id={id}`, which returns an Atom feed of the channel's most recent uploads, newest first. |

---

## Functions

| Function | Returns | Description |
|---|---|---|
| `rss_for_channel(channel_id)` | `str` | The feed URL for a channel id. No network. |
| `channel_id_from_url(url)` | `str` or `None` | The channel id for any YouTube channel URL, fetching the page when the URL does not carry one. |
| `normalize_url(url)` | `str` | Forces https and refuses anything that is not a YouTube URL. No network. |
| `fetch_page(url, timeout=30)` | `str` | Fetches one page as text. |

---

## Resolving a URL

`channel_id_from_url` takes one of three shapes:

| Input | How it resolves |
|---|---|
| `youtube.com/channel/UC...` | Read straight out of the URL. No request. |
| `youtube.com/user/{name}` or `youtu.be/...` | The name is taken as the id and used directly. |
| `youtube.com/@handle`, or a bare host | **The page is fetched** and the id is read out of it. |

The fetch is the only network call, and it happens only for a handle, because a
handle is a display name and the only way to its id is to ask YouTube.

The page is searched for an id in three places, in order, and the first that
yields one wins:

1. `"channelId":"UC..."` in the page's JSON state
2. `"externalId":"UC..."`, which the same state uses under a different key
3. a `<link rel="canonical">` pointing at `/channel/UC...`

**Where a page carries the same id more than once, the most frequent wins.** A
page can name a channel in its own state and several more in embedded
recommendations, video metadata and comment threads; the repeated one is the
channel the URL is actually about.

A `/user/{name}` URL is resolved by using the name as an id rather than fetching
the page. That is a deliberate shortcut and it is only correct while YouTube keeps
accepting user names where a channel id is expected. It falls out of the URL
shape: a `/user/` path carries something that looks like an id, and treating it as
one avoids a request.

---

## Safety

`normalize_url` refuses anything that is not an http or https URL on a YouTube
host, and this is the reason the tool will not fetch an arbitrary address.

- A **bare host** is given an `https://` prefix, so `youtube.com/@x` works.
- A **non-http scheme** is refused outright. This is what blocks `file://`, and
  with it a page that would read from the local disk.
- **A non-YouTube host** is refused, whatever the scheme. This is what blocks a
  redirect trick, where a URL on a host you trust points somewhere you do not.

The host list is checked after lower-casing and dropping a trailing dot, so
`YOUTUBE.COM.` is the same host as `youtube.com`.

---

## Output

`rss_for_channel` builds the feed URL from the id alone. It is not verified against
the live feed, so a syntactically valid but non-existent id produces a URL that
404s when followed. `--fetch` is what confirms the feed resolves.

The CLI prints one JSON object:

```json
{"channel_url": "...", "channel_id": "UC...", "feed_url": "https://www.youtube.com/feeds/videos.xml?channel_id=UC..."}
```

With `--fetch` it also fetches the feed and adds `videos`, each with
`video_id`, `title`, `url` and `published`, newest first as the feed lists them.

How an agent uses it is in [SKILL.md](SKILL.md).

---

## Errors

| Condition | Behaviour |
|---|---|
| No URL given | The usage message is printed. |
| URL is not http or https | Refused, naming the scheme. |
| URL is not a YouTube host | Refused, naming the host. |
| The page cannot be fetched | Reported, with the reason. |
| The page yields no channel id | Reported: the id could not be determined. |
| The feed 404s on `--fetch` | Reported with the status code. |

---

## Notes

**This scraper is not in the shared contract** that the two cinema scrapers hold
to. It has no counterpart on either of them: it resolves a URL and stops, where
they query a site and return records.

`channel_id_from_url` returns `None` when the page yields no channel id.

---

## youtube_channel.py

A creator's name turned into one YouTube channel, or the close candidates when
the match is not clear. It started as the owner's global `yt-channel.sh` agent
script and was moved here in Python.

```bash
lib/.venv/bin/python lib/youtube/youtube_channel.py "alex meyers"
lib/.venv/bin/python lib/youtube/youtube_channel.py "科學漫聊"
```

```json
{"status": "found", "name": "Alex Meyers", "channel": "@AlexMeyersVids",
 "subscribers": "3.92M subscribers", "url": "https://www.youtube.com/@AlexMeyersVids"}
{"status": "ambiguous", "query": "alex",
 "candidates": [{"name": "alex ♡", "channel": "@Alexalexalexalexalexalexalex", "subscribers": "1.18M subscribers",
                 "url": "https://www.youtube.com/@Alexalexalexalexalexalexalex"}]}
```

`channel` is the @handle, or the UC channel id when the channel has no handle,
and `url` follows it, for each candidate too. When nothing comes close the script prints
`No channel found for "<name>".` on stderr and exits 1.

### The clear-match rule

1. One request: YouTube's search with its channels-only filter. The channel
   results are read out of the page's `ytInitialData`, and the first five are
   kept.
2. A result is **close** when its name or handle, with case, spaces and
   punctuation removed, contains the query or is contained in it.
3. The close result with the most subscribers is **found** when its name or
   handle equals the query and it has at least 5 times the subscribers of every
   other close result. Otherwise the close results come back as **ambiguous**,
   most subscribers first, and the caller asks the person which one.

Traditional and Simplified Chinese count as the same: `科學漫聊` matches
`科学漫聊`. The script asks macOS's own Hant-Hans transform through
`osascript`, only when a text holds Chinese. On another system the step is
skipped and the texts are compared as written. This is a local call, not an
HTTP request, so the rule that every request goes through `requests` holds.

### Functions

- `search_page(name)`: the search HTML, with `requests`.
- `parse_channels(html)`: every channel result, in page order. Pure.
- `simplified_chinese(texts)`: `{text: Simplified form}` on macOS, else `{}`.
- `subscriber_count(text)`: `"3.93M subscribers"` as `3930000.0`.
- `choose(query, channels, simplified)`: the rule above. Pure.
- `find_channel(name)`: the whole run.

### Known limit

YouTube's ranking of the first five results changes between requests. A
channel just outside them, such as a big namesake, can be missed, and then a
smaller exact match can clear the five-times lead. `alex` is an example: some
searches return it as ambiguous and some as found. The global script behaved
the same way.

### Testing

`test_youtube.py` runs `parse_channels` and `choose` over a real search for
"alex meyers" in `fixtures/`, cut down to its channel results, and over small
made-up lists for the ambiguous, Chinese and no-match cases.
