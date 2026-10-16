// The render service's pure rules: configuration, the secret, and which
// addresses may be opened. No network, no browser - kb-admin/scripts/selfcheck.mjs
// imports this file and checks it offline.
import { timingSafeEqual } from 'node:crypto';
import { isIP } from 'node:net';

// The same user-agent as kb-admin's watcher (kb-admin/lib/watcher/fetcher.ts USER_AGENT).
export const USER_AGENT = 'MingHweeKBWatcher/1.0 (+knowledge-base change watcher for Ming Hwee Employment Agency; robots.txt respected)';
export const MAX_HTML = 3 * 1024 * 1024;

export class ConfigError extends Error {}

export function readConfig(env) {
  const secret = String(env.RENDER_SECRET ?? '').trim();
  if (secret.length < 32) throw new ConfigError('render-service refuses to start: RENDER_SECRET must be set, 32+ characters');
  const port = Number(env.PORT ?? 8100);
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new ConfigError('render-service refuses to start: PORT is not a port');
  const minGapMs = Number(env.RENDER_MIN_GAP_MS ?? 10_000);
  const timeoutMs = Number(env.RENDER_TIMEOUT_MS ?? 30_000);
  if (!(minGapMs >= 10_000)) throw new ConfigError('render-service refuses to start: RENDER_MIN_GAP_MS may not be under 10000 (the watcher\'s own per-host gap)');
  if (!(timeoutMs >= 5_000 && timeoutMs <= 55_000)) throw new ConfigError('render-service refuses to start: RENDER_TIMEOUT_MS must be 5000-55000');
  return {
    secret, port, host: String(env.HOST ?? '0.0.0.0'), minGapMs, timeoutMs,
    channel: String(env.RENDER_CHROME_CHANNEL ?? '').trim() || null,
    allowedHosts: readAllowedHosts(env.RENDER_ALLOWED_HOSTS),
  };
}

/**
 * Which sites may be rendered (2026-10-16). Default: the Indonesian embassy
 * only (kemlu.go.id), the one watched site built by JavaScript. A comma list
 * of hosts; each also allows its subdomains. "*" switches the allowlist off
 * (any public https host, the 2026-10-15 behaviour). Applies to the page
 * address and to where it ends up after redirects - not to the scripts and
 * data the page itself loads, which the private-address check still covers.
 */
export const DEFAULT_ALLOWED_HOSTS = ['kemlu.go.id'];

export function readAllowedHosts(raw) {
  const value = String(raw ?? '').trim().toLowerCase();
  if (value === '*') return null;
  const hosts = (value ? value.split(',') : DEFAULT_ALLOWED_HOSTS).map((h) => h.trim().replace(/^\.+|\.+$/g, '')).filter(Boolean);
  for (const h of hosts) {
    if (!/^[a-z0-9.-]+\.[a-z]{2,}$/.test(h)) throw new ConfigError(`render-service refuses to start: RENDER_ALLOWED_HOSTS has an invalid host (${h})`);
  }
  if (!hosts.length) throw new ConfigError('render-service refuses to start: RENDER_ALLOWED_HOSTS names no host (use * to allow any public host)');
  return hosts;
}

/** May this host be rendered? `allowed` null = any (the allowlist is off). */
export function hostAllowed(host, allowed) {
  if (allowed === null) return true;
  const h = String(host ?? '').toLowerCase().replace(/\.$/, '');
  return allowed.some((a) => h === a || h.endsWith(`.${a}`));
}

/** "Authorization: Bearer <secret>", compared in constant time. */
export function secretMatches(header, secret) {
  const m = /^Bearer (.+)$/.exec(String(header ?? ''));
  if (!m) return false;
  const a = Buffer.from(m[1]);
  const b = Buffer.from(secret);
  return a.length === b.length && timingSafeEqual(a, b);
}

/** The reason this page address may not be rendered, or null (mirrors kb-admin's addressProblem). */
export function addressProblem(raw) {
  let url;
  try {
    url = new URL(raw);
  } catch {
    return 'not a web address';
  }
  if (url.protocol !== 'https:') return 'only https:// pages are rendered';
  const host = url.hostname.toLowerCase();
  if (!/^[a-z0-9.-]+\.[a-z]{2,}$/.test(host)) return 'the address must name a public host';
  if (/^[0-9.]+$/.test(host) || host === 'localhost' || /\.(local|internal|localhost)$/.test(host)) return 'the address must be a public web page';
  if (url.username || url.password) return 'the address may not carry a user name or password';
  return null;
}

/**
 * Is this IP address private, loopback, link-local or otherwise not on the
 * public internet? The browser runs on the bot's own server, so a page (or a
 * script on it) must never reach the bot, the database or the cloud metadata
 * service through it.
 */
export function isPrivateAddress(ip) {
  const v = isIP(ip);
  if (v === 4) {
    const [a, b] = ip.split('.').map(Number);
    return a === 0 || a === 10 || a === 127 || (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 192 && b === 0) || (a === 198 && (b === 18 || b === 19)) || a >= 224;
  }
  if (v === 6) {
    const x = ip.toLowerCase();
    if (x === '::' || x === '::1') return true;
    const mapped = /^::ffff:(\d+\.\d+\.\d+\.\d+)$/.exec(x);
    if (mapped) return isPrivateAddress(mapped[1]);
    return /^(fc|fd|fe8|fe9|fea|feb|ff)/.test(x);
  }
  return true; // not an IP at all: treat as unsafe
}
