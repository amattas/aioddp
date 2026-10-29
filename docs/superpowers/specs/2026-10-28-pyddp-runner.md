# pyddp component spec: EffectRunner and fades

Date: 2026-10-28
Status: Draft
Parent: `2026-10-28-pyddp-design.md`
Module: `runner.py`

## Scope

The one place an asyncio task renders effects on a schedule. Also the `fade` helper hass-ddp uses for `transition`. Everything here is designed so that hass-ddp can start and stop runners on the HA event loop without ever leaking a task or stalling the loop.

## EffectRunner

```python
class EffectRunner:
    def __init__(
        self,
        sender: DDPSender,
        segment: Segment,
        effect: str | type[Effect],
        *,
        fps: int = DEFAULT_FPS,
        params: Mapping[str, Any] | None = None,
        brightness: float = 1.0,
        seed: int | None = None,
    ) -> None

    async def start(self) -> None
    async def stop(self) -> None
    def update(self, *, params: Mapping[str, Any] | None = None, brightness: float | None = None, fps: int | None = None) -> None
    async def wait(self) -> None               # returns when the task finishes (static effect done, stop, or error)

    @property
    def running(self) -> bool
    @property
    def error(self) -> BaseException | None
    @property
    def stats(self) -> RunnerStats

    async def __aenter__(self) -> EffectRunner  # start()
    async def __aexit__(...) -> None            # stop()
```

### Construction

- `effect` as a string is resolved through `get_effect`; unknown raises `EffectError` immediately, not at `start()`
- `params` go through `validate_params` at construction, so a bad param fails before any task exists
- `fps` is `1 <= fps <= MAX_FPS`, `brightness` is 0.0 to 1.0, else `ConfigError`
- `seed` of `None` becomes 0
- The sender must be open at `start()`, not at construction; hass-ddp builds runners before the entry is fully up

### The loop

```
t0 = loop.time()
next_deadline = t0
frame = 0
while True:
    now = loop.time()
    t = now - t0
    pixels = effect.render(segment.length, t, params, seed)
    await sender.send_frame(pixels, segment=segment, brightness=brightness, push=True)
    frame += 1
    if effect.static: break
    next_deadline += 1 / fps
    if next_deadline < now:                 # fell behind by at least a frame
        dropped += int((now - next_deadline) * fps) + 1
        next_deadline = now + 1 / fps
    await asyncio.sleep(next_deadline - loop.time())
```

- The schedule is anchored to `t0`, so jitter does not accumulate and `t` is wall-time true: an effect with `period=4` really takes 4 s per cycle at any fps
- When the loop falls behind, it resyncs rather than bursting catch-up frames
- `params`, `brightness`, and `fps` are read fresh on every iteration from instance attributes; `update()` just assigns them, so it is synchronous, safe to call from any coroutine on the loop, and takes effect at the next frame. `update(params=...)` re-validates and replaces the whole mapping (merged with defaults), it does not patch keys. `fps` changes also reset `next_deadline` to now
- Static effects render once, send, and the task exits with `running` false and no error. Persistence of a static frame is the keepalive's job

### Cancellation and errors

- `start()` creates the task with name `pyddp-effect-<effect>-<segment>`; calling it while running raises `RuntimeError` (programmer error, not a `PyDDPError`)
- `stop()` cancels the task, awaits it, swallows only `CancelledError`, and is idempotent. After `stop()` returns there is no task. It does not blank the segment; hass-ddp decides whether to `off()` or `fill()` next
- `EffectError` or any other exception from `render` is caught inside the task, logged at ERROR with the effect name and segment, stored in `runner.error`, and ends the loop. It is not re-raised into the loop's exception handler because nothing awaits the task in hass-ddp's normal flow. `wait()` re-raises it for callers that do
- `DDPSendError` (sender closed underneath the runner) ends the loop the same way. Transient send errors never reach here; the sender counts them
- The runner holds no reference cycle with the task; `stop()` or task completion clears `self._task`

### RunnerStats

```python
@dataclass(slots=True)
class RunnerStats:
    frames_rendered: int = 0
    frames_dropped: int = 0
    last_render_seconds: float = 0.0   # wall time of the most recent render call
    max_render_seconds: float = 0.0
```

`max_render_seconds` is what hass-ddp diagnostics show when someone asks why HA is laggy.

## fade

```python
async def fade(
    sender: DDPSender,
    segment: Segment,
    start: bytes,
    end: bytes,
    *,
    duration: float,
    fps: int = DEFAULT_FPS,
    brightness: float = 1.0,
) -> None
```

Linear interpolation from `start` to `end` (both canonical pixel bytes of the segment's length) over `duration` seconds, sending `max(1, round(duration * fps))` frames on the same anchored schedule as the runner, with the last frame being exactly `end`. Interpolation is done with one `bytes` LUT-free loop per frame over `bytearray`s; at 300 pixels and 30 fps that is well inside budget. `duration <= 0` sends `end` once. Cancelling the awaiting coroutine stops the fade mid-way with the last sent frame left on the wire. This is the whole `transition` implementation hass-ddp needs: it knows both colors, pyddp knows nothing about state.

Fading between two effects is out of scope. hass-ddp stops the old runner, fades to the new solid color if any, and starts the new runner.

## Tests (`tests/test_runner.py`)

- Start a `breathe` runner at 50 fps against the UDP listener for 0.2 s: between 8 and 12 frames received, offsets equal the segment's byte offset, every packet has push set
- Static `palette` runner: exactly one frame sent, `running` false afterwards, `wait()` returns, no task left in `asyncio.all_tasks()`
- `stop()` while running: no task left, `stop()` again is a no-op, `error` is `None`
- `update(params=...)` changes the next frame's bytes (chase with a new color); bad params raise `EffectError` and leave the old ones in place
- `update(fps=...)` changes the frame rate (count frames over a window)
- Render that raises: inject an effect class whose `render` raises; task ends, `error` set, `wait()` re-raises, no task left
- Sender closed during run: loop ends with `DDPSendError` in `error`
- Falling behind: effect whose `render` sleeps synchronously 50 ms at 30 fps for 0.3 s; `frames_dropped > 0` and the task still exits cleanly on `stop()`
- `fade`: 10 frames at `duration=1, fps=10`, first frame is near `start`, last frame is exactly `end` after the color pipeline, `duration=0` sends one frame
- Budget guard: `max_render_seconds` is populated
