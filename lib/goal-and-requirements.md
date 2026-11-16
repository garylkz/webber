---
id: webber-goal
type: goal-and-requirements
status: draft
title: "webber: goal and requirements"
---

## Goal

webber is a personal toolkit of small scrapers. Each one reads a public
website or feed the way an anonymous browser would and prints the result as JSON,
so the owner and the AI agents working for them can run it from the command line
and other code can import it.

## Problem

The sites it reads have no usable public API, or an API that needs an account:
cinema chains with undocumented booking services, lottery operators, a port
operator, Goodreads after it retired its API, Meetup's frontend GraphQL endpoint.
Each scraper turns one of those into stable, machine-readable records.

## Audience

The repository owner, and the AI agents that run the scripts on the owner's
behalf (for example, the `youtube-rss` agent skill turns a pasted YouTube
channel URL into its feed URL, and the news tools print JSON for an agent to
relay).

## Capabilities

- **Cinemas (Malaysia):** films, showtimes, seat availability, seat maps and
  ticket prices for GSC and TGV, through one interchangeable interface
  (`gsc`, `tgv`).
- **4D results (Malaysia):** Magnum 4D and Da Ma Cai draws, latest and historical
  (`fourd`).
- **Port enquiries:** Northport container, bill of lading, vessel and booking
  lookups (`northport`).
- **Events:** Meetup event search near a city, or swept across a whole country
  (`meetup`).
- **Books:** a Goodreads user's shelf (`goodreads`), and novel discovery from
  plot clues, keywords, genres and tags on Fenrir Realm, Sakuraze and Novelight
  (`fenrir-realm`, `sakuraze`, `novelight`). Sakuraze and Novelight also provide
  selected public chapter text for plot verification, with previews distinguished
  from full chapters.
- **YouTube:** a channel URL turned into its RSS feed URL (`youtube`).
- **News:** the Hacker News front page and a story's comment thread, and the
  Rock Paper Shotgun feed and a full article, as JSON for an agent to relay
  (`hacker-news`, `rps`).

## Principles

- **JSON on stdout.** Every CLI's result is machine-readable JSON.
- **Anonymous access only.** A scraper calls what a logged-out browser can call:
  no accounts, user credentials, user-provided API keys or login-gated data.
  The user-approved exception for Sakuraze's frontend-distributed anonymous key
  is bounded by `sakuraze`; it grants no paid access or write operations.
- **One file per tool.** Each tool is a single importable module that is also its
  own CLI, and runs on its own.

How the tools hold to these is in `webber-architecture`.

## Non-goals

- Anything behind a login (member benefits, favourites, rewards on the cinema
  booking apps).
