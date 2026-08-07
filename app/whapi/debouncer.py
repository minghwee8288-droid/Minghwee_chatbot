"""Per-conversation message debouncing.

Clients type in bursts — "hi" / "I need helper" / "from philippines" inside
three seconds. Processing each one separately produces three replies and a
confused conversation, so messages are buffered per phone number and flushed
once the client has stopped typing for ``DEBOUNCE_SECONDS``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from app.whapi.parser import IncomingMessage

logger = logging.getLogger(__name__)

Handler = Callable[[str, list[IncomingMessage]], Awaitable[None]]


class MessageDebouncer:
    def __init__(self, delay: float, handler: Handler) -> None:
        self._delay = delay
        self._handler = handler
        self._buffers: dict[str, list[IncomingMessage]] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def add(self, message: IncomingMessage) -> None:
        """Buffer a message and (re)start this conversation's flush timer."""
        phone = message.customer_number
        async with self._lock:
            self._buffers.setdefault(phone, []).append(message)
            existing = self._timers.get(phone)
            if existing and not existing.done():
                existing.cancel()
            self._timers[phone] = asyncio.create_task(self._flush_after_delay(phone))

    async def _flush_after_delay(self, phone: str) -> None:
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            return  # A newer message arrived; that timer owns the flush now.

        async with self._lock:
            batch = self._buffers.pop(phone, [])
            self._timers.pop(phone, None)

        if not batch:
            return

        try:
            await self._handler(phone, batch)
        except Exception:  # noqa: BLE001 - a failed batch must not kill the task
            logger.exception("Debounced handler failed for %s", phone)

    async def requeue(self, phone: str, messages: list[IncomingMessage]) -> None:
        """Hand messages back to the buffer for whoever is mid-turn to collect."""
        async with self._lock:
            self._buffers.setdefault(phone, [])[0:0] = messages

    async def drain(self, phone: str) -> list[IncomingMessage]:
        """Take everything buffered for a conversation right now.

        Used by the processor once it holds the conversation lock: messages that
        arrived while the previous batch was being handled get folded into it
        rather than triggering a second reply to the same train of thought.
        """
        async with self._lock:
            timer = self._timers.pop(phone, None)
            batch = self._buffers.pop(phone, [])
        if timer and not timer.done():
            timer.cancel()
        return batch

    async def flush_now(self, phone: str) -> None:
        """Drop any pending batch for a conversation (e.g. a human took over)."""
        await self.drain(phone)

    async def shutdown(self) -> None:
        async with self._lock:
            timers = list(self._timers.values())
            self._timers.clear()
            self._buffers.clear()
        for timer in timers:
            timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
