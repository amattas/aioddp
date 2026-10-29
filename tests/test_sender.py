"""DDPSender: wire bytes, segments, expansion, latch, keepalive, close, errors."""

from __future__ import annotations

import asyncio
import errno
import logging
import socket
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from pyddp.const import HEADER_SIZE, MAX_DATA_LEN, DataType
from pyddp.exceptions import ConfigError, DDPSendError
from pyddp.protocol import DDPHeader
from pyddp.sender import DDPSender
from pyddp.strip import Segment, Strip
from tests.conftest import UDPListener

MakeStrip = Callable[..., Strip]


def _header(*, flags: int, seq: int, data_type: int, offset: int, length: int, dest: int = 1) -> bytes:
    return bytes([flags, seq, data_type, dest]) + offset.to_bytes(4, "big") + length.to_bytes(2, "big")


async def test_fill_full_strip_sends_golden_capped_reordered_packet(
    udp_listener: UDPListener, make_strip: MakeStrip, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="pyddp.sender")
    strip = make_strip(color_order="GRB", brightness_cap=0.5)
    async with DDPSender(strip) as ddp:
        await ddp.fill(None, (200, 100, 50))
        (packet,) = await udp_listener.wait_for(1)
        stats = ddp.stats

    # cap 0.5 halves every channel, then GRB reorders: (200, 100, 50) -> (100, 50, 25) -> (50, 100, 25)
    payload = bytes([50, 100, 25]) * 10
    assert packet == _header(flags=0x41, seq=1, data_type=0x0B, offset=0, length=30) + payload
    assert (stats.packets_sent, stats.frames_sent, stats.bytes_sent) == (1, 1, HEADER_SIZE + 30)
    assert stats.last_frame == payload
    assert stats.last_send is not None
    (record,) = caplog.records  # DEBUG logs header fields and length only, never payload
    assert "seq=1 type=0x0b dest=1 offset=0 length=30 push=True" in record.getMessage()
    assert repr(payload) not in record.getMessage()


@pytest.mark.parametrize(("data_type", "offset"), [(DataType.RGB, 300), (DataType.RGBW, 400)])
async def test_fill_segment_lands_at_byte_offset_and_in_shadow(
    udp_listener: UDPListener, make_strip: MakeStrip, data_type: DataType, offset: int
) -> None:
    strip = make_strip(pixel_count=200, data_type=data_type, color_order=data_type.value[0])
    channels = data_type.channels
    async with DDPSender(strip) as ddp:
        await ddp.fill(Segment("s", 100, 5), (1, 2, 3))
        await udp_listener.wait_for(1)
        last_frame = ddp.stats.last_frame

    expected = bytes([1, 2, 3, 0][:channels]) * 5
    (header,) = udp_listener.headers()
    assert (header.offset, header.length, header.data_type) == (offset, len(expected), data_type.wire_byte)
    assert udp_listener.payloads() == [expected]
    assert last_frame == bytes(offset) + expected + bytes(strip.frame_size - offset - len(expected))


async def test_large_frame_chunks_with_push_on_last_and_consecutive_sequence(
    udp_listener: UDPListener, make_strip: MakeStrip
) -> None:
    strip = make_strip(pixel_count=1000)
    pixels = bytes(i % 256 for i in range(3000))
    async with DDPSender(strip) as ddp:
        await ddp.send_frame(pixels)
        await udp_listener.wait_for(3)
        stats = ddp.stats

    headers = udp_listener.headers()
    assert [(h.offset, h.length) for h in headers] == [(0, MAX_DATA_LEN), (MAX_DATA_LEN, MAX_DATA_LEN), (2880, 120)]
    assert [h.push for h in headers] == [False, False, True]
    assert [h.sequence for h in headers] == [1, 2, 3]
    assert b"".join(udp_listener.payloads()) == pixels
    assert (stats.packets_sent, stats.frames_sent) == (3, 1)


async def test_rgb_pixels_on_rgbw_strip_are_expanded(udp_listener: UDPListener, make_strip: MakeStrip) -> None:
    async with DDPSender(make_strip(pixel_count=4, data_type=DataType.RGBW, color_order="WRGB")) as ddp:
        await ddp.send_frame(bytes([10, 20, 30]) * 2, segment=Segment("s", 1, 2))
        await udp_listener.wait_for(1)

    (header,) = udp_listener.headers()
    assert (header.offset, header.length) == (4, 8)
    assert udp_listener.payloads() == [bytes([0, 10, 20, 30]) * 2]


@pytest.mark.parametrize(
    ("data_type", "length"),
    [(DataType.RGB, 29), (DataType.RGB, 40), (DataType.RGBW, 39), (DataType.RGBW, 20)],
)
async def test_wrong_pixel_length_raises_config_error_and_sends_nothing(
    udp_listener: UDPListener, make_strip: MakeStrip, data_type: DataType, length: int
) -> None:
    async with DDPSender(make_strip(data_type=data_type, color_order=data_type.value[0])) as ddp:
        with pytest.raises(ConfigError, match="pixel data length"):
            await ddp.send_frame(bytes(length))
        assert ddp.stats.packets_sent == 0
    await asyncio.sleep(0.02)
    assert udp_listener.packets == []


