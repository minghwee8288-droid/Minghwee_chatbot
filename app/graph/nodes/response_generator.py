"""Node 3 — write the reply the client sees."""

from __future__ import annotations

import logging
import re
from typing import Any

from app.config import settings
from app.graph.guards import (
    clamp_reply,
    holding_reply,
    is_degenerate,
    leaks_internal_reasoning,
    looks_like_document,
    recent_bot_lines,
    strip_handover_talk,
    strip_meta_commentary,
    strip_repeated_opener,
    ungrounded_figures,
)
from app.graph.llm import complete
from app.graph.prompts.system import IDENTITY, build_system_prompt
from app.graph.prompts.templates import (
    AGENCY_INFO_INSTRUCTION,
    CANDIDATE_INSTRUCTION,
    CASE_INSTRUCTION,
    CONTACT_DISCOVERY_INSTRUCTION,
    HANDOVER_TOKEN,
    RESPONDER_INSTRUCTION,
)
from app.graph.state import (
    AGENCY_INFO_INTENT,
    CANDIDATE_INTENT,
    MEDIA_INTENT,
    TICKET_ONLY_INTENTS,
    ConversationState,
)
from app.services import handover as handover_service

logger = logging.getLogger(__name__)

# The "I'll check" line is no longer a constant — it is drawn per turn from
# guards.HOLDING_REPLIES so the same wording does not go out twice in a row.
PROMPT_FOR_QUESTION = "Sure, go ahead. What would you like to know?"

# Turns with no reason to match the knowledge base, exempt from the weak-
# retrieval guard. agency_info is answered from the identity block at the top of
# the system prompt; attachments and candidate offers never match anything.
NO_RETRIEVAL_EXPECTED = {
    "greeting",
    "smalltalk",
    "other",
    AGENCY_INFO_INTENT,
    MEDIA_INTENT,
    CANDIDATE_INTENT,
}

# Kept for the weak-retrieval test, which still only applies to real questions.
CHITCHAT_INTENTS = NO_RETRIEVAL_EXPECTED

# A message that announces a question without asking one. Handing these to an
# agent abandons the client before they have said what they want, so the bot
# invites the question instead.
#
# This is a blocklist, not a keyword whitelist: the whitelist it replaced did
# not contain "provide", so "please provide your introduction" was treated as
# nothing-asked and answered with a canned "Sure, go ahead."
_GREETING = re.compile(
    r"^\W*(hi+|hey+|hello+|yo|halo|helo|greetings|"
    r"good\s+(morning|afternoon|evening|day))\b(\s+there)?[\s,.!\-]*",
    re.IGNORECASE,
)
_ANNOUNCEMENT = re.compile(
    r"^\W*("
    r"(i\s+)?(have|had|got)\s+(a|one|some)\s+(question|enquiry|inquiry|doubt)|"
    r"(can|may|could)\s+i\s+ask(\s+you)?(\s+(something|a\s+question))?|"
    r"(i\s+)?need\s+(some\s+)?help|"
    r"are\s+you\s+(there|around|online)|"
    r"anyone\s+there"
    r")[\s\W]*$",
    re.IGNORECASE,
)
# Beyond this length it is a real message whatever the wording.
_NO_REQUEST_MAX_WORDS = 8


def has_no_request(text: str) -> bool:
    """Whether the client has greeted or announced a question without asking it."""
    body = (text or "").strip()
    if not body or len(body.split()) > _NO_REQUEST_MAX_WORDS:
        return False
    rest = _GREETING.sub("", body, count=1).strip()
    if not rest:  # nothing but a greeting
        return True
    return bool(_ANNOUNCEMENT.match(rest))


def asks_something(text: str) -> bool:
    """Whether the client has actually put something to us."""
    return bool((text or "").strip()) and not has_no_request(text)


# Turns where establishing the contact type would be intrusive or beside the
# point — somebody reporting harm is not asked whether they are an employer.
_DISCOVERY_EXEMPT = {"dispute_assault", "dispute_salary", MEDIA_INTENT, AGENCY_INFO_INTENT}


