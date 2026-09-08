"""Seed disposable contacts for testing reset-ui/ by hand.

    python scripts/seed_reset_ui_testdata.py          # create all three
    python scripts/seed_reset_ui_testdata.py --clean  # remove all three
    python scripts/seed_reset_ui_testdata.py --list   # show what exists now

Three contacts, chosen so that between them they exercise every branch the UI
has. Real data cannot be used for this: the interesting cases are a lead that
CAN be deleted and a lead that CANNOT, and finding out which is which on live
rows means deleting somebody's sales pipeline to see what happens.

  A  +6590000199  candidate  A full conversation -- 4 messages, a ticket, a
                             handover, checkpoint rows -- plus a
                             leads_candidate row with no blockers.
                             Every step of the clear has something to do.

  B  +6590000288  employer   A conversation plus an EMPLOYER lead (`leads`)
                             with nothing referencing it, so it is deletable.
                             Also has an `employers` master record and a
                             placement, so the panel identifies them as an
                             employer with 1 placement rather than "not in
                             master records", and the confirm dialog shows the
                             amber "this is an employer lead" warning.

  C  +6590000377  employer   A conversation plus an employer lead that IS
                             referenced -- one lead_activities row. The lead is
                             greyed out and cannot be ticked. Clear the
                             conversation and it succeeds while the lead is
                             refused, which is the partial-outcome path.

All three numbers are in the +65 9000 0xxx range, which is not a real
Singapore mobile prefix and belongs to nobody. Every statement in this file is
filtered by one of those three numbers or by an id it owns; nothing else is
ever in range.

Re-running creates a clean set: existing test rows are removed first, so this
is safe to run repeatedly between UI tests.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.config import settings  # noqa: E402

# Everything this script creates is identifiable by one of these.
PHONES = ("6590000199", "6590000288", "6590000377")
LEAD_NUMBERS = ("LC-TEST-9999", "L-TEST-8888", "L-TEST-8889")
EMPLOYER_NAMES = ("RESET UI TEST EMPLOYER B", "RESET UI TEST EMPLOYER C")
CANDIDATE_NAME = "RESET UI TEST HELPER"

TRANSCRIPT = [
    ("inbound", "Hi, I want to hire a helper", False),
    ("outbound", "Hello! I'm Claire, Ming Hwee's AI assistant. Who would the care be for?", True),
    ("inbound", "For my mother, she is 78", False),
    ("outbound", "Got it - does she need help with mobility?", True),
]


def _remove(cur) -> None:
    """Delete every row this script has ever created. Child-first."""
    # Conversations and everything hanging off them.
    cur.execute(
        "select id, langgraph_thread_id from wp_chat_conversations "
        "where customer_number = any(%s)",
        (list(PHONES),),
    )
    for cid, thread in cur.fetchall():
        cur.execute("delete from cb_handovers where conversation_id=%s", (cid,))
        cur.execute("delete from cb_tickets where conversation_id=%s", (cid,))
        cur.execute("delete from wp_chat_messages where conversation_id=%s", (cid,))
        if thread:
            for table in ("cb_checkpoints", "cb_checkpoint_blobs", "cb_checkpoint_writes"):
                cur.execute(f"delete from {table} where thread_id=%s", (thread,))
        cur.execute("delete from wp_chat_conversations where id=%s", (cid,))
        print(f"  removed conversation {cid}")

    # Leads, and the activity rows that reference them.
    cur.execute(
        "delete from lead_activities where lead_id in "
        "(select id from leads where lead_number = any(%s))",
        (list(LEAD_NUMBERS),),
    )
    cur.execute("delete from leads where lead_number = any(%s)", (list(LEAD_NUMBERS),))
    cur.execute("delete from leads_candidate where lead_number = any(%s)", (list(LEAD_NUMBERS),))

    # Master records, and the placements that reference them.
    cur.execute(
        "delete from placements where employer_id in "
        "(select id from employers where display_name = any(%s))",
        (list(EMPLOYER_NAMES),),
    )
    cur.execute("delete from employers where display_name = any(%s)", (list(EMPLOYER_NAMES),))
    cur.execute("delete from candidates where full_name=%s", (CANDIDATE_NAME,))


def _conversation(cur, phone: str, name: str, contact_type: str) -> tuple[int, str]:
    """A conversation with a transcript, a ticket, a handover and a checkpoint."""
    cur.execute(
        "insert into wp_chat_conversations "
        "(customer_number, customer_name, status, bot_status, unread_count, "
        " langgraph_thread_id, contact_type, service_type, intent) "
        "values (%s,%s,'open','human_active',3,%s,%s,'new_hiring','new_hiring') "
        "returning id",
        (phone, name, "conv-seed-placeholder", contact_type),
    )
    cid = cur.fetchone()[0]
    thread = f"conv-{cid}-seedtest"
    cur.execute(
        "update wp_chat_conversations set langgraph_thread_id=%s where id=%s", (thread, cid)
    )

    for direction, body, is_bot in TRANSCRIPT:
        cur.execute(
            "insert into wp_chat_messages "
            "(conversation_id, direction, body, from_number, to_number, is_bot) "
            "values (%s,%s,%s,%s,%s,%s)",
            (cid, direction, body, phone, "6511111111", is_bot),
        )

    tenant = settings.tenant_id
    ticket_id = str(uuid.uuid4())
    cur.execute(
        "insert into cb_tickets "
        "(id, tenant_id, conversation_id, ticket_number, service_type, priority, status, "
        " captured_info) "
        "values (%s,%s,%s,%s,%s,'low','open','{}'::jsonb)",
        (ticket_id, tenant, cid, f"CB-TEST-{cid}", ["new_hiring"]),
    )
    cur.execute(
        "insert into cb_handovers "
        "(id, tenant_id, conversation_id, direction, reason, ticket_id) "
        "values (%s,%s,%s,'bot_to_human','seeded test',%s)",
        (str(uuid.uuid4()), tenant, cid, ticket_id),
    )
    cur.execute(
        "insert into cb_checkpoints "
        "(thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata) "
        "values (%s,'','ckpt-1','test','{}'::jsonb,'{}'::jsonb)",
        (thread,),
    )
    cur.execute(
        "insert into cb_checkpoint_writes "
        "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob) "
        "values (%s,'','ckpt-1','task-1',0,'ch','test',%s)",
        (thread, b"x"),
    )
    return cid, thread


def _seed(cur) -> None:
    tenant = settings.tenant_id
    cur.execute("select id from branches where tenant_id=%s limit 1", (tenant,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"No branch for tenant {tenant} — a lead cannot be created without one.")
    branch = row[0]

    # --- A: candidate, deletable candidate lead ---------------------------
    cid_a, _ = _conversation(cur, PHONES[0], "RESET UI TEST A", "candidate")
    cur.execute(
        "insert into leads_candidate "
        "(id, tenant_id, branch_id, lead_number, full_name, phone, source, status) "
        "values (%s,%s,%s,%s,%s,%s,'chatbot','new')",
        (str(uuid.uuid4()), tenant, branch, LEAD_NUMBERS[0], "RESET UI TEST LEAD",
         "+" + PHONES[0]),
    )
    print(f"  A  +{PHONES[0]}  conversation {cid_a}  + candidate lead {LEAD_NUMBERS[0]} (deletable)")

    # --- B: employer, deletable employer lead, known in master records ----
    cid_b, _ = _conversation(cur, PHONES[1], "RESET UI TEST B", "employer")
    employer_id = str(uuid.uuid4())
    cur.execute(
        "insert into employers (id, tenant_id, display_name, phone, branch_id) "
        "values (%s,%s,%s,%s,%s)",
        (employer_id, tenant, EMPLOYER_NAMES[0], "+" + PHONES[1], branch),
    )
    candidate_id = str(uuid.uuid4())
    cur.execute(
        "insert into candidates (id, tenant_id, full_name, nationality) values (%s,%s,%s,'PH')",
        (candidate_id, tenant, CANDIDATE_NAME),
    )
    cur.execute(
        "insert into placements (id, tenant_id, employer_id, candidate_id) values (%s,%s,%s,%s)",
        (str(uuid.uuid4()), tenant, employer_id, candidate_id),
    )
    cur.execute(
        "insert into leads "
        "(id, tenant_id, branch_id, lead_number, full_name, phone, source, status, "
        " interest_type, requirement) "
        "values (%s,%s,%s,%s,%s,%s,'chatbot','new','first_time_hire',"
        "'Elderly care for mother, 78')",
        (str(uuid.uuid4()), tenant, branch, LEAD_NUMBERS[1], "RESET UI TEST EMPLOYER LEAD",
         "+" + PHONES[1]),
    )
    print(f"  B  +{PHONES[1]}  conversation {cid_b}  + EMPLOYER lead {LEAD_NUMBERS[1]} (deletable)")
    print("        also an employers record + 1 placement, so the panel says 'employer'")

    # --- C: employer lead BLOCKED by a lead_activities row ----------------
    cid_c, _ = _conversation(cur, PHONES[2], "RESET UI TEST C", "employer")
    blocked_lead = str(uuid.uuid4())
    cur.execute(
        "insert into leads "
        "(id, tenant_id, branch_id, lead_number, full_name, phone, source, status, "
        " interest_type) "
        "values (%s,%s,%s,%s,%s,%s,'chatbot','contacted','transfer')",
        (blocked_lead, tenant, branch, LEAD_NUMBERS[2], "RESET UI TEST BLOCKED LEAD",
         "+" + PHONES[2]),
    )
    # This one row is what makes the lead undeletable: lead_activities.lead_id
    # is ON DELETE NO ACTION, so Postgres refuses. The UI checks for it first.
    cur.execute(
        "insert into lead_activities (id, tenant_id, lead_id, title) "
        "values (%s,%s,%s,'seeded test activity - this is what blocks the delete')",
        (str(uuid.uuid4()), tenant, blocked_lead),
    )
    print(f"  C  +{PHONES[2]}  conversation {cid_c}  + EMPLOYER lead {LEAD_NUMBERS[2]} (BLOCKED)")
    print("        1 lead_activities row references it, so the UI greys it out")


def _list(cur) -> None:
    cur.execute(
        "select customer_number, id, customer_name, bot_status from wp_chat_conversations "
        "where customer_number = any(%s) order by customer_number",
        (list(PHONES),),
    )
    rows = cur.fetchall()
    print("Conversations:")
    for number, cid, name, bot in rows or []:
        cur.execute("select count(*) from wp_chat_messages where conversation_id=%s", (cid,))
        messages = cur.fetchone()[0]
        print(f"  +{number}  #{cid}  {name}  bot_status={bot}  messages={messages}")
    if not rows:
        print("  (none — run without --clean to create them)")

    print("Leads:")
    found = False
    for table in ("leads", "leads_candidate"):
        cur.execute(
            f"select lead_number, full_name, phone, status from {table} "
            "where lead_number = any(%s)",
            (list(LEAD_NUMBERS),),
        )
        for number, name, phone, status in cur.fetchall():
            found = True
            blockers = ""
            if table == "leads":
                cur.execute(
                    "select count(*) from lead_activities where lead_id = "
                    "(select id from leads where lead_number=%s)",
                    (number,),
                )
                count = cur.fetchone()[0]
                blockers = f"  BLOCKED by {count} activity row(s)" if count else "  deletable"
            else:
                blockers = "  deletable"
            print(f"  {number:<14} {name:<32} {phone}  {status}{blockers}")
    if not found:
        print("  (none)")


def main() -> None:
    if not settings.supabase_db_url:
        raise SystemExit("SUPABASE_DB_URL is not set — this script needs a direct connection.")

    clean = "--clean" in sys.argv
    listing = "--list" in sys.argv

    with psycopg.connect(settings.supabase_db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            if listing:
                _list(cur)
                return

            _remove(cur)
            if clean:
                print("Test data removed.")
                return

            _seed(cur)
            print("\nLook these up in the UI at http://localhost:5175")
            print("Re-run this script to rebuild them; --clean to remove; --list to inspect.")


if __name__ == "__main__":
    main()
