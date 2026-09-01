"""Node — acknowledge a message about a topic that already has an open ticket.

The bot keeps working the rest of the conversation; this is the one topic it
must not answer, re-collect, or re-escalate until the ticket closes. Anything
new the client says about it is logged onto the ticket, so the agent opening it
sees the whole picture, not just what was known the moment it was raised.

The reassurance is said ONCE. It used to go out on every message, which is how
a client acknowledging three things in a row collected three identical "still
checking on that for you" replies — the single most robotic thing the bot did
in testing. After the first one the topic is logged in silence, and the client
hears from us again only if they actually ask something.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.graph.guards import (
    clamp_reply,
    is_degenerate,
    looks_like_document,
    mentions_handover,
    near_duplicate,
    recent_bot_lines,
    strip_handover_talk,
    strip_meta_commentary,
    strip_repeated_opener,
    ungrounded_figures,
)
from app.config import settings
from app.graph.closure import is_closing, is_pure_acknowledgement
from app.graph.llm import complete
from app.graph.prompts.system import build_system_prompt
from app.graph.prompts.templates import (
    BLOCKED_TOPIC_ANSWER_INSTRUCTION,
    BLOCKED_TOPIC_INSTRUCTION,
)
from app.graph.state import (
    CASE_INTENT,
    KB_QUESTION_INTENTS,
    ConversationState,
    effective_contact_type,
)
from app.services import ticket as ticket_service
from app.services.ticket import TICKET_SERVICE_TYPES, service_label

logger = logging.getLogger(__name__)

FALLBACK_REPLY = (
    "A live agent has this one and will connect with you shortly. "
    "Anything else I can help you with in the meantime?"
)

# A reassurance we have already sent. Matched against our own recent messages so
# the same holding line never goes out twice running, whatever wording the model
# picked for it this time.
_HOLDING_LINE = re.compile(
    r"\b(still\s+(?:checking|working|on\s+it)|checking\s+on\s+(?:that|this|it)|"
    r"looking\s+into\s+(?:that|this|it)|working\s+on\s+(?:that|this|it)|"
    r"come\s+back\s+to\s+you|get\s+back\s+to\s+you|update\s+(?:you\s+)?(?:soon|shortly))\b",
    re.IGNORECASE,
)

# A message that puts something to us rather than just noting our last one.
_ASKS = re.compile(r"\?|\b(when|how|what|why|which|any\s+update|status|still|already)\b", re.I)

# Checking whether anyone is on the other end. This is the one thing that must
# never be met with silence: live, a client sent "What is the status", "Are you
# there ?" and "I want to ask about fee structure" in a row and got nothing back
# on all three, because the topic had already been acknowledged once. Being
# ignored while asking if we are there is the worst reading the bot can offer.
_DIRECT_ADDRESS = re.compile(
    r"^\W*(hello+|hi+|hey+|you\s+there|are\s+you\s+(there|alive|ok)|anyone|any\s?one\s+there|"
    r"bot|is\s+(anyone|someone)\s+there)\W*$|\bare\s+you\s+there\b|\bhello+\s*\?+",
    re.IGNORECASE,
)

# Said once when a client chases a topic we have already acknowledged and we
# have nothing new to tell them. Different wording from the holding line on
# purpose — repeating "still checking" is what made the bot feel robotic, but
# going quiet is worse.
#
# Written in the first person, and it has to stay that way. The version that
# went out live — "your case is with the consultant handling it ... they will
# come back to you directly" — broke the silent-handover rule the guards exist
# to enforce, and broke it in the one place the guards cannot see: these canned
# strings are returned directly and never pass through strip_handover_talk. To
# the client it read as being palmed off mid-conversation, right after the bot
# had been answering them itself. The assertion below is the safety net.
CHASE_REPLY = (
    "I hear you — this has not been forgotten. It is with a live agent and I am "
    "chasing them for you now, so they should connect with you shortly. Anything "
    "else I can help with while you wait?"
)

# For "are you there" / "hey" specifically — see _DIRECT_ADDRESS below. Worded
# differently from CHASE_REPLY on purpose: they answer different questions
# ("is anyone listening" vs "any progress"), and sharing one string meant a
# direct address could get swallowed by CHASE_REPLY's own near-duplicate
# check when a chase had just gone out. Live, on conversation 3766, "hey"
# after "I hear you — I have not forgotten this..." had gone out minutes
# earlier produced nothing — near_duplicate() correctly refused to repeat
# CHASE_REPLY, but nobody had told it "hey" needed an answer of its own.
STILL_HERE_REPLY = (
    "I'm here! This one is with a live agent and they'll connect with you shortly. "
    "In the meantime, is there anything else I can help you with?"
)

# Pressing for the reasoning behind an answer we have already given: "why this
# much", "what's included", "break it down". Our records carry figures, not the
# justification for them, so this is a question only a person can take.
_PROBES = re.compile(
    r"\bwhy\b|\bhow\s+come\b|what(?:'s| is)\s+(?:included|covered|in\s+(?:it|that|this))|"
    r"\bbreak(?:\s|-)?(?:it\s+)?down\b|\bbreakdown\b|\bexplain\b|\bjustif|"
    r"\bwhat\s+(?:does|do)\s+(?:that|it|this)\s+(?:cover|include)\b|\bso\s+expensive\b",
    re.IGNORECASE,
)

PROBE_REPLY = (
    "That is the standard rate for the service. Let me put together exactly what it "
    "covers so I can walk you through it properly."
)

# The client wants something OTHER than the topic a human is already handling.
# This is not a chase and must never be rationed as one: it is a second piece of
# work walking in the door, and it is the most valuable message in the thread.
#
# Live, "I want another service" and "Hey I want another service" both got
# silence — the classifier kept the sticky 'renewal' label, so neither the
# question words nor the intent backstop in _is_asking() saw anything to answer.
# The client then wrote "Hey can you you help me out ??", which retrieved
# nothing and escalated to a human as bot_confused. They were not confused; we
# were.
_NEW_SERVICE_REQUEST = re.compile(
    # "another service", "1 more service", "a new case", "second enquiry"
    r"\b(another|different|other|second|new|more|extra|additional|"
    r"1|one|two|2)\s+(more\s+)?"
    r"(service|servies|enquiry|inquiry|matter|thing|question|case|request|help)\b|"
    r"\b(something|anything)\s+else\b|"
    # "Service I want", "I want help from your agency", "need your help with"
    r"\b(want|need|looking\s+for|ask\s+(?:you\s+)?for)\b[^?]{0,30}\b(service|help|assist)|"
    r"\b(service|help)\b[^?]{0,20}\b(i\s+want|i\s+need|please)\b|"
    r"\bhelp\s+(?:me\s+)?(?:from|with)\s+(?:your\s+)?(agency|team|something)\b|"
    r"\bnot\s+about\s+(that|this)\b|"
    r"\bchange\s+(the\s+)?(topic|subject)\b",
    re.IGNORECASE,
)

# Deterministic on purpose. The one thing we need is which service they mean,
# and a generated reply here reaches for the parked topic instead of moving off
# it — the whole reason this branch exists.
NEW_SERVICE_REPLY = "Of course — which service can I help you with?"

# Asked once, and they are still asking for "another service" without naming
# one. Going quiet at this point is what produced four unanswered messages in a
# row live. We cannot work out what they want, so stop guessing and put a person
# on it — which is what the client has effectively been asking for.
NEW_SERVICE_HANDOVER = (
    "Let me get a live agent onto that for you — they'll connect with you shortly."
)


# These go to the client verbatim, without passing through the guards that vet
# generated text. Saying a live agent will pick it up is now required (rule 2);
# what mentions_handover() still catches is a promise the office has not made —
# a named colleague, or a specific time. Checked at import so a rewrite that
# introduces one fails the process rather than the conversation.
for _name, _canned in (
    ("FALLBACK_REPLY", FALLBACK_REPLY),
    ("CHASE_REPLY", CHASE_REPLY),
    ("STILL_HERE_REPLY", STILL_HERE_REPLY),
    ("PROBE_REPLY", PROBE_REPLY),
    ("NEW_SERVICE_HANDOVER", NEW_SERVICE_HANDOVER),
    ("NEW_SERVICE_REPLY", NEW_SERVICE_REPLY),
):
    assert not mentions_handover(_canned), (
        f"{_name} names a colleague or promises a specific time, which rule 2a "
        "forbids and strip_handover_talk cannot catch here — this string is sent "
        "as written."
    )


# A bare acknowledgement — nothing is being asked, whatever intent the
# classifier settled on. The classifier usually catches these upstream ("'Ok'
# needs no reply — staying silent"), but intent is now part of _is_asking, and
# an "ok" that arrives labelled case_enquiry must not start earning replies:
# rationing the holding line is the entire point of this node.
_ACKNOWLEDGEMENT = re.compile(
    r"^\W*(ok(ay)?|k|kk|noted|sure|alright|right|fine|got\s?it|understood|thanks?|"
    r"thank\s*you|ty|cool|nice|good|great|yes|yeah|ya|yup|no|nope|nah)"
    r"[\s.!,]*(thanks?|thank\s*you|ok(ay)?)?\W*$",
    re.IGNORECASE,
)


def _is_asking(state: ConversationState, message: str) -> bool:
    """Whether the client put something to us, rather than noting our last message.

    Regex alone under-read this. "I want to ask about fee structure" carries no
    question mark and none of the question words, so it scored as an
    acknowledgement and was answered with silence — while the classifier had
    already labelled it fee_enquiry. The intent the classifier assigned is the
    better signal; the regex stays as the backstop for anything it labels
    'other'.
    """
    if _ACKNOWLEDGEMENT.match((message or "").strip()):
        return False
    if _ASKS.search(message or ""):
        return True
    if state.get("intent") in (KB_QUESTION_INTENTS | {CASE_INTENT}):
        return True
    # Everything that is not an acknowledgement is the client saying something.
    # This used to end at the intent check, which meant replying required
    # MATCHING a pattern and silence was the fallback for anything unfamiliar —
    # so every phrasing nobody had thought of in advance was met with nothing.
    # Live, that was "I want 1 more service", "Service I want" and "I want help
    # from your agency", three messages in a row, all silent. Silence needs a
    # positive reason now; the reason is the acknowledgement check above.
    return not is_pure_acknowledgement(message or "") and not is_closing(message or "")


def _should_reassure(state: ConversationState, ticket: dict[str, Any], message: str) -> bool:
    """Whether this message earns another holding reply, or silence.

    Silence is the default once the topic has been acknowledged AND the client
    is only acknowledging back. Everything they say still goes onto the ticket,
    and repeating "still checking" at someone who just said "ok" is not what a
    consultant working the case would do.

    But it is only the default for acknowledgements. A question always earns a
    reply — see _is_asking — and _chasing() below decides what that reply is
    when we have nothing new to say.
    """
    already_logged = (ticket.get("captured_info") or {}).get("follow_ups") or []
    if not already_logged:
        return True  # first message since the ticket was raised — reassure once
    if _DIRECT_ADDRESS.search((message or "").strip()):
        return True  # "are you there?" — never met with silence
    if _NEW_SERVICE_REQUEST.search(message or ""):
        return True  # asking for other work entirely — not a chase on this one
    return _is_asking(state, message)


def _chasing(state: ConversationState) -> bool:
    """Whether we have just sent a holding line and have nothing to add.

    Used to pick CHASE_REPLY over generating another variation on "still
    checking", which the near-duplicate guard would then throw away — leaving
    the client with silence after all.
    """
    return any(
        _HOLDING_LINE.search(line)
        for line in recent_bot_lines(state.get("history_text", ""), count=2)
    )


def _answerable(state: ConversationState) -> bool:
    """Whether this is a general question our own records can answer.

    A parked topic silences chasing, not curiosity. "How much is your agency
    fee" and "what documents do I need" are answered from cb_knowledge_base_updated
    whether or not a human is working the case — they cost the agent nothing and
    the client is otherwise left with three unanswered questions in a row, which
    is exactly what happened in testing.

    "Can answer" is measured on the MATCHES, never on rag_context: with nothing
    retrieved, format_context still returns a paragraph — "(no relevant records
    found) ... tell the client you will check" — which is a non-empty string. So
    a zero-match turn read as answerable, the model was handed an instruction to
    answer and a page saying there was nothing to answer from, and it invented
    the answer. Live: "The process involves three main stages: first, we capture
    your requirements..." on a search that returned 0 matches. A weak best score
    is the same failure in slower motion, so the soft floor applies too.
    """
    if state.get("intent") not in KB_QUESTION_INTENTS:
        return False
    if not state.get("rag_matches"):
        return False
    return float(state.get("rag_best_score") or 0.0) >= settings.rag_soft_floor


def _current_topic(state: ConversationState) -> tuple[str | None, dict[str, Any]]:
    key = ticket_service.topic_key_for(
        state.get("service_type"), effective_contact_type(state), state.get("intent")
    )
    ticket = (state.get("blocked_topics") or {}).get(key or "") or {}
    return key, ticket


async def blocked_topic_responder(state: ConversationState) -> dict[str, Any]:
    topic_key, ticket = _current_topic(state)
    message = (state.get("incoming_text") or "").strip()

    # Decided before the follow-up is written, since the count of what is
    # already on the ticket is exactly what tells us whether we have spoken
    # about this topic before.
    # A question we can answer outranks the silence rule: the reassurance is
    # rationed because repeating "still checking" is noise, but an actual answer
    # never is.
    answering = _answerable(state)
    reassure = answering or _should_reassure(state, ticket, message)

    if ticket.get("id") and message:
        await ticket_service.add_follow_up(ticket["id"], message)
        # This exact topic already matched — always the same underlying issue
        # by definition, so no LLM check is needed here (find_merge_candidate
        # is only for the harder case where the surface topic differs).
        #
        # What to add to the array is NOT state["service_type"]: the "keep the
        # active service" rule in the classifier deliberately overwrites it
        # back to the ticket's own topic whenever a fee/salary question or a
        # follow-up interrupts an in-progress flow, so it never actually
        # carries what the client just asked about — "how much will this
        # cost" inside an open new_hiring ticket classifies as
        # intent=fee_enquiry but service_type stays 'new_hiring'. intent is
        # what still carries it, and SERVICE_INTENTS | ENQUIRY_INTENTS |
        # DISPUTE_INTENTS together are exactly TICKET_SERVICE_TYPES, so a
        # membership check is enough to tell "a trackable service was just
        # asked about" from "just a question, nothing to add" (e.g.
        # document_question, which is real and gets logged via
        # add_follow_up above, but is not one of the eleven the array can
        # hold).
        intent = state.get("intent")
        new_service = intent if intent in TICKET_SERVICE_TYPES else None
        await ticket_service.merge_into(ticket["id"], service_type=new_service)

    if not reassure:
        logger.info(
            "Conversation %s: logged a message on blocked topic %r (ticket %s) without replying "
            "— it has already been acknowledged",
            state.get("conversation_id"),
            topic_key,
            ticket.get("ticket_number"),
        )
        return {"reply": "", "suppress_reply": True, "needs_handover": False}

    # Checking whether anyone is on the other end. Handled before _chasing()
    # below on purpose: _chasing() reuses CHASE_REPLY and drops the message
    # entirely once that line has already gone out once, which is correct for
    # an ordinary chase but not for this — "hey" deserves an answer even if a
    # chase was just acknowledged a minute ago. This has its own reply and its
    # own dedup so it never inherits CHASE_REPLY's silence.
    if not answering and _DIRECT_ADDRESS.search(message):
        already_said = any(
            near_duplicate(STILL_HERE_REPLY, line)
            for line in recent_bot_lines(state.get("history_text", ""), count=2)
        )
        logger.info(
            "Conversation %s: client checking if anyone is there on blocked topic %r "
            "(ticket %s) — answering",
            state.get("conversation_id"),
            topic_key,
            ticket.get("ticket_number"),
        )
        return {
            "reply": CHASE_REPLY if already_said else STILL_HERE_REPLY,
            "needs_handover": False,
        }

    # Not a chase at all — they want different work done. Ask which, and say
    # nothing about the parked topic: they have already been told it is with a
    # human, and repeating it here is what turns "I want another service" into
    # another round of "still checking".
    if not answering and _NEW_SERVICE_REQUEST.search(message):
        logger.info(
            "Conversation %s: client asking for work other than blocked topic %r "
            "(ticket %s) — asking which service",
            state.get("conversation_id"),
            topic_key,
            ticket.get("ticket_number"),
        )
        asked_already = any(
            near_duplicate(NEW_SERVICE_REPLY, line)
            for line in recent_bot_lines(state.get("history_text", ""), count=3)
        )
        if not asked_already:
            return {"reply": NEW_SERVICE_REPLY, "needs_handover": False}
        # We asked which service and they are still asking for "another" one
        # without naming it. Repeating the question is useless and going quiet
        # is worse, so hand it to a person.
        logger.info(
            "Conversation %s: asked which service once and still unnamed — handing to a human",
            state.get("conversation_id"),
        )
        return {"reply": NEW_SERVICE_HANDOVER, "needs_handover": True}

    # They are asking again about a topic we have just held. There is nothing
    # new to tell them, so do not spend a call generating another "still
    # checking" that the near-duplicate guard will discard into silence. Say the
    # one thing that IS new — that we have flagged the wait — and say it once.
    if not answering and _chasing(state):
        logger.info(
            "Conversation %s: client chasing blocked topic %r (ticket %s) — acknowledging the "
            "wait rather than repeating the holding line",
            state.get("conversation_id"),
            topic_key,
            ticket.get("ticket_number"),
        )
        if any(
            near_duplicate(CHASE_REPLY, line)
            for line in recent_bot_lines(state.get("history_text", ""), count=3)
        ):
            return {"reply": "", "suppress_reply": True, "needs_handover": False}
        return {"reply": CHASE_REPLY, "needs_handover": False}

    label = ticket_service.service_types_label(ticket) if ticket.get("id") else service_label(topic_key)
    template = BLOCKED_TOPIC_ANSWER_INSTRUCTION if answering else BLOCKED_TOPIC_INSTRUCTION
    system_prompt = build_system_prompt(
        dict(state),
        # Only when answering. On the holding path the records must not be in
        # the prompt at all — the instruction says not to answer, and a model
        # looking at a page of fees will answer anyway.
        rag_context=state.get("rag_context", "") if answering else "",
        extra_instructions=template.format(service_label=label),
    )
    user_prompt = (
        f"Conversation so far:\n{state.get('history_text') or '(this is the first message)'}\n\n"
        f"Client's latest message(s):\n{state.get('incoming_text', '')}\n\n"
        "Your reply:"
    )

    try:
        # An answer carrying a fee range or a document checklist does not fit in
        # the holding line's budget — clipped mid-list it reads worse than
        # silence. It is still a WhatsApp message, not a briefing: two short
        # sentences, which is what the clamp below enforces.
        reply = (
            await complete(
                system_prompt,
                user_prompt,
                temperature=0.5,
                max_tokens=150 if answering else 90,
            )
        ).strip()
    except Exception:  # noqa: BLE001 - never leave the client without an answer
        logger.exception(
            "Blocked-topic reply generation failed on conversation %s", state.get("conversation_id")
        )
        reply = FALLBACK_REPLY

    reply = strip_meta_commentary(reply.strip('"'))

    # Same grounding rule as everywhere else: a figure that is not in the
    # records we just retrieved is an invented one, and a wrong fee is worse
    # than no fee. Falling back to the holding line is the safe outcome — the
    # client is no worse off than before this path existed.
    invented = ungrounded_figures(reply, state.get("rag_context", "")) if answering else []
    if invented:
        logger.warning(
            "Conversation %s: blocked-topic answer quoted ungrounded figure(s) %s — holding instead",
            state.get("conversation_id"),
            invented,
        )
        reply = FALLBACK_REPLY
    elif is_degenerate(reply) or looks_like_document(reply):
        reply = FALLBACK_REPLY
    else:
        reply = clamp_reply(strip_handover_talk(reply), max_sentences=2)
    if not reply:
        reply = FALLBACK_REPLY

    reply = strip_repeated_opener(reply, *recent_bot_lines(state.get("history_text", "")))

    # Two ways of asking the same thing arrive as two turns — "how much do I pay
    # my maid" then "what will be the salary of my maid" — and retrieval returns
    # the same records for both, so the same answer is generated twice and sent
    # twice. Live, the client's next message was "Why you tell me twice". Saying
    # nothing is right here: they have the answer already.
    previous = recent_bot_lines(state.get("history_text", ""), count=2)
    if any(near_duplicate(reply, line) for line in previous):
        # Asking the same thing twice and asking us to EXPLAIN the answer are
        # different needs, and only the first is served by silence. Live, a
        # client was quoted a service fee, asked "Why this much?", and got the
        # same sentence back; on the next attempt this guard caught it and they
        # got silence instead. Neither is an answer. Our records hold the figure
        # but not its justification, so the honest move is to offer the person
        # who can break it down.
        if _PROBES.search(message):
            logger.info(
                "Conversation %s: client pressed for detail our records do not hold "
                "(topic %r, ticket %s) — offering the consultant",
                state.get("conversation_id"),
                topic_key,
                ticket.get("ticket_number"),
            )
            return {"reply": PROBE_REPLY, "needs_handover": False}
        logger.info(
            "Conversation %s: blocked-topic reply repeats what we just said — staying quiet",
            state.get("conversation_id"),
        )
        return {"reply": "", "suppress_reply": True, "needs_handover": False}

    logger.info(
        "Conversation %s: %s a message on blocked topic %r (ticket %s)",
        state.get("conversation_id"),
        "answered from the knowledge base" if answering else "acknowledged",
        topic_key,
        ticket.get("ticket_number"),
    )
    return {"reply": reply, "needs_handover": False}
