"""Node 1 — classify what the client wants."""

from __future__ import annotations

import logging
import re
from typing import Any

from app.graph.closure import needs_no_reply
from app.graph.llm import complete_json
from app.graph.prompts.templates import (
    ASSAULT_VERIFY_SYSTEM,
    ASSAULT_VERIFY_USER,
    INTENT_SYSTEM,
    INTENT_USER,
)
from app.graph.state import (
    ALL_INTENTS,
    MEDIA_INTENT,
    TICKET_ONLY_INTENTS,
    TICKETABLE_INTENTS,
    ConversationState,
)

logger = logging.getLogger(__name__)

# Belt-and-braces safety net: these never depend on the model behaving.
ASSAULT_PATTERNS = re.compile(
    r"\b(assault|abuse[ds]?|abusive|molest|rape|beat(en|ing)?|hit me|hit her|hitting|"
    r"slap(ped|ping)?|punch(ed|ing)?|kick(ed|ing)?|choke|strangl|threaten(ed|ing)?|"
    r"violent|violence|harass(ed|ment)?|not safe|unsafe|injur(y|ed)|bleeding|"
    r"police report|run(?:ning)? away)\b",
    re.IGNORECASE,
)

HUMAN_REQUEST_PATTERNS = re.compile(
    r"\b(speak|talk|chat)\s+(to|with)\s+(a\s+)?(human|person|real person|someone|"
    r"agent|consultant|manager|boss|thomas)\b|\bcall me\b|\bwho am i (talking|speaking) to\b",
    re.IGNORECASE,
)


# Enquiries that ride along inside a bigger service request rather than
# replacing it — a new-hiring client always asks about price at some point.
SOFT_SERVICES = {"fee_enquiry", "salary_enquiry"}


# DeepSeek regularly invents intent names — 'request for elderly care services',
# 'clarification', 'provide information'. Dropping those to 'other' sends a real
# hiring enquiry down the informational path, so nothing is ever collected.
# Salvage them by keyword before giving up.
_INTENT_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Before the hiring patterns: "I have a helper looking for work" contains
    # 'helper' and would otherwise be salvaged as new_hiring.
    (re.compile(
        r"candidate|job\s*seeker|register.*(helper|candidate|myself)|"
        r"(looking|search\w*)\s+for\s+(a\s+)?(job|work|employer)|"
        r"want[s]?\s+(a\s+)?(job|work)|find\w*\s+(a\s+)?job",
        re.I,
     ), "candidate_registration"),
    (re.compile(r"introduc|about\s+(you|your|the\s+agency)|who\s+are\s+you", re.I), "agency_info"),
    (re.compile(r"replace|replacement", re.I), "replacement"),
    (re.compile(r"renew", re.I), "renewal"),
    (re.compile(r"transfer", re.I), "transfer"),
    (re.compile(r"home\s*leave|vacation|going home", re.I), "home_leave"),
    (re.compile(r"passport", re.I), "passport_renewal"),
    (re.compile(r"direct\s*hir", re.I), "direct_hiring"),
    (re.compile(r"hir(e|ing)|new helper|maid|worker|domestic|elderly|childcare|kids|care", re.I), "new_hiring"),
    (re.compile(r"fee|cost|price|charge|package", re.I), "fee_enquiry"),
    (re.compile(r"salary|wage|pay", re.I), "salary_enquiry"),
    (re.compile(r"assault|abuse|violen|unsafe", re.I), "dispute_assault"),
    (re.compile(r"dispute|complain|grievance", re.I), "dispute_salary"),
    (re.compile(r"document|form|paperwork", re.I), "document_question"),
    (re.compile(r"process|procedure|step|timeline|how long", re.I), "process_question"),
    (re.compile(r"case|status|progress|update", re.I), "case_enquiry"),
    (re.compile(r"greet|hello|hi\b", re.I), "greeting"),
    (re.compile(r"clarif|information|assistance|enquir|inquir|question", re.I), "general_question"),
)


def _coerce_intent(raw_intent: str, raw_service: Any) -> str:
    """Map whatever the model returned onto an intent we actually route on."""
    intent = (raw_intent or "").strip().lower().replace(" ", "_")
    if intent in ALL_INTENTS:
        return intent

    # The service field is often right even when the intent name is invented.
    if isinstance(raw_service, str) and raw_service in TICKETABLE_INTENTS:
        logger.info("Unknown intent %r — using service_type %r instead", raw_intent, raw_service)
        return raw_service

    for pattern, mapped in _INTENT_ALIASES:
        if pattern.search(raw_intent or ""):
            logger.info("Unknown intent %r — mapped to %r", raw_intent, mapped)
            return mapped

    logger.warning("Unknown intent %r from classifier — treating as other", raw_intent)
    return "other"


def _normalise_service(intent: str, raw_service: Any) -> str | None:
    service = raw_service if isinstance(raw_service, str) else None
    if service in TICKETABLE_INTENTS:
        return service
    if intent in TICKETABLE_INTENTS:
        return intent
    return None


