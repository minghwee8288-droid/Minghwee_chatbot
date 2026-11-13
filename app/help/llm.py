"""Help's own LLM client (the pattern of app/suggest/llm.py, copied, not imported).

Same OpenRouter key and base URL as the bot, read from its settings (read only).
The model is HELP_MODEL. Reasoning is always off, the client retries nothing
itself (max_retries=0), and every call carries its own timeout.

Never logs the model's input or output: both carry staff conversation text.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from openai import AsyncOpenAI

from app.config import settings
from app.graph.llm import try_parse_json  # pure parser, no I/O

logger = logging.getLogger(__name__)

REASONING_OFF = {"reasoning": {"enabled": False}}


class HelpLLMError(RuntimeError):
    """The model call failed, or its output could not be used."""


@lru_cache(maxsize=1)
def _client() -> AsyncOpenAI:
    if not settings.openrouter_api_key:
        raise HelpLLMError("OPENROUTER_API_KEY is not set")
    return AsyncOpenAI(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        max_retries=0,
        default_headers={
            "HTTP-Referer": settings.openrouter_site_url,
            "X-Title": settings.openrouter_app_name,
        },
    )


async def complete(
    system_prompt: str,
    user_prompt: str,
    *,
    model: str,
    timeout: float,
    temperature: float,
    max_tokens: int,
    json_mode: bool = False,
) -> str:
    """One model call; the reply's text. Raises HelpLLMError."""
    kwargs: dict[str, Any] = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        response = await _client().chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body=REASONING_OFF,
            timeout=timeout,
            **kwargs,
        )
    except HelpLLMError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HelpLLMError(f"model call failed: {type(exc).__name__}") from exc
    if not response.choices:
        return ""
    return response.choices[0].message.content or ""


async def complete_json(
    system_prompt: str, user_prompt: str, *, model: str, timeout: float
) -> dict[str, Any]:
    """A JSON-mode call, parsed. Raises HelpLLMError if it cannot be read."""
    content = await complete(
        system_prompt, user_prompt, model=model, timeout=timeout,
        temperature=0.0, max_tokens=300, json_mode=True,
    )
    parsed = try_parse_json(content)
    if parsed is None:
        raise HelpLLMError("model output was not JSON")
    return parsed
