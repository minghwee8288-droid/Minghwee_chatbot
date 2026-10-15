import 'server-only';
import { select } from './db';

/**
 * Every read the KB watcher makes (migration 015's three tables), as
 * kb_admin_reader inside BEGIN READ ONLY. Values are always bound parameters.
 */

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
};

export async function watchedSources(): Promise<SourceRow[]> {
  return select<SourceRow>(`
    select s.id::text, s.url, s.label, s.category, s.active, s.added_by_email, s.added_at::text,
           s.last_checked_at::text, s.last_status, s.last_error,
           (select count(*)::int from public.cb_kb_drafts d where d.source_id = s.id and d.status in ('pending', 'blocked')) as open_drafts
      from public.cb_kb_sources s
     where s.active
     order by s.label`);
}

export async function sourceById(id: string): Promise<SourceRow | null> {
  const [row] = await select<SourceRow>(`
    select s.id::text, s.url, s.label, s.category, s.active, s.added_by_email, s.added_at::text,
           s.last_checked_at::text, s.last_status, s.last_error, 0 as open_drafts
      from public.cb_kb_sources s where s.id = $1::uuid`, [id]);
  return row ?? null;
}

/** Active sources not checked for `days` days (or never). */
export async function sourcesDue(days: number): Promise<SourceRow[]> {
  return select<SourceRow>(`
    select s.id::text, s.url, s.label, s.category, s.active, s.added_by_email, s.added_at::text,
           s.last_checked_at::text, s.last_status, s.last_error, 0 as open_drafts
      from public.cb_kb_sources s
     where s.active and (s.last_checked_at is null or s.last_checked_at < now() - make_interval(days => $1::int))
     order by s.last_checked_at nulls first, s.label`, [days]);
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

const DRAFT_COLUMNS = `
    d.id::text, d.source_id::text, s.label as source_label, d.source_url, d.source_fetched_at::text, d.kind,
    d.target_entry_id::text, d.old_question, d.old_answer, k.question as current_question, k.answer as current_answer,
    d.proposed_question, d.proposed_answer, d.section_heading, d.service, d.audience, d.nationality,
    d.source_passage, d.old_source_text, d.new_source_text, d.status, d.blocked_reason, d.created_at::text,
    d.created_by_email, d.edited_by_email, d.reviewed_by_email, d.review_reason, d.approved_by_email,
    d.approved_at::text, d.entry_id_after::text, d.version_id::text, d.published_at::text`;

const DRAFT_FROM = `
      from public.cb_kb_drafts d
      join public.cb_kb_sources s on s.id = d.source_id
      left join public.cb_knowledge_base_updated k on k.id = d.target_entry_id`;

/** The review queue: open drafts first (oldest first), then the last 30 decided ones. */
export async function watchQueue(): Promise<{ open: WatchDraftRow[]; recent: WatchDraftRow[] }> {
  const [open, recent] = await Promise.all([
    select<WatchDraftRow>(`select ${DRAFT_COLUMNS} ${DRAFT_FROM} where d.status in ('pending', 'blocked', 'approved') order by d.created_at`),
    select<WatchDraftRow>(`select ${DRAFT_COLUMNS} ${DRAFT_FROM} where d.status in ('published', 'rejected', 'superseded') order by coalesce(d.reviewed_at, d.created_at) desc limit 30`),
  ]);
  return { open, recent };
}

export async function watchDraft(id: string): Promise<WatchDraftRow | null> {
  const [row] = await select<WatchDraftRow>(`select ${DRAFT_COLUMNS} ${DRAFT_FROM} where d.id = $1::uuid`, [id]);
  return row ?? null;
}
