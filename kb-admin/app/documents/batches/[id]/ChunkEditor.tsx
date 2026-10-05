'use client';

import { useState } from 'react';
import { useFormState, useFormStatus } from 'react-dom';
import { editChunk, type ActionState } from '@/app/docs/actions';
import { label } from '@/components/labels';
import { AUDIENCES, NATIONALITIES } from '@/lib/editing';

function Save() {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="btn btn-primary" disabled={pending}>
      {pending ? 'Saving…' : 'Save chunk'}
    </button>
  );
}

export type ChunkValues = {
  id: string;
  ordinal: number;
  chunk_type: 'passage' | 'table_unit';
  section_heading: string | null;
  content: string;
  service: string;
  audience: string;
  nationality: string;
  namespace: string;
};

/** Edit one staged chunk. A changed text or heading is embedded again when saved; the impact check must then run again. */
export function ChunkEditor({ chunk, services, namespaces }: { chunk: ChunkValues; services: string[]; namespaces: string[] }) {
  const [open, setOpen] = useState(false);
  const [content, setContent] = useState(chunk.content);
  const [raw, formAction] = useFormState(editChunk, { error: '' });
  const state: ActionState = raw ?? { error: '' };
  const limit = chunk.chunk_type === 'table_unit' ? 3000 : 1200;
  if (!open) {
    return (
      <button type="button" className="btn btn-secondary" onClick={() => setOpen(true)}>
        Edit chunk {chunk.ordinal}
      </button>
    );
  }
  const serviceOptions = services.includes(chunk.service) ? services : [chunk.service, ...services];
  return (
    <form action={formAction} className="card card-pad space-y-3" data-editing={chunk.ordinal}>
      <input type="hidden" name="chunk_id" value={chunk.id} />
      <div>
        <label className="field-label mb-1.5 block" htmlFor={`h-${chunk.id}`}>Heading</label>
        <input id={`h-${chunk.id}`} name="section_heading" className="input" defaultValue={chunk.section_heading ?? ''} maxLength={1200} />
      </div>
      <div>
        <label className="field-label mb-1.5 block" htmlFor={`c-${chunk.id}`}>Text</label>
        <textarea id={`c-${chunk.id}`} name="content" className="textarea" rows={8} value={content} onChange={(e) => setContent(e.target.value)} />
        <p className="mono mt-1 text-[12px]" style={{ color: content.length > limit ? 'var(--bad)' : 'var(--muted)' }}>
          {content.length} / {limit} characters
        </p>
      </div>
      <div className="grid grid-cols-4 gap-2">
        <select name="service" aria-label="Service" className="select" defaultValue={chunk.service}>
          {serviceOptions.map((s) => (
            <option key={s} value={s}>{label('service', s)}</option>
          ))}
        </select>
        <select name="audience" aria-label="Audience" className="select" defaultValue={chunk.audience}>
          {AUDIENCES.map((a) => (
            <option key={a} value={a}>{label('audience', a)}</option>
          ))}
        </select>
        <select name="nationality" aria-label="Nationality" className="select" defaultValue={chunk.nationality}>
          {NATIONALITIES.map((n) => (
            <option key={n} value={n}>{label('nationality', n)}</option>
          ))}
        </select>
        <select name="namespace" aria-label="Namespace" className="select" defaultValue={chunk.namespace}>
          {(namespaces.includes(chunk.namespace) ? namespaces : [chunk.namespace, ...namespaces]).map((n) => (
            <option key={n} value={n}>{n}</option>
          ))}
        </select>
      </div>
      {state.error ? (
        <div className="notice notice-red" role="alert">
          {state.error}
        </div>
      ) : null}
      <div className="flex gap-2">
        <Save />
        <button type="button" className="btn btn-secondary" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </form>
  );
}
