#!/usr/bin/env node -T
// Showtimes for TGV Cinemas Malaysia, read straight from TGV's own JSON API.
//
// Single file, zero dependencies, no build step. Node 22.6+ (developed against
// Node 24 LTS) runs this directly: Node strips the types at load time.
//
//     node tgv_scraper.ts                              # today, every cinema
//     node tgv_scraper.ts BU0                          # one cinema, today
//     node tgv_scraper.ts BU0 2026-10-13               # one cinema, one date
//     node tgv_scraper.ts --cinemas                    # the outlet directory
//     node tgv_scraper.ts --cinemas --region klangvalley
//     node tgv_scraper.ts --dates                      # dates that have sessions
//     node tgv_scraper.ts --movie other-mommy          # one movie, every cinema
//     node tgv_scraper.ts --json                       # machine readable
//     node tgv_scraper.ts --seats                      # add seat availability
//     node tgv_scraper.ts --no-delay                   # skip the polite pause
//     npx tsc --noEmit tgv_scraper.ts                  # optional type check
//
// Also importable:
//
//     import { cinemas, regions, businessDates, day, schedule, movie,
//              sessionPrices } from './tgv_scraper.ts';
//     await cinemas();
//     await day('BU0');
//
// Everything that touches the network is async, so every call returns a
// Promise:
//
//     const films = await day('BU0');
//     const late = films.flatMap((f) => f.showtimes).find((s) => s.time === '21:45');
//     await sessionPrices(late);
//     late.price;
//
// Prices are opt-in and cost one call per session per hall area. Price a single
// screening you picked, rather than the whole day:
//
//     const late = films.flatMap((f) => f.showtimes).find((s) => s.time === '21:45');
//     (await sessionPrices(late)).price;
//
// How it works: tgv.com.my is an Angular app that reads a JSON API on
// api.tgv.com.my. Every endpoint is a plain POST with a JSON body, or a plain GET,
// and none of them want an API key, a signature or a session token. The only
// auth-shaped header the app sends is x-mvcsession, which is the logged-in user's
// token and is sent empty when browsing anonymously. So this calls the same
// endpoints the site itself calls, anonymously.
//
// Times: TGV returns showtimes as naive local strings such as
// "2026-10-11T17:45:00". They are Malaysia wall-clock and are attached to a fixed
// UTC+08:00 offset here. Malaysia has no daylight saving, so the offset is always
// +08:00.

const BASE = 'https://api.tgv.com.my';
const SITE = 'https://www.tgv.com.my';
const CDN = 'https://d9s3e2ooa0od7.cloudfront.net';
const UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';

// Malaysia wall clock. No DST, so a fixed offset is exact.
const MYT_OFFSET_MS = 8 * 60 * 60 * 1000;

// The other scraper in this repo treats these as "no value", so this one does
// too. TGV's own payloads never carry them.
const PLACEHOLDERS = new Set(['N/A', 'NA', '-', 'TBC', 'TBA']);

export const ENDPOINTS = {
  cinemas: '/api/cinemas/v1/getcinemas',
  regions: '/api/cinemas/v1/getstatecinema',
  businessDates: '/api/boxoffice/v1/moviesession_getsessionbusinessdates',
  day: '/api/boxoffice/v1/moviesession_get',
  seatStatus: '/api/boxoffice/v1/moviesession_getseatstatus',
  seatPlan: '/api/boxoffice/v1/moviesession_getseatplan',
  movieCinemas: '/api/boxoffice/v1/moviesession_getmoviecinemas',
  tickets: '/api/boxoffice/v1/moviesession_gettickets',
};

// TGV reports money in cents, as integers. Only `priceInCents` is the ticket
// price and is used; `taxInCents` does not reconcile with any Malaysian rate and
// is ignored (see TicketType).
const CENT = 100;

// A session sells several ticket types at once: the plain seats, then a long
// tail of discounts (SENIOR/OKU, Maybank Mastercard promos, member and card
// rates). The plain seat is what an ordinary customer pays, so it is the one the
// `--prices` output quotes. Everything else stays available on the showtime's
// `ticketTypes` for callers who want the full list.
const STANDARD_TICKETS = new Set(['SINGLE', 'TWIN', 'COUPLE', 'DOUBLE', 'SOLO']);

// TGV flags a finished promo inside the ticket's longDescription rather than
// with a boolean, e.g. "RM10 ticket with Maybank Mastercard debit card\n
// Today's Promotion Limit Reached". The ticket keeps its listed price either
// way, so it has to be filtered out explicitly.
const SOLD_OUT_RE =
  /(promotion\s+limit\s+reached|limit\s+reached|sold\s*out|not\s+available)/i;

// How many people one ticket seats, from the ticket description. The API does
// confirm a bundle via `isPackageTicket`, but it never says how many seats are
// in it, so the count still comes from the name. Unlisted descriptions count as
// 1 so an unfamiliar ticket name never inflates the per-person figure.
const TICKET_SEATS: Record<string, number> = {
  SINGLE: 1,
  SOLO: 1,
  TWIN: 2,
  COUPLE: 2,
  DOUBLE: 2,
};

// The API keys every cinema format by a lowercase code. These are just the ones
// seen in the wild; unknown codes are title cased rather than dropped.
const EXPERIENCE_LABELS: Record<string, string> = {
  deluxe: 'Deluxe',
  beanie: 'Beanie',
  infinity: 'Infinity',
  indy: 'Indy',
  imax: 'IMAX',
  imaxlaser: 'IMAX Laser',
  onyx: 'Onyx',
  indulge: 'Indulge',
  'indulge-bed': 'Indulge Bed',
  'indulge-coway-berex': 'Indulge (Coway Berex)',
  junior: 'Junior',
  'junior-hotwheel': 'Junior (Hot Wheels)',
  premium: 'Premium',
  vip: 'VIP',
};

// ---------------------------------------------------------------------------
// Wire shapes. TGV's payloads are undocumented and every field is optional,
// so these describe what we read and nothing more.
// ---------------------------------------------------------------------------

interface Envelope<T> {
  success?: boolean;
  error?: string;
  results?: T;
}

interface CinemaRecord {
  itemkey?: string;
  name?: string;
  status?: number;
  extdata?: {
    cinema?: {
      location?: string;
      keyword?: string;
      address?: string;
      addressPostcode?: string;
      latitude?: string;
      longitude?: string;
      telephone?: string;
      parkinginfo?: string;
      openingHours?: unknown;
      features?: unknown;
      meta?: { name?: string; description?: string };
    };
    exp?: unknown;
  };
}

interface RegionGroup {
  value?: string;
  label?: string;
  cinemas?: { cinemaid?: string; name?: string }[];
}

interface BusinessDateResults {
  businessdates?: string[];
}

interface MovieInfo {
  movieTrailer?: string;
  genre?: unknown;
  rating?: string;
  runtimemins?: unknown;
  lang?: string;
  subtitlelang?: unknown;
  synopsis?: string;
  director?: string;
  cast?: string;
  distributor?: string;
  releasedatemy?: string;
  hocodes?: unknown;
}

interface MovieRecord {
  recid?: string;
  name?: string;
  itemkey?: string;
  // `assets` is a sibling of `extdata`, not a child of it. Reading it from the
  // wrong place silently yields no poster on every film.
  assets?: { assetkey?: string; extdata?: { fileinfo?: { filepath?: string } } }[];
  extdata?: {
    folderkey?: string;
    tags?: unknown;
    exp?: unknown;
    movieinfo?: MovieInfo;
  };
}

interface SessionRecord {
  sessionid?: unknown;
  movieid?: string;
  cinemaid?: string;
  showtimemy?: string;
  screenname?: string;
  businessdate?: string;
  experience?: string;
  seattypes?: string;
  status?: number;
  extdata?: { areacategorycodes?: string[] };
}

interface DayResults {
  movies?: MovieRecord[];
  businessday?: {
    cinemas?: {
      cinemaid?: string;
      name?: string;
      movies?: { movieid?: string; experiences?: { sessions?: SessionRecord[] }[] }[];
    }[];
  };
}

interface SeatStatusRow {
  sessionid?: string | number;
  seatstotal?: number;
  seatsused?: number;
  usedpercentage?: number;
}

interface SeatStatusResults {
  seatstatuslist?: SeatStatusRow[];
}

interface PlanSeat {
  id?: string | number;
  position?: { rowIndex?: number; columnIndex?: number };
  status?: number;
  priority?: number;
  seatStyle?: number;
  seatsInGroup?: unknown[];
}

interface PlanRow {
  rowIndexZeroBased?: number;
  physicalName?: string;
  seats?: PlanSeat[];
}

interface PlanArea {
  number?: number;
  areaCategoryCode?: string;
  description?: string;
  numberOfSeats?: number;
  rowCount?: number;
  columnCount?: number;
  rows?: PlanRow[];
}

interface SeatPlanResults {
  cinemaid?: string;
  sessionid?: string;
  seatlayout?: {
    areas?: PlanArea[];
    boundaryLeft?: number;
    boundaryRight?: number;
    screenStart?: number;
    screenWidth?: number;
  };
}

interface TicketRow {
  ticketTypeCode?: string;
  description?: string;
  priceInCents?: unknown;
  isChildOnlyTicket?: boolean;
  isAllocatableSeating?: boolean;
  isPackageTicket?: boolean;
  areaCategoryCode?: string;
  longDescription?: string;
}

interface TicketResults {
  tickets?: TicketRow[];
}

interface NetOptions {
  delay?: number;
  timeout?: number;
  throttle?: Throttle;
}

// ---------------------------------------------------------------------------
// Fatal error. Every "this run cannot continue" case throws TgvError, which
// main() turns into a one-line message on stderr plus a non-zero exit code.
// ---------------------------------------------------------------------------

export class TgvError extends Error {}

/** Thrown for a bad --from/--until/date argument; callers catch it to probe. */
class BadInputError extends TgvError {}

const die = (message: string): never => {
  throw new TgvError(message);
};

export function experienceLabel(code: string | null | undefined): string | null {
  // 'junior-hotwheel' -> 'Junior (Hot Wheels)'. Unknown codes still render.
  if (!code) return null;
  const key = String(code).trim().toLowerCase();
  if (key in EXPERIENCE_LABELS) return EXPERIENCE_LABELS[key];
  const titled = key
    .replace(/[-_]+/g, ' ')
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());
  return titled || null;
}

/**
 * Strip whitespace, turn empty strings into null, drop placeholder text.
 *
 * The two scrapers share this so a caller can run the same code over either
 * one's output. TGV's own payloads never carry placeholders, so the extra rule
 * costs nothing here, and it keeps `N/A` and friends from reaching a caller
 * that would otherwise have to handle both.
 */
export function clean(value: unknown): string | null {
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

/** TGV stores synopsis as HTML. Collapse it to plain text. */
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

/** TGV is inconsistent: genre is a list, tags a list, but both can be null. */
export function asList(value: unknown): string[] {
  if (value === null || value === undefined) return [];
  if (typeof value === 'string') return value.trim() ? [value] : [];
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => (item === null || item === undefined ? '' : String(item).trim()))
    .filter(Boolean);
}

