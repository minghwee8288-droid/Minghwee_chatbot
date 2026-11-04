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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import no_network  # noqa: E402,F401 - first: this suite cannot reach production


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

    from starlette.requests import Request

    def http_request(peer="127.0.0.1", headers=()):
        return Request({"type": "http", "client": (peer, 1234), "query_string": b"",
                        "headers": [(k.lower().encode(), v.encode()) for k, v in headers]})

    def reset_channel():
        health._channel_checked_at = None
        health._channel_ok = False
        health._channel_unknown_in_a_row = 0

    async def ready_with(saver, database=True, channel=True, request=None, **overrides):
        keys = {"whapi_api_token": "x", "openrouter_api_key": "x", "tenant_id": "x",
                "embedding_api_key": "x", "transcription_api_key": "x", "transcription_enabled": True,
                "openai_api_key": "", **overrides}
        saved = graph._checkpointer
        graph._checkpointer = saver
        reset_channel()
        patches = [patch.object(settings, name, value) for name, value in keys.items()]
        for p in patches:
            p.start()
        try:
            with patch.object(health.db, "healthcheck", AsyncMock(return_value=database)), \
                    patch.object(health.whapi, "channel_connected",
                                 AsyncMock(side_effect=channel) if isinstance(channel, Exception)
                                 else AsyncMock(return_value=channel)):
                return await health.readiness(request or http_request())
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

    # Only these things decide the status.
    no_openrouter = {"openrouter_api_key": "", "transcription_api_key": ""}
    body = await ready_with(_Fake(), database=False)
    results.append(("database down: degraded", body["status"] == "degraded" and body["checks"]["database"] is False))
    body = await ready_with(_Fake(), whapi_api_token="")
    results.append(("no Whapi token: degraded", body["status"] == "degraded" and body["checks"]["whapi_token"] is False))
    for name, overrides, expected in (
        ("transcription key", {"transcription_api_key": "", "openrouter_api_key": ""}, ["openrouter_key", "transcription_key"]),
        ("embedding key", {"embedding_api_key": "", "openrouter_api_key": "", "embedding_base_url": "https://x"},
         ["embedding_key", "openrouter_key"]),
        ("tenant", {"tenant_id": ""}, ["tenant_id"]),
    ):
        body = await ready_with(_Fake(), **overrides)
        results.append((f"{name} missing: still ok, named as a warning",
                        body["status"] == "ok" and body.get("warnings") == expected and healthcheck.ready(body)))
    body = await ready_with(_Fake())
    results.append(("nothing failing: no warnings, and only the four checks",
                    body.get("warnings") == []
                    and set(body["checks"]) == {"database", "whapi_token", "whapi_channel", "checkpointer_durable"}))
    with patch.object(health, "CRITICAL_CHECKS_ONLY", False):
        body = await ready_with(_Fake(), **no_openrouter)
    results.append(("switch off: any failing check is degraded, as before",
                    body["status"] == "degraded" and "warnings" not in body))
    results.append(("that switch is on", health.CRITICAL_CHECKS_ONLY is True))

    # The body is public. Worst case: every check failing, secrets planted in
    # every setting, and the real db.healthcheck swallowing an error that quotes them.
    from fastapi.responses import JSONResponse

    secret = "sk-hunter2-4242"
    saved = graph._checkpointer
    graph._checkpointer = MemorySaver()
    try:
        with patch.object(settings, "whapi_api_token", ""), \
                patch.object(settings, "openrouter_api_key", ""), \
                patch.object(settings, "tenant_id", ""), \
                patch.object(settings, "embedding_api_key", ""), \
                patch.object(settings, "openai_api_key", ""), \
                patch.object(settings, "transcription_api_key", ""), \
                patch.object(settings, "supabase_service_role_key", secret), \
                patch.object(settings, "supabase_db_url", f"postgresql://u:{secret}@db:5432/x"), \
                patch.object(health.db, "execute", AsyncMock(side_effect=OSError(f"postgresql://u:{secret}@db:5432"))), \
                patch.object(health.db, "table", lambda *_: type("Q", (), {"select": lambda s, *a: s, "limit": lambda s, *a: s})()):
            worst = await health.readiness(http_request())
            public_worst = await health.readiness(http_request("172.18.0.1"))
    finally:
        graph._checkpointer = saved
    text = JSONResponse(worst).body.decode()
    results.append(("the public body quotes no secret and holds no number",
                    secret not in text and not re.search(r"\d", text)))
    results.append(("...and holds exactly these fields",
                    set(worst) == {"checks", "checkpointer", "warnings", "status"}
                    and all(isinstance(v, bool) for v in worst["checks"].values())
                    and all(isinstance(v, str) for v in worst.get("warnings", []))))
    results.append(("...and leaves out what /health shows (version, environment)",
                    "version" not in worst and "environment" not in worst))
    results.append(("the public body, everything failing, names no setting",
                    set(public_worst) == {"checks", "checkpointer", "status"}
                    and public_worst["status"] == "degraded"
                    and not {"openrouter_key", "embedding_key", "transcription_key", "tenant_id"} & set(public_worst["checks"])
                    and not re.search(r"openrouter|embedding|transcription|tenant", JSONResponse(public_worst).body.decode())))

    # Who sees warnings, through the real route so the Request is really injected.
    from fastapi import FastAPI
    import httpx as _httpx

    route_app = FastAPI()
    route_app.include_router(health.router)
    missing = {"transcription_api_key": "", "openrouter_api_key": ""}

    async def over_http(peer, headers=None, key="", url="/health/ready", **overrides):
        keys = {"whapi_api_token": "x", "openrouter_api_key": "x", "tenant_id": "x", "embedding_api_key": "x",
                "transcription_api_key": "x", "transcription_enabled": True, "openai_api_key": "",
                "health_details_key": key, **overrides}
        patches = [patch.object(settings, n, v) for n, v in keys.items()]
        saved_saver = graph._checkpointer
        graph._checkpointer = _Fake()
        reset_channel()
        for p in patches:
            p.start()
        try:
            with patch.object(health.db, "healthcheck", AsyncMock(return_value=True)), \
                    patch.object(health.whapi, "channel_connected", AsyncMock(return_value=True)):
                transport = _httpx.ASGITransport(app=route_app, client=(peer, 1234))
                async with _httpx.AsyncClient(transport=transport, base_url="http://bot") as client:
                    return await client.get(url, headers=headers or {})
        finally:
            for p in patches:
                p.stop()
            graph._checkpointer = saved_saver

    shown = lambda r: "warnings" in r.json() and "openrouter_key" in r.json()["warnings"]  # noqa: E731
    gateway = "172.18.0.1"
    cases = (
        ("inside the container sees them", dict(peer="127.0.0.1"), True),
        ("IPv6 loopback too", dict(peer="::1"), True),
        ("through nginx they are hidden", dict(peer=gateway, headers={"X-Forwarded-For": "8.8.8.8"}), False),
        ("from Docker's gateway with no header, hidden", dict(peer=gateway), False),
        ("a spoofed forward that makes the peer 127.0.0.1, hidden",
         dict(peer="127.0.0.1", headers={"X-Forwarded-For": "127.0.0.1"}), False),
        ("X-Real-IP counts as a proxy", dict(peer="127.0.0.1", headers={"X-Real-IP": "1.2.3.4"}), False),
        ("Forwarded counts as a proxy", dict(peer="127.0.0.1", headers={"Forwarded": "for=1.2.3.4"}), False),
        ("the right key from outside sees them",
         dict(peer=gateway, key="k-right", headers={"X-Health-Key": "k-right", "X-Forwarded-For": "8.8.8.8"}), True),
        ("the wrong key does not", dict(peer=gateway, key="k-right", headers={"X-Health-Key": "nope"}), False),
        ("no key set: an empty header does not open it", dict(peer=gateway, headers={"X-Health-Key": ""}), False),
        ("a key with non-ASCII does not crash it", dict(peer=gateway, key="k", headers={"X-Health-Key": "é".encode("latin-1")}), False),
        ("the key in the URL does not count", dict(peer=gateway, key="k-right", url="/health/ready?x-health-key=k-right"), False),
    )
    for name, kwargs, expected in cases:
        response = await over_http(**kwargs, **missing)
        results.append((f"warnings: {name}", response.status_code == 200 and shown(response) is expected))
    hidden = await over_http(peer=gateway, **missing)
    results.append(("hidden means the word is not in the bytes, and the status is unchanged",
                    "warnings" not in hidden.text and hidden.json()["status"] == "ok" and '"status":"ok"' in hidden.text))
    with patch.object(health, "WARNINGS_PRIVATE", False):
        response = await over_http(peer=gateway, **missing)
    results.append(("switch off: warnings for everyone, as before", shown(response)))
    results.append(("that switch is on", health.WARNINGS_PRIVATE is True))

    # What the uptime monitor matches on, as the bytes it will really receive.
    wire = lambda b: JSONResponse(b).body.decode()  # noqa: E731
    healthy = wire(await ready_with(_Fake()))
    results.append(("the monitor's keywords are in a healthy body", "postgres" in healthy and '"status":"ok"' in healthy))
    for name, saver in (("on memory", MemorySaver()), ("with no checkpointer", None)):
        text = wire(await ready_with(saver))
        results.append((f"{name} neither keyword is there", "postgres" not in text and '"status":"ok"' not in text))
    down = wire(await ready_with(_Fake(), database=False))
    results.append(("database down: only the status keyword goes, postgres stays",
                    "postgres" in down and '"status":"ok"' not in down))
    unpaired = wire(await ready_with(_Fake(), channel=False))
    results.append(("channel down: only the status keyword goes, postgres stays",
                    "postgres" in unpaired and '"status":"ok"' not in unpaired))

    # What the Whapi call makes of each answer, through the real client.
    import httpx

    from app.whapi.client import WhapiClient

    seen_queries: list[str] = []

    async def channel_answer(respond) -> object:
        def handler(request):
            seen_queries.append(str(request.url.query, "ascii"))
            return respond(request)

        client = WhapiClient(token="x", base_url="https://whapi.test")
        client._client = httpx.AsyncClient(base_url="https://whapi.test", transport=httpx.MockTransport(handler))
        try:
            return await client.channel_connected(2.5)
        finally:
            await client.close()

    def stall(request):
        raise httpx.ReadTimeout("stalled", request=request)

    full = {"status": {"code": 4, "text": "AUTH"}, "user": {"id": "6565342277"}, "ip": "10.0.0.1"}
    for name, respond, expected in (
        ("AUTH is connected", lambda r: httpx.Response(200, json=full), True),
        ("QR is not", lambda r: httpx.Response(200, json={"status": {"code": 3, "text": "QR"}}), False),
        ("no status is not", lambda r: httpx.Response(200, json={}), False),
        ("a 404 is not", lambda r: httpx.Response(404, json={}), False),
        ("a 401 is not", lambda r: httpx.Response(401, json={}), False),
        ("a 500 cannot tell", lambda r: httpx.Response(500, json={}), None),
        ("a 429 cannot tell", lambda r: httpx.Response(429, json={}), None),
        ("a stall cannot tell", stall, None),
        ("not JSON cannot tell", lambda r: httpx.Response(200, text="<html>"), None),
        ("a list cannot tell", lambda r: httpx.Response(200, json=[]), None),
    ):
        results.append((f"Whapi channel: {name}", await channel_answer(respond) is expected))
    results.append(("Whapi channel: never asks it to launch the channel", set(seen_queries) == {"wakeup=false"}))

    # The cache: public endpoint, so one call a minute whoever is asking.
    clock = [1000.0]

    async def connected_after_a_pause(*_):
        await asyncio.sleep(0)
        return True

    calls = AsyncMock(side_effect=connected_after_a_pause)

    # health's own name for the clock only: the real time.monotonic is the event loop's.
    def channel_patches(token="x"):
        return (patch.object(settings, "whapi_api_token", token),
                patch.object(health.whapi, "channel_connected", calls),
                patch.object(health, "time", type("Clock", (), {"monotonic": staticmethod(lambda: clock[0])})))

    async def asked(answer=None, token="x"):
        if answer is not None:
            calls.side_effect = answer
        one, two, three = channel_patches(token)
        with one, two, three:
            return await health._whapi_channel_ok()

    reset_channel()
    one, two, three = channel_patches()
    with one, two, three:
        together = await asyncio.gather(*(health._whapi_channel_ok() for _ in range(20)))
    results.append(("cache: twenty at once make one call", calls.await_count == 1 and all(together)))
    clock[0] += 59
    await asked()
    results.append(("cache: inside 60s no second call", calls.await_count == 1))
    clock[0] += 2
    await asked()
    results.append(("cache: past 60s it asks again", calls.await_count == 2))
    results.append(("cache: the call is given the short timeout",
                    calls.await_args.args == (health._CHANNEL_TIMEOUT_SECONDS,)))
    results.append(("the timeout is under the healthcheck command's",
                    health._CHANNEL_TIMEOUT_SECONDS < healthcheck.TIMEOUT_SECONDS))
    results.append(("the cache is 60s", health._CHANNEL_CACHE_SECONDS == 60.0))

    clock[0] += 61
    one_stall = await asked(answer=[None])
    clock[0] += 30
    no_call_yet = calls.await_count
    await asked()
    results.append(("one stall keeps the last good answer, and is cached too",
                    one_stall is True and calls.await_count == no_call_yet))
    clock[0] += 31
    second_stall = await asked(answer=[None])
    results.append(("a second stall in a row is not connected", second_stall is False))
    clock[0] += 61
    recovered = await asked(answer=[True])
    results.append(("...and it recovers on the next good answer", recovered is True))
    clock[0] += 61
    results.append(("...and a good answer clears the count of stalls", await asked(answer=[None]) is True))
    clock[0] += 61
    await asked(answer=[True])
    clock[0] += 61
    results.append(("Whapi saying not connected counts at once", await asked(answer=[False]) is False))
    reset_channel()
    results.append(("no answer yet and a stall is not connected", await asked(answer=[None]) is False))
    reset_channel()
    calls.reset_mock()
    results.append(("no token: false, and no call", await asked(token="") is False and calls.await_count == 0))

    secret_error = RuntimeError("https://x?token=sk-hunter2-4242")
    raised = wire(await ready_with(_Fake(), channel=secret_error))
    results.append(("the call raising: degraded, and its text is not in the body",
                    '"status":"ok"' not in raised and "hunter2" not in raised))
    with patch.object(health, "WHAPI_CHANNEL_IN_READY", False):
        calls.reset_mock()
        with patch.object(health.whapi, "channel_connected", calls):
            body = await ready_with(_Fake(), channel=False)
    results.append(("switch off: not asked, not reported, and it decides nothing",
                    body["status"] == "ok" and "whapi_channel" not in body["checks"]))
    results.append(("that switch is on", health.WHAPI_CHANNEL_IN_READY is True))

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


