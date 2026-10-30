# CLI

The `aioddp` command is a bench-test tool for bringing up new hardware. Install it
with the `cli` extra:

```bash
pip install "aioddp[cli]"
```

## Commands

```bash
aioddp --host 10.0.40.20 query                           # ask the controller for its outputs; prints a config.yaml
aioddp --host 10.0.40.20 --count 300 test                # walk a lit pixel down the string
aioddp --host 10.0.40.20 --count 300 fill --color ff8800 --brightness 0.5
aioddp --host 10.0.40.20 --count 300 effect chase --param speed=8
aioddp --host 10.0.40.20 --count 300 off
aioddp effects                                           # list effects and params
```

Run `aioddp query` first against new hardware, then `aioddp test`. `query` uses the
DDP JSON config query; a controller that does not answer it is configured by hand.
Host and count can also come from `config.yaml` (the format `query` prints).

Host and pixel counts above are placeholders.
