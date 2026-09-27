"""The role gate on the refresh paths: per-table df-key semantics on
/api/chat/{id}/refresh_item (200 {ok:false, code:ROLE_DENIED} — the execution-
failure contract), connectors exempt, the gate keying on the REQUESTER for
shared recipients, the dashboard tile refresh blocked on BOTH branches
(run_item_refresh + _reexecute_full_df) with the caller-specific role_denied
reason never persisted, /schema's additive per-table `allowed` flag, file-only
chats performing zero role reads, the referencing rule (`df`, `dfs.get(...)`,
a generic `dfs` walk) refusing a denied SNAPSHOT table on refresh_item and on
both tile branches with the stored tile document untouched (Task 14b item 3,
D14b-4 — the chat fixture's FIRST frame is the denied "clients information"),
and drop_df_keys hiding denied frames from the exec namespace at the helper
level. Offline — table-kind refreshes only (no chart rendering).

Every refreshed snippet is first seeded as a real AI history row
(`conftest.seed_history`): refresh_item accepts only code the chat's history
holds, and that check runs BEFORE the role gate these tests exercise."""
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import db_sources
import local_store
import roles_store
from conftest import seed_history
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
    seed_history(CHAT, code)
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


def test_a_generic_dfs_walk_over_a_denied_snapshot_table_is_refused(client, registry, chat):
    """INVERTED from `test_drop_df_keys_hides_denied_frames_from_exec` (Task
    14b item 3): code that never subscripts the denied table but walks `dfs`
    generically (`dfs.keys()`) REFERENCES every denied key by the pre-fetch's
    own rule, so the refresh is refused naming the table — before, the
    denied frame was dropped and the walk silently ran on what was left."""
    _grant(OWNER, [registry["tr_data"]])
    out = _refresh(client,
                   "import pandas as pd\n"
                   "RESULT = pd.DataFrame({'k': sorted(dfs.keys())})")
    assert out["ok"] is False, out
    assert out.get("code") == "ROLE_DENIED", out
    assert out.get("blocked_tables") == ["clients information"], out


def test_drop_df_keys_hides_denied_frames_from_the_exec_namespace(client, registry, chat):
    """Defence in depth kept at the HELPER level: `run_item_refresh(...,
    drop_df_keys=)` never shows a dropped frame to the code, whatever the
    route's referencing rule decided. Called directly (the helper has no
    gate of its own), so a generic walk is the right probe here."""
    import asyncio
    import routes.chat as chat_mod
    code = ("import pandas as pd\n"
            "RESULT = pd.DataFrame({'k': sorted(dfs.keys())})")
    out = asyncio.run(chat_mod.run_item_refresh(
        CHAT, code, "table", "s_dropkeys01",
        drop_df_keys=frozenset({"clients information"})))
    assert out["ok"] is True, out
    keys = [r["k"] for r in out["table"]["rows"]]
    assert "clients information" not in keys
    assert "transactions" in keys


# ── Task 14b item 3: a denied SNAPSHOT table reached without a subscript ──
# The chat fixture's FIRST frame is the denied "clients information", so `df`
# is that frame. OWNER is granted "transactions" only; the connector is exempt.
DENIED_FORMS = [
    ("df-alias", "RESULT = df"),
    ("dfs-get", "RESULT = dfs.get('clients information')"),
    ("iteration", "RESULT = [k for k in dfs]"),
    ("items", "RESULT = list(dfs.items())"),
    ("values", "RESULT = list(dfs.values())"),
    ("membership", "RESULT = 'x' in dfs"),
]


@pytest.mark.parametrize("code", [c for _, c in DENIED_FORMS],
                         ids=[i for i, _ in DENIED_FORMS])
def test_refresh_item_refuses_a_denied_snapshot_table_named_without_a_subscript(
        client, registry, chat, code):
    """`df` (the first frame), `dfs.get(...)` and every generic `dfs` walk
    reference the denied snapshot table exactly as the `dfs['…']` form does:
    200 with the denial shape naming the table's display name."""
    _grant(OWNER, [registry["tr_data"]])
    out = _refresh(client, code)
    assert out["ok"] is False, out
    assert out.get("code") == "ROLE_DENIED", out
    assert out.get("blocked_tables") == ["clients information"], out
    assert "role" in (out.get("error") or "").lower(), out


