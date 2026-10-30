# pyddp component spec: NDB-Pro built-in effect control

Date: 2026-10-30
Status: Draft
Parent: `2026-10-28-pyddp-design.md`
Modules: `ndb.py`, `_http.py` (shared with `discovery.py`), plus `DDPSender.release()` in `sender.py`

## Scope

hass-ddp needs to trigger the scenes stored on a Minleon NDB-Pro on a schedule and turn the lights off and on without DDP. The controller has no documented API for this; its own web UI uses a few HTTP endpoints, reverse-engineered and tested live on firmware 2.0. `ndb.py` wraps exactly those, read-only plus effect selection, and is the one vendor-specific module in pyddp. It never writes the controller's saved settings.

## Measured facts (NDB-Pro, firmware 2.0)

- `POST /api/control` with a raw body `{"fx":"<Effect Name>"}` (no content type needed) selects a built-in effect and answers 200. Effects on the tested unit: Color Wave, Ping Pong, Snow, Off, Color Change, Twist, Bars, Circles, Test, Pulsate, Show1, Show2, Bands, Worms, Paint, Glow, Shift, Fixed Colors
- "Off" blanks the lights: measured current draw fell from 1.47 A to 0.73 A idle. Show1 and Show2 play the controller's stored shows
- `POST /api/control` with `{"int":"<0-100>"}` sets intensity percent. The UI sends values as JSON strings, with no spaces
- Other `/api/control` keys exist (`spd`, `trails`, `spacing`, `amount`, `dir`, `rotate`, `saveon`). `saveon` persists settings. None are implemented
- `GET /pshows.html?r=N` recalls stored preset slot N, 0 to 19 ("Show 1" is 0-9, "Show 2" is 10-19). `GET /pshows.html?s=N` saves into a slot and is never sent
- State exists only as HTML. `/pfx2.html` has one button per effect, `<button id='fxb16' class='psel button' onclick='fx2b("Glow",16)'>`, the selected one with class `psel` and the rest `pnsel`; the page's JS also defines `fx2b(name,id)`, which is not an effect. `/pcolors.html` has the intensity slider `<input type='range' min='1' max='100' class='slider Cint' value='100'>`. `/pshows.html` has a table of 20 slots: Recall link, effect name, intensity percent, colors, show time
- DDP data overrides the built-in effect immediately. About 20 s after DDP stops, the controller falls back to its built-in effect

## API

```python
@dataclass(frozen=True, slots=True)
class NDBState:
    effect: str | None          # selected built-in effect; None if the page shows none selected
    intensity: int | None       # 0-100; None if the colors page has no intensity slider
    effects: tuple[str, ...]    # in page order

@dataclass(frozen=True, slots=True)
class NDBPreset:
    slot: int                   # 0-19
    effect: str
    intensity: int              # 0-100

class NDBController:
    def __init__(self, host: str, *, port: int = 80, timeout: float = 3.0) -> None
    async def state(self) -> NDBState                    # GET /pfx2.html, then GET /pcolors.html
    async def presets(self) -> tuple[NDBPreset, ...]     # GET /pshows.html
    async def set_effect(self, name: str) -> None        # POST /api/control {"fx": name}
    async def set_intensity(self, percent: int) -> None  # POST /api/control {"int": "N"}
    async def recall_preset(self, slot: int) -> None     # GET /pshows.html?r=slot
```

- The constructor validates host (hostname, IPv4, or IPv6 literal, as discovery does), port 1 to 65535, and a positive finite timeout; anything else is `ConfigError`. Nothing is sent until a method is called, and nothing is held open between calls
- `set_effect` takes a non-empty, non-blank string; the body is `json.dumps` with compact separators, byte for byte what the UI sends. The name is not checked against `state().effects`, so a firmware with other effects still works
- `set_intensity` takes an `int` 0 to 100 (`bool` rejected), sent as a string. `recall_preset` takes an `int` 0 to 19 (`bool` rejected). Invalid arguments raise `ConfigError` and send nothing
- Errors: `ControllerError(PyDDPError)` when the controller cannot be reached, the request times out, or it answers anything but 200; the message names the method and URL. `DDPQueryError` when a page is unusable: not UTF-8, malformed chunked framing, over the size cap, or markup the parser does not recognize
- Logging is DEBUG only

## Parsing

Regexes on the documented markup, strict enough that a different page raises instead of returning nonsense:

- `/pfx2.html`: every `<button id='fxb` must match the effect button form, with the `fxbN` id equal to the `fx2b` argument; at least one button; no name twice; at most one `psel`. Names are HTML-unescaped
- `/pcolors.html`: no `slider Cint` input means intensity `None`; more than one, or a value that is missing or over 100, is `DDPQueryError`
- `/pshows.html`: every `?r=` Recall link must parse as a row (slot, effect, intensity); at least one row; slots 0 to 19 and unique; non-empty effect; intensity 0 to 100

## HTTP client

`_http.py` is the private HTTP/1.0 client, generalized from discovery: GET or POST with a body and `Content-Length`, one `asyncio.timeout` around connect, request, head, body, and close, head capped at 16 KiB, body at 64 KiB, no redirects, chunked bodies decoded under the same cap (the NDB-Pro answers HTTP/1.0 requests with HTTP/1.1 chunked bodies; measured live), host validated so it cannot steer path, port, or scheme. Discovery's behavior and tests are unchanged: it still maps any transport failure or non-200 to "no answer".

## Handing the lights back: DDPSender.release()

Because DDP overrides the built-in effect and keepalive keeps DDP flowing, a sender with keepalive would hold the lights forever. `release()` pauses keepalive resends until the next frame; see the sender spec. A typical hand-back is `sender.release()` then `ndb.set_effect("Show1")`, accepting that the controller resumes the built-in effect about 20 s after the last DDP packet. `release()` does not blank the lights; for that, select "Off" or send `off()` and keep holding.

## Tests

`tests/test_ndb.py` against the loopback `HTTPController` fixture, which now records method, target, and body, accepts POST, and serves the pages captured verbatim from the tested unit (`tests/fixtures/ndbpro/`): state and presets from the real pages, exact request line and body for each command, invalid arguments, unreachable and non-200 as `ControllerError`, a slow server as a `ControllerError` timeout, and malformed pages as `DDPQueryError`. `test_sender.py` covers `release()` on the wire. Coverage floor for `ndb.py` is 90%.
