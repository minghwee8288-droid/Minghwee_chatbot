"""Import this FIRST in an offline suite: it cannot reach production, whatever the test does.

A check that forgets one stub otherwise talks to whatever .env names. That happened: on
2026-11-04 a first draft of a selfcheck_golive case wrote a real high-priority ticket,
CB-2026-0160, onto a client's conversation, because the fixture's conversation id exists on
production. A docstring saying "nothing touches the network" did not stop it.

Three locks, because each one misses something the others catch:

* No credentials. APP_ENV_FILE names a file that does not exist and every secret is blanked
  before app.config is imported, so the process holds no URL, key or token. This is the one
  that also covers libpq, which opens its own socket in C, and any child process.
* Python sockets. Anything but loopback is refused and noted. Loopback stays open: asyncio
  on Windows builds its self-pipe with a local socket pair.
* psycopg.connect and AsyncConnection.connect are refused outright.

A suite ends with `no_network.report()`, which prints what was tried and returns False if
anything was. A refused attempt is a test that lost a stub, so it fails the run even when
the code it was testing swallowed the error.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from typing import Any

if "app.config" in sys.modules:
    raise RuntimeError("no_network must be imported before app.config, or the credentials are already loaded")

_SECRETS = (
    "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_DB_URL",
    "WHAPI_API_TOKEN", "WHAPI_WEBHOOK_SECRET", "WHAPI_SENDER_PHONE",
    "OPENROUTER_API_KEY", "OPENAI_API_KEY", "EMBEDDING_API_KEY", "TRANSCRIPTION_API_KEY",
    "SUGGEST_SECRET", "ADMIN_PREVIEW_SECRET", "HEALTH_DETAILS_KEY", "BOT_ALLOWED_NUMBERS",
    "INTERNAL_NUMBERS",
)
os.environ["APP_ENV_FILE"] = str(Path(__file__).with_name(".no-such-env-file"))
for _name in _SECRETS:
    os.environ[_name] = ""

ATTEMPTS: list[str] = []
_LOCAL = {"127.0.0.1", "::1", "localhost"}
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _refuse(host: Any) -> None:
    ATTEMPTS.append(str(host))


def _guard(address: Any) -> None:
    host = address[0] if isinstance(address, tuple) else str(address)
    if str(host) not in _LOCAL:
        _refuse(host)
        raise ConnectionRefusedError(f"no_network: {host}")


def _connect(self, address):  # noqa: ANN001
    _guard(address)
    return _real_connect(self, address)


def _connect_ex(self, address):  # noqa: ANN001
    _guard(address)
    return _real_connect_ex(self, address)


def _getaddrinfo(host, *a, **kw):  # noqa: ANN001
    name = host.decode() if isinstance(host, bytes) else host
    if name is not None and str(name) not in _LOCAL:
        _refuse(name)
        raise socket.gaierror(f"no_network: {name}")
    return _real_getaddrinfo(host, *a, **kw)


socket.socket.connect = _connect  # type: ignore[method-assign]
socket.socket.connect_ex = _connect_ex  # type: ignore[method-assign]
socket.getaddrinfo = _getaddrinfo  # type: ignore[assignment]

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None  # type: ignore[assignment]

if psycopg is not None:
    def _no_pg(*a, **kw):  # noqa: ANN001, ANN002, ANN003
        _refuse("postgres")
        raise psycopg.OperationalError("no_network: postgres")

    async def _no_pg_async(*a, **kw):  # noqa: ANN001, ANN002, ANN003
        _no_pg()

    psycopg.connect = _no_pg  # type: ignore[assignment]
    psycopg.AsyncConnection.connect = _no_pg_async  # type: ignore[method-assign,assignment]


def exit_code(code: int | None) -> int:
    """The suite's own result, failed as well if anything tried to leave."""
    clean = report()
    return (code or 0) or (0 if clean else 1)


def report() -> bool:
    """True if nothing tried to leave. Hosts only, no URL, so nothing sensitive is printed."""
    if ATTEMPTS:
        hosts = sorted(set(ATTEMPTS))
        print(f"FAIL  no_network: {len(ATTEMPTS)} attempts to reach {hosts}: a test lost a stub")
        return False
    print("PASS  no_network: nothing tried to leave this machine")
    return True