def test_refresh_item_refuses_df_even_when_rebound_to_an_allowed_frame(
        client, registry, chat):
    """D14b-4, the accepted over-refusal: `df = dfs['transactions']; RESULT =
    df` on a chat whose FIRST frame is a denied snapshot table is refused —
    one rule (the `df` word names the first frame), the fail-closed
    direction."""
    _grant(OWNER, [registry["tr_data"]])
    out = _refresh(client, "df = dfs['transactions']\nRESULT = df")
    assert out["ok"] is False, out
    assert out.get("code") == "ROLE_DENIED", out
    assert out.get("blocked_tables") == ["clients information"], out


def test_refresh_item_still_runs_code_naming_only_the_allowed_table(client, registry, chat):
    """Keep-green guard next to the new refusals: a subscript of the granted
    table only (no `df`, no generic `dfs`) proceeds as before."""
    _grant(OWNER, [registry["tr_data"]])
    out = _refresh(client, "RESULT = dfs['transactions']")
    assert out["ok"] is True, out
    assert out["table"]["total_rows"] == 2


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
    seed_history("c_files1", "RESULT = dfs['old.csv']")
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


# ── Task 14b item 3: both tile branches, the stored document untouched ─────
def _dash_doc_path(dash_id):
    from pathlib import Path
    return Path(settings.DATA_ROOT) / "users" / OWNER / "dashboards" / f"{dash_id}.json"


def _assert_tile_role_denied(body):
    assert body == {"ok": False, "frozen": True, "reason": "role_denied",
                    "blocked_tables": ["clients information"]}, body


@pytest.mark.parametrize("code", [c for _, c in DENIED_FORMS],
                         ids=[i for i, _ in DENIED_FORMS])
def test_dashboard_tile_chart_branch_refuses_a_denied_snapshot_table_without_a_subscript(
        client, registry, chat, code):
    """The chart kind takes the `run_item_refresh` branch. The role gate
    refuses with the caller-specific freeze naming the table, and the stored
    tile document is byte-identical afterwards (nothing persisted)."""
    _grant(OWNER, [registry["tr_data"]])
    dash_id, tile_id = _make_dashboard_with_tile({
        "chat_id": CHAT, "kind": "chart", "code": code,
        "snapshot": {"image_base64": "abc", "is_plotly": False}})
    before_bytes = _dash_doc_path(dash_id).read_bytes()
    before_doc = local_store.DashboardStore().get_dashboard(OWNER, dash_id)
    body = client.post(f"/api/dashboards/{dash_id}/tiles/{tile_id}/refresh").json()
    _assert_tile_role_denied(body)
    assert _dash_doc_path(dash_id).read_bytes() == before_bytes, "the tile document changed"
    assert local_store.DashboardStore().get_dashboard(OWNER, dash_id) == before_doc


TABLE_DENIED_FORMS = [
    ("df-alias", "RESULT = {'k1': df}"),
    ("dfs-get", "RESULT = {'k1': dfs.get('clients information')}"),
    ("iteration", "RESULT = {'k1': dfs[[k for k in dfs][0]]}"),
    ("items", "RESULT = {'k1': [v for k, v in dfs.items()][0]}"),
    ("values", "RESULT = {'k1': list(dfs.values())[0]}"),
    ("membership", "RESULT = {'k1': df if 'x' in dfs else None}"),
]


@pytest.mark.parametrize("code", [c for _, c in TABLE_DENIED_FORMS],
                         ids=[i for i, _ in TABLE_DENIED_FORMS])
