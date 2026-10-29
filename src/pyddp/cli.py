"""``pyddp`` command line: bench tool for a DDP controller with no Home Assistant in the loop.

A thin shell over the library: every command resolves to a ``Strip``, an optional ``Segment``, and one or two
library calls inside ``asyncio.run``. Needs the ``[cli]`` extra; ``import pyddp`` never imports this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sys
import traceback
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import version as _dist_version
from pathlib import Path
from typing import Any

try:
    import click
    import yaml
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
except ImportError as err:  # pragma: no cover - exercised only without the extra installed
    raise ImportError("the pyddp command line needs the [cli] extra: pip install 'pyddp[cli]'") from err

from pyddp.color import Color, kelvin_to_rgb, parse_color, solid_pixels
from pyddp.const import DDP_PORT, DEFAULT_FPS, DEFAULT_GAMMA, DataType
from pyddp.discovery import ControllerLayout, fetch_layout_http, query_layout
from pyddp.effects import Effect, ParamSpec, get_effect, list_effects
from pyddp.exceptions import ConfigError, DDPSendError, EffectError, PyDDPError
from pyddp.runner import EffectRunner
from pyddp.sender import DDPSender
from pyddp.strip import Segment, Strip

EXIT_ERROR = 1
EXIT_SEND_ERROR = 2
EXIT_INTERRUPTED = 130

DEFAULT_CONFIG = Path("config.yaml")
CLI_BRIGHTNESS_CAP = 1.0  # the bench user is standing next to the lights; hass-ddp defaults to 0.6

_TOP_KEYS = frozenset({"strip", "segments"})
_STRIP_KEYS = frozenset(
    {
        "host",
        "pixel_count",
        "port",
        "data_type",
        "color_order",
        "brightness_cap",
        "gamma",
        "destination_id",
        "data_type_byte",
    }
)
_SEGMENT_KEYS = frozenset({"start", "length", "reverse"})
_DATA_TYPES = {"rgb": DataType.RGB, "rgbw": DataType.RGBW}
_ADHOC_SEGMENT = re.compile(r"(\d+):(\d+)(:r)?")
_PLAIN_YAML_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")
_STATUS_REFRESH_PER_SECOND = 2


@dataclass(slots=True)
class _Options:
    """Global options, stored as the click context object; the strip is built from them on demand."""

    config: Path | None
    host: str | None
    count: int | None
    port: int | None
    data_type: str | None
    order: str | None
    cap: float | None
    gamma: float | None
    no_gamma: bool
    data_type_byte: int | None
    segment: str | None
    verbose: int
    json: bool
    segments: dict[str, Segment] = field(default_factory=dict)


class _Failure(click.ClickException):
    """A reported failure with an explicit exit code."""

    def __init__(self, message: str, exit_code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class _Group(click.Group):
    """Usage errors exit 1; click's default of 2 is reserved for ``DDPSendError``."""

    def make_context(
        self, info_name: str | None, args: list[str], parent: click.Context | None = None, **extra: Any
    ) -> click.Context:
        try:
            return super().make_context(info_name, args, parent, **extra)
        except click.UsageError as err:
            err.exit_code = EXIT_ERROR
            raise

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except click.UsageError as err:
            err.exit_code = EXIT_ERROR
            raise


class _StderrHandler(logging.Handler):
    """Writes to whatever ``sys.stderr`` is at emit time; drops tracebacks unless ``tracebacks``."""

    def __init__(self, *, tracebacks: bool) -> None:
        super().__init__()
        self._tracebacks = tracebacks

    def format(self, record: logging.LogRecord) -> str:
        if self._tracebacks:
            return super().format(record)
        saved = record.exc_info, record.exc_text, record.stack_info
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        try:
            return super().format(record)
        finally:
            record.exc_info, record.exc_text, record.stack_info = saved

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(self.format(record) + "\n")
        except Exception:
            self.handleError(record)


def _configure_logging(ctx: click.Context, verbose: int) -> None:
    logger = logging.getLogger("pyddp")
    handler = _StderrHandler(tracebacks=verbose >= 2)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    previous_level = logger.level
    logger.setLevel(logging.DEBUG if verbose >= 2 else logging.INFO if verbose == 1 else logging.WARNING)
    logger.addHandler(handler)

    def restore() -> None:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    ctx.call_on_close(restore)