def needs_contact_discovery(state: ConversationState, intent: str) -> bool:
    """Unrecognised number whose contact type is still unreadable."""
    if intent in _DISCOVERY_EXEMPT:
        return False
    if (state.get("contact_type") or "unknown") != "unknown":
        return False  # the database already told us
    if state.get("detected_contact_type"):
        return False  # this turn read it well enough to proceed
    # Nothing said yet is handled by the ordinary greeting path.
    return asks_something(state.get("incoming_text", ""))


def _clean(reply: str) -> tuple[str, bool]:
    """Strip the handover token and report whether it was present."""
    text = (reply or "").strip()
    needs_human = HANDOVER_TOKEN in text
    if needs_human:
        text = text.replace(HANDOVER_TOKEN, "").strip()
    # The model occasionally wraps WhatsApp text in quotes or markdown.
    text = text.strip('"').strip()
    if text.startswith("**") and text.endswith("**"):
        text = text.strip("*").strip()
    # "(Note: This is a helper/candidate registration request which requires
    # human handling...)" was sent to a client verbatim.
    text = strip_meta_commentary(text)
    # Collapse the email-style paragraph breaks it sometimes falls back into.
    text = re.sub(r"\n{2,}", "\n", text).strip()
    # Handovers are silent — the client must never be told someone else is coming.
    text = strip_handover_talk(text)
    return text, needs_human


