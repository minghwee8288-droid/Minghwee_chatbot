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
  /** An open draft exists. For a new entry (migration 008) the live row has no
   *  text yet, so the list shows the draft's question instead. */
  has_draft: boolean;
  draft_question: string | null;
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
    where.push(`(k.question ilike ${p} or k.answer ilike ${p} or k.content ilike ${p} or k.section_heading ilike ${p}
                 or d.question ilike ${p} or d.answer ilike ${p})`);
  }
  params.push(PAGE_SIZE, Math.max(0, q.page - 1) * PAGE_SIZE);
  return select<RowSummary>(`
    select k.id::text, k.question, k.section_heading,
           left(coalesce(k.answer, k.content, d.answer, ''), 220) as snippet,
           k.service_type, k.contact_type, k.nationality, k.chunk_type, k.source_document,
           k.is_active, k.metadata->>'managed_by' as managed_by, k.updated_at::text,
           (d.id is not null) as has_draft, d.question as draft_question,
           (count(*) over ())::int as total_count
      from ${KB} k
      left join lateral (
           select v.id, v.question, v.answer from public.cb_kb_entry_versions v
            where v.entry_id = k.id and v.status = 'draft' limit 1) d on true
     ${where.length ? `where ${where.join(' and ')}` : ''}
     order by k.source_document, k.service_type, coalesce(k.question, d.question) nulls last, k.id
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

// --- Editor (Phase 2): history, drafts, activity. Read as kb_admin_reader;
// the embedding column of the versions table is not granted and never named.

/** Services the editor may choose: the bot's retrieval vocabulary (every
 *  service an active entry uses) - the same list the database accepts. */
export async function editableServices(): Promise<string[]> {
  const [found] = await select<{ services: string[] }>(`
    select array(select distinct service_type from ${KB} where is_active order by 1) as services`);
  return found?.services ?? [];
}

export type Version = {
  id: string;
  entry_id: string;
  version_number: number;
  question: string | null;
  answer: string | null;
  section_heading: string | null;
  service: string;
  audience: string;
  nationality: string;
  active: boolean;
  model_name: string | null;
  status: 'draft' | 'published' | 'superseded' | 'discarded';
  based_on_version: number | null;
  created_by: string | null;
  created_by_email: string | null;
  reason: string;
  created_at: string;
};

const VERSION_COLUMNS = `id::text, entry_id::text, version_number, question, answer, section_heading,
           service, audience, nationality, active, model_name, status, based_on_version,
           created_by::text, created_by_email, reason, created_at::text`;

export async function versions(entryId: string): Promise<Version[]> {
  if (!UUID.test(entryId)) return [];
  return select<Version>(`
    select ${VERSION_COLUMNS}
      from public.cb_kb_entry_versions
     where entry_id = $1
     order by version_number desc`, [entryId]);
}

export async function version(id: string): Promise<Version | null> {
  if (!UUID.test(id)) return null;
  const [found] = await select<Version>(`
    select ${VERSION_COLUMNS} from public.cb_kb_entry_versions where id = $1`, [id]);
  return found ?? null;
}

export async function openDraft(entryId: string): Promise<Version | null> {
  if (!UUID.test(entryId)) return null;
  const [found] = await select<Version>(`
    select ${VERSION_COLUMNS} from public.cb_kb_entry_versions
     where entry_id = $1 and status = 'draft'`, [entryId]);
  return found ?? null;
}

export type PendingDraft = Version & {
  live_question: string | null;
  live_answer: string | null;
  live_section_heading: string | null;
  live_service: string;
  live_audience: string;
  live_nationality: string;
  live_active: boolean;
};

/** Every open draft, oldest first, with the live entry beside it. */
export async function openDrafts(): Promise<PendingDraft[]> {
  return select<PendingDraft>(`
    select v.id::text, v.entry_id::text, v.version_number, v.question, v.answer, v.section_heading,
           v.service, v.audience, v.nationality, v.active, v.model_name, v.status, v.based_on_version,
           v.created_by::text, v.created_by_email, v.reason, v.created_at::text,
           k.question as live_question, k.answer as live_answer, k.section_heading as live_section_heading,
           k.service_type as live_service, k.contact_type as live_audience,
           k.nationality as live_nationality, k.is_active as live_active
      from public.cb_kb_entry_versions v
      join ${KB} k on k.id = v.entry_id
     where v.status = 'draft'
     order by v.created_at`);
}

export type CanaryRow = { sentence: string; embedding: string; model: string; created_at: string };

export async function canary(): Promise<CanaryRow | null> {
  const [found] = await select<CanaryRow>(`
    select sentence, embedding::text as embedding, model, created_at::text
      from public.cb_kb_canary where id = 1`);
  return found ?? null;
}

export type AuditRow = {
  id: string;
  entry_id: string | null;
  action: string;
  actor_email: string | null;
  actor_db_role: string;
  reason: string;
  self_approved: boolean;
  created_at: string;
  question: string | null;
  old_values: Record<string, unknown> | null;
  new_values: Record<string, unknown> | null;
};

/** The latest 100 audit rows, newest first, with the entry's current question. */
export async function activity(): Promise<AuditRow[]> {
  return select<AuditRow>(`
    select a.id::text, a.entry_id::text, a.action, a.actor_email, a.actor_db_role, a.reason,
           a.self_approved, a.created_at::text, k.question,
           a.old_values, a.new_values
      from public.cb_kb_audit a
      left join ${KB} k on k.id = a.entry_id
     order by a.created_at desc, a.id desc
     limit 100`);
}
