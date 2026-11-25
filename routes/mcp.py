"""POST /mcp — the MCP endpoint (Streamable HTTP, plain JSON answers).

A chatbot inside the customer's network calls three tools: `list_chats`,
`ask`, `get_answer`. The order of the checks: Origin, token, body, the token
again (it may be revoked while the body is sent), JSON-RPC, and for a tool call
the caller's identity (mcp_tokens.py). `get_answer` looks at the token again
while it waits and before it delivers. No session, no cookie, no state between
requests.

Two protocol revisions are served, chosen per request:
* 2025-11-25 (legacy): initialize, ping, tools/list, tools/call;
* 2026-07-28 (modern): server/discover, tools/list, tools/call, with the
  version in `params._meta` and the request headers checked against the body.

A tool problem (an empty question, an unknown job) is a normal result with
`isError`; a protocol problem is a JSON-RPC error. Nothing unexpected reaches
the caller but "Internal error".

Failed tokens are counted per peer address: over the limit a FAILED token gets
429 instead of 401. A valid token always passes, so a shared proxy address
cannot lock a working chatbot out.

File work runs on this module's own small pool; waiting for an answer is a
coroutine and takes no thread. The log gets one line per tool call — never the
token, the question or the answer.

`admin_router` is the local admin's token API (/api/admin/mcp/tokens*): list,
create, revoke. Guarded like every other admin route and audited, without the
token.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect

import db_sources
import local_store
import mcp_jobs
import mcp_tokens
import routes.auth as _auth
import routes.chat as _chat
from logger_utils import log_safe_value, log_with_sid
from routes.admin_data import _json_body, _require_admin
from settings import settings

router = APIRouter()
admin_router = APIRouter(prefix="/api/admin/mcp", tags=["client-admin-mcp"])

MAX_BODY_BYTES = 1024 * 1024
BODY_READ_TIMEOUT_S = 30         # for the whole body

FAILED_TOKEN_LIMIT = 20
FAILED_TOKEN_WINDOW_S = 600
FAILED_TOKEN_MAX_KEYS = 10_000

# get_answer: how often a running job is looked at, and how often the token
# and the chat access are checked again meanwhile.
POLL_S = 0.2
ACCESS_RECHECK_S = 5.0

MODERN = "2026-07-28"
LEGACY = "2025-11-25"
SUPPORTED_VERSIONS = (MODERN, LEGACY)
_META_VERSION = "io.modelcontextprotocol/protocolVersion"
_META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
_META_SERVER = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022

_ECHO_MAX_CHARS = 80
_LIST_TTL_MS = 300_000

INSTRUCTIONS = (
    "PowerDataChat answers questions about a company's own data. Call list_chats first to "
    "see the chats of the user, then ask with a chat_id and the question, then get_answer "
    "with the job_id. While get_answer says the state is running, call it again.")

TOOLS = [
    {
        "name": "list_chats",
        "title": "List chats",
        "description": (
            "Lists the PowerDataChat chats the user can open: the chat_id, the title and the "
            "names of the tables or files in each chat. Call this first, then choose the chat "
            "whose tables fit the question and call ask."),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "ask",
        "title": "Ask a question",
        "description": (
            "Asks a question about the data of one chat. Use a chat_id from list_chats. The "
            "question runs in the background: this returns a job_id at once. Call get_answer "
            "with that job_id to receive the answer."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "A chat_id from list_chats."},
                "question": {"type": "string",
                             "description": "The question, in the user's own words."},
            },
            "required": ["chat_id", "question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_answer",
        "title": "Get the answer",
        "description": (
            "Returns the answer of a question started with ask. The state is running, done or "
            "failed. While it is running, call get_answer again with the same job_id; an "
            "analysis can take a few minutes."),
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string",
                                      "description": "The job_id returned by ask."}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
]
_TOOL_NAMES = tuple(t["name"] for t in TOOLS)

CHAT_NOT_FOUND_TEXT = mcp_jobs.CHAT_NOT_FOUND_TEXT
EMPTY_QUESTION_TEXT = "Question cannot be empty."
JOB_NOT_FOUND_TEXT = "Job not found."
# One sentence per cause; "not allowed" never says whether an account exists.
_NOT_ALLOWED_TEXT = "This user is not allowed to use PowerDataChat through this connection."
_REFUSAL_TEXTS = {
    mcp_tokens.MISSING_HEADER: "The X-PDC-User-Email header is missing.",
    mcp_tokens.MALFORMED_HEADER:
        "The X-PDC-User-Email header must hold exactly one email address.",
    mcp_tokens.NOT_ALLOWED: _NOT_ALLOWED_TEXT,
}

_EXEC = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mcp_http")
atexit.register(lambda: _EXEC.shutdown(wait=False, cancel_futures=True))


async def _off_loop(func, *args):
    """Run blocking work on this module's pool."""
    return await asyncio.get_running_loop().run_in_executor(_EXEC, func, *args)


