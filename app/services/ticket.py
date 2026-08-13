"""Ticket creation.

A ticket is the handover packet: everything the bot managed to collect before
it went silent, so the sales agent opens the thread already knowing what the
client wants.
"""

from __future__ import annotations

import logging
import re
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
    # Not services, but they reach service_label() as topic keys — a ticket
    # raised for an unanswerable question, and the "Also asked about" line on a
    # merge. Without them the line read "Also asked about: their enquiry".
    "document_question": "required documents",
    "process_question": "how the process works",
    "general_question": "a general question",
    "case_enquiry": "their case status",
    "media_received": "a document they sent",
    "dispute_assault": "a safety report",
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
    #
    # The list past the §22 minimum is what an agent actually needs before they
    # can quote or shortlist anybody. With only four questions the bot was
    # closing an enquiry after two exchanges and handing over a ticket that said
    # little more than "wants a helper" — the agent then had to start the
    # conversation over. Everything added here is asked once and let go if it is
    # not answered, so a client in a hurry is still never blocked.
    "new_hiring": [
        Field("full_name", "name", "May I know your name?"),
        _EMAIL,
        Field("requirement", "type of care", "What kind of care are you looking for?"),
        Field("preferred_nationality", "nationality preference", "Do you have a preferred nationality?"),
        Field(
            "household",
            "household",
            "Who would she be looking after at home — how many are in the household?",
            max_asks=2,
        ),
        Field(
            "start_timeline",
            "start date",
            "When are you hoping to have someone start?",
            max_asks=2,
        ),
        Field(
            "budget",
            "monthly budget",
            "Do you have a monthly salary budget in mind?",
            max_asks=1,
            optional=True,
        ),
    ],
    # §3 — candidate lead flow. Separate from new_hiring: a job seeker is never
    # asked an employer's questions. Same identity-first ordering.
    "candidate_new_hiring": [
        Field("full_name", "name", "May I know your name?"),
        _EMAIL,
        Field("nationality", "nationality", "Which country are you from?"),
        Field(
            "experience",
            "experience",
            "How many years of experience do you have as a helper?",
            max_asks=2,
        ),
        Field(
            "current_location",
            "where they are now",
            "Are you currently in Singapore or still overseas?",
            max_asks=2,
        ),
        Field("availability", "availability", "When would you be able to start?", max_asks=2),
    ],
    # §6 — collects nothing at all.
    "direct_hiring": [],
    # §4
    "replacement": [
        _case_id(),
        Field("helper_name", "helper's name", "May I know your current helper's name?", max_asks=2),
        Field("reason", "reason for the replacement", "What is the reason for the replacement?"),
        Field("timeline", "timeline", "When would you need the replacement by?"),
    ],
    # §5 — candidate flow. Employers never initiate a transfer.
    "transfer": [
        Field("helper_name", "name", "May I know your name?"),
        Field("reason", "reason for the transfer", "May I know the reason for the transfer?"),
        Field(
            "permit_expiry",
            "work permit expiry",
            "When does your current work permit expire?",
            max_asks=2,
        ),
        Field(
            "employer_consent",
            "current employer's consent",
            "Has your current employer agreed to the transfer?",
            max_asks=2,
        ),
        Field(
            "availability",
            "availability",
            "When would you be free to start with a new employer?",
            max_asks=2,
        ),
        _CONTACT_NUMBER,
    ],
    # §7, §8, §9 — a case ID alone identifies the case, but only if the client
    # has one to hand. These three are the flows where that most often fails
    # (a helper rarely knows her employer's case reference), which is how a
    # ticket ended up reading "passport renewal" and nothing else. The helper's
    # name and the relevant expiry date let an agent find the case either way.
    "renewal": [
        _case_id(),
        Field("helper_name", "helper's name", "May I know your helper's name?", max_asks=2),
        Field(
            "permit_expiry",
            "work permit expiry",
            "When does her work permit expire?",
            max_asks=2,
        ),
    ],
    "home_leave": [
        _case_id(),
        Field("helper_name", "helper's name", "May I know your helper's name?", max_asks=2),
        Field(
            "leave_dates",
            "travel dates",
            "When is she planning to travel, and when would she be back?",
            max_asks=2,
        ),
    ],
    "passport_renewal": [
        _case_id(),
        Field("helper_name", "helper's name", "May I know your helper's name?", max_asks=2),
        Field(
            "nationality",
            "nationality",
            "Which country is her passport from?",
            max_asks=2,
        ),
        Field(
            "passport_expiry",
            "passport expiry",
            "When does her current passport expire?",
            max_asks=2,
        ),
    ],
    # §10, §11 — only ever asked for what the client has not already stated.
    "fee_enquiry": [
        Field("nationality", "nationality", "Which nationality are you looking at?"),
        Field("care_type", "type of care", "What kind of care would this be for?"),
    ],
    "salary_enquiry": [
        Field("nationality", "nationality", "Which nationality are you looking at?"),
        Field("care_type", "type of care", "What kind of care would this be for?"),
    ],
    # §12 — the issue itself comes from what they already said, but an agent
    # picking up a pay complaint needs to know whose pay and for how long
    # before they can do anything about it.
    "dispute_salary": [
        Field("helper_name", "helper's name", "May I know your helper's name?"),
        Field(
            "issue_detail",
            "what happened",
            "Can you tell me a bit more about what has happened?",
            max_asks=2,
        ),
        Field(
            "issue_duration",
            "how long",
            "How long has this been going on?",
            max_asks=2,
        ),
    ],
    # §13 — immediate escalation, nothing collected.
    "dispute_assault": [],
    # §18 — the bot cannot open the file, so there is nothing to ask.
    "media_received": [],
}

