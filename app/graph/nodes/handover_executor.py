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

from app.graph.llm import complete
from app.graph.prompts.system import build_system_prompt
from app.graph.prompts.templates import ASSAULT_INSTRUCTION
from app.graph.state import ConversationState, conversation_ref
from app.services import assignment as assignment_service
from app.services import handover as handover_service
from app.services import ticket as ticket_service

logger = logging.getLogger(__name__)

ASSAULT_FALLBACK_REPLY = (
    "I'm really sorry to hear this. Please make sure everyone is safe right now — "
    "if anyone is in danger, call the police at 999 immediately. I'm looking into this now."
)

REASON_BY_INTENT = {
    "dispute_assault": handover_service.REASON_DISPUTE,
    "dispute_salary": handover_service.REASON_DISPUTE,
    "fee_enquiry": handover_service.REASON_FEE,
    "salary_enquiry": handover_service.REASON_SALARY,
}


async def _assault_reply(state: ConversationState) -> str:
    system_prompt = build_system_prompt(dict(state), extra_instructions=ASSAULT_INSTRUCTION)
    user_prompt = (
        f"Client's message(s):\n{state.get('incoming_text', '')}\n\nYour reply:"
    )
    try:
        reply = (await complete(system_prompt, user_prompt, temperature=0.3, max_tokens=180)).strip()
    except Exception:  # noqa: BLE001
        logger.exception("Assault reply generation failed — using the fixed message")
        return ASSAULT_FALLBACK_REPLY
    return reply.strip('"') or ASSAULT_FALLBACK_REPLY


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

    update.update(
        {
            "handover_done": True,
            "handover_reason": reason,
            "assigned_agent_id": result.get("assigned_agent_id"),
            "assignment_rule": result.get("assignment_rule"),
        }
    )
    return update
