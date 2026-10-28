"""Read-only input builder: everything a draft is written from.

Reads the conversation row, its messages, the contact and ticket context and
the saved LangGraph state, and assembles the same `state` dict a live turn
hands the graph. Nothing here writes, sends or touches an in-memory cache the
live bot relies on - in particular it never mints a langgraph_thread_id (the
live builder does, webhook.py L1136-L1139) and never runs identify_contact,
which writes the identity onto the conversation row.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.db.supabase import db
from app.services import contact as contact_service
from app.suggest import audience
from app.services import conversation as conversation_service
from app.services import lead as lead_service
from app.services import ticket as ticket_service
from app.utils import normalize_phone, redact_nric

logger = logging.getLogger(__name__)

MESSAGES_TABLE = "wp_chat_messages"
AUTO_REPLY_SENDER = "whatsapp_auto"  # message.AUTO_REPLY_SENDER

CUSTOMER = "CUSTOMER"
CLAIRE = "CLAIRE"
STAFF = "STAFF"

# The labels the bot's prompt and guards read (message.format_history):
# "You:" is Claire, "Agent:" is human staff. The staff member's name is kept
# out of the prefix on purpose - guards test line.startswith("Agent:").
_PREFIX = {CUSTOMER: "Client", CLAIRE: "You", STAFF: "Agent"}


# Why a request gets no drafts (the response's "reason").
NOT_NEWEST_INBOUND = "not_newest_inbound"
ALREADY_ANSWERED = "already_answered"


class ConversationNotFound(LookupError):
    pass


@dataclass
class SuggestContext:
    status: str  # "ok" | "stale" | "skipped"
    conversation: dict[str, Any]
    anchor_message_id: int
    state: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""  # a code: see split_at_anchor and audience.skip_reason


# --- WhatsApp Business auto-replies ------------------------------------------
# The portal stores the handset's greeting and away messages as ordinary
# outbound rows under sent_by "Ming Hwee Agency", not "whatsapp_auto". Live,
# 2026-10-27 (eval, conversation 4633): both arrived 4 seconds after a new
# client's first message, read as a staff reply, and the request came back
# stale. Recognised here without message.is_auto_reply, which caches verdicts
# the live bot relies on.

KNOWN_AUTO_REPLIES = (
    "Thank you for contacting Ming Hwee Agency! Please let us know how we can help you.",
    "Thank You for your message.Our team will reply to you next following working day.",
)
AUTO_REPLY_WINDOW_SECONDS = 10
_AUTO_FLAG = "_auto_reply"


def _squash(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def is_known_auto_reply(body: Any) -> bool:
    """One of the handset's fixed texts (ours, or WHATSAPP_AUTO_REPLY_TEXTS)."""
    squashed = _squash(body)
    if not squashed:
        return False
    known = {_squash(t) for t in KNOWN_AUTO_REPLIES}
    known |= {_squash(t) for t in settings.whatsapp_auto_reply_texts.split("||") if t.strip()}
    return squashed in known


