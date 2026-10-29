# pyddp component spec: color pipeline

Date: 2026-10-28
Status: Draft
Parent: `2026-10-28-pyddp-design.md`
Module: `color.py`

## Scope

Pure functions that turn canonical pixel bytes into wire bytes for one strip, plus color helpers (Kelvin, hex, HSV). No I/O, no asyncio. Coverage target 95%+. This module is the single enforcement point for the brightness cap: `DDPSender` has no public path to the socket that skips `process_frame`.

## Canonical pixel bytes

Every producer (effects, `fill`, hass-ddp, the CLI) hands the sender a `bytes` of `length * channels` where:

- Channel order is canonical: R, G, B, then W for RGBW strips
- Values are 0 to 255, linear intent (what the caller wants to see, before gamma)
- Pixel 0 is the first pixel of the segment in logical order; `reverse` is the strip's problem

Effects always render 3 channels. `expand_rgb_to_rgbw` inserts `W = 0` after every triplet when the strip is RGBW. White on RGBW strips is only lit by explicit RGBW colors passed to `fill` or by a future RGBW-aware effect.

## process_frame

```python
def process_frame(
    pixels: bytes,
    strip: Strip,
    *,
    brightness: float = 1.0,
    reverse: bool = False,
) -> bytes
```

Stages, in order:

1. **Validate**: `len(pixels) % strip.channels == 0` and `0.0 <= brightness <= 1.0`, else `ConfigError`
2. **Level LUT**: one 256-entry table applies per-call brightness, gamma, and the cap in one `bytes.translate`. Entry `v` is `round(255 * cap * ((v / 255) * brightness) ** gamma)`, with gamma 1.0 when the strip disables it
3. **Reorder**: if `strip.color_order` is canonical, skip. Otherwise build the output with per-channel strided slices using `strip.channel_map` (`out[k::ch] = src[channel_map[k]::ch]`)
4. **Reverse**: if `reverse`, reverse pixel order with per-channel strided slices (`out[k::ch] = src[k::ch][::-1]`)

Brightness is applied before gamma so dimming is perceptual; the cap is applied after gamma so it bounds actual drive current, which is what protects a long run from brownout. The W channel goes through the same LUT.

### LUT cache

`_level_lut(brightness: float, gamma: float, cap: float) -> bytes` is wrapped in `functools.lru_cache(maxsize=256)`. Callers that sweep brightness (breathe, fades) hit distinct keys; 256 entries at 256 bytes each is 64 KB and covers every integer percent at one gamma and cap. Brightness is rounded to 4 decimals before lookup so float noise does not defeat the cache.

### Performance

All four stages are C-speed (`translate`, slice assignment). 1000 RGBW pixels process in well under 100 µs. This is what leaves the effect's own render loop as the only Python-speed work per frame.

## Color helpers

```python
RGB = tuple[int, int, int]
RGBW = tuple[int, int, int, int]
Color = RGB | RGBW

def parse_color(value: str | Sequence[int]) -> Color
def scale_color(color: Color, factor: float) -> Color
def lerp_color(a: Color, b: Color, t: float) -> Color
def kelvin_to_rgb(kelvin: int) -> RGB
def hsv_to_rgb(h: float, s: float, v: float) -> RGB
def solid_pixels(color: Color, length: int, channels: int) -> bytes
def expand_rgb_to_rgbw(pixels: bytes) -> bytes
```

- `parse_color` accepts `"ff8800"`, `"#ff8800"`, `"ff8800ff"` (RGBW), a 3- or 4-tuple of ints, or a named color from a small table (`red`, `green`, `blue`, `white`, `warmwhite`, `off`). Anything else is `ConfigError`. Named colors exist for the CLI; the table is short and documented
- `solid_pixels` pads an RGB color with `W=0` on a 4-channel strip and raises `ConfigError` for an RGBW color on a 3-channel strip
- `kelvin_to_rgb` is the Tanner Helland approximation. Input is clamped to 1000 to 40000; the documented supported range is 2000 to 6500 K and the docs say it is an approximation on RGB pixels. On RGBW strips the result goes into RGB with `W=0`; using the white channel for color temperature is a later feature
- `hsv_to_rgb` is `colorsys.hsv_to_rgb` scaled to 0 to 255 and rounded. The rainbow effect uses a precomputed 1536-entry hue table rather than calling this per pixel

## Golden cases (`tests/test_color.py`)

| Case | Expectation |
|---|---|
| `(255,255,255)`, brightness 1.0, gamma off, cap 1.0 | `ff ff ff` |
| `(255,255,255)`, brightness 1.0, gamma off, cap 0.6 | `99 99 99` (153) |
| `(255,255,255)`, brightness 0.5, gamma 2.2, cap 1.0 | `37 37 37` (55) |
| `(255,255,255)`, brightness 1.0, gamma 2.2, cap 0.6 | `99 99 99` |
| `(255,0,0)` on order `GRB` | `00 ff 00` |
| `(255,0,0)` on RGBW order `GRBW` | `00 ff 00 00` |
| 3 pixels R, G, B, reverse | B, G, R |
| 3 pixels R, G, B, reverse, order `GRB` | `00 00 ff` `ff 00 00` `00 ff 00` (reorder is per channel and reverse is per pixel, so they commute) |
| cap 0.6 cannot be exceeded | parametrized over brightness in {0.5, 1.0} and gamma in {None, 2.2}: `max(out) <= 153` |
| `kelvin_to_rgb(6500)` | `(255, 254, 250)` or within ±2 per channel of the published table |
| `kelvin_to_rgb(2700)` | warm: R 255, G in 160 to 175, B in 80 to 100 |
| `parse_color` | parametrized over hex, hash-hex, tuple, name, and three invalid inputs |

Rounding uses Python's `round` (banker's); the golden values above are computed with that and the test asserts byte equality, so the implementation cannot drift to `int()` truncation unnoticed.
