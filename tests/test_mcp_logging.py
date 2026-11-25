"""MCP stage 4 — what the log may say, and what the brain may be sent.

End to end through `POST /mcp` on the real app with the REAL `mcp_answer`;
only the planner pipeline and the PNG renderer are scripted (the Article II
test runs the real pipeline against a recorded transport).

* exactly one `MCP_CALL` line per tools/call, with `token=` (the token's
  name), the acting user (a `user=` field or the line's `[sid=…]` tag),
  `tool=`, `chat_id=`, `job_id=`, `ms=` and `outcome=` — a field the call does
  not have is absent; a get_answer line names the chat of the job it read;
* no log line, nor anything printed, carries the raw token, its hash, the
  answer, a table value, image data, or more than 120 characters of the
  question; nothing a request or a stored document holds can start a line.

Offline: DATA_ROOT is tmp_path; no brain call, no PNG render.
"""
import base64
import copy
import hashlib
import importlib
import json
import logging
import re
import secrets
import threading
import time

import pandas as pd
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import brain_client
import local_store
import mcp_tokens
from conftest import JSON_HEADERS
from settings import settings

ADMIN = "ladmin"
OWNER = "owner@acme.com"
FRIEND = "friend@acme.com"
CHAT = "c_mcplogging00001"
FOREIGN = "c_mcplogging00002"
QUESTION = "How did sales go?"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
RENDERED = base64.b64encode(PNG + b"SENTINELIMAGE" * 3).decode("ascii")

# What an answer may hold and a log line may not.
SENTINELS = {
    "answer": "SENTINELANSWER", "cell": "SENTINELCELL", "number": "918273645",
    "number, grouped": "918,273,645", "title": "SENTINELTITLE", "caption": "SENTINELCAPTION",
    "narrative": "SENTINELNARRATIVE", "value": "SENTINELVALUE", "chart": "SENTINELCHART",
    "image": RENDERED[:60], "image bytes": "SENTINELIMAGE", "code": "SENTINELCODE",
}


# ===========================================================================
# the world
# ===========================================================================
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        if getattr(record, "_mcp_test_seen", False):
            return              # one record reaches two loggers
        record._mcp_test_seen = True
        try:
            text = record.getMessage()
            if record.exc_info:
                text += " " + logging.Formatter().formatException(record.exc_info)
        except Exception as e:
            text = f"UNFORMATTABLE {type(e).__name__}"
        self.lines.append(text)


@pytest.fixture
def logs():
    """EVERY line of every logger."""
    handler = _Capture()
    loggers = [logging.getLogger(), logging.getLogger("datachat")]
    old = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
    loggers[1].setLevel(logging.INFO)
    if loggers[0].level > logging.INFO or loggers[0].level == logging.NOTSET:
        loggers[0].setLevel(logging.INFO)
    yield handler.lines
    for lg, level in zip(loggers, old):
        lg.removeHandler(handler)
        lg.setLevel(level)


def _app_lines(logs):
    """The application's own lines (`log_with_sid`)."""
    return [ln for ln in logs if ln.startswith("[sid=")]


def _make_chat(chat_id, owner, shared=(), title="Sales"):
    store = local_store.ChatDataStore(chat_id)
    pd.DataFrame({"region": [f"r{i}" for i in range(30)],
                  "amount": [30609121 + i for i in range(30)]}).to_csv(
        store.files_dir / "sales.csv", index=False)
    meta = store.read_meta()
    meta.update({"owner": owner, "title": title,
                 "files": [{"file_name": "sales.csv", "file_description": "sales",
                            "schema": {"fields": {"region": {"description": "region"},
                                                  "amount": {"description": "amount"}}}}],
                 "sharing": {"shared_with": list(shared)}})
    store.write_meta(meta)
    store.set_welcome("Welcome to Sales.")
    local_store.AuthStore().record_active_chat(owner, chat_id, title, ["sales.csv"])
    return store


