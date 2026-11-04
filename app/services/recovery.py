"""The startup scan: answer what a crash left unanswered. OFF by default.

Graceful shutdown (debouncer._drain_all) covers a normal stop. A crash or a kill
still loses the debouncer's buffer, and a message that arrives while the bot is
down reaches only the portal's webhook. Both end up in wp_chat_messages with
nothing after them, so that is what this looks for, once, just before the app
starts taking requests. Switched on by STARTUP_RECOVERY_SCAN, and it needs
scripts/sql/recovery_001_claims.sql applied first: without the table no claim
succeeds, so nothing is sent.

The shape of it, each part because a wrong late reply is worse than none:

- Only unanswered messages under RECOVERY_WINDOW_MINUTES old, from before this
  process started. A later message is the live webhook's, and an older one is
  not replied to or claimed, though it stays in the transcript the model reads.
- Those messages are one batch, read together as the debouncer would have, so
  the client gets one reply and not several.
- Each message is claimed by its Whapi id in cb_recovery_claims BEFORE anything
  is sent, and a message somebody else claimed is dropped. A turn that then fails
  stays claimed: at most once. wp_chat_messages cannot do this job, because the
  portal writes the same rows (see webhook._already_processed).
- The same gates as webhook.handle_inbound, in the same order. On a thread a
  human holds, the only thing that can go out is the safety reply to a report of
  harm, as it can live.
- Only messages to OUR number. The portal stores other channels' messages in the
  same table (account 4, section 9.46), and answering one would send it from the
  wrong WhatsApp number. Checked on to_number, not the conversation's
  business_number: 3,454 of 3,829 live rows disagree with our number there, while
  to_number matched on all 6,181 inbound rows of the last 30 days (2026-11-04).
- More than RECOVERY_MAX_BATCHES to answer means something else is wrong, so the
  whole scan stands down and says so instead of messaging a crowd.

Logs ids and counts only, never message text or a phone number.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from app.api import webhook
from app.config import settings
from app.db.supabase import db
from app.services import conversation as conversation_service
from app.services import message as message_service
from app.services import transcription as transcription_service
from app.utils import digits_only, normalize_phone
from app.whapi.parser import IncomingMessage

logger = logging.getLogger(__name__)

CLAIMS_TABLE = "cb_recovery_claims"
MESSAGES_TABLE = "wp_chat_messages"

RECOVERY_WINDOW_MINUTES = 10
RECOVERY_MAX_BATCHES = 25
# Rows read per conversation to find our last outbound: HISTORY_LIMIT's order of
# size. A chat with more unanswered messages than this is not a crash's doing.
RECOVERY_HISTORY_ROWS = 50
# 2026-11-04: an unanswered message older than the window stays out of the batch,
# so a recent "hello" does not bring a question from days ago back as this turn's
# request. False = everything since our last outbound goes in, if the newest is
# inside the window.
BATCH_ONLY_INSIDE_WINDOW = True
# 2026-11-04: a row minted out of a LID (section 9.16) is told by its number, not
# its name. A portal row can gain a push name, and a real client can lack one.
# Tested against Whapi's chat list, 3,085 LIDs and 2,410 real phones: this catches
# 3,061 LIDs and no phone. On the live conversations it catches 155 of 156 LID rows
# and no phone, where the name caught 150 and also skipped 7 real phones and 21 rows
# Whapi's list does not hold, which this now answers. What it misses is a LID that could be a real number: 11-12 digits, or starting 62.
# False = a row named after its own number is skipped instead.
LID_SHAPE_NOT_NAME = True
# The only 13-digit numbers in the live data: Indonesia, Bangladesh, China. Another
# country's real 13-digit number is skipped here, which costs one late reply.
_REAL_13_DIGIT_PREFIXES = ("62", "880", "86")

_MESSAGE_COLUMNS = (
    "direction, body, is_bot, sent_by, to_number, whapi_message_id, created_at, "
    "reply_to_whapi_id, media_path, media_type, media_mime, media_filename, media_caption"
)


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _recent_rows(conversation_id: int, until: datetime) -> list[dict[str, Any]]:
    """This conversation's latest rows from before `until`, oldest first."""
    result = await db.execute(
        db.table(MESSAGES_TABLE)
        .select(_MESSAGE_COLUMNS)
        .eq("conversation_id", conversation_id)
        .lt("created_at", until.isoformat())
        .order("created_at", desc=True)
        .limit(RECOVERY_HISTORY_ROWS)
    )
    return list(reversed(result.data or []))


