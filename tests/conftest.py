"""Shared fixtures: loopback UDP listener, loopback HTTP controller, strip factory, and the leaked-task check."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from pyddp.const import DESTINATION_CONFIG, DESTINATION_STATUS, FLAG_PUSH, FLAG_REPLY, FLAG_VERSION_1, HEADER_SIZE
from pyddp.protocol import DDPHeader
from pyddp.sender import DDPSender
from pyddp.strip import Strip

# Captured verbatim from a Minleon NDB-Pro (firmware 2.0) at /api/config and /api/status.
NDB_CONFIG_JSON = (
    b'{"config":{"ip":"10.0.0.100","nm":"255.255.255.0","gw":"10.0.0.1","wcm":"client","ssid":"SkyDevices",'
    b'"proto":0,"univ":0,"chip":17,"t0h":400,"t1h":800,"tbit":1250,"tres":250,"conv":13,'
    b'"ports":[{"p":0,"ts":0,"l":80,"ss":1,"rev":1},{"p":1,"ts":0,"l":42,"ss":241,"rev":0},'
    b'{"p":2,"ts":0,"l":0,"ss":367,"rev":0},{"p":3,"ts":0,"l":0,"ss":368,"rev":0},'
    b'{"p":4,"ts":0,"l":0,"ss":369,"rev":0},{"p":5,"ts":0,"l":0,"ss":370,"rev":0},'
    b'{"p":6,"ts":0,"l":0,"ss":371,"rev":0},{"p":7,"ts":0,"l":0,"ss":372,"rev":0}]}}'
)
NDB_STATUS_JSON = (
    b'{"status":{"man":"3WL","mod":"NDBPRO","push":true,"mac":"5c:01:3b:39:1b:93","ver":"2.0","name":"ndbpro",'
    b'"fuses":"ff","vin":"24.27","amp":"1.51","links":"04"}}'
)

# Pages captured verbatim from the same NDB-Pro's web UI; the control endpoint's reply body is not recorded.
NDB_PAGES_DIR = Path(__file__).parent / "fixtures" / "ndbpro"
NDB_PAGES: dict[str, bytes] = {
    f"/{name}": (NDB_PAGES_DIR / name).read_bytes() for name in ("pfx2.html", "pcolors.html", "pshows.html")
}

Responder = Callable[[bytes, tuple[str | Any, int]], list[bytes]]


def reply_packet(destination: int, payload: bytes, *, push: bool = True, offset: int = 0) -> bytes:
    """A controller reply: version 1, REPLY flag, push on the last packet."""
    flags = FLAG_VERSION_1 | FLAG_REPLY | (FLAG_PUSH if push else 0)
    return bytes([flags, 0, 0, destination]) + offset.to_bytes(4, "big") + len(payload).to_bytes(2, "big") + payload


def ndb_responder(packet: bytes, addr: tuple[str | Any, int]) -> list[bytes]:
    """Answer config and status queries with the captured NDB-Pro documents; ignore everything else."""
    header = DDPHeader.decode(packet)
    documents = {DESTINATION_CONFIG: NDB_CONFIG_JSON, DESTINATION_STATUS: NDB_STATUS_JSON}
    if not header.query or header.destination not in documents:
        return []
    return [reply_packet(header.destination, documents[header.destination])]


class UDPListener(asyncio.DatagramProtocol):
    """Collects every datagram received on a loopback socket, in arrival order; ``responder`` may answer."""

    host: str = "127.0.0.1"

    def __init__(self, responder: Responder | None = None) -> None:
        self.port: int = 0
        self.packets: list[bytes] = []
        self.transport: asyncio.DatagramTransport | None = None
        self.responder = responder

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport
        self.port = transport.get_extra_info("sockname")[1]

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        self.packets.append(data)
        if self.responder is not None and self.transport is not None:
            for reply in self.responder(data, addr):
                self.transport.sendto(reply, addr)

    async def wait_for(self, count: int, timeout: float = 1.0) -> list[bytes]:
        """Poll until at least ``count`` packets have arrived; fail the test on timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.packets) < count:
            if loop.time() >= deadline:
                pytest.fail(f"expected {count} packets within {timeout}s, got {len(self.packets)}")
            await asyncio.sleep(0.005)
        return list(self.packets)

    def headers(self) -> list[DDPHeader]:
        return [DDPHeader.decode(p) for p in self.packets]

    def payloads(self) -> list[bytes]:
        return [p[HEADER_SIZE:] for p in self.packets]

    def clear(self) -> None:
        self.packets.clear()


@pytest.fixture
async def udp_listener() -> AsyncIterator[UDPListener]:
    loop = asyncio.get_running_loop()
    transport, listener = await loop.create_datagram_endpoint(UDPListener, local_addr=(UDPListener.host, 0))
    try:
        yield listener
    finally:
        transport.close()


