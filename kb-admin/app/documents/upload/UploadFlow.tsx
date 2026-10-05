'use client';

import { useState, useTransition } from 'react';
import { useFormState } from 'react-dom';
import { prepareDocument, previewDocument, type PreviewState } from '@/app/docs/actions';
import { FigureText } from '@/components/FigureText';
import { label } from '@/components/labels';
import { AUDIENCES, NATIONALITIES } from '@/lib/editing';
import { MAX_UPLOAD_BYTES, factWarnings, fileProblem, internalMarkers, sourceProblem } from '@/lib/documents';
import type { LostFact } from '@/lib/facts';

type Override = { include: boolean; service: string; audience: string; nationality: string };

const LIMIT = { passage: 1200, table_unit: 3000 } as const;

function FactList({ facts }: { facts: LostFact[] }) {
  const main = facts.filter((f) => f.kind !== 'number');
  const numbers = facts.filter((f) => f.kind === 'number');
  return (
    <div className="space-y-1">
      {main.length ? (
        <ul className="list-disc space-y-0.5 pl-5">
          {main.map((f) => (
            <li key={`${f.kind}|${f.fact}`}>
              <span className="mono font-semibold">{f.fact}</span> <span className="text-muted">({f.kind}, {f.liveRows} chunk{f.liveRows === 1 ? '' : 's'})</span>{' '}
              <span className="text-muted">…{f.example}…</span>
            </li>
          ))}
        </ul>
      ) : null}
      {numbers.length ? (
        <details>
          <summary className="cursor-pointer text-muted">
            {numbers.length} other number{numbers.length === 1 ? '' : 's'}
          </summary>
          <p className="mono mt-1">{numbers.map((f) => f.fact).join(', ')}</p>
        </details>
      ) : null}
    </div>
  );
}

/**
 * Upload in two steps. Preview reads the file and shows every chunk, with
 * warnings; it saves and embeds nothing. Prepare sends the same file again with
 * the choices made here: the server chunks it again, stores it, embeds the
 * ticked chunks and stages them as a batch for review.
 */
