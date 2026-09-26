"""Live database tables on the routes: the picker and the session selection,
the stream's role drop before the planner, the persisted AI row, and every
re-execution path (per-item refresh, full-table re-execution, dashboard tile
refresh) re-running the SELECT the answer was computed with.

A live registration has no parquet. The picker lists it with `mode: "live"`
and the session route accepts it. On a question the stream drops a live key
the requester's role does not cover BEFORE the planner sees it. The AI row
remembers `sql` / `live_truncated` / `live_rows`, the in-flight registry
holds the code -> sql pair while the turn is running, and the refresh paths
resolve the stored SQL through `stored_sql_for_code`: no SQL for a live key
answers `LIVE_NO_QUERY`; a table switched back to snapshot serves its parquet
and ignores the SQL; the role gate refuses before any fetch; a failing SELECT
answers a value-free class sentence.

Offline: routes.chat + routes.dashboards + routes.upload on a private app
with a session cookie, the executor in process, the live table a tmp-file
sqlite database; `conftest.seed_history` seeds the code the refresh routes
require."""
import asyncio
import json
import logging

import pandas as pd
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from sqlalchemy import create_engine, text
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import db_connector
import db_sources
import local_store
import roles_store
from settings import settings

ADMIN = "ladmin"
OWNER = "user@x.com"
CHAT = "c_liverefresh1"
KEY = "live t"
SID = "s_liveflow"
MARK = "ZQ_MARK_9"
CODE = "RESULT = dfs['live t']"
SQL = "SELECT a, b FROM t"
UNKNOWN_COLUMN_SENTENCE = "The query names a column that does not exist."


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    local_store._DATAFRAME_CACHE.invalidate()
    local_store.AuthStore().ensure_user(OWNER)
    roles_store.RolesStore().ensure_base_role()

    import routes.chat as chat_mod
    import routes.dashboards as dash_mod
    import routes.upload as upload_mod
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(chat_mod.router)
    app.include_router(dash_mod.router)
    app.include_router(upload_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = SID
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    yield tc
    local_store._DATAFRAME_CACHE.invalidate()


def _mk_db(tmp_path):
    db = tmp_path / "live.db"
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)"))
        for i in range(25):
            c.execute(text("INSERT INTO t VALUES (:a, :b)"), {"a": i, "b": f"v{i}"})
    eng.dispose()
    return db


def _insert_rows(db, n):
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as c:
        for i in range(100, 100 + n):
            c.execute(text("INSERT INTO t VALUES (:a, :b)"), {"a": i, "b": f"n{i}"})
    eng.dispose()


@pytest.fixture
def registry(client, tmp_path):
    """One sqlite connection; `t` registered LIVE, `u` registered as a
    snapshot (a parquet on disk)."""
    db = _mk_db(tmp_path)
    store = db_sources.DataSourceStore()
    cid = store.create_connection(
        {"name": "S", "db_type": "sqlite",
         "url_override": f"sqlite+pysqlite:///{db}"}, "pw", actor=ADMIN)["id"]
    cols = [{"name": "a", "dtype": "INTEGER", "description": "col a"},
            {"name": "b", "dtype": "TEXT", "description": "col b"}]
    live = store.upsert_table({
        "connection_id": cid, "schema": "", "table_name": "t",
        "display_name": KEY, "description": "", "columns": cols,
        "is_connector": False, "relations": [], "mode": "live"}, actor=ADMIN)
    snap = store.upsert_table({
        "connection_id": cid, "schema": "", "table_name": "u",
        "display_name": "snap u", "description": "", "columns": cols,
        "is_connector": False, "relations": []}, actor=ADMIN)
    pd.DataFrame({"a": [1, 2], "b": ["x", "y"]}).to_parquet(
        local_store.db_snapshot_path(snap["id"]))
    return {"cid": cid, "tid": live["id"], "snap_tid": snap["id"], "db": db}


