"""MCP stage 3 — the endpoint: transport, token, protocol (no tool behaviour).

`POST /mcp` on the real app. The order of the checks is part of the contract:
Origin -> token -> body -> JSON-RPC -> (tools/call only) the caller's
identity. The token is verified again once the body is there, which must take
at most `BODY_READ_TIMEOUT_S`; a text that cannot be a token is refused without
a look into the token file. Two protocol revisions are served, per request:

* LEGACY, 2025-11-25: `initialize`, `ping`, `tools/list`, `tools/call`. No
  session id, `initialize` is not required first. Results carry no
  `resultType`.
* MODERN, 2026-07-28: `server/discover`, `tools/list`, `tools/call`. A request
  is modern when its method is `server/discover`, its `params._meta` names a
  protocol version, or its `MCP-Protocol-Version` header is 2026-07-28 —
  `initialize` is always legacy. The header checks run in a fixed order.

Failed tokens are limited per peer address inside routes/mcp.py
(`FAILED_TOKEN_LIMIT` / `FAILED_TOKEN_WINDOW_S`, `_limiter_clock`,
`reset_limiter()`); a valid token is never limited. Also pinned here:
`FAILED_TOKEN_MAX_KEYS`, the bound on the remembered peers. All of them are
read per request.

Offline: the real app, DATA_ROOT is tmp_path, the TestClient is built without
the context manager (no lifespan). Every response of every request below is
checked for `Set-Cookie`: the endpoint never touches the session.
"""
import asyncio
import hashlib
import importlib
import json
import logging
import threading

import httpx
import pytest
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import local_store
import logger_utils
import mcp_tokens
from settings import settings
from tests.conftest import csrf_form

ADMIN = "ladmin"
USER = "user@corp.example"
LEGACY = "2025-11-25"
MODERN = "2026-07-28"
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_INFO = "io.modelcontextprotocol/serverInfo"
TOOL_NAMES = ["ask", "get_answer", "list_chats"]

PARSE, INVALID_REQUEST, NO_METHOD = -32700, -32600, -32601
BAD_PARAMS, INTERNAL = -32602, -32603
HEADER_MISMATCH, BAD_VERSION = -32020, -32022
# The only codes of the spec's reserved range the endpoint may emit.
RESERVED_ALLOWED = {HEADER_MISMATCH, BAD_VERSION}


# ===========================================================================
# the world
# ===========================================================================
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            pass


@pytest.fixture
def logs():
    """Every line written through `log_with_sid`, whichever module emits it."""
    logger = logger_utils.get_logger()
    handler = _Capture()
    old_level = logger.level
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", ADMIN)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    monkeypatch.setattr(settings, "EXECUTOR_NETWORK_CIDR", "")
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    local_store._DATAFRAME_CACHE.invalidate()
    local_store.AuthStore().ensure_user(USER)
    yield tmp_path
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def mcp(root, monkeypatch):
    """The endpoint module, with fresh limiter and job state. `clock["t"]` is
    the limiter's clock."""
    mod = importlib.import_module("routes.mcp")
    jobs = importlib.import_module("mcp_jobs")
    mod.reset_limiter()
    jobs.reset()
    yield mod
    mod.reset_limiter()
    jobs.reset()


@pytest.fixture
def clock(mcp, monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(mcp, "_limiter_clock", lambda: now["t"])
    return now


@pytest.fixture
def token(root):
    _row, raw = mcp_tokens.create_token("Helpdesk bot", "per_user", ADMIN)
    return raw


class Peer:
    """A chatbot backend: one peer address, every response checked."""

    def __init__(self, host="203.0.113.7", raw=None, port=50000):
        self.client = TestClient(app_mod.app, base_url="https://testserver",
                                 client=(host, port), follow_redirects=False)
        self.raw = raw

    def request(self, method, *, headers=None, **kw):
        hdrs = list(headers or [])
        if self.raw:
            hdrs.append(("Authorization", f"Bearer {self.raw}"))
        r = self.client.request(method, "/mcp", headers=hdrs, **kw)
        assert "set-cookie" not in r.headers, "the endpoint must never set a cookie"
        assert "mcp-session-id" not in r.headers, "the endpoint is stateless"
        return r

    def post(self, content, *, headers=None, ctype="application/json"):
        return self.request("POST", content=content,
                            headers=[("Content-Type", ctype)] + list(headers or []))

    def send(self, message, *, headers=None):
        return self.post(json.dumps(message), headers=headers)

    def legacy(self, method, params=None, *, id=1, headers=None):
        msg = {"jsonrpc": "2.0", "id": id, "method": method}
        if params is not None:
            msg["params"] = params
        return self.send(msg, headers=headers)

    def modern(self, method, params=None, *, id=1, headers=None, drop=()):
        params = dict(params or {})
        params["_meta"] = {META_VERSION: MODERN, META_CAPS: {}}
        hdrs = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method}
        if method == "tools/call":
            hdrs["Mcp-Name"] = params.get("name")
        hdrs.update(headers or {})
        return self.send({"jsonrpc": "2.0", "id": id, "method": method, "params": params},
                         headers=[(k, v) for k, v in hdrs.items() if k not in drop])


@pytest.fixture
def bot(mcp, token):
    return Peer(raw=token)


def result_of(r, id=1):
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert r.headers["content-type"].split(";")[0] == "application/json"
    body = r.json()
    assert set(body) == {"jsonrpc", "id", "result"}, body
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == id and type(body["id"]) is type(id)
    return body["result"]


def error_of(r, status, code, id=1):
    assert r.status_code == status, (r.status_code, r.text[:300])
    assert r.headers["content-type"].split(";")[0] == "application/json"
    body = r.json()
    assert set(body) == {"jsonrpc", "id", "error"}, body
    assert body["jsonrpc"] == "2.0" and body["id"] == id, body
    err = body["error"]
    assert err["code"] == code, err
    assert isinstance(err["message"], str) and err["message"]
    assert len(err["message"]) <= 300, "an error message is bounded"
    if -32099 <= err["code"] <= -32020:
        assert err["code"] in RESERVED_ALLOWED
    assert "Traceback" not in r.text
    return err


# ===========================================================================
# the module
# ===========================================================================
def test_the_module_constants(mcp):
    assert mcp.MAX_BODY_BYTES == 1024 * 1024
    assert mcp.BODY_READ_TIMEOUT_S == 30
    assert mcp.FAILED_TOKEN_LIMIT == 20
    assert mcp.FAILED_TOKEN_WINDOW_S == 600
    assert tuple(mcp.SUPPORTED_VERSIONS) == (MODERN, LEGACY)


