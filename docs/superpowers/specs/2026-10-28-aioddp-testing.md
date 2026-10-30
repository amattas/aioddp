# aioddp component spec: testing and package

Date: 2026-10-28
Status: Draft
Parent: `2026-10-28-aioddp-design.md`

## Scope

Shared fixtures, the benchmark gate, the test file map, and the public package surface. Per-component test lists live in each component spec; this document is what they share.

## Tooling

`pyproject.toml` mirrors pydmp: pytest, pytest-asyncio with `asyncio_mode = "auto"`, pytest-cov with `--cov --cov-report=term-missing`, ruff (lint and format) at 120 with `S101` ignored under `tests/`, mypy strict on `src/`. Tests are typed too (mypy runs on `tests/` with `disallow_untyped_defs` relaxed). CI runs `pytest -q`, `ruff check`, `ruff format --check`, `mypy`, `bandit -r src`, and `python -m build`, in that order, on 3.12 and the newest stable 3.x.

Coverage gates in CI: 80% total, and `protocol.py` and `color.py` at 95% enforced with `--cov-fail-under` plus a small script that reads `coverage.json` for the per-file floors (same approach as pydmp's verify job).

## Fixtures (`tests/conftest.py`)

### udp_listener

```python
class UDPListener:
    host: str                # "127.0.0.1"
    port: int                # ephemeral, bound at fixture setup
    packets: list[bytes]     # raw datagrams in arrival order
    async def wait_for(self, count: int, timeout: float = 1.0) -> list[bytes]
    def headers(self) -> list[DDPHeader]
    def payloads(self) -> list[bytes]
    def clear(self) -> None
```

An asyncio `DatagramProtocol` bound on loopback. Every sender and CLI test points a `Strip` at `listener.host`/`listener.port`, so tests assert real bytes on a real socket with no mocking of asyncio. An optional `responder` callable (`(data, addr) -> list[bytes]`) sends datagrams back to the sender, which is how query replies are tested. The captured config and status documents (`CONFIG_JSON`, `STATUS_JSON`) and `config_responder` serve query replies. `wait_for` polls on a short sleep rather than using a condition so the loop keeps running the sender's tasks. Loopback never drops datagrams at this volume; if a test sees fewer packets than expected it is a real bug, not flakiness.

### strip and sender factories

```python
@pytest.fixture
def make_strip(udp_listener) -> Callable[..., Strip]      # Strip pointed at the listener, kwargs override defaults
@pytest.fixture
async def sender(make_strip) -> AsyncIterator[DDPSender]  # opened, closed on teardown, default RGB 10 pixels, cap 1.0, gamma off
```

Defaults are cap 1.0 and gamma off so byte assertions in sender and runner tests are literal; color-pipeline math is tested in `test_color.py` and not re-derived elsewhere.

### no_leaked_tasks (autouse)

Records `asyncio.all_tasks()` before each async test and asserts the set is unchanged after teardown. This is the single mechanism behind the "never leaves an orphaned task" promise across runner, keepalive, and CLI tests.

### Golden files

`tests/golden/` holds effect frames as `.bin` plus a `.json` sidecar with the inputs. `python -m tests.golden --write` regenerates; `--check` is what the test does. A changed golden in a PR is a reviewed artifact, never a side effect of running the suite.

## Benchmark gate (`tests/test_effects.py::test_render_budget`)

For each effect: 5 warm-up renders then 50 timed renders at 500 pixels, default params, `t` advancing by 1/30 s. Assert `median < 2.0 ms * AIODDP_BENCH_FACTOR` where the factor defaults to 1.0 and CI sets `AIODDP_BENCH_FACTOR=3` to absorb shared-runner noise. The test also records max and prints a one-line table under `-v`. 2 ms at 500 pixels is 10× headroom on a 300-pixel roofline at 30 fps (0.6 ms of render per 33 ms frame) and is the number hass-ddp's "never stalls HA" claim rests on.

## Test file map

| File | Covers |
|---|---|
| `test_package.py` | `aioddp.__all__` matches the export list below, `__version__` matches metadata, importing `aioddp` does not import `click`, `rich`, `yaml`, or `aioddp.cli` |
| `test_protocol.py` | Golden headers, chunking table, sequence wrap, validation errors, `decode` round-trip |
| `test_strip.py` | Validation table, `channel_map`, byte math |
| `test_color.py` | Golden pipeline cases, cap invariant, helpers |
| `test_sender.py` | Wire bytes, segments, expansion, latch, keepalive, close, errors |
| `test_effects.py` | Contract tests parametrized over the registry, goldens, params, budget |
| `test_runner.py` | Lifecycle, update, errors, drop handling, fade |
| `test_discovery.py` | `parse_config` on captured and spec-format documents, conversion table, malformed documents, `query_layout`, `discover` |
| `test_cli.py` | Commands via `CliRunner` against the listener |

Parametrize over variations; one test per distinct behavior. The sender, runner, and CLI tests assert what is on the wire and never inspect private attributes except the shadow buffer through `stats.last_frame`.

## Public package surface (`src/aioddp/__init__.py`)

```python
__all__ = [
    "__version__",
    # models
    "Strip",
    "Segment",
    "DataType",
    # sending
    "DDPSender",
    "SenderStats",
    # effects
    "EffectRunner",
    "RunnerStats",
    "fade",
    "get_effect",
    "list_effects",
    "validate_params",
    "EffectInfo",
    "ParamSpec",
    # color
    "parse_color",
    "kelvin_to_rgb",
    "process_frame",
    # discovery
    "ControllerLayout",
    "OutputInfo",
    "discover",
    # errors
    "DDPError",
    "ConfigError",
    "DDPSendError",
    "EffectError",
    "DDPQueryError",
    # constants
    "DDP_PORT",
    "DEFAULT_FPS",
    "MAX_FPS",
    "DEFAULT_GAMMA",
]
```

`__version__` comes from `importlib.metadata.version("aioddp")`. Everything not listed is internal and may change without a version bump; hass-ddp imports only from `aioddp`, never from submodules.

## Hardware verification checklist

Not automated. Run before 0.1.0 and record the answers in `docs/compatibility.md`:

1. `aioddp query` against the NDB-Pro: does it answer a DDP query? (Answered: no; it serves its layout over HTTP, read by `pyminleon`; see `docs/compatibility.md`)
1. `aioddp test` against the NDB-Pro: confirms count and color order
2. `aioddp fill --data-type-byte 1` then `--data-type-byte 0`: does the controller care about the byte?
3. `aioddp fill --segment 500:10` on a controller with 8 outputs of fewer than 500 pixels: flat buffer or per-output?
4. `aioddp fill` then wait: how long until it blanks or falls back? Decides the keepalive default for the compatibility entry
5. `aioddp fill --color 808080 --no-gamma` vs `--gamma 2.2`: which one looks like half brightness? Decides whether the controller already applies gamma
6. Two segments with `push=False` then a latch: does a zero-length push latch the frame?
