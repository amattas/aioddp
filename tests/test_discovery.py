"""Discovery: config parsing and DDP query layout."""

from __future__ import annotations

import json
from typing import Any

import pytest

from pyddp.const import DESTINATION_STATUS, MAX_OFFSET, DataType
from pyddp.discovery import ControllerLayout, OutputInfo, discover, parse_config, query_layout
from pyddp.exceptions import DDPQueryError
from pyddp.strip import Segment
from tests.conftest import CONFIG_JSON, STATUS_JSON, UDPListener, config_responder, reply_packet

CONFIG: dict[str, Any] = json.loads(CONFIG_JSON)
STATUS: dict[str, Any] = json.loads(STATUS_JSON)


def _assert_layout(layout: ControllerLayout, *, source: str) -> None:
    assert (layout.host, layout.source) == ("127.0.0.1", source)
    assert (layout.pixel_count, layout.channels, layout.protocol, layout.data_type) == (122, 3, "ddp", DataType.RGB)
    assert layout.active_outputs == (OutputInfo(0, 0, 80, True), OutputInfo(1, 80, 42, False))
    assert [o.index for o in layout.outputs] == list(range(8))
    assert all(o.length == 0 for o in layout.outputs[2:])
    assert layout.segments() == [Segment("output1", 0, 80), Segment("output2", 80, 42)]
    assert all(not s.reverse for s in layout.segments())
    assert layout.config == CONFIG["config"]
    assert (layout.manufacturer, layout.model, layout.firmware) == ("3WL", "NDBPRO", "2.0")
    assert layout.status == STATUS["status"]


@pytest.mark.parametrize("wrapped", [True, False], ids=["wrapped", "inner"])
def test_parse_captured_config(wrapped: bool) -> None:
    config = CONFIG if wrapped else CONFIG["config"]
    status = STATUS if wrapped else STATUS["status"]
    _assert_layout(parse_config(config, host="127.0.0.1", source="http", status=status), source="http")


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
    udp_listener.responder = config_responder
    layout = await query_layout(udp_listener.host, port=udp_listener.port)
    assert layout is not None
    _assert_layout(layout, source="ddp")


async def test_query_layout_tolerates_unusable_status(udp_listener: UDPListener) -> None:
    def responder(packet: bytes, addr: tuple[str | Any, int]) -> list[bytes]:
        if packet[3] == DESTINATION_STATUS:
            return [reply_packet(DESTINATION_STATUS, b"{bad")]
        return config_responder(packet, addr)

    udp_listener.responder = responder
    layout = await query_layout(udp_listener.host, port=udp_listener.port, timeout=0.2)
    assert layout is not None
    assert (layout.pixel_count, layout.status) == (122, None)


@pytest.mark.parametrize("answers", [True, False], ids=["answered", "silent"])
async def test_discover_is_the_ddp_query(udp_listener: UDPListener, answers: bool) -> None:
    if answers:
        udp_listener.responder = config_responder
    layout = await discover(udp_listener.host, port=udp_listener.port, timeout=0.1)
    if answers:
        assert layout is not None
        _assert_layout(layout, source="ddp")
    else:
        assert layout is None
