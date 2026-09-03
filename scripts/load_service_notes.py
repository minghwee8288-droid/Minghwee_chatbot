"""Load the agency's transfer + passport-renewal service notes into the KB.

Ming Hwee sent these timings on 2026-09-03. Until they are IN the knowledge
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

# nationality is varchar(5) and the live vocabulary is all/id/mm/ph.
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
        "nationality": "ph",
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
        "nationality": "id",
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
        "nationality": "mm",
        "section_heading": "Passport renewal — Myanmar helper",
        "question": "How long does passport renewal take for a Myanmar helper?",
        "answer": (
            "The in-person appointment is generally completed within a day once it is "
            "scheduled, but securing an appointment can take weeks or months depending "
            "on availability. This is an estimate and can vary with document "
            "verification."
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