@pytest.fixture
def world(tmp_path, monkeypatch):
    for name in ("MCP_ANSWER_WAIT_SECONDS", "MCP_TABLE_MAX_ROWS"):
        assert name in type(settings).model_fields, f"settings must define {name}"
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", ADMIN)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://pdc.bank.example")
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    monkeypatch.setattr(settings, "EXECUTOR_NETWORK_CIDR", "")
    monkeypatch.setattr(settings, "MCP_ANSWER_WAIT_SECONDS", 15.0)
    monkeypatch.setattr(settings, "MCP_TABLE_MAX_ROWS", 50)
    monkeypatch.setattr(settings, "SLOW_ANSWER_NOTICE_SECONDS", 0)
    local_store._DATAFRAME_CACHE.invalidate()
    for email in (OWNER, FRIEND):
        local_store.AuthStore().ensure_user(email)
    _make_chat(FOREIGN, FRIEND, title="Private")
    yield _make_chat(CHAT, OWNER, shared=(FRIEND,))
    local_store._DATAFRAME_CACHE.invalidate()


class Renderer:
    raises = None
    gate = None

    def __call__(self, image_b64, sid):
        if self.gate is not None:
            assert self.gate.wait(30)
        if self.raises is not None:
            raise self.raises
        return (RENDERED, True) if str(image_b64).startswith("<") else (image_b64, True)


@pytest.fixture
def renderer(monkeypatch):
    report = importlib.import_module("routes.report")
    r = Renderer()
    monkeypatch.setattr(report, "_resolve_chart_image", r)
    monkeypatch.setattr(report, "_plotly_html_to_png", lambda *a, **k: None)
    yield r
    if r.gate is not None:
        r.gate.set()


@pytest.fixture
def jobs(world, renderer, monkeypatch):
    importlib.import_module("mcp_answer")
    endpoint = importlib.import_module("routes.mcp")
    mod = importlib.import_module("mcp_jobs")
    endpoint.reset_limiter()
    mod.reset()
    yield mod
    for t in threading.enumerate():
        if t.name.startswith("mcp-job"):
            t.join(15)
    mod.reset()
    endpoint.reset_limiter()


class Pipeline:
    def __init__(self):
        self.events, self.raises = [], None

    def __call__(self, **kw):
        yield from copy.deepcopy(self.events)
        if self.raises is not None:
            raise self.raises


@pytest.fixture
def pipeline(jobs, monkeypatch):
    p = Pipeline()
    monkeypatch.setattr(importlib.import_module("routes.chat").run_chat_local,
                        "run_chat_multi_plot", p)
    return p


def read_answer(res, job_id):
    """(state, the items of the answer) of a get_answer result. Its first item
    is the state as JSON; only a running job has it as structuredContent too."""
    first = res["content"][0]
    assert set(first) == {"type", "text"} and first["type"] == "text", first
    head = json.loads(first["text"])
    assert set(head) == {"job_id", "state"} and head["job_id"] == job_id, head
    state, items = head["state"], res["content"][1:]
    if state == "running":
        assert res["structuredContent"] == head and items == [], res
    else:
        assert state in ("done", "failed") and items, res
        assert "structuredContent" not in res, "the answer would be shown to no model"
    assert bool(res.get("isError")) is (state == "failed"), res
    return state, items


class Bot:
    def __init__(self, name="Helpdesk bot"):
        self.row, self.raw = mcp_tokens.create_token(name, "per_user", ADMIN)
        self.client = TestClient(importlib.import_module("app").app,
                                 base_url="https://testserver")

    def post(self, tool, args=None, *, email=OWNER, raw=None):
        headers = [("Authorization", f"Bearer {raw or self.raw}")]
        if email is not None:
            headers.append(("X-PDC-User-Email", email))
        return self.client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}}})

    def call(self, tool, args=None, **kw):
        r = self.post(tool, args, **kw)
        assert r.status_code == 200, (r.status_code, r.text[:300])
        return r.json()["result"]

    def ask(self, question=QUESTION, chat=CHAT, **kw):
        res = self.call("ask", {"chat_id": chat, "question": question}, **kw)
        assert not res.get("isError"), res
        return res["structuredContent"]["job_id"]

    def answer(self, question=QUESTION, **kw):
        job_id = self.ask(question, **kw)
        for _ in range(5):
            res = self.call("get_answer", {"job_id": job_id}, **kw)
            state, items = read_answer(res, job_id)
            if state != "running":
                return job_id, {**res, "content": items}    # the answer's own items
        raise AssertionError("the job never finished")


