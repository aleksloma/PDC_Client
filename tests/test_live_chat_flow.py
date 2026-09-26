"""Live database tables in the chat flow.

A table registered in live mode has no parquet: the chat loads an EMPTY typed
placeholder under its df key, the planner is told which tables are live
(`live_tables`), and the client fetches the rows in the main app right before
the sandbox runs the Python — either the planner's own SELECT (`sql`) or, when
none was sent or the registration carries an admin row filter, a capped
default fetch. A failed SELECT goes through the ordinary retry loop with a
value-free `sql_error` (class + dialect + guard message only — never the
driver's text, which quotes literals). The answer remembers `sql`,
`live_truncated` and `live_rows`; the brain-side history rows never do.

Offline: the live table is a tmp-file sqlite database reached through the
hidden sqlite dialect; every brain call is stubbed on `run_chat_local`'s
`brain_client` binding; the executor is a fake that records the frames it
was handed. Nothing here touches the sandbox or the network.
"""
import json
import logging

import pandas as pd
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

import brain_client
import db_connector
import db_scheduler
import db_sources
import local_store
import result_backstop
import roles_store
import run_chat_local
import schema_builder
from settings import settings

ADMIN = "ladmin"
USER = "user@x.com"
CHAT = "c_livechat1"
KEY = "live t"
SNAP_KEY = "snap t"
SNAP_TID = "aa11bb22cc33dd44"
MARK = "ZQ_MARK_9"

# The planner's code shapes used throughout.
CODE_SUM = "RESULT = dfs['live t']['a'].sum()"
CODE_NO_LIVE = "RESULT = 1"
UNKNOWN_COLUMN_SENTENCE = "The query names a column that does not exist."


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    local_store._DATAFRAME_CACHE.invalidate()
    local_store.AuthStore().ensure_user(USER)
    roles_store.RolesStore().ensure_base_role()
    yield tmp_path
    local_store._DATAFRAME_CACHE.invalidate()


def _mk_db(tmp_path):
    """sqlite table t(a INTEGER, b TEXT): 25 rows, a = 0..24, b holds ':x'
    and '10:30' on rows 1 and 2 (bind-parameter look-alikes)."""
    db = tmp_path / "live.db"
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    special = {1: ":x", 2: "10:30"}
    with eng.begin() as c:
        c.execute(text("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)"))
        for i in range(25):
            c.execute(text("INSERT INTO t VALUES (:a, :b)"),
                      {"a": i, "b": special.get(i, f"v{i}")})
    eng.dispose()
    return db


def _register(tmp_path, **table_extra):
    db = _mk_db(tmp_path)
    store = db_sources.DataSourceStore()
    cid = store.create_connection(
        {"name": "S", "db_type": "sqlite",
         "url_override": f"sqlite+pysqlite:///{db}"}, "pw", actor=ADMIN)["id"]
    doc = {"connection_id": cid, "schema": "", "table_name": "t",
           "display_name": KEY, "description": "live desc",
           "columns": [{"name": "a", "dtype": "INTEGER", "description": "col a"},
                       {"name": "b", "dtype": "TEXT", "description": "col b"}],
           "is_connector": False, "relations": [], "mode": "live"}
    doc.update(table_extra)
    t = store.upsert_table(doc, actor=ADMIN)
    return {"cid": cid, "tid": t["id"], "db": db}


@pytest.fixture
def live(env):
    """One sqlite connection with table t registered LIVE."""
    return _register(env)


def _db_entry(key, tid, cid, *, schema="", table="t", **db_extra):
    """Chat-meta DB entry in the frozen `_db_meta_entry` shape."""
    return {"file_name": key, "file_description": "reg desc",
            "source": "database",
            "db": {"table_id": tid, "connection_id": cid,
                   "schema": schema, "table_name": table, "display_name": key,
                   "is_connector": False, "auto_included": False,
                   "row_count": None, "refreshed_at": None, "relations": [],
                   **db_extra},
            "schema": {"file_name": key,
                       "fields": {"a": {"description": "col a", "values": None},
                                  "b": {"description": "col b", "values": None}}}}


def _chat_with(entries, chat_id=CHAT):
    store = local_store.ChatDataStore(chat_id)
    meta = store.read_meta()
    meta["owner"] = USER
    meta["files"] = entries
    store.write_meta(meta)
    return store


@pytest.fixture
def chat(live):
    """A chat whose meta holds the live table under the df key `live t`."""
    return _chat_with([_db_entry(KEY, live["tid"], live["cid"])])


def _grant(email, table_ids):
    role = roles_store.RolesStore().create_role(
        {"name": f"R-{email}-{len(table_ids)}", "table_ids": table_ids},
        actor=ADMIN)
    local_store.AuthStore().set_data_role(email, role["id"])
    return role


@pytest.fixture
def granted(live):
    _grant(USER, [live["tid"]])


@pytest.fixture
def brain(monkeypatch):
    """Every brain call stubbed on run_chat_local's binding. `state["plan"]`
    is the plan response, `state["retry"]` the retry response (a dict, or a
    callable taking the retry kwargs). `calls` records every call's kwargs."""
    calls = {"plan": [], "retry": [], "describe": [], "summarize": [],
             "greeting": []}
    state = {"plan": {"raw_text": "", "kind": "PYTHON", "code": CODE_SUM,
                      "usage": {}, "context_decision": {}},
             "retry": {"kind": "PYTHON", "code": CODE_SUM, "usage": {}}}

    def plan(**kw):
        calls["plan"].append(kw)
        return dict(state["plan"])

    def retry(**kw):
        calls["retry"].append(kw)
        r = state["retry"]
        return dict(r(kw)) if callable(r) else dict(r)

    def describe(**kw):
        calls["describe"].append(kw)
        return {"text": "described", "usage": {}}

    def summarize(**kw):
        calls["summarize"].append(kw)
        json.dumps(kw.get("preview"))
        return {"text": "summarized", "usage": {}}

    def greeting(*a, **kw):
        calls["greeting"].append(kw)
        return {"text": "hi", "usage": {}}

    for name, fn in (("plan", plan), ("retry", retry), ("describe", describe),
                     ("summarize", summarize), ("greeting", greeting)):
        monkeypatch.setattr(run_chat_local.brain_client, name, fn)
    monkeypatch.setattr(run_chat_local, "build_schema_text",
                        lambda *a, **k: "schema")
    return calls, state


