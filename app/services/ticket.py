"""Ticket creation.

A ticket is the handover packet: everything the bot managed to collect before
it went silent, so the sales agent opens the thread already knowing what the
client wants.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.db.supabase import db

logger = logging.getLogger(__name__)

TABLE = "cb_tickets"


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    question: str
    # Asked at most this many times before it is recorded as unanswered and the
    # flow moves on. CONVERSATION_FLOWS §23.5: a client who cannot give a case
    # ID must never be blocked or asked again.
    max_asks: int = 3
    # Not needed for the lead. Asked once, dropped without complaint — §23.6,
    # and email specifically, which plenty of clients do not have.
    optional: bool = False


# --- Fields per service ----------------------------------------------------
#
# CONVERSATION_FLOWS §22, verbatim. Nothing outside that list is asked: budget,
# salary_expectation and summary are captured only if volunteered (§23.4, §23.9),
# interest_type is inferred from intent and never asked (§23.3), and phone,
# source, tenant, branch, temperature and status are already known (§0).
#
# The question wording is the document's own.

# Human-readable name for each service, used in prompts and log lines. Owned
# here rather than by a node module: find_merge_candidate() needs it and
# services must not import from graph.nodes (that direction is the other way
# round already — every node imports ticket_service).
SERVICE_LABELS = {
    "new_hiring": "hiring a new helper",
    "candidate_new_hiring": "finding work as a helper",
    "direct_hiring": "direct hire processing",
    "replacement": "replacing their current helper",
    "transfer": "a helper transfer",
    "renewal": "a work permit renewal",
    "home_leave": "home leave for their helper",
    "passport_renewal": "a passport renewal",
    "fee_enquiry": "our fees",
    "salary_enquiry": "helper salary",
    "dispute_salary": "a salary or leave issue",
    "candidate_registration": "registering a helper for placement",
}


def service_label(service_type: str | None) -> str:
    return SERVICE_LABELS.get(service_type or "", "their enquiry")


CANDIDATE_HIRING = "candidate_new_hiring"

# What the classifier calls a helper offering herself for placement. It is an
# intent name, not a flow — resolve_service() maps it onto CANDIDATE_HIRING.
CANDIDATE_REGISTRATION = "candidate_registration"

# Flows a helper drives herself. §5: employers never initiate a transfer.
CANDIDATE_SERVICES = {CANDIDATE_HIRING, "transfer"}

# Asked once at the end of the two lead-creating flows (§2 step 4, §3). Not in
# the §22 list because it is not required — plenty of clients have no email.
_EMAIL = Field(
    "email",
    "email address",
    "Do you have an email I can note down for updates?",
    max_asks=1,
    optional=True,
)

# §5 step 3 read with §0: the number is already known, so the question is
# whether to use it — never "what is your number".
_CONTACT_NUMBER = Field(
    "contact_number",
    "which number to use",
    "Is this the best number to reach you on, or would you prefer we use another one?",
    max_asks=1,
    optional=True,
)

# A case ID the client does not have must not block the flow (§23.5).
def _case_id() -> Field:
    return Field("case_id", "case ID", "May I have your case ID?", max_asks=1, optional=True)


SERVICE_FIELDS: dict[str, list[Field]] = {
    # §2 — employer lead flow.
    #
    # Ordered identity first, then requirements, which is a deviation from the
    # §22 listing. The lead is opened as soon as the client gives their name, so
    # the questions that make them contactable come before the ones that qualify
    # them: a client who stops answering after two messages still leaves a lead
    # sales can ring, rather than a care type attached to nobody.
    "new_hiring": [
        Field("full_name", "name", "May I know your name?"),
        _EMAIL,
        Field("requirement", "type of care", "What kind of care are you looking for?"),
        Field("preferred_nationality", "nationality preference", "Do you have a preferred nationality?"),
    ],
    # §3 — candidate lead flow. Separate from new_hiring: a job seeker is never
    # asked an employer's questions. Same identity-first ordering.
    "candidate_new_hiring": [
        Field("full_name", "name", "May I know your name?"),
        _EMAIL,
        Field("nationality", "nationality", "Which country are you from?"),
    ],
    # §6 — collects nothing at all.
    "direct_hiring": [],
    # §4
    "replacement": [
        _case_id(),
        Field("reason", "reason for the replacement", "What is the reason for the replacement?"),
        Field("timeline", "timeline", "When would you need the replacement by?"),
    ],
    # §5 — candidate flow. Employers never initiate a transfer.
    "transfer": [
        Field("reason", "reason for the transfer", "May I know the reason for the transfer?"),
        Field("helper_name", "name", "May I know your name?"),
        _CONTACT_NUMBER,
    ],
    # §7, §8, §9 — case ID only, nothing else.
    "renewal": [_case_id()],
    "home_leave": [_case_id()],
    "passport_renewal": [_case_id()],
    # §10, §11 — only ever asked for what the client has not already stated.
    "fee_enquiry": [
        Field("nationality", "nationality", "Which nationality are you looking at?"),
        Field("care_type", "type of care", "What kind of care would this be for?"),
    ],
    "salary_enquiry": [
        Field("nationality", "nationality", "Which nationality are you looking at?"),
        Field("care_type", "type of care", "What kind of care would this be for?"),
    ],
    # §12 — the issue itself comes from what they already said.
    "dispute_salary": [
        Field("helper_name", "helper's name", "May I know your helper's name?"),
    ],
    # §13 — immediate escalation, nothing collected.
    "dispute_assault": [],
    # §18 — the bot cannot open the file, so there is nothing to ask.
    "media_received": [],
}

URGENT_SERVICES = {"dispute_assault"}

# Mirrors cb_tkt_service_check. The chatbot recognises two kinds of enquiry the
# portal's schema predates — a candidate offering herself for placement, and a
# bare attachment — and inserting either aborts the row: a candidate offer was
# lost this way, traceback in the log, no work item, client told someone would
# follow up. The constraint is the portal's and is not ours to widen, so the
# ticket is filed under the nearest permitted type with the real one recorded in
# captured_info, where the agent opening it sees it first.
TICKET_SERVICE_TYPES = {
    "new_hiring",
    "direct_hiring",
    "replacement",
    "transfer",
    "renewal",
    "home_leave",
    "passport_renewal",
    "dispute_salary",
    "dispute_assault",
    "fee_enquiry",
    "salary_enquiry",
}

# §3 files the candidate job-seeking ticket under new_hiring. An attachment
# belongs to whatever the thread is already about, and falls back to transfer
# only when the thread has no service yet.
#
# The four informational intents below are not services at all — they are
# what a "couldn't answer this, raising a ticket" escalation carries as its
# topic now that every escalation gets a ticket (so there is something to
# block a follow-up against). A client explicitly asking for a human files
# under whatever bare intent the message itself classified as, for the same
# reason (see ticket_creator's handling of REASON_CLIENT_REQUESTED) — bare
# intents not listed here still fall through to the "transfer" default below.
# The real value survives in captured_info.topic_key/enquiry_type regardless
# of what the portal's column ends up holding.
TICKET_SERVICE_FALLBACK = {
    CANDIDATE_HIRING: "new_hiring",
    CANDIDATE_REGISTRATION: "new_hiring",
    "media_received": "transfer",
    "general_question": "transfer",
    "process_question": "transfer",
    "document_question": "transfer",
    "case_enquiry": "transfer",
}

# Contacts who are offering someone *else* for placement. §15/§16: they raise no
# lead, so they never go down the candidate lead flow.
_THIRD_PARTY_CONTACTS = {"supplier", "partner"}


def _storable_service(service_type: str, conversation: dict[str, Any]) -> tuple[str, str | None]:
    """(value to store, true type when it had to be substituted)."""
    if service_type in TICKET_SERVICE_TYPES:
        return service_type, None
    active = conversation.get("service_type")
    substitute = (
        active
        if active in TICKET_SERVICE_TYPES
        else TICKET_SERVICE_FALLBACK.get(service_type, "transfer")
    )
    logger.warning(
        "cb_tickets does not allow service_type=%r — filing under %r and recording the "
        "real type in captured_info",
        service_type,
        substitute,
    )
    return substitute, service_type


def resolve_service(service_type: str | None, contact_type: str | None) -> str | None:
    """Split the two hiring flows by who is asking (§3, and the routing rule).

    A helper who says "I am looking for work" produces intent new_hiring like
    an employer does. Running her through the employer flow asks her what kind
    of care *she* is looking for, which is why these are separate services.

    "I want job" takes the other route into the same flow: the classifier reads
    it as candidate_registration, which had no field list of its own. Collection
    therefore "finished" on her very first message with nothing captured, the
    thread was ticketed and handed to an agent before she had been asked a single
    question, and no leads_candidate row was written because the lead rules key
    off candidate_new_hiring. Both spellings now resolve to the one §3 flow.
    """
    contact = (contact_type or "").strip()
    if service_type == "new_hiring" and contact == "candidate":
        return CANDIDATE_HIRING
    # A supplier or partner offering another helper keeps the bare registration
    # service: they raise no lead, so there is nothing to collect from them.
    if service_type == CANDIDATE_REGISTRATION and contact not in _THIRD_PARTY_CONTACTS:
        return CANDIDATE_HIRING
    return service_type


def topic_key_for(service_type: str | None, contact_type: str | None, intent: str | None) -> str | None:
    """Which blocked-topic bucket a turn's classification falls into.

    Mirrors exactly what create() stores as captured_info['topic_key'] for a
    ticket raised from the same classification (resolve_service applied first,
    same as the collector does), so a later turn can look itself up in the
    open-tickets map from open_topics_for_conversation() and get acknowledged
    instead of re-answered or re-escalated.
    """
    resolved = resolve_service(service_type, contact_type)
    return resolved or intent


def fields_for(service_type: str | None) -> list[Field]:
    return SERVICE_FIELDS.get(service_type or "", [])


def missing_fields(service_type: str | None, collected: dict[str, Any]) -> list[Field]:
    return [
        field
        for field in fields_for(service_type)
        if not str(collected.get(field.key) or "").strip()
    ]


def priority_for(service_type: str | None) -> str:
    return "urgent" if service_type in URGENT_SERVICES else "normal"


async def next_ticket_number() -> str:
    """CB-YYYY-NNNN, sequential within the calendar year."""
    year = datetime.now(tz=timezone.utc).year
    prefix = f"CB-{year}-"
    try:
        result = await db.execute(
            db.table(TABLE)
            .select("ticket_number")
            .like("ticket_number", f"{prefix}%")
            .order("ticket_number", desc=True)
            .limit(1)
        )
        rows = result.data or []
        if rows:
            last = str(rows[0]["ticket_number"]).rsplit("-", 1)[-1]
            return f"{prefix}{int(last) + 1:04d}"
    except Exception:  # noqa: BLE001
        logger.exception("Could not read the last ticket number — falling back to a timestamp")
        return f"{prefix}{datetime.now(tz=timezone.utc).strftime('%m%d%H%M')}"
    return f"{prefix}0001"


async def last_for_conversation(conversation_id: int) -> dict[str, Any] | None:
    """The client's most recent ticket, for the 'previous enquiry' prompt line."""
    try:
        result = await db.execute(
            db.table(TABLE)
            .select("service_type, status, created_at")
            .eq("conversation_id", conversation_id)
            .order("created_at", desc=True)
            .limit(1)
        )
    except Exception:  # noqa: BLE001 - a missing history line must not break the turn
        logger.exception("Could not read the last ticket for conversation %s", conversation_id)
        return None
    rows = result.data or []
    return rows[0] if rows else None