# Services that open at the top priority level rather than the default one.
HIGH_PRIORITY_SERVICES = {"dispute_assault"}

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
    """The level a new ticket opens at.

    Set once, here, at insert. The portal owns it from then on: an agent can
    re-triage a ticket to any of high/medium/low and nothing in the bot
    overwrites that. cb_tkt_priority_check permits both these values and the
    'urgent'/'normal' they replaced while the migration is in flight.
    """
    return "high" if service_type in HIGH_PRIORITY_SERVICES else "medium"


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
        primary = info.get("topic_key") or primary_service_type(row)
        # also_topics are the ones folded in later (see add_topic_key). They
        # block exactly as the ticket's own topic does — the whole point of
        # merging is that one human is now handling all of it.
        for key in [primary, *(info.get("also_topics") or [])]:
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


# --- Description -----------------------------------------------------------
#
# A description is a briefing, not a transcript. It is composed from what we
# know about the enquiry — never assembled out of raw client messages, which is
# what produced the eight-line timestamped logs of "Also: 6756453423" that made
# the column unreadable. Three parts, in this order:
#
#   Employer needs help hiring a new helper.     <- one summary sentence
#   Care type: elderly care                      <- the captured facts, labelled
#   Case ID: 6756453423
#   Also asked about: our fees.                  <- anything folded in later
#
# Nothing here is timestamped: cb_tickets.created_at/updated_at already carry
# the times, and wp_chat_messages carries the conversation itself.

# What the ticket is, in one sentence, per service.
SERVICE_SUMMARIES = {
    "new_hiring": "needs help hiring a new helper",
    "candidate_new_hiring": "is looking for work as a helper",
    "candidate_registration": "is offering a helper for placement",
    "direct_hiring": "wants us to process a helper they have already chosen",
    "replacement": "wants to replace their current helper",
    "transfer": "needs help with a work permit transfer",
    "renewal": "needs a work permit renewal",
    "home_leave": "is arranging home leave for their helper",
    "passport_renewal": "needs help renewing a helper's passport",
    "fee_enquiry": "is asking about our agency fees",
    "salary_enquiry": "is asking about helper salary",
    "dispute_salary": "has raised a salary or leave issue",
    "dispute_assault": "has reported a safety incident",
    "media_received": "sent an attachment for us to look at",
    "case_enquiry": "is asking about their existing case",
    "document_question": "is asking which documents are needed",
    "process_question": "is asking how the process works",
    "general_question": "asked something the bot could not answer",
}

# Who is asking, in the words an agent would use.
_CONTACT_NOUNS = {
    "employer": "Employer",
    "candidate": "Helper",
    "supplier": "Agent",
    "partner": "Partner",
}

