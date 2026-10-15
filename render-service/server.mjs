// The page renderer for kb-admin's KB watcher (2026-10-15). Renders a
// JavaScript-built page in headless Chromium and returns its HTML. Runs on our
// own server beside the bot, in its own container; it imports nothing from the
// bot and the bot does not call it. See README.md.
//
//   POST /render   Authorization: Bearer <RENDER_SECRET>   {"url": "https://..."}
//                  -> {ok: true, status, finalUrl, html} | {ok: false, error}
//   GET  /health   -> {ok: true}
//
// kb-admin decides whether a page may be fetched at all (robots.txt, its own
// per-host gap) before it calls this. Here: the same user-agent, one page at a
// time, at least RENDER_MIN_GAP_MS between two renders of one host, https and
// public hosts only - for the page AND every request the page makes - and no
// images, media, fonts, downloads or service workers.
import http from 'node:http';
import { lookup } from 'node:dns/promises';
import { isIP } from 'node:net';
import { chromium } from 'playwright';
import { addressProblem, ConfigError, isPrivateAddress, MAX_HTML, readConfig, secretMatches, USER_AGENT } from './guard.mjs';

let cfg;
try {
  cfg = readConfig(process.env);
} catch (e) {
  console.error(e instanceof ConfigError ? e.message : String(e));
  process.exit(1);
}

let browser = null;
async function getBrowser() {
  if (!browser || !browser.isConnected()) {
    browser = await chromium.launch({ headless: true, args: ['--disable-dev-shm-usage'], ...(cfg.channel ? { channel: cfg.channel } : {}) });
  }
  return browser;
}

const publicHost = new Map(); // host -> { ok, at }
async function hostIsPublic(host) {
  const h = host.replace(/^\[|\]$/g, '').toLowerCase();
  if (isIP(h)) return !isPrivateAddress(h);
  if (h === 'localhost' || /\.(local|internal|localhost)$/.test(h)) return false;
  const hit = publicHost.get(h);
  if (hit && Date.now() - hit.at < 60_000) return hit.ok;
  let ok = false;
  try {
    const addrs = await lookup(h, { all: true });
    ok = addrs.length > 0 && addrs.every((a) => !isPrivateAddress(a.address));
  } catch {
    ok = false;
  }
  publicHost.set(h, { ok, at: Date.now() });
  return ok;
}

const lastHit = new Map();
let queue = Promise.resolve();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function render(url) {
  const problem = addressProblem(url);
  if (problem) return { ok: false, error: problem };
  const host = new URL(url).hostname.toLowerCase();
  if (!(await hostIsPublic(host))) return { ok: false, error: 'the address does not resolve to a public host' };
  const wait = cfg.minGapMs - (Date.now() - (lastHit.get(host) ?? -Infinity));
  if (wait > 0) await sleep(wait);
  lastHit.set(host, Date.now());

  const context = await (await getBrowser()).newContext({ userAgent: USER_AGENT, serviceWorkers: 'block', acceptDownloads: false, locale: 'en-SG' });
  try {
    await context.route('**/*', async (route) => {
      const req = route.request();
      let u;
      try {
        u = new URL(req.url());
      } catch {
        return route.abort();
      }
      if (u.protocol === 'data:' || u.protocol === 'blob:') return route.continue();
      if (u.protocol !== 'https:' && u.protocol !== 'http:') return route.abort();
      if (['image', 'media', 'font'].includes(req.resourceType())) return route.abort();
      if (!(await hostIsPublic(u.hostname))) return route.abort('blockedbyclient');
      return route.continue();
    });
    const page = await context.newPage();
    const res = await page.goto(url, { waitUntil: 'load', timeout: cfg.timeoutMs });
    await page.waitForLoadState('networkidle', { timeout: 8_000 }).catch(() => undefined);
    // "Network idle" never comes on a page with a live map or analytics
    // polling (the Indonesian embassy's site), and an app shell says
    // "Loading..." until its data arrives: wait until the visible text has
    // stopped changing for 2 seconds and is more than a loading line.
    const deadline = Date.now() + 12_000;
    let last = -1;
    let stableSince = Date.now();
    while (Date.now() < deadline) {
      const n = await page.evaluate(() => (document.body?.innerText ?? '').trim().length).catch(() => 0);
      if (n !== last) {
        last = n;
        stableSince = Date.now();
      } else if (n > 200 && Date.now() - stableSince >= 2_000) break;
      await sleep(500);
    }
    const finalUrl = page.url();
    const finalProblem = addressProblem(finalUrl);
    if (finalProblem) return { ok: false, error: `the page ended on a refused address: ${finalProblem}` };
    const html = await page.content();
    if (html.length > MAX_HTML) return { ok: false, error: 'the rendered page is larger than 3 MB' };
    return { ok: true, status: res?.status() ?? 200, finalUrl, html };
  } catch (e) {
    const msg = String(e?.message ?? e);
    return { ok: false, error: /Timeout/i.test(msg) ? `timed out after ${cfg.timeoutMs / 1000}s` : 'the page could not be rendered' };
  } finally {
    await context.close().catch(() => undefined);
  }
}

function send(res, code, body) {
  const s = JSON.stringify(body);
  res.writeHead(code, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(s), 'cache-control': 'no-store' });
  res.end(s);
}

const server = http.createServer((req, res) => {
  const path = (req.url ?? '').split('?')[0];
  if (req.method === 'GET' && path === '/health') return send(res, 200, { ok: true });
  // Anything without the key looks like nothing is here.
  if (req.method !== 'POST' || path !== '/render' || !secretMatches(req.headers.authorization, cfg.secret)) return send(res, 404, { error: 'not found' });
  let raw = '';
  let tooBig = false;
  req.on('data', (c) => {
    raw += c;
    if (raw.length > 4096) {
      tooBig = true;
      req.destroy();
    }
  });
  req.on('end', () => {
    if (tooBig) return;
    let url = '';
    try {
      url = String(JSON.parse(raw).url ?? '');
    } catch {
      return send(res, 400, { ok: false, error: 'the body must be JSON {"url": "..."}' });
    }
    const started = Date.now();
    // One page at a time.
    queue = queue.then(async () => {
      const out = await render(url).catch(() => ({ ok: false, error: 'the page could not be rendered' }));
      console.log(JSON.stringify({ at: new Date().toISOString(), host: (() => { try { return new URL(url).hostname; } catch { return '?'; } })(), ok: out.ok, ms: Date.now() - started, error: out.ok ? undefined : out.error }));
      send(res, out.ok ? 200 : 422, out);
    });
  });
});

server.listen(cfg.port, cfg.host, () => console.log(`render-service listening on ${cfg.host}:${cfg.port}`));
for (const sig of ['SIGTERM', 'SIGINT']) process.on(sig, async () => { server.close(); await browser?.close().catch(() => undefined); process.exit(0); });