@pytest.fixture
def bot(pipeline):
    return Bot()


def _full_turn():
    """A turn holding every sentinel."""
    table = {"columns": ["name", "amount"], "total_rows": 900,
             "rows": [{"name": "SENTINELCELL", "amount": 918273645}], "title": "SENTINELTITLE"}
    charts = [{"partial": True, "phase": 1, "image_base64": f"<div>plotly SENTINELCHART{n}</div>",
               "answer": "SENTINELCAPTION", "caption_template": "In {{x}}.",
               "code": "fig = SENTINELCODE", "chart_n": n, "chart_total": 2, "usage": {},
               "chart_data": {"rows": [{"v": "SENTINELVALUE"}]}} for n in (1, 2)]
    narrative = {"narrative_template": "T {{x}}", "narrative_rendered": "SENTINELNARRATIVE",
                 "values": {"x": "SENTINELVALUE"}, "findings": {}}
    return charts + [
        {"done": True, "partial": False, "combined_answer": "T {{x}}",
         "display_answer": "SENTINELANSWER", "combined_codes": ["fig = SENTINELCODE"] * 2,
         "total_usage": {}, "tables": [table], "table_codes": ["RESULT = SENTINELCODE"],
         "table_result_keys": [None], "table_captions": ["SENTINELCAPTION"],
         "table_caption_templates": ["C {{x}}"], "table_phases": [1],
         "narrative_view": {"phase1": narrative, "phase2": None, "phase2_status": None}}]


def _single(text="SENTINELANSWER", **extra):
    return [{"single_response": True, "result": {
        "text": text, "image_base64": None, "table": None, "code": "RESULT = SENTINELCODE",
        "usage": {}, **extra}}]


def _clean(logs, capfd=None, *secrets_):
    text = list(logs)
    if capfd is not None:
        out = capfd.readouterr()
        text += (out.out + out.err).splitlines()
    for what, mark in SENTINELS.items():
        assert not [ln for ln in text if mark in ln], f"{what} was logged"
    for secret in secrets_:
        assert not [ln for ln in text if secret in ln], "a secret was logged"
    return text


def _token_secrets(raw):
    """The token, its random part, its hash — whole or the larger part."""
    digest = hashlib.sha256(raw.encode()).hexdigest()
    body = raw.split("_", 1)[1]
    return [raw, body, body[:16], body[-16:], digest, digest[:16], digest.upper()]


# ===========================================================================
# one line per call
# ===========================================================================
def _call_lines(logs):
    """The marker itself, not a value that quotes it."""
    return [ln for ln in _app_lines(logs) if re.search(r"\bMCP_CALL\b(?! FORGED)", ln)]


def _field(line, name):
    m = re.search(rf"\b{name}=(.*?)(?=\]| \w+=|$)", line)
    return m.group(1).strip().strip("'\"") if m else None


def _user(line):
    """The acting user: a `user=` field, or the line's tag."""
    return _field(line, "user") or _field(line, "sid")


def test_exactly_one_line_per_call_with_its_fields(bot, pipeline, logs):
    pipeline.events = _full_turn()
    bot.call("list_chats")
    job_id, res = bot.answer()
    assert not res.get("isError")
    lines = _call_lines(logs)
    assert [_field(ln, "tool") for ln in lines] == ["list_chats", "ask", "get_answer"], lines
    for ln in lines:
        assert _field(ln, "token") == "Helpdesk bot", ln
        assert _user(ln) == OWNER, ln
        assert re.fullmatch(r"\d+", _field(ln, "ms") or ""), ln
        assert re.fullmatch(r"[a-z_]+", _field(ln, "outcome") or ""), ln
    assert [_field(ln, "chat_id") for ln in lines] == [None, CHAT, CHAT], lines
    assert [_field(ln, "job_id") for ln in lines] == [None, job_id, job_id], lines


