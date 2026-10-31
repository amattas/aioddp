# aioddp

aioddp is a standalone, platform-agnostic asyncio library for sending DDP
(Distributed Display Protocol) pixel data to LED controllers, with a small
deterministic effect library and a bench-test CLI. The core package has no
runtime dependencies.

!!! note "Status"
    Pre-release. Interfaces may change before 0.1.0. Formerly named `pyddp`;
    renamed because the `pyddp` name on PyPI belongs to an unrelated project.

<div class="pd-cards">
  <a class="pd-card" href="guide/cli/">
    <span class="pd-card-kicker">Guide</span>
    <span class="pd-card-title">CLI</span>
    <span class="pd-card-desc">Bench-test new hardware: query, walk a pixel, fill, run effects.</span>
  </a>
  <a class="pd-card" href="guide/effects/">
    <span class="pd-card-kicker">Guide</span>
    <span class="pd-card-title">Effects</span>
    <span class="pd-card-desc">The six deterministic effects, their params, and the EffectRunner.</span>
  </a>
  <a class="pd-card" href="compatibility/">
    <span class="pd-card-kicker">Reference</span>
    <span class="pd-card-title">Controller compatibility</span>
    <span class="pd-card-desc">Tested controllers and how to test yours.</span>
  </a>
  <a class="pd-card" href="api/reference/">
    <span class="pd-card-kicker">Reference</span>
    <span class="pd-card-title">API reference</span>
    <span class="pd-card-desc">Sender, strip and segments, effects, discovery — generated from docstrings.</span>
  </a>
</div>

## Features

- DDP v1 sender: header encode, frame chunking, push flag, sequence numbers
- Segments mapped to byte offsets, so parts of one string can be driven separately
- RGB and RGBW, configurable color order and data type byte
- Six deterministic effects and an `EffectRunner`
- Global brightness cap enforced in the library; callers can lower it but not bypass it
- Bench-test CLI for hardware bring-up

## Installation

```bash
pip install aioddp
pip install "aioddp[cli]"   # adds the aioddp command
```

Requires Python 3.12+.

## Quick start

```python
import asyncio
from aioddp import DDPSender, EffectRunner, Segment, Strip


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

## Layout discovery

Controllers that answer the DDP JSON config query describe their own outputs:

```python
from aioddp import Strip, discover

layout = await discover("10.0.40.20")
if layout:
    strip = Strip(host=layout.host, pixel_count=layout.pixel_count, data_type=layout.data_type)
    segments = layout.segments()  # one per active controller output
```

Controllers that serve their layout over HTTP (the Minleon NDB-Pro) are read by
the separate [pyminleon](https://amattas.github.io/pyminleon/) library, which can
pass the document to `aioddp.parse_config`.
