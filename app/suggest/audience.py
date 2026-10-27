"""Which conversations get no drafts at all: group chats and our own people.

A draft is a reply to a client. A group chat (wp_chat_conversations.is_group /
group_jid, or a @g.us number) is not one client, and an owner or staff number
is a colleague. Both are answered with status "skipped" and no model call.

Internal numbers come from the same two sources the live bot's may_engage
reads (webhook.py L432, services/internal_contacts.py): INTERNAL_NUMBERS in the
environment, and the cb_internal_contacts table if it exists. The table is read
here directly - a GET - rather than through internal_contacts.is_internal(),
which refreshes a module-level cache the live bot uses. A failed read (the
table is not created yet, the database briefly away) leaves the environment
list alone.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.db.supabase import db
from app.utils import normalize_phone

logger = logging.getLogger(__name__)

INTERNAL_TABLE = "cb_internal_contacts"  # internal_contacts.TABLE

GROUP = "group"
INTERNAL = "internal"


def is_group(conversation: dict[str, Any]) -> bool:
    number = str(conversation.get("customer_number") or "")
    return bool(
        conversation.get("is_group")
        or conversation.get("group_jid")
        or number.endswith("@g.us")
    )


async def internal_numbers() -> set[str]:
    numbers = set(settings.internal_number_set)
    try:
        result = await db.execute(db.table(INTERNAL_TABLE).select("phone").eq("active", True))
        numbers |= {normalize_phone(str(r["phone"])) for r in (result.data or []) if r.get("phone")}
    except Exception as exc:  # noqa: BLE001 - the table may not exist yet
        logger.info("suggest: %s not read (%s) - INTERNAL_NUMBERS only", INTERNAL_TABLE, type(exc).__name__)
    return numbers


async def skip_reason(conversation: dict[str, Any]) -> str:
    """"group", "internal" or "" (draft it)."""
    if is_group(conversation):
        return GROUP
    phone = normalize_phone(str(conversation.get("customer_number") or ""))
    if phone and phone in await internal_numbers():
        return INTERNAL
    return ""
