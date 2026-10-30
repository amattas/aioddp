"""Immutable, validated Strip and Segment configuration models."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TypeGuard

from aioddp.const import DDP_PORT, DEFAULT_BRIGHTNESS_CAP, DEFAULT_GAMMA, DESTINATION_DEFAULT, MAX_OFFSET, DataType
from aioddp.exceptions import ConfigError

_MAX_GAMMA = 4.0


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> TypeGuard[float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _require_byte(name: str, value: object) -> None:
    if not _is_int(value) or not 0 <= value <= 0xFF:
        raise ConfigError(f"{name} must be an integer 0 to 255, got {value!r}")


@dataclass(frozen=True, slots=True)
class Segment:
    """A named pixel range ``[start, start + length)``; ``reverse`` flips pixel order on the wire."""

    name: str
    start: int
    length: int
    reverse: bool = False

    def __post_init__(self) -> None:
        name = self.name.strip() if isinstance(self.name, str) else ""
        if not name:
            raise ConfigError(f"Segment name must be a non-empty string, got {self.name!r}")
        object.__setattr__(self, "name", name)
        if not _is_int(self.start) or self.start < 0:
            raise ConfigError(f"Segment {name!r}: start must be an integer >= 0, got {self.start!r}")
        if not _is_int(self.length) or self.length < 1:
            raise ConfigError(f"Segment {name!r}: length must be an integer >= 1, got {self.length!r}")

    @property
    def end(self) -> int:
        """Exclusive end pixel index."""
        return self.start + self.length


@dataclass(frozen=True, slots=True)
class Strip:
    """One DDP receiver output: where to send, how many pixels, and how to encode them."""

    host: str
    pixel_count: int
    port: int = DDP_PORT
    data_type: DataType = DataType.RGB
    color_order: str = ""  # empty: the data type's canonical order, RGB or RGBW
    brightness_cap: float = DEFAULT_BRIGHTNESS_CAP
    gamma: float | None = DEFAULT_GAMMA
    destination_id: int = DESTINATION_DEFAULT
    data_type_byte: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host or any(c.isspace() or c == "\0" for c in self.host):
            raise ConfigError(f"host must be a non-empty string without whitespace or NUL, got {self.host!r}")
        if not isinstance(self.data_type, DataType):
            raise ConfigError(f"data_type must be a DataType member, got {self.data_type!r}")
        max_pixels = MAX_OFFSET // self.data_type.channels
        if not _is_int(self.pixel_count) or not 1 <= self.pixel_count <= max_pixels:
            raise ConfigError(f"pixel_count must be an integer 1 to {max_pixels}, got {self.pixel_count!r}")
        if not _is_int(self.port) or not 1 <= self.port <= 0xFFFF:
            raise ConfigError(f"port must be an integer 1 to 65535, got {self.port!r}")
        canonical = self._canonical_order()
        order = (self.color_order or canonical).upper() if isinstance(self.color_order, str) else ""
        if sorted(order) != sorted(canonical):
            raise ConfigError(f"color_order must be a permutation of {canonical!r}, got {self.color_order!r}")
        object.__setattr__(self, "color_order", order)
        if not _is_number(self.brightness_cap) or not 0.0 < self.brightness_cap <= 1.0:
            raise ConfigError(f"brightness_cap must be a number > 0.0 and <= 1.0, got {self.brightness_cap!r}")
        if self.gamma is not None and (
            not _is_number(self.gamma) or (self.gamma != 1.0 and not 1.0 < self.gamma <= _MAX_GAMMA)
        ):
            raise ConfigError(f"gamma must be None, 1.0, or a number > 1.0 and <= {_MAX_GAMMA}, got {self.gamma!r}")
        _require_byte("destination_id", self.destination_id)
        if self.data_type_byte is not None:
            _require_byte("data_type_byte", self.data_type_byte)

    def _canonical_order(self) -> str:
        return "RGBW"[: self.data_type.channels]

    @property
    def channels(self) -> int:
        """Bytes per pixel."""
        return self.data_type.channels

    @property
    def frame_size(self) -> int:
        """Bytes in a full-strip frame."""
        return self.pixel_count * self.channels

    @property
    def wire_data_type(self) -> int:
        """Data type byte for the header: the override if set, else the data type's own byte."""
        return self.data_type.wire_byte if self.data_type_byte is None else self.data_type_byte

    @property
    def channel_map(self) -> tuple[int, ...]:
        """For each output channel position, the canonical channel index it reads from."""
        canonical = self._canonical_order()
        return tuple(canonical.index(c) for c in self.color_order)

    def full_segment(self, name: str = "all") -> Segment:
        """A segment covering the whole strip."""
        return Segment(name, 0, self.pixel_count)

    def validate_segment(self, segment: Segment) -> None:
        """Raise ``ConfigError`` if ``segment`` extends past the end of the strip."""
        if segment.end > self.pixel_count:
            raise ConfigError(
                f"Segment {segment.name!r} ends at pixel {segment.end}, past the strip's {self.pixel_count} pixels"
            )

    def byte_offset(self, segment: Segment) -> int:
        """Byte offset of ``segment`` in the receiver's buffer."""
        return segment.start * self.channels

    def byte_length(self, segment: Segment) -> int:
        """Bytes in a frame for ``segment``."""
        return segment.length * self.channels
