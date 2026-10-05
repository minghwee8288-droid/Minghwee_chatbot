import mammoth from 'mammoth';

/**
 * Turn an uploaded document into the chunks kb-admin stages for review.
 *
 *   chunkDocument(fileBytes, 'docx' | 'md' | 'txt', sourceName) -> Chunk[]
 *
 * Pure: no database, no network, no environment. Staging (scripts/sql
 * 009-011) takes these chunks as they are; publish maps 'passage' to the live
 * table's 'document_chunk' (the live CHECK has no 'passage').
 *
 * What it fixes against the original bulk import:
 *   * a passage is never over 1,200 characters and a table never over 3,000 -
 *     the bot's own rag_max_chunk_chars / rag_max_table_chars, which the import
 *     ignored (it stored passages of up to 2,076 and tables of up to 5,512);
 *   * section_heading names where the text sits: its parent heading and the
 *     nearest heading, "27.3 Your Passport — What is OK" (two levels only; the
 *     nearest alone when there is no parent; never the document's title);
 *   * consecutive small sections under the same parent are packed into one
 *     chunk (each keeps its own heading as its first line), headed by the
 *     parent - so a page of short sub-sections is not cut into dozens of
 *     fragments;
 *   * [cite:N] markers left over from the research tool are removed.
 *
 * What it keeps from the import, so unchanged text stays comparable:
 *   * passages are paragraphs joined by a blank line;
 *   * a table is its heading line, then one line per row written
 *     "column: value | column: value" (the import's format), with
 *     metadata.table_column listing the columns.
 *
 * Headings in .docx: Word heading styles (mammoth's h1-h6). Four of the seven
 * source documents use no heading style at all - their headings are a short
 * paragraph in bold, or a one-cell table holding one - so both of those count
 * as headings too. Without that, those documents would have no section
 * headings whatsoever. Their levels: a heading in a one-cell box is a top
 * level; a bold heading numbered "5." is level 1 and "5.1" level 2; any other
 * bold heading sits under the latest of those (level 99, a sub-heading).
 *
 * PDF is deliberately not accepted.
 */

export type FileType = 'docx' | 'md' | 'txt';
export type ChunkType = 'passage' | 'table_unit';

export type ChunkMetadata = {
  figures_present: boolean;
  managed_by: 'ui';
  /** An array, as on every live row and as the staging functions default it. */
  keywords: string[];
  priority: number;
  table_column: string | null;
  date_valid_from: null;
};

export type Chunk = {
  ordinal: number;
  chunk_type: ChunkType;
  section_heading: string | null;
  content: string;
  metadata: ChunkMetadata;
  /** 0.42 when the text carries a figure, else null - what publish sets the row's floor to. */
  suggested_rag_score_floor: number | null;
};

export const MAX_PASSAGE_CHARS = 1200;
export const MAX_TABLE_CHARS = 3000;
export const FILE_TYPES: readonly FileType[] = ['docx', 'md', 'txt'];

/** A short, all-bold paragraph reads as a heading; longer than this it is emphasised text. */
const BOLD_HEADING_MAX = 150;

/** Same pattern as the database's numbers helper (004): a figure, with optional $, commas, decimals, %. */
const FIGURE = /\$?\d[\d,]*(?:\.\d+)?%?/;
const CITE = /\[cite:\d+\]/g;

/** The separator between the parent and the nearest heading in section_heading. */
export const HEADING_JOIN = ' — ';
const SUBHEADING = 99;

type Block =
  | { kind: 'heading'; text: string; level: number }
  | { kind: 'para'; text: string }
  | { kind: 'table'; columns: string[]; rows: string[][] };

export class ChunkerError extends Error {}

export async function chunkDocument(fileBytes: Buffer | Uint8Array, fileType: FileType, sourceName: string): Promise<Chunk[]> {
  if (!FILE_TYPES.includes(fileType)) throw new ChunkerError(`unsupported file type: ${String(fileType)}`);
  const bytes = Buffer.from(fileBytes);
  let blocks: Block[];
  if (fileType === 'docx') {
    const { value } = await mammoth.convertToHtml({ buffer: bytes });
    blocks = htmlBlocks(value);
  } else {
    let text: string;
    try {
      text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    } catch {
      throw new ChunkerError(`${sourceName} is not UTF-8 text`);
    }
    blocks = fileType === 'md' ? markdownBlocks(text) : textBlocks(text);
  }
  return buildChunks(blocks, fileType === 'txt');
}

