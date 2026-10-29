"""Shared fixtures: loopback UDP listener, strip factory, and the leaked-task check."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest

from pyddp.const import HEADER_SIZE
from pyddp.protocol import DDPHeader
from pyddp.sender import DDPSender
from pyddp.strip import Strip


class UDPListener(asyncio.DatagramProtocol):
    """Collects every datagram received on a loopback socket, in arrival order."""

    host: str = "127.0.0.1"

    def __init__(self) -> None:
        self.port: int = 0
        self.packets: list[bytes] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport
        self.port = transport.get_extra_info("sockname")[1]

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        self.packets.append(data)

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
