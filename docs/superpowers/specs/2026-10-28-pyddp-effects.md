# pyddp component spec: effects

Date: 2026-10-28
Status: Draft
Parent: `2026-10-28-pyddp-design.md`
Package: `effects/` (`__init__.py`, `base.py`, `solid.py`, `breathe.py`, `chase.py`, `twinkle.py`, `rainbow.py`, `palette.py`)

## Scope

Six deterministic effects, a parameter schema, and a registry. Effects know nothing about strips, sockets, gamma, or the cap: they produce canonical RGB bytes for a segment length at a time `t`. The runner (next spec) owns timing.

## Effect contract (`base.py`)

```python
class ParamSpec(NamedTuple):
    kind: Literal["float", "int", "bool", "color", "colors"]
    default: Any
    description: str
    min: float | None = None
    max: float | None = None

class Effect(Protocol):
    name: ClassVar[str]
    description: ClassVar[str]
    params: ClassVar[Mapping[str, ParamSpec]]
    static: ClassVar[bool]            # True: render once, no loop

    def render(self, length: int, t: float, params: Mapping[str, Any], seed: int) -> bytes
```

Rules every effect follows, enforced by the shared parametrized test:

- **Pure**: `render` output depends only on `(length, t, params, seed)`. No instance state that changes between calls. Internal caches keyed on those inputs are fine and expected (per-pixel phases, hue tables)
- **Output**: exactly `length * 3` bytes, canonical RGB. Effects never render W; the sender expands for RGBW strips
- **Time**: `t` is seconds since the runner started, non-negative, unbounded. Effects use modular arithmetic so `t = 1e6` renders the same as `t % period`
- **Budget**: 500 pixels in under 2 ms median on the CI runner (see testing spec). Per-pixel Python loops are allowed; per-pixel function calls and tuple allocation are what blow the budget, so effects build a `bytearray` and write channels directly
- **Params**: `render` receives already-validated, default-merged params. It never looks up defaults itself

### Parameter validation

```python
def validate_params(effect: type[Effect], params: Mapping[str, Any] | None) -> dict[str, Any]
```

Merges defaults, rejects unknown keys, coerces and range-checks by kind. `color` accepts anything `parse_color` does and normalizes to an RGB tuple (RGBW input is rejected with `EffectError` since effects render RGB). `colors` is a non-empty list of the same. `int` and `float` are range-checked against `min`/`max` when set; `bool` accepts `bool` only. Failures raise `EffectError` naming the param. The CLI's `--param key=value` strings go through this with string coercion for numbers (`"1.5"`, `"3"`, `"true"`) and comma-split hex lists for `colors`.

## Registry (`effects/__init__.py`)

```python
EFFECTS: Mapping[str, type[Effect]]   # explicit dict, no import-time magic
def get_effect(name: str) -> type[Effect]      # EffectError on unknown name, message lists valid names
def list_effects() -> list[EffectInfo]         # name, description, static, params; used by CLI and hass-ddp effect_list
```

Names are lowercase ASCII: `solid`, `breathe`, `chase`, `twinkle`, `rainbow`, `palette`. hass-ddp uses these strings verbatim as the HA `effect` values, so renaming one is a breaking change.

## The six effects

Colors below are RGB tuples. Defaults are chosen to look right on a roofline at 30 fps without tuning.

### solid (static)

| Param | Kind | Default |
|---|---|---|
| `color` | color | `(255, 255, 255)` |

`render` ignores `t` and `seed`. Exists so "effect = solid" and "no effect" are the same thing in hass-ddp.

### breathe

| Param | Kind | Default | Range |
|---|---|---|---|
| `color` | color | `(255, 255, 255)` | |
| `period` | float | `4.0` | 0.5 to 60 |
| `min_level` | float | `0.05` | 0.0 to 1.0 |

`level = min_level + (1 - min_level) * (0.5 - 0.5 * cos(2π t / period))`. Whole segment is `scale_color(color, level)`. One `scale_color` call per frame, then `bytes * length`; this is the cheapest effect.

### chase (theater chase)