@pytest.fixture
def executor(monkeypatch):
    """Fake sandbox: records a COPY of the `dfs` each call received.
    `behaviour["errors"]` is consumed one entry per Python call — an error
    text makes that call fail, None succeeds with a scalar result."""
    seen = {"exec": [], "plot": []}
    behaviour = {"errors": []}

    def fake_exec(code, dfs, sid, **kw):
        seen["exec"].append({"code": code,
                             "dfs": {k: v.copy() for k, v in dfs.items()}})
        err = behaviour["errors"].pop(0) if behaviour["errors"] else None
        if err:
            return {"error": err, "result": None, "preview": None,
                    "image_base64": None}
        return {"error": None, "result": 42, "preview": 42, "image_base64": None}

    def fake_plot(code, dfs, sid, **kw):
        seen["plot"].append({"code": code,
                             "dfs": {k: v.copy() for k, v in dfs.items()}})
        return {"error": None, "is_plotly": False, "image": "IMG",
                "chart_data": None}

    monkeypatch.setattr(run_chat_local, "safe_execute", fake_exec)
    monkeypatch.setattr(run_chat_local, "render_plot_safe", fake_plot)
    return seen, behaviour


def _no_engine(monkeypatch):
    """Fail the test if ANY database engine is built (no fetch of any kind)."""
    def boom(*a, **kw):
        raise AssertionError("no live fetch was expected here")
    monkeypatch.setattr(db_connector, "get_engine", boom)


def _run(chat, question="how many rows?", user=USER):
    dfs = chat.load_dataframes(include_live=True)
    return run_chat_local.run_chat(
        sid="t", dfs=dfs, schema_docs=chat.schema_docs(), question=question,
        history_rows=[], user_email=user)


def _records(caplog, marker):
    return [r for r in caplog.records if marker in r.getMessage()]


# ===========================================================================
# 1. loading
# ===========================================================================
def test_load_dataframes_default_omits_the_live_key(chat):
    assert KEY not in chat.load_dataframes()


def test_load_dataframes_include_live_yields_a_typed_empty_placeholder(chat):
    dfs = chat.load_dataframes(include_live=True)
    assert KEY in dfs
    df = dfs[KEY]
    assert list(df.columns) == ["a", "b"]
    assert len(df) == 0
    assert str(df["a"].dtype) == "int64"
    assert str(df["b"].dtype) == "object"
    assert df.attrs.get("pdc_live") is True


def test_a_parquet_left_behind_is_never_loaded_for_a_live_row(chat, live):
    """A snapshot kept from before the table went live is stale: neither
    loader mode may serve it as the table's rows."""
    pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]}).to_parquet(
        local_store.db_snapshot_path(live["tid"]))
    local_store._DATAFRAME_CACHE.invalidate()
    assert KEY not in chat.load_dataframes()
    dfs = chat.load_dataframes(include_live=True)
    assert len(dfs[KEY]) == 0
    assert dfs[KEY].attrs.get("pdc_live") is True


# ===========================================================================
# 2. schema_docs
# ===========================================================================
def test_schema_docs_live_entry_carries_the_live_keys(chat, live):
    doc = chat.schema_docs()[KEY]
    assert doc["source"] == "database"
    assert doc["live"] is True
    assert doc["dialect"] == "sqlite"
    assert isinstance(doc["row_cap"], int) and doc["row_cap"] >= 1
    ref = doc["db_ref"]
    assert ref["schema"] == ""
    assert ref["table_name"] == "t"
    assert ref["connection_id"] == live["cid"]
    assert ref["table_id"] == live["tid"]


def test_schema_docs_snapshot_entry_keeps_exactly_todays_keys(env):
    """A snapshot table (a parquet, mode snapshot) emits exactly the keys it
    always did — the live keys appear ONLY for a live registration."""
    reg = {"connections": [], "tables": [{"id": SNAP_TID, "display_name": SNAP_KEY,
                                          "mode": "snapshot"}]}
    (env / "data_sources.json").write_text(json.dumps(reg), encoding="utf-8")
    pd.DataFrame({"a": [1]}).to_parquet(local_store.db_snapshot_path(SNAP_TID))
    store = _chat_with([_db_entry(SNAP_KEY, SNAP_TID, "cc11cc11cc11cc11",
                                  schema="shop", table="T",
                                  refreshed_at="2026-07-27T00:00:00+00:00")],
                       chat_id="c_snaponly")
    docs = store.schema_docs()
    assert set(docs[SNAP_KEY]) == {"source", "db_table", "refreshed_at",
                                   "relations", "file_description", "fields"}


# ===========================================================================
# 3. schema text
# ===========================================================================
def test_schema_text_live_placeholder_renders_the_live_line(chat):
    dfs = chat.load_dataframes(include_live=True)
    txt = schema_builder.schema_text(chat.schema_docs(), dfs)
    assert "[LIVE, dialect=sqlite, row_cap=" in txt
    assert "write ONE read-only SELECT" in txt
    assert "CATEGORICAL (0 unique" not in txt
    assert "snapshot as of" not in txt
    assert "Columns: a, b" in txt


def test_schema_text_snapshot_only_chat_is_unchanged(env):
    """A chat with only snapshot tables renders byte-identically to a call
    whose docs carry none of the live keys (there are none to strip)."""
    import copy
    reg = {"connections": [], "tables": [{"id": SNAP_TID, "display_name": SNAP_KEY,
                                          "mode": "snapshot"}]}
    (env / "data_sources.json").write_text(json.dumps(reg), encoding="utf-8")
    pd.DataFrame({"a": [1, 2]}).to_parquet(local_store.db_snapshot_path(SNAP_TID))
    store = _chat_with([_db_entry(SNAP_KEY, SNAP_TID, "cc11cc11cc11cc11",
                                  schema="shop", table="T",
                                  refreshed_at="2026-07-27T00:00:00+00:00")],
                       chat_id="c_snaponly2")
    dfs = store.load_dataframes()
    docs = store.schema_docs()
    stripped = copy.deepcopy(docs)
    for d in stripped.values():
        for k in ("live", "dialect", "row_cap", "db_ref"):
            d.pop(k, None)
    txt = schema_builder.schema_text(docs, dfs)
    assert txt == schema_builder.schema_text(stripped, dfs)
    assert "snapshot as of 2026-07-27T00:00:00+00:00" in txt
    assert "[LIVE" not in txt
    assert "read-only SELECT" not in txt


