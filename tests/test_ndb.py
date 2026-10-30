"""NDBController: built-in effect control over the NDB-Pro's HTTP UI, against captured pages on loopback."""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest

from pyddp.exceptions import ConfigError, ControllerError, DDPQueryError
from pyddp.ndb import NDBController, NDBPreset, NDBState
from tests.conftest import NDB_PAGES, HTTPController

PAGE_EFFECTS = (
    "Off",
    "Test",
    "Show1",
    "Show2",
    "Bands",
    "Bars",
    "Circles",
    "Color Change",
    "Color Wave",
    "Fixed Colors",
    "Glow",
    "Paint",
    "Ping Pong",
    "Pulsate",
    "Shift",
    "Snow",
    "Twist",
    "Worms",
)


def _controller(http: HTTPController, **kwargs: Any) -> NDBController:
    return NDBController(http.host, port=http.port, **kwargs)


def _closed_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


async def test_state_parses_captured_pages(http_controller: HTTPController) -> None:
    state = await _controller(http_controller).state()
    assert state == NDBState(effect="Glow", intensity=100, effects=PAGE_EFFECTS)
    assert [(method, target) for method, target, _ in http_controller.calls] == [
        ("GET", "/pfx2.html"),
        ("GET", "/pcolors.html"),
    ]


@pytest.mark.parametrize(
    ("page", "transform", "expected"),
    [
        ("/pfx2.html", lambda p: p.replace(b"'psel button'", b"'pnsel button'"), ("effect", None)),
        ("/pfx2.html", lambda p: p.replace(b'fx2b("Glow",16)', b'fx2b("Gl&amp;ow",16)'), ("effect", "Gl&ow")),
        ("/pcolors.html", lambda p: p.replace(b"class='slider Cint' value='100'", b"value='0'"), ("intensity", None)),
        ("/pcolors.html", lambda p: p.replace(b"value='100'", b"value='0'"), ("intensity", 0)),
    ],
    ids=["none-selected", "entity-unescaped", "no-slider", "zero"],
)
async def test_state_variants(
    http_controller: HTTPController, page: str, transform: Any, expected: tuple[str, object]
) -> None:
    http_controller.routes[page] = transform(NDB_PAGES[page])
    state = await _controller(http_controller).state()
    assert getattr(state, expected[0]) == expected[1]


async def test_presets_parse_captured_page(http_controller: HTTPController) -> None:
    presets = await _controller(http_controller).presets()
    assert [p.slot for p in presets] == list(range(20))
    assert presets[0] == NDBPreset(slot=0, effect="Off", intensity=0)
    assert presets[10] == NDBPreset(slot=10, effect="Color Wave", intensity=0)
    assert http_controller.calls == [("GET", "/pshows.html", b"")]


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (lambda c: c.set_effect("Color Wave"), ("POST", "/api/control", b'{"fx":"Color Wave"}')),
        (lambda c: c.set_effect('Odd "name"'), ("POST", "/api/control", b'{"fx":"Odd \\"name\\""}')),
        (lambda c: c.set_intensity(0), ("POST", "/api/control", b'{"int":"0"}')),
        (lambda c: c.set_intensity(100), ("POST", "/api/control", b'{"int":"100"}')),
        (lambda c: c.recall_preset(0), ("GET", "/pshows.html?r=0", b"")),
        (lambda c: c.recall_preset(19), ("GET", "/pshows.html?r=19", b"")),
    ],
    ids=["effect", "effect-escaped", "intensity-0", "intensity-100", "recall-0", "recall-19"],
)
async def test_commands_send_exact_request(
    http_controller: HTTPController, call: Any, expected: tuple[str, str, bytes]
) -> None:
    assert await call(_controller(http_controller)) is None
    assert http_controller.calls == [expected]


