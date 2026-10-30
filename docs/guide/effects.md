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

## Render budget

Every effect renders 500 pixels with a median under 2 ms, enforced by the test
suite, so a 30 fps runner leaves the event loop almost entirely free.