# ===========================================================================
# 4. the plan payload
# ===========================================================================
def test_plan_receives_live_tables_and_df_names(chat, granted, brain, executor):
    calls, state = brain
    state["plan"]["sql"] = {KEY: "SELECT a, b FROM t"}
    _run(chat)
    kw = calls["plan"][0]
    assert KEY in kw["df_names"]
    assert kw["live_tables"] == [{"name": KEY, "dialect": "sqlite",
                                  "row_cap": settings.LIVE_RESULT_ROW_CAP,
                                  "filtered": False}]


def test_plan_for_a_snapshot_only_chat_carries_no_live_tables(env, brain,
                                                              executor):
    calls, state = brain
    state["plan"]["code"] = "RESULT = dfs['snap t']['a'].sum()"
    reg = {"connections": [], "tables": [{"id": SNAP_TID, "display_name": SNAP_KEY,
                                          "mode": "snapshot"}]}
    (env / "data_sources.json").write_text(json.dumps(reg), encoding="utf-8")
    pd.DataFrame({"a": [1, 2]}).to_parquet(local_store.db_snapshot_path(SNAP_TID))
    store = _chat_with([_db_entry(SNAP_KEY, SNAP_TID, "cc11cc11cc11cc11",
                                  schema="shop", table="T")], chat_id="c_snap3")
    out = _run(store)
    assert not calls["plan"][0].get("live_tables")
    assert out["text"] == "summarized"


# ===========================================================================
# 5. the planner's SELECT fills the frame before the executor
# ===========================================================================
def test_plan_sql_fills_the_frame_before_the_executor(chat, granted, brain,
                                                      executor):
    calls, state = brain
    seen, _ = executor
    state["plan"]["sql"] = {KEY: "SELECT a, b FROM t WHERE a > 5"}
    out = _run(chat)
    assert len(seen["exec"]) == 1
    df = seen["exec"][0]["dfs"][KEY]
    assert len(df) == 19
    assert sorted(df["a"].tolist()) == list(range(6, 25))
    assert list(df.columns) == ["a", "b"]
    assert out["sql"] == {KEY: "SELECT a, b FROM t WHERE a > 5"}
    assert out["live_rows"] == {KEY: 19}
    assert out["live_truncated"] is False
    assert out["text"] == "summarized"
    assert calls["retry"] == []


# ===========================================================================
# 6. no SELECT from the planner → the default capped fetch
# ===========================================================================
def test_no_sql_default_fetch_reads_the_whole_table_under_the_cap(
        chat, granted, brain, executor):
    seen, _ = executor
    out = _run(chat)
    df = seen["exec"][0]["dfs"][KEY]
    assert len(df) == 25
    assert {":x", "10:30"} <= set(df["b"].tolist())
    assert out["sql"] == {KEY: None}
    assert out["live_rows"] == {KEY: 25}
    assert out["live_truncated"] is False
    assert not out["text"].endswith(
        result_backstop.live_truncated_sentence("en", KEY, 25))


@pytest.mark.parametrize("question, lang", [
    ("how many rows are there?", "en"),
    ("რამდენი ჩანაწერია ცხრილში?", "ka"),
    ("сколько строк в таблице?", "ru"),
], ids=["en", "ka", "ru"])
def test_default_fetch_truncation_appends_the_localized_sentence(
        chat, granted, brain, executor, monkeypatch, question, lang):
    seen, _ = executor
    monkeypatch.setattr(settings, "LIVE_RESULT_ROW_CAP", 10)
    assert run_chat_local._detect_answer_language(question) == lang
    out = _run(chat, question=question)
    assert len(seen["exec"][0]["dfs"][KEY]) == 10
    assert out["live_truncated"] is True
    assert out["live_rows"] == {KEY: 10}
    sentence = result_backstop.live_truncated_sentence(lang, KEY, 10)
    assert sentence and KEY in sentence and "10" in sentence
    assert out["text"].startswith("summarized")
    assert out["text"].endswith(sentence)


def test_live_truncated_sentence_is_localized():
    en = result_backstop.live_truncated_sentence("en", KEY, 10)
    assert en == ("Note: live t is a live table; only the first 10 rows of the "
                  "query result were used.")
    ka = result_backstop.live_truncated_sentence("ka", KEY, 10)
    ru = result_backstop.live_truncated_sentence("ru", KEY, 10)
    assert len({en, ka, ru}) == 3
    for s in (ka, ru):
        assert KEY in s and "10" in s
    # An unknown language code falls back to English.
    assert result_backstop.live_truncated_sentence("xx", KEY, 10) == en


# ===========================================================================
# 7. an admin row filter: the default fetch only, the planner's SQL ignored
# ===========================================================================
def test_filtered_table_ignores_the_plan_sql(env, brain, executor, caplog):
    live = _register(env, where_filter="a >= 20")
    _grant(USER, [live["tid"]])
    chat = _chat_with([_db_entry(KEY, live["tid"], live["cid"])])
    calls, state = brain
    seen, _ = executor
    state["plan"]["sql"] = {KEY: "SELECT a, b FROM t WHERE a > 5"}
    with caplog.at_level(logging.INFO):
        out = _run(chat)
    df = seen["exec"][0]["dfs"][KEY]
    assert sorted(df["a"].tolist()) == [20, 21, 22, 23, 24]
    assert calls["plan"][0]["live_tables"][0]["filtered"] is True
    assert out["sql"] == {KEY: None}
    assert out["live_rows"] == {KEY: 5}
    assert _records(caplog, "LIVE_SQL_IGNORED_FILTERED")