async def _conversations_with_recent_inbound(cutoff: datetime, until: datetime, ours: str) -> list[int]:
    """Conversations with a message to our number in the window, newest first."""
    result = await db.execute(
        db.table(MESSAGES_TABLE)
        .select("conversation_id, to_number, created_at")
        .eq("direction", "inbound")
        .gte("created_at", cutoff.isoformat())
        .lt("created_at", until.isoformat())
        .order("created_at", desc=True)
        .limit(500)
    )
    seen: dict[int, None] = {}
    for row in result.data or []:
        if _on_our_channel(row, ours):
            seen.setdefault(row["conversation_id"], None)
    return list(seen)


def _on_our_channel(row: dict[str, Any], ours: str) -> bool:
    if str(row.get("to_number") or "") != ours:
        return False
    return True


async def _unanswered(rows: list[dict[str, Any]], conversation_id: int) -> list[dict[str, Any]]:
    """Every inbound row since our last outbound, oldest first.

    "Our" includes a human agent, and leaves out a WhatsApp Business auto-reply:
    an away message answers nobody, and it usually arrives right after the
    message it did not answer. The portal can record one as an ordinary outbound
    row, hence the second test (see message.last_agent_message_at).
    """
    unanswered: list[dict[str, Any]] = []
    for row in reversed(rows):
        if row.get("sent_by") == message_service.AUTO_REPLY_SENDER:
            continue
        if row.get("direction") == "outbound":
            if await message_service.is_auto_reply(row.get("body"), conversation_id):
                continue
            break
        if row.get("whapi_message_id"):
            unanswered.append(row)
    unanswered.reverse()
    return unanswered


def _lid_shaped(digits: str) -> bool:
    """A LID that was stored as a phone number: longer than any real one we have seen."""
    return (
        len(digits) >= 15
        or (len(digits) == 14 and not digits.startswith("62"))
        or (len(digits) == 13 and not digits.startswith(_REAL_13_DIGIT_PREFIXES))
    )


def _not_ours_to_answer(conversation: dict[str, Any]) -> str:
    """Why this conversation is not one to message at all, or empty."""
    if conversation.get("is_group") or conversation.get("group_jid"):
        return "group"
    number = str(conversation.get("customer_number") or "")
    if "@" in number:
        return "no_real_number"
    digits = digits_only(number)
    if LID_SHAPE_NOT_NAME:
        return "no_real_number" if _lid_shaped(digits) else ""
    if digits and digits == digits_only(conversation.get("customer_name") or ""):
        return "no_real_number"
    return ""


async def _claim(conversation_id: int, message_ids: list[str]) -> set[str]:
    """Claim these messages, returning the ids this call actually won.

    An atomic insert that skips ids already there and returns only the rows it
    inserted. Any failure, such as the table not being applied, wins nothing, so
    nothing is sent.
    """
    rows = [{"whapi_message_id": mid, "conversation_id": conversation_id} for mid in message_ids]
    try:
        result = await db.execute(
            db.table(CLAIMS_TABLE).upsert(rows, on_conflict="whapi_message_id", ignore_duplicates=True)
        )
    except Exception as exc:  # noqa: BLE001 - no claim, no reply
        logger.error("Recovery: could not claim on conversation %s (%s) - not replying", conversation_id, type(exc).__name__)
        return set()
    return {row["whapi_message_id"] for row in (result.data or [])}


def _as_message(row: dict[str, Any], phone: str, name: str) -> IncomingMessage:
    """A stored row back in the shape the webhook parsed it into."""
    quoted = row.get("reply_to_whapi_id")
    return IncomingMessage(
        whapi_message_id=row["whapi_message_id"],
        chat_id="",
        customer_number=phone,
        customer_name=name,
        body=row.get("body") or "",
        message_type=row.get("media_type") or "text",
        from_me=False,
        timestamp=_parse(row.get("created_at")) or datetime.now(timezone.utc),
        media_url=row.get("media_path"),
        media_mime=row.get("media_mime"),
        media_filename=row.get("media_filename"),
        media_caption=row.get("media_caption"),
        raw={"context": {"quoted_id": quoted}} if quoted else {},
    )


