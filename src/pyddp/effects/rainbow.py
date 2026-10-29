"""rainbow: hue sweeps along the segment and rotates over time."""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import Any, ClassVar

from pyddp.color import hsv_to_rgb
from pyddp.effects.base import BaseEffect, ParamSpec

WHEEL_SIZE = 1536  # 6 hue sextants x 256 steps


def _build_wheel() -> bytes:
    # One extra entry (a copy of hue 0) so an index that rounds up to WHEEL_SIZE needs no wrap check.
    return b"".join(bytes(hsv_to_rgb(k / WHEEL_SIZE, 1.0, 1.0)) for k in range(WHEEL_SIZE + 1))


_FULL_WHEEL = _build_wheel()


@lru_cache(maxsize=16)
def _wheel(saturation: float) -> bytes:
    """The hue wheel with every channel lerped toward white by ``1 - saturation``."""
    if saturation == 1.0:
        return _FULL_WHEEL
    return bytes(round(c * saturation + 255 * (1.0 - saturation)) for c in _FULL_WHEEL)


class Rainbow(BaseEffect):
    name: ClassVar[str] = "rainbow"
    description: ClassVar[str] = "Moving rainbow across the segment"
    params: ClassVar[Mapping[str, ParamSpec]] = {
        "speed": ParamSpec("float", 0.1, "Hue rotations per second; negative reverses", -5.0, 5.0),
        "spread": ParamSpec("float", 1.0, "Hue cycles across the segment", 0.01, 20.0),
        "saturation": ParamSpec("float", 1.0, "0 is white, 1 is fully saturated", 0.0, 1.0),
    }

    def render(self, length: int, t: float, params: Mapping[str, Any], seed: int) -> bytes:
        wheel = _wheel(params["saturation"])
        step = params["spread"] / length
        offset = (t * params["speed"]) % 1.0
        out = bytearray(length * 3)
        j = 0
        for i in range(length):
            k = int(((i * step + offset) % 1.0) * WHEEL_SIZE) * 3
            out[j] = wheel[k]
            out[j + 1] = wheel[k + 1]
            out[j + 2] = wheel[k + 2]
            j += 3
        return bytes(out)
