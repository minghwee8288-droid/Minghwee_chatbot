"""Record the replaced threads that predate cb_replaced_threads.

    python scripts/backfill_replaced_threads.py                         # shows them, writes nothing
    python scripts/backfill_replaced_threads.py --apply --expect-ref <project ref>

A thread is taken only if it has checkpoints, no wp_chat_conversations row points at
it, and its id reads conv-<conversation id>-<hex>, which is how new_thread_id names
one. The conversation id comes from that name. It need not still exist: on 2026-11-04,
3 of the 6 threads belong to conversations that have since been deleted, and those are
recorded as well, under their own reason, because nothing else will ever find them.
Anything with another shape is listed and left out.

A row already recorded is left as it is (ON CONFLICT DO NOTHING), so it can be run twice.

Touches only cb_replaced_threads. Deletes nothing, anywhere.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from scripts.target_guard import require_ref  # noqa: E402

REASON_CONVERSATION_KEPT = "backfill"
REASON_CONVERSATION_GONE = "backfill_conversation_gone"

# Kept as one statement so the PGlite check runs exactly what the script runs.
SELECT_REPLACED = r"""
select c.thread_id,
       (substring(c.thread_id from '^conv-([0-9]+)-[0-9a-f]+$'))::bigint as conversation_id,
       max(c.checkpoint->>'ts') as last_used,
       exists (
         select 1 from public.wp_chat_conversations w
         where w.id = (substring(c.thread_id from '^conv-([0-9]+)-[0-9a-f]+$'))::bigint
       ) as conversation_exists
from public.cb_checkpoints c
where not exists (
  select 1 from public.wp_chat_conversations w where w.langgraph_thread_id = c.thread_id
)
group by c.thread_id
order by last_used
"""

INSERT_REPLACED = """
insert into public.cb_replaced_threads (thread_id, tenant_id, conversation_id, replaced_at, reason)
values (%s, %s, %s, %s, %s)
on conflict (thread_id) do nothing
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the rows; without it nothing is written")
    parser.add_argument("--expect-ref", help="the Supabase project ref this run may write to")
    args = parser.parse_args()
    if args.apply:
        if not args.expect_ref:
            parser.error("--apply needs --expect-ref")
        require_ref(args.expect_ref)
    if not settings.supabase_db_url:
        print("SUPABASE_DB_URL is not set")
        return 2

    import psycopg

    with psycopg.connect(settings.supabase_db_url, autocommit=False) as conn:
        if not args.apply:
            conn.execute("SET TRANSACTION READ ONLY")
        table = conn.execute("select to_regclass('public.cb_replaced_threads')").fetchone()[0]
        if args.apply and table is None:
            print("cb_replaced_threads does not exist: apply replaced_threads_001_create.sql first")
            return 2

        rows = conn.execute(SELECT_REPLACED).fetchall()
        taken = 0
        for thread_id, conversation_id, last_used, conversation_exists in rows:
            if conversation_id is None:
                print(f"  left out  {thread_id[:24]}: not a conv-<id>-<hex> name")
                continue
            reason = REASON_CONVERSATION_KEPT if conversation_exists else REASON_CONVERSATION_GONE
            print(f"  {thread_id[:16]}...  conversation {conversation_id}  last used {last_used[:10]}  {reason}")
            taken += 1
            if args.apply:
                conn.execute(
                    INSERT_REPLACED,
                    (thread_id, settings.tenant_id or None, conversation_id, last_used, reason),
                )
        if args.apply:
            conn.commit()
            print(f"{taken} thread(s) recorded (any already there were left as they were)")
        else:
            print(f"{taken} thread(s) would be recorded. Nothing written (table exists: {table is not None}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