async def _pending(
    conversation_id: int, cutoff: datetime, until: datetime, ours: str
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    """Read-only: (status, conversation, the batch). The batch is set only on "due"."""
    conversation = await conversation_service.get_by_id(conversation_id)
    if conversation is None:
        return "gone", None, []
    skip = _not_ours_to_answer(conversation)
    if skip:
        return skip, None, []
    rows = await _unanswered(await _recent_rows(conversation_id, until), conversation_id)
    rows = [row for row in rows if _on_our_channel(row, ours)]
    if not rows:
        return "answered", None, []
    if BATCH_ONLY_INSIDE_WINDOW:
        rows = [r for r in rows if (created := _parse(r.get("created_at"))) is not None and created >= cutoff]
        if not rows:
            return "too_old", None, []
    newest = _parse(rows[-1].get("created_at"))
    if newest is None:
        return "too_old", None, []
    if newest < cutoff:
        return "too_old", None, []
    # A reply that went out and whose row was never stored still counts.
    replied_at = _parse(conversation.get("last_bot_reply_at"))
    if replied_at and replied_at >= newest:
        return "already_replied", None, []
    engage, reason = await webhook.may_engage(
        normalize_phone(conversation.get("customer_number")), conversation
    )
    if not engage and reason in (webhook.ALLOWLIST_BLOCK, webhook.INTERNAL_BLOCK):
        return "gated", None, []
    return "due", conversation, rows


async def _answer(conversation: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    """Claim the batch, then the rest of webhook.handle_inbound's path, minus the store."""
    conversation_id = conversation["id"]
    won = await _claim(conversation_id, [r["whapi_message_id"] for r in rows])
    rows = [r for r in rows if r["whapi_message_id"] in won]
    if not rows:
        return "claimed_elsewhere"

    phone = normalize_phone(conversation.get("customer_number"))
    name = conversation.get("customer_name") or ""
    messages = [_as_message(row, phone, name) for row in rows]
    # Marks them seen in this process too, so Whapi redelivering one is dropped,
    # and drops any the live webhook has already taken.
    messages = [m for m in messages if not webhook._already_processed(m.whapi_message_id)]
    messages = [m for m in messages if message_service.has_message_content(m)]
    if not messages:
        return "nothing_to_answer"

    for message in messages:
        if (
            not message.body
            and transcription_service.is_speech(message)
            and str(message.media_url).startswith("http")
        ):
            message.transcript = await transcription_service.transcribe(message)
    text = "\n".join(m.text_for_llm for m in messages if m.text_for_llm)

    # Re-read, because the pre-pass may have been a while ago.
    engage, reason = await webhook.may_engage(phone, conversation)
    if not engage:
        if reason not in (webhook.ALLOWLIST_BLOCK, webhook.INTERNAL_BLOCK):
            await webhook.emergency_override(conversation, text)
        return "stood_down"

    for message in messages:
        if await webhook._human_conversation(conversation, message):
            return "human"

    conversation = await webhook.maybe_return_to_bot(conversation)
    if webhook._is_paused_for_agent(conversation):
        await webhook.emergency_override(conversation, text)
        return "paused"

    conversation, is_new = await conversation_service.get_or_create(phone, name, engage=True)
    if webhook._should_identify(conversation, is_new):
        conversation = await webhook.identify_contact(conversation)
    if not conversation_service.bot_should_reply(conversation):
        return "not_ours"

    await webhook.process_batch(phone, messages)
    return "answered_now"


async def run(until: datetime | None = None) -> dict[str, int]:
    """One pass, returning how each conversation ended. Empty when switched off."""
    if not settings.startup_recovery_scan:
        return {}
    ours = digits_only(settings.whapi_sender_phone)
    if not ours:
        logger.error("Recovery: WHAPI_SENDER_PHONE is not set, so our messages cannot be told apart - not scanning")
        return {"refused": 1}

    until = until or datetime.now(timezone.utc)
    cutoff = until - timedelta(minutes=RECOVERY_WINDOW_MINUTES)
    counts: Counter[str] = Counter()
    candidates: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for conversation_id in await _conversations_with_recent_inbound(cutoff, until, ours):
        status, conversation, rows = await _pending(conversation_id, cutoff, until, ours)
        counts[status] += 1
        if status == "due":
            candidates.append((conversation, rows))

    if len(candidates) > RECOVERY_MAX_BATCHES:
        logger.error(
            "Recovery: %d conversations look unanswered, over the limit of %d - not sending any",
            len(candidates), RECOVERY_MAX_BATCHES,
        )
        return {"refused": len(candidates)}

    for conversation, rows in candidates:
        try:
            counts[await _answer(conversation, rows)] += 1
        except Exception:  # noqa: BLE001 - one conversation must not stop the rest
            logger.exception("Recovery: failed on conversation %s", conversation.get("id"))
            counts["error"] += 1
    logger.info("Recovery scan finished: %s", dict(counts))
    return dict(counts)
