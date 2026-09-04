"""Contact identification — who is this WhatsApp number?

Checked in a fixed order: employer, supplier, partner, candidate, otherwise a
new lead. The result decides both the tone of the conversation and, later, the
assignment rule.
"""

from __future__ import annotations

import logging
from typing import Any

from app.db.supabase import db
from app.services import lead as lead_service
from app.utils import digits_only, phone_variants

logger = logging.getLogger(__name__)

EMPLOYER = "employer"
SUPPLIER = "supplier"
PARTNER = "partner"
CANDIDATE = "candidate"
UNKNOWN = "unknown"

# Columns that hold a phone number, in the order they should be tried.
PHONE_COLUMNS = {
    "employers": ("phone",),
    "profiles": ("phone_e164", "phone"),
    "candidates": ("phone",),
}

# Columns that turn out not to exist are remembered so the error is logged once,
# not on every message.
_MISSING_COLUMNS: set[tuple[str, str]] = set()

UNDEFINED_COLUMN = "42703"


def _is_missing_column(exc: Exception) -> bool:
    text = str(exc)
    return UNDEFINED_COLUMN in text or "does not exist" in text


async def _run(table: str, column: str, build) -> list[dict]:
    """Execute a lookup, tolerating a column the platform schema does not have."""
    if (table, column) in _MISSING_COLUMNS:
        return []
    try:
        result = await db.execute(build())
    except Exception as exc:  # noqa: BLE001 - identification must never break the reply
        if _is_missing_column(exc):
            _MISSING_COLUMNS.add((table, column))
            logger.warning(
                "%s.%s does not exist — contacts of this type cannot be identified by phone",
                table,
                column,
            )
        else:
            logger.exception("Contact lookup failed on %s.%s", table, column)
        return []
    return result.data or []


async def _first_match(table: str, columns: str, phone: str, **eq: Any) -> dict | None:
    """Find a row whose phone number matches, whatever format it is stored in.

    Platform tables hold numbers inconsistently — ``profiles.phone_e164`` is
    clean ('+6591234567') but ``employers.phone`` is spaced ('+65 9188 4442'),
    so an exact match alone misses most employers. Exact variants are tried
    first, then a narrow search on the last four digits with the comparison
    done on digits only.
    """
    variants = phone_variants(phone)
    digits = digits_only(phone)
    if not variants or len(digits) < 8:
        return None
    tail = digits[-8:]
    last4 = digits[-4:]

    def with_filters(query):
        for key, value in eq.items():
            query = query.eq(key, value)
        return query

    for column in PHONE_COLUMNS.get(table, ("phone",)):
        rows = await _run(
            table,
            column,
            lambda c=column: with_filters(db.table(table).select(columns).in_(c, variants)).limit(1),
        )
        if rows:
            return rows[0]

    # Fallback for stored numbers with spaces, dashes or brackets.
    for column in PHONE_COLUMNS.get(table, ("phone",)):
        rows = await _run(
            table,
            column,
            lambda c=column: with_filters(
                db.table(table).select(columns).ilike(c, f"%{last4}%")
            ).limit(25),
        )
        for row in rows:
            if digits_only(row.get(column)).endswith(tail):
                logger.debug("Matched %s.%s on normalised digits", table, column)
                return row
    return None


def _name_of(row: dict[str, Any]) -> str | None:
    """Platform tables spell the display name differently per table.

    profiles/employers use display_name, candidates use full_name.
    """
    for key in ("display_name", "full_name", "name", "employer_name", "contact_name"):
        value = row.get(key)
        if value:
            return str(value)
    return None


async def find_active_case(employer_id: str) -> str | None:
    """Active case for an employer, via their placements."""
    try:
        placements = await db.execute(
            db.table("placements").select("id").eq("employer_id", employer_id).limit(50)
        )
        placement_ids = [row["id"] for row in (placements.data or [])]
        if not placement_ids:
            return None
        cases = await db.execute(
            db.table("cases")
            .select("*")
            .in_("placement_id", placement_ids)
            .eq("status", "active")
            .limit(1)
        )
        rows = cases.data or []
        return rows[0]["id"] if rows else None
    except Exception:  # noqa: BLE001
        logger.exception("Active-case lookup failed for employer %s", employer_id)
        return None


async def count_prior_hires(employer_id: str | None) -> int:
    """How many helpers this employer has actually been placed with, by us.

    `placements` is the only table that answers "did they hire THROUGH US".
    `employers` means the portal holds their details; `leads` means they once
    enquired. Neither is a hire, and treating either as one would tell a
    first-time client we have placed someone with them before.

    Archived rows are excluded — an archived placement is a correction, not a
    placement. Any failure returns 0, which routes the client to the question
    rather than to a wrong claim about their own history.
    """
    if not employer_id:
        return 0
    try:
        result = await db.execute(
            db.table("placements")
            .select("id", count="exact")
            .eq("employer_id", employer_id)
            .is_("archived_at", "null")
            .limit(1)
        )
    except Exception:  # noqa: BLE001 - history is a nicety; the reply is not
        logger.exception("Prior-hire lookup failed for employer %s", employer_id)
        return 0
    return int(result.count or 0)


