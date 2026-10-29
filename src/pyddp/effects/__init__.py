"""Effect registry. Names are stable public API: hass-ddp uses them verbatim as Home Assistant effect values."""

from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

from pyddp.effects.base import Effect, ParamSpec, validate_params
from pyddp.effects.breathe import Breathe
from pyddp.effects.chase import Chase
from pyddp.effects.palette import Palette
from pyddp.effects.rainbow import Rainbow
from pyddp.effects.solid import Solid
from pyddp.effects.twinkle import Twinkle
from pyddp.exceptions import EffectError

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


def get_effect(name: str) -> type[Effect]:
    """Look up an effect class by registry name."""
    try:
        return EFFECTS[name]
    except KeyError:
        raise EffectError(f"unknown effect {name!r}; valid effects: {', '.join(sorted(EFFECTS))}") from None


def list_effects() -> list[EffectInfo]:
    """Every registered effect, in registry order."""
    return [EffectInfo(e.name, e.description, e.static, e.params) for e in EFFECTS.values()]


__all__ = ["EFFECTS", "Effect", "EffectInfo", "ParamSpec", "get_effect", "list_effects", "validate_params"]
