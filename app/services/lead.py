"""Lead creation for contacts we do not already hold a record for.

An enquiry from an unknown number produces a lead, not a master record: an
employer enquiry lands in ``leads``, a helper offering herself lands in
``leads_candidate``. The platform owns ``employers`` and ``candidates``, and a
name typed once into WhatsApp is not verified enough to go there — conversion
is a human decision, which is what ``leads.converted_employer_id`` is for.

Every value written to a constrained column goes through a mapping function
here. The database rejects anything outside its CHECK lists, and the whole
insert fails on one bad value, so the mapping is deliberate rather than a
pass-through of whatever the model extracted.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.db.supabase import db
from app.utils import digits_only, phone_variants

logger = logging.getLogger(__name__)

EMPLOYER_TABLE = "leads"
CANDIDATE_TABLE = "leads_candidate"

EMPLOYER = "employer"
CANDIDATE = "candidate"

TABLE_FOR = {EMPLOYER: EMPLOYER_TABLE, CANDIDATE: CANDIDATE_TABLE}

# What the collector records for a field the client would not answer. It must
# never reach a lead column as if it were a real value.
UNANSWERED = "not provided"

SOURCE = "chatbot"

# §20 — employer L-2026-0001, candidate LC-2026-0001.
NUMBER_PREFIX = {EMPLOYER_TABLE: "L", CANDIDATE_TABLE: "LC"}

# --- Value mappings (every target column below is CHECK-constrained) --------

# §2 INTENT_TO_INTEREST_TYPE, verbatim. Note 'replacement' maps to 'replacement'
# and not 'replacement_hire', and both enquiry types map to first_time_hire.
_INTEREST_BY_SERVICE = {
    "new_hiring": "first_time_hire",
    "direct_hiring": "direct_hire",
    "replacement": "replacement",
    "transfer": "transfer",
    "renewal": "renewal",
    "home_leave": "home_leave",
    "passport_renewal": "passport_renewal",
    "fee_enquiry": "first_time_hire",
    "salary_enquiry": "first_time_hire",
}

_NATIONALITY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"phil|filip|pinay|\bph\b", re.I), "PH"),
    (re.compile(r"indo|\bid\b", re.I), "ID"),
    (re.compile(r"myan|burm|\bmm\b", re.I), "MM"),
    (re.compile(r"no\s*pref|any|up\s+to\s+you|doesn'?t\s+matter|none|open", re.I), "none"),
)

def interest_type(service_type: str | None) -> str:
    return _INTEREST_BY_SERVICE.get(service_type or "", "other")


def nationality_code(value: str | None) -> str | None:
    """'Filipino' -> 'PH'. None when nothing recognisable was said."""
    text = (value or "").strip()
    if not text:
        return None
    for pattern, code in _NATIONALITY_PATTERNS:
        if pattern.search(text):
            return code
    return None


# §0: temperature is a fixed default, not something the bot judges. Sales sets
# it once they have spoken to the client.
DEFAULT_TEMPERATURE = "warm"


# --- Branch ----------------------------------------------------------------

_branch_id: str | None = None


async def resolve_branch_id() -> str | None:
    """The branch a chatbot lead belongs to — configured, else the tenant's HQ."""
    global _branch_id
    if settings.lead_branch_id:
        return settings.lead_branch_id
    if _branch_id is not None:
        return _branch_id or None

    try:
        result = await db.execute(
            db.table("branches").select("id, name, code").eq("tenant_id", settings.tenant_id)
        )
        rows = result.data or []
    except Exception:  # noqa: BLE001 - a lead is not worth breaking the reply for
        logger.exception("Branch lookup failed — leads cannot be created without one")
        return None

    if not rows:
        logger.error("No branches for tenant %s — leads cannot be created", settings.tenant_id)
        _branch_id = ""
        return None

    preferred = next(
        (
            r
            for r in rows
            if (r.get("code") or "").upper() == "JUR" or "HQ" in (r.get("name") or "").upper()
        ),
        rows[0],
    )
    _branch_id = preferred["id"]
    logger.info("Chatbot leads will be filed under %s", preferred.get("name") or _branch_id)
    return _branch_id


# --- Numbering -------------------------------------------------------------