# ---------------------------------------------------------------------------
# the failed-token limiter
# ---------------------------------------------------------------------------
# The limiter's clock; a module attribute so the tests can replace it.
_limiter_clock = time.monotonic

_LIMITER_LOCK = threading.Lock()
# peer -> {"failures": [stamps], "logged": {reason: stamp}}, least recent first.
_PEERS: dict[str, dict] = {}


def reset_limiter() -> None:
    """Forget every peer (tests)."""
    with _LIMITER_LOCK:
        _PEERS.clear()


def _peer_entry(peer: str) -> dict:
    """The peer's entry, now the most recent one. Called under the lock."""
    entry = _PEERS.pop(peer, None) or {"failures": [], "logged": {}}
    _PEERS[peer] = entry
    while len(_PEERS) > FAILED_TOKEN_MAX_KEYS:
        del _PEERS[next(iter(_PEERS))]
    return entry


def _log_refusal(peer: str, reason: str) -> None:
    """One line per peer, reason and window."""
    now = _limiter_clock()
    with _LIMITER_LOCK:
        logged = _peer_entry(peer)["logged"]
        last = logged.get(reason)
        if last is not None and now - last < FAILED_TOKEN_WINDOW_S:
            return
        logged[reason] = now
    log_with_sid("mcp", "warning",
                 f"MCP_REFUSED reason={log_safe_value(reason, 20)} "
                 f"peer={log_safe_value(peer, 64)}")


def _failed_token(peer: str) -> int:
    """Count a failed token. 0: within the limit; else the seconds to wait. An
    attempt over the limit is not counted."""
    now, window = _limiter_clock(), FAILED_TOKEN_WINDOW_S
    with _LIMITER_LOCK:
        entry = _peer_entry(peer)
        failures = entry["failures"] = [t for t in entry["failures"] if now - t < window]
        if len(failures) < FAILED_TOKEN_LIMIT:
            failures.append(now)
            return 0
        # The wait ends when fewer than the limit are left in the window.
        return max(1, math.ceil(failures[-FAILED_TOKEN_LIMIT] + window - now))


# ---------------------------------------------------------------------------
# Origin, token, body
# ---------------------------------------------------------------------------
def _refusal(status: int, text: str, headers: Optional[dict] = None) -> JSONResponse:
    """A refusal before JSON-RPC: a bare JSON body."""
    return JSONResponse({"error": text}, status_code=status, headers=headers)


