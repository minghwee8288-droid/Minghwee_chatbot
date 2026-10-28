"""Node — ask what a vague action request is about, then raise the ticket.

See app/graph/clarify.py for why (conversation 4443, 2026-10-28). Two turns:

  1. "please send the video here" -> one short message with the one or two
     questions an agent would ask first. Nothing is ticketed yet.
  2. Their answer -> the ticket, with the request, our question and their
     answer on it. If the answer quotes an open ticket's reference, or the
     request is a status question with exactly one open ticket, that ticket
     gets the summary instead and keeps its reference - never a duplicate.
"""

from __future__ import annotations

import logging
from typing import Any

from app.graph import clarify
from app.graph.guards import claire_has_spoken
from app.graph.reference import add_reference
from app.graph.state import ConversationState, effective_contact_type
from app.services import ticket as ticket_service

logger = logging.getLogger(__name__)

# The topic a clarified request is filed under: a request about their case,
# which is a modifier topic, so with one open ticket it lands on that ticket
# (pick_ticket_to_update) rather than raising a second one.
ACTION_TOPIC = "case_enquiry"

_INTRO = "Hi, I'm Claire, Ming Hwee's AI assistant. "


async def action_clarifier(state: ConversationState) -> dict[str, Any]:
    message = (state.get("incoming_text") or "").strip()
    history = state.get("history_text") or ""

    if state.get("action_answer"):
        pending = state.get("pending_action") or {}
        summary = clarify.summary(pending, message)
        open_tickets = state.get("blocked_topics") or {}
        existing = clarify.ticket_named(message, open_tickets) or (
            clarify.single_open_ticket(open_tickets) if pending.get("kind") == "update" else None
        )
        if existing:
            await ticket_service.add_follow_up(existing["id"], summary)
            logger.info(
                "Conversation %s: clarified %s request added to open ticket %s",
                state.get("conversation_id"), pending.get("kind"), existing.get("ticket_number"),
            )
            return {
                "pending_action": None,
                "needs_handover": False,
                "reply": add_reference(
                    clarify.ADDED_TO_TICKET_REPLY, existing.get("ticket_number"), history
                ),
            }
        logger.info(
            "Conversation %s: clarified %s request - raising a ticket with the answers on it",
            state.get("conversation_id"), pending.get("kind"),
        )
        return {
            "pending_action": None,
            "action_summary": summary,
            "needs_handover": True,
            "handover_reason": "client_requested",
            "reply": clarify.PASSED_ON_REPLY,
        }

    kind = state.get("clarify_action") or "document"
    question = clarify.question(kind, effective_contact_type(state))
    reply = question if claire_has_spoken(history) else _INTRO + question
    return {
        "reply": reply,
        "pending_action": clarify.pending(kind, message, question),
        "needs_handover": False,
    }
