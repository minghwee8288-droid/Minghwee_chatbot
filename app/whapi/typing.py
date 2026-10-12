"""The "typing..." indicator, shown from the moment a message is accepted.

Added 2026-10-12. Until then the only sign of life between a client's message
and our reply was the read receipt: Whapi's typing_time shows the bubble only
for the second or two before the reply is delivered, after the debounce and the
whole graph run. The agency's complaint is the wait, and an unexplained wait
reads as being ignored.

So once an inbound message has passed every gate (allowlist, bot_status, the
agent grace window), start() shows "typing..." in that chat and keeps it alive
- WhatsApp clears a presence after roughly 25 seconds - until stop() is called:
when the reply is sent, or when the turn ends with nothing to send.

It is NOT shown, because there the bot says nothing or a person owns the chat:
* a message the bot will leave unanswered (closure.needs_no_reply);
* a chat with a topic parked with an agent (an open ticket);
* a chat a human agent is on - those never reach start(): the webhook returns
  before it.

Cosmetic by design: every call is best effort, runs off the message path, and
can never delay or fail a turn. TYPING_INDICATOR=false switches it off.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.config import settings
from app.whapi.client import whapi

logger = logging.getLogger(__name__)

# One keep-alive task per phone. A burst of messages shares it.
_keepers: dict[str, asyncio.Task] = {}
# When each chat was last stopped. start() decides with a couple of reads, and
# a quick turn can finish in between: a keep-alive created after the reply
# would show "typing..." for minutes after we had answered.
_stopped_at: dict[str, float] = {}


def enabled() -> bool:
    return bool(settings.typing_indicator)


async def should_show(conversation_id, text: str, message_id: str | None = None) -> bool:
    """Whether a reply is plausibly coming. Read-only; False on any doubt."""
    # Imported here: the services import the Whapi client, which imports this
    # package - a module-level import would be a cycle.
    from app.graph.closure import needs_no_reply
    from app.services import message as message_service
    from app.services import ticket as ticket_service

    try:
        if await ticket_service.open_topics_for_conversation(conversation_id):
            return False
        # The message itself is already stored; the graph reads the history
        # without it, and needs_no_reply must see what the graph will see.
        history = await message_service.get_history(
            conversation_id, exclude_ids={message_id} if message_id else None
        )
        return not needs_no_reply(
            text, history_text=message_service.format_history(history)
        )
    except Exception:  # noqa: BLE001
        logger.debug("typing: could not decide, not showing it", exc_info=True)
        return False


async def _keep_alive(phone: str) -> None:
    deadline = time.monotonic() + settings.typing_max_seconds
    try:
        while time.monotonic() < deadline:
            await whapi.send_presence(phone, "typing")
            await asyncio.sleep(settings.typing_refresh_seconds)
    except asyncio.CancelledError:
        pass


async def start(phone: str, conversation_id, text: str, message_id: str | None = None) -> None:
    """Show "typing..." in this chat if a reply is coming. Never raises."""
    if not enabled():
        return
    try:
        began = time.monotonic()
        running = _keepers.get(phone)
        if running is not None and not running.done():
            return
        if not await should_show(conversation_id, text, message_id):
            return
        # Checked again: a second message may have started one meanwhile, or
        # the turn may already have finished.
        running = _keepers.get(phone)
        if running is not None and not running.done():
            return
        if _stopped_at.get(phone, 0.0) >= began:
            return
        _keepers[phone] = asyncio.create_task(_keep_alive(phone))
    except Exception:  # noqa: BLE001
        logger.debug("typing: start failed", exc_info=True)


async def stop(phone: str, *, clear: bool) -> None:
    """Stop keeping it alive. `clear` sends "pause", for a turn that sent
    nothing; after a reply WhatsApp clears it itself. Never raises."""
    try:
        _stopped_at[phone] = time.monotonic()
        while len(_stopped_at) > 2000:
            _stopped_at.pop(next(iter(_stopped_at)))
        task = _keepers.pop(phone, None)
        if task is None:
            return
        if not task.done():
            task.cancel()
        if clear:
            await whapi.send_presence(phone, "pause")
    except Exception:  # noqa: BLE001
        logger.debug("typing: stop failed", exc_info=True)


def active(phone: str) -> bool:
    task = _keepers.get(phone)
    return task is not None and not task.done()