def _own_origin() -> Optional[str]:
    """The origin of PUBLIC_BASE_URL, as a browser would send it."""
    base = _auth._public_base()
    if not base:
        return None
    try:
        parts = urlsplit(base)
        host, port = parts.hostname, parts.port
    except ValueError as e:
        log_with_sid("mcp", "warning",
                     f"MCP_BASE_URL_INVALID error={log_safe_value(type(e).__name__, 80)}")
        return None
    if not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    scheme = parts.scheme.lower()
    if port is None or port == {"http": 80, "https": 443}[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _origin_ok(request: Request) -> bool:
    """A browser page of another site may not call; no Origin is no browser."""
    origins = request.headers.getlist("origin")
    if not origins:
        return True
    return len(origins) == 1 and origins[0] == _own_origin()


def _bearer(request: Request) -> Optional[str]:
    """The bearer token of the one Authorization header; None for a text that
    cannot be a token (it is not looked up)."""
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        return None
    scheme, _, token = values[0].partition(" ")
    token = token.strip(" ")
    if scheme.lower() != "bearer" or not mcp_tokens._TOKEN_RE.fullmatch(token):
        return None
    return token


def _unauthorized() -> JSONResponse:
    return _refusal(401, "Unauthorized", {"WWW-Authenticate": "Bearer"})


def _authenticate(token: str) -> Optional[dict]:
    record = mcp_tokens.verify(token)
    if record is not None:
        mcp_tokens.touch(record["id"])
    return record


async def _read_body(request: Request) -> Optional[bytes]:
    """The body, None when it is over the cap. A declared length over the cap
    is refused unread; anything else is counted while it is read."""
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() \
            and (len(declared) > 10 or int(declared) > MAX_BODY_BYTES):
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------
class _TokenRefused(Exception):
    """The token stopped being valid during the request."""


class _RpcError(Exception):
    """A protocol error: its JSON-RPC code, its text and the HTTP status."""

    def __init__(self, code: int, text: str, status: int = 200, data: Any = None):
        super().__init__(text)
        self.code, self.text, self.status, self.data = code, text, status, data


@dataclass
class _Request:
    """One JSON-RPC request of a verified token."""
    http: Request
    token: str = field(repr=False)      # only to verify it again
    record: dict = field(repr=False)    # only for mcp_tokens.resolve_caller
    peer: str
    method: str
    params: Any
    modern: bool


def _json(message: dict, status: int = 200) -> Response:
    # ASCII-escaped: a lone surrogate from the request cannot be sent as UTF-8.
    return Response(json.dumps(message), status_code=status, media_type="application/json")


def _error(id: Any, error: _RpcError) -> Response:
    body = {"code": error.code, "message": error.text}
    if error.data is not None:
        body["data"] = error.data
    return _json({"jsonrpc": "2.0", "id": id, "error": body}, error.status)


def _valid_id(value: Any) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _header(request: Request, name: str) -> Optional[str]:
    """None when absent; repeated copies are joined, so they match nothing."""
    values = request.headers.getlist(name)
    return ", ".join(values) if values else None


def _is_modern(method: str, params: Any, version: Optional[str]) -> bool:
    if method == "initialize":
        return False
    if method == "server/discover" or version == MODERN:
        return True
    meta = params.get("_meta") if isinstance(params, dict) else None
    return isinstance(meta, dict) and _META_VERSION in meta


def _unsupported(version: str) -> _RpcError:
    return _RpcError(UNSUPPORTED_VERSION, "Unsupported protocol version", 400,
                     {"supported": list(SUPPORTED_VERSIONS),
                      "requested": version[:_ECHO_MAX_CHARS]})


def _check_legacy(method: str, version: Optional[str]) -> None:
    if method != "initialize" and version is not None and version != LEGACY:
        raise _unsupported(version)


def _check_modern(request: Request, method: str, params: Any, header: Optional[str]) -> None:
    """The checks of the 2026-07-28 revision, in its order."""
    if header is None:
        raise _RpcError(HEADER_MISMATCH, "The MCP-Protocol-Version header is missing", 400)
    meta = params.get("_meta") if isinstance(params, dict) else None
    version = meta.get(_META_VERSION) if isinstance(meta, dict) else None
    if not isinstance(version, str):
        raise _RpcError(INVALID_PARAMS, "params._meta must name the protocol version", 400)
    if header != version:
        raise _RpcError(HEADER_MISMATCH,
                        "The MCP-Protocol-Version header does not match params._meta", 400)
    if version != MODERN:
        raise _unsupported(version)
    if not isinstance(meta.get(_META_CAPABILITIES), dict):
        raise _RpcError(INVALID_PARAMS, "params._meta must hold the client capabilities", 400)
    if _header(request, "mcp-method") != method:
        raise _RpcError(HEADER_MISMATCH,
                        "The Mcp-Method header is missing or does not match the body", 400)
    if method == "tools/call":
        name = _header(request, "mcp-name")
        if name is None or name != params.get("name"):
            raise _RpcError(HEADER_MISMATCH,
                            "The Mcp-Name header is missing or does not match the body", 400)


def _server_info() -> dict:
    return {"name": "powerdatachat", "title": "PowerDataChat",
            "version": settings.BUILD_COMMIT or "dev"}


async def _result(rpc: _Request) -> dict:
    """The result of a checked request, before the modern wrapping."""
    if rpc.method == "tools/list":
        result: dict = {"tools": TOOLS}
        if rpc.modern:
            result.update(ttlMs=_LIST_TTL_MS, cacheScope="private")
        return result
    if rpc.method == "tools/call":
        return await _call_tool(rpc)
    if rpc.modern:
        if rpc.method == "server/discover":
            return {"supportedVersions": list(SUPPORTED_VERSIONS),
                    "capabilities": {"tools": {}}, "instructions": INSTRUCTIONS,
                    "ttlMs": _LIST_TTL_MS, "cacheScope": "private"}
        raise _RpcError(METHOD_NOT_FOUND, "Method not found", 404)
    if rpc.method == "initialize":
        # Whatever version was asked for: this is the one initialize offers.
        return {"protocolVersion": LEGACY, "capabilities": {"tools": {}},
                "serverInfo": _server_info(), "instructions": INSTRUCTIONS}
    if rpc.method == "ping":
        return {}
    raise _RpcError(METHOD_NOT_FOUND, "Method not found")


async def _handle(request: Request, token: str, record: dict, peer: str,
                  body: bytes) -> Response:
    """One JSON-RPC message."""
    try:
        message = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError) as e:       # UnicodeDecodeError is a ValueError
        log_with_sid("mcp", "info",
                     f"MCP_PARSE_ERROR error={log_safe_value(type(e).__name__, 80)}")
        return _error(None, _RpcError(PARSE_ERROR, "Parse error", 400))
    if not isinstance(message, dict):
        return _error(None, _RpcError(INVALID_REQUEST, "One JSON object is expected", 400))
    if "id" not in message:
        return Response(status_code=202)            # a notification: nothing to do
    id = message["id"]
    if not _valid_id(id):
        return _error(None, _RpcError(INVALID_REQUEST, "The id must be a string or a number",
                                      400))
    try:
        method, params = message.get("method"), message.get("params")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            raise _RpcError(INVALID_REQUEST, "Not a JSON-RPC 2.0 request", 400)
        version = _header(request, "mcp-protocol-version")
        modern = _is_modern(method, params, version)
        if modern:
            _check_modern(request, method, params, version)
        else:
            _check_legacy(method, version)
        result = await _result(
            _Request(request, token, record, peer, method, params, modern))
        if modern:
            result = {"resultType": "complete", "_meta": {_META_SERVER: _server_info()},
                      **result}
        return _json({"jsonrpc": "2.0", "id": id, "result": result})
    except _TokenRefused:
        return _unauthorized()      # logged with the call's outcome
    except _RpcError as e:
        return _error(id, e)        # the caller's mistake, its text is ours
    except Exception as e:
        # The type only: the message can quote a value.
        log_with_sid("mcp", "error",
                     f"MCP_INTERNAL_ERROR error={log_safe_value(type(e).__name__, 80)}")
        return _error(id, _RpcError(INTERNAL_ERROR, "Internal error"))


