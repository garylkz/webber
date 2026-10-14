# Goodreads Scraper — Specification

A user's bookshelf, read through the public RSS endpoint Goodreads left open after
retiring its official API. This document describes what each function does.
Nothing here is specific to a language or runtime.

Available in [Python](../python/gr_scraper.py) and
[TypeScript](../typescript/gr_scraper.ts), with the same behaviour.

---

## Usage

The CLI is the same in both languages. Substitute the runner and the extension:

```bash
cd python        # or: cd typescript
python gr_scraper.py                                  # a default user's read shelf
python gr_scraper.py search <name>                    # look up a user id first
python gr_scraper.py 12345678 to-read                 # a user id and a shelf
```

Output is written to `out/gr_books_{id}_{shelf}_{timestamp}.json`. Pages are
fetched 200 at a time until one comes back empty, and duplicates are dropped by
review GUID rather than by title, since one book can sit on two shelves.

Imported, both languages take the same two calls. The Python one takes a
`requests` session first; the TypeScript one carries its own connection:

```python
import requests
import gr_scraper as gr

session = requests.Session()
books, pages = gr.scrape(session, "1123497", "read")
ids = gr.find_user(session, "tim clark")
```

```ts
import { scrape, findUser } from './gr_scraper.ts';

const { books, pages } = await scrape('1123497', 'read');
const ids = await findUser('tim clark');
```

---

## Concepts

| Term | Meaning |
|---|---|
| **User id** | Goodreads' numeric id for a person, as it appears in a profile URL: `/user/show/{id}-{slug}`. It is what a shelf query needs. |
| **Shelf** | One of a user's book lists. Three are standard — `read`, `currently-reading`, `to-read` — and a user can create their own. |
| **Review** | One entry on a shelf: a book, the user's rating, dates and review text. It is the unit that is scraped and deduplicated. |
| **GUID** | The review's own permalink, e.g. `https://www.goodreads.com/review/show/654641581`. It is the identity of a row. |

---

## Functions

| Function | Returns | Description |
|---|---|---|
| `scrape(userId, shelf)` | `{books, pages}` | Every review on a user's shelf, and how many pages it took. Paginates until a page comes back empty. |
| `findUser(name)` | `list[str]` | The user ids a name matches, via the public people search. Sorted, so the order is stable. |
| `parseItem(raw)` | `Book` | One `<item>` block from the feed, as a record. No network. |

The Python implementation takes a `requests` session as its first argument on
both network calls, so one connection is reused across every page; the
TypeScript one carries its own and has no such argument.

```python
books, pages = gr.scrape(session, "1123497", "read")
ids = gr.find_user(session, "tim clark")
```

```ts
const { books, pages } = await scrape('1123497', 'read');
const ids = await findUser('tim clark');
```

---

## Reading a shelf

`scrape` walks the feed a page at a time:

```
/review/list_rss/{userId}?shelf={shelf}&per_page=200&page={n}
```

- **The page size is capped at 200.** Goodreads ignores anything larger, so asking
  for more silently returns a smaller page. Pages are requested one at a time.
- **Paging stops at the first empty page**, which is how the feed signals the end.
  There is no total count in the response to check against.
- **A hard ceiling of 50 pages** bounds a run, so a shelf that never returns an
  empty page cannot loop forever. That is 10,000 reviews.
- **Duplicates are dropped by review GUID, not by title.** Two shelf entries can
  carry the same book, and the same book can appear on two shelves; the GUID
  identifies the row, so a book read twice stays two rows and one row fetched
  twice stays one.

The endpoint needs no login, which is the whole reason this path exists: the HTML
shelf page at `/review/list/{id}` requires one.

---

## A review record

Every field below is read out of the item's description block, which Goodreads
renders as HTML of `key: value<br>` pairs. The keys are kept as they appear rather
than renamed, so the JSON this writes can be diffed against the other
implementation's.

| Field | Meaning |
|---|---|
| `guid` | The review permalink, and the dedupe key. |
| `title` | The book's title. |
| `author` | The book's author. |
| `name` | The shelf owner's display name, not the author's. |
| `average rating` | The book's mean rating across all readers. |
| `book published` | The publication year. |
| `rating` | **The owner's own rating**, not the book's average. |
| `read at` | When they finished it. |
| `date added` | When they shelved it. |
| `shelves` | Every shelf the book sits on for this user. |
| `review` | The review text. |
| `book_url` | The book's page, trimmed of its tracking query string. |

**A field the description block does not carry is absent from the record**, not
present and empty. Goodreads distinguishes "not rated" from "rated zero", and
flattening that difference would lose it. An empty `read at` for a book that is
shelved but unread is a real value, and does appear.

`rating` and `average rating` are strings, not numbers, because Goodreads ships
them as text and a value like `4.20` is not distinguishable from a number once
rounded by a caller that was not told what it was.

---

## Output

`scrape` returns its records in the order the feed listed them, which is
Goodreads' own ordering and is not stable between runs for a changing shelf.

The CLI writes them to `out/gr_books_{userId}_{shelf}_{timestamp}.json`, where the
timestamp is `YYYYMMDD_HHMMSS` at the moment of writing, so successive runs do not
overwrite each other. `out/` is git-ignored.

---

## Errors

| Condition | Behaviour |
|---|---|
| User id is not numeric | Rejected before any request is made. |
| HTTP error from the feed | Reported with the status code. |
| Network error or timeout | Reported with the reason. |
| People search finds nobody | Reported, and nothing is written. |
| A page returns items with no GUID | Kept, but it cannot deduplicate. |

A shelf name that does not exist is not an error: Goodreads answers an unknown
shelf with an empty feed, which reads as a shelf with nothing on it. The count
printed will be zero, not a failure.

---

## Notes

**This scraper is not in the shared contract** that the two cinema scrapers hold
to. It has no counterpart on either of them and does not need one: it reads one
site, so there is nothing to be interchangeable with. The TypeScript port emits
the same keys and produces byte-identical files, which is the same standard of
parity, just without the second site to prove it against.