export function UploadFlow({
  sources,
  initialSource,
  services,
  namespaces,
}: {
  sources: string[];
  initialSource: string;
  services: string[];
  namespaces: string[];
}) {
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<'existing' | 'new'>(initialSource || sources.length ? 'existing' : 'new');
  const [existing, setExisting] = useState(initialSource || sources[0] || '');
  const [newName, setNewName] = useState('');
  const [displayLabel, setDisplayLabel] = useState('');
  const [service, setService] = useState(services.includes('general') ? 'general' : services[0] ?? '');
  const [audience, setAudience] = useState('all');
  const [nationality, setNationality] = useState('all');
  const [namespace, setNamespace] = useState('');
  const [overrides, setOverrides] = useState<Record<number, Override>>({});
  const [localError, setLocalError] = useState('');
  const [pending, startTransition] = useTransition();

  const [rawPreview, previewDispatch] = useFormState(previewDocument, { error: '' });
  const [rawPrepare, prepareDispatch] = useFormState(prepareDocument, { error: '' });
  const preview: PreviewState = rawPreview ?? { error: '' };
  const prepared: PreviewState = rawPrepare ?? { error: '' };
  const p = preview.preview;

  const source = mode === 'new' ? newName.trim() : existing;

  const form = (extra: Record<string, string> = {}) => {
    const fd = new FormData();
    if (file) fd.set('file', file);
    fd.set('source_mode', mode);
    fd.set('source_existing', existing);
    fd.set('source_new', newName);
    fd.set('display_label', displayLabel);
    fd.set('service', service);
    fd.set('audience', audience);
    fd.set('nationality', nationality);
    fd.set('namespace', namespace);
    for (const [k, v] of Object.entries(extra)) fd.set(k, v);
    return fd;
  };

  const check = (): string => {
    if (!file) return 'Choose a file.';
    return fileProblem(file.name, file.size) ?? sourceProblem(source) ?? '';
  };

  const onPreview = () => {
    const problem = check();
    setLocalError(problem);
    if (problem) return;
    setOverrides({});
    startTransition(() => previewDispatch(form()));
  };

  const chunks = p?.chunks ?? [];
  const opt = (ordinal: number): Override =>
    overrides[ordinal] ?? { include: true, service: '', audience: '', nationality: '' };
  const setOpt = (ordinal: number, patch: Partial<Override>) => setOverrides({ ...overrides, [ordinal]: { ...opt(ordinal), ...patch } });
  const ticked = chunks.filter((c) => opt(c.ordinal).include);

  const facts = p ? factWarnings(p.live, ticked) : { lost: [], stale: [] };
  const markers = chunks
    .map((c) => ({ ordinal: c.ordinal, found: internalMarkers(`${c.section_heading ?? ''}\n${c.content}`) }))
    .filter((m) => m.found.length);

  const onPrepare = () => {
    const problem = check();
    setLocalError(problem);
    if (problem || !p) return;
    if (!ticked.length) {
      setLocalError('Tick at least one chunk to prepare.');
      return;
    }
    const selections = chunks.map((c) => {
      const o = opt(c.ordinal);
      return { ordinal: c.ordinal, include: o.include, service: o.service || undefined, audience: o.audience || undefined, nationality: o.nationality || undefined };
    });
    startTransition(() => prepareDispatch(form({ selections: JSON.stringify(selections) })));
  };

  const stale = p && (p.fileName !== file?.name || p.source !== source);

  return (
    <div className="space-y-5">
      <div className="card card-pad space-y-4">
        <div>
          <label htmlFor="file" className="field-label mb-1.5 block">
            File (.docx, .md or .txt, up to {MAX_UPLOAD_BYTES / 1024 / 1024} MB)
          </label>
          <input
            id="file"
            name="file"
            type="file"
            accept=".docx,.md,.txt"
            className="input"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </div>

        <fieldset className="space-y-2">
          <legend className="field-label mb-1.5">Source</legend>
          <label className="flex items-center gap-2 text-[13px]">
            <input type="radio" name="source_mode" checked={mode === 'existing'} disabled={!sources.length} onChange={() => setMode('existing')} />
            Replace an existing document
          </label>
          {mode === 'existing' ? (
            <select id="source_existing" aria-label="Existing document" className="select" value={existing} onChange={(e) => setExisting(e.target.value)}>
              {sources.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          ) : null}
          <label className="flex items-center gap-2 text-[13px]">
            <input type="radio" name="source_mode" checked={mode === 'new'} onChange={() => setMode('new')} />
            A new document
          </label>
          {mode === 'new' ? (
            <input
              id="source_new"
              aria-label="New document name"
              className="input"
              maxLength={500}
              placeholder="The name the chatbot will cite, e.g. Ming Hwee Rest Day Guide.docx"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
            />
          ) : null}
        </fieldset>

        <div>
          <label htmlFor="display_label" className="field-label mb-1.5 block">
            Display label (optional)
          </label>
          <input id="display_label" className="input" maxLength={200} value={displayLabel} onChange={(e) => setDisplayLabel(e.target.value)} />
        </div>

        <div className="grid grid-cols-4 gap-3">
          <div>
            <label htmlFor="service" className="field-label mb-1.5 block">Service</label>
            <select id="service" className="select" value={service} onChange={(e) => setService(e.target.value)}>
              {services.map((s) => (
                <option key={s} value={s}>{label('service', s)}</option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="audience" className="field-label mb-1.5 block">Audience</label>
            <select id="audience" className="select" value={audience} onChange={(e) => setAudience(e.target.value)}>
              {AUDIENCES.map((a) => (
                <option key={a} value={a}>{label('audience', a)}</option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="nationality" className="field-label mb-1.5 block">Nationality</label>
            <select id="nationality" className="select" value={nationality} onChange={(e) => setNationality(e.target.value)}>
              {NATIONALITIES.map((n) => (
                <option key={n} value={n}>{label('nationality', n)}</option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="namespace" className="field-label mb-1.5 block">Namespace</label>
            <select id="namespace" className="select" value={namespace} onChange={(e) => setNamespace(e.target.value)}>
              <option value="">Usual for the service</option>
              {namespaces.map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </div>
        </div>

        {localError || preview.error ? (
          <div className="notice notice-red" role="alert">
            {localError || preview.error}
          </div>
        ) : null}
        <button type="button" className="btn btn-primary" disabled={pending} onClick={onPreview}>
          {pending && !p ? 'Reading…' : 'Preview'}
        </button>
      </div>

      {p ? (
        <>
          <div className="space-y-3">
            <h2 className="text-[15px] font-semibold">
              {chunks.length} chunk{chunks.length === 1 ? '' : 's'} from {p.fileName} · {ticked.length} ticked
            </h2>
            {stale ? (
              <div className="notice notice-amber" role="status">The file or source changed since this preview. Preview again before preparing.</div>
            ) : null}
            {p.stagedElsewhere ? (
              <div className="notice notice-red" role="alert">
                This document already has a version being prepared. Publish or discard that one first; preparing this would be refused.
              </div>
            ) : null}
            {facts.lost.length ? (
              <div className="notice notice-red" role="alert" data-warning="lost">
                <div>
                  <p className="notice-head">Facts the live version states that this upload drops</p>
                  <p className="mb-1">Publishing would remove these from what the chatbot can quote. Check each was meant to go.</p>
                  <FactList facts={facts.lost} />
                </div>
              </div>
            ) : null}
            {facts.stale.length && p.live.length ? (
              <div className="notice notice-amber" role="status" data-warning="stale">
                <span className="notice-dot" aria-hidden="true" />
                <div>
                  <p className="notice-head">Facts this upload states that the live version no longer does</p>
                  <p className="mb-1">These may be older figures coming back (the live rows were corrected after the source file was written). Confirm each is current.</p>
                  <FactList facts={facts.stale} />
                </div>
              </div>
            ) : null}
            {markers.length ? (
              <div className="notice notice-amber" role="status" data-warning="internal">
                <span className="notice-dot" aria-hidden="true" />
                <div>
                  <p className="notice-head">Text that looks written for staff, not clients</p>
                  <p>
                    {markers.map((m) => `chunk ${m.ordinal}: ${m.found.join(', ')}`).join(' · ')}. The chatbot may quote whatever is published.
                  </p>
                </div>
              </div>
            ) : null}
            {p.qaPairs ? (
              <div className="notice notice-amber" role="status" data-warning="qa">
                <span className="notice-dot" aria-hidden="true" />
                <div>
                  {p.qaPairs} Q&amp;A entr{p.qaPairs === 1 ? 'y' : 'ies'} from this source {p.qaPairs === 1 ? 'is' : 'are'} managed separately and will not change.
                </div>
              </div>
            ) : null}
            {p.importedRows ? (
              <div className="notice notice-amber" role="status" data-warning="imported">
                <span className="notice-dot" aria-hidden="true" />
                <div>
                  This document was imported with longer passages; the new version is split differently, so expect many changes in the impact check.
                  The {p.importedRows} imported chunk{p.importedRows === 1 ? '' : 's'} are kept as the original version and can be restored.
                </div>
              </div>
            ) : null}
          </div>

          <ol className="space-y-3">
            {chunks.map((c) => {
              const o = opt(c.ordinal);
              const limit = LIMIT[c.chunk_type];
              return (
                <li key={c.ordinal} className="card card-pad space-y-2" data-chunk={c.ordinal} style={o.include ? undefined : { opacity: 0.55 }}>
                  <div className="flex flex-wrap items-center gap-3">
                    <label className="flex items-center gap-2 text-[13px] font-medium">
                      <input type="checkbox" aria-label={`Include chunk ${c.ordinal}`} checked={o.include} onChange={(e) => setOpt(c.ordinal, { include: e.target.checked })} />
                      Chunk {c.ordinal}
                    </label>
                    <span className="pill pill-off">{c.chunk_type === 'table_unit' ? 'Table' : 'Passage'}</span>
                    <span className="mono text-[12px] text-muted">
                      {c.content.length} / {limit} characters
                    </span>
                    {internalMarkers(`${c.section_heading ?? ''}\n${c.content}`).length ? <span className="pill pill-warn">Internal wording?</span> : null}
                  </div>
                  <p className="text-[13px] font-semibold">{c.section_heading ?? <span className="text-muted">(no heading)</span>}</p>
                  <FigureText text={c.content} />
                  <div className="grid grid-cols-3 gap-2">
                    <select aria-label={`Service for chunk ${c.ordinal}`} className="select" value={o.service} onChange={(e) => setOpt(c.ordinal, { service: e.target.value })}>
                      <option value="">Service: as the document</option>
                      {services.map((s) => (
                        <option key={s} value={s}>{label('service', s)}</option>
                      ))}
                    </select>
                    <select aria-label={`Audience for chunk ${c.ordinal}`} className="select" value={o.audience} onChange={(e) => setOpt(c.ordinal, { audience: e.target.value })}>
                      <option value="">Audience: as the document</option>
                      {AUDIENCES.map((a) => (
                        <option key={a} value={a}>{label('audience', a)}</option>
                      ))}
                    </select>
                    <select aria-label={`Nationality for chunk ${c.ordinal}`} className="select" value={o.nationality} onChange={(e) => setOpt(c.ordinal, { nationality: e.target.value })}>
                      <option value="">Nationality: as the document</option>
                      {NATIONALITIES.map((n) => (
                        <option key={n} value={n}>{label('nationality', n)}</option>
                      ))}
                    </select>
                  </div>
                </li>
              );
            })}
          </ol>

          <div className="card card-pad space-y-3">
            <p className="text-[13px] text-muted">
              Prepare stores the file, embeds the {ticked.length} ticked chunk{ticked.length === 1 ? '' : 's'} and stages them for review. The chatbot sees nothing until
              an approver publishes. To drop a chunk later, discard the batch and upload again.
            </p>
            {prepared.error ? (
              <div className="notice notice-red" role="alert">
                {prepared.error}
              </div>
            ) : null}
            <button type="button" className="btn btn-primary" disabled={pending || Boolean(stale) || !ticked.length} onClick={onPrepare}>
              {pending ? 'Preparing…' : `Prepare ${ticked.length} chunk${ticked.length === 1 ? '' : 's'}`}
            </button>
          </div>
        </>
      ) : null}
    </div>
  );
}
