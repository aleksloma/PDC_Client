"""MCP stage 6 — `tools/mcp_test_client.py`, the command line test client.

    main(argv=None) -> int
    _post(url, headers, body_bytes, timeout) -> (status, headers, bytes)

`_post` is the only thing that touches the network and the seam of this file:
the tests put the real app (`POST /mcp`, the real tokens, jobs and answers,
only the planner pipeline scripted) or a scripted server behind it. The last
part drives the real `_post` against an `http.server` on 127.0.0.1.

Pinned readings of the brief:
* the options stand before the command; `--token` wins over PDC_MCP_TOKEN, and
  without either nothing is sent (exit 2);
* `_post` returns every HTTP status, it does not raise for one; it may raise
  any of the network exceptions, `main` turns them into the sentence;
* exit 2: the server could not be used (a refusing status, no connection, no
  usable answer) or the command line is wrong. Exit 1: the server answered and
  the answer is a refusal, a failure, or still running after `--max-wait`;
* a wrong command line may leave through argparse's SystemExit(2); the token
  is in no output even then (typed at the wrong place, for example);
* "one plain sentence" is ONE output line, without the server's body;
* redirects are not followed: the token would go to another address;
* waiting uses the module's `time` (`import time`), and there is a pause
  between two polls the server answered at once;
* the default `--timeout` is above 60 s, the longest MCP_ANSWER_WAIT_SECONDS;
* a chart whose name is taken is saved under another name.

Offline: DATA_ROOT is tmp_path; no brain call, no Plotly render.
"""
import ast
import base64
import http.client
import http.server
import importlib
import importlib.util
import io
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pandas as pd
import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

import app as app_mod
import local_store
import mcp_tokens
from settings import settings

ROOT = Path(__file__).resolve().parent.parent
CLIENT = ROOT / "tools" / "mcp_test_client.py"
DEFAULT_URL = "http://127.0.0.1:8091/mcp"
LEGACY, MODERN = "2025-11-25", "2026-07-28"
PROTOCOLS = [LEGACY, MODERN]
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"

ADMIN = "ladmin"
OWNER = "owner@acme.com"
BOT = "bot@acme.com"
CHAT = "c_mcpclient000001"
BOTCHAT = "c_mcpclient000002"
BASE = "https://pdc.bank.example"
QUESTION = "How did sales go?"
GEORGIAN = "გაყიდვები გაიზარდა"
GENERIC = "Something went wrong while processing your request."
TOKEN = "pdcmcp_" + "Zq7" * 14 + "x"

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def b64(raw):
    return base64.b64encode(raw).decode("ascii")


# ===========================================================================
# the client
# ===========================================================================
@pytest.fixture
def mod(monkeypatch):
    """A fresh copy of the client, without a token in the environment."""
    assert CLIENT.is_file(), "tools/mcp_test_client.py does not exist"
    monkeypatch.delenv("PDC_MCP_TOKEN", raising=False)
    spec = importlib.util.spec_from_file_location("mcp_test_client_under_test", CLIENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Run:
    """The outcome of one `main` call."""

    def __init__(self, rc, out, err):
        self.rc, self.out, self.err = rc, out, err
        self.text = out + err
        self.lines = [ln for ln in self.text.splitlines() if ln.strip()]

    def __repr__(self):
        return f"rc={self.rc}\n--- out\n{self.out}\n--- err\n{self.err}"

    def steps(self, mark):
        return [ln for ln in self.lines if ln.lstrip().startswith(f"[{mark}]")]


def _call(module, argv):
    try:
        return module.main([str(a) for a in argv])
    except SystemExit as e:
        return 0 if e.code is None else e.code


@pytest.fixture
def run(mod, capsys):
    def go(*argv, token=TOKEN):
        capsys.readouterr()
        rc = _call(mod, (["--token", token] if token else []) + list(argv))
        got = Run(rc, *capsys.readouterr())
        assert isinstance(rc, int) and not isinstance(rc, bool), got
        assert "Traceback" not in got.text, got
        assert "pdcmcp_" not in got.text, "a token was printed"
        return got
    return go


def _no_network(monkeypatch):
    """Only `_post` may reach the network."""
    def refuse(*a, **k):
        pytest.fail("the client used the network outside _post")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", refuse)


class Seam:
    """`_post` in front of `serve(url, headers, body, timeout)`; keeps what
    was sent."""

    def __init__(self, serve):
        self.serve, self.sent = serve, []

    def __call__(self, url, headers, body_bytes, timeout):
        assert isinstance(body_bytes, bytes)
        self.sent.append({"url": url, "headers": httpx.Headers(dict(headers)),
                          "body": json.loads(body_bytes.decode("utf-8")), "timeout": timeout})
        return self.serve(url, dict(headers), body_bytes, timeout)

    @property
    def requests(self):
        """The requests, without the notifications."""
        return [s for s in self.sent if "id" in s["body"]]

    def methods(self):
        return [s["body"]["method"] for s in self.requests]

    def tool_calls(self, name):
        return [s["body"]["params"].get("arguments") for s in self.requests
                if s["body"]["method"] == "tools/call" and s["body"]["params"]["name"] == name]


# ===========================================================================
# the real endpoint behind the seam
# ===========================================================================
class Pipeline:
    """The scripted `run_chat_multi_plot`."""

    def __init__(self):
        self.calls, self.raises = [], None
        self.result = {"text": "Sales are up.", "image_base64": None, "table": None,
                       "code": "RESULT = 1", "usage": {}}
        self.gate = threading.Event()
        self.gate.set()

    def __call__(self, **kw):
        self.calls.append(kw)
        gate = self.gate

        def events():
            assert gate.wait(20)
            if self.raises is not None:
                raise self.raises
            yield {"single_response": True, "result": dict(self.result)}
        return events()


def _make_chat(chat_id, owner, title):
    store = local_store.ChatDataStore(chat_id)
    pd.DataFrame({"region": ["North", "South"], "amount": [1, 2]}).to_csv(
        store.files_dir / "sales.csv", index=False)
    meta = store.read_meta()
    meta.update({"owner": owner, "title": title,
                 "files": [{"file_name": "sales.csv", "file_description": "sales",
                            "schema": {"fields": {"region": {}, "amount": {}}}}],
                 "sharing": {"shared_with": []}})
    store.write_meta(meta)
    local_store.AuthStore().record_active_chat(owner, chat_id, title, ["sales.csv"])


@pytest.fixture
def world(tmp_path, monkeypatch):
    chat_mod = importlib.import_module("routes.chat")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(settings, "DATA_ROOT", str(data))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", ADMIN)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", BASE)
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    monkeypatch.setattr(settings, "EXECUTOR_NETWORK_CIDR", "")
    monkeypatch.setattr(settings, "MCP_ANSWER_WAIT_SECONDS", 5.0)
    local_store._DATAFRAME_CACHE.invalidate()
    for email in (OWNER, BOT):
        local_store.AuthStore().ensure_user(email)
    _make_chat(CHAT, OWNER, "Sales")
    _make_chat(BOTCHAT, BOT, "Bot chat")
    chat_mod._INFLIGHT_CODES.clear()
    chat_mod._INFLIGHT_SQL.clear()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    yield work
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def pipeline(world, monkeypatch):
    chat_mod = importlib.import_module("routes.chat")
    endpoint = importlib.import_module("routes.mcp")
    jobs = importlib.import_module("mcp_jobs")
    report = importlib.import_module("routes.report")
    p = Pipeline()
    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot", p)
    monkeypatch.setattr(chat_mod, "_start_title_generation", lambda *a, **k: None)
    monkeypatch.setattr(report, "_plotly_html_to_png",
                        lambda *a, **k: pytest.fail("a real chart render"))
    endpoint.reset_limiter()
    jobs.reset()
    yield p
    p.gate.set()
    for t in threading.enumerate():
        if t.name.startswith("mcp-job"):
            t.join(15)
    jobs.reset()
    endpoint.reset_limiter()


