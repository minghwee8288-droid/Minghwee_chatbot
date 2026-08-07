"""Node 5 — turn the collected info into a cb_tickets row."""

from __future__ import annotations

import logging
from typing import Any

from app.graph.state import (
    LEAD_CONTACT_TYPES,
    MEDIA_INTENT,
    ConversationState,
    conversation_ref,
)
from app.services import assignment as assignment_service
from app.services import lead as lead_service
from app.services import ticket as ticket_service

logger = logging.getLogger(__name__)

# Which handover reason a service type produces.
REASON_BY_SERVICE = {
    "fee_enquiry": "fee_enquiry",
    "salary_enquiry": "salary_enquiry",
    "dispute_salary": "dispute_escalation",
    "dispute_assault": "dispute_escalation",
    MEDIA_INTENT: "media_received",
}


def _media_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    """What the agent needs to find the attachment the client sent."""
    if not items:
        return {}
    described = [
        " ".join(
            part
            for part in (
                item.get("type") or "file",
                f"'{item['filename']}'" if item.get("filename") else "",
                f"— \"{item['caption']}\"" if item.get("caption") else "",
            )
            if part
        )
        for item in items
    ]
    summary: dict[str, Any] = {
        "attachments": "; ".join(described),
        "attachment_count": len(items),
    }
    links = [item["url"] for item in items if item.get("url")]
    if links:
        summary["attachment_links"] = " ".join(links)
    return summary


def _lead_name(state: ConversationState, captured: dict[str, Any]) -> str:
    """The best name we have. leads.full_name is NOT NULL.

    A name the client actually gave beats the WhatsApp push name, which is
    whatever they set on their own profile — but the push name is a reasonable
    fallback, and far better than no lead at all.
    """
    for key in ("full_name", "helper_name", "candidate_name", "employer_name"):
        value = str(captured.get(key) or "").strip()
        if value and value.lower() != lead_service.UNANSWERED:
            return value[:200]
    # §23.7 handles the rest: an empty name becomes "WhatsApp Lead {phone}".
    return str(state.get("customer_name") or "").strip()[:200]


# §19 — which services raise a lead, and into which table. Disputes, case
# enquiries, suppliers, partners and general questions never do.
EMPLOYER_LEAD_SERVICES = {
    "new_hiring",
    "fee_enquiry",
    "salary_enquiry",
    "direct_hiring",
    "replacement",
    "renewal",
    "home_leave",
    "passport_renewal",
}
CANDIDATE_LEAD_SERVICES = {ticket_service.CANDIDATE_HIRING, "transfer"}


def _lead_kind(state: ConversationState, service_type: str | None) -> str | None:
    """Which lead table this enquiry belongs in, per §19 — or None."""
    if service_type in CANDIDATE_LEAD_SERVICES:
        return lead_service.CANDIDATE
    if service_type not in EMPLOYER_LEAD_SERVICES:
        return None

    # §19 keys off the service, but a supplier or partner asking about fees is
    # not an employer lead — §15 and §16 say they never produce one.
    contact_type = (state.get("contact_type") or "unknown").strip()
    if contact_type == "unknown":
        contact_type = (state.get("detected_contact_type") or "").strip()
    if contact_type in {"supplier", "partner"}:
        return None
    return lead_service.EMPLOYER


async def _maybe_create_lead(
    state: ConversationState,
    service_type: str | None,
    captured: dict[str, Any],
    agent_id: str | None,
) -> dict[str, Any] | None:
    kind = _lead_kind(state, service_type)
    if not kind:
        # Silence here is why a job seeker was ticketed with no leads_candidate
        # row and nothing in the log said so. Say which service was rejected.
        logger.info(
            "Conversation %s: no lead raised — %r is not a lead-raising service",
            state.get("conversation_id"),
            service_type,
        )
        return None

    phone = state.get("phone") or ""

    # §1B: if this number already has a lead, nothing is written — so the
    # summary, which costs an LLM call, is only generated when it will be used.
    if await lead_service.find_by_phone(phone, kind):
        return await lead_service.create_if_absent(
            kind=kind, name="", phone=phone, service_type=service_type
        )

    summary = ""
    if kind == lead_service.EMPLOYER:  # leads_candidate has no summary column
        summary = await lead_service.write_summary(
            history=state.get("history_text", ""),
            latest=state.get("incoming_text", ""),
            collected=captured,
            service_type=service_type,
        )
    return await lead_service.create_if_absent(
        kind=kind,
        name=_lead_name(state, captured),
        phone=phone,
        collected=captured,
        service_type=service_type,
        summary=summary,
        owner_profile_id=agent_id,
    )


async def ticket_creator(state: ConversationState) -> dict[str, Any]:
    service_type = state.get("service_type")
    if not service_type or state.get("ticket_id"):
        return {}

    intent = state.get("intent")
    agent_id, rule = await assignment_service.resolve_agent(
        intent=intent,
        matched_employer_id=state.get("matched_employer_id"),
        salesperson_profile_id=state.get("salesperson_profile_id"),
        contact_type=state.get("contact_type") or state.get("detected_contact_type"),
    )

    # Only this service's own fields. Belt-and-braces against a stale value
    # surviving a service switch: an agent reading "leave_dates: December" on a
    # salary dispute has no way to tell it is noise.
    allowed = {field.key for field in ticket_service.fields_for(service_type)}
    collected = state.get("collected_info") or {}
    dropped = sorted(set(collected) - allowed)
    if dropped:
        logger.info("Dropping fields not part of %s: %s", service_type, dropped)
    captured = {key: value for key, value in collected.items() if key in allowed}

    # A service with questions that produced no answers means collection was
    # skipped, not completed — the agent gets a work item with nothing on it.
    if allowed and not captured:
        logger.warning(
            "Conversation %s: ticketing %s with none of its %d field(s) captured",
            state.get("conversation_id"),
            service_type,
            len(allowed),
        )

    captured.setdefault("contact_type", state.get("contact_type"))
    captured.setdefault("whatsapp_number", state.get("phone"))
    if state.get("customer_name"):
        captured.setdefault("whatsapp_name", state["customer_name"])
    if state.get("intent_reasoning"):
        captured.setdefault("bot_note", state["intent_reasoning"])

    # A media ticket has no collected fields at all — the attachment details are
    # the whole point of it.
    if service_type == MEDIA_INTENT:
        captured.update(_media_summary(state.get("media_items") or []))
        captured.setdefault("client_message", (state.get("incoming_text") or "")[:2000])

    # A lead first, so the ticket can point at it. Only for contacts we do not
    # already hold a master record for — an existing employer does not become a
    # lead again every time they message.
    lead = await _maybe_create_lead(state, service_type, captured, agent_id)

    ticket = await ticket_service.create(
        conversation=conversation_ref(state),
        service_type=service_type,
        captured_info=captured,
        assigned_agent_id=agent_id,
        assignment_rule=rule,
        created_lead_id=(lead or {}).get("id"),
    )

    update: dict[str, Any] = {
        "assigned_agent_id": agent_id,
        "assignment_rule": rule,
        "handover_reason": state.get("handover_reason")
        or REASON_BY_SERVICE.get(service_type, "ticket_raised"),
    }
    if ticket:
        update["ticket_id"] = ticket.get("id")
        update["ticket_number"] = ticket.get("ticket_number")
    return update