/* ------------------------------------------------------------------ chunks */

/** section_heading for text under `nearest` whose parent is `parent`. */
export function combineHeading(parent: string | null, nearest: string | null): string | null {
  if (!nearest) return parent;
  return parent && parent !== nearest ? `${parent}${HEADING_JOIN}${nearest}` : nearest;
}

type Section = { kind: 'text'; nearest: string | null; parent: string | null; paras: string[] };
type Unit = Section | { kind: 'table'; heading: string | null; columns: string[]; rows: string[][] };

/**
 * The document's title: the first heading, when no later heading is at its
 * level or above. It is never used as a parent - it would sit on every chunk.
 */
function titleIndex(blocks: Block[]): number {
  const first = blocks.findIndex((b) => b.kind === 'heading');
  if (first < 0) return -1;
  const level = (blocks[first] as { level: number }).level;
  return blocks.slice(first + 1).some((b) => b.kind === 'heading' && b.level <= level) ? -1 : first;
}

/** Blocks -> sections (text between headings) and tables, each knowing its nearest heading and its parent. */
function sectionsOf(blocks: Block[], noHeadings: boolean): Unit[] {
  const units: Unit[] = [];
  const stack: { text: string; level: number; title: boolean }[] = [];
  const title = titleIndex(blocks);
  const here = () => {
    const top = stack[stack.length - 1];
    const under = stack.slice(0, -1).filter((h) => !h.title);
    return { nearest: top?.text ?? null, parent: under.length ? under[under.length - 1].text : null };
  };
  let cur: Section | null = null;
  blocks.forEach((b, i) => {
    if (b.kind === 'heading') {
      if (noHeadings) return;
      while (stack.length && stack[stack.length - 1].level >= b.level) stack.pop();
      stack.push({ text: cap(b.text, MAX_PASSAGE_CHARS), level: b.level, title: i === title });
      cur = null;
    } else if (b.kind === 'para') {
      if (!cur) {
        cur = { kind: 'text', ...here(), paras: [] };
        units.push(cur);
      }
      cur.paras.push(b.text);
    } else {
      const { nearest, parent } = here();
      units.push({ kind: 'table', heading: combineHeading(parent, nearest), columns: b.columns, rows: b.rows });
      cur = null;
    }
  });
  return units;
}

/** A section's text inside a packed chunk: its own heading first, unless it is the parent's own intro text. */
function part(s: Section, key: string): string {
  const body = s.paras.join('\n\n');
  return s.nearest && s.nearest !== key ? `${s.nearest}\n${body}` : body;
}

function buildChunks(blocks: Block[], noHeadings: boolean): Chunk[] {
  const out: Omit<Chunk, 'ordinal'>[] = [];
  let group: { key: string; members: Section[] } | null = null;

  const emitAlone = (s: Section) => {
    const heading = combineHeading(s.parent, s.nearest);
    for (const content of packParagraphs(s.paras)) out.push(passage(heading, content));
  };
  const flush = () => {
    if (!group) return;
    if (group.members.length === 1) emitAlone(group.members[0]);
    else out.push(passage(group.key, group.members.map((m) => part(m, group!.key)).join('\n\n')));
    group = null;
  };
  const fits = (members: Section[], key: string) => members.map((m) => part(m, key)).join('\n\n').length <= MAX_PASSAGE_CHARS;

  for (const u of sectionsOf(blocks, noHeadings)) {
    if (u.kind === 'table') {
      flush();
      for (const t of tableChunks(u.heading, u.columns, u.rows)) out.push(t);
      continue;
    }
    const alone = u.paras.join('\n\n');
    // Only small sections pack, and only under a shared parent heading.
    const small = (u.nearest ? u.nearest.length + 1 : 0) + alone.length <= MAX_PASSAGE_CHARS;
    if (!small) {
      flush();
      emitAlone(u);
      continue;
    }
    const g = group as { key: string; members: Section[] } | null;
    if (g && u.parent === g.key && fits([...g.members, u], g.key)) {
      g.members.push(u);
    } else if (g && g.members.length === 1 && u.parent && u.parent === g.members[0].nearest && fits([...g.members, u], u.parent)) {
      // The parent's own intro text, then its first sub-section.
      g.key = u.parent;
      g.members.push(u);
    } else {
      flush();
      group = { key: u.parent ?? u.nearest ?? '', members: [u] };
    }
  }
  flush();
  return out.map((c, i) => ({ ordinal: i + 1, ...c }));
}

