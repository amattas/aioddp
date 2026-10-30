# pyddp component spec: query and discovery

Date: 2026-10-29
Status: Draft
Parent: `2026-10-28-pyddp-design.md`
Modules: `discovery.py`, plus `DDPSender.query` in `sender.py` and `build_query_packet` in `protocol.py`

## Scope

DDP has no handshake, so a sender cannot learn a controller's pixel count or output layout from the data path. The protocol has an optional read side: a packet with the query flag to destination 250 (JSON config) or 251 (JSON status) gets a reply carrying a JSON document. This spec adds that query to the sender, a parser for the config document, and an HTTP fallback for controllers that publish the same document over HTTP instead of answering UDP queries. The result is a `ControllerLayout` that hass-ddp can turn into a `Strip` and one `Segment` per output.

Discovery is best effort. Nothing in the data path depends on it, and a controller that answers nothing is configured by hand exactly as before.

## The config document

From the DDP spec ("schema as used in Minleon NDB"):

```json
{"config": {"ip": "a.b.c.d", "nm": "a.b.c.d", "gw": "a.b.c.d",
            "ports": [{"port": 0, "ts": 0, "l": 80, "ss": 1}, ...]}}
```

Per port: `l` is the number of lights (pixels), `ss` the 1-based starting slot (channel) in the controller's flat buffer. Minleon adds `p` as an alias for `port`, `rev` (the controller reverses that output itself), `proto` (0 DDP, 1 Art-Net, 2 E1.31), and `conv`, the input-to-pixel conversion whose input channel count is:

| `conv` | Input channels | `conv` | Input channels |
|---|---|---|---|
| 0, 1, 4, 5, 6, 7, 10, 11, 12, 13 | 3 | 2, 8, 14 | 4 |
| 3, 9 | 1 (white only; pyddp cannot drive it) | | |

Pixel start of an output is `(ss - 1) // channels`. The buffer is one flat buffer across outputs, which answers the design doc's open question for the NDB-Pro: output 2 at slot 241 is pixel 80 when output 1 has 80 three-channel pixels.

The status document is `{"status": {"man": ..., "mod": ..., "ver": ..., ...}}`; only those three keys are interpreted.

## protocol.py additions

```python
DESTINATION_CONTROL = 246   # const.py
DESTINATION_CONFIG = 250
DESTINATION_STATUS = 251

class DDPHeader:
    @property
    def query(self) -> bool     # FLAG_QUERY
    @property
    def reply(self) -> bool     # FLAG_REPLY

def build_query_packet(*, sequence: SequenceCounter, destination: int, offset: int = 0, length: int = 0) -> bytes
```

A header-only packet: flags `FLAG_VERSION_1 | FLAG_QUERY`, data type 0, validated like `build_header`, one sequence number consumed. Golden vectors: destination 250 at sequence 1 is `42 01 00 FA 00000000 0000`; destination 251 at sequence 2 is `42 02 00 FB 00000000 0000`.

## DDPSender.query

```python
async def query(self, destination: int = DESTINATION_CONFIG, *, timeout: float = 1.0) -> dict[str, Any] | None
```

