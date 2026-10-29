# pyddp component spec: protocol, constants, exceptions

Date: 2026-10-28
Status: Draft
Parent: `2026-10-28-pyddp-design.md`
Modules: `const.py`, `exceptions.py`, `protocol.py`

## Scope

Everything that turns pixel bytes into DDP packets. No sockets, no color math. `protocol.py` is pure, synchronous, and has no imports beyond `struct`, `dataclasses`, `enum`, and `collections.abc`. Coverage target 95%+.

## const.py

```python
DDP_PORT = 4048
HEADER_SIZE = 10
MAX_DATA_LEN = 1440          # DDP recommended max payload; 480 RGB or 360 RGBW pixels
MAX_OFFSET = 0xFFFF_FFFF     # 32-bit data offset
HEADER_FORMAT = ">BBBBIH"    # flags, sequence, data type, destination, offset, length

FLAG_VERSION_MASK = 0xC0
FLAG_VERSION_1 = 0x40
FLAG_TIMECODE = 0x10
FLAG_STORAGE = 0x08
FLAG_REPLY = 0x04
FLAG_QUERY = 0x02
FLAG_PUSH = 0x01

SEQUENCE_MIN = 1
SEQUENCE_MAX = 15            # low 4 bits; 0 means "sequence unused"

DESTINATION_DEFAULT = 1      # default output device
DESTINATION_ALL = 255

DEFAULT_FPS = 30
MAX_FPS = 60
DEFAULT_GAMMA = 2.2
DEFAULT_BRIGHTNESS_CAP = 1.0
```

The timecode, storage, reply, and query bits are named so the decoder can report them and tests can assert they are never set. The sender never sets them in v1.

### DataType

```python
class DataType(Enum):
    RGB = ("RGB", 3, 0x0B)       # type 001 (RGB), size 011 (8-bit)
    RGBW = ("RGBW", 4, 0x1B)     # type 011 (RGBW), size 011 (8-bit)

    @property
    def channels(self) -> int: ...
    @property
    def wire_byte(self) -> int: ...
```

The DDP data type byte is `C R TTT SSS`: bit 7 customer-defined, bit 6 reserved, bits 5 to 3 type (1 RGB, 2 HSL, 3 RGBW, 4 greyscale), bits 2 to 0 size (3 means 8 bits per channel). `0x0B` and `0x1B` follow from that. Only these two members exist; HSL and greyscale are out of scope.

`DATA_TYPE_LEGACY = 0x01` is exported for receivers that expect what older senders put in this byte. The strip's `data_type_byte` override (see the strip spec) lets a user send `0x01`, `0x00`, or anything else while the library still knows the pixel has 3 or 4 channels. The wire byte never changes how the library lays out bytes; only `DataType.channels` does.

## exceptions.py

```python
class PyDDPError(Exception): ...
class ConfigError(PyDDPError, ValueError): ...   # bad strip, segment, params, or color
class DDPSendError(PyDDPError): ...               # socket could not be opened or sender is closed
class EffectError(PyDDPError): ...                # unknown effect, bad params, or render failure
```

`ConfigError` also subclasses `ValueError` so callers that validate user input generically still catch it. Transient UDP send errors are not exceptions (see the sender spec); `DDPSendError` is reserved for "this sender cannot send at all".

## protocol.py

### DDPHeader

```python
@dataclass(frozen=True, slots=True)
class DDPHeader:
    flags: int
    sequence: int
    data_type: int
    destination: int
    offset: int
    length: int

    @property
    def version(self) -> int        # (flags & FLAG_VERSION_MASK) >> 6
    @property
    def push(self) -> bool          # bool(flags & FLAG_PUSH)

    def encode(self) -> bytes       # struct.pack(HEADER_FORMAT, ...)
    @classmethod
    def decode(cls, data: bytes) -> DDPHeader
```

`encode` validates nothing; the builders below produce valid values and the dataclass is the typed carrier. `decode` raises `ValueError` on fewer than 10 bytes. It exists for the test listener fixture and the CLI's debug dump; the library never receives DDP.