class _Query:
    """The few postgrest calls recovery.py makes, run over an in-memory table."""

    def __init__(self, store, table):
        self.store, self.table, self.filters, self.limit_n, self.upserted = store, table, [], None, None

    def select(self, *_):
        return self

    def eq(self, column, value):
        self.filters.append(lambda r: r.get(column) == value)
        return self

    def gte(self, column, value):
        self.filters.append(lambda r: r.get(column) >= value)
        return self

    def lt(self, column, value):
        self.filters.append(lambda r: r.get(column) < value)
        return self

    def order(self, column, desc=False):
        self.order_by = (column, desc)
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def upsert(self, rows, on_conflict="", ignore_duplicates=False):
        self.upserted = rows
        return self

    def run(self):
        from types import SimpleNamespace

        if self.upserted is not None:
            if self.store.get("claims_fail"):
                raise RuntimeError('relation "cb_recovery_claims" does not exist')
            won = []
            for row in self.upserted:
                if row["whapi_message_id"] not in self.store["claims"]:
                    self.store["claims"][row["whapi_message_id"]] = row
                    won.append(row)
            return SimpleNamespace(data=won)
        rows = [r for r in self.store["messages"] if all(f(r) for f in self.filters)]
        column, desc = self.order_by
        rows = sorted(rows, key=lambda r: r[column], reverse=desc)
        return SimpleNamespace(data=rows[: self.limit_n])


