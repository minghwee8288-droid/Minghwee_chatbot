import 'server-only';
import { canaryMatches, decodeEmbedding, parseVector } from './editing';
import { env } from './env';
import { canary } from './queries';

/**
 * Embedding an entry's new text, exactly as the bot does it.
 *
 * The bot (app/services/rag.py embed_query) calls the OpenAI embeddings API -
 * directly, or through OpenRouter when EMBEDDING_BASE_URL is set - with
 * model = EMBEDDING_MODEL, dimensions = 1536, and the text as given (it
 * redacts NRICs first; the database refuses text containing one, so there is
 * nothing to redact here). Its SDK asks for base64 float32; so does this.
 * The loader and kb_admin_publish embed question + newline + answer.
 *
 * The model is not a kb-admin setting: it is the model recorded in
 * cb_kb_canary, which the bot's own function produced (scripts/seed_kb_canary.py).
 * Before the first publish in this process, kb-admin embeds the canary
 * sentence and refuses to publish unless its vector matches the bot's -
 * proof that the key, the endpoint and the model give the bot's vectors.
 */

export const DIMENSIONS = 1536;

async function embedWith(model: string, text: string): Promise<number[]> {
  const cfg = env();
  if (!cfg.editor) throw new EmbedError('the editor is not configured');
  let res: Response;
  try {
    res = await fetch(`${cfg.editor.embedding.baseUrl}/embeddings`, {
      method: 'POST',
      headers: {
        authorization: `Bearer ${cfg.editor.embedding.apiKey}`,
        'content-type': 'application/json',
      },
      body: JSON.stringify({ model, input: text, dimensions: DIMENSIONS, encoding_format: 'base64' }),
      cache: 'no-store',
      signal: AbortSignal.timeout(30_000),
    });
  } catch {
    throw new EmbedError('the embedding service could not be reached');
  }
  if (!res.ok) throw new EmbedError(`the embedding service answered ${res.status}`);
  const data = (await res.json().catch(() => null)) as { data?: { embedding?: unknown }[] } | null;
  const vector = decodeEmbedding(data?.data?.[0]?.embedding);
  if (!vector || vector.length !== DIMENSIONS) throw new EmbedError('the embedding service returned no 1536-number vector');
  return vector;
}

export class EmbedError extends Error {}

/** The canary check, done once per process (and again if the canary row changes). */
let verified: string | null = null;

export async function verifiedModel(): Promise<string> {
  const row = await canary();
  if (!row) throw new EmbedError('the canary is not seeded (run scripts/seed_kb_canary.py), so no text can be embedded yet');
  const stamp = `${row.model}|${row.created_at}`;
  if (verified === stamp) return row.model;
  const ours = await embedWith(row.model, row.sentence);
  const result = canaryMatches(ours, parseVector(row.embedding));
  console.info(`[embed] canary check model=${row.model} similarity=${result.similarity.toFixed(6)} ${result.ok ? 'ok' : 'MISMATCH'}`);
  if (!result.ok) {
    throw new EmbedError("kb-admin's embedding does not match the bot's (canary check failed); nothing was published");
  }
  verified = stamp;
  return row.model;
}

/** Embed an entry's text with the canary-verified model. Returns the vector and that model's name. */
export async function embedEntry(text: string): Promise<{ vector: number[]; model: string }> {
  const model = await verifiedModel();
  return { vector: await embedWith(model, text), model };
}
