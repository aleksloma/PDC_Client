"""Command line test client for the PowerDataChat MCP endpoint (POST /mcp).

Lists the chats of a user, asks a question and waits for the answer, or runs a
self-test of the endpoint. It needs Python only, no installed package.

Usage (the options stand before the command):
    python tools/mcp_test_client.py [options] chats
    python tools/mcp_test_client.py [options] ask CHAT_ID QUESTION [--out FOLDER]
    python tools/mcp_test_client.py [options] selftest [--chat CHAT_ID]

Give the token in the environment variable PDC_MCP_TOKEN, or with --token. It
is never printed. A per-user token also needs --email, the user to act for; a
shared-account token does not.

Examples (the default address is the local stack):
    python tools/mcp_test_client.py --email user@company.com chats
    python tools/mcp_test_client.py --email user@company.com ask c_123 "Sales by month?"
    python tools/mcp_test_client.py --url https://pdc.company.com/mcp selftest

The charts of an answer are saved as PNG files in --out, else in a new folder
mcp_answer_<time> here; no existing file is overwritten. Redirects are not
followed, and an HTTP proxy is not used for an address of this computer. For an
https server with a private certificate authority set SSL_CERT_FILE.

Exit code: 0 when all went well. 1 when the server answered and the answer is
a refusal, a failure, or still running after --max-wait. 2 when the server
could not be used or the command line is wrong.
"""
import argparse
import base64
import http.client
import ipaddress
import itertools
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8091/mcp"
LEGACY, MODERN = "2025-11-25", "2026-07-28"
TOOLS = {"list_chats", "ask", "get_answer"}
DEFAULT_TIMEOUT_S = 100     # above 60, the longest time the server holds get_answer
DEFAULT_MAX_WAIT_S = 900
POLL_PAUSE_S = 1.0
DEFAULT_QUESTION = "How many rows are in each table?"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Control characters (line feed and tab stay) and text direction overrides.
_CONTROL_RE = re.compile(r"[\u0000-\u0008\u000b-\u001f\u007f-\u009f"
                         r"\u202a-\u202e\u2066-\u2069]")
_TOKEN_RE = re.compile(r"pdcmcp_[A-Za-z0-9_-]*")


class Stop(Exception):
    """The end of the command: its exit code and the sentence to print."""

    def __init__(self, code, text):
        super().__init__(text)
        self.code, self.text = code, text


# ---------------------------------------------------------------------------
# printing
# ---------------------------------------------------------------------------
def _say(text, stream=None):
    """Print text of a server: no control characters, no token, and only what
    the console can show."""
    stream = stream or sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    text = _TOKEN_RE.sub("[token]", _CONTROL_RE.sub("", text))
    print(text.encode(encoding, "replace").decode(encoding), file=stream)