### build_header

```python
def build_header(
    *,
    offset: int,
    length: int,
    sequence: int,
    data_type: int,
    push: bool,
    destination: int = DESTINATION_DEFAULT,
) -> bytes
```

Flags are always `FLAG_VERSION_1`, plus `FLAG_PUSH` when `push`. Raises `ConfigError` on `offset > MAX_OFFSET`, `length > MAX_DATA_LEN`, `sequence` outside 0 to 15, `data_type` or `destination` outside 0 to 255. Sequence 0 is allowed here (it means "unused") even though the sender never emits it.

### SequenceCounter

```python
class SequenceCounter:
    def __init__(self, start: int = SEQUENCE_MIN) -> None
    def next(self) -> int           # returns current, then advances; 15 wraps to 1
    @property
    def current(self) -> int
```

Increments once per packet, not once per frame, per the DDP spec. A 3-packet frame consumes three sequence numbers. `start` outside 1 to 15 raises `ConfigError`.

### chunk_data

```python
def chunk_data(data: bytes, base_offset: int = 0, max_len: int = MAX_DATA_LEN) -> list[tuple[int, bytes]]
```

Splits `data` into `(offset, chunk)` pairs of at most `max_len` bytes with offsets `base_offset, base_offset + max_len, ...`. Empty `data` raises `ConfigError` (a zero-length frame has no meaning; an explicit latch uses `build_push_packet`). Does not care about pixel boundaries: a chunk may split a pixel because the receiver reassembles by byte offset. Tests cover a chunk boundary inside a pixel.

### build_packets

```python
def build_packets(
    data: bytes,
    *,
    base_offset: int,
    data_type: int,
    sequence: SequenceCounter,
    push: bool = True,
    destination: int = DESTINATION_DEFAULT,
    max_len: int = MAX_DATA_LEN,
) -> list[bytes]
```

Calls `chunk_data`, then `build_header` per chunk with `push` set only on the final chunk (and only if `push` is true), and returns complete packets (`header + chunk`). Pulls one sequence number per packet in order. The whole list is built before anything is sent so a validation error leaves the counter untouched; the counter is advanced only on success. Implementation: compute chunks, validate, then draw sequence numbers in one pass.

### build_push_packet

```python
def build_push_packet(*, sequence: SequenceCounter, data_type: int, destination: int = DESTINATION_DEFAULT) -> bytes
```

A header-only packet, `length = 0`, `offset = 0`, push set. Used to latch several segments sent with `push=False`. Whether a receiver honors a zero-length push is controller-dependent and is recorded per controller in `docs/compatibility.md`.

## Golden vectors

These are the byte-exact cases the protocol tests assert. Hex, spaces for readability.

| Case | Header |
|---|---|
| RGB, seq 1, offset 0, len 3, push | `41 01 0B 01 00000000 0003` |
| RGB, seq 2, offset 1440, len 3, no push | `40 02 0B 01 000005A0 0003` |
| RGBW, seq 15, offset 0, len 4, push | `41 0F 1B 01 00000000 0004` |
| Legacy byte, seq 1, offset 0, len 3, push | `41 01 01 01 00000000 0003` |
| Push-only latch, seq 3, RGB | `41 03 0B 01 00000000 0000` |

Chunking cases:

| Input | Packets |
|---|---|
| 1440 bytes, offset 0 | one packet, offset 0, len 1440, push |
| 1441 bytes, offset 0 | offset 0 len 1440 no push; offset 1440 len 1 push |
| 3000 bytes, offset 600 | offsets 600, 2040, 3480 with lens 1440, 1440, 120; push on the last only |
| 900 bytes, offset 0, push=False | one packet, no push |

Sequence: 15 consecutive `next()` calls from a fresh counter return 1..15; the 16th returns 1. A 3-packet frame built from a counter at 14 gets 14, 15, 1.

## Out of scope

- Query, reply, storage, and timecode flags
- Destination IDs 250 (JSON config), 251 (JSON status), 254 (DMX transit)
- Receiving or parsing anything beyond `DDPHeader.decode` for tests
