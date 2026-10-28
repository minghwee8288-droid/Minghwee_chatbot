import 'server-only';
import { select } from './db';
import type { EntryText } from './watcher/pipeline';

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

/**
 * Is migration 017 installed (per-source render and unverified, failed drafts
 * retried)? Same rule as 016: a yes is remembered, a no asked again after a
 * minute, so applying 017 needs no redeploy; until then nothing reads or calls
 * what 017 adds.
 */
let schema17: { ok: boolean; at: number } | null = null;
export async function watcherSchema17(): Promise<boolean> {
  if (schema17 && (schema17.ok || Date.now() - schema17.at < 60_000)) return schema17.ok;
  const [row] = await select<{ n: number }>(`
    select count(*)::int as n from information_schema.columns
     where table_schema = 'public'
       and ((table_name = 'cb_kb_sources' and column_name in ('render', 'allow_unverified'))
         or (table_name = 'cb_kb_source_snapshots' and column_name = 'draft_failed'))`);
  schema17 = { ok: (row?.n ?? 0) === 3, at: Date.now() };
  return schema17.ok;
}

/**
 * Is migration 019 installed (a proposal's tags proposed by the watcher, and
 * kb_admin_watch_set_tags to change them before approval; an approved edit is
 * published with the proposal's tags)? Same rule as 016/017: a yes is
 * remembered, a no asked again after a minute. Until then the tags are shown
 * read-only and the propose call gets nulls, exactly as before.
 */
let schema19: { ok: boolean; at: number } | null = null;
export async function watcherSchema19(): Promise<boolean> {
  if (schema19 && (schema19.ok || Date.now() - schema19.at < 60_000)) return schema19.ok;
  const [row] = await select<{ n: number }>(`
    select count(*)::int as n from pg_catalog.pg_proc p join pg_catalog.pg_namespace s on s.oid = p.pronamespace
     where s.nspname = 'public' and p.proname = 'kb_admin_watch_set_tags'`);
  schema19 = { ok: (row?.n ?? 0) === 1, at: Date.now() };
  return schema19.ok;
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
  /** 017: read through the page renderer. */
  render: boolean;
  /** 017: read without a certificate check (or over http), set by an approver with a reason. */
  allow_unverified: boolean;
  unverified_reason: string | null;
  unverified_set_by_email: string | null;
};

const SOURCE_COLUMNS = (has16: boolean, has17: boolean) => `
    s.id::text, s.url, s.label, s.category, s.active, s.added_by_email, s.added_at::text,
    s.last_checked_at::text, s.last_status, s.last_error,
    ${has16 ? 's.suggested_pages' : `'[]'::jsonb as suggested_pages`},
    ${has17 ? 's.render, s.allow_unverified, s.unverified_reason, s.unverified_set_by_email'
    : 'false as render, false as allow_unverified, null::text as unverified_reason, null::text as unverified_set_by_email'}`;

export async function watchedSources(): Promise<SourceRow[]> {
  const [has16, has17] = await Promise.all([watcherSchema16(), watcherSchema17()]);
  return select<SourceRow>(`
    select ${SOURCE_COLUMNS(has16, has17)},
           (select count(*)::int from public.cb_kb_drafts d where d.source_id = s.id and d.status in ('pending', 'blocked')) as open_drafts
      from public.cb_kb_sources s
     where s.active
     order by s.label`);
}

export async function sourceById(id: string): Promise<SourceRow | null> {
  const [has16, has17] = await Promise.all([watcherSchema16(), watcherSchema17()]);
  const [row] = await select<SourceRow>(`
    select ${SOURCE_COLUMNS(has16, has17)}, 0 as open_drafts
      from public.cb_kb_sources s where s.id = $1::uuid`, [id]);
  return row ?? null;
}

/** Active sources not checked for `days` days (or never) - and, with 017, any whose drafting failed (retried on the next run). */
export async function sourcesDue(days: number): Promise<SourceRow[]> {
  const [has16, has17] = await Promise.all([watcherSchema16(), watcherSchema17()]);
  return select<SourceRow>(`
    select ${SOURCE_COLUMNS(has16, has17)}, 0 as open_drafts
      from public.cb_kb_sources s
     where s.active and (s.last_checked_at is null or s.last_checked_at < now() - make_interval(days => $1::int)
                         ${has17 ? `or s.last_status = 'draft_failed'` : ''})
     order by s.last_checked_at nulls first, s.label`, [days]);
}

/** One snapshot's text (a re-draft reads the snapshots kb_admin_source_redraft names). */
export async function snapshotText(snapshotId: string): Promise<string | null> {
  const [row] = await select<{ extracted_text: string | null }>(`
    select extracted_text from public.cb_kb_source_snapshots where id = $1::uuid and fetch_ok`, [snapshotId]);
  return row?.extracted_text ?? null;
}

/**
 * The text of the last SUCCESSFUL snapshot, or null if the page was never
 * read. With 017, a snapshot whose drafting failed does not count: the next
 * change is diffed against the last snapshot that was actually drafted, so
 * what the failed run saw is proposed too.
 */
