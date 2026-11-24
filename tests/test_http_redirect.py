"""The redirect-only plain-HTTP listener of built-in HTTPS (`http_redirect.py`).

Whatever arrives on it -- any method, any path -- gets a 301 to the https
address with an empty body and nothing else: no application route, static file
or health data exists behind it. The target is PUBLIC_BASE_URL when that is an
https address, else the request's own host; a request must never be able to
steer it to another host or to split the response. It runs only while TLS is
on, and failing to start costs the redirect, never the boot.

The listener tests drive a REAL listener on an ephemeral loopback port.
"""
import ast
import asyncio
import contextlib
import logging
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import http_redirect
import serve
from settings import settings
from tls_test_certs import write_pair

ROOT = Path(__file__).resolve().parent.parent
HOST = "app.example.com"


@pytest.fixture(autouse=True)
def no_public_base(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "")


async def _send(port: int, raw: bytes, timeout: float = 20.0) -> bytes:
    """Everything the listener answers to `raw` until it closes."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    out = b""
    try:
        with contextlib.suppress(ConnectionError):
            writer.write(raw)
            await writer.drain()
        deadline = time.monotonic() + timeout
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(65536),
                                               max(0.01, deadline - time.monotonic()))
            except ConnectionError:
                break
            if not chunk:
                break
            out += chunk
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    return out


def _with_listener(coro_fn):
    """Run `await coro_fn(port)` against a listener on an ephemeral port."""
    async def main():
        server = await http_redirect.start("127.0.0.1", 0)
        assert server is not None
        try:
            return await coro_fn(server.sockets[0].getsockname()[1])
        finally:
            await http_redirect.stop(server)
    return asyncio.run(main())


def _exchange(raw: bytes) -> bytes:
    async def go(port):
        return await _send(port, raw)
    return _with_listener(go)


def _request(target="/", host=HOST, method="GET", extra=b"", body=b"", version="HTTP/1.1"):
    head = f"{method} {target} {version}\r\n".encode("latin-1")
    if host is not None:
        head += b"Host: " + host.encode("latin-1") + b"\r\n"
    return head + extra + b"\r\n" + body


def _parse(raw: bytes):
    """(status, {lower header: value}, body); strict about the framing."""
    head, sep, body = raw.partition(b"\r\n\r\n")
    assert sep, raw
    unframed = head.replace(b"\r\n", b"")
    assert b"\n" not in unframed and b"\r" not in unframed, raw
    lines = head.decode("ascii").split("\r\n")
    version, status, _reason = lines[0].split(" ", 2)
    assert version == "HTTP/1.1"
    headers = {}
    for line in lines[1:]:
        name, colon, value = line.partition(": ")
        assert colon, line
        assert name.lower() not in headers, line
        headers[name.lower()] = value
    return int(status), headers, body


def _assert_bare(headers, body, *, redirect):
    """Empty body, no cookie, nothing but the framing (and Location)."""
    assert body == b""
    assert headers.get("content-length") == "0"
    assert headers.get("connection", "").lower() == "close"
    allowed = {"content-length", "connection", "date"} | ({"location"} if redirect else set())
    assert set(headers) <= allowed, headers


def _location(raw: bytes) -> str:
    status, headers, body = _parse(raw)
    assert status == 301, raw
    _assert_bare(headers, body, redirect=True)
    return headers["location"]


def _assert_400(raw: bytes):
    status, headers, body = _parse(raw)
    assert status == 400, raw
    _assert_bare(headers, body, redirect=False)


# ---------------------------------------------------------------------------
# Every request is a 301
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE",
                                    "OPTIONS", "TRACE", "CONNECT", "PROPFIND",
                                    "VERSION-CONTROL"])
def test_every_method_gets_the_301(method):
    body = b'{"a": 1}' if method in ("POST", "PUT", "PATCH") else b""
    extra = f"Content-Length: {len(body)}\r\n".encode() if body else b""
    raw = _exchange(_request("/some/path?x=1", method=method, extra=extra, body=body))
    assert _location(raw) == f"https://{HOST}/some/path?x=1"


@pytest.mark.parametrize("target", [
    "/", "/lab", "/some/path?x=1", "/a/b/c?x=1&y=2&y=3", "/?", "/?a=b?c=d", "/a%20b?q=%2F%3F",
    "/a+b;c=d,e", "/%E1%83%90?k=%D0%B9", "/auth/microsoft/callback?code=0.A-_~&state=a.b==",
    "/x?next=https://other.example/y", "/a//b/../c/./d", "/" + "p" * 2000 + "?q=" + "v" * 2000,
], ids=lambda t: t[:30])
def test_path_and_query_are_preserved_exactly(target):
    assert _location(_exchange(_request(target))) == f"https://{HOST}{target}"


@pytest.mark.parametrize("target", [
    "/health", "/version", "/auth/login", "/auth/me", "/static/dashboard.js",
    "/static/vendor/plotly/plotly.min.js", "/lab", "/api/chat/x/stream", "/charts/token",
    "/auth/reset/" + "a" * 43, "/openapi.json",
])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_no_application_route_is_reachable(target, method):
    extra = b"Content-Type: application/json\r\nCookie: session=abc\r\nContent-Length: 2\r\n"
    raw = _exchange(_request(target, method=method, extra=extra, body=b"{}"))
    # _location also pins: empty body, no Set-Cookie, no other header.
    assert _location(raw) == f"https://{HOST}{target}"


def test_a_pipelined_second_request_is_not_answered():
    raw = _exchange(_request("/one") + _request("/two"))
    assert raw.count(b"HTTP/1.1 ") == 1
    assert _location(raw) == f"https://{HOST}/one"


def test_many_requests_at_once_are_all_answered():
    async def go(port):
        return await asyncio.gather(*[_send(port, _request(f"/n/{i}")) for i in range(40)])
    answers = _with_listener(go)
    assert [_location(a) for a in answers] == [f"https://{HOST}/n/{i}" for i in range(40)]


# ---------------------------------------------------------------------------
# Where it redirects to
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("base,expected", [
    ("https://pdc.bank.example", "https://pdc.bank.example"),
    ("https://pdc.bank.example/", "https://pdc.bank.example"),
    ("https://pdc.bank.example:8443", "https://pdc.bank.example:8443"),
    ("https://localhost:8091", "https://localhost:8091"),
    ("  https://pdc.bank.example  ", "https://pdc.bank.example"),
    ("https://pdc.bank.example/sub/", "https://pdc.bank.example/sub"),
    ("https://[2001:db8::5]:8443", "https://[2001:db8::5]:8443"),
])
@pytest.mark.parametrize("host", [HOST, "elsewhere.example:8080"])
def test_an_https_public_base_url_wins_over_the_host(monkeypatch, base, expected, host):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", base)
    assert _location(_exchange(_request("/some/path?x=1", host=host))) == \
        expected + "/some/path?x=1"


@pytest.mark.parametrize("base", [
    "", "   ", "http://pdc.bank.example", "http://localhost:8091", "pdc.bank.example",
    "ftp://pdc.bank.example", "javascript:alert(1)", "//evil.example", "https://", "https:///",
    "https://evil.example\r\nSet-Cookie: a=b", "https://evil.example\nX: y",
    "https://evil.example /x",
])
def test_any_other_public_base_url_is_ignored(monkeypatch, base):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", base)
    assert _location(_exchange(_request("/p?x=1"))) == f"https://{HOST}/p?x=1"


def test_the_public_base_url_is_read_per_request(monkeypatch):
    async def go(port):
        first = await _send(port, _request("/p"))
        monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://later.example")
        return first, await _send(port, _request("/p"))
    first, second = _with_listener(go)
    assert _location(first) == f"https://{HOST}/p"
    assert _location(second) == "https://later.example/p"


@pytest.mark.parametrize("host,expected", [
    ("app.example.com", "app.example.com"),
    ("app.example.com:8080", "app.example.com"),
    ("APP.Example.com:80", "app.example.com"),
    ("localhost:8092", "localhost"),
    ("server01", "server01"),
    ("my_host:8080", "my_host"),
    ("app_1.branch_2.local", "app_1.branch_2.local"),
    ("10.20.30.40:8080", "10.20.30.40"),
    ("10.20.30.40", "10.20.30.40"),
    ("[2001:db8::1]:8080", "[2001:db8::1]"),
    ("[2001:db8::1]", "[2001:db8::1]"),
    ("[::1]:8092", "[::1]"),
])
def test_without_a_base_the_target_is_the_request_host_without_its_port(host, expected):
    location = _location(_exchange(_request("/p?x=1", host=host)))
    assert location.lower() == f"https://{expected}/p?x=1"


def test_an_underscore_host_is_redirected():
    """Internal Windows / Docker names carry underscores; browsers accept them."""
    assert _location(_exchange(_request("/some/path?x=1", host="my_host:80"))) == \
        "https://my_host/some/path?x=1"


@pytest.mark.parametrize("header", [b"HOST: app.example.com\r\n", b"host:app.example.com\r\n"])
def test_the_host_header_name_and_spacing_do_not_matter(header):
    raw = _exchange(b"GET /p HTTP/1.1\r\nAccept: */*\r\n" + header + b"\r\n")
    assert _location(raw) == f"https://{HOST}/p"


# ---------------------------------------------------------------------------
# Hostile and malformed requests
# ---------------------------------------------------------------------------
BAD_HOSTS = [
    None, "", "   ", "good@evil", "good.example@evil.example", "good:80@evil.example",
    "good:pw@evil.example:80", "evil.example/x", "evil.example?x", "evil.example#x",
    "evil.example\\@good", "evil.example\\", "a b", "a\tb", "https://evil.example",
    "good,evil.example", "good;evil", "host:port", "host:80:80", "host:123456", "::1",
    "[::1", "[::1]evil.example", "[::1].evil.example", "[not-an-ip]", "[::1]@evil", "[]",
    ".", "..", "a..b", ".a", "-bad", "ex\xe4mple", "%65vil", "good\x00", "good\x7f",
    "good\rX-Injected: 1", "good\nX-Injected: 1", "a" * 400, "<script>",
]


@pytest.mark.parametrize("host", BAD_HOSTS, ids=[ascii(h)[:40] for h in BAD_HOSTS])
def test_a_missing_or_malformed_host_is_a_400(host):
    _assert_400(_exchange(_request("/p", host=host)))


def test_two_host_headers_are_a_400():
    _assert_400(_exchange(_request("/p", extra=b"Host: evil.example\r\n")))


@pytest.mark.parametrize("host", [None, "good@evil", "evil.example/x"])
def test_with_an_https_base_the_host_does_not_matter(monkeypatch, host):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://pdc.bank.example")
    assert _location(_exchange(_request("/p", host=host))) == "https://pdc.bank.example/p"


TARGETS = [
    "//evil.com/x", "///evil.com", "/\\evil.com", "\\\\evil.com", "http://evil.com/x",
    "https://evil.com/x", "HTTP://evil.com", "evil.com", "@evil.com/x", ".evil.com/x",
    ":8443/x", ":pw@evil.com", "*", "?x", "#x", "javascript:alert(1)", "/@evil.com",
    "/..//evil.com", "/a\rSet-Cookie:s=1", "/a\nSet-Cookie:s=1", "/a\x00b", "/a\x0bb",
    "/a\x7f", "/a\x85b", "/\xe9", "/%0d%0aSet-Cookie:%20s=1",
    "/%0aLocation:%20https://evil.com", "/%2f%2fevil.com", "/a?x=%0d%0a%0d%0a<script>",
]


@pytest.mark.parametrize("base", ["", "https://app.example.com", "https://app.example.com:8443"])
@pytest.mark.parametrize("target", TARGETS, ids=[ascii(t) for t in TARGETS])
def test_no_target_leaves_the_host_or_splits_the_response(monkeypatch, target, base):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", base)
    raw = _exchange(_request(target))
    status, headers, body = _parse(raw)        # strict framing, one response
    assert status in (301, 400), raw
    _assert_bare(headers, body, redirect=status == 301)
    assert "set-cookie" not in headers
    if status == 301:
        parts = urlsplit(headers["location"])
        assert parts.scheme == "https"
        assert parts.hostname == HOST, headers["location"]
        assert parts.username is None and parts.password is None
        assert parts.port == (8443 if base.endswith(":8443") else None)
        # A path, never something that continues the authority.
        rest = headers["location"].split(HOST, 1)[1].removeprefix(":8443")
        assert rest.startswith("/"), headers["location"]


@pytest.mark.parametrize("line", [
    b"GET / HTTP/1.1 extra", b"GET /", b"GET", b"", b"/ GET", b"G\x00T / HTTP/1.1",
    b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03", b"GET  / HTTP/1.1",
    b"SSH-2.0-OpenSSH_9.6", b"GET /a b HTTP/1.1",
])
def test_something_that_is_not_an_http_request_is_never_redirected(line):
    raw = _exchange(line + b"\r\nHost: app.example.com\r\n\r\n")
    if raw:
        _assert_400(raw)


def test_an_http_1_0_request_without_a_host():
    _assert_400(_exchange(_request("/p", host=None, version="HTTP/1.0")))


@pytest.mark.parametrize("size", [9_000, 100_000, 2_000_000])
@pytest.mark.parametrize("where", ["target", "header"])
def test_an_oversized_head_is_bounded(size, where):
    if where == "target":
        raw_in = _request("/" + "a" * size)
    else:
        raw_in = _request("/p", extra=b"X-Big: " + b"a" * size + b"\r\n")
    started = time.monotonic()
    raw = _exchange(raw_in)
    assert time.monotonic() - started < 15
    # A reset may swallow the answer; whatever does arrive is the refusal.
    if raw:
        _assert_400(raw)


def test_a_head_that_never_ends_is_cut_off(monkeypatch):
    """Neither a long unterminated head nor a short one holds the connection."""
    monkeypatch.setattr(http_redirect, "READ_TIMEOUT_S", 0.5)
    for size in (100, 9_000, 1_000_000):
        started = time.monotonic()
        raw = _exchange(b"GET /" + b"a" * size)
        assert time.monotonic() - started < 10
        if raw:
            _assert_400(raw)


def test_the_listener_survives_hostile_requests():
    async def go(port):
        for raw in (b"GET /" + b"a" * 100_000 + b"\r\n\r\n", b"\x00\xff\r\n\r\n", b"\r\n\r\n",
                    _request("/p", host="good@evil")):
            await _send(port, raw)
        return await _send(port, _request("/after"))
    assert _location(_with_listener(go)) == f"https://{HOST}/after"


def test_a_client_that_sends_nothing_is_dropped(monkeypatch):
    monkeypatch.setattr(http_redirect, "READ_TIMEOUT_S", 0.3)
    started = time.monotonic()
    assert _exchange(b"") == b""
    assert _exchange(b"GET / HTTP/1.1\r\nHost: app.exa") == b""
    assert time.monotonic() - started < 10


def test_the_read_is_bounded_in_time_and_size_by_default():
    assert 0 < http_redirect.READ_TIMEOUT_S <= 30
    assert 0 < http_redirect.MAX_REQUEST_BYTES <= 64 * 1024


def test_idle_connections_cannot_pile_up_without_bound(monkeypatch):
    monkeypatch.setattr(http_redirect, "MAX_CONNECTIONS", 3)

    async def go(port):
        idle = [await asyncio.open_connection("127.0.0.1", port) for _ in range(3)]
        await asyncio.sleep(0.2)
        refused = await _send(port, _request("/p"))
        for _r, w in idle:
            w.close()
        answer = b""
        for _ in range(50):                 # the places are given back
            await asyncio.sleep(0.1)
            answer = await _send(port, _request("/p"))
            if answer:
                break
        return refused, answer
    refused, answer = _with_listener(go)
    assert refused == b""
    assert _location(answer) == f"https://{HOST}/p"


# ---------------------------------------------------------------------------
# The pure function
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("args,expected", [
    (("/p?x=1", "h.example:80", ""), "https://h.example/p?x=1"),
    (("/p", "h.example", "https://b.example:8443/"), "https://b.example:8443/p"),
    (("/p", "h.example", "http://b.example"), "https://h.example/p"),
    (("/p", None, "https://b.example"), "https://b.example/p"),
    (("/p", None, ""), None),
    (("/p", "good@evil", "http://b.example"), None),
    (("http://evil.com/x", "h.example", ""), "https://h.example/"),
    ((None, "h.example", None), "https://h.example/"),
    (("/p", 7, None), None),
    (("/p\r\nX: y", "h.example", ""), "https://h.example/"),
])
def test_redirect_location(args, expected):
    assert http_redirect.redirect_location(*args) == expected


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------
def test_start_on_a_taken_port_returns_none_and_logs(caplog):
    async def go():
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            return await http_redirect.start("127.0.0.1", taken.getsockname()[1])
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(go()) is None
    assert "HTTP_REDIRECT_START_FAILED" in caplog.text


@pytest.mark.parametrize("error", [PermissionError("13"), OSError("boom"), RuntimeError("x")],
                         ids=["permission", "os", "runtime"])
def test_start_never_raises(monkeypatch, error):
    async def boom(*_a, **_k):
        raise error
    monkeypatch.setattr(asyncio, "start_server", boom)
    assert asyncio.run(http_redirect.start("127.0.0.1", 0)) is None


def test_the_default_port_is_8080():
    assert http_redirect.DEFAULT_PORT == 8080


def test_stop_accepts_none_and_never_raises():
    class Broken:
        def close(self):
            raise RuntimeError("x")

    async def go():
        await http_redirect.stop(None)
        await http_redirect.stop(Broken())
    asyncio.run(go())


def test_after_stop_nothing_listens():
    async def go():
        server = await http_redirect.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        assert await _send(port, _request("/p"))
        await http_redirect.stop(server)
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)
    asyncio.run(go())


@pytest.mark.parametrize("sent", [b"", b"GET /half HTTP/1.1\r\nHost: app.exa", b"G"],
                         ids=["idle", "half-sent", "one-byte"])
def test_stop_drops_open_connections_promptly(monkeypatch, sent):
    """Since Python 3.12 `wait_closed` waits for every open connection, so an
    idle client would hold the container's shutdown for its read timeout."""
    monkeypatch.setattr(http_redirect, "READ_TIMEOUT_S", 60.0)
    monkeypatch.setattr(http_redirect, "STOP_TIMEOUT_S", 30.0)

    async def go():
        server = await http_redirect.start("127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        conns = [await asyncio.open_connection("127.0.0.1", port) for _ in range(3)]
        for _r, w in conns:
            if sent:
                w.write(sent)
                await w.drain()
        await asyncio.sleep(0.2)
        started = time.monotonic()
        await http_redirect.stop(server)
        took = time.monotonic() - started
        # The clients are cut off too, with no answer.
        answers = []
        for r, w in conns:
            try:
                answers.append(await asyncio.wait_for(r.read(), 5.0))
            except ConnectionError:
                answers.append(b"")
            w.close()
        return took, answers, time.monotonic() - started
    took, answers, total = asyncio.run(go())
    assert took < 2.0, took
    assert answers == [b"", b"", b""]
    assert total < 4.0, total


def test_stop_touches_the_connections_of_its_own_server_only(monkeypatch):
    monkeypatch.setattr(http_redirect, "READ_TIMEOUT_S", 20.0)

    async def go():
        first = await http_redirect.start("127.0.0.1", 0)
        second = await http_redirect.start("127.0.0.1", 0)
        _r1, w1 = await asyncio.open_connection("127.0.0.1", first.sockets[0].getsockname()[1])
        r2, w2 = await asyncio.open_connection("127.0.0.1", second.sockets[0].getsockname()[1])
        w2.write(b"GET /p?x=1 HTTP/1.1\r\n")
        await w2.drain()
        await asyncio.sleep(0.2)
        await http_redirect.stop(first)
        await asyncio.sleep(0.2)
        w2.write(b"Host: app.example.com\r\n\r\n")        # the other one goes on
        await w2.drain()
        half = await asyncio.wait_for(r2.read(), 10.0)
        fresh = await _send(second.sockets[0].getsockname()[1], _request("/fresh"))
        await http_redirect.stop(second)
        w1.close()
        w2.close()
        return half, fresh
    half, fresh = asyncio.run(go())
    assert _location(half) == f"https://{HOST}/p?x=1"
    assert _location(fresh) == f"https://{HOST}/fresh"


def test_stop_can_be_called_twice():
    async def go():
        server = await http_redirect.start("127.0.0.1", 0)
        await http_redirect.stop(server)
        await http_redirect.stop(server)
    asyncio.run(go())


# ---------------------------------------------------------------------------
# Isolation from the application
# ---------------------------------------------------------------------------
def test_the_module_imports_nothing_from_the_application():
    """By construction there is no route, session or store behind it."""
    tree = ast.parse((ROOT / "http_redirect.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            assert ast.unparse(node.func) not in ("__import__", "importlib.import_module")
    assert "app" not in imported and "routes" not in imported
    local = imported - set(sys.stdlib_module_names) - {"__future__"}
    assert local <= {"settings", "logger_utils"}, local


# ---------------------------------------------------------------------------
# The lifespan
# ---------------------------------------------------------------------------
def _run_lifespan(monkeypatch, tmp_path, *, tls, start=None, stop=None, while_serving=None):
    """Enter and leave the REAL lifespan with every slow or networked step
    stubbed. Returns (events, startup log messages)."""
    import app as app_mod
    import db_connector
    import db_scheduler
    import executor_client
    import plot_utils

    events, logs, ports = [], [], []
    cert, key = write_pair(tmp_path) if tls else ("", "")
    monkeypatch.setattr(settings, "TLS_CERT_FILE", str(cert))
    monkeypatch.setattr(settings, "TLS_KEY_FILE", str(key))
    # Started the way the image starts it: uvicorn with the launcher's argv.
    monkeypatch.setattr(sys, "argv",
                        serve.build_argv((str(cert), str(key)) if tls else None))
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(app_mod.AuthStore, "ensure_local_admin", lambda self: None)
    monkeypatch.setattr(executor_client, "startup", lambda: None)
    monkeypatch.setattr(plot_utils, "ensure_plotly_js_asset", lambda: None)
    monkeypatch.setattr(db_connector, "ensure_oracle_thick_mode", lambda: False)
    monkeypatch.setattr(db_scheduler, "start", lambda: events.append("scheduler_start"))
    monkeypatch.setattr(db_scheduler, "stop", lambda *a, **k: events.append("scheduler_stop"))
    monkeypatch.setattr(app_mod, "log_with_sid",
                        lambda sid, level, message, *a, **k: logs.append(str(message)))
    real_start, real_stop = http_redirect.start, http_redirect.stop

    async def default_start(*_a, **_k):
        events.append("redirect_start")
        server = await real_start("127.0.0.1", 0)       # never the real 8080
        ports.append(server.sockets[0].getsockname()[1])
        return server

    async def default_stop(server):
        events.append("redirect_stop")
        await real_stop(server)

    monkeypatch.setattr(http_redirect, "start", start or default_start)
    monkeypatch.setattr(http_redirect, "stop", stop or default_stop)

    async def enter():
        async with app_mod.lifespan(app_mod.app):
            events.append("serving")
            if while_serving is not None:
                await while_serving(ports[0])
        if ports:
            with pytest.raises(OSError):
                await asyncio.open_connection("127.0.0.1", ports[0])

    asyncio.run(enter())
    return events, logs


def test_the_listener_is_not_started_when_tls_is_off(monkeypatch, tmp_path):
    events, logs = _run_lifespan(monkeypatch, tmp_path, tls=False)
    assert events == ["scheduler_start", "serving", "scheduler_stop"]
    assert not any("TLS_ENABLED" in m or "HTTP_REDIRECT" in m for m in logs), logs


def test_the_listener_runs_exactly_as_long_as_the_app_when_tls_is_on(monkeypatch, tmp_path):
    answers = []

    async def probe(port):
        answers.append(await _send(port, _request("/lab?x=1")))

    events, logs = _run_lifespan(monkeypatch, tmp_path, tls=True, while_serving=probe)
    assert events.count("redirect_start") == events.count("redirect_stop") == 1
    assert events.index("redirect_start") < events.index("serving") \
        < events.index("redirect_stop")
    assert _location(answers[0]) == f"https://{HOST}/lab?x=1"
    assert any("TLS_ENABLED" in m for m in logs)


def test_the_lifespan_starts_it_on_the_default_port(monkeypatch, tmp_path):
    """The compose overrides publish container port 8080: the lifespan's own
    call, through the real `start`, must ask the event loop for exactly that."""
    asked = []
    real = asyncio.start_server

    async def start_server(client_connected_cb, host=None, port=None, **kwargs):
        asked.append((host, port))
        return await real(client_connected_cb, "127.0.0.1", 0, **kwargs)   # never the real 8080

    monkeypatch.setattr(asyncio, "start_server", start_server)
    events, logs = _run_lifespan(monkeypatch, tmp_path, tls=True, start=http_redirect.start)
    assert asked == [("0.0.0.0", 8080)]
    assert "serving" in events
    assert any("HTTP_REDIRECT_STARTED" in m and "8080" in m for m in logs), logs


@pytest.mark.parametrize("raises", [False, True])
def test_a_listener_that_cannot_start_does_not_stop_the_boot(monkeypatch, tmp_path, raises):
    async def start(*_a, **_k):
        if raises:
            raise RuntimeError("cannot bind")
        return None

    events, logs = _run_lifespan(monkeypatch, tmp_path, tls=True, start=start)
    assert events[-2:] == ["serving", "scheduler_stop"], events
    assert not any("HTTP_REDIRECT_STARTED" in m for m in logs), logs
    assert "cannot bind" not in " ".join(logs)


def test_a_listener_that_cannot_stop_does_not_break_the_shutdown(monkeypatch, tmp_path):
    async def start(*_a, **_k):
        return object()

    async def stop(_server):
        raise RuntimeError("stuck")

    events, _logs = _run_lifespan(monkeypatch, tmp_path, tls=True, start=start, stop=stop)
    assert events[-1] == "scheduler_stop", events
