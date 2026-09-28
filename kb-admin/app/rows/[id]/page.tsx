import Link from 'next/link';
import { notFound } from 'next/navigation';
import { Pill, Shell } from '@/components/Shell';
import { requireViewer } from '@/lib/auth';
import { row } from '@/lib/queries';

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-1 border-t border-slate-100 py-2 sm:grid-cols-[10rem_1fr]">
      <dt className="text-xs uppercase text-slate-500">{label}</dt>
      <dd className="min-w-0 text-sm">{children}</dd>
    </div>
  );
}

function Text({ value }: { value: string | null }) {
  return value ? (
    <p className="whitespace-pre-wrap break-words">{value}</p>
  ) : (
    <span className="text-slate-400">—</span>
  );
}

export default async function RowPage({ params }: { params: { id: string } }) {
  const viewer = await requireViewer();
  const r = await row(params.id);
  if (!r) notFound();
  return (
    <Shell viewer={viewer} active="/rows">
      <Link href="/rows" className="text-sm text-brand">
        ← All rows
      </Link>
      <h1 className="mb-3 mt-2 text-xl font-semibold">{r.question || r.section_heading || '(untitled chunk)'}</h1>
      <div className="mb-4 flex flex-wrap gap-1">
        {r.is_active ? <Pill tone="green">active</Pill> : <Pill tone="amber">inactive</Pill>}
        <Pill>{r.service_type}</Pill>
        <Pill>{r.contact_type}</Pill>
        <Pill>{r.nationality}</Pill>
        <Pill>{r.chunk_type}</Pill>
      </div>
      <dl className="rounded-lg border border-slate-200 bg-white px-4 py-2">
        <Field label="Question"><Text value={r.question} /></Field>
        <Field label="Answer"><Text value={r.answer} /></Field>
        <Field label="Content"><Text value={r.content} /></Field>
        <Field label="Section heading"><Text value={r.section_heading} /></Field>
        <Field label="Source document"><Text value={r.source_document} /></Field>
        <Field label="Service">{r.service_type}</Field>
        <Field label="Audience">{r.contact_type}</Field>
        <Field label="Nationality">{r.nationality}</Field>
        <Field label="Type">{r.chunk_type}</Field>
        <Field label="Namespace">{r.namespace}</Field>
        <Field label="Metadata">
          <pre className="overflow-x-auto rounded bg-slate-50 p-2 text-xs">{JSON.stringify(r.metadata, null, 2)}</pre>
        </Field>
        <Field label="Created">{r.created_at}</Field>
        <Field label="Last changed">{r.updated_at}</Field>
        <Field label="Row id"><span className="font-mono text-xs">{r.id}</span></Field>
      </dl>
    </Shell>
  );
}
