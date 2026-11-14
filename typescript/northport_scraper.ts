#!/usr/bin/env node
// Northport (Port Klang) e-NP2 CAS-N4 enquiries: container, B/L, vessel, booking.
//
// Single file, zero dependencies, no build step. Node 22.6+ (developed against
// Node 24 LTS) runs this directly: Node strips the types at load time.
//
//     node northport_scraper.ts container ABCD1234567   # one container
//     node northport_scraper.ts bl SOMEBL123            # one bill of lading
//     node northport_scraper.ts vessel MV-EXAMPLE       # one vessel or SCN
//     node northport_scraper.ts booking BOOKING123      # one booking, with its containers
//
// Every CLI result is printed as JSON (pretty, indent 2): the portal's own
// labels mapped to their values. A booking also carries "Containers", a list
// of {"Container Number", ...} objects. An unknown number gives an empty object.
//
// The portal answers each enquiry with an HTML fragment, not JSON, made of
// `<p>Label : <span>value</span></p>` rows.
//
// Also importable:
//
//     import { getByContainer } from './northport_scraper.ts';
//     const row = await getByContainer('ABCD1234567');

import { realpathSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

export const BASE_URL = 'https://apps.northport.com.my/e-NP2/';
const USER_AGENT = 'Mozilla/5.0';
const TIMEOUT = 30;
// The portal answers its own page's XHR calls; the header marks ours as one.
const HEADERS = { 'X-Requested-With': 'XMLHttpRequest' };

const ROW = /<p[^>]*>\s*(.*?)\s*:\s*(?:<br>\s*)?<span>(.*?)<\/span>/gs;
const CHILD =
  /<div class='div_query_output_parent'[^>]*>.*?<span>(.*?)<\/span>.*?<div id='divvvv\d+' class='div_query_output_child'>(.*?)<\/div>/gs;
const TAG = /<[^>]+>/g;

export type Rows = Record<string, string>;
export type Booking = Record<string, string | Rows[]>;

/** Thrown when a run cannot continue; main() turns it into one stderr line. */
export class NorthportError extends Error {}

const die = (message: string): never => {
  throw new NorthportError(message);
};

// -------------------------------------------------------------- parsers ----

const NAMED: Record<string, string> = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };

/** The HTML entities a portal fragment uses: named basics plus numeric. */
function unescapeHtml(text: string): string {
  return text.replace(/&(?:#(\d+)|#[xX]([0-9a-fA-F]+)|(\w+));/g, (whole, dec, hex, name) => {
    if (dec !== undefined) return String.fromCodePoint(Number(dec));
    if (hex !== undefined) return String.fromCodePoint(parseInt(hex, 16));
    return NAMED[name] ?? whole;
  });
}

/** An enquiry fragment -> {label: value}. The first of a repeated label wins. */
export function parseRows(html: string): Rows {
  const out: Rows = {};
  for (const m of html.matchAll(ROW)) {
    const label = unescapeHtml(m[1]!.replace(TAG, '')).trim();
    if (!(label in out)) out[label] = unescapeHtml(m[2]!).trim();
  }
  return out;
}

/** A booking fragment -> its header rows plus a "Containers" list. */
export function parseBooking(html: string): Booking {
  const at = html.indexOf('Container List');
  const header = at < 0 ? html : html.slice(0, at);
  const rest = at < 0 ? '' : html.slice(at + 'Container List'.length);
  const containers = [...rest.matchAll(CHILD)].map((m) => ({
    'Container Number': unescapeHtml(m[1]!).trim(),
    ...parseRows(m[2]!),
  }));
  return { ...parseRows(header), Containers: containers };
}

// -------------------------------------------------------------- network ----

/** POST one form field to an enquiry page and return the fragment. Failure throws. */
export async function postForm(endpoint: string, field: string, value: string): Promise<string> {
  const url = BASE_URL + endpoint;
  let res: Response;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'User-Agent': USER_AGENT, ...HEADERS },
      body: new URLSearchParams({ [field]: value.trim().toUpperCase() }),
      signal: AbortSignal.timeout(TIMEOUT * 1000),
    });
  } catch (e) {
    return (e as Error).name === 'TimeoutError'
      ? die(`timed out fetching ${url}`)
      : die(`network error fetching ${url}: ${(e as Error).message}`);
  }
  if (!res.ok) die(`HTTP ${res.status} from ${url}`);
  return res.text();
}

export async function getByContainer(containerNo: string): Promise<Rows> {
  return parseRows(await postForm('query_contN4.php', 'cont_num_N4', containerNo));
}

export async function getByBl(blNo: string): Promise<Rows> {
  return parseRows(await postForm('query_contblN4.php', 'cont_bl_N4', blNo));
}

export async function getByVessel(scnOrVesselName: string): Promise<Rows> {
  return parseRows(await postForm('query_vesselN4.php', 'vsl_num_N4', scnOrVesselName));
}

export async function getByBooking(bookingRef: string): Promise<Booking> {
  return parseBooking(await postForm('query_ebkref_N4.php', 'ebkref_num_N4', bookingRef));
}

export const QUERIES = {
  container: getByContainer,
  bl: getByBl,
  vessel: getByVessel,
  booking: getByBooking,
};

/** A result as the CLI prints it: indent 2. */
export function toJson(result: Rows | Booking): string {
  return JSON.stringify(result, null, 2);
}

// ------------------------------------------------------------------- cli ----

const USAGE = `usage: node northport_scraper.ts {${Object.keys(QUERIES).join('|')}} value

Northport e-NP2 CAS-N4 enquiries.

  kind    what the value identifies
  value   container number, B/L, vessel name or SCN, or booking reference

Output is always JSON.`;

export async function main(argv: string[] = process.argv.slice(2)): Promise<void> {
  if (argv.includes('--help') || argv.includes('-h')) {
    console.log(USAGE);
    return;
  }
  const [kind, value] = argv;
  if (argv.length !== 2 || !(kind! in QUERIES)) die(USAGE);
  console.log(toJson(await QUERIES[kind as keyof typeof QUERIES](value!)));
}

// Run main() only when this file is the script node was started with, however
// that path was spelled (relative, symlinked, or with spaces, which a URL encodes).
const entry = process.argv[1];
if (entry && realpathSync(entry) === realpathSync(fileURLToPath(import.meta.url))) {
  main().catch((e: unknown) => {
    console.error(e instanceof Error ? e.message : String(e));
    process.exit(1);
  });
}
