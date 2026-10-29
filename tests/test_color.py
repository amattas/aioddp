"""Color pipeline goldens, cap invariant, and color helpers."""

from __future__ import annotations

from collections.abc import Buffer, Callable, Sequence
from typing import Any

import pytest

from pyddp.color import (
    Color,
    expand_rgb_to_rgbw,
    hsv_to_rgb,
    kelvin_to_rgb,
    lerp_color,
    parse_color,
    process_frame,
    scale_color,
    solid_pixels,
)
from pyddp.const import DataType
from pyddp.exceptions import ConfigError
from pyddp.strip import Strip

R, G, B = b"\xff\x00\x00", b"\x00\xff\x00", b"\x00\x00\xff"


def _strip(**overrides: Any) -> Strip:
    kwargs: dict[str, Any] = {"host": "h", "pixel_count": 10, "gamma": None} | overrides
    return Strip(**kwargs)


@pytest.mark.parametrize(
    ("pixels", "strip_kwargs", "brightness", "reverse", "expected"),
    [
        (b"\xff\xff\xff", {}, 1.0, False, "ff ff ff"),
        (b"\xff\xff\xff", {"brightness_cap": 0.6}, 1.0, False, "99 99 99"),
        (b"\xff\xff\xff", {"gamma": 2.2}, 0.5, False, "37 37 37"),
        (b"\xff\xff\xff", {"gamma": 2.2, "brightness_cap": 0.6}, 1.0, False, "99 99 99"),
        (R, {"color_order": "GRB"}, 1.0, False, "00 ff 00"),
        (R + b"\x00", {"data_type": DataType.RGBW, "color_order": "GRBW"}, 1.0, False, "00 ff 00 00"),
        (R + G + B, {}, 1.0, True, "0000ff 00ff00 ff0000"),
        (R + G + B, {"color_order": "GRB"}, 1.0, True, "0000ff ff0000 00ff00"),
        (b"\x80\x40\x20\xff", {"data_type": DataType.RGBW, "color_order": "RGBW"}, 0.0, False, "00 00 00 00"),
    ],
    ids=["identity", "cap", "gamma-half", "gamma-cap", "grb", "grbw", "reverse", "reverse-grb", "zero-brightness"],
)
def test_process_frame_golden(
    pixels: bytes, strip_kwargs: dict[str, Any], brightness: float, reverse: bool, expected: str
) -> None:
    out = process_frame(pixels, _strip(**strip_kwargs), brightness=brightness, reverse=reverse)
    assert out == bytes.fromhex(expected.replace(" ", ""))


class _TranslateBypass(bytes):
    """A bytes subclass whose ``translate`` ignores the table: must not bypass the cap."""

    def translate(self, table: Any, /, delete: Buffer = b"") -> bytes:
        return bytes(self)


@pytest.mark.parametrize("brightness", [0.5, 1.0])
@pytest.mark.parametrize("gamma", [None, 2.2])
@pytest.mark.parametrize("wrap", [bytes, bytearray, memoryview, _TranslateBypass])
def test_cap_cannot_be_exceeded(brightness: float, gamma: float | None, wrap: Callable[[bytes], Any]) -> None:
    out = process_frame(wrap(bytes(range(256)) * 3), _strip(brightness_cap=0.6, gamma=gamma), brightness=brightness)
    assert type(out) is bytes
    assert max(out) <= 153


@pytest.mark.parametrize(
    ("pixels", "brightness", "match"),
    [(b"\x00\x00", 1.0, "multiple"), (b"\x00\x00\x00", 1.01, "brightness"), (b"\x00\x00\x00", -0.1, "brightness")],
)
def test_process_frame_validation(pixels: bytes, brightness: float, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        process_frame(pixels, _strip(), brightness=brightness)


@pytest.mark.parametrize(
    ("kelvin", "expected"),
    [
        (6500, (255, 254, 250)),
        (2700, (255, 167, 87)),
        (1500, (255, 108, 0)),
        (10000, (202, 218, 255)),
        (500, (255, 68, 0)),
        (90000, (152, 186, 255)),
    ],
    ids=["6500", "2700", "below-19", "above-66", "clamp-low", "clamp-high"],
)
def test_kelvin_to_rgb(kelvin: int, expected: tuple[int, int, int]) -> None:
    assert kelvin_to_rgb(kelvin) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ff8800", (255, 136, 0)),
        ("#FF8800", (255, 136, 0)),
        ("ff8800ff", (255, 136, 0, 255)),
        ((1, 2, 3), (1, 2, 3)),
        ([1, 2, 3, 4], (1, 2, 3, 4)),
        (" Red ", (255, 0, 0)),
        ("warmwhite", (255, 167, 87)),
        ("off", (0, 0, 0)),
    ],
)
def test_parse_color(value: str | Sequence[int], expected: Color) -> None:
    assert parse_color(value) == expected