# Statuses a ticket must be in to still be "live" — eligible to be matched
# against, blocked on, or merged into. resolved/closed are immutable history:
# nothing here ever looks at them, so nothing here can ever touch them.
OPEN_STATUSES = ("open", "in_progress")


async def open_topics_for_conversation(conversation_id: int) -> dict[str, dict[str, Any]]:
    """Live tickets on this conversation, keyed by the topic they block.

    A topic with an entry here is off-limits to the bot until its ticket
    leaves OPEN_STATUSES — the routing check in the graph computes the same
    key fresh each turn via topic_key_for() and looks itself up here. The same
    map doubles as the candidate pool for find_merge_candidate(): every ticket
    here is live and on this conversation, which is exactly what both callers
    need, so one read serves both.
    Fails open (nothing blocked) on a lookup error: a DB hiccup should not be
    able to silently mute the bot on every topic at once.
    """
    try:
        result = await db.execute(
            db.table(TABLE)
            .select("id, ticket_number, service_type, description, status, captured_info, created_at")
            .eq("conversation_id", conversation_id)
            .in_("status", OPEN_STATUSES)
            .order("created_at")
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not read open tickets for conversation %s", conversation_id)
        return {}

    topics: dict[str, dict[str, Any]] = {}
    for row in result.data or []:
        info = row.get("captured_info") or {}
        key = info.get("topic_key") or primary_service_type(row)
        if key:
            topics[key] = row
    return topics


def primary_service_type(row: dict[str, Any] | None) -> str | None:
    """The service a ticket was originally raised for.

    service_type is stored as an array so one ticket can cover more than one
    service after a merge; this is always the first element — the portal-safe
    value _storable_service() chose when the row was created, and the one
    everything that needs a single hashable key (a topic map, a log line)
    should read.
    """
    values = (row or {}).get("service_type") or []
    return values[0] if values else None


def service_types_label(row: dict[str, Any] | None) -> str:
    """Every service a ticket covers, in plain words — 'hiring a new helper and our fees'."""
    values = (row or {}).get("service_type") or []
    labels = [service_label(v) for v in values]
    if not labels:
        return "their enquiry"
    if len(labels) == 1:
        return labels[0]
    return " and ".join(labels)


# How many follow-ups are kept on a ticket. An agent needs the recent gist of
# what the client has said since — not an unbounded transcript growing on a
# JSONB column forever.
_MAX_FOLLOW_UPS = 10


async def add_follow_up(ticket_id: str | None, message: str) -> None:
    """Record a message that arrived on a topic after its ticket was raised.

    Without this, anything the client says while a topic is parked — a chase,
    a correction, a new detail — never reaches the agent opening the ticket;
    it only ever reached the transcript. Never raises: called from the reply
    path, where a logging failure must not cost the client their reply.
    """
    if not ticket_id or not (message or "").strip():
        return
    try:
        row = await db.select_one(TABLE, "captured_info", id=ticket_id)
        if row is None:
            return
        info = dict(row.get("captured_info") or {})
        follow_ups = list(info.get("follow_ups") or [])
        follow_ups.append(
            {"at": datetime.now(tz=timezone.utc).isoformat(), "message": message.strip()[:500]}
        )
        info["follow_ups"] = follow_ups[-_MAX_FOLLOW_UPS:]
        await db.update(TABLE, {"captured_info": info}, id=ticket_id)
    except Exception:  # noqa: BLE001 - a logging failure must not cost the client their reply
        logger.exception("Could not record a follow-up on ticket %s", ticket_id)


def _timestamped(text: str) -> str:
    stamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    return f"[{stamp}] {text.strip()}"[:500]


def initial_description(service_type: str | None, reasoning: str | None, message: str | None) -> str:
    """The first line of a ticket's description — why it was raised.

    Prefers the classifier's own one-line reasoning, already computed for free
    on every turn, over a second LLM call just to restate it. Falls back to the
    client's own words when reasoning is missing (e.g. a ticket-only intent
    that never ran the full classifier reasoning path).
    """
    reason = (reasoning or "").strip() or (message or "").strip()
    label = service_label(service_type)
    if not reason:
        return _timestamped(f"Raised for {label}.")
    return _timestamped(f"{label.capitalize()}: {reason}"[:400])


async def append_description(ticket_id: str, text: str) -> None:
    """Add a dated line to a ticket's description. Never overwrites what is there.

    Read-modify-write on a single text column — fine at this volume (one
    client, one ticket, turns seconds apart) and simpler than a Postgres
    string-concat update. Never raises: a missed description line must not
    cost the client their reply.
    """
    if not ticket_id or not (text or "").strip():
        return
    try:
        row = await db.select_one(TABLE, "description", id=ticket_id)
        if row is None:
            return
        existing = (row.get("description") or "").strip()
        line = _timestamped(text)
        merged = f"{existing}\n{line}" if existing else line
        await db.update(TABLE, {"description": merged}, id=ticket_id)
    except Exception:  # noqa: BLE001
        logger.exception("Could not append description to ticket %s", ticket_id)


async def add_service_type(ticket_id: str, service_type: str | None) -> None:
    """Add a service type to a ticket's service_type array, if it belongs there.

    Silently does nothing for a value outside the portal's fixed list
    (candidate_new_hiring, media_received, a bare intent used as a fallback
    topic) — cb_tkt_service_check only allows the same eleven values a ticket
    could ever be created under, and the real value is already on record in
    captured_info.topic_key regardless of what this array holds. The first
    element is never touched here — it stays whatever the ticket was
    originally raised for (see primary_service_type()).
    """
    if not ticket_id or service_type not in TICKET_SERVICE_TYPES:
        return
    try:
        row = await db.select_one(TABLE, "service_type", id=ticket_id)
        if row is None:
            return
        current = list(row.get("service_type") or [])
        if service_type in current:
            return  # already recorded — do not create duplicates
        await db.update(TABLE, {"service_type": current + [service_type]}, id=ticket_id)
    except Exception:  # noqa: BLE001
        logger.exception("Could not add service_type %r to ticket %s", service_type, ticket_id)


async def merge_into(ticket_id: str, *, reason: str, service_type: str | None) -> None:
    """Fold a new message into an existing ticket instead of raising another.

    The two writes are independent and each already fails safely on its own,
    so a partial failure (description written, service_type array not, or the
    reverse) still leaves the ticket in a valid, useful state rather than
    losing both.
    """
    await append_description(ticket_id, reason)
    await add_service_type(ticket_id, service_type)


async def find_merge_candidate(
    open_tickets: dict[str, dict[str, Any]], *, message: str
) -> dict[str, Any] | None:
    """Whether a new ticket-worthy message actually continues one already open.

    Only called once the deterministic topic_key check has already found no
    exact match — this is for the harder case where the topic looks different
    on the surface (a salary question inside a hiring enquiry, a document
    request for the same case) but is the same underlying issue a human is
    already working. Errs toward "no match": a wrong merge buries one client's
    issue inside an unrelated ticket, silently, where nobody goes looking for
    it. A missed merge just costs one extra ticket, which is recoverable and
    visible.
    """
    candidates = [t for t in open_tickets.values() if t.get("id")]
    if not candidates:
        return None

    from app.graph.llm import complete_json  # here: services must not import graph at module load
    from app.graph.prompts.templates import SAME_ISSUE_SYSTEM, SAME_ISSUE_USER

    lines = []
    for index, ticket in enumerate(candidates, start=1):
        # Every service the ticket already covers, not just the first — a
        # ticket already merged with fee_enquiry is exactly the kind of
        # context that should make the LLM more willing to fold a related
        # follow-up into it rather than raise another.
        label = service_types_label(ticket)
        description = (ticket.get("description") or "").strip() or "(no description yet)"
        lines.append(f"{index}. {label}\n   {description}")

    result = await complete_json(
        SAME_ISSUE_SYSTEM,
        SAME_ISSUE_USER.format(tickets="\n".join(lines), message=message),
        default={},
    )

    try:
        index = int(result.get("ticket_index"))
    except (TypeError, ValueError):
        return None
    try:
        confidence = float(result.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    # A low-confidence "yes" is not enough for something this consequential —
    # see the module docstring: a wrong merge is worse than an extra ticket.
    if not (1 <= index <= len(candidates)) or confidence < 0.6:
        return None

    chosen = candidates[index - 1]
    logger.info(
        "Merge candidate: %r continues ticket %s (confidence %.2f): %s",
        message[:60],
        chosen.get("ticket_number"),
        confidence,
        result.get("reasoning") or "",
    )
    return chosen


async def create(
    *,
    conversation: dict[str, Any],
    service_type: str,
    captured_info: dict[str, Any],
    assigned_agent_id: str | None,
    assignment_rule: str | None,
    created_lead_id: str | None = None,
    description: str = "",
) -> dict[str, Any] | None:
    stored_service, true_service = _storable_service(service_type, conversation)
    info = dict(captured_info or {})
    # The key a follow-up on this exact topic is matched against later — see
    # topic_key_for(). Always the value passed in here, never the (possibly
    # substituted) stored value, so a topic the portal's schema does not know
    # can still be blocked and resolved correctly.
    info.setdefault("topic_key", service_type)
    if true_service:
        # First key, so it heads the agent's view of the ticket.
        info = {"enquiry_type": true_service, **info}

    payload = {
        "tenant_id": settings.tenant_id or None,
        "conversation_id": conversation["id"],
        "ticket_number": await next_ticket_number(),
        # An array so a later merge can add more services onto this same
        # ticket. Seeded with just stored_service, never true_service:
        # _storable_service() only sets true_service when the requested value
        # falls outside TICKET_SERVICE_TYPES, which is exactly what the
        # cb_tkt_service_check containment constraint enforces — a value that
        # needed substituting can never be a member in its own right. The real
        # requested type is still on record, in captured_info.topic_key /
        # enquiry_type above, regardless of what this array can hold.
        "service_type": [stored_service],
        "description": description or "",
        "priority": priority_for(service_type),
        "captured_info": info,
        "employer_id": conversation.get("matched_employer_id"),
        "candidate_id": conversation.get("matched_candidate_id"),
        "supplier_id": conversation.get("matched_supplier_id"),
        "assigned_agent_id": assigned_agent_id,
        "assignment_rule": assignment_rule,
        "status": "open",
    }
    if created_lead_id:
        payload["created_lead_id"] = created_lead_id
    try:
        ticket = await db.insert(TABLE, payload)
    except Exception:  # noqa: BLE001 - a ticket failure must not block the handover
        logger.exception("Ticket creation failed for conversation %s", conversation["id"])
        return None

    if ticket:
        logger.info(
            "Created ticket %s (%s, %s) on conversation %s",
            ticket.get("ticket_number"),
            f"{service_type} as {stored_service}" if true_service else service_type,
            payload["priority"],
            conversation["id"],
        )
    return ticket


def summarize(service_type: str | None, captured: dict[str, Any]) -> str:
    """Human-readable one-liner for logs and agent-facing notes."""
    labels = {field.key: field.label for field in fields_for(service_type)}
    parts = [
        f"{labels.get(key, key)}: {value}"
        for key, value in (captured or {}).items()
        if str(value or "").strip()
    ]
    return "; ".join(parts) or "no details captured"
