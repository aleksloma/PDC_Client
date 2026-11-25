"""MCP stage 5 — the admin token routes (`routes.mcp.admin_router`).

* `GET  /api/admin/mcp/tokens` -> `{"tokens": [public rows], "endpoint": ...}`
* `POST /api/admin/mcp/tokens` `{name, mode, account?}` ->
  `{"token": row, "raw_token": "...", "endpoint": ...}`, `Cache-Control:
  no-store`; the raw token is in this one response and nowhere else
* `POST /api/admin/mcp/tokens/{token_id}/revoke` -> `{"token": row}`

Guard: `routes.admin_data._require_admin`, before the body is read — 401 without
a session, 403 plus exactly one `admin.denied` row for anyone who is not an
admin; the bootstrap admin and promoted admins pass (as for Single sign-on).
Bad input is the store's own sentence with a 400, an unreadable or unwritable
token file one fixed 500 text, an unknown token id a 404. Revoking drops the
token's jobs and holds on /mcp at once. Audited: `mcp_token.create` with
{name, mode, account} and `mcp_token.revoke` with {name} — never a token or a
hash, in the audit, the logs or a list. The token file is never touched on the
event loop.

Offline: DATA_ROOT is tmp_path. Most tests run on a bare app with the two MCP
routers; the last ones on the real app. `admin_router` is looked up inside the
fixture, so a missing one fails the tests, not the collection.
"""
import asyncio
import hashlib
import importlib
import json
import logging
import re
import threading

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import brain_client
import db_sources
import local_store
import logger_utils
import mcp_jobs
import mcp_tokens
from settings import settings
from tests.conftest import csrf_form

ADMIN = "ladmin"                        # the bootstrap local admin
PROMOTED = "boss@corp.example"          # permission "admin"
POWER = "power@corp.example"
USER = "user@corp.example"
BOT = "bot@corp.example"
PW = "secret-pw"

LIST = "/api/admin/mcp/tokens"
RAW_RE = re.compile(r"pdcmcp_[A-Za-z0-9_-]{43}")
ROW_KEYS = {"id", "name", "mode", "account", "created_at", "created_by",
            "last_used_at", "revoked_at", "revoked_by", "active"}
UNKNOWN_ID = "0123456789abcdef"
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "1"}}}


def _revoke_path(token_id):
    return f"{LIST}/{token_id}/revoke"


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
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    auth = local_store.AuthStore()
    for email in (ADMIN, PROMOTED, POWER, USER, BOT):
        auth.ensure_user(email)
    auth.set_role(ADMIN, "admin")
    auth.set_role(PROMOTED, "admin")
    auth.set_role(POWER, "power")
    mod = importlib.import_module("routes.mcp")
    mod.reset_limiter()
    mcp_jobs.reset()
    yield tmp_path
    mod.reset_limiter()
    mcp_jobs.reset()


@pytest.fixture
def bare(root):
    """The two MCP routers on a bare app; nobody is signed in."""
    mod = importlib.import_module("routes.mcp")
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(mod.router)
    app.include_router(mod.admin_router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str, must_change: bool = False):
        request.session.clear()
        request.session["email"] = email
        if must_change:
            request.session["must_change_password"] = True
        return {"ok": True}

    return TestClient(app)


@pytest.fixture
def client(bare):
    """Signed in as the bootstrap admin."""
    _as(bare, ADMIN)
    return bare


def _as(client, email, **params):
    client.cookies.clear()
    r = client.post(f"/_login/{email}", params=params)
    assert r.status_code == 200, r.text


def _create(client, **over):
    body = {"name": "Helpdesk bot", "mode": "per_user", **over}
    return client.post(LIST, json=body)


def _created(client, **over):
    r = _create(client, **over)
    assert r.status_code == 200, r.text
    return r.json()


def _file(root):
    return root / "mcp_tokens.json"


