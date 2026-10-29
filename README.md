# pyddp

**Asyncio Python library for sending DDP pixel data to LED controllers**

[![CI](https://github.com/amattas/pyddp/actions/workflows/ci.yml/badge.svg)](https://github.com/amattas/pyddp/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

pyddp is a standalone, platform-agnostic library for sending DDP (Distributed Display Protocol) pixel data to LED controllers, with a small deterministic effect library and a bench-test CLI.

> Status: pre-release. Interfaces may change before 0.1.0.

## Features

- DDP v1 sender: header encode, frame chunking, push flag, sequence numbers
- Segments mapped to byte offsets, so parts of one string can be driven separately
- RGB and RGBW, configurable color order and data type byte
- Six deterministic effects and an `EffectRunner`
- Global brightness cap enforced in the library; callers can lower it but not bypass it
- Bench-test CLI for hardware bring-up
- No runtime dependencies for the core package

## Installation

```bash
pip install pyddp
pip install "pyddp[cli]"   # adds the pyddp command
```

Requires Python 3.12+.

## Library Quick Start

```python
import asyncio
from pyddp import DDPSender, EffectRunner, Segment, Strip

async def main():
    strip = Strip(host="10.0.40.20", pixel_count=300, brightness_cap=0.6)
    roof = Segment("roofline", start=0, length=260)
    peak = Segment("peak", start=260, length=40)

    async with DDPSender(strip) as tx:
        await tx.fill(roof, rgb=(255, 140, 40), brightness=0.6)
        runner = EffectRunner(tx, peak, effect="chase", fps=30, params={"speed": 1.0})
        await runner.start()
        await asyncio.sleep(10)
        await runner.stop()

asyncio.run(main())
```

Host and pixel counts are placeholders.

## CLI Quick Start

```bash
pyddp --host 10.0.40.20 query                           # ask the controller for its outputs; prints a config.yaml
pyddp --host 10.0.40.20 --count 300 test                # walk a lit pixel down the string
pyddp --host 10.0.40.20 --count 300 fill --color ff8800 --brightness 0.5
pyddp --host 10.0.40.20 --count 300 effect chase --param speed=8
pyddp --host 10.0.40.20 --count 300 off
pyddp effects                                           # list effects and params
```

Run `pyddp query` first against new hardware, then `pyddp test`. `query` uses the DDP JSON config query and falls back to the same document over HTTP, which is what the Minleon NDB-Pro serves; a controller that answers neither is configured by hand. Host and count can also come from `config.yaml`; see `docs/superpowers/specs/2026-10-28-pyddp-cli.md`.

```python
from pyddp import discover

layout = await discover("10.0.40.20")
if layout:
    strip = Strip(host=layout.host, pixel_count=layout.pixel_count, data_type=layout.data_type)
    segments = layout.segments()  # one per active controller output
```

## Effects

| Effect | Params |
|---|---|
| `solid` (static) | `color` |
| `breathe` | `color`, `period`, `min_level` |
| `chase` | `colors`, `speed`, `spacing`, `width`, `background` |
| `twinkle` | `base_color`, `sparkle_color`, `density`, `speed` |
| `rainbow` | `speed`, `spread`, `saturation` |
| `palette` (static) | `colors`, `block_size` |

Effects are pure functions of segment length, time, params, and seed. `twinkle` uses the seed; the others ignore it.

## Compatibility

See [docs/compatibility.md](docs/compatibility.md) for tested controllers and how to test yours.

## Specs

Design and component specs are in `docs/superpowers/specs/`.

## License

Apache-2.0. See [LICENSE](LICENSE).