async def response_generator(state: ConversationState) -> dict[str, Any]:
    intent = state.get("intent") or "other"
    # Varied, so the four separate ways this turn can end up saying "I'll check"
    # do not all say it in the same words twice running.
    fallback = holding_reply(state.get("history_text") or "")
    instruction_template = {
        "case_enquiry": CASE_INSTRUCTION,
        AGENCY_INFO_INTENT: AGENCY_INFO_INSTRUCTION,
        CANDIDATE_INTENT: CANDIDATE_INSTRUCTION,
    }.get(intent, RESPONDER_INSTRUCTION)

    # An unrecognised number whose type we still cannot read: find out who they
    # are before running any workflow, since the workflow depends on it.
    if needs_contact_discovery(state, intent):
        instruction_template = CONTACT_DISCOVERY_INSTRUCTION
        logger.info(
            "Conversation %s: contact type still unclear — asking rather than guessing",
            state.get("conversation_id"),
        )

    instruction = instruction_template.format(handover_token=HANDOVER_TOKEN)

    system_prompt = build_system_prompt(
        dict(state),
        rag_context=state.get("rag_context", ""),
        extra_instructions=instruction,
    )
    user_prompt = (
        f"Conversation so far:\n{state.get('history_text') or '(this is the first message)'}\n\n"
        f"Client's latest message(s):\n{state.get('incoming_text', '')}\n\n"
        "Your reply:"
    )

    try:
        # Not lower than this: at 0.2 the model degenerated into repeating its
        # own instructions. Figures are kept honest by the grounding guard
        # below, not by a low temperature.
        raw = await complete(system_prompt, user_prompt, temperature=0.45, max_tokens=120)
    except Exception:  # noqa: BLE001 - never leave the client without an answer
        logger.exception("Response generation failed on conversation %s", state.get("conversation_id"))
        return {
            "reply": fallback,
            "needs_handover": True,
            "handover_reason": handover_service.REASON_CONFUSED,
        }

    reply, token_present = _clean(raw)

    # Never send a reply that has collapsed into repetition or is echoing the
    # prompt back at the client.
    if is_degenerate(reply):
        logger.error(
            "Conversation %s: discarded degenerate reply (%d chars): %r",
            state.get("conversation_id"),
            len(reply),
            reply[:200],
        )
        reply = ""
    elif leaks_internal_reasoning(reply):
        # The model narrated its own reasoning to the client instead of answering
        # — e.g. reading the empty-records instruction back at them. Drop it; the
        # holding line below says the same thing in a way meant for a person.
        logger.error(
            "Conversation %s: discarded reply that leaked internal reasoning: %r",
            state.get("conversation_id"),
            reply[:200],
        )
        reply = ""
    else:
        # Two sentences. A third almost always turns out to be padding — the
        # explanation after the answer, or the offer of further help — and it is
        # what makes the thread read as a chatbot rather than a consultant.
        reply = clamp_reply(reply, max_sentences=2)

    if not reply:
        reply = fallback
        token_present = True

    # The client has not asked anything yet — invite them to, rather than
    # handing an empty enquiry to an agent and going silent. An attachment or a
    # candidate offer is not "nothing asked": a human still has to act on it.
    if (
        token_present
        and intent not in TICKET_ONLY_INTENTS
        and not asks_something(state.get("incoming_text", ""))
    ):
        logger.info(
            "Conversation %s: nothing asked yet, inviting the question instead of handing over",
            state.get("conversation_id"),
        )
        return {"reply": PROMPT_FOR_QUESTION, "needs_handover": False}

    # Refuse to send a figure that is not in the records, whatever the model says.
    #
    # IDENTITY counts as a record: the licence number and the years in business
    # are our own facts, stated in the prompt. Without it the guard read the
    # 6072 in "MOM Licence 12C6072" as an invented figure and replaced a correct
    # answer to "what services do you provide" with a handover.
    context = state.get("rag_context", "")
    invented = ungrounded_figures(reply, context, IDENTITY, state.get("incoming_text", ""))
    if invented:
        logger.warning(
            "Conversation %s: reply quoted ungrounded figure(s) %s — replaced with a handover",
            state.get("conversation_id"),
            invented,
        )
        reply = fallback
        token_present = True

    # A markdown-formatted answer is the model writing from its own knowledge
    # rather than from the records — it produced a plumber directory complete
    # with headings during testing.
    if looks_like_document(reply):
        logger.warning(
            "Conversation %s: reply was formatted like a document — replaced with a handover",
            state.get("conversation_id"),
        )
        reply = fallback
        token_present = True

    # Nothing relevant in the knowledge base at all — do not let the model
    # improvise. The bar here is rag_soft_floor, not rag_confidence_floor: a
    # partial match is still our own material and should be used, which is the
    # whole point of having a knowledge base. Conversational turns are exempt:
    # "ok thanks", "are you a bot" and similar never match anything, and handing
    # those to an agent would push most conversations to a human within a
    # message or two. Real unanswered questions are still caught by the handover
    # token, and no figure survives the grounding guard above regardless.
    weak_retrieval = (
        intent not in CHITCHAT_INTENTS
        and asks_something(state.get("incoming_text", ""))
        and not state.get("case_summary")
        and float(state.get("rag_best_score") or 0.0) < settings.rag_soft_floor
    )
    # Acknowledged, then passed to a person: the bot cannot open an attachment,
    # and registering a helper needs documents and verification.
    ticket_only = intent in TICKET_ONLY_INTENTS
    needs_handover = (
        bool(state.get("needs_handover")) or token_present or weak_retrieval or ticket_only
    )

    if weak_retrieval:
        # Nothing in the records supported this, so whatever the model wrote came
        # from its own knowledge. Handing over while still sending that answer
        # would defeat the point of handing over.
        logger.info(
            "Weak retrieval (best=%.2f) on conversation %s — replacing the reply and handing over",
            float(state.get("rag_best_score") or 0.0),
            state.get("conversation_id"),
        )
        reply = fallback

    reply = strip_repeated_opener(reply, *recent_bot_lines(state.get("history_text", "")))

    update: dict[str, Any] = {"reply": reply, "needs_handover": needs_handover}
    if needs_handover and not state.get("handover_reason"):
        update["handover_reason"] = {
            MEDIA_INTENT: handover_service.REASON_MEDIA,
            CANDIDATE_INTENT: handover_service.REASON_CANDIDATE,
        }.get(intent, handover_service.REASON_CONFUSED)
    return update