function passage(heading: string | null, content: string): Omit<Chunk, 'ordinal'> {
  return { chunk_type: 'passage', section_heading: heading, content, ...figures(heading, content, null) };
}

function figures(heading: string | null, content: string, tableColumn: string | null) {
  // Heading and content together, exactly as publish computes the floor.
  const has = FIGURE.test(`${heading ?? ''} ${content}`);
  return {
    metadata: {
      figures_present: has,
      managed_by: 'ui' as const,
      keywords: [],
      priority: 3,
      table_column: tableColumn,
      date_valid_from: null,
    },
    suggested_rag_score_floor: has ? 0.42 : null,
  };
}

/** Pack paragraphs (joined by a blank line) up to the passage limit; split any paragraph over it. */
export function packParagraphs(paragraphs: string[]): string[] {
  const pieces = glueQuestions(paragraphs).flatMap((p) => splitLong(p, MAX_PASSAGE_CHARS));
  const out: string[] = [];
  let cur = '';
  for (const p of pieces) {
    if (!cur) cur = p;
    else if (cur.length + 2 + p.length <= MAX_PASSAGE_CHARS) cur += `\n\n${p}`;
    else {
      out.push(cur);
      cur = p;
    }
  }
  if (cur) out.push(cur);
  return out;
}

/**
 * A short question paragraph ("**Q:** ...", or a line ending in "?") stays with
 * the paragraph after it, so a question and its answer never land in two
 * different chunks.
 */
const QUESTION_PARA = /^(?:\*\*)?Q:|\?\s*(?:\*\*)?$/;
function glueQuestions(paragraphs: string[]): string[] {
  const out: string[] = [];
  for (let i = 0; i < paragraphs.length; i += 1) {
    const p = paragraphs[i];
    if (i + 1 < paragraphs.length && p.length <= 300 && QUESTION_PARA.test(p.trim())) {
      out.push(`${p}

${paragraphs[i + 1]}`);
      i += 1;
    } else out.push(p);
  }
  return out;
}

/** Split at the last sentence end (". " or "." at the end) that keeps a piece within the limit; else at the limit. */
export function splitLong(text: string, limit: number): string[] {
  const out: string[] = [];
  let rest = text;
  while (rest.length > limit) {
    const window = rest.slice(0, limit + 1);
    let cut = -1;
    for (let i = Math.min(limit, window.length) - 1; i > 0; i -= 1) {
      if (window[i] === '.' && (i + 1 >= window.length || /\s/.test(window[i + 1]))) {
        cut = i + 1;
        break;
      }
    }
    if (cut <= 0) cut = limit;
    out.push(rest.slice(0, cut).trimEnd());
    rest = rest.slice(cut).trimStart();
  }
  if (rest) out.push(rest);
  return out;
}

function tableChunks(heading: string | null, columns: string[], rows: string[][]): Omit<Chunk, 'ordinal'>[] {
  const tableColumn = columns.join(', ');
  const lines = rows
    .map((r) => columns.map((c, i) => `${c}: ${r[i] ?? ''}`.trim()).filter((s) => !/:$/.test(s)).join(' | '))
    .filter(Boolean);
  if (!lines.length) return [];
  const head = heading ?? '';
  const room = MAX_TABLE_CHARS - (head ? head.length + 1 : 0);
  // A row longer than a whole chunk is split at the limit; it is never dropped.
  const rowPieces = lines.flatMap((l) => (l.length > room ? splitLong(l, room) : [l]));
  const groups: string[][] = [];
  let cur: string[] = [];
  let len = 0;
  for (const l of rowPieces) {
    const add = (cur.length ? 1 : 0) + l.length;
    if (cur.length && len + add > room) {
      groups.push(cur);
      cur = [];
      len = 0;
    }
    cur.push(l);
    len += cur.length > 1 ? l.length + 1 : l.length;
  }
  if (cur.length) groups.push(cur);
  return groups.map((g) => {
    const content = (head ? [head, ...g] : g).join('\n');
    return { chunk_type: 'table_unit' as const, section_heading: heading, content, ...figures(heading, content, tableColumn) };
  });
}

/* ------------------------------------------------------------------ text cleanup */

function clean(text: string): string {
  return text
    .replace(CITE, '')
    .replace(/ /g, ' ')
    .split('\n')
    .map((l) => l.replace(/[ \t]+$/g, ''))
    .join('\n')
    .trim();
}