def test_every_refused_call_has_one_line(bot, pipeline, logs):
    pipeline.events = _single()
    calls = [lambda: bot.call("list_chats", email=None),
             lambda: bot.call("list_chats", email="not-an-address"),
             lambda: bot.call("ask", {"chat_id": FOREIGN, "question": QUESTION}),
             lambda: bot.call("ask", {"chat_id": CHAT, "question": ""}),
             lambda: bot.call("ask", {}),
             lambda: bot.call("get_answer", {"job_id": "0" * 32}),
             lambda: bot.call("get_answer", {})]
    for n, call in enumerate(calls, start=1):
        assert call()["isError"] is True
        lines = _call_lines(logs)
        assert len(lines) == n, lines
        assert _field(lines[-1], "outcome") not in (None, "ok", "done"), lines[-1]
        assert _field(lines[-1], "token") == "Helpdesk bot", lines[-1]


def test_a_failed_answer_and_a_running_one_are_told_apart(bot, pipeline, logs, monkeypatch):
    pipeline.events, pipeline.raises = [], ValueError("SENTINELANSWER")
    _job, res = bot.answer()
    assert res["isError"] is True
    pipeline.raises, pipeline.events = None, _single()
    bot.answer()
    outcomes = [_field(ln, "outcome") for ln in _call_lines(logs)
                if _field(ln, "tool") == "get_answer"]
    assert len(outcomes) == 2 and outcomes[0] != outcomes[1], outcomes


def test_a_call_without_a_valid_token_has_no_call_line(bot, logs):
    wrong = "pdcmcp_" + secrets.token_urlsafe(32)
    assert bot.post("list_chats", raw=wrong).status_code == 401
    assert _call_lines(logs) == []
    _clean(logs, None, *_token_secrets(wrong))


# ===========================================================================
# nothing of the answer, no token
# ===========================================================================
def _all_of_it(bot, pipeline, renderer):
    pipeline.events = _full_turn()
    bot.call("list_chats")
    _job, res = bot.answer()
    dump = json.dumps(res)
    for mark in ("SENTINELCELL", "SENTINELCAPTION", "SENTINELNARRATIVE", "SENTINELTITLE"):
        assert mark in dump, f"the script is blind: {mark} is not in the answer"
    assert [c["type"] for c in res["content"]].count("image") == 2
    pipeline.events = _single(table={"columns": ["a"], "rows": [{"a": "SENTINELCELL"}],
                                     "total_rows": 1}, value_display="SENTINELVALUE",
                              image_base64=base64.b64encode(
                                  b"GIF89a SENTINELIMAGE").decode())
    bot.answer()


def test_no_line_carries_token_answer_table_or_image(bot, pipeline, renderer, logs, capfd):
    _all_of_it(bot, pipeline, renderer)
    assert _call_lines(logs), "the capture is blind"
    _clean(logs, capfd, *_token_secrets(bot.raw))


def test_nor_does_the_log_file(bot, pipeline, renderer, tmp_path, monkeypatch, logs):
    """What is written is what the handlers get: no second, raw channel."""
    seen = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: seen.append(" ".join(map(str, a))))
    _all_of_it(bot, pipeline, renderer)
    _clean(seen, None, *_token_secrets(bot.raw))
    for p in tmp_path.rglob("*.log*"):
        _clean(p.read_text(encoding="utf-8", errors="replace").splitlines(), None,
               *_token_secrets(bot.raw))


@pytest.mark.parametrize("error", [ValueError("SENTINELCELL 918273645 SENTINELIMAGE"),
                                   KeyError("SENTINELANSWER\n[sid=x] FORGED")], ids=["v", "k"])