async def next_lead_number(table: str, *, year: int | None = None) -> str:
    """§20 — L-2026-0001 for employers, LC-2026-0001 for candidates.

    Scoped to the tenant and the current year, so the pre-existing L-2004..
    L-2010 rows (a different format) are not part of the series.
    """
    prefix = NUMBER_PREFIX[table]
    year = year or datetime.now(tz=timezone.utc).year
    stem = f"{prefix}-{year}-"
    try:
        result = await db.execute(
            db.table(table)
            .select("lead_number")
            .eq("tenant_id", settings.tenant_id)
            .like("lead_number", f"{stem}%")
            .order("created_at", desc=True)
            .limit(1)
        )
        rows = result.data or []
    except Exception:  # noqa: BLE001
        logger.exception("Could not read the last lead number from %s", table)
        rows = []

    nxt = 1
    if rows:
        tail = str(rows[0].get("lead_number") or "").rsplit("-", 1)[-1]
        if tail.isdigit():
            nxt = int(tail) + 1
    return f"{stem}{nxt:04d}"


# --- Lookup ----------------------------------------------------------------

async def find_by_phone(phone: str, kind: str | None = None) -> dict[str, Any] | None:
    """§1B — any lead for this number, in ANY status.

    One phone number may have only one lead row, ever. Status and interest_type
    are deliberately not filtered on: a converted or lost lead still counts, and
    finding one means no new row is created.

    Matching tolerates stored formatting — platform rows hold numbers as
    '+65 9188 4442' while WhatsApp sends bare digits, and an exact-match-only
    check would report "no lead" every time and duplicate the row.
    """
    variants = phone_variants(phone)
    digits = digits_only(phone)
    if not variants or len(digits) < 8:
        return None
    tail = digits[-8:]

    tables = [TABLE_FOR[kind]] if kind in TABLE_FOR else [EMPLOYER_TABLE, CANDIDATE_TABLE]
    for table in tables:
        found = None
        try:
            result = await db.execute(
                db.table(table)
                .select("*")
                .eq("tenant_id", settings.tenant_id)
                .in_("phone", variants)
                .limit(1)
            )
            rows = result.data or []
            if rows:
                found = rows[0]
            else:
                # Fall back to comparing on digits alone.
                loose = await db.execute(
                    db.table(table)
                    .select("*")
                    .eq("tenant_id", settings.tenant_id)
                    .ilike("phone", f"%{digits[-4:]}%")
                    .limit(25)
                )
                found = next(
                    (r for r in (loose.data or []) if digits_only(r.get("phone")).endswith(tail)),
                    None,
                )
        except Exception:  # noqa: BLE001
            logger.exception("Lead lookup failed on %s", table)
            continue
        if found:
            return {
                **found,
                "lead_table": table,
                "lead_kind": EMPLOYER if table == EMPLOYER_TABLE else CANDIDATE,
            }
    return None


# --- Creation --------------------------------------------------------------

def _fields(kind: str, *, name: str, phone: str, collected: dict[str, Any],
            service_type: str | None, summary: str) -> dict[str, Any]:
    """Only columns the target table actually has, with mapped values."""
    common: dict[str, Any] = {
        "full_name": name,
        "phone": phone,
        "source": SOURCE,
        "temperature": DEFAULT_TEMPERATURE,
    }
    email = str(collected.get("email") or "").strip()
    if email and "@" in email:
        common["email"] = email[:200]

    # §2: urgency is inferred only if the client signalled it — never asked.
    urgency = str(collected.get("timeline") or collected.get("availability") or "").strip()
    if urgency and urgency.lower() != UNANSWERED:
        common["urgency"] = urgency[:200]

    if kind == CANDIDATE:
        # leads_candidate has no interest_type, requirement, budget or summary.
        code = nationality_code(collected.get("nationality"))
        if code:
            common["nationality"] = code
        # §3: never asked, captured only if volunteered.
        salary = str(collected.get("salary_expectation") or "").strip()
        if salary:
            common["salary_expectation"] = salary[:200]
        return common

    common["interest_type"] = interest_type(service_type)
    code = nationality_code(
        collected.get("preferred_nationality") or collected.get("nationality")
    )
    if code:
        common["preferred_nationality"] = code
    # §2: never asked, captured only if volunteered.
    budget = str(collected.get("budget") or "").strip()
    if budget:
        common["budget"] = budget[:200]
    requirement = str(
        collected.get("requirement") or collected.get("care_type") or collected.get("reason") or ""
    ).strip()
    if requirement and requirement.lower() != UNANSWERED:
        common["requirement"] = requirement[:500]
    if summary:
        common["summary"] = summary[:2000]
    return common