class HTTPController:
    """Loopback HTTP server answering any method for each path in ``routes`` (query string ignored for routing).

    ``None`` or a missing path is a 404. ``requests`` records each request target; ``calls`` records
    ``(method, target, body)``, with the body read by ``Content-Length``.

    Paths in ``redirects`` answer 302 to the given location. ``slow`` maps a path to a misbehaving mode:
    ``"body"`` sends headers, then one body byte every 0.1 s; ``"head"`` sends the whole response one byte every
    0.1 s; both stop when the client hangs up. ``"chunked"`` sends the route's body chunked (``"badchunk"``
    with a malformed size), and
    ``"nolength"`` sends the route's body without a Content-Length header.
    """

    host: str = "127.0.0.1"

    def __init__(self) -> None:
        self.port: int = 0
        self.routes: dict[str, bytes | None] = {
            "/api/config": NDB_CONFIG_JSON,
            "/api/status": NDB_STATUS_JSON,
            "/api/control": b"",
            **NDB_PAGES,
        }
        self.requests: list[str] = []
        self.calls: list[tuple[str, str, bytes]] = []
        self.redirects: dict[str, str] = {}
        self.slow: dict[str, str] = {}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            method, target = (part.decode() for part in head.split(b" ", 2)[:2])
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value)
            body_in = await reader.readexactly(length) if length else b""
            self.requests.append(target)
            self.calls.append((method, target, body_in))
            path = target.partition("?")[0]
            if path in self.redirects:
                writer.write(
                    f"HTTP/1.1 302 Found\r\nLocation: {self.redirects[path]}\r\nContent-Length: 0\r\n"
                    "Connection: close\r\n\r\n".encode()
                )
                await writer.drain()
                return
            body = self.routes.get(path)
            status = "200 OK" if body is not None else "404 Not Found"
            body = body if body is not None else b"not found"
            response = (
                f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n".encode() + body
            )
            mode = self.slow.get(path)
            if mode == "head":
                await self._drip(reader, writer, response)
            elif mode == "body":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n")
                await self._drip(reader, writer, b" " * 200)
            elif mode == "nolength":
                writer.write(b"HTTP/1.0 200 OK\r\n\r\n" + body)
                await writer.drain()
            elif mode in ("chunked", "badchunk"):
                # What the NDB-Pro firmware sends: HTTP/1.1, chunked, lower-case hex sizes, even to HTTP/1.0 requests.
                chunks = b"".join(
                    b"%x\r\n%s\r\n" % (len(body[i : i + 1000]), body[i : i + 1000]) for i in range(0, len(body), 1000)
                )
                if mode == "badchunk":
                    chunks = b"zz\r\n" + chunks
                writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + chunks + b"0\r\n\r\n")
                await writer.drain()
            else:
                writer.write(response)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    async def _drip(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, data: bytes) -> None:
        try:
            for i in range(len(data)):
                writer.write(data[i : i + 1])
                await writer.drain()
                try:
                    if not await asyncio.wait_for(reader.read(1), 0.1):
                        return  # client hung up
                except TimeoutError:
                    continue
        except ConnectionError:
            return


@pytest.fixture
async def http_controller() -> AsyncIterator[HTTPController]:
    controller = HTTPController()
    server = await asyncio.start_server(controller.handle, HTTPController.host, 0)
    controller.port = server.sockets[0].getsockname()[1]
    try:
        yield controller
    finally:
        server.close()
        await server.wait_closed()


@pytest.fixture
def make_strip(udp_listener: UDPListener) -> Callable[..., Strip]:
    """Factory for a Strip aimed at the listener: RGB, 10 pixels, cap 1.0, gamma off; kwargs override."""

    def factory(**overrides: Any) -> Strip:
        kwargs: dict[str, Any] = {
            "host": udp_listener.host,
            "port": udp_listener.port,
            "pixel_count": 10,
            "brightness_cap": 1.0,
            "gamma": None,
        }
        kwargs.update(overrides)
        return Strip(**kwargs)

    return factory


@pytest.fixture
async def sender(make_strip: Callable[..., Strip]) -> AsyncIterator[DDPSender]:
    """An opened DDPSender on the default ``make_strip()`` strip; closed at teardown."""
    ddp = DDPSender(make_strip())
    await ddp.open()
    try:
        yield ddp
    finally:
        await ddp.close()


@pytest.fixture
async def _task_snapshot() -> AsyncIterator[None]:
    current = asyncio.current_task()
    before = asyncio.all_tasks() - {current}
    yield
    after = asyncio.all_tasks() - {asyncio.current_task(), current}
    leaked = after - before
    assert not leaked, f"test leaked asyncio tasks: {leaked}"


@pytest.fixture(autouse=True)
def no_leaked_tasks(request: pytest.FixtureRequest) -> Iterator[None]:
    """For async tests, assert no asyncio task outlives the test and its fixtures."""
    if inspect.iscoroutinefunction(request.function):
        request.getfixturevalue("_task_snapshot")
    yield
