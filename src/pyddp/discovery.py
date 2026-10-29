"""Read a controller's output layout: DDP JSON query first, then the HTTP API some controllers serve instead.

The config document follows the DDP spec (``{"config": {"ports": [{"port", "ts", "l", "ss"}, ...]}}``) plus the
Minleon NDB extensions ``p`` (port number), ``rev``, ``proto``, and ``conv``. Stdlib only.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pyddp.const import DDP_PORT, DESTINATION_CONFIG, DESTINATION_STATUS, MAX_OFFSET, DataType
from pyddp.exceptions import ConfigError, DDPQueryError
from pyddp.protocol import decode_json_document
from pyddp.sender import DDPSender
from pyddp.strip import Segment, Strip

_LOGGER = logging.getLogger(__name__)

_MAX_HTTP_BODY = 64 * 1024
_MAX_HTTP_HEAD = 16 * 1024
_HTTP_CHUNK = 4096
_PATH = re.compile(r"/[!-~]*")
_HOSTNAME = re.compile(r"[A-Za-z0-9.-]+")
# Minleon "conv": input-to-pixel conversion, mapped to the channel count of the INPUT (what the sender sends).
_CONV_INPUT_CHANNELS = {0: 3, 1: 3, 2: 4, 3: 1, 4: 3, 5: 3, 6: 3, 7: 3, 8: 4, 9: 1, 10: 3, 11: 3, 12: 3, 13: 3, 14: 4}
_PROTOCOLS = {0: "ddp", 1: "artnet", 2: "e131"}  # Minleon "proto"
_DATA_TYPES = {data_type.channels: data_type for data_type in DataType}


@dataclass(frozen=True, slots=True)
class OutputInfo:
    """One controller output mapped into the flat pixel buffer."""

    index: int  # controller output number as reported, 0-based
    start: int  # first pixel in the flat buffer, 0-based
    length: int  # pixels; 0 means the output is unused
    reversed_by_controller: bool = False


@dataclass(frozen=True, slots=True)
class ControllerLayout:
    """What a controller reports about its outputs, and enough to build a ``Strip`` and ``Segment`` list."""

    host: str
    source: str  # "ddp" (UDP query) or "http"
    pixel_count: int  # end of the last active output; 0 when no output has pixels
    channels: int  # channels per input pixel used for the slot math
    outputs: tuple[OutputInfo, ...]
    protocol: str | None  # "ddp" | "artnet" | "e131" from Minleon "proto"
    manufacturer: str | None
    model: str | None
    firmware: str | None
    config: Mapping[str, Any]  # the inner "config" object, as received
    status: Mapping[str, Any] | None  # the inner "status" object, when fetched

    @property
    def active_outputs(self) -> tuple[OutputInfo, ...]:
        """Outputs with at least one pixel."""
        return tuple(output for output in self.outputs if output.length > 0)

    @property
    def data_type(self) -> DataType | None:
        """The pyddp data type matching ``channels``, or ``None`` when pyddp cannot drive it."""
        return _DATA_TYPES.get(self.channels)

    def segments(self) -> list[Segment]:
        """One segment per active output, named like the controller's 1-based UI; the controller applies ``rev``."""
        return [Segment(f"output{output.index + 1}", output.start, output.length) for output in self.active_outputs]


def _inner(document: object, key: str) -> Mapping[str, Any]:
    """The object under ``key`` when wrapped (``{"config": {...}}``), else the document itself."""
    if not isinstance(document, Mapping):
        raise DDPQueryError(f"{key} document must be a JSON object, got {type(document).__name__}")
    inner = document.get(key, document)
    if not isinstance(inner, Mapping):
        raise DDPQueryError(f"{key} must be a JSON object, got {type(inner).__name__}")
    return inner


