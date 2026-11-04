"""Per-conversation message debouncing.

Clients type in bursts — "hi" / "I need helper" / "from philippines" inside
three seconds. Processing each one separately produces three replies and a
confused conversation, so messages are buffered per phone number and flushed
once the client has stopped typing for ``DEBOUNCE_SECONDS``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from app.whapi.parser import IncomingMessage

logger = logging.getLogger(__name__)

Handler = Callable[[str, list[IncomingMessage]], Awaitable[None]]

# 2026-11-04: a restart answers what it is holding instead of dropping it. The
# messages waiting here were already stored and read-receipted, so the client
# had seen the blue ticks and got nothing, and nothing ever retried them. False
# = shutdown discards the buffers, as before.
DRAIN_ON_SHUTDOWN = True
# Must stay under the container's stop_grace_period (docker-compose.yml, 60s),
# or Docker kills the turn this is waiting for. smoke_resilience.py compares them.
DRAIN_TIMEOUT_SECONDS = 45.0


class MessageDebouncer:
    def __init__(
        self,
        delay: float,
        handler: Handler,
        delay_for: Optional[Callable[[IncomingMessage], float]] = None,
    ) -> None:
        self._delay = delay
        # How long to wait after THIS message, when the wait depends on what
        # it says (the smart debounce, webhook.debounce_delay). The newest
        # message decides, because every message restarts the timer.
        self._delay_for = delay_for
        self._handler = handler
        self._buffers: dict[str, list[IncomingMessage]] = {}
        self._timers: dict[str, asyncio.Task] = {}
        # Batches being handled right now. Their timers have already left
        # _timers, so shutdown would not otherwise know to wait for them.
        self._running: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    async def add(self, message: IncomingMessage) -> None:
        """Buffer a message and (re)start this conversation's flush timer."""
        phone = message.customer_number
        async with self._lock:
            self._buffers.setdefault(phone, []).append(message)
            existing = self._timers.get(phone)
            if existing and not existing.done():
                existing.cancel()
            self._timers[phone] = asyncio.create_task(
                self._flush_after_delay(phone, self._wait_after(message))
            )

    def _wait_after(self, message: IncomingMessage) -> float:
        if self._delay_for is None:
            return self._delay
        try:
            return float(self._delay_for(message))
        except Exception:  # noqa: BLE001 - a bad classifier must not drop a message
            logger.debug("debounce delay_for failed; using the full wait", exc_info=True)
            return self._delay

    async def _flush_after_delay(self, phone: str, delay: float | None = None) -> None:
        try:
            await asyncio.sleep(self._delay if delay is None else delay)
        except asyncio.CancelledError:
            return  # A newer message arrived; that timer owns the flush now.

        async with self._lock:
            batch = self._buffers.pop(phone, [])
            self._timers.pop(phone, None)

        if not batch:
            return

        await self._handle(phone, batch)

    async def _handle(self, phone: str, batch: list[IncomingMessage]) -> None:
        task = asyncio.current_task()
        self._running.add(task)
        try:
            await self._handler(phone, batch)
        except Exception:  # noqa: BLE001 - a failed batch must not kill the task
            logger.exception("Debounced handler failed for %s", phone)
        finally:
            self._running.discard(task)

    async def requeue(self, phone: str, messages: list[IncomingMessage]) -> None:
        """Hand messages back to the buffer for whoever is mid-turn to collect."""
        async with self._lock:
            self._buffers.setdefault(phone, [])[0:0] = messages

    async def has_pending(self, phone: str) -> bool:
        """Whether more messages are already waiting for this conversation.

        A non-destructive peek, used just before a reply is sent. The turn takes
        several seconds, and anything the client typed during it is buffered
        here: sending the reply we generated before they said it produces two
        answers to one train of thought. Live, 2026-09-03: "Both" and "But I can
        only cook chinese" seconds apart got "Are you currently in Singapore or
        still overseas?" and then "That's okay, Chinese cooking is fine. Are you
        currently in Singapore or still overseas?" — the same question twice.
        """
        async with self._lock:
            return bool(self._buffers.get(phone))

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
        if not DRAIN_ON_SHUTDOWN:
            async with self._lock:
                timers = list(self._timers.values())
                self._timers.clear()
                self._buffers.clear()
            for timer in timers:
                timer.cancel()
            if timers:
                await asyncio.gather(*timers, return_exceptions=True)
            return
        await self._drain_all()

    async def _drain_all(self) -> None:
        """Answer every waiting batch now, and let running ones finish.

        Skipping the rest of the debounce can split a client's thought in two,
        which is a small price against never answering at all. Runs in a loop
        because a batch can land back in the buffer: process_batch hands its
        messages to a turn already running on that number (requeue), and if that
        turn has just finished its last look, nobody else would collect them.
        What is still unfinished at the time limit is cancelled and named in the
        log, by message id.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DRAIN_TIMEOUT_SECONDS
        total = 0
        while True:
            async with self._lock:
                timers = list(self._timers.values())
                self._timers.clear()
                batches = list(self._buffers.items())
                self._buffers.clear()
            for timer in timers:
                timer.cancel()
            if timers:
                await asyncio.gather(*timers, return_exceptions=True)
            total += len(batches)
            tasks = {asyncio.create_task(self._handle(phone, batch)) for phone, batch in batches}
            await asyncio.sleep(0)  # let each register itself as running
            pending = self._running | tasks
            if not pending:
                break
            _, not_done = await asyncio.wait(
                pending, timeout=max(deadline - loop.time(), 0)
            )
            if not_done:
                for task in not_done:
                    task.cancel()
                await asyncio.gather(*not_done, return_exceptions=True)
                logger.error(
                    "Shutdown: %d turn(s) still running after %gs were cancelled",
                    len(not_done), DRAIN_TIMEOUT_SECONDS,
                )
            async with self._lock:
                left = [m.whapi_message_id for batch in self._buffers.values() for m in batch]
            if not_done or not (self._buffers or self._timers):
                if not_done and left:
                    logger.error("Shutdown: messages left unanswered: %s", left)
                break
        if total:
            logger.info("Shutdown: answered %d waiting batch(es)", total)
