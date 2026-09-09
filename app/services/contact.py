"""Contact identification — who is this WhatsApp number?

Checked in a fixed order: employer, supplier, partner, candidate, otherwise a
new lead. The result decides both the tone of the conversation and, later, the
assignment rule.
"""

from __future__ import annotations

import json
import logging
from datetime import date
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


# --- Cases: READ ONLY, and deliberately so --------------------------------
#
# `cases` and the ten case_* tables hanging off it belong to the PORTAL. Every
# function below is a SELECT; there is no INSERT, UPDATE or DELETE against any
# of them anywhere in app/, exactly as the bot never writes cb_tickets.status
# (section 6). scripts/selfcheck_flows.py asserts that mechanically, so a
# future change cannot quietly start writing to them.
#
# THREE columns point at a case, and which one is set depends on how the office
# actioned the enquiry. Read against the live schema on 2026-09-09, using the
# database's own column comments as the source rather than inference:
#
#   leads.converted_case_id
#       The lead-first path, and the one that matches the agency's described
#       lifecycle exactly: a new enquiry is captured as a lead, the office
#       converts it, and the employer's first case is created. The bot already
#       creates and owns that `leads` row, so this is a case id reachable from
#       a table this service reads anyway.
#   employer_service_requests.converted_case_id
#       The returning-employer path. Its own comment: "actioned by creating a
#       CASE directly, rather than first spinning it into a lead - for an
#       employer who already has a directory/history record and needs a
#       straightforward repeat/follow-on service." Mutually exclusive with
#       converted_lead_id by workflow, not by constraint.
#   cases.placement_id
#       The only structural link, and the one this module used to rely on
#       ALONE. `cases` has no employer_id column at all, so an employer is
#       reachable only through `placements` - and placement_id is NOT NULL, so
#       every case does have one.
#
# All three are read. Relying on the third alone means a case created through
# either conversion path is invisible to the bot until a placement row happens
# to exist, and an employer would be told nothing about a case they are in the
# middle of.
#
# NOT FILTERED ON STATUS, and the reason is now measured rather than argued.
# The previous version filtered `status = 'active'`. `cases` has been empty on
# every check, so that filter had never once run against a real row - it was a
# guess that happened to be half right. Probed against the live CHECK
# constraint on 2026-09-09 by attempting inserts, the column takes exactly:
#
#     active | completed | cancelled | on_hold
#
# and rejects open, closed, in_progress, new, pending and draft. So 'active'
# was a real value - but filtering on it alone hid the other three, and an
# employer whose case is `on_hold` would have been treated as having no case at
# all, which is precisely when they are most likely to be asking us about it.
# Reading every status and ORDERING them is strictly better than excluding
# three quarters of the vocabulary.
#
# `cases.country` is the same shape and worth knowing while you are here: it
# takes the codes PH, ID and MM, and rejects "Philippines" and "SG".
#
# Archived rows ARE excluded, because archived_at means one thing and nothing
# about it is a guess. scripts/seed_case_testdata.py re-derives both
# vocabularies on demand, so this comment can be checked rather than trusted.
CASE_COLUMNS = (
    "id,case_number,case_type,status,current_stage_key,country,opened_at,"
    "placement_id,archived_at"
)

# Enough for an agent to tell one case from another. A client with more open
# than this is not someone a bot should be summarising at.
MAX_CASES = 5

# The two measured statuses that mean the case is over, used ONLY to sort a
# finished case below a live one when someone has several - never to exclude
# one. `on_hold` is deliberately NOT here: a case on hold is a live case whose
# client is the most likely of all of them to be chasing us about it.
#
# The synonyms after the measured pair cost nothing and cover the portal adding
# a value later; being wrong about one of those costs ordering, which is
# recoverable, rather than visibility, which is not.
_CLOSED_LOOKING = {
    "completed", "cancelled",                      # measured, 2026-09-09
    "closed", "complete", "canceled", "done", "archived", "lost", "resolved",
}


async def _converted_case_ids(table: str, **filters: Any) -> list[str]:
    """Case ids out of a `converted_case_id` column. [] on any failure."""
    try:
        rows = await db.select_many(
            table, "converted_case_id", limit=MAX_CASES * 4, **filters
        )
    except Exception:  # noqa: BLE001 - case context is a nicety; the reply is not
        logger.exception("Case-id lookup failed on %s %s", table, filters)
        return []
    return [str(r["converted_case_id"]) for r in rows if r.get("converted_case_id")]


