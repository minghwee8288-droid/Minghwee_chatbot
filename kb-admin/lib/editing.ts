/**
 * The editor's rules that do not need the database. PURE: no imports, no
 * process.env, no network, so scripts/selfcheck.mjs can run every one.
 *
 * The database is the authority on all of these (scripts/sql, migration 004).
 * They are repeated here only so a page can warn before a click, never to
 * decide whether a write is allowed.
 */

/** The bot cuts retrieved text at 1200 characters (rag_max_chunk_chars). */
export const CHAR_LIMIT = 1200;
export const CHAR_WARN = 1150;

/** Characters as Postgres length() counts them: code points, not UTF-16 units. */
export function charCount(text: string): number {
  return [...text].length;
}

/** What the database stores and the bot embeds: question, newline, answer, each trimmed. */
export function entryText(question: string, answer: string): string {
  return `${question.trim()}\n${answer.trim()}`;
}

export type LengthState = { count: number; level: 'ok' | 'warn' | 'block' };

/**
 * Over the limit is refused only when the text also grows past the live
 * entry's, so an entry that is already too long can still be shortened
 * (the same rule as kb_admin_save_draft).
 */
export function lengthState(question: string, answer: string, liveLength: number): LengthState {
  const count = charCount(entryText(question, answer));
  if (count > CHAR_LIMIT && count > liveLength) return { count, level: 'block' };
  if (count >= CHAR_WARN) return { count, level: 'warn' };
  return { count, level: 'ok' };
}

export const AUDIENCES = ['employer', 'candidate', 'all'] as const;
export const NATIONALITIES = ['PH', 'ID', 'MM', 'all'] as const;

/** The same NRIC/FIN pattern the database refuses (and the bot redacts). */
export function hasNric(text: string): boolean {
  return /\b[STFGM][0-9]{7}[A-Z]\b/i.test(text);
}

/** Every number in a text, sorted, as the database finds them (Option C). */
export function numbersIn(text: string): string[] {
  return [...text.matchAll(/\$?\d[\d,]*(?:\.\d+)?%?/g)].map((m) => m[0].replace(/,/g, '')).sort();
}

export type Fields = {
  question: string | null;
  answer: string | null;
  section_heading: string | null;
  service: string;
  audience: string;
  nationality: string;
  active: boolean;
};

export type ApprovalReason = 'numbers' | 'service' | 'audience' | 'nationality' | 'on_off';

/** Why publishing this draft would need an approver (Option C). Empty = an editor may publish it. */
export function approvalReasons(live: Fields, draft: Fields): ApprovalReason[] {
  const nums = (f: Fields) => numbersIn([f.question, f.answer, f.section_heading].filter((x) => x !== null).join(' '));
  const why: ApprovalReason[] = [];
  if (nums(live).join('|') !== nums(draft).join('|')) why.push('numbers');
  if (live.service !== draft.service) why.push('service');
  if (live.audience !== draft.audience) why.push('audience');
  if (live.nationality !== draft.nationality) why.push('nationality');
  if (live.active !== draft.active) why.push('on_off');
  return why;
}

export const APPROVAL_WORDS: Record<ApprovalReason, string> = {
  numbers: 'a number changed',
  service: 'the service changed',
  audience: 'the audience changed',
  nationality: 'the nationality changed',
  on_off: 'the entry is switched on or off',
};

/**
 * The database's error codes (SQLSTATE KB001-KB006) in plain English. For
 * KB002 and KB003 the database's own sentence says which rule was hit, so it
 * is shown after the plain one; it never carries anyone's data but an email
 * of the colleague whose draft is open.
 */