class _FakeDb:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _Query(self.store, name)

    async def execute(self, query):
        return query.run()


async def _recovery_checks() -> list[tuple[str, bool]]:
    from datetime import datetime, timedelta, timezone

    from app.api import webhook
    from app.config import Settings, settings
    from app.services import conversation as conversation_service
    from app.services import handover as handover_service
    from app.services import internal_contacts
    from app.services import message as message_service
    from app.services import recovery
    from app.services import ticket as ticket_service

    results: list[tuple[str, bool]] = []
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    ours = "6565342277"
    phone = "6591234567"

    def at(minutes_ago):
        return (now - timedelta(minutes=minutes_ago)).isoformat()

    def inbound(mid, minutes_ago, conversation_id=1, **extra):
        return {"conversation_id": conversation_id, "direction": "inbound", "body": f"text {mid}",
                "is_bot": False, "sent_by": None, "to_number": ours, "whapi_message_id": mid,
                "created_at": at(minutes_ago), **extra}

    def outbound(minutes_ago, conversation_id=1, sent_by="bot", body="a reply", is_bot=True):
        return {"conversation_id": conversation_id, "direction": "outbound", "body": body,
                "is_bot": is_bot, "sent_by": sent_by, "to_number": phone, "whapi_message_id": f"out{minutes_ago}",
                "created_at": at(minutes_ago)}

    def conversation(conversation_id=1, **extra):
        return {"id": conversation_id, "customer_number": phone, "customer_name": "Tan",
                "bot_status": "bot_active", "status": "open", **extra}

    async def scan(messages, conversations=None, *, enabled=True, claims=None, claims_fail=False,
                   allowed=phone, internal=False, quoted_origin=None, paused=False, sender=ours):
        conversations = {c["id"]: c for c in (conversations or [conversation()])}
        store = {"messages": messages, "claims": dict(claims or {}), "claims_fail": claims_fail}
        batches: list[list[str]] = []
        overrides: list[str] = []
        order: list[str] = []
        webhook._PROCESSED.clear()

        async def process(p, batch):
            order.append("reply")
            batches.append([m.whapi_message_id for m in batch])

        async def get_by_id(cid):
            return conversations.get(cid)

        async def get_or_create(p, name, engage=False):
            return next(iter(conversations.values())), False

        async def override(conv, text):
            overrides.append(text)
            return True

        async def origin(_):
            return quoted_origin

        async def is_internal(_):
            return internal

        async def not_auto(body, conversation_id=None):
            return body == "Dear Valued Customer"

        async def returned(conv):
            return {**conv, "bot_status": "human_active"} if paused else conv

        async def nobody(*_):
            return None

        async def not_picked_up(*_):
            return False

        real_claim = recovery._claim

        async def claim(*args):
            order.append("claim")
            return await real_claim(*args)

        with patch.object(recovery, "db", _FakeDb(store)), \
                patch.object(settings, "startup_recovery_scan", enabled), \
                patch.object(settings, "whapi_sender_phone", sender), \
                patch.object(settings, "bot_allowed_numbers", allowed), \
                patch.object(recovery, "_claim", claim), \
                patch.object(conversation_service, "get_by_id", get_by_id), \
                patch.object(conversation_service, "get_or_create", get_or_create), \
                patch.object(webhook, "process_batch", process), \
                patch.object(webhook, "emergency_override", override), \
                patch.object(webhook, "acknowledge_direct_address", _no_op), \
                patch.object(webhook, "maybe_return_to_bot", returned), \
                patch.object(webhook, "_should_identify", lambda *a: False), \
                patch.object(internal_contacts, "is_internal", is_internal), \
                patch.object(message_service, "is_auto_reply", not_auto), \
                patch.object(message_service, "quoted_message_origin", origin), \
                patch.object(message_service, "last_agent_message_at", nobody), \
                patch.object(handover_service, "agent_took_over", _no_op), \
                patch.object(ticket_service, "picked_up_recently", not_picked_up):
            counts = await recovery.run(now)
        return {"batches": batches, "overrides": overrides, "counts": counts, "order": order, "store": store}

    # Off.
    results.append(("the switch defaults to off",
                    Settings.model_fields["startup_recovery_scan"].default is False))
    r = await scan([inbound("m1", 2)], enabled=False)
    results.append(("switched off: nothing read, claimed or sent",
                    not r["batches"] and not r["store"]["claims"] and r["counts"] == {}))

    # The lifespan only starts it when switched on.
    from app import main as app_main

    started: list[str] = []

    async def recording_run(until=None):
        started.append("ran")
        return {}

    async def in_lifespan(enabled):
        started.clear()
        with patch.object(settings, "startup_recovery_scan", enabled), \
                patch.object(recovery, "run", recording_run), \
                patch.object(app_main, "init_graph", _no_op), \
                patch.object(app_main, "close_graph", _no_op), \
                patch.object(app_main.debouncer, "shutdown", _no_op), \
                patch.object(app_main.whapi, "close", _no_op):
            async with app_main.lifespan(app_main.app):
                await asyncio.sleep(0)
        return list(started)

    results.append(("lifespan does not start it when off", await in_lifespan(False) == []))
    results.append(("lifespan starts it when on", await in_lifespan(True) == ["ran"]))

    # One batch, claimed first.
    r = await scan([outbound(30), inbound("m1", 5), inbound("m2", 4), inbound("m3", 3)])
    results.append(("everything since our last outbound is ONE batch",
                    r["batches"] == [["m1", "m2", "m3"]]))
    results.append(("each message is claimed by its Whapi id", set(r["store"]["claims"]) == {"m1", "m2", "m3"}))
    results.append(("the claim comes before the reply", r["order"] == ["claim", "reply"]))
    r = await scan([inbound("old", 200), outbound(100), inbound("m1", 3)])
    results.append(("what came before our last outbound stays out", r["batches"] == [["m1"]]))

    # Already answered, by anyone.
    r = await scan([inbound("m1", 5), outbound(3)])
    results.append(("a bot reply after it: nothing to do", not r["batches"] and not r["store"]["claims"]))
    r = await scan([inbound("m1", 5), outbound(3, sent_by="Shirley", is_bot=False)])
    results.append(("an agent's reply after it: nothing to do", not r["batches"]))
    r = await scan([inbound("m1", 5)], [conversation(last_bot_reply_at=at(4))])
    results.append(("our reply went out but its row was not stored: nothing to do", not r["batches"]))

    # An auto-reply answers nobody.
    r = await scan([inbound("m1", 5), outbound(4, sent_by="whatsapp_auto")])
    results.append(("a WhatsApp auto-reply is not an answer", r["batches"] == [["m1"]]))
    r = await scan([inbound("m1", 5), outbound(4, sent_by="Ming Hwee Agency", body="Dear Valued Customer", is_bot=False)])
    results.append(("...nor one the portal stored as an ordinary row", r["batches"] == [["m1"]]))

    # The window.
    r = await scan([inbound("m1", 11)])
    results.append(("past the window: left alone", not r["batches"] and not r["store"]["claims"]))
    r = await scan([inbound("old", 300), inbound("m1", 9), inbound("m2", 3)])
    results.append(("only what is inside the window goes in the batch", r["batches"] == [["m1", "m2"]]))
    results.append(("...and an older message is not claimed", set(r["store"]["claims"]) == {"m1", "m2"}))
    r = await scan([inbound("edge", recovery.RECOVERY_WINDOW_MINUTES), inbound("past", recovery.RECOVERY_WINDOW_MINUTES + 1)])
    results.append(("exactly at the window's edge is inside it", r["batches"] == [["edge"]]))
    with patch.object(recovery, "BATCH_ONLY_INSIDE_WINDOW", False):
        r = await scan([inbound("old", 300), inbound("m1", 3)])
    results.append(("switch off: the whole unanswered run, as before", r["batches"] == [["old", "m1"]]))
    with patch.object(recovery, "BATCH_ONLY_INSIDE_WINDOW", False):
        # The row with no id puts the conversation in the scan and then leaves only old ones.
        r = await scan([inbound("old", 300), {**inbound("x", 3), "whapi_message_id": None}])
    results.append(("...and still nothing when the newest left is past the window", not r["batches"]))
    results.append(("the window switch is on", recovery.BATCH_ONLY_INSIDE_WINDOW is True))
    # whapi_message_id is nullable: a row in the window with no id leaves only old ones.
    r = await scan([inbound("old", 300), {**inbound("x", 3), "whapi_message_id": None}])
    results.append(("only old messages left to answer: left alone", not r["batches"]))
    r = await scan([inbound("m1", 3), inbound("live", -1)])
    results.append(("a message from after startup is the live webhook's", r["batches"] == [["m1"]]))

    # Claims.
    r = await scan([inbound("m1", 3)], claims={"m1": {}})
    results.append(("a message already claimed is not answered again", not r["batches"]))
    r = await scan([inbound("m1", 4), inbound("m2", 3)], claims={"m1": {}})
    results.append(("...and the rest of the batch still is", r["batches"] == [["m2"]]))
    r = await scan([inbound("m1", 3)], claims_fail=True)
    results.append(("no claims table: nothing is sent", not r["batches"]))
    webhook._PROCESSED.clear()
    r = await scan([inbound("m1", 3)])
    results.append(("recovered ids are marked seen, so a redelivery is dropped",
                    webhook._already_processed("m1") is True))

    # Who may be messaged.
    r = await scan([inbound("m1", 3, to_number="639000000000")])
    results.append(("another channel's message is not answered from ours", not r["batches"]))
    r = await scan([inbound("m1", 3)], sender="")
    results.append(("our own number unknown: no scan at all", not r["batches"] and r["counts"] == {"refused": 1}))
    r = await scan([inbound("m1", 3)], [conversation(is_group=True)])
    results.append(("a group is not answered", not r["batches"]))
    # Fabricated rows, by shape. Every number here is made up.
    def row_verdict(number, name="Tan"):
        return recovery._not_ours_to_answer({"customer_number": number, "customer_name": name})

    for number, why in (("116909177569373", "15 digits"), ("95786411008174", "14 digits, reads like Myanmar"),
                        ("7312345678901", "13 digits, no real prefix")):
        results.append((f"LID-shaped ({why}), with a push name: not answered", row_verdict(number) == "no_real_number"))
    for number, why in (("6591234567", "Singapore"), ("919876543210", "India"), ("6281234567890", "Indonesia, 13"),
                        ("62881234567890", "Indonesia, 14"), ("8801712345678", "Bangladesh, 13"),
                        ("8613812345678", "China, 13")):
        results.append((f"a real number ({why}) is answered", row_verdict(number) == ""))
    results.append(("a real client with no push name is answered", row_verdict(phone, f"+{phone}") == ""))
    results.append(("a chat id instead of a number is not answered", row_verdict("116909177569373@lid") == "no_real_number"))
    r = await scan([inbound("m1", 3)], [conversation(customer_number="116909177569373")], allowed="116909177569373")
    results.append(("a LID row gets nothing, past every other gate", not r["batches"] and not r["store"]["claims"]))
    r = await scan([inbound("m1", 3)], [conversation(customer_name=f"+{phone}")])
    results.append(("...and a row merely named after its number gets its reply", r["batches"] == [["m1"]]))
    with patch.object(recovery, "LID_SHAPE_NOT_NAME", False):
        results.append(("switch off: the name decides, as before",
                        row_verdict(phone, f"+{phone}") == "no_real_number" and row_verdict("116909177569373") == ""))
    results.append(("the shape switch is on", recovery.LID_SHAPE_NOT_NAME is True))
    r = await scan([inbound("m1", 3)], allowed="6500000000")
    results.append(("the allowlist applies", not r["batches"] and not r["store"]["claims"]))
    r = await scan([inbound("m1", 3)], internal=True)
    results.append(("an internal number is not answered", not r["batches"]))
    r = await scan([inbound("m1", 3, reply_to_whapi_id="staff1")], quoted_origin="agent")
    results.append(("a client quoting a staff message is left to them", not r["batches"]))
    r = await scan([inbound("m1", 3, reply_to_whapi_id="ours1")], quoted_origin="bot")
    results.append(("...quoting the bot is an ordinary message", r["batches"] == [["m1"]]))
    r = await scan([inbound("m1", 3)], [conversation(bot_status="human_active")], paused=True)
    results.append(("a human's thread gets no reply", not r["batches"]))
    results.append(("...only the safety check on what they said", r["overrides"] == ["text m1"]))

    # The cap.
    many = [inbound(f"m{i}", 3, conversation_id=i) for i in range(1, recovery.RECOVERY_MAX_BATCHES + 2)]
    r = await scan(many, [conversation(i) for i in range(1, recovery.RECOVERY_MAX_BATCHES + 2)])
    results.append(("over the cap: not one message goes out",
                    not r["batches"] and not r["store"]["claims"]
                    and r["counts"] == {"refused": recovery.RECOVERY_MAX_BATCHES + 1}))
    many = many[:recovery.RECOVERY_MAX_BATCHES]
    r = await scan(many, [conversation(i) for i in range(1, recovery.RECOVERY_MAX_BATCHES + 1)])
    results.append(("at the cap: each gets its own batch", len(r["batches"]) == recovery.RECOVERY_MAX_BATCHES))

    # A stored row comes back as the message the webhook parsed.
    message = recovery._as_message(
        {"whapi_message_id": "v1", "media_type": "voice", "media_path": "https://x/a.ogg", "body": None,
         "reply_to_whapi_id": "q1", "created_at": at(1)}, "+6591234567", "Tan")
    results.append(("a stored row rebuilds: media, and the quoted id",
                    message.has_media and message.quoted_id == "q1" and message.media_url == "https://x/a.ogg"))

    # The real client builds the claim the way it has to be: atomic, returning only what it won.
    from postgrest import SyncPostgrestClient

    query = SyncPostgrestClient("http://localhost:9").table(recovery.CLAIMS_TABLE).upsert(
        [{"whapi_message_id": "x", "conversation_id": 1}], on_conflict="whapi_message_id", ignore_duplicates=True
    )
    prefer = str(query.headers.get("prefer") or query.headers.get("Prefer"))
    results.append(("the claim skips duplicates and returns only the rows it inserted",
                    "ignore-duplicates" in prefer and "return=representation" in prefer
                    and "whapi_message_id" in str(query.params)))

    sql = (ROOT / "scripts" / "sql" / "recovery_001_claims.sql").read_text(encoding="utf-8")
    results.append(("the migration exists, and only creates its own table",
                    "cb_recovery_claims" in sql and "whapi_message_id  text primary key" in sql
                    and not re.search(r"alter table public\.(wp_|leads|employers)|references\s", sql, re.I)))
    results.append(("the rollback exists",
                    (ROOT / "scripts" / "sql" / "rollback" / "rollback_recovery_001_claims.sql").is_file()))
    return results