@pytest.fixture
def app_seam(mod, pipeline, monkeypatch):
    http_client = TestClient(app_mod.app, base_url="https://testserver")

    def serve(url, headers, body, timeout):
        r = http_client.post("/mcp", headers=headers, content=body)
        return r.status_code, r.headers, r.content

    seam = Seam(serve)
    _no_network(monkeypatch)
    monkeypatch.setattr(mod, "_post", seam)
    # The server's log goes to the same streams as the client's output.
    logging.disable(logging.CRITICAL)
    yield seam
    logging.disable(logging.NOTSET)


@pytest.fixture
def per_user(world):
    return mcp_tokens.create_token("Helpdesk bot", "per_user", ADMIN, None)[1]


@pytest.fixture
def shared(world):
    return mcp_tokens.create_token("Shared bot", "shared_account", ADMIN, BOT)[1]


@pytest.fixture(params=["per_user", "shared_account"])
def caller(request, per_user, shared):
    """(token, options, the chat of the acting user)."""
    if request.param == "per_user":
        return per_user, ["--email", OWNER], CHAT
    return shared, [], BOTCHAT


def saved(folder):
    """Every file under `folder`, by its relative name."""
    return {p.relative_to(folder).as_posix(): p.read_bytes()
            for p in Path(folder).rglob("*") if p.is_file()}


TABLE = {"columns": ["region", "amount"], "total_rows": 2,
         "rows": [{"region": "North", "amount": 1}, {"region": "South", "amount": 2}]}


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_chats_prints_the_chats_of_the_acting_user(run, app_seam, caller, protocol):
    token, options, chat = caller
    got = run("--protocol", protocol, *options, "chats", token=token)
    assert got.rc == 0, got
    assert chat in got.out and "sales.csv" in got.out, got
    assert ("Sales" if chat == CHAT else "Bot chat") in got.out
    assert (BOTCHAT if chat == CHAT else CHAT) not in got.text
    assert app_seam.tool_calls("list_chats") in ([{}], [None])


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_ask_prints_the_answer_and_saves_the_chart(run, app_seam, pipeline, caller,
                                                  protocol, tmp_path):
    token, options, chat = caller
    pipeline.result.update(text="Sales are up.", image_base64=b64(PNG), table=TABLE)
    out = tmp_path / "new" / "answers"
    got = run("--protocol", protocol, *options, "ask", chat, QUESTION, "--out", out,
              token=token)
    assert got.rc == 0, got
    assert "Sales are up." in got.out
    assert "| region | amount |" in got.out and "| South | 2 |" in got.out, got
    assert re.search(re.escape(BASE) + r"/c/cv_[0-9a-f]{16}", got.out), got
    assert saved(out) == {"chart_1.png": PNG}, got
    assert "chart_1.png" in got.out
    assert b64(PNG) not in got.text, "the image data is not text"
    assert [c["question"] for c in pipeline.calls] == [QUESTION]
    email = OWNER if chat == CHAT else BOT
    assert pipeline.calls[0]["user_email"] == email


def test_the_default_protocol_is_the_older_one(run, app_seam, per_user):
    assert run("--email", OWNER, "chats", token=per_user).rc == 0
    for s in app_seam.requests:
        if s["body"]["method"] != "initialize":
            assert s["headers"]["mcp-protocol-version"] == LEGACY
            assert "_meta" not in (s["body"].get("params") or {})


def test_the_modern_requests_carry_the_headers_and_the_meta(run, app_seam, pipeline,
                                                            per_user):
    got = run("--protocol", MODERN, "--email", OWNER, "selftest", token=per_user)
    assert got.rc == 0, got
    assert "initialize" not in app_seam.methods() and "ping" not in app_seam.methods()
    for s in app_seam.sent:
        body, headers = s["body"], s["headers"]
        assert headers["mcp-protocol-version"] == MODERN
        assert headers["mcp-method"] == body["method"]
        meta = body["params"]["_meta"]
        assert meta[META_VERSION] == MODERN and isinstance(meta[META_CAPS], dict)
        if body["method"] == "tools/call":
            assert headers["mcp-name"] == body["params"]["name"]


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_every_request_is_framed_right(run, app_seam, caller, protocol):
    token, options, _chat = caller
    assert run("--protocol", protocol, *options, "selftest", token=token).rc == 0
    ids = [s["body"]["id"] for s in app_seam.requests]
    assert all(isinstance(i, (int, str)) and not isinstance(i, bool) for i in ids)
    assert len(set(map(str, ids))) == len(ids), "one id was used twice"
    for s in app_seam.sent:
        assert s["url"] == DEFAULT_URL
        assert s["body"]["jsonrpc"] == "2.0"
        headers = s["headers"]
        assert headers["authorization"] == f"Bearer {token}"
        assert headers["content-type"].split(";")[0].strip() == "application/json"
        # Streamable HTTP: a client accepts both
        assert "application/json" in headers["accept"]
        assert "text/event-stream" in headers["accept"]
        assert headers.get("x-pdc-user-email") == (OWNER if options else None)
        assert "origin" not in headers
        assert token not in json.dumps(s["body"])