@pytest.mark.parametrize(
    ("call", "match"),
    [
        (lambda c: c.set_effect(""), "effect"),
        (lambda c: c.set_effect("   "), "effect"),
        (lambda c: c.set_effect(5), "effect"),
        (lambda c: c.set_effect(None), "effect"),
        (lambda c: c.set_intensity(-1), "intensity"),
        (lambda c: c.set_intensity(101), "intensity"),
        (lambda c: c.set_intensity(True), "intensity"),
        (lambda c: c.set_intensity(50.0), "intensity"),
        (lambda c: c.set_intensity("50"), "intensity"),
        (lambda c: c.recall_preset(-1), "slot"),
        (lambda c: c.recall_preset(20), "slot"),
        (lambda c: c.recall_preset(False), "slot"),
        (lambda c: c.recall_preset("1"), "slot"),
    ],
)
async def test_commands_reject_invalid_arguments(http_controller: HTTPController, call: Any, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        await call(_controller(http_controller))
    assert http_controller.calls == []


@pytest.mark.parametrize(
    ("host", "kwargs", "match"),
    [
        ("a\r\nb", {}, "host"),
        ("127.0.0.1/x", {}, "host"),
        ("fe80::1%en0", {}, "host"),
        ("127.0.0.1", {"port": 0}, "port"),
        ("127.0.0.1", {"port": True}, "port"),
        ("127.0.0.1", {"timeout": 0}, "timeout"),
        ("127.0.0.1", {"timeout": float("nan")}, "timeout"),
    ],
)
def test_constructor_rejects_bad_host_port_timeout(host: str, kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        NDBController(host, **kwargs)


async def test_unreachable_controller_raises_controller_error() -> None:
    controller = NDBController("127.0.0.1", port=_closed_tcp_port(), timeout=0.5)
    with pytest.raises(ControllerError, match=r"127\.0\.0\.1.*/api/control"):
        await controller.set_effect("Off")


@pytest.mark.parametrize(
    ("call", "path"),
    [
        (lambda c: c.state(), "/pfx2.html"),
        (lambda c: c.state(), "/pcolors.html"),
        (lambda c: c.presets(), "/pshows.html"),
        (lambda c: c.set_effect("Off"), "/api/control"),
        (lambda c: c.recall_preset(3), "/pshows.html"),
    ],
)
async def test_non_200_raises_controller_error_naming_host_and_path(
    http_controller: HTTPController, call: Any, path: str
) -> None:
    http_controller.routes[path] = None
    with pytest.raises(ControllerError, match=rf"127\.0\.0\.1.*{path}.*404"):
        await call(_controller(http_controller))


@pytest.mark.parametrize("mode", ["head", "body"])
async def test_slow_controller_times_out_with_controller_error(http_controller: HTTPController, mode: str) -> None:
    http_controller.slow["/pfx2.html"] = mode
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(ControllerError, match="timed out"):
        await _controller(http_controller, timeout=0.3).state()
    assert loop.time() - started < 1.0


def _sub(page: str, old: bytes, new: bytes, match: str) -> tuple[str, bytes, str]:
    assert old in NDB_PAGES[page]
    return page, NDB_PAGES[page].replace(old, new), match


@pytest.mark.parametrize(
    ("page", "body", "match"),
    [
        ("/pfx2.html", b"<html><body>hello</body></html>", "no effect buttons"),
        ("/pfx2.html", b"\xff\xfe<html>", "UTF-8"),
        _sub(
            "/pfx2.html", b"'pnsel button' onclick='fx2b(\"Snow\"", b"'psel button' onclick='fx2b(\"Snow\"", "selected"
        ),
        _sub("/pfx2.html", b'fx2b("Snow",3)', b'fx2b("Snow",4)', "button"),
        _sub("/pfx2.html", b'fx2b("Snow",3)', b'fx2b("Glow",3)', "more than once"),
        ("/pcolors.html", NDB_PAGES["/pcolors.html"].replace(b"value='100'", b"value='101'"), "intensity"),
        ("/pcolors.html", NDB_PAGES["/pcolors.html"].replace(b"value='100'", b"value='x'"), "intensity"),
        ("/pcolors.html", NDB_PAGES["/pcolors.html"] + b"<input class='slider Cint' value='5'>", "intensity"),
    ],
    ids=[
        "no-buttons",
        "not-utf8",
        "two-selected",
        "id-mismatch",
        "duplicate-name",
        "intensity-101",
        "intensity-text",
        "two-sliders",
    ],
)
async def test_state_rejects_unusable_pages(
    http_controller: HTTPController, page: str, body: bytes, match: str
) -> None:
    http_controller.routes[page] = body
    with pytest.raises(DDPQueryError, match=match):
        await _controller(http_controller).state()


_ROW0 = b'<a href="/pshows.html?r=0">Recall</a><td>Off<td>0<'


@pytest.mark.parametrize(
    "body",
    [
        b"<html>nothing here</html>",
        NDB_PAGES["/pshows.html"].replace(_ROW0, b'<a href="/pshows.html?r=0">Recall</a><td>Off<td>101<'),
        NDB_PAGES["/pshows.html"].replace(_ROW0, b'<a href="/pshows.html?r=20">Recall</a><td>Off<td>0<'),
        NDB_PAGES["/pshows.html"].replace(_ROW0, b'<a href="/pshows.html?r=1">Recall</a><td>Off<td>0<'),
        NDB_PAGES["/pshows.html"].replace(_ROW0, b'<a href="/pshows.html?r=0">Recall</a><td><td>0<'),
        NDB_PAGES["/pshows.html"].replace(_ROW0, b'<a href="/pshows.html?r=0">Recall</a><td>Off<td>x<'),
    ],
    ids=["no-rows", "intensity-101", "slot-20", "duplicate-slot", "empty-effect", "row-unparsable"],
)
async def test_presets_reject_unusable_page(http_controller: HTTPController, body: bytes) -> None:
    http_controller.routes["/pshows.html"] = body
    with pytest.raises(DDPQueryError, match="pshows"):
        await _controller(http_controller).presets()