async def _cases_by_id(case_ids: list[str]) -> list[dict[str, Any]]:
    if not case_ids:
        return []
    try:
        result = await db.execute(
            db.table("cases").select(CASE_COLUMNS).in_("id", case_ids[: MAX_CASES * 4])
        )
    except Exception:  # noqa: BLE001
        logger.exception("Case read failed for %s", case_ids)
        return []
    return result.data or []


async def _cases_via_placements(employer_id: str) -> list[dict[str, Any]]:
    """The structural path: employers -> placements -> cases.placement_id."""
    try:
        placements = await db.select_many(
            "placements", "id", limit=50, employer_id=employer_id
        )
        placement_ids = [row["id"] for row in placements if row.get("id")]
        if not placement_ids:
            return []
        result = await db.execute(
            db.table("cases").select(CASE_COLUMNS).in_("placement_id", placement_ids)
        )
        return result.data or []
    except Exception:  # noqa: BLE001
        logger.exception("Case lookup via placements failed for employer %s", employer_id)
        return []


async def _lead_case_ids(employer_id: str | None, phone: str | None) -> list[str]:
    """Case ids from `leads`, by the converted employer and then by number."""
    ids: list[str] = []
    if employer_id:
        ids += await _converted_case_ids("leads", converted_employer_id=employer_id)
    if phone and not ids:
        # The number is tried only when the employer link found nothing: a lead
        # converted before the employers row carried a matchable phone still
        # names its case. Section 1B keeps one lead per number, so this is one
        # extra row at most.
        try:
            lead = await lead_service.find_by_phone(phone)
        except Exception:  # noqa: BLE001
            logger.exception("Lead lookup for a case failed on %s", phone)
            lead = None
        if lead and lead.get("converted_case_id"):
            ids.append(str(lead["converted_case_id"]))
    return ids


async def _helper_names(placement_ids: list[str]) -> dict[str, str]:
    """placement_id -> the helper's name, for placements that name a candidate.

    Unlike get_placed_helper this does NOT need there to be exactly one
    placement: a case names its own placement, so there is no guessing about
    which helper a case is about. That is the whole reason a case is worth
    reading - it resolves the ambiguity that made get_placed_helper cautious.
    """
    if not placement_ids:
        return {}
    try:
        result = await db.execute(
            db.table("placements").select("id,candidate_id").in_("id", placement_ids)
        )
        rows = result.data or []
        by_candidate = {
            str(r["candidate_id"]): str(r["id"]) for r in rows if r.get("candidate_id")
        }
        if not by_candidate:
            return {}
        found = await db.execute(
            db.table("candidates").select("id,full_name").in_("id", list(by_candidate))
        )
    except Exception:  # noqa: BLE001
        logger.exception("Helper name lookup failed for placements %s", placement_ids)
        return {}
    names: dict[str, str] = {}
    for row in found.data or []:
        name = str(row.get("full_name") or "").strip()
        placement_id = by_candidate.get(str(row.get("id")))
        if name and placement_id:
            names[placement_id] = name[:300]
    return names


def _case_sort_key(case: dict[str, Any]) -> tuple[int, str]:
    finished = str(case.get("status") or "").strip().lower() in _CLOSED_LOOKING
    # opened_at is an ISO timestamp, so negating the sort is done by inverting
    # the string comparison rather than parsing a date we only ever order on.
    return (1 if finished else 0, "-" + str(case.get("opened_at") or ""))


