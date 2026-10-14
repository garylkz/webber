#!/usr/bin/env node -T
// Scrape Meetup events from the public gql2 endpoint. No login, no API key.
//
// Single file, zero dependencies, no build step. Node 22.6+ (developed against
// Node 24 LTS) runs this directly: Node strips the types at load time.
//
//     node mu_scrapper.ts                                  # nearby events near KL
//     node mu_scrapper.ts --query python                   # keyword filter
//     node mu_scrapper.ts --radius 10 --event-type ONLINE
//     node mu_scrapper.ts --country sg --city "Singapore" --lat 1.3521 --lon 103.8198
//     node mu_scrapper.ts --start-date 2026-08-20 --end-date 2026-09-20 --dump
//
// Also importable:
//
//     import { searchEvents, dumpCountry } from './mu_scrapper.ts';
//     const { nodes } = await searchEvents({ country: 'my', city: 'Kuala Lumpur' }, 50, 20);
//
// Two modes. The default lists nearby upcoming events; --dump sweeps geo anchors
// across a whole country and writes the deduped result to out/. Defaults are
// Kuala Lumpur, Malaysia (country code "my") and a 25 mile radius, which covers
// KL and the close parts of Selangor.

import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0';
const URL = 'https://www.meetup.com/gql2';
const TIMEOUT = 20;
const DELAY = 0.3;
const PAGE = 50;

const OUT_DIR = join(dirname(fileURLToPath(import.meta.url)), 'out');

const DEFAULT = { city: 'Kuala Lumpur', country: 'my', lat: 3.139, lon: 101.6869 };
const DEFAULT_LIST_RADIUS = 25;
const DEFAULT_DUMP_RADIUS = 500;
const DEFAULT_LIST_MAX = 20;
const DEFAULT_DUMP_MAX = 500;

/** The geo anchors a country sweep radiates out from. */
const ANCHORS: Array<[string, number, number]> = [
  ['Kuala Lumpur', 3.139, 101.6869],
  ['George Town (Penang)', 5.4141, 100.3288],
  ['Johor Bahru', 1.4927, 103.7418],
  ['Ipoh', 4.5975, 101.0901],
  ['Malacca', 2.1896, 102.2501],
  ['Kuantan', 3.8077, 103.326],
  ['Kota Bharu', 6.1256, 102.2381],
  ['Kuala Terengganu', 5.3303, 103.1407],
  ['Alor Setar', 6.1244, 100.3678],
  ['Seremban', 2.7258, 101.9424],
  ['Kota Kinabalu', 5.9804, 116.0735],
  ['Kuching', 1.5535, 110.3593],
  ['Miri', 4.3995, 113.9914],
  ['Bintulu', 3.1865, 113.5473],
  ['Sandakan', 5.8394, 118.1179],
  ['Tawau', 4.2448, 117.891],
  ['Labuan', 5.2831, 115.2308],
];

/**
 * Discard an event unless its venue is in the target country. Where Meetup leaves
 * the country off, these bounding boxes stand in for Malaysia: the peninsula and
 * Borneo, which is the gap between them.
 */
const MY_BOXES: Array<[number, number, number, number]> = [
  [1.0, 6.7, 99.6, 104.6],
  [0.85, 7.36, 109.4, 119.4],
];

/** What Meetup's EventSearchFilter accepts. Every field is optional. */
export interface EventFilter {
  query?: string;
  city?: string;
  country?: string;
  state?: string;
  zip?: string;
  lat?: number;
  lon?: number;
  radius?: number;
  eventType?: 'PHYSICAL' | 'ONLINE' | 'HYBRID';
  isHappeningNow?: boolean;
  startDateRange?: string;
  endDateRange?: string;
}

/** One event, as the query below asks for it. */
export interface EventNode {
  id: string;
  title: string;
  dateTime: string;
  endTime: string | null;
  duration: string | null;
  eventType: string;
  isOnline: boolean;
  status: string;
  eventUrl: string;
  venue: {
    name: string;
    city: string;
    state: string | null;
    country: string | null;
    lat: number;
    lon: number;
  } | null;
  group: { name: string; urlname: string };
  rsvps: { totalCount: number };
}

interface PageInfo {
  endCursor: string | null;
  hasNextPage: boolean;
}

interface SearchPage {
  totalCount: number;
  pageInfo: PageInfo;
  edges: Array<{ node: EventNode }>;
}

