"""palette: repeating blocks of fixed colors (static; RGBW colors render 4-channel pixels)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from aioddp.effects.base import BaseEffect, ParamSpec


class Palette(BaseEffect):
    name: ClassVar[str] = "palette"
    description: ClassVar[str] = "Repeating blocks of fixed colors"
    static: ClassVar[bool] = True
    rgbw: ClassVar[bool] = True
    params: ClassVar[Mapping[str, ParamSpec]] = {
        "colors": ParamSpec("colors", ((255, 0, 0), (0, 255, 0)), "Colors, in order"),
        "block_size": ParamSpec("int", 1, "Pixels per color block", 1, 1000),
    }

    def render(self, length: int, t: float, params: Mapping[str, Any], seed: int) -> bytes:
        block_size: int = params["block_size"]
        colors = params["colors"]
        channels = max(len(color) for color in colors)  # 4 when any color is RGBW; RGB is padded with W=0
        pattern = b"".join((bytes(color) + bytes(channels - len(color))) * block_size for color in colors)
        repeats = -(-length * channels // len(pattern))
        return (pattern * repeats)[: length * channels]
