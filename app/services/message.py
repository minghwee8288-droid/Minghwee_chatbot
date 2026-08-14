"""Message persistence and outbound sending.

Every message — inbound, bot reply, or a human agent's reply typed in the
WhatsApp Business app — lands in ``wp_chat_messages`` so the portal and the
bot share one transcript.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any

from app.config import settings
from app.db.supabase import db
from app.services import conversation as conversation_service
from app.utils import digits_only, normalize_text, redact_nric
from app.whapi.client import WhapiError, whapi
from app.whapi.parser import IncomingMessage

logger = logging.getLogger(__name__)

TABLE = "wp_chat_messages"

# Whapi echoes our own sends back as `from_me` webhooks. The echo can arrive
# before the insert below has committed, so ids are also held in memory for a
# few minutes — otherwise the bot would mistake its own message for an agent.
_SENT_BY_BOT: "OrderedDict[str, float]" = OrderedDict()
_SENT_TTL_SECONDS = 600
_SENT_MAX = 2000


def mark_sent_by_bot(message_id: str | None) -> None:
    if not message_id:
        return
    now = time.monotonic()
    _SENT_BY_BOT[message_id] = now
    while len(_SENT_BY_BOT) > _SENT_MAX:
        _SENT_BY_BOT.popitem(last=False)
    expired = [key for key, stamp in _SENT_BY_BOT.items() if now - stamp > _SENT_TTL_SECONDS]
    for key in expired:
        _SENT_BY_BOT.pop(key, None)


def recently_sent_by_bot(message_id: str | None) -> bool:
    if not message_id:
        return False
    stamp = _SENT_BY_BOT.get(message_id)
    return stamp is not None and (time.monotonic() - stamp) <= _SENT_TTL_SECONDS


async def exists(whapi_message_id: str) -> bool:
    row = await db.select_one(TABLE, "id", whapi_message_id=whapi_message_id)
    return row is not None


async def was_sent_by_bot(whapi_message_id: str) -> bool:
    """Did this outbound message come from us, or from a human agent's phone?"""
    if recently_sent_by_bot(whapi_message_id):
        return True
    row = await db.select_one(TABLE, "id, is_bot", whapi_message_id=whapi_message_id)
    return bool(row and row.get("is_bot"))


def _storable_body(message: IncomingMessage) -> str | None:
    """Body that satisfies the portal's wp_chat_messages_has_content constraint.

    A media message with auto-download off arrives with no text and no link, and
    protocol/system events carry nothing at all — inserting either as a null body
    violates the constraint.

    A transcribed voice note stores its transcript, so the portal and the bot's
    own history read it as what the client actually said. The row is still
    marked ``media_type = 'voice'`` with the audio attached, so nobody mistakes
    it for something they typed.
    """
    if message.transcript:
        return message.transcript
    if message.body:
        return message.body
    if message.media_caption:
        return message.media_caption
    if message.has_media:
        return f"[{message.message_type}]"
    return None


async def store_incoming(conversation_id: int, message: IncomingMessage) -> dict[str, Any] | None:
    """Persist an inbound client message. Idempotent on whapi_message_id."""
    body = _storable_body(message)
    if body is None and not message.media_url:
        logger.debug(
            "Message %s has no content to store (type=%s)",
            message.whapi_message_id,
            message.message_type,
        )
        return None

    payload = {
        "conversation_id": conversation_id,
        "direction": "inbound",
        # Portal format: bare digits, matching how the portal writes these rows.
        "from_number": digits_only(message.customer_number),
        "to_number": digits_only(settings.whapi_sender_phone) or None,
        "body": body,
        "whapi_message_id": message.whapi_message_id,
        "status": "received",
        "is_bot": False,
        "media_path": message.media_url,
        "media_type": message.message_type if message.has_media else None,
        "media_mime": message.media_mime,
        "media_filename": message.media_filename,
        "media_caption": message.media_caption,
    }
    return await _insert_ignoring_duplicates(payload)


