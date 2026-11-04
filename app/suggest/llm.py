"""The suggestion endpoint's own LLM client.

Not the bot's app/graph/llm.py: that one always uses the bot's model and returns
no token usage. This one uses the same OpenRouter base URL and key (read from the
bot's settings, read-only), the model in SUGGEST_MODEL, JSON mode, temperature 0.3,
and returns the usage. Only the parser is shared. The client retries nothing
itself; a reply that cannot be read is asked for once more (RETRY_UNPARSABLE).

Never logs the model's output: it is client conversation content.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from openai import AsyncOpenAI

from app.config import settings
from app.graph.llm import try_parse_json  # pure parser

logger = logging.getLogger(__name__)

TEMPERATURE = 0.3
# Five short WhatsApp drafts with their refs and the JSON around them:
# measured ~100 tokens a draft (eval 2026-10-28, at most 509 for five).
MAX_TOKENS = 800

# A reply the parser still cannot read gets one more call, inside the route's own
# time limit. A call that FAILED is not retried: that is usually the limit itself.
# False: an unreadable reply is an llm_error at once.
RETRY_UNPARSABLE = True


class SuggestLLMError(RuntimeError):
    """The model call failed or returned something that is not JSON."""


@lru_cache(maxsize=1)
def _client() -> AsyncOpenAI:
    if not settings.openrouter_api_key:
        raise SuggestLLMError("OPENROUTER_API_KEY is not set")
    return AsyncOpenAI(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        max_retries=0,
        default_headers={
            "HTTP-Referer": settings.openrouter_site_url,
            "X-Title": settings.openrouter_app_name,
        },
    )


async def _call(
    system_prompt: str, user_prompt: str, *, model: str, timeout: float
) -> tuple[str, dict[str, int]]:
    """One model call: (the reply's text, its token usage)."""
    try:
        response = await _client().chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS + settings.resolved_reasoning_headroom,
            response_format={"type": "json_object"},
            extra_body=settings.reasoning_body or None,
            timeout=timeout,
        )
    except SuggestLLMError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SuggestLLMError(f"model call failed: {type(exc).__name__}") from exc

    usage = getattr(response, "usage", None)
    tokens = {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }
    content = ""
    if response.choices:
        content = response.choices[0].message.content or ""
    return content, tokens


async def draft(system_prompt: str, user_prompt: str, *, model: str, timeout: float) -> tuple[dict[str, Any], dict[str, int]]:
    """(parsed JSON, {"prompt_tokens", "completion_tokens"}). Raises SuggestLLMError."""
    content, tokens = await _call(system_prompt, user_prompt, model=model, timeout=timeout)
    parsed = try_parse_json(content)
    if parsed is None and RETRY_UNPARSABLE:
        logger.warning("suggest: model output was not JSON (%d chars), asking once more", len(content))
        content, again = await _call(system_prompt, user_prompt, model=model, timeout=timeout)
        tokens = {key: tokens[key] + again[key] for key in tokens}
        parsed = try_parse_json(content)
    if parsed is None:
        logger.warning("suggest: model output was not JSON (%d chars)", len(content))
        raise SuggestLLMError("model output was not JSON")
    return parsed, tokens


def dumps(value: Any) -> str:  # for scripts
    return json.dumps(value, ensure_ascii=False, indent=2)