function cap(text: string, limit: number): string {
  return text.length <= limit ? text : text.slice(0, limit).trimEnd();
}

/* ------------------------------------------------------------------ .txt */

function textBlocks(text: string): Block[] {
  return text
    .replace(/\r\n?/g, '\n')
    .split(/\n\s*\n/)
    .map(clean)
    .filter(Boolean)
    .map((t) => ({ kind: 'para' as const, text: t }));
}

/* ------------------------------------------------------------------ .md */

const MD_HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
/** A thematic break (---, ***, ___): a separator, not text. */
const MD_RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
const MD_TABLE_ROW = /^\s*\|.*\|\s*$/;
const MD_TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function markdownBlocks(text: string): Block[] {
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  const blocks: Block[] = [];
  let para: string[] = [];
  const flushPara = () => {
    const t = clean(para.join('\n'));
    if (t) blocks.push({ kind: 'para', text: t });
    para = [];
  };
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const h = line.match(MD_HEADING);
    if (h) {
      flushPara();
      const t = clean(h[2]);
      if (t) blocks.push({ kind: 'heading', text: t, level: h[1].length });
      continue;
    }
    if (MD_TABLE_ROW.test(line) && i + 1 < lines.length && MD_TABLE_RULE.test(lines[i + 1])) {
      flushPara();
      const columns = mdCells(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && MD_TABLE_ROW.test(lines[i])) rows.push(mdCells(lines[i++]));
      i -= 1;
      blocks.push({ kind: 'table', columns, rows });
      continue;
    }
    if (!line.trim() || MD_RULE.test(line)) flushPara();
    else para.push(line);
  }
  flushPara();
  return blocks;
}

function mdCells(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split(/(?<!\\)\|/)
    .map((c) => clean(c.replace(/\\\|/g, '|')));
}

/* ------------------------------------------------------------------ .docx (mammoth HTML) */

type El = { tag: string; children: Node[] };
type Node = El | string;

const VOID = new Set(['br', 'img', 'hr']);

/** A small parser for mammoth's output, which is well-formed and uses a handful of tags. */
function parseHtml(html: string): El {
  const root: El = { tag: 'root', children: [] };
  const stack: El[] = [root];
  const re = /<(\/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*?)(\/?)>|([^<]+)/g;
  for (const m of html.matchAll(re)) {
    const top = stack[stack.length - 1];
    if (m[5] !== undefined) {
      top.children.push(decode(m[5]));
      continue;
    }
    const tag = m[2].toLowerCase();
    if (m[1]) {
      for (let i = stack.length - 1; i > 0; i -= 1) {
        if (stack[i].tag === tag) {
          stack.length = i;
          break;
        }
      }
    } else if (VOID.has(tag) || m[4]) {
      if (tag === 'br') top.children.push({ tag: 'br', children: [] });
    } else {
      const el: El = { tag, children: [] };
      top.children.push(el);
      stack.push(el);
    }
  }
  return root;
}

function decode(s: string): string {
  return s.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (_, e: string) => {
    const k = e.toLowerCase();
    if (k === 'amp') return '&';
    if (k === 'lt') return '<';
    if (k === 'gt') return '>';
    if (k === 'quot') return '"';
    if (k === 'apos') return "'";
    if (k === 'nbsp') return ' ';
    return String.fromCodePoint(k.startsWith('#x') ? parseInt(k.slice(2), 16) : parseInt(k.slice(1), 10));
  });
}

function textOf(n: Node): string {
  if (typeof n === 'string') return n;
  if (n.tag === 'br') return '\n';
  return n.children.map(textOf).join('');
}

function strongTextOf(n: Node): string {
  if (typeof n === 'string') return '';
  if (n.tag === 'strong' || n.tag === 'b') return textOf(n);
  return n.children.map(strongTextOf).join('');
}

const squash = (s: string) => s.replace(/\s+/g, '');

/**
 * A paragraph whose whole text is bold, short, and on one line. A bold block
 * with a line break in it is a title block ("Prepared for ... / Source ...
 * Updated May 2026"), not a heading: read as one, it would be replaced by the
 * next heading and its text lost.
 */
function isBoldHeading(p: El): boolean {
  const all = clean(textOf(p));
  if (!all || all.length > BOLD_HEADING_MAX || all.includes('\n')) return false;
  return squash(strongTextOf(p)) === squash(all);
}

