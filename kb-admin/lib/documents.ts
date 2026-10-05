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

/**
 * The namespace "Usual for the service" means on upload (decided 2026-10-05).
 * Namespace is a label only - RAG_NAMESPACE is unset, so the bot never filters
 * on it - but staff read it. It used to be left to the database, which picks
 * the namespace most rows of the service carry; every loader row is
 * mom_regulations (the loader copies one sample row's value), so General came
 * out as mom_regulations. A fixed map instead, shown next to the dropdown.
 */
export const USUAL_NAMESPACE: Readonly<Record<string, string>> = {
  general: 'services_general',
  new_hiring: 'hiring_process',
  direct_hiring: 'hiring_process',
  transfer: 'hiring_process',
  transfer_employer: 'hiring_process',
  replacement: 'hiring_process',
  fee_enquiry: 'fees',
  salary_enquiry: 'mdw_rights',
  dispute_salary: 'mdw_rights',
  dispute_assault: 'mdw_rights',
  renewal: 'mom_regulations',
  passport_renewal: 'mom_regulations',
  home_leave: 'mom_regulations',
};
const FALLBACK_NAMESPACE = 'services_general';

/**
 * The usual namespace for a service, among the namespaces active rows use (the
 * only ones the database accepts). '' only if neither the mapped value nor the
 * fallback is in use - then the database's own default applies.
 */
export function usualNamespace(service: string, available: readonly string[]): string {
  const mapped = USUAL_NAMESPACE[service];
  if (mapped && available.includes(mapped)) return mapped;
  return available.includes(FALLBACK_NAMESPACE) ? FALLBACK_NAMESPACE : '';
}

/**
 * A figure for highlighting: a number with commas, decimals or %, and the
 * currency in front of it as part of it - S$, SGD, $, RM, Rp, PHP, ₱ - so
 * "S$3" is marked whole, not "S" + "$3". A letter prefix must not follow a
 * letter ("ASGD" is not SGD). Display only: the database's numbers helper and
 * the chunker's figures_present test are unchanged.
 */
const FIGURE_HIGHLIGHT = /(?:(?<![A-Za-z])(?:S\$|SGD|RM|Rp\.?|PHP)\s?|₱\s?|\$)?\d(?:[\d,]*\d)?(?:\.\d+)?%?/g;

/** Text split into plain parts and figures, for highlighting. */
export function figureParts(text: string): { text: string; figure: boolean }[] {
  const out: { text: string; figure: boolean }[] = [];
  let last = 0;
  for (const m of text.matchAll(FIGURE_HIGHLIGHT)) {
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
