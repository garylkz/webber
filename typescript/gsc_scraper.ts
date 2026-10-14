#!/usr/bin/env node -T
// Showtimes, ticket prices and seat maps for GSC Cinemas Malaysia.
//
// Single file, zero dependencies, no build step. Node 22.6+ (developed against
// Node 24 LTS) runs this directly: Node strips the types at load time.
//
//     node gsc_scraper.ts                          // movies now showing
//     node gsc_scraper.ts coming-soon
//     node gsc_scraper.ts advance-sales
//     node gsc_scraper.ts --all                    // every listing, counted
//     node gsc_scraper.ts --movies                 // the booking catalogue
//     node gsc_scraper.ts 235                      // one cinema, today
//     node gsc_scraper.ts 235 2026-10-14           // one cinema, one date
//     node gsc_scraper.ts --cinemas                // every outlet
//     node gsc_scraper.ts --dates                  // dates that have sessions
//     node gsc_scraper.ts --movie digger           // one film, every cinema
//     node gsc_scraper.ts --detail munafik-melawan-iblis
//     node gsc_scraper.ts --json                   // machine readable
//     node gsc_scraper.ts 235 --seats              // add seat availability
//     node gsc_scraper.ts 235 --prices             // add ticket prices
//     node gsc_scraper.ts 235 --seat-map --time 10:50AM
//     node gsc_scraper.ts --no-delay               // skip the polite pause
//
// Also importable:
//
//     import { listing, movies, cinemas, day, schedule, movie, detail } from './gsc_scraper.ts';
//     await listing('now-showing');
//     await day('235');
//
// Everything that touches the network is async, so every call returns a Promise:
//
//     for (const film of await day('235')) {
//       console.log(film.title, film.genre, film.rating);
//       for (const showtime of film.showtimes) {
//         console.log('   ', showtime.time, showtime.hall, showtime.seatsLeft, showtime.price);
//       }
//     }
//
// How it works. gsc.com.my itself is a Nuxt app whose pages are server rendered,
// which is what the editorial listings and the movie detail pages are read from.
// Everything behind the "Buy Tickets" button lives on a separate host,
// `epaymentwebapp.gsc.com.my`, which is an Angular app talking to a plain,
// undocumented ASP.NET service at `epaymentapi.gsc.com.my/showtimews/service.asmx`.
// That service takes no API key, no signature and no session token; the site's
// own interceptor only adds an `Authorization` header when a member is signed in,
// and browsing anonymously sends none at all. So this calls the same endpoints
// the booking app calls, anonymously.
//
// The service speaks XML rather than JSON. Node has no XML parser built in, so
// the small one this file needs lives at the bottom under `xml.ts`: elements,
// attributes, self-closing tags, comments, CDATA and the XML declaration, which
// is everything these payloads use and nothing more.
//
// Two name schemes meet in this file, and it is worth being explicit about which
// is which:
//
// - **The service** names an outlet by a numeric id (235 is GSC Lotus's Kepong)
//   and calls it "Kepong - Lotus`s (Kepong Village Mall)".
// - **The website** names the same outlet "GSC Lotus's Kepong" and gives it a
//   slug, `/cinema/gsc-lotuss-kepong`, plus a town and a list of amenities.
//
// `cinemas()` returns the service's directory, which is what showtimes can be
// queried against. `cinemas({withDetails: true})` adds the website's slug, town
// and amenities by joining the two on name, because the two never share an id.
//
// Times: the service returns a business date plus a wall-clock time with no
// offset, e.g. date 2026-10-14 and time 0015. These are Malaysia wall-clock and
// are attached to a fixed UTC+08:00 here. Malaysia has no daylight saving, so the
// offset is always +08:00.
//
// Money: `tax` is deliberately ignored, for the same reason TGV's `taxInCents`
// is. See `TicketType`.

const SITE = 'https://www.gsc.com.my';
// The booking app and the service behind it.
const BOOKING = 'https://epaymentwebapp.gsc.com.my';
const API = 'https://epaymentapi.gsc.com.my';
// Kept as BASE so code written against the old module keeps working.
export const BASE = SITE;
const UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';

// Malaysia wall clock. No DST, so a fixed offset is exact.
const MYT_OFFSET_MS = 8 * 60 * 60 * 1000;

export const LISTINGS: Record<string, string> = {
  'now-showing': '/movies/now-showing/',
  'coming-soon': '/movies/coming-soon/',
  'advance-sales': '/movies/advance-sales/',
};

// Not a movie listing, so it lives outside LISTINGS: its parser is different.
const CINEMA_PAGE = '/cinemas/gsc/';

export const ENDPOINTS = {
  // The booking catalogue: every film, every format variant, every date it has
  // sessions on.
  movies: '/showtimews/service.asmx/getEpaymentMovie_ParentChild',
  // One film across every cinema, for one date.
  movieShows: '/showtimews/service.asmx/getShowTimesByMovie_ParentChild_V2',
  // One cinema, one date: the whole day.
  day: '/showtimews/service.asmx/getShowTimesByLocation_ParentChild_V2',
  // The hall for one session: counts and the actual seat grid.
  seatPlan: '/showtimews/service.asmx/getHallSeatStatus',
  // Ticket types for one session.
  tickets: '/showtimews/service.asmx/getTicketPricingEpaySpecialV5',
  // The outlet directory, reached through the booking app's own proxy.
  cinemas: '/api/GetCSOAllLocationV2',
};

// The outlet directory comes back as JSON through the app's proxy, so it gets
// its own base rather than the XML service root.
const CINEMA_ENDPOINT = `${BOOKING}${ENDPOINTS.cinemas}`;

// GSC uses these in place of real data, on the website's own pages.
const PLACEHOLDERS = new Set(['N/A', 'NA', '-', 'TBC', 'TBA']);

/** Tickets sold at face value. Everything else is a concession or a bundle. */
const STANDARD_TICKETS = new Set(['ADULT', 'TWIN']);

/** Brand words that carry no identity when comparing two outlet names. */
const BRAND_NOISE = new Set(['gsc', 'aurum', 'velvet']);

export const SEAT_FREE = 'A';
export const SEAT_BOOKED = 'B';
export const SEAT_BLOCKED = 'D';
export const SEAT_UNKNOWN = '?';
export const SEAT_MARKS: Record<string, string | null> = {
  [SEAT_FREE]: null,
  [SEAT_BOOKED]: 'X',
  [SEAT_BLOCKED]: '-',
};

/**
 * Town abbreviations GSC prints on the website where the service spells the town
 * out. Used only when joining the two directories.
 */
const TOWN_ABBREVIATIONS: Record<string, string[]> = {
  jb: ['johor', 'bahru'],
  kl: ['kuala', 'lumpur'],
  kk: ['kota', 'kinabalu'],
  bkt: ['bukit', 'tinggi'],
};

/** Words that carry no identity when comparing two outlet names. */
const NAME_NOISE = new Set(['the', 'mall', 'shopping', 'city', 'of', 'formerly', 'cinema']);

// ---------------------------------------------------------------------------
// Fatal error. Every "this run cannot continue" case throws GscError, which
// main() turns into a one-line message on stderr plus a non-zero exit code.
// ---------------------------------------------------------------------------

export class GscError extends Error {}

/** Thrown for a bad --from/--until/date argument; callers catch it to probe. */
class BadInputError extends GscError {}

const die = (message: string): never => {
  throw new GscError(message);
};

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------

/**
 * Strip whitespace and drop GSC's placeholder text.
 *
 * The website writes `N/A`, `NA`, `-`, `TBC` and `TBA` where it has no real
 * value. Those become null rather than surviving as literal strings a caller has
 * to filter. The service never sends placeholders, so applying the same rule to
 * both is harmless.
 */
export function clean(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const text = String(value).trim();
  if (!text || PLACEHOLDERS.has(text.toUpperCase())) return null;
  return text;
}

/** 'ENG,BM,CHI' -> ['ENG', 'BM', 'CHI']. Empty when there is nothing. */
export function splitCodes(value: string | null | undefined): string[] {
  if (!value) return [];
  return value
    .split(',')
    .map((part) => clean(part))
    .filter((part): part is string => Boolean(part));
}

/**
 * Coerce a string, an array or null into a clean list of strings.
 *
 * The website is inconsistent about whether a multi-value field arrives as one
 * comma-joined string or as a list, and both shapes show up on the same page, so
 * every list field goes through here.
 */
export function asList(value: unknown): string[] {
  if (value === null || value === undefined) return [];
  if (typeof value === 'string') {
    return value
      .split(',')
      .map((part) => part.trim())
      .filter(Boolean);
  }
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => (item === null || item === undefined ? '' : String(item).trim()))
    .filter(Boolean);
}

/** Collapse a value that may hold HTML into plain text. */
export function stripHtml(value: string | null | undefined): string | null {
  if (!value) return null;
  let text = String(value).replace(/<[^>]+>/g, ' ');
  text = text
    .replace(/&hellip;/g, '...')
    .replace(/&amp;/g, '&')
    .replace(/&nbsp;/g, ' ')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'");
  return clean(text.replace(/\s+/g, ' '));
}

/** Dedupe, keeping first-seen order. Python's dict.fromkeys, for the tuples. */
function uniq<T>(values: Iterable<T>): T[] {
  return [...new Set(values)];
}

/**
 * '(Rerun) La La Land' -> 'rerun-la-la-land'.
 *
 * The service has no slug of its own, so it is derived from the title using the
 * website's convention, which is not the obvious one: punctuation is deleted
 * rather than turned into a separator, so `Khong-Khaek 2` is `khongkhaek-2`
 * while `Spider-Man: Brand New Day` is `spiderman-brand-new-day` and
 * `Avengers:Endgame Encore` is `avengersendgame-encore`. Only runs of whitespace
 * become a dash. Checked against every film on the saved now-showing page, this
 * reproduces all of them.
 *
 * Python's `str.isalnum` counts non-ASCII letters, so accented titles keep them.
 * `[^a-z0-9]` would drop them, hence the property escape.
 */
export function slugify(title: string): string {
  const text = [...String(title)].filter((ch) => /[\p{L}\p{N}]/u.test(ch) || /\s/u.test(ch)).join('');
  const slug = text
    .trim()
    .toLowerCase()
    .replace(/\s+/g, '-')
    .replace(/^-+|-+$/g, '');
  return slug || 'untitled';
}

/**
 * '1 hr 45 mins' -> 105. null when the runtime is unknown.
 *
 * Takes either shape the two sources use: the service's bare minute count
 * ("105") or the website's prose ("1 hr 45 mins").
 */
export function durationMinutes(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const text = String(value).trim();
  if (!text) return null;
  if (/^\d+$/.test(text)) return Number(text) || null;
  const match = /(?:(\d+)\s*h(?:rs?)?\s*)?(?:(\d+)\s*m(?:ins?)?)?/i.exec(text);
  if (!match) return null;
  const hours = Number(match[1] ?? 0) || 0;
  const minutes = Number(match[2] ?? 0) || 0;
  const total = hours * 60 + minutes;
  return total || null;
}

/**
 * '(InfinityVision) Avengers:Endgame Encore' -> 'avengersendgameencore'.
 *
 * GSC marks a film's format in the title as often as in the data, so one film
 * exists under several titles that differ only by a bracketed prefix and
 * punctuation. Searching on this form finds the film whichever of them the
 * caller typed.
 */
export function looseTitle(title: string): string {
  const text = String(title).replace(/\([^)]*\)/g, ' ');
  return text.toLowerCase().replace(/[^a-z0-9]+/g, '');
}

/**
 * 'IMAX' -> 'IMAX', '2D' -> 'Digital 2D'.
 *
 * The service sends a bare code in `type` and the human label in `type_desc` as
 * "CODE - Label", so the label is the part after the dash. An unknown code still
 * renders rather than being dropped.
 */
export function experienceLabel(code: string | null | undefined): string | null {
  if (!code) return null;
  const trimmed = String(code).trim();
  if (!trimmed) return null;
  const [, , label] = splitOnFirst(trimmed, ' - ');
  return clean(label) || trimmed;
}

/** Split on the first occurrence only, like Python's str.partition. */
function splitOnFirst(value: string, sep: string): [string, string, string] {
  const at = value.indexOf(sep);
  if (at < 0) return [value, '', ''];
  return [value.slice(0, at), sep, value.slice(at + sep.length)];
}

/**
 * Business date plus a bare wall clock -> an aware Date.
 *
 * The service splits the two: `date="2026-10-14" time="0015"`, where the time is
 * HHMM with no colon and no am/pm. Both halves are needed, because the date half
 * is the calendar day while the business date may be the day before.
 */
export function sessionStart(
  on: string | null | undefined,
  clock: string | null | undefined,
): Date | null {
  if (!on || !clock) return null;
  const digits = String(clock).replace(/\D/g, '');
  if (digits.length < 3) return null;
  const hour = Number(digits.slice(0, -2));
  const minute = Number(digits.slice(-2));
  if (!(hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59)) return null;
  const day = parseIsoDate(on);
  if (!day) return null;
  return new Date(Date.UTC(day[0], day[1] - 1, day[2], hour, minute) - MYT_OFFSET_MS);
}

/** 'YYYY-MM-DD' -> [year, month, day], or null when it is not that shape. */
function parseIsoDate(value: string): [number, number, number] | null {
  const match = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(value.trim());
  if (!match) return null;
  const [, y, m, d] = match as unknown as [string, string, string, string];
  const year = Number(y);
  const month = Number(m);
  const day = Number(d);
  if (month < 1 || month > 12 || day < 1 || day > 31) return null;
  // Reject the 31st of February, which the regex above would otherwise accept.
  const probe = new Date(Date.UTC(year, month - 1, day));
  if (probe.getUTCMonth() !== month - 1 || probe.getUTCDate() !== day) return null;
  return [year, month, day];
}

/** '18:00' or '1800' -> [18, 0]. null passes straight through. */
export function parseClock(value: string | null | undefined): [number, number] | null {
  if (value === null || value === undefined) return null;
  const match = /^(\d{1,2}):?(\d{2})$/.exec(String(value).trim());
  if (!match) die(`bad time '${value}', expected HH:MM`);
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (!(hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59)) {
    die(`bad time '${value}', expected HH:MM`);
  }
  return [hour, minute];
}