def test_the_router_holds_only_the_post(mcp):
    assert sorted((m, r.path) for r in mcp.router.routes for m in r.methods) == \
        [("POST", "/mcp")]


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_any_other_http_method_is_405(bot, method):
    r = bot.request(method, headers=[("Content-Type", "application/json")])
    assert r.status_code == 405, (r.status_code, r.text[:200])


def test_the_security_header_is_there_and_no_page_policy(bot):
    r = bot.legacy("ping")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in r.headers


# ===========================================================================
# the content type
# ===========================================================================
PING = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})


@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded",
                                   "multipart/form-data", "application/jsonp", ""])
def test_another_media_type_is_415(bot, ctype):
    r = bot.post(PING, ctype=ctype)
    assert r.status_code == 415, (r.status_code, r.text[:200])


def test_json_with_a_charset_is_accepted(bot):
    assert result_of(bot.post(PING, ctype="application/json; charset=utf-8")) == {}


# ===========================================================================
# the token
# ===========================================================================
UNKNOWN = "pdcmcp_" + "B" * 43


def _refused(r):
    assert r.status_code == 401, (r.status_code, r.text[:300])
    assert r.headers["www-authenticate"] == "Bearer"
    body = r.json()
    assert isinstance(body, dict) and "jsonrpc" not in body and "result" not in body
    return r.content


def test_with_no_token_created_everything_is_refused(mcp, root):
    peer = Peer(raw=UNKNOWN)
    body = _refused(peer.legacy("initialize", {"protocolVersion": LEGACY}))
    assert _refused(peer.modern("server/discover")) == body
    assert not (root / "mcp_tokens.json").exists()


def test_missing_unknown_revoked_and_repeated_tokens_look_the_same(mcp, token):
    row, revoked = mcp_tokens.create_token("Old bot", "per_user", ADMIN)
    mcp_tokens.revoke_token(row["id"], ADMIN)
    peer = Peer()
    bodies = {_refused(peer.post(PING, headers=h)) for h in (
        [],
        [("Authorization", f"Bearer {UNKNOWN}")],
        [("Authorization", f"Bearer {revoked}")],
        [("Authorization", f"Bearer {token}"), ("Authorization", f"Bearer {token}")],
        [("Authorization", f"Bearer {token}"), ("Authorization", "Bearer x")],
    )}
    assert len(bodies) == 1, "the refusals can be told apart"


@pytest.mark.parametrize("shape", ["{t}", "Basic {t}", "Bearer", "Bearer {t} x",
                                   "Token {t}", "Bearer {t}, Bearer {t}", "Bearer x{t}",
                                   "Bearer {cut}", "", "Bearer \xe9\xe8"])
def test_other_authorization_shapes_are_refused(mcp, token, shape):
    value = shape.format(t=token, cut=token[:-1])
    _refused(Peer().post(PING, headers=[(b"authorization", value.encode("latin-1"))]))


@pytest.mark.parametrize("value", [
    None, "", "Bearer", "Bearer x", "Basic {t}", "{t}", "Bearer {t}x", "Bearer {cut}",
    "Bearer {cut}!", "Bearer {cut}=", "Bearer {cut}\xe9", "Bearer pdcmcp_",
    "Bearer PDCMCP_" + "A" * 43, "Bearer other_" + "A" * 43, "Bearer {t} {t}",
    "Bearer pdcmcp_" + "A" * 5000,
], ids=range(16))
def test_a_text_that_cannot_be_a_token_is_never_looked_up(mcp, token, monkeypatch, value):
    """Unauthenticated noise costs no read of the token file."""
    seen = []
    monkeypatch.setattr(mcp_tokens, "verify", lambda raw: seen.append(raw))
    headers = [] if value is None else [
        (b"authorization", value.format(t=token, cut=token[:-1]).encode("latin-1"))]
    _refused(Peer().post(PING, headers=headers))
    assert seen == []


def test_two_authorization_headers_are_never_looked_up(mcp, token, monkeypatch):
    seen = []
    monkeypatch.setattr(mcp_tokens, "verify", lambda raw: seen.append(raw))
    _refused(Peer().post(PING, headers=[("Authorization", f"Bearer {token}")] * 2))
    assert seen == []


def test_a_well_formed_unknown_token_is_looked_up(mcp, token, monkeypatch):
    """The other half: the shape check alone refuses nothing else."""
    real, seen = mcp_tokens.verify, []
    monkeypatch.setattr(mcp_tokens, "verify", lambda raw: seen.append(raw) or real(raw))
    _refused(Peer(raw=UNKNOWN).legacy("ping"))
    assert seen == [UNKNOWN]


def test_malformed_tokens_are_counted_like_the_others(mcp, clock):
    peer = Peer()
    for i in range(20):
        assert peer.post(PING, headers=[("Authorization", "Bearer x")]).status_code == 401, i
    assert peer.post(PING, headers=[("Authorization", "Bearer x")]).status_code == 429


def test_a_token_anywhere_else_is_not_read(mcp, token):
    peer = Peer()
    msg = {"jsonrpc": "2.0", "id": 1, "method": "ping",
           "params": {"_meta": {"authorization": f"Bearer {token}"}}}
    _refused(peer.send(msg, headers=[("Cookie", f"token={token}"),
                                     ("X-Authorization", f"Bearer {token}")]))
    _refused(peer.client.post(f"/mcp?token={token}&access_token={token}", json=msg))


def test_a_browser_session_is_not_a_token(mcp, root):
    """A signed-in browser gets the same 401: cookies do not count here."""
    local_store.AuthStore().set_password(USER, "Passw0rd-long!")
    tc = TestClient(app_mod.app, base_url="https://testserver", follow_redirects=False)
    r = tc.post("/auth/login", data=csrf_form(tc, {"email": USER,
                                                   "password": "Passw0rd-long!"}))
    assert r.status_code == 302, r.text[:200]
    _refused(tc.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}))


def test_the_token_is_checked_before_the_body(mcp, token):
    peer = Peer()
    for content in ("{not json", "[]", "", "x" * (mcp.MAX_BODY_BYTES + 1)):
        _refused(peer.post(content))


def test_only_an_accepted_token_is_stamped_as_used(bot):
    Peer().legacy("ping")
    assert mcp_tokens.list_tokens()[0]["last_used_at"] is None
    result_of(bot.legacy("ping"))
    assert mcp_tokens.list_tokens()[0]["last_used_at"] is not None


