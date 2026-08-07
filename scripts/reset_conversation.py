"""Hand a conversation back to the bot, for repeat testing.

    python scripts/reset_conversation.py +917970027379
    python scripts/reset_conversation.py +917970027379 --keep-history

By default this is a full clean slate: stored messages, tickets and handovers
for that conversation are deleted, bot_status goes back to 'bot_active' and a
fresh LangGraph thread is started.

Deleting the messages is the part that actually matters. The bot does not read
its memory out of the LangGraph checkpoint alone — every turn rebuilds
``history_text`` from wp_chat_messages (see app/api/webhook.py) and looks up the
last ticket. A new thread id on its own leaves both in place, so the bot picks
up the previous enquiry as if nothing had happened.

``--keep-history`` resets only the routing state and leaves the transcript
alone, for when you want to test how the bot resumes an existing thread.

TEST NUMBERS ONLY — this deletes rows the WhatsApp portal also owns.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.supabase import db  # noqa: E402
from app.services import conversation as conversation_service  # noqa: E402
from app.utils import normalize_phone  # noqa: E402

# Deleted child-first: cb_handovers and cb_tickets reference the conversation,
# and cb_handovers rows point at a ticket.
WIPE_TABLES = ("cb_handovers", "cb_tickets", "wp_chat_messages")


async def _count(table: str, conversation_id: int) -> int:
    result = await db.execute(
        db.table(table).select("id").eq("conversation_id", conversation_id).limit(1000)
    )
    return len(result.data or [])


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    keep_history = "--keep-history" in sys.argv
    if not args:
        print(__doc__)
        return

    phone = normalize_phone(args[0])
    conversation = await conversation_service.get_by_phone(phone)
    if not conversation:
        print(f"No conversation for {phone}")
        return

    cid = conversation["id"]
    print(f"conversation {cid} ({phone}) was bot_status={conversation.get('bot_status')!r}")

    if keep_history:
        remaining = await _count("wp_chat_messages", cid)
        print(f"  --keep-history: {remaining} message(s) left in place")
        print("  the bot will still see the previous enquiry in its history")
    else:
        for table in WIPE_TABLES:
            deleted = await _count(table, cid)
            await db.execute(db.table(table).delete().eq("conversation_id", cid))
            print(f"  cleared {table} ({deleted} row(s))")

    await conversation_service.update(
        cid,
        bot_status=conversation_service.BOT_ACTIVE,
        status="open",
        langgraph_thread_id=conversation_service.new_thread_id(cid),
        intent=None,
        service_type=None,
        assignment_rule=None,
        last_message_body=None,
        last_bot_reply_at=None,
    )
    print("  -> bot_status='bot_active', fresh thread. Send another message to test.")


if __name__ == "__main__":
    asyncio.run(main())
