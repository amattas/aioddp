"""solid: one color on every pixel (static)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pyddp.effects.base import BaseEffect, ParamSpec


class Solid(BaseEffect):
    name: ClassVar[str] = "solid"
    description: ClassVar[str] = "One color on every pixel"
    color_param: ClassVar[str | None] = "color"
    static: ClassVar[bool] = True
    params: ClassVar[Mapping[str, ParamSpec]] = {
        "color": ParamSpec("color", (255, 255, 255), "Fill color"),
    }

    def render(self, length: int, t: float, params: Mapping[str, Any], seed: int) -> bytes:
        return bytes(params["color"]) * length
