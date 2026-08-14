"""Node 5 — turn the collected info into a cb_tickets row."""

from __future__ import annotations

import logging
from typing import Any

from app.graph.state import (
    AGENCY_INFO_INTENT,
    LEAD_CONTACT_TYPES,
    MEDIA_INTENT,
    ConversationState,
    conversation_ref,
    effective_contact_type,
)
from app.services import assignment as assignment_service
from app.services import handover as handover_service
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

# Intents that never justify a ticket on their own — a degenerate reply on a
# "hi" or an unclassifiable message is not a work item for a human. Anything
# else response_generator marks needs_handover on (weak retrieval, a
# malformed reply) becomes one, using the intent as its topic since these
# never resolve a service_type in the first place.
_NO_TICKET_INTENTS = {"greeting", "smalltalk", "other", AGENCY_INFO_INTENT}


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
    return lead_service.kind_for(service_type, effective_contact_type(state))


async def _finish_lead(
    state: ConversationState,
    service_type: str | None,
    captured: dict[str, Any],
    agent_id: str | None,
) -> dict[str, Any] | None:
    """Write the requirements onto the lead the collector opened.

    Only ever touches a row this conversation created. Anything matched from an
    earlier enquiry is left exactly as sales left it (§1B).
    """
    lead_id = state.get("created_lead_id")
    if not lead_id:
        return None

    kind = state.get("lead_kind") or lead_service.EMPLOYER
    summary = ""
    if kind == lead_service.EMPLOYER:  # leads_candidate has no summary column
        summary = await lead_service.write_summary(
            history=state.get("history_text", ""),
            latest=state.get("incoming_text", ""),
            collected=captured,
            service_type=service_type,
        )
    updated = await lead_service.update_from_collected(
        kind=kind,
        lead_id=lead_id,
        customer_name=state.get("customer_name"),
        collected=captured,
        service_type=service_type,
        summary=summary,
        owner_profile_id=agent_id,
    )
    return {
        "id": lead_id,
        "lead_number": state.get("created_lead_number"),
        "lead_kind": kind,
        **(updated or {}),
    }


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
    if not service_type and state.get("needs_handover"):
        # response_generator can decide a question needs a human (weak
        # retrieval, a malformed reply) without ever resolving a service_type
        # — the informational intents never had one. Without this fallback
        # that escalation raised no ticket at all: nothing to assign, nothing
        # for a follow-up to block against, no record beyond the
        # cb_handovers log line.
        intent = state.get("intent")
        if intent and (
            intent not in _NO_TICKET_INTENTS
            or state.get("handover_reason") == handover_service.REASON_CLIENT_REQUESTED
        ):
            # An explicit "let me speak to someone" always gets a ticket, even
            # when the message itself classifies as small talk — it is a
            # deliberate signal, not a side effect. The bare intent is what
            # gets stored as the topic (never a separate bucket) because
            # topic_key_for() has no visibility into handover_reason and can
            # only fall back to intent when there is no service_type — the
            # two must compute the same key, or a repeat "can I speak to
            # someone" is never recognised as the same open topic and raises
            # a second ticket instead of getting acknowledged.
            service_type = intent

    if not service_type or state.get("ticket_id"):
        return {}

    # This turn's exact topic already has an open ticket — the graph routes
    # those to blocked_topic_responder before this node ever runs, so getting
    # here at all means no exact match. What is left is the harder case: a
    # different-looking topic that is usually still the same underlying piece of
    # work (a salary question inside a hiring enquiry, a document request for
    # the same case). pick_ticket_to_update() defaults to folding it in — see
    # its docstring for why, and for the two things that always get their own
    # row. blocked_topics is already fetched once per turn for the routing check
    # above, so this reuses it rather than a second DB read.
    merge_target = await ticket_service.pick_ticket_to_update(
        state.get("blocked_topics") or {},
        message=state.get("incoming_text") or "",
        service_type=service_type,
    )
    if merge_target:
        await ticket_service.merge_into(merge_target["id"], service_type=service_type)
        logger.info(
            "Conversation %s: %s message folded into existing ticket %s instead of a new one",
            state.get("conversation_id"),
            service_type,
            merge_target.get("ticket_number"),
        )
        return {
            "ticket_id": merge_target["id"],
            "ticket_number": merge_target.get("ticket_number"),
            "handover_reason": state.get("handover_reason")
            or REASON_BY_SERVICE.get(service_type, "ticket_raised"),
        }

    intent = state.get("intent")
    agent_id, rule = await assignment_service.resolve_agent(
        intent=intent,
        matched_employer_id=state.get("matched_employer_id"),
        salesperson_profile_id=state.get("salesperson_profile_id"),
        contact_type=state.get("contact_type") or state.get("detected_contact_type"),
        conversation_id=state.get("conversation_id"),
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

    # 'unknown' is truthy, so the plain value was winning over what the turn
    # actually worked out — a ticket read 'contact_type: unknown' after the
    # classifier had called them an employer four messages running.
    captured.setdefault("contact_type", effective_contact_type(state))
    captured.setdefault("whatsapp_number", state.get("phone"))
    if state.get("customer_name"):
        captured.setdefault("whatsapp_name", state["customer_name"])
    if state.get("intent_reasoning"):
        captured.setdefault("bot_note", state["intent_reasoning"])

    # A service with no fields of its own (media, direct hiring, assault, an
    # unanswerable question) has nothing else to show the agent — the
    # client's own words are the whole ticket.
    if not allowed:
        captured.setdefault("client_message", (state.get("incoming_text") or "")[:2000])

    # A media ticket also needs what the attachment actually was.
    if service_type == MEDIA_INTENT:
        captured.update(_media_summary(state.get("media_items") or []))

    # A lead first, so the ticket can point at it. Normally the collector has
    # already opened it on the client's name and this only fills in what was
    # gathered afterwards; the create path still runs for flows that complete
    # without ever asking for a name (direct hiring, a bare fee enquiry).
    lead = await _finish_lead(state, service_type, captured, agent_id)
    if lead is None:
        lead = await _maybe_create_lead(state, service_type, captured, agent_id)

    # cb_tickets.created_lead_id is a foreign key to `leads` alone, and there is
    # no second column for the candidate table. Passing a leads_candidate id
    # violates the constraint and Postgres rejects the whole insert, so the
    # candidate keeps her lead but the agent gets no work item at all — which is
    # what happened to LC-2026-0001. The number goes into captured_info instead,
    # where the agent opening the ticket can still find her lead.
    created_lead_id = None
    if lead:
        if lead.get("lead_kind") == lead_service.EMPLOYER:
            created_lead_id = lead.get("id")
        elif lead.get("lead_number"):
            captured.setdefault("lead_number", lead["lead_number"])

    ticket = await ticket_service.create(
        conversation=conversation_ref(state),
        service_type=service_type,
        # Flat for the lead writer above, grouped for the human reading the
        # ticket — see structure_captured().
        captured_info=ticket_service.structure_captured(captured, detail_keys=allowed),
        assigned_agent_id=agent_id,
        assignment_rule=rule,
        created_lead_id=created_lead_id,
        # Composed from the enquiry, not from the transcript — see
        # compose_description(). `captured` is still flat here, which is the
        # shape it wants.
        description=ticket_service.initial_description(
            service_type, effective_contact_type(state), captured
        ),
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
