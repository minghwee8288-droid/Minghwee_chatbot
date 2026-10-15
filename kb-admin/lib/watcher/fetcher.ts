import { parseRobots, robotsAllows, type RobotsRules } from './robots.ts';

/**
 * Fetching a watched page, politely. No database and no environment: the
 * network call, the clock and the sleep are passed in, so scripts/selfcheck.mjs
 * drives every path with saved fixtures and never opens a connection.
 *
 *   - a clear user-agent naming the agency and the tool;
 *   - https only, public hosts only (no IP literals, no localhost/.local/.internal);
 *   - robots.txt read first (cached per host for the run) and obeyed, including
 *     Crawl-delay; a robots.txt that cannot be fetched (5xx, network) means
 *     "do not fetch this time" - a 4xx (no robots.txt) means allowed;
 *   - one request at a time per host, at least MIN_GAP_MS apart (or the
 *     Crawl-delay, whichever is longer);
 *   - a timeout on every request, at most MAX_ATTEMPTS attempts with
 *     exponential backoff on network errors, 429 and 5xx (Retry-After honoured,
 *     capped); never a retry on any other 4xx;
 *   - redirects followed only to https on a public host, at most 5;
 *   - only text/html or text/plain, at most MAX_BYTES.
 * A refusal or failure comes back as { ok: false, error } - the caller records
 * it as a failed snapshot, so it is shown in kb-admin and never silently lost.
 */

export const USER_AGENT = 'MingHweeKBWatcher/1.0 (+knowledge-base change watcher for Ming Hwee Employment Agency; robots.txt respected)';
export const AGENT_TOKEN = 'MingHweeKBWatcher';
export const TIMEOUT_MS = 20_000;
export const MAX_ATTEMPTS = 3;
export const MIN_GAP_MS = 10_000;
export const MAX_BYTES = 3 * 1024 * 1024;
const BACKOFF_BASE_MS = 2_000;
const MAX_RETRY_AFTER_MS = 60_000;

export type FetchLike = (url: string, init: { method: 'GET'; headers: Record<string, string>; redirect: 'manual'; signal: AbortSignal }) => Promise<{
  status: number;
  headers: { get(name: string): string | null };
  text(): Promise<string>;
}>;

export type FetchDeps = { fetch: FetchLike; now: () => number; sleep: (ms: number) => Promise<void> };

export type PageResult =
  | { ok: true; status: number; html: string; finalUrl: string }
  | { ok: false; status: number | null; error: string };

/** The reason this address may not be fetched, or null. */
export function addressProblem(raw: string): string | null {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return 'not a web address';
  }
  if (url.protocol !== 'https:') return 'only https:// pages are watched';
  const host = url.hostname.toLowerCase();
  if (!/^[a-z0-9.-]+\.[a-z]{2,}$/.test(host)) return 'the address must name a public host';
  if (/^[0-9.]+$/.test(host) || host === 'localhost' || /\.(local|internal|localhost)$/.test(host)) return 'the address must be a public web page';
  if (url.username || url.password) return 'the address may not carry a user name or password';
  return null;
}

/** Per-run state: robots.txt per host and the last request time per host. */
export class PoliteFetcher {
  private robots = new Map<string, RobotsRules | 'unavailable'>();
  private lastHit = new Map<string, number>();
  private deps: FetchDeps;
  constructor(deps: FetchDeps) {
    this.deps = deps;
  }

  private async wait(host: string, delayMs: number): Promise<void> {
    const last = this.lastHit.get(host);
    if (last !== undefined) {
      const gap = Math.max(MIN_GAP_MS, delayMs) - (this.deps.now() - last);
      if (gap > 0) await this.deps.sleep(gap);
    }
    this.lastHit.set(host, this.deps.now());
  }