def _audit(action=None):
    rows = db_sources.read_audit_tail(500)
    return [r for r in rows if action is None or r["action"] == action]


def _denied():
    return len(_audit("admin.denied"))


def _assert_no_secret(text, raw):
    assert raw not in text
    assert raw[len("pdcmcp_"):] not in text
    assert hashlib.sha256(raw.encode()).hexdigest() not in text
    assert not RAW_RE.search(text)


def _error_sentence(r):
    body = r.json()
    assert set(body) == {"error"}, body
    text = body["error"]
    assert isinstance(text, str) and " " in text and "Traceback" not in text, text
    return text


# ===========================================================================
# the router and its guard
# ===========================================================================
def _admin_paths():
    mod = importlib.import_module("routes.mcp")
    return sorted((m, r.path) for r in mod.admin_router.routes
                  for m in r.methods - {"HEAD", "OPTIONS"})


ROUTES = [("GET", LIST), ("POST", LIST), ("POST", _revoke_path(UNKNOWN_ID))]


def test_the_admin_router_holds_exactly_the_three_routes(root):
    assert _admin_paths() == sorted([
        ("GET", LIST), ("POST", LIST), ("POST", LIST + "/{token_id}/revoke")])


def test_the_mcp_endpoint_stays_on_its_own_router(root):
    mod = importlib.import_module("routes.mcp")
    assert [(sorted(r.methods), r.path) for r in mod.router.routes] == [(["POST"], "/mcp")]


@pytest.mark.parametrize("method,path", ROUTES)
def test_without_a_session_every_route_is_401(bare, root, method, path):
    r = bare.request(method, path, json={"name": "x", "mode": "per_user"})
    assert r.status_code == 401
    assert r.json() == {"error": "Not authenticated"}
    assert not _file(root).exists()


@pytest.mark.parametrize("email", [USER, POWER])
@pytest.mark.parametrize("method,path", ROUTES)
def test_a_user_who_is_not_an_admin_is_refused_and_audited_once(bare, root, email, method, path):
    _as(bare, email)
    r = bare.request(method, path, json={"name": "x", "mode": "per_user"})
    assert r.status_code == 403
    assert r.json() == {"error": "Administrator access required"}
    rows = _audit()
    assert [(a["action"], a["actor"], a["target"], a["ok"]) for a in rows] == \
        [("admin.denied", email, path, False)]
    assert not _file(root).exists()


@pytest.mark.parametrize("method,path", ROUTES)
def test_a_pending_password_change_is_refused(bare, root, method, path):
    _as(bare, ADMIN, must_change=True)
    r = bare.request(method, path, json={"name": "x", "mode": "per_user"})
    assert r.status_code == 403
    assert r.json() == {"error": "Password change required"}
    assert not _file(root).exists()


def test_a_demoted_admin_is_refused_on_the_next_request(bare, root):
    _as(bare, PROMOTED)
    assert bare.get(LIST).status_code == 200
    local_store.AuthStore().set_role(PROMOTED, "user")
    assert bare.get(LIST).status_code == 403
    assert _create(bare).status_code == 403
    assert not _file(root).exists()


@pytest.mark.parametrize("email", [ADMIN, PROMOTED])
def test_the_bootstrap_admin_and_a_promoted_admin_may_do_everything(bare, email):
    _as(bare, email)
    made = _created(bare)
    assert made["token"]["created_by"] == email
    assert [t["id"] for t in bare.get(LIST).json()["tokens"]] == [made["token"]["id"]]
    r = bare.post(_revoke_path(made["token"]["id"]))
    assert r.status_code == 200
    assert r.json()["token"]["revoked_by"] == email
    assert _denied() == 0


def _asgi_refusal(app, method, path, cookie):
    """(status, number of body reads) of a request whose body may not be read."""
    reads, sent = [], []

    async def receive():
        reads.append(1)
        return {"type": "http.request", "body": b'{"name": "x"}', "more_body": False}

    async def send(message):
        sent.append(message)

    headers = [(b"host", b"testserver"), (b"content-type", b"application/json")]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "root_path": "", "query_string": b"", "headers": headers,
             "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, len(reads)


