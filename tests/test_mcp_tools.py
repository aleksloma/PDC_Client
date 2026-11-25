"""MCP stage 3 — the three tools, end to end through `POST /mcp`.

`list_chats`, `ask`, `get_answer` on the real app, with the real identity, the
real `chat_turn` / `mcp_conversations` and the real permission helpers of
routes/chat.py; only the planner pipeline (`run_chat_multi_plot`) and the
answer builder (`mcp_answer`, stage 4) are scripted.

Pinned readings of the brief:
* the Auto Analytics check, like the chat access check, refuses in `ask`
  itself, with the web's text; the empty dataset and the role gate show up in
  the job;
* `get_answer` checks the chat access again: a user whose share was removed
  after `ask` gets "Job not found." (on the web the history is refused too);
* it checks the token again too, before delivering and while waiting: a token
  revoked meanwhile gets the 401 of any refused token;
* the job thread checks the chat access once more before the turn;
* every state of `get_answer` is in its FIRST text item, exactly
  {"job_id", "state"}. A finished answer has no structuredContent: a client
  that shows its model only that would lose the answer;
* every refused job read — unknown, expired, another user's, another token's,
  access removed — is the same bytes;
* the question is stripped and an empty one gets the web's text.

Offline: DATA_ROOT is tmp_path; no brain call, no PNG render.
"""
import asyncio
import importlib
import json
import logging
import re
import sys
import threading
import time
import types

import httpx
import pandas as pd
import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import local_store
import logger_utils
import mcp_tokens
from settings import settings

ADMIN = "ladmin"
OWNER = "owner@acme.com"
FRIEND = "friend@acme.com"
STRANGER = "stranger@acme.com"
BOT = "bot@acme.com"
CHAT = "c_mcptools0000001"
FOREIGN = "c_mcptools0000002"
QUESTION = "How did sales go?"
TOOL_NAMES = ["ask", "get_answer", "list_chats"]
NOT_FOUND = "Job not found."
GENERIC = "Something went wrong while processing your request."


# ===========================================================================
# the world
# ===========================================================================
@pytest.fixture
def logs():
    """Every line written through `log_with_sid`, whichever module emits it."""
    class _Capture(logging.Handler):
        def emit(self, record):
            self.lines.append(record.getMessage())

    logger = logger_utils.get_logger()
    handler = _Capture(level=logging.DEBUG)
    handler.lines = []
    old_level = logger.level
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def _make_chat(chat_id, owner, shared=(), title="Sales"):
    store = local_store.ChatDataStore(chat_id)
    pd.DataFrame({"region": ["North", "South"], "amount": [1, 2]}).to_csv(
        store.files_dir / "sales.csv", index=False)
    meta = store.read_meta()
    meta.update({"owner": owner, "title": title,
                 "files": [{"file_name": "sales.csv", "file_description": "sales",
                            "schema": {"fields": {"region": {}, "amount": {}}}}],
                 "sharing": {"shared_with": list(shared)}})
    store.write_meta(meta)
    store.set_welcome("Welcome to Sales.")
    local_store.AuthStore().record_active_chat(owner, chat_id, title, ["sales.csv"])
    for email in shared:
        local_store.AuthStore().record_shared_chat(email, chat_id, title, ["sales.csv"], owner)
    return store


class Content:
    """`mcp_answer`: text of the record's content, one image item."""

    def __init__(self):
        self.calls = []

    def build_content(self, record, *, chat_id, conv_id, email, sid, saved=True):
        self.calls.append({"record": record, "chat_id": chat_id, "conv_id": conv_id,
                           "email": email, "sid": sid, "saved": saved})
        return [{"type": "text", "text": f"ANSWER {record.get('content')}"},
                {"type": "image", "data": "aGk=", "mimeType": "image/png"}]

    def error_content(self, text):
        return [{"type": "text", "text": text}]


class Pipeline:
    """The scripted `run_chat_multi_plot`: `gate` holds every run."""

    def __init__(self):
        self.calls, self.events, self.raises = [], [_single()], None
        self.gate = threading.Event()
        self.gate.set()

    def __call__(self, **kw):
        self.calls.append({**kw, "history_rows": list(kw["history_rows"]),
                           "dfs": dict(kw["dfs"])})
        gate = self.gate

        def run():
            assert gate.wait(20)
            yield from [dict(e) for e in self.events]
            if self.raises is not None:
                raise self.raises
        return run()


def _single(text="the answer", **extra):
    res = {"text": text, "image_base64": None, "table": None, "code": "RESULT = 1",
           "usage": {}}
    res.update(extra)
    return {"single_response": True, "result": res}


@pytest.fixture
def world(tmp_path, monkeypatch):
    chat_mod = importlib.import_module("routes.chat")
    defined = "MCP_ANSWER_WAIT_SECONDS" in type(settings).model_fields
    assert defined, "settings must define MCP_ANSWER_WAIT_SECONDS"
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", ADMIN)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    monkeypatch.setattr(settings, "EXECUTOR_NETWORK_CIDR", "")
    monkeypatch.setattr(settings, "MCP_ANSWER_WAIT_SECONDS", 15.0)
    local_store._DATAFRAME_CACHE.invalidate()
    auth = local_store.AuthStore()
    for email in (OWNER, FRIEND, STRANGER, BOT):
        auth.ensure_user(email)
    _make_chat(FOREIGN, STRANGER, title="Private")
    chat_mod._INFLIGHT_CODES.clear()
    chat_mod._INFLIGHT_SQL.clear()
    yield _make_chat(CHAT, OWNER, shared=(FRIEND,))
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def pipeline(world, monkeypatch):
    p = Pipeline()
    chat_mod = importlib.import_module("routes.chat")
    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot", p)

    titled = []
    monkeypatch.setattr(chat_mod, "_start_title_generation",
                        lambda *a, **k: titled.append(a))
    yield p
    p.gate.set()
    assert titled == [], "an MCP conversation is never titled by the brain"


@pytest.fixture
def content(monkeypatch):
    c = Content()
    try:
        mod = importlib.import_module("mcp_answer")
    except ImportError:
        mod = types.ModuleType("mcp_answer")
        monkeypatch.setitem(sys.modules, "mcp_answer", mod)
    monkeypatch.setattr(mod, "build_content", c.build_content, raising=False)
    monkeypatch.setattr(mod, "error_content", c.error_content, raising=False)
    return c


@pytest.fixture
def jobs(world, pipeline, content):
    endpoint = importlib.import_module("routes.mcp")
    mod = importlib.import_module("mcp_jobs")
    endpoint.reset_limiter()
    mod.reset()
    yield mod
    pipeline.gate.set()
    for t in threading.enumerate():
        if t.name.startswith("mcp-job"):
            t.join(15)
    mod.reset()
    endpoint.reset_limiter()


