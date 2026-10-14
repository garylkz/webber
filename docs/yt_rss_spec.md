# YouTube RSS Scraper — Specification

A YouTube channel URL turned into that channel's official RSS feed URL. This
document describes what each function does. Nothing here is specific to a language
or runtime.

Available in [Python](../python/yt_rss.py) and
[TypeScript](../typescript/yt_rss.ts), with the same behaviour.

---

## Usage

The CLI is the same in both languages. Substitute the runner and the extension:

```bash
cd python        # or: cd typescript
python yt_rss.py "https://youtube.com/@handle"
python yt_rss.py "https://youtube.com/@handle" --fetch
```

A `/channel/UC...` URL carries its own id and needs no request. A handle does not,
so that is the one case where a page is fetched, and the id is read out of it.

Imported:

```python
import yt_rss as yt

feed = yt.rss_for_channel("UC...")
id_ = yt.channel_id_from_url("https://youtube.com/@handle")
```

```ts
import { rssForChannel, channelIdFromUrl } from './yt_rss.ts';

const feed = rssForChannel('UC...');
const id = await channelIdFromUrl('https://youtube.com/@handle');
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
| `rssForChannel(channelId)` | `str` | The feed URL for a channel id. No network. |
| `channelIdFromUrl(url)` | `str` or `null` | The channel id for any YouTube channel URL, fetching the page when the URL does not carry one. |
| `normaliseUrl(url)` | `str` | Forces https and refuses anything that is not a YouTube URL. No network. |
| `fetchPage(url, timeout?)` | `str` | Fetches one page as text. |

---

## Resolving a URL

`channelIdFromUrl` takes one of three shapes:

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

`normaliseUrl` refuses anything that is not an http or https URL on a YouTube
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

`rssForChannel` builds the feed URL from the id alone. It is not verified against
the live feed, so a syntactically valid but non-existent id produces a URL that
404s when followed. `--fetch` is what confirms the feed resolves.

The CLI prints the feed URL on its own line, then prints the feed body if
`--fetch` was passed.

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
they query a site and return records. What the two implementations do share is
the refusal rules above, and those are the part worth checking when porting.

The type of `channelIdFromUrl` is the one real difference between the two: Python
returns `None`, TypeScript returns `null`. Everything else about the pair is
identical, including which id wins when a page offers several.