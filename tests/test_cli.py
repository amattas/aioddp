"""CLI commands via CliRunner against the loopback listener.

``CliRunner.invoke`` blocks while the command runs its own ``asyncio.run``, so it runs in a worker thread while
this test's loop keeps the listener receiving.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from pyddp.cli import cli
from pyddp.effects import EFFECTS
from tests.conftest import UDPListener

WHITE = bytes([255, 255, 255])


class CLI:
    """Invokes ``pyddp`` from a temp working directory; ``target`` aims it at the listener with literal bytes."""

    def __init__(self, listener: UDPListener, workdir: Path) -> None:
        self.listener = listener
        self.workdir = workdir
        self.target = ["--host", listener.host, "--port", str(listener.port), "--count", "10", "--no-gamma"]

    async def __call__(self, *args: str) -> Result:
        return await asyncio.to_thread(CliRunner().invoke, cli, list(args))

    def write_config(self, text: str) -> Path:
        path = self.workdir / "config.yaml"
        path.write_text(text)
        return path


@pytest.fixture
def run(udp_listener: UDPListener, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CLI:
    monkeypatch.chdir(tmp_path)  # isolates from any real ./config.yaml
    return CLI(udp_listener, tmp_path)


SEGMENTS_CONFIG = "segments:\n  peak: { start: 6, length: 4, reverse: true }\n"


@pytest.mark.parametrize(
    ("args", "offset", "payload"),
    [
        (["fill", "--color", "ff8800"], 0, bytes([255, 136, 0]) * 10),
        (["fill", "--kelvin", "2700", "--brightness", "1.0"], 0, bytes([255, 167, 87]) * 10),
        (["off"], 0, bytes(30)),
        (["fill", "--color", "red", "--segment", "peak"], 18, bytes([255, 0, 0]) * 4),
        (["--segment", "2:3:r", "fill", "--color", "0000ff"], 6, bytes([0, 0, 255]) * 3),
        (["-vv", "off", "--segment", "5:5"], 15, bytes(15)),
    ],
    ids=["fill", "kelvin", "off", "config-segment", "adhoc-reverse-global", "adhoc-off"],
)
async def test_fill_and_off_put_expected_bytes_on_the_wire(
    run: CLI, args: list[str], offset: int, payload: bytes
) -> None:
    run.write_config(SEGMENTS_CONFIG)
    result = await run(*run.target, *args)
    assert result.exit_code == 0, result.output
    (header,) = run.listener.headers()
    assert header.offset == offset
    assert header.push
    assert run.listener.payloads() == [payload]
    assert ("offset=15 length=15 push=True" in result.stderr) == ("-vv" in args)  # DEBUG: one line per packet


@pytest.mark.parametrize(
    ("flags", "payload"),
    [
        ([], bytes([16, 8, 24]) * 4),
        (["--order", "rgb", "--count", "2", "--cap", "1"], bytes([16, 32, 48]) * 2),
    ],
    ids=["config", "flags-override"],
)
async def test_flags_override_config_values(run: CLI, flags: list[str], payload: bytes) -> None:
    run.write_config(
        f"strip:\n  host: {run.listener.host}\n  port: {run.listener.port}\n  pixel_count: 4\n"
        "  color_order: GRB\n  brightness_cap: 0.5\n  gamma: null\n"
    )
    result = await run(*flags, "fill", "--color", "102030")
    assert result.exit_code == 0, result.output
    assert run.listener.payloads() == [payload]


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ("strip:\n  hots: 10.0.0.1\n", "strip.hots"),
        ("segments:\n  peak: { start: 0, lenght: 4 }\n", "segments.peak.lenght"),
        ("colour: red\n", "colour"),
        ("strip: [1, 2\n", "config"),
        ("strip:\n  brightness_cap: high\n", "brightness_cap"),
        ("strip:\n  data_type: rgbx\n", "data_type"),
        ("strip: 5\n", "strip must be a mapping"),
        (None, "missing.yaml"),
    ],
    ids=["strip-key", "segment-key", "top-key", "bad-yaml", "bad-type", "bad-data-type", "not-mapping", "missing"],
)
async def test_bad_config_exits_1_naming_the_problem(run: CLI, config: str | None, message: str) -> None:
    path = run.write_config(config) if config is not None else run.workdir / "missing.yaml"
    result = await run("--config", str(path), *run.target, "off")
    assert result.exit_code == 1, result.output
    assert message in result.output
    assert run.listener.packets == []


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        (["--count", "10", "off"], 1, "host"),
        (["--host", "127.0.0.1", "off"], 1, "count"),
        (["--host", "127.0.0.1", "--count", "10", "--cap", "2", "off"], 1, "brightness_cap"),
        (["--host", "127.0.0.1", "--count", "10", "off", "--segment", "8:5"], 1, "past the strip"),
        (["--host", "127.0.0.1", "--count", "10", "off", "--segment", "roof"], 1, "roof"),
        (["--host", "127.0.0.1", "--count", "10", "fill"], 1, "--color"),
        (["--host", "127.0.0.1", "--count", "10", "--bogus", "off"], 1, "--bogus"),
        (["--host", "127.0.0.1", "--count", "10", "--gamma", "2", "--no-gamma", "off"], 1, "--no-gamma"),
        (["--host", "host.invalid", "--count", "10", "off"], 2, "host.invalid"),
        (["-vv", "--host", "host.invalid", "--count", "10", "off"], 2, "host.invalid"),
        (["--host", "a" * 64 + ".x", "--count", "10", "off"], 2, "cannot open"),
    ],
    ids=[
        "no-host",
        "no-count",
        "bad-cap",
        "segment-too-long",
        "unknown-segment",
        "no-color",
        "usage",
        "gamma",
        "dns",
        "dns-vv",
        "label-too-long",
    ],
)
async def test_errors_exit_with_mapped_code_and_traceback_only_under_vv(
    run: CLI, args: list[str], code: int, message: str
) -> None:
    result = await run(*args)
    assert result.exit_code == code, result.output
    assert message in result.output
    assert ("Traceback" in result.output) == ("-vv" in args)


@pytest.mark.parametrize(
    ("args", "min_frames", "max_frames", "blanked"),
    [
        (["effect", "chase", "--fps", "50", "--duration", "0.2"], 5, 14, True),
        (["effect", "chase", "--fps", "50", "--duration", "0.2", "--keep", "--json"], 5, 13, False),
        (["effect", "palette", "--param", "colors=ff0000,00ff00"], 1, 1, False),
    ],
    ids=["blank-on-exit", "keep-json", "static"],
)
async def test_effect_runs_then_blanks_unless_keep_or_static(
    run: CLI, args: list[str], min_frames: int, max_frames: int, blanked: bool
) -> None:
    result = await run(*run.target, *args)
    assert result.exit_code == 0, result.output
    payloads = run.listener.payloads()
    assert min_frames <= len(payloads) <= max_frames
    assert (payloads[-1] == bytes(30)) is blanked
    if "--json" in args:
        stats = json.loads(result.stdout)
        assert stats["frames_rendered"] == len(payloads)
        assert stats["packets_sent"] == len(payloads)
        assert "frames" not in result.stderr
    elif "palette" not in args:
        assert "frames" in result.stderr  # rich status line


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["effect", "chase", "--param", "speed=500"], "speed"),
        (["effect", "chase", "--param", "bogus=1"], "bogus"),
        (["effect", "chase", "--param", "speed"], "key=value"),
        (["effect", "sparkle"], "sparkle"),
        (["effect", "chase", "--fps", "99"], "fps"),
    ],
    ids=["out-of-range", "unknown-param", "no-equals", "unknown-effect", "bad-fps"],
)
async def test_effect_bad_input_exits_1_before_sending(run: CLI, args: list[str], message: str) -> None:
    result = await run(*run.target, *args)
    assert result.exit_code == 1, result.output
    assert message in result.output
    assert run.listener.packets == []


@pytest.mark.parametrize(
    ("type_flags", "order_frame"),
    [
        ([], bytes([255, 0, 0, 0, 255, 0, 0, 0, 255]) + bytes(6)),
        (["--type", "rgbw"], bytes([255, 0, 0, 0, 0, 255, 0, 0, 0, 0, 255, 0, 0, 0, 0, 255]) + bytes(4)),
    ],
    ids=["rgb", "rgbw"],
)
async def test_test_command_runs_order_walk_ends_then_blank(
    run: CLI, type_flags: list[str], order_frame: bytes
) -> None:
    target = ["--host", run.listener.host, "--port", str(run.listener.port), "--count", "5", "--no-gamma"]
    result = await run(*target, *type_flags, "test", "--delay", "0.001", "--hold", "0.01")
    assert result.exit_code == 0, result.output

    channels = len(order_frame) // 5
    lit, dark = WHITE + bytes(channels - 3), bytes(channels)
    walk = [dark * i + lit + dark * (4 - i) for i in range(5)]
    ends = lit + dark * 3 + lit
    assert run.listener.payloads() == [order_frame, *walk, ends, dark * 5]
    assert [int(line) for line in result.stdout.split()] == list(range(5))
    assert "Walk" in result.stderr


async def test_effects_lists_all_six_as_table_and_json(run: CLI) -> None:
    table = await run("effects")
    as_json = await run("effects", "--json")
    assert table.exit_code == as_json.exit_code == 0
    assert all(name in table.stdout for name in EFFECTS)
    listing = json.loads(as_json.stdout)
    assert [e["name"] for e in listing] == list(EFFECTS)
    assert listing[2]["params"]["speed"]["default"] == 8.0


async def test_version_prints_metadata_version(run: CLI) -> None:
    result = await run("version")
    as_json = await run("--json", "version")
    assert result.exit_code == as_json.exit_code == 0
    assert result.stdout == f"pyddp {version('pyddp')}\n"
    assert json.loads(as_json.stdout) == {"version": version("pyddp")}


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT delivery to a child process is POSIX-only")
async def test_ctrl_c_blanks_and_exits_130(run: CLI) -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pyddp.cli",
        *run.target,
        "effect",
        "chase",
        "--fps",
        "50",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await run.listener.wait_for(2, timeout=10)
    proc.send_signal(signal.SIGINT)
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    assert proc.returncode == 130, stderr
    assert run.listener.payloads()[-1] == bytes(30)
    assert b"Traceback" not in stderr


@pytest.mark.parametrize("tracebacks", [False, True])
def test_log_handler_shows_tracebacks_only_when_asked(capsys: pytest.CaptureFixture[str], tracebacks: bool) -> None:
    from pyddp.cli import _StderrHandler

    logger = logging.getLogger("pyddp.test_cli")
    handler = _StderrHandler(tracebacks=tracebacks)
    logger.addHandler(handler)
    try:
        try:
            raise RuntimeError("render exploded")
        except RuntimeError:
            logger.exception("effect failed")
    finally:
        logger.removeHandler(handler)
    err = capsys.readouterr().err
    assert "effect failed" in err
    assert ("Traceback" in err) == tracebacks
