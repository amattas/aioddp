# pyddp - Python Library for DDP LED Controllers

## Project Overview

**Purpose**: Standalone, platform-agnostic asyncio Python library that sends DDP (Distributed Display Protocol) pixel data to LED controllers.

**Features**:
- Correct DDP v1 sender: header encode, chunking, push flag, sequence numbers
- Strips and segments mapped to byte offsets
- Six deterministic effects and an `EffectRunner`
- Global brightness cap enforced in the library
- Bench-test CLI (`pyddp`, `[cli]` extra)
- Zero runtime dependencies for the core package

First target: Minleon NDB-Pro driving Rainmin RGB+ pixels. Nothing in the library is Minleon-specific.

### What This Is NOT
- Not a Home Assistant integration (hass-ddp is; it imports pyddp and holds no protocol code)
- Not a DDP receiver (the only thing it reads is a JSON reply to its own layout query)
- Not an E1.31, Art-Net, or DMX sender
- Not an xLights sequence player
- Not audio-reactive
- Not a WLED effect-library rewrite

If bytes are being built anywhere else, that code belongs here.

## Quick Start

```python
from pyddp import DDPSender, EffectRunner, Segment, Strip

strip = Strip(host="10.0.40.20", pixel_count=300, brightness_cap=0.6)
roof = Segment("roofline", start=0, length=260)
peak = Segment("peak", start=260, length=40)

async with DDPSender(strip) as tx:
    await tx.fill(roof, rgb=(255, 140, 40), brightness=0.6)
    runner = EffectRunner(tx, peak, effect="chase", fps=30, params={"speed": 1.0})
    await runner.start()
```

Host and pixel counts are placeholders. There is no sync API in 0.1.0.

## Architecture Overview

```
pyddp/
├── src/pyddp/
│   ├── __init__.py       # Public exports
│   ├── protocol.py       # Header encode/decode, flags, data types, chunking
│   ├── sender.py         # DDPSender: async UDP endpoint, sequence, send_frame, keepalive
│   ├── strip.py          # Strip and Segment models, validation
│   ├── color.py          # Gamma, brightness, Kelvin, brightness cap, color order
│   ├── effects/          # base.py (contract, params), registry, one module per effect
│   ├── runner.py         # EffectRunner, fade()
│   ├── discovery.py      # ControllerLayout from a DDP JSON config query or the same document over HTTP
│   ├── const.py          # Port, header sizes, flag bits, data type codes, defaults
│   ├── exceptions.py     # PyDDPError hierarchy
│   └── cli.py            # pyddp command (click + rich), [cli] extra
├── tests/
├── scripts/
└── docs/
    ├── compatibility.md
    └── superpowers/specs/   # Design and component specs (source of truth)
```

**Core classes**:
- `Strip(host, pixel_count, port=4048, data_type, color_order, brightness_cap)`: validated config
- `Segment(name, start, length, reverse=False)`: byte-range view of a strip
- `DDPSender(strip, keepalive_interval=None)`: `send_frame`, `fill`, `off`, `push`, `close`. No raw-bytes send path
- `EffectRunner(sender, segment, effect, fps, params, brightness, seed)`: `start`, `stop`, `update`, `wait`
- `Effect`: pure `render(length, t, params, seed) -> bytes` of canonical RGB
- `discover(host) -> ControllerLayout | None`: pixel count and one `OutputInfo` per output from a DDP query (destinations 250/251) or the same JSON over HTTP; `layout.segments()` is one `Segment` per active output. `DDPSender.query(destination)` is the raw read

Exceptions: `PyDDPError` > `ConfigError`, `DDPSendError`, `EffectError`, `DDPQueryError`.

Discovery is best effort and the only vendor-aware code: `fetch_layout_http` defaults to the `/api/config` and `/api/status` paths the Minleon NDB-Pro serves, parsing the DDP-spec JSON document. No scraping, no vendor-specific protocol code.

## DDP Protocol Essentials

UDP to port 4048. 10-byte header followed by raw pixel bytes.

| Bytes | Field | What we send |
|-------|-------|--------------|
| 0 | Flags | Version 1 (`0x40`), plus push (`0x01`) on the last packet of a frame |
| 1 | Sequence | Low 4 bits, cycling 1 to 15 (0 = unused) |
| 2 | Data type | `0x0B` 8-bit RGB, `0x1B` 8-bit RGBW; configurable |
| 3 | Destination ID | 1 (default output device) |
| 4-7 | Data offset | Byte offset, big-endian |
| 8-9 | Data length | Payload bytes in this packet, big-endian |
| 10+ | Data | Pixel bytes in the strip's channel order |

- Max payload 1440 bytes per packet (480 RGB or 360 RGBW pixels); push only on the last packet
- Query flag (`0x02`) to destination 250 (JSON config) or 251 (JSON status) asks for a reply (flag `0x04`, push on the last packet). Optional; the NDB-Pro ignores it and serves the same JSON over HTTP instead
- No acknowledgement, no priority: one sender per controller
- Segments are byte ranges; to latch several together, send without push and push once at the end
- `close()` does not blank the lights; blanking is an explicit `off()`

## Color Pipeline

Every frame from any caller passes through `color.process_frame`:

1. Effect or fill produces linear RGB(W)
2. Per-call brightness
3. Gamma (LUT, default 2.2, can be disabled)
4. Global brightness cap
5. Reorder to the strip's color order
6. Optional segment reverse

**The cap is not bypassable.** It lives in step 4, `DDPSender` has no path to the socket that skips `process_frame`, and callers can lower the cap but not skip it. Do not add a raw-send escape hatch.

## Development

Python env is the conda env `pyddp` (never base, never venv).

```bash
conda run -n pyddp python -m pytest -q
conda run -n pyddp ruff check .
conda run -n pyddp black --check .
conda run -n pyddp mypy
conda run -n pyddp python scripts/check_coverage.py
```

Coverage gates: 80% total, 95% on `protocol.py` and `color.py`.

## Testing Approach

- pytest + pytest-asyncio (`asyncio_mode = "auto"`). TDD; parametrize variations of one behavior
- `udp_listener` fixture: real asyncio UDP socket on loopback; sender and CLI tests assert the exact bytes on the wire, no mocking
- Golden bytes for headers, chunking, offsets, push flag, sequence wrap, and effect frames (`tests/golden/`, regenerate only via `python -m tests.golden --write`)
- Effects tested as pure functions with fixed seeds
- Render budget: median under 2 ms for 500 pixels per effect, scaled by `PYDDP_BENCH_FACTOR` (CI sets 3)
- `no_leaked_tasks` autouse fixture enforces no orphaned asyncio tasks

## Source of Truth

Design and per-component specs live in `docs/superpowers/specs/` (start with `2026-10-28-pyddp-design.md`). Where a component spec and the design doc disagree, the component spec wins and the design doc needs updating. Controller test results go in `docs/compatibility.md`.

## Technical Decisions

- `src/pyddp` layout, hatchling, `requires-python >=3.12`, `dependencies = []`
- Extras: `cli` (click, rich, pyyaml), `dev`
- black line length 120; ruff `E, F, I, B, UP, SIM, S`; mypy strict; bandit
- Standard `logging`; never log full frames above DEBUG
- hass-ddp imports only from `pyddp`, never from submodules

## License

Apache-2.0