def test_a_failing_render_logs_no_data(bot, pipeline, renderer, logs, error):
    pipeline.events = _full_turn()
    renderer.raises = error
    _job, res = bot.answer()
    assert not res.get("isError")
    _clean(logs)
    assert not [ln for ln in _app_lines(logs) if len(ln.splitlines()) > 1]


def test_a_hanging_render_logs_no_data(bot, pipeline, renderer, logs, monkeypatch):
    monkeypatch.setattr(importlib.import_module("mcp_answer"), "RENDER_TIMEOUT_S", 0.2)
    renderer.gate = threading.Event()
    pipeline.events = _single(image_base64="<div>plotly SENTINELCHART</div>")
    _job, res = bot.answer()
    assert not res.get("isError")
    _clean(logs)


def test_a_failing_content_builder_logs_no_data(bot, pipeline, renderer, logs, monkeypatch):
    """Whatever raises inside the builder quotes what it choked on."""
    def boom(*a, **k):
        raise TypeError("SENTINELCELL SENTINELANSWER")

    for mod, name in (("narrative", "plain"), ("ident_rule", "identifier_columns"),
                      ("ident_rule", "plain_text"), ("routes.auth", "_public_base")):
        with monkeypatch.context() as m:
            m.setattr(importlib.import_module(mod), name, boom)
            pipeline.events = _full_turn()
            bot.answer()
            _clean(logs)


def test_a_failed_turn_logs_no_answer(bot, pipeline, logs):
    """A failure after a chart was produced."""
    pipeline.events = _full_turn()[:1]
    pipeline.raises = brain_client.BrainError("x")
    _job, res = bot.answer()
    assert res["isError"] is True
    _clean(logs, None, *_token_secrets(bot.raw))


def test_every_refusal_keeps_the_token_out(bot, pipeline, logs, capfd):
    pipeline.events = _single()
    bot.call("list_chats", email=None)
    bot.call("ask", {"chat_id": bot.raw, "question": QUESTION})
    bot.post(bot.raw)
    bot.call("get_answer", {"job_id": "0" * 32})
    mcp_tokens.revoke_token(bot.row["id"], ADMIN)
    assert bot.post("list_chats").status_code == 401
    # (the chat id IS the token here: the one echo a caller could ask for)
    others = [ln for ln in logs if "MCP_CALL" not in ln]
    _clean(others, capfd, hashlib.sha256(bot.raw.encode()).hexdigest())
    assert not [ln for ln in others if bot.raw in ln and "chat_id" not in ln]


# ===========================================================================
# the question
# ===========================================================================
@pytest.mark.parametrize("fails", [False, True], ids=["answered", "failed"])
def test_at_most_120_characters_of_the_question(bot, pipeline, logs, capfd, fails):
    pipeline.events = [] if fails else _single()
    question = "HEAD " + "q" * 400 + " TAIL"
    bot.answer(question)
    text = _clean(logs, capfd)
    assert not [ln for ln in text if "TAIL" in ln or "q" * 121 in ln]
    assert not [ln for ln in _call_lines(logs) if "qqq" in ln]


def test_the_question_of_a_refused_call_is_not_logged_at_all(bot, pipeline, logs):
    question = "PRIVATEQ please"
    bot.call("ask", {"chat_id": FOREIGN, "question": question})
    bot.call("ask", {"chat_id": CHAT, "question": question}, email=None)
    assert not [ln for ln in logs if "PRIVATEQ" in ln], logs


# ===========================================================================
# nothing can forge a line
# ===========================================================================
BREAKS = ["\n", "\r\n", "\r", "\x0b", "\x0c", "\x1c", "\x85", " ", " "]
FORGED = "[sid=mcp] MCP_CALL FORGED outcome=ok"


def _no_forgery(logs):
    bad = [ln for ln in logs if len(ln.splitlines()) > 1]
    assert bad == [], bad
    assert not [ln for ln in logs if len(ln) > 4000]
    # never a second MCP_CALL record: the genuine marker comes first
    assert not [ln for ln in logs if ln.startswith(FORGED)]


