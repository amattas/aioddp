"""twinkle: a seeded subset of pixels ramps between ``base_color`` and ``sparkle_color`` on a triangle wave."""

from __future__ import annotations

import random
from bisect import bisect_left
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, ClassVar

from pyddp.effects.base import BaseEffect, ParamSpec


@lru_cache(maxsize=32)
def _tables(length: int, seed: int) -> tuple[tuple[float, ...], tuple[int, ...], tuple[float, ...]]:
    """Per-pixel phases, pixel indices ordered by selection value, and the sorted selection values.

    Pixel ``i`` twinkles when its selection value ``s_i < density``; with indices ordered by ``s_i`` the
    twinkling set is a prefix whose length comes from one bisect, so changing ``density`` only grows or
    shrinks that prefix and never reshuffles which pixels twinkle.
    """
    # Visual variation only; not used for anything security-relevant.
    rng = random.Random(seed)  # noqa: S311  # nosec B311
    phases: list[float] = []
    selection: list[float] = []
    for _ in range(length):
        phases.append(rng.random())
        selection.append(rng.random())
    order = tuple(sorted(range(length), key=selection.__getitem__))
    return tuple(phases), order, tuple(selection[i] for i in order)


class Twinkle(BaseEffect):
    name: ClassVar[str] = "twinkle"
    description: ClassVar[str] = "Random pixels sparkle and fade over a base color"
    color_param: ClassVar[str | None] = "sparkle_color"
    params: ClassVar[Mapping[str, ParamSpec]] = {
        "base_color": ParamSpec("color", (0, 0, 0), "Color of resting pixels"),
        "sparkle_color": ParamSpec("color", (255, 255, 255), "Color at the peak of a twinkle"),
        "density": ParamSpec("float", 0.15, "Fraction of pixels that twinkle", 0.0, 1.0),
        "speed": ParamSpec("float", 1.0, "Twinkle cycles per second", 0.05, 10.0),
    }

    def render(self, length: int, t: float, params: Mapping[str, Any], seed: int) -> bytes:
        phases, order, sorted_selection = _tables(length, seed)
        br, bg, bb = params["base_color"]
        sr, sg, sb = params["sparkle_color"]
        dr, dg, db = sr - br, sg - bg, sb - bb
        out = bytearray(bytes((br, bg, bb)) * length)
        offset = (t * params["speed"]) % 1.0
        for i in order[: bisect_left(sorted_selection, params["density"])]:
            x = offset + phases[i]
            if x >= 1.0:
                x -= 1.0
            level = 2.0 * x if x < 0.5 else 2.0 - 2.0 * x
            j = 3 * i
            out[j] = round(br + dr * level)
            out[j + 1] = round(bg + dg * level)
            out[j + 2] = round(bb + db * level)
        return bytes(out)
