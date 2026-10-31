"""Shared fixtures: loopback UDP listener, strip factory, and the leaked-task check."""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest

from aioddp.const import DESTINATION_CONFIG, DESTINATION_STATUS, FLAG_PUSH, FLAG_REPLY, FLAG_VERSION_1, HEADER_SIZE
from aioddp.protocol import DDPHeader
from aioddp.sender import DDPSender
from aioddp.strip import Strip

# Config and status documents captured verbatim from a real controller (firmware 2.0).
CONFIG_JSON = (
    b'{"config":{"ip":"10.0.0.100","nm":"255.255.255.0","gw":"10.0.0.1","wcm":"client","ssid":"SkyDevices",'
    b'"proto":0,"univ":0,"chip":17,"t0h":400,"t1h":800,"tbit":1250,"tres":250,"conv":13,'
    b'"ports":[{"p":0,"ts":0,"l":80,"ss":1,"rev":1},{"p":1,"ts":0,"l":42,"ss":241,"rev":0},'
    b'{"p":2,"ts":0,"l":0,"ss":367,"rev":0},{"p":3,"ts":0,"l":0,"ss":368,"rev":0},'
    b'{"p":4,"ts":0,"l":0,"ss":369,"rev":0},{"p":5,"ts":0,"l":0,"ss":370,"rev":0},'
    b'{"p":6,"ts":0,"l":0,"ss":371,"rev":0},{"p":7,"ts":0,"l":0,"ss":372,"rev":0}]}}'
)
STATUS_JSON = (
    b'{"status":{"man":"3WL","mod":"NDBPRO","push":true,"mac":"5c:01:3b:39:1b:93","ver":"2.0","name":"ndbpro",'
    b'"fuses":"ff","vin":"24.27","amp":"1.51","links":"04"}}'
)

Responder = Callable[[bytes, tuple[str | Any, int]], list[bytes]]


def reply_packet(destination: int, payload: bytes, *, push: bool = True, offset: int = 0) -> bytes:
    """A controller reply: version 1, REPLY flag, push on the last packet."""
    flags = FLAG_VERSION_1 | FLAG_REPLY | (FLAG_PUSH if push else 0)
    return bytes([flags, 0, 0, destination]) + offset.to_bytes(4, "big") + len(payload).to_bytes(2, "big") + payload


def config_responder(packet: bytes, addr: tuple[str | Any, int]) -> list[bytes]:
    """Answer config and status queries with the captured documents; ignore everything else."""
    header = DDPHeader.decode(packet)
    documents = {DESTINATION_CONFIG: CONFIG_JSON, DESTINATION_STATUS: STATUS_JSON}
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


def pytest_configure(config: pytest.Config) -> None:
    """Shared CI runners are noisy: triple the render budget there unless the factor is set explicitly."""
    if os.environ.get("CI"):
        os.environ.setdefault("AIODDP_BENCH_FACTOR", "3")