# Labels for the captured values that go on the description. The Field.label
# wording is written to sit inside a spoken question ("May I have your case
# ID?"), which reads oddly as a column heading, so the ones an agent scans for
# get their own heading here and everything else falls back to the field label.
_DETAIL_LABELS = {
    "case_id": "Case ID",
    "full_name": "Name",
    "helper_name": "Helper",
    "email": "Email",
    "contact_number": "Contact number",
    "nationality": "Nationality",
    "preferred_nationality": "Preferred nationality",
    "requirement": "Care type",
    "care_type": "Care type",
    "household": "Household",
    "start_timeline": "Start date",
    "timeline": "Needed by",
    "budget": "Budget",
    "experience": "Experience",
    "current_location": "Currently in",
    "availability": "Available from",
    "permit_expiry": "Work permit expires",
    "passport_expiry": "Passport expires",
    "leave_dates": "Travel dates",
    "employer_consent": "Employer consent",
    "reason": "Reason",
    "issue_detail": "Issue",
    "issue_duration": "Ongoing for",
    "client_message": "In their words",
    "lead_number": "Lead",
    "attachments": "Attachments",
}

# Recorded by the collector for a field the client would not answer. It belongs
# in captured_info, where the gap is honest, but not on a briefing line.
_UNANSWERED = "not provided"

_MAX_DETAIL_VALUE = 120
_ALSO_PREFIX = "Also asked about:"


def _one_line(text: str | None) -> str:
    """Collapse a value to a single short line."""
    body = re.sub(r"\s+", " ", (text or "").strip())
    if len(body) <= _MAX_DETAIL_VALUE:
        return body
    return body[:_MAX_DETAIL_VALUE].rsplit(" ", 1)[0] + "…"


def _detail_label(service_type: str | None, key: str) -> str:
    if key in _DETAIL_LABELS:
        return _DETAIL_LABELS[key]
    for field in fields_for(service_type):
        if field.key == key:
            return field.label[:1].upper() + field.label[1:]
    return key.replace("_", " ")[:1].upper() + key.replace("_", " ")[1:]


def summary_line(service_type: str | None, contact_type: str | None) -> str:
    """'Employer needs help hiring a new helper.' — the top line of a ticket."""
    who = _CONTACT_NOUNS.get((contact_type or "").strip(), "Client")
    what = SERVICE_SUMMARIES.get(service_type or "")
    if not what:
        what = f"needs help with {service_label(service_type)}"
    return f"{who} {what}."


def compose_description(
    service_type: str | None,
    contact_type: str | None,
    captured: dict[str, Any] | None = None,
    *,
    also: list[str] | tuple[str, ...] = (),
) -> str:
    """The whole description, built from what we know rather than what was typed.

    ``captured`` is the flat collector bag (not the structured ticket shape), so
    this can be called before structure_captured() splits it.
    """
    lines = [summary_line(service_type, contact_type)]

    # In the order the flow asks for them, so the briefing reads the way the
    # conversation went. Anything captured outside the flow's own fields (a
    # media summary, the client's own words) follows.
    flat = {k: v for k, v in (captured or {}).items() if str(v or "").strip()}
    ordered = [f.key for f in fields_for(service_type) if f.key in flat]
    extras = [k for k in flat if k in _DETAIL_LABELS and k not in ordered]
    for key in [*ordered, *extras]:
        value = _one_line(str(flat[key]))
        if not value or value.lower() == _UNANSWERED:
            continue
        lines.append(f"{_detail_label(service_type, key)}: {value}")

    also_line = _also_line(also)
    if also_line:
        lines.append(also_line)
    return "\n".join(lines)


def _also_line(also: list[str] | tuple[str, ...]) -> str:
    labels = []
    for key in also or ():
        label = service_label(key)
        if label not in labels:
            labels.append(label)
    if not labels:
        return ""
    return f"{_ALSO_PREFIX} {', '.join(labels)}."


def initial_description(
    service_type: str | None,
    contact_type: str | None = None,
    captured: dict[str, Any] | None = None,
) -> str:
    """The description a ticket is created with."""
    return compose_description(service_type, contact_type, captured)


async def set_also_asked(ticket_id: str, also: list[str]) -> None:
    """Rewrite the 'Also asked about:' line to cover everything folded in so far.

    Rewritten rather than appended: the list is derived from
    captured_info.also_topics, which is already the complete set, so appending
    would just restate it once per turn. Never raises — a missing briefing line
    must not cost the client their reply.
    """
    if not ticket_id:
        return
    try:
        row = await db.select_one(TABLE, "description", id=ticket_id)
        if row is None:
            return
        lines = [
            ln
            for ln in (row.get("description") or "").splitlines()
            if ln.strip() and not ln.startswith(_ALSO_PREFIX)
        ]
        line = _also_line(also)
        if line:
            lines.append(line)
        await db.update(TABLE, {"description": "\n".join(lines)}, id=ticket_id)
    except Exception:  # noqa: BLE001
        logger.exception("Could not update the 'also asked' line on ticket %s", ticket_id)


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


