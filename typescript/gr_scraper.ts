#!/usr/bin/env node -T
// Scrape a Goodreads user's bookshelf through the public RSS endpoint.
//
// Single file, zero dependencies, no build step. Node 22.6+ (developed against
// Node 24 LTS) runs this directly: Node strips the types at load time.
//
//     node gr_scraper.ts                        # a default user's read shelf
//     node gr_scraper.ts search <name>          # look up a user id first
//     node gr_scraper.ts 1123497 to-read        # a user id and a shelf
//
// Also importable:
//
//     import { scrape, findUser } from './gr_scraper.ts';
//     const books = await scrape('1123497', 'read');
//
// How it works: Goodreads retired its official API and no new API keys exist.
// The HTML shelf page (/review/list/{id}) requires a login, but the RSS endpoint
// (/review/list_rss/{id}?shelf={shelf}) stays public. It paginates, but ignores
// per_page above 200, so pages are fetched in chunks of 200 until one comes back
// empty. Each item carries the book title, cover, author and average rating in
// its description block, along with the user's own rating, read-at date, date
// added, shelves and review text.

import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0';
const BASE = 'https://www.goodreads.com';
const DEFAULT_USER_ID = '1123497';
const MAX_PAGES = 50;
const PER_PAGE = 200;
const TIMEOUT = 30;

const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), 'out');

/** Thrown when a run cannot continue; main() turns it into one stderr line. */
export class GrError extends Error {}

const die = (message: string): never => {
  throw new GrError(message);
};

/**
 * One book off one shelf, as the feed describes it.
 *
 * The keys are the description block's own labels rather than camelCase names,
 * so the JSON this writes is the same shape the Python script writes and the two
 * can be diffed against each other. A key is absent when the block had no such
 * line, rather than present and empty, because an unread field and an empty one
 * are not the same thing on Goodreads.
 */
export interface Book {
  guid: string;
  title: string;
  author?: string;
  name?: string;
  'average rating'?: string;
  'book published'?: string;
  rating?: string;
  'read at'?: string;
  'date added'?: string;
  shelves?: string;
  review?: string;
  book_url?: string;
  [key: string]: string | undefined;
}

/** The description keys read out of the item's description block, in order. */
const FIELDS = [
  'author',
  'name',
  'average rating',
  'book published',
  'rating',
  'read at',
  'date added',
  'shelves',
  'review',
] as const;

/** Strip the CDATA wrapper Goodreads wraps its feed text in. */
function unescapeCdata(text: string): string {
  return text.replace(/<!\[CDATA\[|\]\]>/g, '').trim();
}

function firstMatch(source: string, re: RegExp): string | null {
  const match = re.exec(source);
  return match ? (match[1] as string) : null;
}

/**
 * One <item> block into a Book.
 *
 * The description is HTML rendered as `key: value<br>` pairs, so the fields come
 * out with a per-key regex rather than a parser. The book URL is pulled from the
 * only href in there and trimmed of its query string, which Goodreads uses to
 * carry tracking parameters.
 */
export function parseItem(raw: string): Book {
  const guid = unescapeCdata(firstMatch(raw, /<guid[^>]*>([\s\S]*?)<\/guid>/) ?? '');
  const title = unescapeCdata(firstMatch(raw, /<title>([\s\S]*?)<\/title>/) ?? '');
  const desc = unescapeCdata(firstMatch(raw, /<description>([\s\S]*?)<\/description>/) ?? '');

  const book: Book = { guid, title: title || '?' };

  for (const key of FIELDS) {
    const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const value = firstMatch(desc, new RegExp(`${escaped}:\\s*([\\s\\S]*?)(?:<br|$)`));
    if (value !== null) book[key] = value.trim();
  }

  const url = firstMatch(desc, /href="(https:\/\/www\.goodreads\.com\/book\/show\/\d+[^"]*)"/);
  if (url) book.book_url = url.split('?')[0] as string;

  return book;
}

/** Every <item> block in a feed body. */
function parseItems(xml: string): string[] {
  return [...xml.matchAll(/<item>([\s\S]*?)<\/item>/g)].map((m) => m[1] as string);
}

