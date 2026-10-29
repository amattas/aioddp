# pyddp design spec

Date: 2026-10-28
Status: Draft
Companion: `hass-ddp/docs/superpowers/specs/2026-10-28-hass-ddp-design.md`

## Component specs

This document is the overview. Each module has a detail spec in this directory; where the two disagree, the component spec wins and this one needs updating.

| Spec | Covers |
|---|---|
| `2026-10-28-pyddp-protocol.md` | `const.py`, `exceptions.py`, `protocol.py`: header, data type byte, chunking, sequence, golden vectors |
| `2026-10-28-pyddp-strip-segment.md` | `strip.py`: `Strip` and `Segment` models and validation rules |
| `2026-10-28-pyddp-color.md` | `color.py`: pipeline stages, LUT, cap enforcement, color helpers |
| `2026-10-28-pyddp-sender.md` | `sender.py`: lifecycle, `send_frame`, shadow buffer, keepalive, stats, error policy |
| `2026-10-28-pyddp-effects.md` | `effects/`: effect contract, param schema, registry, the six v1 effects |
| `2026-10-28-pyddp-runner.md` | `runner.py`: `EffectRunner` loop, cancellation, `update`, `fade` |
| `2026-10-28-pyddp-cli.md` | `cli.py`: commands, config file, exit codes |
| `2026-10-28-pyddp-testing.md` | fixtures, benchmark gate, file map, public exports, hardware checklist |

## Summary

pyddp is a standalone, platform-agnostic, asyncio Python library that sends DDP (Distributed Display Protocol) pixel data to LED controllers and renders effects as pure functions. It is the protocol half of a pydmp / hass-dmp style pair: hass-ddp imports it and contains no protocol code. If bytes are being built anywhere else, that code belongs here.

First target is a Minleon NDB-Pro driving Rainmin RGB+ pixels on a roofline, but nothing in the library is Minleon-specific.

### What this is NOT

- Not a Home Assistant integration (hass-ddp is)
- Not a DDP receiver
- Not an E1.31, Art-Net, or DMX sender
- Not an xLights sequence player
- Not audio-reactive
- Not a WLED effect-library rewrite

## Goals

- Correct DDP v1 sender: header encode, frame chunking, push flag, sequence numbers
- Segments mapped to byte offsets so independent parts of one string can be driven separately
- A small, deterministic effect library
- Global brightness cap enforced in the library so no caller can bypass it
- CLI for bench testing a controller with no Home Assistant in the loop
- Zero runtime dependencies for the core package

## DDP protocol summary

UDP to port 4048. A 10-byte header followed by raw pixel bytes. A frame larger than one packet is split by byte offset; the push flag on the last packet tells the receiver to latch the frame.

| Bytes | Field | What we send |
|---|---|---|
| 0 | Flags | Version 1 (`0x40`), plus push (`0x01`) on the last packet of a frame. No timecode, storage, query, or reply bits |
| 1 | Sequence | Low 4 bits, cycling 1 to 15; 0 means unused |
| 2 | Data type | `0x0B` for 8-bit RGB, `0x1B` for 8-bit RGBW. Configurable, since some receivers expect legacy `0x01` or ignore the field |
| 3 | Destination ID | 1 (default output device) |
| 4 to 7 | Data offset | Byte offset into the receiver's buffer, big-endian |
| 8 to 9 | Data length | Payload bytes in this packet, big-endian |
| 10+ | Data | Pixel bytes in the configured channel order |

- Max payload per packet: 1440 bytes (480 RGB pixels, 360 RGBW). Larger frames go out as several packets with increasing offsets, push set only on the last.
- No priority, no acknowledgement. Two senders on one controller fight; only one source talks at a time.
- Segments are byte ranges. A segment can be sent as its own packet(s) at its own offset without merging frames. To latch several segments together, send them without push and push once at the end.

## Architecture

```
pyddp/
├── src/pyddp/
│   ├── __init__.py       # Public exports
│   ├── protocol.py       # Header encode/decode, flags, data types, chunking
│   ├── sender.py         # DDPSender: async UDP endpoint, sequence, send_frame, keepalive
│   ├── strip.py          # Strip and Segment models, validation
│   ├── color.py          # Gamma, brightness scaling, Kelvin to RGB, brightness cap, color order
│   ├── effects/          # base.py (contract, params), __init__.py (registry), one module per effect
│   ├── runner.py         # EffectRunner: async task rendering one segment at a target fps; fade()
│   ├── const.py          # Port, header sizes, flag bits, data type codes, defaults
│   ├── exceptions.py     # PyDDPError hierarchy
│   └── cli.py            # pyddp command (click + rich), [cli] extra
└── tests/
```

### Core classes

- `Strip(host, pixel_count, port=4048, data_type=RGB, color_order="RGB", brightness_cap=1.0)`
- `Segment(name, start, length, reverse=False)`: validated against the strip (no overlap required, but out-of-range is an error)
- `DDPSender(strip, keepalive_interval=None)`: async context manager. `send_frame(pixels, segment=None, brightness=1.0, push=True)`, `fill(segment, color, brightness)`, `off(segment)`, `push()`, `close()`. Owns the sequence counter, a shadow buffer of the last wire bytes, and the optional keepalive task. There is no raw-bytes send: `send_frame` takes canonical RGB(W) pixels and always runs the color pipeline, which is how the cap is enforced
- `EffectRunner(sender, segment, effect, fps=30, params=None, brightness=1.0, seed=None)`: `start()`, `stop()`, `update(params=, brightness=, fps=)`, `wait()`; cancels cleanly and never leaves an orphaned task. `fade(sender, segment, start, end, duration)` is the transition helper
- `Effect` protocol: `render(length: int, t: float, params: Mapping, seed: int) -> bytes` returning `length * 3` canonical RGB bytes, plus a declared `params` schema with defaults and a `static` flag