def test_a_running_answer_is_polled_until_it_is_done(run, app_seam, pipeline, per_user,
                                                     monkeypatch):
    monkeypatch.setattr(settings, "MCP_ANSWER_WAIT_SECONDS", 0.05)
    pipeline.gate.clear()
    states, real = [], app_seam.serve

    def serve(url, headers, body, timeout):
        status, hdrs, raw = real(url, headers, body, timeout)
        if b'"get_answer"' in body:
            states.append(json.loads(raw)["result"]["structuredContent"]["state"])
            if len(states) == 2:
                pipeline.gate.set()
        return status, hdrs, raw

    app_seam.serve = serve
    got = run("--email", OWNER, "ask", CHAT, QUESTION, token=per_user)
    assert got.rc == 0 and "Sales are up." in got.out, got
    assert states[:2] == ["running", "running"] and states[-1] == "done"
    assert len({c["job_id"] for c in app_seam.tool_calls("get_answer")}) == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_a_failed_answer_prints_its_message_and_exits_1(run, app_seam, pipeline, per_user,
                                                        protocol, world):
    pipeline.raises = RuntimeError("column SECRET is missing")
    got = run("--protocol", protocol, "--email", OWNER, "ask", CHAT, QUESTION,
              token=per_user)
    assert got.rc == 1, got
    assert GENERIC in got.text and "SECRET" not in got.text, got
    assert saved(world) == {}


def test_a_refused_call_prints_the_servers_sentence_and_exits_1(run, app_seam, pipeline,
                                                                per_user):
    got = run("--email", OWNER, "ask", BOTCHAT, QUESTION, token=per_user)
    assert got.rc == 1 and "Chat not found." in got.text, got
    assert app_seam.tool_calls("get_answer") == [], "there is no job to wait for"
    got = run("chats", token=per_user)
    assert got.rc == 1 and "X-PDC-User-Email" in got.text, got
    assert pipeline.calls == []


def test_a_georgian_question_arrives_and_its_answer_is_printed(run, app_seam, pipeline,
                                                              per_user):
    pipeline.result["text"] = GEORGIAN
    got = run("--email", OWNER, "ask", CHAT, "როგორ გაიზარდა გაყიდვები?", token=per_user)
    assert got.rc == 0, got
    assert pipeline.calls[0]["question"] == "როგორ გაიზარდა გაყიდვები?"


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_the_selftest_passes_every_step(run, app_seam, pipeline, caller, protocol, tmp_path):
    token, options, chat = caller
    pipeline.result.update(image_base64=b64(PNG))
    out = tmp_path / "selftest"
    got = run("--protocol", protocol, *options, "selftest", "--out", out, token=token)
    assert got.rc == 0, got
    passed = "\n".join(got.steps("PASS"))
    first = "initialize" if protocol == LEGACY else "server/discover"
    for step in (first, "tools/list", "list_chats"):
        assert step in passed, got
    assert len(got.steps("PASS")) >= 4 and got.steps("FAIL") == [], got
    methods = app_seam.methods()
    assert methods[:4] == [first, "tools/list", "tools/call", "tools/call"], methods
    assert app_seam.tool_calls("ask")[0]["chat_id"] == chat
    assert len(pipeline.calls) == 1, "one question"
    assert pipeline.calls[0]["question"].strip()
    assert saved(out) == {"chart_1.png": PNG}


def test_the_initialize_request_is_complete(run, app_seam, per_user):
    run("--email", OWNER, "selftest", token=per_user)
    [params] = [s["body"]["params"] for s in app_seam.requests
                if s["body"]["method"] == "initialize"]
    assert params["protocolVersion"] == LEGACY
    assert isinstance(params["capabilities"], dict)
    assert params["clientInfo"]["name"] and params["clientInfo"]["version"]
    # A notification, when one is sent, has no id and comes after initialize.
    for s in app_seam.sent:
        if "id" not in s["body"]:
            assert s["body"]["method"] == "notifications/initialized"
            assert app_seam.sent.index(s) == 1


def test_the_selftest_takes_a_chat_and_a_question(run, app_seam, pipeline, per_user):
    other = "c_mcpclient000003"
    _make_chat(other, OWNER, "Older")
    listed = []
    for chat in (CHAT, other):
        got = run("--email", OWNER, "selftest", "--chat", chat, "--question", f"Q {chat}",
                  token=per_user)
        assert got.rc == 0, got
        listed.append(app_seam.tool_calls("ask")[-1])
    assert listed == [{"chat_id": c, "question": f"Q {c}"} for c in (CHAT, other)]


def test_without_a_chat_the_selftest_fails_clearly(run, app_seam, pipeline, world):
    local_store.AuthStore().ensure_user("empty@acme.com")
    token = mcp_tokens.create_token("Empty", "shared_account", ADMIN, "empty@acme.com")[1]
    got = run("selftest", token=token)
    assert got.rc == 1, got
    assert any("chat" in ln.lower() for ln in got.steps("FAIL")), got
    passed = "\n".join(got.steps("PASS"))
    assert "initialize" in passed and "tools/list" in passed
    assert app_seam.tool_calls("ask") == [] and pipeline.calls == []


def test_a_failed_question_fails_the_selftest(run, app_seam, pipeline, per_user):
    pipeline.raises = RuntimeError("boom")
    got = run("--email", OWNER, "selftest", token=per_user)
    assert got.rc == 1 and len(got.steps("FAIL")) == 1, got
    assert GENERIC in got.text, "the reason is printed"
    assert len(got.steps("PASS")) >= 3


def test_an_unknown_chat_fails_the_selftest(run, app_seam, pipeline, per_user):
    got = run("--email", OWNER, "selftest", "--chat", BOTCHAT, token=per_user)
    assert got.rc == 1 and got.steps("FAIL"), got
    assert pipeline.calls == []


