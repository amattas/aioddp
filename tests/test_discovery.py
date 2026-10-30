"""Discovery: config parsing, DDP query layout, HTTP fallback."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import pytest

from pyddp.const import DESTINATION_STATUS, MAX_OFFSET, DataType
from pyddp.discovery import (
    ControllerLayout,
    OutputInfo,
    _url,
    discover,
    fetch_layout_http,
    parse_config,
    query_layout,
)
from pyddp.exceptions import ConfigError, DDPQueryError
from pyddp.strip import Segment
from tests.conftest import (
    NDB_CONFIG_JSON,
    NDB_STATUS_JSON,
    HTTPController,
    UDPListener,
    ndb_responder,
    reply_packet,
)

NDB_CONFIG: dict[str, Any] = json.loads(NDB_CONFIG_JSON)
NDB_STATUS: dict[str, Any] = json.loads(NDB_STATUS_JSON)


def _closed_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def _assert_ndb_layout(layout: ControllerLayout, *, source: str) -> None:
    assert (layout.host, layout.source) == ("127.0.0.1", source)
    assert (layout.pixel_count, layout.channels, layout.protocol, layout.data_type) == (122, 3, "ddp", DataType.RGB)
    assert layout.active_outputs == (OutputInfo(0, 0, 80, True), OutputInfo(1, 80, 42, False))
    assert [o.index for o in layout.outputs] == list(range(8))
    assert all(o.length == 0 for o in layout.outputs[2:])
    assert layout.segments() == [Segment("output1", 0, 80), Segment("output2", 80, 42)]
    assert all(not s.reverse for s in layout.segments())
    assert layout.config == NDB_CONFIG["config"]
    assert (layout.manufacturer, layout.model, layout.firmware) == ("3WL", "NDBPRO", "2.0")
    assert layout.status == NDB_STATUS["status"]


@pytest.mark.parametrize("wrapped", [True, False], ids=["wrapped", "inner"])
def test_parse_ndb_pro_config(wrapped: bool) -> None:
    config = NDB_CONFIG if wrapped else NDB_CONFIG["config"]
    status = NDB_STATUS if wrapped else NDB_STATUS["status"]
    _assert_ndb_layout(parse_config(config, host="127.0.0.1", source="http", status=status), source="http")


def _ports(*specs: tuple[int, int], key: str = "port") -> list[dict[str, Any]]:
    return [{key: i, "ts": 0, "l": length, "ss": ss} for i, (length, ss) in enumerate(specs)]


@pytest.mark.parametrize(
    ("config", "channels", "data_type", "starts"),
    [
        ({"ports": _ports((100, 1), (50, 301), (0, 451))}, 3, DataType.RGB, [0, 100]),
        ({"ports": _ports((100, 1), (50, 401))}, 4, DataType.RGBW, [0, 100]),
        ({"ports": _ports((100, 1))}, 3, DataType.RGB, [0]),
        ({"conv": 2, "ports": _ports((10, 1), (5, 41), key="p")}, 4, DataType.RGBW, [0, 10]),
        ({"conv": 3, "ports": _ports((10, 1), (5, 11), key="p")}, 1, None, [0, 10]),
        ({"conv": 99, "ports": _ports((10, 1), (5, 31), key="p")}, 3, DataType.RGB, [0, 10]),
        ({"ports": [{"l": 10, "ss": 1}, {"l": "5", "ss": "31"}]}, 3, DataType.RGB, [0, 10]),
    ],
    ids=["stride-3", "stride-4", "single-port", "conv-rgbw", "conv-single", "conv-unknown", "positional"],
)
def test_parse_config_channels_and_starts(
    config: dict[str, Any], channels: int, data_type: DataType | None, starts: list[int]
) -> None:
    layout = parse_config({"config": config}, host="h", source="ddp")
    assert (layout.channels, layout.data_type) == (channels, data_type)
    assert [o.start for o in layout.active_outputs] == starts
    assert [o.index for o in layout.outputs] == list(range(len(config["ports"])))
    assert (layout.protocol, layout.status, layout.model) == (None, None, None)


@pytest.mark.parametrize(
    ("rev", "expected"), [(True, True), (1, True), ("1", True), (0, False), ("0", False), (False, False), (None, False)]
)
def test_parse_config_rev_flag(rev: Any, expected: bool) -> None:
    port: dict[str, Any] = {"l": 1, "ss": 1} | ({} if rev is None else {"rev": rev})
    (output,) = parse_config({"ports": [port]}, host="h", source="ddp").outputs
    assert output.reversed_by_controller is expected


def test_parse_config_without_active_outputs_has_zero_pixels() -> None:
    layout = parse_config({"ports": _ports((0, 1), (0, 2))}, host="h", source="ddp", status={"mod": "X"})
    assert (layout.pixel_count, layout.active_outputs, layout.segments(), layout.model) == (0, (), [], "X")


@pytest.mark.parametrize(
    ("document", "status", "message"),
    [
        ([], None, "config"),
        ({"config": 5}, None, "config"),
        ({"config": {}}, None, "ports"),
        ({"ports": {"p": 0}}, None, "ports"),
        ({"ports": [5]}, None, "port 0"),
        ({"ports": [{"ss": 1}]}, None, "port 0"),
        ({"ports": [{"l": -1, "ss": 1}]}, None, "port 0"),
        ({"ports": [{"l": True, "ss": 1}]}, None, "port 0"),
        ({"ports": [{"l": 1, "ss": 1}, {"l": 1, "ss": 0}]}, None, "port 1"),
        ({"ports": [{"p": "x", "l": 1, "ss": 1}]}, None, "port 0"),
        ({"ports": [{"l": 10, "ss": 1}, {"l": 5, "ss": 32}]}, None, "slot 32"),
        ({"ports": _ports((1, 1))}, {"status": []}, "status"),
        ({"ports": [{"l": "9" * 5000, "ss": 1}]}, None, "port 0"),
        ({"ports": [{"l": 1, "ss": 1}, {"l": 3, "ss": MAX_OFFSET - 5}]}, None, "port 1"),
        ({"ports": [{"l": MAX_OFFSET, "ss": 1}]}, None, "port 0"),
        ({"ports": [{"p": 0, "l": 1, "ss": 1}, {"p": 0, "l": 1, "ss": 4}]}, None, "port 1"),
        ({"ports": [{"l": 1, "ss": 1, "rev": "yes"}]}, None, "port 0"),
        ({"ports": [{"l": 1, "ss": 1, "rev": [1]}]}, None, "port 0"),
    ],
    ids=[
        "not-mapping",
        "config-not-mapping",
        "no-ports",
        "ports-not-list",
        "port-not-mapping",
        "missing-l",
        "negative-l",
        "bool-l",
        "zero-ss",
        "bad-index",
        "misaligned-slot",
        "status-not-mapping",
        "huge-digit-string",
        "past-max-offset",
        "length-past-max-offset",
        "duplicate-index",
        "rev-word",
        "rev-list",
    ],
)
def test_parse_config_rejects_malformed(document: Any, status: Any, message: str) -> None:
    with pytest.raises(DDPQueryError, match=message):
        parse_config(document, host="h", source="ddp", status=status)


async def test_query_layout_over_ddp(udp_listener: UDPListener) -> None:
    udp_listener.responder = ndb_responder
    layout = await query_layout(udp_listener.host, port=udp_listener.port)
    assert layout is not None
    _assert_ndb_layout(layout, source="ddp")


async def test_query_layout_tolerates_unusable_status(udp_listener: UDPListener) -> None:
    def responder(packet: bytes, addr: tuple[str | Any, int]) -> list[bytes]:
        if packet[3] == DESTINATION_STATUS:
            return [reply_packet(DESTINATION_STATUS, b"{bad")]
        return ndb_responder(packet, addr)

    udp_listener.responder = responder
    layout = await query_layout(udp_listener.host, port=udp_listener.port, timeout=0.2)
    assert layout is not None
    assert (layout.pixel_count, layout.status) == (122, None)


async def test_fetch_layout_http(http_controller: HTTPController) -> None:
    layout = await fetch_layout_http(http_controller.host, port=http_controller.port)
    assert layout is not None
    _assert_ndb_layout(layout, source="http")
    assert http_controller.requests == ["/api/config", "/api/status"]


@pytest.mark.parametrize(
    ("routes", "expected"),
    [
        ({"/api/config": None}, None),
        ({"/api/status": None}, "no-status"),
        ({"/api/status": b"<html>"}, "no-status"),
        ({"/api/config": b"<html>"}, DDPQueryError),
        ({"/api/config": b"   "}, DDPQueryError),
        ({"/api/config": b" " * (64 * 1024 + 1)}, DDPQueryError),
    ],
    ids=["config-404", "status-404", "status-not-json", "config-not-json", "config-empty", "config-too-large"],
)
async def test_fetch_layout_http_failures(
    http_controller: HTTPController, routes: dict[str, bytes | None], expected: str | type[Exception] | None
) -> None:
    http_controller.routes.update(routes)
    if isinstance(expected, type):
        with pytest.raises(expected, match="/api/config"):
            await fetch_layout_http(http_controller.host, port=http_controller.port)
        return
    layout = await fetch_layout_http(http_controller.host, port=http_controller.port)
    if expected is None:
        assert layout is None
    else:
        assert layout is not None
        assert (layout.pixel_count, layout.status, layout.model) == (122, None, None)


@pytest.mark.parametrize(
    ("host", "path", "url"),
    [
        ("10.0.0.5", "/api/config", "http://10.0.0.5:80/api/config"),
        ("fe80::1", "api/status", "http://[fe80::1]:80/api/status"),
        ("[::1]", "/api/config", "http://[::1]:80/api/config"),
        ("::1", "/api/config", "http://[::1]:80/api/config"),
        ("ndb-pro.local", "/api/config", "http://ndb-pro.local:80/api/config"),
    ],
)
def test_http_url_brackets_ipv6(host: str, path: str, url: str) -> None:
    assert _url(host, 80, path) == url


@pytest.mark.parametrize(
    ("host", "config_path"),
    [
        *(
            (host, "/api/config")
            for host in ("[zz", "127.0.0.1:99999/x?", "a\r\nb", "a@127.0.0.1", "127.0.0.1/x#", "", "[1.2.3.4]")
        ),
        ("fe80::1%en0", "/api/config"),
        ("127.0.0.1", "/api/config HTTP/1.0\r\nX-Injected: 1"),
    ],
)
async def test_fetch_layout_http_rejects_malformed_host_or_path(host: str, config_path: str) -> None:
    with pytest.raises(ConfigError, match="host|path"):
        await fetch_layout_http(host, config_path=config_path)


@pytest.mark.parametrize(
    ("port", "timeout"),
    [(0, 1.0), (-1, 1.0), (99999, 1.0), (80, 0), (80, -1.0), (80, float("nan")), (80, float("inf")), (80, True)],
)
async def test_fetch_layout_http_rejects_bad_port_or_timeout(port: int, timeout: float) -> None:
    with pytest.raises(ConfigError, match="port|timeout"):
        await fetch_layout_http("127.0.0.1", port=port, timeout=timeout)


@pytest.mark.parametrize(
    ("mode", "body", "expected"),
    [
        ("nolength", NDB_CONFIG_JSON, 122),
        ("nolength", b" " * (64 * 1024 + 1), "more than"),
        ("chunked", NDB_CONFIG_JSON, 122),
        ("chunked", b" " * (64 * 1024 + 1), "more than"),
        ("badchunk", NDB_CONFIG_JSON, "chunk size"),
    ],
    ids=["unframed", "unframed-oversized", "chunked", "chunked-oversized", "chunked-malformed"],
)
async def test_fetch_layout_http_reads_unframed_or_chunked_body(
    http_controller: HTTPController, mode: str, body: bytes, expected: int | str
) -> None:
    http_controller.routes["/api/config"] = body
    http_controller.slow["/api/config"] = mode
    if isinstance(expected, int):
        layout = await fetch_layout_http(http_controller.host, port=http_controller.port)
        assert layout is not None
        assert layout.pixel_count == expected
    else:
        with pytest.raises(DDPQueryError, match=expected):
            await fetch_layout_http(http_controller.host, port=http_controller.port)


@pytest.mark.parametrize("mode", ["body", "head"])
async def test_fetch_layout_http_slow_config_is_none_within_deadline(
    http_controller: HTTPController, mode: str
) -> None:
    http_controller.slow["/api/config"] = mode
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await fetch_layout_http(http_controller.host, port=http_controller.port, timeout=0.3) is None
    assert loop.time() - started < 1.0


async def test_fetch_layout_http_refuses_redirects(http_controller: HTTPController) -> None:
    connections: list[int] = []

    async def count(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append(1)
        writer.close()
        await writer.wait_closed()

    other = await asyncio.start_server(count, "127.0.0.1", 0)
    try:
        other_port = other.sockets[0].getsockname()[1]
        http_controller.redirects["/api/config"] = f"http://127.0.0.1:{other_port}/api/config"
        assert await fetch_layout_http(http_controller.host, port=http_controller.port) is None
    finally:
        other.close()
        await other.wait_closed()
    assert connections == []


async def test_fetch_layout_http_closed_port_returns_none() -> None:
    dead_port = _closed_tcp_port()
    assert await fetch_layout_http("127.0.0.1", port=dead_port, timeout=0.5) is None


@pytest.mark.parametrize("udp_answers", [True, False], ids=["ddp", "http-fallback"])
async def test_discover_prefers_ddp_then_falls_back_to_http(
    udp_listener: UDPListener, http_controller: HTTPController, udp_answers: bool
) -> None:
    if udp_answers:
        udp_listener.responder = ndb_responder
    layout = await discover(udp_listener.host, port=udp_listener.port, http_port=http_controller.port, timeout=0.1)
    assert layout is not None
    assert layout.source == ("ddp" if udp_answers else "http")
    assert http_controller.requests == ([] if udp_answers else ["/api/config", "/api/status"])


async def test_discover_returns_none_when_nothing_answers(udp_listener: UDPListener) -> None:
    dead_port = _closed_tcp_port()
    assert await discover(udp_listener.host, port=udp_listener.port, http_port=dead_port, timeout=0.05) is None