def _as_int(value: object) -> int | None:
    """An int, or a decimal string (the DDP spec's example sends numbers as strings); else ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        try:
            return int(value)
        except ValueError:  # longer than Python's int-string digit limit
            return None
    return None


def _int_field(port: Mapping[str, Any], key: str, position: int, minimum: int) -> int:
    value = _as_int(port.get(key))
    if value is None or not minimum <= value <= MAX_OFFSET:
        shown = repr(port.get(key))
        shown = shown if len(shown) <= 40 else shown[:37] + "..."
        raise DDPQueryError(
            f"config port {position}: {key!r} must be an integer {minimum} to {MAX_OFFSET}, got {shown}"
        )
    return value


def _rev_field(port: Mapping[str, Any], position: int) -> bool:
    raw = port.get("rev", 0)
    if isinstance(raw, bool):
        return raw
    value = _as_int(raw)
    if value is None:
        raise DDPQueryError(f"config port {position}: 'rev' must be a boolean or integer, got {raw!r}")
    return value != 0


def _channels(config: Mapping[str, Any], ports: list[tuple[int, int, int, bool]]) -> int:
    conv = config.get("conv")
    if isinstance(conv, int) and conv in _CONV_INPUT_CHANNELS:
        return _CONV_INPUT_CHANNELS[conv]
    active = sorted((ss, length) for _, length, ss, _ in ports if length > 0)
    if len(active) >= 2:
        (ss1, length1), (ss2, _) = active[0], active[1]
        for channels in (3, 4):
            if ss2 - ss1 == length1 * channels:
                return channels
    return 3


def parse_config(
    document: Mapping[str, Any], *, host: str, source: str, status: Mapping[str, Any] | None = None
) -> ControllerLayout:
    """Build a layout from a config document (wrapped or inner) and an optional status document."""
    config = _inner(document, "config")
    raw_ports = config.get("ports")
    if not isinstance(raw_ports, list):
        raise DDPQueryError(f"config has no ports list, got {type(raw_ports).__name__}")
    ports: list[tuple[int, int, int, bool]] = []  # index, length, slot, reversed
    seen: set[int] = set()
    for position, port in enumerate(raw_ports):
        if not isinstance(port, Mapping):
            raise DDPQueryError(f"config port {position} must be a JSON object, got {type(port).__name__}")
        key = "port" if "port" in port else "p"
        index = _int_field(port, key, position, 0) if key in port else position
        if index in seen:
            raise DDPQueryError(f"config port {position}: output number {index} appears more than once")
        seen.add(index)
        length = _int_field(port, "l", position, 0)
        slot = _int_field(port, "ss", position, 1)
        ports.append((index, length, slot, _rev_field(port, position)))
    channels = _channels(config, ports)
    outputs: list[OutputInfo] = []
    for position, (index, length, slot, reverse) in enumerate(ports):
        if length > 0 and (slot - 1) % channels:
            raise DDPQueryError(
                f"config port {position}: start slot {slot} is not on a {channels}-channel pixel boundary"
            )
        if (slot - 1) + length * channels > MAX_OFFSET:
            raise DDPQueryError(f"config port {position}: {length} pixels at slot {slot} end past the DDP offset range")
        outputs.append(OutputInfo(index, (slot - 1) // channels, length, reverse))
    inner_status = _inner(status, "status") if status is not None else None

    def status_text(key: str) -> str | None:
        value = inner_status.get(key) if inner_status is not None else None
        return value if isinstance(value, str) else None

    proto = config.get("proto")
    return ControllerLayout(
        host=host,
        source=source,
        pixel_count=max((o.start + o.length for o in outputs if o.length > 0), default=0),
        channels=channels,
        outputs=tuple(outputs),
        protocol=_PROTOCOLS.get(proto) if isinstance(proto, int) else None,
        manufacturer=status_text("man"),
        model=status_text("mod"),
        firmware=status_text("ver"),
        config=config,
        status=inner_status,
    )


async def query_layout(
    host: str, *, port: int = DDP_PORT, timeout: float = 1.0, bind_host: str | None = None
) -> ControllerLayout | None:
    """Ask the controller over DDP (destination 250, then 251 best effort); ``None`` if config gets no answer."""
    sender = DDPSender(Strip(host=host, pixel_count=1, port=port), bind_host=bind_host)
    await sender.open()
    try:
        config = await sender.query(DESTINATION_CONFIG, timeout=timeout)
        if config is None:
            return None
        try:
            status = await sender.query(DESTINATION_STATUS, timeout=timeout)
        except DDPQueryError as exc:
            _LOGGER.debug("ignoring unusable DDP status from %s: %s", host, exc)
            status = None
    finally:
        await sender.close()
    return parse_config(config, host=host, source="ddp", status=status)


def _url_host(host: str) -> str:
    """``host`` as it goes in a URL: a hostname or IPv4 address as is, an IPv6 literal bracketed."""
    literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if ":" in literal:
        try:
            address = ipaddress.IPv6Address(literal)
        except ValueError:
            raise ConfigError(f"host {host!r} is not a valid IPv6 address") from None
        if address.scope_id is not None:
            raise ConfigError(f"host {host!r}: IPv6 scope IDs are not supported")
        return f"[{literal}]"
    if literal != host or not _HOSTNAME.fullmatch(host):
        raise ConfigError(f"host {host!r} must be a hostname, IPv4 address, or IPv6 address")
    return host


def _request_path(path: str) -> str:
    path = "/" + path.lstrip("/")
    if not _PATH.fullmatch(path):
        raise ConfigError(f"HTTP path {path!r} must be printable ASCII without spaces")
    return path


def _url(host: str, port: int, path: str) -> str:
    """``http://HOST:PORT/PATH``; ``host`` must be a hostname, IPv4 address, or IPv6 literal (brackets optional)."""
    return f"http://{_url_host(host)}:{port}{_request_path(path)}"


