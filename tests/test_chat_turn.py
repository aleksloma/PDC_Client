"""MCP stage 2 — `chat_turn`: run a question and save the turn, without a
browser (MCP_INTEGRATION_PROMPT.txt 3.5 and part 5, plan section 3).

    prepare_turn(email, chat_id, question, *, conv_id=None,
                 new_conversation=None, sid=None) -> PreparedTurn | TurnRefusal
    run_prepared_turn(turn, *, on_event=None, title="auto") -> TurnResult

What the saved record looks like is pinned by tests/test_chat_turn_drift.py
(the same events through the browser path and this one). Here: the order of
the gates, the refusals and that a refused question leaves NOTHING behind,
what the planner is handed, the contract of `on_event` / `title` / the
result, and the shape of the module itself (no Request, no session, no SSE,
every `routes.chat` helper read at call time).

Reading of the brief: `persisted` is True only when a record was appended;
a turn without a record (empty generator) has `record=None, persisted=False`.
A `no_conversation` refusal covers a factory answering None / "" and no
conversation given at all.

Offline: DATA_ROOT is tmp_path; `run_chat_multi_plot` is a scripted generator.
"""
import ast
import copy
import dataclasses
import importlib
import inspect
import json
import logging
import secrets
from pathlib import Path

import pandas as pd
import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import local_store
from conftest import JSON_HEADERS
from settings import settings

OWNER = "owner@acme.com"
CHAT = "c_chatturn0000001"
EMPTY = "c_chatturnempty01"
QUESTION = "How did sales go?"
TCODE = "RESULT = dfs['sales.csv'].groupby('region')['amount'].sum().reset_index()"
TABLE = {"columns": ["region", "amount"],
         "rows": [{"region": "North", "amount": 3}], "total_rows": 1}
ROOT = Path(__file__).resolve().parent.parent


def _chat_mod():
    return importlib.import_module("routes.chat")


@pytest.fixture
def ct():
    return importlib.import_module("chat_turn")


def _make_chat(chat_id, *, files=True):
    store = local_store.ChatDataStore(chat_id)
    if files:
        pd.DataFrame({"region": ["North", "South"], "amount": [1, 2]}).to_csv(
            store.files_dir / "sales.csv", index=False)
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["title"] = "Sales"
    meta["files"] = [{"file_name": "sales.csv", "file_description": "sales",
                      "schema": {"fields": {"region": {}, "amount": {}}}}] if files else []
    store.write_meta(meta)
    return store


@pytest.fixture
def store(tmp_path, monkeypatch):
    chat_mod = _chat_mod()
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "SLOW_ANSWER_NOTICE_SECONDS", 0)
    local_store._DATAFRAME_CACHE.invalidate()
    local_store.AuthStore().ensure_user(OWNER)
    chat_mod._INFLIGHT_CODES.clear()
    chat_mod._INFLIGHT_SQL.clear()
    yield _make_chat(CHAT)
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def client(store):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(_chat_mod().router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = "s_" + secrets.token_hex(8)
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}", headers=JSON_HEADERS)
    return tc


@pytest.fixture
def gen(monkeypatch):
    """A scripted pipeline: `gen.events` is yielded (copies), `gen.raises`
    raised afterwards; `gen.calls` records the keyword arguments."""
    class _Gen:
        events: list = []
        raises = None
        calls: list = []

    g = _Gen()
    g.calls, g.events = [], []

    def fake(*a, **kw):
        assert not a, "run_chat_multi_plot is called with keywords, like the web"
        g.calls.append({**kw, "history_rows": copy.deepcopy(kw["history_rows"])})

        def _run():
            for e in copy.deepcopy(g.events):
                yield e
            if g.raises is not None:
                raise g.raises
        return _run()

    monkeypatch.setattr(_chat_mod().run_chat_local, "run_chat_multi_plot", fake)
    return g


@pytest.fixture
def titles(monkeypatch):
    seen = []

    def start(email, chat_id, conv_id, question, answer):
        seen.append((email, chat_id, conv_id, question, answer))

    monkeypatch.setattr(_chat_mod(), "_start_title_generation", start)
    return seen


