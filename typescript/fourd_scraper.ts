#!/usr/bin/env node
// Malaysian 4D draw results from the operators' own sites.
//
// Single file, zero dependencies, no build step. Node 22.6+ (developed against
// Node 24 LTS) runs this directly: Node strips the types at load time.
//
//     node fourd_scraper.ts                     # latest draw, every operator
//     node fourd_scraper.ts 2026-10-25          # one draw date
//     node fourd_scraper.ts --recent 10         # the last 10 draw dates
//     node fourd_scraper.ts --from 2024-01-01 --to 2024-12-31   # a date range
//     node fourd_scraper.ts --operator magnum   # one operator only
//     node fourd_scraper.ts --all --out all.json   # every draw ever, saved to a file
//
// Every CLI result is printed as JSON (pretty, indent 2):
//
//   {"draws": [{"operator", "draw_no", "draw_date", "draw_day", "first",
//               "second", "third", "special", "consolation", "official"}]}
//
// Covers Magnum 4D (magnum4d.my) and Da Ma Cai 1+3D (damacai.com.my). Sports
// Toto is not covered: sportstoto.com.my refuses non-browser clients.
//
// Also importable:
//
//     import { between, drawsOn, latest, recent } from './fourd_scraper.ts';
//     const draws = await between('2024-01-01', '2024-12-31');