def test_a_token_revoked_meanwhile_is_refused_at_once(bot):
    result_of(bot.legacy("ping"))
    mcp_tokens.revoke_token(mcp_tokens.list_tokens()[0]["id"], ADMIN)
    _refused(bot.legacy("ping"))


def test_an_unreadable_token_file_refuses_everything(bot, root):
    (root / "mcp_tokens.json").write_text("{broken", encoding="utf-8")
    _refused(bot.legacy("ping"))


def test_the_token_reaches_no_log_line(bot, token, logs):
    bot.legacy("initialize", {"protocolVersion": LEGACY})
    bot.legacy("tools/list")
    bot.legacy("tools/call", {"name": "list_chats", "arguments": {}})
    bot.legacy("tools/call", {"name": "nope", "arguments": {}})
    Peer(raw=token + "x").legacy("ping")
    Peer(raw="pdcmcp_" + "Q" * 43).legacy("ping")
    secret = token[len("pdcmcp_"):]
    digest = hashlib.sha256(token.encode()).hexdigest()
    bad = [ln for ln in logs if secret[:8] in ln or secret[-8:] in ln
           or digest[:12] in ln or "Q" * 8 in ln]
    assert bad == [], bad


# ===========================================================================
# the failed-token limiter
# ===========================================================================
WRONG = [("Authorization", f"Bearer {UNKNOWN}")]


def _fail(peer, n=1):
    return [peer.post(PING, headers=WRONG).status_code for _ in range(n)]


def test_twenty_failures_are_401_then_429_with_retry_after(mcp, clock):
    peer = Peer()
    assert _fail(peer, 20) == [401] * 20
    r = peer.post(PING, headers=WRONG)
    assert r.status_code == 429, (r.status_code, r.text[:200])
    assert 1 <= int(r.headers["retry-after"]) <= 600
    body = r.json()
    assert isinstance(body, dict) and "jsonrpc" not in body


def test_every_kind_of_failure_counts(mcp, clock, token):
    """A missing, unknown, malformed, revoked or repeated token: the answers
    are the same, so the counting is too."""
    row, revoked = mcp_tokens.create_token("Old bot", "per_user", ADMIN)
    mcp_tokens.revoke_token(row["id"], ADMIN)
    kinds = [[], WRONG, [("Authorization", "Basic x")],
             [("Authorization", f"Bearer {revoked}")],
             [("Authorization", f"Bearer {token}")] * 2]
    peer = Peer()
    for i in range(20):
        assert peer.post(PING, headers=kinds[i % 5]).status_code == 401, i
    for kind in kinds:
        assert peer.post(PING, headers=kind).status_code == 429


def test_a_valid_token_always_passes_from_a_limited_address(mcp, clock, token):
    """One proxy address may carry a working chatbot and a broken one."""
    host = "203.0.113.50"
    _fail(Peer(host), 25)
    good = Peer(host, raw=token)
    for _ in range(3):
        assert result_of(good.legacy("ping")) == {}
    # and its success lifts nothing for the broken one
    assert _fail(Peer(host)) == [429]


def test_addresses_are_counted_separately_and_the_port_does_not_matter(mcp, clock):
    _fail(Peer("203.0.113.1"), 20)
    assert _fail(Peer("203.0.113.2")) == [401]
    assert _fail(Peer("203.0.113.1", port=61234)) == [429]


def test_a_forwarded_header_does_not_choose_the_key(mcp, clock):
    peer = Peer()
    _fail(peer, 20)
    r = peer.post(PING, headers=WRONG + [("X-Forwarded-For", "198.51.100.9"),
                                         ("X-Real-IP", "198.51.100.9")])
    assert r.status_code == 429


def test_the_window_expires(mcp, clock):
    peer, start = Peer(), clock["t"]
    _fail(peer, 20)
    clock["t"] = start + 590
    r = peer.post(PING, headers=WRONG)
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 10
    clock["t"] = start + 599
    assert _fail(peer) == [429]
    clock["t"] = start + 601              # a refused attempt extends nothing
    assert _fail(peer, 20) == [401] * 20
    assert _fail(peer) == [429]


def test_failures_below_the_limit_are_forgotten_after_the_window(mcp, clock):
    peer = Peer()
    _fail(peer, 19)
    clock["t"] += 601
    assert _fail(peer, 20) == [401] * 20


def test_a_later_failure_does_not_prolong_the_earlier_ones(mcp, clock):
    """Twenty failures WITHIN the window, not twenty without a quiet window."""
    peer = Peer()
    _fail(peer, 10)
    clock["t"] += 500
    assert _fail(peer, 10) == [401] * 10
    assert _fail(peer) == [429]
    clock["t"] += 101                       # the first ten are out of the window
    assert _fail(peer) == [401]


def test_the_limits_are_read_per_request(mcp, clock, monkeypatch):
    monkeypatch.setattr(mcp, "FAILED_TOKEN_LIMIT", 2)
    monkeypatch.setattr(mcp, "FAILED_TOKEN_WINDOW_S", 30)
    peer = Peer()
    assert _fail(peer, 3) == [401, 401, 429]
    clock["t"] += 31
    assert _fail(peer) == [401]


def test_reset_limiter_forgets_everything(mcp, clock):
    peer = Peer()
    _fail(peer, 21)
    mcp.reset_limiter()
    assert _fail(peer) == [401]


def test_parallel_failures_are_counted_once_each(mcp, clock):
    """Threads of one peer: never more than the limit of 401s."""
    codes, lock = [], threading.Lock()

    def go():
        got = _fail(Peer(), 5)
        with lock:
            codes.extend(got)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(codes) == 40
    assert codes.count(401) == 20 and codes.count(429) == 20, codes


def _container_sizes(mod):
    """The module's own mutable containers, one attribute level deep."""
    out = {}
    for name, value in vars(mod).items():
        if name.startswith("__"):
            continue
        if isinstance(value, (dict, set, list)):
            out[name] = len(value)
        elif getattr(type(value), "__module__", "") == mod.__name__:
            for sub, inner in getattr(value, "__dict__", {}).items():
                if isinstance(inner, (dict, set, list)):
                    out[f"{name}.{sub}"] = len(inner)
    return out


