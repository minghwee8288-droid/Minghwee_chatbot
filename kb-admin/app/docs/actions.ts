'use server';

import { redirect } from 'next/navigation';
import { actorForWrite } from '@/lib/auth';
import { chunkDocument, ChunkerError, type Chunk } from '@/lib/chunker';
import { embeddingText, fileProblem, fileTypeOf, sourceProblem } from '@/lib/documents';
import { AUDIENCES, NATIONALITIES, hasNric, vectorLiteral } from '@/lib/editing';
import { EmbedError, embedQuestions, embedTexts } from '@/lib/embed';
import {
  batch,
  batchesOf,
  documentBySource,
  editableServices,
  liveDocRows,
  metadataOf,
  namespaces,
  probes,
  qaPairCount,
  stagedChunk,
} from '@/lib/queries';
import { FETCH_COUNT, MATCH_THRESHOLD, botTop, compareProbe, type ImpactRow, type ProbeImpact } from '@/lib/retrieval';
import { callWrite } from '@/lib/write';

/**
 * Every document change kb-admin makes starts here. Each action:
 *   1. runs actorForWrite() FIRST - the session verified again and the role
 *      read from cb_kb_admin_users now (never trusted from the page);
 *   2. calls only the document and search functions listed in lib/access.ts through
 *      lib/write.ts, passing the verified user id and email - the database
 *      checks the role again and enforces every rule itself;
 *   3. turns the database's KB001-KB006 into a plain sentence.
 * The preview and the impact check write nothing.
 */

export type ActionState = { error: string; notice?: string };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const MAX_REASON = 500;
/** Chunks sent to kb_admin_doc_stage per call (each carries a 1536-number vector). */
const STAGE_GROUP = 20;

const text = (form: FormData, name: string, max = 4000) => String(form.get(name) ?? '').slice(0, max);
const id = (form: FormData, name: string) => {
  const v = String(form.get(name) ?? '');
  return UUID.test(v) ? v : null;
};

function log(action: string, userId: string, target: string, outcome: string) {
  console.info(`[documents] ${action} user=${userId.slice(0, 8)} target=${target.slice(0, 8)} ${outcome}`);
}

function docError(error: unknown): string {
  if (error instanceof EmbedError) return `The text could not be indexed for search (${error.message}). Nothing further was saved; try again.`;
  if (error instanceof ChunkerError) return error.message;
  const code = (error as { code?: string })?.code;
  const detail = String((error as { message?: string })?.message ?? '').replace(/\s+/g, ' ').trim();
  switch (code) {
    case 'KB001':
      return 'Your account is not allowed to do this. Ask for editor access, or sign in again.';
    case 'KB002':
      return `This could not be saved: ${detail || 'something in the form is not valid'}.`;
    case 'KB003':
      return `This could not be done now: ${detail || 'the document changed'}.`;
    case 'KB004':
      return 'This needs an approver.';
    case 'KB005':
      return `The search index refused the text: ${detail || 'embedding problem'}.`;
    case 'KB006':
      return 'The text contains what looks like an NRIC or FIN number. Remove it from the document and try again.';
    default:
      console.error('[documents] unexpected error', code ?? '', detail.slice(0, 200));
      return 'Something went wrong and nothing more was changed. Please try again.';
  }
}

/** A read that failed (the database could not be reached): say so, never show the raw error. */
function unreachable(error: unknown): string {
  console.error('[documents] read failed', String((error as { code?: string })?.code ?? ''), String((error as Error)?.message ?? '').slice(0, 120));
  return 'The knowledge base could not be read just now. Nothing was saved; try again.';
}

// ---------------------------------------------------------------------------
// Upload: preview (nothing saved), then prepare.

export type PreviewChunk = {
  ordinal: number;
  chunk_type: Chunk['chunk_type'];
  section_heading: string | null;
  content: string;
};

export type PreviewState = {
  error: string;
  preview?: {
    source: string;
    fileName: string;
    chunks: PreviewChunk[];
    /** The source's active document rows today (what this would replace). */
    live: { section_heading: string | null; content: string }[];
    qaPairs: number;
    /** Live rows the original import made (no kb-admin version yet). */
    importedRows: number;
    stagedElsewhere: boolean;
  };
};