@pytest.mark.parametrize("value", ["ff88", "zzzzzz", "purple", (256, 0, 0), (1, 2), (True, 0, 0), (1.0, 2, 3)])
def test_parse_color_rejects(value: Any) -> None:
    with pytest.raises(ConfigError, match="color"):
        parse_color(value)


@pytest.mark.parametrize(
    ("color", "factor", "expected"),
    [((255, 100, 3), 0.5, (128, 50, 2)), ((10, 20, 30, 40), 0.0, (0, 0, 0, 0)), ((10, 20, 30), 1.0, (10, 20, 30))],
)
def test_scale_color(color: Color, factor: float, expected: Color) -> None:
    assert scale_color(color, factor) == expected


@pytest.mark.parametrize(
    ("a", "b", "t", "expected"),
    [
        ((0, 0, 0), (255, 100, 10), 0.5, (128, 50, 5)),
        ((0, 0, 0, 0), (10, 20, 30, 40), 1.0, (10, 20, 30, 40)),
        ((9, 9, 9), (0, 0, 0), 0.0, (9, 9, 9)),
    ],
)
def test_lerp_color(a: Color, b: Color, t: float, expected: Color) -> None:
    assert lerp_color(a, b, t) == expected


@pytest.mark.parametrize(
    "call",
    [
        lambda: scale_color((1, 2, 3), 1.5),
        lambda: scale_color((1, 2, 3), -0.1),
        lambda: lerp_color((1, 2, 3), (1, 2, 3, 4), 0.5),
        lambda: lerp_color((1, 2, 3), (4, 5, 6), 1.1),
        lambda: solid_pixels((1, 2, 3, 4), 2, 3),
        lambda: solid_pixels((1, 2, 3), 2, 5),
        lambda: solid_pixels((1, 2, 3), -1, 3),
        lambda: expand_rgb_to_rgbw(b"\x00" * 4),
    ],
    ids=[
        "scale-high",
        "scale-low",
        "lerp-mixed",
        "lerp-t",
        "solid-rgbw-on-rgb",
        "solid-channels",
        "solid-length",
        "expand-length",
    ],
)
def test_helper_validation(call: Any) -> None:
    with pytest.raises(ConfigError):
        call()


@pytest.mark.parametrize(
    ("h", "s", "v", "expected"),
    [(0.0, 1.0, 1.0, (255, 0, 0)), (1 / 3, 1.0, 1.0, (0, 255, 0)), (0.5, 0.5, 0.5, (64, 128, 128))],
)
def test_hsv_to_rgb(h: float, s: float, v: float, expected: tuple[int, int, int]) -> None:
    assert hsv_to_rgb(h, s, v) == expected


@pytest.mark.parametrize(
    ("color", "length", "channels", "expected"),
    [
        ((1, 2, 3), 2, 3, "010203 010203"),
        ((1, 2, 3), 2, 4, "01020300 01020300"),
        ((1, 2, 3, 4), 1, 4, "01020304"),
    ],
)
def test_solid_pixels(color: Color, length: int, channels: int, expected: str) -> None:
    assert solid_pixels(color, length, channels) == bytes.fromhex(expected.replace(" ", ""))


def test_expand_rgb_to_rgbw() -> None:
    assert expand_rgb_to_rgbw(b"\x01\x02\x03\x04\x05\x06") == b"\x01\x02\x03\x00\x04\x05\x06\x00"
