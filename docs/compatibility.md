# Controller compatibility

DDP receivers differ in what they accept and how they behave when data stops. This page records what has been tested with pyddp. Nothing is claimed that has not been run on hardware.

## Tested controllers

| Controller | Firmware | Pixel type | Honors data type byte | Offsets | Behavior when DDP stops | Applies own gamma | Zero-length push latch |
|---|---|---|---|---|---|---|---|
| Minleon NDB-Pro | untested | untested | untested | untested | untested | untested | untested |

Reports for other controllers are welcome: open a [Controller Compatibility Report](https://github.com/amattas/pyddp/issues/new?template=controller_compatibility.yml).

## How to test a controller

Install the CLI (`pip install "pyddp[cli]"`) and use a short string. Replace `HOST` and `N` with the controller address and pixel count. The CLI defaults to a brightness cap of 1.0; pass `--cap 0.5` if the string is not power-injected.

1. **Count and order.** `pyddp --host HOST --count N test` runs an order check (red, green, blue, white on RGBW), walks one pixel down the string, then lights both ends. If colors do not match the labels, fix `--order`. Where the walk stops is the real pixel count; a jump or stall marks an output boundary.
2. **Data type byte.** Run `pyddp --host HOST --count N --data-type-byte 1 fill --color ff0000`, then the same with `--data-type-byte 0`. Compare against the default (`0x0B` for RGB, `0x1B` for RGBW with `--type rgbw`). Record which values light the string.
3. **Flat buffer or per-output offsets.** On a multi-output controller where each output has fewer than 500 pixels, run `pyddp --host HOST --count N --segment 500:10 fill --color ff0000`. Lit pixels on a later output mean one flat buffer; nothing lit (or output 1 lit) means per-output offsets.
4. **Behavior when DDP stops.** Run `pyddp --host HOST --count N fill --color ff8800`, then stop sending and time how long until the controller holds, blanks, or falls back to built-in effects. This decides whether keepalive is needed.
5. **Gamma.** Run `pyddp --host HOST --count N --no-gamma fill --color 808080`, then `--gamma 2.2` with the same color. The one that looks like half brightness tells you whether the controller already applies gamma.
6. **Zero-length push latch.** Send two segments with `push=False` from the library, then a zero-length push, and see whether the frame latches. This needs a short script against `DDPSender` (the CLI always pushes).

Record the results in a Controller Compatibility Report, including model, firmware, pixel type, outputs used, and pyddp version.
