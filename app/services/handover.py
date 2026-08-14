"""Handover execution.

Handovers are silent: the bot never announces them. It sets
``bot_status = 'human_active'``, assigns an agent, logs the event, and stops
replying. From the client's side the same person simply keeps typing.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.db.supabase import db
from app.services import assignment as assignment_service
from app.services import conversation as conversation_service

logger = logging.getLogger(__name__)

TABLE = "cb_handovers"

BOT_TO_HUMAN = "bot_to_human"
HUMAN_TO_BOT = "human_to_bot"

# cb_handovers.reason values
REASON_TICKET = "ticket_raised"
REASON_DISPUTE = "dispute_escalation"
REASON_CONFUSED = "bot_confused"
REASON_CLIENT_REQUESTED = "client_requested"
REASON_AGENT_RESOLVED = "agent_resolved"
# Not in the brief's list, but needed: an agent simply replying is neither the
# client requesting a human nor the agent resolving anything, and labelling it
# 'client_requested' makes the handover log unreadable.
REASON_AGENT_TAKEOVER = "agent_takeover"
REASON_AGENT_IDLE = "agent_idle"
REASON_FEE = "fee_enquiry"
REASON_SALARY = "salary_enquiry"
# The client sent a photo or document. The bot cannot open it, so a human does.
REASON_MEDIA = "media_received"
# A helper or an agent offering one: intake needs documents and verification.
REASON_CANDIDATE = "candidate_registration"
# A short, auto-expiring standdown ended on its own — the agent went quiet for
# agent_pause_minutes without resolving anything or replying again.
REASON_AGENT_PAUSE_EXPIRED = "agent_pause_expired"
# A bot crash (REASON_CONFUSED) auto-retried after bot_failure_timeout_minutes
# with nobody having touched it — distinct from REASON_AGENT_IDLE, which is
# the long 72h safety net for a standdown of unknown cause.
REASON_BOT_FAILURE_TIMEOUT = "bot_failure_timeout"


async def _log(
    conversation_id: int,
    direction: str,
    reason: str,
    *,
    agent_profile_id: str | None = None,
    ticket_id: str | None = None,
) -> None:
    try:
        await db.insert(
            TABLE,
            {
                "tenant_id": settings.tenant_id or None,
                "conversation_id": conversation_id,
                "direction": direction,
                "reason": reason,
                "agent_profile_id": agent_profile_id,
                "ticket_id": ticket_id,
            },
        )
    except Exception:  # noqa: BLE001 - logging must never block the handover itself
        logger.exception("Could not log handover for conversation %s", conversation_id)


# The only two reasons a bot_to_human row ever actually accompanies a
# bot_status flip to human_active — see to_human() and agent_took_over().
# Every other bot_to_human reason (ticket_raised, fee_enquiry, salary_enquiry,
# dispute_escalation, media_received, candidate_registration) is written by
# log_escalation() or the emergency-override path, neither of which touches
# bot_status at all — see log_escalation's own docstring. Mixing those into
# "what caused the standdown" is exactly the bug that kept conversation 3766
# stuck: its ticket (reason=ticket_raised) was logged at 22:23, after the
# 22:15 crash (reason=bot_confused), so it became the "latest" bot_to_human
# row despite never having silenced anything — and the crash's own reason,
# the only one that matters here, was invisible to a query that did not know
# to ignore it.
STANDDOWN_REASONS = (REASON_CONFUSED, REASON_AGENT_TAKEOVER)


async def latest_bot_to_human_reason(conversation_id: int) -> str | None:
    """Which of the two status-changing standdowns is currently in force, if any.

    maybe_return_to_bot() used to decide how to recover purely from whether a
    real agent message exists ANYWHERE in the thread's history. That breaks a
    thread that has ever had a real agent on it: a later bot_confused failover
    (the bot crashing mid-turn, nothing to do with that old agent message)
    inherited the short agent_pause_minutes timer keyed off that stale
    message, and if no inbound message happened to arrive more than
    agent_pause_minutes after it, the thread never got a chance to resume.
    Looking at the most recent STANDDOWN_REASONS row instead of message
    history lets the caller tell "a human actually silenced this" apart from
    "the bot silenced itself" — restricted to those two reasons specifically,
    not just any bot_to_human row, because most of them (tickets, fee/salary
    enquiries, disputes) are topic-scoped escalations that never silenced
    anything and must not be mistaken for the cause of a standdown.
    """
    result = await db.execute(
        db.table(TABLE)
        .select("reason")
        .eq("conversation_id", conversation_id)
        .eq("direction", BOT_TO_HUMAN)
        .in_("reason", list(STANDDOWN_REASONS))
        .order("created_at", desc=True)
        .limit(1)
    )
    rows = result.data or []
    return rows[0].get("reason") if rows else None


async def to_human(
    conversation: dict[str, Any],
    *,
    reason: str,
    intent: str | None = None,
    ticket_id: str | None = None,
    salesperson_profile_id: str | None = None,
    assigned_agent_id: str | None = None,
    assignment_rule: str | None = None,
    contact_type: str | None = None,
) -> dict[str, Any]:
    """Hand the conversation to a human agent and silence the bot.

    ``assigned_agent_id``/``assignment_rule`` can be passed in when the caller
    already resolved the agent (e.g. the ticket creator), to avoid consuming a
    second round-robin slot.
    """
    conversation_id = conversation["id"]

    if not assigned_agent_id:
        assigned_agent_id, assignment_rule = await assignment_service.resolve_agent(
            intent=intent,
            matched_employer_id=conversation.get("matched_employer_id"),
            salesperson_profile_id=salesperson_profile_id,
            contact_type=contact_type or conversation.get("contact_type"),
            conversation_id=conversation_id,
        )

    portal_user_id = await assignment_service.map_to_portal_user(assigned_agent_id)

    patch: dict[str, Any] = {
        "bot_status": conversation_service.HUMAN_ACTIVE,
        "assignment_rule": assignment_rule,
        "status": "open",
    }
    if portal_user_id is not None:
        patch["assigned_user_id"] = portal_user_id
    updated = await conversation_service.update(conversation_id, **patch) or conversation

    await _log(
        conversation_id,
        BOT_TO_HUMAN,
        reason,
        agent_profile_id=assigned_agent_id,
        ticket_id=ticket_id,
    )
    logger.info(
        "Handover bot->human on conversation %s (reason=%s, rule=%s, agent=%s)",
        conversation_id,
        reason,
        assignment_rule,
        assigned_agent_id,
    )
    return {
        "conversation": updated,
        "assigned_agent_id": assigned_agent_id,
        "assignment_rule": assignment_rule,
        "assigned_user_id": portal_user_id,
    }


async def log_escalation(
    conversation: dict[str, Any],
    *,
    reason: str,
    intent: str | None = None,
    ticket_id: str | None = None,
    salesperson_profile_id: str | None = None,
    assigned_agent_id: str | None = None,
    assignment_rule: str | None = None,
    contact_type: str | None = None,
) -> dict[str, Any]:
    """Record that a topic was escalated to a human, without silencing the bot.

    This is what a ticket-raising trigger calls now instead of to_human():
    the ticket this reason is attached to is what a human must act on, not the
    whole conversation, so bot_status is left exactly as it was — the client
    keeps talking to the bot about everything else. The only things that stand
    the bot down conversation-wide are a human actually replying
    (agent_took_over, a short auto-expiring pause) and the bot failing
    outright (to_human, via the webhook's failover path).
    """
    conversation_id = conversation["id"]

    if not assigned_agent_id:
        assigned_agent_id, assignment_rule = await assignment_service.resolve_agent(
            intent=intent,
            matched_employer_id=conversation.get("matched_employer_id"),
            salesperson_profile_id=salesperson_profile_id,
            contact_type=contact_type or conversation.get("contact_type"),
            conversation_id=conversation_id,
        )

    await _log(
        conversation_id,
        BOT_TO_HUMAN,
        reason,
        agent_profile_id=assigned_agent_id,
        ticket_id=ticket_id,
    )
    logger.info(
        "Topic escalated on conversation %s (reason=%s, rule=%s, agent=%s, ticket=%s) — "
        "bot stays active on everything else",
        conversation_id,
        reason,
        assignment_rule,
        assigned_agent_id,
        ticket_id,
    )
    return {"assigned_agent_id": assigned_agent_id, "assignment_rule": assignment_rule}


async def agent_took_over(conversation: dict[str, Any], reason: str = REASON_AGENT_TAKEOVER) -> None:
    """A human replied from the WhatsApp Business app — stand the bot down."""
    conversation_id = conversation["id"]
    if (conversation.get("bot_status") or "") == conversation_service.HUMAN_ACTIVE:
        return
    await conversation_service.set_bot_status(conversation_id, conversation_service.HUMAN_ACTIVE)
    await _log(conversation_id, BOT_TO_HUMAN, reason)
    logger.info("Agent detected on conversation %s — bot silenced", conversation_id)


async def back_to_bot(conversation: dict[str, Any], reason: str = REASON_AGENT_RESOLVED) -> None:
    """Return a conversation to the bot (used when an agent closes the thread,
    or when a standdown with no real agent message behind it — a bot failure —
    finally times out).

    Mints a fresh langgraph_thread_id: this path is for a handover with no
    reliable sense of what is still in progress, so a clean slate beats
    carrying stale collected_info into whatever the client raises next.
    """
    conversation_id = conversation["id"]
    await conversation_service.update(
        conversation_id,
        bot_status=conversation_service.BOT_ACTIVE,
        langgraph_thread_id=conversation_service.new_thread_id(conversation_id),
    )
    await _log(conversation_id, HUMAN_TO_BOT, reason)
    logger.info("Handover human->bot on conversation %s (reason=%s)", conversation_id, reason)


async def resume_from_pause(
    conversation: dict[str, Any], reason: str = REASON_AGENT_PAUSE_EXPIRED
) -> None:
    """Resume the bot once a short agent-message pause lapses on its own.

    Deliberately does NOT mint a fresh thread_id like back_to_bot() does: the
    agent may only have said a couple of words before going quiet, mid-way
    through whatever the bot was collecting, and there is no reason to throw
    that away just because a human was briefly on the thread too.
    """
    conversation_id = conversation["id"]
    await conversation_service.set_bot_status(conversation_id, conversation_service.BOT_ACTIVE)
    await _log(conversation_id, HUMAN_TO_BOT, reason)
    logger.info("Agent pause expired on conversation %s — bot resuming", conversation_id)
