/**
 * Why a fetched page gave no readable text. PURE: no network, no database, no
 * environment. Added 2026-10-15 after a MOM page failed with only "the page
 * has no readable main text", which said nothing about what to do next.
 *
 *   blocked      a bot-protection or challenge page (Cloudflare, Akamai,
 *                Incapsula, a captcha). The watcher never tries to get past
 *                one: it reports "Blocked by site" and suggests another source.
 *   maintenance  the site is down for maintenance, or redirected us to a
 *                /maintenance/ page.
 *   script       a JavaScript-built page: an empty app shell whose text only
 *                appears in a browser. The page renderer (render-service/, on
 *                our server) can read it, when it is configured.
 *   empty        none of those: a page with nothing we can read.
 */

export type EmptyKind = 'blocked' | 'maintenance' | 'script' | 'empty';

const CHALLENGE = [
  /<title>\s*(?:just a moment|attention required|access denied|request rejected|are you a robot|security check|pardon our interruption)\b/i,
  /\bcf-(?:browser-verification|challenge|chl-|turnstile)|challenges\.cloudflare\.com|__cf_chl_/i,
  /_Incapsula_Resource|incapsula incident id|Request unsuccessful\. Incapsula/i,
  /\bpx-captcha\b|perimeterx|\bdatadome\b|captcha-delivery\.com/i,
  /\bg-recaptcha\b|\bh-captcha\b|hcaptcha\.com\/1\/api/i,
  /Reference #\d+\.[0-9a-f]+\.\d+\.[0-9a-f]+|errors\.edgesuite\.net/i, // Akamai "Access Denied"
  /The requested URL was rejected\. Please consult with your administrator/i, // F5 / ASM
];

const MAINTENANCE_URL = /\/(?:maintenance|under-maintenance|sorry|outage)(?:[/.?#]|$)/i;
const MAINTENANCE_TEXT = /<title>[^<]*\b(?:maintenance|temporarily unavailable|be right back|down for scheduled)\b/i;

// An app shell: a mount point and a module/bundle script. Only asked about a
// page that already gave no readable text, so the mount point alone is enough.
const APP_MOUNT = /<(?:div|main)\b[^>]*\bid\s*=\s*["'](?:app|root|__next|__nuxt|___gatsby|svelte)["']/i;
const BUNDLE = /<script\b[^>]*\b(?:type\s*=\s*["']module["']|src\s*=\s*["'][^"']*(?:bundle|main|index|app|chunk)[^"']*\.js)/i;
const NEEDS_JS = /\b(?:enable javascript|requires javascript|javascript (?:is )?(?:required|disabled)|you need to enable javascript)\b/i;

export function isChallengePage(html: string): boolean {
  const head = String(html ?? '').slice(0, 200_000);
  return CHALLENGE.some((re) => re.test(head));
}

export function classifyEmptyPage(html: string, finalUrl: string | null): EmptyKind {
  const raw = String(html ?? '');
  if (isChallengePage(raw)) return 'blocked';
  let path = '';
  try {
    path = finalUrl ? new URL(finalUrl).pathname : '';
  } catch {
    path = '';
  }
  if (MAINTENANCE_URL.test(path) || MAINTENANCE_TEXT.test(raw)) return 'maintenance';
  if ((APP_MOUNT.test(raw) && BUNDLE.test(raw)) || NEEDS_JS.test(raw)) return 'script';
  return 'empty';
}

/** The message recorded on the failed snapshot (shown on the Watched pages screen). */
export function emptyPageMessage(kind: EmptyKind, finalUrl: string | null, opts: { renderer: boolean; rendered: boolean }): string {
  const where = finalUrl ? ` (${finalUrl})` : '';
  switch (kind) {
    case 'blocked':
      return `Blocked by site: the page answered with a bot-protection check${where}. The watcher does not try to get past it. Try the page's PDF or print version, or another official page with the same information (for MOM: its press releases or data.gov.sg).`;
    case 'maintenance':
      return `The page is under maintenance${where}. It will be read again on the next check.`;
    case 'script':
      return opts.rendered
        ? `The page is built by JavaScript and still had no readable text after rendering${where}.`
        : opts.renderer
          ? `The page is built by JavaScript and the page renderer could not read it${where}.`
          : `The page is built by JavaScript and needs the page renderer, which is not set up (KB_RENDER_URL)${where}.`;
    default:
      return opts.rendered ? `The page has no readable main text, even after rendering${where}.` : 'the page has no readable main text';
  }
}
