"""Effect contract tests parametrized over the registry, goldens, params, budget, and a few per-effect checks."""

from __future__ import annotations

import math
import os
import statistics
import time
from collections.abc import Mapping
from typing import Any, ClassVar

import pytest

from pyddp.effects import EFFECTS, EffectInfo, get_effect, list_effects
from pyddp.effects.base import ParamSpec, validate_params
from pyddp.exceptions import EffectError
from tests import golden

NAMES = sorted(EFFECTS)
BLACK, WHITE, RED, GREEN = (0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 255, 0)

# One full cycle of each effect at its default params, in seconds (static effects: any value).
PERIOD_AT_DEFAULTS = {"solid": 1.0, "breathe": 4.0, "chase": 3 / 8, "twinkle": 1.0, "rainbow": 10.0, "palette": 1.0}


def _render(name: str, length: int, t: float, params: Mapping[str, Any] | None = None, seed: int = 0) -> bytes:
    effect = EFFECTS[name]
    return effect().render(length, t, validate_params(effect, params), seed)


def _pixels(frame: bytes) -> list[tuple[int, ...]]:
    return [tuple(frame[i : i + 3]) for i in range(0, len(frame), 3)]


class _Probe:
    """One param of every kind; deliberately not a BaseEffect, so it has no cross-param hook."""

    name: ClassVar[str] = "probe"
    description: ClassVar[str] = "validation probe"
    static: ClassVar[bool] = False
    params: ClassVar[Mapping[str, ParamSpec]] = {
        "f": ParamSpec("float", 1.0, "a float", 0.0, 10.0),
        "i": ParamSpec("int", 3, "an int", 1, 50),
        "b": ParamSpec("bool", False, "a bool"),
        "c": ParamSpec("color", (0, 0, 0), "a color"),
        "cs": ParamSpec("colors", ((255, 0, 0),), "colors"),
    }

    def render(self, length: int, t: float, params: Mapping[str, Any], seed: int) -> bytes:
        return bytes(length * 3)


# --- registry ---------------------------------------------------------------


def test_registry_names_and_lookup() -> None:
    assert NAMES == ["breathe", "chase", "palette", "rainbow", "solid", "twinkle"]
    assert all(get_effect(name) is EFFECTS[name] and EFFECTS[name].name == name for name in NAMES)
    infos = list_effects()
    assert infos == [EffectInfo(e.name, e.description, e.static, e.params) for e in EFFECTS.values()]
    assert {info.name for info in infos if info.static} == {"solid", "palette"}
    assert set(PERIOD_AT_DEFAULTS) == set(EFFECTS)
    with pytest.raises(EffectError, match="unknown effect 'sparkle'.*breathe.*twinkle"):
        get_effect("sparkle")


# --- contract rules, one parametrized test each -----------------------------


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("length", [1, 2, 300])
def test_output_length(name: str, length: int) -> None:
    assert len(_render(name, length, 0.5)) == length * 3


@pytest.mark.parametrize("name", NAMES)
def test_deterministic_and_only_twinkle_uses_seed(name: str) -> None:
    assert _render(name, 300, 0.5, seed=7) == _render(name, 300, 0.5, seed=7)
    seeds_differ = _render(name, 300, 0.5, seed=0) != _render(name, 300, 0.5, seed=1)
    assert seeds_differ is (name == "twinkle")


@pytest.mark.parametrize("name", NAMES)
def test_periodic_in_t(name: str) -> None:
    t = 0.3
    later = t + 10_000 * PERIOD_AT_DEFAULTS[name]
    a, b = _render(name, 300, t), _render(name, 300, later)
    assert max(abs(x - y) for x, y in zip(a, b, strict=True)) <= 1


@pytest.mark.parametrize(("name", "t"), golden.CASES, ids=[f"{n}-{t!r}" for n, t in golden.CASES])
def test_golden_frames(name: str, t: float) -> None:
    assert golden.check_case(name, t) == []


@pytest.mark.parametrize("name", NAMES)
def test_render_budget(name: str) -> None:
    effect = EFFECTS[name]
    instance, params = effect(), validate_params(effect, None)
    for k in range(5):
        instance.render(500, k / 30, params, 0)
    samples = []
    for k in range(50):
        start = time.perf_counter()
        instance.render(500, (5 + k) / 30, params, 0)
        samples.append(time.perf_counter() - start)
    median_ms, max_ms = statistics.median(samples) * 1e3, max(samples) * 1e3
    limit_ms = 2.0 * float(os.environ.get("PYDDP_BENCH_FACTOR", "1.0"))
    print(f"{name:8s} median {median_ms:.3f} ms  max {max_ms:.3f} ms  limit {limit_ms:.1f} ms")
    assert median_ms < limit_ms