def _db_entry(df_key, tid, cid, table="t"):
    return {"file_name": df_key, "file_description": "", "source": "database",
            "db": {"table_id": tid, "connection_id": cid, "schema": "",
                   "table_name": table, "display_name": df_key,
                   "is_connector": False, "auto_included": False,
                   "row_count": None, "refreshed_at": None, "relations": []},
            "schema": {"file_name": df_key, "fields": {
                "a": {"description": "col a", "values": None},
                "b": {"description": "col b", "values": None}}}}


@pytest.fixture
def chat(registry):
    """A chat owned by OWNER on the live table plus one csv file (so the
    chat is never empty even when the live key is dropped)."""
    store = local_store.ChatDataStore(CHAT)
    (store.files_dir / "d.csv").write_text("x\n1\n2\n", encoding="utf-8")
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["files"] = [
        {"file_name": "d.csv", "file_description": "",
         "schema": {"file_name": "d.csv", "fields": {}}},
        _db_entry(KEY, registry["tid"], registry["cid"]),
    ]
    store.write_meta(meta)
    return store


def _grant(email, table_ids):
    role = roles_store.RolesStore().create_role(
        {"name": f"R-{email}-{len(table_ids)}", "table_ids": table_ids},
        actor=ADMIN)
    local_store.AuthStore().set_data_role(email, role["id"])
    return role


@pytest.fixture
def granted(registry):
    _grant(OWNER, [registry["tid"], registry["snap_tid"]])


def _seed_row(chat_id, code, *, sql=None, conv_id=None):
    """A real AI history row carrying `code` (and `sql` when given)."""
    store = local_store.ChatDataStore(chat_id)
    if conv_id is None:
        conv_id = store.new_conversation("seeded")
    row = {"role": "ai", "content": "", "code": code}
    if sql is not None:
        row["sql"] = sql
    store.append_history(conv_id, row)
    return conv_id


def _refresh(client, code):
    return client.post(f"/api/chat/{CHAT}/refresh_item",
                       json={"code": code, "kind": "table"}).json()


def _records(caplog, marker):
    return [r for r in caplog.records if marker in r.getMessage()]


def _no_fetch(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("no live fetch was expected here")
    monkeypatch.setattr(db_connector, "get_engine", boom)
    monkeypatch.setattr(db_connector, "run_live_select", boom)


# ===========================================================================
# 1. the picker and the session selection
# ===========================================================================
def test_api_db_tables_lists_the_live_table_with_its_mode(client, registry, granted):
    r = client.get("/api/db_tables")
    assert r.status_code == 200
    rows = {t["display_name"]: t for t in r.json()["tables"]}
    assert KEY in rows
    assert rows[KEY]["mode"] == "live"
    assert rows["snap u"]["mode"] == "snapshot"


def test_session_db_tables_accepts_a_live_seed(client, registry, granted):
    r = client.post("/session/db_tables", json={"table_ids": [registry["tid"]]})
    assert r.status_code == 200, r.json()
    keys = {row["df_key"] for row in r.json()["tables"]}
    assert keys == {KEY}
    meta = local_store.UserStore(SID).read_meta()
    entries = local_store.db_entries_from_meta(meta)
    assert [e["db"]["table_id"] for e in entries] == [registry["tid"]]


# ===========================================================================
# 2. the stream's role drop before the planner
# ===========================================================================
def _capture_multi_plot(monkeypatch, captured):
    import routes.chat as chat_mod

    def fake_gen(**kw):
        captured["dfs"] = {k: v.copy() for k, v in kw["dfs"].items()}
        captured["schema_docs"] = dict(kw["schema_docs"])
        yield {"single_response": True,
               "result": {"text": "ok", "image_base64": None, "table": None,
                          "code": None, "usage": {}}}

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot",
                        lambda **kw: fake_gen(**kw))


def test_stream_drops_an_uncovered_live_key_before_the_planner(
        client, chat, monkeypatch, caplog):
    captured = {}
    _capture_multi_plot(monkeypatch, captured)
    with caplog.at_level(logging.INFO):
        r = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": "q"})
    assert r.status_code == 200, r.text[:300]
    assert '"done": true' in r.text, r.text[-300:]
    assert "d.csv" in captured["dfs"]
    assert KEY not in captured["dfs"]
    assert KEY not in captured["schema_docs"]
    assert _records(caplog, "LIVE_ROLE_DROPPED")