/** '2026-10-11T17:45:00' -> a Date at that Malaysia wall-clock instant. */
export function parseTime(value: string | null | undefined): Date | null {
  if (!value) return null;
  const text = String(value).trim();
  if (!text) return null;
  // Naive local time gets the fixed +08:00 offset. A 'Z' means the API already
  // told us the offset, so it is left alone.
  const iso = /[Zz]|[+-]\d{2}:?\d{2}$/.test(text)
    ? text.replace('Z', '+00:00')
    : `${text}+08:00`;
  const stamp = Date.parse(iso);
  return Number.isNaN(stamp) ? null : new Date(stamp);
}

/** A Date -> 'YYYY-MM-DD' in Malaysia wall-clock time. */
function mytDateString(date: Date): string {
  const shifted = new Date(date.getTime() + MYT_OFFSET_MS);
  const y = shifted.getUTCFullYear();
  const m = String(shifted.getUTCMonth() + 1).padStart(2, '0');
  const d = String(shifted.getUTCDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

/** 'YYYY-MM-DD' -> the Date of that midnight in Malaysia wall-clock time. */
export function mytMidnight(dateText: string): Date {
  const [y, m, d] = dateText.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d) - MYT_OFFSET_MS);
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
  const myt = new Date(date.getTime() + MYT_OFFSET_MS);
  const p = (n: number) => String(n).padStart(2, '0');
  return (
    `${myt.getUTCFullYear()}-${p(myt.getUTCMonth() + 1)}-${p(myt.getUTCDate())}` +
    `T${p(myt.getUTCHours())}:${p(myt.getUTCMinutes())}:${p(myt.getUTCSeconds())}+08:00`
  );
}

