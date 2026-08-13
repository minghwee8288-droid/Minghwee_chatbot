"""Whapi webhook endpoint and the message-processing pipeline.

The endpoint acknowledges immediately and does the real work in the
background — Whapi retries anything that does not return quickly.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request, Response, status

from app.config import settings
from app.graph.graph import run_turn
from app.graph.guards import near_duplicate, recent_bot_lines
from app.graph.nodes.handover_executor import ASSAULT_FALLBACK_REPLY
from app.graph.nodes.intent_classifier import confirm_harm, matches_assault_keywords
from app.services import assignment as assignment_service
from app.services import contact as contact_service
from app.services import conversation as conversation_service
from app.services import handover as handover_service
from app.services import lead as lead_service
from app.services import message as message_service
from app.services import ticket as ticket_service
from app.services import transcription as transcription_service
from app.utils import redact_nric
from app.whapi.client import WhapiError
from app.whapi.debouncer import MessageDebouncer
from app.whapi.parser import IncomingMessage, parse_webhook, verify_signature

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhook"])

# Marker for the one stand-down reason no emergency may override.
ALLOWLIST_BLOCK = "number not in BOT_ALLOWED_NUMBERS"

# One lock per conversation, so turns on the same thread never overlap.
_LOCKS: dict[str, asyncio.Lock] = {}

# Webhook-level replay guard.
#
# This deliberately does NOT test whether the message row already exists: the
# WhatsApp portal is subscribed to the same Whapi channel and writes the same
# rows to wp_chat_messages. If a stored row were treated as "already handled",
# whichever service inserted first would silence the other — the bot would stop
# replying to clients, or miss an agent taking over.
_PROCESSED: "OrderedDict[str, float]" = OrderedDict()
_PROCESSED_TTL_SECONDS = 900
_PROCESSED_MAX = 5000


def _already_processed(message_id: str) -> bool:
    now = time.monotonic()
    stamp = _PROCESSED.get(message_id)
    if stamp is not None and now - stamp <= _PROCESSED_TTL_SECONDS:
        return True
    _PROCESSED[message_id] = now
    while len(_PROCESSED) > _PROCESSED_MAX:
        _PROCESSED.popitem(last=False)
    return False


# --- Endpoint --------------------------------------------------------------

@router.post("/webhook/whapi")
async def whapi_webhook(request: Request, background: BackgroundTasks) -> Response:
    return await _accept(request, background)


async def _accept(
    request: Request, background: BackgroundTasks, *, verified: bool = False
) -> Response:
    raw_body = await request.body()

    if not verified and not verify_signature(raw_body, dict(request.headers)):
        logger.warning("Rejected webhook with an invalid signature")
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed body, nothing to retry
        logger.warning("Webhook body was not valid JSON")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if isinstance(payload, list):  # some channels batch events into an array
        for item in payload:
            if isinstance(item, dict):
                background.add_task(handle_payload, item)
    elif isinstance(payload, dict):
        background.add_task(handle_payload, payload)

    return Response(status_code=status.HTTP_200_OK)


@router.post("/webhook/whapi/{secret}")
async def whapi_webhook_with_path_secret(
    secret: str, request: Request, background: BackgroundTasks
) -> Response:
    """Same endpoint, with the shared secret in the URL path.

    This mirrors how the WhatsApp portal's webhook is configured on this Whapi
    channel, and works even where custom webhook headers are not available.
    """
    configured = settings.whapi_webhook_secret
    if not configured or not compare_digest(secret, configured):
        logger.warning("Rejected webhook with an invalid path secret")
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    return await _accept(request, background, verified=True)


@router.get("/webhook/whapi")
async def whapi_webhook_verify() -> dict[str, str]:
    """Whapi pings the URL when the webhook is configured."""
    return {"status": "ok"}


# --- Pipeline --------------------------------------------------------------

async def handle_payload(payload: dict[str, Any]) -> None:
    for message in parse_webhook(payload):
        try:
            if message.from_me:
                await handle_outbound(message)
            else:
                await handle_inbound(message)
        except Exception:  # noqa: BLE001 - one bad message must not stop the rest
            logger.exception("Failed to handle message %s", message.whapi_message_id)


async def emergency_override(conversation: dict[str, Any], text: str) -> bool:
    """Answer a report of harm even when the bot is meant to stay silent.

    Without this, someone reporting an assault at 2am on a thread an agent
    touched yesterday gets nothing at all — no reply, no ticket, nobody paged —
    until an agent opens the portal in the morning. Safety outranks the silent-
    handover rule.

    The conversation still belongs to the human: this sends one safety message
    and raises an urgent escalation, then stops. It does not take the thread
    over, so an agent arriving later is not fighting the bot for it.
    """
    if not matches_assault_keywords(text):
        return False
    if not await confirm_harm(text):
        logger.info("Assault keywords on a stood-down thread, but verification disagreed")
        return False

    conversation_id = conversation["id"]
    logger.warning(
        "EMERGENCY OVERRIDE on conversation %s — replying and escalating despite standing down",
        conversation_id,
    )

    agent_id, rule = await assignment_service.resolve_agent(
        intent="dispute_assault",
        matched_employer_id=conversation.get("matched_employer_id"),
    )
    ticket = await ticket_service.create(
        conversation=conversation,
        service_type="dispute_assault",
        captured_info=ticket_service.structure_captured(
            {
                "contact_type": conversation.get("contact_type"),
                "whatsapp_number": conversation.get("customer_number"),
                "whatsapp_name": conversation.get("customer_name"),
                "client_message": text[:2000],
                "bot_note": "Raised by the out-of-hours safety override; the thread was with a human.",
            },
            detail_keys=set(),
        ),
        assigned_agent_id=agent_id,
        assignment_rule=rule,
        description=ticket_service.initial_description(
            "dispute_assault",
            conversation.get("contact_type"),
            {"client_message": text},
        ),
    )
    await handover_service._log(
        conversation_id,
        handover_service.BOT_TO_HUMAN,
        handover_service.REASON_DISPUTE,
        agent_profile_id=agent_id,
        ticket_id=(ticket or {}).get("id"),
    )

    try:
        await message_service.send_bot_reply(conversation, ASSAULT_FALLBACK_REPLY)
    except WhapiError:
        logger.exception("Could not deliver the emergency reply on conversation %s", conversation_id)
    return True


# Someone checking whether anyone is on the other end. blocked_topic_responder
# has its own copy of this for the case where the bot is still running the
# thread; this one covers the case where it has stood itself down, which is the
# one that produced silence live.
#
# Chasing a reply counts. "when can i expect your reply?" is the same person
# asking the same thing as "are you there" — it arrived in the same unanswered
# run live — and the alternative for both is nothing at all until the 72-hour
# net expires.
_DIRECT_ADDRESS = re.compile(
    r"^\W*(hello+|hi+|hey+|you\s+there|are\s+you\s+(there|alive|ok)|anyone|any\s?one\s+there|"
    r"bot|is\s+(anyone|someone)\s+there)\W*$|\bare\s+you\s+there\b|\bhello+\s*\?+|^\W*\?+\W*$"
    r"|\bany\s+(update|news|reply|response)\b|\bwhen\s+(can|will|do)\s+(i|you)\b"
    r"|\bhow\s+long\s+(will|does|do)\b|\bstill\s+waiting\b|\bwaiting\s+for\s+(your|a)\s+(reply|response)\b",
    re.IGNORECASE,
)

STILL_HERE_REPLY = (
    "I am here — still working on this for you. I will come back to you as soon as "
    "I have something."
)


async def acknowledge_direct_address(conversation: dict[str, Any], text: str) -> bool:
    """Answer "are you there?" on a thread the bot has stood itself down on.

    A bot-initiated handover sets bot_status=human_active with no agent message
    behind it, so maybe_return_to_bot() falls through to the 72-hour safety net
    and every message until then is dropped without a word. Live, that was
    "so please find the suitable candidates for me", "when can i expect your
    reply?" and "??" in a row, then "Are you there", "Hello", "Hello", "Hi" on
    the other number — eight unanswered messages across two conversations in one
    test. Being ignored while asking whether anyone is there is the worst thing
    the bot can do, and it is worse precisely because the client was in the
    middle of a conversation that had been going well.

    Deliberately narrow. It fires only when a REAL agent has never messaged the
    thread — an agent mid-conversation owns it and must not be talked over — and
    only once, because a bot repeating "I am here" at someone who is waiting is
    its own kind of insult. Everything else stays silent exactly as before, and
    nothing here takes the conversation back: bot_status is left alone.
    """
    if not _DIRECT_ADDRESS.search((text or "").strip()):
        return False

    conversation_id = conversation["id"]
    if await message_service.last_agent_message_at(conversation_id) is not None:
        return False  # a human is on this thread — theirs to answer

    history = await message_service.get_history(conversation_id)
    if any(
        near_duplicate(STILL_HERE_REPLY, line, threshold=0.7)
        for line in recent_bot_lines(message_service.format_history(history), count=3)
    ):
        logger.info(
            "Conversation %s: already told them we are here — staying quiet", conversation_id
        )
        return False

    logger.info(
        "Conversation %s: client asking if anyone is there during a bot standdown — answering once",
        conversation_id,
    )
    try:
        await message_service.send_bot_reply(conversation, STILL_HERE_REPLY)
    except WhapiError:
        logger.exception("Could not deliver the standdown acknowledgement on %s", conversation_id)
        return False
    return True


async def may_engage(phone: str, conversation: dict[str, Any] | None) -> tuple[bool, str]:
    """Decide whether this conversation is the bot's to answer.

    The portal has been running on this number for months, so most existing
    threads are live human conversations. Engaging them would put the bot in the
    middle of an agent's client relationship.
    """
    if settings.gate_enabled and phone not in settings.allowed_numbers:
        # Absolute: never message a number that is not being tested, not even
        # for an emergency.
        return False, ALLOWLIST_BLOCK

    if conversation is None:
        return True, "new conversation"

    if (conversation.get("bot_status") or "") == conversation_service.BOT_ACTIVE:
        return True, "already a bot conversation"

    if (conversation.get("bot_status") or "") == conversation_service.HUMAN_ACTIVE:
        # The bot stood ITSELF down on this thread, so the resume clock is ours:
        # maybe_return_to_bot() below applies agent_pause_minutes (10) and hands
        # the conversation back once the agent has been quiet that long.
        #
        # This used to fall through to the agent_grace_hours check underneath,
        # which sees the agent's message and stands the bot down for 24 hours —
        # so the 10-minute resume was unreachable and every human reply silenced
        # the bot for the rest of the day. The grace window is for threads the
        # bot was never on (below), where the portal owns the conversation and
        # there is no pause of ours to expire.
        return True, "bot-managed standdown — the agent-pause rules decide"

    if settings.agent_grace_hours > 0:
        last_agent = await message_service.last_agent_message_at(conversation["id"])
        if last_agent is not None:
            # Clamp: the database clock can run marginally ahead of ours, which
            # would otherwise report a negative age for a message just written.
            age = max(datetime.now(tz=timezone.utc) - last_agent, timedelta(0))
            if age < timedelta(hours=settings.agent_grace_hours):
                minutes = age.total_seconds() / 60
                return False, f"an agent replied {minutes:.0f} min ago"

    return True, "no recent agent activity"


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def maybe_return_to_bot(conversation: dict[str, Any]) -> dict[str, Any]:
    """Hand a conversation back to the bot once the pause that silenced it is over.

    bot_status = human_active now has exactly two causes, and each gets its
    own recovery:

    1. A real agent message (agent_took_over). last_agent_message_at() finds
       a timestamp, and the bot resumes on its own after
       AGENT_PAUSE_MINUTES of no further agent message — it does not wait for
       anyone to mark anything resolved. An explicit resolve (status ->
       closed/resolved in the portal) still short-circuits this immediately.
    2. The bot failing outright, with no agent ever involved
       (_fail_over_to_human). last_agent_message_at() finds nothing, so this
       falls back to the long HUMAN_ACTIVE_TIMEOUT_HOURS safety net, measured
       from the bot's own last reply.

    Either way this only flips bot_status; it does not itself decide whether
    to answer whatever triggered this call, and it never marks a message
    "already handled" — the next thing that happens is that same inbound
    message being processed normally, once (see handle_inbound). Nothing here
    proactively sends anything on a timer.
    """
    if (conversation.get("bot_status") or "") != conversation_service.HUMAN_ACTIVE:
        return conversation

    conversation_id = conversation["id"]

    if (conversation.get("status") or "").lower() in conversation_service.CLOSED_STATUSES:
        logger.info("Conversation %s was resolved by an agent — returning it to the bot", conversation_id)
        await handover_service.back_to_bot(conversation, reason=handover_service.REASON_AGENT_RESOLVED)
        return await conversation_service.get_by_id(conversation_id) or conversation

    last_agent = await message_service.last_agent_message_at(conversation_id)
    if last_agent is not None:
        if settings.agent_pause_minutes <= 0:
            return conversation  # auto-resume disabled — permanent until explicitly resolved
        idle = max(datetime.now(tz=timezone.utc) - last_agent, timedelta(0))
        if idle >= timedelta(minutes=settings.agent_pause_minutes):
            logger.info(
                "Conversation %s: agent quiet for %.0f min — resuming the bot",
                conversation_id,
                idle.total_seconds() / 60,
            )
            await handover_service.resume_from_pause(conversation)
            return await conversation_service.get_by_id(conversation_id) or conversation
        return conversation

    # No agent message was ever found — this standdown came from the bot
    # failing outright, not a human being on the thread. Long safety-net
    # timeout only, so an unresolved bot failure does not sit silent forever.
    if settings.human_active_timeout_hours <= 0:
        return conversation

    reference = _parse_timestamp(conversation.get("last_bot_reply_at")) or _parse_timestamp(
        conversation.get("updated_at")
    )
    if reference is None:
        return conversation

    idle = max(datetime.now(tz=timezone.utc) - reference, timedelta(0))
    if idle >= timedelta(hours=settings.human_active_timeout_hours):
        logger.info(
            "Conversation %s idle with no agent ever involved, for %.0fh — returning it to the bot",
            conversation_id,
            idle.total_seconds() / 3600,
        )
        await handover_service.back_to_bot(conversation, reason=handover_service.REASON_AGENT_IDLE)
        return await conversation_service.get_by_id(conversation_id) or conversation

    return conversation


async def handle_inbound(message: IncomingMessage) -> None:
    """Store an incoming client message and queue it for the bot."""
    if _already_processed(message.whapi_message_id):
        logger.debug("Replayed webhook for %s — ignoring", message.whapi_message_id)
        return

    phone = message.customer_number
    existing = await conversation_service.get_by_phone(phone)
    engage, reason = await may_engage(phone, existing)

    # Transcribe before anything reads the text — including the safety override
    # below, which is exactly the path a distressed client is most likely to
    # send a voice note down. Skipped for allowlist-blocked numbers, where the
    # bot touches nothing at all.
    if reason != ALLOWLIST_BLOCK and transcription_service.is_speech(message):
        message.transcript = await transcription_service.transcribe(message)

    if not engage:
        # Touch nothing at all. The portal owns these conversations and is
        # already storing the message from its own webhook.
        logger.info("Bot standing down on %s: %s", phone, reason)
        # ...unless someone is reporting harm. Silence is not an option there.
        if reason != ALLOWLIST_BLOCK and existing is not None:
            if await emergency_override(existing, message.text_for_llm):
                return
            await acknowledge_direct_address(existing, message.text_for_llm)
        return

    if existing is not None:
        existing = await maybe_return_to_bot(existing)
        if not conversation_service.bot_should_reply(existing):
            # Still inside the pause — the agent is mid-conversation. Touch
            # nothing (the portal owns the thread and is storing the message
            # from its own webhook), but never stay silent on a report of harm.
            logger.info(
                "Conversation %s still with a human — bot quiet until the agent "
                "has been idle %d min",
                existing["id"],
                settings.agent_pause_minutes,
            )
            if not await emergency_override(existing, message.text_for_llm):
                await acknowledge_direct_address(existing, message.text_for_llm)
            return

    conversation, is_new = await conversation_service.get_or_create(
        phone, message.customer_name, engage=True
    )
    conversation_id = conversation["id"]

    await message_service.store_incoming(conversation_id, message)
    await conversation_service.touch_inbound(conversation_id, message.text_for_llm)

    if _should_identify(conversation, is_new):
        conversation = await identify_contact(conversation)

    if not conversation_service.bot_should_reply(conversation):
        logger.info(
            "Conversation %s is with a human (bot_status=%s) — bot staying quiet",
            conversation_id,
            conversation.get("bot_status"),
        )
        await acknowledge_direct_address(conversation, message.text_for_llm)
        return

    await debouncer.add(message)


async def handle_outbound(message: IncomingMessage) -> None:
    """An outbound message appeared on the number — ours, or a human agent's."""
    if _already_processed(message.whapi_message_id):
        return

    if await message_service.was_sent_by_bot(message.whapi_message_id):
        return  # our own send, echoed back by Whapi

    conversation = await conversation_service.get_by_phone(message.customer_number)
    if conversation is None:
        # An agent opened the conversation from the WhatsApp Business app.
        # engage=False: an outbound message is never a reason for the bot to
        # start answering a number it would otherwise leave alone.
        conversation, _ = await conversation_service.get_or_create(
            message.customer_number, engage=False
        )
        conversation = await identify_contact(conversation)

    # The WhatsApp Business app's own greeting/away messages are not an agent.
    # Treating them as one would silence the bot on every new conversation.
    if await message_service.is_auto_reply(message.body):
        await message_service.store_auto_reply(conversation["id"], message)
        logger.info(
            "Ignoring WhatsApp Business auto-reply on conversation %s", conversation["id"]
        )
        return

    try:
        await message_service.store_agent_reply(conversation["id"], message)
        await conversation_service.touch_outbound(
            conversation["id"], message.text_for_llm, is_bot=False
        )
    except Exception:  # noqa: BLE001 - standing down matters more than the row
        logger.exception(
            "Could not store agent message on conversation %s — silencing the bot anyway",
            conversation["id"],
        )

    # Agent detector: a human is on the thread, so the bot stands down and any
    # half-buffered client messages are dropped.
    await debouncer.flush_now(message.customer_number)
    await handover_service.agent_took_over(conversation)