- Raises `DDPSendError` when the sender is not open and `ConfigError` for a timeout that is not a positive finite number
- Sends one query packet through the normal send path (it counts in `stats.packets_sent` and prints the DEBUG line)
- Waits up to `timeout` for a reply: datagrams with the reply flag and a matching destination are collected, `data[10:10 + length]` per packet, keyed by header offset (a repeated offset replaces), until one arrives with push set, which per the spec marks the end of the reply. The chunks must then be contiguous from offset 0, else the query raises `DDPQueryError` naming the gap. Anything else received on the socket is ignored
- Replies are matched by destination only, because the spec does not say a reply echoes the query's sequence number. A reply that arrives after its query timed out is dropped, unless a new query to the same destination is already pending, in which case that query returns it. In practice a query is one round trip and `discover` makes one per destination, so this is noted, not guarded
- Returns `None` on timeout or on an empty reply (the spec's "reading not supported" answer is length 0 with push). Returns the parsed JSON object otherwise. A reply that is not valid JSON or not an object raises `DDPQueryError`
- One pending query per destination; a second concurrent query to the same destination is a programmer error (`RuntimeError`). Queries to different destinations may overlap
- `close()` or a lost connection fails a pending query with `DDPSendError`; nothing hangs and no task leaks
- `stats.replies_received` counts completed replies
- `_DDPProtocol.datagram_received` is the only receive path in the library. It feeds `query` and nothing else; pyddp is still not a DDP receiver

`DDPQueryError(PyDDPError)` is new: a controller answered a query or config request with something unusable.

## discovery.py

```python
@dataclass(frozen=True, slots=True)
class OutputInfo:
    index: int                      # controller output number as reported, 0-based
    start: int                      # first pixel in the flat buffer, 0-based
    length: int                     # pixels; 0 means unused
    reversed_by_controller: bool = False

@dataclass(frozen=True, slots=True)
class ControllerLayout:
    host: str
    source: str                     # "ddp" or "http"
    pixel_count: int                # max(start + length) over active outputs, else 0
    channels: int                   # channels per input pixel used for slot math
    outputs: tuple[OutputInfo, ...]
    protocol: str | None            # "ddp", "artnet", "e131", or None when the document does not say
    manufacturer: str | None
    model: str | None
    firmware: str | None
    config: Mapping[str, Any]       # the inner config object as received
    status: Mapping[str, Any] | None

    @property
    def active_outputs(self) -> tuple[OutputInfo, ...]
    @property
    def data_type(self) -> DataType | None     # RGB for 3 channels, RGBW for 4, else None
    def segments(self) -> list[Segment]        # one per active output

def parse_config(document, *, host, source, status=None) -> ControllerLayout
async def query_layout(host, *, port=DDP_PORT, timeout=1.0, bind_host=None) -> ControllerLayout | None
async def fetch_layout_http(host, *, port=80, timeout=2.0, config_path="/api/config", status_path="/api/status") -> ControllerLayout | None
async def discover(host, *, port=DDP_PORT, http_port=80, timeout=1.0) -> ControllerLayout | None
```

### parse_config

Accepts the `{"config": {...}}` wrapper or the inner object, and the same for `status`. `ports` must be a list of mappings with integer `l >= 0` and `ss >= 1` (decimal strings accepted, since the spec's example is loose about types); anything else, a duplicate port index, a `rev` that is not a bool or int, or an output that would end past `MAX_OFFSET` is `DDPQueryError` naming the port. Any document that reaches the parser was decoded by `decode_json_document`, which rejects NaN and Infinity. `channels` comes from `conv` when present and known, else is inferred from the first two active outputs when `ss2 - ss1 == l1 * c` for `c` in 3 or 4, else defaults to 3. `(ss - 1)` must divide by `channels`.

`segments()` returns `Segment(f"output{index + 1}", start, length)` for each active output, named to match the controller's 1-based "Output 1" in its web UI. `reverse` is always `False`: when the controller reverses an output itself, the pixel order in the DDP buffer is already what the controller wants, and reversing again in pyddp would undo it. `reversed_by_controller` carries the fact for display.

### query_layout

Opens a throwaway `DDPSender` on `Strip(host, pixel_count=1, port=port)`, queries config, then status as a best-effort second query, closes the sender whether or not anything answered. `None` when config does not answer. `DDPSendError` from opening the socket propagates.

### fetch_layout_http

A minimal HTTP/1.0 GET over `asyncio.open_connection`, so one `asyncio.timeout(timeout)` covers connect, request, headers, and body (a threaded `urllib` call cannot be bounded this way: its socket timeout is per read, which a server dripping one byte at a time defeats). Head capped at 16 KiB, body at 64 KiB, `Content-Length` honored, chunked transfer encoding decoded under the same cap (malformed framing is `DDPQueryError`). Only status 200 is an answer; redirects are never followed because nothing implements them. The host is validated first (hostname characters, IPv4, or an IPv6 literal, bracketed in the URL) and anything else is `ConfigError`, so the host can never steer the path, port, or scheme. Any connection, HTTP, or timeout error on the config request means "this controller has no HTTP config" and returns `None` (logged at DEBUG). A body that is not a JSON object raises `DDPQueryError`. The status request is best effort. The default paths are the ones the Minleon NDB-Pro serves; they are parameters, not constants, so another controller that publishes the DDP JSON over HTTP at a different path still works. This is the one place pyddp knows a vendor convention, and it knows only a URL path and a JSON document that the DDP spec already defines.

### discover

`query_layout`, then `fetch_layout_http`; the first answer wins.

## CLI: `pyddp query`

```
pyddp --host HOST query [--timeout 1.0] [--http-port 80] [--no-http] [--json]
```

Needs host and port only; `--count` is what this command exists to find. Human output is the controller identity line (when status answered), one row per active output with pixels, start, and whether the controller reverses it, a count of unused outputs, the total, and a `config.yaml` snippet with `strip` and one segment per active output, ready to paste. A protocol other than DDP or a channel count pyddp cannot drive prints a warning on stderr. `--json` prints one object with host, source, manufacturer, model, firmware, protocol, pixel_count, channels, data_type, outputs, and the raw config and status documents.

No answer from either path exits 2 with a message naming the host and both ports. A malformed answer exits 1 through the normal `PyDDPError` mapping.

## Tests

- `test_protocol.py`: query packet goldens; `decode` reports query and reply flags
- `test_sender.py`: `query` against the `udp_listener` fixture, which gains an optional `responder` hook that sends datagrams back to the sender. Cases: full reply, two-packet reply, timeout, empty reply, malformed JSON, not open, concurrent same destination, stray non-reply datagram ignored, close while pending
- `test_discovery.py`: `parse_config` on the captured NDB-Pro document and on the spec-format document; conversion table cases; malformed documents parametrized; `query_layout` through the responder; `fetch_layout_http` against a loopback HTTP server fixture, and against a closed port; `discover` falling back to HTTP
- `test_cli.py`: `query` with host only, `--json`, `--no-http` with no answer, HTTP fallback

## Compatibility notes

The Minleon NDB-Pro (firmware 2.0) does not answer UDP queries to 250 or 251, from either an ephemeral source port or port 4048, but serves both documents at `/api/config` and `/api/status` over HTTP. Its status document reports `"push": true`. Both facts are recorded in `docs/compatibility.md`.
