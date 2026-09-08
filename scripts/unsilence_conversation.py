"""Put the bot back on a conversation that was stood down by mistake.

The agent detector silences the bot whenever an outbound message appears that
we did not send. That is right for a human agent replying and wrong for a
BROADCAST: live on 2026-09-08 the agency sent a number-migration notice to
about fifty clients, the first conversations it reached were read as an agent
taking over, and the bot went quiet on real enquiries until the 72-hour safety
net would have released them.

webhook._undo_broadcast_standdowns now reverses that automatically for copies
that arrive after the broadcast is recognised. This script is for the ones
already stuck, and for any other stand-down you want to lift by hand.

Deliberately NOT reset_conversation.py: this changes bot_status and NOTHING
else. The thread id, the checkpoint and everything collected so far are kept,
so a client four questions into a passport renewal carries on from where they
were instead of starting again.

    python scripts/unsilence_conversation.py --list
    python scripts/unsilence_conversation.py +6591234567
    python scripts/unsilence_conversation.py +6591234567 --dry-run

--list shows only the stood-down conversations on numbers the bot would
actually answer. The estate has hundreds of threads in human_active and almost
all of them are the portal doing its job; the allowlisted ones are the only
place a stand-down costs a client a reply. Run it IN THE CONTAINER, so it reads
the server's BOT_ALLOWED_NUMBERS rather than a local .env.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db.supabase import db  # noqa: E402
from app.services import conversation as conversation_service  # noqa: E402
from app.services import handover as handover_service  # noqa: E402
from app.utils import normalize_phone  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
logger = logging.getLogger("unsilence")


async def list_stranded() -> None:
    """Stood-down conversations the bot would otherwise be answering."""
    # settings.bot_allowed_numbers is the RAW comma-separated STRING; iterating
    # it walks characters, which silently compared phone numbers against "+9"
    # and reported nothing affected no matter what was stranded.
    # settings.allowed_numbers is the parsed, normalised set.
    allowed = set(settings.allowed_numbers)
    logger.info("%d number(s) on the safety gate", len(allowed))
    rows = await db.select_many(
        "wp_chat_conversations", "id,customer_number,bot_status,updated_at",
        limit=500, bot_status=conversation_service.HUMAN_ACTIVE,
    )
    hit = [r for r in rows if normalize_phone(r.get("customer_number")) in allowed]
    logger.info("%d conversation(s) stood down in total", len(rows))
    logger.info("%d of them are numbers the bot would otherwise answer", len(hit))
    for r in sorted(hit, key=lambda x: str(x.get("updated_at")), reverse=True):
        print(f"   {r.get('customer_number'):<16} id={r['id']:<6} "
              f"since {r.get('updated_at')}")
    if not hit:
        print("   (none - every stood-down thread is one the portal owns anyway)")


async def main(phone: str, dry_run: bool) -> None:
    normalised = normalize_phone(phone)
    conversation = await conversation_service.get_by_phone(normalised)
    if not conversation:
        raise SystemExit(f"No conversation found for {normalised}")

    status = conversation.get("bot_status") or "(none)"
    logger.info(
        "Conversation %s (%s) is currently bot_status=%s",
        conversation["id"],
        normalised,
        status,
    )
    if status == conversation_service.BOT_ACTIVE:
        logger.info("Already active — nothing to do.")
        return

    if dry_run:
        logger.info("WOULD set bot_status=%s (thread kept)", conversation_service.BOT_ACTIVE)
        return

    await handover_service.undo_agent_takeover(conversation["id"], "unsilenced by hand")
    logger.info(
        "Done — the bot will answer %s again. Nothing collected was discarded.",
        normalised,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phone", nargs="?", help="the client's number, any format"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show the change without making it"
    )
    parser.add_argument(
        "--list", action="store_true", dest="list_only",
        help="list stood-down conversations the bot would otherwise answer",
    )
    args = parser.parse_args()
    if args.list_only:
        asyncio.run(list_stranded())
    elif args.phone:
        asyncio.run(main(args.phone, args.dry_run))
    else:
        parser.error("give a phone number, or --list")