# ===========================================================================
# 8. the second role gate inside the helper
# ===========================================================================
def test_role_denied_live_key_answers_the_denial_sentence(chat, brain, executor,
                                                          monkeypatch, caplog):
    """USER holds no role covering the table: the code references its key,
    so the turn ends with the plain sentence — no fetch, no executor, no
    retry / describe / summarize call."""
    calls, state = brain
    seen, _ = executor
    state["plan"]["sql"] = {KEY: "SELECT a, b FROM t"}
    _no_engine(monkeypatch)
    with caplog.at_level(logging.INFO):
        out = _run(chat)
    assert "no longer have access" in out["text"]
    assert KEY in out["text"]
    assert seen["exec"] == [] and seen["plot"] == []
    assert calls["retry"] == [] and calls["describe"] == [] and \
        calls["summarize"] == []
    assert _records(caplog, "LIVE_ROLE_DENIED")


# ===========================================================================
# 9. a guard refusal goes to retry as class "guard"
# ===========================================================================
def test_guard_refusal_reaches_retry_as_class_guard(chat, granted, brain,
                                                    executor):
    calls, state = brain
    seen, _ = executor
    state["plan"]["sql"] = {KEY: "DELETE FROM t"}
    # The retry answers code but no new SQL: a failed attempt each time.
    state["retry"] = {"kind": "PYTHON", "code": CODE_SUM, "usage": {}}
    out = _run(chat)
    assert seen["exec"] == []
    assert len(calls["retry"]) == 3
    first = calls["retry"][0]
    # Contract change (retry `sql` = what RAN this turn): a SELECT the guard
    # refused never reached the database, so it is reported as `null` for
    # its key — `sql_error` names the refusal. Re-pinned from the earlier
    # echo of the refused text.
    assert first["sql"] == {KEY: None}
    err = first["sql_error"]
    assert err["table"] == KEY
    assert err["dialect"] == "sqlite"
    assert err["class"] == "guard"
    assert err["guard"] is True
    assert isinstance(err["message"], str) and err["message"]
    assert set(err) == {"table", "dialect", "class", "guard", "message"}
    assert first["error_msg"].startswith("Live query for table 'live t' failed")
    assert first["failed_code"] == CODE_SUM
    assert [c["use_pro"] for c in calls["retry"]] == [False, True, True]
    assert "couldn't" in out["text"] or "could not" in out["text"].lower()
    # Same contract change on the result: the refused SELECT did not run.
    assert out["sql"] == {KEY: None}


# ===========================================================================
# 10. a bad column: class unknown_column, the literal never leaves
# ===========================================================================
def test_bad_column_reaches_retry_as_unknown_column_without_the_literal(
        chat, granted, brain, executor, caplog):
    calls, state = brain
    seen, _ = executor
    bad = f"SELECT nope FROM t WHERE b = '{MARK}'"
    state["plan"]["sql"] = {KEY: bad}
    state["retry"] = {"kind": "PYTHON", "code": CODE_SUM, "usage": {}}
    with caplog.at_level(logging.INFO):
        _run(chat)
    assert seen["exec"] == []
    assert calls["retry"], "the fetch failure never reached retry"
    first = calls["retry"][0]
    err = first["sql_error"]
    assert err["class"] == "unknown_column"
    assert err["guard"] is False
    assert err["message"] is None
    assert first["error_msg"] == \
        f"Live query for table '{KEY}' failed: {UNKNOWN_COLUMN_SENTENCE}"
    # The planner's own SQL is echoed back to it verbatim under `sql`; every
    # OTHER retry field is value-free.
    assert first["sql"] == {KEY: bad}
    for name, value in first.items():
        if name == "sql":
            continue
        assert MARK not in json.dumps(value, default=str), name
    for r in caplog.records:
        assert MARK not in r.getMessage()
        assert "nope" not in r.getMessage()
    assert _records(caplog, "LIVE_PREFETCH_FAILED")


# ===========================================================================
# 11. a retry with new SQL re-fetches; without one it is a failed attempt
# ===========================================================================
def test_retry_with_new_sql_refetches_and_succeeds(chat, granted, brain,
                                                   executor):
    calls, state = brain
    seen, _ = executor
    state["plan"]["sql"] = {KEY: "SELECT nope FROM t"}
    state["retry"] = {"kind": "PYTHON", "code": CODE_SUM, "usage": {},
                      "sql": {KEY: "SELECT a FROM t"}}
    out = _run(chat)
    assert len(calls["retry"]) == 1
    assert len(seen["exec"]) == 1
    df = seen["exec"][0]["dfs"][KEY]
    assert len(df) == 25 and list(df.columns) == ["a"]
    assert out["text"] == "summarized"
    assert out["sql"] == {KEY: "SELECT a FROM t"}
    assert out["live_rows"] == {KEY: 25}


def test_retry_without_new_sql_after_a_fetch_failure_never_defaults(
        chat, granted, brain, executor, monkeypatch):
    """Once a planner SELECT failed, a retry that brings code but no SQL for
    that key is a failed attempt — never a capped whole-table fetch that
    would silently change what the answer computes."""
    calls, state = brain
    seen, _ = executor
    state["plan"]["sql"] = {KEY: "SELECT nope FROM t"}
    state["retry"] = {"kind": "PYTHON", "code": CODE_SUM, "usage": {}}
    sampled = []
    real = db_connector.sample_rows
    monkeypatch.setattr(db_connector, "sample_rows",
                        lambda *a, **k: sampled.append(1) or real(*a, **k))
    out = _run(chat)
    assert len(calls["retry"]) == 3
    assert seen["exec"] == []
    assert sampled == []
    assert "couldn't" in out["text"] or "could not" in out["text"].lower()
    assert out["live_rows"] == {}
    assert out["live_truncated"] is False


# ===========================================================================
# 12. an execution error with live frames in place
# ===========================================================================
def test_execution_error_retry_carries_the_sql_map_and_no_sql_error(
        chat, granted, brain, executor):
    calls, state = brain
    seen, behaviour = executor
    state["plan"]["sql"] = {KEY: "SELECT a, b FROM t WHERE a > 5"}
    behaviour["errors"] = ["NameError: name 'x' is not defined"]
    out = _run(chat)
    assert len(calls["retry"]) == 1
    kw = calls["retry"][0]
    assert kw["sql"] == {KEY: "SELECT a, b FROM t WHERE a > 5"}
    assert kw["sql_error"] is None
    assert kw["error_msg"].startswith("NameError")
    # The frames were fetched once; the second execution reused them.
    assert len(seen["exec"]) == 2
    assert len(seen["exec"][1]["dfs"][KEY]) == 19
    assert out["text"] == "summarized"