# ---------------------------------------------------------------------------
# the tools
# ---------------------------------------------------------------------------
@dataclass
class _Call:
    """One tool call, as its log line names it."""
    tool: Any
    token_id: str
    token_name: Any
    email: str = ""
    chat_id: Any = None
    job_id: Any = None
    outcome: str = "error"


def _structured(value: dict) -> dict:
    """A result the model reads as text and a program as data."""
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
            "structuredContent": value}


def _tool_error(call: _Call, outcome: str, text: str) -> dict:
    call.outcome = outcome
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _table_names(chat_id: str) -> list[str]:
    """The names of the chat's tables and files."""
    try:
        files = local_store.ChatDataStore(chat_id).read_meta().get("files")
        if not isinstance(files, list):
            return []
        names = [f.get("file_name") for f in files if isinstance(f, dict)]
        return [n for n in names if isinstance(n, str) and n]
    except Exception as e:
        log_with_sid("mcp", "warning",
                     f"MCP_CHAT_META_FAILED error={log_safe_value(type(e).__name__, 80)}",
                     chat_id=log_safe_value(chat_id, 80))
        return []


def _list_chats(email: str) -> list[dict]:
    chats = []
    for row in local_store.AuthStore().list_active_chats(email):
        chat_id, title = row.get("chat_id"), row.get("title")
        if mcp_jobs.may_open(email, chat_id):
            chats.append({"chat_id": chat_id, "title": title if isinstance(title, str) else "",
                          "tables": _table_names(chat_id)})
    return chats


