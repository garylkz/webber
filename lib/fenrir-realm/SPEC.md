---
id: fenrir-realm
type: module-design
status: draft
title: Fenrir Realm novel discovery
parent: webber-architecture
---

## Responsibility

Retrieve public metadata from `https://fenrirealm.com` so an assistant can identify novels from plot descriptions, genres and remembered clues. The script exposes keyword search/browse, genre/tag taxonomy lookup, and full novel details with chapter lists. The assistant owns semantic interpretation, query refinement and evidence-backed explanations; the script neither runs a model nor invents match confidence.

Catalogue summaries are truncated and omit tags. Candidate verification uses full descriptions and assigned tags from details. Search results carry page coverage, always explicitly non-exhaustive; source keyword matching and multiple-filter semantics are not treated as a verified semantic or all-traits match. The source's public `/api/new/v2` endpoints are undocumented and may change.
Chapter links preserve the source's canonical relative slug, including grouped
paths, rather than inventing paths from numbers. Only absent slugs use the
frontend's group/number-part fallback. Relative-path validation rejects traversal,
foreign URLs, empty path segments, query strings and fragments.

## Local-first responses

Every successful endpoint response is saved beneath the repository's ignored `out/fenrir-realm/`, keyed by endpoint and normalized parameters, including pagination and resolved taxonomy identifiers. Saved responses are reused for 24 hours, then fetched live. Explicit refresh bypasses reads and updates the cache. This response cache is not a full catalogue or a cross-query semantic index.

Each request reports its source and UTC fetched timestamp, including taxonomy resolutions. Atomic replacement prevents partial cache files. Invalid, future-dated, mismatched or corrupt entries are ignored. Cache write failures warn without losing a successful response; failed live requests remain errors, with no silent stale fallback. These rules distinguish an unavailable source from an empty match set and let the assistant judge freshness.

## Boundary

The module follows `webber-architecture` conventions and uses anonymous `requests` only. Inputs and response shapes are validated; foreign URLs and path traversal are not accepted as slugs, and redirects are not followed. A taxonomy filter must resolve unambiguously by exact case-insensitive name, slug or UUID. No new dependency or separate HTTP service is required.

Full chapter text is unsupported. Public chapter lists expose price/read-only metadata, not proof of readability. An observed free chapter returned locked content, and the site uses read authorization and content protection. There is no challenge solving, authentication, decryption or payment bypass. Metadata-only reading support is an unconfirmed scope assumption because the user skipped its confirmation, consistent with the project's anonymous-access boundary.

## Verification

Offline tests block network access and use a saved public response sample to exercise parsing, input validation, filter resolution, cache identity and expiry, write failures, malformed responses and protected-reading disclosure. MCP command construction and schema checks live in `mcp`. Live smoke checks can confirm search, taxonomy resolution, detail and chapter-list access without requesting chapter text.