# Unknown contacts are looked up again periodically rather than once, so a
# number that is added to employers/candidates/profiles later is recognised on
# the client's next message. Without this a conversation stamped 'unknown' on
# its first message stayed unknown permanently — 'unknown' is truthy, so the
# "have we identified this yet" test never fired again.
_IDENTIFY_RETRY_SECONDS = 900
_IDENTIFIED_AT: "OrderedDict[str, float]" = OrderedDict()
_IDENTIFIED_MAX = 5000


def _retry_due(phone: str) -> bool:
    """Throttle, so a burst of messages does not repeat the lookup each time."""
    now = time.monotonic()
    last = _IDENTIFIED_AT.get(phone)
    if last is not None and now - last < _IDENTIFY_RETRY_SECONDS:
        return False
    _IDENTIFIED_AT[phone] = now
    while len(_IDENTIFIED_AT) > _IDENTIFIED_MAX:
        _IDENTIFIED_AT.popitem(last=False)
    return True


def _should_identify(conversation: dict[str, Any], is_new: bool) -> bool:
    contact_type = (conversation.get("contact_type") or "").strip()
    if is_new or not contact_type:
        return True

    # Two ways the stored identity goes stale: the number was never matched to
    # anything, or it matched an employer who had no case open at the time and
    # has one now.
    stale = contact_type == contact_service.UNKNOWN or (
        contact_type == contact_service.EMPLOYER and not conversation.get("matched_case_id")
    )
    if not stale:
        return False
    return _retry_due(conversation.get("customer_number") or "")