@pytest.mark.parametrize("method,path", ROUTES)
def test_the_guard_runs_before_the_body_is_read(bare, method, path):
    assert _asgi_refusal(bare.app, method, path, "") == (401, 0)
    _as(bare, USER)
    cookie = "; ".join(f"{c.name}={c.value}" for c in bare.cookies.jar)
    assert _asgi_refusal(bare.app, method, path, cookie) == (403, 0)


# ===========================================================================
# list
# ===========================================================================
def test_an_empty_list_creates_no_file(client, root):
    r = client.get(LIST)
    assert r.status_code == 200
    assert r.json() == {"tokens": [], "endpoint": ""}
    assert not _file(root).exists()


def test_the_list_holds_public_rows_newest_first(client, monkeypatch):
    now = {"t": 1_700_000_000.0}
    monkeypatch.setattr(mcp_tokens, "_clock", lambda: now["t"])
    first = _created(client, name="First")
    now["t"] += 60
    second = _created(client, name="Second", mode="shared_account", account=BOT)
    r = client.get(LIST)
    rows = r.json()["tokens"]
    assert [t["name"] for t in rows] == ["Second", "First"]
    assert all(set(t) == ROW_KEYS for t in rows)
    assert rows == [second["token"], first["token"]]
    assert (rows[0]["account"], rows[1]["account"]) == (BOT, None)
    for made in (first, second):
        _assert_no_secret(r.text, made["raw_token"])
    assert "token_hash" not in r.text and "account_created_at" not in r.text


def test_a_revoked_token_stays_in_the_list(client):
    made = _created(client)
    client.post(_revoke_path(made["token"]["id"]))
    rows = client.get(LIST).json()["tokens"]
    assert [(t["id"], t["active"], t["revoked_by"]) for t in rows] == \
        [(made["token"]["id"], False, ADMIN)]


@pytest.mark.parametrize("base,expected", [
    ("", ""),
    ("   ", ""),
    ("pdc.corp.example", ""),
    ("ftp://pdc.corp.example", ""),
    ("https://pdc.corp.example", "https://pdc.corp.example/mcp"),
    ("https://pdc.corp.example/", "https://pdc.corp.example/mcp"),
    ("http://10.0.0.5:8091", "http://10.0.0.5:8091/mcp"),
    ("https://corp.example/pdc/", "https://corp.example/pdc/mcp"),
])
def test_the_endpoint_comes_from_public_base_url_only(client, monkeypatch, base, expected):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", base)
    spoof = {"Host": "evil.example", "X-Forwarded-Host": "evil.example"}
    assert client.get(LIST, headers=spoof).json()["endpoint"] == expected
    r = client.post(LIST, json={"name": "Bot", "mode": "per_user"}, headers=spoof)
    assert r.status_code == 200
    assert r.json()["endpoint"] == expected


def test_an_unreadable_token_file_does_not_break_the_list(client, root):
    _file(root).write_text("{broken", encoding="utf-8")
    r = client.get(LIST)
    assert r.status_code < 500 or set(r.json()) == {"error"}
    assert "broken" not in r.text
    assert _file(root).read_text(encoding="utf-8") == "{broken"


