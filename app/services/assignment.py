"""Agent assignment — the three assignment logics.

Priority:
1. dispute_assault            -> admin escalation
2. returning employer with a  -> that salesperson
   salesperson_profile_id
3. everything else            -> round robin (cb_get_next_agent)
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.db.supabase import db

logger = logging.getLogger(__name__)

ADMIN_ESCALATION = "admin_escalation"
EXISTING_SALESPERSON = "existing_salesperson"
ROUND_ROBIN = "round_robin"
# Reusing an agent this conversation already has is still reported as
# ROUND_ROBIN, not a distinct value: cb_tkt_assignment_rule_check on
# cb_tickets only permits the three rules above, and adding a new one needs a
# migration this code cannot run. The agent still originally came from the
# rotation — this ticket just didn't draw a new one.


async def _find_admin() -> str | None:
    """Who receives assault escalations and partner enquiries.

    ESCALATION_PROFILE_ID names them outright. Without it the first active
    admin is used — ordered by created_at, because an unordered LIMIT 1 returns
    whatever row Postgres happens to reach first, and that changes after an
    update or a vacuum. A safety escalation quietly moving to a different
    person is not something that should depend on physical row order.
    """
    if settings.escalation_profile_id:
        return settings.escalation_profile_id
    try:
        result = await db.execute(
            db.table("profiles")
            .select("id, display_name")
            .eq("tenant_id", settings.tenant_id)
            .eq("archetype_key", "admin")
            .eq("status", "active")
            .order("created_at")
            .limit(1)
        )
    except Exception:  # noqa: BLE001
        logger.exception("Admin lookup failed")
        return None
    rows = result.data or []
    if not rows:
        return None
    logger.debug("Escalation target: %s", rows[0].get("display_name"))
    return rows[0]["id"]


async def _employer_salesperson(employer_id: str | None) -> str | None:
    if not employer_id:
        return None
    try:
        employer = await db.select_one("employers", "id, salesperson_profile_id", id=employer_id)
    except Exception:  # noqa: BLE001
        logger.exception("Salesperson lookup failed for employer %s", employer_id)
        return None
    return (employer or {}).get("salesperson_profile_id")


async def _conversation_agent(conversation_id: int | None) -> str | None:
    """The agent already handling this conversation, from an earlier ticket.

    Each ticket used to run its own round robin, so one client could pick up
    a different agent per ticket on the same thread — three tickets, three
    strangers. Every ticket on a conversation should land on whoever already
    has it; round robin only picks a new agent for a conversation that has
    never been assigned one at all.
    """
    if not conversation_id:
        return None
    try:
        result = await db.execute(
            db.table("cb_tickets")
            .select("assigned_agent_id")
            .eq("conversation_id", conversation_id)
            .order("created_at", desc=True)
            .limit(5)
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not look up the existing agent for conversation %s", conversation_id)
        return None
    for row in result.data or []:
        agent_id = row.get("assigned_agent_id")
        if agent_id:
            return agent_id
    return None


async def _next_round_robin_agent() -> str | None:
    try:
        data = await db.rpc("cb_get_next_agent", {"p_tenant_id": settings.tenant_id})
    except Exception:  # noqa: BLE001
        logger.exception("cb_get_next_agent failed")
        return None
    if not data:
        return None
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        first = data[0]
        if isinstance(first, dict):
            return first.get("cb_get_next_agent") or next(iter(first.values()), None)
        return str(first)
    if isinstance(data, dict):
        return data.get("cb_get_next_agent") or next(iter(data.values()), None)
    return None


async def resolve_agent(
    *,
    intent: str | None,
    matched_employer_id: str | None = None,
    salesperson_profile_id: str | None = None,
    contact_type: str | None = None,
    conversation_id: int | None = None,
) -> tuple[str | None, str]:
    """Pick the agent and report which rule produced them."""
    if intent == "dispute_assault":
        admin_id = await _find_admin()
        if admin_id:
            logger.info("Assault escalation -> admin %s", admin_id)
            return admin_id, ADMIN_ESCALATION
        logger.error("No active admin found for assault escalation — falling back to round robin")

    # CONVERSATION_FLOWS §16: a partnership approach is a business decision, not
    # a sales lead, so it goes to an admin rather than into the sales rotation.
    if (contact_type or "") == "partner":
        admin_id = await _find_admin()
        if admin_id:
            logger.info("Partner enquiry -> admin %s", admin_id)
            return admin_id, ADMIN_ESCALATION
        logger.warning("No active admin for a partner enquiry — falling back to round robin")

    salesperson = salesperson_profile_id or await _employer_salesperson(matched_employer_id)
    if salesperson:
        logger.info("Returning employer -> existing salesperson %s", salesperson)
        return salesperson, EXISTING_SALESPERSON

    sticky_agent = await _conversation_agent(conversation_id)
    if sticky_agent:
        logger.info("Conversation %s already has agent %s -> keeping it", conversation_id, sticky_agent)
        return sticky_agent, ROUND_ROBIN

    agent_id = await _next_round_robin_agent()
    if agent_id:
        logger.info("Round robin -> agent %s", agent_id)
        return agent_id, ROUND_ROBIN

    logger.error("No agent could be assigned — conversation will sit unassigned")
    return None, ROUND_ROBIN


async def map_to_portal_user(profile_id: str | None) -> int | None:
    """Translate a platform profile UUID into the portal's wp_chat_users.id."""
    if not profile_id:
        return None
    try:
        row = await db.select_one("wp_chat_users", "id, profile_id", profile_id=profile_id)
    except Exception:  # noqa: BLE001
        logger.exception("Portal user lookup failed for profile %s", profile_id)
        return None
    if not row:
        logger.warning("Profile %s has no wp_chat_users row — portal cannot show the assignment", profile_id)
        return None
    return int(row["id"])


async def agent_profile(profile_id: str | None) -> dict[str, Any] | None:
    if not profile_id:
        return None
    try:
        return await db.select_one("profiles", "*", id=profile_id)
    except Exception:  # noqa: BLE001
        logger.exception("Profile lookup failed for %s", profile_id)
        return None
