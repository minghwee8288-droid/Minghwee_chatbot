/**
 * POST /api/reset -- the destructive one.
 *
 * The request carries ONE thing: a phone number. The conversation and every
 * lead are resolved from it server-side, so no request can name a row
 * belonging to somebody else, and `lib/reset.ts` contains no unscoped delete.
 */

import { NextResponse } from 'next/server';
import { checkPassword } from '@/lib/auth';
import { envReport } from '@/lib/env';
import { isUsablePhone } from '@/lib/phone';
import { reset } from '@/lib/reset';
import type { ResetRequest } from '@/lib/types';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
// A reset touches four tables plus the checkpoint over a separate Postgres
// connection. Ten seconds is not always enough on a long transcript.
export const maxDuration = 60;

export async function POST(request: Request) {
  const auth = checkPassword(request);
  if (!auth.ok) {
    return NextResponse.json({ error: auth.error }, { status: auth.status });
  }

  const report = envReport();
  if (!report.ok) {
    return NextResponse.json(
      {
        error:
          'The server is missing required configuration: ' +
          report.missing.join(', ') +
          '. Set these in the Vercel project settings and redeploy.',
      },
      { status: 503 },
    );
  }

  let body: any;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: 'Malformed request.' }, { status: 400 });
  }

  const phone = String(body?.phone ?? '').trim();

  if (!isUsablePhone(phone)) {
    return NextResponse.json(
      { error: 'Enter a full phone number, including the country code.' },
      { status: 400 },
    );
  }

  const payload: ResetRequest = {
    phone,
    keepHistory: Boolean(body?.keepHistory),
  };

  try {
    const result = await reset(payload);
    return NextResponse.json(result, { status: result.ok ? 200 : 409 });
  } catch (error) {
    return NextResponse.json(
      {
        ok: false,
        error:
          'The reset failed: ' + (error instanceof Error ? error.message : String(error)),
      },
      { status: 500 },
    );
  }
}