async def identify_contact(conversation: dict[str, Any]) -> dict[str, Any]:
    """Resolve the phone number against the platform tables and persist it."""
    identity = await contact_service.identify(conversation["customer_number"])
    patch = {
        key: identity[key]
        for key in (
            "contact_type",
            "matched_employer_id",
            "matched_candidate_id",
            "matched_supplier_id",
            "matched_case_id",
        )
        if identity.get(key) is not None
    }
    if identity.get("display_name") and not conversation.get("customer_name"):
        patch["customer_name"] = identity["display_name"]

    # salesperson_profile_id is not a conversation column — the assignment
    # service reads it straight off the employer row when a handover happens.
    return await conversation_service.update(conversation["id"], **patch) or conversation


def _conversation_lock(phone: str) -> asyncio.Lock:
    return _LOCKS.setdefault(phone, asyncio.Lock())


async def process_batch(phone: str, messages: list[IncomingMessage]) -> None:
    """Run one debounced batch of client messages through the graph.

    Serialised per conversation. A turn takes several seconds end to end, and
    without the lock a message arriving mid-turn starts a second graph run on
    the same thread: the client gets two replies and, when both runs complete
    collection, two tickets are raised for one lead.
    """
    lock = _conversation_lock(phone)

    if lock.locked():
        # A turn is already running for this client. Give these messages to it
        # rather than queueing a second turn behind it — three messages typed as
        # one thought should produce one reply, not three.
        await debouncer.requeue(phone, messages)
        logger.info("Conversation busy — folded %d message(s) into the running turn", len(messages))
        return

    async with lock:
        pending = _dedupe(messages + await debouncer.drain(phone))
        # A message that genuinely arrives after the reply is a new turn, so
        # keep going until nothing is left. Bounded in case of a message storm.
        for _ in range(5):
            if not pending:
                break
            await _process_locked(phone, pending)
            pending = _dedupe(await debouncer.drain(phone))