/** Accept 'YYYY-MM-DD', a Date, or nothing. Returns 'YYYY-MM-DD'. */
export function normaliseDate(value?: string | Date | null): string {
  if (value === null || value === undefined) return todayMyt();
  if (value instanceof Date) return mytDateString(value);
  const text = String(value).trim();
  if (!text) return todayMyt();

  const order: Array<[RegExp, [number, number, number]]> = [
    [/^(\d{4})-(\d{1,2})-(\d{1,2})$/, [1, 2, 3]],
    [/^(\d{1,2})-(\d{1,2})-(\d{4})$/, [3, 2, 1]],
    [/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/, [3, 2, 1]],
    [/^(\d{4})\/(\d{1,2})\/(\d{1,2})$/, [1, 2, 3]],
  ];
  for (const [pattern, [yi, mi, di]] of order) {
    const match = pattern.exec(text);
    if (!match) continue;
    const y = Number(match[yi]);
    const m = Number(match[mi]);
    const d = Number(match[di]);
    // Round-trip through a Date so 2026-02-30 is rejected, as strptime did.
    const probe = new Date(Date.UTC(y, m - 1, d));
    if (
      probe.getUTCFullYear() === y &&
      probe.getUTCMonth() === m - 1 &&
      probe.getUTCDate() === d
    ) {
      return `${String(y).padStart(4, '0')}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    }
  }
  throw new BadInputError(`bad date "${value}", expected YYYY-MM-DD`);
}

const sleep = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

/** Run `fn` over `items` with at most `workers` in flight, results in order. */
async function poolMap<T, R>(
  items: T[],
  workers: number,
  fn: (item: T, index: number) => Promise<R>
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
 * stay parallel, but the global rate stays at one request per `delay` ms.
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
    const now = Date.now();
    const slot = Math.max(now, this._nextSlot);
    this._nextSlot = slot + this.delay * 1000;
    const wait = slot - now;
    if (wait > 0) await sleep(wait);
  }
}

/** Read the options this module accepts, with their defaults. */
function netOptions(options: NetOptions = {}): Required<NetOptions> {
  const delay = options.delay ?? 0;
  return {
    delay,
    timeout: options.timeout ?? 25,
    throttle: options.throttle ?? new Throttle(delay),
  };
}

/** Call one TGV API endpoint and return the decoded body. */
export async function request<T>(
  path: string,
  payload: Record<string, unknown> | null = null,
  { timeout = 25, throttle = null }: { timeout?: number; throttle?: Throttle | null } = {}
): Promise<Envelope<T>> {
  // A payload of null/undefined sends GET, anything else sends POST with that
  // JSON body.
  if (throttle) await throttle.wait();

  const headers: Record<string, string> = {
    'User-Agent': UA,
    Accept: 'application/json, text/plain, */*',
    'Accept-Language': 'en-MY,en;q=0.9',
    // The API is public but the site calls it same-origin. Sending the real
    // Origin/Referer costs nothing and keeps us indistinguishable from normal
    // browsing.
    Origin: SITE,
    Referer: `${SITE}/`,
    // The site's own header for an anonymous visitor.
    'x-mvcsession': '',
  };
  const posting = payload !== null && payload !== undefined;
  if (posting) headers['Content-Type'] = 'application/json';

  const init: RequestInit = { method: posting ? 'POST' : 'GET', headers };
  if (posting) init.body = JSON.stringify(payload);

  // Node's fetch handles gzip and the CA store for us, which is what the
  // Python version had to do by hand.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout * 1000);
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, { ...init, signal: controller.signal });
  } catch (exc) {
    const reason = exc instanceof Error ? exc.message : String(exc);
    if ((exc as Error)?.name === 'AbortError') {
      die(`network error talking to api.tgv.com.my: timed out after ${timeout}s`);
    }
    die(`network error talking to api.tgv.com.my: ${reason}`);
  } finally {
    clearTimeout(timer);
  }

  const text = await response.text();
  if (!response.ok) {
    die(`HTTP ${response.status} from ${path}: ${text.slice(0, 300)}`);
  }
  try {
    return JSON.parse(text) as Envelope<T>;
  } catch {
    die(`non-JSON reply from ${path}`);
  }
}

/** '18:00' or '1800' -> [18, 0]. Null/undefined passes straight through. */
export function parseClock(value: string | null | undefined): [number, number] | null {
  if (value === null || value === undefined) return null;
  const match = /^(\d{1,2}):?(\d{2})$/.exec(String(value).trim());
  if (!match) throw new BadInputError(`bad time "${value}", expected HH:MM`);
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (!(hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59)) {
    throw new BadInputError(`bad time "${value}", expected HH:MM`);
  }
  return [hour, minute];
}

function showtimeOrder(a: Showtime, b: Showtime): number {
  const an = a.start === null ? 1 : 0;
  const bn = b.start === null ? 1 : 0;
  if (an !== bn) return an - bn;
  if (an || !a.start || !b.start) return 0;
  return a.start.getTime() - b.start.getTime();
}

/** Keep only the showtimes inside a window. Both bounds are inclusive. */
export function filterTimes(
  movies: Movie[],
  after: string | null = null,
  before: string | null = null
): Movie[] {
  // The window is anchored on the business date, not on the clock alone. TGV
  // files a 00:30 show under the previous day's business date but timestamps it
  // on the next calendar day, so `--from 18:00` still picks those up: you get
  // everything from 18:00 that evening onwards, late shows included.
  const low = parseClock(after);
  const high = parseClock(before);
  if (low === null && high === null) return movies;

  const keep = (showtime: Showtime): boolean => {
    if (showtime.start === null) return false;
    const midnight = mytMidnight(
      showtime.businessDate || mytDateString(showtime.start)
    ).getTime();
    const start = showtime.start.getTime();
    if (low !== null && start < midnight + (low[0] * 60 + low[1]) * 60000) return false;
    if (high !== null && start > midnight + (high[0] * 60 + high[1]) * 60000) return false;
    return true;
  };

  const kept: Movie[] = [];
  for (const film of movies) {
    film.showtimes = film.showtimes.filter(keep);
    if (film.showtimes.length) kept.push(film);
  }
  return kept;
}

/** Keep only showtimes in the named hall format(s). */
export function filterFormats(movies: Movie[], wanted: string | null = null): Movie[] {
  // `--format deluxe` is the budget tier, `--format imaxlaser` the premium one.
  // Comma-separate for several: `--format deluxe,indulge`. Matching is on the
  // experience code, case-insensitively, and unknown codes simply return
  // nothing rather than erroring.
  if (!wanted) return movies;
  const codes = new Set(
    String(wanted)
      .split(',')
      .map((code) => code.trim().toLowerCase())
      .filter(Boolean)
  );
  const kept: Movie[] = [];
  for (const film of movies) {
    film.showtimes = film.showtimes.filter((s) =>
      codes.has((s.experience || '').toLowerCase())
    );
    if (film.showtimes.length) kept.push(film);
  }
  return kept;
}

/** Unwrap the API's {success, error, results} envelope. */
function results<T>(body: Envelope<T>, path: string): T {
  if (body.success === false) die(`API error from ${path}: ${body.error}`);
  return (body.results ?? {}) as T;
}

// ---------------------------------------------------------------------------
// Records
// ---------------------------------------------------------------------------

export class Cinema {
  id: string;
  name: string;
  /**
   * The URL segment, and the outlet's own keyword. The Python scraper spells
   * this `slug` so both read one contract, and `keyword` is kept here as an
   * alias so existing callers keep working.
   */
  slug: string | null;
  city: string | null;
  location: string | null; // region key, e.g. "klangvalley"
  region: string | null; // region label, e.g. "Klang Valley"
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
  status: number;

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
    status?: number;
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
    this.status = fields.status ?? 1;
  }

  /** Alias of `slug`, the name this field had before the two scrapers were
   * brought onto one contract. */
  get keyword(): string | null {
    return this.slug;
  }

  get url(): string {
    if (this.location && this.slug) return `${SITE}/cinemas/${this.location}/${this.slug}`;
    return `${SITE}/cinemas`;
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

export class TicketType {
  code: string;
  description: string;
  priceCents: number | null;
  childOnly: boolean;
  allocatedSeating: boolean;
  /**
   * True when the ticket bundles more than one seat (TWIN, COUPLE). This is the
   * API's own `isPackageTicket` flag, so it does not depend on guessing from the
   * description. Compare `perPerson`, not `price`, for these.
   */
  package: boolean;
  /**
   * Which hall area this price came from. A session has several, each with its
   * own price list, and the same ticket type can differ between them.
   */
  areaCode: string | null;
  /**
   * TGV marks a promo as used up in its longDescription, e.g. "RM10 ticket
   * with Maybank Mastercard debit card\nToday's Promotion Limit Reached".
   * Such a ticket is still listed with its price, so it must not be quoted.
   */
  soldOut: boolean;
  /**
   * A plain SINGLE/TWIN/COUPLE seat sold at face value. Everything else on a
   * session (senior/OKU, Maybank promos, member rates) is a discounted or
   * special ticket, and `price`/`priceRange` deliberately ignore those so the
   * quoted figure is what an ordinary walk-in pays.
   */
  standard: boolean;

  constructor(fields: {
    code: string;
    description: string;
    priceCents?: number | null;
    childOnly?: boolean;
    allocatedSeating?: boolean;
    package?: boolean;
    areaCode?: string | null;
    soldOut?: boolean;
    standard?: boolean;
  }) {
    this.code = fields.code;
    this.description = fields.description;
    this.priceCents = fields.priceCents ?? null;
    this.childOnly = fields.childOnly ?? false;
    this.allocatedSeating = fields.allocatedSeating ?? false;
    this.package = fields.package ?? false;
    this.areaCode = fields.areaCode ?? null;
    this.soldOut = fields.soldOut ?? false;
    this.standard = fields.standard ?? false;
  }

  /** Advertised ticket price in ringgit, from the API's `priceInCents`. */
  get price(): number | null {
    return this.priceCents === null ? null : this.priceCents / CENT;
  }

  /** How many people this ticket admits. Bundles seat more than one. */
  get seats(): number {
    return TICKET_SEATS[this.description.toUpperCase()] ?? 1;
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

export class Showtime {
  sessionId: string;
  movieId: string;
  cinemaId: string;
  start: Date | null;
  hall: string | null;
  businessDate: string | null;
  experience: string | null;
  seatTypes: string[];
  status: number;
  seatsTotal: number | null;
  seatsUsed: number | null;
  /**
   * TGV prices per hall area and does not send the hall's own id, so this stays
   * null here. The other scraper fills it in.
   */
  hallId: string | null;
  /** Needed to ask for prices; TGV prices tickets per hall area. */
  areaCodes: string[];
  /** The movie's runtime in minutes, set by the caller so `ends` works. */
  runtime: number | null;
  /** `--prices` only, one entry per ticket type TGV sells for this session. */
  ticketTypes: TicketType[];

  constructor(fields: {
    sessionId: string;
    movieId: string;
    cinemaId: string;
    start: Date | null;
    hall?: string | null;
    businessDate?: string | null;
    experience?: string | null;
    seatTypes?: string[];
    status?: number;
    seatsTotal?: number | null;
    seatsUsed?: number | null;
    hallId?: string | null;
    areaCodes?: string[];
    runtime?: number | null;
    ticketTypes?: TicketType[];
  }) {
    this.sessionId = fields.sessionId;
    this.movieId = fields.movieId;
    this.cinemaId = fields.cinemaId;
    this.start = fields.start;
    this.hall = fields.hall ?? null;
    this.businessDate = fields.businessDate ?? null;
    this.experience = fields.experience ?? null;
    this.seatTypes = fields.seatTypes ?? [];
    this.status = fields.status ?? 1;
    this.seatsTotal = fields.seatsTotal ?? null;
    this.seatsUsed = fields.seatsUsed ?? null;
    this.hallId = fields.hallId ?? null;
    this.areaCodes = fields.areaCodes ?? [];
    this.runtime = fields.runtime ?? null;
    this.ticketTypes = fields.ticketTypes ?? [];
  }

  get experienceName(): string | null {
    return experienceLabel(this.experience);
  }

  /**
   * Cheapest standard ticket that admits exactly one person.
   *
   * This is the figure to compare across cinemas: it ignores TWIN and other
   * bundles, which seat two, and ignores concession and promo tickets.
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

  /** Promo tickets TGV has marked as no longer obtainable today. */
  get soldOutPromos(): string[] {
    return this.ticketTypes.filter((t) => t.soldOut).map((t) => t.description);
  }

  /**
   * Cheapest standard single-seat ticket price, in ringgit.
   *
   * This is TGV's advertised price, straight from the API's `priceInCents`.
   * Bundles (TWIN, COUPLE) seat two people, so they are excluded: quoting one
   * here would compare a pair against a single seat. Concession and finished
   * promo tickets are excluded too. `priceBundle` has the pair price if you
   * want it. Without --prices this is null.
   */
  get price(): number | null {
    const single = this.singleSeatPrice;
    if (single !== null) return single;
    const bundles = this.ticketTypes
      .filter((t) => t.price !== null && t.standard)
      .map((t) => t.price as number);
    return bundles.length ? Math.min(...bundles) : null;
  }

  /** Cheapest multi-seat bundle price, if the hall sells one. */
  get priceBundle(): number | null {
    const prices = this.ticketTypes
      .filter((t) => t.price !== null && t.standard && t.seats > 1)
      .map((t) => t.price as number);
    return prices.length ? Math.min(...prices) : null;
  }

  /** 'RM25.00 - RM35.00' when the session sells more than one single price. */
  get priceRange(): string | null {
    const prices = [
      ...new Set(
        this.ticketTypes
          .filter((t) => t.price !== null && t.standard && t.seats === 1)
          .map((t) => t.price as number)
      ),
    ].sort((a, b) => a - b);
    if (!prices.length) return null;
    if (prices.length === 1) return `RM${prices[0].toFixed(2)}`;
    return `RM${prices[0].toFixed(2)} - RM${prices[prices.length - 1].toFixed(2)}`;
  }

  /**
   * Cheapest standard price divided by how many people a ticket seats.
   *
   * TGV sells bundles (TWIN, COUPLE, DOUBLE) alongside plain SINGLE seats,
   * sometimes exclusively. Comparing the raw ticket price across cinemas then
   * compares two people against one, so compare this instead.
   */
  get perPerson(): number | null {
    const prices = this.ticketTypes
      .filter((t) => t.price !== null && t.standard && t.seats)
      .map((t) => (t.price as number) / t.seats);
    return prices.length ? Math.min(...prices) : null;
  }

  get time(): string | null {
    if (!this.start) return null;
    // Shift into Malaysia wall-clock first. A JS Date is an instant, so
    // getUTCHours() on a 15:30 MYT showtime reads 07, nine hours behind what the
    // site lists.
    const myt = new Date(this.start.getTime() + MYT_OFFSET_MS);
    return `${String(myt.getUTCHours()).padStart(2, '0')}:${String(
      myt.getUTCMinutes()
    ).padStart(2, '0')}`;
  }

  /** End time, if this Showtime knows the movie's runtime. */
  get ends(): Date | null {
    if (!this.start || this.runtime === null) return null;
    return new Date(this.start.getTime() + this.runtime * 60000);
  }

  get seatsLeft(): number | null {
    if (this.seatsTotal === null || this.seatsUsed === null) return null;
    return Math.max(0, this.seatsTotal - this.seatsUsed);
  }

  get soldOut(): boolean {
    const left = this.seatsLeft;
    return left !== null && left === 0;
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
      sold_out_promos: this.soldOutPromos,
      ticket_types: this.ticketTypes.map((t) => t.toDict()),
    };
  }
}

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
  /**
   * Names as a list. TGV stores the cast as one comma-joined string, so it is
   * split here rather than left for every caller to split again.
   */
  cast: string[];
  distributor: string | null;
  releaseDate: string | null;
  trailerUrl: string | null;
  posterUrl: string | null;
  bannerUrl: string | null;
  experiences: string[];
  sessionIds: string[]; // the HO codes TGV books against
  showtimes: Showtime[];
  cinemaId: string | null;
  cinemaName: string | null;
  // Fields the other scraper fills in and TGV has nothing for. They stay in
  // the shared output so a consumer reads one shape from either source.
  duration: string | null;
  bookingUrl: string | null;
  bookingId: number | null;

  constructor(fields: {
    id: string;
    title: string;
    slug?: string | null;
    genre?: string[];
    tags?: string[];
    rating?: string | null;
    runtime?: number | null;
    language?: string | null;
    subtitles?: string[];
    synopsis?: string | null;
    director?: string | null;
    cast?: string[] | string | null;
    distributor?: string | null;
    releaseDate?: string | null;
    trailerUrl?: string | null;
    posterUrl?: string | null;
    bannerUrl?: string | null;
    experiences?: string[];
    sessionIds?: string[];
    showtimes?: Showtime[];
    cinemaId?: string | null;
    cinemaName?: string | null;
    duration?: string | null;
    bookingUrl?: string | null;
    bookingId?: number | null;
  }) {
    this.id = fields.id;
    this.title = fields.title;
    this.slug = fields.slug ?? null;
    this.genre = fields.genre ?? [];
    this.tags = fields.tags ?? [];
    this.rating = fields.rating ?? null;
    this.runtime = fields.runtime ?? null;
    this.language = fields.language ?? null;
    this.subtitles = fields.subtitles ?? [];
    this.synopsis = fields.synopsis ?? null;
    this.director = fields.director ?? null;
    // A raw string is still accepted, so a caller building a Movie by hand does
    // not have to split it first.
    this.cast = Array.isArray(fields.cast) ? [...fields.cast] : splitCodes(fields.cast);
    this.distributor = fields.distributor ?? null;
    this.releaseDate = fields.releaseDate ?? null;
    this.trailerUrl = fields.trailerUrl ?? null;
    this.posterUrl = fields.posterUrl ?? null;
    this.bannerUrl = fields.bannerUrl ?? null;
    this.experiences = fields.experiences ?? [];
    this.sessionIds = fields.sessionIds ?? [];
    this.showtimes = fields.showtimes ?? [];
    this.cinemaId = fields.cinemaId ?? null;
    this.cinemaName = fields.cinemaName ?? null;
    this.duration = fields.duration ?? null;
    this.bookingUrl = fields.bookingUrl ?? null;
    this.bookingId = fields.bookingId ?? null;
  }

  get url(): string {
    return this.slug ? `${SITE}/movies/details/${this.slug}` : SITE;
  }

  /** Audio codes as a list, split from the raw `language` string. */
  get languages(): string[] {
    return splitCodes(this.language);
  }

  /** YouTube id from whatever trailer URL shape TGV stored. */
  get trailerId(): string | null {
    if (!this.trailerUrl) return null;
    const match = /(?:embed\/|v=|youtu\.be\/|\/shorts\/)([\w-]{6,})/.exec(this.trailerUrl);
    return match ? match[1] : null;
  }

  get times(): string[] {
    return this.showtimes.map((s) => s.time).filter((t): t is string => Boolean(t));
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

// ---------------------------------------------------------------------------
// Parsers
// ---------------------------------------------------------------------------

/** Build a CDN url for one of a movie's named assets (poster, banner...). */
function assetUrl(movie: MovieRecord, key: string): string | null {
  for (const asset of movie.assets ?? []) {
    if (asset.assetkey !== key) continue;
    const path = asset.extdata?.fileinfo?.filepath;
    if (path) return `${CDN}/${String(path).replace(/^\/+/, '')}`;
  }
  return null;
}

/**
 * Turn a getcinemas payload into Cinema records.
 *
 * `regionLabels` is the optional {region key: label} map from getstatecinema,
 * used to give each outlet a readable region name.
 */
export function parseCinemas(
  payload: { cinemas?: CinemaRecord[] },
  regionLabels: Record<string, string> | null = null
): Cinema[] {
  const labels = regionLabels || {};
  const found: Cinema[] = [];
  const seen = new Set<string>();

  for (const record of payload.cinemas ?? []) {
    const extdata = record.extdata ?? {};
    const info = extdata.cinema ?? {};
    const meta = info.meta ?? {};
    const cid = clean(record.itemkey);
    const name = clean(record.name) || clean(meta.name);
    if (!cid || !name || seen.has(cid)) continue;
    seen.add(cid);
    const location = clean(info.location);
    found.push(
      new Cinema({
        id: cid,
        name,
        slug: clean(info.keyword),
        location,
        region: location ? (labels[location] ?? null) : null,
        address: clean(info.address),
        postcode: clean(info.addressPostcode),
        latitude: clean(info.latitude),
        longitude: clean(info.longitude),
        telephone: clean(info.telephone),
        parking: clean(info.parkinginfo),
        openingHours: asList(info.openingHours).filter(Boolean),
        features: asList(info.features),
        experiences: asList(extdata.exp),
        description: stripHtml(meta.description),
        status: record.status ?? 1,
      })
    );
  }
  return found;
}

/**
 * getstatecinema -> {region key: region label}.
 *
 * The payload lists every region with its cinemas repeated, including one "all"
 * bucket. Only the labels are kept, so a region's label is unambiguous even
 * though its cinema list is not.
 */
export function parseRegions(payload: RegionGroup[]): Record<string, string> {
  const labels: Record<string, string> = {};
  for (const group of payload ?? []) {
    const value = clean(group.value);
    const label = clean(group.label);
    if (value && label && value !== 'all') labels[value] = label;
  }
  return labels;
}

/** Turn one entry of the getcinemas/moviesession payload into a Movie. */
export function parseMovie(record: MovieRecord): Movie {
  const extdata = record.extdata ?? {};
  const info = extdata.movieinfo ?? {};
  let trailer: string | null = info.movieTrailer || null;
  if (trailer && !trailer.includes('youtube.com') && !trailer.includes('youtu.be')) {
    trailer = null; // drop anything that is not a YouTube link
  }
  const runtime = typeof info.runtimemins === 'number' ? info.runtimemins : null;
  return new Movie({
    id: record.recid || '',
    title: clean(record.name) || 'Untitled',
    slug: clean(record.itemkey) || clean(extdata.folderkey),
    genre: asList(info.genre),
    tags: asList(extdata.tags),
    rating: clean(info.rating),
    runtime,
    language: clean(info.lang),
    subtitles: asList(info.subtitlelang),
    synopsis: stripHtml(info.synopsis),
    director: clean(info.director),
    cast: clean(info.cast),
    distributor: clean(info.distributor),
    releaseDate: clean(info.releasedatemy),
    trailerUrl: trailer,
    posterUrl: assetUrl(record, 'poster'),
    bannerUrl: assetUrl(record, 'banner-desktop'),
    experiences: asList(extdata.exp),
    sessionIds: asList(info.hocodes),
  });
}

export function parseShowtime(
  session: SessionRecord,
  cinemaId: string,
  runtime: number | null = null
): Showtime {
  return new Showtime({
    sessionId: String(session.sessionid || ''),
    movieId: session.movieid || '',
    cinemaId: clean(session.cinemaid) || cinemaId,
    start: parseTime(session.showtimemy),
    hall: clean(session.screenname),
    businessDate: clean(session.businessdate),
    experience: clean(session.experience),
    seatTypes: String(session.seattypes || '')
      .split('|')
      .map((t) => t.trim())
      .filter(Boolean),
    status: session.status ?? 1,
    runtime,
    areaCodes: session.extdata?.areacategorycodes ?? [],
  });
}

/**
 * gettickets -> the ticket types on sale for one session.
 *
 * Price comes from the API's `priceInCents` integer-cent field; `taxInCents` is
 * deliberately not read (see TicketType). `isAvailableForLoyaltyMembersOnly` and
 * friends are not filtered on: TGV already excludes what a walk-in cannot buy.
 */
export function parseTickets(payload: TicketResults): TicketType[] {
  const found: TicketType[] = [];
  for (const row of payload.tickets ?? []) {
    // Only priceInCents is read. taxInCents is skipped on purpose: see
    // TicketType for why it does not reconcile with any Malaysian rate.
    const price = row.priceInCents;
    found.push(
      new TicketType({
        code: String(row.ticketTypeCode || ''),
        description: clean(row.description) || 'TICKET',
        priceCents: typeof price === 'number' && Number.isInteger(price) ? price : null,
        childOnly: Boolean(row.isChildOnlyTicket),
        allocatedSeating: Boolean(row.isAllocatableSeating),
        package: Boolean(row.isPackageTicket),
        areaCode: clean(row.areaCategoryCode),
        soldOut: Boolean(
          SOLD_OUT_RE.test(row.longDescription || '') || SOLD_OUT_RE.test(row.description || '')
        ),
        standard: STANDARD_TICKETS.has((clean(row.description) || '').toUpperCase()),
      })
    );
  }
  return found;
}

/** getseatstatus -> Map of session id -> {seatsTotal, seatsUsed}. */
export function parseSeatStatus(payload: SeatStatusResults): Map<string, SeatCounts> {
  const out = new Map<string, SeatCounts>();
  for (const row of payload.seatstatuslist ?? []) {
    if (row.sessionid === null || row.sessionid === undefined) continue;
    out.set(String(row.sessionid), {
      seatsTotal: row.seatstotal || 0,
      seatsUsed: row.seatsused || 0,
    });
  }
  return out;
}

export interface SeatCounts {
  seatsTotal: number;
  seatsUsed: number;
}

// The API reports a seat's state as a bare integer. These are the values seen in
// the wild; anything else is rendered as `?` rather than guessed at, because a
// mislabelled seat is worse than an unreadable one. `state` normalises them to
// the same letters the other scraper uses, so one renderer reads both.
export const SEAT_FREE = 0;
export const SEAT_BOOKED = 1;
export const SEAT_STATE: Record<number, string> = { [SEAT_FREE]: 'A', [SEAT_BOOKED]: 'B' };
export const SEAT_UNKNOWN = '?';

export class Seat {
  /**
   * One seat in a hall.
   *
   * `row` and `column` are the API's own zero-based grid coordinates, which is
   * what carries the hall's shape: columns with no seat in them are aisles, and
   * dropping them is what makes a grid look one seat wider on some rows than on
   * others. `id` is the number TGV prints on the booking page.
   */
  id: string;
  row: number;
  column: number;
  status: number;
  priority: number;
  seatStyle: number;
  seatsInGroup: unknown[];
  /**
   * The hall area this seat is in, as its name: 'SINGLE', 'TWIN'. TGV prices
   * per area and never sends the seat's own category, so the area is the closest
   * thing to the type the other scraper reports.
   */
  seatType: string | null;

  constructor(fields: {
    id: string;
    row: number;
    column: number;
    status?: number;
    priority?: number;
    seatStyle?: number;
    seatsInGroup?: unknown[];
    seatType?: string | null;
  }) {
    this.id = fields.id;
    this.row = fields.row;
    this.column = fields.column;
    this.status = fields.status ?? SEAT_FREE;
    this.priority = fields.priority ?? 0;
    this.seatStyle = fields.seatStyle ?? 0;
    this.seatsInGroup = fields.seatsInGroup ?? [];
    this.seatType = fields.seatType ?? null;
  }

  /** The seat's printed number, or null when the id is not numeric. */
  get number(): string | null {
    return seatNumberStr(this);
  }

  /** Normalised state: 'A' free, 'B' booked, '?' unknown. */
  get state(): string {
    return SEAT_STATE[this.status] ?? SEAT_UNKNOWN;
  }

  get free(): boolean {
    return this.state === 'A';
  }

  get booked(): boolean {
    return this.state === 'B';
  }

  toDict(): Record<string, unknown> {
    return {
      id: this.id,
      number: this.number,
      row: this.row,
      column: this.column,
      status: this.status,
      state: this.state,
      category: this.seatStyle,
      seat_type: this.seatType,
      free: this.free,
      booked: this.booked,
    };
  }
}

export class SeatRow {
  /** One row of a hall area. `index` is the API's rowIndexZeroBased. */
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
    return { index: this.index, name: this.name, seats: this.seats.map((s) => s.toDict()) };
  }
}

export class SeatArea {
  /**
   * One priced part of a hall, e.g. SINGLE or TWIN.
   *
   * A hall sells several kinds of seat at different prices, and TGV returns each
   * as its own area with its own layout. The areas share one coordinate space,
   * so their columns line up and can be drawn against a single ruler.
   */
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
    return this.description || this.code || `area ${this.number}`;
  }

  /**
   * Rows that actually hold seats, in API order.
   *
   * Every area reports a full `rowCount`, but the rows of a second area come
   * back as empty placeholders with no name. Drawing those produces blank lines
   * and a phantom row count, so they are dropped here.
   */
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

export class SeatPlan {
  /** The whole hall for one session: every area, and the screen's position. */
  cinemaId: string | null;
  sessionId: string | null;
  /** TGV does not send the hall's own id, so this stays null. */
  hallId: string | null;
  hallName: string | null;
  areas: SeatArea[];
  boundaryLeft: number;
  boundaryRight: number;
  screenStart: number;
  screenWidth: number;
  /** How many seats alignByNumber had to move. 0 on a plan it did not touch. */
  movedByAlignment: number;

  constructor(fields: {
    cinemaId?: string | null;
    sessionId?: string | null;
    hallId?: string | null;
    hallName?: string | null;
    areas?: SeatArea[];
    boundaryLeft?: number;
    boundaryRight?: number;
    screenStart?: number;
    screenWidth?: number;
    movedByAlignment?: number;
  }) {
    this.cinemaId = fields.cinemaId ?? null;
    this.sessionId = fields.sessionId ?? null;
    this.hallId = fields.hallId ?? null;
    this.hallName = fields.hallName ?? null;
    this.areas = fields.areas ?? [];
    this.boundaryLeft = fields.boundaryLeft ?? 0;
    this.boundaryRight = fields.boundaryRight ?? 0;
    this.screenStart = fields.screenStart ?? 0;
    this.screenWidth = fields.screenWidth ?? 0;
    this.movedByAlignment = fields.movedByAlignment ?? 0;
  }

  /**
   * Leftmost column any area uses. Shared across areas, so one ruler fits every
   * grid on the page.
   */
  get firstColumn(): number {
    const columns = this.areas.flatMap((a) => a.seats.map((s) => s.column));
    return columns.length ? Math.min(...columns) : 0;
  }

  get lastColumn(): number {
    const columns = this.areas.flatMap((a) => a.seats.map((s) => s.column));
    return columns.length ? Math.max(...columns) : 0;
  }

  get booked(): Seat[] {
    return this.areas.flatMap((a) => a.booked);
  }

  get free(): Seat[] {
    return this.areas.flatMap((a) => a.free);
  }

  /**
   * Every row of the hall, screen end first, paired with its area.
   *
   * TGV numbers rows from the back of the hall towards the screen, and the
   * payload proves it three ways: the indices form one contiguous run shared by
   * all areas rather than restarting per area; the TWIN area, which is sold at
   * the back, holds the low indices; and at index 0 the row is named `M` while
   * at the highest index it is named `A`.
   *
   * So descending index puts the screen end first, and the row names then read
   * A, B, C downwards on their own, with the TWIN rows falling at the bottom
   * where the low indices put them. Ordering by the raw index instead draws the
   * hall upside down.
   */
  get free(): Seat[] {
    return this.areas.flatMap((a) => a.free);
  }

  get booked(): Seat[] {
    return this.areas.flatMap((a) => a.booked);
  }

  get seats(): Seat[] {
    return this.areas.flatMap((a) => a.seats);
  }

  /**
   * Seats in the hall, from the area counts the payload declares.
   *
   * The area figure is used rather than counting the grid because the payload
   * reports `numberOfSeats` per area and the rows are padded: the same count the
   * other scraper reports as `null` on an empty plan.
   */
  get seatsTotal(): number | null {
    const counted = this.areas.map((a) => a.seatCount).filter((n) => n > 0);
    return counted.length ? counted.reduce((a, b) => a + b, 0) : null;
  }

  /** Seats still sellable in this hall, counted off the grid. */
  get seatsLeft(): number | null {
    const counts = this.areas.filter((a) => a.seats.length).map((a) => a.free.length);
    return counts.length ? counts.reduce((a, b) => a + b, 0) : null;
  }

  get seatsBooked(): number | null {
    const total = this.seatsTotal;
    return total === null ? null : this.booked.length;
  }

  /**
   * Always 0: TGV has no blocked state to count.
   *
   * The other scraper's grid marks seats `D`, so it reports a real figure here;
   * this stays 0 rather than null so one consumer can add the two up.
   */
  get seatsBlocked(): number {
    return 0;
  }

  /**
   * {area name: seats still free in that area}, e.g. `{'SINGLE': 154}`.
   *
   * TGV sends no per-category availability block, so this is counted off the grid
   * rather than read. It is the same idea as the other scraper's seat-category
   * breakdown.
   */
  get available(): Record<string, number> {
    const out: Record<string, number> = {};
    for (const area of this.areas) {
      if (area.seats.length) out[area.label] = area.free.length;
    }
    return out;
  }

  /** TGV sends no seat category table. */
  get categories(): Record<string, { name: string | null; seatsTaken: number }> {
    return {};
  }

  get hallRows(): [SeatArea, SeatRow][] {
    const rows = this.areas.flatMap((area) => area.occupiedRows.map((row) => [area, row] as [SeatArea, SeatRow]));
    rows.sort((a, b) => b[1].index - a[1].index);
    return rows;
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
      available: this.available,
      areas: this.areas.map((a) => a.toDict()),
    };
  }
}

/** The seat's printed number, or null if the id is not a plain integer. */
export function seatNumber(seat: Seat): number | null {
  const text = String(seat.id).trim();
  return /^\d+$/.test(text) ? Number(text) : null;
}

/**
 * `seatNumber` as the printed string, or null.
 *
 * The other scraper's seats carry the printed label as a string already, so
 * both expose it under one name and one type.
 */
export function seatNumberStr(seat: Seat): string | null {
  const number = seatNumber(seat);
  return number === null ? null : String(number);
}

/**
 * seat number -> column, taken from the fullest row of this area.
 *
 * The fullest row is the one that spans the hall, so it fixes where each number
 * sits. Ties go to the row nearest the screen, which is arbitrary but
 * deterministic. An empty area has no anchor and gets an empty map.
 */
export function numberAnchor(area: SeatArea): Map<number, number> {
  const rows = area.occupiedRows;
  if (!rows.length) return new Map();
  const fullest = rows.reduce((best, row) =>
    row.seats.length > best.seats.length ||
    (row.seats.length === best.seats.length && row.index < best.index)
      ? row
      : best
  );
  const anchor = new Map<number, number>();
  for (const seat of fullest.seats) {
    const number = seatNumber(seat);
    if (number !== null && !anchor.has(number)) anchor.set(number, seat.column);
  }
  return anchor;
}

/**
 * Return a copy where each seat's column comes from its number.
 *
 * TGV gives every row its own columns, and in a row with fewer seats the
 * numbering packs against one end, so the same number lands in a different
 * column in different rows. Real halls do not work that way: seat 15 is in the
 * same place in every row, and a row short of one seat simply has an empty
 * space where the missing number would be.
 *
 * So this rebuilds each area from its own number-to-column map. Seats whose
 * number is missing from the anchor, or that the API labels non-numerically, keep
 * the column they arrived with.
 */
export function alignByNumber(plan: SeatPlan): SeatPlan {
  let moved = 0;
  const areas: SeatArea[] = [];
  for (const area of plan.areas) {
    const anchor = numberAnchor(area);
    const rows: SeatRow[] = [];
    for (const row of area.rows) {
      const seats: Seat[] = [];
      const taken = new Set<number>();
      for (const seat of row.seats) {
        const number = seatNumber(seat);
        let column = number === null ? undefined : anchor.get(number);
        if (column === undefined || taken.has(column)) {
          // Unknown number, or this row already has a seat in that column: keep
          // the seat where the API put it rather than stacking two seats in one
          // place.
          column = seat.column;
          if (taken.has(column)) column = -1;
        } else {
          if (column !== seat.column) moved += 1;
          taken.add(column);
        }
        seats.push(
          new Seat({
            id: seat.id,
            row: seat.row,
            column,
            status: seat.status,
            priority: seat.priority,
            seatStyle: seat.seatStyle,
            seatsInGroup: seat.seatsInGroup,
            seatType: seat.seatType,
          })
        );
      }
      rows.push(new SeatRow({ index: row.index, name: row.name, seats }));
    }
    areas.push(
      new SeatArea({
        number: area.number,
        code: area.code,
        description: area.description,
        seatCount: area.seatCount,
        rowCount: area.rowCount,
        columnCount: area.columnCount,
        rows,
      })
    );
  }
  const aligned = new SeatPlan({
    cinemaId: plan.cinemaId,
    sessionId: plan.sessionId,
    areas,
    boundaryLeft: plan.boundaryLeft,
    boundaryRight: plan.boundaryRight,
    screenStart: plan.screenStart,
    screenWidth: plan.screenWidth,
  });
  aligned.movedByAlignment = moved;
  return aligned;
}

/**
 * getseatplan -> a SeatPlan.
 *
 * The endpoint validates its body strictly: `sessionid` must be a bare string
 * (getseatstatus takes an array instead), and `req` must be present. Rows arrive
 * as a fixed-length list padded with nameless empty entries, and each seat
 * carries its own grid position, so the aisle columns are recoverable.
 */
export function parseSeatPlan(payload: SeatPlanResults): SeatPlan {
  const layout = payload.seatlayout ?? {};
  const areas: SeatArea[] = [];

  for (const area of layout.areas ?? []) {
    const rows: SeatRow[] = [];
    // 'SINGLE', 'TWIN'... rather than the opaque category code, which is what a
    // seat's type is worth to a caller.
    const areaLabel = clean(area.description) ?? clean(area.areaCategoryCode);
    for (const row of area.rows ?? []) {
      const seats = (row.seats ?? [])
        .filter((seat) => seat.id !== null && seat.id !== undefined)
        .map(
          (seat) =>
            new Seat({
              id: String(seat.id),
              row: seat.position?.rowIndex ?? row.rowIndexZeroBased ?? 0,
              column: seat.position?.columnIndex ?? 0,
              status: seat.status ?? SEAT_FREE,
              priority: seat.priority ?? 0,
              seatStyle: seat.seatStyle ?? 0,
              seatsInGroup: seat.seatsInGroup ?? [],
              seatType: areaLabel,
            })
        );
      rows.push(
        new SeatRow({
          index: row.rowIndexZeroBased ?? 0,
          name: clean(row.physicalName),
          seats,
        })
      );
    }

    areas.push(
      new SeatArea({
        number: area.number ?? 0,
        code: clean(area.areaCategoryCode),
        description: clean(area.description),
        seatCount: area.numberOfSeats ?? 0,
        rowCount: area.rowCount ?? 0,
        columnCount: area.columnCount ?? 0,
        rows: rows.sort((a, b) => a.index - b.index),
      })
    );
  }

  return new SeatPlan({
    cinemaId: clean(payload.cinemaid),
    sessionId: clean(payload.sessionid),
    areas,
    boundaryLeft: layout.boundaryLeft ?? 0,
    boundaryRight: layout.boundaryRight ?? 0,
    screenStart: layout.screenStart ?? 0,
    screenWidth: layout.screenWidth ?? 0,
  });
}

/**
 * Turn one moviesession_get payload into movies carrying their sessions.
 *
 * The payload nests the same film twice: a detail record under
 * `results.movies`, and a bare id under
 * `results.businessday.cinemas[].movies[]` next to the sessions. They are joined
 * on the movie id.
 */
export function parseDay(
  payload: DayResults,
  cinemaId: string,
  cinemaName: string | null = null
): Movie[] {
  const details = new Map<string, MovieRecord>();
  for (const record of payload.movies ?? []) {
    if (record.recid) details.set(record.recid, record);
  }

  const movies: Movie[] = [];
  const seen = new Set<string>();

  for (const cinema of payload.businessday?.cinemas ?? []) {
    const cid = clean(cinema.cinemaid) || cinemaId;
    const name = clean(cinema.name) || cinemaName;

    for (const entry of cinema.movies ?? []) {
      const movieId = entry.movieid || '';
      // Sessions with no detail record still deserve to show up.
      const record = details.get(movieId) ?? { recid: movieId, name: movieId };
      const movie = parseMovie(record);

      const showtimes: Showtime[] = [];
      for (const group of entry.experiences ?? []) {
        for (const session of group.sessions ?? []) {
          showtimes.push(parseShowtime(session, cid, movie.runtime));
        }
      }
      showtimes.sort(showtimeOrder);

      movie.cinemaId = cid;
      movie.cinemaName = name;
      movie.showtimes = showtimes;

      if (seen.has(movieId)) {
        // Same film listed twice under one cinema, e.g. two blocs.
        const existing = movies.find((m) => m.id === movieId);
        if (existing) {
          existing.showtimes = [...existing.showtimes, ...showtimes].sort(showtimeOrder);
        }
        continue;
      }
      seen.add(movieId);
      movies.push(movie);
    }
  }

  movies.sort((a, b) => a.title.toLowerCase().localeCompare(b.title.toLowerCase()));
  return movies;
}

/** Fold getseatstatus numbers onto the showtimes, in place. */
export function applySeatStatus(movies: Movie[], seats: Map<string, SeatCounts>): Movie[] {
  for (const movie of movies) {
    for (const showtime of movie.showtimes) {
      const found = seats.get(showtime.sessionId);
      if (found) {
        showtime.seatsTotal = found.seatsTotal;
        showtime.seatsUsed = found.seatsUsed;
      }
    }
  }
  return movies;
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

/** {region key: region label} for every TGV region. */
export async function regions(options: NetOptions = {}): Promise<Record<string, string>> {
  const net = netOptions(options);
  const body = await request<RegionGroup[]>(ENDPOINTS.regions, null, net);
  return parseRegions(results(body, 'regions'));
}

/**
 * Every TGV outlet in Malaysia.
 *
 * withDetails=true adds addresses, coordinates and formats, which costs one more
 * request for the region labels. withDetails=false returns the lighter id and
 * name list.
 */
export async function cinemas(
  { withDetails = true, ...options }: NetOptions & { withDetails?: boolean } = {}
): Promise<Cinema[]> {
  const net = netOptions(options);

  if (!withDetails) {
    const body = await request<RegionGroup[]>(ENDPOINTS.regions, null, net);
    const found = new Map<string, Cinema>();
    for (const group of results(body, 'regions')) {
      for (const record of group.cinemas ?? []) {
        const cid = clean(record.cinemaid);
        if (cid && !found.has(cid)) {
          found.set(cid, new Cinema({ id: cid, name: clean(record.name) || cid }));
        }
      }
    }
    return [...found.values()].sort((a, b) => a.name.localeCompare(b.name));
  }

  const body = await request<{ cinemas?: CinemaRecord[] }>(
    ENDPOINTS.cinemas,
    { startrowindex: 0, maxrows: 500 },
    net
  );
  const regionBody = await request<RegionGroup[]>(ENDPOINTS.regions, null, net);
  const found = parseCinemas(
    results(body, 'cinemas'),
    parseRegions(results(regionBody, 'regions'))
  );
  return found.sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * Every date the API will answer for, ascending.
 *
 * This is the set of business dates across all cinemas, so it includes a few
 * special and advance dates that have no sessions today. Check with `day()`
 * before trusting a date to have anything on it.
 */
export async function businessDates(options: NetOptions = {}): Promise<string[]> {
  const net = netOptions(options);
  const body = await request<BusinessDateResults>(ENDPOINTS.businessDates, {}, net);
  return [...(results(body, 'business_dates').businessdates ?? [])].sort();
}

/** One getseatstatus call covering every session in the batch. */
async function seatStatusFor(
  cinemaId: string,
  movies: Movie[],
  net: Required<NetOptions>
): Promise<Map<string, SeatCounts>> {
  const ids = movies.flatMap((m) => m.showtimes.map((s) => s.sessionId)).filter(Boolean);
  if (!ids.length) return new Map();
  const body = await request<SeatStatusResults>(
    ENDPOINTS.seatStatus,
    { cinemaid: cinemaId, sessionid: ids },
    net
  );
  return parseSeatStatus(results(body, 'seat_status'));
}

/**
 * Every movie and every session at one cinema on one date.
 *
 * seats adds remaining-seat counts (one extra call). prices adds ticket prices
 * (one extra call per session per hall area, so it is opt-in).
 *
 * `after`, `before` and `formats` narrow the result. They are applied to the
 * parsed day **before** seats and prices are fetched, not after. That ordering is
 * the whole point: a price costs one request per session per area, so filtering
 * afterwards still pays for every screening you just threw away.
 */
export async function day(
  cinemaId: string,
  on: string | Date | null = null,
  {
    seats = false,
    prices = false,
    after = null,
    before = null,
    formats = null,
    workers = 8,
    ...options
  }: NetOptions & {
    seats?: boolean;
    prices?: boolean;
    after?: string | null;
    before?: string | null;
    formats?: string | null;
    workers?: number;
  } = {}
): Promise<Movie[]> {
  const net = netOptions(options);
  const when = normaliseDate(on);
  const cid = String(cinemaId).trim().toUpperCase();

  const body = await request<DayResults>(
    ENDPOINTS.day,
    { cinemaid: cid, businessdate: when },
    net
  );
  let movies = parseDay(results(body, 'day'), cid);
  movies = filterFormats(filterTimes(movies, after, before), formats);
  if (seats) applySeatStatus(movies, await seatStatusFor(cid, movies, net));
  if (prices) await attachPrices(movies, net, workers);
  return movies;
}

/**
 * Every ticket type on sale for one session in one hall area.
 *
 * The lowest-level pricing call: a single `gettickets` request, no batching and
 * no filtering. TGV takes one session id and one area code at a time, so a
 * session with several hall areas needs one call per area.
 *
 *     await tickets('BU0', '327053', '0000000002', '2026-10-18')
 *
 * Prefer `sessionPrices()` unless you know the area code already; it loops the
 * areas for you and hands back a priced `Showtime`.
 */
export async function tickets(
  cinemaId: string,
  sessionId: string,
  areaCode: string,
  businessDate: string | Date | null = null,
  options: NetOptions = {}
): Promise<TicketType[]> {
  const net = netOptions(options);
  const body = await request<TicketResults>(
    ENDPOINTS.tickets,
    {
      cinemaid: String(cinemaId).trim().toUpperCase(),
      sessionid: String(sessionId),
      businessdate: normaliseDate(businessDate),
      req: 'x',
      areacategorycodes: areaCode,
    },
    net
  );
  return parseTickets(results(body, 'tickets')).map((ticket) => {
    ticket.areaCode = areaCode;
    return ticket;
  });
}

/**
 * Price one screening and return it.
 *
 * This is the on-demand path: you pick the session, this prices that session and
 * nothing else. `day(..., { prices: true })` and friends are the bulk path and
 * loop this logic over every showtime they kept.
 *
 *     const films = await day('BU0', '2026-10-18');
 *     const late = films.flatMap((f) => f.showtimes).find((s) => s.time === '21:45');
 *     (await sessionPrices(late)).price       // 30
 *
 * Mutates and returns the showtime, so `showtime.price` reads the same
 * afterwards. The result is the cheapest standard single seat; `priceBundle` has
 * the pair price if the hall sells one.
 */
export async function sessionPrices(
  showtime: Showtime,
  options: NetOptions = {}
): Promise<Showtime> {
  const net = netOptions(options);
  const collected: TicketType[] = [];
  for (const code of showtime.areaCodes) {
    if (!showtime.sessionId) continue;
    try {
      collected.push(
        ...(await tickets(showtime.cinemaId, showtime.sessionId, code, showtime.businessDate, net))
      );
    } catch (exc) {
      // One dead area must not cost the session its other areas, which may hold
      // the single seat. Reported, then move on.
      if (!(exc instanceof TgvError)) throw exc;
      console.error(`tgv: price lookup failed, ${showtime.time} area ${code}: ${exc.message}`);
    }
  }
  showtime.ticketTypes = collected;
  return showtime;
}

/**
 * Fill in ticket prices on every showtime, in place.
 *
 * Prices only get fetched for the showtimes still in `movies`, so a narrowed
 * query (`--from`, `--until`, `--format`, or one film at one cinema) costs only
 * the showtimes it kept. Filter first, then call this.
 *
 * Costs one request per session per hall area, which is why it is opt-in. Those
 * go out in parallel: the throttle still holds the *global* rate, so the request
 * count is unchanged and a run stops being serial.
 *
 * A failed call does not take the run down with it. That showtime is left without
 * a price and reported on stderr, so it prints as a missing price rather than a
 * missing screening.
 */
async function attachPrices(
  movies: Movie[],
  net: Required<NetOptions>,
  workers = 8
): Promise<void> {
  const jobs = movies
    .flatMap((film) => film.showtimes)
    .filter((showtime) => showtime.sessionId && showtime.areaCodes.length);
  if (!jobs.length) return;

  const failed: string[] = [];
  // sessionPrices writes straight onto the showtime, so nothing is merged back
  // here; a showtime whose call failed is left with no ticket types, which
  // renders as a missing price rather than a missing screening.
  await poolMap(jobs, workers, async (showtime) => {
    try {
      await sessionPrices(showtime, net);
    } catch (exc) {
      // request() reports network and HTTP problems as TgvError. Losing one
      // area's price list must not abort the rest of the run.
      if (!(exc instanceof TgvError)) throw exc;
      failed.push(`${showtime.time}: ${exc.message}`);
    }
  });

  for (const problem of failed) console.error(`tgv: price lookup failed, ${problem}`);
}

/**
 * {cinema id: movies} for one date across many cinemas.
 *
 * Each cinema is one request, so a national run is 45 calls. They go out in
 * parallel but are still rate limited by the throttle. prices=true adds one more
 * request per session per area on top, which is a lot, so it is off by default.
 *
 * `after`, `before` and `formats` are applied per cinema before seats and prices
 * are fetched, so a filtered run never pays for screenings it drops.
 */
export async function schedule(
  cinemaIds: string[] | null = null,
  on: string | Date | null = null,
  {
    seats = false,
    prices = false,
    workers = 4,
    after = null,
    before = null,
    formats = null,
    ...options
  }: NetOptions & {
    seats?: boolean;
    prices?: boolean;
    workers?: number;
    after?: string | null;
    before?: string | null;
    formats?: string | null;
  } = {}
): Promise<Record<string, Movie[]>> {
  const net = netOptions(options);
  const when = normaliseDate(on);

  const ids =
    cinemaIds === null
      ? (await cinemas({ withDetails: false, ...net })).map((c) => c.id)
      : cinemaIds.map((c) => String(c).trim().toUpperCase());

  const found = await poolMap(ids, workers, async (cid): Promise<[string, Movie[]]> => {
    const body = await request<DayResults>(
      ENDPOINTS.day,
      { cinemaid: cid, businessdate: when },
      net
    );
    let movies = parseDay(results(body, 'day'), cid);
    movies = filterFormats(filterTimes(movies, after, before), formats);
    if (seats) applySeatStatus(movies, await seatStatusFor(cid, movies, net));
    if (prices) await attachPrices(movies, net, workers);
    return [cid, movies];
  });

  const byCinema = new Map(found);
  return Object.fromEntries(
    ids.filter((cid) => byCinema.get(cid)?.length).map((cid) => [cid, byCinema.get(cid) as Movie[]])
  );
}

/**
 * Every film with sessions on a date, one record per film.
 *
 * The API has no catalogue endpoint: it only reports films through the sessions
 * they have. So this is the union of one date's schedules, which costs one
 * request per cinema, and unlike `day()` it merges a film seen in several
 * cinemas into one record with every session on it.
 *
 * Pass `cinemaIds` to narrow the sweep.
 */
export async function movies(
  on: string | Date | null = null,
  cinemaIds: string[] | null = null,
  options: NetOptions = {}
): Promise<Movie[]> {
  const everything = await schedule(cinemaIds, on, options);
  const merged = new Map<string, Movie>();
  for (const films of Object.values(everything)) {
    for (const film of films) {
      const existing = merged.get(film.id);
      if (!existing) {
        // Keep one record per film, but remember it is a national run.
        film.cinemaId = null;
        film.cinemaName = null;
        merged.set(film.id, film);
        continue;
      }
      existing.showtimes = [...existing.showtimes, ...film.showtimes].sort(showtimeOrder);
      existing.experiences = [...new Set([...existing.experiences, ...film.experiences])];
    }
  }
  const out = [...merged.values()];
  for (const film of out) {
    film.sessionIds = film.showtimes.map((s) => s.sessionId).filter(Boolean);
  }
  out.sort((a, b) => a.title.toLowerCase().localeCompare(b.title.toLowerCase()));
  return out;
}

/**
 * One film, as a single record.
 *
 * TGV has no detail page separate from the schedule, so this is the film
 * `movie()` finds first, with its sessions and every field the API carries.
 * The other scraper needs a second call here because its service has no
 * synopsis, director or cast; TGV has all of them in the session payload.
 */
export async function detail(
  slugOrTitle: string,
  on: string | Date | null = null,
  options: NetOptions = {}
): Promise<Movie> {
  const found = await movie(slugOrTitle, on, options);
  if (!found.length) {
    throw new TgvError(`no sessions for ${slugOrTitle} on ${normaliseDate(on)}`);
  }
  return found[0][1];
}

/**
 * The hall formats one cinema offers, from the outlet directory.
 *
 * TGV publishes these on the cinema record itself, so this is one request for
 * the directory. The other scraper has to read them off a day payload.
 */
export async function cinemaExperiences(
  cinemaId: string,
  options: NetOptions = {}
): Promise<string[]> {
  const outlets = await cinemas(options);
  return findCinema(outlets, cinemaId).experiences;
}

export interface MovieHit {
  cinemaId: string;
  movie: Movie;
}

/**
 * Every session of one film, across cinemas, as [{cinemaId, movie}].
 *
 * The API has no movie-centric session lookup that returns times, so this walks
 * the same cinema-by-cinema day payload and keeps the matches.
 *
 * Pass `cinemaIds` to stay on one or a few outlets. Left as null it sweeps every
 * cinema in the country, and with `prices: true` that is a price lookup per
 * session per area at all 45 of them.
 */
export async function movie(
  slugOrTitle: string,
  on: string | Date | null = null,
  {
    seats = false,
    prices = false,
    cinemaIds = null,
    workers = 4,
    after = null,
    before = null,
    formats = null,
    ...options
  }: NetOptions & {
    seats?: boolean;
    prices?: boolean;
    cinemaIds?: string[] | null;
    workers?: number;
    after?: string | null;
    before?: string | null;
    formats?: string | null;
  } = {}
): Promise<MovieHit[]> {
  const net = netOptions(options);
  const wanted = String(slugOrTitle).trim().toLowerCase().replace(/ /g, '-');
  const title = String(slugOrTitle).trim().toLowerCase();
  const when = normaliseDate(on);

  const ids =
    cinemaIds === null
      ? (await cinemas({ withDetails: false, ...net })).map((c) => c.id)
      : cinemaIds.map((c) => String(c).trim().toUpperCase());

  const found: MovieHit[] = [];
  await poolMap(ids, workers, async (cid) => {
    const body = await request<DayResults>(
      ENDPOINTS.day,
      { cinemaid: cid, businessdate: when },
      net
    );
    for (const film of parseDay(results(body, 'day'), cid)) {
      if (!(film.slug === wanted || film.title.toLowerCase() === title)) continue;
      // Narrow before enriching: a price call per session per area is the single
      // most expensive thing this script does. Both filters rewrite
      // film.showtimes in place as well as returning a list.
      filterFormats(filterTimes([film], after, before), formats);
      if (!film.showtimes.length) continue;
      if (seats) applySeatStatus([film], await seatStatusFor(cid, [film], net));
      if (prices) await attachPrices([film], net, workers);
      found.push({ cinemaId: cid, movie: film });
    }
  });

  if (!found.length) {
    const hint = after || before || formats ? ' with those --from/--until/--format filters' : '';
    die(`no sessions for "${slugOrTitle}" on ${when}${hint}`);
  }
  return found;
}

/** Raw {session id: {seats_total, seats_used, seats_left, used_percentage}}. */
export async function seatStatus(
  cinemaId: string,
  sessionIds: string[],
  options: NetOptions = {}
): Promise<Record<string, Record<string, number | null>>> {
  const net = netOptions(options);
  const body = await request<SeatStatusResults>(
    ENDPOINTS.seatStatus,
    { cinemaid: String(cinemaId).trim().toUpperCase(), sessionid: sessionIds.map(String) },
    net
  );
  const out: Record<string, Record<string, number | null>> = {};
  for (const row of results(body, 'seat_status').seatstatuslist ?? []) {
    out[String(row.sessionid)] = {
      seats_total: row.seatstotal ?? null,
      seats_used: row.seatsused ?? null,
      seats_left: Math.max(0, (row.seatstotal || 0) - (row.seatsused || 0)),
      used_percentage: row.usedpercentage ?? null,
    };
  }
  return out;
}

/**
 * The full seat map for one session, aisle positions included.
 *
 * `getseatstatus` answers "how many are left"; this answers "which ones". One
 * call per session, and it is the only way to see the actual grid, because the
 * day payload carries counts but never seat coordinates.
 *
 *     const films = await day('BU0', '2026-10-18');
 *     const late = films.flatMap((f) => f.showtimes).at(-1);
 *     const plan = await seatPlan('BU0', late);
 *     console.log(renderSeatPlan(plan));
 *
 * The body is stricter than the other endpoints: `sessionid` is a bare string
 * here, not the array `getseatstatus` wants, and `req` must be sent or the API
 * answers 400 with two validation errors at once.
 */
export async function seatPlan(
  cinemaId: string,
  showtime: Showtime | string,
  options: NetOptions = {}
): Promise<SeatPlan> {
  const net = netOptions(options);
  const bare = typeof showtime === 'string';
  const sessionId = bare ? showtime : showtime.sessionId;
  if (!sessionId) die('seatPlan needs a session id');
  const body = await request<SeatPlanResults>(
    ENDPOINTS.seatPlan,
    {
      cinemaid: String(cinemaId).trim().toUpperCase(),
      sessionid: String(sessionId),
      businessdate: normaliseDate(bare ? null : showtime.businessDate),
      req: 'x',
    },
    net
  );
  const plan = parseSeatPlan(results(body, 'seat_plan'));
  // The plan payload names no hall, so the showtime the caller already has is
  // where the hall's name comes from.
  if (!bare) plan.hallName = showtime.hall;
  return plan;
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

const SEAT_MARKS: Record<number, string | null> = { [SEAT_FREE]: null, [SEAT_BOOKED]: 'X' };

/**
 * One fixed-width cell. Blank for an aisle, the seat number when free, `X` when
 * taken. The width is constant so every column lines up down the grid.
 */
function seatCell(seat: Seat | undefined, width: number): string {
  if (!seat) return ' '.repeat(width);
  const mark = Object.hasOwn(SEAT_MARKS, seat.status) ? SEAT_MARKS[seat.status] : '?';
  return String(mark || seat.id).padStart(width);
}

/**
 * Draw a hall as fixed-width text.
 *
 * With `align`, seat numbers are laid out on a single column map per area, the
 * way a real hall is lettered, so a short row shows a gap where its missing
 * number would be instead of sliding every number sideways. Pass false to see
 * the raw per-row columns TGV returned instead.
 *
 * Every cell is `width` characters, and a column with no seat in it is left blank
 * rather than closed up. That is the whole point: TGV's rows genuinely differ in
 * length (a short front row, aisles mid-hall), and packing the numbers together
 * makes a wide row look like it has an extra seat. Rendering the column
 * positions is what makes the aisles visible.
 */
export function renderSeatPlan(
  plan: SeatPlan,
  { width = 3, ruler = true, align = true }: { width?: number; ruler?: boolean; align?: boolean } = {}
): string {
  const drawn = align ? alignByNumber(plan) : plan;
  const first = drawn.firstColumn;
  const last = drawn.lastColumn;
  const columns: number[] = [];
  for (let c = first; c <= last; c += 1) columns.push(c);
  const labelWidth =
    Math.max(
      1,
      ...drawn.areas.flatMap((a) => a.rows.map((row) => (row.name || '').length))
    ) + 2;

  const out: string[] = [];
  if (ruler && columns.length) {
    out.push(' '.repeat(labelWidth) + columns.map((c) => String(c).padStart(width)).join(''));
    // Ticks are as wide as the label they sit under and right-aligned to match
    // it. A fixed-width dash fills the whole cell and lands one character left of
    // the digit, which reads as every column being shifted.
    out.push(
      (' '.repeat(labelWidth) +
        columns.map((c) => '-'.repeat(String(c).length).padStart(width)).join('')
      ).replace(/\s+$/, '')
    );
  }

  const screen = '='.repeat(labelWidth + columns.length * width);
  out.push((''.padStart(Math.max(0, screen.length - 1)) + 'SCREEN').padEnd(Math.max(0, screen.length - 6)));

  const drawnFor = new Map<number, number>();
  for (const [area, row] of drawn.hallRows) {
    if (!drawnFor.has(area.number)) {
      const taken = area.booked.length;
      drawnFor.set(area.number, 0);
      if (out.length && out[out.length - 1] !== screen) out.push('');
      out.push(
        `${area.label} - ${area.seatCount} seats, ${taken} taken, ${area.seatCount - taken} free`
      );
    }
    const byColumn = new Map(row.seats.map((seat) => [seat.column, seat]));
    out.push(
      (row.name || String(row.index)).padEnd(labelWidth) +
        columns.map((c) => seatCell(byColumn.get(c), width)).join('')
    );
    drawnFor.set(area.number, (drawnFor.get(area.number) as number) + row.seats.length);
  }

  // Proof the drawing matches the payload, so a miscount shows up here rather
  // than being argued about.
  for (const area of drawn.areas) {
    const counted = drawnFor.get(area.number) || 0;
    if (!counted) continue;
    out.push(
      `${area.label}: ${counted} seats drawn` +
        (counted === area.seatCount ? '' : ` (API says ${area.seatCount})`)
    );
  }
  return out.join('\n');
}

/**
 * Narrow an already-fetched cinema list to one region.
 *
 * Accepts either the region key ("klangvalley") or its label ("Klang Valley"),
 * case-insensitively.
 */
export function byRegion(outlets: Cinema[], region: string): Cinema[] {
  const wanted = region.trim().toLowerCase();
  return outlets.filter(
    (c) =>
      wanted === (c.region || '').toLowerCase() || wanted === (c.location || '').toLowerCase()
  );
}

/**
 * Narrow an already-fetched cinema list to one town.
 *
 * TGV files every outlet under a sales region rather than a town, so this
 * matches on the town's own name where one is set and otherwise on the town
 * part of the outlet name, e.g. "Kuala Lumpur" for "Kuala Lumpur - 1 Utama".
 */
export function byCity(outlets: Cinema[], city: string): Cinema[] {
  const wanted = city.trim().toLowerCase();
  return outlets.filter((c) => wanted === (c.city || '').toLowerCase() || wanted === townOf(c));
}

/** The town part of an outlet name, e.g. "Kuala Lumpur". */
export function townOf(outlet: Cinema): string {
  const cut = outlet.name.indexOf(' - ');
  return cut === -1 ? '' : outlet.name.slice(0, cut).trim().toLowerCase();
}

/** Narrow an outlet list by region or town, whichever matches. */
export function filterCinemas(outlets: Cinema[], where: string): Cinema[] {
  const byRegionMatch = byRegion(outlets, where);
  return byRegionMatch.length ? byRegionMatch : byCity(outlets, where);
}

/** Look one outlet up by id, slug or name, case-insensitively. */
export function findCinema(outlets: Cinema[], needle: string): Cinema {
  const key = needle.trim().toLowerCase();
  for (const c of outlets) {
    if (c.id.toLowerCase() === key || (c.slug || '').toLowerCase() === key) return c;
  }
  for (const c of outlets) {
    if (c.name.toLowerCase() === key) return c;
  }
  const hits = outlets.filter((c) => c.name.toLowerCase().includes(key));
  if (hits.length === 1) return hits[0] as Cinema;
  if (hits.length > 1) {
    die(
      `"${needle}" matches ${hits.length} cinemas: ` +
        hits.slice(0, 8).map((c) => `${c.id} ${c.name}`).join(', ')
    );
  }
  die(`no cinema matching "${needle}"`);
}

function sessionLine(showtime: Showtime, perPerson = false): string {
  const parts: string[] = [showtime.time || '??:??', showtime.hall || '-'];
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

export function showMovies(
  movies: Movie[],
  cinemaLabel: string | null = null,
  perPerson = false
): void {
  if (cinemaLabel) console.log(`\n=== ${cinemaLabel}\n`);
  for (const film of movies) {
    const bits = [
      film.genre.length ? film.genre.join(', ') : null,
      film.runtime ? `${film.runtime} min` : null,
      film.rating,
      film.language,
    ].filter(Boolean);
    console.log(`  ${film.title}` + (bits.length ? `  (${bits.join(' | ')})` : ''));
    for (const showtime of film.showtimes) {
      console.log(`      ${sessionLine(showtime, perPerson)}`);
    }
  }
}

export function showSchedule(
  scheduleData: Record<string, Movie[]>,
  perPerson = false
): void {
  for (const [cid, movies] of Object.entries(scheduleData)) {
    showMovies(movies, movies.length ? `${movies[0]?.cinemaName || cid} [${cid}]` : cid, perPerson);
  }
}

export function showCinemas(outlets: Cinema[]): void {
  outlets.forEach((c, index) => {
    const formats = c.experiences.join(', ') || '-';
    const location = c.region || c.location || '-';
    console.log(`${String(index + 1).padStart(2)}. ${c.name} [${c.id}]`);
    console.log(`    ${location} | ${formats}`);
  });
}

/**
 * True when a positional argument is a date rather than a cinema name.
 *
 * The CLI takes an optional cinema and an optional date for `--movie`, and the
 * parser cannot tell them apart by position alone. Deciding by shape works
 * because every cinema id and name contains a letter and no date does.
 */
export function looksLikeDate(value: string | null | undefined): boolean {
  if (!value) return false;
  const text = String(value).trim();
  if (!text) return false;
  try {
    normaliseDate(text);
  } catch {
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

interface CliArgs {
  cinema: string | null;
  date: string | null;
  movie: string | null;
  region: string | null;
  earliest: string | null;
  latest: string | null;
  format: string | null;
  workers: number;
  time: string | null;
  cinemas: boolean;
  regions: boolean;
  dates: boolean;
  json: boolean;
  seats: boolean;
  seatMap: boolean;
  prices: boolean;
  perPerson: boolean;
  rawColumns: boolean;
  noDelay: boolean;
  help: boolean;
}

const FLAGS = new Set([
  '--cinemas', '--regions', '--dates', '--json', '--seats', '--seat-map',
  '--prices', '--per-person', '--raw-columns', '--no-delay', '--help', '-h',
]);
const VALUED = new Set(['--movie', '--region', '--from', '--until', '--format', '--workers', '--time']);

const ARG_KEYS: Record<string, keyof CliArgs> = {
  '--movie': 'movie',
  '--region': 'region',
  '--from': 'earliest',
  '--until': 'latest',
  '--format': 'format',
  '--time': 'time',
  '--seat-map': 'seatMap',
  '--per-person': 'perPerson',
  '--raw-columns': 'rawColumns',
  '--no-delay': 'noDelay',
  '--cinemas': 'cinemas',
  '--regions': 'regions',
  '--dates': 'dates',
  '--json': 'json',
  '--seats': 'seats',
  '--prices': 'prices',
  '--help': 'help',
  '-h': 'help',
};

/** Minimal argparse: a few flags, a few valued options, two positionals. */
function parseArgs(argv: string[]): CliArgs {
  const args: CliArgs = {
    cinema: null,
    date: null,
    movie: null,
    region: null,
    earliest: null,
    latest: null,
    format: null,
    workers: 4,
    time: null,
    cinemas: false,
    regions: false,
    dates: false,
    json: false,
    seats: false,
    seatMap: false,
    prices: false,
    perPerson: false,
    rawColumns: false,
    noDelay: false,
    help: false,
  };
  const positionals: string[] = [];
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i] as string;
    if (VALUED.has(token)) {
      const value = argv[++i];
      if (value === undefined) die(`${token} needs a value`);
      const key = ARG_KEYS[token] as string;
      if (key === 'workers') {
        const n = Number(value);
        if (!Number.isFinite(n)) die(`--workers expects a number, got "${value}"`);
        args.workers = n;
      } else {
        (args as unknown as Record<string, unknown>)[key] = value;
      }
    } else if (FLAGS.has(token)) {
      (args as unknown as Record<string, unknown>)[ARG_KEYS[token] as string] = true;
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

const USAGE = `usage: node tgv_scraper.ts [cinema] [YYYY-MM-DD] [options]

Showtimes for TGV Cinemas Malaysia.

  --cinemas               list every outlet instead
  --regions               list regions instead
  --dates                 list dates that have sessions
  --movie SLUG            every session of one film
  --region NAME           with --cinemas, only this region
  --from HH:MM            only showtimes at or after this time
  --until HH:MM           only showtimes at or before this time
  --format CODES          only these hall formats, e.g. deluxe or deluxe,indulge
  --json                  print JSON
  --seats                 include seat availability
  --seat-map              draw the seat map for one session (needs a cinema, a date and --time)
  --prices                include ticket prices (one extra call per showtime)
  --per-person            with --prices, quote per person instead of per ticket
  --workers N             parallel cinema requests
  --time HH:MM            with --seat-map, which session to draw
  --raw-columns           with --seat-map, draw TGV's per-row columns instead of aligning by number
  --no-delay              skip the polite pause

examples:
  node tgv_scraper.ts BU0
  node tgv_scraper.ts --cinemas
  node tgv_scraper.ts --movie other-mommy`;

const printJson = (value: unknown): void => console.log(JSON.stringify(value, null, 2));

export async function main(argv: string[] = process.argv.slice(2)): Promise<void> {
  const args = parseArgs(argv);
  if (args.help) {
    console.log(USAGE);
    return;
  }

  const delay = args.noDelay ? 0 : 0.25;
  const throttle = new Throttle(delay);
  const net = { delay, throttle };

  if (args.regions) {
    const found = await regions({ ...net, throttle });
    if (args.json) printJson(found);
    else {
      console.log();
      for (const [key, label] of Object.entries(found).sort((a, b) => a[1].localeCompare(b[1]))) {
        console.log(`  ${key.padEnd(16)} ${label}`);
      }
    }
    return;
  }

  if (args.dates) {
    const found = await businessDates({ ...net, throttle });
    if (args.json) printJson(found);
    else {
      console.log(`\ndates with sessions: ${found.length}\n`);
      console.log('  ' + found.join('  '));
    }
    return;
  }

  if (args.cinemas) {
    let outlets = await cinemas({ ...net, throttle });
    if (args.region) outlets = byRegion(outlets, args.region);
    if (args.json) printJson(outlets.map((c) => c.toDict()));
    else {
      console.log(`\ncinemas${args.region ? ` in ${args.region}` : ''}: ${outlets.length} outlets\n`);
      showCinemas(outlets);
    }
    return;
  }

  if (args.movie) {
    // `--movie` accepts an optional cinema and date, in either order of
    // appearance, and the parser cannot tell a date from a cinema name, so
    // decide by shape rather than by position:
    //   --movie slug                      every cinema, today
    //   --movie slug 2026-10-18           every cinema, that date
    //   --movie slug BU0                  one cinema, today
    //   --movie slug BU0 2026-10-18       one cinema, that date
    let movieCinema = args.cinema;
    let movieOn: string | null = args.date;
    if (args.date === null && args.cinema && looksLikeDate(args.cinema)) {
      movieOn = args.cinema;
      movieCinema = null;
    } else if (args.date !== null && looksLikeDate(args.date)) {
      movieOn = args.date;
    } else if (args.date !== null) {
      // Second positional was a cinema, so swap them.
      movieOn = movieCinema;
      movieCinema = args.date;
    }
    let cinemaIds: string[] | null = null;
    if (movieCinema !== null) {
      const target = findCinema(await cinemas({ withDetails: false, ...net }), movieCinema);
      cinemaIds = [target.id];
    } else if (args.region) {
      // Needs the full directory: withDetails=false carries no location, so
      // byRegion would silently match nothing.
      const inRegion = byRegion(await cinemas({ ...net }), args.region);
      if (!inRegion.length) die(`no cinemas in region "${args.region}"`);
      cinemaIds = inRegion.map((c) => c.id);
    }

    const found = await movie(args.movie, movieOn, {
      seats: args.seats,
      prices: args.prices,
      cinemaIds,
      workers: args.workers,
      after: args.earliest,
      before: args.latest,
      formats: args.format,
      ...net,
    });
    if (args.json) {
      printJson(found.map(({ cinemaId, movie: m }) => ({ cinema_id: cinemaId, ...m.toDict() })));
    } else {
      const where = cinemaIds === null ? 'every cinema' : `${cinemaIds.length} cinema(s)`;
      console.log(`\n${args.movie} on ${normaliseDate(movieOn)}, ${where}: ${found.length} cinemas\n`);
      for (const { cinemaId, movie: m } of found) {
        showMovies([m], `${m.cinemaName || cinemaId} [${cinemaId}]`, args.perPerson);
      }
    }
    return;
  }

  if (args.seatMap) {
    if (!args.cinema || !args.time) die('--seat-map needs a cinema and --time HH:MM');
    const target = findCinema(await cinemas({ withDetails: false, ...net }), args.cinema);
    const when = normaliseDate(args.date);
    const wanted = args.time.trim();
    const matches = (await day(target.id, when, { ...net }))
      .flatMap((film) => film.showtimes.filter((s) => s.time === wanted).map((s) => [film, s] as [Movie, Showtime]));
    if (!matches.length) die(`no session at ${wanted} on ${when} at ${target.name}`);
    for (const [film, showtime] of matches) {
      const plan = await seatPlan(target.id, showtime, { ...net, throttle });
      console.log(
        `\n${target.name} [${target.id}] ${when} ${showtime.time} ` +
          `${showtime.hall || ''} ${film.title} (session ${showtime.sessionId})`
      );
      console.log(renderSeatPlan(plan, { align: !args.rawColumns }));
    }
    return;
  }

  const on = normaliseDate(args.date);

  if (args.cinema) {
    const target = findCinema(await cinemas({ withDetails: false, ...net }), args.cinema);
    // Filters go in, not out: prices cost one call per session per area, so
    // narrowing after the fact still pays for every discarded screening.
    const movies = await day(target.id, on, {
      seats: args.seats,
      prices: args.prices,
      after: args.earliest,
      before: args.latest,
      formats: args.format,
      ...net,
    });
    if (args.json) printJson(movies.map((m) => m.toDict()));
    else {
      console.log(`\n${target.name} [${target.id}] ${on}: ${movies.length} movies\n`);
      showMovies(movies, null, args.perPerson);
    }
    return;
  }

  const everything = await schedule(null, on, {
    seats: args.seats,
    prices: args.prices,
    workers: args.workers,
    after: args.earliest,
    before: args.latest,
    formats: args.format,
    ...net,
  });
  if (args.json) {
    printJson(
      Object.fromEntries(
        Object.entries(everything).map(([cid, ms]) => [cid, ms.map((m) => m.toDict())])
      )
    );
  } else {
    const entries = Object.entries(everything);
    const total = entries.reduce((sum, [, ms]) => sum + ms.length, 0);
    console.log(`\n${on}: ${entries.length} cinemas, ${total} movie slots\n`);
    showSchedule(everything, args.perPerson);
  }
}

const entry = process.argv[1];
const isMain = Boolean(entry) && import.meta.url === `file://${entry}`;
if (isMain) {
  main().catch((exc: unknown) => {
    // Ctrl-C in Node arrives as SIGINT and is not catchable as an exception, so
    // the only error worth a bare 130 is an aborted fetch.
    if ((exc as Error)?.name === 'AbortError') {
      process.exit(130);
    }
    console.error(`error: ${exc instanceof Error ? exc.message : String(exc)}`);
    process.exit(1);
  });
}