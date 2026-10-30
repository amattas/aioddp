# Controller compatibility

DDP receivers differ in what they accept and how they behave when data stops. This page records what has been tested with pyddp. Nothing is claimed that has not been run on hardware.

## Tested controllers

| Controller | Firmware | Pixel type | Honors data type byte | Offsets | Behavior when DDP stops | Applies own gamma | Zero-length push latch | Layout discovery |
|---|---|---|---|---|---|---|---|---|
| Minleon NDB-Pro | 2.0 | Rainmin RGB+ (RGBW+2, input RGB) | untested | one flat buffer, 1-based starting slot per output | falls back to its built-in effect about 20 s after DDP stops | untested | untested | HTTP only |

### Minleon NDB-Pro notes

- **Layout discovery.** It does not answer DDP queries to destination 250 or 251, whether the query comes from an ephemeral source port or from port 4048. It serves the DDP JSON config document at `http://HOST/api/config` and the status document at `http://HOST/api/status` over plain HTTP (no HTTPS). `pyddp query` reads them.
- **Offsets.** Every output has a `ss` starting slot, 1-based, in one flat channel buffer. With "RGB=>RGBW" conversion the sender sends 3 channels per pixel and output 2 of an 80-pixel output 1 starts at slot 241, pixel 80. Outputs can be auto-filled contiguously or placed by hand, so read the slots rather than assuming they are contiguous.
- **Reverse.** The per-output "Reverse?" setting is applied by the controller. `pyddp query` reports it and leaves segment `reverse` off so the two do not cancel.
- **Status document.** Reports manufacturer `3WL`, model `NDBPRO`, firmware, MAC, `"push": true`, input voltage, current, and fuse state.
- **DDP and built-in effects.** DDP data overrides the running built-in effect immediately. About 20 s after DDP stops, the controller falls back to its built-in effect; this was measured with the status document's current readback, not by eye. So holding a DDP frame needs `keepalive_interval`, and handing the lights back to the controller means stopping DDP: `DDPSender.release()` pauses keepalive until the next frame.
- **Built-in effect control (HTTP).** The web UI drives the controller through endpoints `NDBController` uses: `POST /api/control` with a raw JSON body (no content type needed) `{"fx":"<Effect Name>"}` selects a built-in effect, and `{"int":"<0-100>"}` sets intensity, values as JSON strings; both answer 200. `GET /pshows.html?r=N` recalls stored preset slot N (0-9 are "Show 1", 10-19 "Show 2"). Effects on this unit: Color Wave, Ping Pong, Snow, Off, Color Change, Twist, Bars, Circles, Test, Pulsate, Show1, Show2, Bands, Worms, Paint, Glow, Shift, Fixed Colors. "Off" blanks the lights (current draw fell from 1.47 A to 0.73 A idle); Show1 and Show2 play the stored shows. State is only in HTML: `/pfx2.html` (selected effect), `/pcolors.html` (intensity slider), `/pshows.html` (the 20 preset slots). Other `/api/control` keys (`spd`, `trails`, `spacing`, `amount`, `dir`, `rotate`, `saveon`) and `GET /pshows.html?s=N` (save into a slot) exist; pyddp does not use them, and never writes saved settings.

Reports for other controllers are welcome: open a [Controller Compatibility Report](https://github.com/amattas/pyddp/issues/new?template=controller_compatibility.yml).

## How to test a controller

Install the CLI (`pip install "pyddp[cli]"`) and use a short string. Replace `HOST` and `N` with the controller address and pixel count. The CLI defaults to a brightness cap of 1.0; pass `--cap 0.5` if the string is not power-injected.

1. **Layout.** `pyddp --host HOST query` asks for the controller's outputs over DDP, then HTTP. If it answers, it prints the pixel count, one line per output, and a `config.yaml` to paste; record whether it answered over DDP, HTTP, or neither.
2. **Count and order.** `pyddp --host HOST --count N test` runs an order check (red, green, blue, white on RGBW), walks one pixel down the string, then lights both ends. If colors do not match the labels, fix `--order`. Where the walk stops is the real pixel count; a jump or stall marks an output boundary.
3. **Data type byte.** Run `pyddp --host HOST --count N --data-type-byte 1 fill --color ff0000`, then the same with `--data-type-byte 0`. Compare against the default (`0x0B` for RGB, `0x1B` for RGBW with `--type rgbw`). Record which values light the string.
4. **Flat buffer or per-output offsets.** On a multi-output controller where each output has fewer than 500 pixels, run `pyddp --host HOST --count N --segment 500:10 fill --color ff0000`. Lit pixels on a later output mean one flat buffer; nothing lit (or output 1 lit) means per-output offsets.
5. **Behavior when DDP stops.** Run `pyddp --host HOST --count N fill --color ff8800`, then stop sending and time how long until the controller holds, blanks, or falls back to built-in effects. This decides whether keepalive is needed.
6. **Gamma.** Run `pyddp --host HOST --count N --no-gamma fill --color 808080`, then `--gamma 2.2` with the same color. The one that looks like half brightness tells you whether the controller already applies gamma.
7. **Zero-length push latch.** Send two segments with `push=False` from the library, then a zero-length push, and see whether the frame latches. This needs a short script against `DDPSender` (the CLI always pushes).

Record the results in a Controller Compatibility Report, including model, firmware, pixel type, outputs used, and pyddp version.