/** A Date -> 'YYYY-MM-DD' in Malaysia wall-clock time. */
function mytDateString(date: Date): string {
  const shifted = new Date(date.getTime() + MYT_OFFSET_MS);
  const y = shifted.getUTCFullYear();
  const m = String(shifted.getUTCMonth() + 1).padStart(2, '0');
  const d = String(shifted.getUTCDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

const todayMyt = (): string => mytDateString(new Date());

/**
 * A Date -> 'YYYY-MM-DDTHH:MM:SS+08:00', the shape a Malaysia-aware datetime
 * serialises to.
 *
 * `toISOString()` would give the same instant as a `Z`, but the offset is what a
 * reader of this scraper's output expects to see, and it is what the Python side
 * writes, so the two stay comparable.
 */
function isoMyt(date: Date | null): string | null {
  if (!date) return null;
  const shifted = new Date(date.getTime() + MYT_OFFSET_MS);
  const p = (n: number) => String(n).padStart(2, '0');
  return (
    `${shifted.getUTCFullYear()}-${p(shifted.getUTCMonth() + 1)}-${p(shifted.getUTCDate())}` +
    `T${p(shifted.getUTCHours())}:${p(shifted.getUTCMinutes())}:${p(shifted.getUTCSeconds())}+08:00`
  );
}

/** Accept 'YYYY-MM-DD', a Date, or nothing. Returns 'YYYY-MM-DD'. */
export function normaliseDate(value?: string | Date | null): string {
  if (value === null || value === undefined) return todayMyt();
  if (value instanceof Date) return mytDateString(value);
  const text = String(value).trim();
  if (!text) return todayMyt();

  const forms: Array<[RegExp, [number, number, number]]> = [
    [/^(\d{4})-(\d{1,2})-(\d{1,2})$/, [1, 2, 3]],
    [/^(\d{1,2})-(\d{1,2})-(\d{4})$/, [3, 2, 1]],
    [/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/, [3, 2, 1]],
    [/^(\d{4})\/(\d{1,2})\/(\d{1,2})$/, [1, 2, 3]],
  ];
  for (const [re, order] of forms) {
    const match = re.exec(text);
    if (!match) continue;
    const iso = `${match[order[0]]}-${String(Number(match[order[1]])).padStart(2, '0')}-${String(
      Number(match[order[2]]),
    ).padStart(2, '0')}`;
    if (parseIsoDate(iso)) return iso;
  }
  die(`bad date '${value}', expected YYYY-MM-DD`);
}

/**
 * True when a positional argument is a date rather than a cinema name.
 *
 * The CLI takes an optional cinema and an optional date, and the parser cannot
 * tell them apart by position alone. Deciding by shape works because every
 * outlet id and name contains a letter and no date does.
 */
export function looksLikeDate(value: string | null | undefined): boolean {
  if (!value || !value.trim()) return false;
  try {
    normaliseDate(value);
    return true;
  } catch {
    return false;
  }
}

/**
 * True when a `--time` value is a time of day.
 *
 * The service prints `timestr` in 12-hour form ("10:50AM") while the CLI
 * documents 24-hour `HH:MM`, so both have to be recognised.
 */
export function looksLikeClock(value: string | null | undefined): boolean {
  if (!value) return false;
  const text = String(value).trim().toUpperCase().replace(/ /g, '').replace(/(AM|PM)$/, '');
  return /^\d{1,2}:?\d{2}$/.test(text);
}

/**
 * Every spelling of one `--time` value worth matching against `timestr`.
 *
 * The service prints `timestr` in 12-hour form ("10:50AM") while the CLI
 * documents 24-hour `HH:MM`, so both renderings are produced whichever side the
 * user is reading times off.
 */
export function clockCandidates(value: string): string[] {
  let text = value.trim().toUpperCase().replace(/ /g, '');
  const found = [value.trim()];
  let suffix = '';
  if (text.endsWith('AM') || text.endsWith('PM')) {
    suffix = text.slice(-2);
    text = text.slice(0, -2);
  }
  const digits = text.replace(/\D/g, '');
  if (digits.length !== 3 && digits.length !== 4) return uniq(found.filter(Boolean));
  let hour = Number(digits.slice(0, -2));
  const minute = Number(digits.slice(-2));
  if (suffix === 'PM' && hour >= 1 && hour <= 11) hour += 12;
  if (suffix === 'AM' && hour === 12) hour = 0;
  const twelve = hour % 12 || 12;
  const mm = String(minute).padStart(2, '0');
  found.push(`${String(hour).padStart(2, '0')}:${mm}`);
  found.push(`${twelve}:${mm}AM`);
  found.push(`${twelve}:${mm} AM`);
  found.push(`${twelve}:${mm}PM`);
  found.push(`${twelve}:${mm} PM`);
  return uniq(found.filter(Boolean));
}

// ---------------------------------------------------------------------------
// network
// ---------------------------------------------------------------------------

const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

/** Run `fn` over `items` with at most `workers` in flight, results in order. */
async function poolMap<T, R>(
  items: T[],
  workers: number,
  fn: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  const list = [...items];
  if (!list.length) return [];
  const width = Math.max(1, Math.min(workers, list.length));
  const out: R[] = new Array(list.length);
  let next = 0;
  const runners = Array.from({ length: width }, async () => {
    for (;;) {
      const index = next++;
      if (index >= list.length) return;
      out[index] = await fn(list[index], index);
    }
  });
  await Promise.all(runners);
  return out;
}

/**
 * Spaces requests out so a pool of workers still hits the site politely.
 *
 * The slot is claimed synchronously, before the await, so concurrent callers
 * queue up behind each other rather than all reading the same clock: workers
 * stay parallel, but the global rate stays at one request per `delay` seconds.
 */
export class Throttle {
  delay: number;
  private _nextSlot: number;

  constructor(delay = 0.25) {
    this.delay = Math.max(0, Number(delay) || 0);
    this._nextSlot = 0;
  }

  async wait(): Promise<void> {
    if (!this.delay) return;
    const now = performance.now();
    const slot = Math.max(now, this._nextSlot);
    this._nextSlot = slot + this.delay * 1000;
    if (slot > now) await sleep(slot - now);
  }
}

/** Options every network call takes. */
export interface NetOptions {
  delay?: number;
  timeout?: number;
  throttle?: Throttle;
}

/**
 * GET a page from gsc.com.my and return the HTML.
 *
 * This is the website, not the booking service. Used for the editorial listings,
 * the movie detail pages and the public outlet directory.
 */
export async function fetchPage(
  path: string,
  { timeout = 20, delay = 0.5, throttle }: NetOptions = {},
): Promise<string> {
  if (throttle) await throttle.wait();
  else if (delay) await sleep(delay * 1000);

  const url = `${SITE}${path}`;
  let res: Response;
  try {
    res = await fetch(url, {
      headers: { 'User-Agent': UA, Accept: 'text/html,*/*;q=0.8', 'Accept-Language': 'en' },
      signal: AbortSignal.timeout(timeout * 1000),
    });
  } catch (e) {
    die(`network error talking to gsc.com.my: ${(e as Error).message}`);
  }
  if (res.status === 404) die(`not found: ${path}`);
  if (!res.ok) die(`HTTP ${res.status} from ${path}`);
  const html = await res.text();
  // Unknown pages sometimes come back 200 with a meta refresh to /not-found.
  if (html.slice(0, 400).includes('not-found')) die(`not found: ${path}`);
  return html;
}

/**
 * Call one booking-service endpoint and return the raw body.
 *
 * The body is XML for everything under `showtimews` and JSON for the outlet
 * directory, which the booking app proxies. There is nothing to send but the
 * query: no key, no signature, no session token. The Origin and Referer the
 * booking app sends are reproduced because they cost nothing and keep the request
 * indistinguishable from normal browsing.
 */
export async function request(
  path: string,
  params?: Record<string, string | number | boolean> | null,
  { timeout = 25, throttle }: NetOptions = {},
): Promise<string> {
  if (throttle) await throttle.wait();

  let url: string;
  let accept: string;
  if (path.startsWith('/api/') || path.startsWith('http')) {
    url = path.startsWith('http') ? path : `${BOOKING}${path}`;
    accept = 'application/json, text/plain, */*';
  } else {
    const base = path.startsWith('/') ? `${API}${path}` : path;
    // urlencode does not escape '/' or '+', URLSearchParams escapes both.
    url = params !== undefined ? `${base}?${encodeQuery(params)}` : base;
    accept = 'text/xml, application/xml, */*';
  }

  let res: Response;
  try {
    res = await fetch(url, {
      headers: {
        'User-Agent': UA,
        Accept: accept,
        'Accept-Language': 'en-MY,en;q=0.9',
        Origin: BOOKING,
        Referer: `${BOOKING}/`,
      },
      signal: AbortSignal.timeout(timeout * 1000),
    });
  } catch (e) {
    die(`network error talking to gsc.com.my: ${(e as Error).message}`);
  }
  if (!res.ok) {
    const detail = (await res.text()).slice(0, 300);
    die(`HTTP ${res.status} from ${path}: ${detail}`);
  }
  return res.text();
}

/** urlencode, but without Python's habit of leaving '/' and '+' readable. */
function encodeQuery(params: Record<string, string | number | boolean>): string {
  return Object.entries(params)
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
    .join('&');
}

// ---------------------------------------------------------------------------
// records
// ---------------------------------------------------------------------------

/**
 * One GSC outlet.
 *
 * `id` is the numeric id the booking service knows the outlet by, and is what
 * every schedule query needs. `name` is the service's own name for it, which is
 * prefixed with the town ("Kuala Lumpur - EkoCheras Mall").
 *
 * `slug`, `city` and `features` come from the website instead, and are only
 * filled in when `cinemas({withDetails: true})` could join the two directories
 * on name. They are null otherwise rather than guessed.
 */
export class Cinema {
  id: string;
  name: string;
  slug: string | null;
  city: string | null;
  location: string | null; // region key, lower-cased
  region: string | null;
  state: string | null;
  address: string | null;
  postcode: string | null;
  latitude: string | null;
  longitude: string | null;
  telephone: string | null;
  parking: string | null;
  openingHours: string[];
  features: string[];
  experiences: string[];
  description: string | null;

  constructor(fields: {
    id: string;
    name: string;
    slug?: string | null;
    city?: string | null;
    location?: string | null;
    region?: string | null;
    state?: string | null;
    address?: string | null;
    postcode?: string | null;
    latitude?: string | null;
    longitude?: string | null;
    telephone?: string | null;
    parking?: string | null;
    openingHours?: string[];
    features?: string[];
    experiences?: string[];
    description?: string | null;
  }) {
    this.id = fields.id;
    this.name = fields.name;
    this.slug = fields.slug ?? null;
    this.city = fields.city ?? null;
    this.location = fields.location ?? null;
    this.region = fields.region ?? null;
    this.state = fields.state ?? null;
    this.address = fields.address ?? null;
    this.postcode = fields.postcode ?? null;
    this.latitude = fields.latitude ?? null;
    this.longitude = fields.longitude ?? null;
    this.telephone = fields.telephone ?? null;
    this.parking = fields.parking ?? null;
    this.openingHours = fields.openingHours ?? [];
    this.features = fields.features ?? [];
    this.experiences = fields.experiences ?? [];
    this.description = fields.description ?? null;
  }

  get url(): string {
    if (this.slug) return `${SITE}/cinema/${this.slug}/`;
    return `${SITE}${CINEMA_PAGE}`;
  }

  /**
   * The town part of the service's name, e.g. 'Kepong'.
   *
   * This is always available and is not the same field as `city`: the website
   * files some outlets under a state rather than a town, so `city` is whatever
   * GSC labels and may say "Sarawak" where `town` says "Bintulu".
   */
  get town(): string | null {
    const [head, sep] = splitOnFirst(this.name, ' - ');
    if (sep) return clean(head);
    return this.city ?? null;
  }

  toDict(): Record<string, unknown> {
    return {
      id: this.id,
      name: this.name,
      slug: this.slug,
      url: this.url,
      city: this.city,
      location: this.location,
      region: this.region,
      state: this.state,
      address: this.address,
      postcode: this.postcode,
      latitude: this.latitude,
      longitude: this.longitude,
      telephone: this.telephone,
      parking: this.parking,
      opening_hours: [...this.openingHours],
      features: [...this.features],
      experiences: [...this.experiences],
      description: this.description,
    };
  }
}

/**
 * One kind of ticket GSC sells for a session: Adult, OKU, Twin...
 *
 * `price` is the advertised ticket price, read from the service's `price`
 * field.
 *
 * The second money field, `tax`, is deliberately **not** used, for the same
 * reason TGV's `taxInCents` is not. It does not reconcile with any Malaysian
 * rate: it works out to exactly 20.00% of the ticket at one Selangor outlet and
 * about 9.09% at another, both in Selangor, where cinema admission carries a
 * flat 25% entertainment duty and movie tickets are exempt from service tax.
 * Adding it inflated every price and made identical tickets read differently
 * between outlets.
 */
export class TicketType {
  code: string;
  description: string;
  price: number | null;
  /** How many people this ticket admits. Twin seats two. */
  seats: number;
  package: boolean;
  /** Which seat category the price belongs to, matching the seat map. */
  areaCode: string | null;
  soldOut: boolean;
  childOnly: boolean;
  allocatedSeating: boolean;
  /**
   * A plain adult seat sold at face value. Everything else on a session is a
   * concession or a bundle, and `price` on a showtime ignores those.
   */
  standard: boolean;

  constructor(fields: {
    code: string;
    description: string;
    price?: number | null;
    seats?: number;
    package?: boolean;
    areaCode?: string | null;
    soldOut?: boolean;
    childOnly?: boolean;
    allocatedSeating?: boolean;
    standard?: boolean;
  }) {
    this.code = fields.code;
    this.description = fields.description;
    this.price = fields.price ?? null;
    this.seats = fields.seats ?? 1;
    this.package = fields.package ?? false;
    this.areaCode = fields.areaCode ?? null;
    this.soldOut = fields.soldOut ?? false;
    this.childOnly = fields.childOnly ?? false;
    this.allocatedSeating = fields.allocatedSeating ?? false;
    this.standard = fields.standard ?? false;
  }

  toDict(): Record<string, unknown> {
    return {
      code: this.code,
      description: this.description,
      price: this.price,
      seats: this.seats,
      package: this.package,
      area_code: this.areaCode,
      sold_out: this.soldOut,
      child_only: this.childOnly,
      allocated_seating: this.allocatedSeating,
      standard: this.standard,
    };
  }
}

/** One screening of one film in one hall. */
export class Showtime {
  sessionId: string;
  movieId: string;
  cinemaId: string;
  start: Date | null;
  hall: string | null;
  hallId: string | null;
  businessDate: string | null;
  experience: string | null;
  /**
   * The service's "CODE - Label" rendering of the format, e.g. '2D - Digital 2D'.
   * `experience` alone is only the code.
   */
  experienceDesc: string | null;
  seatTypes: string[];
  status: number | null;
  seatsTotal: number | null;
  seatsUsed: number | null;
  /** --prices only, one entry per ticket type on sale for this session. */
  ticketTypes: TicketType[];
  /** Filled in by the caller so `ends` works without a back reference. */
  runtime: number | null = null;
  /**
   * The service's own 12-hour rendering, e.g. '10:50AM'. Not in toDict; `time`
   * is the 24-hour form and is what the rest of the API speaks.
   */
  timeText: string | null;

  constructor(fields: {
    sessionId: string;
    movieId: string;
    cinemaId: string;
    start: Date | null;
    hall?: string | null;
    hallId?: string | null;
    businessDate?: string | null;
    experience?: string | null;
    experienceDesc?: string | null;
    seatTypes?: string[];
    status?: number | null;
    seatsTotal?: number | null;
    seatsUsed?: number | null;
    ticketTypes?: TicketType[];
    timeText?: string | null;
  }) {
    this.sessionId = fields.sessionId;
    this.movieId = fields.movieId;
    this.cinemaId = fields.cinemaId;
    this.start = fields.start;
    this.hall = fields.hall ?? null;
    this.hallId = fields.hallId ?? null;
    this.businessDate = fields.businessDate ?? null;
    this.experience = fields.experience ?? null;
    this.experienceDesc = fields.experienceDesc ?? null;
    this.seatTypes = fields.seatTypes ?? [];
    this.status = fields.status ?? null;
    this.seatsTotal = fields.seatsTotal ?? null;
    this.seatsUsed = fields.seatsUsed ?? null;
    this.ticketTypes = fields.ticketTypes ?? [];
    this.timeText = fields.timeText ?? null;
  }

  /** The format as a display name, e.g. 'Digital 2D' for '2D'. */
  get experienceName(): string | null {
    return experienceLabel(this.experienceDesc || this.experience);
  }

  get time(): string | null {
    if (!this.start) return null;
    const shifted = new Date(this.start.getTime() + MYT_OFFSET_MS);
    return `${String(shifted.getUTCHours()).padStart(2, '0')}:${String(
      shifted.getUTCMinutes(),
    ).padStart(2, '0')}`;
  }

  /** End time, if this Showtime knows the film's runtime. */
  get ends(): Date | null {
    if (!this.start || this.runtime === null) return null;
    return new Date(this.start.getTime() + this.runtime * 60_000);
  }

  get seatsLeft(): number | null {
    if (this.seatsTotal === null || this.seatsUsed === null) return null;
    return Math.max(0, this.seatsTotal - this.seatsUsed);
  }

  get soldOut(): boolean {
    const left = this.seatsLeft;
    return left !== null && left === 0;
  }

  /**
   * Cheapest standard ticket that admits exactly one person.
   *
   * This is the figure to compare across cinemas: it ignores Twin, which seats
   * two, and it ignores concession tickets.
   */
  get singleSeatPrice(): number | null {
    const prices = this.ticketTypes
      .filter((t) => t.price !== null && t.standard && t.seats === 1)
      .map((t) => t.price as number);
    return prices.length ? Math.min(...prices) : null;
  }

  /** True when the only standard tickets on sale seat more than one. */
  get isBundleOnly(): boolean {
    const standard = this.ticketTypes.filter((t) => t.standard);
    return standard.length > 0 && standard.every((t) => t.seats > 1);
  }

  /** Promotions the service has marked as no longer obtainable today. */
  get soldOutPromos(): string[] {
    return this.ticketTypes.filter((t) => t.soldOut).map((t) => t.description);
  }

  /**
   * Cheapest standard single-seat ticket price, in ringgit.
   *
   * A bundle covers two people, so quoting one here would compare a pair against
   * a single seat. Concessions are excluded too. Without --prices this is null.
   */
  get price(): number | null {
    const single = this.singleSeatPrice;
    if (single) return single;
    const standard = this.ticketTypes
      .filter((t) => t.price !== null && t.standard)
      .map((t) => t.price as number);
    return standard.length ? Math.min(...standard) : null;
  }

  /** Cheapest multi-seat bundle price, if the hall sells one. */
  get priceBundle(): number | null {
    const prices = this.ticketTypes
      .filter((t) => t.price !== null && t.standard && t.seats > 1)
      .map((t) => t.price as number);
    return prices.length ? Math.min(...prices) : null;
  }

  /** 'RM25.00 - RM35.00' when the hall sells more than one single price. */
  get priceRange(): string | null {
    const prices = uniq(
      this.ticketTypes
        .filter((t) => t.price !== null && t.standard && t.seats === 1)
        .map((t) => t.price as number),
    ).sort((a, b) => a - b);
    if (!prices.length) return null;
    if (prices.length === 1) return `RM${(prices[0] as number).toFixed(2)}`;
    return `RM${(prices[0] as number).toFixed(2)} - RM${(prices[prices.length - 1] as number).toFixed(2)}`;
  }

  /**
   * Cheapest standard price divided by how many people a ticket seats.
   *
   * Comparing the raw ticket price across cinemas can compare two people against
   * one, so compare this instead.
   */
  get perPerson(): number | null {
    const prices = this.ticketTypes
      .filter((t) => t.price !== null && t.standard && t.seats)
      .map((t) => (t.price as number) / t.seats);
    return prices.length ? Math.min(...prices) : null;
  }

  toDict(): Record<string, unknown> {
    return {
      session_id: this.sessionId,
      movie_id: this.movieId,
      cinema_id: this.cinemaId,
      hall_id: this.hallId,
      date: this.businessDate,
      start: isoMyt(this.start),
      time: this.time,
      ends: isoMyt(this.ends),
      hall: this.hall,
      experience: this.experience,
      experience_name: this.experienceName,
      seat_types: [...this.seatTypes],
      seats_total: this.seatsTotal,
      seats_used: this.seatsUsed,
      seats_left: this.seatsLeft,
      sold_out: this.soldOut,
      status: this.status,
      price: this.price,
      price_range: this.priceRange,
      price_bundle: this.priceBundle,
      per_person: this.perPerson,
      sold_out_promos: [...this.soldOutPromos],
      ticket_types: this.ticketTypes.map((t) => t.toDict()),
    };
  }
}

/**
 * A film.
 *
 * GSC files a film under a parent record and then one child per bookable
 * variant, e.g. "Digger" and "(IMAX) Digger" are separate children of one
 * parent. `id` is the parent, which is the film; each showtime carries the child
 * it belongs to in `Showtime.movieId`, which is what pricing is quoted against.
 */
export class Movie {
  id: string;
  title: string;
  slug: string | null;
  genre: string[];
  tags: string[];
  rating: string | null;
  runtime: number | null;
  language: string | null;
  subtitles: string[];
  synopsis: string | null;
  director: string | null;
  cast: string[];
  distributor: string | null;
  releaseDate: string | null;
  trailerUrl: string | null;
  posterUrl: string | null;
  bannerUrl: string | null;
  experiences: string[];
  sessionIds: string[];
  cinemaId: string | null;
  cinemaName: string | null;
  showtimes: Showtime[];
  /** The bookable variant codes, one per format (2D, IMAX, 4DX...). */
  variants: string[] = [];
  /** The service's parent and child codes, spelled out. */
  parentId: string | null = null;
  /** The runtime as the website writes it, e.g. '1 hr 45 mins'. */
  duration: string | null = null;
  bookingUrl: string | null = null;
  bookingId: number | null = null;
  /** Set by the website's detail page only. */
  stillUrl: string | null = null;
  /** Carries `dates`, the only thing in here that is not a field. */
  extra: Record<string, unknown> = {};

  constructor(fields: Partial<Movie> & { id?: string; title?: string } = {}) {
    this.id = fields.id ?? '';
    this.title = fields.title ?? '';
    this.slug = fields.slug ?? null;
    this.genre = fields.genre ?? [];
    this.tags = fields.tags ?? [];
    this.rating = fields.rating ?? null;
    this.runtime = fields.runtime ?? null;
    this.language = fields.language ?? null;
    this.subtitles = fields.subtitles ?? [];
    this.synopsis = fields.synopsis ?? null;
    this.director = fields.director ?? null;
    this.cast = fields.cast ?? [];
    this.distributor = fields.distributor ?? null;
    this.releaseDate = fields.releaseDate ?? null;
    this.trailerUrl = fields.trailerUrl ?? null;
    this.posterUrl = fields.posterUrl ?? null;
    this.bannerUrl = fields.bannerUrl ?? null;
    this.experiences = fields.experiences ?? [];
    this.sessionIds = fields.sessionIds ?? [];
    this.cinemaId = fields.cinemaId ?? null;
    this.cinemaName = fields.cinemaName ?? null;
    this.showtimes = fields.showtimes ?? [];
    this.variants = fields.variants ?? [];
    this.parentId = fields.parentId ?? null;
    this.duration = fields.duration ?? null;
    this.bookingUrl = fields.bookingUrl ?? null;
    this.bookingId = fields.bookingId ?? null;
    this.stillUrl = fields.stillUrl ?? null;
    this.extra = fields.extra ?? {};
  }

  get url(): string {
    return this.slug ? `${SITE}/movie/${this.slug}/` : SITE;
  }

  /** Audio codes as a list. GSC joins several with commas. */
  get languages(): string[] {
    return splitCodes(this.language);
  }

  /** YouTube id from whatever trailer URL shape was stored. */
  get trailerId(): string | null {
    if (!this.trailerUrl) return null;
    const match = /(?:embed\/|v=|youtu\.be\/|\/shorts\/)([\w-]{6,})/.exec(this.trailerUrl);
    return match ? (match[1] as string) : null;
  }

  get times(): string[] {
    return this.showtimes.map((s) => s.time).filter((t): t is string => Boolean(t));
  }

  /**
   * Every date this film has sessions on.
   *
   * Only the catalogue knows these: one film carries its whole run on the same
   * payload, so this costs nothing extra. `businessDates()` is the union across
   * every film.
   */
  get dates(): string[] {
    const found = this.extra.dates;
    return Array.isArray(found) ? (found as string[]) : [];
  }

  /** Alias of `cast`, the name the website's detail page used. */
  get mainStars(): string[] {
    return this.cast;
  }

  toDict(): Record<string, unknown> {
    return {
      id: this.id,
      title: this.title,
      slug: this.slug,
      url: this.url,
      genre: [...this.genre],
      tags: [...this.tags],
      rating: this.rating,
      runtime: this.runtime,
      duration: this.duration,
      language: this.language,
      languages: [...this.languages],
      subtitles: [...this.subtitles],
      synopsis: this.synopsis,
      director: this.director,
      cast: [...this.cast],
      distributor: this.distributor,
      release_date: this.releaseDate,
      trailer_url: this.trailerUrl,
      trailer_id: this.trailerId,
      poster_url: this.posterUrl,
      banner_url: this.bannerUrl,
      experiences: [...this.experiences],
      session_ids: [...this.sessionIds],
      cinema_id: this.cinemaId,
      cinema_name: this.cinemaName,
      booking_url: this.bookingUrl,
      booking_id: this.bookingId,
      showtimes: this.showtimes.map((s) => s.toDict()),
    };
  }
}

/**
 * One seat in a hall.
 *
 * `row` and `column` are the service's own grid coordinates, which are absolute
 * across the hall: an aisle is simply a column with no seat in it. `number` is
 * the printed label the customer reads, and `id` is the booking identifier the
 * service issues.
 */
export class Seat {
  id: string;
  row: number;
  column: number;
  number: string | null;
  status: string;
  category: number | null;
  seatType: string | null;
  priority: number;

  constructor(fields: {
    id: string;
    row: number;
    column: number;
    number?: string | null;
    status?: string;
    category?: number | null;
    seatType?: string | null;
    priority?: number;
  }) {
    this.id = fields.id;
    this.row = fields.row;
    this.column = fields.column;
    this.number = fields.number ?? null;
    this.status = fields.status ?? SEAT_FREE;
    this.category = fields.category ?? null;
    this.seatType = fields.seatType ?? null;
    this.priority = fields.priority ?? 0;
  }

  /** Normalised state: 'A' free, 'B' booked, 'D' blocked, '?' unknown. */
  get state(): string {
    return this.status in SEAT_MARKS ? this.status : SEAT_UNKNOWN;
  }

  get free(): boolean {
    return this.state === SEAT_FREE;
  }

  get booked(): boolean {
    return this.state === SEAT_BOOKED;
  }

  toDict(): Record<string, unknown> {
    return {
      id: this.id,
      number: this.number,
      row: this.row,
      column: this.column,
      status: this.status,
      state: this.state,
      category: this.category,
      seat_type: this.seatType,
      free: this.free,
      booked: this.booked,
    };
  }
}

/** One row of a hall area. `index` is the service's grid row. */
export class SeatRow {
  index: number;
  name: string | null;
  seats: Seat[];

  constructor(fields: { index: number; name?: string | null; seats?: Seat[] }) {
    this.index = fields.index;
    this.name = fields.name ?? null;
    this.seats = fields.seats ?? [];
  }

  get columns(): number[] {
    return this.seats.map((s) => s.column).sort((a, b) => a - b);
  }

  get freeSeats(): Seat[] {
    return this.seats.filter((s) => s.free);
  }

  toDict(): Record<string, unknown> {
    return {
      index: this.index,
      name: this.name,
      seats: this.seats.map((s) => s.toDict()),
    };
  }
}

/**
 * One part of a hall, e.g. a section.
 *
 * GSC divides a hall into sections rather than priced areas, but the shape is the
 * same as TGV's: a group of rows that can be drawn against one ruler.
 */
export class SeatArea {
  number: number;
  code: string | null;
  description: string | null;
  seatCount: number;
  rowCount: number;
  columnCount: number;
  rows: SeatRow[];

  constructor(fields: {
    number: number;
    code?: string | null;
    description?: string | null;
    seatCount?: number;
    rowCount?: number;
    columnCount?: number;
    rows?: SeatRow[];
  }) {
    this.number = fields.number;
    this.code = fields.code ?? null;
    this.description = fields.description ?? null;
    this.seatCount = fields.seatCount ?? 0;
    this.rowCount = fields.rowCount ?? 0;
    this.columnCount = fields.columnCount ?? 0;
    this.rows = fields.rows ?? [];
  }

  get label(): string {
    return this.description || this.code || `section ${this.number}`;
  }

  /** Rows that actually hold seats, in service order. */
  get occupiedRows(): SeatRow[] {
    return this.rows.filter((row) => row.seats.length > 0);
  }

  get seats(): Seat[] {
    return this.rows.flatMap((row) => row.seats);
  }

  get booked(): Seat[] {
    return this.seats.filter((s) => s.booked);
  }

  get free(): Seat[] {
    return this.seats.filter((s) => s.free);
  }

  toDict(): Record<string, unknown> {
    return {
      number: this.number,
      code: this.code,
      description: this.description,
      seat_count: this.seatCount,
      row_count: this.rowCount,
      column_count: this.columnCount,
      rows: this.rows.map((r) => r.toDict()),
    };
  }
}

/**
 * The whole hall for one session, with the screen's position.
 *
 * The counts are the service's own availability figure, taken from the hall
 * booking block rather than from counting seat states. That block is the number
 * a customer-facing "seats left" would show, and counting the grid does not
 * reproduce it: on the captured hall the states explain 6 of the 7 seats the
 * service holds back. `blocked` is the count of seats the grid does mark as
 * unavailable, and `reportedTotal`/`reportedBooked` keep the hall element's own
 * numbers, which mean something else entirely and are kept only so the
 * discrepancy stays visible.
 */
export class SeatPlan {
  cinemaId: string | null = null;
  sessionId: string | null = null;
  hallId: string | null = null;
  hallName: string | null = null;
  areas: SeatArea[] = [];
  boundaryLeft = 0;
  boundaryRight = 0;
  screenStart = 0;
  screenWidth = 0;
  seatsTotal: number | null = null;
  seatsBooked: number | null = null;
  seatsBlocked: number | null = null;
  /** Seat category name -> seats still available, from the hall booking block. */
  available: Record<string, number> = {};
  /**
   * Seat category id -> {name, seatsTaken}, i.e. how many seats one ticket of
   * that category occupies.
   */
  categories: Record<string, { name: string | null; seats_taken: number; seat_type: string | null }> = {};
  reportedTotal: number | null = null;
  reportedBooked: number | null = null;
  /** The hall's formats, one line each, when the service describes them. */
  experiences: string[] = [];

  constructor(fields: Partial<SeatPlan> = {}) {
    Object.assign(this, fields);
  }

  /** Leftmost column any area uses, so one ruler fits every grid. */
  get firstColumn(): number {
    const columns = this.areas.flatMap((a) => a.seats).map((s) => s.column);
    return columns.length ? Math.min(...columns) : 0;
  }

  get lastColumn(): number {
    const columns = this.areas.flatMap((a) => a.seats).map((s) => s.column);
    return columns.length ? Math.max(...columns) : 0;
  }

  get seats(): Seat[] {
    return this.areas.flatMap((a) => a.seats);
  }

  get booked(): Seat[] {
    return this.areas.flatMap((a) => a.booked);
  }

  get free(): Seat[] {
    return this.areas.flatMap((a) => a.free);
  }

  /** Seats the service will still sell for this session. */
  get seatsLeft(): number | null {
    const values = Object.values(this.available);
    if (!values.length) return null;
    return values.reduce((a, b) => a + b, 0);
  }

  /**
   * Every row of the hall, screen end first, paired with its section.
   *
   * GSC numbers rows from the back of the hall towards the screen: the indices
   * ascend while the row letters descend, ending on `A` at the highest index.
   * Descending index therefore puts the screen end first. Ordering by the raw
   * index draws the hall upside down.
   */
  get hallRows(): Array<[SeatArea, SeatRow]> {
    const rows: Array<[SeatArea, SeatRow]> = [];
    for (const area of this.areas) {
      for (const row of area.occupiedRows) rows.push([area, row]);
    }
    return rows.sort((a, b) => b[1].index - a[1].index);
  }

  toDict(): Record<string, unknown> {
    return {
      cinema_id: this.cinemaId,
      session_id: this.sessionId,
      hall_id: this.hallId,
      hall_name: this.hallName,
      seats_total: this.seatsTotal,
      seats_left: this.seatsLeft,
      seats_booked: this.seatsBooked,
      seats_blocked: this.seatsBlocked,
      available: { ...this.available },
      areas: this.areas.map((a) => a.toDict()),
    };
  }
}

/**
 * A seat's printed number.
 *
 * The other scraper needs this to realign a hall whose rows are numbered
 * independently; GSC's coordinates are already absolute so nothing here calls
 * it, but both modules expose it so a caller can ask either the same question.
 */
export function seatNumber(seat: Seat): string | null {
  if (seat.number !== null) return seat.number;
  const text = String(seat.id).replace(/\D/g, '');
  return text || null;
}

// ---------------------------------------------------------------------------
// parsers: every one is a pure function over a saved body
// ---------------------------------------------------------------------------

/**
 * The outlet directory payload -> Cinema records, sorted by name.
 *
 * `directory` is the optional list parsed from the website's outlet page. It is
 * joined on name to attach `slug`, `city` and `features`; see `joinDirectories`.
 */
export function parseCinemas(payload: unknown, directory?: Cinema[] | null): Cinema[] {
  const asRecord = payload as Record<string, unknown> | null;
  const body = asRecord?.Response as Record<string, unknown> | undefined;
  const records =
    Array.isArray(payload)
      ? (payload as Record<string, unknown>[])
      : ((body?.Body as Record<string, unknown> | undefined)?.Location as
          | Record<string, unknown>[]
          | undefined) ?? [];

  const found: Cinema[] = [];
  const seen = new Set<string>();
  for (const record of records) {
    if (!record || typeof record !== 'object') continue;
    const r = record as Record<string, unknown>;
    const cid = clean(r.OutletId as string | null);
    const name = clean(r.OutletName as string | null);
    // The service sends a "Please Select Cinema" placeholder row twice, once per
    // region, which is not an outlet.
    if (
      !cid ||
      !name ||
      seen.has(cid) ||
      name.toLowerCase().startsWith('please select')
    ) {
      continue;
    }
    seen.add(cid);
    const region = clean(r.Region as string | null);
    const address = clean(r.Address as string | null);
    const state = clean(r.StateDesc as string | null);
    let postcode: string | null = null;
    if (address) {
      const at = address.lastIndexOf(',');
      if (at >= 0 && /^\d{5}$/.test(address.slice(at + 1).trim())) {
        postcode = address.slice(at + 1).trim();
      }
    }
    found.push(
      new Cinema({
        id: cid,
        name,
        location: region ? region.toLowerCase() : null,
        region,
        state,
        address,
        postcode,
      }),
    );
  }
  found.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  if (directory && directory.length) joinDirectories(found, directory);
  return found;
}

/**
 * Words that identify an outlet, for joining the two directories.
 *
 * Glued words are split first: the service writes `TropicanaGardens` where the
 * website writes `Tropicana Gardens`, and `MyTOWN` where the website writes
 * `MyTown`, so both sides are cut the same way before comparing. The brand token
 * is dropped, since the website prefixes every name with it and the service
 * prefixes none.
 */
function nameTokens(value: string, expand = false): Set<string> {
  const text = String(value).replace(/`/g, "'").replace(/’/g, "'");
  const spaced = text.replace(/(?<=[a-z0-9])(?=[A-Z])/g, ' ');
  // Casefold before matching, not per match. That is what lets the noise list
  // work at all: `Mall` has to become `mall` before it is recognised as noise,
  // and matching the mixed-case string instead yields `all`, which is a word
  // that identifies nothing and never matches the other side.
  const words = [...spaced.toLowerCase().matchAll(/[a-z0-9]+/g)]
    .map((m) => m[0] as string)
    .filter((w) => !NAME_NOISE.has(w) && w.length > 1)
    .filter((w) => !BRAND_NOISE.has(w));
  return new Set(expand ? words.flatMap((w) => TOWN_ABBREVIATIONS[w] ?? [w]) : words);
}

/**
 * Split 'Johor Bahru - AEON Dato Onn' into (town, site) candidates.
 *
 * The service is inconsistent about spacing around the dash, so both splits are
 * tried and the caller takes whichever matches better.
 */
function nameParts(value: string): Array<[string, string]> {
  const parts: Array<[string, string]> = [];
  const loose = /^(.*?)\s+-\s+(.+)$/.exec(value);
  if (loose) parts.push([loose[1] as string, loose[2] as string]);
  const tight = /^(.*?)\s*-\s*(.+)$/.exec(value);
  if (tight) {
    const pair: [string, string] = [tight[1] as string, tight[2] as string];
    if (!parts.some(([a, b]) => a === pair[0] && b === pair[1])) parts.push(pair);
  }
  return parts.length ? parts : [['', value]];
}

/**
 * How well a website outlet name and a service outlet name agree.
 *
 * 1.0 means one name is a subset of the other's tokens, plus a bonus when the
 * town agrees. The bonus is what separates `GSC Spring Bintulu` from `GSC The
 * Spring, Kuching`, which are otherwise near-identical.
 */
function nameScore(siteName: string, serviceName: string): number {
  const site = nameTokens(siteName, true);
  if (!site.size) return 0;
  let best = 0;
  for (const [town, place] of nameParts(serviceName)) {
    const tokens = nameTokens(place);
    if (!tokens.size) continue;
    let shared = 0;
    for (const word of site) if (tokens.has(word)) shared += 1;
    let score = (2 * shared) / (site.size + tokens.size);
    const siteSubset = [...site].every((w) => tokens.has(w));
    const tokensSubset = [...tokens].every((w) => site.has(w));
    if (siteSubset || tokensSubset) score += 0.5;
    const townTokens = nameTokens(town, true);
    for (const word of townTokens) {
      if (site.has(word)) {
        score += 0.5;
        break;
      }
    }
    if (score > best) best = score;
  }
  return best;
}

/**
 * Below this, two names are not the same outlet. Every pair the two directories
 * actually share scores at least 0.7, and the weakest real pair scores 0.9.
 */
export const JOIN_THRESHOLD = 0.7;

/**
 * Attach slug, city and features from the website to service outlets.
 *
 * The two directories share no id, so they are matched on name. Candidates are
 * taken strongest-first and each side is used once, which is what keeps the
 * near-identical names apart: `GSC IOI Mall Kulai` and `GSC IOI Mall Puchong`
 * both share tokens with the other, and only the town token tells them from each
 * other.
 *
 * Mutates `outlets` and returns how many were joined. Unmatched outlets keep
 * their service values and leave slug, city and features as null.
 */
export function joinDirectories(
  outlets: Cinema[],
  directory: Cinema[],
  threshold = JOIN_THRESHOLD,
): number {
  const candidates: Array<[number, number, number]> = [];
  directory.forEach((site, si) => {
    outlets.forEach((service, oi) => {
      candidates.push([nameScore(site.name, service.name), si, oi]);
    });
  });
  // Strongest first. Ties keep the enumeration order above, which is site-major:
  // Array.prototype.sort is stable, as Python's sorted is, so two names that score
  // alike are decided by which the website lists first. That is what separates
  // `GSC Paradigm JB` from `GSC Paradigm Mall`, which both score 1.5 against
  // `Johor Bahru - Paradigm Mall`.
  candidates.sort((a, b) => b[0] - a[0]);

  const takenSites = new Set<number>();
  const takenOutlets = new Set<number>();
  let joined = 0;
  for (const [score, si, oi] of candidates) {
    if (score < threshold) break;
    if (takenSites.has(si) || takenOutlets.has(oi)) continue;
    takenSites.add(si);
    takenOutlets.add(oi);
    const site = directory[si] as Cinema;
    const service = outlets[oi] as Cinema;
    service.slug = site.slug;
    service.city = site.city || service.town;
    service.features = [...site.features];
    joined += 1;
  }
  return joined;
}

/**
 * The catalogue payload -> one Movie per film.
 *
 * One `<parent>` is a film and its `<child>` records are the bookable format
 * variants of it, e.g. "Digger" and "(IMAX) Digger". The parent becomes the
 * Movie; the children become its `variants` and its `experiences`.
 */
export function parseMovies(xml: string): Movie[] {
  const root = xmlOf(xml, 'movies');
  const movies: Movie[] = [];
  for (const parent of root.findAll('parent')) {
    const code = clean(parent.attr('code'));
    const title = clean(parent.attr('title'));
    if (!code || !title) continue;
    const children = parent.findAll('child');
    const first = children[0] ?? parent;
    const trailer = youtubeOnly(clean(first.attr('trailer_url')));
    const variants = children.map((c) => clean(c.attr('code'))).filter((c): c is string => Boolean(c));
    const experiences = uniq(
      children.map((c) => clean(c.attr('film_type'))).filter((t): t is string => Boolean(t)),
    );
    const dates = children.flatMap((child) =>
      child.findAll('show').map((show) => clean(show.attr('opsdate'))),
    );
    movies.push(
      new Movie({
        id: code,
        title,
        slug: slugify(title),
        genre: asList(first.attr('genre')),
        rating: clean(first.attr('rating')),
        runtime: durationMinutes(first.attr('duration')),
        language: clean(first.attr('lang')),
        trailerUrl: trailer,
        posterUrl: clean(first.attr('thumb_big')) || clean(first.attr('thumb_medium')),
        bannerUrl: clean(first.attr('stills')),
        experiences,
        variants,
        parentId: code,
        extra: { dates: uniq(dates.filter((d): d is string => Boolean(d))) },
      }),
    );
  }
  return movies;
}

/** A trailer URL only counts if it is actually a YouTube link. */
function youtubeOnly(value: string | null): string | null {
  if (!value) return null;
  return value.includes('youtube') || value.includes('youtu.be') ? value : null;
}

/**
 * One `<show>` element -> a Showtime.
 *
 * The show element does not name the film, so the bookable variant code is passed
 * in from the `<child>` that encloses it. That code is what pricing is quoted
 * against.
 */
function showtimeFrom(
  element: XmlElement,
  cinemaId: string,
  cinemaName: string | null,
  businessDate: string | null,
  filmId: string | null = null,
): Showtime {
  return new Showtime({
    sessionId: clean(element.attr('id')) ?? '',
    movieId: filmId || clean(element.attr('movieid')) || '',
    cinemaId,
    start: sessionStart(element.attr('date'), element.attr('time')),
    hall: clean(element.attr('hname')),
    hallId: clean(element.attr('hid')),
    businessDate,
    experience: clean(element.attr('type')),
    experienceDesc: clean(element.attr('type_desc')),
    seatTypes: asList(element.attr('seattypes')),
    timeText: clean(element.attr('timestr')),
  });
}

/** Sort key: unknown starts last, then by instant. */
function byStart(a: Showtime, b: Showtime): number {
  if (!a.start && !b.start) return 0;
  if (!a.start) return 1;
  if (!b.start) return -1;
  return a.start.getTime() - b.start.getTime();
}

/**
 * The one-cinema, one-date payload -> movies carrying their sessions.
 *
 * Films arrive under `<oprn>`, which carries the business date, then a `<parent>`
 * per film and a `<child>` per bookable variant with its `<show>` elements.
 * Children of one parent are merged into one Movie, because a film playing in 2D
 * and in IMAX at the same cinema is still one film.
 */
export function parseDay(xml: string, cinemaId: string, cinemaName: string | null = null): Movie[] {
  const root = xmlOf(xml, 'day');
  let businessDate: string | null = null;
  const movies: Movie[] = [];
  const byParent = new Map<string, Movie>();

  for (const oprn of root.findAll('oprn')) {
    businessDate = clean(oprn.attr('date')) || businessDate;
    for (const parent of oprn.findAll('parent')) {
      const code = clean(parent.attr('code'));
      if (!code) continue;
      let movie = byParent.get(code) ?? null;
      for (const child of parent.findAll('child')) {
        const childCode = clean(child.attr('code'));
        const shows = child.findAll('show');
        const showtimes = shows
          .map((show) => showtimeFrom(show, cinemaId, cinemaName, businessDate, childCode))
          .sort(byStart);
        if (movie === null) {
          const parentTitle = clean(parent.attr('title')) || code;
          movie = new Movie({
            id: code,
            title: parentTitle,
            slug: slugify(parentTitle),
            genre: asList(child.attr('genre')),
            rating: clean(child.attr('rating')),
            runtime: durationMinutes(child.attr('duration')),
            language: clean(child.attr('lang')),
            trailerUrl: youtubeOnly(clean(child.attr('trailer_url'))),
            posterUrl: clean(child.attr('thumb_big')),
            parentId: code,
            cinemaId,
            cinemaName,
          });
          byParent.set(code, movie);
          movies.push(movie);
        } else if (movie.runtime === null) {
          movie.runtime = durationMinutes(child.attr('duration'));
        }
        movie.variants = uniq([...movie.variants, ...(childCode ? [childCode] : [])]);
        // The day payload carries no format on the child, but each show names the
        // hall format it is in, which is the same thing.
        movie.experiences = uniq([
          ...movie.experiences,
          ...shows.map((show) => clean(show.attr('type'))).filter((t): t is string => Boolean(t)),
        ]);
        movie.showtimes = [...movie.showtimes, ...showtimes].sort(byStart);
      }
    }
  }

  for (const movie of movies) {
    movie.cinemaId = cinemaId;
    if (movie.cinemaName === null) movie.cinemaName = cinemaName;
    for (const showtime of movie.showtimes) showtime.runtime = movie.runtime;
    movie.sessionIds = movie.showtimes.map((s) => s.sessionId).filter(Boolean);
  }
  movies.sort((a, b) => a.title.toLowerCase().localeCompare(b.title.toLowerCase()));
  return movies;
}

/**
 * The one-film payload -> one Movie per cinema showing it.
 *
 * The service answers for every cinema at once, so a national lookup of one film
 * is a single request rather than one per outlet. The result is the same shape
 * `day()` produces, which is what `movie()` returns.
 *
 * `on` is the business date that was asked for. The payload does not echo it back,
 * and it cannot be recovered from the shows: a 00:15 screening is filed under one
 * business date and timestamped on the next calendar day.
 */
export function parseMovieShows(
  xml: string,
  parentId: string | null = null,
  on: string | null = null,
): Movie[] {
  const root = xmlOf(xml, 'movie_shows');
  const parentCode = clean(root.attr('parent_code'));
  const parentTitle = clean(root.attr('parent_title'));
  const found: Movie[] = [];

  for (const location of root.findAll('location')) {
    const cid = clean(location.attr('id'));
    const name = clean(location.attr('name'));
    if (!cid) continue;
    const children = location.findAll('child');
    if (!children.length) continue;

    // The payload drops the <parent> level, so it is put back for parseDay.
    // Python builds an ET tree and re-serialises it; this feeds parseDayNodes
    // the same nodes directly, which is the same work without the round trip.
    const oprn = new XmlElement('oprn', on ? { date: on } : {});
    const parent = new XmlElement(
      'parent',
      parentCode || parentTitle
        ? {
            ...(parentCode ? { code: parentCode } : {}),
            ...(parentTitle ? { title: parentTitle } : {}),
          }
        : {},
    );
    for (const child of children) parent.append(child);
    oprn.append(parent);
    found.push(...parseDayNodes([oprn], cid, name));
  }
  return found;
}

/** parseDay's body, over `<oprn>` elements already in hand. */
function parseDayNodes(
  oprns: XmlElement[],
  cinemaId: string,
  cinemaName: string | null,
): Movie[] {
  let businessDate: string | null = null;
  const movies: Movie[] = [];
  const byParent = new Map<string, Movie>();

  for (const oprn of oprns) {
    businessDate = clean(oprn.attr('date')) || businessDate;
    for (const parent of oprn.findAll('parent')) {
      const code = clean(parent.attr('code'));
      if (!code) continue;
      let movie = byParent.get(code) ?? null;
      for (const child of parent.findAll('child')) {
        const childCode = clean(child.attr('code'));
        const shows = child.findAll('show');
        const showtimes = shows
          .map((show) => showtimeFrom(show, cinemaId, cinemaName, businessDate, childCode))
          .sort(byStart);
        if (movie === null) {
          const parentTitle = clean(parent.attr('title')) || code;
          movie = new Movie({
            id: code,
            title: parentTitle,
            slug: slugify(parentTitle),
            genre: asList(child.attr('genre')),
            rating: clean(child.attr('rating')),
            runtime: durationMinutes(child.attr('duration')),
            language: clean(child.attr('lang')),
            trailerUrl: youtubeOnly(clean(child.attr('trailer_url'))),
            posterUrl: clean(child.attr('thumb_big')),
            parentId: code,
            cinemaId,
            cinemaName,
          });
          byParent.set(code, movie);
          movies.push(movie);
        } else if (movie.runtime === null) {
          movie.runtime = durationMinutes(child.attr('duration'));
        }
        movie.variants = uniq([...movie.variants, ...(childCode ? [childCode] : [])]);
        movie.experiences = uniq([
          ...movie.experiences,
          ...shows.map((show) => clean(show.attr('type'))).filter((t): t is string => Boolean(t)),
        ]);
        movie.showtimes = [...movie.showtimes, ...showtimes].sort(byStart);
      }
    }
  }

  for (const movie of movies) {
    movie.cinemaId = cinemaId;
    if (movie.cinemaName === null) movie.cinemaName = cinemaName;
    for (const showtime of movie.showtimes) showtime.runtime = movie.runtime;
    movie.sessionIds = movie.showtimes.map((s) => s.sessionId).filter(Boolean);
  }
  movies.sort((a, b) => a.title.toLowerCase().localeCompare(b.title.toLowerCase()));
  return movies;
}

/**
 * The hall payload -> a SeatPlan.
 *
 * The grid is complete: every row is present and every seat carries its own
 * absolute column, so an aisle is simply a column with no seat in it and the
 * layout can be drawn as it arrives, with no alignment pass needed.
 *
 * Three separate notions of "how many seats" arrive in one payload and they do not
 * agree, so they are kept apart:
 *
 * - the grid itself, which is the physical seat count,
 * - the seat states, `A` free, `B` booked, `D` blocked,
 * - the hall booking block, which is what the service still sells.
 *
 * The booking block is what `seatsLeft` reports, because it is the figure the
 * site itself shows, and counting the states does not reproduce it: on the
 * captured hall they account for 6 of the 7 seats the service holds back.
 */
export function parseSeatPlan(
  xml: string,
  cinemaId: string | null = null,
  sessionId: string | null = null,
): SeatPlan {
  const root = xmlOf(xml, 'seat_plan');
  const areas: SeatArea[] = [];

  for (const section of root.findAll('section')) {
    const rows: SeatRow[] = [];
    for (const row of section.findAll('row')) {
      const seats = row
        .findAll('col')
        .filter((col) => col.attr('id') !== null)
        .map(
          (col) =>
            new Seat({
              id: clean(col.attr('id')) ?? '',
              row: intOr(col.attr('y') ?? row.attr('y'), 0),
              column: intOr(col.attr('x'), 0),
              number: clean(col.attr('value')),
              status: clean(col.attr('status')) ?? SEAT_FREE,
              category: intOrNullable(col.attr('seatcategory')),
              seatType: clean(col.attr('type')),
              priority: intOr(col.attr('priority'), 0),
            }),
        );
      rows.push(
        new SeatRow({
          index: intOr(row.attr('y'), 0),
          name: clean(row.attr('value')),
          seats,
        }),
      );
    }
    rows.sort((a, b) => a.index - b.index);
    const columns = new Set(rows.flatMap((r) => r.seats.map((s) => s.column)));
    areas.push(
      new SeatArea({
        number: intOr(section.attr('value'), 0),
        code: clean(section.attr('value')),
        seatCount: rows.reduce((n, r) => n + r.seats.length, 0),
        rowCount: rows.length,
        columnCount: columns.size,
        rows,
      }),
    );
  }

  const available: Record<string, number> = {};
  for (const tkt of root.findAll('hallbooking').flatMap((h) => h.findAll('tkt'))) {
    available[clean(tkt.attr('name')) ?? SEAT_FREE] = intOr(tkt.attr('qty'), 0);
  }

  const categories: SeatPlan['categories'] = {};
  for (const item of root.findAll('mappings').flatMap((m) => m.findAll('category'))) {
    const id = clean(item.attr('id'));
    if (!id) continue;
    const seat = item.findAll('seat')[0];
    categories[id] = {
      name: clean(item.attr('name')),
      seats_taken: intOr(item.attr('seats_taken'), 1),
      seat_type: clean(seat ? seat.attr('type') : null),
    };
  }

  const experiences = root
    .findAll('experience')
    .flatMap((e) => e.findAll('exp_img'))
    .filter((exp) => clean(exp.attr('name')))
    .map((exp) => `${clean(exp.attr('name'))}: ${clean(exp.attr('description'))}`.replace(/[: ]+$/, ''));

  const plan = new SeatPlan({
    cinemaId,
    sessionId,
    hallId: clean(root.attr('no')),
    areas,
    available,
    categories,
    reportedTotal: intOrNullable(root.attr('maximumseats')),
    reportedBooked: intOr(root.attr('hbooked'), 0),
    experiences,
  });
  plan.seatsTotal = plan.seats.length || null;
  plan.seatsBlocked = plan.seats.filter((s) => s.state === SEAT_BLOCKED).length || null;
  const left = plan.seatsLeft;
  if (plan.seatsTotal !== null) {
    plan.seatsBooked = Math.max(0, plan.seatsTotal - (left !== null ? left : 0));
  }
  return plan;
}

/** int(), or the fallback. */
function intOr(value: string | null | undefined, fallback: number): number {
  const n = Number.parseInt(String(value ?? ''), 10);
  return Number.isNaN(n) ? fallback : n;
}

/** int(), or null when the value is not numeric at all. */
function intOrNullable(value: string | null | undefined): number | null {
  const text = String(value ?? '');
  return /^-?\d+$/.test(text) ? Number.parseInt(text, 10) : null;
}

/** The counts a hall reports, the shape `seatStatus` returns. */
export interface SeatCounts {
  seats_total: number | null;
  seats_used: number | null;
  seats_left: number | null;
  seats_blocked?: number | null;
  used_percentage: number | null;
}

/** Fold a SeatPlan's own figures into the flat shape seatStatus returns. */
export function countsFromPlan(plan: SeatPlan): SeatCounts {
  const total = plan.seatsTotal;
  const left = plan.seatsLeft;
  let used = plan.seatsBooked;
  if (left === null && used === null) used = plan.booked.length || null;
  return {
    seats_total: total,
    seats_used: used,
    seats_left:
      left !== null
        ? left
        : total !== null && used !== null
          ? Math.max(0, total - used)
          : null,
    seats_blocked: plan.seatsBlocked,
    used_percentage:
      total && used !== null ? Math.round((used / total) * 100 * 10) / 10 : null,
  };
}

/**
 * The pricing payload -> the ticket types on sale for one session.
 *
 * `price` is the advertised price and `tax` is deliberately not read, for the
 * reason given on `TicketType`. The food-and-bundle block the service attaches to
 * the same response is ignored here: it is concessions, not admission.
 */
export function parseTickets(xml: string): TicketType[] {
  const root = xmlOf(xml, 'tickets');
  const found: TicketType[] = [];
  for (const ticket of root
    .findAll('ticketdata')
    .flatMap((t) => t.findAll('ticket'))) {
    const raw = ticket.attr('price');
    const amount = raw !== null && raw !== '' && Number.isFinite(Number(raw)) ? Number(raw) : null;
    const code = clean(ticket.attr('id')) ?? '';
    const seatsTaken = ticket.attr('seats_taken');
    const seats = intOr(seatsTaken, 1);
    found.push(
      new TicketType({
        code,
        description: clean(ticket.attr('type')) || code,
        price: amount,
        seats,
        package: seatsTaken !== null && seats > 1,
        areaCode: clean(ticket.attr('seatcategory')),
        childOnly: code.toLowerCase() === 'children',
        standard: STANDARD_TICKETS.has(code.toUpperCase()),
      }),
    );
  }
  return found;
}

/** Pull every movie card out of a website listing page. */
export function parseListing(html: string): Movie[] {
  const cards = html.split('<div class="movie-poster-card"').slice(1);
  const movies: Movie[] = [];
  const seen = new Set<string>();

  for (const card of cards) {
    const slug = slugOf(textOf(/<a href="(\/movie\/[^"]+)"[^>]*class="card-link"/i, card));
    const title = textOf(/class="title-text"[^>]*>(.*?)<\/span>/is, card);
    if (!slug || !title || seen.has(slug)) continue;
    seen.add(slug);

    // Each chip carries an icon that says what it means.
    const chips: Record<string, string> = {};
    const icons: Record<string, string> = {
      'i-tag.svg': 'genre',
      'i-clock.svg': 'duration',
      'i-chat.svg': 'language',
      'i-keyboard.svg': 'subtitles',
      'i-lpf.svg': 'rating',
    };
    const chipRe = /<span class="chip"[^>]*>[\s\S]*?src="([^"]+)"[^>]*>([\s\S]*?)<\/span>/gi;
    for (const match of card.matchAll(chipRe)) {
      const iconSrc = match[1] as string;
      const chipText = (match[2] as string).replace(/<[^>]+>/g, '');
      for (const [icon, key] of Object.entries(icons)) {
        const value = clean(chipText);
        if (iconSrc.includes(icon) && value && !(key in chips)) chips[key] = value;
      }
    }

    const booking = abs(textOf(/<a href="([^"]+)"[^>]*class="btn-buy"/i, card));
    const match = booking ? /[?&]id=(\d+)/.exec(booking) : null;
    const bookingId = match ? Number(match[1]) : null;
    const duration = clean(chips.duration ?? null) ?? undefined;

    movies.push(
      new Movie({
        title,
        slug,
        genre: asList(chips.genre ?? null),
        runtime: durationMinutes(duration ?? null),
        duration: duration ?? null,
        language: clean(chips.language ?? null),
        subtitles: splitCodes(chips.subtitles ?? null),
        rating: clean(chips.rating ?? null),
        posterUrl: abs(textOf(/<div class="poster"[^>]*>\s*<img src="([^"]+)"/i, card)),
        bookingUrl: booking,
        bookingId,
        id: match ? String(match[1]) : '',
        parentId: match ? String(match[1]) : null,
      }),
    );
  }
  return movies;
}

/**
 * Pull every outlet card out of the website's /cinemas/gsc/ page.
 *
 * Each card carries a link, a name, a town, and an optional comma-joined list of
 * formats and amenities. GSC files a couple of outlets under a state rather than a
 * town (GSC Spring Bintulu is listed as "Sarawak"), so `city` is whatever GSC says
 * and is not guaranteed to be a locality.
 *
 * The page is not only GSC-branded: it also lists Aurum Theatre and Velvet
 * Cinemas, so `joinDirectories` ignores anything not called "GSC ...".
 */
export function parseCinemasPage(html: string): Cinema[] {
  const cards = html.split('<div class="card-info ratio ratio-4x3"').slice(1);
  const found: Cinema[] = [];
  const seen = new Set<string>();

  for (const card of cards) {
    const slug = slugPathOf(textOf(/<a href="(\/cinema\/[^"]+)"/i, card), 'cinema');
    const name = textOf(/class="font-m fw-bold mb-2"[^>]*>(.*?)<\/div>/is, card);
    if (!slug || !name || seen.has(slug)) continue;
    seen.add(slug);
    found.push(
      new Cinema({
        id: slug,
        name,
        slug,
        city: textOf(/twi-location[^>]*>(.*?)<\/li>/is, card),
        // The amenities row is absent on plain single-screen outlets.
        features: splitCodes(textOf(/twi-experience[^>]*>(.*?)<\/li>/is, card)),
      }),
    );
  }
  return found;
}

/**
 * Read one movie's detail page from the website.
 *
 * The page embeds a schema.org/Movie JSON-LD block, which is the richest and
 * cleanest source. Falls back to the visible specs table for anything missing.
 */
export function parseDetail(html: string, slug: string): Movie {
  let data: Record<string, unknown> = {};
  // Attributes come in any order, e.g. <script data-n-head="ssr" type="application/ld+json">.
  const scriptRe =
    /<script[^>]*type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi;
  for (const [, raw] of html.matchAll(scriptRe)) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw as string);
    } catch {
      continue;
    }
    for (const item of Array.isArray(parsed) ? parsed : [parsed]) {
      if (item && typeof item === 'object' && (item as Record<string, unknown>)['@type'] === 'Movie') {
        data = item as Record<string, unknown>;
        break;
      }
    }
    if (Object.keys(data).length) break;
  }

  const specs = (label: string): string | null => {
    const match = new RegExp(
      `class="spec-label"[^>]*>\\s*${label}\\s*<\\/span>\\s*<span class="spec-value"[^>]*>\\s*:?\\s*([^<]*)`,
      'i',
    ).exec(html);
    return match ? clean(match[1]) : null;
  };

  const credit = (label: string): string | null => {
    const match = new RegExp(`class="credit-label"[^>]*>\\s*${label}\\s*<\\/span>\\s*:\\s*([^<]*)`, 'i').exec(
      html,
    );
    return match ? clean(match[1]) : null;
  };

  let genre = data.genre;
  if (Array.isArray(genre)) genre = genre.filter(Boolean).map(String).join(', ');
  const booking = abs(textOf(/<a href="([^"]+)"[^>]*class="cta-book"/i, html));
  const match = booking ? /[?&]id=(\d+)/.exec(booking) : null;
  const actors = data.actor ?? [];
  const actorList = Array.isArray(actors) ? actors : [actors];
  const trailer = data.trailer;
  const duration = clean(data.duration as string | null) || specs('Running Time');
  const title =
    (data.name as string | undefined) ||
    textOf(/class="movie-title"[^>]*>(.*?)<\/h1>/is, html) ||
    slug;

  const fromJsonLd = actorList
    .map((item) =>
      item && typeof item === 'object' ? (item as Record<string, unknown>).name : item,
    )
    .filter((name): name is string => Boolean(name)) as string[];

  return new Movie({
    title,
    slug,
    genre: asList(genre ?? specs('Genre')),
    runtime: durationMinutes(duration),
    duration,
    language: clean((data.inLanguage as string | null) ?? null) || specs('Language'),
    subtitles: splitCodes(specs('Subtitles')),
    rating: clean((data.contentRating as string | null) ?? null) || specs('Classification'),
    posterUrl: abs(data.image as string | null),
    bookingUrl: booking,
    bookingId: match ? Number(match[1]) : null,
    synopsis: clean(data.description as string | null),
    director: credit('Director'),
    cast: fromJsonLd.length ? fromJsonLd : splitCodes(credit('Cast')),
    trailerUrl: trailer && typeof trailer === 'object' ? ((trailer as Record<string, unknown>).embedUrl as string | undefined) ?? null : null,
    releaseDate: clean((data.dateCreated as string | null) ?? null) || specs('Release Date'),
  });
}

/** Match a pattern and return group 1 with its tags stripped and cleaned. */
function textOf(re: RegExp, html: string): string | null {
  const match = re.exec(html);
  if (!match) return null;
  return clean((match[1] ?? '').replace(/<[^>]+>/g, ' '));
}

/** Resolve a possibly-relative URL against GSC's own hosts. */
function abs(url: string | null): string | null {
  if (!url) return null;
  if (url.startsWith('//')) return `https:${url}`;
  if (url.startsWith(`http${BOOKING}`) || url.startsWith(`http${SITE}`)) return url;
  return url.startsWith('/') ? `${SITE}${url}` : url;
}

/** '/movie/some-slug/' -> 'some-slug'. Slugs with an accent are percent-encoded. */
function slugOf(href: string | null): string | null {
  const match = /\/movie\/([^/?#]+)/.exec(href ?? '');
  return match ? decodeURIComponent(match[1] as string) : null;
}

/** '/cinema/some-slug/' -> 'some-slug'. */
function slugPathOf(href: string | null, section: string): string | null {
  const match = new RegExp(`/${section}/([^/?#]+)`).exec(href ?? '');
  return match ? (match[1] as string) : null;
}

// ---------------------------------------------------------------------------
// filtering
// ---------------------------------------------------------------------------

/**
 * Keep only the showtimes inside a window. Both bounds are inclusive.
 *
 * The window is anchored on the business date, not on the clock alone. GSC files a
 * 00:15 show under the previous day's business date but timestamps it on the next
 * calendar day, so `--from 18:00` still picks those up: you get everything from
 * 18:00 that evening onwards, late shows included.
 */
export function filterTimes(
  movies: Movie[],
  after?: string | null,
  before?: string | null,
): Movie[] {
  const low = parseClock(after ?? null);
  const high = parseClock(before ?? null);
  if (low === null && high === null) return movies;

  const keep = (showtime: Showtime): boolean => {
    if (!showtime.start) return false;
    let anchor: number;
    if (showtime.businessDate) {
      const day = parseIsoDate(showtime.businessDate);
      anchor = day
        ? Date.UTC(day[0], day[1] - 1, day[2]) - MYT_OFFSET_MS
        : showtime.start.getTime();
    } else {
      anchor = mytMidnight(showtime.start);
    }
    const midnight = anchor;
    if (low !== null && showtime.start.getTime() < midnight + low[0] * 3_600_000 + low[1] * 60_000) {
      return false;
    }
    if (high !== null && showtime.start.getTime() > midnight + high[0] * 3_600_000 + high[1] * 60_000) {
      return false;
    }
    return true;
  };

  for (const film of movies) film.showtimes = film.showtimes.filter(keep);
  return movies.filter((film) => film.showtimes.length > 0);
}

/** That showtime's own calendar midnight, in Malaysia time. */
function mytMidnight(date: Date): number {
  const shifted = new Date(date.getTime() + MYT_OFFSET_MS);
  return Date.UTC(shifted.getUTCFullYear(), shifted.getUTCMonth(), shifted.getUTCDate()) - MYT_OFFSET_MS;
}

/**
 * Keep only showtimes in the named hall format(s).
 *
 * `--format imax` is the premium tier, `--format 2d` the ordinary one.
 * Comma-separate for several: `--format imax,4dx`. Matching is on the format
 * code, case-insensitively, and unknown codes simply return nothing rather than
 * erroring.
 */
export function filterFormats(movies: Movie[], wanted?: string | null): Movie[] {
  if (!wanted) return movies;
  const codes = new Set(
    wanted
      .split(',')
      .map((c) => c.trim().toLowerCase())
      .filter(Boolean),
  );
  for (const film of movies) {
    film.showtimes = film.showtimes.filter((s) => codes.has((s.experience ?? '').toLowerCase()));
  }
  return movies.filter((film) => film.showtimes.length > 0);
}

/** Fold seat counts onto the showtimes, in place. */
export function applySeatStatus(
  movies: Movie[],
  seats: Record<string, SeatCounts | undefined>,
): Movie[] {
  for (const movie of movies) {
    for (const showtime of movie.showtimes) {
      const counts = seats[showtime.sessionId];
      if (counts) {
        showtime.seatsTotal = counts.seats_total;
        showtime.seatsUsed = counts.seats_used;
      }
    }
  }
  return movies;
}

// ---------------------------------------------------------------------------
// outlet filters
// ---------------------------------------------------------------------------

/**
 * Narrow an already-fetched outlet list to one region.
 *
 * Accepts either the region key ("klang valley") or its label ("Klang Valley"),
 * case-insensitively.
 */
export function byRegion(outlets: Cinema[], region: string): Cinema[] {
  const wanted = region.trim().toLowerCase();
  return outlets.filter(
    (c) =>
      wanted === (c.region ?? '').toLowerCase() || wanted === (c.location ?? '').toLowerCase(),
  );
}

/**
 * Narrow an already-fetched outlet list to one town, case-insensitively.
 *
 * Matches the town in the service's own name, which every outlet has, as well as
 * the website's `city` label when the two directories were joined.
 */
export function byCity(outlets: Cinema[], city: string): Cinema[] {
  const wanted = city.trim().toLowerCase();
  return outlets.filter(
    (c) => wanted === (c.city ?? '').toLowerCase() || wanted === (c.town ?? '').toLowerCase(),
  );
}

/** Narrow an outlet list by region or town, whichever matches. */
export function filterCinemas(outlets: Cinema[], where: string): Cinema[] {
  return byRegion(outlets, where).length ? byRegion(outlets, where) : byCity(outlets, where);
}

/** A name with everything but its letters and digits removed. */
function squash(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '');
}

/**
 * Look one outlet up by id, slug or name, case-insensitively.
 *
 * Punctuation is ignored, so "GSC 1 Utama" finds the outlet the website calls
 * `GSC 1 Utama` even though the service, which is what an id has to come from,
 * calls it `Petaling Jaya - 1 Utama (New Wing)`. Its slug is what the website
 * name becomes, so matching on that bridges the two naming schemes.
 */
export function findCinema(outlets: Cinema[], needle: string): Cinema {
  const key = needle.trim().toLowerCase();
  const squashed = squash(needle);
  for (const c of outlets) {
    if (c.id.toLowerCase() === key || (c.slug ?? '').toLowerCase() === key) return c;
  }
  for (const c of outlets) {
    if (c.name.toLowerCase() === key || squash(c.name) === squashed) return c;
  }
  let hits = outlets.filter(
    (c) => key.includes(c.name.toLowerCase()) || c.name.toLowerCase().includes(key) || squashed.includes(squash(c.name)),
  );
  if (!hits.length) {
    hits = outlets.filter((c) => c.slug && squashed.includes(squash(c.slug)));
  }
  if (hits.length === 1) return hits[0] as Cinema;
  if (hits.length) {
    die(
      `'${needle}' matches ${hits.length} cinemas: ` +
        hits.slice(0, 8).map((c) => `${c.id} ${c.name}`).join(', '),
    );
  }
  die(`no cinema matching '${needle}'`);
}

// ---------------------------------------------------------------------------
// queries
// ---------------------------------------------------------------------------

function optionsOf(net: NetOptions): { throttle: Throttle; timeout: number } {
  const throttle = net.throttle ?? new Throttle(net.delay ?? 0);
  return { throttle, timeout: net.timeout ?? 25 };
}

/**
 * The whole booking catalogue: every film, one record each.
 *
 * One request. Each film carries the dates it has sessions on in
 * `businessDates()` terms, and its bookable format variants in `variants`.
 */
export async function movies(net: NetOptions = {}): Promise<Movie[]> {
  const { throttle, timeout } = optionsOf(net);
  const body = await request(
    ENDPOINTS.movies,
    { includeChild: 'true' },
    { timeout, throttle },
  );
  return parseMovies(body);
}

/**
 * The website's editorial listing: now-showing, coming-soon, advance-sales.
 *
 * This is the one function GSC has that TGV does not, and it has no counterpart
 * in the booking service: which shelf a film sits on is an editorial decision the
 * website makes, not a property of a session. The catalogue from `movies()` is the
 * service's own view and is not divided this way.
 */
export async function listing(name: string, net: NetOptions = {}): Promise<Movie[]> {
  if (!(name in LISTINGS)) {
    die(`unknown listing '${name}', pick one of ${Object.keys(LISTINGS).join(', ')}`);
  }
  return parseListing(await fetchPage(LISTINGS[name] as string, net));
}

/**
 * The website's record for one film, with synopsis and credits.
 *
 * The booking service carries no synopsis, director or cast, so the detail page
 * is still the only place those come from. Its booking link carries the same
 * parent code the catalogue uses, so the two records join on it.
 *
 * The website leaves fields it has not published as `N/A`, and an unreleased
 * title's running time is one of them, while the booking service already knows
 * it. So with `enrich` the record is backfilled from the catalogue, one extra
 * request, and anything the catalogue does not know either stays null.
 */
export async function detail(slug: string, { enrich = true, ...net }: NetOptions & { enrich?: boolean } = {}): Promise<Movie> {
  const trimmed = slug.trim().replace(/^\/+|\/+$/g, '').replace(/^movie\//, '').replace(/\/+$/, '');
  if (!trimmed || trimmed.includes('/')) die(`bad slug: '${trimmed}'`);
  const movie = parseDetail(await fetchPage(`/movie/${trimmed}/`, net), trimmed);
  if (movie.bookingId !== null) {
    movie.id = String(movie.bookingId);
    movie.parentId = movie.id;
  }
  if (enrich && movie.id) {
    const fromService = await serviceFilm(movie, net);
    for (const name of [
      'runtime',
      'language',
      'rating',
      'genre',
      'posterUrl',
      'trailerUrl',
      'slug',
    ] as const) {
      const here = movie[name];
      const there = fromService[name];
      const empty = here === null || here === undefined || (Array.isArray(here) && !here.length) || here === '';
      if (empty && there && (!Array.isArray(there) || there.length)) movie[name] = there;
    }
    movie.experiences = [...fromService.experiences];
    movie.variants = [...fromService.variants];
  }
  return movie;
}

/** The catalogue's record for one film, matched on its booking id. */
async function serviceFilm(movie: Movie, net: NetOptions): Promise<Movie> {
  for (const candidate of await movies(net)) {
    if (candidate.id === movie.id) return candidate;
  }
  return movie;
}

/**
 * Every GSC outlet in Malaysia, sorted by name.
 *
 * withDetails=true adds the website's slug, town and amenities, which costs one
 * more request for the outlet page. withDetails=false returns the lighter id and
 * name list only, in one request.
 */
export async function cinemas(
  { withDetails = true, ...net }: NetOptions & { withDetails?: boolean } = {},
): Promise<Cinema[]> {
  const { throttle, timeout } = optionsOf(net);
  const body = await request(ENDPOINTS.cinemas, null, { timeout, throttle });
  let payload: unknown;
  try {
    payload = JSON.parse(body);
  } catch {
    die(`non-JSON reply from ${ENDPOINTS.cinemas}`);
  }

  let directory: Cinema[] | null = null;
  if (withDetails) {
    const page = await fetchPage(CINEMA_PAGE, { timeout, throttle });
    directory = parseCinemasPage(page).filter((c) => c.name.startsWith('GSC '));
  }
  return parseCinemas(payload, directory);
}

/**
 * {region key: region label} for every GSC region.
 *
 * Derived from the outlet directory rather than a dedicated endpoint: the service
 * tags each outlet with a region but publishes no list of its own.
 */
export async function regions(net: NetOptions = {}): Promise<Record<string, string>> {
  const { throttle, timeout } = optionsOf(net);
  const labels: Record<string, string> = {};
  for (const cinema of await cinemas({ ...net, withDetails: false, throttle, timeout })) {
    if (cinema.location && cinema.region) labels[cinema.location] = cinema.region;
  }
  return Object.fromEntries(
    Object.entries(labels).sort((a, b) => (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0)),
  );
}

/**
 * Every date the service will answer for, ascending.
 *
 * The union of every date any film has a session on, so it reaches past today into
 * advance sales, and it includes dates only a handful of cinemas run. One
 * request, the same one `movies()` makes.
 */
export async function businessDates(net: NetOptions = {}): Promise<string[]> {
  const found = new Set<string>();
  for (const movie of await movies(net)) {
    for (const date of movie.dates) found.add(date);
  }
  return [...found].sort();
}

/** Options the schedule-shaped queries share. */
export interface QueryOptions extends NetOptions {
  on?: string | Date | null;
  seats?: boolean;
  prices?: boolean;
  after?: string | null;
  before?: string | null;
  formats?: string | null;
  workers?: number;
}

/**
 * Every movie and every session at one cinema on one date.
 *
 * One request for the day itself. `seats` adds remaining-seat counts and costs one
 * further request per session, because the service has no batched seat call;
 * `prices` costs one further request per session.
 *
 * `after`, `before` and `formats` narrow the result. They are applied to the parsed
 * day **before** seats and prices are fetched, not after. That ordering is the
 * whole point: a seat or price lookup is a request per screening, so filtering
 * afterwards still pays for every one you discarded.
 */
export async function day(cinemaId: string, options: QueryOptions = {}): Promise<Movie[]> {
  const { throttle, timeout, on, seats, prices, after, before, formats, workers = 8 } = options;
  const cid = String(cinemaId).trim();
  if (!cid) die('day() needs a cinema id');
  const body = await request(
    ENDPOINTS.day,
    { locationid: cid, oprndate: normaliseDate(on ?? null) },
    { timeout, throttle },
  );
  const found = filterFormats(filterTimes(parseDay(body, cid), after, before), formats);
  if (seats) applySeatStatus(found, await seatStatusBatch(cid, found, throttle, timeout));
  if (prices) await attachPrices(found, throttle, timeout, workers);
  return found;
}

/**
 * Seat counts for every session still in the batch.
 *
 * The service answers one session at a time, so this is one request per showtime.
 * That is why it is opt-in and why the filters run first: a narrowed query only
 * pays for the screenings it kept.
 */
async function seatStatusBatch(
  cinemaId: string,
  movies: Movie[],
  throttle: Throttle,
  timeout: number,
): Promise<Record<string, SeatCounts>> {
  const jobs = movies.flatMap((m) => m.showtimes).filter((s) => s.sessionId);
  if (!jobs.length) return {};
  const results = await poolMap(jobs, 8, async (showtime): Promise<[string, SeatCounts | null]> => {
    try {
      const plan = await seatPlan(cinemaId, showtime, { throttle, timeout });
      return [showtime.sessionId, countsFromPlan(plan)];
    } catch (e) {
      console.error(`gsc: seat lookup failed, ${showtime.time}: ${(e as Error).message}`);
      return [showtime.sessionId, null];
    }
  });
  const out: Record<string, SeatCounts> = {};
  for (const [sid, counts] of results) if (counts) out[sid] = counts;
  return out;
}

/** The counts the seat plan's own availability block implies, for seatStatus. */
function countsForStatus(plan: SeatPlan): SeatCounts {
  const total = plan.seatsTotal;
  const booked = plan.seatsBooked ?? 0;
  let left: number | null = null;
  const values = Object.values(plan.available);
  if (values.length) left = values.reduce((a, b) => a + b, 0);
  if (left === null && total !== null) left = Math.max(0, total - booked);
  return {
    seats_total: total,
    seats_used: booked,
    seats_left: left,
    used_percentage: total ? Math.round((booked / total) * 100 * 10) / 10 : null,
  };
}

/**
 * {cinema id: movies} for one date across many cinemas.
 *
 * Each cinema is one request, so a national run is one request per outlet. They go
 * out in parallel but are still rate limited by the throttle. prices=True adds one
 * more request per session on top, which is a lot, so it is off by default.
 *
 * `after`, `before` and `formats` are applied per cinema before seats and prices
 * are fetched, so a filtered run never pays for screenings it drops.
 */
export async function schedule(
  cinemaIds: string[] | null = null,
  options: QueryOptions = {},
): Promise<Record<string, Movie[]>> {
  const { throttle, timeout, on, seats, prices, after, before, formats, workers = 4 } = options;
  const when = normaliseDate(on ?? null);

  let ids: string[];
  if (cinemaIds === null) {
    ids = (await cinemas({ ...options, withDetails: false, throttle, timeout })).map((c) => c.id);
  } else {
    ids = cinemaIds.map((c) => String(c).trim());
  }
  if (!ids.length) return {};

  const results = await poolMap(ids, workers, async (cid): Promise<[string, Movie[]]> => {
    const body = await request(
      ENDPOINTS.day,
      { locationid: cid, oprndate: when },
      { timeout, throttle },
    );
    const found = filterFormats(filterTimes(parseDay(body, cid), after, before), formats);
    if (seats) applySeatStatus(found, await seatStatusBatch(cid, found, throttle, timeout));
    if (prices) await attachPrices(found, throttle, timeout, workers);
    return [cid, found];
  });

  const out: Record<string, Movie[]> = {};
  for (const id of ids) {
    const films = results.find(([cid]) => cid === id)?.[1];
    if (films && films.length) out[id] = films;
  }
  return out;
}

/** One film, and the cinemas it plays at. */
export interface MovieHit {
  cinemaId: string;
  movie: Movie;
}

/**
 * Every session of one film, as [{cinemaId, movie}].
 *
 * The service has no movie-centric session lookup by title, so the film is resolved
 * in the catalogue first and then asked for directly. That is one request to
 * resolve the film and one for its sessions, which is cheaper than walking every
 * cinema the way a title search would have to, and it returns every cinema at once
 * rather than only the ones you guessed.
 *
 * A film can be filed under several titles that differ only by a bracketed format
 * prefix, so a title is matched loosely too. Every variant that has sessions is
 * returned; if none does, the film is reported as having none.
 *
 * Pass `cinemaIds` to stay on one or a few outlets. Left as null it sweeps the
 * whole country, and with `prices: true` that is a price lookup per session at
 * every outlet.
 */
export async function movie(
  slugOrTitle: string,
  cinemaIds: string[] | null = null,
  options: QueryOptions = {},
): Promise<MovieHit[]> {
  const { throttle, timeout, on, seats, prices, after, before, formats, workers = 4 } = options;
  const when = normaliseDate(on ?? null);
  const catalogue = await movies({ throttle, timeout });

  const wanted = slugOrTitle.trim().toLowerCase();
  const slug = wanted.replace(/ /g, '-');
  const loose = looseTitle(slugOrTitle);
  const hits = catalogue.filter(
    (m) =>
      m.id === wanted ||
      (m.slug ?? '') === slug ||
      m.title.toLowerCase() === wanted ||
      looseTitle(m.title) === loose,
  );
  if (!hits.length) die(noSessions(slugOrTitle, when, after, before, formats));

  let found: Movie[];
  if (cinemaIds !== null) {
    const targets = cinemaIds.map((c) => String(c).trim());
    // The day payload carries no outlet name, so the directory is asked for the
    // names. One request, and only when the caller narrowed to a few outlets.
    const names = new Map(
      (await cinemas({ ...options, withDetails: false, throttle, timeout })).map((c) => [c.id, c.name]),
    );
    found = [];
    for (const cid of targets) {
      const body = await request(
        ENDPOINTS.day,
        { locationid: cid, oprndate: when },
        { timeout, throttle },
      );
      found.push(
        ...parseDay(body, cid, names.get(cid) ?? null).filter((film) =>
          hits.some((hit) => film.id === hit.id || looseTitle(film.title) === looseTitle(hit.title)),
        ),
      );
    }
  } else {
    // Ask for every variant and keep the ones that have sessions, rather than
    // betting on the first catalogue match.
    found = [];
    for (const hit of hits) {
      const body = await request(
        ENDPOINTS.movieShows,
        { parentid: hit.id, oprndate: when },
        { timeout, throttle },
      );
      found.push(...parseMovieShows(body, hit.id, when));
    }
  }

  found = found.filter((m) => filterFormats(filterTimes([m], after, before), formats).length > 0);
  found = found.filter((m) => m.showtimes.length > 0);
  if (!found.length) die(noSessions(slugOrTitle, when, after, before, formats));

  for (const film of found) {
    if (seats) {
      applySeatStatus(
        [film],
        await seatStatusBatch(film.cinemaId ?? '', [film], throttle, timeout),
      );
    }
    if (prices) await attachPrices([film], throttle, timeout, workers);
  }
  return found.map((f) => ({ cinemaId: f.cinemaId ?? '', movie: f }));
}

function noSessions(
  wanted: string,
  when: string,
  after: string | null | undefined,
  before: string | null | undefined,
  formats: string | null | undefined,
): string {
  const hint = after || before || formats ? ' with those --from/--until/--format filters' : '';
  return `no sessions for '${wanted}' on ${when}${hint}`;
}

/** The query the hall endpoints want: which hall, which day, which time. */
function seatParams(cinemaId: string, showtime: Showtime): Record<string, string> {
  return {
    locationid: String(cinemaId).trim(),
    hallid: String(showtime.hallId ?? ''),
    showdate: showtime.start ? mytDateString(showtime.start) : '',
    showtime: showtime.start
      ? `${String(new Date(showtime.start.getTime() + MYT_OFFSET_MS).getUTCHours()).padStart(2, '0')}${String(
          new Date(showtime.start.getTime() + MYT_OFFSET_MS).getUTCMinutes(),
        ).padStart(2, '0')}`
      : '',
  };
}

/**
 * The full seat map for one session.
 *
 * The day payload carries no seat coordinates at all, so this is the only way to
 * see which seats are taken. One request per session. Pass a Showtime, or a session
 * id with the hall, date and time spelled out.
 */
export async function seatPlan(
  cinemaId: string,
  showtime: Showtime | string,
  net: NetOptions & { hallId?: string; showdate?: string; showtime?: string; hallName?: string } = {},
): Promise<SeatPlan> {
  const { throttle, timeout } = optionsOf(net);
  let sessionId: string;
  let params: Record<string, string>;
  if (typeof showtime !== 'string') {
    sessionId = showtime.sessionId;
    params = seatParams(cinemaId, showtime);
  } else {
    sessionId = showtime;
    params = {
      locationid: String(cinemaId).trim(),
      hallid: String(net.hallId ?? ''),
      showdate: normaliseDate(net.showdate ?? null),
      showtime: String(net.showtime ?? '').replace(/\D/g, ''),
    };
  }
  if (!sessionId) die('seatPlan needs a session id');
  const body = await request(ENDPOINTS.seatPlan, params, { timeout, throttle });
  const plan = parseSeatPlan(body, String(cinemaId).trim(), sessionId);
  plan.hallName = typeof showtime === 'string' ? (net.hallName ?? null) : showtime.hall;
  return plan;
}

/**
 * Seat counts for one or more sessions: total, used, left, percentage.
 *
 * The service answers one session at a time, so this is one request per showtime. A
 * failed session is reported on stderr and left out of the result rather than
 * taking the run down.
 */
export async function seatStatus(
  cinemaId: string,
  showtimes: Showtime | Showtime[],
  net: NetOptions = {},
): Promise<Record<string, SeatCounts>> {
  const { throttle, timeout } = optionsOf(net);
  const list = Array.isArray(showtimes) ? showtimes : [showtimes];
  const out: Record<string, SeatCounts> = {};
  for (const showtime of list) {
    try {
      const plan = await seatPlan(cinemaId, showtime, { throttle, timeout });
      out[showtime.sessionId] = countsForStatus(plan);
    } catch (e) {
      console.error(`gsc: seat lookup failed, ${showtime.time}: ${(e as Error).message}`);
    }
  }
  return out;
}

/**
 * Every ticket type on sale for one session.
 *
 * The lowest-level pricing call: a single request, no batching and no filtering.
 * Prefer `sessionPrices` unless the ids are already to hand.
 *
 *     await tickets('235', late)
 *
 * Pass a Showtime and the hall, film and time are read off it. Otherwise pass the
 * ids: hall, film, and the date and time.
 */
export async function tickets(
  cinemaId: string,
  showtime: Showtime | string,
  {
    hallId = null,
    filmId = null,
    on = null,
    showtimeClock,
    ...net
  }: {
    hallId?: string | null;
    filmId?: string | null;
    on?: string | Date | null;
    showtimeClock?: string;
  } & NetOptions = {},
): Promise<TicketType[]> {
  const { throttle, timeout } = optionsOf(net);
  let hall: string | null;
  let film: string | null;
  let when: string;
  let clock: string;
  if (typeof showtime !== 'string') {
    hall = hallId || showtime.hallId;
    film = filmId || showtime.movieId;
    when = normaliseDate(on ?? (showtime.start ? mytDateString(showtime.start) : null));
    clock = showtime.start
      ? `${String(new Date(showtime.start.getTime() + MYT_OFFSET_MS).getUTCHours()).padStart(2, '0')}${String(
          new Date(showtime.start.getTime() + MYT_OFFSET_MS).getUTCMinutes(),
        ).padStart(2, '0')}`
      : '';
  } else {
    when = normaliseDate(on ?? null);
    clock = String(showtimeClock ?? '').replace(/\D/g, '');
  }
  if (!(hall && film && clock)) die('tickets needs a hall id, a film id and a show time');
  const body = await request(
    ENDPOINTS.tickets,
    { locationid: String(cinemaId).trim(), hallid: String(hall), filmid: String(film), showdate: when, showtime: clock },
    { timeout, throttle },
  );
  return parseTickets(body);
}

/**
 * Price one screening and return it.
 *
 * This is the on-demand path: you pick the session, this prices that session and
 * nothing else. `day({prices: true})` and friends are the bulk path and loop this
 * logic over every showtime they kept.
 *
 *     const films = await day('235', { on: '2026-10-14' });
 *     const late = films.flatMap((f) => f.showtimes).find((s) => s.time === '22:00');
 *     (await sessionPrices(late)).price;
 *
 * Mutates and returns the showtime, so `showtime.price` reads the same afterwards.
 * The result is the cheapest standard adult seat; `priceBundle` has the pair price
 * if the hall sells one.
 */
export async function sessionPrices(showtime: Showtime, net: NetOptions = {}): Promise<Showtime> {
  try {
    showtime.ticketTypes = await tickets(showtime.cinemaId, showtime, net);
  } catch (e) {
    console.error(`gsc: price lookup failed, ${showtime.time}: ${(e as Error).message}`);
    showtime.ticketTypes = [];
  }
  return showtime;
}

/**
 * Fill in ticket prices on every showtime, in place.
 *
 * Prices are only fetched for the showtimes still in `movies`, so a narrowed query
 * (`--from`, `--until`, `--format`, or one film at one cinema) costs only the
 * showtimes it kept. Filter first, then call this.
 *
 * One request per showtime. Those go out in parallel; the throttle still holds the
 * *global* rate, so the request count is unchanged and a run stops being serial.
 *
 * A failed call does not take the run down with it. That showtime is left without a
 * price and reported on stderr, so it prints as a missing price rather than a
 * missing screening.
 */
async function attachPrices(
  movies: Movie[],
  throttle: Throttle,
  timeout: number,
  workers: number,
): Promise<void> {
  const jobs = movies.flatMap((m) => m.showtimes).filter((s) => s.sessionId);
  if (!jobs.length) return;
  await poolMap(jobs, workers, async (showtime) => {
    await sessionPrices(showtime, { throttle, timeout });
  });
}

/**
 * {format code: label} for one cinema, from its day payload.
 *
 * The service publishes no cinema directory with formats on it, but every day
 * payload repeats that cinema's formats in its filter block, so this is one
 * request. `day()` and `schedule()` fold the same block into each movie's
 * `experiences`.
 */
export async function cinemaExperiences(
  cinemaId: string,
  { on = null, ...net }: QueryOptions = {},
): Promise<Record<string, string>> {
  const { throttle, timeout } = optionsOf(net);
  const body = await request(
    ENDPOINTS.day,
    { locationid: String(cinemaId).trim(), oprndate: normaliseDate(on) },
    { timeout, throttle },
  );
  const root = xmlOf(body, 'day');
  const found: Record<string, string> = {};
  for (const group of root.findAll('filters').flatMap((f) => f.findAll('group'))) {
    if (group.attr('name') !== 'Experiences') continue;
    for (const item of group.findAll('type')) {
      const code = clean(item.attr('code'));
      if (code) found[code] = clean(item.attr('name')) || code;
    }
  }
  return found;
}

// ---------------------------------------------------------------------------
// rendering
// ---------------------------------------------------------------------------

/**
 * One fixed-width cell. Blank for an aisle, the seat number when free, `X` when
 * taken and `-` when blocked. The width is constant so every column lines up down
 * the grid.
 */
function seatCell(seat: Seat | undefined, width: number): string {
  if (!seat) return ' '.repeat(width);
  const mark = SEAT_MARKS[seat.state];
  const text = mark !== undefined && mark !== null ? mark : (seat.number ?? seat.id);
  return text.padStart(width);
}

/**
 * Draw a hall as fixed-width text.
 *
 * Every cell is `width` characters, and a column with no seat in it is left blank
 * rather than closed up. That is the whole point: halls genuinely differ row to
 * row, with aisles mid-hall, and packing the numbers together makes a wide row look
 * like it has an extra seat. Rendering the column positions is what makes the aisles
 * visible.
 *
 * GSC's grid is already absolute, so the columns can be drawn as they arrive. The
 * drawing ends with a count of seats drawn per section, and prints the service's
 * own figure when the two disagree, so a miscount surfaces immediately rather than
 * being argued about.
 */
export function renderSeatPlan(plan: SeatPlan, width = 3, ruler = true): string {
  const first = plan.firstColumn;
  const last = plan.lastColumn;
  const columns: number[] = [];
  for (let c = first; c <= last; c += 1) columns.push(c);
  const names = plan.areas.flatMap((a) => a.rows.map((r) => (r.name ?? '').length));
  const labelWidth = (names.length ? Math.max(...names) : 0) + 2;

  const out: string[] = [];
  if (ruler && columns.length) {
    out.push(' '.repeat(labelWidth) + columns.map((c) => String(c).padStart(width)).join(''));
    // Ticks are as wide as the label they sit under and right-aligned to match it,
    // so every column reads as sitting on its own digit.
    out.push(
      ' '.repeat(labelWidth) +
        columns.map((c) => '-'.repeat(String(c).length).padStart(width)).join('').replace(/\s+$/, ''),
    );
  }

  const screen = '='.repeat(labelWidth + columns.length * width);
  out.push(''.padStart(screen.length - 1) + 'SCREEN');

  const drawnFor = new Map<number, number>();
  for (const [area, row] of plan.hallRows) {
    if (!drawnFor.has(area.number)) {
      const taken = area.booked.length;
      drawnFor.set(area.number, 0);
      if (out.length && out[out.length - 1] !== screen) out.push('');
      out.push(
        `${area.label} - ${area.seatCount} seats, ${taken} taken, ${area.seatCount - taken} free`,
      );
    }
    const byColumn = new Map<number, Seat>();
    for (const seat of row.seats) byColumn.set(seat.column, seat);
    out.push(
      (row.name ?? String(row.index)).padEnd(labelWidth) +
        columns.map((c) => seatCell(byColumn.get(c), width)).join(''),
    );
    drawnFor.set(area.number, (drawnFor.get(area.number) ?? 0) + row.seats.length);
  }

  for (const area of plan.areas) {
    const counted = drawnFor.get(area.number) ?? 0;
    if (!counted) continue;
    out.push(
      `${area.label}: ${counted} seats drawn` +
        (counted === area.seatCount ? '' : ` (service says ${area.seatCount})`),
    );
  }
  return out.join('\n');
}

// ---------------------------------------------------------------------------
// printing
// ---------------------------------------------------------------------------

function sessionLine(showtime: Showtime, perPerson = false): string {
  const parts = [showtime.time || showtime.timeText || '??:??', showtime.hall || '-'];
  const left = showtime.seatsLeft;
  if (left !== null) parts.push(left === 0 ? 'SOLD OUT' : `${left} seats`);
  if (perPerson && showtime.perPerson !== null) {
    parts.push(`RM${showtime.perPerson.toFixed(2)}/person`);
    return parts.join('  ');
  }
  // Quote the single-seat price. A bundle covers two people, so showing its full
  // total here would read as the cost of one seat.
  const single = showtime.singleSeatPrice;
  if (single !== null) {
    parts.push(`RM${single.toFixed(2)}`);
    if (showtime.priceBundle !== null) parts.push(`(pair RM${showtime.priceBundle.toFixed(2)})`);
  } else if (showtime.priceRange) {
    parts.push(showtime.priceRange);
  }
  return parts.join('  ');
}

/** Print one cinema's films and their sessions. */
export function showMovies(movies: Movie[], cinemaLabel: string | null = null, perPerson = false): void {
  if (cinemaLabel) console.log(`\n=== ${cinemaLabel}\n`);
  for (const film of movies) {
    const bits = [
      film.genre.length ? film.genre.join(', ') : null,
      film.runtime ? `${film.runtime} min` : null,
      film.rating,
      film.language,
    ].filter((b): b is string => Boolean(b));
    console.log(`  ${film.title}` + (bits.length ? `  (${bits.join(' | ')})` : ''));
    for (const showtime of film.showtimes) {
      console.log(`      ${sessionLine(showtime, perPerson)}`);
    }
    if (!film.showtimes.length) {
      const subs = film.subtitles.join(', ') || '-';
      const audio = film.language || '-';
      console.log(`      ${audio} audio | ${subs} subs | ${film.slug}`);
    }
  }
}

/**
 * Print a website listing, numbered.
 *
 * A listing carries no sessions, so each film gets one summary line rather than the
 * session lines `showMovies` prints.
 */
export function showListing(movies: Movie[], label: string | null = null): void {
  if (label) console.log(`\n${label}: ${movies.length} movies\n`);
  movies.forEach((m, index) => {
    const runtime = m.runtime ? `${m.runtime} min` : 'TBA';
    const subs = m.subtitles.join(', ') || '-';
    console.log(`${String(index + 1).padStart(2)}. ${m.title}`);
    console.log(
      `    ${m.genre.join(' | ') || '-'} | ${runtime} | ${m.rating ?? 'unrated'} | ` +
        `audio ${m.language ?? '-'} | subs ${subs}`,
    );
  });
}

/** Kept as the name this module used before it grew sessions. */
export const show = showListing;

/** Print a whole schedule, one section per cinema. */
export function showSchedule(
  scheduleData: Record<string, Movie[]>,
  perPerson = false,
): void {
  for (const [cid, films] of Object.entries(scheduleData)) {
    const label = films.length ? `${films[0]?.cinemaName || cid} [${cid}]` : cid;
    showMovies(films, label, perPerson);
  }
}

/** Print the numbered outlet directory with region and amenities. */
export function showCinemas(outlets: Cinema[]): void {
  outlets.forEach((c, index) => {
    const formats = c.features.join(', ') || '-';
    const location = c.region || c.city || '-';
    console.log(`${String(index + 1).padStart(2)}. ${c.name} [${c.id}]`);
    console.log(`    ${location} | ${formats}`);
  });
}

// ---------------------------------------------------------------------------
// a small XML reader
// ---------------------------------------------------------------------------

/**
 * One element: a tag, its attributes, its children and its text.
 *
 * Node has no XML parser, so this is the smallest one these payloads need:
 * elements, attributes (single and double quoted), self-closing tags, comments,
 * CDATA, processing instructions and the XML declaration. Nothing here reads
 * namespaces, entities beyond the five predefined ones, or DTDs, because none of
 * the GSC payloads use them.
 */
export class XmlElement {
  tag: string;
  attributes: Record<string, string>;
  children: XmlElement[] = [];
  text = '';

  constructor(tag: string, attributes: Record<string, string> = {}) {
    this.tag = tag;
    this.attributes = attributes;
  }

  append(child: XmlElement): void {
    child.detach();
    this.children.push(child);
    child.parent = this;
  }

  parent: XmlElement | null = null;

  /** Detach from whatever tree this currently sits in, so it can be re-parented. */
  detach(): void {
    if (!this.parent) return;
    const at = this.parent.children.indexOf(this);
    if (at >= 0) this.parent.children.splice(at, 1);
    this.parent = null;
  }

  /** This element's attribute, or null. */
  attr(name: string): string | null {
    return name in this.attributes ? this.attributes[name] : null;
  }

  /** Direct children with this tag name. */
  findAll(tag: string): XmlElement[] {
    return this.children.filter((c) => c.tag === tag);
  }

  /** Direct children with any of these tag names, in document order. */
  findAny(tags: string[]): XmlElement[] {
    return this.children.filter((c) => tags.includes(c.tag));
  }

  /** One direct child with this tag name, or null. */
  find(tag: string): XmlElement | null {
    return this.children.find((c) => c.tag === tag) ?? null;
  }
}

const XML_ENTITIES: Record<string, string> = {
  amp: '&',
  lt: '<',
  gt: '>',
  quot: '"',
  apos: "'",
};

/** The five predefined entities, and numeric references. */
function decodeEntities(value: string): string {
  return value.replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, (whole, body: string) => {
    if (body.startsWith('#')) {
      const code = body[1] === 'x' || body[1] === 'X'
        ? Number.parseInt(body.slice(2), 16)
        : Number.parseInt(body.slice(1), 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : whole;
    }
    return body in XML_ENTITIES ? (XML_ENTITIES[body] as string) : whole;
  });
}

/** Attribute values, where `<` and `&` are not markup. */
function decodeAttribute(value: string): string {
  return decodeEntities(value);
}

const NAME_CHARS = /[A-Za-z0-9_.:-]/;

/** Parse a document and return its root element. */
export function xmlOf(body: string, path: string): XmlElement {
  const root = parseXml(body);
  if (!root) die(`malformed XML from ${path}: no root element`);
  return root;
}

/** Parse a document, or null when there is no root element in it. */
export function parseXml(source: string): XmlElement | null {
  let at = 0;
  const stack: XmlElement[] = [];
  let root: XmlElement | null = null;

  while (at < source.length) {
    const open = source.indexOf('<', at);
    if (open < 0) break;

    if (open > at && stack.length) {
      const top = stack[stack.length - 1] as XmlElement;
      top.text += decodeEntities(source.slice(at, open));
    }
    at = open;

    if (source.startsWith('<!--', at)) {
      const end = source.indexOf('-->', at);
      if (end < 0) break;
      at = end + 3;
      continue;
    }
    if (source.startsWith('<![CDATA[', at)) {
      const end = source.indexOf(']]>', at);
      if (end < 0) break;
      const top = stack[stack.length - 1];
      if (top) top.text += source.slice(at + 9, end);
      at = end + 3;
      continue;
    }
    if (source.startsWith('<?', at)) {
      const end = source.indexOf('?>', at);
      if (end < 0) break;
      at = end + 2;
      continue;
    }
    if (source.startsWith('<!', at)) {
      // A DOCTYPE or similar. Skip it, balancing any internal subset.
      let depth = 0;
      let cursor = at;
      for (; cursor < source.length; cursor += 1) {
        const ch = source[cursor];
        if (ch === '<') depth += 1;
        else if (ch === '>') {
          depth -= 1;
          if (depth === 0) break;
        }
      }
      at = cursor + 1;
      continue;
    }

    const close = source.indexOf('>', at);
    if (close < 0) break;
    let inner = source.slice(at + 1, close);

    if (inner.startsWith('/')) {
      const tag = inner.slice(1).trim();
      // Close up to the matching open tag, tolerating a mismatched tail.
      for (let depth = stack.length - 1; depth >= 0; depth -= 1) {
        if ((stack[depth] as XmlElement).tag === tag) {
          stack.length = depth;
          break;
        }
      }
      at = close + 1;
      continue;
    }

    const selfClosing = inner.endsWith('/');
    if (selfClosing) inner = inner.slice(0, -1);
    const { tag, attributes } = parseTag(inner);
    const element = new XmlElement(tag, attributes);
    const parent = stack[stack.length - 1];
    if (parent) parent.append(element);
    else if (!root) root = element;
    if (!selfClosing) stack.push(element);
    at = close + 1;
  }

  return root;
}

/** Split `tag a="1" b='2'` into its name and attributes. */
function parseTag(inner: string): { tag: string; attributes: Record<string, string> } {
  let at = 0;
  while (at < inner.length && NAME_CHARS.test(inner[at] as string)) at += 1;
  const tag = inner.slice(0, at);
  const attributes: Record<string, string> = {};

  while (at < inner.length) {
    while (at < inner.length && /\s/.test(inner[at] as string)) at += 1;
    if (at >= inner.length) break;
    let nameStart = at;
    while (at < inner.length && !/[\s=]/.test(inner[at] as string)) at += 1;
    const name = inner.slice(nameStart, at);
    while (at < inner.length && /\s/.test(inner[at] as string)) at += 1;
    if (inner[at] !== '=') {
      // A valueless attribute, which is not legal XML but appears in the wild.
      if (name) attributes[name] = '';
      continue;
    }
    at += 1;
    while (at < inner.length && /\s/.test(inner[at] as string)) at += 1;
    const quote = inner[at];
    if (quote === '"' || quote === "'") {
      const end = inner.indexOf(quote, at + 1);
      const stop = end < 0 ? inner.length : end;
      attributes[name] = decodeAttribute(inner.slice(at + 1, stop));
      at = end < 0 ? stop : end + 1;
    } else {
      let valueStart = at;
      while (at < inner.length && !/[\s>]/.test(inner[at] as string)) at += 1;
      attributes[name] = decodeAttribute(inner.slice(valueStart, at));
    }
  }

  return { tag, attributes };
}

// ---------------------------------------------------------------------------
// command line
// ---------------------------------------------------------------------------

interface CliArgs {
  cinema: string | null;
  date: string | null;
  cinemas: boolean;
  movies: boolean;
  dates: boolean;
  regions: boolean;
  listing: string | null;
  all: boolean;
  movie: string | null;
  detail: string | null;
  region: string | null;
  city: string | null;
  earliest: string | null;
  latest: string | null;
  format: string | null;
  json: boolean;
  seats: boolean;
  seatMap: boolean;
  prices: boolean;
  perPerson: boolean;
  workers: number;
  time: string | null;
  noDelay: boolean;
  help: boolean;
}

const VALUED: Record<string, keyof CliArgs> = {
  '--listing': 'listing',
  '--movie': 'movie',
  '--detail': 'detail',
  '--region': 'region',
  '--city': 'city',
  '--from': 'earliest',
  '--until': 'latest',
  '--format': 'format',
  '--workers': 'workers',
  '--time': 'time',
};

const FLAGS: Record<string, keyof CliArgs> = {
  '--cinemas': 'cinemas',
  '--movies': 'movies',
  '--dates': 'dates',
  '--regions': 'regions',
  '--all': 'all',
  '--json': 'json',
  '--seats': 'seats',
  '--seat-map': 'seatMap',
  '--prices': 'prices',
  '--per-person': 'perPerson',
  '--no-delay': 'noDelay',
  '--help': 'help',
};

function parseArgs(argv: string[]): CliArgs {
  const args: CliArgs = {
    cinema: null,
    date: null,
    cinemas: false,
    movies: false,
    dates: false,
    regions: false,
    listing: null,
    all: false,
    movie: null,
    detail: null,
    region: null,
    city: null,
    earliest: null,
    latest: null,
    format: null,
    json: false,
    seats: false,
    seatMap: false,
    prices: false,
    perPerson: false,
    workers: 4,
    time: null,
    noDelay: false,
    help: false,
  };
  const positionals: string[] = [];
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i] as string;
    if (token in VALUED) {
      const value = argv[++i];
      if (value === undefined) die(`${token} needs a value`);
      const key = VALUED[token] as string;
      if (key === 'workers') {
        const n = Number(value);
        if (!Number.isFinite(n)) die(`--workers expects a number, got "${value}"`);
        args.workers = n;
      } else {
        (args as unknown as Record<string, unknown>)[key] = value;
      }
    } else if (token in FLAGS) {
      (args as unknown as Record<string, unknown>)[FLAGS[token] as string] = true;
    } else if (token.startsWith('-') && token !== '-') {
      die(`unknown option ${token}`);
    } else {
      positionals.push(token);
    }
  }
  // Destructuring an absent positional gives undefined, not null, and the
  // `--movie` branch below tests `!== null`, so normalise here.
  [args.cinema, args.date] = [positionals[0] ?? null, positionals[1] ?? null];
  return args;
}

const USAGE = `usage: node gsc_scraper.ts [cinema] [YYYY-MM-DD] [options]

Showtimes, prices and seat maps for GSC Cinemas Malaysia.

  --cinemas               list every outlet instead
  --movies                list the booking catalogue
  --dates                 list dates that have sessions
  --regions               list regions instead
  --listing NAME          the website's listing: now-showing, coming-soon, advance-sales
  --all                   fetch every website listing
  --movie SLUG            every session of one film
  --detail SLUG           the website's full record for one film
  --region NAME           with --cinemas, only this region
  --city NAME             with --cinemas, only this town
  --from HH:MM            only showtimes at or after this time
  --until HH:MM           only showtimes at or before this time
  --format CODES          only these hall formats, e.g. imax or imax,4dx
  --json                  print JSON
  --seats                 include seat availability
  --seat-map              draw the seat map for one session (needs a cinema and --time)
  --prices                include ticket prices (one extra call per session)
  --per-person            with --prices, quote per person instead of per ticket
  --workers N             parallel cinema requests
  --time HH:MM            with --seat-map, which session to draw
  --no-delay              skip the polite pause

examples:
  node gsc_scraper.ts --json
  node gsc_scraper.ts 235 2026-10-14 --prices
  node gsc_scraper.ts --movie digger`;

const printJson = (value: unknown): void => console.log(JSON.stringify(value, null, 2));

export async function main(argv: string[] = process.argv.slice(2)): Promise<void> {
  const args = parseArgs(argv);
  if (args.help) {
    console.log(USAGE);
    return;
  }

  const delay = args.noDelay ? 0 : 0.25;
  const throttle = new Throttle(delay);
  const net: NetOptions = { delay, throttle };

  if (args.regions) {
    const found = await regions(net);
    if (args.json) printJson(found);
    else {
      console.log();
      for (const [key, label] of Object.entries(found)) {
        console.log(`  ${key.padEnd(16)} ${label}`);
      }
    }
    return;
  }

  if (args.dates) {
    const found = await businessDates(net);
    if (args.json) printJson(found);
    else {
      console.log(`\ndates with sessions: ${found.length}\n`);
      console.log(`  ${found.join('  ')}`);
    }
    return;
  }

  if (args.cinemas) {
    let outlets = await cinemas(net);
    if (args.region) outlets = byRegion(outlets, args.region);
    if (args.city) outlets = byCity(outlets, args.city);
    if (args.json) printJson(outlets.map((c) => c.toDict()));
    else {
      const where = args.region || args.city;
      console.log(`\ncinemas${where ? ` in ${where}` : ''}: ${outlets.length} outlets\n`);
      showCinemas(outlets);
    }
    return;
  }

  if (args.movies) {
    const found = await movies(net);
    if (args.json) printJson(found.map((m) => m.toDict()));
    else {
      console.log(`\ncatalogue: ${found.length} films\n`);
      found.forEach((m, index) => {
        const formats = m.experiences.join(', ') || '-';
        console.log(`${String(index + 1).padStart(2)}. ${m.title} [${m.id}]`);
        console.log(
          `    ${m.genre.join(', ') || '-'} | ${m.runtime ?? 'TBA'} min | ` +
            `${m.rating ?? 'unrated'} | ${formats}`,
        );
      });
    }
    return;
  }

  if (args.detail) {
    const found = await detail(args.detail, net);
    if (args.json) {
      printJson(found.toDict());
      return;
    }
    for (const [key, value] of Object.entries(found.toDict())) {
      console.log(`${key.padEnd(18)} ${String(value)}`);
    }
    return;
  }

  if (args.movie) {
    // `--movie` takes an optional cinema and an optional date, in either order of
    // appearance, and the parser cannot tell a date from a cinema name, so decide by
    // shape rather than by position:
    //   --movie slug                      every cinema, today
    //   --movie slug 2026-10-18           every cinema, that date
    //   --movie slug 235                  one cinema, today
    //   --movie slug 235 2026-10-18       one cinema, that date
    let movieCinema = args.cinema;
    let movieOn = args.date;
    if (args.date === null && args.cinema && looksLikeDate(args.cinema)) {
      movieOn = args.cinema;
      movieCinema = null;
    } else if (args.date !== null && looksLikeDate(args.date)) {
      movieOn = args.date;
    } else if (args.date !== null) {
      movieCinema = args.date;
      movieOn = args.cinema;
    }

    let cinemaIds: string[] | null = null;
    if (movieCinema !== null) {
      const target = findCinema(await cinemas({ ...net, withDetails: false }), movieCinema);
      cinemaIds = [target.id];
    } else if (args.region || args.city) {
      const narrowed = filterCinemas(await cinemas(net), (args.region || args.city) as string);
      if (!narrowed.length) die(`no cinemas in '${args.region || args.city}'`);
      cinemaIds = narrowed.map((c) => c.id);
    }

    const found = await movie(args.movie, cinemaIds, {
      ...net,
      on: movieOn,
      seats: args.seats,
      prices: args.prices,
      workers: args.workers,
      after: args.earliest,
      before: args.latest,
      formats: args.format,
    });
    if (args.json) {
      printJson(found.map(({ cinemaId, movie: m }) => ({ cinema_id: cinemaId, ...m.toDict() })));
    } else {
      const where = cinemaIds === null ? 'every cinema' : `${cinemaIds.length} cinema(s)`;
      console.log(
        `\n${args.movie} on ${normaliseDate(movieOn)}, ${where}: ${found.length} cinemas\n`,
      );
      for (const { cinemaId, movie: m } of found) {
        showMovies([m], `${m.cinemaName || cinemaId} [${cinemaId}]`, args.perPerson);
      }
    }
    return;
  }

  if (args.seatMap) {
    if (!args.cinema || !args.time) die('--seat-map needs a cinema and --time HH:MM');
    if (!looksLikeClock(args.time)) die(`bad time '${args.time}', expected HH:MM or h:mmAM/PM`);
    const target = findCinema(await cinemas({ ...net, withDetails: false }), args.cinema);
    const when = normaliseDate(args.date);
    const wanted = new Set(clockCandidates(args.time).map((w) => w.toUpperCase()));
    const matches = (await day(target.id, { ...net, on: when })).flatMap((film) =>
      film.showtimes
        .filter((s) => (s.time !== null && wanted.has(s.time.toUpperCase())) || (s.timeText ?? '').toUpperCase() !== '' && wanted.has((s.timeText ?? '').toUpperCase()))
        .map((s) => [film, s] as [Movie, Showtime]),
    );
    if (!matches.length) die(`no session at ${args.time} on ${when} at ${target.name}`);
    for (const [film, showtime] of matches) {
      const plan = await seatPlan(target.id, showtime, net);
      console.log(
        `\n${target.name} [${target.id}] ${when} ${showtime.time} ` +
          `${showtime.hall ?? ''} ${film.title} (session ${showtime.sessionId})`,
      );
      console.log(renderSeatPlan(plan));
    }
    return;
  }

  if (args.listing || args.all) {
    const names = args.all ? Object.keys(LISTINGS) : [args.listing ?? 'now-showing'];
    if (args.json) {
      const out: Record<string, unknown[]> = {};
      for (const name of names) out[name] = (await listing(name, net)).map((m) => m.toDict());
      printJson(out);
      return;
    }
    for (const name of names) showListing(await listing(name, net), name);
    return;
  }

  if (args.cinema) {
    const target = findCinema(await cinemas({ ...net, withDetails: false }), args.cinema);
    const when = normaliseDate(args.date);
    // Filters go in, not out: a seat or price call is a request per session, so
    // narrowing after the fact still pays for every discarded screening.
    const found = await day(target.id, {
      ...net,
      on: when,
      seats: args.seats,
      prices: args.prices,
      after: args.earliest,
      before: args.latest,
      formats: args.format,
    });
    if (args.json) printJson(found.map((m) => m.toDict()));
    else {
      console.log(`\n${target.name} [${target.id}] ${when}: ${found.length} movies\n`);
      showMovies(found, null, args.perPerson);
    }
    return;
  }

  // No arguments at all: the website's front page listing, which is what a bare
  // invocation has always meant here. A national schedule sweep is one request per
  // outlet, so that has to be asked for.
  showListing(await listing('now-showing', net), 'now-showing');
}

const entry = process.argv[1];
if (entry && import.meta.url === `file://${entry}`) {
  main().catch((e: unknown) => {
    console.error(e instanceof Error ? e.message : String(e));
    process.exit(1);
  });
}