def _dedupe(messages: list[IncomingMessage]) -> list[IncomingMessage]:
    """Unique messages in the order the client actually sent them.

    Sorted by timestamp rather than trusting list order: batches are stitched
    together from the flush, the buffer and anything requeued mid-turn, so
    insertion order does not reflect what the client typed first.
    """
    seen: set[str] = set()
    unique = []
    for message in messages:
        if message.whapi_message_id in seen:
            continue
        seen.add(message.whapi_message_id)
        unique.append(message)
    return sorted(unique, key=lambda m: m.timestamp)


def _predates_our_last_reply(
    conversation: dict[str, Any], messages: list[IncomingMessage]
) -> bool:
    """Whether the client sent all of this before they could have read our reply.

    A burst typed with pauses longer than the debounce window becomes two turns.
    The client typed all of it as one thought, so the second turn's messages are
    not an answer to the question the first turn just asked — they were written
    before it arrived. Generating the same question again is the visible result:
    live, the client got "When would you like the helper to start?" and, in the
    very next message, "What about start date — when do you need her to begin?"

    The repeat guard below lets questions through on purpose, because a repeated
    question usually means the client has not answered it yet and going quiet
    would strand the flow. That reasoning holds only when they have SEEN it.
    This is the test for whether they have.
    """
    sent_at = _parse_timestamp(conversation.get("last_bot_reply_at"))
    if sent_at is None or not messages:
        return False
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return all(m.timestamp <= sent_at for m in messages)


