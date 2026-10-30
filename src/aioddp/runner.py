"""EffectRunner: one asyncio task rendering an effect onto a segment at a target fps; and the ``fade`` helper."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from aioddp.const import DEFAULT_FPS, MAX_FPS
from aioddp.effects import Effect, get_effect, validate_params
from aioddp.exceptions import ConfigError, DDPSendError
from aioddp.sender import DDPSender
from aioddp.strip import Segment

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RunnerStats:
    """Live counters for one runner."""

    frames_rendered: int = 0
    frames_dropped: int = 0
    last_render_seconds: float = 0.0  # wall time of the most recent render call
    max_render_seconds: float = 0.0


def _check_fps(fps: object) -> int:
    if not isinstance(fps, int) or isinstance(fps, bool) or not 1 <= fps <= MAX_FPS:
        raise ConfigError(f"fps must be an integer 1 to {MAX_FPS}, got {fps!r}")
    return fps


def _check_brightness(brightness: object) -> float:
    if not isinstance(brightness, int | float) or isinstance(brightness, bool) or not 0.0 <= brightness <= 1.0:
        raise ConfigError(f"brightness must be 0.0 to 1.0, got {brightness!r}")
    return float(brightness)


class EffectRunner:
    """Renders ``effect`` onto ``segment`` on an anchored schedule until stopped, until a static effect has sent
    its one frame, or until an error ends it (stored in ``error``, logged, re-raised by ``wait()``)."""

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
    ) -> None:
        self._effect_cls = get_effect(effect) if isinstance(effect, str) else effect
        self._params = validate_params(self._effect_cls, params)
        self._fps = _check_fps(fps)
        self._brightness = _check_brightness(brightness)
        self._sender = sender
        self._segment = segment
        self._seed = 0 if seed is None else seed
        self._effect = self._effect_cls()
        self._resync = False
        self._task: asyncio.Task[None] | None = None
        self._error: BaseException | None = None
        self._stats = RunnerStats()

    @property
    def running(self) -> bool:
        """Whether the render task exists and has not finished."""
        return self._task is not None and not self._task.done()

    @property
    def error(self) -> BaseException | None:
        """The exception that ended the last run, if any."""
        return self._error

    @property
    def stats(self) -> RunnerStats:
        """The live stats object."""
        return self._stats

    async def start(self) -> None:
        """Start the render task. The sender must be open; raises ``RuntimeError`` if already running."""
        if self.running:
            raise RuntimeError(f"effect runner for {self._effect_cls.name!r} is already running")
        if not self._sender.is_open:
            raise DDPSendError("cannot start an effect runner on a sender that is not open")
        self._error = None
        task = asyncio.get_running_loop().create_task(
            self._run(), name=f"aioddp-effect-{self._effect_cls.name}-{self._segment.name}"
        )
        task.add_done_callback(self._on_done)
        self._task = task

    async def stop(self) -> None:
        """Cancel the render task and wait for it to finish. Idempotent; never blanks the segment."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        current = asyncio.current_task()
        cancelling = current.cancelling() if current is not None else 0
        try:
            await task
        except asyncio.CancelledError:
            # Swallow the render task's cancellation, but not a new cancellation of the caller while waiting.
            if current is not None and current.cancelling() > cancelling:
                raise

    def update(
        self,
        *,
        params: Mapping[str, Any] | None = None,
        brightness: float | None = None,
        fps: int | None = None,
    ) -> None:
        """Replace params (re-validated, merged with defaults), brightness, and/or fps from the next frame on.

        All values are validated before any is applied, so a bad value leaves the runner unchanged.
        """
        new_params = validate_params(self._effect_cls, params) if params is not None else None
        new_brightness = _check_brightness(brightness) if brightness is not None else None
        new_fps = _check_fps(fps) if fps is not None else None
        if new_params is not None:
            self._params = new_params
        if new_brightness is not None:
            self._brightness = new_brightness
        if new_fps is not None:
            self._fps = new_fps
            self._resync = True

    async def wait(self) -> None:
        """Return when the render task finishes; re-raise the error that ended it, if any."""
        task = self._task
        if task is not None:
            await asyncio.wait({task})
        if self._error is not None:
            raise self._error

    async def __aenter__(self) -> EffectRunner:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    def _on_done(self, task: asyncio.Task[None]) -> None:
        if self._task is task:
            self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        effect, segment, stats = self._effect, self._segment, self._stats
        try:
            t0 = loop.time()
            next_deadline = t0
            while True:
                now = loop.time()
                started = time.perf_counter()
                pixels = effect.render(segment.length, now - t0, self._params, self._seed)
                elapsed = time.perf_counter() - started
                stats.frames_rendered += 1
                stats.last_render_seconds = elapsed
                stats.max_render_seconds = max(stats.max_render_seconds, elapsed)
                await self._sender.send_frame(pixels, segment=segment, brightness=self._brightness, push=True)
                if self._effect_cls.static:
                    return
                fps = self._fps
                now = loop.time()
                if self._resync:
                    self._resync = False
                    next_deadline = now
                next_deadline += 1 / fps
                if next_deadline < now:  # fell behind by at least a frame: resync instead of bursting
                    stats.frames_dropped += int((now - next_deadline) * fps) + 1
                    next_deadline = now + 1 / fps
                await asyncio.sleep(next_deadline - now)
        except Exception as exc:
            self._error = exc
            _LOGGER.error("effect %r on segment %r stopped: %s", self._effect_cls.name, segment.name, exc, exc_info=exc)


async def fade(
    sender: DDPSender,
    segment: Segment,
    start: bytes,
    end: bytes,
    *,
    duration: float,
    fps: int = DEFAULT_FPS,
    brightness: float = 1.0,
) -> None:
    """Linearly interpolate ``segment`` from ``start`` to ``end`` over ``duration`` seconds.

    Sends ``max(1, round(duration * fps))`` frames on an anchored schedule, frame ``i`` of ``n`` at
    ``i * duration / n``, so the last, exactly ``end``, lands at ``duration``.
    ``duration <= 0`` sends ``end`` once. Cancelling leaves the last sent frame on the wire.
    """
    _check_fps(fps)
    if not isinstance(duration, int | float) or isinstance(duration, bool) or not math.isfinite(duration):
        raise ConfigError(f"fade duration must be a finite number of seconds, got {duration!r}")
    if len(start) != len(end):
        raise ConfigError(f"fade start and end differ in length: {len(start)} and {len(end)} bytes")
    if duration <= 0:
        await sender.send_frame(end, segment=segment, brightness=brightness)
        return
    frames = max(1, round(duration * fps))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    for i in range(1, frames + 1):
        await asyncio.sleep(max(0.0, t0 + i * duration / frames - loop.time()))
        f = i / frames
        frame = end if i == frames else bytes(a + round((b - a) * f) for a, b in zip(start, end, strict=True))
        await sender.send_frame(frame, segment=segment, brightness=brightness)
