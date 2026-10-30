"""A minimal asyncio HTTP/1.0 client for talking to controllers on the LAN. Private; stdlib only.

One ``asyncio.timeout`` bounds connect, request, head, and body together, so a server dripping one byte at a time
cannot stall a caller. Head and body are size-capped, redirects are never followed, and the host is validated so it
can never steer the path, port, or scheme.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import math
import re
from dataclasses import dataclass

from pyddp.exceptions import ConfigError, DDPQueryError

_LOGGER = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
MAX_HEAD = 16 * 1024
_CHUNK = 4096
_PATH = re.compile(r"/[!-~]*")
_CHUNK_SIZE = re.compile(rb"[0-9A-Fa-f]{1,8}")
_HOSTNAME = re.compile(r"[A-Za-z0-9.-]+")

# What a failed exchange raises out of ``request``: no route, refused, reset, timed out, or a truncated/oversized head.
TRANSPORT_ERRORS = (TimeoutError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError)


@dataclass(frozen=True, slots=True)
class Response:
    """Status code (``None`` if unparsable) and body; the body is read only for a 200 (``None`` otherwise)."""

    status: int | None
    body: bytes | None


def validate_port(port: object) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError(f"port must be 1 to 65535, got {port!r}")
    return port


def validate_timeout(timeout: object) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not math.isfinite(timeout) or timeout <= 0:
        raise ConfigError(f"timeout must be a positive finite number of seconds, got {timeout!r}")
    return float(timeout)


def url_host(host: str) -> str:
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


def request_path(path: str) -> str:
    path = "/" + path.lstrip("/")
    if not _PATH.fullmatch(path):
        raise ConfigError(f"HTTP path {path!r} must be printable ASCII without spaces")
    return path


def url(host: str, port: int, path: str) -> str:
    """``http://HOST:PORT/PATH``; ``host`` must be a hostname, IPv4 address, or IPv6 literal (brackets optional)."""
    return f"http://{url_host(host)}:{port}{request_path(path)}"


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


async def request(
    host: str, port: int, path: str, *, timeout: float, method: str = "GET", body: bytes | None = None
) -> Response:
    """One HTTP/1.0 exchange under one deadline. Redirects are never followed.

    Raises ``ConfigError`` for a bad host or path, ``DDPQueryError`` for a body over ``MAX_BODY``, and one of
    ``TRANSPORT_ERRORS`` when the exchange fails or the deadline passes.
    """
    host_part = url_host(host)
    path = request_path(path)
    full_url = f"http://{host_part}:{port}{path}"
    head = f"{method} {path} HTTP/1.0\r\nHost: {host_part}:{port}\r\nConnection: close\r\n"
    if body is not None:
        head += f"Content-Length: {len(body)}\r\n"
    payload = (head + "\r\n").encode() + (body or b"")
    _LOGGER.debug("HTTP %s %s (%d body bytes)", method, full_url, len(body or b""))
    async with asyncio.timeout(timeout):  # connect, request, head, body, and close together
        return await _exchange(host_part, port, payload, method, full_url)


async def _read_chunked(reader: asyncio.StreamReader, full_url: str) -> bytes:
    """A chunked body, capped at ``MAX_BODY``; trailers are skipped. ``DDPQueryError`` on malformed framing."""
    received = bytearray()
    while True:
        size_text = (await reader.readuntil(b"\r\n"))[:-2].split(b";", 1)[0].strip()
        if not _CHUNK_SIZE.fullmatch(size_text):
            raise DDPQueryError(f"{full_url} sent a malformed chunk size {size_text[:20]!r}")
        size = int(size_text, 16)
        if size == 0:
            break
        if len(received) + size > MAX_BODY:
            raise DDPQueryError(f"{full_url} returned more than {MAX_BODY} bytes")
        received += await reader.readexactly(size)
        if await reader.readexactly(2) != b"\r\n":
            raise DDPQueryError(f"{full_url} sent a chunk without its closing CRLF")
    while (await reader.readuntil(b"\r\n")) != b"\r\n":
        pass  # trailer fields
    return bytes(received)


async def _exchange(host_part: str, port: int, payload: bytes, method: str, full_url: str) -> Response:
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.open_connection(host_part.strip("[]"), port, limit=MAX_HEAD)
        writer.write(payload)
        await writer.drain()
        status, headers = _parse_head(await reader.readuntil(b"\r\n\r\n"))
        if status != 200:
            _LOGGER.debug("HTTP %s %s answered %s", method, full_url, status)
            return Response(status, None)
        if "chunked" in headers.get("transfer-encoding", "").lower():
            # The NDB-Pro answers HTTP/1.0 requests with HTTP/1.1 chunked bodies.
            return Response(status, await _read_chunked(reader, full_url))
        raw_length = headers.get("content-length", "")
        length = int(raw_length) if raw_length.isdigit() else None
        if length is not None and length > MAX_BODY:
            raise DDPQueryError(f"{full_url} returned more than {MAX_BODY} bytes")
        received = bytearray()
        while len(received) <= MAX_BODY and (length is None or len(received) < length):
            chunk = await reader.read(_CHUNK)
            if not chunk:
                break
            received += chunk
        if len(received) > MAX_BODY:
            raise DDPQueryError(f"{full_url} returned more than {MAX_BODY} bytes")
        return Response(status, bytes(received[:length]))
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
