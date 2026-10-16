import { NextResponse, type NextRequest } from 'next/server';
import { embedTexts } from '@/lib/embed';
import { env } from '@/lib/env';
import { checkCronRequest } from '@/lib/watcher/cron';
import { draftWithModel } from '@/lib/watcher/llm';
import { checkSource, type WatchCall } from '@/lib/watcher/pipeline';
import { watchFetcher } from '@/lib/watchFetcher';
import { draftFailed, lastGoodText, liveEntryTexts, sourcesDue, watcherSchema16, watcherSchema17 } from '@/lib/watchQueries';
import { callWrite } from '@/lib/write';
import { membership } from '@/lib/queries';

/**
 * The scheduled watcher (stage 2). Vercel's cron calls GET with
 * "Authorization: Bearer <CRON_SECRET>" (kb-admin/vercel.json, daily); this
 * route has no session, so it checks that secret itself (checkCronRequest)
 * and the middleware lets it through without one.
 *
 * Off unless WATCHER_ENABLED=true AND KB_SOURCES_ENABLED=true AND CRON_SECRET
 * and KB_WATCHER_ACTOR_ID are set (lib/config.ts): otherwise 404, as if the
 * route did not exist. When on, it re-checks every active source not checked
 * for WATCHER_INTERVAL_DAYS (default 7), one at a time, politely
 * (lib/watcher/fetcher.ts), stopping early to stay inside the function's time
 * limit. A page whose normalised text is unchanged produces nothing; a changed
 * one produces DRAFTS for review - never a live change. Snapshots and drafts
 * are recorded as "watcher (scheduled)"; KB_WATCHER_ACTOR_ID (an active editor
 * account) is used only for the read-only live search, and the run stops if
 * that account is no longer an active editor or approver.
 */

export const dynamic = 'force-dynamic';
export const maxDuration = 300;
const TIME_BUDGET_MS = 240_000;

const call: WatchCall = <T,>(fn: string, params: unknown[]) => callWrite<T>(fn as Parameters<typeof callWrite>[0], params as Parameters<typeof callWrite>[1]);

export async function GET(req: NextRequest) {
  const cfg = env();
  const schedule = cfg.watcher?.schedule ?? null;
  if (!cfg.watcher || !schedule) return NextResponse.json({ error: 'not found' }, { status: 404 });
  if (!checkCronRequest(req.headers.get('authorization'), schedule.cronSecret)) return NextResponse.json({ error: 'not found' }, { status: 404 });

  const who = await membership(schedule.actorId);
  if (!who || !who.active || (who.role !== 'editor' && who.role !== 'approver')) {
    console.error('[watcher] schedule stopped: KB_WATCHER_ACTOR_ID is not an active editor or approver');
    return NextResponse.json({ ran: false, error: 'the watcher account is not an active editor' }, { status: 200 });
  }
  const searcher = { userId: schedule.actorId, email: 'watcher (scheduled)' };
  const started = Date.now();
  const due = await sourcesDue(schedule.intervalDays);
  const { fetcher, renders } = watchFetcher();
  const [has016, has017] = await Promise.all([watcherSchema16(), watcherSchema17()]);
  const summary = { due: due.length, checked: 0, unchanged: 0, changed: 0, failed: 0, drafts: 0, left: 0 };
  for (const source of due) {
    if (Date.now() - started > TIME_BUDGET_MS) {
      summary.left = due.length - summary.checked;
      break;
    }
    try {
      const r = await checkSource(source, {
        call,
        fetchPage: (u, o) => fetcher.fetchPage(u, o),
        renderPage: renders ? (u) => fetcher.renderPage(u) : undefined,
        previousText: lastGoodText,
        // 017: a failed draft marks its snapshot, and the next run drafts it again.
        draftFailed,
        has017,
        embed: embedTexts,
        recorder: null,
        searcher,
        entryTexts: liveEntryTexts,
        llm: draftWithModel,
        model: cfg.watcher.llm.model,
        has016,
        log: (line) => console.info(`[watcher] ${line}`),
      });
      summary.checked += 1;
      summary.drafts += r.drafts.length;
      if (r.outcome === 'failed') summary.failed += 1;
      else if (r.outcome === 'unchanged') summary.unchanged += 1;
      else summary.changed += 1;
    } catch (error) {
      // A failure after the fetch was recorded (drafting, embedding): logged, the
      // run carries on. With 017 the snapshot is marked and the next run drafts
      // it again; the page shows "Drafting failed" with the reason.
      summary.checked += 1;
      summary.failed += 1;
      console.error(`[watcher] source=${source.id.slice(0, 8)} drafting failed (nothing proposed; ${has017 ? 'retried on the next run' : 'Re-draft once fixed'}): ${String((error as Error)?.message ?? error).slice(0, 200)}`);
    }
  }
  console.info(`[watcher] scheduled run ${JSON.stringify(summary)}`);
  return NextResponse.json({ ran: true, ...summary });
}
