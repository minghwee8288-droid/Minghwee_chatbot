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

# What the collector records for a field the client would not answer. Defined
# up here rather than beside the briefing labels below because Gate has to read
# it too, and a gate is evaluated long before a description is composed.
UNANSWERED = "not provided"


def _mentions(value: str, terms: tuple[str, ...]) -> bool:
    """Whether an answer contains any of these words.

    Anchored on a word boundary rather than a bare substring test: "all" as a
    plain substring matches "small" and "overall", and the care-type gates key
    off exactly that word.
    """
    haystack = (value or "").lower()
    return any(re.search(rf"(?<![a-z]){re.escape(term)}", haystack) for term in terms)


@dataclass(frozen=True)
class Gate:
    """A condition on an earlier answer that decides whether a field is asked.

    Three states, not a boolean, because "not yet known" is not "no":

    - ``open``      the answer is in and it opens this field — ask it.
    - ``closed``    the answer is in and it rules this field out — never ask,
                    and never even offer it to the extractor.
    - ``undecided`` the field it keys off is still unanswered. Not asked yet,
                    but still handed to the extractor, so a client who opens
                    with "I need someone for my 2 kids, 3 and 5" has already
                    answered it by the time we get there and is never asked.

    ``excludes`` is checked first: "no, I don't have pets" contains "have", and
    without it every household in Singapore would be asked what breed.
    """

    field: str
    matches: tuple[str, ...]
    excludes: tuple[str, ...] = ()

    def state(self, collected: dict[str, Any] | None) -> str:
        value = str((collected or {}).get(self.field) or "").strip()
        # UNANSWERED leaves the gate undecided rather than closing it. A client
        # who would not say what kind of care they need has not said there are
        # no children, and if they mention them later the extractor should
        # still be listening for it.
        if not value or value.lower() == UNANSWERED:
            return "undecided"
        if _mentions(value, self.excludes):
            return "closed"
        return "open" if _mentions(value, self.matches) else "closed"


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
    # Which part of the conversation this belongs to. Fields are asked in list
    # order, so a group is simply a run of consecutive fields — the collector
    # uses it to let the model bridge naturally when the subject changes
    # instead of jumping from pets to salary with no seam.
    group: str = ""
    # The answers the form behind this flow accepts. Offered to the client as
    # examples, never read out as a menu, and never used to reject what they
    # actually say — a client who answers "3 bedroom HDB" has answered.
    options: tuple[str, ...] = ()
    # Only asked once an earlier answer opens it. See Gate.
    gate: Gate | None = None


# --- Fields per service ----------------------------------------------------
#
# CONVERSATION_FLOWS §22 for every flow except new_hiring, which now runs the
# full employer qualification set (§22-EXT below). Outside that flow nothing
# beyond the §22 list is asked: salary_expectation and summary are captured only
# if volunteered (§23.4, §23.9), interest_type is inferred from intent and never
# asked (§23.3), and phone, source, tenant, branch, temperature and status are
# already known (§0).
#
# Fields are asked in list order and grouped by topic, so the conversation moves
# through one subject at a time rather than hopping between them. A gated field
# sits directly under the answer that opens it.
#
# NOT collected here, deliberately: the identity block the work permit
# application needs — NRIC/FIN, date of birth, citizenship, passport number,
# spouse identity, residential address, occupation, income bracket and the IC
# numbers of everyone at the address. Every one of those is a Singpass field,
# and Rule 4 plus redact_nric() mean an NRIC typed into WhatsApp reaches the
# ticket as [REDACTED-ID] anyway. PENDING_DOCUMENTATION below puts it on the
# ticket as the salesperson's next step instead.

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


# --- Gates on the employer flow --------------------------------------------
#
# Each keys off an answer already given, so the detail questions only appear
# for the households they apply to: an eldercare-only client is never asked
# how old the children are, and a client with no pets is never asked the breed.
# "all of the above" deliberately opens both care gates.

_CHILDCARE = Gate(
    "requirement",
    ("child", "kid", "baby", "infant", "toddler", "newborn", "son", "daughter",
     "all of the above", "all", "everything", "both"),
)

