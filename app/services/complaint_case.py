"""A helper complaint's ticket: raised, kept up to date, and read by the portal.

cb_tickets is the record (migration 0133 of the portal gave it complaint_level).
What has no column goes on captured_info. COMPLAINT_CASE_TABLE also copies each
case into cb_complaint_cases, which is off until
scripts/sql/cb_complaint_cases_001.sql is applied; nothing reads that copy, so the
flow does not depend on it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.db.supabase import db
from app.services import ticket as ticket_service

logger = logging.getLogger(__name__)

# The portal's rule: a dispute value in service_type is what puts a ticket on the
# Complaints tab. Both values qualify; the choice only tells the agent which kind.
_SAFETY_TYPES = {"physical", "sexual", "self_harm", "medical", "confinement"}
# In the tab's own vocabulary: high | medium | low. There is no "urgent".
PRIORITY_BY_LEVEL = {"P1": "high", "P2": "high", "P3": "medium", "P4": "medium"}
RETRY_ATTEMPTS = 3
RETRY_PAUSE_SECONDS = 2.0
_MAX_FOLLOW_UPS = 20

_COMPLAINT_KEYS = (
    "level", "complaint_type", "owner", "facts_in_her_words", "safe_to_chat", "safe_status",
    "best_time_to_contact", "consent_employer_contact", "employer_contact_status",
    "next_followup_date", "level_history",
)
_TRIAGE_KEYS = (
    "stage", "pending", "asked", "answers", "awaiting", "language", "duration",
    "facts_complete", "p1_signs",
)
TABLE = "cb_complaint_cases"


def service_for(complaint_type: str | None) -> str:
    return "dispute_assault" if complaint_type in _SAFETY_TYPES else "dispute_salary"


def description_for(case: dict[str, Any]) -> str:
    kind = (case.get("complaint_type") or "general").replace("_", " ")
    return (
        f"Helper complaint - {case.get('level')}, {kind}.\n"
        f"In her words: {(case.get('facts_in_her_words') or '').strip()[:1500]}"
    )


def split(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The agent's fields and the flow's fields, as captured_info holds them."""
    complaint = {k: case.get(k) for k in _COMPLAINT_KEYS}
    complaint["owner"] = complaint.get("owner") or "FCC pool"
    return complaint, {k: case.get(k) for k in _TRIAGE_KEYS}


async def raise_ticket(
    conversation: dict[str, Any],
    case: dict[str, Any],
    message: str,
    *,
    contact_type: str | None,
    whatsapp_number: str | None,
    whatsapp_name: str | None,
    transcript: str = "",
    source: str | None = None,
) -> dict[str, Any] | None:
    """The complaint's ticket, retried: a helper's report must not be lost."""
    complaint, triage = split(case)
    captured = {
        "topic_key": ticket_service.HELPER_COMPLAINT,
        **ticket_service.structure_captured(
            {"contact_type": contact_type, "whatsapp_number": whatsapp_number,
             "whatsapp_name": whatsapp_name},
            detail_keys=set(),
        ),
        "complaint": complaint,
        "triage": triage,
    }
    # A complaint raised from a phone call (app/services/call_context.py).
    if source:
        captured["source"] = source
    for attempt in range(RETRY_ATTEMPTS):
        ticket = await ticket_service.create(
            conversation=conversation,
            service_type=service_for(case.get("complaint_type")),
            captured_info=captured,
            assigned_agent_id=None,
            assignment_rule="round_robin",  # NOT NULL; no agent is assigned
            description=description_for(case),
            complaint_level=case.get("level"),
            priority=PRIORITY_BY_LEVEL.get(case.get("level") or "", "medium"),
            # So a P1 stands out on the Complaints tab until assignment is decided.
            # status stays open, for the reason given in write_through.
            workflow_status="escalated" if case.get("level") == "P1" else None,
        )
        if ticket:
            await _mirror({**case, "ticket_id": ticket.get("id")}, conversation.get("id"), transcript)
            return ticket
        if attempt + 1 < RETRY_ATTEMPTS:
            await asyncio.sleep(RETRY_PAUSE_SECONDS)
    logger.critical(
        "Conversation %s: no complaint ticket after %d attempts (%s)",
        conversation.get("id"), RETRY_ATTEMPTS, case.get("level"),
    )
    return None


async def write_through(case: dict[str, Any], message: str, transcript: str = "") -> None:
    """This message and the case's new state on its ticket. Never raises: the
    message to a helper must not depend on it."""
    ticket_id = case.get("ticket_id")
    if not ticket_id:
        return
    try:
        row = await db.select_one(ticket_service.TABLE, "captured_info, service_type", id=ticket_id)
        if row is None:
            return
        info = dict(row.get("captured_info") or {})
        was_p1 = (info.get("complaint") or {}).get("level") == "P1"
        info["complaint"], info["triage"] = split(case)
        if (message or "").strip():
            info["follow_ups"] = [
                *(info.get("follow_ups") or []),
                {"at": datetime.now(timezone.utc).isoformat(), "message": message.strip()[:500]},
            ][-_MAX_FOLLOW_UPS:]
        patch: dict[str, Any] = {
            "captured_info": info,
            "complaint_level": case.get("level"),
            "description": description_for(case),
            "priority": PRIORITY_BY_LEVEL.get(case.get("level") or "", "medium"),
        }
        # Reaching P1 escalates once. Not on every message: an agent may have moved
        # the ticket on since.
        if case.get("urgent") or (case.get("level") == "P1" and not was_p1):
            # "Urgent" in the tab's terms: top priority and the Escalated status.
            # status stays open: in_progress reads as an agent picking it up, which
            # would pause the bot (TICKET_PICKUP_PAUSES).
            patch["priority"] = "high"
            patch["workflow_status"] = "escalated"
        await db.update(ticket_service.TABLE, patch, id=ticket_id)
        service = service_for(case.get("complaint_type"))
        if service not in (row.get("service_type") or []):
            await ticket_service.add_service_type(ticket_id, service)
        await _mirror(case, None, transcript)
    except Exception:  # noqa: BLE001
        logger.exception("Could not update complaint ticket %s", ticket_id)


async def _mirror(case: dict[str, Any], conversation_id: Any, transcript: str) -> None:
    """The queryable copy, once its table exists. Off by default."""
    if not settings.complaint_case_table or not case.get("ticket_id"):
        return
    payload = {
        "ticket_id": case["ticket_id"],
        "complaint_type": case.get("complaint_type"),
        "facts_in_her_words": case.get("facts_in_her_words"),
        "safe_to_chat": case.get("safe_to_chat"),
        "safe_status": case.get("safe_status"),
        "best_time_to_contact": case.get("best_time_to_contact"),
        "consent_employer_contact": case.get("consent_employer_contact") or "unknown",
        "employer_contact_status": case.get("employer_contact_status") or "none",
        "next_followup_date": case.get("next_followup_date"),
        "level_history": case.get("level_history") or [],
        "triage_stage": case.get("stage"),
        "triage_pending": case.get("pending"),
        "triage_answers": case.get("answers") or {},
        "transcript": transcript[-20000:],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if settings.tenant_id:
        payload["tenant_id"] = settings.tenant_id
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    try:
        await db.execute(db.table(TABLE).upsert(payload, on_conflict="ticket_id"))
    except Exception:  # noqa: BLE001 - nothing reads this copy
        logger.exception("Could not copy complaint case %s to %s", case.get("ticket_id"), TABLE)
