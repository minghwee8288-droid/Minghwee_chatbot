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
from app.graph.guards import without_names_addressed
from app.graph.llm import complete
from app.graph.prompts.system import build_system_prompt
from app.graph.prompts.templates import ASSAULT_INSTRUCTION
from app.graph.reference import add_reference
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