# --- validate_params ----------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_validate_fills_defaults(name: str) -> None:
    effect = EFFECTS[name]
    merged = validate_params(effect, None)
    assert merged == validate_params(effect, {})
    expected = {k: list(s.default) if s.kind == "colors" else s.default for k, s in effect.params.items()}
    assert merged == expected


@pytest.mark.parametrize("name", NAMES)
def test_validate_rejects_unknown_key(name: str) -> None:
    with pytest.raises(EffectError, match="bogus"):
        validate_params(EFFECTS[name], {"bogus": 1})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("f", "fast"),
        ("f", True),
        ("f", math.nan),
        ("f", 10.5),
        ("f", [1.0]),
        ("i", 2.5),
        ("i", "3.5"),
        ("i", True),
        ("i", 0),
        ("i", 51),
        ("b", 1),
        ("b", "maybe"),
        ("c", "nothex"),
        ("c", (255, 0, 0, 0)),
        ("c", 7),
        ("cs", []),
        ("cs", ""),
        ("cs", "ff0000,zz"),
        ("cs", [(1, 2, 3, 4)]),
        ("cs", 5),
    ],
)
def test_validate_rejects_bad_value_naming_param(key: str, value: object) -> None:
    with pytest.raises(EffectError, match=f"'{key}'"):
        validate_params(_Probe, {key: value})


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("f", "1.5", 1.5),
        ("f", 2, 2.0),
        ("i", "3", 3),
        ("i", 4.0, 4),
        ("b", "true", True),
        ("b", "False", False),
        ("c", "#ff0000", RED),
        ("c", [0, 255, 0], GREEN),
        ("cs", "ff0000, 00ff00", [RED, GREEN]),
        ("cs", ["white", (0, 0, 0)], [WHITE, BLACK]),
    ],
)
def test_validate_coerces(key: str, value: object, expected: object) -> None:
    result = validate_params(_Probe, {key: value})[key]
    assert result == expected
    assert type(result) is type(expected)


@pytest.mark.parametrize(("width", "ok"), [(3, True), (4, False)])
def test_validate_chase_width_within_spacing(width: int, ok: bool) -> None:
    params = {"width": width, "spacing": "3"}
    if ok:
        assert validate_params(get_effect("chase"), params)["width"] == width
    else:
        with pytest.raises(EffectError, match="'width'.*spacing"):
            validate_params(get_effect("chase"), params)


# --- effect-specific ----------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "t", "expected"),
    [
        ({}, 0.0, [WHITE, BLACK, BLACK, WHITE, BLACK, BLACK]),
        ({}, 1 / 8, [BLACK, WHITE, BLACK, BLACK, WHITE, BLACK]),
        ({"speed": -8.0}, 1 / 8, [BLACK, BLACK, WHITE, BLACK, BLACK, WHITE]),
        ({"width": 2}, 0.0, [WHITE, WHITE, BLACK, WHITE, WHITE, BLACK]),
        # Color blocks travel with the chase rather than snapping back every `spacing` steps.
        ({"colors": "ff0000,00ff00", "spacing": 2, "speed": 1.0}, 0.0, [RED, BLACK, GREEN, BLACK, RED, BLACK]),
        ({"colors": "ff0000,00ff00", "spacing": 2, "speed": 1.0}, 2.0, [GREEN, BLACK, RED, BLACK, GREEN, BLACK]),
    ],
)
def test_chase_pattern(params: dict[str, Any], t: float, expected: list[tuple[int, int, int]]) -> None:
    assert _pixels(_render("chase", 6, t, params)) == expected


@pytest.mark.parametrize(("t", "expected"), [(0.0, (13, 13, 13)), (2.0, WHITE)])
def test_breathe_level(t: float, expected: tuple[int, int, int]) -> None:
    assert set(_pixels(_render("breathe", 4, t))) == {expected}


@pytest.mark.parametrize("density", [0.15, 0.5])
def test_twinkle_density(density: float) -> None:
    length = 1000
    lit: set[int] = set()
    for step in range(10):
        frame = _pixels(_render("twinkle", length, step / 10 + 0.05, {"density": density}))
        lit.update(i for i, px in enumerate(frame) if px != BLACK)
    assert abs(len(lit) - density * length) <= 0.3 * density * length


@pytest.mark.parametrize(("saturation", "expected"), [(1.0, RED), (0.5, (255, 128, 128)), (0.0, WHITE)])
def test_rainbow_pixel_zero_at_t0(saturation: float, expected: tuple[int, int, int]) -> None:
    assert _pixels(_render("rainbow", 300, 0.0, {"saturation": saturation}))[0] == expected


def test_palette_block_boundaries() -> None:
    frame = _render("palette", 5, 0.0, {"colors": [RED, GREEN], "block_size": 2})
    assert _pixels(frame) == [RED, RED, GREEN, GREEN, RED]
