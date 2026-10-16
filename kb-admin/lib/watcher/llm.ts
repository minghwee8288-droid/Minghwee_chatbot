import 'server-only';
import { env } from '../env';

/**
 * The drafting call: OpenRouter chat completions with the bot's current model
 * (KB_ADMIN_DRAFT_MODEL, default openai/gpt-5.6-luna), temperature 0, JSON
 * out. One immediate retry on a network error or a 5xx, never on a 4xx.
 * Only the page passages and the candidate entries are sent - no client data
 * exists on this path. Used only when the watcher is switched on.
 */

const TIMEOUT_MS = 90_000;

export class DraftingError extends Error {}

export async function draftWithModel(system: string, user: string): Promise<string> {
  const cfg = env();
  if (!cfg.watcher) throw new DraftingError('the watcher is switched off');
  const { baseUrl, apiKey, model } = cfg.watcher.llm;
  let lastError = '';
  for (let attempt = 0; attempt < 2; attempt++) {
    let res: Response;
    try {
      res = await fetch(`${baseUrl}/chat/completions`, {
        method: 'POST',
        headers: { authorization: `Bearer ${apiKey}`, 'content-type': 'application/json', 'x-title': 'Ming Hwee KB watcher' },
        body: JSON.stringify({
          model,
          temperature: 0,
          // Thinking off, as the bot runs the same model (app/config.py
          // reasoning_body): left on, hidden reasoning could spend the budget
          // and cut the JSON short - seen in the 2026-10-15 dry run on MOM's
          // paying-levy page, which then drafted nothing.
          max_tokens: 6000,
          reasoning: { enabled: false },
          response_format: { type: 'json_object' },
          messages: [
            { role: 'system', content: system },
            { role: 'user', content: user },
          ],
        }),
        cache: 'no-store',
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
    } catch {
      lastError = 'the drafting model could not be reached';
      continue;
    }
    if (res.status >= 500) {
      lastError = `the drafting model answered ${res.status}`;
      continue;
    }
    if (!res.ok) throw new DraftingError(`the drafting model answered ${res.status}`);
    const data = (await res.json().catch(() => null)) as { choices?: { message?: { content?: unknown }; finish_reason?: string }[] } | null;
    const content = data?.choices?.[0]?.message?.content;
    if (typeof content !== 'string') throw new DraftingError('the drafting model returned no text');
    if (data?.choices?.[0]?.finish_reason === 'length') throw new DraftingError('the drafting model\'s answer was cut off before it finished');
    return content;
  }
  throw new DraftingError(lastError || 'the drafting model failed');
}