const elements = (n: El) => n.children.filter((c): c is El => typeof c !== 'string');

/** Level of a bold heading: a box title is top level, "5." is 1, "5.1" is 2, anything else a sub-heading. */
function boldLevel(text: string, inBox: boolean): number {
  if (inBox) return 1;
  const m = text.match(/^(\d+(?:\.\d+)*)\.?\s/);
  return m ? m[1].split('.').length : SUBHEADING;
}

function htmlBlocks(html: string): Block[] {
  const blocks: Block[] = [];
  walkBlocks(parseHtml(html.replace(/<img[^>]*>/gi, '')), blocks);
  return blocks;
}

function walkBlocks(node: El, out: Block[], inBox = false): void {
  for (const child of node.children) {
    if (typeof child === 'string') {
      const t = clean(child);
      if (t) out.push({ kind: 'para', text: t });
      continue;
    }
    const tag = child.tag;
    if (/^h[1-6]$/.test(tag)) {
      const t = clean(textOf(child));
      if (t) out.push({ kind: 'heading', text: t, level: Number(tag[1]) });
    } else if (tag === 'p') {
      const t = clean(textOf(child));
      if (!t) continue;
      out.push(isBoldHeading(child) ? { kind: 'heading', text: t, level: boldLevel(t, inBox) } : { kind: 'para', text: t });
    } else if (tag === 'ul' || tag === 'ol') {
      for (const li of elements(child)) {
        // A list item's own text, then any list nested inside it.
        const own: Node[] = li.children.filter((c) => typeof c === 'string' || (c.tag !== 'ul' && c.tag !== 'ol'));
        const t = clean(own.map(textOf).join(''));
        if (t) out.push({ kind: 'para', text: t });
        for (const sub of elements(li).filter((c) => c.tag === 'ul' || c.tag === 'ol')) walkBlocks({ tag: 'root', children: [sub] }, out, inBox);
      }
    } else if (tag === 'table') {
      tableBlocks(child, out);
    } else {
      walkBlocks(child, out, inBox);
    }
  }
}

function rowsOf(table: El): { cells: El[]; head: boolean }[] {
  const rows: { cells: El[]; head: boolean }[] = [];
  const visit = (n: El, inHead: boolean) => {
    for (const c of elements(n)) {
      if (c.tag === 'tr') rows.push({ cells: elements(c).filter((x) => x.tag === 'td' || x.tag === 'th'), head: inHead || elements(c).every((x) => x.tag === 'th') });
      else if (c.tag !== 'table') visit(c, inHead || c.tag === 'thead');
    }
  };
  visit(table, false);
  return rows.filter((r) => r.cells.length);
}

/** Text of one table cell on one line: its paragraphs joined by a space. */
function cellText(cell: El): string {
  const parts: string[] = [];
  const collect = (n: El) => {
    for (const c of n.children) {
      if (typeof c === 'string') parts.push(c);
      else if (c.tag === 'p' || c.tag === 'li' || /^h[1-6]$/.test(c.tag)) parts.push(` ${textOf(c)} `);
      else if (c.tag === 'br') parts.push(' ');
      else collect(c);
    }
  };
  collect(cell);
  return clean(parts.join('')).replace(/\s+/g, ' ');
}

function tableBlocks(table: El, out: Block[]): void {
  const rows = rowsOf(table);
  if (!rows.length) return;
  const width = Math.max(...rows.map((r) => r.cells.length));
  if (width === 1) {
    // A one-column table is a box around ordinary text (a title, a callout):
    // read its contents as paragraphs, so a bold title becomes a heading.
    for (const r of rows) walkBlocks({ tag: 'root', children: r.cells.flatMap((c) => c.children) }, out, true);
    return;
  }
  // Header: the thead / all-th row, else the first row (Word tables rarely mark one).
  const headerIndex = rows.findIndex((r) => r.head);
  const hi = headerIndex >= 0 ? headerIndex : 0;
  const columns = rows[hi].cells.map((c, i) => cellText(c) || `Column ${i + 1}`);
  while (columns.length < width) columns.push(`Column ${columns.length + 1}`);
  const body = rows.filter((_, i) => i !== hi).map((r) => r.cells.map(cellText));
  if (!body.length) {
    out.push({ kind: 'para', text: columns.join(' | ') });
    return;
  }
  out.push({ kind: 'table', columns, rows: body });
}
