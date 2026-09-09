"""Prove the case lookup works, against the real database, with disposable rows.

`cases` is empty and will stay empty until the portal side starts opening them,
so there is nothing live to test contact.get_cases() against. This script
creates a throwaway employer with three cases hanging off it — one reachable
down each of the three paths get_cases() reads — runs the real lookup, and
deletes everything again.

    python scripts/seed_case_testdata.py            create, verify, remove
    python scripts/seed_case_testdata.py --keep     leave the rows in place
    python scripts/seed_case_testdata.py --remove   delete a --keep run's rows

WRITES, unlike every other selfcheck in this folder. It is the one exception
and it is confined to rows it created itself: every insert carries the marker
below in a name or a reference number, and every delete is keyed on an id this
run resolved through that marker. It never touches a real employer, a real case
or a real lead. The bot itself still only ever reads these tables — see the
read-only note above contact.get_cases.

The three paths, which is the whole point of the shape below:

  A  leads.converted_case_id                     the lead-first path
  B  employer_service_requests.converted_case_id the returning-employer path
  C  cases.placement_id                          the structural path

cases.placement_id is NOT NULL, so a case ALWAYS has a placement. To prove A
and B are doing real work, their cases are given placements belonging to a
DIFFERENT employer — so path C cannot reach them, and if the lookup finds them
it is because the converted_case_id column was read.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db.supabase import db  # noqa: E402
from app.services import contact as contact_service  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

# Everything this script creates carries one of these. Nothing is ever deleted
# that does not.
MARKER = "CASE TEST"
CASE_PREFIX = "CS-TEST-"
LEAD_PREFIX = "L-TEST-CASE-"

# A reserved number, not on the bot allowlist and not a real client's.
# +6590000288 and +6590000377 are BOTH taken by seed_reset_ui_testdata.py (the
# second is its deliberately-blocked lead, which this script's phone fallback
# would otherwise find), so this is a third number. Checked, not assumed.
TEST_PHONE = "+65 9000 0399"

# Both of these columns are CHECK-constrained and neither vocabulary is
# documented anywhere readable, so they were derived by attempting inserts
# against the live constraint on 2026-09-09:
#
#   cases.status   active | completed | cancelled | on_hold
#                  (rejects open, closed, in_progress, new, pending, draft)
#   cases.country  PH | ID | MM
#                  (rejects "Philippines", "SG", "PHL")
#
# The list is still walked rather than hard-coded to one value, so if the
# portal widens or changes the constraint this script reports the new answer
# instead of failing with a constraint name. Same reason the country list has
# more than the two it needs.
#   cases.case_type   First-time hire | Home leave | Transfer | Renewal |
#                     Direct hire | NULL
#                     (rejects "Passport renewal", "Replacement", "Insurance",
#                     "Other", and every snake_case spelling)
#   cases.current_stage_key  free text, no constraint
#
# The case_type list is the one worth reading twice: the platform has no case
# type for a PASSPORT RENEWAL or a REPLACEMENT, which are two of the services
# the bot runs a full intake for. See the note in CLAUDE.md section 9.
STATUS_CANDIDATES = ("active", "on_hold", "completed", "open", "in_progress")
COUNTRY_CANDIDATES = ("PH", "ID", "MM")
CASE_TYPE_CANDIDATES = (
    "First-time hire", "Home leave", "Transfer", "Renewal", "Direct hire",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _delete(table: str, **eq) -> int:
    query = db.table(table).delete()
    for column, value in eq.items():
        query = query.eq(column, value)
    result = await db.execute(query)
    return len(result.data or [])


async def _branch_id() -> str:
    row = await db.select_one("branches", "id", tenant_id=settings.tenant_id)
    if not row:
        raise SystemExit("No branch on this tenant — cannot create a case.")
    return row["id"]


async def _a_candidate() -> tuple[str | None, str]:
    """An existing candidate to name as the helper. Never created, never edited."""
    row = await db.select_one("candidates", "id,full_name", nationality="PH")
    if not row:
        return None, ""
    return row["id"], str(row.get("full_name") or "")


async def _insert_case(
    *, branch_id: str, placement_id: str, number: str, case_type: str, country: str
) -> tuple[dict, str]:
    """Insert one case, discovering the accepted status value as it goes."""
    last_error = ""
    assert country in COUNTRY_CANDIDATES, f"{country} is not an accepted country code"
    assert case_type in CASE_TYPE_CANDIDATES, f"{case_type} is not an accepted case type"
    for status in STATUS_CANDIDATES:
        payload = {
            "id": str(uuid.uuid4()),
            "tenant_id": settings.tenant_id,
            "branch_id": branch_id,
            "placement_id": placement_id,
            "case_number": number,
            "country": country,
            "current_stage_key": "documents",
            "status": status,
            "case_type": case_type,
            "opened_at": _now(),
        }
        try:
            row = await db.insert("cases", payload)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)[:600]
            continue
        if row:
            return row, status
    raise SystemExit(
        f"Could not insert a case with any of {STATUS_CANDIDATES}.\n"
        f"Last error: {last_error}"
    )


async def create() -> dict:
    branch_id = await _branch_id()
    candidate_id, helper_name = await _a_candidate()

    # The employer under test, and a decoy who owns the placements for the two
    # cases that must NOT be reachable structurally.
    employer = await db.insert(
        "employers",
        {
            "id": str(uuid.uuid4()),
            "tenant_id": settings.tenant_id,
            "branch_id": branch_id,
            "display_name": f"{MARKER} EMPLOYER",
            "phone": TEST_PHONE,
        },
    )
    decoy = await db.insert(
        "employers",
        {
            "id": str(uuid.uuid4()),
            "tenant_id": settings.tenant_id,
            "branch_id": branch_id,
            "display_name": f"{MARKER} DECOY EMPLOYER",
        },
    )

    async def placement(owner: dict, with_candidate: bool) -> dict:
        return await db.insert(
            "placements",
            {
                "id": str(uuid.uuid4()),
                "tenant_id": settings.tenant_id,
                "employer_id": owner["id"],
                "candidate_id": candidate_id if with_candidate else None,
            },
        )

    own_placement = await placement(employer, True)
    decoy_a = await placement(decoy, False)
    decoy_b = await placement(decoy, False)

    case_c, status_used = await _insert_case(
        branch_id=branch_id, placement_id=own_placement["id"],
        number=f"{CASE_PREFIX}C", case_type="First-time hire", country="PH",
    )
    case_a, _ = await _insert_case(
        branch_id=branch_id, placement_id=decoy_a["id"],
        number=f"{CASE_PREFIX}A", case_type="Home leave", country="PH",
    )
    case_b, _ = await _insert_case(
        branch_id=branch_id, placement_id=decoy_b["id"],
        number=f"{CASE_PREFIX}B", case_type="Renewal", country="ID",
    )

    # Path A: a lead the office converted onto this employer and this case.
    lead = await db.insert(
        "leads",
        {
            "id": str(uuid.uuid4()),
            "tenant_id": settings.tenant_id,
            "branch_id": branch_id,
            "lead_number": f"{LEAD_PREFIX}A",
            "full_name": f"{MARKER} LEAD",
            "phone": TEST_PHONE,
            "source": "chatbot",
            "temperature": "warm",
            "status": "converted",
            "received_at": _now(),
            "converted_employer_id": employer["id"],
            "converted_case_id": case_a["id"],
            "converted_at": _now(),
        },
    )

    # Path B: a service request the office actioned straight into a case.
    request = await db.insert(
        "employer_service_requests",
        {
            "id": str(uuid.uuid4()),
            "tenant_id": settings.tenant_id,
            "branch_id": branch_id,
            "employer_id": employer["id"],
            "service_type": "passport_renewal",
            "status": "converted",
            "converted_case_id": case_b["id"],
        },
    )

    return {
        "employer": employer, "decoy": decoy, "lead": lead, "request": request,
        "cases": [case_c, case_a, case_b],
        "placements": [own_placement, decoy_a, decoy_b],
        "helper_name": helper_name, "status_used": status_used,
    }


async def remove() -> None:
    """Delete every row carrying the marker, children first."""
    # Order matters: leads and service requests hold a foreign key to cases,
    # and cases hold one to placements. Deleting a parent first fails.
    counts = {
        "employer_service_requests": 0, "leads": 0,
        "cases": 0, "placements": 0, "employers": 0,
    }
    employers = await db.select_many("employers", "id,display_name")
    ours = [e["id"] for e in employers if MARKER in str(e.get("display_name") or "")]
    for employer_id in ours:
        counts["employer_service_requests"] += await _delete(
            "employer_service_requests", employer_id=employer_id
        )
    # By number for the lead this script creates, and by NAME for any lead a
    # live turn opened on the way past: running the real info_collector against
    # the seeded employer calls _open_lead_early and mints an ordinary
    # L-YYYY-NNNN row carrying the employer's name. Found by counting the table
    # afterwards rather than by expecting it.
    leads = await db.select_many("leads", "id,lead_number,full_name")
    for row in leads:
        by_number = str(row.get("lead_number") or "").startswith(LEAD_PREFIX)
        by_name = MARKER in str(row.get("full_name") or "")
        if by_number or by_name:
            counts["leads"] += await _delete("leads", id=row["id"])

    cases = await db.select_many("cases", "id,case_number,placement_id")
    case_placements = []
    for row in cases:
        if str(row.get("case_number") or "").startswith(CASE_PREFIX):
            case_placements.append(row.get("placement_id"))
            counts["cases"] += await _delete("cases", id=row["id"])
    for placement_id in filter(None, case_placements):
        counts["placements"] += await _delete("placements", id=placement_id)
    for employer_id in ours:
        # Any placement of the test employer that had no case on it.
        counts["placements"] += await _delete("placements", employer_id=employer_id)
        counts["employers"] += await _delete("employers", id=employer_id)

    print("Removed:", ", ".join(f"{k}={v}" for k, v in counts.items()))


async def verify(seeded: dict) -> bool:
    employer_id = seeded["employer"]["id"]
    print(f"\nInserted 3 cases with status={seeded['status_used']!r} "
          f"(the first value cases_status_check accepted)\n")

    found = await contact_service.get_cases(employer_id, TEST_PHONE)
    numbers = sorted(c["case_number"] for c in found)
    print("contact.get_cases() returned:")
    for case in found:
        print(f"  {case['case_number']:14} {case['case_type']:18} "
              f"{case['country']:12} helper={case['helper_name'] or '-':20} "
              f"stage={case['stage']} status={case['status']}")

    expected = sorted(f"{CASE_PREFIX}{s}" for s in "ABC")
    ok = numbers == expected
    print(f"\n  all three paths resolved: {ok}  ({numbers})")

    # The structural path alone must NOT see A and B — that is what proves the
    # two converted_case_id columns are being read rather than coincidence.
    structural = await contact_service._cases_via_placements(employer_id)
    only_c = sorted(str(r.get("case_number")) for r in structural)
    print(f"  placements path alone sees: {only_c}  "
          f"(must be just {CASE_PREFIX}C)")
    ok = ok and only_c == [f"{CASE_PREFIX}C"]

    single = await contact_service.find_active_case(employer_id)
    print(f"  find_active_case() -> {single} "
          f"({'a real case id' if single else 'NOTHING - regression'})")
    ok = ok and bool(single)

    helper = seeded["helper_name"]
    named = [c for c in found if c["helper_name"]]
    print(f"  the helper is named on the structural case: "
          f"{[c['case_number'] for c in named]} -> {helper!r}")
    ok = ok and bool(named)
    return ok


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="leave the rows in place")
    parser.add_argument("--remove", action="store_true", help="delete them and stop")
    args = parser.parse_args()

    if args.remove:
        await remove()
        return

    # Clear anything a previous --keep run left, so this is repeatable.
    await remove()
    seeded = await create()
    try:
        ok = await verify(seeded)
    finally:
        if args.keep:
            print(f"\n--keep: rows LEFT IN PLACE on {TEST_PHONE}. "
                  f"Remove them with --remove.")
        else:
            print()
            await remove()
    print("\n" + ("ALL PASS" if ok else "FAILED"))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
