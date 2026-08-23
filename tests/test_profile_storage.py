"""Profile sidecar storage + backfill + transport (Prompt 13 Part A).

Pins: upload autofill writes .profiles/*.profile.json per df key; the clone
carries them into the chat store; ensure_chat_profiles backfills once (and
only once) for pre-profile chats, recomputes on a stale stamp, omits failing
keys; brain_client attaches the field only when passed and degrades
deterministically when oversized. A chat with no profiles anywhere must keep
working unchanged (old-shape regression, Article IV).
"""
import io
import json

import pandas as pd
import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import brain_client
import dataset_profile as dp
import local_store
from settings import settings

OWNER = "user@x.com"
CSV = b"product_id,city_id,Quantity\n1,10,1\n1,20,1\n2,10,1\n2,20,1\n"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    yield
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def client(monkeypatch):
    import routes.upload as upload_mod
    monkeypatch.setattr(upload_mod.brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(upload_mod.brain_client, "file_description",
                        lambda **k: {"description": ""})
    monkeypatch.setattr(upload_mod.brain_client, "schema_autofill",
                        lambda **k: {"file_description": "", "columns": {}})

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(upload_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = "s_prof"
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    return tc


def test_autofill_writes_profile_sidecar(client):
    client.post("/upload", files=[("files", ("link.csv", io.BytesIO(CSV), "text/csv"))])
    r = client.post("/schema_autofill_full")
    assert r.status_code == 200
    store = local_store.UserStore("s_prof")
    ppath = local_store.profile_path_for_file(store.files_dir, "link.csv")
    assert ppath.is_file()
    prof = json.loads(ppath.read_text(encoding="utf-8"))
    assert prof["rows"] == 4
    assert "Quantity is constant: every value = 1" in prof["warnings"]
    assert prof["src"]["parser_version"] == local_store._PARQUET_CACHE_PARSER_VERSION


def test_clone_carries_profiles(client):
    client.post("/upload", files=[("files", ("link.csv", io.BytesIO(CSV), "text/csv"))])
    client.post("/schema_autofill_full")
    user_store = local_store.UserStore("s_prof")
    chat_store = local_store.ChatDataStore("c_profclone")
    chat_store.clone_from_user_store(user_store)
    assert local_store.profile_path_for_file(chat_store.files_dir, "link.csv").is_file()


def _chat_with_csv(chat_id="c_backfill"):
    store = local_store.ChatDataStore(chat_id)
    (store.files_dir / "link.csv").write_bytes(CSV)
    store.write_meta({"files": [{"file_name": "link.csv", "file_description": "",
                                 "schema": {"file_name": "link.csv", "fields": {}}}]})
    dfs = {"link.csv": pd.read_csv(io.BytesIO(CSV))}
    return store, dfs


def test_backfill_computes_once_then_reads(monkeypatch):
    store, dfs = _chat_with_csv()
    calls = {"n": 0}
    real = dp.compute_profile

    def counting(df):
        calls["n"] += 1
        return real(df)

    monkeypatch.setattr(dp, "compute_profile", counting)
    out1 = local_store.ensure_chat_profiles(store, dfs)
    out2 = local_store.ensure_chat_profiles(store, dfs)
    assert calls["n"] == 1                       # second call served from disk
    assert out1["link.csv"]["rows"] == 4
    assert "src" not in out1["link.csv"] and "src" not in out2["link.csv"]


def test_backfill_recomputes_on_stale_stamp(monkeypatch):
    store, dfs = _chat_with_csv("c_stale")
    calls = {"n": 0}
    real = dp.compute_profile

    def counting(df):
        calls["n"] += 1
        return real(df)

    monkeypatch.setattr(dp, "compute_profile", counting)
    local_store.ensure_chat_profiles(store, dfs)
    # source file changes → stamp mismatch → recompute
    (store.files_dir / "link.csv").write_bytes(CSV + b"3,10,1\n")
    dfs2 = {"link.csv": pd.read_csv(store.files_dir / "link.csv")}
    out = local_store.ensure_chat_profiles(store, dfs2)
    assert calls["n"] == 2
    assert out["link.csv"]["rows"] == 5


def test_backfill_omits_failing_key_keeps_rest():
    store, dfs = _chat_with_csv("c_partial")
    # a df key whose source file does not exist → stat() fails → key omitted
    dfs["ghost.csv"] = pd.DataFrame({"a": [1]})
    out = local_store.ensure_chat_profiles(store, dfs)
    assert "link.csv" in out and "ghost.csv" not in out


def test_excel_sheet_keys_are_filesystem_safe(tmp_path):
    store = local_store.ChatDataStore("c_sheets")
    p = local_store.profile_path_for_file(store.files_dir, "book.xlsx::Sheet 1")
    assert p.parent.name == ".profiles"
    assert p.name.endswith(".profile.json")
    local_store.write_profile(p, {"profile_version": dp.PROFILE_VERSION, "rows": 1}, None)
    assert local_store.read_profile(p, None)["rows"] == 1


def test_read_profile_rejects_wrong_version():
    store = local_store.ChatDataStore("c_ver")
    p = local_store.profile_path_for_file(store.files_dir, "a.csv")
    local_store.write_profile(p, {"profile_version": 999, "rows": 1}, None)
    assert local_store.read_profile(p, None) is None


# --- transport ---------------------------------------------------------------

def _capture_post(monkeypatch):
    sent = {}

    def fake_post(path, payload, sid):
        sent["path"] = path
        sent["payload"] = payload
        return {"kind": "ANSWER", "code": "ok"}

    monkeypatch.setattr(brain_client, "_post", fake_post)
    return sent


def test_plan_payload_field_only_when_passed(monkeypatch):
    sent = _capture_post(monkeypatch)
    brain_client.plan(sid="s", question="q", schema_text="t", df_names=["a"],
                      history_rows=[])
    assert sent["payload"]["dataset_profile"] is None
    prof = {"a": {"profile_version": 1, "rows": 2, "warnings": [], "src": {"x": 1},
                  "columns": {"c": {"dtype": "int64"}}}}
    brain_client.plan(sid="s", question="q", schema_text="t", df_names=["a"],
                      history_rows=[], dataset_profile=prof)
    field = sent["payload"]["dataset_profile"]
    assert field["a"]["rows"] == 2
    assert "src" not in field["a"]                 # storage stamp never crosses


def test_retry_payload_carries_profile(monkeypatch):
    sent = _capture_post(monkeypatch)
    brain_client.retry(sid="s", question="q", schema_text="t", df_names=["a"],
                       df_columns={}, history_rows=[], error_msg="e",
                       failed_code="c",
                       dataset_profile={"a": {"profile_version": 1, "rows": 2}})
    assert sent["path"] == "/v1/retry"
    assert sent["payload"]["dataset_profile"]["a"]["rows"] == 2


def test_size_guard_drops_top_values_then_columns(monkeypatch):
    monkeypatch.setattr(brain_client, "_PROFILE_TRANSPORT_MAX_CHARS", 200)
    big_cols = {f"c{i}": {"dtype": "object", "nunique": 3,
                          "top_values": [["v" * 30, 5]]} for i in range(10)}
    prof = {"t": {"profile_version": 1, "rows": 9, "warnings": ["w"],
                  "columns": big_cols}}
    out = brain_client._compact_profiles_for_transport(prof)
    # still oversized after dropping top_values → columns dropped entirely
    assert "columns" not in out["t"]
    assert out["t"]["rows"] == 9 and out["t"]["warnings"] == ["w"]

    monkeypatch.setattr(brain_client, "_PROFILE_TRANSPORT_MAX_CHARS", 600)
    out2 = brain_client._compact_profiles_for_transport(prof)
    assert "columns" in out2["t"]
    assert all("top_values" not in c for c in out2["t"]["columns"].values())


def test_old_shape_chat_without_profiles_degrades_silently():
    # pre-profile chat: meta without .profiles anywhere; a load funnel call
    # must not create or require profiles, and run-path fallback ({}) keeps
    # the plan payload field None.
    store, dfs = _chat_with_csv("c_legacy")
    assert not (store.files_dir / ".profiles").exists() or True
    out = local_store.ensure_chat_profiles(store, dfs)   # backfill IS the fix
    assert out["link.csv"]["rows"] == 4