async function get(url: string): Promise<string> {
  const res = await fetch(url, {
    headers: { 'User-Agent': UA },
    signal: AbortSignal.timeout(TIMEOUT * 1000),
  });
  if (!res.ok) die(`GET ${url} failed: HTTP ${res.status}`);
  return res.text();
}

/**
 * Every book on a user's shelf, and how many pages it took.
 *
 * Pages stop at the first empty one, which is how the feed signals the end. A
 * book that appears on more than one page is dropped on its later appearance,
 * matched on the review GUID rather than the title: two shelves entries can share
 * a title and still be different rows.
 */
export async function scrape(
  userId: string,
  shelf = 'read',
): Promise<{ books: Book[]; pages: number }> {
  if (!/^\d+$/.test(userId)) die(`User ID must be numeric, got '${userId}'`);

  const seen: Book[] = [];
  const guids = new Set<string>();
  let pages = 0;

  for (let page = 1; page <= MAX_PAGES; page += 1) {
    const query = new URLSearchParams({ shelf, per_page: String(PER_PAGE), page: String(page) });
    const xml = await get(`${BASE}/review/list_rss/${userId}?${query}`);
    const items = parseItems(xml);
    if (!items.length) break;
    pages = page;
    for (const raw of items) {
      const book = parseItem(raw);
      if (book.guid && guids.has(book.guid)) continue;
      if (book.guid) guids.add(book.guid);
      seen.push(book);
    }
  }

  return { books: seen, pages };
}

/**
 * The Goodreads user ids a name matches, via the public people search.
 *
 * Ids also appear in profile URLs as /user/show/{id}-{slug}, which is what this
 * reads. Sorted, so the order is stable between runs.
 */
export async function findUser(name: string): Promise<string[]> {
  const url = `${BASE}/search?q=${encodeURIComponent(name)}&search_type=people`;
  const html = await get(url);
  const found = new Set<string>();
  for (const match of html.matchAll(/\/user\/show\/(\d+)-[a-zA-Z0-9_]+/g)) {
    found.add(match[1] as string);
  }
  return [...found].sort();
}

const USAGE = `usage: node gr_scraper.ts [<user-id> [shelf]] | search <name>

  search <name>          look up a Goodreads user id by name
  <user-id>              the id, or use a profile URL to find it
  shelf                  read (default), currently-reading, to-read, or a custom shelf

Output is written to out/gr_books_<id>_<shelf>_<timestamp>.json.

examples:
  node gr_scraper.ts
  node gr_scraper.ts search "tim clark"
  node gr_scraper.ts 1123497 to-read`;

/** A YYYYMMDD_HHMMSS stamp for the output file name. */
function timestamp(): string {
  const now = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  return (
    `${now.getFullYear()}${p(now.getMonth() + 1)}${p(now.getDate())}` +
    `_${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}`
  );
}

export async function main(argv: string[] = process.argv.slice(2)): Promise<void> {
  if (argv.includes('--help') || argv.includes('-h')) {
    console.log(USAGE);
    return;
  }

  if (argv[0] === 'search') {
    if (argv.length !== 2) die('Usage: node gr_scraper.ts search <name>');
    const ids = await findUser(argv[1] as string);
    if (!ids.length) die('No users found.');
    for (const id of ids.slice(0, 10)) console.log(id);
    return;
  }

  const userId = argv[0] ?? DEFAULT_USER_ID;
  const shelf = argv[1] ?? 'read';
  if (!/^\d+$/.test(userId)) die('Usage: node gr_scraper.ts <numeric-user-id> [shelf]');

  const { books, pages } = await scrape(userId, shelf);
  console.log(
    `Fetched ${books.length} unique books from user ${userId} ` +
      `shelf '${shelf}' (${pages} page(s)).`,
  );

  await mkdir(OUT_DIR, { recursive: true });
  const path = join(OUT_DIR, `gr_books_${userId}_${shelf}_${timestamp()}.json`);
  await writeFile(path, JSON.stringify(books, null, 2), 'utf8');
  console.log(`Saved ${books.length} books to ${path}`);
}

const entry = process.argv[1];
if (entry && import.meta.url === `file://${entry}`) {
  main().catch((e: unknown) => {
    console.error(e instanceof Error ? e.message : String(e));
    process.exit(1);
  });
}