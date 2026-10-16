import 'server-only';
import { select } from './db';

/**
 * Every read the KB watcher makes (migration 015's three tables, and the
 * columns 016 adds), as kb_admin_reader inside BEGIN READ ONLY. Values are
 * always bound parameters.
 */

/**
 * Is migration 016 installed? Until it is, kb-admin reads and calls exactly
 * what it did with 015 alone (no suggested pages, no overlap panel, no
 * re-draft), so this commit can be deployed before the migration is applied.
 * A yes is remembered for the life of the process; a no is asked again after
 * a minute, so applying 016 needs no redeploy.
 */
let schema16: { ok: boolean; at: number } | null = null;
export async function watcherSchema16(): Promise<boolean> {
  if (schema16 && (schema16.ok || Date.now() - schema16.at < 60_000)) return schema16.ok;
  const [row] = await select<{ n: number }>(`
    select count(*)::int as n from information_schema.columns
     where table_schema = 'public'
       and ((table_name = 'cb_kb_drafts' and column_name in ('similar_entries', 'conflict_check'))
         or (table_name = 'cb_kb_sources' and column_name = 'suggested_pages'))`);
  schema16 = { ok: (row?.n ?? 0) === 3, at: Date.now() };
  return schema16.ok;
}

export type SuggestedPage = { url: string; label: string };

export type SourceRow = {
  id: string;
  url: string;
  label: string;
  category: string;
  active: boolean;
  added_by_email: string | null;
  added_at: string;
  last_checked_at: string | null;
  last_status: string | null;
  last_error: string | null;
  open_drafts: number;
  /** 016: an index page's sub-pages, offered as "Suggested pages to watch". */
  suggested_pages: SuggestedPage[];
};

const SOURCE_COLUMNS = (has16: boolean) => `
    s.id::text, s.url, s.label, s.category, s.active, s.added_by_email, s.added_at::text,
    s.last_checked_at::text, s.last_status, s.last_error,
    ${has16 ? 's.suggested_pages' : `'[]'::jsonb as suggested_pages`}`;

export async function watchedSources(): Promise<SourceRow[]> {
  const has16 = await watcherSchema16();
  return select<SourceRow>(`
    select ${SOURCE_COLUMNS(has16)},
           (select count(*)::int from public.cb_kb_drafts d where d.source_id = s.id and d.status in ('pending', 'blocked')) as open_drafts
      from public.cb_kb_sources s
     where s.active
     order by s.label`);
}

export async function sourceById(id: string): Promise<SourceRow | null> {
  const has16 = await watcherSchema16();
  const [row] = await select<SourceRow>(`
    select ${SOURCE_COLUMNS(has16)}, 0 as open_drafts
      from public.cb_kb_sources s where s.id = $1::uuid`, [id]);
  return row ?? null;
}

/** Active sources not checked for `days` days (or never). */
export async function sourcesDue(days: number): Promise<SourceRow[]> {
  const has16 = await watcherSchema16();
  return select<SourceRow>(`
    select ${SOURCE_COLUMNS(has16)}, 0 as open_drafts
      from public.cb_kb_sources s
     where s.active and (s.last_checked_at is null or s.last_checked_at < now() - make_interval(days => $1::int))
     order by s.last_checked_at nulls first, s.label`, [days]);
}

/** One snapshot's text (a re-draft reads the snapshots kb_admin_source_redraft names). */
export async function snapshotText(snapshotId: string): Promise<string | null> {
  const [row] = await select<{ extracted_text: string | null }>(`
    select extracted_text from public.cb_kb_source_snapshots where id = $1::uuid and fetch_ok`, [snapshotId]);
  return row?.extracted_text ?? null;
}

/** The text of the last SUCCESSFUL snapshot, or null if the page was never read. */
export async function lastGoodText(sourceId: string): Promise<string | null> {
  const [row] = await select<{ extracted_text: string }>(`
    select extracted_text from public.cb_kb_source_snapshots
     where source_id = $1::uuid and fetch_ok
     order by fetched_at desc, id limit 1`, [sourceId]);
  return row?.extracted_text ?? null;
}