  /** One GET with timeout, retries and backoff. Redirects are returned, not followed. */
  private async attempt(url: string, host: string, delayMs: number): Promise<{ status: number; location: string | null; type: string; body: string } | { error: string; status: number | null }> {
    let lastError = 'no attempt made';
    let lastStatus: number | null = null;
    for (let i = 0; i < MAX_ATTEMPTS; i++) {
      if (i > 0) await this.deps.sleep(BACKOFF_BASE_MS * 2 ** (i - 1));
      await this.wait(host, delayMs);
      try {
        const res = await this.deps.fetch(url, {
          method: 'GET',
          headers: { 'user-agent': USER_AGENT, accept: 'text/html,text/plain;q=0.9,*/*;q=0.1', 'accept-language': 'en' },
          redirect: 'manual',
          signal: AbortSignal.timeout(TIMEOUT_MS),
        });
        lastStatus = res.status;
        if (res.status === 429 || res.status >= 500) {
          lastError = `HTTP ${res.status}`;
          const after = Number(res.headers.get('retry-after'));
          if (Number.isFinite(after) && after > 0) await this.deps.sleep(Math.min(after * 1000, MAX_RETRY_AFTER_MS));
          continue;
        }
        const body = res.status >= 200 && res.status < 300 ? await res.text() : '';
        return { status: res.status, location: res.headers.get('location'), type: (res.headers.get('content-type') ?? '').toLowerCase(), body };
      } catch (e) {
        lastError = (e as Error)?.name === 'TimeoutError' ? `timed out after ${TIMEOUT_MS / 1000}s` : 'network error';
      }
    }
    return { error: `${lastError} (after ${MAX_ATTEMPTS} attempts)`, status: lastStatus };
  }

  private async robotsFor(origin: string, host: string): Promise<RobotsRules | 'unavailable'> {
    const cached = this.robots.get(host);
    if (cached) return cached;
    const r = await this.attempt(`${origin}/robots.txt`, host, 0);
    let rules: RobotsRules | 'unavailable';
    if ('error' in r) rules = 'unavailable';
    else if (r.status >= 200 && r.status < 300) rules = parseRobots(r.body.slice(0, 500_000), AGENT_TOKEN);
    else if (r.status >= 400 && r.status < 500) rules = { rules: [], crawlDelaySeconds: null };
    else rules = 'unavailable'; // a redirect or anything odd: be cautious
    this.robots.set(host, rules);
    return rules;
  }

  async fetchPage(rawUrl: string): Promise<PageResult> {
    let current = rawUrl;
    for (let hop = 0; hop <= 5; hop++) {
      const problem = addressProblem(current);
      if (problem) return { ok: false, status: null, error: hop ? `redirected to a refused address: ${problem}` : problem };
      const url = new URL(current);
      const host = url.hostname.toLowerCase();
      const robots = await this.robotsFor(url.origin, host);
      if (robots === 'unavailable') return { ok: false, status: null, error: `robots.txt for ${host} could not be read; not fetched this time` };
      if (!robotsAllows(robots, url.pathname + url.search)) return { ok: false, status: null, error: `robots.txt for ${host} does not allow this page` };
      const delayMs = (robots.crawlDelaySeconds ?? 0) * 1000;
      const r = await this.attempt(url.toString(), host, delayMs);
      if ('error' in r) return { ok: false, status: r.status, error: r.error };
      if (r.status >= 300 && r.status < 400 && r.location) {
        current = new URL(r.location, url).toString();
        continue;
      }
      if (r.status < 200 || r.status >= 300) return { ok: false, status: r.status, error: `HTTP ${r.status}` };
      if (r.type && !/text\/html|text\/plain|application\/xhtml\+xml/.test(r.type)) {
        return { ok: false, status: r.status, error: `not a web page (${r.type.split(';')[0]})` };
      }
      if (r.body.length > MAX_BYTES) return { ok: false, status: r.status, error: 'the page is larger than 3 MB' };
      return { ok: true, status: r.status, html: r.body, finalUrl: url.toString() };
    }
    return { ok: false, status: null, error: 'too many redirects' };
  }
}