def test_stream_keeps_a_covered_live_key_as_the_placeholder(
        client, chat, granted, monkeypatch, caplog):
    captured = {}
    _capture_multi_plot(monkeypatch, captured)
    with caplog.at_level(logging.INFO):
        r = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": "q"})
    assert r.status_code == 200, r.text[:300]
    assert KEY in captured["dfs"]
    df = captured["dfs"][KEY]
    assert len(df) == 0 and list(df.columns) == ["a", "b"]
    assert df.attrs.get("pdc_live") is True
    assert captured["schema_docs"][KEY]["live"] is True
    assert _records(caplog, "LIVE_ROLE_DROPPED") == []


# ===========================================================================
# 3. the persisted AI row and the in-flight code -> sql pair
# ===========================================================================
def test_stream_persists_the_live_fields_and_registers_the_sql_in_flight(
        client, chat, granted, monkeypatch):
    import routes.chat as chat_mod
    during = {}

    def fake_gen(**kw):
        yield {"single_response": True,
               "result": {"text": "ok", "image_base64": None, "table": None,
                          "code": CODE, "usage": {},
                          "sql": {KEY: SQL}, "live_truncated": True,
                          "live_rows": {KEY: 25}}}
        # Resumed after the worker handled the event: the pair is in flight
        # although the row is not on disk yet.
        during["sql"] = chat_mod.stored_sql_for_code(CHAT, CODE)

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot",
                        lambda **kw: fake_gen(**kw))
    r = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": "q"})
    assert r.status_code == 200, r.text[:300]
    assert '"done": true' in r.text, r.text[-300:]
    assert during["sql"] == {KEY: SQL}
    conv_id = json.loads(r.text.split("data: ")[1].split("\n")[0])["conv_id"]
    rows = chat.get_history(conv_id)
    ai = [m for m in rows if m.get("role") == "ai" and m.get("code") == CODE]
    assert len(ai) == 1
    assert ai[0]["sql"] == {KEY: SQL}
    assert ai[0]["live_truncated"] is True
    assert ai[0]["live_rows"] == {KEY: 25}
    # Persisted now: the lookup reads it from the history row.
    assert CHAT not in chat_mod._INFLIGHT_CODES
    assert chat_mod.stored_sql_for_code(CHAT, CODE) == {KEY: SQL}
    assert chat_mod.stored_sql_for_code(CHAT, "RESULT = 2") is None


# ===========================================================================
# 4-8. per-item refresh
# ===========================================================================
def test_refresh_item_reruns_the_stored_sql_against_the_live_table(
        client, chat, registry, granted):
    _seed_row(CHAT, CODE, sql={KEY: SQL})
    _insert_rows(registry["db"], 5)           # the table changed after the answer
    out = _refresh(client, CODE)
    assert out["ok"] is True, out
    assert out["kind"] == "table"
    assert out["table"]["total_rows"] == 30
    assert out["table"]["columns"] == ["a", "b"]


def test_refresh_item_without_a_stored_sql_for_a_live_key_is_refused(
        client, chat, granted, monkeypatch):
    _seed_row(CHAT, CODE)                     # the row predates the live switch
    _no_fetch(monkeypatch)
    out = _refresh(client, CODE)
    assert out["ok"] is False
    assert out["code"] == "LIVE_NO_QUERY"
    assert KEY in out["error"]


def test_refresh_item_uses_the_parquet_once_the_table_is_back_to_snapshot(
        client, chat, registry, granted, monkeypatch):
    _seed_row(CHAT, CODE, sql={KEY: SQL})
    store = db_sources.DataSourceStore()
    assert store.set_table_mode(registry["tid"], "snapshot", actor=ADMIN) is True
    pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]}).to_parquet(
        local_store.db_snapshot_path(registry["tid"]))
    local_store._DATAFRAME_CACHE.invalidate()
    _no_fetch(monkeypatch)
    out = _refresh(client, CODE)
    assert out["ok"] is True, out
    assert out["table"]["total_rows"] == 3


