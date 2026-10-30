"""palette: repeating blocks of fixed colors (static)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from aioddp.effects.base import BaseEffect, ParamSpec


class Palette(BaseEffect):
    name: ClassVar[str] = "palette"
    description: ClassVar[str] = "Repeating blocks of fixed colors"
    static: ClassVar[bool] = True
    params: ClassVar[Mapping[str, ParamSpec]] = {
        "colors": ParamSpec("colors", ((255, 0, 0), (0, 255, 0)), "Colors, in order"),
        "block_size": ParamSpec("int", 1, "Pixels per color block", 1, 1000),
    }

    def render(self, length: int, t: float, params: Mapping[str, Any], seed: int) -> bytes:
        block_size: int = params["block_size"]
        pattern = b"".join(bytes(color) * block_size for color in params["colors"])
        repeats = -(-length * 3 // len(pattern))
        return (pattern * repeats)[: length * 3]
