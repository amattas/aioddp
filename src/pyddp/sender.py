"""DDPSender: the one object that owns a socket to a DDP controller.

Every frame goes through ``process_frame``, so the strip's brightness cap cannot be bypassed.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from pyddp.color import Color, expand_rgb_to_rgbw, process_frame, solid_pixels
from pyddp.const import DESTINATION_CONFIG, HEADER_SIZE
from pyddp.exceptions import ConfigError, DDPQueryError, DDPSendError
from pyddp.protocol import (
    DDPHeader,
    SequenceCounter,
    build_packets,
    build_push_packet,
    build_query_packet,
    decode_json_document,
)
from pyddp.strip import Segment, Strip

_LOGGER = logging.getLogger(__name__)

_MIN_KEEPALIVE_INTERVAL = 0.5
_BLACK: Color = (0, 0, 0)


@dataclass(slots=True)
class SenderStats:
    """Live counters for one sender; not reset by ``close()``. ``bytes_sent`` counts whole datagrams."""

    packets_sent: int = 0
    bytes_sent: int = 0
    frames_sent: int = 0
    keepalives_sent: int = 0
    replies_received: int = 0
    send_errors: int = 0
    last_send: float | None = None  # loop.time()
    last_frame: bytes = b""  # copy of the shadow buffer at last send


class _DDPProtocol(asyncio.DatagramProtocol):
    """Routes transport callbacks back to the owning sender; never raises."""

    def __init__(self, sender: DDPSender) -> None:
        self._sender = sender

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        self._sender._on_datagram_received(data)

    def error_received(self, exc: Exception) -> None:
        self._sender._on_error_received(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self._sender._on_connection_lost(self, exc)


class DDPSender:
    """Async DDP sender for one strip. Bound to the loop it was opened on; not thread-safe."""

    def __init__(
        self,
        strip: Strip,
        *,
        keepalive_interval: float | None = None,
        bind_host: str | None = None,
    ) -> None:
        if keepalive_interval is not None and not keepalive_interval >= _MIN_KEEPALIVE_INTERVAL:
            raise ConfigError(
                f"keepalive_interval must be None or >= {_MIN_KEEPALIVE_INTERVAL} seconds, got {keepalive_interval!r}"
            )
        self._strip = strip
        self._keepalive_interval = keepalive_interval
        self._bind_host = bind_host
        self._seq = SequenceCounter()
        self._shadow = bytearray(strip.frame_size)
        self._stats = SenderStats()
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _DDPProtocol | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._seen_errnos: set[int | None] = set()
        self._pending: dict[int, asyncio.Future[bytes]] = {}  # destination -> reply payload
        self._reply_chunks: dict[int, dict[int, bytes]] = {}  # destination -> offset -> data; repeats replace
        self._lifecycle = asyncio.Lock()  # serializes open() and close()

    @property
    def is_open(self) -> bool:
        """Whether the socket is open and sends are accepted."""
        return self._transport is not None

    @property
    def strip(self) -> Strip:
        """The strip this sender targets."""
        return self._strip

    @property
    def stats(self) -> SenderStats:
        """The live stats object."""
        return self._stats

    async def open(self) -> None:
        """Resolve the host and open the UDP socket; start keepalive if configured. No-op when open."""
        async with self._lifecycle:
            if self.is_open:
                return
            await self._stop_keepalive()  # reap a task left by connection_lost before reopening
            loop = asyncio.get_running_loop()
            strip = self._strip
            local_addr = (self._bind_host, 0) if self._bind_host else None
            try:
                transport, protocol = await loop.create_datagram_endpoint(
                    lambda: _DDPProtocol(self), remote_addr=(strip.host, strip.port), local_addr=local_addr
                )
            except (OSError, ValueError) as exc:  # ValueError covers UnicodeError from IDNA-encoding the host
                raise DDPSendError(f"cannot open DDP socket to {strip.host}:{strip.port}: {exc}") from exc
            self._transport, self._protocol = transport, protocol
            if self._keepalive_interval is not None:
                self._keepalive_task = loop.create_task(
                    self._keepalive(self._keepalive_interval), name=f"pyddp-keepalive-{strip.host}"
                )

    async def close(self) -> None:
        """Stop keepalive and close the socket. Idempotent; never blanks the lights."""
        async with self._lifecycle:
            try:
                await self._stop_keepalive()
            finally:
                transport, self._transport, self._protocol = self._transport, None, None
                if transport is not None:
                    transport.close()
                self._fail_pending()

    async def __aenter__(self) -> DDPSender:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def send_frame(
        self,
        pixels: bytes,
        *,
        segment: Segment | None = None,
        brightness: float = 1.0,
        push: bool = True,
    ) -> None:
        """Send canonical RGB(W) ``pixels`` to ``segment`` (whole strip by default) through the color pipeline."""
        transport = self._require_open()
        strip = self._strip
        target = segment if segment is not None else strip.full_segment()
        strip.validate_segment(target)
        expected = strip.byte_length(target)
        if len(pixels) != expected:
            if strip.channels == 4 and len(pixels) == target.length * 3:
                pixels = expand_rgb_to_rgbw(pixels)
            else:
                raise ConfigError(
                    f"pixel data length {len(pixels)} does not match segment {target.name!r} "
                    f"({target.length} pixels, {expected} bytes)"
                )
        wire = process_frame(pixels, strip, brightness=brightness, reverse=target.reverse)
        offset = strip.byte_offset(target)
        self._shadow[offset : offset + len(wire)] = wire
        packets = build_packets(
            wire,
            base_offset=offset,
            data_type=strip.wire_data_type,
            sequence=self._seq,
            push=push,
            destination=strip.destination_id,
        )
        self._send(transport, packets)
        self._stats.frames_sent += 1
        self._stats.last_frame = bytes(self._shadow)

    async def fill(self, segment: Segment | None, color: Color, *, brightness: float = 1.0, push: bool = True) -> None:
        """Send one solid ``color`` to ``segment`` (whole strip when ``None``)."""
        length = segment.length if segment is not None else self._strip.pixel_count
        pixels = solid_pixels(color, length, self._strip.channels)
        await self.send_frame(pixels, segment=segment, brightness=brightness, push=push)

    async def off(self, segment: Segment | None = None, *, push: bool = True) -> None:
        """Blank ``segment`` (whole strip by default)."""
        await self.fill(segment, _BLACK, brightness=1.0, push=push)

    async def push(self) -> None:
        """Send a header-only latch packet."""
        transport = self._require_open()
        strip = self._strip
        packet = build_push_packet(sequence=self._seq, data_type=strip.wire_data_type, destination=strip.destination_id)
        self._send(transport, [packet])

    async def query(self, destination: int = DESTINATION_CONFIG, *, timeout: float = 1.0) -> dict[str, Any] | None:
        """Send a DDP query and return the reply's JSON object, or ``None`` on timeout or an empty reply.

        Raises ``DDPQueryError`` for a reply that is not a JSON object, ``DDPSendError`` if the sender is not open
        or closes while waiting, and ``RuntimeError`` if a query to ``destination`` is already pending.
        """
        transport = self._require_open()
        valid = isinstance(timeout, int | float) and not isinstance(timeout, bool) and math.isfinite(timeout)
        if not valid or timeout <= 0:
            raise ConfigError(f"timeout must be a positive finite number of seconds, got {timeout!r}")
        if destination in self._pending:
            raise RuntimeError(f"a query to destination {destination} is already pending on this sender")
        packet = build_query_packet(sequence=self._seq, destination=destination)
        future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._pending[destination] = future
        self._reply_chunks[destination] = {}
        try:
            self._send(transport, [packet])
            try:
                async with asyncio.timeout(timeout):
                    payload = await future
            except TimeoutError:
                _LOGGER.debug("no reply from %s to query for destination %d", self._strip.host, destination)
                return None
        finally:
            del self._pending[destination]
            del self._reply_chunks[destination]
        _LOGGER.debug("reply from %s for destination %d: %r", self._strip.host, destination, payload)
        return decode_json_document(payload, what=f"{self._strip.host} destination {destination}")

    def _require_open(self) -> asyncio.DatagramTransport:
        if self._transport is None:
            raise DDPSendError(f"sender for {self._strip.host}:{self._strip.port} is not open")
        return self._transport

    def _send(self, transport: asyncio.DatagramTransport, packets: list[bytes]) -> None:
        # No await in here: one frame's packets stay contiguous on the wire.
        stats = self._stats
        debug = _LOGGER.isEnabledFor(logging.DEBUG)
        for packet in packets:
            if debug:
                header = DDPHeader.decode(packet)
                _LOGGER.debug(
                    "send %s seq=%d type=0x%02x dest=%d offset=%d length=%d push=%s query=%s",
                    self._strip.host,
                    header.sequence,
                    header.data_type,
                    header.destination,
                    header.offset,
                    header.length,
                    header.push,
                    header.query,
                )
            try:
                transport.sendto(packet)
            except OSError as exc:
                self._on_error_received(exc)
                continue
            stats.packets_sent += 1
            stats.bytes_sent += len(packet)
        stats.last_send = asyncio.get_running_loop().time()

    async def _keepalive(self, interval: float) -> None:
        loop = asyncio.get_running_loop()
        strip = self._strip
        while True:
            await asyncio.sleep(interval)
            transport = self._transport
            last_send = self._stats.last_send
            if transport is None or not self._stats.frames_sent or last_send is None:
                continue
            if loop.time() - last_send < interval:
                continue
            packets = build_packets(
                bytes(self._shadow),
                base_offset=0,
                data_type=strip.wire_data_type,
                sequence=self._seq,
                push=True,
                destination=strip.destination_id,
            )
            self._send(transport, packets)
            self._stats.keepalives_sent += 1

    async def _stop_keepalive(self) -> None:
        task, self._keepalive_task = self._keepalive_task, None
        if task is None:
            return
        task.cancel()
        current = asyncio.current_task()
        cancelling = current.cancelling() if current is not None else 0
        try:
            await task
        except asyncio.CancelledError:
            # Swallow the keepalive's cancellation, but not a new cancellation of the caller while waiting.
            if current is not None and current.cancelling() > cancelling:
                raise

    def _on_datagram_received(self, data: bytes) -> None:
        """Collect reply packets for a pending query; on push, join them in offset order if none is missing."""
        if len(data) < HEADER_SIZE:
            return
        header = DDPHeader.decode(data)
        future = self._pending.get(header.destination)
        if not header.reply or future is None or future.done():
            return
        chunks = self._reply_chunks[header.destination]
        chunks[header.offset] = data[HEADER_SIZE : HEADER_SIZE + header.length]
        if not header.push:
            return
        expected = 0
        for offset in sorted(chunks):
            if offset != expected:
                future.set_exception(
                    DDPQueryError(
                        f"reply from {self._strip.host} destination {header.destination} is incomplete: "
                        f"missing bytes at offset {expected}"
                    )
                )
                return
            expected += len(chunks[offset])
        future.set_result(b"".join(chunks[offset] for offset in sorted(chunks)))
        self._stats.replies_received += 1

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(DDPSendError("sender closed"))

    def _on_error_received(self, exc: Exception) -> None:
        self._stats.send_errors += 1
        code = exc.errno if isinstance(exc, OSError) else None
        level = logging.DEBUG if code in self._seen_errnos else logging.WARNING
        self._seen_errnos.add(code)
        _LOGGER.log(level, "DDP send error to %s:%d: %s", self._strip.host, self._strip.port, exc)

    def _on_connection_lost(self, protocol: _DDPProtocol, exc: Exception | None) -> None:
        if protocol is not self._protocol:
            return  # stale callback from a transport this sender already closed
        self._transport, self._protocol = None, None
        self._fail_pending()
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()  # close() or the next open() awaits it
        if exc is not None:
            _LOGGER.warning("DDP connection lost to %s:%d: %s", self._strip.host, self._strip.port, exc)
