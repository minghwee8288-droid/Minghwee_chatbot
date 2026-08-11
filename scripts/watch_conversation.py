"""Show what the bot did with a conversation — transcript, state, tickets, handovers.

    python scripts/watch_conversation.py                 # most recent conversation
    python scripts/watch_conversation.py 6591234567      # a specific number
    python scripts/watch_conversation.py --follow        # refresh every 3s while testing
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.supabase import db  # noqa: E402
from app.services import conversation as conversation_service  # noqa: E402

BOT_STATUS_MEANING = {
    "bot_active": "bot is answering",
    "human_active": "handed over — bot is silent",
    "none": "bot not engaged",
}


async def latest_conversation() -> dict | None:
    result = await db.execute(
        db.table("wp_chat_conversations").select("*").order("updated_at", desc=True).limit(1)
    )
    rows = result.data or []
    return rows[0] if rows else None


async def show(phone: str | None) -> None:
    if phone:
        # Via the service so it matches however the portal stored the number.
        conversation = await conversation_service.get_by_phone(phone)
    else:
        conversation = await latest_conversation()

    if not conversation:
        print("No conversation found yet — send a WhatsApp message to the channel first.")
        return

    cid = conversation["id"]
    status = conversation.get("bot_status") or "none"
    print("=" * 78)
    print(f"conversation {cid}  {conversation.get('customer_number')}  "
          f"({conversation.get('customer_name')})")
    print(f"  bot_status      : {status}  <- {BOT_STATUS_MEANING.get(status, '?')}")
    print(f"  contact_type    : {conversation.get('contact_type')}")
    print(f"  intent          : {conversation.get('intent')}")
    print(f"  service_type    : {conversation.get('service_type')}")
    print(f"  assignment_rule : {conversation.get('assignment_rule')}")
    print(f"  assigned_user_id: {conversation.get('assigned_user_id')}")
    print(f"  thread          : {conversation.get('langgraph_thread_id')}")

    messages = await db.execute(
        db.table("wp_chat_messages")
        .select("direction, body, is_bot, sent_by, created_at")
        .eq("conversation_id", cid)
        .order("created_at", desc=False)
        .limit(50)
    )
    print("\n  transcript")
    for m in messages.data or []:
        if m.get("direction") == "inbound":
            who = "CLIENT"
        else:
            who = "BOT   " if m.get("is_bot") else "AGENT "
        body = (m.get("body") or "").replace("\n", " ")[:120]
        print(f"    {who} | {body}")

    tickets = await db.select_many("cb_tickets", "*", limit=10, conversation_id=cid)
    if tickets:
        print("\n  tickets")
        for t in tickets:
            # service_type is an array — a merged ticket can cover more than one.
            services = ", ".join(t.get("service_type") or [])
            print(f"    {t.get('ticket_number')}  [{services}]  status={t.get('status')}  "
                  f"priority={t.get('priority')}  rule={t.get('assignment_rule')}")
            print(f"      description: {t.get('description') or '(none)'}")
            print(f"      captured: {t.get('captured_info')}")

    handovers = await db.select_many("cb_handovers", "*", limit=10, conversation_id=cid)
    if handovers:
        print("\n  handovers")
        for h in handovers:
            print(f"    {h.get('direction')}  reason={h.get('reason')}  "
                  f"agent={h.get('agent_profile_id')}")


async def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--follow"]
    follow = "--follow" in sys.argv
    phone = args[0] if args else None

    if not follow:
        await show(phone)
        return

    print("Watching — Ctrl+C to stop\n")
    while True:
        print("\033[2J\033[H", end="")  # clear screen
        await show(phone)
        await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