class Bot:
    """A chatbot backend holding one token."""

    def __init__(self, mode="per_user", name="Helpdesk bot", account=None):
        self.row, self.raw = mcp_tokens.create_token(name, mode, ADMIN, account)
        self.client = TestClient(app_mod.app, base_url="https://testserver")

    def call_raw(self, tool, args=None, *, emails=()):
        headers = [("Authorization", f"Bearer {self.raw}")]
        headers += [("X-PDC-User-Email", e) for e in emails]
        return self.client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": {} if args is None else args}})

    def call(self, tool, args=None, email=OWNER, emails=None):
        r = self.call_raw(tool, args, emails=[email] if emails is None and email
                          else (emails or []))
        assert r.status_code == 200, (r.status_code, r.text[:300])
        assert "set-cookie" not in r.headers
        body = r.json()
        assert "result" in body, body
        if tool == "get_answer":
            state_of(body["result"])            # every answer keeps the shape
        return body["result"]

    def ask(self, question=QUESTION, chat=CHAT, **kw):
        res = self.call("ask", {"chat_id": chat, "question": question}, **kw)
        assert not res.get("isError"), res
        return res["structuredContent"]["job_id"]

    def wait(self, job_id, **kw):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            res = self.call("get_answer", {"job_id": job_id}, **kw)
            if state_of(res) != "running":
                return res
        raise AssertionError("the job never finished")


@pytest.fixture
def bot(jobs):
    return Bot()


def state_of(res, job_id=None):
    """The state of a get_answer result, None for a refused call ("Job not
    found.", an identity refusal). Checks the shape of each."""
    first = res["content"][0]
    assert first["type"] == "text" and set(first) == {"type", "text"}, res
    try:
        out = json.loads(first["text"])
    except ValueError:
        error_text(res)
        return None
    assert isinstance(out, dict) and set(out) == {"job_id", "state"}, res
    assert isinstance(out["job_id"], str) and re.fullmatch(r"[0-9a-f]{32}", out["job_id"])
    assert out["state"] in ("running", "done", "failed"), res
    assert job_id is None or out["job_id"] == job_id
    if out["state"] == "running":
        assert len(res["content"]) == 1, res
        assert res["structuredContent"] == out
        assert not res.get("isError")
    else:
        assert "structuredContent" not in res, "the client would show only this"
        assert len(res["content"]) >= 2, "a finished job delivers something"
        assert res.get("isError", False) is (out["state"] == "failed"), res
    return out["state"]


def failed_text(res, job_id=None):
    """The one sentence of a failed answer."""
    assert state_of(res, job_id) == "failed", res
    assert [c["type"] for c in res["content"]] == ["text", "text"], res
    text = res["content"][1]["text"]
    assert isinstance(text, str) and text.strip() and "Traceback" not in text
    return text


def error_text(res):
    """The one sentence of a refused call."""
    assert res["isError"] is True, res
    assert "structuredContent" not in res, res
    assert [c["type"] for c in res["content"]] == ["text"], res
    text = res["content"][0]["text"]
    assert isinstance(text, str) and text.strip()
    assert "Traceback" not in text
    return text