async def get_cases(
    employer_id: str | None, phone: str | None = None
) -> list[dict[str, Any]]:
    """Every case we can reach for this contact, live ones first. READ ONLY.

    Additive: nothing calls this to decide whether to ask a question, and no
    flow changes shape because it returned something. It supplies context the
    prompt and the ticket can carry, and an empty list is a perfectly normal
    answer - which is what it returns today, `cases` having no rows in it yet.
    """
    if not employer_id and not phone:
        return []

    ids: list[str] = await _lead_case_ids(employer_id, phone)
    if employer_id:
        ids += await _converted_case_ids(
            "employer_service_requests", employer_id=employer_id
        )

    rows = await _cases_by_id(ids)
    if employer_id:
        rows += await _cases_via_placements(employer_id)

    # Deduped by case id: the same case is legitimately reachable down more
    # than one path, and an employer told about their case twice reads as a bot
    # that cannot count.
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        case_id = str(row.get("id") or "")
        if case_id and not row.get("archived_at"):
            unique[case_id] = row

    ordered = sorted(unique.values(), key=_case_sort_key)[:MAX_CASES]
    names = await _helper_names(
        [str(r["placement_id"]) for r in ordered if r.get("placement_id")]
    )
    return [
        {
            "case_id": str(row["id"]),
            "case_number": str(row.get("case_number") or "").strip(),
            "case_type": str(row.get("case_type") or "").strip(),
            "status": str(row.get("status") or "").strip(),
            "stage": str(row.get("current_stage_key") or "").strip(),
            "country": str(row.get("country") or "").strip(),
            "opened_at": str(row.get("opened_at") or "").strip(),
            "helper_name": names.get(str(row.get("placement_id") or ""), ""),
        }
        for row in ordered
    ]


async def find_active_case(employer_id: str) -> str | None:
    """The case id stored on the conversation row. READ ONLY.

    Same name, same signature and the same single-id contract as before, so
    every existing caller is untouched - it is only the way the id is found
    that has widened. See the block above for why the old `status = 'active'`
    filter had to go and why two more paths were added.
    """
    cases = await get_cases(employer_id)
    return cases[0]["case_id"] if cases else None


async def get_record_name(employer_id: str | None) -> str | None:
    """The employer's name as OUR RECORDS hold it, never as WhatsApp reports it.

    These are two different things and the agency drew the line between them on
    2026-09-08. `conversation.customer_name` is the WhatsApp push name - set by
    the client on their own profile, written to the row the first time they
    message, and never overwritten afterwards because the identity patch only
    fills it when it is empty. So for a known employer it is still the push
    name, not the name on their file.

    Used to decide whether to greet them or to ask. A flow in
    NAME_FROM_RECORD_ONLY greets on this and asks when it is missing; the push
    name is not evidence either way.
    """
    if not employer_id:
        return None
    try:
        employer = await db.select_one("employers", "*", id=employer_id)
    except Exception:  # noqa: BLE001 - a missing name asks a question, it does not fail
        logger.warning("Could not read the employer record for a name", exc_info=True)
        return None
    name = _name_of(employer) if employer else None
    return str(name).strip()[:300] or None if name else None


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


def _passport_expiry(biodata: Any) -> str | None:
    """`biodata.passportExpiry` as a date a person would read, or None.

    The column is jsonb, so it arrives as a dict; a str is tolerated because
    nothing stops the portal writing one. ONLY the expiry is read — never
    `passportNo`, which sits in the same blob. See get_placed_helper.

    The stored form is ISO ("2033-09-27"). It is reformatted to "27 September
    2033" because this value goes to the model, which reads it back to the
    client, and 09/27 versus 27/09 is a real ambiguity in Singapore. Anything
    that does not parse is passed through as written rather than dropped — an
    odd-looking date on the ticket beats asking a client for something we hold.
    """
    if isinstance(biodata, str):
        try:
            biodata = json.loads(biodata)
        except (TypeError, ValueError):
            return None
    if not isinstance(biodata, dict):
        return None
    raw = str(biodata.get("passportExpiry") or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10]).strftime("%d %B %Y").lstrip("0")
    except ValueError:
        return raw[:100]


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

    The passport expiry comes out of `candidates.biodata`, a jsonb blob the
    portal writes — there is no passport COLUMN on any table, which is what an
    earlier pass concluded from the column names alone and got wrong. All 6
    candidate rows carry `biodata.passportExpiry` (confirmed 2026-09-04).

    `biodata.passportNo` sits right beside it and is deliberately NOT read.
    Rule 4a keeps the whole Singpass block off WhatsApp, and anything returned
    here lands in `collected_info`, which goes into the model's prompt — a
    passport number in the prompt is a passport number one bad turn away from
    being sent to a client. The renewal does not need it; the office has it.
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
            "candidates", "full_name,nationality,biodata", id=live[0]["candidate_id"]
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
    expiry = _passport_expiry(candidate.get("biodata"))
    if expiry:
        helper["passport_expiry"] = expiry
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
