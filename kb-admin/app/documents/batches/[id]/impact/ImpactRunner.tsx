'use client';

import { useFormState, useFormStatus } from 'react-dom';
import { markChecked, runImpactCheck, type ActionState, type ImpactState } from '@/app/docs/actions';
import { documentLabel, label } from '@/components/labels';
import { Pill } from '@/components/ui';
import type { ImpactRow, ProbeImpact } from '@/lib/retrieval';

function Run({ again }: { again: boolean }) {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="btn btn-primary" disabled={pending}>
      {pending ? 'Running 42 questions…' : again ? 'Run again' : 'Run the impact check'}
    </button>
  );
}

function Mark() {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="btn btn-primary" disabled={pending}>
      {pending ? 'Recording…' : 'Mark as checked'}
    </button>
  );
}

function Rows({ rows, highlight, tone }: { rows: ImpactRow[]; highlight: string[]; tone: 'leave' | 'enter' }) {
  if (!rows.length) return <p className="text-[12px] text-muted">Nothing above the threshold.</p>;
  return (
    <ol className="space-y-1 text-[12px]">
      {rows.map((r) => {
        const moved = highlight.includes(r.id);
        return (
          <li key={r.id} className={moved ? (tone === 'enter' ? 'diff-ins' : 'diff-del') : undefined}>
            <span className="mono">{r.similarity.toFixed(3)}</span> {r.from_batch ? <Pill tone="warn">new</Pill> : null}
            {r.figures ? <Pill tone="lock">figures</Pill> : null} <span title={r.source_document}>{documentLabel(r.source_document)}</span>
            {r.section_heading ? <span className="text-muted"> — {r.section_heading}</span> : null}
            <span className="block text-muted">{r.preview.replace(/\s+/g, ' ').slice(0, 140)}</span>
          </li>
        );
      })}
    </ol>
  );
}

function Probe({ p, source }: { p: ProbeImpact; source: string }) {
  const changed = p.entering.length || p.leaving.length;
  const touches = (rows: ImpactRow[]) => rows.some((r) => r.source_document === source);
  return (
    <details className="card" open={Boolean(changed) || p.weakAfter} data-probe={p.question}>
      <summary className="card-pad flex cursor-pointer flex-wrap items-center gap-2 text-[13px]">
        <span className="font-medium">{p.question}</span>
        <span className="text-muted">
          {[p.service && label('service', p.service), p.audience && label('audience', p.audience), p.nationality && label('nationality', p.nationality)]
            .filter(Boolean)
            .join(' · ') || 'no filter'}
        </span>
        {changed ? <Pill tone="warn">{p.entering.length} in, {p.leaving.length} out</Pill> : <Pill tone="off">No change</Pill>}
        {p.weakAfter ? <Pill tone="bad">Best score {p.bestAfter.toFixed(3)} (under 0.40)</Pill> : null}
        {p.newFigures.length ? <Pill tone="lock">{p.newFigures.length} new row{p.newFigures.length === 1 ? '' : 's'} with figures</Pill> : null}
        {touches(p.now) || touches(p.after) ? <Pill tone="good">This document</Pill> : null}
      </summary>
      <div className="grid grid-cols-2 gap-4 px-5 pb-4">
        <div>
          <h3 className="field-label mb-1">Top 5 now (best {p.bestNow.toFixed(3)})</h3>
          <Rows rows={p.now} highlight={p.leaving} tone="leave" />
        </div>
        <div>
          <h3 className="field-label mb-1">Top 5 after publishing (best {p.bestAfter.toFixed(3)})</h3>
          <Rows rows={p.after} highlight={p.entering} tone="enter" />
        </div>
      </div>
    </details>
  );
}

/**
 * Runs the 42 probe questions twice - the bot's search now, and with this batch
 * in place of the document's live chunks - with the bot's own filtering and
 * reranking, and shows what moves. Recording the check is a separate click.
 */
export function ImpactRunner({ batchId, source, canMark }: { batchId: string; source: string; canMark: boolean }) {
  const [raw, run] = useFormState(runImpactCheck, { error: '' });
  const state: ImpactState = raw ?? { error: '' };
  const [rawMark, mark] = useFormState(markChecked, { error: '' });
  const marked: ActionState = rawMark ?? { error: '' };
  const r = state.report;
  const changed = r?.probes.filter((p) => p.entering.length || p.leaving.length) ?? [];
  const weak = r?.probes.filter((p) => p.weakAfter) ?? [];
  const figures = r?.probes.filter((p) => p.newFigures.length) ?? [];
  return (
    <div className="space-y-4">
      <form action={run} className="flex items-center gap-3">
        <input type="hidden" name="batch_id" value={batchId} />
        <Run again={Boolean(r)} />
        <span className="text-[12px] text-muted">Writes nothing. Takes a little while: 84 searches.</span>
      </form>
      {state.error ? (
        <div className="notice notice-red" role="alert">
          {state.error}
        </div>
      ) : null}
      {r ? (
        <>
          <div className="grid grid-cols-3 gap-4" data-impact="summary">
            <div className="card card-pad">
              <p className="stat-label">Questions whose top 5 changes</p>
              <p className="stat-value mono">{changed.length} / {r.probes.length}</p>
            </div>
            <div className="card card-pad">
              <p className="stat-label">Best score under 0.40 after</p>
              <p className="stat-value mono" style={weak.length ? { color: 'var(--amber-dot)' } : undefined}>{weak.length}</p>
            </div>
            <div className="card card-pad">
              <p className="stat-label">New top-5 rows carrying figures</p>
              <p className="stat-value mono">{figures.reduce((n, p) => n + p.newFigures.length, 0)}</p>
            </div>
          </div>
          {weak.length ? (
            <div className="notice notice-amber" role="status">
              <span className="notice-dot" aria-hidden="true" />
              <div>
                <p className="notice-head">Weak answers after publishing (a warning, not a block)</p>
                <p>{weak.map((p) => p.question).join(' · ')}</p>
              </div>
            </div>
          ) : null}
          <div className="space-y-2">
            {r.probes.map((p) => (
              <Probe key={p.question} p={p} source={source} />
            ))}
          </div>
          {canMark ? (
            <form action={mark} className="card card-pad space-y-2">
              <input type="hidden" name="batch_id" value={batchId} />
              <p className="text-[13px]">
                Recording the check lets an approver publish this exact version. Any later edit to a chunk clears it.
              </p>
              {marked.error ? (
                <div className="notice notice-red" role="alert">
                  {marked.error}
                </div>
              ) : null}
              <Mark />
            </form>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
