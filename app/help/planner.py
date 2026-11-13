"""One small JSON call: turn the conversation into one standalone search query.

"and for Indonesia?" after "what is the transfer fee for a Filipino helper?"
becomes "what is the transfer fee for an Indonesian helper?", with the service
and nationality it names. The planner can never fail a request: any error,
timeout or unusable output falls back to the last staff message, unfiltered.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.help import llm as help_llm
from app.help.prompt import render_conversation
from app.help.schemas import Message

logger = logging.getLogger(__name__)

NATIONALITIES = ("PH", "ID", "MM")
MAX_QUERY_CHARS = 500

PLANNER_PROMPT = """You prepare a knowledge-base search for the staff assistant of a Singapore \
maid agency. Read the conversation and return ONE JSON object:
{{"standalone_query": "...", "service_type": "...", "nationality": "..."}}

- standalone_query: the staff member's LAST question rewritten so it can be understood \
on its own. Resolve follow-ups from earlier messages ("and for Indonesia?" after a \
question about a Filipino transfer fee becomes "What is the transfer fee for an \
Indonesian helper?"). Keep it a question, in English, under 40 words. Do not answer it.
- service_type: one of {services}, or null if the question is not about one of them \
or names none. A fee that belongs to a package is filed under the package: the \
replacement fee for a new hire is new_hiring, for a transfer helper it is transfer; \
use replacement only for the replacement service itself.
- nationality: "PH" (Philippines/Filipino), "ID" (Indonesia/Indonesian), "MM" \
(Myanmar), or null if no single nationality is meant.

Return only the JSON object."""


@dataclass(frozen=True)
class Plan:
    standalone_query: str
    service_type: str | None
    nationality: str | None
    planned: bool  # False = the fallback was used


def fallback(messages: tuple[Message, ...]) -> Plan:
    return Plan(messages[-1].content, None, None, False)


async def plan(
    messages: tuple[Message, ...], allowed_services: frozenset[str], *, model: str, timeout: float
) -> Plan:
    system = PLANNER_PROMPT.format(services=", ".join(sorted(allowed_services)) or "(none)")
    try:
        raw = await asyncio.wait_for(
            help_llm.complete_json(system, render_conversation(messages), model=model, timeout=timeout),
            timeout=timeout,
        )
    except (asyncio.TimeoutError, help_llm.HelpLLMError) as exc:
        logger.info("help planner: falling back to the last message (%s)", type(exc).__name__)
        return fallback(messages)
    except Exception:  # noqa: BLE001 - the planner never fails a request
        logger.exception("help planner: unexpected error, falling back")
        return fallback(messages)

    query = raw.get("standalone_query")
    if not isinstance(query, str) or not query.strip():
        query = messages[-1].content
    query = query.strip()[:MAX_QUERY_CHARS]

    service = raw.get("service_type")
    service = service.strip().lower() if isinstance(service, str) else None
    if service not in allowed_services:
        service = None

    nationality = raw.get("nationality")
    nationality = nationality.strip().upper() if isinstance(nationality, str) else None
    if nationality not in NATIONALITIES:
        nationality = None

    return Plan(query, service, nationality, True)