/** Thrown when a run cannot continue; main() turns it into one stderr line. */
export class MuError extends Error {}

const die = (message: string): never => {
  throw new MuError(message);
};

const ANSI_RE = /\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))/g;
const CTRL_RE = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g;

/**
 * Strip escape sequences and control characters from untrusted strings before
 * printing them. Event titles and venue names come from Meetup and go straight
 * to the terminal, so an escape sequence in one would otherwise be executed.
 */
export function sanitize(text: string | null | undefined): string {
  return (text ?? '').replace(ANSI_RE, '').replace(CTRL_RE, '');
}

const QUERY = `query($filter: EventSearchFilter!, $first: Int!, $after: String) {
  eventSearch(filter: $filter, first: $first, after: $after) {
    totalCount
    pageInfo { endCursor hasNextPage }
    edges {
      node {
        id
        title
        dateTime
        endTime
        duration
        eventType
        isOnline
        status
        eventUrl
        venue { name city state country lat lon }
        group { name urlname }
        rsvps { totalCount }
      }
    }
  }
}`;

/** One page of one search. */
async function fetchPage(
  filter: EventFilter,
  first: number,
  after: string | null,
): Promise<SearchPage> {
  const res = await fetch(URL, {
    method: 'POST',
    headers: { 'User-Agent': UA, 'Content-Type': 'application/json' },
    body: JSON.stringify({ query: QUERY, variables: { filter, first, after } }),
    signal: AbortSignal.timeout(TIMEOUT * 1000),
  });
  if (!res.ok) die(`Meetup returned HTTP ${res.status}`);

  const body = (await res.json()) as {
    errors?: Array<{ message: string }>;
    data?: { eventSearch?: SearchPage };
  };
  if (body.errors) die(sanitize(body.errors[0]?.message ?? 'unknown GraphQL error').slice(0, 300));
  const page = body.data?.eventSearch;
  if (!page) die('Meetup sent no eventSearch in the response');
  return page;
}

const sleep = (seconds: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, seconds * 1000));

/**
 * Paginate one geo anchor, and report how many events Meetup says it has.
 *
 * The delay sits between pages rather than before the first one, since the first
 * page has no cursor to wait on.
 */
export async function searchEvents(
  filter: EventFilter,
  first: number,
  maxResults: number,
): Promise<{ nodes: EventNode[]; totalCount: number }> {
  let page = await fetchPage(filter, first, null);
  const nodes: EventNode[] = [];
  let cursor = page.pageInfo.endCursor;

  while (page.edges.length && nodes.length < maxResults) {
    nodes.push(...page.edges.map((edge) => edge.node));
    if (!page.pageInfo.hasNextPage || nodes.length >= maxResults) break;
    await sleep(DELAY);
    page = await fetchPage(filter, first, cursor);
    cursor = page.pageInfo.endCursor;
  }

  return { nodes: nodes.slice(0, maxResults), totalCount: page.totalCount };
}

/** Whether a venue is in `code`, falling back to a bounding box for Malaysia. */
export function inCountry(venue: EventNode['venue'], code: string): boolean {
  if (!venue) return false;
  const country = (venue.country ?? '').toUpperCase();
  if (country) return country === code.toUpperCase();
  if (code.toUpperCase() !== 'MY') return false;
  const { lat, lon } = venue;
  if (lat === null || lat === undefined || lon === null || lon === undefined) return false;
  return MY_BOXES.some(([lo, hi, lo2, hi2]) => lo <= lat && lat <= hi && lo2 <= lon && lon <= hi2);
}

/**
 * Accept `YYYY-MM-DD` or a full ISO 8601 datetime, and return an ISO string with
 * a timezone offset, which is what Meetup's ZonedDateTime scalar requires.
 *
 * A bare date is local midnight, or end-of-day when `end` is set, so that
 * `--start-date X --end-date X` means that one whole day.
 */
export function parseDate(value: string | Date, end = false): string {
  if (value instanceof Date) return value.toISOString();
  const text = String(value).trim();
  const stamp = /^\d{4}-\d{2}-\d{2}$/.test(text) ? `${text}T${end ? '23:59:59' : '00:00:00'}` : text;
  const parsed = new Date(stamp);
  if (Number.isNaN(parsed.getTime())) die(`bad date: ${value}`);
  return parsed.toISOString();
}

