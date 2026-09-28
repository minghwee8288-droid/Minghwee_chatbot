import 'server-only';
import { select } from './db';

/**
 * Every SQL statement kb-admin runs is in this file. Values are always bound
 * parameters; the only text ever assembled is the WHERE clause, and only from
 * the fixed column list below.
 *
 * The columns read are the 15 granted to kb_admin_reader. embedding,
 * page_or_section, frequency and rag_score_floor are not granted and are
 * never named.
 */

const KB = 'public.cb_knowledge_base_updated';

export type DocumentSummary = {
  source_document: string;
  total: number;
  active: number;
  inactive: number;
  services: string[];
  last_changed: string;
};

export async function documents(): Promise<DocumentSummary[]> {
  return select<DocumentSummary>(`
    select source_document,
           count(*)::int as total,
           count(*) filter (where is_active)::int as active,
           count(*) filter (where not is_active)::int as inactive,
           array_agg(distinct service_type order by service_type) as services,
           max(updated_at)::text as last_changed
      from ${KB}
     group by source_document
     order by source_document`);
}

export type Totals = { total: number; active: number; inactive: number };

export async function totals(): Promise<Totals> {
  const [row] = await select<Totals>(`
    select count(*)::int as total,
           count(*) filter (where is_active)::int as active,
           count(*) filter (where not is_active)::int as inactive
      from ${KB}`);
  return row;
}

/** Filterable columns, and the query-string key for each. */
export const FILTERS = {
  source_document: 'source_document',
  service_type: 'service_type',
  contact_type: 'contact_type',
  nationality: 'nationality',
  chunk_type: 'chunk_type',
  managed_by: "(metadata->>'managed_by')",
} as const;

export type FilterKey = keyof typeof FILTERS;
export type FilterOptions = Record<FilterKey, string[]>;

export async function filterOptions(): Promise<FilterOptions> {
  const [row] = await select<FilterOptions>(`
    select array(select distinct source_document from ${KB} order by 1) as source_document,
           array(select distinct service_type from ${KB} order by 1) as service_type,
           array(select distinct contact_type from ${KB} order by 1) as contact_type,
           array(select distinct nationality from ${KB} order by 1) as nationality,
           array(select distinct chunk_type from ${KB} order by 1) as chunk_type,
           array(select distinct coalesce(metadata->>'managed_by', '(none)') from ${KB} order by 1) as managed_by`);
  return row;
}

export type RowQuery = {
  filters: Partial<Record<FilterKey, string>>;
  active: 'all' | 'active' | 'inactive';
  search: string;
  page: number;
};

export type RowSummary = {
  id: string;
  question: string | null;
  section_heading: string | null;
  snippet: string;
  service_type: string;
  contact_type: string;
  nationality: string;
  chunk_type: string;
  source_document: string | null;
  is_active: boolean;
  managed_by: string | null;
  updated_at: string;
  total_count: number;
};

export const PAGE_SIZE = 50;

/** Escape LIKE wildcards so a search for "50%" means those characters. */
function likePattern(text: string): string {
  return `%${text.replace(/[\\%_]/g, (c) => `\\${c}`)}%`;
}

export async function rows(q: RowQuery): Promise<RowSummary[]> {
  const where: string[] = [];
  const params: (string | number)[] = [];
  for (const key of Object.keys(FILTERS) as FilterKey[]) {
    const value = q.filters[key];
    if (!value) continue;
    params.push(value === '(none)' && key === 'managed_by' ? '' : value);
    const column = key === 'managed_by' ? "coalesce(metadata->>'managed_by', '')" : FILTERS[key];
    where.push(`${column} = $${params.length}`);
  }
  if (q.active === 'active') where.push('is_active');
  if (q.active === 'inactive') where.push('not is_active');
  const search = q.search.trim().slice(0, 200);
  if (search) {
    params.push(likePattern(search));
    const p = `$${params.length}`;
    where.push(`(question ilike ${p} or answer ilike ${p} or content ilike ${p} or section_heading ilike ${p})`);
  }
  params.push(PAGE_SIZE, Math.max(0, q.page - 1) * PAGE_SIZE);
  return select<RowSummary>(`
    select id::text, question, section_heading,
           left(coalesce(answer, content, ''), 220) as snippet,
           service_type, contact_type, nationality, chunk_type, source_document,
           is_active, metadata->>'managed_by' as managed_by, updated_at::text,
           (count(*) over ())::int as total_count
      from ${KB}
     ${where.length ? `where ${where.join(' and ')}` : ''}
     order by source_document, service_type, question nulls last, id
     limit $${params.length - 1} offset $${params.length}`, params);
}

export type RowDetail = {
  id: string;
  question: string | null;
  answer: string | null;
  content: string | null;
  section_heading: string | null;
  source_document: string | null;
  chunk_type: string;
  service_type: string;
  contact_type: string;
  nationality: string;
  namespace: string;
  is_active: boolean;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function row(id: string): Promise<RowDetail | null> {
  if (!UUID.test(id)) return null;
  const [found] = await select<RowDetail>(`
    select id::text, question, answer, content, section_heading, source_document,
           chunk_type, service_type, contact_type, nationality, namespace, is_active,
           metadata, created_at::text, updated_at::text
      from ${KB}
     where id = $1`, [id]);
  return found ?? null;
}

export type Rule = {
  id: string;
  rule_type: string;
  service_type: string;
  nationality: string;
  value: unknown;
  locked: boolean;
  updated_by: string;
  updated_at: string;
};

export async function rules(): Promise<Rule[]> {
  return select<Rule>(`
    select id::text, rule_type, service_type, nationality, value, locked, updated_by,
           updated_at::text
      from public.cb_kb_rules
     order by rule_type, service_type, nationality`);
}

export async function membership(userId: string): Promise<{ role: string; active: boolean } | undefined> {
  if (!UUID.test(userId)) return undefined;
  const [found] = await select<{ role: string; active: boolean }>(`
    select role, active from public.cb_kb_admin_users where user_id = $1`, [userId]);
  return found;
}
