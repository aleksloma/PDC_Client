"""The role gate on the refresh paths: per-table df-key semantics on
/api/chat/{id}/refresh_item (200 {ok:false, code:ROLE_DENIED} — the execution-
failure contract), connectors exempt, the gate keying on the REQUESTER for
shared recipients, the dashboard tile refresh blocked on BOTH branches
(run_item_refresh + _reexecute_full_df) with the caller-specific role_denied
reason never persisted, /schema's additive per-table `allowed` flag, file-only
chats performing zero role reads, and drop_df_keys hiding denied frames from
the exec namespace. Offline — table-kind refreshes only (no chart rendering)."""
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import db_sources
import local_store
import roles_store
from settings import settings

ADMIN = "ladmin"
OWNER = "user@x.com"
FRIEND = "friend@x.com"
CHAT = "c_rolegate1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    local_store._DATAFRAME_CACHE.invalidate()
    for email in (OWNER, FRIEND):
        local_store.AuthStore().ensure_user(email)
    roles_store.RolesStore().ensure_base_role()

    import routes.chat as chat_mod
    import routes.dashboards as dash_mod
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(chat_mod.router)
    app.include_router(dash_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    yield tc
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def registry(client):
    """Two normal tables + one connector, snapshots on disk, no grants."""
    import pandas as pd
    store = db_sources.DataSourceStore()
    conn = store.create_connection(
        {"name": "c", "db_type": "postgresql", "host": "h", "port": 5432,
         "database": "d", "user": "u"}, "pw", actor=ADMIN)
    ids = {"conn": conn["id"]}
    for tname, disp, is_conn in [("cl_info", "clients information", False),
                                 ("tr_data", "transactions", False),
                                 ("city_dict", "cities dictionary", True)]:
        t = store.upsert_table({
            "connection_id": conn["id"], "schema": "shop", "table_name": tname,
            "display_name": disp, "description": "", "is_connector": is_conn,
            "relations": [], "columns": []}, actor=ADMIN)
        ids[tname] = t["id"]
        pd.DataFrame({"city_code": [1, 2], "amount": [10, 20]}).to_parquet(
            local_store.db_snapshot_path(t["id"]))
    return ids


def _db_entry(df_key, tid, *, connector=False):
    """Chat-meta DB entry in the frozen _db_meta_entry shape."""
    return {"file_name": df_key, "file_description": "", "source": "database",
            "db": {"table_id": tid, "connection_id": "x", "schema": "shop",
                   "table_name": df_key, "display_name": df_key,
                   "is_connector": connector, "auto_included": connector,
                   "row_count": 2, "refreshed_at": None, "relations": []},
            "schema": {"file_name": df_key, "fields": {}}}


@pytest.fixture
def chat(registry):
    """A chat owned by OWNER using both normal tables + the connector."""
    store = local_store.ChatDataStore(CHAT)
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["files"] = [
        _db_entry("clients information", registry["cl_info"]),
        _db_entry("transactions", registry["tr_data"]),
        _db_entry("cities dictionary", registry["city_dict"], connector=True),
    ]
    store.write_meta(meta)
    return store


def _grant(email, table_ids):
    role = roles_store.RolesStore().create_role(
        {"name": f"R-{email}-{len(table_ids)}", "table_ids": table_ids},
        actor=ADMIN)
    local_store.AuthStore().set_data_role(email, role["id"])
    return role


def _refresh(client, code):
    return client.post(f"/api/chat/{CHAT}/refresh_item",
                       json={"code": code, "kind": "table"}).json()


# ── chat refresh_item ──────────────────────────────────────────────────────

def test_refresh_ok_with_access(client, registry, chat):
    _grant(OWNER, [registry["cl_info"], registry["tr_data"]])
    out = _refresh(client, "RESULT = dfs['clients information']")
    assert out["ok"] is True and out["kind"] == "table"
    assert out["table"]["total_rows"] == 2


def test_refresh_blocked_after_revocation(client, registry, chat):
    """Base role (no grants) — the snapshot data stays viewable elsewhere,
    only the refresh is refused, via the 200 execution-failure contract."""
    out = _refresh(client, "RESULT = dfs['clients information']")
    assert out["ok"] is False
    assert out["code"] == "ROLE_DENIED"
    assert out["blocked_tables"] == ["clients information"]
    assert "role" in out["error"].lower()


def test_refresh_per_table_semantics(client, registry, chat):
    """Chat holds a denied table, but this ITEM only touches a granted one —
    the refresh proceeds (per-table, not per-chat)."""
    _grant(OWNER, [registry["tr_data"]])
    out = _refresh(client, "RESULT = dfs['transactions']")
    assert out["ok"] is True


def test_refresh_connector_reference_not_blocked(client, registry, chat):
    """Connectors are exempt even with zero grants."""
    out = _refresh(client, "RESULT = dfs['cities dictionary']")
    assert out["ok"] is True


def test_refresh_gate_keys_on_requester_for_shared_chat(client, registry, chat):
    chat.add_share_recipients([FRIEND])
    _grant(OWNER, [registry["cl_info"]])          # owner can refresh…
    client.post(f"/_login/{FRIEND}")               # …the recipient cannot
    out = _refresh(client, "RESULT = dfs['clients information']")
    assert out["ok"] is False and out["code"] == "ROLE_DENIED"
    _grant(FRIEND, [registry["cl_info"]])
    out2 = _refresh(client, "RESULT = dfs['clients information']")
    assert out2["ok"] is True


def test_drop_df_keys_hides_denied_frames_from_exec(client, registry, chat):
    """Defense in depth: code that never subscripts the denied table (so the
    key regex cannot block it) still must not see the denied frame."""
    _grant(OWNER, [registry["tr_data"]])
    out = _refresh(client,
                   "import pandas as pd\n"
                   "RESULT = pd.DataFrame({'k': sorted(dfs.keys())})")
    assert out["ok"] is True
    keys = [r["k"] for r in out["table"]["rows"]]
    assert "clients information" not in keys
    assert "transactions" in keys


def test_file_only_chat_performs_zero_role_reads(client, monkeypatch, tmp_path):
    """No DB entries → the gate returns before any roles/registry read (a
    poisoned resolver proves it is never called), and refresh works as before."""
    store = local_store.ChatDataStore("c_files1")
    (store.files_dir / "old.csv").write_text("x\n1\n2\n", encoding="utf-8")
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["files"] = [{"file_name": "old.csv", "file_description": "",
                      "schema": {"file_name": "old.csv", "fields": {}}}]
    store.write_meta(meta)

    def _boom(email):
        raise AssertionError("role resolver must not run for file-only chats")
    monkeypatch.setattr(roles_store, "allowed_table_ids_for", _boom)
    out = client.post("/api/chat/c_files1/refresh_item",
                      json={"code": "RESULT = dfs['old.csv']",
                            "kind": "table"}).json()
    assert out["ok"] is True


# ── /schema allowed flag ───────────────────────────────────────────────────

def test_schema_emits_per_table_allowed_flags(client, registry, chat):
    _grant(OWNER, [registry["tr_data"]])
    rows = client.get(f"/api/chat/{CHAT}/schema").json()["db_tables"]
    by_key = {r["df_key"]: r for r in rows}
    assert by_key["transactions"]["allowed"] is True
    assert by_key["clients information"]["allowed"] is False
    assert by_key["cities dictionary"]["allowed"] is True   # connector: always


# ── dashboard tile refresh (both branches) ─────────────────────────────────

def _make_dashboard_with_tile(tile):
    ds = local_store.DashboardStore()
    row = ds.create_dashboard(OWNER, "D")
    saved = ds.add_tile(OWNER, row["dash_id"], tile)
    return row["dash_id"], saved["tile_id"]


def test_dashboard_tile_role_denied_run_item_refresh_branch(client, registry, chat):
    dash_id, tile_id = _make_dashboard_with_tile({
        "chat_id": CHAT, "kind": "chart",
        "code": "dfs['clients information'].plot()",
        "snapshot": {"image_base64": "abc", "is_plotly": False}})
    r = client.post(f"/api/dashboards/{dash_id}/tiles/{tile_id}/refresh")
    body = r.json()
    assert body == {"ok": False, "frozen": True, "reason": "role_denied",
                    "blocked_tables": ["clients information"]}
    # Caller-specific: never persisted into the shared doc.
    doc = local_store.DashboardStore().get_dashboard(OWNER, dash_id)
    assert doc["tiles"][0]["frozen"] is False


def test_dashboard_tile_role_denied_reexecute_branch(client, registry, chat):
    """The table+result_key branch bypasses run_item_refresh — the gate sits
    BEFORE the branch split so it is covered too."""
    dash_id, tile_id = _make_dashboard_with_tile({
        "chat_id": CHAT, "kind": "table", "result_key": "k1",
        "code": "RESULT = {'k1': dfs['clients information']}",
        "snapshot": {"table": {"columns": ["a"], "rows": [{"a": 1}],
                               "total_rows": 1}}})
    body = client.post(f"/api/dashboards/{dash_id}/tiles/{tile_id}/refresh").json()
    assert body["ok"] is False and body["reason"] == "role_denied"


def test_dashboard_tile_refresh_ok_with_access_reexecute_branch(client, registry, chat):
    _grant(OWNER, [registry["tr_data"]])
    dash_id, tile_id = _make_dashboard_with_tile({
        "chat_id": CHAT, "kind": "table", "result_key": "k1",
        "code": "RESULT = {'k1': dfs['transactions']}",
        "snapshot": {"table": {"columns": ["a"], "rows": [{"a": 1}],
                               "total_rows": 1}}})
    body = client.post(f"/api/dashboards/{dash_id}/tiles/{tile_id}/refresh").json()
    assert body["ok"] is True
    assert body["table"]["total_rows"] == 2
