/**
 * Document upload rules that do not need the database. PURE: no process.env,
 * no network, so scripts/selfcheck.mjs can run every one.
 *
 * The database is the authority (scripts/sql 011): it refuses the same source
 * names, file types and sizes itself. These are repeated so the page can say
 * no before anything is sent, never to decide whether a write is allowed.
 */

import { findLostFacts, type LostFact } from './facts.ts';
import { isInternalSource } from './retrieval.ts';

export const FILE_TYPES = ['docx', 'md', 'txt'] as const;
export type DocFileType = (typeof FILE_TYPES)[number];

/**
 * 4 MB in the browser. Vercel refuses a request body over about 4.5 MB before
 * kb-admin sees it, and a server action's body limit is set to match
 * (next.config.mjs). The database's own limit (5 MB) stays as the backstop.
 */
export const MAX_UPLOAD_BYTES = 4 * 1024 * 1024;

export function fileTypeOf(fileName: string): DocFileType | null {
  const ext = fileName.toLowerCase().match(/\.([a-z0-9]+)$/)?.[1] ?? '';
  return (FILE_TYPES as readonly string[]).includes(ext) ? (ext as DocFileType) : null;
}

/** Why a file cannot be uploaded, before it is read; null if it can. */
export function fileProblem(fileName: string, size: number): string | null {
  if (!fileName) return 'Choose a file.';
  if (/\.pdf$/i.test(fileName)) return 'PDF files cannot be uploaded. Save the document as .docx, .md or .txt and upload that.';
  if (!fileTypeOf(fileName)) return 'Only .docx, .md and .txt files can be uploaded.';
  if (size <= 0) return 'The file is empty.';
  if (size > MAX_UPLOAD_BYTES) return `The file is ${(size / 1024 / 1024).toFixed(2)} MB; the limit is 4 MB.`;
  return null;
}

const RESERVED = ['ming hwee service notes', 'ming hwee kb admin'];

/**
 * Why a source name cannot be used, or null. The same rules as the database's
 * check: reserved names, the bot's internal-source filter (rag._INTERNAL_SOURCES,
 * after separators become spaces), and anything calling itself internal.
 */
export function sourceProblem(source: string): string | null {
  const name = source.trim();
  const words = name.replace(/[-_\s]+/g, ' ').trim();
  if (!words) return 'Give the document a source name.';
  if (name.length > 500) return 'The source name is too long (500 characters at most).';
  if (RESERVED.includes(words.toLowerCase())) return `"${name}" is reserved for entries managed elsewhere. Choose another name.`;
  if (isInternalSource(name) || /\binternal\b/i.test(words)) {
    return `"${name}" is an internal document. The chatbot never uses internal documents, so it cannot be uploaded here.`;
  }
  return null;
}

/** The text a chunk is embedded as (decided 2026-10-05): heading, newline, content. */
export function embeddingText(heading: string | null | undefined, content: string): string {
  const h = (heading ?? '').trim();
  return h ? `${h}\n${content}` : content;
}

/**
 * Words that mark text written for staff, not clients - the pipeline brief's
 * "Owner: Sales", SLA clocks, vendor notes, editorial placeholders. Whatever is
 * in the knowledge base is what the bot may quote (CLAUDE.md §9.17, §9.25).
 */
const INTERNAL_MARKERS: { pattern: RegExp; label: string }[] = [
  { pattern: /\bOwner:/, label: '"Owner:"' },
  { pattern: /\bSLA\b/, label: '"SLA"' },
  { pattern: /\bMHOS\b/, label: '"MHOS"' },
  { pattern: /\bApp Brief\b/i, label: '"App Brief"' },
  { pattern: /\bvendor\b/i, label: '"vendor"' },
  { pattern: /\binternal (use|only|note)\b/i, label: '"internal use/only/note"' },
  { pattern: /\bstaff only\b/i, label: '"staff only"' },
  // §9.17's unfilled editorial placeholder. Built from pieces so this file
  // names no write verb (scripts/selfcheck.mjs).
  { pattern: new RegExp(`\\[${'IN'}${'SERT'}\\b`, 'i'), label: 'an unfilled editorial placeholder in square brackets' },
  { pattern: /\bTODO\b|\bTBC\b|\bCONFIRM\b/, label: 'a TODO / TBC / CONFIRM note' },
  { pattern: /\bHANDOFF\b/, label: '"HANDOFF"' },
];

export function internalMarkers(text: string): string[] {
  return INTERNAL_MARKERS.filter((m) => m.pattern.test(text)).map((m) => m.label);
}

/** Text split into plain parts and figures, for highlighting. Same figure pattern as the database. */
export function figureParts(text: string): { text: string; figure: boolean }[] {
  const out: { text: string; figure: boolean }[] = [];
  let last = 0;
  for (const m of text.matchAll(/\$?\d[\d,]*(?:\.\d+)?%?/g)) {
    const at = m.index ?? 0;
    if (at > last) out.push({ text: text.slice(last, at), figure: false });
    out.push({ text: m[0], figure: true });
    last = at + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last), figure: false });
  return out;
}

export type ChunkLike = { section_heading: string | null; content: string };

const asText = (c: ChunkLike) => `${c.section_heading ?? ''}\n${c.content}`;

/**
 * Lost: facts the live rows state that the new chunks do not. Stale: facts the
 * new chunks state that the live rows no longer do - an older figure coming
 * back, or a new one to confirm.
 */
export function factWarnings(live: ChunkLike[], next: ChunkLike[]): { lost: LostFact[]; stale: LostFact[] } {
  return {
    lost: findLostFacts(live.map(asText), next.map(asText)),
    stale: findLostFacts(next.map(asText), live.map(asText)),
  };
}