def _start_question(call: _Call, arguments: dict) -> dict:
    """`ask`: the checks of a web question that need no dataset, then the job.
    The others run in the job (chat_turn.prepare_turn)."""
    chat_id, question = arguments.get("chat_id"), arguments.get("question")
    call.chat_id = chat_id
    if not mcp_jobs.may_open(call.email, chat_id):
        return _tool_error(call, "refused", CHAT_NOT_FOUND_TEXT)
    question = question.strip() if isinstance(question, str) else ""
    if not question:
        return _tool_error(call, "refused", EMPTY_QUESTION_TEXT)
    busy = _chat._auto_analysis_busy_response(
        local_store.ChatDataStore(chat_id), call.email, chat_id)
    if busy is not None:
        return _tool_error(call, "refused", json.loads(busy.body)["error"])
    try:
        job = mcp_jobs.start(call.token_id, call.token_name, call.email, chat_id, question)
    except mcp_jobs.JobRefused as e:
        return _tool_error(call, "refused", str(e))     # our own sentence
    call.job_id, call.outcome = job.id, "ok"
    return _structured({"job_id": job.id, "state": "running"})


async def _get_answer(call: _Call, token: str, arguments: dict) -> dict:
    """Wait for the job, at most the configured time. The token and the chat
    access are checked again before every answer and now and then meanwhile."""
    call.job_id = arguments.get("job_id")
    deadline = time.monotonic() + settings.MCP_ANSWER_WAIT_SECONDS
    next_check = 0.0
    while True:
        job = mcp_jobs.get(call.job_id, call.token_id, call.email)
        if job is None:
            break
        call.chat_id = job.chat_id
        now = time.monotonic()
        last_look = job.state != "running" or now >= deadline
        if last_look or now >= next_check:
            if await _off_loop(mcp_tokens.verify, token) is None:
                call.outcome = "token_refused"
                raise _TokenRefused()
            if not await _off_loop(mcp_jobs.may_open, call.email, job.chat_id):
                break
            next_check = now + ACCESS_RECHECK_S
        if last_look:
            call.outcome = job.state
            return _answer(job)
        await asyncio.sleep(min(POLL_S, deadline - now))
    # The same answer whatever the reason.
    return _tool_error(call, "not_found", JOB_NOT_FOUND_TEXT)


def _answer(job: mcp_jobs.Job) -> dict:
    state = {"job_id": job.id, "state": job.state}
    if job.state == "running":
        return _structured(state)
    result = {"content": job.content, "structuredContent": state}
    if job.state == "failed":
        result["isError"] = True
    return result


async def _run_tool(rpc: _Request, call: _Call, arguments: dict) -> dict:
    emails = rpc.http.headers.getlist("x-pdc-user-email")
    caller = await _off_loop(mcp_tokens.resolve_caller, rpc.record, emails, rpc.peer)
    if not caller.email:
        refusal = caller.refusal or mcp_tokens.NOT_ALLOWED
        return _tool_error(call, refusal, _REFUSAL_TEXTS.get(refusal, _NOT_ALLOWED_TEXT))
    call.email = caller.email
    if call.tool == "list_chats":
        chats = await _off_loop(_list_chats, call.email)
        call.outcome = "ok"
        return _structured({"chats": chats})
    if call.tool == "ask":
        return await _off_loop(_start_question, call, arguments)
    return await _get_answer(call, rpc.token, arguments)


def _log_call(call: _Call, started: float) -> None:
    def text(value, max_chars):
        """Bounded, printable text of a request value (a lone surrogate
        included), for log_safe_value."""
        return ascii(value[:max_chars])[1:-1] if isinstance(value, str) else ""

    log_with_sid(log_safe_value(call.email, 254) or "mcp", "info",
                 f"MCP_CALL token={log_safe_value(call.token_name, 120)} "
                 f"tool={log_safe_value(text(call.tool, 40), 80)} "
                 f"outcome={log_safe_value(call.outcome, 40)} "
                 f"ms={int((time.monotonic() - started) * 1000)}",
                 chat_id=log_safe_value(text(call.chat_id, 64), 80),
                 job_id=log_safe_value(text(call.job_id, 32), 80))


async def _call_tool(rpc: _Request) -> dict:
    """`tools/call`. The caller is resolved after the parameters are checked."""
    started = time.monotonic()
    params = rpc.params if isinstance(rpc.params, dict) else {}
    call = _Call(params.get("name"), rpc.record["id"], rpc.record.get("name"))
    try:
        if call.tool not in _TOOL_NAMES:
            call.outcome = "bad_params"
            raise _RpcError(INVALID_PARAMS, "Unknown tool")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            call.outcome = "bad_params"
            raise _RpcError(INVALID_PARAMS, "The arguments must be an object")
        return await _run_tool(rpc, call, arguments)
    finally:
        _log_call(call, started)


