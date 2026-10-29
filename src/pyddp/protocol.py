"""DDP packet construction: header encode/decode, sequence numbers, chunking.

Pure and synchronous; no sockets and no color math.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from pyddp.const import (
    DESTINATION_DEFAULT,
    FLAG_PUSH,
    FLAG_VERSION_1,
    FLAG_VERSION_MASK,
    HEADER_FORMAT,
    HEADER_SIZE,
    MAX_DATA_LEN,
    MAX_OFFSET,
    SEQUENCE_MAX,
    SEQUENCE_MIN,
)
from pyddp.exceptions import ConfigError


@dataclass(frozen=True, slots=True)
class DDPHeader:
    """Typed view of the 10-byte DDP header. ``encode`` does not validate; use ``build_header``."""

    flags: int
    sequence: int
    data_type: int
    destination: int
    offset: int
    length: int

    @property
    def version(self) -> int:
        """Protocol version from the top two flag bits."""
        return (self.flags & FLAG_VERSION_MASK) >> 6

    @property
    def push(self) -> bool:
        """Whether the push (latch) flag is set."""
        return bool(self.flags & FLAG_PUSH)

    def encode(self) -> bytes:
        """Pack the header into 10 wire bytes."""
        return struct.pack(
            HEADER_FORMAT, self.flags, self.sequence, self.data_type, self.destination, self.offset, self.length
        )

    @classmethod
    def decode(cls, data: bytes) -> DDPHeader:
        """Parse the first 10 bytes of ``data``; raises ``ValueError`` if it is shorter."""
        if len(data) < HEADER_SIZE:
            raise ValueError(f"DDP header needs {HEADER_SIZE} bytes, got {len(data)}")
        return cls(*struct.unpack_from(HEADER_FORMAT, data))


def _check_range(name: str, value: int, low: int, high: int) -> None:
    if not low <= value <= high:
        raise ConfigError(f"{name} must be {low} to {high}, got {value}")


def build_header(
    *,
    offset: int,
    length: int,
    sequence: int,
    data_type: int,
    push: bool,
    destination: int = DESTINATION_DEFAULT,
) -> bytes:
    """Build a validated v1 header; flags are version 1 plus push when requested."""
    _check_range("offset", offset, 0, MAX_OFFSET)
    _check_range("length", length, 0, MAX_DATA_LEN)
    _check_range("sequence", sequence, 0, SEQUENCE_MAX)
    _check_range("data_type", data_type, 0, 0xFF)
    _check_range("destination", destination, 0, 0xFF)
    flags = FLAG_VERSION_1 | (FLAG_PUSH if push else 0)
    return struct.pack(HEADER_FORMAT, flags, sequence, data_type, destination, offset, length)


def _advance(value: int) -> int:
    return SEQUENCE_MIN if value >= SEQUENCE_MAX else value + 1


class SequenceCounter:
    """Per-sender DDP sequence number cycling 1 to 15; one number per packet."""

    __slots__ = ("_current",)

    def __init__(self, start: int = SEQUENCE_MIN) -> None:
        _check_range("start", start, SEQUENCE_MIN, SEQUENCE_MAX)
        self._current = start

    @property
    def current(self) -> int:
        """The number the next call to ``next`` returns."""
        return self._current

    def next(self) -> int:
        """Return the current number, then advance (15 wraps to 1)."""
        value = self._current
        self._current = _advance(value)
        return value


def chunk_data(data: bytes, base_offset: int = 0, max_len: int = MAX_DATA_LEN) -> list[tuple[int, bytes]]:
    """Split ``data`` into ``(offset, chunk)`` pairs of at most ``max_len`` bytes, ignoring pixel boundaries."""
    if not data:
        raise ConfigError("data must not be empty; use build_push_packet for an explicit latch")
    if max_len < 1:
        raise ConfigError(f"max_len must be at least 1, got {max_len}")
    return [(base_offset + i, data[i : i + max_len]) for i in range(0, len(data), max_len)]


def build_packets(
    data: bytes,
    *,
    base_offset: int,
    data_type: int,
    sequence: SequenceCounter,
    push: bool = True,
    destination: int = DESTINATION_DEFAULT,
    max_len: int = MAX_DATA_LEN,
) -> list[bytes]:
    """Build every packet of one frame; push only on the last. The counter advances only on success."""
    chunks = chunk_data(data, base_offset, max_len)
    last = len(chunks) - 1
    seq = sequence.current
    packets: list[bytes] = []
    for index, (offset, chunk) in enumerate(chunks):
        header = build_header(
            offset=offset,
            length=len(chunk),
            sequence=seq,
            data_type=data_type,
            push=push and index == last,
            destination=destination,
        )
        packets.append(header + chunk)
        seq = _advance(seq)
    for _ in packets:
        sequence.next()
    return packets


def build_push_packet(*, sequence: SequenceCounter, data_type: int, destination: int = DESTINATION_DEFAULT) -> bytes:
    """Header-only latch packet: offset 0, length 0, push set."""
    packet = build_header(
        offset=0, length=0, sequence=sequence.current, data_type=data_type, push=True, destination=destination
    )
    sequence.next()
    return packet