def test_the_remembered_addresses_are_bounded(mcp, clock, monkeypatch):
    assert 100 <= mcp.FAILED_TOKEN_MAX_KEYS <= 100_000
    monkeypatch.setattr(mcp, "FAILED_TOKEN_MAX_KEYS", 5)
    before = _container_sizes(mcp)
    for i in range(40):
        assert _fail(Peer(f"198.51.100.{i}")) == [401]
    after = _container_sizes(mcp)
    grown = {k: n - before.get(k, 0) for k, n in after.items() if n > before.get(k, 0)}
    assert grown, "no address was remembered at all"
    assert max(grown.values()) <= 6, grown


def test_a_refusal_is_logged_once_per_address_and_window(mcp, clock, logs):
    host = "203.0.113.77"
    _fail(Peer(host), 25)
    assert len([ln for ln in logs if host in ln]) == 1, logs
    _fail(Peer("203.0.113.78"), 2)
    assert len([ln for ln in logs if "203.0.113.78" in ln]) == 1
    clock["t"] += 601
    _fail(Peer(host), 3)
    assert len([ln for ln in logs if host in ln]) == 2


def test_a_peer_value_cannot_forge_a_log_line(mcp, clock, logs):
    _fail(Peer("evil\n[sid=mcp] FORGED"))
    assert not [ln for ln in logs if "\n" in ln], logs


# ===========================================================================
# Origin
# ===========================================================================
BASE = "https://pdc.bank.example"


@pytest.mark.parametrize("base,origin,ok", [
    (BASE, BASE, True),
    (BASE + "/", BASE, True),
    (BASE + "/pdc/", BASE, True),
    ("https://PDC.Bank.example", BASE, True),
    ("https://pdc.bank.example:8443", "https://pdc.bank.example:8443", True),
    (BASE, "http://pdc.bank.example", False),
    (BASE, "https://pdc.bank.example:8443", False),
    (BASE, "https://pdc.bank.example.evil.example", False),
    (BASE, "https://evil.example", False),
    (BASE, "https://pdc.bank.example@evil.example", False),
    (BASE, "null", False),
    (BASE, "", False),
    (BASE, "https://evil.example/\xe9", False),
    ("", BASE, False),
    ("", "https://testserver", False),
    ("ftp://pdc.bank.example", "ftp://pdc.bank.example", False),
    ("not a url", "not a url", False),
], ids=range(17))
def test_the_origin_rule(bot, monkeypatch, base, origin, ok):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", base)
    r = bot.post(PING, headers=[(b"origin", origin.encode("latin-1"))])
    if ok:
        assert result_of(r) == {}
        return
    assert r.status_code == 403, (r.status_code, r.text[:200])
    body = r.json()
    assert isinstance(body, dict) and "jsonrpc" not in body
    assert "evil" not in r.text and "bank" not in r.text


def test_no_origin_is_accepted(bot, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", BASE)
    assert result_of(bot.legacy("ping")) == {}


def test_two_origins_are_refused(bot, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", BASE)
    r = bot.post(PING, headers=[("Origin", BASE), ("Origin", "https://evil.example")])
    assert r.status_code == 403


def test_the_origin_is_checked_before_the_token_and_counts_no_failure(mcp, clock):
    peer = Peer()
    for _ in range(25):
        r = peer.post(PING, headers=WRONG + [("Origin", "https://evil.example")])
        assert r.status_code == 403
    assert _fail(peer) == [401]


# ===========================================================================
# the body
# ===========================================================================
def _padded(size):
    head = '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"pad":"'
    tail = '"}}'
    return head + "x" * (size - len(head) - len(tail)) + tail


def test_a_body_of_exactly_the_cap_is_served(bot, mcp):
    assert result_of(bot.post(_padded(mcp.MAX_BODY_BYTES))) == {}


def test_one_byte_more_is_413(bot, mcp):
    r = bot.post(_padded(mcp.MAX_BODY_BYTES + 1))
    assert r.status_code == 413, (r.status_code, r.text[:200])
    assert isinstance(r.json(), dict)


def test_an_oversized_body_without_a_length_is_413(bot, mcp):
    data = _padded(mcp.MAX_BODY_BYTES + 1).encode()
    r = bot.post(data[i:i + 65536] for i in range(0, len(data), 65536))
    assert "content-length" not in r.request.headers, "the test must send no length"
    assert r.status_code == 413, (r.status_code, r.text[:200])


def test_a_chunked_body_under_the_cap_is_served(bot):
    r = bot.post(iter([PING[:10].encode(), b"", PING[10:].encode()]))
    assert result_of(r) == {}


def _scope(token, extra_headers=(), client=("203.0.113.7", 5)):
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "https", "path": "/mcp", "raw_path": b"/mcp",
            "root_path": "", "query_string": b"",
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                        (b"authorization", b"Bearer " + token.encode()), *extra_headers],
            "client": client, "server": ("testserver", 443), "state": {}}


def _asgi(token, chunks, extra_headers=(), **kw):
    """The real app, called as ASGI: (status, body, sizes of the chunks read)."""
    sent, read, feed = [], [], iter(chunks)

    async def receive():
        chunk = next(feed, None)
        if chunk is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        read.append(len(chunk))
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message):
        sent.append(message)

    asyncio.run(app_mod.app(_scope(token, extra_headers, **kw), receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, read


def test_the_reading_stops_at_the_cap(mcp, token):
    status, _b, read = _asgi(token, [b"x" * 65536] * 400)
    assert status == 413
    assert sum(read) <= mcp.MAX_BODY_BYTES + 65536, "the whole body was read"


def test_a_declared_oversized_length_is_413_unread(mcp, token):
    status, _b, read = _asgi(token, [PING.encode()],
                             [(b"content-length", b"%d" % (mcp.MAX_BODY_BYTES + 1))])
    assert status == 413 and read == []


@pytest.mark.parametrize("value", [b"abc", b"-5", b"", b"1e9", b"\xb2"])
def test_a_junk_length_is_never_a_crash(mcp, token, value):
    status, body, _r = _asgi(token, [PING.encode()], [(b"content-length", value)])
    assert status in (200, 400, 413), (status, body[:200])


def test_a_lying_small_length_is_still_counted(mcp, token):
    status, _b, read = _asgi(token, [b"x" * 65536] * 400, [(b"content-length", b"10")])
    assert status == 413 and sum(read) <= mcp.MAX_BODY_BYTES + 65536


def test_a_request_without_a_peer_address_is_served(mcp, token):
    status, body, _r = _asgi(token, [PING.encode()], client=None)
    assert status == 200, body
    status, _b, _r = _asgi("wrong", [PING.encode()], client=None)
    assert status == 401


def test_a_client_that_goes_away_mid_body_is_no_crash(mcp, token):
    sent = []
    messages = iter([{"type": "http.request", "body": b'{"json', "more_body": True}])

    async def receive():
        return next(messages, {"type": "http.disconnect"})

    async def send(m):
        sent.append(m)

    asyncio.run(app_mod.app(_scope(token), receive, send))       # must not raise
    assert not [m for m in sent if m.get("status", 0) >= 500], sent


# --- while the body is being sent --------------------------------------------
def _slow(token, receive, limit=15):
    """The real app with this `receive`: (status, headers, body)."""
    sent = []

    async def send(message):
        sent.append(message)

    async def go():
        await asyncio.wait_for(app_mod.app(_scope(
            token, [(b"x-pdc-user-email", USER.encode())]), receive, send), limit)

    asyncio.run(go())
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


CALL = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "list_chats", "arguments": {}}}).encode()