def has_reply_content(message: IncomingMessage) -> bool:
    """Whether this outbound event is an actual message, not a reaction/protocol event.

    Whapi's ``messages`` array is not limited to text and media: reactions,
    delete notices and other protocol events arrive the same way, with
    ``from_me=True`` and nothing store_agent_reply can persist. Conversation
    3766 got silenced by exactly one of these — the handover row said "agent
    replied directly", but wp_chat_messages has no matching outbound row at
    all, so last_agent_message_at() could never find it and the standdown had
    no idle time to measure against, ever. The caller must check this BEFORE
    deciding the thread has a real agent on it — storage succeeding or failing
    is a separate question from whether there was anything to store.
    """
    return _storable_body(message) is not None or bool(message.media_url)


async def store_agent_reply(conversation_id: int, message: IncomingMessage) -> dict[str, Any] | None:
    """Persist an outbound message the bot did not send (a human agent typed it).

    Never raises: the caller uses this on the agent-detection path, and failing
    to store a row must not stop the bot from standing down.
    """
    if not has_reply_content(message):
        return None
    payload = {
        "conversation_id": conversation_id,
        "direction": "outbound",
        "from_number": digits_only(settings.whapi_sender_phone) or None,
        "to_number": digits_only(message.customer_number),
        "body": _storable_body(message),
        "whapi_message_id": message.whapi_message_id,
        "status": "sent",
        "sent_by": "agent",
        "is_bot": False,
        "media_path": message.media_url,
        "media_type": message.message_type if message.has_media else None,
        "media_mime": message.media_mime,
        "media_filename": message.media_filename,
        "media_caption": message.media_caption,
    }
    return await _insert_ignoring_duplicates(payload)


async def store_auto_reply(conversation_id: int, message: IncomingMessage) -> dict[str, Any] | None:
    """Persist a WhatsApp Business greeting/away message.

    Recorded with ``is_bot = true`` because it is machine-generated, not a human
    agent: that keeps it out of ``last_agent_message_at`` so it cannot trip the
    agent grace window either.
    """
    return await _insert_ignoring_duplicates(
        {
            "conversation_id": conversation_id,
            "direction": "outbound",
            "from_number": settings.whapi_sender_phone or None,
            "to_number": message.customer_number,
            "body": message.body or None,
            "whapi_message_id": message.whapi_message_id,
            "status": "sent",
            "sent_by": "whatsapp_auto",
            "is_bot": True,
        }
    )


# Verdicts for bodies already checked against the database.
_AUTO_REPLY_VERDICTS: dict[str, bool] = {}
# Templates are long; a genuine agent's "ok" or "noted" is not.
_AUTO_REPLY_MIN_CHARS = 60
# How many different clients must have received the identical text.
_AUTO_REPLY_MIN_CONVERSATIONS = 3


async def is_auto_reply(body: str | None) -> bool:
    """Whether an outbound message came from WhatsApp Business, not a person.

    The configured list catches known templates. Beyond that the test is
    structural: WhatsApp's greeting and away messages go out verbatim to every
    client, while a human writes something different each time. Anything long
    enough to be a template and seen word-for-word across several conversations
    is treated as automated.

    This matters because a missed auto-reply silences the bot on that thread —
    an away message outside office hours would otherwise take the bot down every
    evening, exactly when nobody is there to pick up.
    """
    normalised = normalize_text(body)
    if not normalised:
        return False
    if normalised in settings.auto_reply_texts:
        return True
    if len(normalised) < _AUTO_REPLY_MIN_CHARS:
        return False

    cached = _AUTO_REPLY_VERDICTS.get(normalised)
    if cached is not None:
        return cached

    try:
        result = await db.execute(
            db.table(TABLE)
            .select("conversation_id")
            .eq("direction", "outbound")
            .eq("body", body)
            .limit(50)
        )
        conversations = {row["conversation_id"] for row in (result.data or [])}
        verdict = len(conversations) >= _AUTO_REPLY_MIN_CONVERSATIONS
    except Exception:  # noqa: BLE001 - if in doubt, treat it as a human
        logger.exception("Auto-reply lookup failed")
        return False

    _AUTO_REPLY_VERDICTS[normalised] = verdict
    if verdict:
        logger.info(
            "Treating a message sent to %d clients verbatim as a WhatsApp auto-reply: %r",
            len(conversations),
            (body or "")[:80],
        )
    return verdict