# A wider net than ASSAULT_PATTERNS, used only by the out-of-hours override.
# There, recall matters more than precision: every hit is verified by a second
# LLM check before anything happens, so a false positive costs one API call,
# while a miss costs a client reporting harm at 2am and hearing nothing back.
# ASSAULT_PATTERNS stays tight because it force-escalates without verification.
HARM_SIGNALS = re.compile(
    r"\b(scared|afraid|frighten(ed|ing)?|terrified|panic|"
    r"lock(ed|ing)?\s+(her|herself|him|himself|the\s+door)|trapped|escape|"
    r"danger(ous)?|emergency|hurt(ing)?|harm(ed|ing)?|"
    r"forc(e|ed|ing)|not\s+allowed|won'?t\s+let\s+her|refus(e|ed|ing)\s+to\s+let|"
    r"took\s+her\s+passport|confiscat|"
    r"no\s+food|not\s+enough\s+food|starv(e|ed|ing)|no\s+sleep|"
    r"shout(ing|ed)?|scream(ing|ed)?|yell(ing|ed)?|"
    r"bruis(e|ed|es)|blood|wound|suicide|kill|"
    r"police)\b",
    re.IGNORECASE,
)


def matches_assault_keywords(text: str) -> bool:
    """Cheap first pass for the safety net outside the graph."""
    text = text or ""
    return bool(ASSAULT_PATTERNS.search(text) or HARM_SIGNALS.search(text))


# Contact types the classifier may report. 'unclear' is a real answer — it is
# what triggers the discovery question instead of a guess.
_CONTACT_TYPES = {"employer", "candidate", "supplier", "partner"}

# Intents that settle the question on their own, whatever the model reported:
# nobody asks to renew their helper's work permit except an employer.
#
# new_hiring is deliberately NOT here. It is the one intent both sides raise —
# "I am looking for a maid" and "I am looking for work" both land on it, which
# is the whole reason resolve_service() splits the two hiring flows. Forcing it
# to employer would send every job seeker down the employer's questions.
#
# transfer is NOT here either, for the same reason. "Transfer" is raised by both
# sides: a helper already in Singapore looking for a new employer, AND an
# employer wanting to hire a transfer helper. Forcing it to employer sent a real
# helper down the wrong flow; forcing it either way is wrong. Let the model
# decide, fall back to employer (most inbound is), and let resolve_service()
# split the flow — an employer's "transfer" is really a hiring enquiry.
_CONTACT_BY_INTENT = {
    "direct_hiring": "employer",
    "replacement": "employer",
    "renewal": "employer",
    "home_leave": "employer",
    "passport_renewal": "employer",
    "case_enquiry": "employer",
}

# Where the model could not tell, but the intent leans one way on balance.
# Applied only after the model has been given its say.
_CONTACT_FALLBACK_BY_INTENT = {
    "new_hiring": "employer",
    "transfer": "employer",
}


def _detected_contact_type(intent: str, raw: Any) -> str | None:
    """Who the classifier thinks this is, or None while it cannot tell."""
    by_intent = _CONTACT_BY_INTENT.get(intent)
    if by_intent:
        return by_intent
    value = str(raw or "").strip().lower()
    if value in _CONTACT_TYPES:
        return value
    # 'unclear' on a hiring enquiry: far more of those are employers, and the
    # employer flow asks nothing a helper would find strange ("what kind of care
    # are you looking for") until the contact type has had another turn to firm
    # up.
    return _CONTACT_FALLBACK_BY_INTENT.get(intent)


async def confirm_harm(message: str) -> bool:
    """Second opinion on an assault classification the keywords did not catch.

    Fails open: if the check errors, the escalation stands. A false escalation
    is recoverable; a missed one is not.
    """
    result = await complete_json(
        ASSAULT_VERIFY_SYSTEM,
        ASSAULT_VERIFY_USER.format(message=message),
        default={"harm": True},
    )
    harm = result.get("harm")
    if isinstance(harm, str):
        harm = harm.strip().lower() in {"true", "yes", "1"}
    return bool(harm)