@pytest.fixture
def tool_runs(monkeypatch):
    ran = []
    monkeypatch.setattr(local_store.AuthStore, "list_active_chats",
                        lambda self, email: ran.append(email) or [])
    return ran


def test_a_token_revoked_while_the_body_is_sent_is_refused(mcp, token, tool_runs):
    tid = mcp_tokens.list_tokens()[0]["id"]
    parts = iter([CALL[:20], CALL[20:]])

    def receiver(revoke):
        async def receive():
            part = next(parts, None)
            if part is None:
                return {"type": "http.request", "body": b"", "more_body": False}
            if revoke and part == CALL[20:]:
                mcp_tokens.revoke_token(tid, ADMIN)
            return {"type": "http.request", "body": part, "more_body": True}
        return receive

    status, _h, body = _slow(token, receiver(False))
    assert status == 200 and tool_runs == [USER], body
    parts = iter([CALL[:20], CALL[20:]])
    status, headers, body = _slow(token, receiver(True))
    assert status == 401, body
    assert headers[b"www-authenticate"] == b"Bearer"
    assert b"jsonrpc" not in body
    assert body == _refused(Peer(raw=UNKNOWN).legacy("ping"))
    assert tool_runs == [USER], "the refused call ran"


def test_a_token_revoked_while_the_body_is_sent_never_reaches_the_parser(mcp, token):
    """Not even a protocol error: the 401 comes first."""
    tid = mcp_tokens.list_tokens()[0]["id"]
    parts = iter([b"{not json"])

    async def receive():
        part = next(parts, None)
        if part is None:
            mcp_tokens.revoke_token(tid, ADMIN)
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": part, "more_body": True}

    status, _h, body = _slow(token, receive)
    assert status == 401, body


@pytest.mark.parametrize("how", ["silent", "stalled", "dripping"])
def test_a_body_that_does_not_finish_in_time_is_408(mcp, token, tool_runs, monkeypatch, how):
    """The deadline is for the WHOLE body: one byte now and then extends
    nothing."""
    monkeypatch.setattr(mcp, "BODY_READ_TIMEOUT_S", 0.4)
    first = {"n": 0}

    async def receive():
        first["n"] += 1
        if how == "dripping":
            await asyncio.sleep(0.05)
            return {"type": "http.request", "body": b" ", "more_body": True}
        if how == "stalled" and first["n"] == 1:
            return {"type": "http.request", "body": CALL[:20], "more_body": True}
        await asyncio.sleep(3600)

    status, headers, body = _slow(token, receive)
    assert status == 408, body
    assert headers[b"content-type"].split(b";")[0] == b"application/json"
    out = json.loads(body)
    assert isinstance(out, dict) and "jsonrpc" not in out
    assert b"set-cookie" not in headers
    assert tool_runs == []


def test_a_slow_body_within_the_deadline_is_served(mcp, token, tool_runs, monkeypatch):
    monkeypatch.setattr(mcp, "BODY_READ_TIMEOUT_S", 8)
    parts = iter([CALL[:20], CALL[20:]])

    async def receive():
        await asyncio.sleep(0.2)
        part = next(parts, None)
        return {"type": "http.request", "body": part or b"", "more_body": part is not None}

    status, _h, body = _slow(token, receive)
    assert status == 200 and tool_runs == [USER], body


def test_the_deadline_never_covers_the_work_after_the_body(mcp, token, tool_runs, monkeypatch):
    """The wait of get_answer, for one, is longer than any body."""
    import time
    monkeypatch.setattr(mcp, "BODY_READ_TIMEOUT_S", 0.3)
    monkeypatch.setattr(local_store.AuthStore, "list_active_chats",
                        lambda self, email: time.sleep(0.8) or [])
    status, body, _r = _asgi(token, [CALL], [(b"x-pdc-user-email", USER.encode())])
    assert status == 200, body


@pytest.mark.parametrize("content", [
    "", "{", "{'jsonrpc': '2.0'}", "not json", b"\xff\xfe{}", b"\xff", '{"id": 1,}',
    "\x00", '{"jsonrpc":"2.0","id":' + "9" * 6000 + ',"method":"ping"}',
], ids=range(9))
def test_bad_json_is_a_parse_error(bot, content):
    error_of(bot.post(content), 400, PARSE, id=None)


@pytest.mark.parametrize("content", [
    "[" * 100_000, '{"a":' * 100_000,
    '{"jsonrpc":"2.0","id":1,"method":"ping","params":' + "[" * 100_000 + "]" * 100_000 + "}",
], ids=["arrays", "objects", "inside-params"])
def test_deeply_nested_json_is_never_a_crash(bot, content):
    r = bot.post(content)
    assert r.status_code == 400, (r.status_code, r.text[:200])
    assert r.json()["error"]["code"] in (PARSE, INVALID_REQUEST)