def _snapshot(root):
    """Everything a question could create or change (not the token stamps)."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and p.name != "mcp_tokens.json" and "logs" not in p.parts}


def _until(predicate, what):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out: {what}")


# ===========================================================================
# the tool definitions
# ===========================================================================
@pytest.fixture
def tools(bot):
    r = bot.client.post("/mcp", headers={"Authorization": f"Bearer {bot.raw}"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return {t["name"]: t for t in r.json()["result"]["tools"]}


def test_the_three_tools_and_their_schemas(tools):
    assert sorted(tools) == TOOL_NAMES
    wanted = {"list_chats": [], "ask": ["chat_id", "question"], "get_answer": ["job_id"]}
    for name, fields in wanted.items():
        schema = tools[name]["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert sorted(schema["properties"]) == fields
        assert sorted(schema.get("required") or []) == fields
        assert all(p["type"] == "string" for p in schema["properties"].values())


def test_no_tool_takes_an_identity(tools):
    """The address comes from the header only: a language model fills tool
    arguments."""
    fields = {f for t in tools.values() for f in t["inputSchema"]["properties"]}
    assert not [f for f in fields if "mail" in f or "user" in f or "token" in f], fields


def test_the_descriptions_tell_the_model_the_order(tools):
    text = {n: t["description"] for n, t in tools.items()}
    assert "first" in text["list_chats"].lower() and "ask" in text["list_chats"]
    assert "list_chats" in text["ask"] and "get_answer" in text["ask"]
    assert "running" in text["get_answer"] and "again" in text["get_answer"].lower()
    assert "ask" in text["get_answer"]


# ===========================================================================
# who the caller is
# ===========================================================================
def test_one_fixed_sentence_per_cause(bot, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "root@acme.com")
    local_store.AuthStore().ensure_user("root@acme.com")
    missing = error_text(bot.call("list_chats", emails=[]))
    blank = error_text(bot.call("list_chats", emails=[" "]))
    malformed = error_text(bot.call("list_chats", emails=["not-an-address"]))
    repeated = error_text(bot.call("list_chats", emails=[OWNER, OWNER]))
    unknown = error_text(bot.call("list_chats", email="nobody@evil.example"))
    bootstrap = error_text(bot.call("list_chats", email="root@acme.com"))
    assert missing == blank
    assert malformed == repeated
    # whether an address exists must not be readable from the answer
    assert unknown == bootstrap
    assert len({missing, malformed, unknown}) == 3
    for text in (missing, malformed, unknown):
        assert text[0].isupper() and text.endswith("."), text
        assert "evil" not in text and "root" not in text and "@" not in text


@pytest.mark.parametrize("tool,args", [("list_chats", {}),
                                       ("ask", {"chat_id": CHAT, "question": QUESTION}),
                                       ("get_answer", {"job_id": "0" * 32})])
def test_every_tool_needs_an_identity(bot, tool, args, tmp_path, pipeline):
    before = _snapshot(tmp_path)
    res = bot.call(tool, args, emails=[])
    assert error_text(res) != NOT_FOUND
    assert _snapshot(tmp_path) == before and pipeline.calls == []


def test_the_address_is_trimmed_and_lower_cased(bot):
    res = bot.call("list_chats", email="  Owner@ACME.com ")
    assert [c["chat_id"] for c in res["structuredContent"]["chats"]] == [CHAT]


def test_an_address_in_an_approved_domain_gets_an_account(bot, tmp_path):
    import signin_domains
    signin_domains.save(["newbank.example"], ADMIN)
    res = bot.call("list_chats", email="first@newbank.example")
    assert res["structuredContent"] == {"chats": []}
    assert local_store.AuthStore().user_exists("first@newbank.example")


def test_a_shared_token_acts_as_its_account_whatever_the_header_says(jobs):
    chat = "c_mcptools0000003"
    _make_chat(chat, BOT, title="Bot chat")
    shared = Bot("shared_account", "Shared", BOT)
    for emails in ([], [OWNER], [OWNER, FRIEND], ["not-an-address"]):
        res = shared.call("list_chats", emails=emails)
        assert [c["chat_id"] for c in res["structuredContent"]["chats"]] == [chat], emails
    error_text(shared.call("ask", {"chat_id": CHAT, "question": QUESTION}, emails=[OWNER]))


def test_a_shared_token_whose_account_was_removed(jobs, pipeline, tmp_path):
    chat = "c_mcptools0000003"
    _make_chat(chat, OWNER, shared=(BOT,), title="Bot chat")
    shared = Bot("shared_account", "Shared", BOT)
    job = shared.ask(chat=chat, emails=[])
    assert not shared.wait(job, emails=[]).get("isError")
    assert local_store.AuthStore().remove_user(BOT) is True
    refusals = {error_text(shared.call("list_chats", emails=[])),
                error_text(shared.call("get_answer", {"job_id": job}, emails=[])),
                error_text(shared.call("ask", {"chat_id": chat, "question": "q"}, emails=[]))}
    assert len(refusals) == 1
    assert not (tmp_path / "users" / BOT).exists()
    # the same address later is another account
    local_store.AuthStore().ensure_user(BOT)
    profile = tmp_path / "users" / BOT / "profile.json"
    doc = json.loads(profile.read_text(encoding="utf-8"))
    doc["created_at"] = "2030-01-01T00:00:00+00:00"
    profile.write_text(json.dumps(doc), encoding="utf-8")
    assert error_text(shared.call("get_answer", {"job_id": job}, emails=[])) in refusals
    assert len(pipeline.calls) == 1


def test_an_address_in_the_arguments_changes_nothing(bot, pipeline):
    """Whatever the model puts into the arguments, the header decides."""
    args = {"chat_id": FOREIGN, "question": QUESTION, "email": STRANGER,
            "X-PDC-User-Email": STRANGER, "user": STRANGER}
    error_text(bot.call("ask", args, email=OWNER))
    assert pipeline.calls == []
    res = bot.call("list_chats", {"email": STRANGER}, email=OWNER)
    assert "Private" not in json.dumps(res)


# ===========================================================================
# list_chats
# ===========================================================================
def _one_text(res):
    assert [c["type"] for c in res["content"]] == ["text"], res
    return json.loads(res["content"][0]["text"])


def test_own_and_shared_chats_with_names_only(bot):
    expected = {"chats": [{"chat_id": CHAT, "title": "Sales", "tables": ["sales.csv"]}]}
    for email in (OWNER, FRIEND):
        res = bot.call("list_chats", email=email)
        assert not res.get("isError")
        assert res["structuredContent"] == expected
        assert _one_text(res) == expected
        dump = json.dumps(res)
        for value in ("North", "South", "region", OWNER, "Welcome"):
            assert value not in dump, value


def test_the_title_is_the_users_own(bot):
    local_store.AuthStore().rename_active_chat(FRIEND, CHAT, "My numbers")
    res = bot.call("list_chats", email=FRIEND)
    assert [c["title"] for c in res["structuredContent"]["chats"]] == ["My numbers"]
    res = bot.call("list_chats", email=OWNER)
    assert [c["title"] for c in res["structuredContent"]["chats"]] == ["Sales"]


def test_the_order_is_the_sidebars(bot):
    auth = local_store.AuthStore()
    for i in (3, 4):
        _make_chat(f"c_mcptools000000{i}", OWNER, title=f"Chat {i}")
    expected = [r["chat_id"] for r in auth.list_active_chats(OWNER)]
    res = bot.call("list_chats")
    assert [c["chat_id"] for c in res["structuredContent"]["chats"]] == expected
    assert len(expected) == 3


def test_a_removed_share_is_not_listed(bot, world):
    world.remove_share_recipient(FRIEND)
    assert [r["chat_id"] for r in local_store.AuthStore().list_active_chats(FRIEND)] == [CHAT]
    assert bot.call("list_chats", email=FRIEND)["structuredContent"] == {"chats": []}


def test_another_users_chat_is_never_listed(bot):
    """Even with a sidebar row naming it."""
    local_store.AuthStore().record_active_chat(OWNER, FOREIGN, "Private", [])
    res = bot.call("list_chats", email=OWNER)
    assert [c["chat_id"] for c in res["structuredContent"]["chats"]] == [CHAT]
    assert "Private" not in json.dumps(res)


def test_unknown_and_broken_rows_create_no_folder_and_never_fail(bot, tmp_path):
    path = tmp_path / "users" / OWNER / "active_chats.jsonl"
    rows = [{"chat_id": "c_ghost00000000001", "title": "Ghost"},
            {"title": "No id"}, {"chat_id": None, "title": "x"}, {"chat_id": 7},
            {"chat_id": ["c_x"]}, {"chat_id": ""}, {"chat_id": "../../users/x"},
            {"chat_id": "x" * 500}]
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
        fh.write("{not json\n")
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")
                    if "logs" not in p.parts)
    res = bot.call("list_chats")
    assert [c["chat_id"] for c in res["structuredContent"]["chats"]] == [CHAT]
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")
                   if "logs" not in p.parts)
    assert after == before, "listing created files or folders"


@pytest.mark.parametrize("files", [None, "x", [None, 5, "x", {}, {"file_name": None},
                                                {"file_name": 7}], {"a": 1}], ids=range(4))
def test_a_broken_chat_meta_never_fails_the_list(bot, world, files):
    meta = world.read_meta()
    meta["files"] = files
    world.write_meta(meta)
    chats = bot.call("list_chats")["structuredContent"]["chats"]
    assert [c["chat_id"] for c in chats] == [CHAT]
    assert all(isinstance(t, str) for t in chats[0]["tables"])


@pytest.mark.parametrize("title", [None, 5, ["x"], {"a": 1}], ids=repr)
def test_a_title_is_always_text(bot, tmp_path, title):
    path = tmp_path / "users" / OWNER / "active_chats.jsonl"
    path.write_text(json.dumps({"chat_id": CHAT, "title": title}) + "\n", encoding="utf-8")
    chats = bot.call("list_chats")["structuredContent"]["chats"]
    assert [c["chat_id"] for c in chats] == [CHAT]
    assert isinstance(chats[0]["title"], str)


def test_a_database_table_is_listed_by_its_display_name(bot, world):
    meta = world.read_meta()
    meta["files"].append({"file_name": "Loans", "source": "database",
                          "db": {"table_id": "aa11bb22cc33dd44", "display_name": "Loans"},
                          "schema": {"fields": {"secret_column": {}}}})
    world.write_meta(meta)
    res = bot.call("list_chats")
    assert res["structuredContent"]["chats"][0]["tables"] == ["sales.csv", "Loans"]
    assert "secret_column" not in json.dumps(res)


# ===========================================================================
# ask
# ===========================================================================
def test_ask_starts_a_job_and_answers_at_once(bot, pipeline):
    pipeline.gate.clear()
    started = time.monotonic()
    res = bot.call("ask", {"chat_id": CHAT, "question": QUESTION})
    assert time.monotonic() - started < 10
    assert not res.get("isError")
    out = res["structuredContent"]
    assert set(out) == {"job_id", "state"} and out["state"] == "running"
    assert re.fullmatch(r"[0-9a-f]{32}", out["job_id"])
    assert _one_text(res) == out


def test_the_question_reaches_the_planner_as_on_the_web(bot, pipeline):
    bot.wait(bot.ask("  How did sales go?\n"))
    kw = pipeline.calls[0]
    assert kw["question"] == QUESTION
    assert kw["user_email"] == OWNER
    assert list(kw["dfs"]) == ["sales.csv"]


def test_the_days_conversation_is_visible_on_the_web(bot, world, content):
    bot.wait(bot.ask())
    rows = [r for r in local_store.AuthStore().list_conversations(OWNER)
            if r["chat_id"] == CHAT]
    assert len(rows) == 1 and re.fullmatch(r"MCP \d{4}-\d\d-\d\d", rows[0]["title"])
    history = world.get_history(rows[0]["conv_id"])
    assert [(r["role"], r["content"]) for r in history] == [
        ("ai", "Welcome to Sales."), ("human", QUESTION), ("ai", "the answer")]
    assert content.calls[0]["conv_id"] == rows[0]["conv_id"]
    assert content.calls[0]["chat_id"] == CHAT and content.calls[0]["email"] == OWNER


@pytest.mark.parametrize("question", ["", "   ", "\n\t", None, 5, ["x"], {"a": 1}, True,
                                      "missing"], ids=repr)
def test_an_empty_or_odd_question_is_refused(bot, pipeline, tmp_path, question):
    before = _snapshot(tmp_path)
    args = {"chat_id": CHAT, "question": question}
    if question == "missing":
        del args["question"]
    text = error_text(bot.call("ask", args))
    if isinstance(question, str):
        assert text == "Question cannot be empty."
    assert _snapshot(tmp_path) == before and pipeline.calls == []


@pytest.mark.parametrize("chat_id", ["c_nosuchchat", "", None, 5, ["x"], {"a": 1},
                                     "../users/owner@acme.com", "c_x/../" + CHAT,
                                     CHAT + "\n", " " + CHAT, "x" * 500, "\ud800", "missing"],
                         ids=lambda v: ascii(v)[:20])
def test_an_unknown_or_malformed_chat_id_is_refused(bot, pipeline, tmp_path, chat_id):
    before = _snapshot(tmp_path)
    dirs = sorted(p.name for p in (tmp_path / "chatdata").iterdir())
    args = {"chat_id": chat_id, "question": QUESTION}
    if chat_id == "missing":
        del args["chat_id"]
    error_text(bot.call("ask", args))
    assert sorted(p.name for p in (tmp_path / "chatdata").iterdir()) == dirs, \
        "a chat folder was created"
    assert _snapshot(tmp_path) == before and pipeline.calls == []


@pytest.mark.parametrize("email,chat", [(OWNER, FOREIGN), (FRIEND, FOREIGN),
                                        (STRANGER, CHAT), (BOT, CHAT)])
def test_another_users_chat_is_refused_and_nothing_is_created(bot, pipeline, tmp_path,
                                                              email, chat):
    before = _snapshot(tmp_path)
    text = error_text(bot.call("ask", {"chat_id": chat, "question": QUESTION}, email=email))
    assert "Private" not in text and "Sales" not in text and "@" not in text
    assert _snapshot(tmp_path) == before and pipeline.calls == []


def test_a_share_recipient_asks_into_their_own_conversation(bot, world, pipeline):
    bot.wait(bot.ask("owner's secret question", email=OWNER), email=OWNER)
    bot.wait(bot.ask("friend's question", email=FRIEND), email=FRIEND)
    auth = local_store.AuthStore()
    mine = [r["conv_id"] for r in auth.list_conversations(OWNER)]
    theirs = [r["conv_id"] for r in auth.list_conversations(FRIEND)]
    assert len(mine) == len(theirs) == 1 and mine != theirs
    # the recipient's planner never sees the owner's conversation
    assert "secret" not in json.dumps(pipeline.calls[1]["history_rows"])
    assert "friend's question" not in json.dumps(world.get_history(mine[0]))


def test_the_earlier_questions_of_the_day_are_the_history(bot, pipeline):
    bot.wait(bot.ask("first?"))
    bot.wait(bot.ask("second?"))
    seen = [(r["role"], r["content"]) for r in pipeline.calls[1]["history_rows"]]
    assert seen == [("ai", "Welcome to Sales."), ("human", "first?"), ("ai", "the answer")]


def test_a_removed_share_is_refused(bot, world, pipeline, tmp_path):
    world.remove_share_recipient(FRIEND)
    before = _snapshot(tmp_path)
    error_text(bot.call("ask", {"chat_id": CHAT, "question": QUESTION}, email=FRIEND))
    assert _snapshot(tmp_path) == before and pipeline.calls == []


def test_auto_analytics_busy_is_refused_with_the_webs_text(bot, world, pipeline, tmp_path):
    meta = world.read_meta()
    meta["auto_analysis"] = {"status": "processing"}
    world.write_meta(meta)
    before = _snapshot(tmp_path)
    text = error_text(bot.call("ask", {"chat_id": CHAT, "question": QUESTION}))
    chat_mod = importlib.import_module("routes.chat")
    web = json.loads(chat_mod._auto_analysis_busy_response(world, OWNER, CHAT).body)
    assert text == web["error"]
    assert _snapshot(tmp_path) == before and pipeline.calls == []
    # and it leaves no reservation behind
    meta["auto_analysis"] = {"status": "done"}
    world.write_meta(meta)
    assert not bot.wait(bot.ask()).get("isError")


def test_a_second_question_while_one_runs_is_refused(bot, pipeline):
    pipeline.gate.clear()
    first = bot.ask()
    res = bot.call("ask", {"chat_id": CHAT, "question": "and now?"})
    text = error_text(res)
    assert text[0].isupper() and text.endswith(".")
    # another user of the same chat is not held up
    friends = bot.ask(email=FRIEND)
    pipeline.gate.set()
    assert not bot.wait(first).get("isError")
    assert not bot.wait(friends, email=FRIEND).get("isError")
    assert len(pipeline.calls) == 2


def test_an_empty_dataset_fails_with_the_webs_text(bot, world, pipeline):
    (world.files_dir / "sales.csv").unlink()
    meta = world.read_meta()
    meta["files"] = []
    world.write_meta(meta)
    local_store._DATAFRAME_CACHE.invalidate()
    job_id = bot.ask()
    assert failed_text(bot.wait(job_id), job_id) == "Chat dataset is empty."
    assert pipeline.calls == []
    assert local_store.AuthStore().list_conversations(OWNER) == []


@pytest.mark.parametrize("what", ["deleted", "unshared"])
def test_a_chat_gone_between_ask_and_the_turn(bot, world, jobs, pipeline, tmp_path,
                                              monkeypatch, what):
    """The job fails with the sentence `ask` itself uses; no folder comes
    back and nothing is saved."""
    import shutil
    asked = error_text(bot.call("ask", {"chat_id": "c_nosuchchat", "question": QUESTION}))
    real = jobs.start

    def start(*a, **k):
        if what == "deleted":
            shutil.rmtree(tmp_path / "chatdata" / CHAT)
        else:
            world.remove_share_recipient(FRIEND)
        return real(*a, **k)

    with monkeypatch.context() as m:
        m.setattr(jobs, "start", start)
        job_id = bot.ask(email=FRIEND)
    _until(lambda: jobs.get(job_id, bot.row["id"], FRIEND).state != "running", "the job")
    job = jobs.get(job_id, bot.row["id"], FRIEND)
    assert (job.state, job.error_text) == ("failed", asked)
    assert pipeline.calls == []
    assert local_store.AuthStore().list_conversations(FRIEND) == []
    assert not (tmp_path / "users" / FRIEND / "mcp_conversations.json").exists()
    assert (tmp_path / "chatdata" / CHAT).exists() is (what == "unshared"), \
        "the chat folder was re-created"


def test_an_answer_that_was_not_saved_is_delivered_and_says_so(bot, pipeline, content,
                                                               monkeypatch):
    real = local_store.ChatDataStore.append_history

    def broken(self, conv_id, message):
        if message.get("role") == "ai" and message.get("content") == "the answer":
            raise OSError("disk full")
        return real(self, conv_id, message)

    monkeypatch.setattr(local_store.ChatDataStore, "append_history", broken)
    res = bot.wait(bot.ask())
    assert state_of(res) == "done"
    assert [c["saved"] for c in content.calls] == [False]
    monkeypatch.setattr(local_store.ChatDataStore, "append_history", real)
    bot.wait(bot.ask())
    assert [c["saved"] for c in content.calls] == [False, True]


def _raise_busy(monkeypatch, jobs, exc):
    chat_mod = importlib.import_module("routes.chat")
    monkeypatch.setattr(chat_mod, "_auto_analysis_busy_response", exc)


def _raise_access(monkeypatch, jobs, exc):
    monkeypatch.setattr(importlib.import_module("routes.chat"), "_require_chat", exc)


def _raise_start(monkeypatch, jobs, exc):
    monkeypatch.setattr(jobs, "start", exc)


@pytest.mark.parametrize("where", [_raise_busy, _raise_access, _raise_start],
                         ids=["busy", "access", "start"])
def test_an_unexpected_failure_in_ask_is_an_internal_error(bot, jobs, pipeline, monkeypatch,
                                                           logs, where):
    """-32603, and "error" in the log: a refusal is a decision, this is not."""
    def boom(*a, **k):
        raise KeyError("column 'SECRET'")

    with monkeypatch.context() as m:
        where(m, jobs, boom)
        r = bot.call_raw("ask", {"chat_id": CHAT, "question": QUESTION}, emails=[OWNER])
    assert r.status_code == 200
    assert r.json()["error"] == {"code": -32603, "message": "Internal error"}
    lines = _call_lines(logs)
    assert len(lines) == 1 and re.search(r"\boutcome=error\b", lines[0]), lines
    assert not [ln for ln in logs if "SECRET" in ln]
    assert not bot.wait(bot.ask()).get("isError"), "a reservation was left behind"


def test_a_question_that_cannot_be_stored_never_crashes(bot, pipeline):
    """A lone surrogate is valid JSON and cannot be written as UTF-8."""
    res = bot.call("ask", {"chat_id": CHAT, "question": "x \ud800 y"})
    if res.get("isError"):
        error_text(res)
    else:
        job_id = res["structuredContent"]["job_id"]
        assert state_of(bot.wait(job_id), job_id) in ("done", "failed")
    assert not bot.wait(bot.ask("a normal one")).get("isError")


# ===========================================================================
# table permissions
# ===========================================================================
def _add_db_table(store, *, live, keep_file):
    """A registered table of the chat; nobody's role covers it."""
    import db_sources
    import roles_store
    roles_store.RolesStore().ensure_base_role()
    reg = db_sources.DataSourceStore()
    cid = reg.create_connection({"name": "S", "db_type": "sqlite",
                                 "url_override": "sqlite+pysqlite:///x.db"},
                                "pw", actor=ADMIN)["id"]
    doc = {"connection_id": cid, "schema": "", "table_name": "loans",
           "display_name": "Loans", "description": "", "is_connector": False,
           "relations": [], "columns": [{"name": "a", "dtype": "INTEGER",
                                         "description": "col a"}]}
    if live:
        doc["mode"] = "live"
    tid = reg.upsert_table(doc, actor=ADMIN)["id"]
    if not live:
        pd.DataFrame({"a": [1, 2, 3]}).to_parquet(local_store.db_snapshot_path(tid))
    meta = store.read_meta()
    if not keep_file:
        (store.files_dir / "sales.csv").unlink()
        meta["files"] = []
    meta["files"].append({
        "file_name": "Loans", "file_description": "", "source": "database",
        "db": {"table_id": tid, "connection_id": cid, "schema": "", "table_name": "loans",
               "display_name": "Loans", "is_connector": False, "auto_included": False,
               "row_count": None, "refreshed_at": None, "relations": []},
        "schema": {"file_name": "Loans",
                   "fields": {"a": {"description": "col a", "values": None}}}})
    store.write_meta(meta)
    local_store._DATAFRAME_CACHE.invalidate()
    assert "Loans" in store.load_dataframes(include_live=True), "the fixture must load"
    return tid


