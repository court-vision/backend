"""
The container's launcher: one server heard on IPv4 (Railway's public proxy) and
on IPv6 (its private network), and still starting where there is no IPv6.
"""

import http.client
import socket
import threading
import time

import pytest
import uvicorn

import serve

pytestmark = pytest.mark.unit


def _ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


needs_ipv6 = pytest.mark.skipif(not _ipv6_loopback(), reason="this host has no IPv6 loopback")


@pytest.fixture
def sockets():
    opened = serve.open_sockets(0)
    yield opened
    for sock in opened:
        sock.close()


async def _app(scope, receive, send):
    assert scope["type"] == "http"
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"pong"})


def _get(host: str, port: int) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", "/ping")
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


@needs_ipv6
def test_both_families_listen_on_one_port(sockets):
    assert [s.family for s in sockets] == [socket.AF_INET, socket.AF_INET6]
    v4, v6 = sockets
    assert v4.getsockname()[0] == "0.0.0.0"
    assert v6.getsockname()[0] == "::"
    assert v4.getsockname()[1] == v6.getsockname()[1] != 0


@needs_ipv6
def test_the_ipv6_listener_leaves_ipv4_to_the_other_socket(sockets):
    # Dual-stack would make the second bind fail on Linux, where the container runs.
    assert sockets[1].getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 1


@needs_ipv6
def test_one_server_answers_on_both(sockets):
    server = uvicorn.Server(uvicorn.Config(_app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        port = sockets[0].getsockname()[1]
        assert _get("127.0.0.1", port) == (200, b"pong")
        assert _get("::1", port) == (200, b"pong")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_a_host_without_ipv6_still_listens_on_ipv4(monkeypatch, caplog):
    real = serve._bound

    def no_ipv6(family, host, port):
        if family == socket.AF_INET6:
            raise OSError(97, "Address family not supported by protocol")
        return real(family, host, port)

    monkeypatch.setattr(serve, "_bound", no_ipv6)
    # uvicorn's logging config stops its loggers propagating to the root one
    # caplog listens on, so listen on the launcher's logger itself.
    serve.log.addHandler(caplog.handler)
    try:
        opened = serve.open_sockets(0)
    finally:
        serve.log.removeHandler(caplog.handler)
    try:
        assert [s.family for s in opened] == [socket.AF_INET]
        assert "No IPv6 listener" in caplog.text
    finally:
        for sock in opened:
            sock.close()


def test_a_failed_bind_does_not_leak_its_socket(monkeypatch):
    closed = []

    class Refusing(socket.socket):
        def bind(self, address):
            raise OSError(98, "Address already in use")

        def close(self):
            closed.append(self)
            super().close()

    monkeypatch.setattr(serve.socket, "socket", Refusing)
    with pytest.raises(OSError):
        serve._bound(socket.AF_INET, "0.0.0.0", 0)
    assert len(closed) == 1


class _Recorder:
    """Stands in for uvicorn.Server: records what main() built and ran."""

    instances: list["_Recorder"] = []
    starts = True

    def __init__(self, config):
        self.config = config
        self.started = False
        self.sockets = None
        _Recorder.instances.append(self)

    def run(self, sockets=None):
        self.sockets = sockets
        self.started = _Recorder.starts


@pytest.fixture
def recorder(monkeypatch):
    _Recorder.instances = []
    _Recorder.starts = True
    opened: list[socket.socket] = []
    real = serve.open_sockets

    def on_a_free_port(port=serve.PORT):
        opened.extend(real(0))
        return list(opened)

    monkeypatch.setattr(serve, "open_sockets", on_a_free_port)
    monkeypatch.setattr(serve.uvicorn, "Server", _Recorder)
    yield _Recorder
    for sock in opened:
        sock.close()


def test_main_serves_the_app_on_the_open_sockets(recorder):
    serve.main()
    (server,) = recorder.instances
    assert server.config.app == "main:app"
    assert server.config.access_log is False
    assert server.sockets and server.sockets[0].family == socket.AF_INET


def test_main_exits_like_uvicorn_when_the_app_never_started(recorder):
    recorder.starts = False
    with pytest.raises(SystemExit) as exit_info:
        serve.main()
    assert exit_info.value.code == serve.STARTUP_FAILURE == 3