type DocSettings = {
  file: File;
  type: 'docx' | 'md' | 'txt';
  source: string;
  label: string;
  service: string;
  audience: string;
  nationality: string;
  namespace: string;
};

async function readSettings(form: FormData): Promise<{ ok: true; s: DocSettings } | { ok: false; error: string }> {
  const file = form.get('file');
  if (!(file instanceof File)) return { ok: false, error: 'Choose a file.' };
  const problem = fileProblem(file.name, file.size);
  if (problem) return { ok: false, error: problem };
  const mode = text(form, 'source_mode', 10);
  const source = (mode === 'new' ? text(form, 'source_new', 500) : text(form, 'source_existing', 500)).trim();
  const sp = sourceProblem(source);
  if (sp) return { ok: false, error: sp };
  const service = text(form, 'service', 60).trim();
  const audience = text(form, 'audience', 20).trim();
  const nationality = text(form, 'nationality', 5).trim();
  const namespace = text(form, 'namespace', 100).trim();
  const [services, spaces] = await Promise.all([editableServices(), namespaces()]);
  if (!services.includes(service)) return { ok: false, error: 'Choose a service from the list.' };
  if (!(AUDIENCES as readonly string[]).includes(audience)) return { ok: false, error: 'Choose an audience from the list.' };
  if (!(NATIONALITIES as readonly string[]).includes(nationality)) return { ok: false, error: 'Choose a nationality from the list.' };
  if (namespace && !spaces.includes(namespace)) return { ok: false, error: 'Choose a namespace from the list.' };
  const label = text(form, 'display_label', 200).trim();
  return {
    ok: true,
    s: { file, type: fileTypeOf(file.name) as DocSettings['type'], source, label, service, audience, nationality, namespace },
  };
}

async function chunksOf(s: DocSettings): Promise<Chunk[]> {
  return chunkDocument(Buffer.from(await s.file.arrayBuffer()), s.type, s.source);
}

/** Step 1: read the file and show what would be staged. Writes nothing, embeds nothing. */
export async function previewDocument(_prev: PreviewState, form: FormData): Promise<PreviewState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  let read: Awaited<ReturnType<typeof readSettings>>;
  try {
    read = await readSettings(form);
  } catch (error) {
    return { error: unreachable(error) };
  }
  if (!read.ok) return { error: read.error };
  const s = read.s;
  let chunks: Chunk[];
  try {
    chunks = await chunksOf(s);
  } catch (error) {
    return { error: docError(error) };
  }
  if (!chunks.length) return { error: 'No text was found in this file.' };
  if (hasNric(chunks.map((c) => `${c.section_heading ?? ''} ${c.content}`).join(' '))) {
    return { error: 'The document contains what looks like an NRIC or FIN number. Remove it and upload again.' };
  }
  let live: Awaited<ReturnType<typeof liveDocRows>>;
  let qaPairs: number;
  let stagedElsewhere = false;
  try {
    let doc;
    [live, qaPairs, doc] = await Promise.all([liveDocRows(s.source), qaPairCount(s.source), documentBySource(s.source)]);
    if (doc) stagedElsewhere = (await batchesOf(doc.id)).some((b) => b.status === 'staged');
  } catch (error) {
    return { error: unreachable(error) };
  }
  log('preview', auth.actor.userId, 'new', `ok chunks=${chunks.length}`);
  return {
    error: '',
    preview: {
      source: s.source,
      fileName: s.file.name,
      chunks: chunks.map((c) => ({ ordinal: c.ordinal, chunk_type: c.chunk_type, section_heading: c.section_heading, content: c.content })),
      live: live.map((r) => ({ section_heading: r.section_heading, content: r.content })),
      qaPairs,
      importedRows: live.filter((r) => !r.batched).length,
      stagedElsewhere,
    },
  };
}

type Selection = { ordinal: number; include: boolean; service?: string; audience?: string; nationality?: string };

