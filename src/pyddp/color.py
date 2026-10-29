"""Color pipeline (brightness, gamma, cap, color order, reverse) and color helpers.

``process_frame`` is the single enforcement point for a strip's brightness cap.
"""

from __future__ import annotations

import colorsys
import math
from collections.abc import Sequence
from functools import lru_cache
from typing import cast

from pyddp.exceptions import ConfigError
from pyddp.strip import Strip

RGB = tuple[int, int, int]
RGBW = tuple[int, int, int, int]
Color = RGB | RGBW

NAMED_COLORS: dict[str, RGB] = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "white": (255, 255, 255),
    "warmwhite": (255, 167, 87),  # kelvin_to_rgb(2700)
    "off": (0, 0, 0),
}

_KELVIN_MIN = 1000
_KELVIN_MAX = 40000


@lru_cache(maxsize=256)
def _level_lut(brightness: float, gamma: float, cap: float) -> bytes:
    return bytes(round(255 * cap * ((v / 255) * brightness) ** gamma) for v in range(256))


def process_frame(
    pixels: bytes | bytearray | memoryview, strip: Strip, *, brightness: float = 1.0, reverse: bool = False
) -> bytes:
    """Turn canonical pixel bytes into wire bytes for ``strip``.

    Applies per-call brightness, gamma, and the strip's brightness cap via one lookup table,
    then reorders to the strip's color order and optionally reverses pixel order.
    """
    if type(pixels) is not bytes:
        pixels = bytes(pixels)  # plain bytes only: a subclass could override translate() and skip the cap
    channels = strip.channels
    if len(pixels) % channels:
        raise ConfigError(f"pixel data length {len(pixels)} is not a multiple of {channels} channels")
    if not 0.0 <= brightness <= 1.0:
        raise ConfigError(f"brightness must be 0.0 to 1.0, got {brightness!r}")

    gamma = 1.0 if strip.gamma is None else strip.gamma
    out = pixels.translate(_level_lut(round(brightness, 4), gamma, strip.brightness_cap))

    channel_map = strip.channel_map
    if channel_map != tuple(range(channels)):
        src = out
        reordered = bytearray(len(src))
        for k, source in enumerate(channel_map):
            reordered[k::channels] = src[source::channels]
        out = bytes(reordered)

    if reverse:
        src = out
        flipped = bytearray(len(src))
        for k in range(channels):
            flipped[k::channels] = src[k::channels][::-1]
        out = bytes(flipped)

    return out


def _validated_color(value: Sequence[object]) -> Color:
    if len(value) not in (3, 4) or not all(
        isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255 for c in value
    ):
        raise ConfigError(f"color must be 3 or 4 integers 0 to 255, got {value!r}")
    return cast(Color, tuple(value))


def parse_color(value: str | Sequence[int]) -> Color:
    """Parse hex (``"ff8800"``, ``"#ff8800"``, ``"ff8800ff"``), a 3/4-int sequence, or a named color."""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in NAMED_COLORS:
            return NAMED_COLORS[text]
        digits = text.removeprefix("#")
        if len(digits) not in (6, 8):
            raise ConfigError(f"color must be 6 or 8 hex digits or a name in {sorted(NAMED_COLORS)}, got {value!r}")
        try:
            raw = bytes.fromhex(digits)
        except ValueError as err:
            raise ConfigError(f"color is not valid hex: {value!r}") from err
        return _validated_color(tuple(raw))
    return _validated_color(value)


def _check_unit(name: str, value: float) -> None:
    if not 0.0 <= value <= 1.0:
        raise ConfigError(f"{name} must be 0.0 to 1.0, got {value!r}")


def scale_color(color: Color, factor: float) -> Color:
    """Multiply every channel by ``factor`` (0.0 to 1.0), rounding."""
    _check_unit("factor", factor)
    return cast(Color, tuple(round(c * factor) for c in color))


def lerp_color(a: Color, b: Color, t: float) -> Color:
    """Linear interpolation from ``a`` (t=0) to ``b`` (t=1); both must have the same channel count."""
    if len(a) != len(b):
        raise ConfigError(f"cannot interpolate colors with different channel counts: {a!r}, {b!r}")
    _check_unit("t", t)
    return cast(Color, tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True)))


def kelvin_to_rgb(kelvin: int) -> RGB:
    """Approximate a color temperature as RGB (Tanner Helland). Clamped to 1000-40000 K; best 2000-6500 K."""
    temp = min(max(kelvin, _KELVIN_MIN), _KELVIN_MAX) / 100
    if temp <= 66:
        red = 255.0
        green = 99.4708025861 * math.log(temp) - 161.1195681661
    else:
        red = 329.698727446 * (temp - 60) ** -0.1332047592
        green = 288.1221695283 * (temp - 60) ** -0.0755148492
    if temp >= 66:
        blue = 255.0
    elif temp <= 19:
        blue = 0.0
    else:
        blue = 138.5177312231 * math.log(temp - 10) - 305.0447927307
    r, g, b = (round(min(max(c, 0.0), 255.0)) for c in (red, green, blue))
    return (r, g, b)


def hsv_to_rgb(h: float, s: float, v: float) -> RGB:
    """HSV in 0.0 to 1.0 to RGB 0 to 255, rounded."""
    r, g, b = (round(c * 255) for c in colorsys.hsv_to_rgb(h, s, v))
    return (r, g, b)


def solid_pixels(color: Color, length: int, channels: int) -> bytes:
    """``length`` pixels of ``color`` for a 3- or 4-channel strip; RGB is padded with W=0 on 4 channels."""
    if channels not in (3, 4):
        raise ConfigError(f"channels must be 3 or 4, got {channels}")
    if length < 0:
        raise ConfigError(f"length must be >= 0, got {length}")
    if len(color) > channels:
        raise ConfigError(f"RGBW color {color!r} cannot be sent to a {channels}-channel strip")
    pixel = bytes(color) + bytes(channels - len(color))
    return pixel * length


def expand_rgb_to_rgbw(pixels: bytes) -> bytes:
    """Insert W=0 after every RGB triplet."""
    if len(pixels) % 3:
        raise ConfigError(f"RGB pixel data length {len(pixels)} is not a multiple of 3")
    out = bytearray(len(pixels) // 3 * 4)
    for k in range(3):
        out[k::4] = pixels[k::3]
    return bytes(out)