def fallback_name(phone: str) -> str:
    """§23.7 — full_name is NOT NULL, and a refused name must not lose the lead."""
    return f"WhatsApp Lead {phone}".strip()


# --- Summary (§21) ---------------------------------------------------------

SUMMARY_SYSTEM = """You write the one-line summary a sales agent reads first when \
they open a new lead at a Singapore maid agency.

Write 1-2 short sentences covering what this person wants and any detail that \
would change how the agent handles it — who the helper is for, a medical or \
family situation, timing pressure, where they are.

Plain statements about the client, in the third person. No greeting, no advice, \
no bullet points, no quotes, and never address the client. Do not invent \
anything that is not in the conversation. Do not include a fee, salary or any \
figure the client did not say themselves.

Examples of the register:
"Looking for Indonesian helper for elderly care. Mother 78, diabetes. Contact via WhatsApp."
"Indonesian helper, 3 years elderly care experience in Hong Kong. Available immediately."
"""


async def write_summary(
    *, history: str, latest: str, collected: dict[str, Any], service_type: str | None
) -> str:
    """§21 — never asked, written from the conversation just before insert.

    Falls back to a plain join of the captured fields: a lead with a mechanical
    summary is worth far more than no lead, and this runs on the handover path
    where the client is already waiting.
    """
    details = "; ".join(
        f"{key}: {value}"
        for key, value in (collected or {}).items()
        if str(value or "").strip() and str(value).lower() != UNANSWERED
    )
    try:
        from app.graph.llm import complete  # here: app.graph imports services

        text = await complete(
            SUMMARY_SYSTEM,
            f"Enquiry type: {service_type or 'general'}\n\n"
            f"Conversation:\n{history or '(none)'}\n\n"
            f"Latest message(s):\n{latest}\n\n"
            f"Details captured: {details or '(none)'}\n\nSummary:",
            temperature=0.2,
            max_tokens=120,
        )
    except Exception:  # noqa: BLE001 - never block the handover on a summary
        logger.exception("Lead summary generation failed — falling back to the field list")
        return details[:2000]

    text = " ".join((text or "").split()).strip().strip('"')
    return (text or details)[:2000]


async def create_if_absent(
    *,
    kind: str,
    name: str,
    phone: str,
    collected: dict[str, Any] | None = None,
    service_type: str | None = None,
    summary: str = "",
    owner_profile_id: str | None = None,
) -> dict[str, Any] | None:
    """§1B — create the lead only if this phone has none. Never updates.

    An existing lead is returned untouched so the caller can point the ticket at
    it. Nothing on it is rewritten: whatever sales has since changed on that row
    is theirs, and the current enquiry is recorded on the ticket instead.

    Never raises: a lead is bookkeeping, and failing to write one must not stop
    the client getting an answer or the agent getting a ticket.
    """
    if not settings.lead_enabled:
        return None
    if kind not in TABLE_FOR:
        logger.warning("No lead table for contact type %r", kind)
        return None

    table = TABLE_FOR[kind]

    existing = await find_by_phone(phone, kind)
    if existing:
        logger.info(
            "%s already has lead %s — reusing it, no new row (§1B)",
            phone,
            existing.get("lead_number"),
        )
        return {**existing, "lead_existed": True}

    branch_id = await resolve_branch_id()
    if not branch_id:
        logger.error("No branch resolved — cannot create a %s lead for %s", kind, phone)
        return None

    payload = _fields(
        kind,
        name=(name or "").strip() or fallback_name(phone),
        phone=phone,
        collected=collected or {},
        service_type=service_type,
        summary=summary,
    )
    payload.update(
        {
            "tenant_id": settings.tenant_id or None,
            "branch_id": branch_id,
            "lead_number": await next_lead_number(table),
            # status ('new'), temperature and received_at come from §0 defaults.
        }
    )
    if owner_profile_id:
        payload["owner_profile_id"] = owner_profile_id

    try:
        created = await db.insert(table, payload)
    except Exception:  # noqa: BLE001
        logger.exception("Lead creation failed for %s on %s", phone, table)
        return None

    if created:
        logger.info(
            "Created %s lead %s (%s) for %s",
            kind,
            created.get("lead_number"),
            created.get("interest_type") or created.get("nationality") or "-",
            phone,
        )
        return {**created, "lead_table": table, "lead_kind": kind, "lead_existed": False}
    return None