/** Step 2: store the file, embed the ticked chunks, stage them. Then the batch page. */
export async function prepareDocument(_prev: PreviewState, form: FormData): Promise<PreviewState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  const read = await readSettings(form);
  if (!read.ok) return { error: read.error };
  const s = read.s;

  let selections: Selection[];
  try {
    selections = JSON.parse(text(form, 'selections', 200_000)) as Selection[];
    if (!Array.isArray(selections)) throw new Error('not a list');
  } catch {
    return { error: 'The chunk choices were not received. Preview the file again.' };
  }
  let chunks: Chunk[];
  try {
    chunks = await chunksOf(s);
  } catch (error) {
    return { error: docError(error) };
  }
  // The file is chunked again here; the choices must be for exactly these chunks.
  const byOrdinal = new Map(selections.map((x) => [x.ordinal, x]));
  if (selections.length !== chunks.length || chunks.some((c) => !byOrdinal.has(c.ordinal))) {
    return { error: 'The file changed since it was previewed. Preview it again.' };
  }
  const services = await editableServices();
  const picked = chunks
    .filter((c) => byOrdinal.get(c.ordinal)?.include)
    .map((c) => {
      const sel = byOrdinal.get(c.ordinal) as Selection;
      return {
        chunk: c,
        service: sel.service && services.includes(sel.service) ? sel.service : s.service,
        audience: sel.audience && (AUDIENCES as readonly string[]).includes(sel.audience) ? sel.audience : s.audience,
        nationality: sel.nationality && (NATIONALITIES as readonly string[]).includes(sel.nationality) ? sel.nationality : s.nationality,
      };
    });
  if (!picked.length) return { error: 'Tick at least one chunk to prepare.' };

  let uploaded: { document_id: string; batch_id: string };
  try {
    const bytes = Buffer.from(await s.file.arrayBuffer());
    uploaded = await callWrite<{ document_id: string; batch_id: string }>('kb_admin_doc_upload', [
      actor.userId,
      actor.email,
      s.source,
      s.file.name,
      s.type,
      bytes,
      s.label || null,
    ]);
  } catch (error) {
    log('upload', actor.userId, 'new', `failed code=${(error as { code?: string })?.code ?? 'none'}`);
    return { error: docError(error) };
  }
  log('upload', actor.userId, uploaded.batch_id, 'ok');

  try {
    const { vectors, model } = await embedTexts(picked.map((p) => embeddingText(p.chunk.section_heading, p.chunk.content)));
    for (let i = 0; i < picked.length; i += STAGE_GROUP) {
      const group = picked.slice(i, i + STAGE_GROUP).map((p, j) => ({
        ordinal: p.chunk.ordinal,
        chunk_type: p.chunk.chunk_type,
        section_heading: p.chunk.section_heading,
        content: p.chunk.content,
        metadata: p.chunk.metadata,
        embedding: vectors[i + j],
        service: p.service,
        audience: p.audience,
        nationality: p.nationality,
        namespace: s.namespace || null,
      }));
      await callWrite<number>('kb_admin_doc_stage', [actor.userId, actor.email, uploaded.batch_id, group, model]);
    }
  } catch (error) {
    log('stage', actor.userId, uploaded.batch_id, `failed code=${(error as { code?: string })?.code ?? 'none'}`);
    return {
      error: `The file was stored but its chunks were not all prepared: ${docError(error)} Open the batch, discard it, and upload again.`,
    };
  }
  log('stage', actor.userId, uploaded.batch_id, `ok chunks=${picked.length}`);
  redirect(`/documents/batches/${uploaded.batch_id}?prepared=${picked.length}`);
}

// ---------------------------------------------------------------------------
// A staged batch.