def _rename_token(tmp_path, name):
    """A name create_token would refuse, put into the stored document."""
    path = tmp_path / "mcp_tokens.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["tokens"][0]["name"] = name
    path.write_text(json.dumps(doc), encoding="utf-8")


@pytest.mark.parametrize("brk", BREAKS, ids=ascii)
def test_a_hostile_token_name(bot, pipeline, logs, tmp_path, brk):
    pipeline.events = _single()
    _rename_token(tmp_path, f"bot{brk}{FORGED}")
    bot.call("list_chats")
    bot.answer()
    bot.call("ask", {"chat_id": FOREIGN, "question": QUESTION})
    assert len(_call_lines(logs)) == 4
    _no_forgery(logs)


@pytest.mark.parametrize("name", [None, 5, ["x"], {"a": 1}, "n" * 50_000], ids=lambda v: ascii(v)[:9])
def test_an_odd_stored_token_name(bot, pipeline, logs, tmp_path, name):
    _rename_token(tmp_path, name)
    bot.call("list_chats")
    assert len(_call_lines(logs)) == 1
    _no_forgery(logs)


@pytest.mark.parametrize("brk", BREAKS, ids=ascii)
def test_hostile_arguments(bot, logs, brk):
    evil = f"x{brk}{FORGED}"
    bot.call("ask", {"chat_id": evil, "question": evil})
    bot.call("ask", {"chat_id": CHAT + evil, "question": evil})
    bot.call("get_answer", {"job_id": evil})
    bot.call("get_answer", {"job_id": evil + "y" * 50_000})
    assert len(_call_lines(logs)) == 4
    bot.post(evil)
    _no_forgery(logs)


@pytest.mark.parametrize("email", [b"owner@acme.com\x85FORGED", b"a\x1cb@acme.com",
                                   b"\xe2\x80\xa8@acme.com", b"x" * 5000 + b"@acme.com",
                                   b"owner@acme.com\tMCP_CALL", b"[sid=x] MCP_CALL FORGED"])
def test_a_hostile_address(bot, logs, email):
    try:
        res = bot.call("list_chats", email=email.decode("latin-1").encode("latin-1"))
    except (UnicodeError, ValueError):
        pytest.skip("the HTTP client refuses this header")
    assert res["isError"] is True
    assert len(_call_lines(logs)) == 1
    _no_forgery(logs)


def test_a_hostile_chat_title_and_conversation(bot, pipeline, renderer, logs, world):
    """Names that reach the answer's last line are not log text."""
    settings.PUBLIC_BASE_URL = ""
    meta = world.read_meta()
    meta["title"] = "Sales\n" + FORGED
    world.write_meta(meta)
    local_store.AuthStore().rename_active_chat(OWNER, CHAT, "Sales\n" + FORGED)
    pipeline.events = _single()
    _job, res = bot.answer()
    assert not res.get("isError")
    _no_forgery(logs)


# ===========================================================================
# Article II: an MCP question sends the brain what a web question sends
# ===========================================================================
TCODE = ("g = dfs['sales.csv'].head(4)[['region', 'amount']].copy()\n"
         "VALUES['total'] = int(g['amount'].sum())\n"
         "FINDINGS['big'] = True\n"
         "RESULT = g")
PCODE = ("import plotly.graph_objects as go\n"
         "g = dfs['sales.csv'].head(5)\n"
         "VALUES['total'] = int(g['amount'].sum())\n"
         "fig = go.Figure(go.Bar(x=g['region'], y=g['amount']))")
NTEMPLATE = "The total was {{total:count}}.{{#big}} It is large.{{/big}}"
# the sums the code computes, and single cells
TOTALS = [sum(30609121 + i for i in range(n)) for n in (4, 5)]
VALUES_ = [str(v) for v in TOTALS + [30609121, 30609124]]
# (`rows` is a dataset profile's row COUNT)
FORBIDDEN_KEYS = {"image_base64", "images", "chart_data", "table", "tables",
                  "narrative_rendered", "caption_rendered", "value_display", "values",
                  "headline_display", "preview_rows", "data", "mimeType", "content_items"}