# ===========================================================================
# 13. a live table the code never references costs nothing
# ===========================================================================
def _add_csv(chat):
    """A second, file-based key `d.csv` (column x) beside the live table."""
    (chat.files_dir / "d.csv").write_text("x\n1\n2\n", encoding="utf-8")
    meta = chat.read_meta()
    meta["files"].append({"file_name": "d.csv", "file_description": "",
                          "schema": {"file_name": "d.csv", "fields": {}}})
    chat.write_meta(meta)
    local_store._DATAFRAME_CACHE.invalidate()


def test_unreferenced_live_table_is_never_fetched(chat, granted, brain, executor,
                                                  monkeypatch):
    """Code that names ONLY another key, with no generic use of `dfs`."""
    calls, state = brain
    seen, _ = executor
    _add_csv(chat)
    state["plan"]["code"] = "RESULT = dfs['d.csv']['x'].sum()"
    state["plan"]["sql"] = {KEY: "SELECT a, b FROM t"}
    _no_engine(monkeypatch)

    def boom(*a, **kw):
        raise AssertionError("run_live_select must not run for an unreferenced key")
    monkeypatch.setattr(db_connector, "run_live_select", boom)
    out = _run(chat)
    assert len(seen["exec"]) == 1
    assert "d.csv" in seen["exec"][0]["dfs"]
    assert len(seen["exec"][0]["dfs"][KEY]) == 0        # the placeholder
    assert out["sql"] == {}
    assert out["live_rows"] == {}
    assert out["live_truncated"] is False
    assert out["text"] == "summarized"


# ===========================================================================
# 13b. other ways of naming the key, and generic access to `dfs`
# ===========================================================================
@pytest.mark.parametrize("code", [
    'RESULT = dfs.get("live t")["a"].sum()',
    "RESULT = dfs.get('live t')['a'].sum()",
    'name = "live t"\nRESULT = dfs[name]["a"].sum()',
], ids=["dfs-get-double-quotes", "dfs-get-single-quotes", "bare-key-mention"])
def test_other_spellings_of_the_key_fetch_the_live_table(
        chat, granted, brain, executor, code):
    """The key is referenced without the `dfs['…']` subscript form: `dfs.get`
    (either quote) or a bare mention of the name in another expression —
    the rows are fetched all the same."""
    calls, state = brain
    seen, _ = executor
    assert "dfs['live t']" not in code and 'dfs["live t"]' not in code
    state["plan"]["code"] = code
    state["plan"]["sql"] = {KEY: "SELECT a, b FROM t WHERE a > 5"}
    out = _run(chat)
    assert len(seen["exec"]) == 1
    assert len(seen["exec"][0]["dfs"][KEY]) == 19
    assert out["live_rows"] == {KEY: 19}
    assert out["sql"] == {KEY: "SELECT a, b FROM t WHERE a > 5"}


@pytest.mark.parametrize("code", [
    "RESULT = sum(len(df) for df in dfs.values())",
    "total = 0\nfor k, df in dfs.items():\n    total += len(df)\nRESULT = total",
], ids=["dfs-values", "dfs-items"])
def test_generic_access_fetches_every_live_key_with_the_default_fetch(
        chat, granted, brain, executor, code):
    """Code that walks `dfs` without naming a key touches every table, so
    every live key is fetched — with the DEFAULT fetch when the planner sent
    no SQL."""
    calls, state = brain
    seen, _ = executor
    _add_csv(chat)
    state["plan"]["code"] = code
    state["plan"].pop("sql", None)
    out = _run(chat)
    assert len(seen["exec"]) == 1
    got = seen["exec"][0]["dfs"]
    assert len(got[KEY]) == 25
    assert "d.csv" in got
    assert out["sql"] == {KEY: None}
    assert out["live_rows"] == {KEY: 25}
    assert out["live_truncated"] is False


def test_generic_access_still_uses_the_planners_select_when_sent(
        chat, granted, brain, executor):
    """Generic access decides only WHICH keys are touched; when the planner
    DID send a SELECT for a live key, that SELECT is what runs — never the
    default whole-table read in its place."""
    calls, state = brain
    seen, _ = executor
    _add_csv(chat)
    state["plan"]["code"] = "RESULT = sum(len(df) for df in dfs.values())"
    state["plan"]["sql"] = {KEY: "SELECT a, b FROM t WHERE a > 5"}
    out = _run(chat)
    assert len(seen["exec"]) == 1
    got = seen["exec"][0]["dfs"]
    assert len(got[KEY]) == 19
    assert sorted(got[KEY]["a"].tolist()) == list(range(6, 25))
    assert out["sql"] == {KEY: "SELECT a, b FROM t WHERE a > 5"}
    assert out["live_rows"] == {KEY: 19}


# ===========================================================================
# 14. the multi-chart path fetches once per turn
# ===========================================================================
def _plot_blocks(codes):
    blocks = "\n###NEXT_PLOT###\n".join(f"```plot_code\n{c}\n```" for c in codes)
    return blocks + "\n###NEXT_PLOT###\n"


def _run_multi(chat, question="dashboard", user=USER):
    dfs = chat.load_dataframes(include_live=True)
    return list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=dfs, schema_docs=chat.schema_docs(), question=question,
        history_rows=[], user_email=user))


def test_multi_plot_fetches_once_and_the_done_event_carries_the_live_fields(
        chat, granted, brain, executor, monkeypatch):
    calls, state = brain
    seen, _ = executor
    state["plan"] = {"raw_text": _plot_blocks([
        "fig = px.bar(dfs['live t'], x='a', y='b')",
        "fig = px.line(dfs['live t'], x='b', y='a')"]),
        "kind": "PLOT_CODE", "code": "", "usage": {}, "context_decision": {},
        "sql": {KEY: "SELECT a, b FROM t WHERE a > 5"}}
    fetches = []
    real = db_connector.run_live_select

    def counting(*a, **kw):
        fetches.append(1)
        return real(*a, **kw)
    monkeypatch.setattr(db_connector, "run_live_select", counting)
    events = _run_multi(chat)
    assert len(fetches) == 1
    assert len(seen["plot"]) == 2
    for call in seen["plot"]:
        assert len(call["dfs"][KEY]) == 19
    done = [e for e in events if e.get("done")][0]
    assert done["sql"] == {KEY: "SELECT a, b FROM t WHERE a > 5"}
    assert done["live_rows"] == {KEY: 19}
    assert done["live_truncated"] is False
    partials = [e for e in events if e.get("partial")]
    assert partials, "no chart was streamed"
    for p in partials:
        assert p["sql"] == {KEY: "SELECT a, b FROM t WHERE a > 5"}