async def _forwarded_checks() -> list[tuple[str, bool]]:
    import os
    import runpy
    import tempfile

    import httpx
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from app import forwarded
    from app.api import health
    from app.config import settings

    results: list[tuple[str, bool]] = []
    header = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
    subnet = "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
    default = "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"

    with tempfile.TemporaryDirectory() as folder:
        def gateway_from(text: str) -> str | None:
            path = os.path.join(folder, "route")
            with open(path, "w", encoding="ascii") as out:
                out.write(text)
            return forwarded.default_gateway(path)

        results.append(("the gateway is read from the default route", gateway_from(header + subnet + default) == "172.18.0.1"))
        results.append(("a table with no default route has no gateway", gateway_from(header + subnet) is None))
        results.append(("a default route with no gateway has none", gateway_from(header + "eth0\t00000000\t00000000\t0001\n") is None))
        results.append(("a malformed table has no gateway, and no crash",
                        gateway_from(header + "eth0\t00000000\tnothex\n") is None and gateway_from("x\ny") is None))
        results.append(("no route file has no gateway", forwarded.default_gateway(os.path.join(folder, "absent")) is None))

    def allowed(env: str | None, gateway: str | None) -> str:
        values = {} if env is None else {"FORWARDED_ALLOW_IPS": env}
        with patch.dict(os.environ, values), patch.object(forwarded, "default_gateway", return_value=gateway):
            if env is None:
                os.environ.pop("FORWARDED_ALLOW_IPS", None)
            return forwarded.allowed_ips()

    results.append(("only the gateway is believed", allowed(None, "172.18.0.1") == "172.18.0.1"))
    results.append(("not in a container: only loopback", allowed(None, None) == "127.0.0.1"))
    results.append(("the setting overrides the gateway", allowed("10.0.0.5", "172.18.0.1") == "10.0.0.5"))
    results.append(("a blank setting is no setting", allowed("  ", "172.18.0.1") == "172.18.0.1"))
    results.append(("the setting cannot open it to everyone",
                    allowed("*", "172.18.0.1") == "172.18.0.1" and allowed("10.0.0.5,*", "172.18.0.1") == "172.18.0.1"))
    with patch.object(forwarded, "NGINX_ONLY_FORWARDED", False):
        results.append(("switch off: everyone, as before", allowed(None, "172.18.0.1") == "*"))
    results.append(("that switch is on", forwarded.NGINX_ONLY_FORWARDED is True))

    # run.py really hands that value to uvicorn.
    policy = asyncio.get_event_loop_policy()
    try:
        with patch("uvicorn.run") as started, patch.object(forwarded, "allowed_ips", return_value="172.18.0.1"):
            runpy.run_path(str(ROOT / "run.py"), run_name="__main__")
    finally:
        asyncio.set_event_loop_policy(policy)
    kwargs = started.call_args.kwargs if started.call_args else {}
    results.append(("run.py passes allowed_ips() to uvicorn",
                    kwargs.get("forwarded_allow_ips") == "172.18.0.1" and kwargs.get("proxy_headers") is True))
    results.append(("startup logs who is believed", "forwarded.allowed_ips()" in (ROOT / "app" / "main.py").read_text(encoding="utf-8")))

    # What the app sees, through uvicorn's real middleware and the value above.
    gateway = "172.18.0.1"

    async def seen_as(peer: str, trusted: str, headers: dict) -> dict:
        scopes: list[dict] = []

        async def seen_app(scope, receive, send) -> None:
            scopes.append(scope)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        transport = httpx.ASGITransport(app=ProxyHeadersMiddleware(seen_app, trusted_hosts=trusted), client=(peer, 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://bot") as client:
            await client.get("/seen", headers=headers)
        return {"host": scopes[0]["client"][0], "scheme": scopes[0]["scheme"]}

    trusted = allowed(None, gateway)
    cases = (
        ("through nginx: the caller's address", gateway, {"X-Forwarded-For": "9.9.9.9"}, "9.9.9.9", "http"),
        ("through nginx: the scheme it reports", gateway, {"X-Forwarded-Proto": "https"}, gateway, "https"),
        ("a forged value nginx appended to: nginx's entry wins", gateway, {"X-Forwarded-For": "127.0.0.1, 9.9.9.9"}, "9.9.9.9", "http"),
        ("from anyone else: the address is ignored", "8.8.8.8", {"X-Forwarded-For": "127.0.0.1"}, "8.8.8.8", "http"),
        ("from anyone else: the scheme is ignored", "8.8.8.8", {"X-Forwarded-Proto": "https"}, "8.8.8.8", "http"),
        ("from inside the container: ignored too", "127.0.0.1", {"X-Forwarded-For": "9.9.9.9"}, "127.0.0.1", "http"),
    )
    for name, peer, headers, host, scheme in cases:
        results.append((f"forwarded: {name}", await seen_as(peer, trusted, headers) == {"host": host, "scheme": scheme}))
    results.append(("forwarded: with * a forged value was believed, which is why this exists",
                    (await seen_as("8.8.8.8", "*", {"X-Forwarded-For": "127.0.0.1"}))["host"] == "127.0.0.1"))

    # The one reader of the client's address, end to end.
    route_app = FastAPI()
    route_app.include_router(health.router)

    async def ready_as(peer: str, headers: dict) -> str:
        transport = httpx.ASGITransport(app=ProxyHeadersMiddleware(route_app, trusted_hosts=trusted), client=(peer, 1234))
        with patch.object(settings, "transcription_api_key", ""), patch.object(settings, "transcription_enabled", True), \
                patch.object(settings, "openai_api_key", ""), patch.object(settings, "health_details_key", ""), \
                patch.object(health, "WARNINGS_PRIVATE", True), \
                patch.object(health.db, "healthcheck", AsyncMock(return_value=True)), \
                patch.object(health.whapi, "channel_connected", AsyncMock(return_value=True)):
            async with httpx.AsyncClient(transport=transport, base_url="http://bot") as client:
                return (await client.get("/health/ready", headers=headers)).text

    for name, peer, headers in (
        ("a stranger forging loopback", "8.8.8.8", {"X-Forwarded-For": "127.0.0.1"}),
        ("nginx passing a forged loopback through", gateway, {"X-Forwarded-For": "127.0.0.1"}),
        ("nginx appending to a forged loopback", gateway, {"X-Forwarded-For": "127.0.0.1, 9.9.9.9"}),
    ):
        results.append((f"forwarded: {name} sees no warnings", '"warnings"' not in await ready_as(peer, headers)))
    results.append(("forwarded: the container itself still sees them", '"warnings"' in await ready_as("127.0.0.1", {})))
    return results


async def _json_checks() -> list[tuple[str, bool]]:
    """The shared parser, the bot's complete_json and the suggest route's draft."""
    import logging
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.graph import llm
    from app.suggest import llm as suggest_llm

    results: list[tuple[str, bool]] = []
    parse = llm.try_parse_json
    whole = '{"suggestions": [{"text": "a"}], "language": "en"}'
    first = {"suggestions": [{"text": "a"}], "language": "en"}

    # The shape replayed from section 9.54, and its relations.
    results.append(("json: two complete objects give the first", parse(whole + whole) == first))
    results.append(("json: a space between them changes nothing", parse(whole + "\n" + whole) == first))
    results.append(("json: a cut-off second object still gives the first", parse(whole + '{"suggestions": [') == first))
    results.append(("json: words before and two objects after", parse("Here you go: " + whole + whole) == first))
    results.append(("json: two objects in a code fence", parse("```json\n" + whole + "\n" + whole + "\n```") == first))
    results.append(("json: words after a closing brace, then a stray brace", parse(whole + " see {x}") == first))

    # What it must not start accepting.
    results.append(("json: a cut-off reply is not read from its inner object", parse('{"a": {"b": 1}, "c":') is None))
    results.append(("json: a stray {} in front is not an answer", parse('{} {"a": 1}') is None))
    results.append(("json: a list of numbers is not an object", parse("[1, 2]") is None))
    results.append(("json: words only", parse("sorry, no") is None))
    results.append(("json: nothing at all", parse("") is None))

    # What it did already.
    results.append(("json: one object as before", parse(whole) == first))
    results.append(("json: {} alone still means it said nothing", parse("{}") == {}))
    results.append(("json: prose around one object as before", parse("ok " + whole + " done") == first))

    with patch.object(llm, "FIRST_JSON_OBJECT", False):
        results.append(("json: switch off, two objects are a failure again", parse(whole + whole) is None))
    results.append(("json: the switch is on", llm.FIRST_JSON_OBJECT is True))

    # complete_json: read at once, so no second model call.
    model = AsyncMock(return_value=whole + whole)
    with patch.object(llm, "complete", model):
        got = await llm.complete_json("s", "u", default={"x": 1})
    results.append(("json: complete_json takes the first object", got == first))
    results.append(("json: ...with no second model call", model.await_count == 1))
    model = AsyncMock(side_effect=["not json", whole])
    with patch.object(llm, "complete", model):
        got = await llm.complete_json("s", "u", default={"x": 1})
    results.append(("json: complete_json still asks again when it cannot read it", got == first and model.await_count == 2))
    model = AsyncMock(side_effect=["not json", "not json either"])
    with patch.object(llm, "complete", model):
        got = await llm.complete_json("s", "u", default={"x": 1})
    results.append(("json: complete_json still falls back to its default", got == {"x": 1} and model.await_count == 2))

    # The suggest route's draft, with only the model faked.
    def reply(text: str, tokens: int = 10) -> SimpleNamespace:
        return SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=tokens, completion_tokens=tokens),
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        )

    def client(*answers: object) -> tuple[SimpleNamespace, AsyncMock]:
        create = AsyncMock(side_effect=list(answers))
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), create

    async def draft(*answers: object) -> tuple[object, AsyncMock]:
        fake, create = client(*answers)
        with patch.object(suggest_llm, "_client", lambda: fake):
            try:
                return await suggest_llm.draft("s", "u", model="m", timeout=1), create
            except suggest_llm.SuggestLLMError as exc:
                return exc, create

    got, create = await draft(reply(whole + whole))
    results.append(("suggest: two objects are read with one call", got == (first, {"prompt_tokens": 10, "completion_tokens": 10}) and create.await_count == 1))
    got, create = await draft(reply("not json"), reply(whole, 20))
    answered = isinstance(got, tuple)
    results.append(("suggest: an unreadable reply is asked for once more", create.await_count == 2 and answered and got[0] == first))
    results.append(("suggest: both calls are in the usage", answered and got[1] == {"prompt_tokens": 30, "completion_tokens": 30}))
    got, create = await draft(reply("not json"), reply("still not"), reply(whole))
    results.append(("suggest: never a third call", create.await_count == 2 and isinstance(got, suggest_llm.SuggestLLMError)))
    got, create = await draft(TimeoutError("slow"), reply(whole))
    results.append(("suggest: a failed call is not retried", create.await_count == 1 and isinstance(got, suggest_llm.SuggestLLMError)))
    got, create = await draft(reply("not json"), TimeoutError("slow"))
    results.append(("suggest: a failed retry is an error, not a hang", create.await_count == 2 and isinstance(got, suggest_llm.SuggestLLMError)))
    with patch.object(suggest_llm, "RETRY_UNPARSABLE", False):
        got, create = await draft(reply("not json"), reply(whole))
    results.append(("suggest: switch off, no second call", create.await_count == 1 and isinstance(got, suggest_llm.SuggestLLMError)))
    results.append(("suggest: the switch is on", suggest_llm.RETRY_UNPARSABLE is True))

    # It is client conversation content, so the log gets lengths only.
    lines: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: lines.append(record.getMessage())  # type: ignore[method-assign]
    suggest_llm.logger.addHandler(handler)
    try:
        await draft(reply("SECRET-CLIENT-WORDS"), reply("SECRET-CLIENT-WORDS"))
    finally:
        suggest_llm.logger.removeHandler(handler)
    results.append(("suggest: the log holds no model output", bool(lines) and not any("SECRET" in line for line in lines)))
    return results