### Public API sketch

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

Host and pixel counts above are placeholders.

## Color pipeline

Every frame, from any caller, goes through the same stage before it hits the socket:

1. Effect or fill produces linear RGB(W) per pixel
2. Per-call brightness applied
3. Gamma correction (table, configurable, default 2.2; can be disabled if the controller already applies it)
4. Global brightness cap applied
5. Reorder to the strip's color order
6. Optional segment reverse

The cap lives in step 4 and is not optional at the API level: callers can lower it, not skip it. This protects long runs without power injection from voltage drop and color shift.

Kelvin to RGB uses a standard approximation for 2000 to 6500 K. Color temp on RGB-only pixels is an approximation and the docs say so.

## Effects (v1)

Each effect is a pure function of segment length, time, and params, deterministic given a seed so tests can assert exact bytes.

| Effect | Params |
|---|---|
| Solid | color. Not a loop: render once, send, keepalive only if enabled |
| Breathe | color, period |
| Chase / theater chase | colors, speed, spacing |
| Twinkle | base color, sparkle color, density, seed |
| Rainbow | speed, spread |
| Palette fill | list of colors, block size (static; holiday, team colors) |

Effects must render a few hundred pixels in well under 1 ms. A benchmark test enforces a budget so hass-ddp can run them on the HA event loop without stalling it.

## Sender behavior

- `asyncio` datagram endpoint, no threads
- Sequence counter cycles 1 to 15 per sender
- Frame chunking at 1440 bytes, push on the last packet only
- Optional keepalive: resend the last full frame every N seconds when nothing else has been sent. Off by default; enabled only if the controller blanks without data
- `close()` stops keepalive and closes the socket. It does not blank the lights; blanking is an explicit `off()`
- Send errors raise `DDPSendError`; transient `OSError` on UDP send is logged and counted, not fatal, so one dropped packet never kills an effect

## CLI

Installed via the `[cli]` extra (click, rich, pyyaml), same pattern as pydmp. Optional `config.yaml` (gitignored) for host and pixel count.

- `pyddp fill <host> --count N --color ff8800 [--brightness 0.5] [--cap 0.6]`
- `pyddp effect <host> --count N --effect chase --fps 30 [--param speed=1.0]`
- `pyddp off <host> --count N`
- `pyddp test <host> --count N`: walks a single lit pixel down the string to confirm pixel count, channel order, and segment boundaries. First thing to run against real hardware
- `pyddp effects`: list effects and their params

## Error handling and logging

- Exception hierarchy rooted at `PyDDPError`: `ConfigError`, `DDPSendError`, `EffectError`
- Input validation: pixel count > 0, segments in range, brightness and cap in 0.0 to 1.0, fps in 1 to 60, data type in the supported set
- Standard `logging`; never log full frames above DEBUG

## Testing

- pytest + pytest-asyncio (`asyncio_mode = "auto"`), pytest-cov
- Golden-byte tests for header encoding, chunking, offsets, push flag, sequence wrap
- Local UDP listener fixture to assert exactly what the sender puts on the wire
- Effects tested as pure functions with fixed seeds, no network
- Render-time budget test per effect
- Coverage target 80%+ overall, 95%+ on `protocol.py` and `color.py`

## Repo conventions (mirror pydmp)

- `src/pyddp` layout, hatchling build backend
- `requires-python = ">=3.12"`, Apache-2.0 license, author Anthony Mattas
- `dependencies = []`; dev, cli, docs extras with exact pins
- black (line length 120), ruff (`E, F, I, B, UP, SIM, S`, `S101` ignored in tests), mypy strict pinned to 3.12, bandit, pre-commit
- `.editorconfig`, `.gitignore` copied from pydmp (adds `config.yaml`)
- `CLAUDE.md` and `AGENTS.md` at the root, same structure as pydmp's
- Docs: Zensical + mike, published to `https://amattas.github.io/pyddp/`, API reference generated with pydoc-markdown, `scripts/publish_versioned_docs.py` copied from pydmp
- GitHub: `ci.yml`, `docs.yml`, `release.yml` manual-only; release from an `X.Y.Z` tag on main, PyPI trusted publishing via the `pypi` environment with a reviewer gate. `CODEOWNERS`, `dependabot.yml`, issue templates (swap `panel_compatibility` for `controller_compatibility`)
- `docs/compatibility.md` lists controllers tested and what data type byte and timeout behavior each one has

## Milestones

1. **Core:** protocol, sender, color pipeline with cap, `fill`, `off`, `test` CLI. Done when the CLI lights the real string at the right count and order
2. **Effects:** registry, six v1 effects, `EffectRunner`, `effect` CLI, budget tests
3. **Release 0.1.0:** docs site, PyPI publish, compatibility page with the NDB-Pro

Maintenance-only after 0.1.0 until school is done.

## Open questions

- [ ] Is `pyddp` free on PyPI? If not, pick a name before scaffolding
- [ ] Does the NDB-Pro honor the data type byte, or accept anything?
- [ ] Does the NDB-Pro treat DDP offsets as one flat buffer across its 8 outputs, or per output?
- [ ] What does the NDB-Pro do when DDP stops: hold, blank, or fall back to built-in effects, and after how long? Decides the keepalive default
- [ ] Does the NDB-Pro apply its own gamma? Decides whether ours is on by default
- [ ] Is a sync wrapper worth it (pydmp has one), or is the CLI enough for scripting? The sender spec recommends dropping it from 0.1.0; `sender_sync.py` is out of the tree until someone asks

The first five are answered by the hardware checklist at the end of the testing spec and recorded in `docs/compatibility.md`.