@pytest.mark.parametrize("live", [False, True], ids=["snapshot", "live"])
@pytest.mark.parametrize("email", [OWNER, FRIEND])
def test_an_uncovered_table_never_reaches_the_planner(bot, world, pipeline, live, email):
    _add_db_table(world, live=live, keep_file=True)
    assert not bot.wait(bot.ask(email=email), email=email).get("isError")
    assert list(pipeline.calls[0]["dfs"]) == ["sales.csv"]
    assert "Loans" not in pipeline.calls[0]["schema_docs"]


@pytest.mark.parametrize("live", [False, True], ids=["snapshot", "live"])
def test_a_chat_with_nothing_left_answers_the_denial_like_the_web(bot, world, pipeline,
                                                                  content, live):
    _add_db_table(world, live=live, keep_file=False)
    res = bot.wait(bot.ask(email=FRIEND), email=FRIEND)
    assert not res.get("isError"), "on the web the denial is a saved answer"
    assert pipeline.calls == []
    assert "no longer have access to Loans" in content.calls[0]["record"]["content"]


@pytest.mark.parametrize("live", [False, True], ids=["snapshot", "live"])
def test_a_covered_table_reaches_the_planner(bot, world, pipeline, live):
    import roles_store
    tid = _add_db_table(world, live=live, keep_file=True)
    role = roles_store.RolesStore().create_role({"name": "R", "table_ids": [tid]},
                                                actor=ADMIN)
    local_store.AuthStore().set_data_role(FRIEND, role["id"])
    bot.wait(bot.ask(email=FRIEND), email=FRIEND)
    assert set(pipeline.calls[0]["dfs"]) == {"sales.csv", "Loans"}
    # the same question of the owner, who holds no such role
    bot.wait(bot.ask(email=OWNER), email=OWNER)
    assert set(pipeline.calls[1]["dfs"]) == {"sales.csv"}


