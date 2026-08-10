"""Hand a conversation back to the bot, for repeat testing.

    python scripts/reset_conversation.py +917970027379
    python scripts/reset_conversation.py +917970027379 --keep-history
    python scripts/reset_conversation.py +917970027379 --wipe-lead

By default this is a full clean slate: stored messages, tickets and handovers
for that conversation are deleted, bot_status goes back to 'bot_active' and a
fresh LangGraph thread is started.

Deleting the messages is the part that actually matters. The bot does not read
its memory out of the LangGraph checkpoint alone — every turn rebuilds
``history_text`` from wp_chat_messages (see app/api/webhook.py) and looks up the
last ticket. A new thread id on its own leaves both in place, so the bot picks
up the previous enquiry as if nothing had happened.

This also deletes the conversation's old LangGraph checkpoint rows
(checkpoints / checkpoint_blobs / checkpoint_writes, keyed by thread_id) —
without it, resetting the conversation row still leaves collected_info,
asked_field_counts and everything else the checkpointer tracked sitting under
the abandoned thread id forever. Harmless to the next run, which gets a fresh
thread id either way, but it is real state from a "deleted" conversation still
sitting in the database, and it accumulates by one orphaned thread per reset.

``--keep-history`` resets only the routing state and leaves the transcript
alone, for when you want to test how the bot resumes an existing thread.

``--wipe-lead`` additionally deletes this number's ``leads_candidate`` row.
That table is keyed by phone and has no conversation_id, so the ordinary wipe
above cannot reach it — and §1B makes a lead permanent, so without this the
second test run is always a returning client and the first-contact flow cannot
be tested twice. Left off by default.

``leads`` is NEVER touched, with or without the flag. The employer lead table
holds real sales pipeline the team works from, and a test number colliding with
it is not a reason to delete a row nobody can get back. If an employer lead is
genuinely in the way, delete it by hand knowing what it is.

TEST NUMBERS ONLY — this deletes rows the WhatsApp portal also owns.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db.supabase import db  # noqa: E402
from app.services import conversation as conversation_service  # noqa: E402
from app.services import lead as lead_service  # noqa: E402
from app.utils import normalize_phone  # noqa: E402

# Deleted child-first: cb_handovers and cb_tickets reference the conversation,
# and cb_handovers rows point at a ticket.
WIPE_TABLES = ("cb_handovers", "cb_tickets", "wp_chat_messages")

# LangGraph's own tables (app/graph/graph.py's AsyncPostgresSaver.setup()),
# all keyed by thread_id. Created over a raw psycopg connection, not through
# the Supabase project — so they are not necessarily in PostgREST's schema
# cache and db.table(...) is not a reliable way to reach them. This script
# connects to SUPABASE_DB_URL directly instead, the same DSN the checkpointer
# itself uses.
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


async def _count(table: str, conversation_id: int) -> int:
    result = await db.execute(
        db.table(table).select("id").eq("conversation_id", conversation_id).limit(1000)
    )
    return len(result.data or [])


def _wipe_checkpoints(thread_id: str | None) -> None:
    """Delete the abandoned thread's rows from LangGraph's checkpoint tables.

    Synchronous on purpose: psycopg's async mode cannot run on Windows'
    default ProactorEventLoop (see run.py), and a one-off CLI script has no
    reason to fight that for a handful of DELETEs. A plain blocking connect
    is instant here — nothing else is running concurrently.
    """
    if not thread_id:
        return
    if not settings.supabase_db_url:
        print("  SUPABASE_DB_URL not set — checkpoints are in-memory only, nothing to clear")
        return

    import psycopg

    try:
        with psycopg.connect(settings.supabase_db_url, autocommit=True) as conn:
            with conn.cursor() as cur:
                total = 0
                for table in CHECKPOINT_TABLES:
                    cur.execute(f"DELETE FROM {table} WHERE thread_id = %s", (thread_id,))
                    total += cur.rowcount
                print(f"  cleared LangGraph checkpoint for thread {thread_id} ({total} row(s))")
    except Exception as exc:  # noqa: BLE001 - an uninitialised checkpointer must not block the reset
        print(f"  could not clear checkpoint rows for thread {thread_id}: {exc}")


async def _wipe_candidate_lead(phone: str) -> None:
    """Delete this number's leads_candidate row, if it has one.

    Deliberately narrow: find_by_phone is asked for the candidate table by name,
    so an employer lead on the same number is found by nothing here and cannot
    be deleted by accident.
    """
    lead = await lead_service.find_by_phone(phone, lead_service.CANDIDATE)
    if not lead:
        print("  no leads_candidate row for this number")
        return
    await db.execute(
        db.table(lead_service.CANDIDATE_TABLE).delete().eq("id", lead["id"])
    )
    print(f"  cleared leads_candidate ({lead.get('lead_number')} — {lead.get('full_name')})")


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    keep_history = "--keep-history" in sys.argv
    wipe_lead = "--wipe-lead" in sys.argv
    if not args:
        print(__doc__)
        return

    phone = normalize_phone(args[0])
    conversation = await conversation_service.get_by_phone(phone)
    if not conversation:
        print(f"No conversation for {phone}")
        return

    cid = conversation["id"]
    old_thread_id = conversation.get("langgraph_thread_id")
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

    # A fresh thread id is minted below regardless of --keep-history, so the
    # old thread's checkpoint is abandoned either way — always clear it.
    _wipe_checkpoints(old_thread_id)

    if wipe_lead:
        await _wipe_candidate_lead(phone)

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
        # Identity too, or the next run inherits it. contact_type is only
        # re-derived while it is unknown, so a number left branded 'candidate'
        # by an earlier test had every later enquiry — including "I am looking
        # for a maid" — routed down the candidate flow.
        contact_type=None,
        matched_employer_id=None,
        matched_candidate_id=None,
        matched_supplier_id=None,
        matched_case_id=None,
    )
    print("  -> bot_status='bot_active', fresh thread. Send another message to test.")


if __name__ == "__main__":
    asyncio.run(main())