def _parse_head(head: bytes) -> tuple[int | None, dict[str, str]]:
    """Status code (``None`` if unparsable) and lower-cased headers of an HTTP response head."""
    status_line, *lines = head.decode("latin-1").split("\r\n")
    parts = status_line.split(" ", 2)
    status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
    headers: dict[str, str] = {}
    for line in lines:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return status, headers


async def _http_get(host: str, port: int, path: str, url: str) -> bytes | None:
    """HTTP/1.0 GET; the body of a 200 response, else ``None``. Redirects are never followed."""
    url_host = _url_host(host)
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.open_connection(url_host.strip("[]"), port, limit=_MAX_HTTP_HEAD)
        writer.write(f"GET {path} HTTP/1.0\r\nHost: {url_host}:{port}\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        status, headers = _parse_head(await reader.readuntil(b"\r\n\r\n"))
        if status != 200:
            _LOGGER.debug("HTTP %s answered %s", url, status)
            return None
        if "chunked" in headers.get("transfer-encoding", "").lower():
            _LOGGER.debug("HTTP %s uses chunked transfer encoding, which pyddp does not decode", url)
            return None
        raw_length = headers.get("content-length", "")
        length = int(raw_length) if raw_length.isdigit() else None
        if length is not None and length > _MAX_HTTP_BODY:
            raise DDPQueryError(f"{url} returned more than {_MAX_HTTP_BODY} bytes")
        body = bytearray()
        while len(body) <= _MAX_HTTP_BODY and (length is None or len(body) < length):
            chunk = await reader.read(_HTTP_CHUNK)
            if not chunk:
                break
            body += chunk
        if len(body) > _MAX_HTTP_BODY:
            raise DDPQueryError(f"{url} returned more than {_MAX_HTTP_BODY} bytes")
        return bytes(body[:length])
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


async def _fetch_document(host: str, port: int, path: str, timeout: float) -> dict[str, Any] | None:
    """The JSON object at ``path``; ``None`` if the request fails or is not a 200; ``DDPQueryError`` if not JSON."""
    path = _request_path(path)
    url = _url(host, port, path)
    try:
        async with asyncio.timeout(timeout):  # connect, request, head, and body together
            body = await _http_get(host, port, path, url)
    except (TimeoutError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        _LOGGER.debug("HTTP request to %s failed: %r", url, exc)
        return None
    if body is None:
        return None
    document = decode_json_document(body, what=url)
    if document is None:
        raise DDPQueryError(f"{url} returned an empty body")
    return document


async def fetch_layout_http(
    host: str,
    *,
    port: int = 80,
    timeout: float = 2.0,
    config_path: str = "/api/config",
    status_path: str = "/api/status",
) -> ControllerLayout | None:
    """Read the layout from the controller's HTTP API; ``None`` if it serves no config there."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError(f"port must be 1 to 65535, got {port!r}")
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError(f"timeout must be a positive finite number of seconds, got {timeout!r}")
    config = await _fetch_document(host, port, config_path, timeout)
    if config is None:
        return None
    try:
        status = await _fetch_document(host, port, status_path, timeout)
    except DDPQueryError as exc:
        _LOGGER.debug("ignoring unusable HTTP status from %s: %s", host, exc)
        status = None
    return parse_config(config, host=host, source="http", status=status)


async def discover(
    host: str, *, port: int = DDP_PORT, http_port: int = 80, timeout: float = 1.0
) -> ControllerLayout | None:
    """DDP query first, then the HTTP API; ``timeout`` bounds the DDP wait, HTTP uses its own default."""
    layout = await query_layout(host, port=port, timeout=timeout)
    if layout is not None:
        return layout
    return await fetch_layout_http(host, port=http_port)
