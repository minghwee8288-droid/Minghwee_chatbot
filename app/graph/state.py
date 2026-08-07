"""LangGraph conversation state.

The checkpointer persists this per ``langgraph_thread_id``, so multi-turn
information collection survives across separate WhatsApp messages.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

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
}

ENQUIRY_INTENTS = {"fee_enquiry", "salary_enquiry"}

DISPUTE_INTENTS = {"dispute_salary", "dispute_assault"}

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
    # Their most recent cb_tickets row, shown to a returning employer's prompt.
    last_ticket: dict[str, Any] | None
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

    # --- Handover ---
    needs_handover: bool
    handover_reason: str | None
    handover_done: bool
    ticket_id: str | None
    ticket_number: str | None
    assigned_agent_id: str | None
    assignment_rule: str | None
