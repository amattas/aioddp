"""EffectRunner and fade: lifecycle, update, errors, drop handling, fade."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Mapping
from typing import Any, ClassVar

import pytest

from aioddp.effects import Effect, ParamSpec
from aioddp.effects.base import BaseEffect
from aioddp.exceptions import ConfigError, DDPSendError, EffectError
from aioddp.runner import EffectRunner, fade
from aioddp.sender import DDPSender
from aioddp.strip import Segment, Strip
from tests.conftest import UDPListener

MakeStrip = Callable[..., Strip]


def _effect_tasks() -> set[asyncio.Task[Any]]:
    return {t for t in asyncio.all_tasks() if t.get_name().startswith("aioddp-effect-")}


class _Boom(BaseEffect):
    name: ClassVar[str] = "boom"
    description: ClassVar[str] = "Renders once, then raises"
    params: ClassVar[Mapping[str, ParamSpec]] = {}

    def __init__(self) -> None:
        self.calls = 0

    def render(self, length: int, t: float, params: Mapping[str, Any], seed: int) -> bytes:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("render exploded")
        return bytes(length * 3)


class _Slow(BaseEffect):
    name: ClassVar[str] = "slow"
    description: ClassVar[str] = "Blocks the loop for 50 ms per render"
    params: ClassVar[Mapping[str, ParamSpec]] = {}

    def render(self, length: int, t: float, params: Mapping[str, Any], seed: int) -> bytes:
        time.sleep(0.05)
        return bytes(length * 3)


async def test_runner_streams_frames_at_fps_and_stops_cleanly(udp_listener: UDPListener, sender: DDPSender) -> None:
    segment = Segment("peak", 2, 5)
    runner = EffectRunner(sender, segment, "breathe", fps=50)
    await runner.start()
    assert runner.running
    assert {t.get_name() for t in _effect_tasks()} == {"aioddp-effect-breathe-peak"}
    await asyncio.sleep(0.2)
    await runner.stop()

    headers = udp_listener.headers()
    assert 5 <= len(headers) <= 12  # ideal 11; loose floor for loaded runners, ceiling proves no burst
    assert {h.offset for h in headers} == {6}
    assert all(h.push for h in headers)
    assert runner.stats.frames_rendered == len(headers)
    assert runner.stats.max_render_seconds >= runner.stats.last_render_seconds > 0
    assert not runner.running
    assert runner.error is None
    assert not _effect_tasks()
    await runner.stop()  # idempotent


async def test_static_effect_sends_one_frame_and_exits(udp_listener: UDPListener, sender: DDPSender) -> None:
    runner = EffectRunner(sender, Segment("all", 0, 4), "palette")
    async with runner:
        await runner.wait()
        assert not runner.running
        assert not _effect_tasks()
        await asyncio.sleep(0.05)
    assert udp_listener.payloads() == [bytes([255, 0, 0, 0, 255, 0]) * 2]
    assert runner.error is None


async def test_start_requires_open_sender_and_rejects_double_start(make_strip: MakeStrip) -> None:
    ddp = DDPSender(make_strip())
    runner = EffectRunner(ddp, Segment("all", 0, 10), "rainbow")  # sender may be closed at construction
    with pytest.raises(DDPSendError):
        await runner.start()
    async with ddp, runner:
        with pytest.raises(RuntimeError):
            await runner.start()


@pytest.mark.parametrize(
    ("bad_update", "error"),
    [
        ({"params": {"speed": 500}}, EffectError),
        ({"brightness": 1.5}, ConfigError),
        ({"fps": 0}, ConfigError),
    ],
)
async def test_update_applies_at_next_frame_and_rejects_bad_values(
    udp_listener: UDPListener, sender: DDPSender, bad_update: dict[str, Any], error: type[Exception]
) -> None:
    segment = Segment("all", 0, 3)
    async with EffectRunner(sender, segment, "chase", fps=60, params={"speed": 0, "spacing": 2}) as runner:
        await udp_listener.wait_for(1)
        assert udp_listener.payloads()[-1] == bytes([255, 255, 255, 0, 0, 0, 255, 255, 255])

        runner.update(params={"colors": "ff0000", "speed": 0, "spacing": 2}, brightness=0.5)
        udp_listener.clear()
        await udp_listener.wait_for(2)  # the first may predate the update
        expected = bytes([128, 0, 0, 0, 0, 0, 128, 0, 0])
        assert udp_listener.payloads()[-1] == expected

        with pytest.raises(error):
            runner.update(**bad_update)
        udp_listener.clear()
        await udp_listener.wait_for(2)
        assert udp_listener.payloads()[-1] == expected
        assert runner.running


async def test_update_fps_changes_frame_rate(udp_listener: UDPListener, sender: DDPSender) -> None:
    async with EffectRunner(sender, Segment("all", 0, 10), "rainbow", fps=5) as runner:
        await asyncio.sleep(0.3)
        slow = len(udp_listener.packets)
        runner.update(fps=60)
        udp_listener.clear()
        await asyncio.sleep(0.3)
        fast = len(udp_listener.packets)
    assert slow <= 3  # 5 fps over 0.3 s: at most 2 frames, plus one of slack
    assert fast > 2 * max(slow, 1)


async def _close_sender_after_first_frame(runner: EffectRunner, ddp: DDPSender, listener: UDPListener) -> None:
    await listener.wait_for(1)
    await ddp.close()


async def _wait_only(runner: EffectRunner, ddp: DDPSender, listener: UDPListener) -> None:
    await listener.wait_for(1)


@pytest.mark.parametrize(
    ("effect", "trigger", "error"),
    [
        (_Boom, _wait_only, RuntimeError),
        ("rainbow", _close_sender_after_first_frame, DDPSendError),
    ],
    ids=["render-raises", "sender-closed"],
)
async def test_failure_ends_loop_and_is_captured(
    udp_listener: UDPListener,
    sender: DDPSender,
    caplog: pytest.LogCaptureFixture,
    effect: str | type[Effect],
    trigger: Callable[[EffectRunner, DDPSender, UDPListener], Coroutine[Any, Any, None]],
    error: type[Exception],
) -> None:
    runner = EffectRunner(sender, Segment("eaves", 0, 10), effect, fps=60)
    await runner.start()
    await trigger(runner, sender, udp_listener)
    with pytest.raises(error):
        await runner.wait()
    assert isinstance(runner.error, error)
    assert not runner.running
    assert not _effect_tasks()
    (record,) = [r for r in caplog.records if r.name == "aioddp.runner"]
    assert record.levelno == logging.ERROR
    assert "eaves" in record.getMessage()
    await runner.stop()  # no-op after the task already ended
    assert isinstance(runner.error, error)


async def test_falling_behind_drops_frames_and_still_stops(udp_listener: UDPListener, sender: DDPSender) -> None:
    async with EffectRunner(sender, Segment("all", 0, 10), _Slow, fps=30) as runner:
        await asyncio.sleep(0.3)
    assert runner.stats.frames_dropped > 0
    assert runner.stats.frames_rendered < 9
    assert runner.stats.max_render_seconds >= 0.04
    assert runner.error is None


@pytest.mark.parametrize(
    "build",
    [
        lambda ddp: EffectRunner(ddp, Segment("all", 0, 10), "nope"),
        lambda ddp: EffectRunner(ddp, Segment("all", 0, 10), "chase", params={"width": 9}),
        lambda ddp: EffectRunner(ddp, Segment("all", 0, 10), "chase", fps=0),
        lambda ddp: EffectRunner(ddp, Segment("all", 0, 10), "chase", fps=61),
        lambda ddp: EffectRunner(ddp, Segment("all", 0, 10), "chase", fps=True),
        lambda ddp: EffectRunner(ddp, Segment("all", 0, 10), "chase", brightness=-0.1),
        lambda ddp: asyncio.run(fade(ddp, Segment("all", 0, 1), b"\0\0\0", b"\0\0", duration=1)),
        lambda ddp: asyncio.run(fade(ddp, Segment("all", 0, 1), b"\0\0\0", b"\0\0\0", duration=1, fps=0)),
        lambda ddp: asyncio.run(fade(ddp, Segment("all", 0, 1), b"\0\0\0", b"\0\0\0", duration=float("nan"))),
        lambda ddp: asyncio.run(fade(ddp, Segment("all", 0, 1), b"\0\0\0", b"\0\0\0", duration=float("inf"))),
    ],
    ids=[
        "unknown-effect",
        "bad-param",
        "fps-0",
        "fps-61",
        "fps-bool",
        "brightness",
        "fade-length",
        "fade-fps",
        "fade-nan",
        "fade-inf",
    ],
)
def test_bad_arguments_fail_before_any_task(make_strip: MakeStrip, build: Callable[[DDPSender], object]) -> None:
    with pytest.raises((ConfigError, EffectError)):
        build(DDPSender(make_strip()))


@pytest.mark.parametrize(("duration", "fps", "frames"), [(0.2, 50, 10), (0.2, 10, 2), (0, 30, 1)])
async def test_fade_interpolates_to_exact_end_at_duration(
    udp_listener: UDPListener, sender: DDPSender, duration: float, fps: int, frames: int
) -> None:
    start, end = bytes(30), bytes([200, 100, 50]) * 10
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await fade(sender, sender.strip.full_segment(), start, end, duration=duration, fps=fps)
    assert loop.time() - t0 >= duration * 0.95  # the last frame lands at ``duration``, not one frame early
    await udp_listener.wait_for(frames)
    await asyncio.sleep(0.02)
    payloads = udp_listener.payloads()
    assert len(payloads) == frames
    assert payloads[-1] == end
    if frames == 10:
        assert payloads[0] == bytes([20, 10, 5]) * 10