async def add_topic_key(ticket_id: str, topic_key: str | None) -> list[str] | None:
    """Record a second topic this ticket now also covers.

    open_topics_for_conversation() blocks a topic by looking its key up against
    open tickets. Without this, a topic folded into an existing ticket was
    never blocked: only the key the ticket was RAISED under is in the map, so
    the next message about the merged topic missed the block, ran the whole
    collection flow again, and came back round to the merge — appending to the
    same ticket on every turn. Recording it here means the second topic behaves
    exactly like the first: acknowledged once, then handled by the human.

    Unlike service_type, this takes any value — it is our own key, not the
    portal's constrained column, so candidate_new_hiring and the bare-intent
    fallback topics are all valid here.

    Returns the full also_topics list after the write, so the caller can rebuild
    the description's "Also asked about" line from it, or None when there was
    nothing to record.
    """
    if not ticket_id or not topic_key:
        return None
    try:
        row = await db.select_one(TABLE, "captured_info", id=ticket_id)
        if row is None:
            return None
        info = dict(row.get("captured_info") or {})
        if topic_key == info.get("topic_key"):
            return None  # this is what the ticket was raised for
        also = list(info.get("also_topics") or [])
        if topic_key in also:
            return also
        info["also_topics"] = also = also + [topic_key]
        await db.update(TABLE, {"captured_info": info}, id=ticket_id)
        return also
    except Exception:  # noqa: BLE001
        logger.exception("Could not record topic %r on ticket %s", topic_key, ticket_id)
        return None


async def merge_into(ticket_id: str, *, service_type: str | None) -> None:
    """Fold a related enquiry into an existing ticket instead of raising another.

    Deliberately takes no free text. It used to take the client's raw message
    and append it to the description, which is how the column filled up with
    "Also: 6756453423" and "Also: Dhdpyglxi" — fragments an agent cannot act
    on. What a merge actually adds is *which service* is now also covered; the
    message itself is already on the ticket as a follow-up (add_follow_up) and
    in the conversation transcript.

    The writes are independent and each fails safely on its own, so a partial
    failure still leaves the ticket valid rather than losing all of them.
    """
    await add_service_type(ticket_id, service_type)
    also = await add_topic_key(ticket_id, service_type)
    if also:
        await set_also_asked(ticket_id, also)


# Topics that always get their own ticket and never absorb another. A safety
# report or a formal complaint folded into a hiring enquiry is a complaint
# nobody sees; both directions of that merge are barred here.
ALWAYS_SEPARATE = {"dispute_assault", "dispute_salary"}

# Which distinct piece of work each service belongs to.
#
# Two services in the same family are the same job seen from different angles
# and belong on one ticket. Two in different families are different jobs and
# must never share one, however close together they were mentioned: a transfer
# and a passport renewal are handled by different people, at different times,
# against different MOM submissions — merging them produced a single ticket
# reading ['new_hiring','fee_enquiry','transfer','passport_renewal'], which is
# four jobs and no owner for any of them.
#
# Anything not listed is its own family, so a new service type is separate by
# default rather than silently absorbed.
SERVICE_FAMILIES = {
    "new_hiring": "hiring",
    "direct_hiring": "hiring",
    CANDIDATE_HIRING: "candidate",
    CANDIDATE_REGISTRATION: "candidate",
}

# Topics that are not a piece of work in their own right — they qualify one.
# "How much is it?" and "what documents do I need?" asked during a hiring
# enquiry are part of that enquiry; asked cold, with nothing open, they raise
# their own ticket like anything else.
MODIFIER_TOPICS = {
    "fee_enquiry",
    "salary_enquiry",
    "general_question",
    "process_question",
    "document_question",
    "case_enquiry",
    "media_received",
}


def family_for(service_type: str | None) -> str:
    """The piece of work a service belongs to. Unlisted services are their own."""
    return SERVICE_FAMILIES.get(service_type or "", service_type or "")


def _families_covered(ticket: dict[str, Any]) -> set[str]:
    """Every family a ticket already covers — its own topic and anything merged.

    Read from captured_info, never from the service_type array: that column is
    constrained to the portal's eleven values, so a topic outside them is filed
    under a substitute (TICKET_SERVICE_FALLBACK sends an unanswerable general
    question to 'transfer'). Matching on the substitute would fold a real
    transfer request into an unrelated ticket. topic_key and also_topics hold
    the true values, which is exactly why they exist.
    """
    info = ticket.get("captured_info") or {}
    keys = [
        info.get("topic_key") or primary_service_type(ticket),
        *(info.get("also_topics") or []),
    ]
    return {family_for(key) for key in keys if key}


