"""chase: theater chase, lit pixels every ``spacing`` moving at ``speed`` pixels per second."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, ClassVar

from aioddp.effects.base import BaseEffect, ParamSpec
from aioddp.exceptions import EffectError


class Chase(BaseEffect):
    name: ClassVar[str] = "chase"
    description: ClassVar[str] = "Theater chase; multiple colors move as blocks with the chase"
    color_param: ClassVar[str | None] = "colors"
    params: ClassVar[Mapping[str, ParamSpec]] = {
        "colors": ParamSpec("colors", ((255, 255, 255),), "Colors of successive lit groups"),
        "speed": ParamSpec("float", 8.0, "Pixels per second; negative runs backwards", -100.0, 100.0),
        "spacing": ParamSpec("int", 3, "Pixels from one lit group to the next", 2, 50),
        "width": ParamSpec("int", 1, "Lit pixels per group (at most spacing)", 1, 50),
        "background": ParamSpec("color", (0, 0, 0), "Color of unlit pixels"),
    }

    @classmethod
    def check_params(cls, params: Mapping[str, Any]) -> None:
        if params["width"] > params["spacing"]:
            raise EffectError(
                f"{cls.name} param 'width': must be <= spacing ({params['spacing']}), got {params['width']}"
            )

    def render(self, length: int, t: float, params: Mapping[str, Any], seed: int) -> bytes:
        colors = params["colors"]
        spacing: int = params["spacing"]
        width: int = params["width"]
        background = bytes(params["background"])
        # One full cycle of the pattern covers every color block, so the shift wraps there (not at `spacing`);
        # that keeps multi-color blocks travelling with the chase instead of snapping back each group.
        cycle = spacing * len(colors)
        pattern = b"".join(bytes(colors[m // spacing]) if m % spacing < width else background for m in range(cycle))
        shift = math.floor(t * params["speed"]) % cycle
        # Pixel i shows pattern[(i - shift) mod cycle]: rotate so pixel 0 lands on pattern index -shift.
        start = (-shift % cycle) * 3
        rotated = pattern[start:] + pattern[:start]
        repeats = -(-length // cycle)
        return (rotated * repeats)[: length * 3]
