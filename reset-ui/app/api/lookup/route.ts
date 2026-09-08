/**
 * POST /api/lookup -- read-only. Never deletes anything.
 *
 * Runs on the Node runtime, not the edge: the Postgres driver the reset path
 * uses needs raw sockets, and both routes should behave the same way.
 */

import { NextResponse } from 'next/server';
import { checkPassword } from '@/lib/auth';
import { envReport } from '@/lib/env';
import { lookup } from '@/lib/lookup';
import { isUsablePhone } from '@/lib/phone';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

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

  let phone = '';
  try {
    const body = await request.json();
    phone = String(body?.phone ?? '').trim();
  } catch {
    return NextResponse.json({ error: 'Malformed request.' }, { status: 400 });
  }

  if (!isUsablePhone(phone)) {
    return NextResponse.json(
      { error: 'Enter a full phone number, including the country code.' },
      { status: 400 },
    );
  }

  try {
    return NextResponse.json(await lookup(phone));
  } catch (error) {
    return NextResponse.json(
      {
        error:
          'The lookup failed: ' + (error instanceof Error ? error.message : String(error)),
      },
      { status: 500 },
    );
  }
}