def test_refresh_item_role_denial_comes_before_any_fetch(client, chat, monkeypatch):
    _seed_row(CHAT, CODE, sql={KEY: SQL})
    _no_fetch(monkeypatch)
    out = _refresh(client, CODE)
    assert out["ok"] is False
    assert out["code"] == "ROLE_DENIED"
    assert out["blocked_tables"] == [KEY]


def test_refresh_item_failing_sql_answers_a_class_sentence_without_the_literal(
        client, chat, granted, caplog):
    _seed_row(CHAT, CODE, sql={KEY: f"SELECT nope FROM t WHERE b = '{MARK}'"})
    with caplog.at_level(logging.INFO):
        out = _refresh(client, CODE)
    assert out["ok"] is False
    assert out["error"] == UNKNOWN_COLUMN_SENTENCE
    assert MARK not in json.dumps(out)
    for r in caplog.records:
        assert MARK not in r.getMessage()
        assert "nope" not in r.getMessage()


# ===========================================================================
# 9. full-table re-execution
# ===========================================================================
def test_reexecute_full_df_refetches_the_live_rows(client, chat, registry, granted):
    import routes.chat as chat_mod
    _seed_row(CHAT, CODE, sql={KEY: SQL})
    _insert_rows(registry["db"], 3)
    df = asyncio.run(chat_mod._reexecute_full_df(CHAT, CODE))
    assert df is not None
    assert len(df) == 28
    assert list(df.columns) == ["a", "b"]


# ===========================================================================
# 10. a dashboard tile pinned from the chat
# ===========================================================================
def test_dashboard_tile_refresh_refetches_the_live_rows(client, chat, registry,
                                                        granted):
    _seed_row(CHAT, CODE, sql={KEY: SQL})
    ds = local_store.DashboardStore()
    row = ds.create_dashboard(OWNER, "D")
    saved = ds.add_tile(OWNER, row["dash_id"], {
        "chat_id": CHAT, "kind": "table", "code": CODE,
        "snapshot": {"table": {"columns": ["a", "b"],
                               "rows": [{"a": 0, "b": "v0"}], "total_rows": 1}}})
    _insert_rows(registry["db"], 4)
    r = client.post(f"/api/dashboards/{row['dash_id']}/tiles/{saved['tile_id']}/refresh")
    body = r.json()
    assert body["ok"] is True, body
    assert body["table"]["total_rows"] == 29
    doc = ds.get_dashboard(OWNER, row["dash_id"])
    assert doc["tiles"][0]["snapshot"]["table"]["total_rows"] == 29


def test_dashboard_tile_refresh_without_a_stored_sql_is_refused(
        client, chat, granted, monkeypatch):
    _seed_row(CHAT, CODE)
    ds = local_store.DashboardStore()
    row = ds.create_dashboard(OWNER, "D")
    saved = ds.add_tile(OWNER, row["dash_id"], {
        "chat_id": CHAT, "kind": "table", "code": CODE,
        "snapshot": {"table": {"columns": ["a", "b"],
                               "rows": [{"a": 0, "b": "v0"}], "total_rows": 1}}})
    _no_fetch(monkeypatch)
    body = client.post(
        f"/api/dashboards/{row['dash_id']}/tiles/{saved['tile_id']}/refresh").json()
    assert body["ok"] is False
    assert body.get("code") == "LIVE_NO_QUERY"
    # The stored snapshot is untouched.
    doc = ds.get_dashboard(OWNER, row["dash_id"])
    assert doc["tiles"][0]["snapshot"]["table"]["total_rows"] == 1


# ===========================================================================
# 11. /schema
# ===========================================================================
def test_schema_rows_mark_the_live_table_as_live_and_not_missing(client, chat,
                                                                 granted):
    rows = client.get(f"/api/chat/{CHAT}/schema").json()["db_tables"]
    by_key = {r["df_key"]: r for r in rows}
    assert by_key[KEY]["live"] is True
    assert by_key[KEY]["missing"] is False
    assert by_key[KEY]["allowed"] is True


# ===========================================================================
# 12. a chat whose ONLY table is live, asked by a user its role does not cover
# ===========================================================================
LIVE_ONLY_CHAT = "c_liveonly1"


