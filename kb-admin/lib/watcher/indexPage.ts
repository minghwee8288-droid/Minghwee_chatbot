import { isPointerSentence, actionableKinds } from './quality.ts';

/**
 * Is a page mainly an INDEX - a list of links to the pages that hold the
 * facts? PURE.
 *
 * MOM's ".../foreign-domestic-worker-levy" is one: a line of introduction,
 * then "Paying levy / How to pay levy, levy rates...", "Levy concession / Find
 * out if you qualify...", "Apply for levy waiver / When and how...". Drafting
 * from it produced four proposals that only pointed elsewhere. An index page
 * drafts nothing; its useful sub-pages (same site, below its own address,
 * named in its main text) are offered on the Watched pages row as "Suggested
 * pages to watch" instead - here, ".../foreign-domestic-worker-levy/paying-levy",
 * which holds the levy rates.
 */

export type SubPage = { url: string; label: string };

export const MAX_SUGGESTIONS = 20;
const INDEX_MAX_CHARS = 2500;
const NOT_A_PAGE = /\.(?:pdf|docx?|xlsx?|pptx?|zip|jpe?g|png|gif|svg|mp4|mp3)$/i;

const decode = (s: string) =>
  s.replace(/&nbsp;|&#160;/gi, ' ').replace(/&amp;/gi, '&').replace(/&#39;|&apos;|&rsquo;|&lsquo;/gi, "'")
    .replace(/&quot;|&ldquo;|&rdquo;/gi, '"').replace(/&ndash;|&mdash;/gi, '-').replace(/&lt;/gi, '<').replace(/&gt;/gi, '>');
const textOf = (html: string) => decode(html.replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ').trim();
const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();

/**
 * Links to pages BELOW this one on the same site whose link text is a line of
 * the page's main text (so menus and footers, which the extractor dropped, do
 * not count). https only, no query string, no files; at most 20.
 */
export function findSubPages(html: string, pageUrl: string, mainParagraphs: string[]): SubPage[] {
  let base: URL;
  try {
    base = new URL(pageUrl);
  } catch {
    return [];
  }
  const basePath = base.pathname.replace(/\/+$/, '');
  const lines = new Set(mainParagraphs.map(norm).filter(Boolean));
  const out: SubPage[] = [];
  const seen = new Set<string>();
  for (const m of html.matchAll(/<a\b[^>]*?\bhref\s*=\s*(["'])(.*?)\1[^>]*>([\s\S]*?)<\/a>/gi)) {
    let u: URL;
    try {
      u = new URL(decode(m[2]).trim(), base);
    } catch {
      continue;
    }
    if (u.protocol !== 'https:' || u.host !== base.host || u.search || NOT_A_PAGE.test(u.pathname)) continue;
    const path = u.pathname.replace(/\/+$/, '');
    if (!path.startsWith(basePath + '/') || path === basePath) continue;
    // A card link wraps a heading and a blurb (MOM's index): its label is the heading.
    const heading = /<h[1-6]\b[^>]*>([\s\S]*?)<\/h[1-6]>/i.exec(m[3]);
    const label = textOf(heading ? heading[1] : m[3]).slice(0, 200);
    if (!label || !lines.has(norm(label))) continue;
    const url = `https://${u.host}${path}`;
    if (seen.has(url)) continue;
    seen.add(url);
    out.push({ url, label });
    if (out.length >= MAX_SUGGESTIONS) break;
  }
  return out;
}

/** A line that only introduces a link ("How to pay levy, levy rates...", "Find out if you qualify..."). */
function isTeaser(line: string): boolean {
  return /^(?:how|when|what|where|which|who|find out|learn|read|see|view|check|about|information|details|everything|overview)\b/i.test(line)
    || isPointerSentence(line) || !actionableKinds(line).length;
}

const isHeading = (line: string) => line.split(/\s+/).length <= 6 && !/[.!?:]$/.test(line);

/**
 * From the text alone (a re-draft has no HTML): mostly short headings, each
 * followed by a one-line teaser, and short overall.
 */
export function looksLikeIndexText(paragraphs: string[]): boolean {
  const text = paragraphs.join('\n');
  if (text.length > INDEX_MAX_CHARS || paragraphs.length < 4) return false;
  let pairs = 0;
  for (let i = 0; i + 1 < paragraphs.length; i++) {
    if (isHeading(paragraphs[i]) && !isHeading(paragraphs[i + 1]) && isTeaser(paragraphs[i + 1])) {
      pairs += 1;
      i += 1;
    }
  }
  return pairs >= 3 && pairs * 2 >= paragraphs.length * 0.6;
}

/** The verdict for a fetched page: index or not, and why. */
export function indexVerdict(paragraphs: string[], subPages: SubPage[]): { index: boolean; reason: string } {
  const text = paragraphs.join('\n');
  if (subPages.length < 2 || text.length > INDEX_MAX_CHARS) return { index: false, reason: '' };
  const labels = new Set(subPages.map((s) => norm(s.label)));
  const linkLines = paragraphs.filter((p) => labels.has(norm(p))).length;
  if (linkLines * 2 >= paragraphs.length * 0.5 || looksLikeIndexText(paragraphs)) {
    return { index: true, reason: `it is mostly links to ${subPages.length} sub-pages` };
  }
  return { index: false, reason: '' };
}
