# pyddp component spec: DDPSender

Date: 2026-10-28
Status: Draft
Parent: `2026-10-28-pyddp-design.md`
Module: `sender.py`

## Scope

The one object that owns a socket. Everything that reaches the controller goes through `DDPSender.send_frame`, which runs the color pipeline and so cannot bypass the brightness cap. Async only; the sync wrapper is deferred (see the end).

## Construction and lifecycle

```python
class DDPSender:
    def __init__(
        self,
        strip: Strip,
        *,
        keepalive_interval: float | None = None,
        bind_host: str | None = None,
    ) -> None

    async def open(self) -> None
    async def close(self) -> None
    async def __aenter__(self) -> DDPSender      # open()
    async def __aexit__(...) -> None             # close()

    @property
    def is_open(self) -> bool
    @property
    def strip(self) -> Strip
    @property
    def stats(self) -> SenderStats
```

- `open()` calls `loop.create_datagram_endpoint(_DDPProtocol, remote_addr=(strip.host, strip.port), local_addr=(bind_host, 0) if bind_host else None)`. Name resolution happens here. `OSError` (unresolvable host, no route, bind failure) is re-raised as `DDPSendError` with the original as `__cause__`. Opening an already open sender is a no-op
- `close()` cancels the keepalive task, awaits it, closes the transport, and sets `is_open` false. Idempotent. It does not blank the lights; a reload must not flash the house
- The sender is bound to the event loop it was opened on and is not thread-safe. Nothing in it uses threads or executors
- `keepalive_interval` must be `None` or `>= 0.5` seconds, else `ConfigError`

## Sending

```python
    async def send_frame(
        self,
        pixels: bytes,
        *,
        segment: Segment | None = None,
        brightness: float = 1.0,
        push: bool = True,
    ) -> None

    async def fill(self, segment: Segment | None, color: Color, *, brightness: float = 1.0, push: bool = True) -> None
    async def off(self, segment: Segment | None = None, *, push: bool = True) -> None
    async def push(self) -> None
```

`send_frame`:

1. Raise `DDPSendError` if not open
2. Resolve the target: `segment` or `strip.full_segment()`; `strip.validate_segment` it
3. Require `len(pixels) == strip.byte_length(segment)`; if the strip is RGBW and `len(pixels)` equals `segment.length * 3`, expand with `expand_rgb_to_rgbw` first. Any other length is `ConfigError`
4. `wire = process_frame(pixels, strip, brightness=brightness, reverse=segment.reverse)`
5. Copy `wire` into the shadow buffer at `strip.byte_offset(segment)`
6. `packets = build_packets(wire, base_offset=..., data_type=strip.wire_data_type, sequence=self._seq, push=push, destination=strip.destination_id)`
7. `transport.sendto(p)` for each packet, with no `await` between packets so two coroutines sending concurrently cannot interleave the packets of one frame
8. Update stats and `last_send`

`fill` is `send_frame(solid_pixels(color, segment.length, strip.channels), ...)`. `off` is `fill` with black at brightness 1.0, and it is the only thing that blanks a strip. `push` sends `build_push_packet`; use it after a series of `push=False` sends to latch them together on receivers that honor a zero-length push.

Declared `async` even though `sendto` does not await, so the API does not change if pacing is added and so hass-ddp's call sites are uniform.

## Shadow buffer

`self._shadow = bytearray(strip.frame_size)` holds the last wire bytes sent for every position, initially all zero. Three things read it:

- Keepalive resends it whole
- `stats.last_frame` exposes a `bytes` copy for diagnostics and tests
- Nothing else. It is not a source of truth for color, since it is post-pipeline; hass-ddp keeps its own state

## Keepalive

When `keepalive_interval` is set, `open()` starts one task:

```
loop:
    sleep(interval)
    if now - last_send >= interval and any frame has been sent:
        send the shadow buffer as one full-strip frame, push=True, via build_packets
```

It reuses the sequence counter and counts in `stats` like any other send. It runs only when the caller has been idle, so an active effect never doubles traffic. It is off by default; the compatibility page records which controllers blank without it. The task is created with a name (`pyddp-keepalive-<host>`) and `close()` always cancels and awaits it so no task outlives the sender.

## Error handling

- `_DDPProtocol.error_received(exc)` logs at WARNING once per distinct `errno` and then at DEBUG, increments `stats.send_errors`. ICMP port-unreachable on a controller that is off shows up here; it must not kill an effect loop
- `_DDPProtocol.connection_lost(exc)` marks the sender closed and logs at WARNING if `exc` is not `None`. The next `send_frame` raises `DDPSendError`
- `transport.sendto` itself does not raise for UDP; if it ever does, the `OSError` is caught, counted, and logged, not raised
- Frames are never logged above DEBUG, and at DEBUG only the header fields and byte length, never the payload

## SenderStats

```python
@dataclass(slots=True)
class SenderStats:
    packets_sent: int = 0
    bytes_sent: int = 0
    frames_sent: int = 0
    keepalives_sent: int = 0
    send_errors: int = 0
    last_send: float | None = None     # loop.time()
    last_frame: bytes = b""             # copy of the shadow buffer at last send
```

`stats` returns the live object; hass-ddp diagnostics read it. It is not reset by `close()`.

## Concurrency contract

- One `DDPSender` per controller. Two senders to one host fight at the controller and the library does not detect it
- Any number of coroutines may call `send_frame` concurrently; each frame's packets are contiguous on the wire because there is no suspension point inside the send loop
- Rate limiting is not implemented. At 30 fps a 1000-pixel RGB strip is 90 packets per second, which is nothing. If a controller needs pacing, that is a compatibility-page note and a later feature

## Tests (`tests/test_sender.py`)

All against the `udp_listener` fixture from the testing spec; no mocking of asyncio.

- `fill` on full strip: one packet, header matches golden, payload is the capped and reordered bytes
- `fill` on a segment at pixel 100 of an RGB strip: offset 300
- Frame larger than 1440 bytes: packet count, offsets, push only on last, sequence numbers consecutive
- RGB pixels sent to an RGBW strip are expanded; wrong length raises `ConfigError`
- Two segments sent with `push=False` then `push()`: three sends, push bit only on the latch packet
- `off` writes zeros into the shadow buffer and onto the wire
- Keepalive at 0.5 s with a fake clock or short real sleep: no packets while sending every 0.1 s; one full-strip resend after idle
- `close()` cancels keepalive (no pending tasks after, asserted via `asyncio.all_tasks`), second `close()` is a no-op, `send_frame` after close raises `DDPSendError`
- Unresolvable host raises `DDPSendError` from `open()`
- `error_received` increments `send_errors` and does not raise

## Deferred: sync wrapper

`sender_sync.py` is not in 0.1.0. The CLI covers scripting, and a sync API would need its own loop-ownership rules that the keepalive task makes awkward. If a demand appears, the shape is `DDPSenderSync(strip)` running a private loop in `asyncio.run`-style for each call, with no keepalive. The design doc's open question stays open until then; this spec's recommendation is to drop it.