export type WatchDraftRow = {
  id: string;
  source_id: string;
  source_label: string;
  source_category: string;
  source_url: string;
  source_fetched_at: string;
  kind: 'edit' | 'new';
  target_entry_id: string | null;
  old_question: string | null;
  old_answer: string | null;
  current_question: string | null;
  current_answer: string | null;
  proposed_question: string;
  proposed_answer: string;
  section_heading: string | null;
  service: string;
  audience: string;
  nationality: string;
  source_passage: string;
  old_source_text: string | null;
  new_source_text: string | null;
  status: string;
  blocked_reason: string | null;
  figure_check: boolean;
  figure_confirmed: boolean;
  /** 016: the similar live entries found when it was drafted, and whether it conflicts with one. */
  similar_entries: SimilarEntry[];
  conflict_check: boolean;
  conflict_note: string | null;
  conflict_confirmed: boolean;
  created_at: string;
  created_by_email: string | null;
  edited_by_email: string | null;
  reviewed_by_email: string | null;
  review_reason: string | null;
  approved_by_email: string | null;
  approved_at: string | null;
  entry_id_after: string | null;
  version_id: string | null;
  published_at: string | null;
};

export type SimilarEntry = {
  id: string;
  question: string;
  similarity: number;
  differs?: { proposal: string[]; entry: string[] };
  conflicts?: string[];
};

const DRAFT_16 = (has16: boolean) => has16
  ? 'd.similar_entries, d.conflict_check, d.conflict_note, d.conflict_confirmed'
  : `'[]'::jsonb as similar_entries, false as conflict_check, null::text as conflict_note, false as conflict_confirmed`;

const DRAFT_COLUMNS = `
    d.id::text, d.source_id::text, s.label as source_label, s.category as source_category, d.source_url, d.source_fetched_at::text, d.kind,
    d.target_entry_id::text, d.old_question, d.old_answer, k.question as current_question, k.answer as current_answer,
    d.proposed_question, d.proposed_answer, d.section_heading, d.service, d.audience, d.nationality,
    d.source_passage, d.old_source_text, d.new_source_text, d.status, d.blocked_reason, d.figure_check, d.figure_confirmed, d.created_at::text,
    d.created_by_email, d.edited_by_email, d.reviewed_by_email, d.review_reason, d.approved_by_email,
    d.approved_at::text, d.entry_id_after::text, d.version_id::text, d.published_at::text`;

const DRAFT_FROM = `
      from public.cb_kb_drafts d
      join public.cb_kb_sources s on s.id = d.source_id
      left join public.cb_knowledge_base_updated k on k.id = d.target_entry_id`;

/** The review queue: open drafts first (oldest first), then the last 30 decided ones. */
export async function watchQueue(): Promise<{ open: WatchDraftRow[]; recent: WatchDraftRow[] }> {
  const has16 = await watcherSchema16();
  const [open, recent] = await Promise.all([
    select<WatchDraftRow>(`select ${DRAFT_COLUMNS}, ${DRAFT_16(has16)} ${DRAFT_FROM} where d.status in ('pending', 'blocked', 'approved') order by d.created_at`),
    select<WatchDraftRow>(`select ${DRAFT_COLUMNS}, ${DRAFT_16(has16)} ${DRAFT_FROM} where d.status in ('published', 'rejected', 'superseded') order by coalesce(d.reviewed_at, d.created_at) desc limit 30`),
  ]);
  return { open, recent };
}

export async function watchDraft(id: string): Promise<WatchDraftRow | null> {
  const has16 = await watcherSchema16();
  const [row] = await select<WatchDraftRow>(`select ${DRAFT_COLUMNS}, ${DRAFT_16(has16)} ${DRAFT_FROM} where d.id = $1::uuid`, [id]);
  return row ?? null;
}