def test_dashboard_tile_table_branch_refuses_a_denied_snapshot_table_without_a_subscript(
        client, registry, chat, code):
    """The table kind WITH `result_key` takes the `_reexecute_full_df`
    branch — the one that used to persist ANOTHER table's rows into the
    shared dashboard document when `df` silently bound to the next frame.
    Refused before the branch split; the stored snapshot is unchanged."""
    _grant(OWNER, [registry["tr_data"]])
    snapshot = {"table": {"columns": ["a"], "rows": [{"a": 1}], "total_rows": 1}}
    dash_id, tile_id = _make_dashboard_with_tile({
        "chat_id": CHAT, "kind": "table", "result_key": "k1", "code": code,
        "snapshot": snapshot})
    before_bytes = _dash_doc_path(dash_id).read_bytes()
    body = client.post(f"/api/dashboards/{dash_id}/tiles/{tile_id}/refresh").json()
    _assert_tile_role_denied(body)
    assert _dash_doc_path(dash_id).read_bytes() == before_bytes, "the tile document changed"
    stored = local_store.DashboardStore().get_dashboard(OWNER, dash_id)["tiles"][0]
    assert stored["snapshot"]["table"] == snapshot["table"], stored
    assert stored.get("frozen") is False, stored


# ── Task 14b review: a failure AFTER the denial is known fails CLOSED ──────
# The gate loads the chat's frames only once a table is denied. When that load
# (or the referencing rule) fails, the denial is already known: the gate must
# refuse naming every denied table — on a snapshot-only chat a `_GateFailed`
# answer would fail OPEN (no drop, no refusal) and hand the denied frames to
# the executor even for an explicit `dfs['denied']`.
def _fail_first_frame_load(monkeypatch):
    """`ChatDataStore.load_dataframes` raises on its FIRST call (the gate's)
    and delegates afterwards; returns the call log, so a test can prove the
    refresh itself never loaded frames (nothing executed)."""
    real = local_store.ChatDataStore.load_dataframes
    calls = []

    def _load(self, *a, **kw):
        calls.append(self.chat_id)
        if len(calls) == 1:
            raise OSError("frame load failed")
        return real(self, *a, **kw)

    monkeypatch.setattr(local_store.ChatDataStore, "load_dataframes", _load)
    return calls


@pytest.mark.parametrize("code", ["RESULT = dfs['clients information']",
                                  "RESULT = dfs['transactions']"],
                         ids=["denied-subscript", "allowed-subscript"])
def test_refresh_item_refuses_when_the_gate_frame_load_fails(
        client, registry, chat, monkeypatch, code):
    _grant(OWNER, [registry["tr_data"]])
    seed_history(CHAT, code)
    calls = _fail_first_frame_load(monkeypatch)
    out = client.post(f"/api/chat/{CHAT}/refresh_item",
                      json={"code": code, "kind": "table"}).json()
    assert out["ok"] is False, out
    assert out.get("code") == "ROLE_DENIED", out
    assert out.get("blocked_tables") == ["clients information"], out
    assert calls == [CHAT], f"the refresh loaded frames after the gate failed: {calls}"


@pytest.mark.parametrize("code", ["RESULT = {'k1': dfs['clients information']}",
                                  "RESULT = {'k1': dfs['transactions']}"],
                         ids=["denied-subscript", "allowed-subscript"])
@pytest.mark.parametrize("kind", ["chart", "table"])
def test_dashboard_tile_refuses_when_the_gate_frame_load_fails(
        client, registry, chat, monkeypatch, code, kind):
    _grant(OWNER, [registry["tr_data"]])
    tile = {"chat_id": CHAT, "kind": kind, "code": code,
            "snapshot": {"image_base64": "abc", "is_plotly": False}}
    if kind == "table":
        tile.update({"result_key": "k1", "snapshot": {"table": {
            "columns": ["a"], "rows": [{"a": 1}], "total_rows": 1}}})
    dash_id, tile_id = _make_dashboard_with_tile(tile)
    before_bytes = _dash_doc_path(dash_id).read_bytes()
    calls = _fail_first_frame_load(monkeypatch)
    body = client.post(f"/api/dashboards/{dash_id}/tiles/{tile_id}/refresh").json()
    _assert_tile_role_denied(body)
    assert _dash_doc_path(dash_id).read_bytes() == before_bytes, "the tile document changed"
    assert calls == [CHAT], f"the refresh loaded frames after the gate failed: {calls}"
