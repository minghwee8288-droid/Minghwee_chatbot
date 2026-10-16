/**
 * Finding a quoted passage in a text, spacing aside. PURE.
 *
 * Used for the source passage on the fetched page (lib/watcher/drafting.ts)
 * and for the sentences of a live entry a change replaces or follows
 * (lib/watcher/additive.ts).
 */

// Whitespace of every kind: spaces, tabs, line breaks, non-breaking and
// zero-width spaces.
const SPACE = /[\s\u00a0\u1680\u2000-\u200d\u2028\u2029\u202f\u205f\u3000\ufeff]/u;
const LETTER = /\p{L}/u;
const DIGIT = /\p{N}/u;

/**
 * The text with its spacing normalised, and for every kept character its
 * index in the original. A run of whitespace becomes nothing, or one space
 * where it separates two words or two numbers ("per month", "1 2"); anywhere
 * else - between a number and a letter, or beside punctuation - spacing
 * carries no word or figure, so it is dropped. The MOM paying-levy page writes
 * "Normal (1 st helper)", "the 17 th of" and "apply for GIRO ." (inline links),
 * and the model copies them as "1st", "17th", "GIRO." - the same words and
 * figures (dry runs 2026-10-16: 2 of 6 dropped a correct table passage).
 */
function spacingKey(text: string): { key: string; at: number[] } {
  let key = '';
  const at: number[] = [];
  let pendingSpace = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (SPACE.test(ch)) { pendingSpace = key.length > 0; continue; }
    if (pendingSpace) {
      const prev = key[key.length - 1];
      if ((LETTER.test(prev) && LETTER.test(ch)) || (DIGIT.test(prev) && DIGIT.test(ch))) { key += ' '; at.push(i - 1); }
      pendingSpace = false;
    }
    key += ch;
    at.push(i);
  }
  return { key, at };
}

// Curly quotes and apostrophes are the same character to a reader.
const sameQuotes = (s: string) => s.replace(/[\u2018\u2019\u201b\u2032]/g, "'").replace(/[\u201c\u201d\u201f\u2033]/g, '"');

/** Where `needle` is in `text`, spacing (and curly quotes) aside: [start, end) in `text`, or null. */
export function locateSpan(text: string, needle: string): { start: number; end: number } | null {
  const want = spacingKey(sameQuotes(needle)).key;
  if (!want) return null;
  const page = spacingKey(sameQuotes(text));
  const start = page.key.indexOf(want);
  if (start < 0) return null;
  return { start: page.at[start], end: page.at[start + want.length - 1] + 1 };
}

/**
 * Where the model's passage is on the page, spacing aside: the page's own
 * span (so the reviewer sees the page's exact words, and the database's check,
 * which only collapses whitespace, finds it verbatim), or null when the words
 * or figures differ.
 */
export function locatePassage(pageText: string, passage: string): string | null {
  const at = locateSpan(pageText, passage);
  return at ? pageText.slice(at.start, at.end) : null;
}