export function errorMessage(code: string | undefined, dbMessage: string | undefined): string {
  const detail = (dbMessage ?? '').replace(/\s+/g, ' ').trim();
  switch (code) {
    case 'KB001':
      return 'Your account is not allowed to make this change. Ask for editor access, or sign in again.';
    case 'KB002':
      return `This change could not be saved: ${detail || 'something in the form is not valid'}.`;
    case 'KB003':
      return `Someone else changed this entry first (${detail || 'it is out of date'}). Open the entry again and start from the current version.`;
    case 'KB004':
      return 'This change needs an approver. It has been kept as a pending draft; an approver can open it and publish it.';
    case 'KB005':
      return 'The new text could not be given its search fingerprint, so nothing was published. Try again; if it keeps failing, tell the administrator.';
    case 'KB006':
      return 'The text contains what looks like an NRIC or FIN number. Remove it and try again: knowledge base text is shown to clients.';
    default:
      return 'Something went wrong and nothing was changed. Please try again.';
  }
}

// --- Embeddings -------------------------------------------------------------

/** OpenAI returns a vector as JSON numbers, or (as the bot's SDK asks for) base64 float32. */
export function decodeEmbedding(value: unknown): number[] | null {
  if (Array.isArray(value) && value.every((x) => typeof x === 'number')) return value as number[];
  if (typeof value !== 'string') return null;
  const bytes = Uint8Array.from(atob(value), (c) => c.charCodeAt(0));
  if (bytes.length % 4 !== 0) return null;
  return Array.from(new Float32Array(bytes.buffer, bytes.byteOffset, bytes.length / 4));
}

/** Postgres' text form of a pgvector: [0.1,0.2,...]. */
export function vectorLiteral(v: number[]): string {
  return `[${v.map((x) => (Number.isFinite(x) ? String(x) : 'NaN')).join(',')}]`;
}

/** pgvector text -> numbers. */
export function parseVector(text: string): number[] {
  return text.replace(/^\[|\]$/g, '').split(',').map(Number);
}

export function cosine(a: number[], b: number[]): number {
  if (a.length !== b.length || !a.length) return 0;
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < a.length; i += 1) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  return na && nb ? dot / Math.sqrt(na * nb) : 0;
}

/**
 * Does kb-admin's vector for the canary sentence match the one the BOT made?
 * 1536 dimensions, and cosine similarity of at least CANARY_MIN_COSINE.
 * The same model gives 0.9999+ (float rounding, and OpenAI's own small
 * run-to-run jitter); a different model or dimension setting gives far less.
 */
export const CANARY_MIN_COSINE = 0.999;
export function canaryMatches(ours: number[], bots: number[]): { ok: boolean; similarity: number } {
  if (ours.length !== 1536 || bots.length !== 1536) return { ok: false, similarity: 0 };
  const similarity = cosine(ours, bots);
  return { ok: similarity >= CANARY_MIN_COSINE, similarity };
}

// --- Word diff ----------------------------------------------------------------

export type DiffPart = { text: string; kind: 'same' | 'del' | 'ins' };

/** Word-level diff (longest common subsequence). Whitespace is kept with the word before it. */
export function wordDiff(before: string, after: string): { before: DiffPart[]; after: DiffPart[] } {
  const a = before.match(/\S+\s*|\s+/g) ?? [];
  const b = after.match(/\S+\s*|\s+/g) ?? [];
  const key = (t: string) => t.trim();
  const n = a.length;
  const m = b.length;
  const table: Uint16Array[] = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      table[i][j] = key(a[i]) === key(b[j]) ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const left: DiffPart[] = [];
  const right: DiffPart[] = [];
  const push = (list: DiffPart[], text: string, kind: DiffPart['kind']) => {
    const last = list[list.length - 1];
    if (last && last.kind === kind) last.text += text;
    else list.push({ text, kind });
  };
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (key(a[i]) === key(b[j])) {
      push(left, a[i], 'same');
      push(right, b[j], 'same');
      i += 1;
      j += 1;
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      push(left, a[i], 'del');
      i += 1;
    } else {
      push(right, b[j], 'ins');
      j += 1;
    }
  }
  for (; i < n; i += 1) push(left, a[i], 'del');
  for (; j < m; j += 1) push(right, b[j], 'ins');
  return { before: left, after: right };
}