# ===========================================================================
# get_answer
# ===========================================================================
def test_a_done_answer_is_the_state_and_the_jobs_content_as_it_is(bot, jobs):
    items = [{"type": "text", "text": "ANSWER the answer"},
             {"type": "image", "data": "aGk=", "mimeType": "image/png"}]
    job_id = bot.ask()
    res = bot.wait(job_id)
    assert not res.get("isError")
    assert json.loads(res["content"][0]["text"]) == {"job_id": job_id, "state": "done"}
    assert res["content"][1:] == items
    assert set(res) <= {"content", "isError"}, set(res)
    # it can be read again: the state item is never added to the job itself
    assert bot.wait(job_id) == res
    assert jobs.get(job_id, bot.row["id"], OWNER).content == items


@pytest.mark.parametrize("text", ["ANSWER", '{"job_id": "x", "state": "running"}', "", " "],
                         ids=["plain", "json-lookalike", "empty", "blank"])
def test_the_answers_own_items_are_never_merged_or_changed(bot, monkeypatch, text):
    """An answer that looks like a state stays the second item."""
    items = [{"type": "text", "text": text}, {"type": "text", "text": "two"}]
    monkeypatch.setattr(sys.modules["mcp_answer"], "build_content",
                        lambda *a, **k: [dict(i) for i in items])
    job_id = bot.ask()
    res = bot.wait(job_id)
    assert state_of(res, job_id) == "done" and res["content"][1:] == items


