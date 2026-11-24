"""Redirect-only plain-HTTP listener for built-in HTTPS (port 8080).

Started by the app lifespan only while `TLS_CERT_FILE` / `TLS_KEY_FILE` are
set. It answers EVERY request, whatever the method or path, with
`301 Moved Permanently` to the https address and closes the connection. It
imports nothing from the application (stdlib + settings + logger_utils), so
no route, static file, health data or session exists on it by construction.

Where it redirects to: `PUBLIC_BASE_URL` (read per request) when that starts
with `https://`; otherwise `https://<Host header without its port>`, and only
when the Host is a plain hostname (underscores inside a label allowed) or IP address. The path and query are copied
only when the request target starts with `/` and holds nothing but printable
ASCII; anything else redirects to `/`. A request it cannot read, or one
without a usable Host while no https base is configured, gets a bare 400.

`start` and `stop` never raise (Article IV): a bind failure is logged and the
application keeps serving HTTPS without the redirect.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re

from settings import settings
from logger_utils import log_safe_value, log_with_sid

DEFAULT_PORT = 8080
MAX_REQUEST_BYTES = 8192      # request line + headers; more is a 400
READ_TIMEOUT_S = 5.0          # a client that sends nothing is dropped
WRITE_TIMEOUT_S = 5.0
STOP_TIMEOUT_S = 5.0
MAX_CONNECTIONS = 256         # open at once; the next one is closed unread

_HOSTNAME_RE = re.compile(
    r"([A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?)*)\.?"
    r"(?::[0-9]{1,5})?")
_IPV6_HOST_RE = re.compile(r"\[([0-9A-Fa-f:.]{2,45})\](?::[0-9]{1,5})?")
# Printable ASCII without the space: what a request target and a configured
# base may consist of. Everything else (controls, spaces, non-ASCII) is out.
_PRINTABLE_RE = re.compile(r"[\x21-\x7e]+")
# An RFC 9110 token, bounded.
_METHOD_RE = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}")

_RESPONSE_400 = (b"HTTP/1.1 400 Bad Request\r\n"
                 b"Content-Length: 0\r\n"
                 b"Connection: close\r\n\r\n")

_open_connections = 0
# server -> the writers of its open connections, so `stop` can drop them
# (since Python 3.12 `wait_closed` waits for every connection to end).
_server_writers: dict = {}
_handler_failure_logged = False


def _https_base(public_base) -> str | None:
    """`PUBLIC_BASE_URL` without its trailing slash when it is an https
    address made of printable ASCII, else None."""
    if not isinstance(public_base, str):
        return None
    base = public_base.strip()
    if not base.lower().startswith("https://"):
        return None
    base = "https://" + base[len("https://"):].rstrip("/")
    if len(base) <= len("https://") or len(base) > 2048:
        return None
    if not _PRINTABLE_RE.fullmatch(base):
        return None
    return base


def _host_without_port(host_header) -> str | None:
    """The Host header's host (lower-cased, IPv6 in brackets) or None when it
    is missing or anything but a plain hostname / IP address."""
    if not isinstance(host_header, str):
        return None
    raw = host_header.strip()
    if not raw or len(raw) > 300:
        return None
    m = _IPV6_HOST_RE.fullmatch(raw)
    if m:
        try:
            addr = ipaddress.IPv6Address(m.group(1))
        except ValueError:
            return None
        return f"[{addr.compressed}]"
    m = _HOSTNAME_RE.fullmatch(raw)
    if not m:
        return None
    host = m.group(1)
    if len(host) > 253:
        return None
    return host.lower()


def _safe_path(target) -> str:
    """The request target when it is an origin-form path of printable ASCII,
    else `/`."""
    if (isinstance(target, str) and target.startswith("/")
            and _PRINTABLE_RE.fullmatch(target)):
        return target
    return "/"


def redirect_location(target, host_header, public_base) -> str | None:
    """The `Location` of the redirect, or None when there is none to give.

    `public_base` wins when it is an https address; otherwise the address is
    built from `host_header` (port dropped — HTTPS is served on the default
    port of that name). None means: no https base and no usable Host, which
    the listener answers with 400. Pure; its helpers accept any input.
    """
    base = _https_base(public_base)
    if base is None:
        host = _host_without_port(host_header)
        if host is None:
            return None
        base = "https://" + host
    return base + _safe_path(target)


def _parse_request(head: bytes) -> tuple[str, str | None] | None:
    """(request target, Host header or None) of a request head, or None when
    it is not an HTTP request line. Two Host headers count as none."""
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        return None
    method, target, version = parts
    if not _METHOD_RE.fullmatch(method) or not version.startswith("HTTP/") or not target:
        return None
    hosts = []
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "host":
            hosts.append(value.strip())
    return target, (hosts[0] if len(hosts) == 1 else None)


def _response_for(head: bytes) -> bytes:
    parsed = _parse_request(head)
    if parsed is None:
        return _RESPONSE_400
    target, host = parsed
    location = redirect_location(target, host,
                                 getattr(settings, "PUBLIC_BASE_URL", ""))
    if location is None:
        return _RESPONSE_400
    return (b"HTTP/1.1 301 Moved Permanently\r\n"
            b"Location: " + location.encode("ascii") + b"\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n\r\n")


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                  writers: set | None = None) -> None:
    """One connection: read the head, answer once, close. Never raises."""
    global _open_connections, _handler_failure_logged
    _open_connections += 1
    if writers is not None:
        writers.add(writer)
    try:
        if _open_connections > MAX_CONNECTIONS:
            return
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"),
                                          READ_TIMEOUT_S)
            response = _response_for(head)
        except asyncio.LimitOverrunError:
            response = _RESPONSE_400            # head above MAX_REQUEST_BYTES
        except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionError, OSError):
            return                              # nothing readable: just close
        writer.write(response)
        await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT_S)
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    except Exception as e:
        if not _handler_failure_logged:
            _handler_failure_logged = True
            log_with_sid("http_redirect", "error",
                         "HTTP_REDIRECT_HANDLER_FAILED "
                         f"error={log_safe_value(type(e).__name__, 80)}")
    finally:
        _open_connections -= 1
        if writers is not None:
            writers.discard(writer)
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), WRITE_TIMEOUT_S)
        except Exception:
            pass


async def start(host: str = "0.0.0.0", port: int = DEFAULT_PORT):
    """Start the listener in the running event loop. Returns the server, or
    None (logged `HTTP_REDIRECT_START_FAILED`) when it could not bind."""
    try:
        writers: set = set()

        async def _on_connection(reader, writer):
            await _handle(reader, writer, writers)

        server = await asyncio.start_server(_on_connection, host, port,
                                            limit=MAX_REQUEST_BYTES)
        _server_writers[server] = writers
        return server
    except Exception as e:
        log_with_sid("http_redirect", "error",
                     "HTTP_REDIRECT_START_FAILED "
                     f"error={log_safe_value(type(e).__name__, 80)}")
        return None


async def stop(server) -> None:
    """Stop a listener `start` returned (None is accepted). Never raises."""
    if server is None:
        return
    try:
        server.close()
        # Connections still open (a client that connected and sent nothing)
        # are dropped, so shutdown does not wait for their read timeout.
        for writer in list(_server_writers.pop(server, ())):
            try:
                writer.transport.abort()
            except Exception:
                pass
        await asyncio.wait_for(server.wait_closed(), STOP_TIMEOUT_S)
    except Exception as e:
        log_with_sid("http_redirect", "warning",
                     "HTTP_REDIRECT_STOP_FAILED "
                     f"error={log_safe_value(type(e).__name__, 80)}")