async def pick_ticket_to_update(
    open_tickets: dict[str, dict[str, Any]], *, message: str, service_type: str | None
) -> dict[str, Any] | None:
    """The live ticket this message belongs on, or None to raise a new one.

    The rule is the service family, decided in code rather than by a model:

      * a dispute or safety report always gets its own row (ALWAYS_SEPARATE);
      * a modifier topic — a fee, salary, document or process question, an
        attachment — lands on the open ticket it is qualifying;
      * anything else merges only into a ticket already covering its own
        family, and otherwise raises a new one.

    This replaces a merge-by-default rule that asked an LLM whether two topics
    were "the same issue" and folded them together whenever it was unsure. That
    fixed the duplicate-ticket-per-turn problem and created a worse one: four
    unrelated services on a single row. Families keep the deduplication (a fee
    question during a hiring enquiry still updates the hiring ticket) without
    ever merging two genuinely different jobs.
    """
    if service_type in ALWAYS_SEPARATE:
        return None

    candidates = [
        t
        for t in open_tickets.values()
        if t.get("id") and primary_service_type(t) not in ALWAYS_SEPARATE
    ]
    if not candidates:
        return None

    if service_type in MODIFIER_TOPICS:
        # Qualifies whatever is open. With one ticket there is nothing to decide;
        # with several, ask which enquiry it belongs to and fall back to the one
        # being worked most recently.
        if len(candidates) == 1:
            return candidates[0]
        return await find_merge_candidate(
            {str(i): t for i, t in enumerate(candidates)}, message=message
        ) or candidates[-1]

    family = family_for(service_type)
    same_family = [t for t in candidates if family in _families_covered(t)]
    if not same_family:
        logger.info(
            "%r is a separate piece of work from the %d open ticket(s) — raising its own",
            service_type,
            len(candidates),
        )
        return None
    return same_family[-1]


async def find_merge_candidate(
    open_tickets: dict[str, dict[str, Any]], *, message: str
) -> dict[str, Any] | None:
    """Which open ticket a new ticket-worthy message continues, if any.

    The judgement itself, without the default: returns the ticket the model
    picked, or None when it picked none or was not confident enough. Callers
    decide what None means — pick_ticket_to_update() treats it as "raise a
    separate ticket", which is the only place that decision is made.
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

    # The bar sits at "more likely than not". It used to be 0.6, back when a
    # missed merge cost one extra ticket; now that a conversation is meant to
    # carry one live ticket, a hedged "probably the same" is the answer we want
    # to act on. Disputes and safety reports never reach here at all — see
    # ALWAYS_SEPARATE.
    if not (1 <= index <= len(candidates)) or confidence < 0.5:
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


# Flat keys that describe who is writing rather than what they asked for.
_CONTACT_KEYS = {
    "contact_type": "type",
    "whatsapp_name": "name",
    "whatsapp_number": "whatsapp_number",
}


def structure_captured(flat: dict[str, Any], *, detail_keys: set[str]) -> dict[str, Any]:
    """Group a flat bag of captured values into the shape a ticket stores.

        {"contact": {...}, "details": {...}, "notes": {...}}

    The collector and the lead writer both work in flat key/value pairs, and
    that is the right shape for them — a lead row has one column per answer.
    A ticket is read by a person, though, and a single flat dict mixing
    'preferred_nationality' with 'bot_note' and 'whatsapp_number' gives them no
    idea which parts are the client's answers and which are our own plumbing.
    The split happens here, at the last moment, so nothing upstream has to care.

    topic_key and enquiry_type are deliberately NOT folded in: create() puts
    them at the top level, where open_topics_for_conversation() looks for them.
    """
    contact: dict[str, Any] = {}
    details: dict[str, Any] = {}
    notes: dict[str, Any] = {}
    for key, value in (flat or {}).items():
        if value in (None, "", []):
            continue
        if key in _CONTACT_KEYS:
            contact[_CONTACT_KEYS[key]] = value
        elif key in detail_keys:
            details[key] = value
        else:
            notes[key] = value

    structured: dict[str, Any] = {}
    if contact:
        structured["contact"] = contact
    if details:
        structured["details"] = details
    if notes:
        structured["notes"] = notes
    return structured


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
