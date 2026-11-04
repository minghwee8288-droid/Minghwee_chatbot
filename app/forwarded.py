"""Who may tell the app which address a request came from.

uvicorn believes X-Forwarded-For and X-Forwarded-Proto only from the addresses
named here. Standard library only: run.py reads this before the app is imported.

nginx runs on the host and reaches the container through the published port, so
inside the container it arrives from Docker's gateway. That address is the
container's default route, so it is read from there rather than configured: it
differs per server and can change when the network is recreated. Nothing here
can tell nginx from anything else on the host, but the port is bound to
127.0.0.1, so nothing outside the server arrives from that address.

If this ever names the wrong address, every request reads as coming from the
gateway. Nothing in the app depends on the client's address except
health._may_see_warnings, which then says no.
"""

from __future__ import annotations

import os
import socket
import struct

# 2026-11-04: was forwarded_allow_ips="*", so any caller could choose the address
# the app saw (and read loopback, with the left-most value winning). False = "*".
NGINX_ONLY_FORWARDED = True

ROUTE_FILE = "/proc/net/route"
_NO_GATEWAY = "127.0.0.1"  # not in a container: uvicorn's own default


def default_gateway(route_file: str = ROUTE_FILE) -> str | None:
    """The default route's gateway, or None if there is no such file or route."""
    try:
        with open(route_file, encoding="ascii") as routes:
            rows = routes.read().splitlines()[1:]
        for row in rows:
            columns = row.split()
            if columns[1] == "00000000" and columns[2] != "00000000":
                return socket.inet_ntoa(struct.pack("<L", int(columns[2], 16)))
    except (OSError, ValueError, IndexError, struct.error):
        pass
    return None


def allowed_ips() -> str:
    """The value for uvicorn's forwarded_allow_ips.

    FORWARDED_ALLOW_IPS in the environment overrides the gateway, for a server
    where nginx arrives some other way. It cannot widen this back to everyone:
    that takes the switch.
    """
    if not NGINX_ONLY_FORWARDED:
        return "*"
    explicit = os.environ.get("FORWARDED_ALLOW_IPS", "").strip()
    if explicit and "*" not in explicit:
        return explicit
    return default_gateway() or _NO_GATEWAY