def test_a_running_answer(bot, pipeline, monkeypatch):
    monkeypatch.setattr(settings, "MCP_ANSWER_WAIT_SECONDS", 0.0)
    pipeline.gate.clear()
    job_id = bot.ask()
    res = bot.call("get_answer", {"job_id": job_id})
    assert not res.get("isError")
    assert res["structuredContent"] == {"job_id": job_id, "state": "running"}
    assert _one_text(res) == res["structuredContent"]
    assert set(res) <= {"content", "structuredContent", "isError"}, set(res)


def _errors():
    m = importlib.import_module("routes.chat")
    return {
        "tenant": (m.TenantRevokedError("x"),
                   "Service unavailable. Please contact your administrator."),
        "analysis": (m.run_chat_local.AnalysisFailedError("llm_unavailable"),
                     m.ANALYSIS_FAILED_TEXT),
        "timeout": (m.BrainTimeoutError("x"), m.BRAIN_TIMEOUT_TEXT),
        "brain": (m.BrainError("x"), "Analysis service is temporarily unavailable."),
        "generic": (ValueError("column 'SECRET'"), GENERIC),
    }


@pytest.mark.parametrize("name", ["tenant", "analysis", "timeout", "brain", "generic"])
def test_a_failed_answer_is_the_webs_text(bot, pipeline, name):
    pipeline.events = []
    pipeline.raises, text = _errors()[name]
    job_id = bot.ask()
    res = bot.wait(job_id)
    assert failed_text(res, job_id) == text
    assert json.loads(res["content"][0]["text"]) == {"job_id": job_id, "state": "failed"}
    assert set(res) == {"content", "isError"}, set(res)
    assert "SECRET" not in json.dumps(res)
    # the next question is accepted
    pipeline.events, pipeline.raises = [_single()], None
    assert not bot.wait(bot.ask()).get("isError")


def test_a_turn_without_any_event_is_failed_with_the_generic_text(bot, pipeline):
    pipeline.events = []
    assert failed_text(bot.wait(bot.ask())) == GENERIC


def test_a_stopped_turn_is_done(bot, pipeline, content, monkeypatch):
    chat_mod = importlib.import_module("routes.chat")
    monkeypatch.setattr(chat_mod, "_is_cancelled", lambda conv_id: True)
    res = bot.wait(bot.ask())
    assert state_of(res) == "done"
    assert content.calls[0]["record"]["stopped"] is True


# --- the wait ---------------------------------------------------------------
def test_it_waits_up_to_the_setting_and_then_says_running(bot, pipeline, monkeypatch):
    pipeline.gate.clear()
    job_id = bot.ask()
    for wait in (0.6, 0.0):                         # read per call
        monkeypatch.setattr(settings, "MCP_ANSWER_WAIT_SECONDS", wait)
        started = time.monotonic()
        res = bot.call("get_answer", {"job_id": job_id})
        took = time.monotonic() - started
        assert state_of(res, job_id) == "running"
        assert wait - 0.05 <= took < wait + 5, (wait, took)


def test_it_answers_as_soon_as_the_job_is_done(bot, pipeline, monkeypatch):
    monkeypatch.setattr(settings, "MCP_ANSWER_WAIT_SECONDS", 30.0)
    pipeline.gate.clear()
    job_id = bot.ask()
    threading.Timer(0.4, pipeline.gate.set).start()
    started = time.monotonic()
    res = bot.call("get_answer", {"job_id": job_id})
    assert state_of(res, job_id) == "done"
    assert time.monotonic() - started < 15


def test_an_unknown_job_does_not_wait(bot, monkeypatch):
    monkeypatch.setattr(settings, "MCP_ANSWER_WAIT_SECONDS", 30.0)
    started = time.monotonic()
    assert error_text(bot.call("get_answer", {"job_id": "0" * 32})) == NOT_FOUND
    assert time.monotonic() - started < 10


def _async_calls(bot, coro):
    """Run `coro(send)` on one event loop; send(method, tool, args) posts."""
    async def go():
        transport = httpx.ASGITransport(app=app_mod.app, client=("203.0.113.7", 1))
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver",
                                     timeout=60) as c:
            async def send(method, params=None):
                r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method,
                                               "params": params or {}},
                                 headers={"Authorization": f"Bearer {bot.raw}",
                                          "X-PDC-User-Email": OWNER})
                return r.json()["result"]
            return await coro(send)
    return asyncio.run(go())


def test_the_wait_does_not_block_the_event_loop(bot, pipeline, monkeypatch):
    """Everything else is served while calls wait for their answers."""
    monkeypatch.setattr(settings, "MCP_ANSWER_WAIT_SECONDS", 6.0)
    pipeline.gate.clear()
    job_id = bot.ask()

    async def go(send):
        waiting = [asyncio.create_task(send("tools/call", {
            "name": "get_answer", "arguments": {"job_id": job_id}})) for _ in range(12)]
        await asyncio.sleep(0.5)
        started = time.monotonic()
        for _ in range(3):
            assert await send("ping") == {}
        listed = await send("tools/call", {"name": "list_chats", "arguments": {}})
        took = time.monotonic() - started
        assert listed["structuredContent"]["chats"]
        assert not any(t.done() for t in waiting), "the calls did not wait at all"
        pipeline.gate.set()
        return took, await asyncio.gather(*waiting)

    took, results = _async_calls(bot, go)
    assert took < 4, f"another request took {took:.1f}s while answers were awaited"
    assert {state_of(r, job_id) for r in results} == {"done"}


def test_many_waiting_calls_use_no_worker_threads(bot, pipeline, monkeypatch):
    """The wait is a coroutine, never a parked pool thread: with every thread
    parked the whole application would stop."""
    monkeypatch.setattr(settings, "MCP_ANSWER_WAIT_SECONDS", 3.0)
    pipeline.gate.clear()
    job_id = bot.ask()

    async def go(send):
        tasks = [asyncio.create_task(send("tools/call", {
            "name": "get_answer", "arguments": {"job_id": job_id}})) for _ in range(60)]
        await asyncio.sleep(1.0)
        started = time.monotonic()
        await send("tools/call", {"name": "list_chats", "arguments": {}})
        took = time.monotonic() - started
        pipeline.gate.set()
        await asyncio.gather(*tasks)
        return took

    assert _async_calls(bot, go) < 2.5


def test_a_token_dropped_while_waiting_ends_the_wait(bot, jobs, pipeline, monkeypatch):
    monkeypatch.setattr(settings, "MCP_ANSWER_WAIT_SECONDS", 30.0)
    pipeline.gate.clear()
    job_id = bot.ask()
    threading.Timer(0.4, jobs.drop_token, [bot.row["id"]]).start()
    started = time.monotonic()
    assert error_text(bot.call("get_answer", {"job_id": job_id})) == NOT_FOUND
    assert time.monotonic() - started < 15


# --- who may read -----------------------------------------------------------
def _refusal(b, job_id, email=OWNER):
    r = b.call_raw("get_answer", {"job_id": job_id}, emails=[email])
    assert r.status_code == 200
    assert error_text(r.json()["result"]) == NOT_FOUND
    return r.content