# ===========================================================================
# create
# ===========================================================================
def test_create_answers_the_raw_token_once_and_is_not_cached(client, root, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://pdc.corp.example")
    r = _create(client, name="  Helpdesk bot  ")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {"token", "raw_token", "endpoint"}
    assert body["endpoint"] == "https://pdc.corp.example/mcp"
    row, raw = body["token"], body["raw_token"]
    assert RAW_RE.fullmatch(raw)
    assert set(row) == ROW_KEYS
    assert (row["name"], row["mode"], row["account"], row["created_by"], row["active"]) == \
        ("Helpdesk bot", "per_user", None, ADMIN, True)
    assert raw not in json.dumps(row)
    assert (mcp_tokens.verify(raw) or {}).get("id") == row["id"]
    stored = _file(root).read_text(encoding="utf-8")
    assert raw not in stored


def test_the_created_token_works_on_the_endpoint(client):
    raw = _created(client)["raw_token"]
    r = client.post("/mcp", json=INITIALIZE, headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "powerdatachat"


def test_a_shared_account_token_is_bound_to_the_account(client):
    row = _created(client, mode="shared_account", account=f" {BOT.title()} ")["token"]
    assert (row["mode"], row["account"]) == ("shared_account", BOT)


def test_the_account_of_a_per_user_token_is_ignored(client):
    assert _created(client, account=BOT)["token"]["account"] is None


BAD_BODIES = [
    ({}, "name"),
    ({"mode": "per_user"}, "name"),
    ({"name": "", "mode": "per_user"}, "name"),
    ({"name": "   ", "mode": "per_user"}, "name"),
    ({"name": 7, "mode": "per_user"}, "name"),
    ({"name": ["x"], "mode": "per_user"}, "name"),
    ({"name": "x" * 61, "mode": "per_user"}, "name"),
    ({"name": "two\nlines", "mode": "per_user"}, "name"),
    ({"name": "bot\u202e", "mode": "per_user"}, "name"),
    ({"name": "bot", "mode": "admin"}, "mode"),
    ({"name": "bot"}, "mode"),
    ({"name": "bot", "mode": ["per_user"]}, "mode"),
    ({"name": "bot", "mode": "shared_account"}, "account"),
    ({"name": "bot", "mode": "shared_account", "account": ""}, "account"),
    ({"name": "bot", "mode": "shared_account", "account": "not-an-address"}, "account"),
    ({"name": "bot", "mode": "shared_account", "account": "nobody@corp.example"}, "account"),
    ({"name": "bot", "mode": "shared_account", "account": {"a": 1}}, "account"),
    ({"name": "bot", "mode": "shared_account", "account": ADMIN}, "account"),
]


@pytest.mark.parametrize("body,about", BAD_BODIES)
def test_bad_input_is_a_400_with_the_stores_sentence(client, root, body, about):
    r = client.post(LIST, json=body)
    assert r.status_code == 400, r.text
    assert about in _error_sentence(r).lower()
    assert "raw_token" not in r.text
    assert not _file(root).exists()
    assert _audit("mcp_token.create") == []


def test_a_bootstrap_admin_with_an_address_cannot_be_bound(client, root, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", PROMOTED)
    r = _create(client, mode="shared_account", account=PROMOTED)
    assert r.status_code == 400
    assert "local admin" in _error_sentence(r)
    assert not _file(root).exists()


@pytest.mark.parametrize("content", [b"", b"{not json", b"[]", b'"x"', b"null", b"\xff\xfe"])
def test_a_body_that_is_not_an_object_is_a_400(client, root, content):
    r = client.post(LIST, content=content, headers={"Content-Type": "application/json"})
    assert r.status_code == 400, r.text
    _error_sentence(r)
    assert not _file(root).exists()


def test_the_name_of_an_active_token_is_refused_and_of_a_revoked_one_is_free(client):
    made = _created(client)
    r = _create(client, name="HELPDESK BOT")
    assert r.status_code == 400
    assert "already exists" in _error_sentence(r)
    client.post(_revoke_path(made["token"]["id"]))
    _created(client, name="HELPDESK BOT")


def test_create_is_audited_without_the_token(client, root):
    per_user = _created(client, name="Helpdesk bot")
    shared = _created(client, name="Kiosk", mode="shared_account", account=BOT)
    rows = list(reversed(_audit("mcp_token.create")))
    assert [r["actor"] for r in rows] == [ADMIN, ADMIN]
    assert all(r["ok"] is True for r in rows)
    assert rows[0]["detail"] == {"name": "Helpdesk bot", "mode": "per_user", "account": None}
    assert rows[1]["detail"] == {"name": "Kiosk", "mode": "shared_account", "account": BOT}
    text = (root / "admin_audit.jsonl").read_text(encoding="utf-8")
    for made in (per_user, shared):
        _assert_no_secret(text, made["raw_token"])


# ===========================================================================
# revoke
# ===========================================================================
def test_revoke_answers_the_row_and_is_audited(client, root):
    made = _created(client)
    r = client.post(_revoke_path(made["token"]["id"]))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"token"}
    row = body["token"]
    assert set(row) == ROW_KEYS
    assert (row["id"], row["active"], row["revoked_by"]) == (made["token"]["id"], False, ADMIN)
    assert row["revoked_at"]
    rows = _audit("mcp_token.revoke")
    assert [(a["actor"], a["ok"], a["detail"]) for a in rows] == \
        [(ADMIN, True, {"name": "Helpdesk bot"})]
    _assert_no_secret(r.text, made["raw_token"])
    _assert_no_secret((root / "admin_audit.jsonl").read_text(encoding="utf-8"),
                      made["raw_token"])


def test_a_body_is_not_needed_and_is_ignored(client):
    one, two = _created(client, name="One"), _created(client, name="Two")
    r = client.post(_revoke_path(one["token"]["id"]),
                    json={"token_id": two["token"]["id"], "id": two["token"]["id"]})
    assert r.json()["token"]["id"] == one["token"]["id"]
    assert mcp_tokens.verify(two["raw_token"]) is not None


def test_a_revoked_token_is_refused_at_once_and_others_still_pass(client):
    one, two = _created(client, name="One"), _created(client, name="Two")

    def status(raw):
        return client.post("/mcp", json=INITIALIZE,
                           headers={"Authorization": f"Bearer {raw}"}).status_code

    assert (status(one["raw_token"]), status(two["raw_token"])) == (200, 200)
    client.post(_revoke_path(one["token"]["id"]))
    assert (status(one["raw_token"]), status(two["raw_token"])) == (401, 200)


def test_a_second_revoke_keeps_the_first_stamps(client, monkeypatch):
    now = {"t": 1_700_000_000.0}
    monkeypatch.setattr(mcp_tokens, "_clock", lambda: now["t"])
    token_id = _created(client)["token"]["id"]
    first = client.post(_revoke_path(token_id)).json()
    now["t"] += 3600
    _as(client, PROMOTED)
    again = client.post(_revoke_path(token_id))
    assert again.status_code == 200
    assert again.json() == first


def test_revoke_drops_the_tokens_jobs_after_the_token_is_off(client, monkeypatch):
    one, two = _created(client, name="One"), _created(client, name="Two")
    tid = one["token"]["id"]
    for n, token_id in enumerate((tid, two["token"]["id"])):
        job = mcp_jobs.Job(id=f"{n:032x}", token_id=token_id, email=USER, chat_id="c1",
                           state="done", content=[])
        mcp_jobs._JOBS[job.id] = job
    seen = []
    real = mcp_jobs.drop_token

    def spy(token_id):
        seen.append((token_id, mcp_tokens.verify(one["raw_token"])))
        return real(token_id)

    monkeypatch.setattr(mcp_jobs, "drop_token", spy)
    client.post(_revoke_path(tid))
    # A job started between the drop and the revocation would survive it.
    assert seen == [(tid, None)]
    assert [j.token_id for j in mcp_jobs._JOBS.values()] == [two["token"]["id"]]


def test_an_unknown_id_is_a_404_and_changes_nothing(client, root, monkeypatch):
    made = _created(client)
    before = _file(root).read_bytes()
    dropped = []
    monkeypatch.setattr(mcp_jobs, "drop_token", dropped.append)
    r = client.post(_revoke_path(UNKNOWN_ID))
    assert r.status_code == 404
    _error_sentence(r)
    assert _file(root).read_bytes() == before
    assert _audit("mcp_token.revoke") == []
    assert dropped == []
    assert mcp_tokens.verify(made["raw_token"]) is not None


def test_revoking_with_no_token_file_is_a_404(client, root):
    assert client.post(_revoke_path(UNKNOWN_ID)).status_code == 404
    assert not _file(root).exists()


MALFORMED = ["x", "ABCDEF0123456789", "0123456789abcde", "0123456789abcdef0",
             "%0A0123456789abcdef", "0123456789abcdef%0A", "%2e%2e", "..%5c..%5cx",
             "%E1%83%90%E1%83%91", "%00", "a" * 5000, "%F0%9F%98%80",
             "MCP_CALL%0D%0Aforged", "null", "*"]


@pytest.mark.parametrize("raw_id", MALFORMED)
def test_a_malformed_id_is_a_404_and_forges_no_log_line(client, root, logs, raw_id):
    _created(client)
    before = _file(root).read_bytes()
    r = client.post(_revoke_path(raw_id))
    assert r.status_code == 404, r.text
    assert len(r.text) < 300, "the id is echoed"
    assert _file(root).read_bytes() == before
    assert _audit("mcp_token.revoke") == []
    assert not [l for l in logs if "\n" in l or "\r" in l or "\x00" in l]


# ===========================================================================
# a token file that cannot be read or written
# ===========================================================================
def test_an_unreadable_file_is_one_fixed_500_and_is_left_alone(client, root, logs):
    _file(root).write_text('{"tokens": "MARKER-IN-FILE', encoding="utf-8")
    before = _file(root).read_bytes()
    texts = set()
    for r in (_create(client), client.post(_revoke_path(UNKNOWN_ID))):
        assert r.status_code == 500
        texts.add(_error_sentence(r))
        assert "MARKER" not in r.text and "mcp_tokens" not in r.text
    assert _file(root).read_bytes() == before
    assert _audit("mcp_token.create") == [] and _audit("mcp_token.revoke") == []
    assert not [l for l in logs if "MARKER" in l]


@pytest.mark.parametrize("func,path,body", [
    ("create_token", LIST, {"name": "bot", "mode": "per_user"}),
    ("revoke_token", _revoke_path(UNKNOWN_ID), {})])
def test_the_500_text_never_carries_the_exception(client, monkeypatch, func, path, body):
    texts = []
    for message in (r"C:\secret\path", "another reason"):
        def boom(*a, _message=message, **k):
            raise mcp_tokens.TokenStoreError(_message)
        monkeypatch.setattr(mcp_tokens, func, boom)
        r = client.post(path, json=body)
        assert r.status_code == 500
        texts.append(_error_sentence(r))
        assert message not in r.text
    assert texts[0] == texts[1]


def test_a_failed_revoke_drops_no_jobs(client, monkeypatch):
    def boom(*a, **k):
        raise mcp_tokens.TokenStoreError("x")
    dropped = []
    monkeypatch.setattr(mcp_tokens, "revoke_token", boom)
    monkeypatch.setattr(mcp_jobs, "drop_token", dropped.append)
    assert client.post(_revoke_path(UNKNOWN_ID)).status_code == 500
    assert dropped == []


# ===========================================================================
# logs, the event loop
# ===========================================================================
def test_no_log_line_carries_a_token_or_a_hash(client, logs):
    made = [_created(client, name="One"),
            _created(client, name="Two", mode="shared_account", account=BOT)]
    client.get(LIST)
    _create(client, name="One")
    client.post(_revoke_path(made[0]["token"]["id"]))
    assert logs, "nothing was captured"
    for m in made:
        _assert_no_secret("\n".join(logs), m["raw_token"])


def test_a_refused_request_logs_nothing_of_its_body(bare, logs):
    _as(bare, USER)
    _create(bare, name="BODY-MARKER")
    assert not [l for l in logs if "BODY-MARKER" in l]


def test_the_token_store_is_never_used_on_the_event_loop(client, monkeypatch):
    on_loop = {}

    def watch(name):
        real = getattr(mcp_tokens, name)

        def wrapper(*a, **k):
            try:
                asyncio.get_running_loop()
                on_loop[name] = True
            except RuntimeError:
                on_loop.setdefault(name, False)
            return real(*a, **k)
        monkeypatch.setattr(mcp_tokens, name, wrapper)

    for name in ("create_token", "list_tokens", "revoke_token"):
        watch(name)
    token_id = _created(client)["token"]["id"]
    client.get(LIST)
    client.post(_revoke_path(token_id))
    assert on_loop == {"create_token": False, "list_tokens": False, "revoke_token": False}


def test_two_creations_at_once_keep_both_tokens(client):
    results = []

    def make(name):
        results.append(_create(client, name=name).status_code)

    threads = [threading.Thread(target=make, args=(f"bot {i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results == [200] * 4
    assert len(client.get(LIST).json()["tokens"]) == 4


# ===========================================================================
# the real app
# ===========================================================================
@pytest.fixture
def real(root, monkeypatch):
    import app as app_mod
    import routes.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    auth = local_store.AuthStore()
    for email in (PROMOTED, POWER):
        auth.set_password(email, PW)
    tc = TestClient(app_mod.app, base_url="https://testserver")

    def login(email):
        tc.cookies.clear()
        r = tc.post("/auth/login", data=csrf_form(tc, {"email": email, "password": PW}),
                    follow_redirects=False)
        assert r.status_code == 302, r.text[:300]
        return tc
    return login


def test_the_real_app_serves_the_three_routes(real):
    tc = real(PROMOTED)
    made = tc.post(LIST, json={"name": "Bot", "mode": "per_user"})
    assert made.status_code == 200, made.text
    assert made.headers["cache-control"] == "no-store"
    raw, token_id = made.json()["raw_token"], made.json()["token"]["id"]
    auth = {"Authorization": f"Bearer {raw}"}
    assert tc.post("/mcp", json=INITIALIZE, headers=auth).status_code == 200
    assert [t["id"] for t in tc.get(LIST).json()["tokens"]] == [token_id]
    assert tc.post(_revoke_path(token_id), json={}).status_code == 200
    assert tc.post("/mcp", json=INITIALIZE, headers=auth).status_code == 401


def test_a_request_that_is_not_json_never_reaches_the_routes(real, root):
    tc = real(PROMOTED)
    for path in (LIST, _revoke_path(UNKNOWN_ID)):
        for ctype in ("text/plain", "application/x-www-form-urlencoded"):
            r = tc.post(path, content='{"name": "x", "mode": "per_user"}',
                        headers={"Content-Type": ctype})
            assert r.status_code == 415, path
    assert not _file(root).exists()


def test_a_power_user_is_refused_on_the_real_app(real):
    tc = real(POWER)
    assert tc.get(LIST).status_code == 403
    assert tc.post(LIST, json={"name": "x", "mode": "per_user"}).status_code == 403
    assert tc.post(_revoke_path(UNKNOWN_ID), json={}).status_code == 403
    assert _denied() == 3


def test_an_mcp_token_opens_no_admin_route(real):
    tc = real(PROMOTED)
    raw = tc.post(LIST, json={"name": "Bot", "mode": "per_user"}).json()["raw_token"]
    tc.cookies.clear()
    auth = {"Authorization": f"Bearer {raw}", "X-PDC-User-Email": PROMOTED}
    assert tc.get(LIST, headers=auth).status_code == 401
    assert tc.post(LIST, json={"name": "x", "mode": "per_user"}, headers=auth).status_code == 401