export async function lastGoodText(sourceId: string): Promise<string | null> {
  const has17 = await watcherSchema17();
  const [row] = await select<{ extracted_text: string }>(`
    select extracted_text from public.cb_kb_source_snapshots
     where source_id = $1::uuid and fetch_ok ${has17 ? 'and not draft_failed' : ''}
     order by fetched_at desc, id limit 1`, [sourceId]);
  return row?.extracted_text ?? null;
}

/** 017: was drafting from this snapshot recorded as failed? */
export async function draftFailed(snapshotId: string): Promise<boolean> {
  if (!(await watcherSchema17())) return false;
  const [row] = await select<{ failed: boolean }>(`
    select draft_failed as failed from public.cb_kb_source_snapshots where id = $1::uuid`, [snapshotId]);
  return Boolean(row?.failed);
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
  /** An edit: the live entry's tags now (null for a new entry). */
  current_service: string | null;
  current_audience: string | null;
  current_nationality: string | null;
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
  /** 017: the proposal comes from a page read without a certificate check (or over http). */
  source_unverified: boolean;
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
const DRAFT_17 = (has17: boolean) => has17 ? 's.allow_unverified as source_unverified' : 'false as source_unverified';

const DRAFT_COLUMNS = `
    d.id::text, d.source_id::text, s.label as source_label, s.category as source_category, d.source_url, d.source_fetched_at::text, d.kind,
    d.target_entry_id::text, d.old_question, d.old_answer, k.question as current_question, k.answer as current_answer,
    d.proposed_question, d.proposed_answer, d.section_heading, d.service, d.audience, d.nationality,
    k.service_type as current_service, k.contact_type as current_audience, k.nationality as current_nationality,
    d.source_passage, d.old_source_text, d.new_source_text, d.status, d.blocked_reason, d.figure_check, d.figure_confirmed, d.created_at::text,
    d.created_by_email, d.edited_by_email, d.reviewed_by_email, d.review_reason, d.approved_by_email,
    d.approved_at::text, d.entry_id_after::text, d.version_id::text, d.published_at::text`;

const DRAFT_FROM = `
      from public.cb_kb_drafts d
      join public.cb_kb_sources s on s.id = d.source_id
      left join public.cb_knowledge_base_updated k on k.id = d.target_entry_id`;

/** The review queue: open drafts first (oldest first), then the last 30 decided ones. */
export async function watchQueue(): Promise<{ open: WatchDraftRow[]; recent: WatchDraftRow[] }> {
  const [has16, has17] = await Promise.all([watcherSchema16(), watcherSchema17()]);
  const [open, recent] = await Promise.all([
    select<WatchDraftRow>(`select ${DRAFT_COLUMNS}, ${DRAFT_16(has16)}, ${DRAFT_17(has17)} ${DRAFT_FROM} where d.status in ('pending', 'blocked', 'approved') order by d.created_at`),
    select<WatchDraftRow>(`select ${DRAFT_COLUMNS}, ${DRAFT_16(has16)}, ${DRAFT_17(has17)} ${DRAFT_FROM} where d.status in ('published', 'rejected', 'superseded') order by coalesce(d.reviewed_at, d.created_at) desc limit 30`),
  ]);
  return { open, recent };
}

export async function watchDraft(id: string): Promise<WatchDraftRow | null> {
  const [has16, has17] = await Promise.all([watcherSchema16(), watcherSchema17()]);
  const [row] = await select<WatchDraftRow>(`select ${DRAFT_COLUMNS}, ${DRAFT_16(has16)}, ${DRAFT_17(has17)} ${DRAFT_FROM} where d.id = $1::uuid`, [id]);
  return row ?? null;
}

/**
 * The live question and answer of Q&A entries by id, for the watcher's
 * existing-entry search: kb_admin_match_live returns only a preview, so the
 * pipeline joins its matches to their text here (read-only, as kb_admin_reader).
 */
/** The proposal as the watcher drafted it, before any Edit (the first watch_edited audit row's old answer), or null when it was never edited. */
export async function modelDraftAnswer(draftId: string): Promise<string | null> {
  const [row] = await select<{ answer: string | null }>(
    `select a.old_values->>'answer' as answer from public.cb_kb_audit a
      where a.action = 'watch_edited' and a.new_values->>'draft_id' = $1
      order by a.created_at asc, a.id asc limit 1`, [draftId]);
  return row?.answer ?? null;
}

export async function liveEntryTexts(ids: string[]): Promise<EntryText[]> {
  const wanted = ids.filter((x) => /^[0-9a-f-]{36}$/i.test(x));
  if (!wanted.length) return [];
  // The tags too: an edit's proposal starts from the entry's own (lib/watcher/tags.ts).
  return select<EntryText>(`
    select id::text as id, question, answer, service_type, contact_type, nationality from public.cb_knowledge_base_updated
     where id = any(string_to_array($1, ',')::uuid[])`, [wanted.join(',')]);
}