| Param | Kind | Default | Range |
|---|---|---|---|
| `colors` | colors | `[(255, 255, 255)]` | |
| `speed` | float | `8.0` | -100 to 100, pixels per second; negative runs backwards |
| `spacing` | int | `3` | 2 to 50 |
| `width` | int | `1` | 1 to `spacing` |
| `background` | color | `(0, 0, 0)` | |

`shift = floor(t * speed) mod spacing`. Pixel `i` is lit when `(i - shift) mod spacing < width`. A lit pixel's color is `colors[((i - shift) // spacing) mod len(colors)]` so multi-color chases keep their color blocks moving with the chase. Unlit pixels get `background`. `width > spacing` is a validation error (checked in `validate_params` as a cross-param rule).

### twinkle

| Param | Kind | Default | Range |
|---|---|---|---|
| `base_color` | color | `(0, 0, 0)` | |
| `sparkle_color` | color | `(255, 255, 255)` | |
| `density` | float | `0.15` | 0.0 to 1.0, fraction of pixels that twinkle |
| `speed` | float | `1.0` | 0.05 to 10, twinkle cycles per second |

Per `(length, seed)` the effect computes and caches, with `random.Random(seed)`, a phase `φ_i ∈ [0, 1)` and a selection value `s_i ∈ [0, 1)` for each pixel. Pixel `i` twinkles when `s_i < density`; its level is a triangle wave `tri((t * speed + φ_i) mod 1)` rising 0 to 1 to 0, and its color is `lerp_color(base, sparkle, level)`. Non-twinkling pixels are `base_color`. Cache: `functools.lru_cache(maxsize=32)` on a module function `_tables(length, seed)`. Changing `density` or `speed` through `update` does not change which pixels could twinkle, only how many of the pre-rolled ones do, which keeps updates visually continuous.

### rainbow

| Param | Kind | Default | Range |
|---|---|---|---|
| `speed` | float | `0.1` | -5 to 5, hue rotations per second |
| `spread` | float | `1.0` | 0.01 to 20, hue cycles across the segment |
| `saturation` | float | `1.0` | 0.0 to 1.0 |

`hue_i = (i / length * spread + t * speed) mod 1`. RGB comes from a module-level 1536-entry hue wheel built once at import (`hsv_to_rgb(h, 1, 1)` sampled); saturation below 1.0 lerps toward white per pixel. Index math is integer: `idx = int(hue_i * 1536)`.

### palette (static)

| Param | Kind | Default | Range |
|---|---|---|---|
| `colors` | colors | `[(255, 0, 0), (0, 255, 0)]` | |
| `block_size` | int | `1` | 1 to 1000 |

Pixel `i` is `colors[(i // block_size) mod len(colors)]`. Holiday and team-color fills. Static, so the runner sends it once.

## Determinism

`seed` defaults to 0 in the runner when the caller passes `None`, so two runners with the same inputs produce identical frames. Only `twinkle` consumes the seed in v1; the others accept and ignore it. Tests render every effect at `t` in `{0, 0.5, 123456.789}` with a fixed seed and compare against golden bytes checked into `tests/golden/<effect>_<t>.bin` plus a `.txt` of the params. Regenerating goldens is an explicit `python -m tests.golden --write` step, never automatic.

## Tests (`tests/test_effects.py`)

One parametrized test over `EFFECTS.values()` for each contract rule:

- Output length is `length * 3` for lengths 1, 2, 300
- Same inputs twice give identical bytes; different seeds differ only for `twinkle`
- `t` and `t + 10_000 * period_equivalent` match within one frame's rounding (periodicity)
- Golden bytes for default params at three `t` values
- `validate_params`: defaults fill in, unknown key rejected, each kind's bad value rejected, `width > spacing` rejected, CLI string coercion works
- Budget: median of 50 renders at 500 pixels under the threshold in the testing spec

Effect-specific tests are few: chase pixel pattern at `t=0` and after one shift, breathe level at `t=0` and `t=period/2`, twinkle density produces roughly `density * length` twinkling pixels at seed 0 (asserted within ±30%), rainbow pixel 0 at `t=0` is red, palette block boundaries.