export async function editChunk(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  const chunkId = id(form, 'chunk_id');
  if (!chunkId) return { error: 'No chunk was named.' };
  const current = await stagedChunk(chunkId);
  if (!current) return { error: 'That chunk no longer exists.' };
  const content = text(form, 'content', 3000).replace(/\r\n?/g, '\n').trim();
  const heading = text(form, 'section_heading', 1200).trim();
  if (!content) return { error: 'A chunk needs some text.' };
  if (hasNric(`${content} ${heading}`)) return { error: 'The text contains what looks like an NRIC or FIN number.' };
  // The heading is part of what is embedded, so a new heading needs a new vector too.
  const textChanged = content !== current.content || (heading || null) !== current.section_heading;
  try {
    let vector: number[] | null = null;
    let model: string | null = null;
    if (textChanged) {
      const embedded = await embedTexts([embeddingText(heading, content)]);
      vector = embedded.vectors[0];
      model = embedded.model;
    }
    await callWrite('kb_admin_doc_edit_chunk', [
      actor.userId,
      actor.email,
      chunkId,
      content,
      heading,
      text(form, 'service', 60),
      text(form, 'audience', 20),
      text(form, 'nationality', 5),
      text(form, 'namespace', 100) || null,
      vector,
      model,
    ]);
  } catch (error) {
    log('edit_chunk', actor.userId, chunkId, `failed code=${(error as { code?: string })?.code ?? 'none'}`);
    return { error: docError(error) };
  }
  log('edit_chunk', actor.userId, chunkId, `ok reembedded=${textChanged}`);
  redirect(`/documents/batches/${current.batch_id}?edited=${current.ordinal}`);
}

export async function discardBatch(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  const batchId = id(form, 'batch_id');
  if (!batchId) return { error: 'No batch was named.' };
  if (String(form.get('confirm') ?? '') !== 'yes') return { error: 'Tick the box to confirm.' };
  const b = await batch(batchId);
  try {
    await callWrite('kb_admin_doc_discard', [actor.userId, actor.email, batchId]);
  } catch (error) {
    return { error: docError(error) };
  }
  log('discard', actor.userId, batchId, 'ok');
  redirect(`/documents/history?source=${encodeURIComponent(b?.source_name ?? '')}&discarded=1`);
}

export type ImpactReport = {
  batchId: string;
  model: string;
  ranAt: string;
  probes: ProbeImpact[];
};

export type ImpactState = { error: string; report?: ImpactReport };

/** The 42 probe questions, top five now against top five with this batch in place. Writes nothing. */
export async function runImpactCheck(_prev: ImpactState, form: FormData): Promise<ImpactState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  const batchId = id(form, 'batch_id');
  if (!batchId) return { error: 'No batch was named.' };
  const b = await batch(batchId);
  if (!b || b.status !== 'staged') return { error: 'Only a batch being prepared can be checked.' };

  type Found = { id: string; chunk_type: string; source_document: string; section_heading: string | null;
                 rag_score_floor: string | number | null; similarity: number; preview: string; from_batch?: boolean;
                 metadata?: Record<string, unknown> | null };
  try {
    const list = await probes();
    const { vectors, model } = await embedQuestions(list.map((p) => p.question_text));
    const raw: { now: Found[]; after: Found[] }[] = [];
    // Two searches per question, a few questions at a time.
    for (let i = 0; i < list.length; i += 4) {
      raw.push(
        ...(await Promise.all(
          list.slice(i, i + 4).map(async (p, j) => {
            const v = vectorLiteral(vectors[i + j]);
            const [now, after] = await Promise.all([
              callWrite<Found[]>('kb_admin_match_live', [actor.userId, actor.email, v, p.service, p.audience, p.nationality, FETCH_COUNT, MATCH_THRESHOLD]),
              callWrite<Found[]>('kb_admin_match_with_batch', [
                actor.userId, actor.email, batchId, v, p.service, p.audience, p.nationality, FETCH_COUNT, MATCH_THRESHOLD,
              ]),
            ]);
            return { now, after };
          }),
        )),
      );
    }
    // The bot reranks by metadata.priority; the batch search does not return metadata.
    const meta = await metadataOf(raw.flatMap((r) => r.after.map((x) => x.id)));
    const toRow = (r: Found): ImpactRow => ({
      id: r.id,
      source_document: r.source_document,
      section_heading: r.section_heading,
      chunk_type: r.chunk_type,
      similarity: Number(r.similarity),
      figures: r.rag_score_floor !== null && r.rag_score_floor !== undefined,
      from_batch: Boolean(r.from_batch),
      preview: r.preview ?? '',
    });
    const report: ImpactReport = {
      batchId,
      model,
      ranAt: new Date().toISOString(),
      probes: list.map((p, i) =>
        compareProbe(
          { question: p.question_text, service: p.service, audience: p.audience, nationality: p.nationality },
          botTop(raw[i].now).map(toRow),
          botTop(raw[i].after.map((r) => ({ ...r, metadata: meta[r.id] ?? null }))).map(toRow),
        ),
      ),
    };
    log('impact', actor.userId, batchId, `ok probes=${list.length}`);
    return { error: '', report };
  } catch (error) {
    log('impact', actor.userId, batchId, `failed code=${(error as { code?: string })?.code ?? 'none'}`);
    return { error: docError(error) };
  }
}

