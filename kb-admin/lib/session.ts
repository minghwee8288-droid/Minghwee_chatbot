/**
 * Supabase Auth over plain fetch, plus the session cookies. No Node imports,
 * so the edge middleware can use it too.
 *
 * kb-admin keeps the Supabase access and refresh tokens in httpOnly cookies;
 * the browser never sees a token or a key. A session lasts at most
 * SESSION_HOURS from sign-in, however often it is refreshed.
 */

export const ACCESS_COOKIE = 'kba_at';
export const REFRESH_COOKIE = 'kba_rt';
export const DEADLINE_COOKIE = 'kba_until';
export const SESSION_HOURS = 12;

export type AuthUser = { id: string; email: string };
export type Tokens = { access_token: string; refresh_token: string; user: AuthUser };

export function cookieOptions(maxAgeSeconds: number) {
  return {
    httpOnly: true,
    sameSite: 'strict' as const,
    secure: process.env.NODE_ENV === 'production',
    path: '/',
    maxAge: maxAgeSeconds,
  };
}

type AuthConfig = { supabaseUrl: string; anonKey: string };

async function tokenRequest(cfg: AuthConfig, grant: string, body: object): Promise<Tokens | null> {
  const res = await fetch(`${cfg.supabaseUrl}/auth/v1/token?grant_type=${grant}`, {
    method: 'POST',
    headers: { apikey: cfg.anonKey, 'content-type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
  });
  if (!res.ok) return null;
  const data = (await res.json()) as {
    access_token?: string;
    refresh_token?: string;
    user?: { id?: string; email?: string };
  };
  if (!data.access_token || !data.refresh_token || !data.user?.id) return null;
  return {
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    user: { id: data.user.id, email: data.user.email ?? '' },
  };
}

export function passwordSignIn(cfg: AuthConfig, email: string, password: string) {
  return tokenRequest(cfg, 'password', { email, password });
}

export function refreshSession(cfg: AuthConfig, refreshToken: string) {
  return tokenRequest(cfg, 'refresh_token', { refresh_token: refreshToken });
}

/** Verifies the token WITH Supabase Auth (signature, expiry, revocation). */
export async function userFromToken(cfg: AuthConfig, accessToken: string): Promise<AuthUser | null> {
  const res = await fetch(`${cfg.supabaseUrl}/auth/v1/user`, {
    headers: { apikey: cfg.anonKey, authorization: `Bearer ${accessToken}` },
    cache: 'no-store',
  });
  if (!res.ok) return null;
  const data = (await res.json()) as { id?: string; email?: string };
  return data.id ? { id: data.id, email: data.email ?? '' } : null;
}

/** Revokes this session's refresh token at Supabase Auth. Best effort. */
export async function signOut(cfg: AuthConfig, accessToken: string): Promise<void> {
  try {
    await fetch(`${cfg.supabaseUrl}/auth/v1/logout?scope=local`, {
      method: 'POST',
      headers: { apikey: cfg.anonKey, authorization: `Bearer ${accessToken}` },
      cache: 'no-store',
    });
  } catch {
    // The cookies are cleared either way.
  }
}