import { mkdirSync, realpathSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

export const MAGNUM_BASE = 'https://www.magnum4d.my';
export const DAMACAI_BASE = 'https://www.damacai.com.my';
export const OPERATORS = ['magnum', 'damacai'] as const;
export type Operator = (typeof OPERATORS)[number];

const USER_AGENT = 'Mozilla/5.0';
// Da Ma Cai's result endpoint answers "Page Not Found" without this header,
// which its own page sends on every call.
const DAMACAI_HEADERS = { cookiesession: '421' };
const DAYS = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'];
// How far back a Da Ma Cai "latest" walks before giving up. Draws are at most
// four days apart (Sun to Wed), so a week always finds one.
const DAMACAI_LOOKBACK = 7;
const FETCH_ATTEMPTS = 3;
// The oldest draw each operator still publishes.
export const MAGNUM_FIRST = '1985-04-25';
export const DAMACAI_FIRST = '2005-01-01';
// Da Ma Cai costs two requests per date, so a long range fetches a few dates
// at once rather than one after another.
const DAMACAI_WORKERS = 4;
// How often a long run reports how many Da Ma Cai dates it has done.
const PROGRESS_EVERY = 50;

export type Progress = (done: number, total: number) => void;

/** Thrown when a run cannot continue; main() turns it into one stderr line. */
export class FourDError extends Error {}

const die = (message: string): never => {
  throw new FourDError(message);
};

/** One operator's result for one draw. */
export class Draw {
  operator: Operator;
  drawNo: string;
  drawDate: string; // YYYY-MM-DD
  drawDay: string; // MON..SUN
  first: string;
  second: string;
  third: string;
  special: string[];
  consolation: string[];
  official: boolean;

  constructor(
    operator: Operator,
    drawNo: string,
    drawDate: string,
    drawDay: string,
    first: string,
    second: string,
    third: string,
    special: string[] = [],
    consolation: string[] = [],
    official = true,
  ) {
    this.operator = operator;
    this.drawNo = drawNo;
    this.drawDate = drawDate;
    this.drawDay = drawDay;
    this.first = first;
    this.second = second;
    this.third = third;
    this.special = special;
    this.consolation = consolation;
    this.official = official;
  }

  toDict(): Record<string, unknown> {
    return {
      operator: this.operator,
      draw_no: this.drawNo,
      draw_date: this.drawDate,
      draw_day: this.drawDay,
      first: this.first,
      second: this.second,
      third: this.third,
      special: [...this.special],
      consolation: [...this.consolation],
      official: this.official,
    };
  }
}

// ---------------------------------------------------------------- dates ----

/** YYYY-MM-DD, YYYYMMDD or DD/MM/YYYY -> YYYY-MM-DD. Throws on anything else. */
export function normaliseDate(value: string): string {
  const text = value.trim();
  const shapes: [RegExp, (m: RegExpExecArray) => [string, string, string]][] = [
    [/^(\d{4})-(\d{2})-(\d{2})$/, (m) => [m[1]!, m[2]!, m[3]!]],
    [/^(\d{4})(\d{2})(\d{2})$/, (m) => [m[1]!, m[2]!, m[3]!]],
    [/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/, (m) => [m[3]!, m[2]!.padStart(2, '0'), m[1]!.padStart(2, '0')]],
  ];
  for (const [re, pick] of shapes) {
    const m = re.exec(text);
    if (!m) continue;
    const [y, mo, d] = pick(m);
    const iso = `${y}-${mo}-${d}`;
    const check = new Date(`${iso}T00:00:00Z`);
    if (!Number.isNaN(check.getTime()) && check.toISOString().slice(0, 10) === iso) return iso;
  }
  return die(`not a date: '${value}'`);
}

export function dayOf(iso: string): string {
  return DAYS[new Date(`${iso}T00:00:00Z`).getUTCDay()]!;
}

export function todayMyt(): string {
  return new Date(Date.now() + 8 * 3600_000).toISOString().slice(0, 10);
}

function shiftDays(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

// -------------------------------------------------------------- parsers ----

/** A 4D number as the operators print it, or "" for a blank slot. */
function number(value: unknown): string {
  const text = String(value ?? '').trim();
  return /^\d{4}$/.test(text) ? text : '';
}

function numbers(values: unknown[]): string[] {
  return values.map(number).filter(Boolean);
}

type Raw = Record<string, unknown>;

/** One record off magnum4d.my's results JSON. */
export function parseMagnumDraw(raw: Raw): Draw {
  const iso = normaliseDate(String(raw.DrawDate ?? ''));
  const pick = (prefix: string, count: number) =>
    Array.from({ length: count }, (_, i) => raw[`${prefix}${i + 1}`]);
  return new Draw(
    'magnum',
    String(raw.DrawID ?? '').trim(),
    iso,
    dayOf(iso),
    number(raw.FirstPrize),
    number(raw.SecondPrize),
    number(raw.ThirdPrize),
    numbers(pick('Special', 13)),
    numbers(pick('Console', 10)),
    raw.Official === undefined ? true : Boolean(raw.Official),
  );
}

/** A /results/latest or /results/past/... body -> draws, newest first. */
export function parseMagnum(body: string): Draw[] {
  const records = JSON.parse(body) as Raw[];
  return records
    .filter((r) => r.DrawDate)
    .map(parseMagnumDraw)
    .sort((a, b) => (a.drawDate < b.drawDate ? 1 : a.drawDate > b.drawDate ? -1 : 0));
}

/** The /callpassresult reply -> the signed URL that holds the draw. */
export function parseDamacaiLink(body: string): string {
  return (JSON.parse(body) as { link: string }).link;
}

/** A Da Ma Cai draw blob -> its 1+3D draw, or null if it holds none. */
export function parseDamacai(body: string): Draw | null {
  const raw = JSON.parse(body) as Raw | null;
  if (!raw || !raw.drawDate) return null;
  const iso = normaliseDate(String(raw.drawDate));
  return new Draw(
    'damacai',
    String(raw.drawNo ?? '').trim(),
    iso,
    dayOf(iso),
    number(raw.p1),
    number(raw.p2),
    number(raw.p3),
    numbers((raw.starterList as unknown[]) ?? []),
    numbers((raw.consolidateList as unknown[]) ?? []),
    // Older draws carry "C" where newer ones spell it out.
    ['C', 'COMPLETED'].includes(String(raw.status ?? '').toUpperCase()),
  );
}

// -------------------------------------------------------------- network ----

/**
 * GET one URL as text. A 404 is null; any other failure throws.
 *
 * Da Ma Cai's blob host now and then stalls on a request it answers at once
 * when asked again, so a timeout is retried before giving up.
 */
export async function fetchText(
  url: string,
  headers: Record<string, string> = {},
  timeout = 15,
): Promise<string | null> {
  const where = url.split('?')[0];
  let res: Response | undefined;
  for (let attempt = 0; !res; attempt++) {
    try {
      res = await fetch(url, {
        headers: { 'User-Agent': USER_AGENT, ...headers },
        signal: AbortSignal.timeout(timeout * 1000),
      });
    } catch (e) {
      const timedOut = (e as Error).name === 'TimeoutError';
      if (!timedOut || attempt === FETCH_ATTEMPTS - 1) {
        return die(`network error fetching ${where}: ${(e as Error).message}`);
      }
    }
  }
  if (res.status === 404) return null;
  if (!res.ok) die(`HTTP ${res.status} from ${where}`);
  return (await res.text()).replace(/^﻿/, '');
}

export async function magnumLatest(): Promise<Draw | null> {
  const body = await fetchText(`${MAGNUM_BASE}/results/latest`);
  return (body ? parseMagnum(body) : [])[0] ?? null;
}

/** The last `count` Magnum draws on or before a date (default: today). */
export async function magnumRecent(count: number, onOrBefore?: string): Promise<Draw[]> {
  const day = onOrBefore ?? todayMyt();
  const body = await fetchText(`${MAGNUM_BASE}/results/past/between-dates/null/${day}/${count}`);
  return body ? parseMagnum(body) : [];
}

/** The Magnum draw held on exactly this date, if there was one. */
export async function magnumDraw(day: string): Promise<Draw | null> {
  return (await magnumRecent(1, day)).find((d) => d.drawDate === day) ?? null;
}

/** Every Magnum draw from `start` to `end`, both inclusive, newest first. */
export async function magnumBetween(start: string, end: string): Promise<Draw[]> {
  // At most one draw a day, so the day count is a safe upper bound.
  const limit = (Date.parse(end) - Date.parse(start)) / 86_400_000 + 1;
  if (limit < 1) return [];
  const body = await fetchText(`${MAGNUM_BASE}/results/past/between-dates/${start}/${end}/${limit}`);
  return body ? parseMagnum(body) : [];
}

/** The Da Ma Cai draw held on exactly this date, if there was one. */
export async function damacaiDraw(day: string): Promise<Draw | null> {
  if (day < DAMACAI_FIRST) return null;
  const link = await fetchText(
    `${DAMACAI_BASE}/callpassresult?pastdate=${day.replaceAll('-', '')}`,
    DAMACAI_HEADERS,
  );
  if (!link || !link.trimStart().startsWith('{')) die('Da Ma Cai did not return a result link');
  const body = await fetchText(parseDamacaiLink(link!));
  return body ? parseDamacai(body) : null;
}

export async function damacaiLatest(today?: string): Promise<Draw | null> {
  const start = today ?? todayMyt();
  for (let back = 0; back <= DAMACAI_LOOKBACK; back++) {
    const found = await damacaiDraw(shiftDays(start, -back));
    if (found) return found;
  }
  return null;
}

// -------------------------------------------------------------- queries ----

/** Every chosen operator's draw on one date. Empty on a non-draw day. */
export async function drawsOn(day: string, operators: readonly Operator[] = OPERATORS): Promise<Draw[]> {
  const fetchers = { magnum: magnumDraw, damacai: damacaiDraw };
  const out: Draw[] = [];
  for (const op of operators) {
    const found = await fetchers[op](day);
    if (found) out.push(found);
  }
  return out;
}

/** Each chosen operator's most recent published draw. */
export async function latest(operators: readonly Operator[] = OPERATORS): Promise<Draw[]> {
  const out: Draw[] = [];
  if (operators.includes('magnum')) {
    const found = await magnumLatest();
    if (found) out.push(found);
  }
  if (operators.includes('damacai')) {
    const found = await damacaiLatest();
    if (found) out.push(found);
  }
  return out;
}

/**
 * The last `count` draw dates, for every chosen operator.
 *
 * The draw calendar comes from Magnum, which lists past draws by count. The
 * operators draw on the same Wed, Sat and Sun, so Da Ma Cai is then asked for
 * each of those dates. Some special draws are Magnum's alone; Da Ma Cai simply
 * has nothing on those dates.
 */
export async function recent(
  count: number,
  operators: readonly Operator[] = OPERATORS,
  progress?: Progress,
): Promise<Draw[]> {
  return withDamacai(await magnumRecent(count), operators, progress);
}

/**
 * Every draw from `start` to `end`, both inclusive, for every chosen operator.
 *
 * The calendar comes from Magnum, as in `recent`.
 */
export async function between(
  start: string,
  end: string,
  operators: readonly Operator[] = OPERATORS,
  progress?: Progress,
): Promise<Draw[]> {
  return withDamacai(await magnumBetween(start, end), operators, progress);
}

/**
 * Every draw each chosen operator still publishes, up to today.
 *
 * Magnum's whole history is one request. Da Ma Cai's is two requests per date
 * since 2005, a few thousand in all, so this takes a while; pass
 * `progress(done, total)` to follow it.
 */
export async function fetchAll(
  operators: readonly Operator[] = OPERATORS,
  progress?: Progress,
): Promise<Draw[]> {
  return between(MAGNUM_FIRST, todayMyt(), operators, progress);
}

/** Magnum's draws (if chosen) plus Da Ma Cai's on the same dates (if chosen). */
async function withDamacai(
  calendar: Draw[],
  operators: readonly Operator[],
  progress?: Progress,
): Promise<Draw[]> {
  const out = operators.includes('magnum') ? [...calendar] : [];
  if (operators.includes('damacai')) {
    const dates = calendar.map((m) => m.drawDate).filter((d) => d >= DAMACAI_FIRST);
    const found: (Draw | null)[] = new Array(dates.length).fill(null);
    let next = 0;
    let done = 0;
    const worker = async () => {
      while (next < dates.length) {
        const i = next++;
        found[i] = await damacaiDraw(dates[i]!);
        progress?.(++done, dates.length);
      }
    };
    await Promise.all(Array.from({ length: DAMACAI_WORKERS }, worker));
    for (const d of found) if (d) out.push(d);
  }
  return sortDraws(out);
}

/** Newest date first, then in OPERATORS order. */
export function sortDraws(draws: Draw[]): Draw[] {
  return [...draws].sort((a, b) => {
    if (a.drawDate !== b.drawDate) return a.drawDate < b.drawDate ? 1 : -1;
    return OPERATORS.indexOf(a.operator) - OPERATORS.indexOf(b.operator);
  });
}

/** Draws as the CLI prints them: {"draws": [...]}, newest first, indent 2. */
export function toJson(draws: Draw[]): string {
  return JSON.stringify({ draws: sortDraws(draws).map((d) => d.toDict()) }, null, 2);
}

/** Write draws to a JSON file, creating its folder if needed. Returns the path. */
export function saveJson(draws: Draw[], path: string): string {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, toJson(draws) + '\n', 'utf8');
  return path;
}

// ------------------------------------------------------------------- cli ----

const USAGE = `usage: node fourd_scraper.ts [date] [--recent N] [--from DATE [--to DATE]] [--all]
                               [--operator magnum|damacai|all] [--out FILE]

Malaysian 4D draw results from Magnum 4D and Da Ma Cai.

  date            draw date as YYYY-MM-DD (default: latest draw)
  --recent N      the last N draw dates instead
  --from DATE     every draw from this date on
  --to DATE       with --from, the last date (default: today)
  --all           every draw ever published (slow for Da Ma Cai)
  --operator OP   one operator only (default: all)
  --out FILE      write the JSON to this file instead of stdout

Output is always JSON: {"draws": [...]}.`;

export async function main(argv: string[] = process.argv.slice(2)): Promise<void> {
  if (argv.includes('--help') || argv.includes('-h')) {
    console.log(USAGE);
    return;
  }
  let day: string | undefined;
  let count: number | undefined;
  let operator = 'all';
  let start: string | undefined;
  let end: string | undefined;
  let all = false;
  let out: string | undefined;
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i]!;
    if (token === '--recent') count = Number(argv[++i]);
    else if (token === '--operator') operator = argv[++i] ?? '';
    else if (token === '--from') start = argv[++i] ?? '';
    else if (token === '--to') end = argv[++i] ?? '';
    else if (token === '--all') all = true;
    else if (token === '--out') out = argv[++i] ?? '';
    else if (token.startsWith('-')) die(`unknown option ${token}\n\n${USAGE}`);
    else if (day === undefined) day = token;
    else die(USAGE);
  }
  if (operator !== 'all' && !(OPERATORS as readonly string[]).includes(operator)) {
    die(`--operator must be one of ${[...OPERATORS, 'all'].join(', ')}`);
  }
  if ([day, count, start, all || undefined].filter((x) => x !== undefined).length > 1) {
    die('give one of: a date, --recent, --from, --all');
  }
  if (end !== undefined && start === undefined) die('--to needs --from');
  if (count !== undefined && !(Number.isInteger(count) && count >= 1)) die('--recent must be at least 1');

  const operators = operator === 'all' ? OPERATORS : [operator as Operator];
  const report: Progress = (done, total) => {
    if (done % PROGRESS_EVERY === 0 || done === total) console.error(`damacai: ${done}/${total} dates`);
  };
  let draws: Draw[];
  if (all) {
    draws = await fetchAll(operators, report);
  } else if (start !== undefined) {
    const from = normaliseDate(start);
    const to = end !== undefined ? normaliseDate(end) : todayMyt();
    if (from > to) die('--from is after --to');
    draws = await between(from, to, operators, report);
  } else if (count !== undefined) {
    draws = await recent(count, operators, report);
  } else if (day !== undefined) {
    const iso = normaliseDate(day);
    draws = await drawsOn(iso, operators);
    if (!draws.length) console.error(`no draw on ${iso}`);
  } else {
    draws = await latest(operators);
  }
  if (out) {
    saveJson(draws, out);
    console.error(`saved ${draws.length} draws to ${out}`);
  } else {
    console.log(toJson(draws));
  }
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