def test_single_response_result_carries_the_live_fields(chat, granted, brain,
                                                        executor):
    calls, state = brain
    state["plan"]["sql"] = {KEY: "SELECT a, b FROM t WHERE a > 5"}
    events = _run_multi(chat, question="how many?")
    assert len(events) == 1 and events[0]["single_response"]
    res = events[0]["result"]
    assert res["sql"] == {KEY: "SELECT a, b FROM t WHERE a > 5"}
    assert res["live_rows"] == {KEY: 19}
    assert res["live_truncated"] is False


# ===========================================================================
# 15. a key first referenced by a retry's code is fetched before it runs
# ===========================================================================
def test_key_first_referenced_by_a_retry_is_fetched_before_execution(
        chat, granted, brain, executor):
    calls, state = brain
    seen, behaviour = executor
    state["plan"]["code"] = CODE_NO_LIVE
    state["plan"]["sql"] = {KEY: "SELECT a, b FROM t WHERE a > 5"}
    behaviour["errors"] = ["ZeroDivisionError: division by zero"]
    state["retry"] = {"kind": "PYTHON", "code": CODE_SUM, "usage": {}}
    out = _run(chat)
    assert len(seen["exec"]) == 2
    assert len(seen["exec"][0]["dfs"][KEY]) == 0        # not yet referenced
    assert len(seen["exec"][1]["dfs"][KEY]) == 19       # fetched for the retry
    assert out["sql"] == {KEY: "SELECT a, b FROM t WHERE a > 5"}
    assert out["live_rows"] == {KEY: 19}


# ===========================================================================
# 16. the brain-side boundary
# ===========================================================================
def test_sanitize_history_rows_drops_the_live_fields():
    rows = [{"role": "ai", "content": "x", "code": "RESULT = 1",
             "sql": {KEY: "SELECT a FROM t"}, "live_truncated": True,
             "live_rows": {KEY: 3}}]
    out = brain_client._sanitize_history_rows(rows)
    assert out == [{"role": "ai", "content": "x", "code": "RESULT = 1"}]


def _capture_post(monkeypatch):
    sent = []

    def fake_post(path, payload, sid, *a, **kw):
        sent.append((path, payload))
        return {}
    monkeypatch.setattr(brain_client, "_post", fake_post)
    return sent


_PLAN_ARGS = dict(sid="s", question="q", schema_text="s", df_names=["a"],
                  history_rows=[], common_fields=[], user_email="u@x.com")


def test_plan_payload_carries_live_tables_only_when_non_empty(monkeypatch):
    sent = _capture_post(monkeypatch)
    brain_client.plan(**_PLAN_ARGS)
    assert "live_tables" not in sent[-1][1]
    brain_client.plan(**_PLAN_ARGS, live_tables=None)
    assert "live_tables" not in sent[-1][1]
    brain_client.plan(**_PLAN_ARGS, live_tables=[])
    assert "live_tables" not in sent[-1][1]
    rows = [{"name": KEY, "dialect": "sqlite", "row_cap": 5, "filtered": False}]
    brain_client.plan(**_PLAN_ARGS, live_tables=rows)
    assert sent[-1][0] == "/v1/plan"
    assert sent[-1][1]["live_tables"] == rows


_RETRY_ARGS = dict(sid="s", question="q", schema_text="s", df_names=["a"],
                   df_columns={"a": ["x"]}, history_rows=[], error_msg="e",
                   failed_code="c", use_pro=False, use_search=False,
                   user_email="u@x.com")


def test_retry_payload_carries_sql_and_sql_error_only_when_given(monkeypatch):
    sent = _capture_post(monkeypatch)
    brain_client.retry(**_RETRY_ARGS)
    body = sent[-1][1]
    assert "sql" not in body and "sql_error" not in body and \
        "live_tables" not in body
    err = {"table": KEY, "dialect": "sqlite", "class": "syntax",
           "guard": False, "message": None}
    brain_client.retry(**_RETRY_ARGS, sql={KEY: "SELECT a FROM t"},
                       sql_error=err,
                       live_tables=[{"name": KEY, "dialect": "sqlite",
                                     "row_cap": 5, "filtered": False}])
    body = sent[-1][1]
    assert sent[-1][0] == "/v1/retry"
    assert body["sql"] == {KEY: "SELECT a FROM t"}
    assert body["sql_error"] == err
    assert body["live_tables"][0]["name"] == KEY


# ===========================================================================
# 17. profiles: never computed from a placeholder
# ===========================================================================
def test_ensure_chat_profiles_never_writes_a_profile_for_a_placeholder(chat, live):
    dfs = chat.load_dataframes(include_live=True)
    out = local_store.ensure_chat_profiles(chat, dfs)
    assert KEY not in out
    assert not local_store.db_profile_path(live["tid"]).exists()


def _write_live_profile(tid, stamp):
    import dataset_profile
    prof = dataset_profile.compute_profile(
        pd.DataFrame({"a": [1, 2], "b": ["x", "y"]}))
    local_store.write_profile(local_store.db_profile_path(tid), prof, stamp)


def test_ensure_chat_profiles_returns_a_stored_live_profile_without_src(chat, live):
    _write_live_profile(live["tid"], {"kind": "live",
                                      "profiled_at": "2026-09-26T00:00:00+00:00",
                                      "sample_rows": 2})
    dfs = chat.load_dataframes(include_live=True)
    out = local_store.ensure_chat_profiles(chat, dfs)
    assert KEY in out
    assert "src" not in out[KEY]
    assert out[KEY]["rows"] == 2
    assert set(out[KEY]["columns"]) == {"a", "b"}


def test_ensure_chat_profiles_omits_a_profile_without_the_live_kind(chat, live):
    _write_live_profile(live["tid"], {"size": 1, "mtime_ns": 1})
    dfs = chat.load_dataframes(include_live=True)
    out = local_store.ensure_chat_profiles(chat, dfs)
    assert KEY not in out