# ---------------------------------------------------------------------------
# the route
# ---------------------------------------------------------------------------
@router.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    # The socket's own address: a forwarded header is the caller's claim.
    peer = request.client.host if request.client else ""
    if not _origin_ok(request):
        _log_refusal(peer, "origin")
        return _refusal(403, "Origin not allowed")
    token = _bearer(request)
    record = await _off_loop(_authenticate, token) if token else None
    if record is None:
        _log_refusal(peer, "token")
        wait = _failed_token(peer)
        if wait:
            return _refusal(429, "Too many failed attempts", {"Retry-After": str(wait)})
        return _unauthorized()
    try:
        async with asyncio.timeout(BODY_READ_TIMEOUT_S):
            body = await _read_body(request)
    except ClientDisconnect:
        log_with_sid("mcp", "info", "MCP_CLIENT_DISCONNECTED")
        return _refusal(400, "Incomplete request")
    except TimeoutError:
        log_with_sid("mcp", "warning", f"MCP_BODY_TIMEOUT peer={log_safe_value(peer, 64)}")
        return _refusal(408, "Request body not received in time", {"Connection": "close"})
    if body is None:
        return _refusal(413, "Request too large", {"Connection": "close"})
    record = await _off_loop(mcp_tokens.verify, token)
    if record is None:              # revoked while the body was sent
        return _unauthorized()
    return await _handle(request, token, record, peer, body)


# ---------------------------------------------------------------------------
# the local admin's token API
# ---------------------------------------------------------------------------
# One text for every failure of the token file; the cause is in the log. The
# admin page translates both (admin.mcp.error_*, matched on the English text).
TOKEN_STORE_TEXT = "The tokens could not be read or saved. The server log has the reason."
UNKNOWN_TOKEN_TEXT = "Unknown token."


def _endpoint() -> str:
    """The address a chatbot calls; "" without a usable PUBLIC_BASE_URL (the
    request's own address is the caller's claim)."""
    base = _auth._public_base()
    return f"{base}/mcp" if base else ""


def _admin_failure(admin: str, what: str, e: Exception) -> JSONResponse:
    log_with_sid(log_safe_value(admin, 254), "error",
                 f"MCP_ADMIN_FAILED what={log_safe_value(what, 20)} "
                 f"error={log_safe_value(type(e).__name__, 80)}")
    return JSONResponse({"error": TOKEN_STORE_TEXT}, status_code=500)


def _admin_create(admin: str, body: dict) -> tuple[dict, str]:
    row, raw = mcp_tokens.create_token(body.get("name"), body.get("mode"), admin,
                                       body.get("account"))
    db_sources.audit(admin, "mcp_token.create", target=row["id"],
                     detail={"name": row["name"], "mode": row["mode"],
                             "account": row["account"]})
    return row, raw


def _admin_revoke(admin: str, token_id: str) -> Optional[dict]:
    row = mcp_tokens.revoke_token(token_id, admin)
    if row is not None:
        mcp_jobs.drop_token(row["id"])      # only once the token is off
        db_sources.audit(admin, "mcp_token.revoke", target=row["id"],
                         detail={"name": row["name"]})
    return row


@admin_router.get("/tokens")
async def list_tokens(request: Request):
    email, err = _require_admin(request)
    if err:
        return err
    return {"tokens": await _off_loop(mcp_tokens.list_tokens), "endpoint": _endpoint()}


@admin_router.post("/tokens")
async def create_token(request: Request):
    """The only answer that ever carries the raw token."""
    email, err = _require_admin(request)
    if err:
        return err
    body = await _json_body(request)
    try:
        row, raw = await _off_loop(_admin_create, email, body)
    except ValueError as e:
        # A sentence of mcp_tokens about the admin's own input.
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return _admin_failure(email, "create", e)
    return JSONResponse({"token": row, "raw_token": raw, "endpoint": _endpoint()},
                        headers={"Cache-Control": "no-store"})


@admin_router.post("/tokens/{token_id}/revoke")
async def revoke_token(request: Request, token_id: str):
    """The token's jobs are forgotten with it. The body is ignored."""
    email, err = _require_admin(request)
    if err:
        return err
    try:
        row = await _off_loop(_admin_revoke, email, token_id)
    except Exception as e:
        return _admin_failure(email, "revoke", e)
    if row is None:
        return JSONResponse({"error": UNKNOWN_TOKEN_TEXT}, status_code=404)
    return {"token": row}