def _options(ctx: click.Context) -> _Options:
    opts: _Options = ctx.obj
    return opts


@contextlib.contextmanager
def _reported_errors(ctx: click.Context) -> Iterator[None]:
    """Map library errors and Ctrl-C to exit codes; print a traceback only under ``-vv``."""
    try:
        yield
    except KeyboardInterrupt:
        click.echo("Interrupted", err=True)
        raise click.exceptions.Exit(EXIT_INTERRUPTED) from None
    except PyDDPError as err:
        if _options(ctx).verbose >= 2:
            click.echo("".join(traceback.format_exception(err)).rstrip(), err=True)
        code = EXIT_SEND_ERROR if isinstance(err, DDPSendError) else EXIT_ERROR
        raise _Failure(str(err), code) from err


def _config_error(path: Path, problem: str) -> _Failure:
    return _Failure(f"config file {path}: {problem}")


def _check_keys(path: Path, data: Mapping[Any, Any], allowed: frozenset[str], prefix: str) -> None:
    unknown = sorted(str(key) for key in data if key not in allowed)
    if unknown:
        raise _config_error(path, f"unknown key '{prefix}{unknown[0]}' (valid: {', '.join(sorted(allowed))})")


def _mapping(path: Path, value: object, where: str) -> Mapping[Any, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _config_error(path, f"{where} must be a mapping, got {type(value).__name__}")
    return value


def _load_config(opts: _Options) -> dict[str, Any]:
    """Read the config file (explicit, else ``./config.yaml`` if present); fill ``opts.segments``; return strip keys."""
    path = opts.config
    if path is None:
        if not DEFAULT_CONFIG.is_file():
            return {}
        path = DEFAULT_CONFIG
    try:
        data = yaml.safe_load(path.read_text())
    except OSError as err:
        raise _config_error(path, f"cannot read: {err.strerror or err}") from err
    except yaml.YAMLError as err:
        raise _config_error(path, f"invalid YAML: {err}") from err
    top = _mapping(path, data, "the top level")
    _check_keys(path, top, _TOP_KEYS, "")
    strip = _mapping(path, top.get("strip"), "strip")
    _check_keys(path, strip, _STRIP_KEYS, "strip.")
    for name, raw in _mapping(path, top.get("segments"), "segments").items():
        spec = _mapping(path, raw, f"segments.{name}")
        _check_keys(path, spec, _SEGMENT_KEYS, f"segments.{name}.")
        reverse = spec.get("reverse", False)
        if not isinstance(reverse, bool):
            raise _config_error(path, f"segments.{name}.reverse must be true or false, got {reverse!r}")
        start: Any = spec.get("start")  # Segment validates type and range
        length: Any = spec.get("length")
        try:
            opts.segments[str(name)] = Segment(str(name), start, length, reverse)
        except ConfigError as err:
            raise _config_error(path, str(err)) from err
    return dict(strip)


def _merged_values(ctx: click.Context) -> dict[str, Any]:
    """Library defaults < config file < command-line flags, unvalidated except that a host is required."""
    opts = _options(ctx)
    values: dict[str, Any] = {"brightness_cap": CLI_BRIGHTNESS_CAP, **_load_config(opts)}
    flags = {
        "host": opts.host,
        "pixel_count": opts.count,
        "port": opts.port,
        "data_type": opts.data_type,
        "color_order": opts.order,
        "brightness_cap": opts.cap,
        "gamma": opts.gamma,
        "data_type_byte": opts.data_type_byte,
    }
    values.update({key: value for key, value in flags.items() if value is not None})
    if opts.no_gamma:
        values["gamma"] = None
    if not values.get("host"):
        raise click.UsageError("no host: pass --host or set strip.host in config.yaml")
    return values


def _build_strip(ctx: click.Context) -> Strip:
    """Merge library defaults < config file < command-line flags into a validated ``Strip``."""
    values = _merged_values(ctx)
    if values.get("pixel_count") is None:
        raise click.UsageError("no pixel count: pass --count or set strip.pixel_count in config.yaml")
    raw_type = values.get("data_type", "rgb")
    data_type = _DATA_TYPES.get(raw_type.lower()) if isinstance(raw_type, str) else None
    if data_type is None:
        raise ConfigError(f"data_type must be one of {sorted(_DATA_TYPES)}, got {raw_type!r}")
    values["data_type"] = data_type
    values.setdefault("color_order", data_type.value[0])
    return Strip(**values)


def _resolve_segment(ctx: click.Context, strip: Strip, name: str | None) -> Segment | None:
    """A config segment or ad hoc ``START:LENGTH[:r]``; the command's ``--segment`` beats the global one."""
    opts = _options(ctx)
    name = name if name is not None else opts.segment
    if name is None:
        return None
    segment = opts.segments.get(name)
    if segment is None:
        match = _ADHOC_SEGMENT.fullmatch(name)
        if match is None:
            known = ", ".join(sorted(opts.segments)) or "none in config"
            raise click.UsageError(f"unknown segment {name!r}: not a config segment ({known}) or START:LENGTH[:r]")
        segment = Segment(name, int(match[1]), int(match[2]), reverse=match[3] is not None)
    strip.validate_segment(segment)
    return segment


def _wants_json(ctx: click.Context, as_json: bool) -> bool:
    return as_json or _options(ctx).json


def _segment_option(func: Any) -> Any:
    return click.option(
        "--segment", default=None, metavar="NAME", help="Config segment or START:LENGTH[:r]; overrides global."
    )(func)


def _json_option(func: Any) -> Any:
    return click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")(func)


@click.group(cls=_Group, context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--config",
    type=click.Path(dir_okay=False, path_type=Path),
    help="YAML config file [default: ./config.yaml if it exists].",
)
@click.option("--host", help="Controller host or IP.")
@click.option("--count", type=int, help="Pixel count.")
@click.option("--port", type=int, help=f"UDP port [default: {DDP_PORT}].")
@click.option(
    "--type", "data_type", type=click.Choice(sorted(_DATA_TYPES), case_sensitive=False), help="[default: rgb]"
)
@click.option("--order", help="Color order, e.g. GRB [default: RGB, or RGBW for --type rgbw].")
@click.option("--cap", type=float, help=f"Brightness cap, 0 to 1 [default: {CLI_BRIGHTNESS_CAP}].")
@click.option("--gamma", type=float, help=f"Gamma [default: {DEFAULT_GAMMA}].")
@click.option("--no-gamma", is_flag=True, help="Disable gamma correction.")
@click.option("--data-type-byte", type=int, help="Override the DDP header data type byte.")
@click.option("--segment", default=None, metavar="NAME", help="Config segment or START:LENGTH[:r] [default: all].")
@click.option("-v", "--verbose", count=True, help="-v INFO, -vv DEBUG (one line per packet) and tracebacks.")
@_json_option
@click.pass_context
def cli(
    ctx: click.Context,
    config: Path | None,
    host: str | None,
    count: int | None,
    port: int | None,
    data_type: str | None,
    order: str | None,
    cap: float | None,
    gamma: float | None,
    no_gamma: bool,
    data_type_byte: int | None,
    segment: str | None,
    verbose: int,
    as_json: bool,
) -> None:
    """Bench tool for DDP pixel controllers."""
    if gamma is not None and no_gamma:
        raise click.UsageError("--gamma and --no-gamma are mutually exclusive")
    if config is not None and not config.is_file():
        raise _config_error(config, "not found")
    _configure_logging(ctx, verbose)
    ctx.obj = _Options(
        config, host, count, port, data_type, order, cap, gamma, no_gamma, data_type_byte, segment, verbose, as_json
    )


@cli.command()
@click.option("--color", help="Hex, name, or RGB(W) color.")
@click.option("--kelvin", type=int, help="Color temperature instead of --color.")
@click.option("--brightness", type=float, default=1.0, show_default=True)
@_segment_option
@click.pass_context
def fill(ctx: click.Context, color: str | None, kelvin: int | None, brightness: float, segment: str | None) -> None:
    """Fill the target with one color, then exit."""
    if color is not None and kelvin is not None:
        raise click.UsageError("pass only one of --color or --kelvin")
    with _reported_errors(ctx):
        if color is not None:
            rgb: Color = parse_color(color)
        elif kelvin is not None:
            rgb = kelvin_to_rgb(kelvin)
        else:
            raise click.UsageError("pass --color or --kelvin")
        strip = _build_strip(ctx)
        target = _resolve_segment(ctx, strip, segment)

        async def run() -> None:
            async with DDPSender(strip) as ddp:
                await ddp.fill(target, rgb, brightness=brightness)

        asyncio.run(run())


@cli.command()
@_segment_option
@click.pass_context
def off(ctx: click.Context, segment: str | None) -> None:
    """Blank the target, then exit."""
    with _reported_errors(ctx):
        strip = _build_strip(ctx)
        target = _resolve_segment(ctx, strip, segment)

        async def run() -> None:
            async with DDPSender(strip) as ddp:
                await ddp.off(target)

        asyncio.run(run())


def _parse_params(pairs: Sequence[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip():
            raise click.BadParameter(f"expected key=value, got {pair!r}", param_hint="--param")
        params[key.strip()] = value
    return params


def _param_line(key: str, spec: ParamSpec) -> str:
    bounds = ""
    if spec.min is not None or spec.max is not None:
        low = "" if spec.min is None else f"{spec.min:g}"
        high = "" if spec.max is None else f"{spec.max:g}"
        bounds = f", {low}..{high}"
    return f"{key} ({spec.kind}, default {spec.default!r}{bounds})"


def _describe_params(effect: type[Effect]) -> str:
    lines = [f"  {_param_line(key, spec)}: {spec.description}" for key, spec in effect.params.items()]
    return f"{effect.name} params:\n" + ("\n".join(lines) if lines else "  (none)")


def _status(runner: EffectRunner, sender: DDPSender) -> str:
    r, s = runner.stats, sender.stats
    return (
        f"frames {r.frames_rendered}  dropped {r.frames_dropped}  packets {s.packets_sent}  "
        f"send errors {s.send_errors}  max render {r.max_render_seconds * 1000:.2f} ms"
    )


def _stats_json(name: str, segment: Segment, runner: EffectRunner, sender: DDPSender) -> str:
    r, s = runner.stats, sender.stats
    return json.dumps(
        {
            "effect": name,
            "segment": segment.name,
            "frames_rendered": r.frames_rendered,
            "frames_dropped": r.frames_dropped,
            "max_render_seconds": r.max_render_seconds,
            "packets_sent": s.packets_sent,
            "send_errors": s.send_errors,
        }
    )


async def _run_effect(
    sender: DDPSender,
    runner: EffectRunner,
    effect: type[Effect],
    segment: Segment,
    *,
    duration: float | None,
    keep: bool,
    as_json: bool,
) -> None:
    async with sender:
        await runner.start()
        status: contextlib.AbstractContextManager[Any] = (
            contextlib.nullcontext()
            if as_json
            else Live(
                get_renderable=lambda: _status(runner, sender),
                console=Console(stderr=True),
                refresh_per_second=_STATUS_REFRESH_PER_SECOND,
            )
        )
        try:
            with status, contextlib.suppress(TimeoutError):
                async with asyncio.timeout(duration):
                    await runner.wait()
        finally:
            # Also runs on Ctrl-C (the main task is cancelled): stop the runner, then blank unless asked not to.
            await runner.stop()
            if not keep and not effect.static and sender.is_open:
                await sender.off(segment)
            if as_json:
                click.echo(_stats_json(effect.name, segment, runner, sender))


@cli.command()
@click.argument("name")
@click.option("--fps", type=int, default=DEFAULT_FPS, show_default=True)
@click.option("--param", "params", multiple=True, metavar="KEY=VALUE", help="Effect parameter; repeatable.")
@click.option("--brightness", type=float, default=1.0, show_default=True)
@click.option("--seed", type=int, help="Seed for random effects [default: 0].")
@_segment_option
@click.option("--duration", type=click.FloatRange(min=0, min_open=True), help="Seconds to run [default: until Ctrl-C].")
@click.option("--keep", is_flag=True, help="Leave the last frame up on exit instead of blanking.")
@_json_option
@click.pass_context
def effect(
    ctx: click.Context,
    name: str,
    fps: int,
    params: tuple[str, ...],
    brightness: float,
    seed: int | None,
    segment: str | None,
    duration: float | None,
    keep: bool,
    as_json: bool,
) -> None:
    """Run effect NAME until Ctrl-C or --duration; blank on exit unless --keep or the effect is static."""
    parsed = _parse_params(params)
    with _reported_errors(ctx):
        effect_cls = get_effect(name)
        strip = _build_strip(ctx)
        target = _resolve_segment(ctx, strip, segment) or strip.full_segment()
        sender = DDPSender(strip)
        try:
            runner = EffectRunner(sender, target, effect_cls, fps=fps, params=parsed, brightness=brightness, seed=seed)
        except EffectError as err:
            raise EffectError(f"{err}\n{_describe_params(effect_cls)}") from err
        asyncio.run(
            _run_effect(
                sender, runner, effect_cls, target, duration=duration, keep=keep, as_json=_wants_json(ctx, as_json)
            )
        )


def _frame(length: int, channels: int, lit: Mapping[int, bytes]) -> bytes:
    pixels = bytearray(length * channels)
    for index, pixel in lit.items():
        if index < length:
            pixels[index * channels : (index + 1) * channels] = pixel
    return bytes(pixels)


async def _run_test(
    strip: Strip, segment: Segment, color: Color, *, delay: float, hold: float, loop: bool, as_json: bool
) -> None:
    channels, length = strip.channels, segment.length
    lit = solid_pixels(color, 1, channels)
    order: list[Color] = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
    labels = "pixel 0 red, 1 green, 2 blue"
    if channels == 4:
        order.append((0, 0, 0, 255))
        labels += ", 3 white (W channel)"
    async with DDPSender(strip) as ddp:
        try:
            click.echo(f"Order check: {labels}. If the colors differ, --order is wrong.", err=True)
            order_pixels = {i: solid_pixels(c, 1, channels) for i, c in enumerate(order)}
            await ddp.send_frame(_frame(length, channels, order_pixels), segment=segment)
            await asyncio.sleep(hold)
            while True:
                click.echo(f"Walk: one pixel from 0 to {length - 1}", err=True)
                for index in range(length):
                    await ddp.send_frame(_frame(length, channels, {index: lit}), segment=segment)
                    click.echo(json.dumps({"index": index}) if as_json else str(index))
                    await asyncio.sleep(delay)
                if not loop:
                    break
            click.echo("Ends: first and last pixel", err=True)
            await ddp.send_frame(_frame(length, channels, {0: lit, length - 1: lit}), segment=segment)
            await asyncio.sleep(hold)
        finally:
            if ddp.is_open:
                await ddp.off(segment)


@cli.command("test")
@click.option("--delay", type=click.FloatRange(min=0), default=0.05, show_default=True, help="Seconds per walk step.")
@click.option("--color", default="ffffff", show_default=True, help="Walk color.")
@click.option("--hold", type=click.FloatRange(min=0), default=3.0, show_default=True, help="Order and ends hold.")
@_segment_option
@click.option("--loop", is_flag=True, help="Repeat the walk until Ctrl-C.")
@_json_option
@click.pass_context
def test_command(
    ctx: click.Context, delay: float, color: str, hold: float, segment: str | None, loop: bool, as_json: bool
) -> None:
    """Hardware bring-up: color order check, single-pixel walk, ends; blanks on exit."""
    with _reported_errors(ctx):
        rgb = parse_color(color)
        strip = _build_strip(ctx)
        target = _resolve_segment(ctx, strip, segment) or strip.full_segment()
        asyncio.run(_run_test(strip, target, rgb, delay=delay, hold=hold, loop=loop, as_json=_wants_json(ctx, as_json)))


@cli.command()
@_json_option
@click.pass_context
def effects(ctx: click.Context, as_json: bool) -> None:
    """List effects and their params. Needs no host."""
    infos = list_effects()
    if _wants_json(ctx, as_json):
        listing = [
            {
                "name": info.name,
                "description": info.description,
                "static": info.static,
                "params": {key: spec._asdict() for key, spec in info.params.items()},
            }
            for info in infos
        ]
        click.echo(json.dumps(listing, indent=2))
        return
    table = Table("Effect", "Static", "Description", "Params")
    table.columns[0].no_wrap = True
    for info in infos:
        params = "\n".join(_param_line(key, spec) for key, spec in info.params.items())
        table.add_row(info.name, "yes" if info.static else "no", info.description, params)
    Console().print(table)


def _layout_json(layout: ControllerLayout) -> str:
    data_type = layout.data_type
    return json.dumps(
        {
            "host": layout.host,
            "source": layout.source,
            "manufacturer": layout.manufacturer,
            "model": layout.model,
            "firmware": layout.firmware,
            "protocol": layout.protocol,
            "pixel_count": layout.pixel_count,
            "channels": layout.channels,
            "data_type": data_type.name.lower() if data_type is not None else None,
            "outputs": [
                {
                    "index": o.index,
                    "start": o.start,
                    "length": o.length,
                    "reversed_by_controller": o.reversed_by_controller,
                }
                for o in layout.outputs
            ],
            "config": dict(layout.config),
            "status": dict(layout.status) if layout.status is not None else None,
        },
        indent=2,
        allow_nan=False,
    )


def _layout_text(layout: ControllerLayout) -> str:
    """Human summary plus a ready-to-paste config.yaml snippet."""
    parts: list[str] = []
    if layout.model or layout.manufacturer:
        parts.append(f"{layout.model or 'Controller'}" + (f" by {layout.manufacturer}" if layout.manufacturer else ""))
    if layout.firmware:
        parts.append(f"firmware {layout.firmware}")
    where = f"at {layout.host} via {layout.source}" + (f" (protocol {layout.protocol})" if layout.protocol else "")
    parts.append(where if parts else f"Controller {where}")
    lines = [", ".join(parts), f"{'Output':<8}{'Pixels':<8}{'Start':<7}Reversed by controller"]
    for o in layout.active_outputs:
        lines.append(f"{o.index + 1:<8}{o.length:<8}{o.start:<7}{'yes' if o.reversed_by_controller else 'no'}")
    unused = len(layout.outputs) - len(layout.active_outputs)
    if unused:
        lines.append(f"{unused} unused output{'s' if unused != 1 else ''}")
    data_type = layout.data_type
    type_name = data_type.name.lower() if data_type is not None else None
    lines.append(
        f"Total {layout.pixel_count} pixels, {layout.channels} channels per pixel"
        + (f" ({type_name})" if type_name else "")
    )
    if layout.pixel_count:
        host = layout.host if _PLAIN_YAML_HOST.fullmatch(layout.host) else json.dumps(layout.host)
        lines += ["", "config.yaml:", "strip:", f"  host: {host}", f"  pixel_count: {layout.pixel_count}"]
        if type_name:
            lines.append(f"  data_type: {type_name}")
        lines.append("segments:")
        lines += [f"  {s.name}: {{ start: {s.start}, length: {s.length} }}" for s in layout.segments()]
    return "\n".join(lines)


@cli.command()
@click.option(
    "--timeout", type=click.FloatRange(min=0, min_open=True), default=1.0, show_default=True, help="DDP query wait."
)
@click.option("--http-port", type=click.IntRange(1, 0xFFFF), default=80, show_default=True)
@click.option("--no-http", is_flag=True, help="Skip the HTTP /api/config fallback.")
@_json_option
@click.pass_context
def query(ctx: click.Context, timeout: float, http_port: int, no_http: bool, as_json: bool) -> None:
    """Ask the controller for its output layout (DDP query, then HTTP) and print a config snippet. Needs no --count."""
    with _reported_errors(ctx):
        values = _merged_values(ctx)
        host: str = values["host"]
        port: int = values.get("port", DDP_PORT)

        async def run() -> ControllerLayout | None:
            layout = await query_layout(host, port=port, timeout=timeout)
            if layout is None and not no_http:
                layout = await fetch_layout_http(host, port=http_port)
            return layout

        layout = asyncio.run(run())
    if layout is None:
        tried = "" if no_http else f" or an HTTP config request on port {http_port}"
        raise _Failure(f"{host} did not answer a DDP query on port {port}{tried}", EXIT_SEND_ERROR)
    if layout.protocol not in (None, "ddp"):
        click.echo(f"warning: controller protocol is {layout.protocol}; set it to DDP", err=True)
    if layout.data_type is None:
        click.echo(f"warning: pyddp cannot drive {layout.channels}-channel pixels", err=True)
    if not layout.pixel_count:
        click.echo("warning: controller reports no pixels on any output", err=True)
    click.echo(_layout_json(layout) if _wants_json(ctx, as_json) else _layout_text(layout))


@cli.command("version")
@click.pass_context
def version_command(ctx: click.Context) -> None:
    """Print the pyddp version."""
    installed = _dist_version("pyddp")
    click.echo(json.dumps({"version": installed}) if _options(ctx).json else f"pyddp {installed}")


def main() -> None:
    """Console-script entry point."""
    cli.main(prog_name="pyddp")


if __name__ == "__main__":  # pragma: no cover
    main()
