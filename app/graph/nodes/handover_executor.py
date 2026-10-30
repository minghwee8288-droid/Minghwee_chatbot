"""Node 6 — escalate a topic to a human without silencing the bot.

Every trigger that reaches this node is topic-scoped: it logs the escalation
and finalises the ticket's agent assignment, but leaves bot_status alone, so
the bot keeps answering everything else on the conversation. The only message
this node itself may produce is the single empathetic reply for a first
assault report.
"""

from __future__ import annotations

import logging
from typing import Any

from app.graph import clarify
from app.graph import guards
from app.graph.guards import without_names_addressed
from app.graph.language import mainly_non_english, reply_language
from app.graph.llm import complete
from app.graph.prompts.system import build_system_prompt
from app.graph.prompts.templates import ASSAULT_INSTRUCTION
from app.graph.reference import _SENTENCE_END, add_reference, reference_sentence
from app.graph.state import ConversationState, conversation_ref
from app.services import assignment as assignment_service
from app.services import handover as handover_service
from app.services import ticket as ticket_service

logger = logging.getLogger(__name__)

# Deliberately does NOT end with "anything else I can help you with?" — rule 2
# asks for that offer on an ordinary handover, and it is the wrong thing to say
# to someone who has just reported harm. Safety first, then a person, and stop.
ASSAULT_FALLBACK_REPLY = (
    "I'm really sorry to hear this. Please make sure everyone is safe right now — "
    "if anyone is in danger, call the police at 999 immediately. I've alerted our "
    "team and a live agent will connect with you shortly."
)

# Whether the safety reply to a harm report carries the ticket reference.
# Off until the agency decides (2026-10-12): it is the one message that goes to
# someone reporting harm, and what it should say is theirs to settle.
SAFETY_REPLY_SHOWS_REFERENCE = False

# 2026-10-30 (test 7:45 PM IST, "hey i was beaten up by my employer please
# help me"): ticket CB-2026-0143 was raised high priority, but the reply
# carried no reference and nothing for someone who is hurt. With this on, the
# safety reply (every dispute_assault turn: abuse, assault, harassment, locked
# in, no food) carries the reference as the other handovers do, plus the
# ambulance line after the 999 one and the MOM helpline (both in the KB
# rows), and never opens with an introduction. In English the two lines are
# fixed text; in another language the model is told to include both numbers.
# False = the reply as before (and SAFETY_REPLY_SHOWS_REFERENCE decides).
CRISIS_REPLY_COMPLETE = True
AMBULANCE_LINE = "If you are injured, please call an ambulance on 995."
MOM_HELPLINE_LINE = "You can also call the MOM helpline on 1800-339-5505."
_CRISIS_NO_INTRO_LINE = (
    "\n\nDo NOT introduce yourself or say who you are - go straight to their safety."
)
_CRISIS_NUMBERS_LINE = (
    "\n\nAlso say that if they are injured they should call an ambulance on 995, and "
    "that the MOM helpline is 1800-339-5505."
)


def complete_crisis_reply(reply: str, number: str | None, history_text: str = "") -> str:
    """The safety reply with the ambulance and MOM lines after the 999 sentence
    (English only) and the ticket reference after the handover sentence."""
    if not reply:
        return reply
    stripped = guards._ANY_INTRODUCTION.sub("", reply, count=1).strip()
    if stripped and stripped != reply.strip():
        reply = stripped[0].upper() + stripped[1:]
    if reply_language(reply) == "en" and "995" not in reply:
        lines = f"{AMBULANCE_LINE} {MOM_HELPLINE_LINE}"
        at = reply.find("999")
        end = _SENTENCE_END.search(reply, at) if at >= 0 else None
        if end:
            reply = f"{reply[:end.end()].rstrip()} {lines} {reply[end.end():].lstrip()}".rstrip()
        else:
            reply = f"{lines} {reply}"
    if number and number not in reply:
        referenced = add_reference(reply, number, history_text)
        if referenced == reply:
            referenced = f"{reply.rstrip()} {reference_sentence(reply, number, history_text)}"
        reply = referenced
    return reply

REASON_BY_INTENT = {
    "dispute_assault": handover_service.REASON_DISPUTE,
    "dispute_salary": handover_service.REASON_DISPUTE,
    "fee_enquiry": handover_service.REASON_FEE,
    "salary_enquiry": handover_service.REASON_SALARY,
}


# Phase 3 Step 5 Batch F item 4 (2026-10-14). The Step 3 Burmese safety reply
# addressed the thread's EMPLOYER by name - the record name on the number -
# while the person reporting harm may well be the helper, writing on her
# employer's thread. Nothing we hold confirms a record name is the sender's
# own, so the safety reply uses no name: none reaches its prompt, the
# instruction says so, and any name we hold that still appears as a form of
# address is taken out. False = the reply as before.
SAFETY_REPLY_NO_NAME = True
_NO_NAME_LINE = (
    "\n\nAddress them by NO name at all - not a name from our records and not one "
    "from earlier in the conversation. The person writing may not be the person our "
    "records name."
)


def _names_held(state: ConversationState) -> list[str]:
    collected = state.get("collected_info") or {}
    return [
        str(n) for n in (state.get("record_name"), state.get("customer_name"),
                         collected.get("full_name"))
        if n
    ]


