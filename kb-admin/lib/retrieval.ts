/**
 * What the bot does to its search results before it uses them, ported from
 * app/services/rag.py search() so the impact check ranks exactly as the bot
 * does. PURE: no imports, no database, no network.
 *
 * The bot asks cb_match_knowledge_base_updated for twice the rows it wants
 * (5 -> 10), then, in this order:
 *   _drop_non_evidence  removes style_example rows;
 *   _drop_internal      removes rows from an internal source document;
 *   _enforce_row_floor  removes a row below its own rag_score_floor;
 *   _rerank             sorts by similarity + a priority boost (stable);
 * and keeps the first five. scripts/check_rerank_parity.py runs the Python
 * and this file on the same inputs and requires identical output.
 *
 * Python semantics kept on purpose (they decide edge cases): a missing or
 * unreadable similarity is 0.0; a floor that will not parse keeps the row;
 * a priority is int() of the value - a whole number or a whole-number string,
 * a float truncated, True as 1 - and only 1, 2 or 3 count.
 */

export const MATCH_COUNT = 5;
export const FETCH_COUNT = MATCH_COUNT * 2;
export const MATCH_THRESHOLD = 0.35;
/** rag_soft_floor: under this best score the bot treats retrieval as weak. */
export const SOFT_FLOOR = 0.4;

const EXCLUDED_CHUNK_TYPES = new Set(['style_example']);
const INTERNAL_SOURCES = /(MHOS\b|for Vendor|Blueprint|Hiring Pipelines Brief|version control)/i;
const PRIORITY_BOOST: Record<number, number> = { 1: 0.06, 2: 0.03, 3: 0.0 };

export type MatchRow = {
  id: string;
  chunk_type?: string | null;
  source_document?: string | null;
  similarity?: unknown;
  rag_score_floor?: unknown;
  metadata?: unknown;
  [key: string]: unknown;
};

/** rag._normalize_source: 'MHOS-100_Product_Blueprint' -> 'MHOS 100 Product Blueprint'. */
export function normalizeSource(name: unknown): string {
  return String(name || '').replace(/[-_\s]+/g, ' ').trim();
}

export function isInternalSource(name: unknown): boolean {
  return INTERNAL_SOURCES.test(normalizeSource(name));
}

/** Python float() of a value, or null where float() would raise. */
function pyFloat(value: unknown): number | null {
  if (typeof value === 'number') return value;
  if (typeof value === 'boolean') return value ? 1 : 0;
  if (typeof value === 'string') {
    const t = value.trim();
    if (!/^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/.test(t) && !/^[+-]?(inf|infinity|nan)$/i.test(t)) return null;
    return Number(t.replace(/^([+-]?)inf(inity)?$/i, '$1Infinity'));
  }
  return null;
}

/** Python int() of a value, or null where int() would raise. */
function pyInt(value: unknown): number | null {
  if (typeof value === 'boolean') return value ? 1 : 0;
  if (typeof value === 'number') return Number.isFinite(value) ? Math.trunc(value) : null;
  if (typeof value === 'string') {
    const t = value.trim().replace(/_/g, '');
    return /^[+-]?\d+$/.test(t) ? Number(t) : null;
  }
  return null;
}

/** rag._similarity: float(row.similarity or 0.0), 0.0 if unreadable. */
export function similarity(row: MatchRow): number {
  const v = row.similarity;
  if (v === null || v === undefined || v === 0 || v === '' || v === false) return 0;
  const f = pyFloat(v);
  return f === null ? 0 : f;
}

export function dropNonEvidence<T extends MatchRow>(rows: T[]): T[] {
  return rows.filter((r) => !EXCLUDED_CHUNK_TYPES.has(String(r.chunk_type || '').trim()));
}

export function dropInternal<T extends MatchRow>(rows: T[]): T[] {
  return rows.filter((r) => !isInternalSource(r.source_document));
}

export function enforceRowFloor<T extends MatchRow>(rows: T[]): T[] {
  return rows.filter((r) => {
    if (r.rag_score_floor === null || r.rag_score_floor === undefined) return true;
    const floor = pyFloat(r.rag_score_floor);
    if (floor === null) return true;
    return similarity(r) >= floor;
  });
}

/** rag._priority: metadata.priority as 1, 2 or 3, else null. */
export function priority(row: MatchRow): number | null {
  const m = row.metadata;
  if (!m || typeof m !== 'object' || Array.isArray(m)) return null;
  const v = pyInt((m as Record<string, unknown>).priority);
  return v !== null && v in PRIORITY_BOOST ? v : null;
}

/** rag._rerank: similarity + priority boost, descending; ties keep their order. */
export function rerank<T extends MatchRow>(rows: T[]): T[] {
  const key = (r: T) => -(similarity(r) + (PRIORITY_BOOST[priority(r) || 3] ?? 0));
  return rows
    .map((r, i) => ({ r, i, k: key(r) }))
    .sort((a, b) => (a.k < b.k ? -1 : a.k > b.k ? 1 : a.i - b.i))
    .map((x) => x.r);
}

/** The five rows the bot would put in front of the model, from the rows the search returned. */
export function botTop<T extends MatchRow>(searchRows: T[], wanted = MATCH_COUNT): T[] {
  return rerank(enforceRowFloor(dropInternal(dropNonEvidence(searchRows)))).slice(0, wanted);
}

// --- The impact check's comparison ---------------------------------------------

export type ImpactRow = {
  id: string;
  source_document: string;
  section_heading: string | null;
  chunk_type: string;
  similarity: number;
  /** A row carries a figure: its floor is set (publish and the import set 0.42 exactly then). */
  figures: boolean;
  from_batch: boolean;
  preview: string;
};

export type ProbeImpact = {
  question: string;
  service: string | null;
  audience: string | null;
  nationality: string | null;
  now: ImpactRow[];
  after: ImpactRow[];
  entering: string[];
  leaving: string[];
  bestNow: number;
  bestAfter: number;
  /** Best score falls under the soft floor (0.40) - a warning, not a block. */
  weakAfter: boolean;
  /** Rows that enter the top five carrying a figure. */
  newFigures: string[];
};

export function compareProbe(
  probe: { question: string; service: string | null; audience: string | null; nationality: string | null },
  now: ImpactRow[],
  after: ImpactRow[],
): ProbeImpact {
  const nowIds = new Set(now.map((r) => r.id));
  const afterIds = new Set(after.map((r) => r.id));
  const entering = after.filter((r) => !nowIds.has(r.id));
  const bestNow = now[0]?.similarity ?? 0;
  const bestAfter = after[0]?.similarity ?? 0;
  return {
    ...probe,
    now,
    after,
    entering: entering.map((r) => r.id),
    leaving: now.filter((r) => !afterIds.has(r.id)).map((r) => r.id),
    bestNow,
    bestAfter,
    weakAfter: bestAfter < SOFT_FLOOR,
    newFigures: entering.filter((r) => r.figures).map((r) => r.id),
  };
}