_ELDERCARE = Gate(
    "requirement",
    ("elder", "senior", "old", "aged", "grandma", "grandmother", "grandpa",
     "grandfather", "parent", "mother", "father", "mum", "mom", "dad",
     "bedridden", "dementia", "stroke",
     "all of the above", "all", "everything", "both"),
)

_HAS_PETS = Gate(
    "pets",
    ("yes", "have", "dog", "cat", "bird", "fish", "hamster", "rabbit", "tortoise"),
    # "no pets" and "no, I don't have any" both contain a match term. Checked
    # first, so a negative answer closes the gate rather than opening it.
    excludes=("no pet", "none", "nope", "don't", "dont", "do not", "haven't", "havent"),
)

_WAS_REFERRED = Gate(
    "referral_source",
    ("referral", "referred", "refer", "friend", "family", "relative", "staff",
     "colleague", "word of mouth", "recommend"),
)


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
        # --- About them ---
        Field("full_name", "name", "May I know your name?", group="who they are"),
        _EMAIL,
        Field(
            "first_time_hire",
            "whether this is their first time hiring",
            "Is this your first time hiring a domestic helper?",
            max_asks=2,
            group="who they are",
            options=("first time hiring", "hired before"),
        ),
        # --- What they need ---
        #
        # requirement gates the two detail questions below it, so it is asked
        # before them and never after. Its own value is also what leads.requirement
        # is written from, which is why the key is unchanged from the four-question
        # version of this flow.
        Field(
            "requirement",
            "type of care",
            "What would you mainly need help with — childcare, eldercare, or general "
            "housework and cooking?",
            group="what they need",
            options=(
                "childcare",
                "eldercare",
                "general housework and cooking",
                "all of the above",
            ),
        ),
        Field(
            "children_detail",
            "the children",
            "How many children, and how old are they?",
            max_asks=2,
            group="what they need",
            gate=_CHILDCARE,
        ),
        Field(
            "elderly_detail",
            "the elderly family member",
            "Could you tell me a bit about them — their age, how mobile they are, and "
            "any medical conditions?",
            max_asks=2,
            group="what they need",
            gate=_ELDERCARE,
        ),
        # --- Their household ---
        Field(
            "household",
            "household size",
            "How many people live in your household?",
            max_asks=2,
            group="their household",
            options=("1-2", "3-4", "5-6", "7 or more"),
        ),
        Field(
            "home_type",
            "type of home",
            "What type of home are you in?",
            max_asks=2,
            group="their household",
            options=(
                "HDB 1-3 room",
                "HDB 4-5 room",
                "HDB Executive",
                "condo",
                "private apartment",
                "landed",
            ),
        ),
        Field(
            "pets",
            "whether they have pets",
            "Do you have any pets at home?",
            max_asks=2,
            group="their household",
            options=("no pets", "yes"),
        ),
        Field(
            "pet_detail",
            "the pets",
            "What kind, and how many?",
            max_asks=2,
            group="their household",
            gate=_HAS_PETS,
        ),
        Field(
            "languages",
            "languages spoken at home",
            "What languages are spoken at home?",
            max_asks=2,
            group="their household",
            options=(
                "English",
                "Mandarin",
                "Malay",
                "Hokkien",
                "Teochew",
                "Cantonese",
                "Tamil",
            ),
        ),
        # --- Their preferences ---
        #
        # Everything from here down is optional bar the nationality: a client who
        # has answered eleven questions has earned the right to stop, and each of
        # these is asked once and let go. They are still worth asking — an agent
        # shortlisting without a budget or a rest-day expectation is guessing.
        Field(
            "preferred_nationality",
            "nationality preference",
            "Do you have a preferred nationality?",
            group="their preferences",
            options=("Filipino", "Indonesian", "Myanmar", "no preference"),
        ),
        Field(
            "hire_source",
            "transfer or new hire",
            "Would you prefer a transfer helper already in Singapore, or a new hire "
            "from overseas?",
            max_asks=1,
            optional=True,
            group="their preferences",
            options=("transfer", "new hire from overseas", "no preference"),
        ),
        Field(
            "cooking",
            "cooking requirements",
            "Any particular cooking you would want her to handle?",
            max_asks=1,
            optional=True,
            group="their preferences",
            options=(
                "Chinese",
                "Malay",
                "Indian",
                "Western",
                "halal kitchen",
                "vegetarian",
                "no specific requirement",
            ),
        ),
        Field(
            "budget",
            "monthly salary budget",
            "Do you have a monthly salary budget in mind?",
            max_asks=1,
            optional=True,
            group="their preferences",
            options=(
                "below $500",
                "$500-600",
                "$600-700",
                "$700-800",
                "above $800",
                "not sure yet",
            ),
        ),
        Field(
            "rest_day",
            "rest day arrangement",
            "How would you want to handle her rest days?",
            max_asks=1,
            optional=True,
            group="their preferences",
            options=("weekly day off", "compensation in lieu", "flexible"),
        ),
        # --- When they need someone ---
        Field(
            "start_timeline",
            "start date",
            "How soon are you hoping to have someone start?",
            max_asks=2,
            group="timing",
            options=(
                "as soon as possible - within 2 weeks",
                "within 1 month",
                "within 2 months",
                "just exploring for now",
            ),
        ),
        # --- Anything else ---
        Field(
            "additional_notes",
            "anything else we should know",
            "Anything else I should note down before I pull this together?",
            max_asks=1,
            optional=True,
            group="anything else",
        ),
        # --- Where they came from ---
        Field(
            "referral_source",
            "how they heard about us",
            "How did you hear about Ming Hwee?",
            max_asks=1,
            optional=True,
            group="how they found us",
            options=(
                "Google search",
                "friend or family referral",
                "social media",
                "returning client",
                "staff referral",
            ),
        ),
        Field(
            "referrer_name",
            "who referred them",
            "Who was it that referred you?",
            max_asks=1,
            optional=True,
            group="how they found us",
            gate=_WAS_REFERRED,
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
    """Every field the flow defines, gated or not.

    The unfiltered list, because that is what the callers who need it want:
    ordering a briefing, labelling a captured value, and deciding whether a
    service collects anything at all. Use applicable_fields() to decide what to
    put to a client.
    """
    return SERVICE_FIELDS.get(service_type or "", [])


def applicable_fields(
    service_type: str | None,
    collected: dict[str, Any] | None,
    *,
    include_undecided: bool = False,
) -> list[Field]:
    """The fields that apply to this client, in asking order.

    Ungated fields always apply. A gated one applies once the answer it keys
    off has opened it.

    ``include_undecided`` keeps the fields whose gate cannot be decided yet.
    That is what the extractor wants and the asker does not: a client who says
    "I need someone for my mum, she's 82 and had a stroke" answers
    elderly_detail in the same breath as requirement, and dropping the field
    from the extraction list on that turn means asking for it again afterwards.
    """
    applicable: list[Field] = []
    for field in fields_for(service_type):
        if field.gate is None:
            applicable.append(field)
            continue
        state = field.gate.state(collected)
        if state == "open" or (include_undecided and state == "undecided"):
            applicable.append(field)
    return applicable


def missing_fields(service_type: str | None, collected: dict[str, Any]) -> list[Field]:
    """What still has to be asked — gated fields that do not apply are not missing."""
    return [
        field
        for field in applicable_fields(service_type, collected)
        if not str(collected.get(field.key) or "").strip()
    ]


def preceding_group(service_type: str | None, collected: dict[str, Any], field: Field) -> str:
    """The topic covered just before this field, or "" if it opens the flow.

    Lets the collector tell the model it is changing the subject, so the move
    from pets to salary gets a seam instead of reading as the next row of a form.
    """
    ordered = applicable_fields(service_type, collected)
    for index, candidate in enumerate(ordered):
        if candidate.key == field.key:
            # Back to the nearest field that belongs to a topic at all. _EMAIL
            # is shared with the candidate flow and carries no group of its
            # own, and reading the blank straight off would announce "who they
            # are" as a new subject on the question right after their name.
            for earlier in reversed(ordered[:index]):
                if earlier.group:
                    return earlier.group
            return ""
    return ""


# What still has to be collected after the chat, and never over it. These are
# the Singpass-backed fields on the work permit application: the employer's
# legal identity, their spouse's, where they live, what they earn and who else
# is at the address. The bot must not ask for any of it (Rule 4), so the ticket
# carries it as the salesperson's next step — otherwise a qualification the bot
# ran perfectly still leaves the agent wondering what is outstanding.
PENDING_DOCUMENTATION = (
    "Singpass identity (full legal name, NRIC/FIN, date of birth, citizenship, "
    "passport if EP holder)",
    "Marital status, and spouse identity and citizenship if married",
    "Residential address and type of residence",
    "Occupation, employer and monthly income bracket (NOA)",
    "Family members at the same address (name, relationship, IC/BC, date of birth)",
)

# Only the employer new-hire flow, which is the one that qualifies a client in
# full and therefore the one where "what is left to do" is a real question.
# direct_hiring and replacement end in the same paperwork, but neither was
# changed here and neither collects enough for the note to add anything an
# agent does not already know — adding it there would just be noise on a
# ticket whose handling nobody has revisited.
_DOCUMENTATION_SERVICES = {"new_hiring"}


def pending_documentation(service_type: str | None) -> tuple[str, ...]:
    return PENDING_DOCUMENTATION if service_type in _DOCUMENTATION_SERVICES else ()


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
    recent = await recent_for_conversation(conversation_id, limit=1)
    return recent[0] if recent else None


async def recent_for_conversation(conversation_id: int, *, limit: int = 3) -> list[dict[str, Any]]:
    """The client's last few tickets, newest first, so the prompt can flag a
    topic that looks different from what they raised before — not just repeat
    the single latest one. ``description`` is what carries the distinguishing
    detail (whose care, which child) that service_type alone cannot.
    """
    try:
        result = await db.execute(
            db.table(TABLE)
            .select("service_type, status, created_at, description")
            .eq("conversation_id", conversation_id)
            .order("created_at", desc=True)
            .limit(limit)
        )
    except Exception:  # noqa: BLE001 - a missing history line must not break the turn
        logger.exception("Could not read recent tickets for conversation %s", conversation_id)
        return []
    return result.data or []


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
    "first_time_hire": "Hired before",
    "children_detail": "Children",
    "elderly_detail": "Elderly",
    "household": "Household size",
    "home_type": "Home",
    "pets": "Pets",
    "pet_detail": "Pet details",
    "languages": "Languages at home",
    "hire_source": "Transfer or new hire",
    "cooking": "Cooking",
    "rest_day": "Rest day",
    "additional_notes": "Also mentioned",
    "referral_source": "Heard about us via",
    "referrer_name": "Referred by",
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
_UNANSWERED = UNANSWERED

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
        # ticket_number is filled in by insert_numbered() below, which re-reads
        # it on a collision — two clients messaging at once otherwise generate
        # the same CB-YYYY-NNNN and the second ticket is silently lost.
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

    async def _insert(row: dict[str, Any]) -> dict[str, Any] | None:
        return await db.insert_numbered(
            TABLE,
            row,
            number_field="ticket_number",
            next_number=next_ticket_number,
        )

    try:
        ticket = await _insert(payload)
    except Exception:  # noqa: BLE001 - a ticket failure must not block the handover
        # cb_tickets.created_lead_id is a foreign key to leads(id), and the graph
        # checkpoint holds that id for the life of the thread. Delete the lead row
        # and every later insert on the conversation violates the constraint —
        # forever, because nothing clears the stale pointer. Live: conversation 36
        # raised ten ticket_raised handovers in twenty minutes, every one with
        # ticket_id NULL and no ticket behind it, because L-2026-0001 had been
        # removed by hand.
        #
        # The ticket is the work item; the lead link is a convenience. Dropping
        # the link to keep the ticket is the right trade in both directions.
        if not created_lead_id:
            logger.exception("Ticket creation failed for conversation %s", conversation["id"])
            return None
        logger.exception(
            "Ticket creation failed for conversation %s — retrying without the "
            "created_lead_id %s, which no longer resolves",
            conversation["id"],
            created_lead_id,
        )
        orphaned = dict(payload)
        orphaned.pop("created_lead_id", None)
        # Keep the trail: the agent should still see which lead this came from,
        # even though the row it pointed at is gone.
        orphaned["captured_info"] = {
            **info,
            "notes": {
                **(info.get("notes") or {}),
                "lead_link_broken": created_lead_id,
            },
        }
        try:
            ticket = await _insert(orphaned)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Ticket creation failed for conversation %s even without the lead link",
                conversation["id"],
            )
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
