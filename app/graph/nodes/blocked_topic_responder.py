"""Node — acknowledge a message about a topic that already has an open ticket.

The bot keeps working the rest of the conversation; this is the one topic it
must not answer, re-collect, or re-escalate until the ticket closes. The
client should never feel ignored on it, so every message here gets a short,
varied reassurance rather than silence — and anything new they say about it
is logged onto the ticket, so the agent opening it sees the whole picture,
not just what was known the moment it was raised.
"""

from __future__ import annotations

import logging
from typing import Any

from app.graph.guards import (
    clamp_reply,
    is_degenerate,
    looks_like_document,
    recent_bot_lines,
    strip_handover_talk,
    strip_meta_commentary,
    strip_repeated_opener,
)
from app.graph.llm import complete
from app.graph.prompts.system import build_system_prompt
from app.graph.prompts.templates import BLOCKED_TOPIC_INSTRUCTION
from app.graph.state import ConversationState, effective_contact_type
from app.services import ticket as ticket_service
from app.services.ticket import TICKET_SERVICE_TYPES, service_label

logger = logging.getLogger(__name__)

FALLBACK_REPLY = "Still checking on that for you — I'll have an update as soon as I can."


def _current_topic(state: ConversationState) -> tuple[str | None, dict[str, Any]]:
    key = ticket_service.topic_key_for(
        state.get("service_type"), effective_contact_type(state), state.get("intent")
    )
    ticket = (state.get("blocked_topics") or {}).get(key or "") or {}
    return key, ticket


async def blocked_topic_responder(state: ConversationState) -> dict[str, Any]:
    topic_key, ticket = _current_topic(state)
    message = (state.get("incoming_text") or "").strip()

    if ticket.get("id") and message:
        await ticket_service.add_follow_up(ticket["id"], message)
        # This exact topic already matched — always the same underlying issue
        # by definition, so no LLM check is needed here (find_merge_candidate
        # is only for the harder case where the surface topic differs).
        #
        # What to add to the array is NOT state["service_type"]: the "keep the
        # active service" rule in the classifier deliberately overwrites it
        # back to the ticket's own topic whenever a fee/salary question or a
        # follow-up interrupts an in-progress flow, so it never actually
        # carries what the client just asked about — "how much will this
        # cost" inside an open new_hiring ticket classifies as
        # intent=fee_enquiry but service_type stays 'new_hiring'. intent is
        # what still carries it, and SERVICE_INTENTS | ENQUIRY_INTENTS |
        # DISPUTE_INTENTS together are exactly TICKET_SERVICE_TYPES, so a
        # membership check is enough to tell "a trackable service was just
        # asked about" from "just a question, nothing to add" (e.g.
        # document_question, which is real and gets logged via
        # add_follow_up above, but is not one of the eleven the array can
        # hold).
        intent = state.get("intent")
        new_service = intent if intent in TICKET_SERVICE_TYPES else None
        await ticket_service.merge_into(ticket["id"], reason=message, service_type=new_service)

    label = ticket_service.service_types_label(ticket) if ticket.get("id") else service_label(topic_key)
    instruction = BLOCKED_TOPIC_INSTRUCTION.format(service_label=label)
    system_prompt = build_system_prompt(dict(state), extra_instructions=instruction)
    user_prompt = (
        f"Conversation so far:\n{state.get('history_text') or '(this is the first message)'}\n\n"
        f"Client's latest message(s):\n{state.get('incoming_text', '')}\n\n"
        "Your reply:"
    )

    try:
        reply = (await complete(system_prompt, user_prompt, temperature=0.5, max_tokens=150)).strip()
    except Exception:  # noqa: BLE001 - never leave the client without an answer
        logger.exception(
            "Blocked-topic reply generation failed on conversation %s", state.get("conversation_id")
        )
        reply = FALLBACK_REPLY

    reply = strip_meta_commentary(reply.strip('"'))
    if is_degenerate(reply) or looks_like_document(reply):
        reply = FALLBACK_REPLY
    else:
        reply = clamp_reply(strip_handover_talk(reply), max_sentences=2)
    if not reply:
        reply = FALLBACK_REPLY

    reply = strip_repeated_opener(reply, *recent_bot_lines(state.get("history_text", "")))

    logger.info(
        "Conversation %s: acknowledged a message on blocked topic %r (ticket %s)",
        state.get("conversation_id"),
        topic_key,
        ticket.get("ticket_number"),
    )
    return {"reply": reply, "needs_handover": False}