/** Print a list of events the way the default mode does. */
export function printEvents(
  nodes: EventNode[],
  city: string,
  lat: number,
  lon: number,
  radius: number,
  query: string,
): void {
  console.log(
    `location: ${city} (${lat}, ${lon}), radius ${radius} mi, ` +
      `query: ${query !== '*' ? query : '<all>'}`,
  );
  console.log(`total matching: ${nodes.length} (fetched)`);
  console.log();

  for (const node of nodes) {
    const venue = node.venue;
    const venueText = venue
      ? sanitize(`${venue.name} (${venue.city})`)
      : 'online/unknown';
    console.log(`- ${sanitize(node.title)}`);
    console.log(
      `    ${sanitize(node.dateTime)}  [${sanitize(node.eventType)}]  ` +
        `${sanitize(node.group.name)} (${sanitize(node.group.urlname)})  ` +
        `rsvps:${node.rsvps.totalCount}`,
    );
    console.log(`    ${venueText}`);
  }
}

/** One anchor's result: what to print, and what it kept. */
interface AnchorSummary {
  name: string;
  total: number;
  got: number;
}

/**
 * Sweep every anchor and keep only in-country events, deduped by id.
 *
 * The anchors overlap, so the same event comes back from several of them. Ids
 * are the dedupe key: the same meetup seen from George Town and from Kuala Lumpur
 * is one event with one id.
 */
export async function dumpCountry(
  filter: EventFilter,
  radius: number,
  cap: number,
  country: string,
): Promise<EventNode[]> {
  const anchors =
    country.toUpperCase() === 'MY'
      ? ANCHORS
      : ([['', filter.lat ?? 0, filter.lon ?? 0]] as Array<[string, number, number]>);
  const seen = new Map<string, EventNode>();
  const summary: AnchorSummary[] = [];

  for (const [name, lat, lon] of anchors) {
    const anchorFilter: EventFilter = { ...filter, lat, lon, radius };
    if (name) anchorFilter.city = name;

    let page = await fetchPage(anchorFilter, PAGE, null);
    const total = page.totalCount;
    const collected: EventNode[] = [];
    let nodes = page.edges.map((edge) => edge.node);
    let cursor = page.pageInfo.endCursor;

    while (nodes.length && collected.length < cap) {
      for (const node of nodes) {
        if (!inCountry(node.venue, country)) continue;
        if (seen.has(node.id)) continue;
        seen.set(node.id, node);
        collected.push(node);
      }
      if (!page.pageInfo.hasNextPage || collected.length >= cap) break;
      await sleep(DELAY);
      page = await fetchPage(anchorFilter, PAGE, cursor);
      nodes = page.edges.map((edge) => edge.node);
      cursor = page.pageInfo.endCursor;
    }

    summary.push({ name: name || `${lat.toFixed(4)},${lon.toFixed(4)}`, total, got: collected.length });
  }

  const events = [...seen.values()];
  await mkdir(OUT_DIR, { recursive: true });
  const outPath = join(OUT_DIR, `mtp_events_${country}_${timestamp()}.json`);
  await writeFile(outPath, JSON.stringify(events, null, 2), 'utf8');

  for (const { name, total, got } of summary) {
    console.log(`${name}: ${total} matching, ${got} new in-country unique`);
  }
  console.log(`\ntotal unique events in ${country.toUpperCase()}: ${events.length}`);
  console.log(`wrote: ${outPath}`);

  return events;
}

/** A YYYYMMDD_HHMMSS stamp for the output file name. */
function timestamp(): string {
  const now = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  return (
    `${now.getFullYear()}${p(now.getMonth() + 1)}${p(now.getDate())}` +
    `_${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}`
  );
}

interface CliArgs {
  query: string;
  country: string;
  city: string;
  state: string | null;
  zip: string | null;
  lat: number;
  lon: number;
  radius: number | null;
  eventType: 'PHYSICAL' | 'ONLINE' | 'HYBRID' | null;
  happeningNow: boolean;
  startDate: string | null;
  endDate: string | null;
  max: number | null;
  dump: boolean;
  help: boolean;
}

const VALUED: Record<string, keyof CliArgs> = {
  '--query': 'query',
  '--country': 'country',
  '--city': 'city',
  '--state': 'state',
  '--zip': 'zip',
  '--lat': 'lat',
  '--lon': 'lon',
  '--radius': 'radius',
  '--event-type': 'eventType',
  '--start-date': 'startDate',
  '--end-date': 'endDate',
  '--max': 'max',
};