# ===========================================================================
# the JSON-RPC message
# ===========================================================================
@pytest.mark.parametrize("value", [
    [], [{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
    [{"jsonrpc": "2.0", "method": "notifications/initialized"}],
    "ping", 5, 1.5, None, True,
], ids=repr)
def test_only_one_object_per_post(bot, value):
    error_of(bot.send(value), 400, INVALID_REQUEST, id=None)


@pytest.mark.parametrize("id", [1, 0, -7, 2 ** 53, "abc", "", "1", "ქ"])
def test_the_id_is_echoed_with_its_type(bot, id):
    assert result_of(bot.legacy("ping", id=id), id=id) == {}
    assert result_of(bot.modern("tools/list", id=id), id=id)
    error_of(bot.legacy("nope", id=id), 200, NO_METHOD, id=id)


@pytest.mark.parametrize("id", [None, True, False, 1.5, [], [1], {}, {"a": 1}], ids=repr)
def test_any_other_id_is_invalid(bot, id):
    r = bot.send({"jsonrpc": "2.0", "id": id, "method": "ping"})
    error_of(r, 400, INVALID_REQUEST, id=None)


@pytest.mark.parametrize("patch", [
    {"jsonrpc": "1.0"}, {"jsonrpc": 2.0}, {"jsonrpc": None}, {"jsonrpc": ["2.0"]},
    {"method": None}, {"method": 5}, {"method": ["ping"]}, {"method": {"a": 1}},
], ids=repr)
def test_a_malformed_request_is_invalid(bot, patch):
    r = bot.send({"jsonrpc": "2.0", "id": 1, "method": "ping", **patch})
    assert r.status_code == 400, r.text[:200]
    assert r.json()["error"]["code"] == INVALID_REQUEST


@pytest.mark.parametrize("msg", [{"id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 1},
                                 {"jsonrpc": "2.0", "id": 1, "result": {}}], ids=repr)
def test_a_missing_member_is_invalid(bot, msg):
    r = bot.send(msg)
    assert r.status_code == 400 and r.json()["error"]["code"] == INVALID_REQUEST


@pytest.mark.parametrize("content", [
    '{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}',
    '{"jsonrpc":"2.0","id":1,"method":"\\ud800"}',
    '{"jsonrpc":"2.0","id":1,"method":"tools/call",'
    '"params":{"name":"\\ud800","arguments":{}}}',
], ids=["id", "method", "tool"])
def test_text_that_cannot_be_encoded_is_never_a_crash(bot, content):
    """A lone surrogate is valid JSON and cannot be written as UTF-8."""
    r = bot.post(content)
    assert r.status_code in (200, 400), (r.status_code, r.text[:200])
    assert r.json()["jsonrpc"] == "2.0"


# --- notifications ----------------------------------------------------------
def _accepted(r):
    assert r.status_code == 202, (r.status_code, r.text[:200])
    assert r.content == b""


@pytest.mark.parametrize("method", ["notifications/initialized", "notifications/cancelled",
                                    "notifications/whatever", "ping", "tools/list",
                                    "nope"])
def test_a_message_without_an_id_is_accepted_silently(bot, method):
    _accepted(bot.send({"jsonrpc": "2.0", "method": method}))
    _accepted(bot.send({"jsonrpc": "2.0", "method": method, "params": {"a": 1}}))


@pytest.mark.parametrize("version", ["1999-01-01", MODERN])    # MODERN: no _meta either
def test_a_notification_never_gets_an_error_body(bot, version):
    _accepted(bot.send({"jsonrpc": "2.0", "method": "notifications/x"},
                       headers=[("MCP-Protocol-Version", version)]))


def test_a_notification_runs_no_tool(bot, monkeypatch):
    ran = []
    monkeypatch.setattr(local_store.AuthStore, "list_active_chats",
                        lambda self, email: ran.append(email) or [])
    _accepted(bot.send({"jsonrpc": "2.0", "method": "tools/call",
                        "params": {"name": "list_chats", "arguments": {}}},
                       headers=[("X-PDC-User-Email", USER)]))
    assert ran == []


def test_a_notification_still_needs_a_token(mcp):
    _refused(Peer().send({"jsonrpc": "2.0", "method": "notifications/initialized"}))


# ===========================================================================
# legacy: 2025-11-25
# ===========================================================================
@pytest.mark.parametrize("asked", [LEGACY, MODERN, "2025-06-18", "2024-11-05",
                                   "1999-01-01", "", "x" * 5000], ids=range(7))
def test_initialize_negotiates(bot, asked):
    res = result_of(bot.legacy("initialize", {
        "protocolVersion": asked, "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"}}))
    assert set(res) == {"protocolVersion", "capabilities", "serverInfo", "instructions"}
    assert res["protocolVersion"] == LEGACY
    assert res["capabilities"] == {"tools": {}}
    assert res["serverInfo"] == {"name": "powerdatachat", "title": "PowerDataChat",
                                 "version": "abc1234"}


def test_the_version_is_dev_on_an_unstamped_build(bot, monkeypatch):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "")
    res = result_of(bot.legacy("initialize", {"protocolVersion": LEGACY}))
    assert res["serverInfo"]["version"] == "dev"
    disc = result_of(bot.modern("server/discover"))
    assert disc["_meta"][META_INFO]["version"] == "dev"


def test_the_instructions_name_the_call_order(bot):
    text = result_of(bot.legacy("initialize", {"protocolVersion": LEGACY}))["instructions"]
    order = [text.index(n) for n in ("list_chats", "ask", "get_answer")]
    assert order == sorted(order), text
    assert result_of(bot.modern("server/discover"))["instructions"] == text


@pytest.mark.parametrize("version", ["1999-01-01", MODERN, "2025-06-18", "junk"])
def test_initialize_ignores_the_version_header(bot, version):
    res = result_of(bot.legacy("initialize", {"protocolVersion": LEGACY},
                               headers=[("MCP-Protocol-Version", version)]))
    assert res["protocolVersion"] == LEGACY


def test_initialize_is_always_legacy(bot):
    res = result_of(bot.modern("initialize", {"protocolVersion": MODERN}))
    assert res["protocolVersion"] == LEGACY
    assert "resultType" not in res and "_meta" not in res


def test_ping_answers_an_empty_object(bot):
    assert result_of(bot.legacy("ping")) == {}
    assert result_of(bot.legacy("ping", {})) == {}


def test_legacy_tools_list_needs_no_initialize_first(bot):
    res = result_of(bot.legacy("tools/list"))
    assert set(res) == {"tools"}
    assert sorted(t["name"] for t in res["tools"]) == TOOL_NAMES


@pytest.mark.parametrize("method", ["nope", "", "resources/list", "prompts/list",
                                    "tools/unknown", "Tools/List", "ping ",
                                    "logging/setLevel", "completion/complete"])
def test_an_unknown_method_is_32601_with_http_200(bot, method):
    error_of(bot.legacy(method), 200, NO_METHOD)


def test_an_unknown_method_is_not_echoed_at_length(bot):
    err = error_of(bot.legacy("m" * 5000 + "\nX"), 200, NO_METHOD)
    assert "\n" not in err["message"]


@pytest.mark.parametrize("method", ["ping", "tools/list", "nope"])
@pytest.mark.parametrize("version", ["2025-06-18", "2024-11-05", "junk", "", "x" * 3000],
                         ids=["2025-06-18", "2024-11-05", "junk", "empty", "long"])
def test_a_legacy_request_with_another_version_header_is_400(bot, method, version):
    err = error_of(bot.legacy(method, headers=[("MCP-Protocol-Version", version)]),
                   400, BAD_VERSION)
    assert "x" * 100 not in json.dumps(err)


def test_the_legacy_version_header_is_accepted(bot):
    assert result_of(bot.legacy("ping", headers=[("MCP-Protocol-Version", LEGACY)])) == {}


def test_the_modern_request_headers_mean_nothing_to_a_legacy_request(bot):
    r = bot.legacy("ping", headers=[("Mcp-Method", "tools/call"), ("Mcp-Name", "x")])
    assert result_of(r) == {}


def test_a_meta_without_a_version_keeps_the_request_legacy(bot):
    assert result_of(bot.legacy("ping", {"_meta": {"progressToken": 1}})) == {}


# ===========================================================================
# modern: 2026-07-28
# ===========================================================================
def _modern_result(r, id=1):
    res = result_of(r, id)
    assert res["resultType"] == "complete"
    assert res["_meta"] == {META_INFO: {"name": "powerdatachat", "title": "PowerDataChat",
                                        "version": "abc1234"}}
    return res


def test_server_discover(bot):
    res = _modern_result(bot.modern("server/discover"))
    assert set(res) == {"resultType", "_meta", "supportedVersions", "capabilities",
                        "instructions", "ttlMs", "cacheScope"}
    assert res["supportedVersions"] == [MODERN, LEGACY]
    assert res["capabilities"] == {"tools": {}}
    assert res["ttlMs"] == 300000 and res["cacheScope"] == "private"


def test_modern_tools_list(bot):
    res = _modern_result(bot.modern("tools/list"))
    assert set(res) == {"resultType", "_meta", "tools", "ttlMs", "cacheScope"}
    assert res["ttlMs"] == 300000 and res["cacheScope"] == "private"
    assert res["tools"] == result_of(bot.legacy("tools/list"))["tools"]


def test_a_modern_tool_result_carries_the_result_type_too(bot):
    call = {"name": "list_chats", "arguments": {}}
    res = _modern_result(bot.modern("tools/call", call,
                                    headers={"X-PDC-User-Email": USER}))
    assert res["structuredContent"] == {"chats": []}
    legacy = result_of(bot.legacy("tools/call", call,
                                  headers=[("X-PDC-User-Email", USER)]))
    assert "resultType" not in legacy and "_meta" not in legacy
    assert legacy["structuredContent"] == {"chats": []}


def test_a_modern_refused_tool_call_carries_it_as_well(bot):
    res = _modern_result(bot.modern("tools/call", {"name": "list_chats", "arguments": {}}))
    assert res["isError"] is True               # no X-PDC-User-Email


@pytest.mark.parametrize("method", ["ping", "nope", "resources/list", "initialized"])
def test_a_modern_unknown_method_is_404(bot, method):
    error_of(bot.modern(method), 404, NO_METHOD)


# --- the era ----------------------------------------------------------------
def test_each_of_the_three_signs_makes_a_request_modern(bot):
    # 1. the method
    error_of(bot.legacy("server/discover"), 400, HEADER_MISMATCH)
    # 2. the body
    error_of(bot.legacy("tools/list", {"_meta": {META_VERSION: MODERN}}),
             400, HEADER_MISMATCH)
    # 3. the header, in any case
    for name in ("MCP-Protocol-Version", "mcp-protocol-version"):
        error_of(bot.legacy("tools/list", headers=[(name, MODERN)]), 400, BAD_PARAMS)


# --- the checks, in order ---------------------------------------------------
GOOD_META = {META_VERSION: MODERN, META_CAPS: {}}


def _call(bot, *, headers, meta="absent", method="tools/call"):
    params = {"name": "list_chats", "arguments": {}}
    if meta != "absent":
        params["_meta"] = meta
    return bot.send({"jsonrpc": "2.0", "id": "q1", "method": method, "params": params},
                    headers=list(headers.items()))


@pytest.mark.parametrize("meta", [{META_VERSION: "1999-01-01"}, {META_VERSION: MODERN},
                                  GOOD_META, {META_VERSION: 5}], ids=range(4))
def test_check_1_no_version_header(bot, meta):
    """First: the checks below would fail too."""
    error_of(_call(bot, headers={"Mcp-Method": "x"}, meta=meta),
             400, HEADER_MISMATCH, id="q1")


@pytest.mark.parametrize("meta", ["absent", None, [], "x", {}, {META_CAPS: {}},
                                  {META_VERSION: None}, {META_VERSION: 20260728},
                                  {META_VERSION: [MODERN]}], ids=range(9))
def test_check_2_no_version_in_the_body(bot, meta):
    error_of(_call(bot, headers={"MCP-Protocol-Version": MODERN, "Mcp-Method": "x"},
                   meta=meta), 400, BAD_PARAMS, id="q1")


def test_check_2_comes_before_check_3(bot):
    error_of(_call(bot, headers={"MCP-Protocol-Version": "1999-01-01"},
                   method="server/discover"), 400, BAD_PARAMS, id="q1")


@pytest.mark.parametrize("header,body", [("1999-01-01", MODERN), (MODERN, "1999-01-01"),
                                         (LEGACY, MODERN), (MODERN, LEGACY),
                                         (MODERN + "x", MODERN)])
def test_check_3_header_and_body_disagree(bot, header, body):
    error_of(_call(bot, headers={"MCP-Protocol-Version": header},
                   meta={META_VERSION: body}), 400, HEADER_MISMATCH, id="q1")


@pytest.mark.parametrize("version", ["1999-01-01", "2099-01-01", LEGACY, "junk"])
def test_check_4_an_unsupported_version(bot, version):
    err = error_of(_call(bot, headers={"MCP-Protocol-Version": version, "Mcp-Method": "x"},
                         meta={META_VERSION: version}), 400, BAD_VERSION, id="q1")
    assert err["message"] == "Unsupported protocol version"
    assert err["data"] == {"supported": [MODERN, LEGACY], "requested": version}


def test_check_4_the_requested_version_is_capped(bot):
    version = "v" * 4000
    err = error_of(_call(bot, headers={"MCP-Protocol-Version": version},
                         meta={META_VERSION: version}), 400, BAD_VERSION, id="q1")
    assert 1 <= len(err["data"]["requested"]) <= 80


@pytest.mark.parametrize("caps", ["absent", None, [], "x", 1, True], ids=repr)
def test_check_5_no_client_capabilities(bot, caps):
    meta = {META_VERSION: MODERN}
    if caps != "absent":
        meta[META_CAPS] = caps
    error_of(_call(bot, headers={"MCP-Protocol-Version": MODERN}, meta=meta),
             400, BAD_PARAMS, id="q1")


@pytest.mark.parametrize("value", [None, "tools/list", "", "TOOLS/CALL", "x" * 3000],
                         ids=["missing", "other", "empty", "upper", "long"])
def test_check_6_the_method_header(bot, value):
    headers = {"MCP-Protocol-Version": MODERN}        # Mcp-Name is missing too
    if value is not None:
        headers["Mcp-Method"] = value
    err = error_of(_call(bot, headers=headers, meta=GOOD_META),
                   400, HEADER_MISMATCH, id="q1")
    assert "x" * 100 not in err["message"]


@pytest.mark.parametrize("value", [None, "ask", "", "LIST_CHATS", "x" * 3000],
                         ids=["missing", "other", "empty", "upper", "long"])
def test_check_7_the_name_header(bot, value):
    headers = {"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/call"}
    if value is not None:
        headers["Mcp-Name"] = value
    err = error_of(_call(bot, headers=headers, meta=GOOD_META),
                   400, HEADER_MISMATCH, id="q1")
    assert "x" * 100 not in err["message"]


def test_the_name_header_is_needed_for_a_tool_call_only(bot):
    _modern_result(bot.modern("tools/list"))
    _modern_result(bot.modern("server/discover"))


def test_the_checks_come_before_the_method_lookup(bot):
    error_of(bot.modern("nope", drop=("Mcp-Method",)), 400, HEADER_MISMATCH)


@pytest.mark.parametrize("params", [None, [], "x", 5], ids=repr)
def test_params_that_are_not_an_object_never_crash_a_modern_request(bot, params):
    r = bot.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": params},
                 headers=[("MCP-Protocol-Version", MODERN), ("Mcp-Method", "tools/list")])
    error_of(r, 400, BAD_PARAMS)