async def intent_classifier(state: ConversationState) -> dict[str, Any]:
    message = state.get("incoming_text", "")
    active_service = state.get("service_type")

    # Hard override — an assault signal is never left to model confidence.
    if ASSAULT_PATTERNS.search(message):
        logger.warning(
            "Assault keyword matched on conversation %s — forcing escalation",
            state.get("conversation_id"),
        )
        return {
            "intent": "dispute_assault",
            "service_type": "dispute_assault",
            "intent_confidence": 1.0,
            "intent_reasoning": "safety keyword match",
        }

    # "ok", "thanks", "noted 👍", "I'll get back to you" — read it, do not answer
    # it. Checked after the assault override and before the classifier call, so
    # a sign-off costs nothing and can never be turned into a topic, a ticket or
    # a reply. The active service, collected fields and ask counts all live on
    # the checkpoint and are untouched, so the flow resumes exactly where it was
    # the moment the client says something real again.
    if needs_no_reply(message, history_text=state.get("history_text") or ""):
        logger.info(
            "Conversation %s: %r needs no reply — staying silent",
            state.get("conversation_id"),
            message[:60],
        )
        return {"suppress_reply": True, "reply": ""}

    user_prompt = INTENT_USER.format(
        active_service=active_service or "none",
        contact_type=state.get("contact_type") or "unknown",
        history=state.get("history_text") or "(no earlier messages)",
        message=message,
    )
    fallback = {"intent": "other", "service_type": active_service, "confidence": 0.0}
    result = await complete_json(INTENT_SYSTEM, user_prompt, default=fallback)

    # An empty intent means the call fell apart, not that the message was
    # unclassifiable. Retrying once is far cheaper than mis-routing the turn:
    # a hiring enquiry dropped to 'other' collects nothing at all.
    if not str(result.get("intent") or "").strip():
        logger.info("Classifier returned no intent — retrying once")
        result = await complete_json(
            INTENT_SYSTEM, user_prompt, temperature=0.2, default=fallback
        )

    intent = _coerce_intent(str(result.get("intent") or ""), result.get("service_type"))

    # An escalation pings an admin and sends the client an alarming "call 999"
    # message, so it must never rest on one judgement. The keyword override
    # above already handled the clear cases; if the model reached this on its
    # own, confirm it with a separate, narrowly-scoped question. A prompt
    # injection classified as dispute_assault at confidence 1.00 is what made
    # this necessary.
    if intent == "dispute_assault":
        if not await confirm_harm(message):
            logger.warning(
                "Model claimed assault but verification disagreed — treating as other: %r",
                message[:160],
            )
            intent = "other"
            result["service_type"] = None

    service_type = _normalise_service(intent, result.get("service_type"))

    # These are their own ticket rather than something to collect against. The
    # classifier is told to return service_type null for them, so set it here
    # rather than trusting the model to name the ticket type.
    if intent in TICKET_ONLY_INTENTS:
        service_type = intent
        if intent == MEDIA_INTENT and active_service:
            # The enquiry it interrupted is context the agent needs — the
            # conversation goes to them, so nothing else carries it over.
            result["reasoning"] = (
                f"Attachment received during a {active_service} enquiry. "
                f"{result.get('reasoning') or ''}".strip()
            )

    # An in-flight service survives an ambiguous follow-up message.
    if active_service and intent in {"other", "greeting", "smalltalk"}:
        service_type = active_service
        intent = active_service if active_service in ALL_INTENTS else intent

    # A price question inside a live enquiry is part of that enquiry, not a new
    # one. Without this, "how much do you charge" halfway through new_hiring
    # switches the service to fee_enquiry, whose two fields are already filled,
    # so collection ends early and the ticket is filed under the wrong service
    # with the remaining requirements never asked for.
    #
    # Only while that service is still IN PROGRESS, though. Once its ticket is
    # raised and the topic is parked, "sticking" to it does the opposite of
    # what this rule is for: it drags a genuinely new, unrelated question back
    # into the blocked topic, where blocked_topic_responder's chase-dedup then
    # swallows it as a repeat of an old chase. Live, on conversation 3766,
    # "I want salary — know the salary expectation of indo workers" — asked
    # right after the bot itself had said "which service can I help you
    # with?" — got glued back onto the already-raised 'renewal' ticket this
    # way and was silently dropped.
    if (
        active_service
        and active_service not in SOFT_SERVICES
        and service_type in SOFT_SERVICES
        and active_service not in (state.get("blocked_topics") or {})
    ):
        logger.info(
            "Keeping active service %s despite a %s question", active_service, service_type
        )
        service_type = active_service

    try:
        confidence = float(result.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0

    update: dict[str, Any] = {
        "intent": intent,
        "service_type": service_type,
        "intent_confidence": confidence,
        "intent_reasoning": str(result.get("reasoning") or "")[:300],
    }

    # Read every time there is no master record behind the stored type, not just
    # when it says 'unknown'. A type inherited from an old lead is only what this
    # number asked about last time, and effective_contact_type() weighs the two;
    # skipping the read here left it nothing to weigh.
    has_master_record = any(
        state.get(key)
        for key in ("matched_employer_id", "matched_candidate_id", "matched_supplier_id")
    )
    if not has_master_record:
        detected = _detected_contact_type(intent, result.get("contact_type"))
        if detected:
            update["detected_contact_type"] = detected
            stored = (state.get("contact_type") or "unknown").strip()
            if stored not in {"", "unknown", detected}:
                logger.info(
                    "Conversation %s reads as a %s this time, not the %s on the row "
                    "(no master record — that came from an earlier lead)",
                    state.get("conversation_id"),
                    detected,
                    stored,
                )
            else:
                logger.info(
                    "Conversation %s: unrecognised number reads as a %s",
                    state.get("conversation_id"),
                    detected,
                )

    if HUMAN_REQUEST_PATTERNS.search(message):
        # Honour it silently: no announcement, the bot simply steps aside.
        update["needs_handover"] = True
        update["handover_reason"] = "client_requested"

    logger.info(
        "Conversation %s classified as intent=%s service=%s confidence=%.2f",
        state.get("conversation_id"),
        intent,
        service_type,
        confidence,
    )
    return update
