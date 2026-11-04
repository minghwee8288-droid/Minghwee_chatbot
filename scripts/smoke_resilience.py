"""Smoke tests for how the bot starts, reports its health and shuts down.

Kept apart from smoke_nodes.py so it runs in seconds, and smoke_nodes.py runs it
too, so running that one still covers everything. Nothing here touches the
network or a database: each check replaces what it would have called.

    python scripts/smoke_resilience.py
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _seconds(text: str) -> float:
    return float(re.fullmatch(r"(\d+)s", text.strip()).group(1))


class _Fake:
    """Stands in for a Postgres saver: anything that is not a MemorySaver."""


async def _checkpointer_checks() -> list[tuple[str, bool]]:
    from langgraph.checkpoint.memory import MemorySaver

    from app.config import settings
    from app.graph import graph

    results: list[tuple[str, bool]] = []

    async def fails(open_timeout: float = 15):
        raise OSError("connection to postgresql://user:hunter2@db failed")

    def fast():
        return (
            patch.object(graph, "CHECKPOINTER_RETRY_SECONDS", 0.3),
            patch.object(graph, "CHECKPOINTER_RETRY_PAUSE_SECONDS", 0.05),
        )

    a, b = fast()
    with a, b:
        calls = {"n": 0}

        async def heals(open_timeout: float = 15):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("database not up yet")
            return _Fake()

        saver = None
        with patch.object(graph, "_build_checkpointer", heals):
            try:
                saver = await graph._durable_checkpointer()
            except graph.CheckpointerUnavailable:
                pass
        results.append(("a short failure heals itself: 3 attempts, then Postgres",
                        isinstance(saver, _Fake) and calls["n"] == 3))

        loop = asyncio.get_running_loop()
        started = loop.time()
        refused, message = False, ""
        with patch.object(graph, "_build_checkpointer", fails):
            try:
                saver = await graph._durable_checkpointer()
            except graph.CheckpointerUnavailable as exc:
                refused, message = True, str(exc)
        results.append(("a lasting failure refuses to start, never memory", refused))
        results.append(("the refusal stays within the retry budget", loop.time() - started < 1.5))
        results.append(("the refusal never quotes the connection string",
                        refused and "hunter2" not in message and "postgresql" not in message))

        # One attempt that hangs cannot outlast the budget.
        async def hangs(open_timeout: float = 15):
            await asyncio.sleep(30)

        started = loop.time()
        refused = False
        with patch.object(graph, "_build_checkpointer", hangs):
            try:
                await graph._durable_checkpointer()
            except graph.CheckpointerUnavailable:
                refused = True
        results.append(("a hung connection is cut at the budget",
                        refused and loop.time() - started < 1.5))

        # A failed attempt's pool is closed, not left to pile up.
        closed = {"n": 0}

        class _Pool:
            async def close(self):
                closed["n"] += 1

        async def leaves_a_pool(open_timeout: float = 15):
            graph._pool = _Pool()
            raise OSError("setup failed")

        with patch.object(graph, "_build_checkpointer", leaves_a_pool):
            try:
                await graph._durable_checkpointer()
            except graph.CheckpointerUnavailable:
                pass
        results.append(("each failed attempt closes its pool", closed["n"] >= 2 and graph._pool is None))

        # init_graph is the door lifespan goes through.
        saved = graph._graph, graph._checkpointer
        graph._graph, graph._checkpointer = None, None
        try:
            refused = False
            with patch.object(graph, "_build_checkpointer", fails):
                try:
                    await graph.init_graph()
                except graph.CheckpointerUnavailable:
                    refused = True
            results.append(("init_graph refuses, and leaves no graph behind",
                            refused and graph._graph is None and graph.checkpointer_backend() == "none"))

            with patch.object(graph, "REQUIRE_DURABLE_CHECKPOINTER", False), \
                    patch.object(graph, "_build_checkpointer", fails):
                await graph.init_graph()
            results.append(("switch off: the old fallback to memory",
                            graph.checkpointer_backend() == "memory"))
        finally:
            graph._graph, graph._checkpointer = saved

        # With no database configured, only a non-production machine gets memory.
        with patch.object(settings, "supabase_db_url", ""):
            with patch.object(settings, "environment", "development"):
                saver = await graph._durable_checkpointer()
            results.append(("no database, development: memory is allowed", isinstance(saver, MemorySaver)))
            refused = False
            with patch.object(settings, "environment", "production"):
                try:
                    await graph._durable_checkpointer()
                except graph.CheckpointerUnavailable:
                    refused = True
            results.append(("no database, production: refuses to start", refused))

    results.append(("the switch is on", graph.REQUIRE_DURABLE_CHECKPOINTER is True))

    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    start = re.search(r"start_period:\s*(\d+s)", compose)
    start_file = re.search(r"--start-period=(\d+s)", dockerfile)
    budget = graph.CHECKPOINTER_RETRY_SECONDS
    results.append((f"the {budget:g}s retry fits inside compose's start_period, with room for the rest",
                    bool(start) and _seconds(start.group(1)) >= budget + 20))
    results.append(("...and inside the Dockerfile's",
                    bool(start_file) and _seconds(start_file.group(1)) >= budget + 20))
    return results


async def _health_checks() -> list[tuple[str, bool]]:
    import http.server
    import threading
    from unittest.mock import AsyncMock

    from langgraph.checkpoint.memory import MemorySaver

    from app import healthcheck
    from app.api import health
    from app.config import settings
    from app.graph import graph

    results: list[tuple[str, bool]] = []
    results.append(("healthcheck: status ok is healthy", healthcheck.ready({"status": "ok"})))
    results.append(("healthcheck: degraded is not", not healthcheck.ready({"status": "degraded"})))
    results.append(("healthcheck: a body that is not the expected shape is not",
                    not healthcheck.ready([]) and not healthcheck.ready({})))

    async def ready_with(saver):
        keys = {"whapi_api_token": "x", "openrouter_api_key": "x", "tenant_id": "x",
                "embedding_api_key": "x", "transcription_api_key": "x"}
        saved = graph._checkpointer
        graph._checkpointer = saver
        patches = [patch.object(settings, name, value) for name, value in keys.items()]
        for p in patches:
            p.start()
        try:
            with patch.object(health.db, "healthcheck", AsyncMock(return_value=True)):
                return await health.readiness()
        finally:
            for p in patches:
                p.stop()
            graph._checkpointer = saved

    body = await ready_with(_Fake())
    results.append(("Postgres: ready says ok and names it",
                    body["status"] == "ok" and body.get("checkpointer") == "postgres"))
    body = await ready_with(MemorySaver())
    results.append(("memory: ready says degraded and names it",
                    body["status"] == "degraded" and body.get("checkpointer") == "memory"))
    body = await ready_with(None)
    results.append(("no checkpointer yet: ready says degraded", body["status"] == "degraded"))
    results.append(("the switch is on", health.CHECKPOINTER_IN_READY is True))

    # The real command against a real socket: /health/ready answers 200 either way.
    class Handler(http.server.BaseHTTPRequestHandler):
        payload = b"{}"

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(type(self).payload)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/health/ready"
    try:
        Handler.payload = b'{"status": "ok"}'
        code_ok = await asyncio.to_thread(healthcheck.main, url)
        Handler.payload = b'{"status": "degraded", "checkpointer": "memory"}'
        code_degraded = await asyncio.to_thread(healthcheck.main, url)
        Handler.payload = b"not json"
        code_garbage = await asyncio.to_thread(healthcheck.main, url)
    finally:
        server.shutdown()
        server.server_close()
    code_down = await asyncio.to_thread(healthcheck.main, url)
    results.append(("the command exits 0 on a 200 saying ok", code_ok == 0))
    results.append(("...and 1 on a 200 saying degraded", code_degraded == 1))
    results.append(("...and 1 on a 200 it cannot read", code_garbage == 1))
    results.append(("...and 1 when nothing answers", code_down == 1))

    source = (ROOT / "app" / "healthcheck.py").read_text(encoding="utf-8")
    results.append(("the command imports nothing from the app",
                    not re.search(r"^\s*(from|import)\s+app\b", source, re.M)))
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    results.append(("compose's healthcheck runs it",
                    '"python", "-m", "app.healthcheck"' in compose and "localhost:8000/health" not in compose))
    results.append(("the Dockerfile's healthcheck runs it",
                    "CMD python -m app.healthcheck" in dockerfile and "localhost:8000/health" not in dockerfile))
    return results


def _message(message_id: str, phone: str = "+6591234567"):
    from datetime import datetime, timezone

    from app.whapi.parser import IncomingMessage

    return IncomingMessage(
        whapi_message_id=message_id, chat_id="", customer_number=phone, customer_name="",
        body=f"body {message_id}", message_type="text", from_me=False,
        timestamp=datetime.now(timezone.utc),
    )


async def _shutdown_checks() -> list[tuple[str, bool]]:
    from app.api import webhook
    from app.whapi import debouncer as module
    from app.whapi.debouncer import MessageDebouncer

    results: list[tuple[str, bool]] = []
    handled: list[list[str]] = []

    async def handler(phone, batch):
        handled.append([m.whapi_message_id for m in batch])

    # Waiting out a long debounce: answered at once, not dropped.
    d = MessageDebouncer(delay=600, handler=handler)
    await d.add(_message("a"))
    await d.add(_message("b"))
    await d.add(_message("c", "+6598765432"))
    loop = asyncio.get_running_loop()
    started = loop.time()
    await d.shutdown()
    results.append(("waiting messages are answered, not dropped",
                    sorted(map(sorted, handled)) == [["a", "b"], ["c"]]))
    results.append(("...without waiting out the debounce", loop.time() - started < 2))
    results.append(("...and the buffers end empty", not d._buffers and not d._timers))

    # Switch off: the old behaviour.
    handled.clear()
    d = MessageDebouncer(delay=600, handler=handler)
    await d.add(_message("a"))
    with patch.object(module, "DRAIN_ON_SHUTDOWN", False):
        await d.shutdown()
    results.append(("switch off: dropped, as before", not handled and not d._buffers))
    results.append(("the switch is on", module.DRAIN_ON_SHUTDOWN is True))

    # A turn already running is waited for, not abandoned.
    finished: list[str] = []
    entered = asyncio.Event()

    async def slow(phone, batch):
        entered.set()
        await asyncio.sleep(0.2)
        finished.append(batch[0].whapi_message_id)

    d = MessageDebouncer(delay=0, handler=slow)
    await d.add(_message("running"))
    await asyncio.wait_for(entered.wait(), 2)
    await d.shutdown()
    results.append(("a turn already running is waited for", finished == ["running"]))

    # Bounded: a turn that never ends cannot outlast the limit.
    cancelled: list[bool] = []

    async def stuck(phone, batch):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    d = MessageDebouncer(delay=600, handler=stuck)
    await d.add(_message("stuck"))
    started = loop.time()
    with patch.object(module, "DRAIN_TIMEOUT_SECONDS", 0.2):
        await d.shutdown()
    results.append(("a stuck turn is cancelled at the limit",
                    loop.time() - started < 2 and cancelled == [True]))

    # One bad batch does not cost the others.
    handled.clear()

    async def one_bad(phone, batch):
        if batch[0].whapi_message_id == "bad":
            raise ValueError("boom")
        handled.append([m.whapi_message_id for m in batch])

    d = MessageDebouncer(delay=600, handler=one_bad)
    await d.add(_message("bad", "+6591111111"))
    await d.add(_message("good", "+6592222222"))
    await d.shutdown()
    results.append(("one failing batch does not cost the others", handled == [["good"]]))

    # A batch that lands back in the buffer during the drain is still answered.
    handled.clear()
    d = None

    async def requeues(phone, batch):
        ids = [m.whapi_message_id for m in batch]
        if ids == ["first"]:
            await d.requeue(phone, [_message("second")])
        handled.append(ids)

    d = MessageDebouncer(delay=600, handler=requeues)
    await d.add(_message("first"))
    await d.shutdown()
    results.append(("a batch requeued during the drain is answered",
                    handled == [["first"], ["second"]]))

    # The real handler, with only the turn itself stubbed.
    turns: list[list[str]] = []

    async def locked(phone, pending, *, allow_hold=True):
        turns.append([m.whapi_message_id for m in pending])

    d = MessageDebouncer(delay=600, handler=webhook.process_batch)
    await d.add(_message("real"))
    with patch.object(webhook, "_process_locked", locked), \
            patch.object(webhook.typing_indicator, "stop", _no_op):
        await d.shutdown()
    results.append(("the real process_batch runs the batch", turns == [["real"]]))

    main_source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    results.append(("lifespan drains before it closes Whapi and the graph",
                    main_source.index("debouncer.shutdown()") < main_source.index("whapi.close()")
                    < main_source.index("close_graph()")))
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    grace = re.search(r"stop_grace_period:\s*(\d+s)", compose)
    results.append((f"the {module.DRAIN_TIMEOUT_SECONDS:g}s limit fits inside stop_grace_period",
                    bool(grace) and _seconds(grace.group(1)) > module.DRAIN_TIMEOUT_SECONDS + 5))
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    results.append(("the app is PID 1 in exec form, so the stop signal reaches it",
                    'CMD ["python", "run.py"]' in dockerfile))
    return results


async def _no_op(*args, **kwargs):
    return None


async def checks() -> list[tuple[str, bool]]:
    return [
        *await _checkpointer_checks(),
        *await _health_checks(),
        *await _shutdown_checks(),
    ]


async def main() -> int:
    failures = 0
    for label, ok in await checks():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failures += not ok
    print("\nALL PASS" if not failures else f"\n{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