async def _assault_reply(state: ConversationState) -> str:
    prompt_state = dict(state)
    instruction = ASSAULT_INSTRUCTION
    if SAFETY_REPLY_NO_NAME:
        prompt_state["record_name"] = ""
        prompt_state["collected_info"] = {
            k: v for k, v in (state.get("collected_info") or {}).items() if k != "full_name"
        }
        instruction = ASSAULT_INSTRUCTION + _NO_NAME_LINE
    if CRISIS_REPLY_COMPLETE:
        instruction += _CRISIS_NO_INTRO_LINE
        if mainly_non_english(state.get("incoming_text") or ""):
            instruction += _CRISIS_NUMBERS_LINE
    system_prompt = build_system_prompt(prompt_state, extra_instructions=instruction)
    user_prompt = (
        f"Client's message(s):\n{state.get('incoming_text', '')}\n\nYour reply:"
    )
    try:
        reply = (await complete(system_prompt, user_prompt, temperature=0.3, max_tokens=180)).strip()
    except Exception:  # noqa: BLE001
        logger.exception("Assault reply generation failed — using the fixed message")
        return ASSAULT_FALLBACK_REPLY
    reply = reply.strip('"')
    if SAFETY_REPLY_NO_NAME and reply:
        reply = without_names_addressed(reply, _names_held(state))
    return reply or ASSAULT_FALLBACK_REPLY


async def _assault_ticket(state: ConversationState) -> dict[str, Any]:
    agent_id, rule = await assignment_service.resolve_agent(
        intent="dispute_assault",
        matched_employer_id=state.get("matched_employer_id"),
        salesperson_profile_id=None,  # assault always escalates to admin, never sales
    )
    ticket = await ticket_service.create(
        conversation=conversation_ref(state),
        service_type="dispute_assault",
        captured_info=ticket_service.structure_captured(
            {
                "contact_type": state.get("contact_type"),
                "whatsapp_number": state.get("phone"),
                "whatsapp_name": state.get("customer_name"),
                "client_message": (state.get("incoming_text") or "")[:2000],
            },
            detail_keys=set(),
        ),
        assigned_agent_id=agent_id,
        assignment_rule=rule,
        # The one ticket where the client's own words belong on the briefing:
        # whoever opens a safety report must read what was actually said.
        description=ticket_service.initial_description(
            "dispute_assault",
            state.get("contact_type"),
            {"client_message": state.get("incoming_text")},
        ),
    )
    return {
        "assigned_agent_id": agent_id,
        "assignment_rule": rule,
        "ticket_id": (ticket or {}).get("id"),
        "ticket_number": (ticket or {}).get("ticket_number"),
    }


async def handover_executor(state: ConversationState) -> dict[str, Any]:
    intent = state.get("intent")
    update: dict[str, Any] = {}

    if intent == "dispute_assault":
        if not state.get("reply"):
            update["reply"] = await _assault_reply(state)
        if not state.get("ticket_id"):
            update.update(await _assault_ticket(state))
        if CRISIS_REPLY_COMPLETE:
            update["reply"] = complete_crisis_reply(
                update.get("reply") or state.get("reply") or "",
                update.get("ticket_number") or state.get("ticket_number"),
                state.get("history_text") or "")

    reason = (
        state.get("handover_reason")
        or REASON_BY_INTENT.get(intent or "")
        or (handover_service.REASON_TICKET if state.get("ticket_id") or update.get("ticket_id") else handover_service.REASON_CONFUSED)
    )

    result = await handover_service.log_escalation(
        conversation_ref(state),
        reason=reason,
        intent=intent,
        ticket_id=update.get("ticket_id") or state.get("ticket_id"),
        salesperson_profile_id=state.get("salesperson_profile_id"),
        assigned_agent_id=update.get("assigned_agent_id") or state.get("assigned_agent_id"),
        assignment_rule=update.get("assignment_rule") or state.get("assignment_rule"),
        contact_type=state.get("contact_type") or state.get("detected_contact_type"),
    )

    # The ticket's reference in the handover sentence the reply already has
    # (app/graph/reference.py). ticket_creator ran just before this node, so
    # the number - new, or the ticket this message was folded into - is here.
    reply = update.get("reply") or state.get("reply") or ""
    number = update.get("ticket_number") or state.get("ticket_number")
    if (reply and number and state.get("clarify_action") == "call"
            and clarify.CALL_REQUEST_DIRECT and not update.get("reply")):
        # A call request's reply has no handover sentence to sit after, so
        # the reference is written into it (app/graph/clarify.py).
        update["reply"] = reply.replace(clarify.CALL_REPLY, clarify.call_reply(number), 1)
    elif reply and number and (intent != "dispute_assault" or SAFETY_REPLY_SHOWS_REFERENCE):
        referenced = add_reference(reply, number, state.get("history_text") or "")
        if referenced != reply:
            update["reply"] = referenced

    update.update(
        {
            "handover_done": True,
            "handover_reason": reason,
            "assigned_agent_id": result.get("assigned_agent_id"),
            "assignment_rule": result.get("assignment_rule"),
        }
    )
    return update