async def _process_locked(phone: str, messages: list[IncomingMessage]) -> None:
    # Re-read after acquiring the lock: the previous batch may have handed over.
    conversation = await conversation_service.get_by_phone(phone)
    if conversation is None:
        logger.warning("No conversation for %s at processing time", phone)
        return

    if not conversation_service.bot_should_reply(conversation):
        logger.info("Conversation %s taken over — dropping batch", conversation["id"])
        return

    combined = "\n".join(m.text_for_llm for m in messages if m.text_for_llm).strip()
    if not combined:
        return
    combined = redact_nric(combined, context=f"conversation:{conversation['id']}")

    # A transcribed voice note is text now, not an attachment — only files
    # nobody can read need a human to open them.
    media_items = [
        {
            "type": m.message_type,
            "filename": m.media_filename,
            "mime": m.media_mime,
            "caption": m.media_caption,
            "url": m.media_url,
        }
        for m in messages
        if m.has_media and not m.is_transcribed
    ]

    batch_ids = {m.whapi_message_id for m in messages}
    history = await message_service.get_history(conversation["id"], exclude_ids=batch_ids)

    thread_id = conversation.get("langgraph_thread_id") or conversation_service.new_thread_id(
        conversation["id"]
    )
    if not conversation.get("langgraph_thread_id"):
        await conversation_service.update(conversation["id"], langgraph_thread_id=thread_id)

    # Only employers get the "previous enquiry" line, so only look it up for them.
    contact_type = conversation.get("contact_type") or ""
    last_ticket = (
        await ticket_service.last_for_conversation(conversation["id"])
        if contact_type == "employer"
        else None
    )
    # An open lead means we have qualified this number before: the collector
    # should not start the same questions again. Master records make it moot.
    open_lead = (
        await lead_service.find_by_phone(phone)
        if not (conversation.get("matched_employer_id") or conversation.get("matched_candidate_id"))
        else None
    )

    # Topics a human is already working on this thread — read fresh every
    # turn (never persisted on the checkpoint) so a ticket closing anywhere
    # unblocks its topic on the very next message, with no extra sync step.
    blocked_topics = await ticket_service.open_topics_for_conversation(conversation["id"])

    payload = {
        "conversation_id": conversation["id"],
        "phone": phone,
        "customer_name": conversation.get("customer_name") or "",
        "thread_id": thread_id,
        "contact_type": conversation.get("contact_type") or "unknown",
        "matched_employer_id": conversation.get("matched_employer_id"),
        "matched_candidate_id": conversation.get("matched_candidate_id"),
        "matched_supplier_id": conversation.get("matched_supplier_id"),
        "matched_case_id": conversation.get("matched_case_id"),
        "last_ticket": last_ticket,
        # Lead columns do not exist on wp_chat_conversations, so an open lead is
        # read per turn rather than stored on the row.
        "matched_lead_id": (open_lead or {}).get("id"),
        "matched_lead_number": (open_lead or {}).get("lead_number"),
        "lead_kind": (open_lead or {}).get("lead_kind"),
        # The row itself, so the collector can reuse what this number already
        # told us. Without it the prompt says "do not start their details
        # again" while the collector asks for all of them from scratch.
        "matched_lead": open_lead,
        "salesperson_profile_id": None,
        "blocked_topics": blocked_topics,
        "incoming_text": combined,
        "history_text": message_service.format_history(history),
        "media_items": media_items,
    }

    try:
        result = await run_turn(thread_id, payload)
    except Exception:  # noqa: BLE001 - a broken turn must not leave the client hanging
        logger.exception("Graph execution failed for conversation %s", conversation["id"])
        await _fail_over_to_human(conversation)
        return

    reply = (result.get("reply") or "").strip()
    if not reply:
        return

    # Last line of defence against sending the same thing twice. Each node has
    # its own repetition check, but none of them can see the case that actually
    # produced it live: two messages typed seconds apart arrive as two turns
    # (the second is folded in mid-turn and processed on the next pass), both
    # retrieve the same records, and both generate the same answer. The client
    # got an identical salary quote at 3:53 and 3:54 and replied "Why you tell
    # me twice".
    #
    # Statements only. A repeated QUESTION is sent: the collector has its own
    # rewrite for that, and if it still comes out the same the client has not
    # answered it yet — going silent there strands the flow, which is worse than
    # asking twice.
    repeats_us = any(
        near_duplicate(reply, line) for line in recent_bot_lines(payload["history_text"], 2)
    )
    if repeats_us and ("?" not in reply or _predates_our_last_reply(conversation, messages)):
        logger.info(
            "Conversation %s: reply repeats our last message — not sending it again",
            conversation["id"],
        )
        return

    try:
        await message_service.send_bot_reply(conversation, reply)
    except WhapiError:
        await _fail_over_to_human(conversation)
        return

    intent_patch = {
        "intent": result.get("intent"),
        "service_type": result.get("service_type"),
    }
    await conversation_service.update(
        conversation["id"], **{k: v for k, v in intent_patch.items() if v}
    )


async def _fail_over_to_human(conversation: dict[str, Any]) -> None:
    """If the bot cannot function, a human must pick the conversation up."""
    try:
        await handover_service.to_human(
            conversation,
            reason=handover_service.REASON_CONFUSED,
            intent=conversation.get("intent"),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failover handover failed for conversation %s", conversation.get("id"))


debouncer = MessageDebouncer(settings.debounce_seconds, process_batch)
