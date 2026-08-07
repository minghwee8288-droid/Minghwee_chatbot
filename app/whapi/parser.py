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
    counterparty = chat_id if from_me else (message.get("from") or chat_id)

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
