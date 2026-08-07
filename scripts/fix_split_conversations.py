"""Find and merge conversations the bot split in two.

Early builds wrote ``customer_number`` in E.164 ('+6591234567') while the
WhatsApp portal stores bare digits ('6591234567'). Because customer_number is
unique, that created a second row per client: the portal's row holding the
client's messages, ours holding the bot's replies.

    python scripts/fix_split_conversations.py           # report only
    python scripts/fix_split_conversations.py --merge   # move rows and delete

Merging moves messages, tickets and handovers onto the portal's row and deletes
the duplicate. Run the report first and read it.
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.supabase import db  # noqa: E402
from app.utils import digits_only  # noqa: E402


async def message_count(conversation_id: int) -> int:
    rows = await db.execute(
        db.table("wp_chat_messages").select("id").eq("conversation_id", conversation_id).limit(500)
    )
    return len(rows.data or [])


async def main() -> None:
    merge = "--merge" in sys.argv

    # PostgREST caps a response at 1000 rows, so page through explicitly —
    # a single .limit(5000) silently returns a partial picture.
    rows: list[dict] = []
    page = 0
    while True:
        result = await db.execute(
            db.table("wp_chat_conversations")
            .select("*")
            .order("id", desc=False)
            .range(page * 1000, page * 1000 + 999)
        )
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < 1000:
            break
        page += 1
    print(f"scanned {len(rows)} conversations\n")

    by_digits: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = digits_only(row.get("customer_number") or "")
        if key:
            by_digits[key].append(row)

    duplicates = {k: v for k, v in by_digits.items() if len(v) > 1}
    if not duplicates:
        print(f"No split conversations found across {len(rows)} rows.")
        return

    print(f"{len(duplicates)} number(s) with more than one conversation row:\n")
    plan: list[tuple[dict, dict]] = []

    for key, group in duplicates.items():
        # The portal's row is the one stored as bare digits.
        keeper = next((r for r in group if r.get("customer_number") == key), None)
        if keeper is None:
            print(f"  {key}: no portal-format row — skipping, resolve by hand")
            for r in group:
                print(f"      id={r['id']} customer_number={r['customer_number']!r}")
            continue

        print(f"  {key}")
        for row in group:
            count = await message_count(row["id"])
            role = "KEEP  (portal)" if row["id"] == keeper["id"] else "MERGE (ours)"
            print(f"      {role}  id={row['id']:<6} {row['customer_number']!r:<16} "
                  f"{count} messages  bot_status={row.get('bot_status')!r}")
            if row["id"] != keeper["id"]:
                plan.append((row, keeper))

    if not plan:
        return

    if not merge:
        print(f"\n{len(plan)} row(s) would be merged. Re-run with --merge to apply.")
        return

    print(f"\nMerging {len(plan)} row(s)...")
    for duplicate, keeper in plan:
        dup_id, keep_id = duplicate["id"], keeper["id"]
        for table in ("wp_chat_messages", "cb_tickets", "cb_handovers"):
            await db.execute(
                db.table(table).update({"conversation_id": keep_id}).eq("conversation_id", dup_id)
            )
        # Carry the chatbot state across to the surviving row.
        patch = {
            k: duplicate.get(k)
            for k in (
                "bot_status", "intent", "service_type", "contact_type",
                "matched_employer_id", "matched_candidate_id", "matched_supplier_id",
                "matched_case_id", "langgraph_thread_id", "assignment_rule",
            )
            if duplicate.get(k) is not None
        }
        if patch:
            await db.execute(db.table("wp_chat_conversations").update(patch).eq("id", keep_id))
        await db.execute(db.table("wp_chat_conversations").delete().eq("id", dup_id))
        print(f"  merged {dup_id} -> {keep_id}")

    print("\nDone. Verify with: python scripts/watch_conversation.py <number>")


if __name__ == "__main__":
    asyncio.run(main())
