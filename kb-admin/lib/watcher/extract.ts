/**
 * A web page's MAIN text, as paragraphs. PURE: no network, no database, no
 * environment, so scripts/selfcheck.mjs runs it on saved HTML fixtures.
 *
 * Deliberately dependency-free and conservative:
 *   1. drop what is never content: script, style, noscript, template, svg,
 *      iframe, form controls, a form (unless it wraps the page's main content -
 *      see unwrapPageForms), and the page furniture nav, header, footer, aside, plus
 *      any element whose role, id or class names navigation, a breadcrumb, a
 *      footer, a cookie banner or a "skip to content" link;
 *   2. if the page marks its main content (<main>, role="main", a single
 *      <article>, id="main-content"/"content"), read only that;
 *   3. turn block elements into line breaks, strip the remaining tags, decode
 *      entities, and keep each non-empty line as a paragraph.
 * Lines that are page chrome everywhere ("Back to top", "Share this page",
 * "Last updated ...") are dropped, so they cannot register as a change.
 */

export type Extracted = { title: string; paragraphs: string[]; text: string };

const DROP_WHOLE = ['script', 'style', 'noscript', 'template', 'svg', 'iframe', 'form', 'nav', 'header', 'footer', 'aside', 'select', 'button', 'textarea'];

// Marks of a page's main content: tells a page-wide form wrapper from a real form.
const MAIN_MARK = /<main\b|\brole\s*=\s*["']?main\b|\bid\s*=\s*["'](?:main-content|maincontent)["']/i;

/**
 * ASP.NET WebForms pages (mom.gov.sg among them) put the WHOLE page inside one
 * <form id="mainform">. Dropping every form dropped the page: the MOM levy
 * page came out empty ("no readable main text", 2026-10-15). A form that
 * contains the page's main-content marker is a wrapper, so its tags go and its
 * contents stay; any other form (a search box, a feedback form) is still
 * dropped whole afterwards.
 */
function unwrapPageForms(html: string): string {
  let out = html;
  for (let pass = 0; pass < 6; pass++) {
    const re = /<form\b[^>]*>/gi;
    let match: RegExpExecArray | null;
    let changed = false;
    while ((match = re.exec(out))) {
      const end = closingIndex(out, 'form', match.index + match[0].length);
      const inner = out.slice(match.index + match[0].length, end).replace(/<\/form\s*>$/i, '');
      if (!MAIN_MARK.test(inner)) continue;
      out = out.slice(0, match.index) + ' ' + inner + ' ' + out.slice(end);
      changed = true;
      break;
    }
    if (!changed) break;
  }
  return out;
}

