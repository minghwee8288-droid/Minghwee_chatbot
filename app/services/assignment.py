"""Agent assignment — the three assignment logics.

Priority:
1. dispute_assault            -> admin escalation
2. returning employer with a  -> that salesperson
   salesperson_profile_id
3. the conversation already   -> that person: the portal inbox's owner first,
   has an owner                  then the agent on an earlier ticket
4. everything else            -> round robin (cb_get_next_agent)

The agency's rule (2026-10-08): one salesperson handles one client's whole
conversation. Rule 3 is that rule, and it reads the PORTAL's owner as well as
our own tickets, because the portal team also assigns chats by hand.

Profiles and portal users are two different ids. The bot assigns a PROFILE;
the portal inbox shows a PORTAL USER (wp_chat_conversations.assigned_user_id).
cb_agent_portal_users links the two (scripts/sql/assignment_002), with the
portal's own, so far empty, wp_chat_users.profile_id as the fallback.
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

    # Someone on the portal team already took this chat: the ticket is theirs
    # too, or the client ends up with two salespeople (the round robin would
    # pick whoever is next, knowing nothing of the inbox).
    portal_owner = await _portal_owner(conversation_id)
    if portal_owner:
        logger.info("Conversation %s is owned in the portal by %s -> keeping it", conversation_id, portal_owner)
        return portal_owner, EXISTING_SALESPERSON

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


PORTAL_LINKS = "cb_agent_portal_users"


async def map_to_portal_user(profile_id: str | None) -> int | None:
    """Translate a platform profile UUID into the portal's wp_chat_users.id.

    Our own link table first; the portal's wp_chat_users.profile_id only as a
    fallback, in case the portal team ever fills it themselves.
    """
    if not profile_id:
        return None
    try:
        row = await db.select_one(PORTAL_LINKS, "portal_user_id", profile_id=profile_id)
        if row:
            return int(row["portal_user_id"])
        row = await db.select_one("wp_chat_users", "id", profile_id=profile_id)
    except Exception:  # noqa: BLE001
        logger.exception("Portal user lookup failed for profile %s", profile_id)
        return None
    if not row:
        logger.warning("Profile %s has no portal login linked — portal cannot show the assignment", profile_id)
        return None
    return int(row["id"])


async def _profile_of_portal_user(portal_user_id: int | None) -> str | None:
    """The reverse of map_to_portal_user: which profile a portal login is."""
    if portal_user_id is None:
        return None
    try:
        row = await db.select_one(PORTAL_LINKS, "profile_id", portal_user_id=portal_user_id)
        if row:
            return row["profile_id"]
        row = await db.select_one("wp_chat_users", "profile_id", id=portal_user_id)
    except Exception:  # noqa: BLE001
        logger.exception("Profile lookup failed for portal user %s", portal_user_id)
        return None
    return (row or {}).get("profile_id")


async def _portal_owner(conversation_id: int | None) -> str | None:
    """The profile of whoever owns this chat in the portal inbox, if we can tell.

    An owner we cannot map (an admin, a login with no sales profile) returns
    None, so the ticket falls to the next rule - and claim_portal_owner below
    still leaves that owner in place in the inbox.
    """
    if not conversation_id:
        return None
    try:
        row = await db.select_one("wp_chat_conversations", "assigned_user_id", id=conversation_id)
    except Exception:  # noqa: BLE001
        logger.exception("Portal owner lookup failed for conversation %s", conversation_id)
        return None
    owner = (row or {}).get("assigned_user_id")
    if owner is None:
        return None
    profile_id = await _profile_of_portal_user(int(owner))
    if not profile_id:
        logger.info(
            "Conversation %s is owned in the portal by user %s, who has no linked profile", conversation_id, owner
        )
    return profile_id


async def claim_portal_owner(
    conversation_id: int | None, profile_id: str | None, assignment_rule: str | None = None
) -> int | None:
    """Show this ticket's agent as the chat's owner in the portal inbox - ONLY if
    the chat has no owner yet.

    Never overwrites: a chat the portal team assigned by hand stays theirs. The
    condition is in the UPDATE itself (assigned_user_id IS NULL), so a person
    assigning the chat at the same moment cannot be overwritten either.
    Returns the portal user now shown, or None if nothing was written.
    """
    if not conversation_id or not profile_id:
        return None
    portal_user_id = await map_to_portal_user(profile_id)
    if portal_user_id is None:
        return None
    patch: dict[str, Any] = {"assigned_user_id": portal_user_id}
    if assignment_rule:
        patch["assignment_rule"] = assignment_rule
    try:
        result = await db.execute(
            db.table("wp_chat_conversations")
            .update(patch)
            .eq("id", conversation_id)
            .is_("assigned_user_id", "null")
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not set the portal owner on conversation %s", conversation_id)
        return None
    if result.data:
        logger.info("Conversation %s now owned in the portal by user %s", conversation_id, portal_user_id)
        return portal_user_id
    logger.info("Conversation %s already has a portal owner - left as it is", conversation_id)
    return None


async def agent_profile(profile_id: str | None) -> dict[str, Any] | None:
    if not profile_id:
        return None
    try:
        return await db.select_one("profiles", "*", id=profile_id)
    except Exception:  # noqa: BLE001
        logger.exception("Profile lookup failed for %s", profile_id)
        return None
