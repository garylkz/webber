---
name: fenrir-realm-discovery
description: Find Fenrir Realm novels matching a plot description, genre, trope or remembered detail. Retrieve public summaries, tags and chapter lists, with local-first caching.
---

# Find a Fenrir Realm novel

Use this when the user describes a plot or asks for novels on Fenrir Realm, rather than requiring an exact title. The site is `https://fenrirealm.com`.

## Retrieve candidates

Use the `fenrir_realm_novels` MCP tool, or run the script from this repository's root:

```bash
lib/.venv/bin/python lib/fenrir-realm/fenrir_realm_novels.py --search 'demon hunter' --per-page 10
lib/.venv/bin/python lib/fenrir-realm/fenrir_realm_novels.py --taxonomies
lib/.venv/bin/python lib/fenrir-realm/fenrir_realm_novels.py --genre Fantasy --tag Demons --page 1
lib/.venv/bin/python lib/fenrir-realm/fenrir_realm_novels.py --detail american-demon-hunting-idol
```

1. Extract distinctive clues from the user's description: setting, character roles, events, genres and tropes. Distinguish required clues from vague memories.
2. Try short keyword searches and alternative phrasings. The site's search is not semantic search; pasting the entire description can miss candidates. Genre and tag filters use exact names, slugs or UUIDs; use taxonomies when unsure.
3. Inspect `coverage` and request additional pages where useful. Broaden an unsuccessful query instead of declaring the novel absent. Do not assume that multiple source filters require all the requested traits; verify candidates' metadata.
4. Fetch `detail` for promising slugs. Catalogue summaries are truncated and omit tags; detail supplies the full summary, tags and the first 20 chapters (`chapter_limit` changes that), plus the latest chapter and the total count. Do not claim to have read chapters.
5. Compare evidence against the clues. If none fits, explain the gaps and ask for a discriminating detail rather than inventing a match.

MCP uses `search`, `genres` and `tags` arrays, `page` and `per_page`; select `detail` or `taxonomies: true` instead of search/filter/pagination parameters.

## Local first

Responses are saved in `out/fenrir-realm/`. Identical endpoint requests and normalized filter combinations reuse the saved response for 24 hours. `requests` in the output records each response's `source` and `fetched_at`. A new query or page still needs a live request; this is not a complete local catalogue or semantic index.

Use `refresh: true` in MCP or `--refresh` in the CLI to fetch live and update saved responses. Expired responses are refreshed automatically. Network errors are errors, not proof of no matches; stale results are not silently returned.

## Present matches

Return each candidate's title, its exact `url`, and a brief explanation of which clues the summary or tags support. Separate verified facts from inference and state uncertainty. Do not label a keyword hit a confirmed match, or claim exhaustive coverage from a few queries or pages. For example: "This summary supports the demon-hunter and entertainment-industry clues; the school setting is not confirmed."

## Reading boundary

Chapter lists and prices are metadata only. Protected chapter text is unsupported, even if a chapter costs zero. Do not solve challenges, negotiate read authorization, decrypt content or bypass login/payment. Report this limit if chapter text is requested.