// An opening tag whose attributes mark furniture (navigation, breadcrumbs, cookie banners, footers, skip links).
const FURNITURE_ATTR = /\b(?:role\s*=\s*["']?(?:navigation|banner|contentinfo|search)\b|(?:id|class)\s*=\s*["'][^"']*\b(?:nav|navbar|menu|breadcrumbs?|footer|cookie|consent|skip-link|skip-to|sidebar|share|social)\b[^"']*["'])/i;

const BLOCK = /<\/?(?:p|div|section|article|main|li|ul|ol|h[1-6]|tr|td|th|table|thead|tbody|br|hr|dd|dt|dl|blockquote|pre|figcaption|caption)\b[^>]*>/gi;

const CHROME_LINE = /^(?:loading(?:\.\.\.|…)?|please wait\.*|back to top|top of page|share this page|share|print|skip to (?:main )?content|last (?:updated|reviewed|modified)\b.*|page last (?:updated|reviewed)\b.*|was this (?:page|article) (?:helpful|useful)\??.*|yes|no|cookies?\b.*|accept(?: all)?(?: cookies)?|©.*|copyright\b.*|all rights reserved\.?)$/i;

const ENTITIES: Record<string, string> = {
  amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ', ndash: '–', mdash: '—', hellip: '…',
  rsquo: '’', lsquo: '‘', rdquo: '”', ldquo: '“', bull: '•', middot: '·', copy: '©', reg: '®', trade: '™',
};

export function decodeEntities(s: string): string {
  return s.replace(/&(#x[0-9a-f]+|#[0-9]+|[a-z]+);/gi, (m, e: string) => {
    if (e[0] === '#') {
      const code = e[1].toLowerCase() === 'x' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      return Number.isFinite(code) && code > 0 && code < 0x110000 ? String.fromCodePoint(code) : m;
    }
    return ENTITIES[e.toLowerCase()] ?? m;
  });
}

/** Remove every <tag ...>...</tag> of these names, nested ones included (repeats until stable). */
function dropElements(html: string, names: string[]): string {
  let out = html;
  for (let pass = 0; pass < 6; pass++) {
    const before = out;
    for (const n of names) out = out.replace(new RegExp(`<${n}\\b[^>]*>(?:(?!<${n}\\b)[\\s\\S])*?<\\/${n}\\s*>`, 'gi'), ' ');
    if (out === before) break;
  }
  // An unclosed script/style runs to the end of the document: drop the rest.
  return out.replace(/<(?:script|style)\b[\s\S]*$/i, ' ');
}

/** Remove a furniture element (marked by its attributes) and its contents. */
function dropFurniture(html: string): string {
  let out = html;
  for (let pass = 0; pass < 6; pass++) {
    const m = /<(div|section|ul|ol|p|span|a|table)\b([^>]*)>/gi;
    let changed = false;
    let match: RegExpExecArray | null;
    while ((match = m.exec(out))) {
      if (!FURNITURE_ATTR.test(match[2])) continue;
      const tag = match[1].toLowerCase();
      const end = closingIndex(out, tag, match.index + match[0].length);
      out = out.slice(0, match.index) + ' ' + out.slice(end);
      changed = true;
      break;
    }
    if (!changed) break;
  }
  return out;
}

/** Where the element opened just before `from` closes (counting nested ones of the same tag). */
function closingIndex(html: string, tag: string, from: number): number {
  const re = new RegExp(`<(/?)${tag}\\b[^>]*>`, 'gi');
  re.lastIndex = from;
  let depth = 1;
  let m: RegExpExecArray | null;
  while ((m = re.exec(html))) {
    depth += m[1] ? -1 : 1;
    if (depth === 0) return m.index + m[0].length;
  }
  return html.length;
}

/** The page's marked main content, if it marks one. */
function mainRegion(html: string): string | null {
  const markers = [
    /<main\b[^>]*>/i,
    /<[a-z]+\b[^>]*\brole\s*=\s*["']?main\b[^>]*>/i,
    // A listing page (a news index) has many <article>s; reading only the first
    // would watch one teaser. Only a page with exactly one is marked by it.
    ...((html.match(/<article\b/gi) ?? []).length === 1 ? [/<article\b[^>]*>/i] : []),
    /<[a-z]+\b[^>]*\bid\s*=\s*["'](?:main-content|maincontent|main|content)["'][^>]*>/i,
  ];
  for (const re of markers) {
    const m = re.exec(html);
    if (!m) continue;
    const tag = /^<([a-z]+)/i.exec(m[0])?.[1].toLowerCase() ?? 'div';
    const end = closingIndex(html, tag, m.index + m[0].length);
    const region = html.slice(m.index + m[0].length, end);
    if (region.replace(/<[^>]*>/g, '').trim().length > 80) return region;
  }
  return null;
}

export function normaliseLine(s: string): string {
  return s.replace(/[  -​  　\t\r\f\v ]+/g, ' ').trim();
}

export function extractMainText(html: string): Extracted {
  const raw = String(html ?? '');
  const titleMatch = /<title\b[^>]*>([\s\S]*?)<\/title\s*>/i.exec(raw);
  const title = titleMatch ? normaliseLine(decodeEntities(titleMatch[1].replace(/<[^>]*>/g, ''))) : '';
  let body = raw.replace(/<!--[\s\S]*?-->/g, ' ');
  const bodyMatch = /<body\b[^>]*>([\s\S]*)<\/body\s*>/i.exec(body);
  if (bodyMatch) body = bodyMatch[1];
  body = unwrapPageForms(body);
  body = dropElements(body, DROP_WHOLE);
  body = dropFurniture(body);
  const region = mainRegion(body) ?? body;
  // Whitespace inside HTML is insignificant: a line wrap in the source is not
  // a new paragraph. Collapse it first; only block elements make line breaks.
  const text = decodeEntities(region.replace(/\s+/g, ' ').replace(BLOCK, '\n').replace(/<[^>]*>/g, ' '));
  const paragraphs: string[] = [];
  for (const line of text.split(/\n+/)) {
    const clean = normaliseLine(line);
    if (!clean || clean.length < 2 || CHROME_LINE.test(clean)) continue;
    if (paragraphs[paragraphs.length - 1] === clean) continue;
    paragraphs.push(clean);
  }
  return { title, paragraphs, text: paragraphs.join('\n') };
}