async def _replaced_thread_checks() -> list[tuple[str, bool]]:
    """Recording only: the real get_or_create and back_to_bot over a fake table."""
    import logging
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.config import settings
    from app.services import conversation as conv
    from app.services import handover

    results: list[tuple[str, bool]] = []

    class Db:
        """Writes in the order they happen. Anything to the record table can be made to fail."""

        def __init__(self, fail: bool = False) -> None:
            self.fail, self.log = fail, []

        def table(self, name: str) -> SimpleNamespace:
            return SimpleNamespace(
                upsert=lambda row, **kw: ("record", name, row, kw),
                insert=lambda row: ("insert", name, row, {}),
            )

        async def execute(self, query: tuple) -> SimpleNamespace:
            if self.fail:
                raise RuntimeError("relation does not exist: SECRET-DETAIL")
            self.log.append(query)
            return SimpleNamespace(data=[])

        async def insert(self, table: str, row: dict) -> dict:
            self.log.append(("insert", table, row, {}))
            return {**row, "id": 77}

        async def update(self, table: str, row: dict, **where: object) -> list[dict]:
            self.log.append(("update", table, row, where))
            return [{**row, **where}]

    def row(**fields: object) -> dict:
        return {"id": 5, "status": "open", "bot_status": conv.BOT_ACTIVE, "langgraph_thread_id": "conv-5-old", **fields}

    async def open_it(existing: dict | None, *, on: bool = True, fail: bool = False, engage: bool = True) -> Db:
        fake = Db(fail)
        with patch.object(conv, "db", fake), patch.object(settings, "record_replaced_threads", on), \
                patch.object(conv, "get_by_phone", AsyncMock(return_value=existing)):
            await conv.get_or_create("+6590000000", "Tan", engage=engage)
        return fake

    def kinds(fake: Db) -> list[str]:
        return [entry[0] for entry in fake.log]

    def new_id_written(fake: Db) -> bool:
        return any(e[0] == "update" and str(e[2].get("langgraph_thread_id", "")).startswith("conv-5-") for e in fake.log)

    results.append(("threads: the switch is off by default", type(settings).model_fields["record_replaced_threads"].default is False))

    fake = await open_it(row(status="resolved"))
    entry = next((e for e in fake.log if e[0] == "record"), None)
    results.append(("threads: a reopened conversation records its old thread", bool(entry) and entry[1] == "cb_replaced_threads"))
    results.append(("threads: ...before the new id is written", kinds(fake) == ["record", "update"]))
    results.append(("threads: ...with the old id, not the new one", bool(entry) and entry[2]["thread_id"] == "conv-5-old"))
    results.append(("threads: ...its conversation and why", bool(entry) and entry[2]["conversation_id"] == 5 and entry[2]["reason"] == "conversation_reopened"))
    results.append(("threads: ...and a repeat changes nothing", bool(entry) and entry[3] == {"on_conflict": "thread_id", "ignore_duplicates": True}))
    results.append(("threads: the new id is still written", new_id_written(fake)))

    fake = await open_it(row(bot_status=conv.BOT_NONE))
    entry = next((e for e in fake.log if e[0] == "record"), None)
    results.append(("threads: a claimed conversation says so", bool(entry) and entry[2]["reason"] == "conversation_claimed"))

    fake = await open_it(row(status="resolved"), on=False)
    results.append(("threads: switch off, nothing recorded, thread still replaced", kinds(fake) == ["update"] and new_id_written(fake)))
    fake = await open_it(row(status="resolved", langgraph_thread_id=None))
    results.append(("threads: no old id, nothing to record", kinds(fake) == ["update"] and new_id_written(fake)))
    fake = await open_it(row())
    results.append(("threads: a thread kept is not recorded", "record" not in kinds(fake) and not new_id_written(fake)))
    fake = await open_it(row(bot_status=conv.HUMAN_ACTIVE, status="resolved"))
    results.append(("threads: a human's thread is not replaced or recorded", "record" not in kinds(fake)))
    fake = await open_it(row(status="resolved"), engage=False)
    results.append(("threads: not engaging replaces and records nothing", "record" not in kinds(fake)))
    fake = await open_it(None)
    results.append(("threads: a brand-new conversation records nothing", "record" not in kinds(fake)))

    # A failed record must cost the client nothing.
    lines: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: lines.append(f"{record.levelname} {record.getMessage()}")  # type: ignore[method-assign]
    conv.logger.addHandler(handler)
    try:
        fake = await open_it(row(status="resolved"), fail=True)
    finally:
        conv.logger.removeHandler(handler)
    results.append(("threads: a failed record still replaces the thread", new_id_written(fake)))
    results.append(("threads: ...and is logged as an error", any(line.startswith("ERROR") for line in lines)))
    results.append(("threads: ...without the database's own words", not any("SECRET" in line for line in lines)))

    async def hand_back(on: bool = True, fail: bool = False) -> Db:
        fake = Db(fail)
        with patch.object(conv, "db", fake), patch.object(settings, "record_replaced_threads", on), \
                patch.object(handover, "_log", AsyncMock()):
            await handover.back_to_bot(row(), reason="bot_error")
        return fake

    fake = await hand_back()
    entry = next((e for e in fake.log if e[0] == "record"), None)
    results.append(("threads: back_to_bot records before it writes", kinds(fake) == ["record", "update"]))
    results.append(("threads: ...the old id and the handover's reason", bool(entry) and entry[2]["thread_id"] == "conv-5-old" and entry[2]["reason"] == "bot_error"))
    fake = await hand_back(on=False)
    results.append(("threads: back_to_bot with the switch off", kinds(fake) == ["update"]))
    fake = await hand_back(fail=True)
    results.append(("threads: back_to_bot survives a failed record", new_id_written(fake)))

    # The two paths that keep the thread record nothing.
    for name, call in (
        ("resume_from_pause", lambda: handover.resume_from_pause(row())),
        ("undo_agent_takeover", lambda: handover.undo_agent_takeover(5, "broadcast")),
    ):
        fake = Db()
        with patch.object(conv, "db", fake), patch.object(settings, "record_replaced_threads", True), \
                patch.object(handover, "_log", AsyncMock()):
            await call()
        results.append((f"threads: {name} keeps the thread and records nothing", "record" not in kinds(fake)))

    # Recording only: nothing here may delete a checkpoint.
    sql = (ROOT / "scripts" / "sql" / "replaced_threads_001_create.sql").read_text(encoding="utf-8").lower()
    sql = re.sub(r"--.*", "", sql)  # the statements, not the notes about them
    backfill = (ROOT / "scripts" / "backfill_replaced_threads.py").read_text(encoding="utf-8").lower()
    code = (ROOT / "app" / "services" / "conversation.py").read_text(encoding="utf-8").lower()
    results.append(("threads: the migration deletes nothing", "delete " not in sql and "cb_checkpoint" not in sql))
    results.append(("threads: the migration has no foreign key to the portal", "references" not in sql))
    results.append(("threads: the migration turns RLS on", "enable row level security" in sql))
    results.append(("threads: the rollback exists", (ROOT / "scripts" / "sql" / "rollback" / "rollback_replaced_threads_001_create.sql").is_file()))
    results.append(("threads: the backfill deletes nothing", "delete from" not in backfill and ".delete(" not in backfill))
    results.append(("threads: the backfill writes only on --apply, with a project ref", "require_ref(args.expect_ref)" in backfill and "if args.apply:" in backfill and "set transaction read only" in backfill))
    results.append(("threads: the app deletes no checkpoint", "cb_checkpoint" not in code))

    # The reset deletes the old thread's rows, so its record goes with them. The real
    # function, with only the connection faked.
    import runpy

    import psycopg

    wipe = runpy.run_path(str(ROOT / "scripts" / "reset_conversation.py"), run_name="reset")["_wipe_checkpoints"]

    async def reset_statements(table_exists: bool) -> list[tuple[str, tuple]]:
        seen: list[tuple[str, tuple]] = []

        class Cursor:
            rowcount = 0

            def __enter__(self) -> "Cursor":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def execute(self, statement: str, params: tuple = ()) -> None:
                seen.append((statement, params))

            def fetchone(self) -> tuple:
                return ("cb_replaced_threads" if table_exists else None,)

        class Connection:
            def __enter__(self) -> "Connection":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def cursor(self) -> Cursor:
                return Cursor()

        with patch.object(psycopg, "connect", lambda *a, **k: Connection()), \
                patch.object(settings, "supabase_db_url", "postgresql://fake"), \
                patch("builtins.print"):
            wipe("conv-5-old")
        return seen

    seen = await reset_statements(True)
    record = [s for s in seen if "cb_replaced_threads" in s[0] and s[0].startswith("DELETE")]
    results.append(("threads: a reset deletes that thread's record, by its id", record == [("DELETE FROM cb_replaced_threads WHERE thread_id = %s", ("conv-5-old",))]))
    results.append(("threads: ...after its checkpoint rows", seen.index(record[0]) > 2 if record else False))
    seen = await reset_statements(False)
    results.append(("threads: a reset works before the table is applied", not any(s[0].startswith("DELETE FROM cb_replaced") for s in seen) and len(seen) == 4))
    return results


async def checks() -> list[tuple[str, bool]]:
    return [
        *await _json_checks(),
        *await _replaced_thread_checks(),
        *await _checkpointer_checks(),
        *await _health_checks(),
        *await _forwarded_checks(),
        *await _shutdown_checks(),
        *await _recovery_checks(),
    ]


async def main() -> int:
    failures = 0
    for label, ok in await checks():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failures += not ok
    print("\nALL PASS" if not failures else f"\n{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(no_network.exit_code(asyncio.run(main())))
