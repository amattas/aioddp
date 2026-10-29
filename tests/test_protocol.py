"""Protocol: golden headers, chunking, sequence numbers, validation."""

from __future__ import annotations

import socket
from collections.abc import Callable

import pytest

from pyddp.const import (
    DATA_TYPE_LEGACY,
    FLAG_QUERY,
    FLAG_REPLY,
    FLAG_STORAGE,
    FLAG_TIMECODE,
    HEADER_SIZE,
    MAX_DATA_LEN,
    MAX_OFFSET,
    DataType,
)
from pyddp.exceptions import ConfigError
from pyddp.protocol import (
    DDPHeader,
    SequenceCounter,
    build_header,
    build_packets,
    build_push_packet,
    chunk_data,
)
from tests.conftest import UDPListener

RGB = DataType.RGB.wire_byte
RGBW = DataType.RGBW.wire_byte


def _hex(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", ""))


@pytest.mark.parametrize(
    ("built", "expected"),
    [
        (lambda: build_header(offset=0, length=3, sequence=1, data_type=RGB, push=True), "41 01 0B 01 00000000 0003"),
        (
            lambda: build_header(offset=1440, length=3, sequence=2, data_type=RGB, push=False),
            "40 02 0B 01 000005A0 0003",
        ),
        (lambda: build_header(offset=0, length=4, sequence=15, data_type=RGBW, push=True), "41 0F 1B 01 00000000 0004"),
        (
            lambda: build_header(offset=0, length=3, sequence=1, data_type=DATA_TYPE_LEGACY, push=True),
            "41 01 01 01 00000000 0003",
        ),
        (lambda: build_push_packet(sequence=SequenceCounter(3), data_type=RGB), "41 03 0B 01 00000000 0000"),
    ],
    ids=["rgb-push", "rgb-offset-nopush", "rgbw-seq15", "legacy-byte", "push-latch"],
)
def test_golden_headers(built: Callable[[], bytes], expected: str) -> None:
    header = built()
    assert header == _hex(expected)
    decoded = DDPHeader.decode(header)
    assert decoded.encode() == header
    assert decoded.version == 1
    assert decoded.push is bool(header[0] & 0x01)
    assert decoded.flags & (FLAG_TIMECODE | FLAG_STORAGE | FLAG_REPLY | FLAG_QUERY) == 0


def test_decode_rejects_short_input() -> None:
    with pytest.raises(ValueError, match="10 bytes"):
        DDPHeader.decode(b"\x41" * (HEADER_SIZE - 1))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"offset": MAX_OFFSET + 1},
        {"offset": -1},
        {"length": MAX_DATA_LEN + 1},
        {"length": -1},
        {"sequence": 16},
        {"sequence": -1},
        {"data_type": 256},
        {"destination": 256},
        {"destination": -1},
    ],
)
def test_build_header_rejects_out_of_range(kwargs: dict[str, int]) -> None:
    args: dict[str, int] = {"offset": 0, "length": 3, "sequence": 1, "data_type": RGB} | kwargs
    field = next(iter(kwargs))
    with pytest.raises(ConfigError, match=field):
        build_header(push=True, **args)


def test_build_header_allows_sequence_zero() -> None:
    assert build_header(offset=0, length=0, sequence=0, data_type=RGB, push=False)[1] == 0


@pytest.mark.parametrize(
    ("size", "base_offset", "push", "expected"),
    [
        (1440, 0, True, [(0, 1440, True)]),
        (1441, 0, True, [(0, 1440, False), (1440, 1, True)]),
        (3000, 600, True, [(600, 1440, False), (2040, 1440, False), (3480, 120, True)]),
        (900, 0, False, [(0, 900, False)]),
    ],
)
def test_build_packets_chunking(size: int, base_offset: int, push: bool, expected: list[tuple[int, int, bool]]) -> None:
    data = bytes(i % 251 for i in range(size))
    packets = build_packets(data, base_offset=base_offset, data_type=RGB, sequence=SequenceCounter(), push=push)
    headers = [DDPHeader.decode(p) for p in packets]
    assert [(h.offset, h.length, h.push) for h in headers] == expected
    assert all(len(p) == HEADER_SIZE + h.length for p, h in zip(packets, headers, strict=True))
    assert b"".join(p[HEADER_SIZE:] for p in packets) == data


def test_chunk_data_splits_inside_a_pixel() -> None:
    # Two RGB pixels with a 4-byte chunk limit: the boundary falls inside the second pixel.
    assert chunk_data(b"\x01\x02\x03\x04\x05\x06", base_offset=30, max_len=4) == [
        (30, b"\x01\x02\x03\x04"),
        (34, b"\x05\x06"),
    ]


def test_sequence_counter_wraps_15_to_1() -> None:
    counter = SequenceCounter()
    assert [counter.next() for _ in range(16)] == [*range(1, 16), 1]
    assert counter.current == 2


@pytest.mark.parametrize("start", [0, 16])
def test_sequence_counter_rejects_bad_start(start: int) -> None:
    with pytest.raises(ConfigError, match="start"):
        SequenceCounter(start)


def test_build_packets_draws_one_sequence_per_packet() -> None:
    counter = SequenceCounter(14)
    packets = build_packets(bytes(3000), base_offset=0, data_type=RGB, sequence=counter)
    assert [DDPHeader.decode(p).sequence for p in packets] == [14, 15, 1]
    assert counter.current == 2


@pytest.mark.parametrize(
    ("data", "base_offset", "max_len"),
    [
        (b"", 0, MAX_DATA_LEN),
        (bytes(2000), MAX_OFFSET - 100, MAX_DATA_LEN),
        (bytes(2000), 0, MAX_DATA_LEN + 100),
        (bytes(10), 0, 0),
    ],
    ids=["empty", "second-chunk-offset-overflow", "max-len-too-big", "max-len-zero"],
)
def test_build_packets_validation_error_leaves_counter_untouched(data: bytes, base_offset: int, max_len: int) -> None:
    counter = SequenceCounter(5)
    with pytest.raises(ConfigError):
        build_packets(data, base_offset=base_offset, data_type=RGB, sequence=counter, max_len=max_len)
    assert counter.current == 5


async def test_udp_listener_receives_and_decodes(udp_listener: UDPListener) -> None:
    packet = build_header(offset=6, length=3, sequence=4, data_type=RGB, push=True) + b"\xaa\xbb\xcc"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(packet, (udp_listener.host, udp_listener.port))
    assert await udp_listener.wait_for(1) == [packet]
    assert udp_listener.headers() == [DDPHeader(0x41, 4, RGB, 1, 6, 3)]
    assert udp_listener.payloads() == [b"\xaa\xbb\xcc"]
    udp_listener.clear()
    assert udp_listener.packets == []