def quick_outbound(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Outbound non-Claire rows sent within AUTO_REPLY_WINDOW_SECONDS after an
    inbound message (rows in time order) - the shape of an automatic reply."""
    out: list[dict[str, Any]] = []
    last_inbound = None
    for row in rows:
        at = _parse_time(row.get("created_at"))
        if row.get("direction") == "inbound":
            last_inbound = at
            continue
        if row.get("is_bot") or row.get("sent_by") in ("bot", AUTO_REPLY_SENDER):
            continue
        if at and last_inbound and 0 <= (at - last_inbound).total_seconds() <= AUTO_REPLY_WINDOW_SECONDS:
            out.append(row)
    return out


async def _sent_elsewhere(body: str, conversation_id: Any) -> bool:
    """The same outbound text in another conversation: a template, not a person
    typing. A GET; compared in Python so it reads nothing it is not shown."""
    result = await db.execute(
        db.table(MESSAGES_TABLE).select("conversation_id")
        .eq("direction", "outbound").eq("body", body).limit(5)
    )
    return any(r.get("conversation_id") not in (None, conversation_id) for r in result.data or [])


async def mark_auto_replies(rows: list[dict[str, Any]], conversation_id: Any) -> None:
    """Mark (in place) the rows that are WhatsApp Business auto-replies: a
    known text anywhere, or an identical outbound text sent within ~10 s of an
    inbound message that was also sent in another conversation."""
    for row in rows:
        if row.get("direction") == "outbound" and is_known_auto_reply(row.get("body")):
            row[_AUTO_FLAG] = True
    for row in quick_outbound(rows):
        body = (row.get("body") or "").strip()
        if not row.get(_AUTO_FLAG) and body and await _sent_elsewhere(body, conversation_id):
            row[_AUTO_FLAG] = True


def label_of(row: dict[str, Any]) -> str | None:
    """CUSTOMER / CLAIRE / STAFF, or None for a row left out of the history.

    `sent_by='bot'` counts as Claire even when `is_bot` is false, so a bot reply
    stored by another system with the wrong flag is still read as hers.
    """
    if row.get("sent_by") == AUTO_REPLY_SENDER or row.get(_AUTO_FLAG):
        return None
    if row.get("direction") == "inbound":
        return CUSTOMER
    if row.get("is_bot") or row.get("sent_by") == "bot":
        return CLAIRE
    return STAFF


def labelled(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows (oldest first) as {id, role, name, body, created_at}, empty bodies dropped."""
    out: list[dict[str, Any]] = []
    for row in rows:
        role = label_of(row)
        body = (row.get("body") or "").strip()
        if role is None or not body:
            continue
        out.append({
            "id": row.get("id"),
            "role": role,
            "name": (row.get("sent_by") or "") if role == STAFF else "",
            "body": body,
            "created_at": row.get("created_at"),
        })
    return out


def render(history: list[dict[str, Any]]) -> str:
    """The transcript in the bot's own format: Client: / You: / Agent:."""
    return "\n".join(f"{_PREFIX[item['role']]}: {item['body']}" for item in history)


async def _recent_rows(conversation_id: int, up_to_id: int | None = None) -> list[dict[str, Any]]:
    """Recent messages, oldest first, NRIC-redacted. A GET only. With
    `up_to_id`, only the messages up to and including that one."""
    query = (
        db.table(MESSAGES_TABLE)
        .select("id, direction, body, is_bot, sent_by, created_at")
        .eq("conversation_id", conversation_id)
    )
    if up_to_id is not None:
        query = query.lte("id", up_to_id)
    # id breaks a tie: two messages stored in the same second came back in
    # either order, so the anchor was sometimes not the "newest" inbound and the
    # same request read as stale on one run and ready on the next (eval, 4615).
    result = await db.execute(
        query.order("created_at", desc=True).order("id", desc=True).limit(settings.history_limit + 50)
    )
    rows = list(reversed(result.data or []))
    for row in rows:
        row["body"] = redact_nric(row.get("body"), context=f"suggest:{conversation_id}")
    return rows


def split_at_anchor(
    rows: list[dict[str, Any]], anchor_message_id: int
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """(stale reason code, earlier history, the client's unanswered messages).

    The incoming batch is every inbound message since our last outbound
    message (staff or Claire), ending at the anchor: "Hi" / "What is the
    transfer fee?" / "For a Filipino helper" is one batch, answered together.
    An auto-reply, or an outbound row with no text (a reaction, a delete
    notice), is not a reply and does not end the batch. Stale when the anchor
    is not the newest inbound message, or a human or Claire has replied after
    it.
    """
    inbound = [r for r in rows if r.get("direction") == "inbound"]
    if not inbound or inbound[-1].get("id") != anchor_message_id:
        return NOT_NEWEST_INBOUND, [], []
    index = next(i for i, r in enumerate(rows) if r.get("id") == anchor_message_id)
    if any(label_of(r) in (CLAIRE, STAFF) and (r.get("body") or "").strip()
           for r in rows[index + 1:]):
        return ALREADY_ANSWERED, [], []
    start = index
    while start > 0 and (
        rows[start - 1].get("direction") == "inbound"
        or label_of(rows[start - 1]) is None
        or not (rows[start - 1].get("body") or "").strip()
    ):
        start -= 1
    batch = [r for r in rows[start:index + 1] if r.get("direction") == "inbound"]
    return "", rows[:start], batch


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _saved_state(thread_id: str | None) -> tuple[dict[str, Any], Any]:
    """The checkpoint's values and timestamp. A read; never creates a thread."""
    if not thread_id:
        return {}, None
    from app.graph.graph import get_graph

    try:
        snapshot = await get_graph().aget_state({"configurable": {"thread_id": thread_id}})
    except Exception:  # noqa: BLE001 - drafts without saved state are still drafts
        logger.exception("suggest: could not read the saved state")
        return {}, None
    return dict(snapshot.values or {}), snapshot.created_at


async def build(
    conversation_id: int, anchor_message_id: int, *, as_of: bool = False
) -> SuggestContext:
    """With `as_of` (scripts/eval_suggest.py only, never the route), the
    conversation is read as it stood at the anchor: later messages are ignored
    and the saved LangGraph state is NOT used, because it holds what was
    collected after the anchor. Contact, ticket and case context is still
    today's - there is no history of it to read."""
    conversation = await conversation_service.get_by_id(conversation_id)
    if not conversation:
        raise ConversationNotFound(conversation_id)

    # A group chat or one of our own numbers is not a client: no drafts, and
    # nothing else is read.
    skip = await audience.skip_reason(conversation)
    if skip:
        return SuggestContext("skipped", conversation, anchor_message_id, reason=skip)

    rows = await _recent_rows(conversation_id, anchor_message_id if as_of else None)
    await mark_auto_replies(rows, conversation_id)
    reason, earlier, batch = split_at_anchor(rows, anchor_message_id)
    if reason:
        return SuggestContext("stale", conversation, anchor_message_id, reason=reason)

    history = labelled(earlier)[-settings.history_limit:]
    # All of the client's unanswered messages, not only the newest: this one
    # string is what the knowledge-base search runs on (rag_retriever
    # _search_query reads incoming_text) and what the prompt answers.
    # (The rows are NRIC-redacted already, in _recent_rows.)
    unanswered = [(r.get("body") or "").strip() for r in batch if (r.get("body") or "").strip()]
    combined = "\n".join(unanswered)
    phone = normalize_phone(conversation.get("customer_number"))

    # ------------------------------------------------------------------
    # Copied from app/api/webhook.py L1137-L1267 at commit c138f91. Keep in sync.
    # Left out: the langgraph_thread_id write, the timing hooks, and
    # last_human_activity_at (it reads the handover log and runs the
    # auto-reply detector, which caches verdicts) - the last human message is
    # taken from the transcript instead.
    # ------------------------------------------------------------------
    contact_type = conversation.get("contact_type") or ""
    employer_id = conversation.get("matched_employer_id")

    async def _nothing(value: Any) -> Any:
        return value

    (
        recent_tickets,
        open_lead,
        prior_hires,
        placed_helper,
        placement_helpers,
        ticket_helpers,
        record_name,
        matched_cases,
        blocked_topics,
        completed_topics,
        candidate_record,
    ) = await asyncio.gather(
        ticket_service.recent_for_conversation(conversation_id, limit=3)
        if contact_type == "employer" else _nothing([]),
        lead_service.find_by_phone(phone)
        if not (employer_id or conversation.get("matched_candidate_id")) else _nothing(None),
        contact_service.count_prior_hires(employer_id),
        contact_service.get_placed_helper(employer_id),
        contact_service.get_placement_helpers(employer_id)
        if contact_type == "employer" else _nothing([]),
        ticket_service.helpers_on_conversation(conversation_id)
        if contact_type == "employer" else _nothing([]),
        contact_service.get_record_name(employer_id),
        contact_service.get_cases(employer_id, phone),
        ticket_service.open_topics_for_conversation(conversation_id),
        ticket_service.completed_topics_for_conversation(conversation_id),
        contact_service.get_candidate_record(conversation.get("matched_candidate_id")),
    )

    from app.api.webhook import merge_known_helpers  # pure

    known_helpers = merge_known_helpers([*placement_helpers, *ticket_helpers])

    hours_since_last_message = None
    last_at = _parse_time(earlier[-1].get("created_at")) if earlier else None
    now = datetime.now(timezone.utc)
    if as_of and batch:
        now = _parse_time(batch[-1].get("created_at")) or now
    if last_at is not None:
        hours_since_last_message = round((now - last_at).total_seconds() / 3600, 2)

    staff_times = [_parse_time(item["created_at"]) for item in history if item["role"] == STAFF]
    staff_times = [t for t in staff_times if t is not None]
    last_human_at = max(staff_times) if staff_times else None

    payload = {
        "conversation_id": conversation["id"],
        "phone": phone,
        "customer_name": conversation.get("customer_name") or "",
        "thread_id": conversation.get("langgraph_thread_id") or "",
        "contact_type": conversation.get("contact_type") or "unknown",
        "matched_employer_id": conversation.get("matched_employer_id"),
        "matched_candidate_id": conversation.get("matched_candidate_id"),
        "matched_supplier_id": conversation.get("matched_supplier_id"),
        "matched_case_id": conversation.get("matched_case_id"),
        "matched_cases": matched_cases,
        "prior_hires": prior_hires,
        "placed_helper": placed_helper,
        "known_helpers": known_helpers,
        "completed_topics": completed_topics,
        "hours_since_last_message": hours_since_last_message,
        "record_name": record_name or "",
        "candidate_record": candidate_record,
        "recent_tickets": recent_tickets,
        "matched_lead_id": (open_lead or {}).get("id"),
        "matched_lead_number": (open_lead or {}).get("lead_number"),
        "lead_kind": (open_lead or {}).get("lead_kind"),
        "matched_lead": open_lead,
        "salesperson_profile_id": None,
        "blocked_topics": blocked_topics,
        "incoming_text": redact_nric(combined, context=f"suggest:{conversation_id}"),
        "history_text": render(history),
        "media_items": [],
    }
    # ------------------------------------------------------------------ end copy

    # Copied from app/graph/graph.py L538-L559 at commit c138f91. Keep in sync.
    # The saved state, the per-turn reset, this turn's payload, then the stale
    # intake reset - exactly the order run_turn builds its input in.
    from app.graph.graph import _TURN_RESET, stale_intake_reset

    values, created_at = (
        ({}, None) if as_of else await _saved_state(conversation.get("langgraph_thread_id"))
    )
    state = {**values, **_TURN_RESET, **payload}
    if values:
        reset = stale_intake_reset(values, created_at, human_since=last_human_at)
        # In the graph a {RESET_KEY: True} value is applied by a reducer and
        # empties the field; merged into a plain dict it would be printed into
        # the prompt as a field, so it is applied here as what it means.
        from app.graph.state import RESET_KEY

        for key, value in reset.items():
            state[key] = {} if isinstance(value, dict) and RESET_KEY in value else value
    # One entry per message, for the prompt (prompt.unanswered_block). Not a
    # graph field: nothing in the bot reads it.
    state["suggest_unanswered"] = unanswered

    return SuggestContext(
        "ok", conversation, anchor_message_id, state=state, payload=payload, history=history
    )
