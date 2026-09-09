"""Make a test number look BRAND NEW again — master records included.

`reset_conversation.py` clears the chat: messages, tickets, handovers, the
LangGraph checkpoint, and the identity on the conversation row. It deliberately
stops there, and that is correct for a real client — `employers`, `profiles` and
`leads` are the platform's records, shared with two other products, and the bot
does not own them.

But it means a number the PORTAL has converted can never be reset. Live,
2026-09-09: the agency reset conversation 3766 and deleted its lead, and the bot
still opened with "Hi tunaktun" — because at 12:16 that day somebody had
converted the lead on the portal, which created an `employers` row AND a
synthetic `profiles` row (employer+<hex>@no-email.local). Neither is chatbot
data; neither reset touches them; and `employers` is what identify() matches a
phone against FIRST. So the number stays recognised forever.

This script closes that gap for a TEST number, and nothing else:

    python scripts/purge_test_contact.py +917970027379           # show only
    python scripts/purge_test_contact.py +917970027379 --yes     # delete

DESTRUCTIVE, and the only script here that deletes a master record. Three
guards, because the cost of getting this wrong is a real client's file:

  * it prints everything it found and deletes NOTHING without --yes;
  * it REFUSES a contact with any placement, because a placement means we have
    actually placed a helper with them and they are not a test number. --force
    overrides, and you should have a reason;
  * every delete is keyed on a resolved row id, never on the phone number, so
    the loose last-four-digits fallback in the lookup cannot sweep up a
    stranger's row.

Run `reset_conversation.py` as well, for the messages and the checkpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.supabase import db  # noqa: E402
from app.utils import digits_only, phone_variants  # noqa: E402

# Where a phone number lives, and what to call the row when we print it.
LOOKUPS = (
    ("employers", ("phone",), "display_name"),
    ("profiles", ("phone_e164", "phone"), "display_name"),
    ("candidates", ("phone",), "full_name"),
    ("leads", ("phone",), "full_name"),
    ("leads_candidate", ("phone",), "full_name"),
)


async def _rows_for(table: str, columns: tuple[str, ...], phone: str) -> list[dict]:
    """Every row in `table` whose phone matches, however it is formatted."""
    variants = phone_variants(phone)
    digits = digits_only(phone)
    if not variants or len(digits) < 8:
        return []
    tail, last4 = digits[-8:], digits[-4:]
    found: dict[str, dict] = {}
    for column in columns:
        try:
            exact = await db.execute(
                db.table(table).select("*").in_(column, variants).limit(25)
            )
            for row in exact.data or []:
                found[row["id"]] = row
            loose = await db.execute(
                db.table(table).select("*").ilike(column, f"%{last4}%").limit(50)
            )
            for row in loose.data or []:
                if digits_only(row.get(column)).endswith(tail):
                    found[row["id"]] = row
        except Exception:  # noqa: BLE001 - a column this table lacks is not an error
            continue
    return list(found.values())


async def _count(table: str, column: str, value) -> int:
    try:
        result = await db.execute(
            db.table(table).select("id", count="exact").eq(column, value).limit(1)
        )
        return int(result.count or 0)
    except Exception:  # noqa: BLE001
        return 0


async def _delete(table: str, **eq) -> int:
    query = db.table(table).delete()
    for column, value in eq.items():
        query = query.eq(column, value)
    result = await db.execute(query)
    return len(result.data or [])


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phone")
    parser.add_argument("--yes", action="store_true", help="actually delete")
    parser.add_argument("--force", action="store_true",
                        help="delete even if the contact has a placement")
    args = parser.parse_args()

    print(f"Looking up {args.phone}\n")
    found: dict[str, list[dict]] = {}
    for table, columns, name_col in LOOKUPS:
        rows = await _rows_for(table, columns, args.phone)
        if rows:
            found[table] = rows
        for row in rows:
            print(f"  {table:16} {row.get(name_col)!r:34} id={row['id']}")

    conversations = await db.select_many(
        "wp_chat_conversations", "id,customer_number,customer_name,contact_type",
        limit=25, customer_number=digits_only(args.phone),
    )
    for row in conversations:
        print(f"  {'conversation':16} {row.get('customer_name')!r:34} id={row['id']} "
              f"contact_type={row.get('contact_type')}")

    if not found and not conversations:
        print("Nothing on this number — it is already new.")
        return

    # The conversation row is UPDATED by a reset, never deleted, so it is still
    # here after a successful purge and there is nothing left for this script to
    # do. Saying "re-run with --yes to delete all of the above" at that point
    # reads as though the number were still recognised, which is the opposite of
    # the truth.
    if not found:
        recognised = [r for r in conversations if r.get("contact_type")]
        if not recognised:
            print("\nNo master record on this number — it is already unrecognised.\n"
                  "The conversation row above survives a reset by design; its "
                  "identity is blank, which is what matters.")
            return

    # A placement means a helper was actually placed with them. That is a real
    # client, not a test number.
    employer_ids = [r["id"] for r in found.get("employers", [])]
    placements = sum([await _count("placements", "employer_id", e) for e in employer_ids])
    print(f"\n  placements on these employers: {placements}")
    if placements and not args.force:
        print("\nREFUSING: this contact has a placement, so it is not a test number.\n"
              "Pass --force if you are certain.")
        raise SystemExit(1)

    if not args.yes:
        print("\nNothing deleted. Re-run with --yes to delete all of the above.")
        return

    print("\nDeleting, children first:")
    counts: dict[str, int] = {}

    def note(table: str, n: int) -> None:
        if n:
            counts[table] = counts.get(table, 0) + n

    # cb_* rows first: they hold foreign keys to leads and employers.
    for row in conversations:
        note("cb_handovers", await _delete("cb_handovers", conversation_id=row["id"]))
        note("cb_tickets", await _delete("cb_tickets", conversation_id=row["id"]))
    for employer_id in employer_ids:
        note("cb_tickets", await _delete("cb_tickets", employer_id=employer_id))
        note("employer_service_requests",
             await _delete("employer_service_requests", employer_id=employer_id))

    # The conversation identity is blanked BEFORE the master records go, not
    # after. `wp_chat_conversations.matched_employer_id` is a foreign key to
    # employers(id) — found the hard way, 2026-09-09: deleting the employer
    # first fails outright with
    # "wp_chat_conversations_matched_employer_id_fkey", after the leads and
    # tickets have already gone, which leaves the number half-cleared and
    # still recognised. Blanking first also means that if anything below
    # fails, the number is ALREADY unrecognised, which is the outcome that
    # matters.
    for row in conversations:
        await db.execute(
            db.table("wp_chat_conversations").update({
                "contact_type": None, "matched_employer_id": None,
                "matched_candidate_id": None, "matched_supplier_id": None,
                "matched_case_id": None, "customer_name": None,
            }).eq("id", row["id"])
        )
        print(f"  blanked the identity on conversation {row['id']}")

    # employers.profile_id references profiles(id), so the employer goes first.
    for table in ("leads", "leads_candidate", "candidates", "employers", "profiles"):
        for row in found.get(table, []):
            note(table, await _delete(table, id=row["id"]))

    for table, n in sorted(counts.items()):
        print(f"  deleted {n:2} from {table}")

    left = [
        f"{table}={len(await _rows_for(table, cols, args.phone))}"
        for table, cols, _ in LOOKUPS
        if await _rows_for(table, cols, args.phone)
    ]
    print("\nStill on this number:", ", ".join(left) if left else "nothing — it is new again")
    print("Run reset_conversation.py too, for the messages and the checkpoint.")


if __name__ == "__main__":
    asyncio.run(main())
