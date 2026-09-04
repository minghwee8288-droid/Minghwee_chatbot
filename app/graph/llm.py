"""LLM access. Every call goes through OpenRouter's OpenAI-compatible API."""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


@lru_cache(maxsize=16)
def get_llm(
    temperature: float = 0.4, max_tokens: int = 400, json_mode: bool = False
) -> ChatOpenAI:
    """Chat model via OpenRouter.

    ``json_mode`` constrains the response to a JSON object. Without it DeepSeek
    occasionally ignores the "return JSON" instruction and echoes the prompt back
    in a loop, which costs a classification and drops the turn to intent=other.
    """
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required for LLM calls")
    # Do not add provider routing constraints here. `require_parameters: true`
    # was tried to stop OpenRouter routing to providers that ignore
    # response_format, and no endpoint satisfied it — every call 404'd.
    kwargs: dict = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    # Reasoning models think on the clock and on the bill; this turns it down.
    # `reasoning` is an OpenRouter field the OpenAI SDK does not know, so it
    # travels in extra_body — passing it as a plain kwarg raises TypeError.
    reasoning = settings.reasoning_body or None

    return ChatOpenAI(
        model=settings.openrouter_model,
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=45,
        max_retries=2,
        # Mild penalties damp the repetition loops seen on long instruction-heavy
        # prompts. Kept low: high values make a small model incoherent, which is
        # worse than repetitive.
        frequency_penalty=0.0 if json_mode else 0.2,
        presence_penalty=0.0,
        model_kwargs=kwargs,
        extra_body=reasoning,
        default_headers={
            "HTTP-Referer": settings.openrouter_site_url,
            "X-Title": settings.openrouter_app_name,
        },
    )


async def complete(
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.4,
    max_tokens: int = 400,
    json_mode: bool = False,
) -> str:
    messages: list[BaseMessage] = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]
    # Callers ask for the length of the reply they want; the model's thinking
    # is charged to the same budget, so the headroom is added here rather than
    # every call site having to know the model is a reasoning one.
    budget = max_tokens + settings.resolved_reasoning_headroom
    response = await get_llm(temperature, budget, json_mode).ainvoke(messages)
    content = response.content
    if isinstance(content, list):  # some providers return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return (content or "").strip()


async def complete_json(
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.0,
    max_tokens: int = 800,
    default: dict | None = None,
) -> dict:
    """Ask for JSON and parse it defensively — models add prose and fences."""
    try:
        raw = await complete(
            system_prompt,
            user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
        )
        parsed = try_parse_json(raw)
        if parsed is not None:
            # An EMPTY dict is a real answer, not a failure: the extractor
            # returns {} whenever the client's message fills no field, which is
            # most of a conversation. `if parsed:` treated that as unparseable
            # and re-ran the whole call — a duplicate LLM round trip on nearly
            # every turn, logged as a warning, for a result that came back
            # identically empty. Live 2026-09-04: the warning fired on 5 of the
            # first 6 turns of a passport renewal.
            return parsed
        logger.warning("JSON-mode response was unparseable, retrying without it")
    except Exception as exc:  # noqa: BLE001
        # Strict JSON mode raises rather than truncating when the model runs to
        # the token limit, so a rambling response loses the whole call. Plain
        # mode returns the partial text, which parse_json can often salvage.
        logger.warning("JSON-mode call failed (%s), retrying without it", type(exc).__name__)
    try:
        raw = await complete(
            system_prompt, user_prompt, temperature=temperature, max_tokens=max_tokens
        )
    except Exception:  # noqa: BLE001
        logger.exception("LLM JSON retry failed")
        return dict(default or {})
    return parse_json(raw, default=default)


def parse_json(raw: str, *, default: dict | None = None) -> dict:
    """Parse, falling back to ``default`` when nothing usable comes back."""
    parsed = try_parse_json(raw)
    if parsed is not None:
        return parsed
    logger.warning("Could not parse LLM JSON output: %s", (raw or "")[:300])
    return dict(default or {})


def try_parse_json(raw: str) -> dict | None:
    """The parse itself. ``None`` means it failed; ``{}`` means it said nothing.

    Keeping those two apart is the whole point — see complete_json.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = _JSON_BLOCK.search(text)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return None