@pytest.fixture
def live_only_chat(registry):
    """A chat owned by OWNER holding nothing but the live table (no file)."""
    store = local_store.ChatDataStore(LIVE_ONLY_CHAT)
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["files"] = [_db_entry(KEY, registry["tid"], registry["cid"])]
    store.write_meta(meta)
    return store


def _planner_must_not_run(monkeypatch):
    import routes.chat as chat_mod

    def boom(**kw):
        raise AssertionError("the planner must not run for a denied live table")
    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot", boom)


def test_stream_on_a_denied_live_only_chat_ends_without_the_planner(
        client, live_only_chat, monkeypatch, caplog):
    _planner_must_not_run(monkeypatch)
    with caplog.at_level(logging.INFO):
        r = client.post(f"/api/chat/{LIVE_ONLY_CHAT}/chat/stream",
                        json={"question": "how many rows?"})
    assert r.status_code == 200, r.text[:300]
    assert "Chat dataset is empty." not in r.text
    assert "no longer have access" in r.text
    assert _records(caplog, "LIVE_ROLE_DENIED")
    conv_id = json.loads(r.text.split("data: ")[1].split("\n")[0])["conv_id"]
    rows = live_only_chat.get_history(conv_id)
    ai = [m for m in rows if m.get("role") == "ai"
          and "no longer have access" in (m.get("content") or "")]
    assert len(ai) == 1, rows
    assert KEY in ai[0]["content"]


def test_edit_regenerate_on_a_denied_live_only_chat_ends_without_the_planner(
        client, live_only_chat, monkeypatch, caplog):
    conv_id = live_only_chat.new_conversation("seeded")
    live_only_chat.append_history(conv_id, {"role": "human", "content": "q1", "ts": 1.0})
    live_only_chat.append_history(conv_id, {"role": "ai", "content": "a1", "ts": 2.0})
    local_store.AuthStore().record_conversation(OWNER, LIVE_ONLY_CHAT, conv_id,
                                                title="seeded")
    _planner_must_not_run(monkeypatch)
    with caplog.at_level(logging.INFO):
        r = client.post(f"/api/chat/{LIVE_ONLY_CHAT}/edit-regenerate",
                        json={"edited_question": "again?", "conv_id": conv_id})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert "Chat dataset is empty." not in r.text
    assert "no longer have access" in body.get("answer", "")
    assert KEY in body.get("answer", "")
    assert _records(caplog, "LIVE_ROLE_DENIED")
    rows = live_only_chat.get_history(conv_id)
    assert rows[-1]["role"] == "ai"
    assert "no longer have access" in rows[-1]["content"]
    assert KEY in rows[-1]["content"]


# ===========================================================================
# 13. "Show full table" and "Download Excel" re-run a stored live SELECT:
#     the requester's role gate runs BEFORE any fetch
# ===========================================================================
FRIEND = "friend@x.com"
ROLE_DENIED_TEXT = ("Your role does not include this table's data — "
                    "refresh is unavailable.")
PREVIEW = {"columns": ["a", "b"], "rows": [{"a": 0, "b": "v0"}], "total_rows": 1}


def _full_record(chat_id, code, *, sql=None):
    """What the product leaves behind for a tabular answer: the AI row
    holding `code` (and `sql`) plus the durable full-table record the two
    routes resolve by key."""
    import routes.chat as chat_mod
    store = local_store.ChatDataStore(chat_id)
    _seed_row(chat_id, code, sql=sql)
    key = chat_mod._persist_full_table(store, PREVIEW, code, sql=sql)
    assert key
    return key


def _full_table(client, key, chat_id=CHAT):
    return client.get(f"/api/chat/{chat_id}/full_table/{key}")


def _excel(client, key, chat_id=CHAT):
    return client.post(f"/api/chat/{chat_id}/download_excel/{key}", json={})


def _assert_role_denied(r, blocked):
    assert r.status_code == 403, r.text[:300]
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "ROLE_DENIED"
    assert body["blocked_tables"] == blocked
    assert body["error"] == ROLE_DENIED_TEXT


