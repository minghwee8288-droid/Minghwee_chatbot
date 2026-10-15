import 'server-only';
import { env } from '@/lib/env';
import { PoliteFetcher } from '@/lib/watcher/fetcher';
import { makeRenderer } from '@/lib/watcher/renderClient';

/**
 * The real network for the watcher: one PoliteFetcher per run (it keeps
 * robots.txt and the per-host gap for that run), with the page renderer
 * attached when KB_RENDER_URL and KB_RENDER_SECRET are set. Shared by
 * "Fetch now" (app/watch/actions.ts) and the schedule (app/api/watcher/run).
 */
export function watchFetcher(): { fetcher: PoliteFetcher; renders: boolean } {
  const render = env().watcher?.render ?? null;
  const fetcher = new PoliteFetcher({
    fetch: (u, init) => fetch(u, { ...init, cache: 'no-store' }),
    now: () => Date.now(),
    sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
    render: render ? makeRenderer(render, (u, init) => fetch(u, { ...init, cache: 'no-store' })) : undefined,
  });
  return { fetcher, renders: Boolean(render) };
}