async def _insert_ignoring_duplicates(payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return await db.insert(TABLE, payload)
    except Exception as exc:  # noqa: BLE001 - unique violation on replayed webhooks
        if "duplicate key" in str(exc).lower() or "23505" in str(exc):
            logger.debug("Message %s already stored", payload.get("whapi_message_id"))
            return None
        raise


async def send_bot_reply(conversation: dict[str, Any], body: str) -> dict[str, Any] | None:
    """Send a bot reply through Whapi and record it as ``is_bot = true``."""
    body = (body or "").strip()
    if not body:
        return None

    phone = conversation["customer_number"]
    conversation_id = conversation["id"]

    # Typing indicator scaled to the reply length keeps the thread human.
    typing_time = max(1, min(len(body) // 25, 6))
    try:
        response = await whapi.send_text(phone, body, typing_time=typing_time)
    except WhapiError:
        logger.exception("Failed to deliver bot reply on conversation %s", conversation_id)
        raise

    message_id = whapi.extract_message_id(response)
    mark_sent_by_bot(message_id)
    row = await _insert_ignoring_duplicates(
        {
            "conversation_id": conversation_id,
            "direction": "outbound",
            "from_number": digits_only(settings.whapi_sender_phone) or None,
            "to_number": digits_only(phone),
            "body": body,
            "whapi_message_id": message_id,
            "status": "sent",
            "sent_by": "bot",
            "is_bot": True,
        }
    )
    await conversation_service.touch_outbound(conversation_id, body, is_bot=True)
    return row


async def last_agent_message_at(conversation_id: int) -> datetime | None:
    """When a human agent last replied on this thread, if ever.

    WhatsApp Business greeting messages are excluded. The portal records them
    with ``is_bot = false`` because it cannot tell them apart from a real agent,
    and counting one would put every brand-new client inside the agent grace
    window — blocking the bot on exactly the conversations it exists to answer.
    """
    result = await db.execute(
        db.table(TABLE)
        .select("created_at, body, sent_by")
        .eq("conversation_id", conversation_id)
        .eq("direction", "outbound")
        .eq("is_bot", False)
        .order("created_at", desc=True)
        .limit(10)
    )
    for row in result.data or []:
        if row.get("sent_by") == "whatsapp_auto" or await is_auto_reply(row.get("body")):
            continue
        raw = str(row.get("created_at") or "")
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            logger.debug("Could not parse created_at %r", raw)
            return None
    return None


async def get_history(
    conversation_id: int,
    limit: int | None = None,
    exclude_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Recent transcript, oldest first, with NRICs redacted.

    ``exclude_ids`` drops the messages that are being processed this turn — they
    are passed to the graph separately and must not appear twice.
    """
    limit = limit or settings.history_limit
    result = await db.execute(
        db.table(TABLE)
        .select("direction, body, is_bot, sent_by, whapi_message_id, created_at")
        .eq("conversation_id", conversation_id)
        .order("created_at", desc=True)
        .limit(limit + len(exclude_ids or ()))
    )
    rows = list(reversed(result.data or []))
    if exclude_ids:
        rows = [row for row in rows if row.get("whapi_message_id") not in exclude_ids]
    for row in rows[-limit:]:
        row["body"] = redact_nric(row.get("body"), context="history")
    return rows[-limit:]


def format_history(rows: list[dict[str, Any]]) -> str:
    """Render the transcript for the LLM prompt."""
    lines: list[str] = []
    for row in rows:
        body = (row.get("body") or "").strip()
        if not body:
            continue
        speaker = "Client" if row.get("direction") == "inbound" else "You"
        lines.append(f"{speaker}: {body}")
    return "\n".join(lines)
