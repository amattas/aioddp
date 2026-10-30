"""breathe: whole segment fades between ``min_level`` and full on a cosine."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, ClassVar

from aioddp.color import scale_color
from aioddp.effects.base import BaseEffect, ParamSpec


class Breathe(BaseEffect):
    name: ClassVar[str] = "breathe"
    description: ClassVar[str] = "Whole segment fades smoothly up and down"
    color_param: ClassVar[str | None] = "color"
    params: ClassVar[Mapping[str, ParamSpec]] = {
        "color": ParamSpec("color", (255, 255, 255), "Color at full level"),
        "period": ParamSpec("float", 4.0, "Seconds per breath", 0.5, 60.0),
        "min_level": ParamSpec("float", 0.05, "Level at the bottom of the breath", 0.0, 1.0),
    }

    def render(self, length: int, t: float, params: Mapping[str, Any], seed: int) -> bytes:
        period: float = params["period"]
        min_level: float = params["min_level"]
        phase = (t % period) / period
        level = min_level + (1.0 - min_level) * (0.5 - 0.5 * math.cos(2.0 * math.pi * phase))
        return bytes(scale_color(params["color"], min(level, 1.0))) * length