def _single(text="the answer", **extra):
    res = {"text": text, "image_base64": None, "table": None, "code": None, "usage": {}}
    res.update(extra)
    return {"single_response": True, "result": res}


def _chart(n, **extra):
    ev = {"partial": True, "phase": 1, "image_base64": f"<div>plotly {n}</div>",
          "answer": f"Chart {n}.", "code": f"fig = chart_{n}", "chart_n": n,
          "chart_total": 2, "usage": {}}
    ev.update(extra)
    return ev


def _done(combined="All.", codes=("fig = chart_1", "fig = chart_2")):
    return {"done": True, "partial": False, "combined_answer": combined,
            "display_answer": combined, "combined_codes": list(codes),
            "total_usage": {}, "narrative_view": None}


def _conv(store, rows=(), email=OWNER):
    conv_id = store.new_conversation("t")
    local_store.AuthStore().record_conversation(email, store.chat_id, conv_id, "t")
    for row in rows:
        store.append_history(conv_id, row)
    return conv_id


def _snapshot(tmp_root):
    """Every file below DATA_ROOT that a turn could create or change."""
    out = {}
    for p in Path(tmp_root).rglob("*"):
        rel = p.relative_to(tmp_root).as_posix()
        if p.is_file() and ("conversations" in rel or rel.startswith("users/")):
            out[rel] = p.read_bytes()
    return out


class Factory:
    """A `new_conversation` callable that records being called."""

    def __init__(self, answer="create"):
        self.answer, self.stores = answer, []

    def __call__(self, store):
        self.stores.append(store)
        return _conv(store) if self.answer == "create" else self.answer


def _busy(store):
    meta = store.read_meta()
    meta["auto_analysis"] = {"status": "processing"}
    store.write_meta(meta)


def _run(ct, store, gen, events, *, conv_id=None, **kw):
    gen.events = events
    conv_id = conv_id or _conv(store)
    turn = ct.prepare_turn(OWNER, store.chat_id, QUESTION, conv_id=conv_id)
    assert isinstance(turn, ct.PreparedTurn), turn
    return ct.run_prepared_turn(turn, **kw), conv_id


# ===========================================================================
# the module
# ===========================================================================
def test_the_public_names_and_the_result_fields(ct):
    for name in ("TurnRefusal", "PreparedTurn", "TurnResult"):
        assert dataclasses.is_dataclass(getattr(ct, name)), name
    assert [f.name for f in dataclasses.fields(ct.TurnRefusal)] == \
        ["kind", "status_code", "body"]
    assert {f.name for f in dataclasses.fields(ct.TurnResult)} >= \
        {"record", "error_text", "error_key", "cancelled", "persisted", "conv_id", "sid"}


def test_the_signatures_take_plain_arguments(ct):
    prep = inspect.signature(ct.prepare_turn).parameters
    assert list(prep) == ["email", "chat_id", "question", "conv_id",
                          "new_conversation", "sid"]
    assert all(prep[n].kind is inspect.Parameter.KEYWORD_ONLY and prep[n].default is None
               for n in ("conv_id", "new_conversation", "sid"))
    run = inspect.signature(ct.run_prepared_turn).parameters
    assert list(run) == ["turn", "on_event", "title"]
    assert run["on_event"].kind is run["title"].kind is inspect.Parameter.KEYWORD_ONLY
    assert run["on_event"].default is None and run["title"].default == "auto"
    # plain, blocking functions: the caller decides the thread
    for fn in (ct.prepare_turn, ct.run_prepared_turn):
        assert not inspect.iscoroutinefunction(fn) and not inspect.isgeneratorfunction(fn)


def _tree(ct):
    return ast.parse(Path(ct.__file__).read_text(encoding="utf-8"))


def test_the_module_sits_in_the_repository_root(ct):
    assert Path(ct.__file__).resolve() == ROOT / "chat_turn.py"


