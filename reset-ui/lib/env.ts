/**
 * Environment, read once and validated loudly.
 *
 * Mirrors the chatbot's own `app/config.py` philosophy: a misconfigured
 * safety gate FAILS CLOSED, and the resolved configuration is reported to
 * the operator rather than guessed at.
 *
 * Every value here is server-side only. None of these names begins with
 * NEXT_PUBLIC_, so none of them is ever bundled into the browser — the
 * service-role key in particular bypasses RLS entirely and must not leave
 * the serverless function.
 */

import { digitsOnly, normalizePhone } from './phone';

/**
 * The password the client is given, built in so the page works with no
 * configuration at all.
 *
 * It is in the repository on purpose, at the client's request. Two consequences
 * worth being clear about rather than discovering later:
 *
 *   - anyone who can read this repository knows the password;
 *   - changing it means editing this line and redeploying.
 *
 * Setting RESET_UI_PASSWORD in the Vercel project overrides it and keeps the
 * real password out of git, which is the better arrangement the day this page
 * is used on live client numbers. Nothing else has to change to switch over.
 */
export const DEFAULT_PASSWORD = 'Minghwee@123';

export type EnvReport = {
  ok: boolean;
  missing: string[];
  checkpointsReachable: boolean;
  gateEnabled: boolean;
  allowedNumbers: string[];
};

export const env = {
  supabaseUrl: process.env.SUPABASE_URL ?? '',
  serviceRoleKey: process.env.SUPABASE_SERVICE_ROLE_KEY ?? '',
  // Optional. Without it the LangGraph checkpoint tables cannot be cleared —
  // exactly as `scripts/reset_conversation.py` reports and carries on.
  dbUrl: process.env.SUPABASE_DB_URL ?? '',
  tenantId: process.env.TENANT_ID ?? '',
  // The environment wins when it is set, so the built-in default can be
  // replaced without a code change.
  password: process.env.RESET_UI_PASSWORD || DEFAULT_PASSWORD,
  allowedNumbersRaw: process.env.RESET_ALLOWED_NUMBERS ?? '',
};

/** Required for the app to do anything at all. */
export function missingRequired(): string[] {
  const required: [string, string][] = [
    ['SUPABASE_URL', env.supabaseUrl],
    ['SUPABASE_SERVICE_ROLE_KEY', env.serviceRoleKey],
    ['TENANT_ID', env.tenantId],
    // RESET_UI_PASSWORD is deliberately absent: it always has a value, either
    // from the environment or from DEFAULT_PASSWORD above.
  ];
  return required.filter(([, value]) => !value).map(([name]) => name);
}

/**
 * The safety gate, read from the RAW setting exactly as
 * `config.gate_enabled` does.
 *
 * Reading it from the parsed list instead would mean that a list of entirely
 * malformed numbers parsed to empty, and empty would read as "no gate" —
 * silently unlocking every client in the database. Fail closed.
 */
export function gateEnabled(): boolean {
  return Boolean(env.allowedNumbersRaw.trim());
}

/** app/config.py :: allowed_numbers */
export function allowedNumbers(): Set<string> {
  const numbers = new Set<string>();
  for (const part of env.allowedNumbersRaw.replace(/;/g, ',').split(',')) {
    const trimmed = part.trim();
    if (!trimmed) continue;
    if (digitsOnly(trimmed).length < 8) continue; // e.g. a '+65XXXXXXXX' placeholder
    numbers.add(normalizePhone(trimmed));
  }
  return numbers;
}

/**
 * Whether this number may be reset at all.
 *
 * With RESET_ALLOWED_NUMBERS unset the tool works on any number, which is
 * what the client asked for. Set it and the tool becomes test-numbers-only,
 * the same protection `BOT_ALLOWED_NUMBERS` gives the bot.
 */
export function mayReset(phone: string): boolean {
  if (!gateEnabled()) return true;
  return allowedNumbers().has(normalizePhone(phone));
}

export function envReport(): EnvReport {
  const missing = missingRequired();
  return {
    ok: missing.length === 0,
    missing,
    checkpointsReachable: Boolean(env.dbUrl),
    gateEnabled: gateEnabled(),
    allowedNumbers: [...allowedNumbers()],
  };
}
