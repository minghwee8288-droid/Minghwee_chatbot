"""LangGraph conversation state.

The checkpointer persists this per ``langgraph_thread_id``, so multi-turn
information collection survives across separate WhatsApp messages.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, TypedDict

logger = logging.getLogger(__name__)

# --- Intents ---------------------------------------------------------------

# Questions about Ming Hwee itself — who we are, what we do, where we are.
# Answered from the identity block in the system prompt, not from the knowledge
# base: "what services do you provide" retrieves at 0.38, below the confidence
# floor, so the weak-retrieval guard was replacing a perfectly good answer with
# "let me check with the team".
AGENCY_INFO_INTENT = "agency_info"

INFORMATIONAL_INTENTS = {
    "general_question",
    "process_question",
    "document_question",
    "greeting",
    "smalltalk",
    AGENCY_INFO_INTENT,
}

SERVICE_INTENTS = {
    "new_hiring",
    "direct_hiring",
    "replacement",
    "transfer",
    "renewal",
    "home_leave",
    "passport_renewal",
    "insurance",
}

ENQUIRY_INTENTS = {"fee_enquiry", "salary_enquiry"}

DISPUTE_INTENTS = {"dispute_salary", "dispute_assault"}

# Questions whose answer lives in the knowledge base rather than on a ticket.
#
# These are the one thing a parked topic must NOT silence. "How much is your
# agency fee", "what documents do I need" and "how long does it take" are not
# chasing the case a human is working — they are general questions we can answer
# from our own material, and answering them costs the agent nothing. Left in the
# blocked-topic branch they were logged and ignored: three questions in a row,
# three silences, which is what the client sees as the bot having died.
KB_QUESTION_INTENTS = {
    "fee_enquiry",
    "salary_enquiry",
    "document_question",
    "process_question",
    "general_question",
}

CASE_INTENT = "case_enquiry"

# An attachment with no question attached: acknowledged, then ticketed and
# handed to a human, who is the only one who can actually open it.
MEDIA_INTENT = "media_received"

# A helper offering herself for placement, or a supplier/agent offering one.
# Both are real Ming Hwee business, and neither is an employer flow — before
# this existed a job seeker was pushed through direct_hiring and asked for the
# employer's name and the helper's passport number.
CANDIDATE_INTENT = "candidate_registration"

ALL_INTENTS = (
    INFORMATIONAL_INTENTS
    | SERVICE_INTENTS
    | ENQUIRY_INTENTS
    | DISPUTE_INTENTS
    | {CASE_INTENT, MEDIA_INTENT, CANDIDATE_INTENT, "other"}
)

# Intents that map 1:1 onto a cb_tickets.service_type
TICKETABLE_INTENTS = (
    SERVICE_INTENTS
    | ENQUIRY_INTENTS
    | {"dispute_salary", "dispute_assault", MEDIA_INTENT, CANDIDATE_INTENT}
)

# Ticketed straight away rather than collected against: the bot acknowledges,
# raises the ticket and hands over without asking a list of questions.
# candidate_registration used to be here; it now collects the handful of
# entry-level details a leads_candidate row needs.
TICKET_ONLY_INTENTS = {MEDIA_INTENT}

# Contact types we can raise a lead for. Suppliers and partners have no lead
# table yet — they are ticketed and handed over as before.
LEAD_CONTACT_TYPES = {"employer", "candidate"}


def _last_write(_current: Any, new: Any) -> Any:
    return new


RESET_KEY = "__reset__"


def _merge_counts(current: dict | None, new: dict | None) -> dict:
    """Per-field ask counters, added rather than replaced.

    Drives two things the collector cannot do without memory across turns:
    knowing whether a field was ever actually put to the client, and noticing
    that it has now been put to them three times running with no usable answer.
    """
    incoming = dict(new or {})
    reset = bool(incoming.pop(RESET_KEY, False))
    merged = {} if reset else dict(current or {})
    for key, value in incoming.items():
        merged[key] = merged.get(key, 0) + int(value or 0)
    return merged


def _merge_dict(current: dict | None, new: dict | None) -> dict:
    """Collected info accumulates across turns instead of being replaced.

    A node can force a clean slate by including ``__reset__``. The info
    collector uses that when the client changes service: answers given for a
    hiring enquiry are not answers to a work-permit renewal, and carrying them
    over puts a previous service's fields into the new service's ticket.
    """
    incoming = dict(new or {})
    reset = bool(incoming.pop(RESET_KEY, False))
    merged = {} if reset else dict(current or {})
    for key, value in incoming.items():
        if value in (None, "", []):
            continue
        merged[key] = value
    return merged


def effective_contact_type(state: "ConversationState") -> str:
    """Who we are talking to, weighing the record against what this message says.

    A contact_type backed by an employers/candidates/profiles row is a master
    record and always wins — one message that sounds otherwise does not turn an
    employer into a job seeker.

    A contact_type with no master record behind it came from a previous lead,
    which is a far softer thing: it says only what that number enquired about
    last time. Trusting it as absolutely as a master record meant a number that
    once registered a helper stayed a candidate forever, so an employer writing
    "I am looking for a maid" had a leads_candidate row opened for them and was
    asked which country they were from.
    """
    stored = (state.get("contact_type") or "").strip().lower()
    detected = (state.get("detected_contact_type") or "").strip().lower()

    has_master_record = any(
        state.get(key)
        for key in ("matched_employer_id", "matched_candidate_id", "matched_supplier_id")
    )
    if stored and stored != "unknown" and has_master_record:
        return stored

    # An OPEN lead on this number sits between the two. It is not a master
    # record, but it is not one message's reading either: it was written when
    # this enquiry started and it is what the current flow is being collected
    # against. Letting a fresh per-message detection override it is how a live
    # conversation flipped employer -> candidate -> employer on consecutive
    # turns, and the collector — correctly, for a real service change — threw
    # away the name and email it had already gathered and asked for them again.
    #
    # Only while the lead is open, and only against a detection that disagrees:
    # a client who genuinely comes back as the other party gets a new lead, and
    # that new kind then wins here in its turn.
    lead_kind = (state.get("lead_kind") or "").strip().lower()
    if lead_kind and detected and detected != lead_kind:
        logger.info(
            "Conversation %s reads as a %s this message, but the open lead on this "
            "number is a %s one — keeping %s so the flow is not reset mid-enquiry",
            state.get("conversation_id"),
            detected,
            lead_kind,
            lead_kind,
        )
        return lead_kind

    return detected or stored or "unknown"


def conversation_ref(state: "ConversationState") -> dict[str, Any]:
    """The subset of the conversation row the ticket/handover services need."""
    return {
        "id": state.get("conversation_id"),
        "customer_number": state.get("phone"),
        "customer_name": state.get("customer_name"),
        "matched_employer_id": state.get("matched_employer_id"),
        "matched_candidate_id": state.get("matched_candidate_id"),
        "matched_supplier_id": state.get("matched_supplier_id"),
        "matched_case_id": state.get("matched_case_id"),
    }


class ConversationState(TypedDict, total=False):
    # --- Conversation identity ---
    conversation_id: int
    phone: str
    customer_name: str
    thread_id: str

    # --- Who we are talking to ---
    contact_type: str
    matched_employer_id: str | None
    matched_candidate_id: str | None
    matched_supplier_id: str | None
    matched_case_id: str | None
    salesperson_profile_id: str | None
    # Non-archived `placements` rows for this employer: how many helpers we have
    # actually placed with them. 0 means "we have no record", NOT "first time" —
    # an unknown number is also 0. Only a positive count is evidence.
    prior_hires: int
    # The one helper our records place with this employer, when there is exactly
    # one and the row names her: {"helper_name": ..., "nationality": ...}. None
    # whenever there is any doubt. There is NO passport data in the database to
    # go with it — see contact.get_placed_helper.
    placed_helper: dict[str, Any] | None
    # Their last few cb_tickets rows (newest first), shown to a returning
    # employer's prompt so it can flag a topic that looks different from what
    # they raised before instead of silently starting a fresh collection.
    recent_tickets: list[dict[str, Any]] | None
    # An open leads/leads_candidate row this number already matched.
    matched_lead_id: str | None
    matched_lead_number: str | None
    lead_kind: str | None
    # The whole lead row, so the collector can fill in what we were told last
    # time instead of asking for it again. Read per turn, never persisted on
    # wp_chat_conversations — it has no column for it.
    matched_lead: dict[str, Any] | None
    # A lead THIS conversation opened, on the turn the client gave their name.
    # Kept apart from matched_lead_id because only this one may be written to:
    # §1B leaves a lead from an earlier enquiry to whoever has worked it since.
    created_lead_id: str | None
    created_lead_number: str | None
    # Which table created_lead_id lives on. Deliberately NOT lead_kind: that one
    # describes the MATCHED lead and the webhook rewrites it every turn from a
    # fresh find_by_phone(), so it is None whenever no earlier lead exists — which
    # is exactly the case where we opened one ourselves. _finish_lead() read
    # lead_kind, got None, and fell back to 'employer'; on a candidate enquiry
    # that sent the update to `leads` with a leads_candidate id, where it matched
    # nothing and said so in no log.
    created_lead_kind: str | None
    # What the classifier thinks this contact is, used only while the database
    # says 'unknown'.
    detected_contact_type: str | None

    # --- This turn's input ---
    incoming_text: str
    history_text: str
    # Attachments in this batch, so the media ticket can name what to open.
    media_items: list[dict[str, Any]]

    # --- Classification ---
    intent: str
    service_type: str | None
    intent_confidence: float
    intent_reasoning: str

    # --- Retrieval ---
    rag_matches: list[dict[str, Any]]
    rag_context: str
    rag_best_score: float
    case_summary: dict[str, Any] | None

    # --- Topic-scoped blocking ---
    # Open tickets on this conversation, keyed by topic — read fresh from
    # cb_tickets every turn (see open_topics_for_conversation), never
    # persisted on the checkpoint. A topic with an entry here is off-limits:
    # the bot acknowledges a message about it instead of answering or
    # re-collecting, until the ticket closes. Every other topic on the same
    # conversation is unaffected.
    blocked_topics: dict[str, dict[str, Any]]

    # --- Information collection ---
    collected_info: Annotated[dict[str, Any], _merge_dict]
    # Which service collected_info belongs to, so a switch can clear it.
    collected_service: str | None
    missing_field_keys: list[str]
    # How many times each field has actually been asked, cleared on a service
    # switch alongside collected_info.
    asked_field_counts: Annotated[dict[str, int], _merge_counts]
    info_complete: bool

    # --- Output ---
    reply: str
    reply_sent: bool
    # This turn needs no reply at all — an acknowledgement or a sign-off, which
    # a person reads and does not answer. Set by the classifier, honoured by the
    # router, and delivered as an empty reply (the webhook sends nothing).
    suppress_reply: bool

    # --- Handover ---
    needs_handover: bool
    handover_reason: str | None
    handover_done: bool
    ticket_id: str | None
    ticket_number: str | None
    assigned_agent_id: str | None
    assignment_rule: str | None
