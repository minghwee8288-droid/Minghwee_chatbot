/**
 * Has a page REALLY changed? PURE (Web Crypto for the hash), so the self-check runs it.
 *
 * Pages change their bytes all the time without changing what they say: a
 * re-flowed paragraph, curly quotes, a "Last updated 3 March 2026" stamp. None
 * of that may produce a draft. So the comparison is on NORMALISED paragraphs:
 *   - whitespace collapsed, case folded, typographic quotes and dashes made plain;
 *   - a paragraph that is ONLY a date, or a date stamp ("Last updated 3 March
 *     2026", "Reviewed on 2026-04-09", "As at Mar 2026"), is left out entirely.
 * The snapshot's hash is the SHA-256 of those paragraphs. Equal hash = no
 * change, no draft. Only then is a paragraph diff taken, to find WHICH passages
 * changed - and those are what a draft is made from.
 *
 * A date INSIDE a sentence is kept on purpose: "From 1 January 2027, ..."
 * becoming "From 1 July 2027, ..." is a change to the rule, and must be drafted.
 */

const MONTHS = '(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)';
const DATE_PATTERNS: RegExp[] = [
  new RegExp(String.raw`\b\d{1,2}(?:st|nd|rd|th)?\s+${MONTHS}\.?,?\s+\d{4}\b`, 'gi'), // 12 March 2026
  new RegExp(String.raw`\b${MONTHS}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b`, 'gi'), // March 12, 2026
  new RegExp(String.raw`\b${MONTHS}\.?\s+\d{4}\b`, 'gi'), // March 2026
  /\b\d{4}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?)?\b/gi, // 2026-03-12, ISO
  /\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b/g, // 12/03/2026, 12.03.26
  /\b(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?\b,?/gi, // weekdays
];

export function stripDates(s: string): string {
  let out = s;
  for (const re of DATE_PATTERNS) out = out.replace(re, ' ');
  return out;
}

/** One paragraph, normalised for comparison (never shown to anyone). */
export function normaliseForCompare(s: string): string {
  return String(s ?? '')
    .toLowerCase()
    .replace(/[‘’`´]/g, "'")
    .replace(/[“”]/g, '"')
    .replace(/[‐-―−]/g, '-')
    .replace(/\s+/g, ' ')
    .replace(/\s+([.,;:!?)])/g, '$1')
    .replace(/\(\s+/g, '(')
    .trim();
}

// What is left of a date stamp once its date is removed ("reviewed on .", "last updated:").
const STAMP_ONLY = /^(?:page\s+)?(?:last\s+)?(?:updated|reviewed|modified|published|revised|posted|effective|dated?)(?:\s+(?:on|at))?[\s:.,-]*$|^as\s+(?:at|of)[\s:.,-]*$/;

/** Does this normalised paragraph say anything (not empty, not only a date or a date stamp)? */
function meaningful(n: string): boolean {
  const bare = stripDates(n).replace(/\s+/g, ' ').trim();
  return /[a-z0-9]/.test(bare) && !STAMP_ONLY.test(bare);
}

export function normalisedParagraphs(text: string): string[] {
  return String(text ?? '')
    .split(/\n+/)
    .map(normaliseForCompare)
    .filter(meaningful);
}

/** The snapshot hash: SHA-256 hex of the normalised paragraphs. */
export async function contentHash(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(normalisedForm(text));
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/** The exact text the hash is taken of: equal forms = the same page, whatever its bytes did. */
export function normalisedForm(text: string): string {
  return normalisedParagraphs(text).join('\n');
}

export type PageChange = {
  changed: boolean;
  /** Paragraphs (original wording, from the NEW page) that are new or reworded. */
  added: string[];
  /** Paragraphs (original wording, from the OLD page) that are gone or reworded. */
  removed: string[];
};

/**
 * What changed between two extracted texts. Equal normalised text = no
 * change at all, whatever the bytes did. Otherwise the paragraphs whose
 * normalised form appears on one side only.
 */
export function pageChange(oldText: string | null, newText: string): PageChange {
  const newLines = String(newText ?? '').split(/\n+/).filter((l) => l.trim());
  if (oldText == null) return { changed: true, added: newLines, removed: [] };
  if (normalisedForm(oldText) === normalisedForm(newText)) return { changed: false, added: [], removed: [] };
  const oldLines = String(oldText).split(/\n+/).filter((l) => l.trim());
  const oldSet = new Set(oldLines.map(normaliseForCompare));
  const newSet = new Set(newLines.map(normaliseForCompare));
  const added = newLines.filter((l) => { const n = normaliseForCompare(l); return meaningful(n) && !oldSet.has(n); });
  const removed = oldLines.filter((l) => { const n = normaliseForCompare(l); return meaningful(n) && !newSet.has(n); });
  return { changed: added.length > 0 || removed.length > 0, added, removed };
}

/** The passages a draft is made from, capped so one prompt stays small. */
export function changedPassages(change: PageChange, maxChars = 6000): string[] {
  const out: string[] = [];
  let used = 0;
  for (const p of change.added) {
    if (used + p.length > maxChars) break;
    out.push(p);
    used += p.length;
  }
  return out;
}
