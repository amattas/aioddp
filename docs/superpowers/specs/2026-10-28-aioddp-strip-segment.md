# aioddp component spec: Strip and Segment

Date: 2026-10-28
Status: Draft
Parent: `2026-10-28-aioddp-design.md`
Module: `strip.py`

## Scope

Immutable configuration models. Everything else in the library takes a `Strip` and zero or more `Segment`s and never re-validates what these classes already guarantee. No I/O.

## Strip

```python
@dataclass(frozen=True, slots=True)
class Strip:
    host: str
    pixel_count: int
    port: int = DDP_PORT
    data_type: DataType = DataType.RGB
    color_order: str = ""  # empty: the data type's canonical order, RGB or RGBW
    brightness_cap: float = DEFAULT_BRIGHTNESS_CAP
    gamma: float | None = DEFAULT_GAMMA
    destination_id: int = DESTINATION_DEFAULT
    data_type_byte: int | None = None
```

### Validation (`__post_init__`, raises `ConfigError`)

| Field | Rule |
|---|---|
| `host` | Non-empty after `strip()`. Not resolved here; resolution happens when the sender opens |
| `pixel_count` | Integer, `1 <= pixel_count <= MAX_OFFSET // channels`. In practice the upper bound is never reached |
| `port` | 1 to 65535 |
| `data_type` | A `DataType` member. Strings are not coerced here; the CLI and hass-ddp map user input to the enum |
| `color_order` | Empty (the default) means the canonical order for the data type. Otherwise upper-cased and must be a permutation of `"RGB"` for `DataType.RGB` and of `"RGBW"` for `DataType.RGBW`. `"GRB"`, `"BRG"`, `"GRBW"` are all valid; `"RGB"` on an RGBW strip is an error, as is a repeated letter |
| `brightness_cap` | `0.0 < cap <= 1.0`. Zero is rejected because it makes every frame black and is never what a user meant |
| `gamma` | `None` or `1.0` disables correction; otherwise `1.0 < gamma <= 4.0` |
| `destination_id` | 0 to 255 |
| `data_type_byte` | `None` or 0 to 255 |

Validation normalizes `color_order` to upper case; because the dataclass is frozen this is done with `object.__setattr__` in `__post_init__`.

### Derived properties

```python
channels: int  # data_type.channels
frame_size: int  # pixel_count * channels, in bytes
wire_data_type: int  # data_type_byte if set, else data_type.wire_byte
channel_map: tuple[int, ...]  # for each output channel position, the canonical index to read from
```

`channel_map` for `"GRB"` is `(1, 0, 2)`: output position 0 reads canonical channel 1 (G). The color pipeline uses it directly.

### Segment helpers

```python
def full_segment(self, name: str = "all") -> Segment
def validate_segment(self, segment: Segment) -> None     # raises ConfigError when out of range
def byte_offset(self, segment: Segment) -> int           # segment.start * channels
def byte_length(self, segment: Segment) -> int           # segment.length * channels
```

## Segment

```python
@dataclass(frozen=True, slots=True)
class Segment:
    name: str
    start: int
    length: int
    reverse: bool = False

    @property
    def end(self) -> int      # exclusive: start + length
```

### Validation (`__post_init__`, raises `ConfigError`)

- `name` non-empty after `strip()`; stored stripped. No slug rules; hass-ddp slugifies for entity IDs
- `start >= 0`
- `length >= 1`

A `Segment` does not know its strip, so range checking happens in `Strip.validate_segment`, called by the sender on every `fill`, `off`, and `send_frame`. It is a two-comparison check and is not cached. Overlap between segments is allowed: the library does not own a segment list, hass-ddp does, and it enforces non-overlap in its options flow because overlapping entities would fight.

### reverse

`reverse=True` means pixel index 0 of the rendered frame lands on the last physical pixel of the segment. It is applied as the final step of the color pipeline (see the color spec) and never changes `start` or `length`. A reversed segment still has one byte offset: `start * channels`.

## Usage

```python
strip = Strip(host="10.0.40.20", pixel_count=300, color_order="GRB", brightness_cap=0.6)
roof = Segment("roofline", start=0, length=260)
peak = Segment("peak", start=260, length=40, reverse=True)
strip.validate_segment(peak)  # ok
Segment("bad", start=290, length=20)  # ConfigError from validate_segment: ends at 310 > 300
```

## Tests (`tests/test_strip.py`)

Parameterized over the validation table above, one case per rule, asserting `ConfigError` and that the message names the field. Positive cases for `channel_map` on each RGB and RGBW permutation, `byte_offset` and `byte_length` for RGB and RGBW, and `full_segment`.
