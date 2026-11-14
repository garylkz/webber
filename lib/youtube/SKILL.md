---
name: youtube-rss
description: Turns a YouTube channel URL into the channel's RSS feed URL. Use when the user pastes a YouTube channel URL (youtube.com/@handle, /channel/UC..., /user/...), with or without asking for anything, or asks for a channel's RSS feed.
---

# YouTube RSS

Run this from the webber repo root, with the URL the user pasted:

```sh
lib/.venv/bin/python lib/youtube/youtube_rss.py "<channel url>"
```

It prints one JSON object:

```json
{"channel_url": "...", "channel_id": "UC...", "feed_url": "https://www.youtube.com/feeds/videos.xml?channel_id=UC..."}
```

Reply with the `feed_url`.

- Add `--fetch` only when the user also wants the channel's recent videos: the
  result then gains `videos`, each with `title`, `url` and `published`.
- If the script fails, it prints the reason on stderr (not a YouTube URL, the
  page could not be fetched, no channel id found). Report it as is.

How the tool works is in [SPEC.md](SPEC.md).