# ===========================================================================
# 18. a live row is not a missing table
# ===========================================================================
def test_missing_db_tables_does_not_report_a_live_row(chat):
    meta = chat.read_meta()
    assert local_store.missing_db_tables(meta) == []
    text_, missing = local_store.empty_dataset_message(meta)
    assert text_ == "Chat dataset is empty."
    assert missing == []


# ===========================================================================
# 19. the scheduler's chat resync for a live row
# ===========================================================================
def test_resync_writes_mode_live_and_keeps_refreshed_at_none(chat, live):
    assert db_scheduler.resync_chats_for_table(live["tid"]) == 1
    entry = [e for e in chat.read_meta()["files"]
             if e.get("file_name") == KEY][0]
    assert entry["db"]["mode"] == "live"
    assert entry["db"]["refreshed_at"] is None
    assert entry["db"]["table_id"] == live["tid"]
    assert set(entry["schema"]["fields"]) == {"a", "b"}


# ===========================================================================
# 20. the registry cannot be read: no DB entry is served (fail closed)
# ===========================================================================
def _registry_unreadable(monkeypatch):
    def boom(self, *a, **kw):
        raise RuntimeError("registry unreadable")
    monkeypatch.setattr(db_sources.DataSourceStore, "list_tables", boom)


@pytest.mark.parametrize("include_live", [False, True],
                         ids=["default", "include_live"])
def test_unreadable_registry_never_serves_a_stale_parquet_of_a_live_row(
        chat, live, monkeypatch, caplog, include_live):
    """The registry decides whether an entry is live or snapshot; when it
    cannot be read, a parquet left behind by a live table must not be served
    as its rows — the DB keys are simply absent, file keys still load."""
    _add_csv(chat)
    pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]}).to_parquet(
        local_store.db_snapshot_path(live["tid"]))
    local_store._DATAFRAME_CACHE.invalidate()
    _registry_unreadable(monkeypatch)
    with caplog.at_level(logging.INFO):
        dfs = (chat.load_dataframes(include_live=True) if include_live
               else chat.load_dataframes())
    assert "d.csv" in dfs
    assert KEY not in dfs
    assert _records(caplog, "LIVE_REGISTRY_PROBE_FAILED")


@pytest.mark.parametrize("include_live", [False, True],
                         ids=["default", "include_live"])
def test_unreadable_registry_serves_no_snapshot_entry_either(
        env, monkeypatch, caplog, include_live):
    reg = {"connections": [], "tables": [{"id": SNAP_TID, "display_name": SNAP_KEY,
                                          "mode": "snapshot"}]}
    (env / "data_sources.json").write_text(json.dumps(reg), encoding="utf-8")
    pd.DataFrame({"a": [1, 2]}).to_parquet(local_store.db_snapshot_path(SNAP_TID))
    store = _chat_with([_db_entry(SNAP_KEY, SNAP_TID, "cc11cc11cc11cc11",
                                  schema="shop", table="T")], chat_id="c_snapreg")
    _add_csv(store)
    _registry_unreadable(monkeypatch)
    with caplog.at_level(logging.INFO):
        dfs = (store.load_dataframes(include_live=True) if include_live
               else store.load_dataframes())
    assert "d.csv" in dfs
    assert SNAP_KEY not in dfs
    assert _records(caplog, "LIVE_REGISTRY_PROBE_FAILED")


# ===========================================================================
# 21. the `df` alias: the executor binds `df` to the FIRST frame, so on a
#     chat whose first frame is a live table `df`-only code references it
# ===========================================================================
CODE_DF = "RESULT = df['a'].sum()"


def test_df_alias_on_a_live_first_chat_takes_the_default_read(chat, granted, brain,
                                                              executor):
    """The chat's only frame is the live table: code using nothing but `df`
    fetches it (the default read when the planner sent no SELECT) — never
    an answer computed on the empty placeholder."""
    calls, state = brain
    seen, _ = executor
    state["plan"]["code"] = CODE_DF
    state["plan"].pop("sql", None)
    out = _run(chat)
    assert len(seen["exec"]) == 1
    assert len(seen["exec"][0]["dfs"][KEY]) == 25
    assert out["sql"] == {KEY: None}
    assert out["live_rows"] == {KEY: 25}
    assert out["text"] == "summarized"


def test_df_alias_on_a_live_first_chat_takes_the_planners_select(chat, granted,
                                                                 brain, executor):
    calls, state = brain
    seen, _ = executor
    state["plan"]["code"] = CODE_DF
    state["plan"]["sql"] = {KEY: "SELECT a, b FROM t WHERE a > 5"}
    out = _run(chat)
    assert len(seen["exec"]) == 1
    assert len(seen["exec"][0]["dfs"][KEY]) == 19
    assert out["sql"] == {KEY: "SELECT a, b FROM t WHERE a > 5"}
    assert out["live_rows"] == {KEY: 19}


def test_df_alias_on_a_snapshot_first_chat_fetches_nothing(chat, granted, brain,
                                                           executor, monkeypatch):
    """With a file loaded first, `df` is that file: the live table is not
    referenced and nothing is fetched (`sql == {}`, the placeholder stays)."""
    calls, state = brain
    seen, _ = executor
    _add_csv(chat)
    dfs = chat.load_dataframes(include_live=True)
    assert next(iter(dfs)) == "d.csv" and KEY in dfs
    state["plan"]["code"] = "RESULT = df['x'].sum()"
    state["plan"].pop("sql", None)
    _no_engine(monkeypatch)
    out = _run(chat)
    assert len(seen["exec"]) == 1
    assert len(seen["exec"][0]["dfs"][KEY]) == 0
    assert out["sql"] == {}
    assert out["live_rows"] == {}


@pytest.mark.parametrize("code", ["RESULT = df2", "RESULT = 'xdfx'"],
                         ids=["df2", "quoted-word"])