const FLAGS: Record<string, keyof CliArgs> = {
  '--happening-now': 'happeningNow',
  '--dump': 'dump',
  '--help': 'help',
};

function parseArgs(argv: string[]): CliArgs {
  const args: CliArgs = {
    query: '*',
    country: DEFAULT.country,
    city: DEFAULT.city,
    state: null,
    zip: null,
    lat: DEFAULT.lat,
    lon: DEFAULT.lon,
    radius: null,
    eventType: null,
    happeningNow: false,
    startDate: null,
    endDate: null,
    max: null,
    dump: false,
    help: false,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i] as string;
    const key = VALUED[token];
    if (key) {
      const value = argv[++i];
      if (value === undefined) die(`${token} needs a value`);
      if (key === 'lat' || key === 'lon' || key === 'radius' || key === 'max') {
        const n = Number(value);
        if (!Number.isFinite(n)) die(`${token} expects a number, got "${value}"`);
        (args as Record<string, unknown>)[key] = n;
      } else {
        (args as Record<string, unknown>)[key] = value;
      }
      continue;
    }
    const flag = FLAGS[token];
    if (flag) {
      (args as Record<string, unknown>)[flag] = true;
      continue;
    }
    if (token.startsWith('-') && token !== '-') die(`unknown option ${token}`);
    die(`unexpected argument ${token}`);
  }

  if (args.eventType && !['PHYSICAL', 'ONLINE', 'HYBRID'].includes(args.eventType)) {
    die('--event-type must be PHYSICAL, ONLINE or HYBRID');
  }
  return args;
}

const USAGE = `usage: node mu_scrapper.ts [options]

Scrape Meetup events from the public gql2 endpoint. No login, no API key.

  --query TEXT        keyword filter (default: '*' = all events)
  --country CODE      ISO country code (default: my)
  --city NAME         city name (default: Kuala Lumpur)
  --state NAME        state/province name (e.g. Selangor)
  --zip CODE          postal code
  --lat N             latitude
  --lon N             longitude
  --radius N          search radius in miles (list default 25, dump default 500)
  --event-type T      PHYSICAL, ONLINE or HYBRID
  --happening-now     only events happening right now
  --start-date D      range start, YYYY-MM-DD or ISO datetime (local time)
  --end-date D        range end, YYYY-MM-DD or ISO datetime (local time)
  --max N             max results (list default 20, dump default 500 per anchor)
  --dump              country-wide scan over geo anchors, written to out/

examples:
  node mu_scrapper.ts
  node mu_scrapper.ts --query python --radius 10
  node mu_scrapper.ts --country sg --city "Singapore" --lat 1.3521 --lon 103.8198
  node mu_scrapper.ts --start-date 2026-08-20 --end-date 2026-09-20 --dump`;

export async function main(argv: string[] = process.argv.slice(2)): Promise<void> {
  const args = parseArgs(argv);
  if (args.help) {
    console.log(USAGE);
    return;
  }

  const filter: EventFilter = {
    query: args.query,
    city: args.city,
    country: args.country,
    lat: args.lat,
    lon: args.lon,
  };
  if (args.state) filter.state = args.state;
  if (args.zip) filter.zip = args.zip;
  if (args.eventType) filter.eventType = args.eventType;
  if (args.happeningNow) filter.isHappeningNow = true;
  if (args.startDate) filter.startDateRange = parseDate(args.startDate, false);
  if (args.endDate) filter.endDateRange = parseDate(args.endDate, true);

  if (args.dump) {
    await dumpCountry(
      filter,
      args.radius ?? DEFAULT_DUMP_RADIUS,
      args.max ?? DEFAULT_DUMP_MAX,
      args.country,
    );
    return;
  }

  const radius = args.radius ?? DEFAULT_LIST_RADIUS;
  const maxResults = args.max ?? DEFAULT_LIST_MAX;
  filter.radius = radius;

  const { nodes } = await searchEvents(filter, Math.min(PAGE, maxResults), maxResults);
  printEvents(nodes, args.city, args.lat, args.lon, radius, args.query);
}

const entry = process.argv[1];
if (entry && import.meta.url === `file://${entry}`) {
  main().catch((e: unknown) => {
    console.error(e instanceof Error ? e.message : String(e));
    process.exit(1);
  });
}