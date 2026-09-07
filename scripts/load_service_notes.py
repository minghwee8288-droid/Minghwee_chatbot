"""Load the agency's transfer + passport-renewal service notes into the KB.

Ming Hwee sent these timings on 2026-09-03, and the service process +
timeline table (transfer, passport renewal, new hiring, direct hiring) on
2026-09-07. Until they are IN the knowledge
base the bot cannot say them: every figure is checked against the retrieved
records by guards.ungrounded_figures, so an unretrieved "6 to 8 weeks" is
binned and the client gets "I'll check with the team" instead — which is
exactly what was happening on passport and transfer questions.

Rewritten as client-facing Q&A. The source text was staff-facing ("you may
advise employers as follows"); that phrasing must not reach the KB, because
whatever is in the records is what the model quotes.

Idempotent: a row with the same question + service_type is skipped, so this can
be re-run safely. Read-then-write — it reads a live row first to confirm the
column set rather than trusting this file's idea of the schema (CLAUDE.md §0.5).

    python scripts/load_service_notes.py --dry-run    # show what would be written
    python scripts/load_service_notes.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.supabase import db  # noqa: E402
from app.services.rag import KB_TABLE, embed_query  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
logger = logging.getLogger("load_service_notes")

# Must not match rag._INTERNAL_SOURCES (MHOS / for Vendor / Blueprint /
# Hiring Pipelines Brief / version control) or every row is dropped from
# retrieval without an error.
SOURCE_DOCUMENT = "Ming Hwee Service Notes"

# nationality is varchar(5) and the CHECK constraint
# cb_knowledge_base_updated_nationality_check requires UPPERCASE codes —
# 'PH'/'ID'/'MM' — alongside lowercase 'all'. Confirmed against the live table
# (all 253, PH 12, ID 4, MM 3) after a lowercase 'ph' was rejected with 23514.
# nationality_code() emits the same uppercase codes and the match function
# compares them exactly, so these rows are reachable by a nationality-filtered
# search. Do not "normalise" these to lowercase: the constraint will reject it.
# contact_type vocabulary is all/candidate/employer. Both filters are inclusive
# of their catch-all bucket, so 'all' is reachable from every query.
ROWS: list[dict[str, Any]] = [
    {
        "service_type": "transfer",
        "nationality": "all",
        "section_heading": "Transfer — process and timing",
        "question": "How long does a transfer take and what is the process?",
        "answer": (
            "A transfer is subject to MOM approval, which usually takes 1 to 3 working "
            "days unless MOM asks for additional documents to be uploaded. Once MOM "
            "approves the transfer we purchase the required insurance, and after the "
            "insurance is transmitted the helper can start work with the new employer "
            "the following day."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "all",
        "section_heading": "Passport renewal — timing by nationality",
        "question": "How long does a passport renewal take for a helper?",
        "answer": (
            "It depends on the helper's nationality and her embassy. For a Filipino "
            "helper it takes roughly 6 to 8 weeks from the embassy appointment; for an "
            "Indonesian helper roughly 3 working days; for a Myanmar helper the "
            "appointment itself is usually done within a day, though getting an "
            "appointment can take weeks or months. These are estimates and vary with "
            "appointment availability, document verification and the embassy's own "
            "requirements."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "PH",
        "section_heading": "Passport renewal — Filipino helper",
        "question": "How long does passport renewal take for a Filipino helper?",
        "answer": (
            "Roughly 6 to 8 weeks from the date of the appointment at the Embassy of "
            "the Republic of the Philippines in Singapore. The passport is processed "
            "and printed in the Philippines, shipped back, and then made ready for "
            "collection in Singapore. This is an estimate and can vary with appointment "
            "availability and document verification."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "ID",
        "section_heading": "Passport renewal — Indonesian helper",
        "question": "How long does passport renewal take for an Indonesian helper?",
        "answer": (
            "Roughly 3 working days. The Indonesian Embassy in Singapore requires an "
            "online appointment, and passport services are handled during weekday "
            "operating hours. This is an estimate and can vary with appointment "
            "availability and document verification."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "MM",
        "section_heading": "Passport renewal — Myanmar helper",
        "question": "How long does passport renewal take for a Myanmar helper?",
        "answer": (
            "The in-person appointment is generally completed within a day once it is "
            "scheduled, but securing an appointment can take weeks or months depending "
            "on availability. This is an estimate and can vary with document "
            "verification."
        ),
    },
    # --- 2026-09-07: the agency's service process + timeline table ---------
    #
    # Additive only. The five rows above are untouched - the script skips on
    # question + service_type, so re-running it will not rewrite them.
    #
    # What this adds is the PROCESS half. Until now the KB held passport
    # timings with no steps, and nothing at all for new hiring or direct
    # hiring, so "what is the process?" fell under the soft floor and the
    # client got the holding line. It has bitten in testing three times.
    #
    # Written from the client's side of the desk, not ours. The failure this
    # avoids is on record: a "what's the process" question once retrieved the
    # internal pipeline brief and the bot replied "The process involves three
    # main stages: first, we capture your requirements and match you with
    # suitable candidates" - our own workflow, described to the person it is
    # being run on. So these say what HAPPENS and what the client will be asked
    # to do, never what our internal stages are called.
    {
        "service_type": "transfer",
        "nationality": "all",
        "section_heading": "Transfer - the steps",
        "question": "What are the steps to transfer a helper to a new employer?",
        "answer": (
            "First the transfer application is submitted to MOM and we wait for their "
            "approval, which usually takes 1 to 3 working days. MOM sometimes asks for "
            "extra documents to be uploaded, and that adds to the wait. Once MOM "
            "approves it the required insurance is purchased, and once the insurance "
            "is transmitted the helper can start work with her new employer the "
            "following day."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "all",
        "section_heading": "Passport renewal - the steps",
        "question": "What is the process for renewing my helper's passport?",
        "answer": (
            "It runs through her own country's embassy in Singapore, so the steps "
            "depend on her nationality. In every case an appointment at the embassy "
            "has to be secured first, the helper attends it in person, the passport is "
            "then processed, and it is made ready for collection in Singapore. What "
            "differs is how long each part takes and where the passport is actually "
            "printed. Tell us her nationality and we can be specific."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "PH",
        "section_heading": "Passport renewal - Filipino helper, the steps",
        "question": "What is the process for renewing a Filipino helper's passport?",
        "answer": (
            "An appointment is made at the Embassy of the Republic of the Philippines "
            "in Singapore and the helper attends it in person. The passport is then "
            "processed and printed in the Philippines, shipped back to Singapore, and "
            "made ready for collection here. From the date of the embassy appointment "
            "it usually takes about 6 to 8 weeks, though that can vary with "
            "appointment availability and document verification."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "ID",
        "section_heading": "Passport renewal - Indonesian helper, the steps",
        "question": "What is the process for renewing an Indonesian helper's passport?",
        "answer": (
            "It is processed through the Indonesian embassy in Singapore. An online "
            "appointment is booked, the helper attends in person during weekday "
            "operating hours, and the renewal is processed from there - usually about "
            "3 working days. That can vary with appointment availability and document "
            "verification."
        ),
    },
    {
        "service_type": "passport_renewal",
        "nationality": "MM",
        "section_heading": "Passport renewal - Myanmar helper, the steps",
        "question": "What is the process for renewing a Myanmar helper's passport?",
        "answer": (
            "There are three parts: securing an appointment, attending it in person, "
            "and the renewal being processed. Once the appointment is secured the "
            "in-person part is generally completed within a day. The waiting time for "
            "the appointment itself is the long part - it can run to weeks or months "
            "depending on availability, so it is worth starting early."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - what to expect",
        "question": "What is the process for hiring a new helper?",
        "answer": (
            "We start by going through what you need. That covers who is at home and "
            "the ages of any children or elderly family members, the type of home and "
            "how many bedrooms and bathrooms there are, whether you have pets or any "
            "dietary restrictions, and what her main and secondary duties would be. "
            "Then we take your preferences - which nationality you would like, and "
            "whether you are open to a first-timer or would rather someone who has "
            "worked in Singapore before, worked abroad, or is a transfer helper "
            "already here. We also ask when you need her to start. With all of that we "
            "recommend the helpers who genuinely suit your household, rather than "
            "sending you a stack of profiles to sift through."
        ),
    },
    {
        "service_type": "new_hiring",
        "nationality": "all",
        "section_heading": "New hiring - start date",
        "question": "How long does it take to hire a new helper?",
        "answer": (
            "There is no single answer, because it turns on your requirements and on "
            "which helper you choose. What we work to is your own date - so the "
            "question we ask is when you need her to start, and we plan back from "
            "that. A consultant will confirm the timeline once your requirements are "
            "in and we know which helper you are going for."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - what to expect",
        "question": "What is the process for a direct hire?",
        "answer": (
            "A direct hire is one where you have already found the helper yourself and "
            "want us to process her. We take her full name and contact number, confirm "
            "where she is at the moment - here in Singapore, back in her home country, "
            "or working in another country - along with her nationality, whether she "
            "is currently employed, and when she would be free to start. If she is "
            "employed overseas we also check whether she has a notice period to serve "
            "or any clearance to obtain from her current employer before she can "
            "leave. Once we have that we can set out the procedure, the documents "
            "required, the likely timeframe and the costs."
        ),
    },
    {
        "service_type": "direct_hiring",
        "nationality": "all",
        "section_heading": "Direct hire - timing",
        "question": "How long does a direct hire take?",
        "answer": (
            "There is no fixed timeline for a direct hire. It depends on where the "
            "helper is, her nationality, whether she is currently employed, when she "
            "is available, and whether she has a notice period or clearance to "
            "complete before she can leave her current job. Once we have those details "
            "we can give you a proper estimate rather than a guess."
        ),
    },
]


async def _existing_shape() -> tuple[set[str], str | None]:
    """Confirm the live column set and namespace instead of assuming them."""
    sample = await db.select_one(KB_TABLE, "*", is_active=True)
    if not sample:
        raise SystemExit(
            f"{KB_TABLE} returned no active row — refusing to write into a table "
            "whose shape I cannot confirm."
        )
    return set(sample.keys()), sample.get("namespace")


async def main(dry_run: bool) -> None:
    columns, namespace = await _existing_shape()
    logger.info("%s has %d columns; namespace=%r", KB_TABLE, len(columns), namespace)

    required = {"question", "answer", "service_type", "nationality", "embedding"}
    missing = required - columns
    if missing:
        raise SystemExit(f"{KB_TABLE} is missing expected column(s): {sorted(missing)}")

    written = skipped = 0
    for row in ROWS:
        already = await db.select_one(
            KB_TABLE, "id", question=row["question"], service_type=row["service_type"]
        )
        if already:
            logger.info("SKIP  (already present) %s", row["question"])
            skipped += 1
            continue

        payload: dict[str, Any] = {
            "namespace": namespace,
            "service_type": row["service_type"],
            "contact_type": "all",
            "nationality": row["nationality"],
            "chunk_type": "qa_pair",
            "question": row["question"],
            "answer": row["answer"],
            "content": f"{row['question']}\n{row['answer']}",
            "source_document": SOURCE_DOCUMENT,
            "section_heading": row["section_heading"],
            "is_active": True,
        }
        # Only send columns the table actually has.
        payload = {k: v for k, v in payload.items() if k in columns}

        if dry_run:
            logger.info(
                "WOULD WRITE  service=%s nat=%s  %s",
                row["service_type"],
                row["nationality"],
                row["question"],
            )
            written += 1
            continue

        # Embedded on the question+answer together: clients ask the question,
        # but the figures that make the row worth retrieving are in the answer.
        payload["embedding"] = await embed_query(payload["content"])
        await db.insert(KB_TABLE, payload)
        logger.info("WROTE  service=%s nat=%s", row["service_type"], row["nationality"])
        written += 1

    verb = "would write" if dry_run else "wrote"
    logger.info("Done — %s %d row(s), skipped %d already present.", verb, written, skipped)
    if not dry_run and written:
        logger.info(
            "Now run:  python scripts/check_retrieval.py   "
            "and confirm a passport/transfer question retrieves these."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be written without touching the database",
    )
    asyncio.run(main(parser.parse_args().dry_run))