def test_a_missing_address_fails_the_selftest_at_the_tools(run, app_seam, per_user):
    got = run("selftest", token=per_user)
    assert got.rc == 1, got
    assert "X-PDC-User-Email" in got.text
    assert any("list_chats" in ln for ln in got.steps("FAIL")), got
    assert "tools/list" in "\n".join(got.steps("PASS"))


@pytest.mark.parametrize("command", [["chats"], ["ask", CHAT, QUESTION], ["selftest"]])
@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_an_unknown_token_is_refused(run, app_seam, pipeline, command, protocol):
    got = run("--protocol", protocol, "--email", OWNER, *command)
    assert got.rc in ((1, 2) if command == ["selftest"] else (2,)), got
    assert "token" in got.text.lower() and got.steps("PASS") == [], got
    assert pipeline.calls == []


def test_the_limiter_of_the_real_endpoint(run, app_seam, pipeline):
    for _ in range(20):
        run("chats")
    got = run("chats")
    assert got.rc == 2 and len(got.lines) == 1, got
    assert re.search(r"\d", got.text), "how long to wait"


def test_an_oversized_question_at_the_real_endpoint(run, app_seam, pipeline, per_user):
    got = run("--email", OWNER, "ask", CHAT, "x" * (1024 * 1024), token=per_user)
    assert got.rc == 2 and len(got.lines) == 1 and len(got.text) < 500, got
    assert "large" in got.text.lower() or "413" in got.text
    assert pipeline.calls == []


# ===========================================================================
# a scripted server behind the seam
# ===========================================================================
class Script:
    """A small MCP server. `answers`: the results of get_answer in turn, the
    last one again and again. `override[key]`: a whole result, or a function
    of the body giving (status, headers, bytes); key = method or tool name."""

    def __init__(self):
        self.chats = [{"chat_id": CHAT, "title": "Sales", "tables": ["sales.csv"]}]
        self.answers = [self.answer("done", {"type": "text", "text": "Sales are up."})]
        self.override, self.polls = {}, 0
        self.every = None           # (status, headers, bytes) for every request

    @staticmethod
    def answer(state, *content):
        return {"content": list(content), "structuredContent": {"job_id": "j", "state": state}}

    def result(self, body):
        method, params = body["method"], body.get("params") or {}
        key = params.get("name") if method == "tools/call" else method
        if key in self.override and not callable(self.override[key]):
            return self.override[key]
        if key == "initialize":
            return {"protocolVersion": LEGACY, "capabilities": {"tools": {}},
                    "serverInfo": {"name": "powerdatachat", "version": "dev"}}
        if key == "server/discover":
            return {"supportedVersions": [MODERN, LEGACY], "capabilities": {"tools": {}}}
        if key == "tools/list":
            return {"tools": [{"name": n, "inputSchema": {"type": "object"}}
                              for n in ("list_chats", "ask", "get_answer")]}
        if key == "list_chats":
            return {"content": [], "structuredContent": {"chats": self.chats}}
        if key == "ask":
            return {"content": [],
                    "structuredContent": {"job_id": "0" * 32, "state": "running"}}
        if key == "get_answer":
            self.polls += 1
            assert self.polls < 5000, "the client polls without a pause or an end"
            return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return {}

    def __call__(self, url, headers, body, timeout):
        if self.every is not None:
            if isinstance(self.every, Exception):
                raise self.every
            return self.every
        message = json.loads(body)
        if "id" not in message:
            return 202, {}, b""
        params = message.get("params") or {}
        key = params.get("name") if message["method"] == "tools/call" else message["method"]
        if callable(self.override.get(key)):
            return self.override[key](message)
        reply = {"jsonrpc": "2.0", "id": message["id"], "result": self.result(message)}
        return 200, httpx.Headers({"Content-Type": "application/json"}), \
            json.dumps(reply).encode("utf-8")


class Clock:
    """`time` as the client sees it: sleeping takes no time."""

    def __init__(self):
        self.now, self.slept = 1_800_000_000.0, []

    def time(self):
        return self.now

    monotonic = perf_counter = time

    def sleep(self, seconds):
        assert seconds >= 0
        self.slept.append(seconds)
        self.now += seconds

    def localtime(self, t=None):
        return time.localtime(self.now if t is None else t)

    def gmtime(self, t=None):
        return time.gmtime(self.now if t is None else t)

    def strftime(self, fmt, t=None):
        return time.strftime(fmt, t or self.localtime())

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def script(mod, monkeypatch, tmp_path):
    s = Script()
    s.seam = Seam(s)
    s.clock = Clock()
    assert getattr(mod, "time", None) is time, "the client must `import time`"
    monkeypatch.setattr(mod, "time", s.clock)
    monkeypatch.setattr(mod, "_post", s.seam)
    _no_network(monkeypatch)
    s.work = tmp_path / "work"
    s.work.mkdir()
    monkeypatch.chdir(s.work)
    return s


def image(raw, mime="image/png"):
    return {"type": "image", "data": b64(raw) if isinstance(raw, bytes) else raw,
            "mimeType": mime}


def text_item(value):
    return {"type": "text", "text": value}


# --- options ---------------------------------------------------------------
def test_the_url_option_and_the_timeout(run, script):
    assert run("--url", "http://pdc.local:9/mcp", "--timeout", "7", "chats").rc == 0
    assert {(s["url"], s["timeout"]) for s in script.seam.sent} == \
        {("http://pdc.local:9/mcp", 7)}
    script.seam.sent.clear()
    assert run("chats").rc == 0
    assert {s["url"] for s in script.seam.sent} == {DEFAULT_URL}
    # a get_answer may be held for 60 s by the server
    assert all(s["timeout"] > 60 for s in script.seam.sent)