def _one_line(text):
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# the request
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The token must not travel to another address."""

    def redirect_request(self, *args, **kwargs):
        return None


def _is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:          # a name
        return False


def _post(url, headers, body_bytes, timeout):
    """POST the body. Returns (status, headers, bytes) for every HTTP status;
    raises the network's own exceptions."""
    handlers = [_NoRedirect()]
    if _is_loopback(urllib.parse.urlsplit(url).hostname or ""):
        handlers.append(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    try:
        with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.headers, e.read()


def _status_text(status, headers):
    if status == 401:
        return "The token was refused (HTTP status 401)."
    if status == 403:
        return "The server does not allow this call (HTTP status 403)."
    if status == 404:
        return "The server has no MCP endpoint at this address (HTTP status 404)."
    if status == 413:
        return "The request is too large (HTTP status 413)."
    if status == 429:
        wait = str(headers.get("Retry-After"))
        when = f"in {wait} seconds" if re.fullmatch("[0-9]{1,6}", wait) else "later"
        return f"Too many calls with a refused token (HTTP status 429), try again {when}."
    if 300 <= status < 400:
        return (f"The server points to another address (HTTP status {status}), "
                "which is not followed.")
    return f"The server did not accept the request (HTTP status {status})."


def _host(url):
    """The host and port of the address, without a name or password in it."""
    return _one_line(urllib.parse.urlsplit(url).netloc.rpartition("@")[2])


class Client:
    def __init__(self, url, token, email, protocol, timeout):
        self.url, self.protocol, self.timeout = url, protocol, timeout
        self.headers = {"Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": protocol}
        if email:
            self.headers["X-PDC-User-Email"] = email
        self.ids = itertools.count(1)

    def call(self, method, params):
        """The result of one JSON-RPC request."""
        headers, params = dict(self.headers), dict(params)
        if self.protocol == MODERN:
            headers["Mcp-Method"] = method
            if method == "tools/call":
                headers["Mcp-Name"] = params["name"]
            params["_meta"] = {"io.modelcontextprotocol/protocolVersion": MODERN,
                               "io.modelcontextprotocol/clientCapabilities": {}}
        message = {"jsonrpc": "2.0", "id": next(self.ids), "method": method,
                   "params": params}
        try:
            status, got, raw = _post(self.url, headers, json.dumps(message).encode("ascii"),
                                     self.timeout)
        except (OSError, http.client.HTTPException, ValueError) as e:
            # The type only: the message can quote a header.
            reason = getattr(e, "reason", e)
            kind = type(reason if isinstance(reason, Exception) else e).__name__
            raise Stop(2, f"Could not connect to {_host(self.url)} ({kind}).")
        if status != 200:
            raise Stop(2, _status_text(status, got))
        try:
            reply = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            reply = None
        reply = reply if isinstance(reply, dict) else {}
        error, result = reply.get("error"), reply.get("result")
        if isinstance(error, dict):
            raise Stop(1, "The server answered with an error: "
                          + _one_line(str(error.get("message"))))
        if not isinstance(result, dict) or reply.get("id") != message["id"]:
            raise Stop(2, "The server's answer is not an MCP answer.")
        return result

    def tool(self, name, arguments):
        """The result of one tool call."""
        result = self.call("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise Stop(1, "\n".join(_texts(result))
                       or "The server refused the call without a reason.")
        return result


# ---------------------------------------------------------------------------
# tool results
# ---------------------------------------------------------------------------
def _items(result, kind, key):
    """`key` of every content item of one type."""
    content = result.get("content")
    return [c.get(key) for c in (content if isinstance(content, list) else [])
            if isinstance(c, dict) and c.get("type") == kind]


def _texts(result):
    return [t for t in _items(result, "text", "text") if isinstance(t, str)]


def _structured(result, key):
    value = result.get("structuredContent")
    return value.get(key) if isinstance(value, dict) else None


def _list_chats(client):
    chats = _structured(client.tool("list_chats", {}), "chats")
    if not isinstance(chats, list):
        raise Stop(2, "The server's list of chats cannot be read.")
    return [c for c in chats
            if isinstance(c, dict) and isinstance(c.get("chat_id"), str) and c["chat_id"]]


def _ask(client, chat_id, question, max_wait):
    """Ask and wait. The result of the finished answer."""
    job_id = _structured(client.tool("ask", {"chat_id": chat_id, "question": question}),
                         "job_id")
    if not isinstance(job_id, str) or not job_id:
        raise Stop(2, "The server's answer to ask names no job.")
    _say("The question runs, waiting for the answer.", sys.stderr)
    deadline = time.monotonic() + max_wait
    while True:
        result = client.tool("get_answer", {"job_id": job_id})
        state = _structured(result, "state")
        if state == "done":
            return result
        if state != "running":
            raise Stop(2, "The server's answer to get_answer has no known state.")
        left = deadline - time.monotonic()
        if left <= 0:
            raise Stop(1, f"The question is still running after {max_wait:g} seconds.")
        time.sleep(min(POLL_PAUSE_S, left))


def _png(data):
    """The bytes of a PNG image, None for anything else."""
    try:
        raw = base64.b64decode(data, validate=True)
    except (TypeError, ValueError):
        return None
    return raw if raw.startswith(PNG_MAGIC) else None


def _new(create, name, end=""):
    """`create` the path, under a numbered name when it is taken."""
    for n in itertools.count(1):
        path = name if n == 1 else f"{name}_{n}"
        try:
            return create(path + end)
        except FileExistsError:
            continue


def _new_folder(name):
    os.mkdir(name)
    return name


def _show(result, out):
    """Print the texts of an answer, then save its charts."""
    for text in _texts(result):
        _say(text)
    images = [_png(data) for data in _items(result, "image", "data")]
    try:
        if any(images):
            if out is None:
                out = _new(_new_folder, time.strftime("mcp_answer_%Y%m%d_%H%M%S"))
            Path(out).mkdir(parents=True, exist_ok=True)
        for n, raw in enumerate(images, 1):
            if raw:
                with _new(lambda p: open(p, "xb"), str(Path(out) / f"chart_{n}"), ".png") as f:
                    f.write(raw)
                _say(f"Chart saved: {f.name}")
    except (OSError, ValueError) as e:
        raise Stop(1, f"The charts could not be saved ({type(e).__name__}).")
    if not all(images):
        raise Stop(1, "A chart of the answer is not a PNG image and was not saved.")


# ---------------------------------------------------------------------------
# the commands
# ---------------------------------------------------------------------------
def _run_chats(client, args):
    chats = _list_chats(client)
    for chat in chats:
        title, tables = chat.get("title"), chat.get("tables")
        names = [t for t in (tables if isinstance(tables, list) else [])
                 if isinstance(t, str)]
        _say(_one_line(f"{chat['chat_id']} | {title if isinstance(title, str) else ''} | "
                       f"{', '.join(names)}"))
    if not chats:
        _say("This user has no chats.")


def _run_ask(client, args):
    _show(_ask(client, args.chat_id, args.question, args.max_wait), args.out)


def _run_selftest(client, args):
    """The steps in turn; the first failure ends the test."""
    def start():
        if client.protocol == MODERN:
            offered = client.call(first, {}).get("supportedVersions")
            if not isinstance(offered, list):
                offered = []
        else:
            offered = [client.call(first, {
                "protocolVersion": LEGACY, "capabilities": {},
                "clientInfo": {"name": "mcp_test_client", "version": "1"}}
            ).get("protocolVersion")]
        if client.protocol not in offered:
            raise Stop(1, f"The server does not offer protocol {client.protocol}.")

    def tools():
        listed = client.call("tools/list", {}).get("tools")
        names = [t.get("name") for t in (listed if isinstance(listed, list) else [])
                 if isinstance(t, dict)]
        missing = [name for name in sorted(TOOLS) if name not in names]
        if missing:
            raise Stop(1, f"Not listed: {', '.join(missing)}.")

    def list_chats():
        chats.extend(_list_chats(client))

    def ask():
        chat_id = args.chat or (chats[0]["chat_id"] if chats else None)
        if not chat_id:
            raise Stop(1, "This user has no chat to ask in.")
        _show(_ask(client, chat_id, args.question, args.max_wait), args.out)

    first = "server/discover" if client.protocol == MODERN else "initialize"
    chats = []
    for name, step in ((first, start), ("tools/list", tools),
                       ("list_chats", list_chats), ("ask and get_answer", ask)):
        try:
            step()
        except Stop as e:
            _say(f"[FAIL] {name}: {_one_line(e.text)}")
            return e.code
        _say(f"[PASS] {name}")
    return 0


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------
class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Not the rest of the message: it quotes what was typed, maybe the token.
        self.print_usage(sys.stderr)
        self.exit(2, f"Wrong command line ({message.split(':')[0]}). See --help.\n")


def _url(value):
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError
    return value


def _seconds(value):
    seconds = float(value)
    if not 0 < seconds <= 86400:
        raise ValueError
    return seconds


def _parse(argv):
    parser = _Parser(prog="mcp_test_client.py", description=__doc__,
                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", type=_url, default=DEFAULT_URL,
                        help="http or https address of the endpoint (default %(default)s)")
    parser.add_argument("--token", help="MCP token (else the variable PDC_MCP_TOKEN)")
    parser.add_argument("--email", help="user to act for, with a per-user token")
    parser.add_argument("--protocol", choices=[LEGACY, MODERN], default=LEGACY,
                        help="protocol revision (default %(default)s)")
    parser.add_argument("--timeout", type=_seconds, default=DEFAULT_TIMEOUT_S,
                        help="seconds for one request (default %(default)s)")
    commands = parser.add_subparsers(dest="command", required=True)
    chats = commands.add_parser("chats", help="list the chats of the user")
    chats.set_defaults(run=_run_chats)
    ask = commands.add_parser("ask", help="ask a question and print the answer")
    ask.add_argument("chat_id", help="a chat id from the list of chats")
    ask.add_argument("question")
    ask.set_defaults(run=_run_ask)
    selftest = commands.add_parser("selftest", help="test every step of the endpoint")
    selftest.add_argument("--chat", help="chat to ask in (default: the first listed)")
    selftest.add_argument("--question", default=DEFAULT_QUESTION,
                          help="question to ask (default: %(default)s)")
    selftest.set_defaults(run=_run_selftest)
    for command in (ask, selftest):
        command.add_argument("--out", help="folder for the charts (default: a new one)")
        command.add_argument("--max-wait", type=_seconds, default=DEFAULT_MAX_WAIT_S,
                             help="seconds to wait for the answer (default %(default)s)")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse(argv)
    token = (args.token or os.environ.get("PDC_MCP_TOKEN") or "").strip()
    email = (args.email or "").strip()
    try:
        if not token:
            raise Stop(2, "No token: set PDC_MCP_TOKEN or use --token.")
        for what, value in (("token", token), ("email address", email)):
            if not re.fullmatch("[!-~]*", value):
                raise Stop(2, f"The {what} may hold only ASCII characters without spaces.")
        client = Client(args.url, token, email, args.protocol, args.timeout)
        return args.run(client, args) or 0
    except Stop as e:
        _say(e.text, sys.stderr)
        return e.code
    except KeyboardInterrupt:
        _say("Interrupted.", sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