def test_names_that_merely_contain_df_are_not_the_alias(chat, granted, brain,
                                                        executor, monkeypatch, code):
    """`df2` and a `df` inside another word are not the first-frame alias:
    no fetch, `sql == {}`."""
    calls, state = brain
    seen, _ = executor
    state["plan"]["code"] = code
    state["plan"].pop("sql", None)
    _no_engine(monkeypatch)
    out = _run(chat)
    assert len(seen["exec"]) == 1
    assert len(seen["exec"][0]["dfs"][KEY]) == 0
    assert out["sql"] == {}


# ===========================================================================
# 22. the retry's `sql` is what RAN this turn: the text for a SELECT that
#     reached the database, None for a default read / an ignored SELECT
#     (filtered table) / a guard refusal, absent for a key never fetched
# ===========================================================================
FILTERED_SQL_MESSAGE = ("The table is filtered by the administrator; "
                        "no SELECT is accepted for it.")


def test_bad_column_retry_keeps_the_select_text_that_reached_the_database(
        chat, granted, brain, executor):
    """A SELECT the database refused (unknown column) IS sent back under
    `sql` — the brain needs the text to fix it."""
    calls, state = brain
    bad = "SELECT nope FROM t"
    state["plan"]["sql"] = {KEY: bad}
    state["retry"] = {"kind": "PYTHON", "code": CODE_SUM, "usage": {}}
    out = _run(chat)
    assert calls["retry"]
    assert calls["retry"][0]["sql"] == {KEY: bad}
    assert calls["retry"][0]["sql_error"]["class"] == "unknown_column"
    assert out["sql"] == {KEY: bad}


def test_filtered_table_python_error_retry_sends_null_and_the_ignore_refusal(
        env, brain, executor, caplog):
    """The registration carries an admin row filter, so the planner's SELECT
    was IGNORED and the default read ran. A Python error then goes to retry
    with `sql: {key: null}` (the SELECT did not run) and a `sql_error` of
    class `guard` carrying the fixed sentence — never the echo of a SELECT
    that was not executed."""
    live = _register(env, where_filter="a >= 20")
    _grant(USER, [live["tid"]])
    chat = _chat_with([_db_entry(KEY, live["tid"], live["cid"])])
    calls, state = brain
    seen, behaviour = executor
    state["plan"]["sql"] = {KEY: "SELECT a, b FROM t WHERE a > 5"}
    behaviour["errors"] = ["NameError: name 'x' is not defined"]
    with caplog.at_level(logging.INFO):
        out = _run(chat)
    assert _records(caplog, "LIVE_SQL_IGNORED_FILTERED")
    assert len(calls["retry"]) == 1
    kw = calls["retry"][0]
    assert kw["sql"] == {KEY: None}
    assert kw["sql_error"] == {"table": KEY, "dialect": "sqlite",
                               "class": "guard", "guard": True,
                               "message": FILTERED_SQL_MESSAGE}
    assert kw["error_msg"].startswith("NameError")
    assert sorted(seen["exec"][0]["dfs"][KEY]["a"].tolist()) == [20, 21, 22, 23, 24]
    assert out["sql"] == {KEY: None}
    assert out["text"] == "summarized"


def test_python_error_on_a_default_read_retry_sends_null_and_no_sql_error(
        chat, granted, brain, executor):
    """No planner SELECT: the default read ran, so the retry says
    `sql: {key: null}` (the key WAS fetched, by the default read) and
    carries no `sql_error` — a Python error is not a query error."""
    calls, state = brain
    seen, behaviour = executor
    state["plan"].pop("sql", None)
    behaviour["errors"] = ["NameError: name 'x' is not defined"]
    out = _run(chat)
    assert len(calls["retry"]) == 1
    kw = calls["retry"][0]
    assert kw["sql"] == {KEY: None}
    assert kw["sql_error"] is None
    assert len(seen["exec"]) == 2
    assert out["sql"] == {KEY: None}
    assert out["text"] == "summarized"


def test_plan_sql_for_a_key_never_referenced_is_absent_from_the_retry(
        chat, granted, brain, executor, monkeypatch):
    """The planner sent a SELECT for a key the code never touches: nothing
    was fetched, so the retry's `sql` does not carry that key (None, or a
    map without it) — the map reports what ran, not what was planned."""
    calls, state = brain
    seen, behaviour = executor
    state["plan"]["code"] = CODE_NO_LIVE
    state["plan"]["sql"] = {"other key": "SELECT 1"}
    behaviour["errors"] = ["ZeroDivisionError: division by zero"]
    state["retry"] = {"kind": "PYTHON", "code": CODE_NO_LIVE, "usage": {}}
    _no_engine(monkeypatch)
    out = _run(chat)
    assert len(calls["retry"]) == 1
    kw = calls["retry"][0]
    assert "other key" not in (kw["sql"] or {})
    assert kw["sql_error"] is None
    assert out["sql"] == {}



# ===========================================================================
# 29. a REGENERATION retry's `sql` holds only what ran
# ===========================================================================
def test_plotly_regen_retry_sql_holds_only_the_selects_that_ran(chat, granted,
                                                                brain, executor):
    """A static matplotlib bar chart triggers the one-shot Plotly rewrite
    (`_PLOTLY_REGEN_INSTRUCTION`). The planner sent SELECTs for the live key
    AND for a key the code never touches: the regen retry's `sql` carries
    the SELECT that ran for the live key and nothing for the other key, and
    no `sql_error`."""
    calls, state = brain
    seen, _ = executor
    select = "SELECT a, b FROM t WHERE a > 5"
    state["plan"] = {"raw_text": "", "kind": "PLOT_CODE",
                     "code": "plt.bar(dfs['live t']['b'], dfs['live t']['a'])",
                     "usage": {}, "context_decision": {},
                     "sql": {KEY: select, "other key": "SELECT 1"}}
    # The rewrite answers prose, so the static chart is kept (one regen call).
    state["retry"] = {"kind": "NO_CODE", "code": "", "usage": {}}
    _run(chat)
    assert len(seen["plot"]) >= 1
    assert len(seen["plot"][0]["dfs"][KEY]) == 19
    regen = [kw for kw in calls["retry"]
             if kw["error_msg"] == run_chat_local._PLOTLY_REGEN_INSTRUCTION]
    assert len(regen) == 1, [kw["error_msg"][:60] for kw in calls["retry"]]
    kw = regen[0]
    assert "other key" not in kw["sql"]
    assert kw["sql"][KEY] == select
    assert kw["sql_error"] is None