async def test_segment_past_strip_end_raises_config_error(sender: DDPSender) -> None:
    with pytest.raises(ConfigError, match="past the strip"):
        await sender.fill(Segment("s", 8, 5), (1, 1, 1))


async def test_push_false_segments_then_push_latches_once(udp_listener: UDPListener, sender: DDPSender) -> None:
    await sender.fill(Segment("a", 0, 5), (255, 0, 0), push=False)
    await sender.fill(Segment("b", 5, 5), (0, 0, 255), push=False)
    await sender.push()
    await udp_listener.wait_for(3)

    headers = udp_listener.headers()
    assert [h.push for h in headers] == [False, False, True]
    assert [(h.offset, h.length) for h in headers] == [(0, 15), (15, 15), (0, 0)]
    assert [h.sequence for h in headers] == [1, 2, 3]
    assert udp_listener.packets[2] == _header(flags=0x41, seq=3, data_type=0x0B, offset=0, length=0)
    assert sender.stats.last_frame == bytes([255, 0, 0]) * 5 + bytes([0, 0, 255]) * 5
    assert (sender.stats.packets_sent, sender.stats.frames_sent) == (3, 2)


async def test_off_writes_black_to_wire_and_shadow(udp_listener: UDPListener, sender: DDPSender) -> None:
    await sender.fill(None, (10, 10, 10), brightness=0.5)
    await sender.off(Segment("s", 2, 3))
    await udp_listener.wait_for(2)

    off_header = udp_listener.headers()[1]
    assert (off_header.offset, off_header.length, off_header.push) == (6, 9, True)
    assert udp_listener.payloads()[1] == bytes(9)
    assert sender.stats.last_frame == bytes([5, 5, 5]) * 2 + bytes(9) + bytes([5, 5, 5]) * 5


async def test_keepalive_resends_shadow_only_after_idle(udp_listener: UDPListener, make_strip: MakeStrip) -> None:
    async with DDPSender(make_strip(), keepalive_interval=0.5) as ddp:
        await asyncio.sleep(0.6)  # one keepalive tick with nothing sent yet: stays silent
        assert udp_listener.packets == []

        for _ in range(6):  # active for 0.6 s across a tick: no keepalive
            await ddp.fill(None, (7, 8, 9))
            await asyncio.sleep(0.1)
        assert len(udp_listener.packets) == 6
        assert ddp.stats.keepalives_sent == 0

        packets = await udp_listener.wait_for(7, timeout=1.5)  # idle: resent within two ticks
        await asyncio.sleep(0.05)
        stats = ddp.stats

    assert len(udp_listener.packets) == 7
    keepalive = DDPHeader.decode(packets[6])
    assert (keepalive.offset, keepalive.length, keepalive.push, keepalive.sequence) == (0, 30, True, 7)
    assert packets[6][HEADER_SIZE:] == stats.last_frame == bytes([7, 8, 9]) * 10
    assert (stats.keepalives_sent, stats.frames_sent, stats.packets_sent) == (1, 6, 7)


@pytest.mark.parametrize("interval", [0.0, 0.49, -1.0])
def test_keepalive_interval_below_floor_raises_config_error(make_strip: MakeStrip, interval: float) -> None:
    with pytest.raises(ConfigError, match="keepalive_interval"):
        DDPSender(make_strip(), keepalive_interval=interval)


async def test_close_cancels_keepalive_is_idempotent_and_blocks_sends(make_strip: MakeStrip) -> None:
    ddp = DDPSender(make_strip(), keepalive_interval=0.5)
    await ddp.open()
    await ddp.open()  # no-op: still one keepalive task
    assert [t.get_name() for t in asyncio.all_tasks()].count("pyddp-keepalive-127.0.0.1") == 1

    await ddp.close()
    assert not ddp.is_open
    assert all(not t.get_name().startswith("pyddp-keepalive") for t in asyncio.all_tasks())
    await ddp.close()
    assert not ddp.is_open


async def test_close_from_cancelled_task_still_closes_socket(make_strip: MakeStrip) -> None:
    ddp = DDPSender(make_strip(), keepalive_interval=0.5)

    async def worker() -> None:
        async with ddp:
            await asyncio.sleep(10)

    task = asyncio.create_task(worker())
    await asyncio.sleep(0.05)
    transport = ddp._transport
    assert transport is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not ddp.is_open
    assert transport.is_closing()
    assert all(not t.get_name().startswith("pyddp-keepalive") for t in asyncio.all_tasks())


