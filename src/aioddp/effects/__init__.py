"""Effect registry. Names are stable public API: hass-ddp uses them verbatim as Home Assistant effect values."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

from aioddp.color import RGB
from aioddp.effects.base import Effect, ParamSpec, validate_params
from aioddp.effects.breathe import Breathe
from aioddp.effects.chase import Chase
from aioddp.effects.palette import Palette
from aioddp.effects.rainbow import Rainbow
from aioddp.effects.solid import Solid
from aioddp.effects.twinkle import Twinkle
from aioddp.exceptions import EffectError

EFFECTS: Mapping[str, type[Effect]] = {
    "solid": Solid,
    "breathe": Breathe,
    "chase": Chase,
    "twinkle": Twinkle,
    "rainbow": Rainbow,
    "palette": Palette,
}


class EffectInfo(NamedTuple):
    """Description of one registered effect, for the CLI ``effects`` listing and hass-ddp's effect list."""

    name: str
    description: str
    static: bool
    params: Mapping[str, ParamSpec]
    color_param: str | None = None  # the param a single color (e.g. Home Assistant's rgb_color) drives, if any

    def color_params(self, rgb: RGB) -> dict[str, Any]:
        """Params that put one ``rgb`` color on this effect; empty when the effect has no color to drive."""
        if self.color_param is None:
            return {}
        if self.params[self.color_param].kind == "colors":
            return {self.color_param: [rgb]}
        return {self.color_param: rgb}


def get_effect(name: str) -> type[Effect]:
    """Look up an effect class by registry name."""
    try:
        return EFFECTS[name]
    except KeyError:
        raise EffectError(f"unknown effect {name!r}; valid effects: {', '.join(sorted(EFFECTS))}") from None


def list_effects() -> list[EffectInfo]:
    """Every registered effect, in registry order."""
    return [
        EffectInfo(e.name, e.description, e.static, e.params, getattr(e, "color_param", None)) for e in EFFECTS.values()
    ]


__all__ = ["EFFECTS", "Effect", "EffectInfo", "ParamSpec", "get_effect", "list_effects", "validate_params"]
