"""Conversation lookup, creation and state transitions.

Owns the ``wp_chat_conversations`` row, which is shared with the WhatsApp
portal — the portal reads the same row while an agent works the thread.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.db.supabase import db
from app.utils import digits_only, phone_variants

logger = logging.getLogger(__name__)

TABLE = "wp_chat_conversations"

# bot_status values
BOT_NONE = "none"
BOT_ACTIVE = "bot_active"
HUMAN_ACTIVE = "human_active"

# conversation status values that mean "this thread was finished"
CLOSED_STATUSES = {"closed", "resolved"}


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def new_thread_id(conversation_id: int | None = None) -> str:
    suffix = uuid.uuid4().hex[:12]
    return f"conv-{conversation_id or 'new'}-{suffix}"[:100]


def storage_key(phone: str) -> str:
    """The value to write into ``wp_chat_conversations.customer_number``.

    The WhatsApp portal owns this table and stores bare digits ('917970027379').
    Writing E.164 ('+917970027379') creates a SECOND row for the same client —
    customer_number is unique, so the portal's row is never matched and the
    thread splits: the client's messages on one row, the bot's on another.
    """
    return digits_only(phone)


async def get_by_phone(phone: str) -> dict[str, Any] | None:
    """Find the conversation whatever format the number is stored in."""
    variants = phone_variants(phone)
    if not variants:
        return None
    result = await db.execute(db.table(TABLE).select("*").in_("customer_number", variants).limit(5))
    rows = result.data or []
    if not rows:
        return None
    if len(rows) > 1:
        logger.warning(
            "Multiple conversations for %s: %s — using the portal-format row",
            phone,
            [r["id"] for r in rows],
        )
    key = storage_key(phone)
    # Prefer the portal's own row over any duplicate we may have created.
    return next((r for r in rows if r.get("customer_number") == key), rows[0])


async def get_by_id(conversation_id: int) -> dict[str, Any] | None:
    return await db.select_one(TABLE, id=conversation_id)


# The portal enforces CHECK constraints on these columns. Writing a value
# outside the list aborts the whole UPDATE, so values are filtered rather than
# risking an exception in the middle of a conversation.
#
# Note service_type deliberately excludes dispute_salary/dispute_assault: the
# portal does not model disputes as a service. cb_tickets.service_type does
# allow them, which is where a dispute is recorded.
ALLOWED_VALUES: dict[str, set[str]] = {
    "bot_status": {BOT_NONE, BOT_ACTIVE, HUMAN_ACTIVE},
    "status": {"open", "resolved"},
    "last_direction": {"inbound", "outbound"},
    "contact_type": {"employer", "candidate", "supplier", "partner", "unknown"},
    "assignment_rule": {"round_robin", "existing_salesperson", "admin_escalation"},
    "service_type": {
        "new_hiring",
        "direct_hiring",
        "replacement",
        "transfer",
        "renewal",
        "home_leave",
        "passport_renewal",
        "fee_enquiry",
        "salary_enquiry",
    },
}


# Flows the chatbot runs that the portal's service_type column predates. Filed
# under the nearest permitted value rather than dropped: a thread left with a
# null service_type reads in the portal as an enquiry about nothing, which is
# what an agent picking up a job seeker saw. The real flow is on the ticket, in
# captured_info.enquiry_type.
SERVICE_TYPE_SUBSTITUTES = {
    "candidate_new_hiring": "new_hiring",
    "candidate_registration": "new_hiring",
    "media_received": None,  # not a service — leave the column as it was
}


def _sanitize(fields: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        allowed = ALLOWED_VALUES.get(key)
        if allowed is not None and value is not None and value not in allowed:
            if key == "service_type" and value in SERVICE_TYPE_SUBSTITUTES:
                substitute = SERVICE_TYPE_SUBSTITUTES[value]
                if substitute is None:
                    continue
                logger.info(
                    "Recording service_type=%r as %r — the portal's column has no value for it",
                    value,
                    substitute,
                )
                clean[key] = substitute
                continue
            logger.info("Dropping %s=%r — not permitted by the portal's constraint", key, value)
            continue
        clean[key] = value
    return clean


async def update(conversation_id: int, **fields: Any) -> dict[str, Any] | None:
    fields = _sanitize(fields)
    if not fields:
        return None
    fields["updated_at"] = _now()
    rows = await db.update(TABLE, fields, id=conversation_id)
    return rows[0] if rows else None


async def get_or_create(
    phone: str, customer_name: str = "", *, engage: bool = True
) -> tuple[dict[str, Any], bool]:
    """Return (conversation, is_new).

    A resolved/closed thread that receives a new message is reopened with a
    fresh LangGraph thread so stale collected info does not leak into the new
    enquiry.

    ``engage=False`` means the safety gate has decided this conversation is not
    the bot's to answer: the row is created or read but ``bot_status`` is left
    alone, so the portal behaves exactly as it did before the bot existed.
    """
    existing = await get_by_phone(phone)
    if existing:
        patch: dict[str, Any] = {}
        if customer_name and not existing.get("customer_name"):
            patch["customer_name"] = customer_name

        bot_status = existing.get("bot_status") or BOT_NONE
        status = (existing.get("status") or "open").lower()
        reopened = status in CLOSED_STATUSES

        if engage and bot_status != HUMAN_ACTIVE and (bot_status == BOT_NONE or reopened):
            patch.update(
                {
                    "bot_status": BOT_ACTIVE,
                    "status": "open",
                    "langgraph_thread_id": new_thread_id(existing["id"]),
                }
            )
            if reopened:
                # New enquiry on an old thread — drop the previous routing.
                patch.update({"intent": None, "service_type": None, "assignment_rule": None})

        if not existing.get("langgraph_thread_id"):
            patch["langgraph_thread_id"] = new_thread_id(existing["id"])

        if patch:
            updated = await update(existing["id"], **patch)
            if updated:
                existing = updated
        return existing, False

    payload = {
        # Portal format, not E.164 — see storage_key().
        "customer_number": storage_key(phone),
        "customer_name": customer_name or phone,
        "status": "open",
        "bot_status": BOT_ACTIVE if engage else BOT_NONE,
        "unread_count": 0,
        "langgraph_thread_id": new_thread_id(),
    }
    created = await db.insert(TABLE, _sanitize(payload))
    if created is None:
        # Lost a race against a concurrent webhook — re-read the winner's row.
        created = await get_by_phone(phone)
        if created is None:
            raise RuntimeError(f"Could not create or read conversation for {phone}")
        return created, False

    thread_id = new_thread_id(created["id"])
    updated = await update(created["id"], langgraph_thread_id=thread_id)
    logger.info("Created conversation %s for %s", created["id"], phone)
    return (updated or created), True


async def touch_inbound(conversation_id: int, body: str) -> None:
    """Mirror the portal's own bookkeeping for an incoming message.

    Skipped when the portal is connected to the same Whapi channel — it writes
    these columns itself, and incrementing unread_count from both sides would
    inflate every agent's badge.
    """
    if settings.portal_manages_conversation_meta:
        await update(conversation_id, status="open")
        return

    conversation = await get_by_id(conversation_id)
    unread = int((conversation or {}).get("unread_count") or 0) + 1
    await update(
        conversation_id,
        last_message_body=body[:1000],
        last_message_at=_now(),
        last_direction="inbound",
        unread_count=unread,
        status="open",
    )


async def touch_outbound(conversation_id: int, body: str, *, is_bot: bool) -> None:
    fields: dict[str, Any] = {}
    if is_bot:
        # Chatbot-only column: the portal never sets this.
        fields["last_bot_reply_at"] = _now()
    if not settings.portal_manages_conversation_meta:
        fields.update(
            {
                "last_message_body": body[:1000],
                "last_message_at": _now(),
                "last_direction": "outbound",
                "unread_count": 0,
            }
        )
    if fields:
        await update(conversation_id, **fields)


async def set_bot_status(conversation_id: int, bot_status: str) -> None:
    await update(conversation_id, bot_status=bot_status)


def bot_should_reply(conversation: dict[str, Any]) -> bool:
    """The single gate that keeps the bot quiet while a human is on the thread."""
    return (conversation.get("bot_status") or BOT_NONE) == BOT_ACTIVE