def _add_snapshot_entry(chat, registry):
    meta = chat.read_meta()
    meta["files"].append(_db_entry("snap u", registry["snap_tid"],
                                   registry["cid"], table="u"))
    chat.write_meta(meta)
    local_store._DATAFRAME_CACHE.invalidate()


def test_full_table_and_excel_refetch_the_live_rows_for_a_granted_owner(
        client, chat, registry, granted):
    """The owner's role covers the table: both routes re-run the stored
    SELECT and serve the CURRENT rows (the table grew after the answer)."""
    key = _full_record(CHAT, CODE, sql={KEY: SQL})
    _insert_rows(registry["db"], 3)
    r = _full_table(client, key)
    assert r.status_code == 200, r.text[:300]
    assert r.json()["total_rows"] == 28
    assert r.json()["columns"] == ["a", "b"]
    r = _excel(client, key)
    assert r.status_code == 200, r.text[:300]
    assert "spreadsheetml" in r.headers["content-type"]
    assert r.content[:2] == b"PK"


def test_full_table_and_excel_refuse_an_uncovered_live_key_before_any_fetch(
        client, chat, monkeypatch, caplog):
    """OWNER holds only Base: both routes answer the refresh path's denial
    shape with 403 and issue NO database query — no LIVE_PREFETCH /
    LIVE_QUERY_OK line exists for the refused calls."""
    key = _full_record(CHAT, CODE, sql={KEY: SQL})
    _no_fetch(monkeypatch)
    with caplog.at_level(logging.INFO):
        _assert_role_denied(_full_table(client, key), [KEY])
        _assert_role_denied(_excel(client, key), [KEY])
    assert _records(caplog, "LIVE_PREFETCH") == []
    assert _records(caplog, "LIVE_QUERY_OK") == []
    assert _records(caplog, "FULL_TABLE_ROLE_DENIED")
    assert _records(caplog, "DOWNLOAD_EXCEL_ROLE_DENIED")


def test_full_table_and_excel_refuse_a_share_recipient_without_the_grant(
        client, chat, granted, monkeypatch, caplog):
    """The gate keys on the REQUESTER: the owner's grant does not carry over
    to a recipient whose role never covered the table."""
    key = _full_record(CHAT, CODE, sql={KEY: SQL})
    local_store.AuthStore().ensure_user(FRIEND)
    chat.add_share_recipients([FRIEND])
    client.post(f"/_login/{FRIEND}")
    _no_fetch(monkeypatch)
    with caplog.at_level(logging.INFO):
        _assert_role_denied(_full_table(client, key), [KEY])
        _assert_role_denied(_excel(client, key), [KEY])
    assert _records(caplog, "LIVE_PREFETCH") == []


def test_snapshot_only_answer_stays_served_on_both_routes_without_a_grant(
        client, chat, registry, monkeypatch):
    """Unchanged contract: an answer computed on a SNAPSHOT table is still
    viewable and downloadable after a role change — the gate on these two
    routes covers the live fetch only (no query is issued either way)."""
    _add_snapshot_entry(chat, registry)
    key = _full_record(CHAT, "RESULT = dfs['snap u']")
    _no_fetch(monkeypatch)
    r = _full_table(client, key)
    assert r.status_code == 200, r.text[:300]
    assert r.json()["total_rows"] == 2
    r = _excel(client, key)
    assert r.status_code == 200, r.text[:300]
    assert r.content[:2] == b"PK"


@pytest.mark.parametrize("route", ["full_table", "download_excel"])
def test_full_table_gate_crash_fails_closed_without_a_fetch(
        client, chat, granted, monkeypatch, caplog, route):
    """A crash inside the role gate on these two routes refuses (403, the
    denial shape with no table named) instead of letting the fetch through:
    `LIVE_REEXEC_GATE_FAILED` names the exception type, and no query runs."""
    import routes.chat as chat_mod
    key = _full_record(CHAT, CODE, sql={KEY: SQL})

    def boom(email, chat_id, code):
        raise RuntimeError("gate unavailable")
    monkeypatch.setattr(chat_mod, "_role_refresh_block", boom)
    _no_fetch(monkeypatch)
    with caplog.at_level(logging.INFO):
        r = _full_table(client, key) if route == "full_table" else _excel(client, key)
    _assert_role_denied(r, [])
    failed = _records(caplog, "LIVE_REEXEC_GATE_FAILED")
    assert failed
    assert any("RuntimeError" in x.getMessage() for x in failed)
    assert _records(caplog, "LIVE_PREFETCH") == []