def test_error_texts_never_repeat_a_hostile_value(bot):
    for r in (bot.legacy("ping", headers=[(b"mcp-protocol-version", b"<script>\xe9")]),
              bot.modern("tools/list", headers={"Mcp-Method": "<script>alert(1)</script>"})):
        assert r.status_code == 400
        assert "<script>" not in r.json()["error"]["message"]


# ===========================================================================
# tools/call: what is a protocol error
# ===========================================================================
@pytest.mark.parametrize("params", [
    {"name": "nope", "arguments": {}}, {"name": "", "arguments": {}},
    {"name": None, "arguments": {}}, {"name": 5, "arguments": {}},
    {"name": ["ask"], "arguments": {}}, {"name": "Ask", "arguments": {}},
    {"arguments": {}}, {}, [], "x", 5, None,
    {"name": "list_chats", "arguments": []}, {"name": "list_chats", "arguments": "x"},
    {"name": "ask", "arguments": 5},
], ids=repr)
def test_an_unknown_tool_or_malformed_params_is_32602(bot, params):
    """Before the identity: no X-PDC-User-Email is sent."""
    r = bot.send({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
    error_of(r, 200, BAD_PARAMS)


def test_a_modern_unknown_tool_is_32602_with_http_200(bot):
    error_of(bot.modern("tools/call", {"name": "nope", "arguments": {}}), 200, BAD_PARAMS)


def test_an_unexpected_failure_is_32603_and_says_nothing(bot, monkeypatch, logs):
    def boom(self, email):
        raise KeyError("column 'SECRET'\nFORGED")

    monkeypatch.setattr(local_store.AuthStore, "list_active_chats", boom)
    r = bot.legacy("tools/call", {"name": "list_chats", "arguments": {}},
                   headers=[("X-PDC-User-Email", USER)])
    err = error_of(r, 200, INTERNAL)
    assert err["message"] == "Internal error"
    assert "SECRET" not in r.text and "KeyError" not in r.text
    assert [ln for ln in logs if "KeyError" in ln], "must be logged (Article IV)"
    assert not [ln for ln in logs if "\n" in ln], logs


@pytest.mark.parametrize("method", ["initialize", "ping", "tools/list"])
def test_no_identity_is_needed_outside_tools_call(bot, method):
    result_of(bot.legacy(method, {"protocolVersion": LEGACY}))


def test_the_gate_for_a_pending_password_change_is_not_opened(mcp):
    assert "/mcp" not in app_mod._PASSWORD_CHANGE_OPEN_PATHS
    assert not "/mcp".startswith(tuple(app_mod._PASSWORD_CHANGE_OPEN_PREFIXES))


# ===========================================================================
# off the event loop
# ===========================================================================
def test_the_blocking_work_runs_off_the_event_loop(mcp, token, monkeypatch):
    """Reading the token file, resolving the caller and reading the stores are
    file operations: on the loop they would stall every other user."""
    threads = {}

    def spy(owner, name):
        real = getattr(owner, name)

        def wrapped(*a, **k):
            threads.setdefault(name, set()).add(threading.get_ident())
            return real(*a, **k)
        monkeypatch.setattr(owner, name, wrapped)

    for owner, name in ((mcp_tokens, "verify"), (mcp_tokens, "touch"),
                        (mcp_tokens, "resolve_caller"),
                        (local_store.AuthStore, "list_active_chats")):
        spy(owner, name)

    async def go():
        transport = httpx.ASGITransport(app=app_mod.app, client=("203.0.113.7", 1))
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
            r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": "list_chats", "arguments": {}}},
                             headers={"Authorization": f"Bearer {token}",
                                      "X-PDC-User-Email": USER})
            return threading.get_ident(), r

    loop_thread, r = asyncio.run(go())
    assert result_of(r)["structuredContent"] == {"chats": []}
    assert set(threads) == {"verify", "touch", "resolve_caller", "list_active_chats"}
    on_loop = [k for k, v in threads.items() if loop_thread in v]
    assert on_loop == [], f"on the event loop: {on_loop}"