def test_the_token_from_the_environment(run, script, monkeypatch):
    monkeypatch.setenv("PDC_MCP_TOKEN", "pdcmcp_" + "e" * 43)
    assert run("chats", token=None).rc == 0
    assert script.seam.sent[-1]["headers"]["authorization"] == "Bearer pdcmcp_" + "e" * 43
    assert run("chats").rc == 0
    assert script.seam.sent[-1]["headers"]["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("value", [None, "", "  "])
def test_without_a_token_nothing_is_sent(run, script, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("PDC_MCP_TOKEN", value)
    got = run("chats", token=None)
    assert got.rc == 2 and script.seam.sent == [], got
    assert "token" in got.text.lower() and "PDC_MCP_TOKEN" in got.text


@pytest.mark.parametrize("argv", [
    [], ["nonsense"], ["ask"], ["ask", CHAT], ["--protocol", "2024-11-05", "chats"],
    ["--timeout", "0", "chats"], ["--timeout", "-1", "chats"],
    ["--timeout", "nan", "chats"], ["--timeout", "abc", "chats"],
    ["--url", "file:///etc/passwd", "chats"], ["--url", "ftp://127.0.0.1/mcp", "chats"],
    ["--url", "127.0.0.1:8091/mcp", "chats"], ["--url", "", "chats"],
    ["--url", "http://", "chats"]], ids=" ".join)
def test_a_wrong_command_line_is_exit_2_and_sends_nothing(run, script, argv):
    got = run(*argv)
    assert got.rc == 2 and got.lines and script.seam.sent == [], got


@pytest.mark.parametrize("argv", [["selftest", "--token", TOKEN], ["chats", TOKEN], [TOKEN],
                                  ["--tok" + "en" + "x", TOKEN, "chats"],
                                  ["--protocol", TOKEN, "chats"], ["--timeout", TOKEN, "chats"],
                                  ["--url", TOKEN, "chats"]],
                         ids=["after-the-command", "as-an-argument", "as-the-command",
                              "unknown-option", "as-protocol", "as-timeout", "as-url"])
def test_a_token_at_the_wrong_place_is_not_printed(run, script, argv):
    """`run` checks the output; whether the command works is free."""
    run(*argv, token=None)


def test_help_does_not_print_the_token(run, script, monkeypatch):
    monkeypatch.setenv("PDC_MCP_TOKEN", TOKEN)
    got = run("--help", token=None)
    assert got.rc == 0, got
    for word in ("--url", "--token", "--email", "--protocol", "--timeout", "chats", "ask",
                 "selftest", MODERN, LEGACY):
        assert word in got.out, word


# --- waiting ---------------------------------------------------------------
def test_there_is_a_pause_between_two_polls(run, script):
    script.answers = [script.answer("running", text_item("{}"))] * 5 + script.answers
    assert run("ask", CHAT, QUESTION).rc == 0
    assert script.polls == 6
    assert len(script.clock.slept) >= 5 and min(script.clock.slept[:5]) > 0


def test_max_wait_ends_the_waiting(run, script):
    script.answers = [script.answer("running", text_item("{}"))]
    started = script.clock.now
    got = run("ask", CHAT, QUESTION, "--max-wait", "30")
    assert got.rc == 1, got
    assert "running" in got.text.lower(), got
    assert "Sales are up." not in got.text
    assert 25 <= script.clock.now - started <= 40
    assert script.polls <= 100, "the server is asked too often"


def test_the_waiting_ends_by_itself_too(run, script):
    """Without --max-wait a job that never ends does not hang the client."""
    script.answers = [script.answer("running", text_item("{}"))]
    started = script.clock.now
    assert run("ask", CHAT, QUESTION).rc == 1
    assert 120 <= script.clock.now - started <= 3600


# --- printing --------------------------------------------------------------
def test_an_empty_list_of_chats_is_said(run, script):
    script.chats = []
    got = run("chats")
    assert got.rc == 0 and "no chats" in got.text.lower(), got


def test_every_text_is_printed(run, script):
    script.answers = [script.answer(
        "done", text_item("First part."), image(PNG), text_item("| a | b |\n| --- | --- |"),
        text_item(f"Open in PowerDataChat: {BASE}/c/cv_0123456789abcdef"))]
    got = run("ask", CHAT, QUESTION)
    assert got.rc == 0, got
    for piece in ("First part.", "| a | b |", "| --- | --- |",
                  f"{BASE}/c/cv_0123456789abcdef"):
        assert piece in got.out, piece


class _Stream(io.TextIOWrapper):
    def __init__(self, encoding):
        super().__init__(io.BytesIO(), encoding=encoding, errors="strict",
                         write_through=True)

    def text(self):
        self.flush()
        return self.buffer.getvalue().decode(self.encoding, "replace")


@pytest.mark.parametrize("encoding", ["cp1252", "cp437", "ascii", "utf-8"])
@pytest.mark.parametrize("state", ["done", "failed"])
def test_a_console_that_cannot_show_the_answer(mod, script, monkeypatch, encoding, state):
    answer = script.answer(state, text_item(f"Before {GEORGIAN} \ud83d after"))
    if state == "failed":
        answer["isError"] = True
    script.answers = [answer]
    script.chats = [{"chat_id": CHAT, "title": GEORGIAN, "tables": [GEORGIAN + ".csv"]}]
    out, err = _Stream(encoding), _Stream(encoding)
    with monkeypatch.context() as m:
        m.setattr(sys, "stdout", out)
        m.setattr(sys, "stderr", err)
        codes = [_call(mod, ["--token", TOKEN, "chats"]),
                 _call(mod, ["--token", TOKEN, "ask", CHAT, GEORGIAN])]
    assert codes == [0, 0 if state == "done" else 1]
    shown = out.text() + err.text()
    assert CHAT in shown and "Before " in shown and " after" in shown
    assert (GEORGIAN in shown) == (encoding == "utf-8")


def test_a_stream_that_cannot_be_reconfigured(mod, script, monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    with monkeypatch.context() as m:
        m.setattr(sys, "stdout", out)
        m.setattr(sys, "stderr", err)
        code = _call(mod, ["--token", TOKEN, "ask", CHAT, QUESTION])
    assert code == 0 and "Sales are up." in out.getvalue()


# --- files -----------------------------------------------------------------
def test_the_charts_are_numbered_in_order(run, script, tmp_path):
    pngs = [PNG + bytes([i]) for i in range(3)]
    script.answers = [script.answer("done", text_item("Three."), *map(image, pngs))]
    out = tmp_path / "out"
    assert run("ask", CHAT, QUESTION, "--out", out).rc == 0
    assert saved(out) == {f"chart_{i + 1}.png": p for i, p in enumerate(pngs)}
    assert saved(script.work) == {}


def test_the_default_folder_is_new_every_time(run, script):
    """Two answers in the same second must not share a file."""
    script.answers = [script.answer("done", image(PNG))]
    for _ in range(2):
        got = run("ask", CHAT, QUESTION)
        assert got.rc == 0 and "chart_1.png" in got.out, got
    files = saved(script.work)
    assert list(files.values()) == [PNG, PNG], files
    for name in files:
        assert re.fullmatch(r"mcp_answer_\d[0-9A-Za-z_-]*/chart_\w+\.png", name), name


def test_an_existing_file_is_never_overwritten(run, script, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "chart_1.png").write_bytes(b"mine")
    (out / "notes.txt").write_bytes(b"mine too")
    script.answers = [script.answer("done", image(PNG))]
    for saved_count in (1, 2):
        got = run("ask", CHAT, QUESTION, "--out", out)
        assert got.rc == 0, got
        files = saved(out)
        assert files.pop("chart_1.png") == b"mine"
        assert files.pop("notes.txt") == b"mine too"
        assert list(files.values()) == [PNG] * saved_count, files
        assert all(name.endswith(".png") and name in got.out or saved_count == 2
                   for name in files)


@pytest.mark.parametrize("data", [b"GIF89a" + b"\0" * 20, b"<svg/>", b"", PNG[1:], PNG[:4],
                                  "not base64 !!", "", None, 5, ["x"], "ა"])
def test_only_a_png_is_saved(run, script, tmp_path, data):
    script.answers = [script.answer("done", text_item("The text."), image(data),
                                    image(PNG))]
    out = tmp_path / "out"
    got = run("ask", CHAT, QUESTION, "--out", out)
    assert "The text." in got.out, got
    assert list(saved(out).values()) == [PNG], "the good one is still saved"
    assert saved(script.work) == {}


def test_nothing_of_an_answer_chooses_a_file_name(run, script, tmp_path):
    item = {**image(PNG), "name": "../../evil.png", "filename": "evil.png",
            "_meta": {"name": "evil.png"}}
    script.answers = [script.answer("done", item)]
    assert run("ask", CHAT, QUESTION, "--out", tmp_path / "out").rc == 0
    assert "evil" not in "".join(saved(tmp_path))


def test_an_out_folder_that_is_a_file(run, script, tmp_path):
    out = tmp_path / "out"
    out.write_bytes(b"mine")
    script.answers = [script.answer("done", text_item("The text."), image(PNG))]
    got = run("ask", CHAT, QUESTION, "--out", out)
    assert got.rc in (1, 2) and got.lines, got
    assert out.read_bytes() == b"mine"
    # an answer that was paid for is not lost
    assert "The text." in got.out or script.seam.tool_calls("ask") == [], got


# --- a server that cannot be used ------------------------------------------
STATUSES = [400, 401, 403, 404, 405, 408, 413, 415, 429, 500, 502, 503]


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("body", [b'{"error": "REASON %s"}' % TOKEN.encode(),
                                  b"<html><body>\n<h1>PROXY PAGE</h1>\n</body></html>", b"",
                                  b"\xff\xfe\x00"],
                         ids=["json", "html", "empty", "junk"])
def test_an_http_error_is_one_sentence_and_exit_2(run, script, status, body):
    script.every = (status, httpx.Headers({"Retry-After": "37"}), body)
    for command in (["chats"], ["ask", CHAT, QUESTION]):
        got = run(*command)
        assert got.rc == 2, got
        assert len(got.lines) == 1 and got.lines[0].rstrip().endswith("."), got
        assert "PROXY PAGE" not in got.text and "<" not in got.text, got


def test_the_sentences_tell_the_causes_apart(run, script):
    said = {}
    for status in (401, 403, 413, 429):
        script.every = (status, httpx.Headers({"Retry-After": "37"}), b"{}")
        said[status] = run("chats").text
    script.every = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
    said["refused"] = run("chats").text
    assert len(set(said.values())) == 5, said
    assert "token" in said[401].lower()
    assert "large" in said[413].lower() or "413" in said[413]
    assert "37" in said[429], "how long to wait"
    assert "8091" in said["refused"] or "connect" in said["refused"].lower()


def test_a_429_without_a_usable_retry_after(run, script):
    for headers in ({}, {"Retry-After": ""}, {"Retry-After": "soon\x1b[2J"}):
        script.every = (429, headers, b"{}")
        got = run("chats")
        assert got.rc == 2 and len(got.lines) == 1 and "\x1b" not in got.text, got


@pytest.mark.parametrize("error", [
    urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
    urllib.error.URLError("getaddrinfo failed"), urllib.error.URLError(socket.gaierror(1)),
    ConnectionResetError(104, "reset"), TimeoutError("timed out"), OSError("network"),
    http.client.RemoteDisconnected("gone"), http.client.IncompleteRead(b"x", 9),
    http.client.BadStatusLine("junk"), http.client.InvalidURL("bad url")],
    ids=lambda e: type(getattr(e, "reason", e)).__name__)
def test_a_network_failure_is_one_sentence_and_exit_2(run, script, error):
    script.every = error
    got = run("ask", CHAT, QUESTION)
    assert got.rc == 2 and len(got.lines) == 1, got


def test_a_failure_while_waiting_is_exit_2(run, script):
    script.override["get_answer"] = lambda m: (401, {}, b"{}")
    got = run("ask", CHAT, QUESTION)
    assert got.rc == 2, got
    assert "Sales are up." not in got.text


@pytest.mark.parametrize("raw", [b"", b"<html>", b"[]", b"null", b"5", b'"x"', b"{}",
                                 b'{"jsonrpc": "2.0", "id": 1}', b"\xff\xfe",
                                 b'{"jsonrpc": "2.0", "id": 1, "result": null}',
                                 b'{"jsonrpc": "2.0", "id": 1, "result": []}',
                                 b'{"jsonrpc": "2.0", "id": 1, "error": null}',
                                 b'{"jsonrpc": "2.0", "id": 1, "error": "x"}',
                                 b"[" * 100_000], ids=lambda raw: repr(raw[:45]))
def test_a_200_that_is_no_mcp_answer(run, script, raw):
    script.every = (200, {}, raw)
    for command in (["chats"], ["ask", CHAT, QUESTION], ["selftest"]):
        got = run(*command)
        assert got.rc in (1, 2) and got.lines, (command, got)


def test_a_json_rpc_error_is_printed(run, script):
    def refuse(message):
        return 200, {}, json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {
            "code": -32603, "message": "Internal error"}}).encode()
    script.override["list_chats"] = refuse
    got = run("chats")
    assert got.rc in (1, 2) and "Internal error" in got.text, got


JUNK_RESULTS = [{}, {"content": None}, {"content": "text"}, {"content": [None, 5, "x", {}]},
                {"content": [], "structuredContent": None},
                {"content": [], "structuredContent": []},
                {"content": [], "structuredContent": {"chats": None, "job_id": None}},
                {"content": [], "structuredContent": {"chats": "x", "job_id": 5,
                                                      "state": ["done"]}},
                {"isError": True}, {"isError": True, "content": [{"type": "text"}]}]


@pytest.mark.parametrize("result", JUNK_RESULTS, ids=lambda r: json.dumps(r)[:50])
@pytest.mark.parametrize("tool", ["list_chats", "ask", "get_answer"])
def test_a_tool_result_of_another_shape(run, script, tool, result):
    script.override[tool] = result
    command = ["chats"] if tool == "list_chats" else ["ask", CHAT, QUESTION]
    got = run(*command, "--max-wait", "5") if command[0] == "ask" else run(*command)
    assert got.rc in (1, 2) and got.lines, got
    got = run("selftest")
    assert got.rc in (1, 2) and got.steps("FAIL"), got


def test_strange_rows_of_the_chat_list_do_not_stop_the_listing(run, script):
    script.chats = [None, 5, {}, {"chat_id": None}, {"chat_id": "c_x", "title": 5,
                                                     "tables": [None, 7]},
                    {"chat_id": CHAT, "title": "Sales", "tables": ["sales.csv"]}]
    got = run("chats")
    assert got.rc == 0 and CHAT in got.out, got


# --- the selftest against a server with something wrong --------------------
@pytest.mark.parametrize("key,result,protocol", [
    ("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}, LEGACY),
    ("initialize", {}, LEGACY),
    ("server/discover", {"supportedVersions": [LEGACY]}, MODERN),
    ("server/discover", {}, MODERN),
    ("tools/list", {"tools": []}, LEGACY),
    ("tools/list", {"tools": [{"name": "list_chats"}, {"name": "ask"}]}, MODERN),
    ("tools/list", {"tools": "x"}, LEGACY)])
def test_a_wrong_step_fails_the_selftest(run, script, key, result, protocol):
    script.override[key] = result
    got = run("--protocol", protocol, "selftest")
    assert got.rc == 1, got
    assert any(key in ln for ln in got.steps("FAIL")), got
    assert not any(key in ln for ln in got.steps("PASS")), got


def test_the_selftest_asks_the_first_listed_chat(run, script):
    script.chats = [{"chat_id": "c_one", "title": "One", "tables": []},
                    {"chat_id": "c_two", "title": "Two", "tables": []}]
    assert run("selftest").rc == 0
    assert [a["chat_id"] for a in script.seam.tool_calls("ask")] == ["c_one"]


def test_an_unreachable_server_fails_the_selftest(run, script):
    script.every = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
    got = run("selftest")
    assert got.rc in (1, 2) and got.steps("PASS") == [], got
    assert len(got.lines) < 10


# ===========================================================================
# the real `_post` on a real socket
# ===========================================================================
@pytest.fixture
def serve(monkeypatch):
    """start(handler) -> (url, seen): a server on 127.0.0.1."""
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    servers = []

    def start(answer):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def _any(self):
                size = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(size)
                seen.append({"method": self.command, "path": self.path,
                             "headers": httpx.Headers(self.headers.items()), "body": body})
                answer(self, body)

            do_POST = do_GET = do_PUT = do_HEAD = _any

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}/mcp", seen

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _send(handler, status, raw=b"", headers=()):
    handler.send_response(status)
    for name, value in headers:
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _scripted(script):
    def answer(handler, body):
        status, headers, raw = script(None, {}, body, None)
        _send(handler, status, raw, list(headers.items()))
    return answer


def test_post_sends_the_body_and_returns_the_answer(mod, serve):
    url, seen = serve(lambda h, b: _send(h, 200, "აბგ".encode(), [("X-Extra", "1")]))
    body = json.dumps({"q": GEORGIAN}, ensure_ascii=False).encode("utf-8")
    status, headers, raw = mod._post(url, {"Content-Type": "application/json",
                                           "Authorization": "Bearer x",
                                           "X-PDC-User-Email": OWNER}, body, 10)
    assert (status, raw) == (200, "აბგ".encode()) and isinstance(raw, bytes)
    assert {k.lower(): v for k, v in dict(headers).items()}["x-extra"] == "1"
    [request] = seen
    assert (request["method"], request["path"], request["body"]) == ("POST", "/mcp", body)
    assert request["headers"]["content-length"] == str(len(body))
    assert request["headers"]["content-type"] == "application/json"
    assert request["headers"]["authorization"] == "Bearer x"
    assert request["headers"]["x-pdc-user-email"] == OWNER


@pytest.mark.parametrize("status", [202, 400, 401, 403, 413, 429, 500])
def test_post_returns_a_refusal_with_its_body(mod, serve, status):
    raw = b"" if status == 202 else b'{"error": "x"}'
    url, _seen = serve(lambda h, b: _send(h, status, raw, [("Retry-After", "9")]))
    got, headers, body = mod._post(url, {"Content-Type": "application/json"}, b"{}", 10)
    assert (got, body) == (status, raw)
    assert {k.lower(): v for k, v in dict(headers).items()}["retry-after"] == "9"


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_the_selftest_over_a_socket(run, serve, protocol, tmp_path):
    script = Script()
    script.answers = [script.answer("done", text_item("Sales are up."), image(PNG))]
    url, seen = serve(_scripted(script))
    got = run("--url", url, "--email", OWNER, "--protocol", protocol, "selftest",
              "--out", tmp_path / "out")
    assert got.rc == 0 and len(got.steps("PASS")) >= 4, got
    assert saved(tmp_path / "out") == {"chart_1.png": PNG}
    for request in seen:
        assert request["method"] == "POST" and request["path"] == "/mcp"
        assert request["headers"]["authorization"] == f"Bearer {TOKEN}"
        assert request["headers"]["content-type"].startswith("application/json")
        assert request["headers"]["x-pdc-user-email"] == OWNER
        message = json.loads(request["body"])
        if protocol == MODERN:
            assert request["headers"]["mcp-method"] == message["method"]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_not_followed(run, serve, status):
    """The token must not travel to an address the user did not name."""
    other, reached = serve(lambda h, b: _send(h, 200, b"{}"))
    url, seen = serve(lambda h, b: _send(h, status, b"", [("Location", other)]))
    got = run("--url", url, "chats")
    assert got.rc == 2 and len(got.lines) == 1, got
    assert len(seen) == 1 and reached == []


def test_a_refused_connection(run, serve):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    got = run("--url", f"http://127.0.0.1:{port}/mcp", "chats")
    assert got.rc == 2 and len(got.lines) == 1, got


def _hang_up(handler, body):
    handler.close_connection = True
    handler.connection.shutdown(socket.SHUT_RDWR)


def _cut_short(handler, body):
    handler.send_response(200)
    handler.send_header("Content-Length", "1000")
    handler.end_headers()
    handler.wfile.write(b'{"jsonrpc"')
    handler.close_connection = True


def _junk_status(handler, body):
    handler.wfile.write(b"this is not HTTP\r\n\r\n")
    handler.close_connection = True


@pytest.mark.parametrize("answer", [_hang_up, _cut_short, _junk_status])
def test_a_broken_answer(run, serve, answer):
    url, _seen = serve(answer)
    got = run("--url", url, "--timeout", "10", "chats")
    assert got.rc == 2 and len(got.lines) == 1, got


def test_a_silent_server_is_given_up_after_the_timeout(run, serve):
    release = threading.Event()
    url, _seen = serve(lambda h, b: release.wait(20))
    started = time.monotonic()
    try:
        got = run("--url", url, "--timeout", "1", "chats")
    finally:
        release.set()
    assert got.rc == 2 and len(got.lines) == 1, got
    assert time.monotonic() - started < 12


@pytest.mark.parametrize("email", [GEORGIAN + "@acme.com", "a@b.co\r\nX-Evil: 1", "a\nb@c.co"])
def test_an_address_that_is_no_header_value(run, serve, email):
    script = Script()
    url, seen = serve(_scripted(script))
    got = run("--url", url, "--email", email, "chats")
    assert got.rc == 2 and len(got.lines) == 1, got
    assert all("x-evil" not in r["headers"] for r in seen)


@pytest.mark.parametrize("tail", ["\r\nX-Evil: 1", "\nx", GEORGIAN])
def test_a_token_that_is_no_header_value(run, serve, tail):
    """http.client quotes the header value in its error."""
    script = Script()
    url, seen = serve(_scripted(script))
    got = run("--url", url, "chats", token=TOKEN + tail)
    assert got.rc == 2 and got.lines, got
    assert all("x-evil" not in r["headers"] for r in seen)


def test_a_line_end_after_the_token_is_ignored(run, serve, monkeypatch):
    """`PDC_MCP_TOKEN=$(type token.txt)` keeps a carriage return."""
    script = Script()
    url, seen = serve(_scripted(script))
    monkeypatch.setenv("PDC_MCP_TOKEN", f" {TOKEN}\r\n")
    assert run("--url", url, "chats", token=None).rc == 0
    assert {r["headers"]["authorization"] for r in seen} == {f"Bearer {TOKEN}"}


# ===========================================================================
# the file itself
# ===========================================================================
def _process(argv, env=None, flags=()):
    return subprocess.run([sys.executable, *flags, str(CLIENT), *argv], timeout=60,
                          capture_output=True, stdin=subprocess.DEVNULL,
                          env={**os.environ, **(env or {})}, cwd=str(CLIENT.parent.parent))


def test_it_runs_without_any_installed_package(mod):
    """`-S`: no site-packages, `-I`: no repository on the path."""
    got = _process(["--help"], flags=("-I", "-S"))
    assert got.returncode == 0, got.stderr
    assert b"selftest" in got.stdout


def test_it_runs_as_a_program_on_a_narrow_console(mod, serve):
    script = Script()
    script.answers = [script.answer("done", text_item(f"Before {GEORGIAN} after"))]
    url, seen = serve(_scripted(script))
    env = {"PDC_MCP_TOKEN": TOKEN, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    got = _process(["--url", url, "ask", CHAT, QUESTION], env)
    assert got.returncode == 0, got.stderr
    assert b"Before " in got.stdout and b" after" in got.stdout
    assert seen and TOKEN.encode() not in got.stdout + got.stderr
    script.answers = [dict(script.answer("failed", text_item(GEORGIAN)), isError=True)]
    got = _process(["--url", url, "ask", CHAT, QUESTION], env)
    assert got.returncode == 1 and b"Traceback" not in got.stderr, got.stderr


@pytest.fixture
def tree(mod):
    return ast.parse(CLIENT.read_text(encoding="utf-8"))


def test_only_the_standard_library_is_imported(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative import"
            names.add(node.module.split(".")[0])
    assert {"urllib", "argparse", "json", "base64", "pathlib", "time", "sys"} <= names
    assert names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)
    # nothing that runs other code, and no import by another way
    assert not names & {"subprocess", "ctypes", "pickle", "importlib", "runpy", "code"}
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not called & {"__import__", "exec", "eval", "compile"}
    here = {p.stem for p in ROOT.glob("*.py")} | {"routes", "tools", "executor"}
    assert not names & here, "no module of the application"


def test_the_certificate_of_an_https_server_is_checked(tree):
    source = ast.unparse(tree)
    for word in ("_create_unverified_context", "CERT_NONE", "check_hostname"):
        assert word not in source, word


def test_main_and_post_have_the_agreed_signatures(tree):
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert [a.arg for a in functions["main"].args.args] == ["argv"]
    assert [a.arg for a in functions["_post"].args.args][:4] == \
        ["url", "headers", "body_bytes", "timeout"]


def test_importing_it_does_nothing(mod, capsys):
    _out, _err = capsys.readouterr()
    assert _out + _err == ""


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "executor/Dockerfile"])
def test_it_is_in_no_image(mod, dockerfile):
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    assert "mcp_test_client" not in text
    joined = re.sub(r"\\\r?\n", " ", text)
    for line in joined.splitlines():
        words = line.split()
        if words and words[0].upper() in ("COPY", "ADD") \
                and not any(w.startswith("--from") for w in words):
            for source in words[1:-1]:
                assert source.rstrip("/") not in (".", "./", "tools", "./tools", "tools/*",
                                                  "tools/*.py"), line
