"""Parsing and verification of inbound Whapi webhook payloads."""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.utils import digits_only, normalize_phone

logger = logging.getLogger(__name__)

# Whapi message types that carry a media object of the same name.
MEDIA_TYPES = ("image", "video", "audio", "voice", "document", "sticker")

# A WhatsApp identifier is only a phone number if its JID says so.
#
# Live, 2026-09-10: every message from an allowlisted tester arrived as
# "Bot standing down on +116909177569373: number not in BOT_ALLOWED_NUMBERS"
# while they were messaging from +917970027379. 116909177569373 is not a
# mangled phone number - it is a **LID**, the opaque identifier Meta uses in
# place of a phone number once a business number is moved onto its hosted
# service ("This business uses a secure service from Meta to manage this
# chat"). normalize_phone() splits on "@" and keeps whatever is in front of
# it, so "116909177569373@lid" became the phone number "+116909177569373",
# the allowlist correctly refused a number nobody has ever heard of, and the
# bot went silent on a client it was supposed to answer.
#
# Minting a fake phone number is the worse half of that bug. Every downstream
# lookup is keyed on the number - the allowlist, conversation.get_by_phone,
# contact.identify, the lead - so a LID that reaches them does not just fail
# the gate: allowlist it and it would open a SECOND conversation keyed on an
# identifier that is not a phone number, which is the split-conversation bug
# scripts/fix_split_conversations.py exists to repair.
#
# So the counterparty is resolved from the first candidate whose JID is
# actually a phone, rather than from whichever field came first.
_PHONE_JID_SUFFIXES = ("@s.whatsapp.net", "@c.us")
_LID_SUFFIX = "@lid"


def _is_phone_jid(value: str) -> bool:
    """Whether this identifier is a phone number rather than a LID."""
    value = (value or "").strip()
    if not value:
        return False
    if "@" not in value:
        return True  # a bare number, which is what Whapi sends for some events
    return value.lower().endswith(_PHONE_JID_SUFFIXES)


def _counterparty(message: dict[str, Any], *keys: str) -> str:
    """The client's identifier, preferring a phone JID over a LID.

    `keys` are tried in order of how well each names the counterparty; a LID
    is only used when nothing better is present, so behaviour is unchanged on
    every payload that carries a phone number somewhere.
    """
    candidates = [str(message.get(key) or "").strip() for key in keys]
    for candidate in candidates:
        if _is_phone_jid(candidate):
            return candidate
    lid = next((c for c in candidates if c.lower().endswith(_LID_SUFFIX)), "")
    if lid:
        # Kept rather than dropped: standing down on a number we cannot read is
        # the same outcome as today, and a dropped message logs nothing at all.
        # This line is what makes the next occurrence diagnosable - it names
        # every identifier the payload carried, which is what tells us where
        # the real number is hiding (if it is there at all).
        logger.warning(
            "Whapi message %s carries only a LID (%s) and no phone JID - the "
            "allowlist and every phone lookup will miss it. Identifiers in "
            "this payload: %s",
            message.get("id"),
            lid,
            {k: v for k, v in message.items()
             if isinstance(v, str) and ("@" in v or k in ("from", "to", "chat_id"))},
        )
    return next((c for c in candidates if c), "")


@dataclass
class IncomingMessage:
    """One WhatsApp message, normalised out of the Whapi payload."""

    whapi_message_id: str
    chat_id: str
    customer_number: str
    customer_name: str
    body: str
    message_type: str
    from_me: bool
    timestamp: datetime
    media_url: str | None = None
    media_mime: str | None = None
    media_filename: str | None = None
    media_caption: str | None = None
    # Set once a voice note has been through speech-to-text. From here on the
    # message behaves as though the client typed it (system prompt rule 14).
    transcript: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_media(self) -> bool:
        return self.message_type in MEDIA_TYPES

    @property
    def is_transcribed(self) -> bool:
        return bool(self.transcript)

    @property
    def text_for_llm(self) -> str:
        """What the conversation engine should read for this message."""
        if self.transcript:
            return self.transcript
        if self.body:
            return self.body
        if self.has_media:
            label = self.media_filename or self.message_type
            return f"[client sent a {self.message_type}: {label}]"
        return f"[{self.message_type} message]"