export async function markChecked(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  const batchId = id(form, 'batch_id');
  if (!batchId) return { error: 'No batch was named.' };
  try {
    await callWrite('kb_admin_doc_mark_checked', [actor.userId, actor.email, batchId]);
  } catch (error) {
    return { error: docError(error) };
  }
  log('mark_checked', actor.userId, batchId, 'ok');
  redirect(`/documents/batches/${batchId}?checked=1`);
}

// ---------------------------------------------------------------------------
// Approver only: publish, restore, retire.

export async function publishBatch(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  if (!actor.canApprove) return { error: 'Only an approver can publish a document.' };
  const batchId = id(form, 'batch_id');
  if (!batchId) return { error: 'No batch was named.' };
  const reason = text(form, 'reason', MAX_REASON).trim();
  if (!reason) return { error: 'Please say why you are publishing this version.' };
  let r: { rows_inserted: number; rows_retired: number; baseline_batch_id: string | null };
  try {
    r = await callWrite('kb_admin_doc_publish', [actor.userId, actor.email, batchId, reason]);
  } catch (error) {
    return { error: docError(error) };
  }
  log('publish', actor.userId, batchId, `ok in=${r.rows_inserted} out=${r.rows_retired}`);
  const q = new URLSearchParams({ published: '1', added: String(r.rows_inserted), off: String(r.rows_retired) });
  if (r.baseline_batch_id) q.set('baseline', r.baseline_batch_id);
  redirect(`/documents/batches/${batchId}?${q.toString()}`);
}

export async function restoreBatch(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  if (!actor.canApprove) return { error: 'Only an approver can restore an earlier version.' };
  const batchId = id(form, 'batch_id');
  if (!batchId) return { error: 'No version was named.' };
  const reason = text(form, 'reason', MAX_REASON).trim();
  if (!reason) return { error: 'Please say why you are restoring this version.' };
  const b = await batch(batchId);
  let r: { rows_restored: number; rows_retired: number };
  try {
    r = await callWrite('kb_admin_doc_restore', [actor.userId, actor.email, batchId, reason]);
  } catch (error) {
    return { error: docError(error) };
  }
  log('restore', actor.userId, batchId, `ok on=${r.rows_restored} off=${r.rows_retired}`);
  redirect(`/documents/history?source=${encodeURIComponent(b?.source_name ?? '')}&restored=${r.rows_restored}&off=${r.rows_retired}`);
}

export async function retireDocument(_prev: ActionState, form: FormData): Promise<ActionState> {
  const auth = await actorForWrite();
  if (!auth.ok) return { error: auth.message };
  const { actor } = auth;
  if (!actor.canApprove) return { error: 'Only an approver can retire a document.' };
  const documentId = id(form, 'document_id');
  if (!documentId) return { error: 'No document was named.' };
  const reason = text(form, 'reason', MAX_REASON).trim();
  if (!reason) return { error: 'Please say why you are retiring this document.' };
  if (String(form.get('confirm') ?? '') !== 'yes') return { error: 'Tick the box to confirm that the whole document goes off.' };
  let r: { rows_retired: number };
  try {
    r = await callWrite('kb_admin_doc_retire', [actor.userId, actor.email, documentId, reason]);
  } catch (error) {
    return { error: docError(error) };
  }
  log('retire', actor.userId, documentId, `ok off=${r.rows_retired}`);
  const source = String(form.get('source') ?? '');
  redirect(`/documents/history?source=${encodeURIComponent(source)}&retired=${r.rows_retired}`);
}
