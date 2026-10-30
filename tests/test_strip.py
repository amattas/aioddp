"""Strip and Segment: validation table, channel map, byte math."""

from __future__ import annotations

from itertools import permutations
from typing import Any

import pytest

from aioddp.const import DDP_PORT, DEFAULT_GAMMA, MAX_OFFSET, DataType
from aioddp.exceptions import ConfigError
from aioddp.strip import Segment, Strip


def _strip(**overrides: Any) -> Strip:
    kwargs: dict[str, Any] = {"host": "10.0.0.1", "pixel_count": 300} | overrides
    return Strip(**kwargs)


@pytest.mark.parametrize(
    ("field", "value", "extra"),
    [
        ("host", "   ", {}),
        ("host", "10.0.0.1\x00", {}),
        ("host", "10.0.0 .1", {}),
        ("host", " 10.0.0.1", {}),
        ("pixel_count", 0, {}),
        ("pixel_count", MAX_OFFSET // 3 + 1, {}),
        ("pixel_count", 1.5, {}),
        ("port", 0, {}),
        ("port", 65536, {}),
        ("data_type", "RGB", {}),
        ("color_order", "RGB", {"data_type": DataType.RGBW}),
        ("color_order", "RRB", {}),
        ("color_order", "RGBW", {}),
        ("brightness_cap", 0.0, {}),
        ("brightness_cap", 1.01, {}),
        ("brightness_cap", "0.5", {}),
        ("brightness_cap", None, {}),
        ("brightness_cap", True, {}),
        ("gamma", 0.5, {}),
        ("gamma", "2.2", {}),
        ("gamma", 4.01, {}),
        ("destination_id", 256, {}),
        ("destination_id", -1, {}),
        ("data_type_byte", 256, {}),
        ("data_type_byte", -1, {}),
    ],
)
def test_strip_validation(field: str, value: Any, extra: dict[str, Any]) -> None:
    with pytest.raises(ConfigError, match=field):
        _strip(**{field: value}, **extra)


def test_strip_defaults_and_normalization() -> None:
    strip = _strip(color_order="grbw", data_type=DataType.RGBW, gamma=None)
    assert strip.color_order == "GRBW"
    assert strip.port == DDP_PORT
    assert _strip().gamma == DEFAULT_GAMMA
    assert _strip(gamma=1.0).gamma == 1.0


@pytest.mark.parametrize(
    ("data_type", "order"),
    [(DataType.RGB, "".join(p)) for p in permutations("RGB")]
    + [(DataType.RGBW, "".join(p)) for p in permutations("RGBW")],
)
def test_channel_map_reproduces_color_order(data_type: DataType, order: str) -> None:
    strip = _strip(data_type=data_type, color_order=order)
    canonical = "RGBW"[: data_type.channels]
    assert "".join(canonical[i] for i in strip.channel_map) == order


@pytest.mark.parametrize(
    ("data_type", "type_byte", "channels", "wire"),
    [
        (DataType.RGB, None, 3, 0x0B),
        (DataType.RGBW, None, 4, 0x1B),
        (DataType.RGB, 0x01, 3, 0x01),
        (DataType.RGBW, 0x00, 4, 0x00),
    ],
)
def test_byte_math(data_type: DataType, type_byte: int | None, channels: int, wire: int) -> None:
    strip = _strip(data_type=data_type, data_type_byte=type_byte)  # color order defaults to the canonical one
    assert strip.color_order == "RGBW"[:channels]
    segment = Segment("peak", start=260, length=40)
    assert strip.channels == channels
    assert strip.frame_size == 300 * channels
    assert strip.wire_data_type == wire
    assert strip.byte_offset(segment) == 260 * channels
    assert strip.byte_length(segment) == 40 * channels


def test_full_segment() -> None:
    assert _strip().full_segment() == Segment("all", 0, 300)
    assert _strip().full_segment("roof").name == "roof"


@pytest.mark.parametrize(
    ("segment", "ok"),
    [
        (Segment("last", start=299, length=1), True),
        (Segment("peak", start=260, length=40, reverse=True), True),
        (Segment("bad", start=290, length=20), False),
        (Segment("beyond", start=300, length=1), False),
    ],
)
def test_validate_segment(segment: Segment, ok: bool) -> None:
    strip = _strip()
    if ok:
        strip.validate_segment(segment)
    else:
        with pytest.raises(ConfigError, match=segment.name):
            strip.validate_segment(segment)


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("name", {"name": "  ", "start": 0, "length": 1}),
        ("start", {"name": "a", "start": -1, "length": 1}),
        ("length", {"name": "a", "start": 0, "length": 0}),
    ],
)
def test_segment_validation(field: str, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ConfigError, match=field):
        Segment(**kwargs)


def test_segment_name_stripped_and_end() -> None:
    segment = Segment("  roof ", start=10, length=5)
    assert segment.name == "roof"
    assert segment.end == 15