def verify_signature(raw_body: bytes, headers: dict[str, str]) -> bool:
    """Verify the webhook came from Whapi.

    Whapi lets you attach a shared secret to the webhook. Depending on the
    channel configuration it arrives either as a plain token header or as an
    HMAC-SHA256 hex digest of the body, so both are accepted.
    """
    secret = settings.whapi_webhook_secret
    if not secret:
        if settings.is_production:
            logger.error("WHAPI_WEBHOOK_SECRET is not set in production — rejecting webhook")
            return False
        return True

    lowered = {k.lower(): v for k, v in headers.items()}
    token = (
        lowered.get("x-whapi-secret")
        or lowered.get("x-webhook-secret")
        or lowered.get("x-whapi-token")
    )
    if token and hmac.compare_digest(token.strip(), secret):
        return True

    signature = lowered.get("x-whapi-signature") or lowered.get("x-hub-signature-256")
    if signature:
        provided = signature.split("=", 1)[-1].strip()
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(provided, expected):
            return True

    authorization = lowered.get("authorization", "")
    if authorization.removeprefix("Bearer ").strip() == secret:
        return True

    return False


def _timestamp(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(tz=timezone.utc)


def _extract_media(message: dict[str, Any], msg_type: str) -> dict[str, Any]:
    media = message.get(msg_type)
    if not isinstance(media, dict):
        return {}
    return {
        "media_url": media.get("link") or media.get("url"),
        "media_mime": media.get("mime_type"),
        "media_filename": media.get("file_name") or media.get("filename"),
        "media_caption": media.get("caption"),
    }


def parse_message(message: dict[str, Any]) -> IncomingMessage | None:
    """Convert one raw Whapi message object into an IncomingMessage."""
    message_id = message.get("id")
    if not message_id:
        logger.warning("Whapi message without an id — skipping: %s", message)
        return None

    msg_type = message.get("type") or "text"
    from_me = bool(message.get("from_me"))
    chat_id = message.get("chat_id") or message.get("from") or ""

    # For outbound (from_me) messages the counterparty is the chat itself.
    # Either way the identifier has to be a PHONE, not a LID - see above.
    counterparty = (
        _counterparty(message, "chat_id", "to", "from")
        if from_me
        else _counterparty(message, "from", "chat_id")
    )

    body = ""
    if msg_type == "text":
        body = (message.get("text") or {}).get("body", "") or ""
    media = _extract_media(message, msg_type)
    if not body:
        body = media.get("media_caption") or ""

    return IncomingMessage(
        whapi_message_id=str(message_id),
        chat_id=str(chat_id),
        customer_number=normalize_phone(counterparty),
        customer_name=(message.get("from_name") or "").strip(),
        body=body.strip(),
        message_type=msg_type,
        from_me=from_me,
        timestamp=_timestamp(message.get("timestamp")),
        raw=message,
        **media,
    )


def parse_webhook(payload: dict[str, Any]) -> list[IncomingMessage]:
    """Extract every message from a Whapi webhook payload.

    Status callbacks (``statuses``), presence and channel events are ignored —
    only ``messages`` matter to the bot.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []

    parsed: list[IncomingMessage] = []
    own_number = digits_only(settings.whapi_sender_phone)
    for message in messages:
        if not isinstance(message, dict):
            continue
        # Group chats are out of scope: the bot only handles 1:1 client threads.
        chat_id = str(message.get("chat_id") or "")
        if chat_id.endswith("@g.us") or chat_id.endswith("@broadcast"):
            logger.debug("Ignoring group/broadcast message %s", message.get("id"))
            continue
        item = parse_message(message)
        if not item or not item.customer_number:
            continue
        # A "chat" whose counterparty is our own number is WhatsApp's
        # message-yourself thread or a protocol message, not a client.
        if own_number and digits_only(item.customer_number) == own_number:
            logger.debug("Ignoring self-chat message %s", item.whapi_message_id)
            continue
        parsed.append(item)
    return parsed
