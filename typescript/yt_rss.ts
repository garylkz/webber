#!/usr/bin/env node -T
// Convert a YouTube channel URL into its RSS feed URL.
//
// Single file, zero dependencies, no build step. Node 22.6+ (developed against
// Node 24 LTS) runs this directly: Node strips the types at load time.
//
//     node yt_rss.ts "https://youtube.com/@handle"    # handle to a feed
//     node yt_rss.ts "https://youtube.com/channel/UC..." # already an id
//     node yt_rss.ts "https://youtube.com/@handle" --fetch
//
// Also importable:
//
//     import { rssForChannel, channelIdFromUrl } from './yt_rss.ts';
//     await rssForChannel('UC...');

const FEEDS_URL = 'https://www.youtube.com/feeds/videos.xml';
const ALLOWED_SCHEMES = new Set(['http:', 'https:']);
const ALLOWED_HOSTS = new Set([
  'youtube.com',
  'www.youtube.com',
  'm.youtube.com',
  'music.youtube.com',
  'youtu.be',
]);

const CHANNEL_RE = /(?:youtube\.com|youtu\.be)\/(channel|user)\/([^/?]+)/;
const CHANNEL_ID_RE = /"channelId":"(UC[\w-]{22})"/g;
const EXTERNAL_ID_RE = /"externalId":"(UC[\w-]{22})"/g;
const CANONICAL_RE = /rel="canonical" href="[^"]*?\/channel\/(UC[\w-]{22})"/g;

/** Thrown when a run cannot continue; main() turns it into one stderr line. */
export class YtRssError extends Error {}

const die = (message: string): never => {
  throw new YtRssError(message);
};

/** The official feed URL for a channel id. */
export function rssForChannel(channelId: string): string {
  return `${FEEDS_URL}?channel_id=${channelId}`;
}

/**
 * Force https for a bare host, then refuse non-http(s) schemes and non-YouTube
 * hosts. This blocks file:// and SSRF-style fetches.
 */
export function normaliseUrl(url: string): string {
  const withScheme = /^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(url) ? url : `https://${url}`;
  let parsed: URL;
  try {
    parsed = new URL(withScheme);
  } catch {
    die(`Refusing to parse URL: ${url}`);
  }
  if (!ALLOWED_SCHEMES.has(parsed.protocol)) {
    die(`Refusing URL with scheme '${parsed.protocol}': ${url}`);
  }
  const host = parsed.hostname.toLowerCase().replace(/\.$/, '');
  if (!ALLOWED_HOSTS.has(host)) {
    die(`Refusing to fetch non-YouTube host '${host}': ${url}`);
  }
  return withScheme;
}

/** Fetch one page as text. */
export async function fetchPage(url: string, timeout = 30): Promise<string> {
  const res = await fetch(url, {
    headers: { 'User-Agent': 'Mozilla/5.0' },
    signal: AbortSignal.timeout(timeout * 1000),
  });
  if (!res.ok) die(`Failed to fetch ${url}: HTTP ${res.status}`);
  return res.text();
}

/** Every match of a global regex, as an array. */
function matchAll(source: string, re: RegExp): string[] {
  const found: string[] = [];
  for (const match of source.matchAll(re)) {
    found.push(match[1] as string);
  }
  return found;
}

/** The most common of the ids a page offers, which is the real one. */
function mostCommon(values: string[]): string | null {
  if (!values.length) return null;
  const counts = new Map<string, number>();
  for (const value of values) counts.set(value, (counts.get(value) ?? 0) + 1);
  return [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? null;
}

/**
 * The channel id for any YouTube channel URL.
 *
 * A `/channel/UC...` URL carries its id. A handle, a `/user/...` name or a bare
 * `@name` does not, so the page is fetched and read for it. `/user/...` names
 * are resolved too rather than passed through, because the feed endpoint takes a
 * channel id and not a user name.
 */
export async function channelIdFromUrl(url: string): Promise<string | null> {
  const direct = CHANNEL_RE.exec(url);
  if (direct) return direct[2] as string;

  const pageUrl = normaliseUrl(url);
  let html: string;
  try {
    html = await fetchPage(pageUrl);
  } catch (e) {
    die(`Failed to fetch ${pageUrl}: ${(e as Error).message}`);
  }

  for (const re of [CHANNEL_ID_RE, EXTERNAL_ID_RE, CANONICAL_RE]) {
    const found = mostCommon(matchAll(html, re));
    if (found) return found;
  }
  return null;
}

const USAGE = `usage: node yt_rss.ts <channel-url> [--fetch]

Convert a YouTube channel, user or @handle URL into its official RSS feed URL.

  --fetch   also fetch and print the feed XML

examples:
  node yt_rss.ts "https://youtube.com/@handle"
  node yt_rss.ts "https://www.youtube.com/channel/UC..." --fetch`;

const HANDLES = new Set(['--fetch', '-f', '--help', '-h']);

export async function main(argv: string[] = process.argv.slice(2)): Promise<void> {
  const url = argv.find((token) => !HANDLES.has(token));
  if (argv.includes('--help') || argv.includes('-h')) {
    console.log(USAGE);
    return;
  }
  if (!url) die(USAGE);

  const channelId = await channelIdFromUrl(url);
  if (!channelId) die('Could not determine the channel ID from that URL.');

  const rssUrl = rssForChannel(channelId);
  console.log(rssUrl);

  if (argv.includes('--fetch') || argv.includes('-f')) {
    console.log(await fetchPage(rssUrl));
  }
}

const entry = process.argv[1];
if (entry && import.meta.url === `file://${entry}`) {
  main().catch((e: unknown) => {
    console.error(e instanceof Error ? e.message : String(e));
    process.exit(1);
  });
}