async def get_placed_helper(employer_id: str | None) -> dict[str, str] | None:
    """The helper we placed with this employer, when there is exactly one.

    Lets the passport-renewal and replacement flows fill her name and
    nationality from our own records instead of asking an existing client for
    details we already hold (client instruction, 2026-09-04: "always check the
    database before asking the user for information that may already exist").

    Deliberately conservative — it returns something only when the employer has
    ONE non-archived placement AND that row carries a candidate_id. Confirmed
    live 2026-09-04: of 6 placement rows only 2 name a candidate, and one
    employer has 4 placements with a single candidate among them. Guessing which
    of four helpers a client means, and then putting that name on a ticket, is
    worse than asking.

    NOTE: `candidates` holds full_name and nationality and NOTHING about a
    passport — there is no passport number, expiry or issue-date column anywhere
    in this database (checked across candidates, placements and cases on
    2026-09-04). The passport expiry therefore still has to be asked, for
    existing and new clients alike, until the portal starts recording it.
    """
    if not employer_id:
        return None
    try:
        placements = await db.select_many(
            "placements", "candidate_id,archived_at", employer_id=employer_id
        )
    except Exception:  # noqa: BLE001 - a nicety; never break the reply over it
        logger.exception("Placement lookup failed for employer %s", employer_id)
        return None

    live = [row for row in placements if not row.get("archived_at")]
    if len(live) != 1 or not live[0].get("candidate_id"):
        return None

    try:
        candidate = await db.select_one(
            "candidates", "full_name,nationality", id=live[0]["candidate_id"]
        )
    except Exception:  # noqa: BLE001
        logger.exception("Candidate lookup failed for employer %s", employer_id)
        return None
    if not candidate:
        return None

    helper: dict[str, str] = {}
    name = str(candidate.get("full_name") or "").strip()
    nationality = str(candidate.get("nationality") or "").strip()
    if name:
        helper["helper_name"] = name[:300]
    if nationality:
        helper["nationality"] = nationality[:100]
    return helper or None


async def identify(phone: str) -> dict[str, Any]:
    """Resolve a WhatsApp number to a platform record.

    Returns a dict shaped for ``wp_chat_conversations``:
    ``contact_type``, ``matched_employer_id``, ``matched_candidate_id``,
    ``matched_supplier_id``, ``matched_case_id`` — plus ``salesperson_profile_id``
    (not a conversation column; used by the assignment service).
    """
    result: dict[str, Any] = {
        "contact_type": UNKNOWN,
        "matched_employer_id": None,
        "matched_candidate_id": None,
        "matched_supplier_id": None,
        "matched_case_id": None,
        "salesperson_profile_id": None,
        "display_name": None,
        # Lead columns are not on wp_chat_conversations — the webhook passes
        # these to the graph rather than persisting them on the row.
        "matched_lead_id": None,
        "matched_lead_number": None,
        "lead_kind": None,
    }

    employer = await _first_match("employers", "*", phone)
    if employer:
        result.update(
            contact_type=EMPLOYER,
            matched_employer_id=employer["id"],
            salesperson_profile_id=employer.get("salesperson_profile_id"),
            display_name=_name_of(employer),
            matched_case_id=await find_active_case(employer["id"]),
        )
        logger.info("Phone %s identified as employer %s", phone, employer["id"])
        return result

    supplier = await _first_match("profiles", "*", phone, archetype_key=SUPPLIER)
    if supplier:
        result.update(
            contact_type=SUPPLIER,
            matched_supplier_id=supplier["id"],
            display_name=_name_of(supplier),
        )
        logger.info("Phone %s identified as supplier %s", phone, supplier["id"])
        return result

    partner = await _first_match("profiles", "*", phone, archetype_key=PARTNER)
    if partner:
        # Partners share the supplier column — contact_type keeps them apart.
        result.update(
            contact_type=PARTNER,
            matched_supplier_id=partner["id"],
            display_name=_name_of(partner),
        )
        logger.info("Phone %s identified as partner %s", phone, partner["id"])
        return result

    candidate = await _first_match("candidates", "*", phone)
    if candidate:
        result.update(
            contact_type=CANDIDATE,
            matched_candidate_id=candidate["id"],
            display_name=_name_of(candidate),
        )
        logger.info("Phone %s identified as candidate %s", phone, candidate["id"])
        return result

    # Last: an open lead from a previous conversation. Not a master record, but
    # it tells us who they are and what they wanted, so the bot picks up where
    # it left off instead of starting the qualification over.
    lead = await lead_service.find_by_phone(phone)
    if lead:
        kind = lead.get("lead_kind")
        result.update(
            contact_type=EMPLOYER if kind == lead_service.EMPLOYER else CANDIDATE,
            display_name=lead.get("full_name"),
            matched_lead_id=lead.get("id"),
            matched_lead_number=lead.get("lead_number"),
            lead_kind=kind,
        )
        logger.info(
            "Phone %s matched open %s lead %s", phone, kind, lead.get("lead_number")
        )
        return result

    logger.info("Phone %s not recognised — treating as a new lead", phone)
    return result


async def get_case_summary(case_id: str) -> dict[str, Any] | None:
    """Current stage/status of a case, for the case-enquiry flow."""
    try:
        return await db.select_one("cases", "*", id=case_id)
    except Exception:  # noqa: BLE001
        logger.exception("Case summary lookup failed for %s", case_id)
        return None