def _walk_keys(value, out):
    if isinstance(value, dict):
        for k, v in value.items():
            out.add(k)
            _walk_keys(v, out)
    elif isinstance(value, list):
        for v in value:
            _walk_keys(v, out)
    return out


@pytest.fixture
def transport(jobs, monkeypatch):
    """The real pipeline behind both ways in, recorded at `_post`: a table
    question, a chart question, a table question."""
    posts = []

    def post(path, payload, sid, timeout=None):
        posts.append((path, {k: v for k, v in copy.deepcopy(payload).items() if k != "sid"}))
        if path == "/v1/plan":
            charty = "chart" in payload.get("question", "")
            code, kind = (PCODE, "PLOT_CODE") if charty else (TCODE, "PYTHON")
            return {"kind": kind, "code": code, "usage": {},
                    "raw_text": f"```python\n{code}\n```", "narrative": NTEMPLATE}
        return {"text": "Described.", "usage": {}}

    monkeypatch.setattr(brain_client, "_post", post)
    monkeypatch.setattr(settings, "BRAIN_CAPABILITIES", "narrative_v1,diagnostics_v1")
    return posts


QUESTIONS = ["first question", "draw a chart please", "third question"]


def _web_posts(posts):
    chat_mod = importlib.import_module("routes.chat")
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = "s_" + secrets.token_hex(8)
        return {"ok": True}

    client = TestClient(app)
    client.post(f"/_login/{OWNER}", headers=JSON_HEADERS)
    posts.clear()
    conv_id = None
    for q in QUESTIONS:
        body = {"question": q, **({"conv_id": conv_id} if conv_id else {})}
        r = client.post(f"/api/chat/{CHAT}/chat/stream", json=body, headers=JSON_HEADERS)
        assert r.status_code == 200, r.text[:300]
        conv_id = conv_id or json.loads(
            next(ln[6:] for ln in r.text.splitlines() if ln.startswith("data: ")))["conv_id"]
        time.sleep(0.05)
    return copy.deepcopy(posts)


def test_the_brain_is_sent_exactly_what_the_web_sends(transport):
    web = _web_posts(transport)
    bot = Bot()
    transport.clear()
    answers = [bot.answer(q)[1] for q in QUESTIONS]
    mcp = copy.deepcopy(transport)
    # the script is not blind: the answers do hold the data
    assert not [a for a in answers if a.get("isError")], answers
    dump = json.dumps(answers)
    assert f"{TOTALS[0]:,}" in dump or str(TOTALS[0]) in dump
    assert "image" in [c["type"] for c in answers[1]["content"]]
    assert [p for p, _ in mcp].count("/v1/plan") == 3
    assert json.dumps(mcp, sort_keys=True, default=str) == \
        json.dumps(web, sort_keys=True, default=str)


def test_no_answer_table_or_image_reaches_the_brain(transport):
    bot = Bot()
    for q in QUESTIONS:
        bot.answer(q)
    sent = json.dumps(transport, ensure_ascii=False, default=str)
    assert "third question" in sent and "draw a chart" in sent, "the recorder is blind"
    for value in VALUES_ + [f"{int(v):,}" for v in VALUES_]:
        assert value not in sent, value
    assert RENDERED[:40] not in sent and "plotly.js" not in sent.lower()
    keys = set()
    for path, payload in transport:
        _walk_keys(payload, keys)
    assert not keys & FORBIDDEN_KEYS, keys & FORBIDDEN_KEYS
    assert bot.raw not in sent and "Helpdesk" not in sent and "MCP 20" not in sent


def test_reading_an_answer_sends_nothing(transport):
    bot = Bot()
    job_id, _res = bot.answer()
    transport.clear()
    for _ in range(3):
        bot.call("get_answer", {"job_id": job_id})
    bot.call("list_chats")
    assert transport == []