@pytest.mark.parametrize("state", ["running", "done"])
def test_every_foreign_read_is_the_same_not_found(bot, jobs, pipeline, state):
    other = Bot(name="Other bot")
    if state == "running":
        pipeline.gate.clear()
    job_id = bot.ask()
    if state == "done":
        bot.wait(job_id)
    answers = {
        _refusal(bot, job_id, FRIEND),          # another user, the chat is shared with them
        _refusal(bot, job_id, STRANGER),        # another user
        _refusal(other, job_id, OWNER),         # the same user, another token
        _refusal(bot, "0" * 32),                # unknown
        _refusal(bot, "nonsense"),
    }
    assert len(answers) == 1, "the refusals can be told apart"
    assert CHAT not in str(answers)


def test_a_shared_token_does_not_read_the_same_users_per_user_job(bot, jobs):
    shared = Bot("shared_account", "Shared", OWNER)
    job_id = bot.ask()
    bot.wait(job_id)
    assert error_text(shared.call("get_answer", {"job_id": job_id}, emails=[])) == NOT_FOUND


@pytest.mark.parametrize("job_id", [None, "", 5, True, ["x"], {"a": 1}, "\ud800",
                                    "x" * 100_000, "../x", "missing"],
                         ids=lambda v: ascii(v)[:20])
def test_a_malformed_job_id_is_not_found(bot, job_id):
    args = {} if job_id == "missing" else {"job_id": job_id}
    assert error_text(bot.call("get_answer", args)) == NOT_FOUND


def test_a_refused_read_is_one_sentence_and_nothing_else(bot):
    res = bot.call("get_answer", {"job_id": "0" * 32})
    assert res == {"content": [{"type": "text", "text": NOT_FOUND}], "isError": True}


def test_an_expired_job_is_not_found(bot, jobs, monkeypatch):
    now = {"t": 9_000.0}
    monkeypatch.setattr(jobs, "_clock", lambda: now["t"])
    job_id = bot.ask()
    assert not bot.wait(job_id).get("isError")
    now["t"] += jobs.JOB_TTL_S + 1
    _refusal(bot, job_id)


def test_a_revoked_token_reads_nothing(bot, jobs):
    job_id = bot.ask()
    bot.wait(job_id)
    mcp_tokens.revoke_token(bot.row["id"], ADMIN)
    r = bot.call_raw("get_answer", {"job_id": job_id}, emails=[OWNER])
    assert r.status_code == 401 and "ANSWER" not in r.text


