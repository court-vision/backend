"""
Start the API on both of Railway's networks.

Public traffic reaches this container from Railway's proxy over IPv4. Other
services reach it over the private network (`backend.railway.internal`), and
in this environment that network is IPv6 only. `uvicorn --host 0.0.0.0`
listens on IPv4 alone, so every private call was refused: the data-platform's
lineup evaluation and the projections editor's ranks, the two jobs routes it
calls with the pipeline token.

uvicorn's command line binds one address. Handed open sockets it serves all of
them, so this opens two on the same port and runs the same server on both.

The IPv4 socket is the listener the container has always had. The IPv6 one is
additional: where it cannot be opened (a host with no IPv6) the API starts
without it and says so, rather than not starting.

Local development is unchanged: `uvicorn main:app --reload --port 8000`.
"""

from __future__ import annotations

import errno
import logging
import socket
import sys

import uvicorn

PORT = 8080
STARTUP_FAILURE = 3  # uvicorn's own exit code when the application fails to start

# What binding [::] raises on a host that has no IPv6.
NO_IPV6 = frozenset({errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.EPROTONOSUPPORT})

log = logging.getLogger("uvicorn.error")


def _bound(family: socket.AddressFamily, host: str, port: int) -> socket.socket:
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            # IPv6 only: without this the wildcard would also claim IPv4 on
            # Linux, and it could not share the port with the socket above.
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    return sock


def open_sockets(port: int = PORT) -> list[socket.socket]:
    """The IPv4 listener, and the IPv6 one beside it on the same port when the host has IPv6."""
    sockets = [_bound(socket.AF_INET, "0.0.0.0", port)]
    port = sockets[0].getsockname()[1]  # the port the system chose, when asked for 0
    try:
        sockets.append(_bound(socket.AF_INET6, "::", port))
    except OSError as exc:
        # The API starts either way. The IPv4 listener serves every user, and
        # refusing to start over the private one would trade a refused jobs
        # call for an outage. A host without IPv6 is expected somewhere;
        # anything else is not, and is logged as an error.
        level = logging.WARNING if exc.errno in NO_IPV6 else logging.ERROR
        log.log(level, "No IPv6 listener (%s): calls over the private network will be refused", exc)
    return sockets


def main() -> None:
    # Config first: it sets up the logging the lines below are written with.
    config = uvicorn.Config("main:app", access_log=False)
    server = uvicorn.Server(config)
    sockets = open_sockets()
    for sock in sockets:
        host, port = sock.getsockname()[:2]
        log.info("Uvicorn running on http://%s:%d", f"[{host}]" if ":" in host else host, port)
    try:
        server.run(sockets=sockets)
    except KeyboardInterrupt:
        pass
    if not server.started:
        sys.exit(STARTUP_FAILURE)


if __name__ == "__main__":
    main()
