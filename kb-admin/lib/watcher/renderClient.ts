import type { RenderLike } from './fetcher.ts';

/**
 * The client for the page renderer (render-service/ in this repo, run on our
 * own server, 2026-10-15). Vercel cannot run a browser reliably, so a page
 * built by JavaScript (the Indonesian embassy's site) is rendered there.
 * PURE apart from the network call, which is passed in.
 *
 * kb-admin still decides WHETHER to render: only after the plain fetch gave no
 * readable text, and only through PoliteFetcher.renderPage, which checks the
 * address, robots.txt and the per-host gap first. The renderer uses the same
 * user-agent, one page at a time, its own per-host gap, and refuses private
 * addresses - see render-service/README.md.
 */

export const RENDER_TIMEOUT_MS = 90_000;

type PostLike = (url: string, init: { method: 'POST'; headers: Record<string, string>; body: string; signal: AbortSignal }) => Promise<{
  status: number;
  json(): Promise<unknown>;
}>;

export function makeRenderer(cfg: { url: string; secret: string }, post: PostLike): RenderLike {
  return async (pageUrl) => {
    const res = await post(cfg.url, {
      method: 'POST',
      headers: { authorization: `Bearer ${cfg.secret}`, 'content-type': 'application/json' },
      body: JSON.stringify({ url: pageUrl }),
      signal: AbortSignal.timeout(RENDER_TIMEOUT_MS),
    });
    let body: Record<string, unknown> = {};
    try {
      body = (await res.json()) as Record<string, unknown>;
    } catch {
      body = {};
    }
    if (res.status === 401 || res.status === 404) return { ok: false, status: null, error: 'refused our key (check KB_RENDER_SECRET)' };
    if (body.ok === true && typeof body.html === 'string' && typeof body.finalUrl === 'string') {
      return { ok: true, status: typeof body.status === 'number' ? body.status : 200, html: body.html, finalUrl: body.finalUrl };
    }
    const error = typeof body.error === 'string' ? body.error.slice(0, 300) : `HTTP ${res.status}`;
    return { ok: false, status: typeof body.status === 'number' ? body.status : null, error };
  };
}
