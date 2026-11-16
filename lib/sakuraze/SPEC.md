---
id: sakuraze
type: module-design
status: draft
title: Sakuraze novel discovery and public reading
parent: webber-architecture
---

## Responsibility

Retrieve public novel metadata and selected chapter text from `https://sakuraze.vercel.app` for assistant-led plot discovery. Search operates locally over a bounded snapshot of up to 500 visible, approved, non-NSFW novels, including full descriptions and joined genre/tag names. The snapshot follows the site's public browsing scope, not its limited title-search RPC. Its source count, cap, matched count and page coverage remain explicit; it is not semantic ranking or an unrestricted catalogue crawl.

Search is a case-insensitive substring across titles, descriptions, authors and taxonomy names. Genre filters match any selected exact name or known UUID; tag filters require all selected exact names. Tag names come from the snapshot's joins, which do not expose tag IDs. The assistant owns alternative queries, candidate comparison and uncertainty.

## Anonymous API exception

Ordinary browser request headers are required: the default Python user agent can receive 404 even while the site works. The public SPA distributes a Supabase anonymous key for logged-out visitors. The user explicitly authorized this narrowly scoped exception to `webber-goal`'s no-user-credentials rule. The script discovers the key from the unique same-origin entry asset, checks the expected project and anon role, and sends it only to the fixed source API project. It is never printed in provenance or errors. This is a sanity check of public configuration, not cryptographic authentication.

The API host is `hlzjslwrhabsxdskinwd.supabase.co`. A strict allowlist admits only approved public novel queries, genre lookup, `get_chapter_list_for_reader` and `get_chapter_for_reader`. No account authentication, arbitrary API calls, service-role keys, view increments, automatic unlocks, purchases or reading-history writes are permitted. Redirects are not followed and user sessions are not inherited.

## Chapter evidence

Details expose canonical novel/chapter URLs and reader chapter-list metadata. Locked novels are rejected before requesting chapter text. The reader RPC always uses the site's fixed 1600-character preview parameter. Only explicitly free/full or preview/not-full combinations are returned, preserving access flags and extracted text separately from source HTML. A public preview is never labelled a full chapter. Other access states and unavailable responses are errors, including HTTP200 responses with failed access flags.

## Local-first cache

Validated public configuration and read responses are cached atomically under ignored `out/sakuraze/` for 24 hours. Identity includes version, method, URL, parameters and body; the public key is omitted from request provenance. Different discovery queries reuse the same saved snapshot, so cached searches need no network bootstrap. Explicit refresh updates saved responses. Invalid, future, mismatched and corrupt entries are ignored; failed live requests never silently fall back to stale data. Cache-write failure warns but preserves successful live results.

## Verification

Network-blocked tests cover parsing, public-scope and lock enforcement, config/endpoint validation, filters, cache identity/expiry/failures and CLI combinations. Small real metadata fixtures contain neither keys nor chapter prose. Minimal anonymous smoke checks distinguish a free full chapter from a paid public preview and confirm cache-only reuse.
