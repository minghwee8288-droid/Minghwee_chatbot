import { NextResponse, type NextRequest } from 'next/server';
import { checkRequest } from '@/lib/auth';
import { env } from '@/lib/env';

/**
 * "Test a question": forwards to the bot's POST /admin/preview, which runs the
 * real conversation graph read-only and returns what the bot would say. The
 * preview secret is added HERE, server-side; the browser never sees it.
 *
 * Only the question(s) and the audience are forwarded. The bot also accepts a
 * `context` (record name, placed helper, ...), which could carry client data,
 * so kb-admin never sends one.
 */

const MAX_TURNS = 10;
const MAX_CHARS = 2000;
const AUDIENCES = new Set(['employer', 'candidate', 'unknown']);

function sameOrigin(req: NextRequest): boolean {
  const origin = req.headers.get('origin');
  if (!origin) return false;
  try {
    return new URL(origin).host === req.headers.get('host');
  } catch {
    return false;
  }
}

export async function POST(req: NextRequest) {
  if (!sameOrigin(req)) return NextResponse.json({ error: 'cross-origin request refused' }, { status: 403 });
  const auth = await checkRequest();
  if (!auth.ok) return NextResponse.json({ error: 'not signed in', reason: auth.reason }, { status: 401 });

  const cfg = env();
  if (!cfg.previewSecret) {
    return NextResponse.json({ error: 'Test a question is switched off (no preview secret configured).' }, { status: 503 });
  }

  const raw = await req.text();
  if (raw.length > 32_000) return NextResponse.json({ error: 'request too large' }, { status: 413 });
  let body: { messages?: unknown; contact_type?: unknown };
  try {
    body = JSON.parse(raw);
  } catch {
    return NextResponse.json({ error: 'invalid request' }, { status: 400 });
  }
  const messages = Array.isArray(body.messages)
    ? body.messages.filter((m): m is string => typeof m === 'string').map((m) => m.trim()).filter(Boolean)
    : [];
  const audience = typeof body.contact_type === 'string' && AUDIENCES.has(body.contact_type) ? body.contact_type : 'unknown';
  if (!messages.length || messages.length > MAX_TURNS || messages.some((m) => m.length > MAX_CHARS)) {
    return NextResponse.json({ error: `Enter 1 to ${MAX_TURNS} messages of up to ${MAX_CHARS} characters.` }, { status: 400 });
  }

  let res: Response;
  try {
    res = await fetch(`${cfg.previewUrl}/admin/preview`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'x-admin-preview-key': cfg.previewSecret },
      body: JSON.stringify({ messages, contact_type: audience }),
      cache: 'no-store',
      signal: AbortSignal.timeout(180_000),
    });
  } catch {
    console.info(`[preview] user=${auth.viewer.userId.slice(0, 8)} bot unreachable`);
    return NextResponse.json({ error: 'The bot preview service is not reachable.' }, { status: 502 });
  }
  console.info(`[preview] user=${auth.viewer.userId.slice(0, 8)} turns=${messages.length} status=${res.status}`);

  const known: Record<number, string> = {
    401: 'The bot rejected the preview key (ADMIN_PREVIEW_SECRET differs between kb-admin and the bot).',
    404: 'The bot has no preview route (ADMIN_PREVIEW_SECRET is not set on the bot).',
    429: 'The bot is rate-limiting previews, or one is already running. Try again in a minute.',
  };
  if (!res.ok) {
    return NextResponse.json({ error: known[res.status] ?? `The bot answered ${res.status}.` }, { status: 502 });
  }
  const data = (await res.json()) as Record<string, unknown>;
  // Pass through only the fields the page shows.
  return NextResponse.json({
    turns: data.turns ?? [],
    refused_writes: data.refused_writes ?? [],
    rules_source: data.rules_source ?? null,
  });
}
