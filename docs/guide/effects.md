# Effects

Effects are pure functions of segment length, time, params, and seed, so a frame
is reproducible. `twinkle` uses the seed; the others ignore it. Run one on a
segment with `EffectRunner`, or from the CLI with `aioddp effect <name>`.

| Effect | Params |
|---|---|
| `solid` (static) | `color` |
| `breathe` | `color`, `period`, `min_level` |
| `chase` | `colors`, `speed`, `spacing`, `width`, `background` |
| `twinkle` | `base_color`, `sparkle_color`, `density`, `speed` |
| `rainbow` | `speed`, `spread`, `saturation` |
| `palette` (static) | `colors`, `block_size` |

`aioddp effects` lists every effect with its params and defaults.

## RGBW

The static effects `solid` and `palette` accept RGBW colors (`"ff000080"` or a
4-tuple) on a 4-channel strip; `palette` pads any RGB colors in the list with
W = 0. Every animated effect renders RGB only and rejects RGBW colors. An RGBW
color on a 3-channel strip raises `EffectError` when the runner is built or
updated. From the CLI: `aioddp --type rgbw effect palette --param colors=ff000080,00ff00`.

## Render budget

Every effect renders 500 pixels with a median under 2 ms, enforced by the test
suite, so a 30 fps runner leaves the event loop almost entirely free.