# ===========================================================================
# 14. the `df` alias on the re-execution paths: `df` is the FIRST frame, so
#     on a chat whose first frame is a live table it references that table
# ===========================================================================
def test_reexecute_full_df_fetches_for_df_only_code_on_a_live_first_chat(
        client, live_only_chat, registry, granted):
    """Stored code that uses only `df` on a chat whose first (and only) frame
    is the live table re-runs the stored SELECT — the executor binds `df`
    to that frame, so the placeholder must never be what it computes on."""
    import routes.chat as chat_mod
    _seed_row(LIVE_ONLY_CHAT, "RESULT = df", sql={KEY: SQL})
    _insert_rows(registry["db"], 2)
    df = asyncio.run(chat_mod._reexecute_full_df(LIVE_ONLY_CHAT, "RESULT = df"))
    assert df is not None
    assert len(df) == 27
    assert list(df.columns) == ["a", "b"]


def test_persist_full_table_keeps_the_sql_of_a_df_only_answer(client, live_only_chat):
    """`_persist_full_table(..., first_key=)` — the first frame's key — lets
    a `df`-only answer keep its SELECT on the durable record."""
    import routes.chat as chat_mod
    key = chat_mod._persist_full_table(live_only_chat, PREVIEW, "RESULT = df",
                                       sql={KEY: SQL}, first_key=KEY)
    assert key
    rec = chat_mod._load_full_table_record(live_only_chat, key)
    assert rec["code"] == "RESULT = df"
    assert rec["sql"] == {KEY: SQL}


@pytest.mark.parametrize("route", ["full_table", "download_excel"])
def test_full_table_gate_fails_closed_when_the_role_lookup_itself_fails(
        client, chat, granted, monkeypatch, caplog, route):
    """The REAL `_role_refresh_block` swallows a crash of the roles lookup
    and answers "nothing denied" (`ROLE_GATE_FAILED`). On these two routes
    that answer must not let a live fetch through: 403 with the denial
    shape and no table named, `LIVE_REEXEC_GATE_FAILED`, no query."""
    key = _full_record(CHAT, CODE, sql={KEY: SQL})

    def boom(email):
        raise RuntimeError("roles unreadable")
    monkeypatch.setattr(roles_store, "allowed_table_ids_for", boom)
    _no_fetch(monkeypatch)
    with caplog.at_level(logging.INFO):
        r = _full_table(client, key) if route == "full_table" else _excel(client, key)
    _assert_role_denied(r, [])
    assert _records(caplog, "LIVE_REEXEC_GATE_FAILED")
    assert _records(caplog, "LIVE_PREFETCH") == []


def test_refresh_item_keeps_its_fail_open_gate_when_the_role_lookup_fails(
        client, chat, granted, monkeypatch, caplog):
    """Companion, unchanged behaviour: `refresh_item`'s gate fails OPEN on
    the same crash (`ROLE_GATE_FAILED`) and the refresh proceeds to its own
    pre-fetch — here `_no_fetch` makes that pre-fetch fail, so the answer
    is the execution-failure shape (200, ok false), not a role denial."""
    _seed_row(CHAT, CODE, sql={KEY: SQL})

    def boom(email):
        raise RuntimeError("roles unreadable")
    monkeypatch.setattr(roles_store, "allowed_table_ids_for", boom)
    _no_fetch(monkeypatch)
    with caplog.at_level(logging.INFO):
        r = client.post(f"/api/chat/{CHAT}/refresh_item",
                        json={"code": CODE, "kind": "table"})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["ok"] is False
    assert body.get("code") != "ROLE_DENIED"
    assert _records(caplog, "ROLE_GATE_FAILED")
    assert _records(caplog, "LIVE_REEXEC_GATE_FAILED") == []
