"""Effect contract, parameter schema, and parameter validation.

An effect turns ``(length, t, params, seed)`` into exactly ``length * 3`` canonical RGB bytes. It knows nothing
about strips, gamma, or the brightness cap. ``render`` receives params that already went through
``validate_params`` (defaults merged, values coerced and range-checked).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Literal, NamedTuple, Protocol, cast

from pyddp.color import RGB, parse_color
from pyddp.exceptions import ConfigError, EffectError

ParamKind = Literal["float", "int", "bool", "color", "colors"]

_TRUE = frozenset({"true", "yes", "on", "1"})
_FALSE = frozenset({"false", "no", "off", "0"})


class ParamSpec(NamedTuple):
    """Schema for one effect parameter. ``min``/``max`` are inclusive and apply to ``int`` and ``float``."""

    kind: ParamKind
    default: Any
    description: str
    min: float | None = None
    max: float | None = None


class Effect(Protocol):
    """What the runner, CLI, and hass-ddp rely on. See the module docstring for the render contract."""

    name: ClassVar[str]
    description: ClassVar[str]
    params: ClassVar[Mapping[str, ParamSpec]]
    static: ClassVar[bool]  # True: render once, no loop

    def render(self, length: int, t: float, params: Mapping[str, Any], seed: int) -> bytes: ...


class BaseEffect:
    """Convenience base for built-in effects: not static, no cross-param rules.

    Subclasses may override ``check_params`` to enforce rules spanning several params; ``validate_params``
    calls it (when present) after per-param coercion. Effects that don't subclass this still satisfy
    ``Effect`` and simply get no cross-param check.
    """

    static: ClassVar[bool] = False

    @classmethod
    def check_params(cls, params: Mapping[str, Any]) -> None:
        """Raise ``EffectError`` naming the offending param when a cross-param rule fails."""


def _fail(effect: type[Effect], key: str, problem: str) -> EffectError:
    return EffectError(f"{effect.name} param {key!r}: {problem}")


def _coerce_number(effect: type[Effect], key: str, spec: ParamSpec, value: object) -> float | int:
    result: float | int
    if isinstance(value, bool):
        raise _fail(effect, key, f"expected {spec.kind}, got bool {value!r}")
    if spec.kind == "float":
        if isinstance(value, int | float):
            result = float(value)
        elif isinstance(value, str):
            try:
                result = float(value)
            except ValueError:
                raise _fail(effect, key, f"expected a number, got {value!r}") from None
        else:
            raise _fail(effect, key, f"expected a number, got {value!r}")
        if not math.isfinite(result):
            raise _fail(effect, key, f"must be finite, got {value!r}")
    else:
        if isinstance(value, int):
            result = value
        elif isinstance(value, float) and value.is_integer():
            result = int(value)
        elif isinstance(value, str):
            try:
                result = int(value)
            except ValueError:
                raise _fail(effect, key, f"expected an integer, got {value!r}") from None
        else:
            raise _fail(effect, key, f"expected an integer, got {value!r}")
    if spec.min is not None and result < spec.min:
        raise _fail(effect, key, f"must be >= {spec.min:g}, got {value!r}")
    if spec.max is not None and result > spec.max:
        raise _fail(effect, key, f"must be <= {spec.max:g}, got {value!r}")
    return result


def _coerce_bool(effect: type[Effect], key: str, value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    raise _fail(effect, key, f"expected a bool, got {value!r}")


def _coerce_color(effect: type[Effect], key: str, value: object) -> RGB:
    if not isinstance(value, Sequence):
        raise _fail(effect, key, f"expected a color, got {value!r}")
    try:
        color = parse_color(cast("str | Sequence[int]", value))
    except ConfigError as err:
        raise _fail(effect, key, str(err)) from None
    if len(color) != 3:
        raise _fail(effect, key, f"effects render RGB; RGBW color {value!r} is not allowed")
    return (color[0], color[1], color[2])


def _coerce_colors(effect: type[Effect], key: str, value: object) -> list[RGB]:
    if isinstance(value, str):
        items: Sequence[object] = [part.strip() for part in value.split(",")]
    elif isinstance(value, Sequence):
        items = value
    else:
        raise _fail(effect, key, f"expected a list of colors, got {value!r}")
    if not items:
        raise _fail(effect, key, "must contain at least one color")
    return [_coerce_color(effect, key, item) for item in items]


def _coerce(effect: type[Effect], key: str, spec: ParamSpec, value: object) -> Any:
    if spec.kind in ("float", "int"):
        return _coerce_number(effect, key, spec, value)
    if spec.kind == "bool":
        return _coerce_bool(effect, key, value)
    if spec.kind == "color":
        return _coerce_color(effect, key, value)
    return _coerce_colors(effect, key, value)


def validate_params(effect: type[Effect], params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge defaults, reject unknown keys, coerce and range-check every param.

    Strings are accepted for every kind so CLI ``--param key=value`` input works: numbers (``"1.5"``, ``"3"``),
    bools (``"true"``/``"false"``), hex or named colors, and comma-separated color lists. Colors normalize to
    RGB tuples; RGBW is rejected. Raises ``EffectError`` naming the param on any failure.
    """
    given = dict(params or {})
    unknown = sorted(set(given) - set(effect.params))
    if unknown:
        raise EffectError(f"unknown param {unknown[0]!r} for effect {effect.name!r}; valid: {sorted(effect.params)}")
    merged = {key: _coerce(effect, key, spec, given.get(key, spec.default)) for key, spec in effect.params.items()}
    check = getattr(effect, "check_params", None)
    if check is not None:
        check(merged)
    return merged
