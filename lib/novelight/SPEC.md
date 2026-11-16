---
id: novelight
type: module-design
status: draft
title: Novelight novel discovery and public reading
parent: webber-architecture
---

## Responsibility

Retrieve the whole public Novelight catalogue, full novel summaries and selected public chapters from `https://novelight.net` for assistant-led discovery and plot verification. This is not limited to Sakuraze's translator profile. The module follows `webber-architecture`'s standalone Python/CLI and JSON conventions without importing other source modules.

Catalogue retrieval uses verified full-page search and genre/tag filters, not the live-search widget. The assistant interprets plot clues, refines short keyword queries and checks candidates' full details or selected chapters. Keyword matches are not semantic proof, and no source substitution or bulk chapter download is performed.

## Metadata and coverage

Genre/tag identifiers come from each catalogue checkbox's value, not the element ID. Exact case-insensitive names or numeric IDs must resolve unambiguously. Search and filter parameters are retained when paging, even though source pagination links omit them. Catalogue pages have a source-defined size of 30; output reports returned items and discovered page links, not an invented total count. Invalid/out-of-range requests remain errors rather than empty results.

Only catalogue cards are novel listings. Details extract the information section and actual chapter-list container, excluding recommendation widgets and duplicate latest-release previews. Chapter-list pagination is explicit: detail returns one selected list page, not necessarily the whole book. Translator attribution is metadata and never changes the selected source.

## Public reading boundary

The public chapter wrapper initially contains a loading placeholder. Selected chapter reading fetches the source's anonymous AJAX endpoint with ordinary browser, AJAX and same-origin Referer headers. No login, cookies, CSRF tokens, challenge solving, decryption or purchases are required by the verified requests. Redirects and foreign source links are not followed.

Readable prose comes only from the chapter-text container, excluding advertisement blocks (including source spelling variants), executable content, hidden elements, comments and navigation. Loading pages, access prompts, empty content and failed requests are unavailable, not chapter text. A successful read establishes access to that chapter only; it does not establish that all chapters are free or readable. Canonical chapter/novel links and previous/next navigation accompany text for evidence tracing. Access-prompt detection is pinned to tested formats, not every possible catalogue chapter variant.

## Local-first cache

Validated raw HTML/JSON responses are saved atomically under ignored `out/novelight/` and reused for 24 hours. Identity includes URL, normalized filter parameters and request-header/response mode. Explicit refresh updates cached responses. Each result carries source and per-request retrieval timestamps. Invalid, future-dated, mismatched and corrupt cache entries are ignored; live failures never silently return stale data. Cache-write failures warn while preserving successful retrievals.

## Verification

Offline tests block network access and use small synthetic HTML/JSON fixtures for selectors, coverage, filters, chapter prose cleaning, errors, cache behavior and CLI validation. Anonymous smoke checks cover source markup, full-page search, details, public AJAX reading and local cache reuse. Multi-page chapter-list parsing is tested offline; not every source chapter format or later chapter-list page is live-verified.