async def test_concurrent_open_creates_one_socket_and_keepalive(
    make_strip: MakeStrip, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    transports: list[asyncio.BaseTransport] = []
    real_create = loop.create_datagram_endpoint

    async def counting_create(*args: Any, **kwargs: Any) -> Any:
        transport, protocol = await real_create(*args, **kwargs)
        transports.append(transport)
        return transport, protocol

    monkeypatch.setattr(loop, "create_datagram_endpoint", counting_create)
    ddp = DDPSender(make_strip(), keepalive_interval=0.5)
    await asyncio.gather(ddp.open(), ddp.open())
    assert [t.get_name() for t in asyncio.all_tasks()].count("pyddp-keepalive-127.0.0.1") == 1
    await ddp.close()

    assert len(transports) == 1
    assert all(t.is_closing() for t in transports)
    assert all(not t.get_name().startswith("pyddp-keepalive") for t in asyncio.all_tasks())


_OPS: dict[str, Callable[[DDPSender], Awaitable[None]]] = {
    "send_frame": lambda d: d.send_frame(bytes(30)),
    "fill": lambda d: d.fill(None, (1, 2, 3)),
    "off": lambda d: d.off(),
    "push": lambda d: d.push(),
}


@pytest.mark.parametrize("op", sorted(_OPS))
@pytest.mark.parametrize("opened_then_closed", [False, True])
async def test_operations_when_not_open_raise_send_error(
    make_strip: MakeStrip, op: str, opened_then_closed: bool
) -> None:
    ddp = DDPSender(make_strip())
    if opened_then_closed:
        await ddp.open()
        await ddp.close()
    with pytest.raises(DDPSendError, match="not open"):
        await _OPS[op](ddp)


@pytest.mark.parametrize(
    ("host", "bind_host", "cause"),
    [
        ("pyddp-test.invalid", None, OSError),
        ("127.0.0.1", "192.0.2.1", OSError),
        ("a" * 64 + ".invalid", None, UnicodeError),
    ],
    ids=["unresolvable-host", "unbindable-local-address", "label-too-long"],
)
async def test_open_wraps_resolution_errors_as_send_error(
    host: str, bind_host: str | None, cause: type[Exception]
) -> None:
    ddp = DDPSender(Strip(host=host, pixel_count=1), bind_host=bind_host)
    with pytest.raises(DDPSendError) as info:
        await ddp.open()
    assert isinstance(info.value.__cause__, cause)
    assert not ddp.is_open


async def test_bind_host_sets_local_address(udp_listener: UDPListener, make_strip: MakeStrip) -> None:
    async with DDPSender(make_strip(), bind_host="127.0.0.1") as ddp:
        await ddp.fill(None, (1, 1, 1))
        await udp_listener.wait_for(1)
        assert ddp.strip.host == "127.0.0.1"


async def test_icmp_port_unreachable_counts_error_without_raising() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    loop = asyncio.get_running_loop()
    async with DDPSender(Strip(host="127.0.0.1", port=dead_port, pixel_count=1)) as ddp:
        deadline = loop.time() + 1.0
        while ddp.stats.send_errors == 0 and loop.time() < deadline:
            await ddp.fill(None, (1, 1, 1))
            await asyncio.sleep(0.01)
        assert ddp.stats.send_errors >= 1
        assert ddp.is_open
        await ddp.fill(None, (2, 2, 2))


async def test_error_received_warns_once_per_errno_then_debug(
    sender: DDPSender, caplog: pytest.LogCaptureFixture
) -> None:
    protocol = sender._protocol  # fault injection: the OS decides when ICMP errors arrive
    assert protocol is not None
    caplog.set_level(logging.DEBUG, logger="pyddp.sender")
    for code in (errno.ECONNREFUSED, errno.ECONNREFUSED, errno.EHOSTUNREACH):
        protocol.error_received(OSError(code, "boom"))

    levels = [r.levelno for r in caplog.records if "send error" in r.getMessage()]
    assert levels == [logging.WARNING, logging.DEBUG, logging.WARNING]
    assert sender.stats.send_errors == 3
    assert sender.is_open


async def test_sendto_os_error_is_counted_not_raised(
    udp_listener: UDPListener, sender: DDPSender, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = sender._transport  # fault injection: UDP sendto does not raise in practice
    assert transport is not None

    def failing_sendto(data: bytes, addr: object = None) -> None:
        raise OSError(errno.ENOBUFS, "no buffer space")

    monkeypatch.setattr(transport, "sendto", failing_sendto)
    await sender.fill(None, (1, 2, 3))
    assert (sender.stats.send_errors, sender.stats.packets_sent) == (1, 0)
    assert sender.stats.last_frame == bytes([1, 2, 3]) * 10


async def test_connection_lost_marks_sender_closed(make_strip: MakeStrip, caplog: pytest.LogCaptureFixture) -> None:
    ddp = DDPSender(make_strip(), keepalive_interval=0.5)
    await ddp.open()
    protocol, transport = ddp._protocol, ddp._transport
    assert protocol is not None
    assert transport is not None
    protocol.connection_lost(OSError(errno.ENETDOWN, "network down"))
    transport.close()  # the OS would already have torn the socket down

    assert not ddp.is_open
    assert any(r.levelno == logging.WARNING and "connection lost" in r.getMessage() for r in caplog.records)
    with pytest.raises(DDPSendError):
        await ddp.fill(None, (1, 1, 1))
    await ddp.close()  # still reaps the keepalive task