def _imports(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            for a in n.names:
                yield n, a.name
        elif isinstance(n, ast.ImportFrom):
            yield n, n.module or ""


def test_the_module_knows_no_request_session_or_sse(ct):
    tree = _tree(ct)
    web = [m for _n, m in _imports(tree)
           if m.split(".")[0] in ("fastapi", "starlette", "asyncio", "queue", "threading")]
    assert not web, f"chat_turn imports {web}"
    bad = {"Request", "session", "StreamingResponse", "EventSourceResponse",
           "call_soon_threadsafe", "put_nowait"}
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} \
        | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} \
        | {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
    assert not (bad & used), bad & used


def test_routes_chat_helpers_are_read_at_call_time(ct):
    """`import routes.chat as _chat`, never `from routes.chat import x` at
    module level and no `_chat.<helper>` evaluated outside a function: a
    monkeypatch then reaches both paths, and `routes/chat.py` can import this
    module one day without an import cycle."""
    tree = _tree(ct)
    alias = None
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "") != "routes.chat", \
                f"line {node.lineno}: from routes.chat import … at module level"
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "routes.chat":
                    alias = a.asname
    assert alias == "_chat", "expected `import routes.chat as _chat` at module level"

    def module_level(node):
        """Everything evaluated at import: function bodies excluded, their
        decorators and defaults included."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                args = child.args
                for d in list(args.defaults) + [x for x in args.kw_defaults if x]:
                    yield from ast.walk(d)
                for d in getattr(child, "decorator_list", []):
                    yield from ast.walk(d)
                continue
            yield child
            yield from module_level(child)

    early = [n.lineno for n in module_level(tree)
             if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
             and n.value.id == "_chat"]
    assert not early, f"_chat.<name> read at import time on lines {early}"


def test_no_helper_of_routes_chat_is_written_again(ct):
    """Reuse, not a third copy: the record builders and table persistence are
    called, never redefined here."""
    defs = {n.name for n in ast.walk(_tree(ct))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    ours = defs & {n for n, v in vars(_chat_mod()).items() if inspect.isfunction(v)
                   and v.__module__ == "routes.chat"}
    assert not ours, f"helpers of routes/chat.py written again: {sorted(ours)}"


HELPERS = {
    "done": ["_auto_analysis_busy_response", "_drop_uncovered_db_keys",
             "_event_table_fields", "_persist_full_table", "_event_codes", "_event_sql",
             "_inflight_add", "_inflight_discard", "_mark_generating",
             "_unmark_generating", "_is_cancelled", "_clear_cancel",
             "_build_ai_history_record", "_title_due", "_start_title_generation",
             "_answer_for_title"],
    "stopped": ["_build_stopped_record", "_narr_event_view"],
}


@pytest.mark.parametrize("turn_kind,name", [(k, n) for k, v in HELPERS.items() for n in v])
def test_each_helper_is_the_one_of_routes_chat(ct, store, gen, monkeypatch, turn_kind, name):
    chat_mod = _chat_mod()
    real = getattr(chat_mod, name)
    hits = []

    def spy(*a, **k):
        hits.append(1)
        return None if name == "_start_title_generation" else real(*a, **k)

    monkeypatch.setattr(chat_mod, name, spy)
    if turn_kind == "stopped":
        monkeypatch.setattr(chat_mod, "_is_cancelled", lambda conv_id: True)
    if name != "_start_title_generation":
        monkeypatch.setattr(chat_mod, "_start_title_generation", lambda *a, **k: None)
    events = [_chart(1), {"partial": True, "phase": 1, "tables": [dict(TABLE)],
                          "table_codes": [TCODE], "table_result_keys": [None]}, _done()]
    _run(ct, store, gen, events)
    assert hits, f"chat_turn does not call routes.chat.{name} at call time"


# ===========================================================================
# prepare_turn: the order
# ===========================================================================
@pytest.fixture
def trace(monkeypatch):
    """The steps, in order, whichever path takes them."""
    chat_mod, steps = _chat_mod(), []

    def spy(owner, name, label=None, note=None):
        real = getattr(owner, name)

        def wrapped(*a, **k):
            steps.append((label or name, note(a, k)) if note else (label or name))
            return real(*a, **k)
        monkeypatch.setattr(owner, name, wrapped)

    cds = local_store.ChatDataStore
    spy(chat_mod, "_auto_analysis_busy_response")
    spy(cds, "load_dataframes", note=lambda a, k: k.get("include_live"))
    spy(chat_mod, "_empty_dataset_response")
    spy(cds, "schema_docs")
    spy(chat_mod, "_drop_uncovered_db_keys", note=lambda a, k: a[0] if a else k.get("email"))
    spy(local_store, "ensure_chat_profiles")
    spy(cds, "new_conversation")
    spy(local_store.AuthStore, "record_conversation")
    spy(cds, "get_history")
    spy(cds, "append_history", note=lambda a, k: (a[-1] if a else k["message"])["role"])
    return steps


ORDER = ["_auto_analysis_busy_response", ("load_dataframes", True), "schema_docs",
         ("_drop_uncovered_db_keys", OWNER), "ensure_chat_profiles"]


def test_prepare_order_with_a_conversation_is_the_webs(ct, store, client, gen, trace):
    conv_id = _conv(store)
    gen.events = [_single()]
    trace.clear()
    r = client.post(f"/api/chat/{CHAT}/chat/stream",
                    json={"question": QUESTION, "conv_id": conv_id}, headers=JSON_HEADERS)
    assert r.status_code == 200
    web = trace[:trace.index(("append_history", "human")) + 1]
    assert web == ORDER + ["get_history", ("append_history", "human")], web
    trace.clear()
    ct.prepare_turn(OWNER, CHAT, QUESTION, conv_id=conv_id)
    assert trace == web
    assert len(gen.calls) == 1, "prepare_turn must not start the pipeline"


def test_the_factory_runs_after_every_gate_and_before_the_history(ct, store, gen, trace):
    class Traced(Factory):
        def __call__(self, st):
            trace.append("factory")
            return super().__call__(st)

    factory = Traced()
    turn = ct.prepare_turn(OWNER, CHAT, QUESTION, new_conversation=factory)
    assert isinstance(turn, ct.PreparedTurn)
    assert trace == ORDER + ["factory", "new_conversation", "record_conversation",
                             "get_history", ("append_history", "human")], trace
    assert [s.chat_id for s in factory.stores] == [CHAT]


def test_a_given_conversation_wins_over_the_factory(ct, store, gen):
    conv_id, factory = _conv(store), Factory()
    gen.events = [_single()]
    turn = ct.prepare_turn(OWNER, CHAT, QUESTION, conv_id=conv_id,
                           new_conversation=factory)
    assert factory.stores == []
    assert ct.run_prepared_turn(turn).conv_id == conv_id


def test_prepare_appends_exactly_one_human_row(ct, store, gen):
    conv_id = _conv(store, [{"role": "ai", "content": "Welcome.", "ts": 1.0}])
    ct.prepare_turn(OWNER, CHAT, QUESTION, conv_id=conv_id)
    rows = store.get_history(conv_id)
    assert list(rows[-1]) == ["role", "content", "ts"]
    assert rows[-1]["role"] == "human" and rows[-1]["content"] == QUESTION
    assert isinstance(rows[-1]["ts"], float)
    assert len(rows) == 2


# ===========================================================================
# prepare_turn: refusals leave nothing behind
# ===========================================================================
def _web_refusal(client, chat_id=CHAT):
    r = client.post(f"/api/chat/{chat_id}/chat/stream", json={"question": QUESTION},
                    headers=JSON_HEADERS)
    return r.status_code, r.json()


@pytest.mark.parametrize("given", ["conv_id", "factory"])
def test_auto_analytics_busy_refuses_like_the_web(ct, store, client, gen, tmp_path, given):
    conv_id, factory = _conv(store), Factory()
    _busy(store)
    status, body = _web_refusal(client)
    assert status == 409 and body["busy"] == "auto_analysis"
    before = _snapshot(tmp_path)
    out = ct.prepare_turn(OWNER, CHAT, QUESTION,
                          **({"conv_id": conv_id} if given == "conv_id"
                             else {"new_conversation": factory}))
    assert isinstance(out, ct.TurnRefusal), out
    assert (out.kind, out.status_code, out.body) == ("auto_analysis_busy", status, body)
    assert _snapshot(tmp_path) == before
    assert factory.stores == [] and gen.calls == []


@pytest.mark.parametrize("given", ["conv_id", "factory"])
def test_an_empty_dataset_refuses_like_the_web(ct, store, client, gen, tmp_path, given):
    empty = _make_chat(EMPTY, files=False)
    conv_id, factory = _conv(empty), Factory()
    status, body = _web_refusal(client, EMPTY)
    assert (status, body) == (400, {"error": "Chat dataset is empty."})
    before = _snapshot(tmp_path)
    out = ct.prepare_turn(OWNER, EMPTY, QUESTION,
                          **({"conv_id": conv_id} if given == "conv_id"
                             else {"new_conversation": factory}))
    assert isinstance(out, ct.TurnRefusal), out
    assert (out.kind, out.status_code, out.body) == ("empty_dataset", status, body)
    assert _snapshot(tmp_path) == before
    assert factory.stores == [] and gen.calls == []


def test_missing_database_tables_are_named_like_on_the_web(ct, store, client, gen):
    empty = _make_chat(EMPTY, files=False)
    meta = empty.read_meta()
    meta["files"] = [{"file_name": "Loans", "source": "database",
                      "db": {"table_id": "aa11bb22cc33dd44", "display_name": "Loans"},
                      "schema": {"fields": {}}}]
    empty.write_meta(meta)
    status, body = _web_refusal(client, EMPTY)
    assert body["code"] == "DB_TABLES_MISSING"
    out = ct.prepare_turn(OWNER, EMPTY, QUESTION, new_conversation=Factory())
    assert (out.kind, out.status_code, out.body) == ("empty_dataset", status, body)


@pytest.mark.parametrize("answer", [None, "", "no-factory"])
def test_no_conversation_refuses_and_writes_no_human_row(ct, store, gen, tmp_path, answer):
    before = _snapshot(tmp_path)
    out = ct.prepare_turn(
        OWNER, CHAT, QUESTION,
        new_conversation=None if answer == "no-factory" else Factory(answer))
    assert isinstance(out, ct.TurnRefusal), out
    assert out.kind == "no_conversation"
    assert isinstance(out.status_code, int) and out.status_code >= 400
    assert isinstance(out.body, dict) and isinstance(out.body.get("error"), str) \
        and out.body["error"]
    assert _snapshot(tmp_path) == before
    assert gen.calls == []


def test_a_profile_failure_never_blocks_the_turn(ct, store, gen, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("profiles")

    monkeypatch.setattr(local_store, "ensure_chat_profiles", boom)
    res, _conv_id = _run(ct, store, gen, [_single()])
    assert gen.calls[0]["dataset_profile"] == {}
    assert res.persisted is True


# ===========================================================================
# what the planner is handed
# ===========================================================================
def test_the_pipeline_arguments(ct, store, gen):
    earlier = [{"role": "ai", "content": "Welcome.", "ts": 1.0},
               {"role": "human", "content": "before", "ts": 2.0},
               {"role": "ai", "content": "then", "code": "RESULT = 1", "ts": 3.0}]
    conv_id = _conv(store, earlier)
    gen.events = [_single()]
    turn = ct.prepare_turn(OWNER, CHAT, QUESTION, conv_id=conv_id, sid="sid-given")
    ct.run_prepared_turn(turn)
    kw = gen.calls[0]
    # the earlier rows, and NOT the current question (it travels as `question`)
    assert kw["history_rows"] == earlier
    assert kw["question"] == QUESTION
    assert kw["user_email"] == OWNER and kw["sid"] == "sid-given"
    assert list(kw["dfs"]) == ["sales.csv"] and list(kw["schema_docs"]) == ["sales.csv"]
    assert set(kw["dataset_profile"]) == {"sales.csv"}
    assert set(kw) - {"on_progress"} == {"sid", "dfs", "schema_docs", "question",
                                         "history_rows", "user_email", "dataset_profile"}


def test_a_sid_is_made_when_none_is_given(ct, store, gen):
    res, _ = _run(ct, store, gen, [_single()])
    res2, _ = _run(ct, store, gen, [_single()])
    assert res.sid and res.sid == gen.calls[0]["sid"]
    assert res.sid != res2.sid


def test_a_second_turn_sees_the_first_as_history(ct, store, gen):
    _res, conv_id = _run(ct, store, gen, [_single("first answer", code=TCODE)])
    gen.events = [_single("second")]
    turn = ct.prepare_turn(OWNER, CHAT, "and by month?", conv_id=conv_id)
    ct.run_prepared_turn(turn)
    seen = [(r["role"], r["content"]) for r in gen.calls[1]["history_rows"]]
    assert seen == [("human", QUESTION), ("ai", "first answer")]


# ===========================================================================
# run_prepared_turn: the result
# ===========================================================================
def test_the_result_of_an_answered_turn(ct, store, gen):
    res, conv_id = _run(ct, store, gen, [_single("the answer", table=dict(TABLE), code=TCODE)])
    assert isinstance(res, ct.TurnResult)
    assert (res.error_text, res.error_key, res.cancelled, res.persisted) == \
        (None, None, False, True)
    assert res.conv_id == conv_id
    stored = store.get_history(conv_id)[-1]
    assert json.loads(json.dumps(local_store._json_safe(res.record))) == stored
    assert stored["content"] == "the answer" and stored["full_table_key"]


def _errors():
    m = _chat_mod()
    return {
        "tenant": (m.TenantRevokedError("x"),
                   "Service unavailable. Please contact your administrator.", None),
        "analysis": (m.run_chat_local.AnalysisFailedError("llm_unavailable"),
                     m.ANALYSIS_FAILED_TEXT, m.ANALYSIS_FAILED_KEY),
        "timeout": (m.BrainTimeoutError("x"), m.BRAIN_TIMEOUT_TEXT, m.BRAIN_TIMEOUT_KEY),
        "brain": (m.BrainError("x"), "Analysis service is temporarily unavailable.", None),
        "generic": (ValueError("x"),
                    "Something went wrong while processing your request.", None),
    }


@pytest.mark.parametrize("name", ["tenant", "analysis", "timeout", "brain", "generic"])
def test_the_result_of_a_failed_turn(ct, store, gen, titles, name):
    exc, text, key = _errors()[name]
    gen.raises = exc
    seen = []
    res, conv_id = _run(ct, store, gen, [_chart(1)],
                        on_event=lambda tag, payload: seen.append((tag, payload)))
    assert (res.error_text, res.error_key, res.cancelled, res.persisted) == \
        (text, key, False, True)
    row = store.get_history(conv_id)[-1]
    assert list(row) == ["role", "content", "ts"] and row["content"] == text
    assert seen[-1] == ("error", {"error": text, "error_key": key} if key else text)
    assert [t for t, _p in seen] == ["event", "error"]
    assert titles == []


def test_an_error_text_never_carries_the_exception(ct, store, gen, caplog):
    gen.raises = KeyError("column 'SECRET'\nFORGED RECORD")
    with caplog.at_level(logging.INFO):
        res, conv_id = _run(ct, store, gen, [])
    assert "SECRET" not in json.dumps(store.get_history(conv_id))
    assert "SECRET" not in res.error_text
    lines = [r.getMessage() for r in caplog.records if "CHAT_THREAD_ERROR" in r.getMessage()]
    assert len(lines) == 1 and "KeyError" in lines[0], lines
    assert "\n" not in lines[0], "an untrusted line break reached the log"


def test_a_stopped_turn(ct, store, gen, titles, monkeypatch, caplog):
    chat_mod = _chat_mod()
    monkeypatch.setattr(chat_mod, "_is_cancelled", lambda conv_id: True)
    seen = []
    with caplog.at_level(logging.INFO):
        res, conv_id = _run(ct, store, gen, [_chart(1), _chart(2), _done()],
                            on_event=lambda tag, payload: seen.append((tag, payload)))
    assert res.cancelled is True and res.error_text is None and res.persisted is True
    row = store.get_history(conv_id)[-1]
    assert row["stopped"] is True and row["image_base64"] == "<div>plotly 1</div>"
    # the synthetic done reached the consumer, after the one chart
    assert [p.get("done") for _t, p in seen] == [None, True]
    assert seen[-1][1]["display_answer"] == "Chart 1."
    assert seen[-1][1]["combined_codes"] == ["fig = chart_1"]
    assert titles == []
    assert [r for r in caplog.records if "CHAT_STOP_AT_BOUNDARY" in r.getMessage()]


def test_a_stop_during_the_last_event_still_stops(ct, store, gen, monkeypatch):
    """The cancel check runs after EVERY event, the single answer included."""
    chat_mod = _chat_mod()
    monkeypatch.setattr(chat_mod, "_is_cancelled", lambda conv_id: True)
    res, conv_id = _run(ct, store, gen, [_single("late")])
    assert res.cancelled is True
    assert store.get_history(conv_id)[-1]["content"] == "Response stopped by user."


def test_a_turn_without_a_record_saves_nothing(ct, store, gen):
    res, conv_id = _run(ct, store, gen, [])
    assert res.record is None and res.persisted is False and res.error_text is None
    assert [r["role"] for r in store.get_history(conv_id)] == ["human"]


def _break_ai_append(monkeypatch):
    real = local_store.ChatDataStore.append_history

    def broken(self, conv_id, message):
        if message.get("role") == "ai":
            raise OSError("disk full about 'SECRET'\nFORGED")
        return real(self, conv_id, message)

    monkeypatch.setattr(local_store.ChatDataStore, "append_history", broken)


def test_a_persistence_failure_is_logged_and_reported(ct, store, gen, titles, monkeypatch,
                                                      caplog):
    chat_mod = _chat_mod()
    conv_id = _conv(store)
    _break_ai_append(monkeypatch)
    with caplog.at_level(logging.INFO):
        res, _ = _run(ct, store, gen, [_single("x", code=TCODE)], conv_id=conv_id)
    assert res.persisted is False and res.cancelled is False
    assert titles == []
    lines = [r.getMessage() for r in caplog.records if "CHAT_PERSIST_ERROR" in r.getMessage()]
    assert len(lines) == 1 and "\n" not in lines[0], lines
    # the turn is over for everyone else too
    assert not chat_mod._is_generating(conv_id)
    assert CHAT not in chat_mod._INFLIGHT_CODES


def test_generating_and_in_flight_states_around_the_save(ct, store, gen, monkeypatch):
    """Marked while running, unmarked only AFTER the row is on disk."""
    chat_mod, at_save = _chat_mod(), []
    real = local_store.ChatDataStore.append_history

    def spy(self, conv_id, message):
        if message.get("role") == "ai":
            at_save.append((chat_mod._is_generating(conv_id),
                            chat_mod._inflight_has(CHAT, "fig = chart_1")))
        return real(self, conv_id, message)

    monkeypatch.setattr(local_store.ChatDataStore, "append_history", spy)
    during = []

    def on_event(tag, payload):
        during.append((chat_mod._is_generating(conv_id),
                       chat_mod._inflight_has(CHAT, "fig = chart_1")))

    conv_id = _conv(store)
    _run(ct, store, gen, [_chart(1), _done()], conv_id=conv_id, on_event=on_event)
    assert during == [(True, True), (True, True)]
    assert at_save == [(True, True)]
    assert not chat_mod._is_generating(conv_id) and not chat_mod._is_cancelled(conv_id)
    assert CHAT not in chat_mod._INFLIGHT_CODES and CHAT not in chat_mod._INFLIGHT_SQL


@pytest.mark.parametrize("stale", ["before", "during"])
def test_the_stop_flag_never_outlives_the_turn(ct, store, gen, stale):
    chat_mod, conv_id = _chat_mod(), _conv(store)
    if stale == "before":
        chat_mod._request_cancel(conv_id)
    res, _ = _run(ct, store, gen, [_chart(1), _chart(2), _done()], conv_id=conv_id,
                  on_event=(None if stale == "before" else
                            lambda t, p: chat_mod._request_cancel(conv_id)))
    assert res.cancelled is (stale == "during")
    assert not chat_mod._is_cancelled(conv_id)


# ===========================================================================
# on_event
# ===========================================================================
def test_table_keys_are_on_the_event_and_on_disk_before_on_event(ct, store, gen):
    seen = []

    def on_event(tag, payload):
        keys = [payload["full_table_key"]] if payload.get("full_table_key") else []
        keys += payload.get("full_table_keys") or []
        for key in keys:
            assert (store.conversations_dir / "full" / f"{key}.json").is_file(), key
        seen.append(len(keys))

    _run(ct, store, gen, [
        {"partial": True, "phase": 1, "tables": [dict(TABLE), dict(TABLE)],
         "table_codes": [TCODE, TCODE], "table_result_keys": [None, None]},
        {**_done(), "tables": [dict(TABLE)], "table_codes": [TCODE]}], on_event=on_event)
    _run(ct, store, gen, [_single(table=dict(TABLE), code=TCODE)], on_event=on_event)
    assert seen == [2, 1, 1]


@pytest.mark.parametrize("case", ["answered", "failed", "stopped"])
def test_a_raising_on_event_is_swallowed(ct, store, gen, monkeypatch, case):
    """A broken consumer never costs the user the answer (the documented
    difference: the browser's consumer sits behind a queue)."""
    chat_mod, attempts = _chat_mod(), []

    def on_event(tag, payload):
        attempts.append(tag)
        raise RuntimeError("consumer broke")

    if case == "failed":
        gen.raises = chat_mod.BrainError("x")
    if case == "stopped":
        monkeypatch.setattr(chat_mod, "_is_cancelled", lambda conv_id: len(attempts) >= 2)
    res, conv_id = _run(ct, store, gen, [_chart(1), _chart(2)]
                        + ([] if case == "failed" else [_done("All.")]), on_event=on_event)
    assert res.persisted is True
    row = store.get_history(conv_id)[-1]
    if case == "answered":
        assert attempts == ["event"] * 3
        assert row["content"] == "All." and len(row["images"]) == 2
        assert res.error_text is None
    elif case == "failed":
        assert attempts == ["event", "event", "error"]
        assert row["content"] == res.error_text == \
            "Analysis service is temporarily unavailable."
    else:
        assert attempts == ["event"] * 3, "the synthetic done is emitted too"
        assert row["stopped"] is True and len(row["images"]) == 2


def test_without_on_event_the_turn_runs(ct, store, gen):
    res, conv_id = _run(ct, store, gen, [_chart(1), _chart(2), _done("All.")])
    assert res.persisted is True
    assert len(store.get_history(conv_id)[-1]["images"]) == 2


# ===========================================================================
# title
# ===========================================================================
def test_title_auto_follows_the_web_rule(ct, store, gen, titles):
    _res, conv_id = _run(ct, store, gen, [_single("first answer", code=TCODE)])
    assert titles == [(OWNER, CHAT, conv_id, QUESTION, "first answer")]
    # the second answered turn is not titled
    _run(ct, store, gen, [_single("second", code=TCODE)], conv_id=conv_id)
    assert len(titles) == 1


def test_title_never_starts_nothing(ct, store, gen, titles, monkeypatch):
    import brain_client
    calls = []
    monkeypatch.setattr(brain_client, "title", lambda *a, **k: calls.append(1) or {})
    _run(ct, store, gen, [_single("first answer", code=TCODE)], title="never")
    assert titles == [] and calls == []


def test_the_title_is_decided_on_the_rows_the_turn_started_from(ct, store, gen, titles):
    """An answer a concurrent turn wrote meanwhile does not take the title
    away (the rows before this turn decide, never a re-read)."""
    conv_id = _conv(store)

    def on_event(tag, payload):
        store.append_history(conv_id, {"role": "ai", "content": "other", "code": "x = 1",
                                       "ts": 1.0})

    _run(ct, store, gen, [_single("mine", code=TCODE)], conv_id=conv_id, on_event=on_event)
    assert len(titles) == 1


def test_a_title_failure_keeps_the_answer(ct, store, gen, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(_chat_mod(), "_start_title_generation", boom)
    with caplog.at_level(logging.INFO):
        res, conv_id = _run(ct, store, gen, [_single("a", code=TCODE)])
    assert res.persisted is True and store.get_history(conv_id)[-1]["content"] == "a"
    assert [r for r in caplog.records if "TITLE_TRIGGER_FAILED" in r.getMessage()]
    assert not [r for r in caplog.records if "CHAT_PERSIST_ERROR" in r.getMessage()]