def _is_401(r):
    """The refusal of any bad token, and nothing of the answer."""
    assert r.status_code == 401, (r.status_code, r.text[:300])
    assert r.headers["www-authenticate"] == "Bearer"
    assert "jsonrpc" not in r.json() and "ANSWER" not in r.text
    bad = TestClient(app_mod.app, base_url="https://testserver").post(
        "/mcp", headers={"Authorization": "Bearer pdcmcp_" + "B" * 43},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.content == bad.content


def test_a_token_revoked_during_the_wait_is_401_at_the_end_of_it(bot, pipeline, monkeypatch):
    """Without drop_token: the token itself is looked at again."""
    monkeypatch.setattr(settings, "MCP_ANSWER_WAIT_SECONDS", 1.2)
    pipeline.gate.clear()
    job_id = bot.ask()
    threading.Timer(0.5, mcp_tokens.revoke_token, [bot.row["id"], ADMIN]).start()
    _is_401(bot.call_raw("get_answer", {"job_id": job_id}, emails=[OWNER]))


def test_a_token_revoked_during_the_wait_is_seen_at_the_next_recheck(bot, pipeline,
                                                                     monkeypatch):
    endpoint = importlib.import_module("routes.mcp")
    monkeypatch.setattr(endpoint, "ACCESS_RECHECK_S", 0.3)
    monkeypatch.setattr(settings, "MCP_ANSWER_WAIT_SECONDS", 40.0)
    pipeline.gate.clear()
    job_id = bot.ask()
    threading.Timer(0.5, mcp_tokens.revoke_token, [bot.row["id"], ADMIN]).start()
    started = time.monotonic()
    _is_401(bot.call_raw("get_answer", {"job_id": job_id}, emails=[OWNER]))
    assert time.monotonic() - started < 20


def test_a_token_revoked_just_before_the_answer_arrives_gets_none_of_it(bot, pipeline,
                                                                        monkeypatch):
    monkeypatch.setattr(settings, "MCP_ANSWER_WAIT_SECONDS", 40.0)
    pipeline.gate.clear()
    job_id = bot.ask()

    def revoke_then_finish():
        mcp_tokens.revoke_token(bot.row["id"], ADMIN)
        pipeline.gate.set()

    threading.Timer(0.5, revoke_then_finish).start()
    _is_401(bot.call_raw("get_answer", {"job_id": job_id}, emails=[OWNER]))


def test_the_token_is_verified_once_more_before_a_finished_answer(bot, monkeypatch, logs):
    """Valid when the request and its body arrived (two looks), not any more
    when the answer is handed out."""
    job_id = bot.ask()
    bot.wait(job_id)
    real, looks = mcp_tokens.verify, []

    def verify(raw):
        looks.append(1)
        return real(raw) if len(looks) <= 2 else None

    logs.clear()
    with monkeypatch.context() as m:
        m.setattr(mcp_tokens, "verify", verify)
        r = bot.call_raw("get_answer", {"job_id": job_id}, emails=[OWNER])
    assert len(looks) >= 3, looks
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert "ANSWER" not in r.text and "jsonrpc" not in r.json()
    lines = _call_lines(logs)
    assert len(lines) == 1, lines
    assert not re.search(r"\boutcome=(done|running|ok)\b", lines[0]), lines
    # a valid token still reads it
    assert not bot.wait(job_id).get("isError")


def test_the_tokens_later_checks_run_off_the_event_loop(bot, monkeypatch):
    job_id = bot.ask()
    bot.wait(job_id)
    real, threads = mcp_tokens.verify, []

    def verify(raw):
        threads.append(threading.get_ident())
        return real(raw)

    monkeypatch.setattr(mcp_tokens, "verify", verify)

    async def go(send):
        await send("tools/call", {"name": "get_answer", "arguments": {"job_id": job_id}})
        return threading.get_ident()

    loop_thread = _async_calls(bot, go)
    assert len(threads) >= 3, "the token is checked on arrival, after the body, before delivery"
    assert loop_thread not in threads


@pytest.mark.parametrize("when", ["running", "done"])
def test_a_share_removed_after_ask_hides_the_answer(bot, world, pipeline, when):
    """On the web the history is refused once the share is gone."""
    if when == "running":
        pipeline.gate.clear()
    job_id = bot.ask(email=FRIEND)
    if when == "done":
        assert not bot.wait(job_id, email=FRIEND).get("isError")
    else:
        _until(lambda: pipeline.calls, "the turn to start")
    world.remove_share_recipient(FRIEND)
    pipeline.gate.set()
    _refusal(bot, job_id, FRIEND)
    # shared again: the same job is readable
    world.add_share_recipients([FRIEND])
    assert not bot.wait(job_id, email=FRIEND).get("isError")


def test_a_share_removed_while_the_call_waits_hides_the_answer(bot, world, pipeline,
                                                               monkeypatch):
    monkeypatch.setattr(settings, "MCP_ANSWER_WAIT_SECONDS", 30.0)
    pipeline.gate.clear()
    job_id = bot.ask(email=FRIEND)
    _until(lambda: pipeline.calls, "the turn to start")

    def unshare():
        world.remove_share_recipient(FRIEND)
        pipeline.gate.set()

    threading.Timer(0.5, unshare).start()
    res = bot.call("get_answer", {"job_id": job_id}, email=FRIEND)
    assert error_text(res) == NOT_FOUND
    assert "ANSWER" not in json.dumps(res)


def test_a_deleted_chat_hides_the_answer(bot, world, tmp_path):
    import shutil
    job_id = bot.ask()
    bot.wait(job_id)
    shutil.rmtree(tmp_path / "chatdata" / CHAT)
    _refusal(bot, job_id)
    assert not (tmp_path / "chatdata" / CHAT).exists(), "reading re-created the chat folder"


def test_an_account_removed_before_the_turn_leaves_nothing(jobs, world, pipeline, tmp_path):
    assert local_store.AuthStore().remove_user(FRIEND) is True
    job = jobs.start("a" * 16, "Helpdesk bot", FRIEND, CHAT, QUESTION)
    _until(lambda: jobs.get(job.id, "a" * 16, FRIEND).state != "running", "the job")
    assert jobs.get(job.id, "a" * 16, FRIEND).state == "failed"
    assert not (tmp_path / "users" / FRIEND).exists()
    assert pipeline.calls == []


# ===========================================================================
# what the brain is told
# ===========================================================================
@pytest.fixture
def activity(monkeypatch):
    seen = []
    monkeypatch.setattr(brain_client, "post_activity",
                        lambda *a, **k: seen.append((a, k)) or {"ok": True})
    return seen


def _chart(n, **extra):
    ev = {"partial": True, "phase": 1, "image_base64": f"<div>plotly {n}</div>",
          "answer": f"Chart {n}.", "code": f"fig = chart_{n}", "chart_n": n,
          "chart_total": 2, "usage": {}}
    ev.update(extra)
    return ev


DONE = {"done": True, "partial": False, "combined_answer": "All.", "display_answer": "All.",
        "combined_codes": ["fig = chart_1", "fig = chart_2"], "total_usage": {},
        "narrative_view": None}


def _plots(activity):
    """The plot_generated events, positional or not."""
    out = []
    for a, k in activity:
        names = ("event", "email", "payload")
        got = dict(zip(names, a))
        got.update({names[i]: v for i, v in enumerate(k.values(), start=len(a))})
        if got["event"] == "plot_generated":
            out.append((got["email"], got["payload"]))
    return out


def test_one_activity_event_per_chart_like_the_web(bot, pipeline, activity, content):
    pipeline.events = [_chart(1), {"caption": True, "index": 1, "text": "c"},
                       {"partial": True, "phase": 1, "tables": [
                           {"columns": ["a"], "rows": [{"a": 1}], "total_rows": 1}],
                        "table_codes": ["RESULT = 1"], "table_result_keys": [None]},
                       _chart(2), DONE]
    bot.wait(bot.ask())
    conv = content.calls[0]["conv_id"]
    assert _plots(activity) == [
        (OWNER, {"chat_id": CHAT, "conv_id": conv, "chart_n": 1}),
        (OWNER, {"chat_id": CHAT, "conv_id": conv, "chart_n": 2})]


def test_a_single_chart_posts_one_event_without_a_number(bot, pipeline, activity, content):
    pipeline.events = [_single(image_base64="<div>plotly</div>", code="fig = 1")]
    bot.wait(bot.ask())
    assert _plots(activity) == [
        (OWNER, {"chat_id": CHAT, "conv_id": content.calls[0]["conv_id"]})]


def test_a_text_or_table_answer_posts_none(bot, pipeline, activity):
    pipeline.events = [_single(table={"columns": ["a"], "rows": [{"a": 1}], "total_rows": 1})]
    bot.wait(bot.ask())
    assert _plots(activity) == []


def test_a_failing_activity_call_costs_nothing(bot, pipeline, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(brain_client, "post_activity", boom)
    pipeline.events = [_chart(1), _chart(2), DONE]
    res = bot.wait(bot.ask())
    assert state_of(res) == "done"


def test_no_activity_carries_the_question_or_the_answer(bot, pipeline, activity):
    pipeline.events = [_chart(1), _chart(2), DONE]
    bot.wait(bot.ask())
    assert QUESTION not in repr(activity) and "Chart 1" not in repr(activity)


# ===========================================================================
# the log
# ===========================================================================
def _call_lines(logs):
    return [ln for ln in logs if re.search(r"\bMCP_CALL\b", ln)]


def test_one_line_per_tool_call(bot, logs):
    bot.call("list_chats")
    job_id = bot.ask()
    bot.wait(job_id)
    bot.call("get_answer", {"job_id": "0" * 32})
    lines = _call_lines(logs)
    assert len(lines) == 4, lines
    for line, tool in zip(lines, ("list_chats", "ask", "get_answer", "get_answer")):
        assert "Helpdesk bot" in line and OWNER in line
        assert re.search(rf"\b{tool}\b", line), line
        assert re.search(r"\bms=\d+", line), line
        assert re.search(r"\boutcome=\w+", line), line
    assert CHAT in lines[1] and job_id in lines[1]
    assert job_id in lines[2]
    outcomes = [re.search(r"\boutcome=(\w+)", ln).group(1) for ln in lines]
    assert outcomes[2] != outcomes[3], "a refused read must be told apart in the log"


def test_refused_calls_have_their_line_too(bot, logs):
    bot.call("list_chats", emails=[])
    bot.call("ask", {"chat_id": FOREIGN, "question": QUESTION})
    bot.call("ask", {"chat_id": CHAT, "question": ""})
    lines = _call_lines(logs)
    assert len(lines) == 3, lines
    ok = re.compile(r"\boutcome=ok\b")
    assert not [ln for ln in lines if ok.search(ln)], lines


def test_the_question_is_logged_only_as_the_web_logs_it(bot, logs):
    question = "MARKQ " + "z" * 400 + " TAILMARK"
    bot.wait(bot.ask(question))
    hits = [ln for ln in logs if "MARKQ" in ln]
    # like the web: ONE request line (chat_turn writes it), 120 characters
    assert len(hits) == 1 and "_REQ" in hits[0], hits
    assert "z" * 121 not in hits[0] and "TAILMARK" not in hits[0]
    assert not [ln for ln in _call_lines(logs) if "MARKQ" in ln]


def test_no_answer_table_or_image_in_the_log(bot, pipeline, logs):
    pipeline.events = [_single("SECRETANSWER", table={
        "columns": ["a"], "rows": [{"a": "SECRETCELL"}], "total_rows": 1},
        image_base64="<div>SECRETIMAGE</div>")]
    bot.wait(bot.ask())
    for mark in ("SECRETANSWER", "SECRETCELL", "SECRETIMAGE", "aGk="):
        assert not [ln for ln in logs if mark in ln], mark


def test_no_request_value_can_forge_a_log_line(bot, logs):
    evil = "x\n[sid=mcp] MCP_CALL FORGED"
    hostile = Bot(name="Second bot")
    hostile.call("ask", {"chat_id": evil, "question": evil})
    hostile.call("ask", {"chat_id": CHAT, "question": evil})
    hostile.call("get_answer", {"job_id": evil + "y" * 50_000})
    hostile.call_raw(evil, {}, emails=[OWNER])
    hostile.call_raw("z" * 50_000, {}, emails=[OWNER])
    assert not [ln for ln in logs if "\n" in ln], logs
    assert not [ln for ln in logs if len(ln) > 4000], "an unbounded value was logged"
