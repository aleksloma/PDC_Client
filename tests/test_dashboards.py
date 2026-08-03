"""Unit tests for the Dashboards feature (routes/dashboards.py + DashboardStore).

Everything is offline and client-local: DATA_ROOT is isolated to tmp_path,
chat auth is stubbed via local_store.chat_exists / get_chat_meta_owner, the
brain share-email relay is stubbed, and tile re-execution is stubbed at the
routes.dashboards.run_item_refresh seam.
"""
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import local_store
import routes.chat as chat_mod
import routes.dashboards as dash_mod
from settings import settings

OWNER = "alice@acme.com"
FRIEND = "bob@acme.com"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(dash_mod.router)
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    return TestClient(app)


def _stub_chat(monkeypatch, owner=OWNER, exists=True):
    monkeypatch.setattr(local_store, "chat_exists", lambda cid: exists)
    monkeypatch.setattr(local_store, "get_chat_meta_owner", lambda cid: owner)


def _stub_share_email(monkeypatch):
    calls = []

    def fake(**kw):
        calls.append(kw)
        return {"smtp_configured": True, "sent": kw.get("to") or [], "failed": []}

    monkeypatch.setattr(dash_mod.brain_client, "send_share_email", fake)
    return calls


def _mk_dash(client, name="Sales"):
    resp = client.post("/api/dashboards", json={"name": name})
    assert resp.status_code == 200
    return resp.json()["dash_id"]


def _pin_chart(client, dash_id, chat_id="chat123", **extra):
    body = {"chat_id": chat_id, "kind": "chart",
            "description": "Monthly revenue by region\nmore text",
            "code": "import plotly.express as px",
            "image_base64": "iVBORfakepng", "is_plotly": False}
    body.update(extra)
    return client.post(f"/api/dashboards/{dash_id}/tiles", json=body)


# ---------------------------------------------------------------------------
# Auth + CRUD
# ---------------------------------------------------------------------------

def test_all_endpoints_401_without_session(client):
    dash, tile = "a" * 16, "b" * 16
    assert client.get("/api/dashboards").status_code == 401
    assert client.post("/api/dashboards", json={"name": "x"}).status_code == 401
    assert client.get(f"/api/dashboards/{dash}").status_code == 401
    for path in (f"/api/dashboards/{dash}/rename", f"/api/dashboards/{dash}/delete",
                 f"/api/dashboards/{dash}/tiles", f"/api/dashboards/{dash}/layout",
                 f"/api/dashboards/{dash}/tiles/{tile}/remove",
                 f"/api/dashboards/{dash}/tiles/{tile}/refresh",
                 f"/api/dashboards/{dash}/share"):
        assert client.post(path, json={}).status_code == 401, path


def test_create_validates_name(client):
    client.post(f"/_login/{OWNER}")
    assert client.post("/api/dashboards", json={"name": ""}).status_code == 400
    assert client.post("/api/dashboards", json={"name": "x" * 101}).status_code == 400
    row = client.post("/api/dashboards", json={"name": "  Ops KPIs  "}).json()
    assert row["name"] == "Ops KPIs"
    assert local_store.DashboardStore.valid_id(row["dash_id"])


def test_list_sorted_by_last_used_after_get_bump(client):
    client.post(f"/_login/{OWNER}")
    first = _mk_dash(client, "First")
    second = _mk_dash(client, "Second")
    # Second is newer → listed first; opening First bumps it to the top.
    names = [r["name"] for r in client.get("/api/dashboards").json()["dashboards"]]
    assert names == ["Second", "First"]
    assert client.get(f"/api/dashboards/{first}").status_code == 200
    names = [r["name"] for r in client.get("/api/dashboards").json()["dashboards"]]
    assert names == ["First", "Second"]


def test_unknown_and_malformed_ids_404(client):
    client.post(f"/_login/{OWNER}")
    assert client.get("/api/dashboards/" + "f" * 16).status_code == 404
    assert client.get("/api/dashboards/..%2f..%2fescape").status_code == 404
    assert client.post("/api/dashboards/" + "f" * 16 + "/rename",
                       json={"name": "x"}).status_code == 404


def test_rename_and_delete_idempotent(client):
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    assert client.post(f"/api/dashboards/{dash}/rename", json={"name": "Renamed"}).json()["ok"]
    assert client.get(f"/api/dashboards/{dash}").json()["name"] == "Renamed"
    assert client.post(f"/api/dashboards/{dash}/delete", json={}).json()["ok"]
    # Second delete: no own doc, no pointer — still ok:true (idempotent).
    assert client.post(f"/api/dashboards/{dash}/delete", json={}).json()["ok"]
    assert client.get(f"/api/dashboards/{dash}").status_code == 404


# ---------------------------------------------------------------------------
# Tiles
# ---------------------------------------------------------------------------

def test_add_tile_chart_ok_and_shape(client, monkeypatch):
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    resp = _pin_chart(client, dash)
    assert resp.status_code == 200
    tile = resp.json()["tile"]
    assert tile["kind"] == "chart"
    assert tile["title"] == "Monthly revenue by region"
    assert tile["snapshot"]["image_base64"] == "iVBORfakepng"
    assert tile["layout"] == {"x": 0, "y": 0, "w": 6, "h": 5}
    # Second tile auto-places below the first.
    tile2 = _pin_chart(client, dash).json()["tile"]
    assert tile2["layout"]["y"] == 5


def test_add_tile_requires_chat_access(client, monkeypatch):
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    _stub_chat(monkeypatch, exists=False)
    assert _pin_chart(client, dash).status_code == 404
    _stub_chat(monkeypatch, owner="someone@else.com", exists=True)
    assert _pin_chart(client, dash).status_code == 403


def test_add_tile_validation(client, monkeypatch):
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    # bad kind
    assert client.post(f"/api/dashboards/{dash}/tiles",
                       json={"chat_id": "c1", "kind": "gif"}).status_code == 400
    # chart without image
    assert client.post(f"/api/dashboards/{dash}/tiles",
                       json={"chat_id": "c1", "kind": "chart"}).status_code == 400
    # oversized snapshot
    assert _pin_chart(client, dash,
                      image_base64="x" * 5_000_001).status_code == 400
    # table without rows
    assert client.post(f"/api/dashboards/{dash}/tiles",
                       json={"chat_id": "c1", "kind": "table",
                             "table": {"columns": ["a"], "rows": []}}).status_code == 400
    # joined multi-chart code is nulled (tile saved but not refreshable)
    tile = _pin_chart(client, dash,
                      code="plot1 ###NEXT_PLOT### plot2").json()["tile"]
    assert tile["code"] is None


def test_add_tile_table_caps_rows_keeps_total(client, monkeypatch):
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    rows = [{"a": i} for i in range(80)]
    resp = client.post(f"/api/dashboards/{dash}/tiles",
                       json={"chat_id": "c1", "kind": "table",
                             "description": "Top rows",
                             "table": {"columns": ["a"], "rows": rows, "total_rows": 80},
                             "full_table_key": "deadbeefcafef00d"})
    tile = resp.json()["tile"]
    assert len(tile["snapshot"]["table"]["rows"]) == 50
    assert tile["snapshot"]["table"]["total_rows"] == 80
    assert tile["full_table_key"] == "deadbeefcafef00d"


def test_add_tile_inlines_chart_data_key(client, monkeypatch):
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    key = "deadbeefcafef00d"
    chat_mod._FULL_TABLE_CACHE[key] = {"columns": ["x"], "rows": [{"x": 1}]}
    try:
        tile = _pin_chart(client, dash, chart_data_key=key).json()["tile"]
        assert tile["chart_data"] == {"columns": ["x"], "rows": [{"x": 1}]}
    finally:
        chat_mod._FULL_TABLE_CACHE.pop(key, None)


def test_layout_update_persists_and_ignores_unknown(client, monkeypatch):
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    tile = _pin_chart(client, dash).json()["tile"]
    resp = client.post(f"/api/dashboards/{dash}/layout", json={"tiles": [
        {"tile_id": tile["tile_id"], "x": 3, "y": 2, "w": 4, "h": 6},
        {"tile_id": "0" * 16, "x": 9, "y": 9, "w": 1, "h": 1},   # stale — ignored
    ]})
    assert resp.json()["ok"]
    doc = client.get(f"/api/dashboards/{dash}").json()
    assert doc["tiles"][0]["layout"] == {"x": 3, "y": 2, "w": 4, "h": 6}


def test_remove_tile(client, monkeypatch):
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    tile = _pin_chart(client, dash).json()["tile"]
    assert client.post(f"/api/dashboards/{dash}/tiles/{tile['tile_id']}/remove",
                       json={}).json()["ok"]
    assert client.get(f"/api/dashboards/{dash}").json()["tiles"] == []


# ---------------------------------------------------------------------------
# Tile refresh
# ---------------------------------------------------------------------------

def _stub_refresh(monkeypatch, result):
    async def fake(chat_id, code, kind, sid, *, drop_df_keys=None):
        return result
    monkeypatch.setattr(dash_mod, "run_item_refresh", fake)


def test_refresh_chart_updates_snapshot_on_disk(client, monkeypatch):
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    tile = _pin_chart(client, dash).json()["tile"]
    _stub_refresh(monkeypatch, {"ok": True, "kind": "chart",
                                "image_base64": "FRESHPNG", "is_plotly": True})
    resp = client.post(f"/api/dashboards/{dash}/tiles/{tile['tile_id']}/refresh", json={})
    assert resp.json()["ok"] is True
    doc = client.get(f"/api/dashboards/{dash}").json()
    snap = doc["tiles"][0]["snapshot"]
    assert snap["image_base64"] == "FRESHPNG"
    assert snap["is_plotly"] is True
    assert doc["tiles"][0]["frozen"] is False


def test_refresh_table_persists_new_full_key(client, monkeypatch):
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    tile = client.post(f"/api/dashboards/{dash}/tiles",
                       json={"chat_id": "c1", "kind": "table", "code": "RESULT = dfs",
                             "table": {"columns": ["a"], "rows": [{"a": 1}],
                                       "total_rows": 1}}).json()["tile"]
    _stub_refresh(monkeypatch, {"ok": True, "kind": "table",
                                "table": {"columns": ["a"], "rows": [{"a": 9}],
                                          "total_rows": 1},
                                "full_table_key": "cafebabecafebabe"})
    resp = client.post(f"/api/dashboards/{dash}/tiles/{tile['tile_id']}/refresh", json={})
    assert resp.json()["ok"] is True
    doc = client.get(f"/api/dashboards/{dash}").json()
    assert doc["tiles"][0]["snapshot"]["table"]["rows"] == [{"a": 9}]
    assert doc["tiles"][0]["full_table_key"] == "cafebabecafebabe"


def test_refresh_failure_keeps_old_snapshot(client, monkeypatch):
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    tile = _pin_chart(client, dash).json()["tile"]
    _stub_refresh(monkeypatch, {"ok": False, "error": "boom"})
    resp = client.post(f"/api/dashboards/{dash}/tiles/{tile['tile_id']}/refresh", json={})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    doc = client.get(f"/api/dashboards/{dash}").json()
    assert doc["tiles"][0]["snapshot"]["image_base64"] == "iVBORfakepng"


def test_refresh_deleted_chat_freezes_then_success_clears(client, monkeypatch):
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    tile = _pin_chart(client, dash).json()["tile"]
    _stub_chat(monkeypatch, exists=False)
    resp = client.post(f"/api/dashboards/{dash}/tiles/{tile['tile_id']}/refresh", json={})
    assert resp.json() == {"ok": False, "frozen": True, "reason": "source_deleted"}
    doc = client.get(f"/api/dashboards/{dash}").json()
    assert doc["tiles"][0]["frozen"] is True
    assert doc["tiles"][0]["frozen_reason"] == "source_deleted"
    # Chat comes back → successful refresh clears the freeze.
    _stub_chat(monkeypatch, exists=True)
    _stub_refresh(monkeypatch, {"ok": True, "kind": "chart",
                                "image_base64": "BACK", "is_plotly": False})
    assert client.post(f"/api/dashboards/{dash}/tiles/{tile['tile_id']}/refresh",
                       json={}).json()["ok"] is True
    doc = client.get(f"/api/dashboards/{dash}").json()
    assert doc["tiles"][0]["frozen"] is False


def test_refresh_without_code_is_soft_error(client, monkeypatch):
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    tile = _pin_chart(client, dash, code="").json()["tile"]
    resp = client.post(f"/api/dashboards/{dash}/tiles/{tile['tile_id']}/refresh", json={})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


# ---------------------------------------------------------------------------
# Styled tables (conditional formatting) + authoritative table code
# ---------------------------------------------------------------------------

def test_add_tile_preserves_styled_html_dtype_title(client, monkeypatch):
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    styled = "<table><tr><td style='background:#fee'>x</td></tr></table>"
    tile = client.post(f"/api/dashboards/{dash}/tiles",
                       json={"chat_id": "c1", "kind": "table",
                             "table": {"columns": ["a"], "rows": [{"a": 1}],
                                       "total_rows": 1, "styled_html": styled,
                                       "dtype": "series", "title": "KPIs"}}).json()["tile"]
    snap = tile["snapshot"]["table"]
    assert snap["styled_html"] == styled
    assert snap["dtype"] == "series"
    assert snap["title"] == "KPIs"


def test_add_tile_drops_oversized_styled_html_keeps_rows(client, monkeypatch):
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    tile = client.post(f"/api/dashboards/{dash}/tiles",
                       json={"chat_id": "c1", "kind": "table",
                             "table": {"columns": ["a"], "rows": [{"a": 1}],
                                       "total_rows": 1,
                                       "styled_html": "x" * 2_000_001}}).json()["tile"]
    snap = tile["snapshot"]["table"]
    assert "styled_html" not in snap
    assert snap["rows"] == [{"a": 1}]


def test_add_tile_table_code_resolved_from_full_record(client, monkeypatch):
    """The durable record's code overrides the client-sent (chart) code."""
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    key = "aaaabbbbccccdddd"
    chat_mod._FULL_TABLE_CACHE[key] = {"columns": ["a"], "rows": [{"a": 1}],
                                       "code": "RESULT = dfs['f'].head()",
                                       "result_key": "kpi_table"}
    try:
        tile = client.post(f"/api/dashboards/{dash}/tiles",
                           json={"chat_id": "c1", "kind": "table",
                                 "code": "fig = make_subplots()",   # wrong: chart code
                                 "table": {"columns": ["a"], "rows": [{"a": 1}],
                                           "total_rows": 1},
                                 "full_table_key": key}).json()["tile"]
        assert tile["code"] == "RESULT = dfs['f'].head()"
        assert tile["result_key"] == "kpi_table"
    finally:
        chat_mod._FULL_TABLE_CACHE.pop(key, None)


def test_refresh_self_heals_wrong_table_code(client, monkeypatch):
    """A tile pinned before the fix (chart code stored) is healed at refresh
    time from the durable record, and the corrected code is persisted."""
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    key = "1111222233334444"
    # Record unknown at pin time → the wrong client code is stored (old tile).
    tile = client.post(f"/api/dashboards/{dash}/tiles",
                       json={"chat_id": "c1", "kind": "table",
                             "code": "fig = make_subplots()",
                             "table": {"columns": ["a"], "rows": [{"a": 1}],
                                       "total_rows": 1},
                             "full_table_key": key}).json()["tile"]
    assert tile["code"] == "fig = make_subplots()"
    # Record becomes resolvable → refresh must heal + execute the RIGHT code.
    chat_mod._FULL_TABLE_CACHE[key] = {"columns": ["a"], "rows": [{"a": 1}],
                                       "code": "RESULT = dfs['f']"}
    seen = {}

    async def fake(chat_id, code, kind, sid, *, drop_df_keys=None):
        seen["code"] = code
        return {"ok": True, "kind": "table",
                "table": {"columns": ["a"], "rows": [{"a": 2}], "total_rows": 1}}
    monkeypatch.setattr(dash_mod, "run_item_refresh", fake)
    try:
        resp = client.post(f"/api/dashboards/{dash}/tiles/{tile['tile_id']}/refresh", json={})
        assert resp.json()["ok"] is True
        assert seen["code"] == "RESULT = dfs['f']"
        doc = client.get(f"/api/dashboards/{dash}").json()
        assert doc["tiles"][0]["code"] == "RESULT = dfs['f']"
    finally:
        chat_mod._FULL_TABLE_CACHE.pop(key, None)


def test_refresh_table_with_result_key_uses_reexecute(client, monkeypatch):
    """Multi-table tiles (result_key set) re-execute via _reexecute_full_df."""
    import pandas as pd
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    key = "5555666677778888"
    chat_mod._FULL_TABLE_CACHE[key] = {"columns": ["a"], "rows": [{"a": 1}],
                                       "code": "RESULT = {'t1': dfs['f']}",
                                       "result_key": "t1"}
    seen = {}

    async def fake_reexec(chat_id, code, result_key=None, *, drop_df_keys=None):
        seen["args"] = (chat_id, code, result_key)
        return pd.DataFrame({"a": [7, 8]})
    monkeypatch.setattr(dash_mod, "_reexecute_full_df", fake_reexec)
    try:
        tile = client.post(f"/api/dashboards/{dash}/tiles",
                           json={"chat_id": "c1", "kind": "table",
                                 "table": {"columns": ["a"], "rows": [{"a": 1}],
                                           "total_rows": 1},
                                 "full_table_key": key}).json()["tile"]
        assert tile["result_key"] == "t1"
        resp = client.post(f"/api/dashboards/{dash}/tiles/{tile['tile_id']}/refresh",
                           json={}).json()
        assert resp["ok"] is True
        assert seen["args"] == ("c1", "RESULT = {'t1': dfs['f']}", "t1")
        assert resp["table"]["rows"] == [{"a": 7}, {"a": 8}]
        assert resp["table"]["total_rows"] == 2
        assert resp.get("full_table_key")   # fresh durable key persisted
        doc = client.get(f"/api/dashboards/{dash}").json()
        assert doc["tiles"][0]["snapshot"]["table"]["rows"] == [{"a": 7}, {"a": 8}]
    finally:
        chat_mod._FULL_TABLE_CACHE.pop(key, None)


def test_refresh_patch_keeps_styled_html(client, monkeypatch):
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    tile = client.post(f"/api/dashboards/{dash}/tiles",
                       json={"chat_id": "c1", "kind": "table", "code": "RESULT = dfs",
                             "table": {"columns": ["a"], "rows": [{"a": 1}],
                                       "total_rows": 1}}).json()["tile"]
    styled = "<table><tr><td style='color:red'>9</td></tr></table>"
    _stub_refresh(monkeypatch, {"ok": True, "kind": "table",
                                "table": {"columns": ["a"], "rows": [{"a": 9}],
                                          "total_rows": 1, "styled_html": styled}})
    assert client.post(f"/api/dashboards/{dash}/tiles/{tile['tile_id']}/refresh",
                       json={}).json()["ok"] is True
    doc = client.get(f"/api/dashboards/{dash}").json()
    assert doc["tiles"][0]["snapshot"]["table"]["styled_html"] == styled


# ---------------------------------------------------------------------------
# refresh_item extraction regression (contract unchanged)
# ---------------------------------------------------------------------------

def test_refresh_item_endpoint_contract_after_extraction(client, monkeypatch):
    _stub_chat(monkeypatch)
    client.post(f"/_login/{OWNER}")
    # Validation errors stay HTTP 400.
    assert client.post("/api/chat/c1/refresh_item",
                       json={"code": "", "kind": "chart"}).status_code == 400
    assert client.post("/api/chat/c1/refresh_item",
                       json={"code": "a ###NEXT_PLOT### b"}).status_code == 400
    # Execution path delegates to run_item_refresh with the same payload out.
    async def fake(chat_id, code, kind, sid, *, drop_df_keys=None):
        assert chat_id == "c1" and code == "print(1)" and kind == "chart"
        return {"ok": True, "kind": "chart", "image_base64": "IMG", "is_plotly": False}
    monkeypatch.setattr(chat_mod, "run_item_refresh", fake)
    out = client.post("/api/chat/c1/refresh_item",
                      json={"code": "print(1)", "kind": "chart"}).json()
    assert out == {"ok": True, "kind": "chart", "image_base64": "IMG", "is_plotly": False}


# ---------------------------------------------------------------------------
# Sharing
# ---------------------------------------------------------------------------

def _share(client, dash, emails=FRIEND):
    return client.post(f"/api/dashboards/{dash}/share",
                       json={"emails": emails, "message": "hi"})


def test_share_flow_recipient_sees_and_reads(client, monkeypatch, tmp_path):
    _stub_chat(monkeypatch)
    calls = _stub_share_email(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client, "Team KPIs")
    _pin_chart(client, dash)
    resp = _share(client, dash)
    assert resp.json()["ok"] and resp.json()["added"] == [FRIEND]
    assert calls and calls[0]["to"] == [FRIEND]
    # Second share of the same email adds nothing.
    assert _share(client, dash).json()["added"] == []

    client.post(f"/_login/{FRIEND}")
    rows = client.get("/api/dashboards").json()["dashboards"]
    assert len(rows) == 1
    assert rows[0]["shared_by"] == OWNER and rows[0]["name"] == "Team KPIs"
    doc = client.get(f"/api/dashboards/{dash}").json()
    assert doc["is_owner"] is False
    assert doc["tiles"][0]["snapshot"]["image_base64"] == "iVBORfakepng"


def test_share_grants_source_chat_access(client, monkeypatch, tmp_path):
    _stub_chat(monkeypatch)
    _stub_share_email(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    _pin_chart(client, dash, chat_id="chat42")
    _share(client, dash)
    meta = json.loads((tmp_path / "chatdata" / "chat42" / "meta.json").read_text("utf-8"))
    assert FRIEND in meta["sharing"]["shared_with"]


def test_share_recipient_mutations_403(client, monkeypatch):
    _stub_chat(monkeypatch)
    _stub_share_email(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    tile = _pin_chart(client, dash).json()["tile"]
    _share(client, dash)
    client.post(f"/_login/{FRIEND}")
    assert client.post(f"/api/dashboards/{dash}/rename",
                       json={"name": "mine now"}).status_code == 403
    assert client.post(f"/api/dashboards/{dash}/layout",
                       json={"tiles": []}).status_code == 403
    assert _pin_chart(client, dash).status_code == 403
    assert client.post(f"/api/dashboards/{dash}/tiles/{tile['tile_id']}/remove",
                       json={}).status_code == 403
    assert _share(client, dash, "eve@acme.com").status_code == 403


def test_share_recipient_delete_drops_pointer_only(client, monkeypatch):
    _stub_chat(monkeypatch)
    _stub_share_email(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    _share(client, dash)
    client.post(f"/_login/{FRIEND}")
    out = client.post(f"/api/dashboards/{dash}/delete", json={}).json()
    assert out == {"ok": True, "deleted": False}
    assert client.get("/api/dashboards").json()["dashboards"] == []
    # Owner still has it.
    client.post(f"/_login/{OWNER}")
    assert client.get(f"/api/dashboards/{dash}").status_code == 200


def test_share_recipient_refresh_ok_and_access_gate(client, monkeypatch):
    _stub_chat(monkeypatch)
    _stub_share_email(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    tile = _pin_chart(client, dash).json()["tile"]
    _share(client, dash)
    client.post(f"/_login/{FRIEND}")
    # Refresh works for the recipient (chat access granted by the share —
    # simulated here by stubbing the owner check to pass via sharing).
    def owner_check(cid):
        return OWNER
    monkeypatch.setattr(local_store, "get_chat_meta_owner", owner_check)
    _stub_refresh(monkeypatch, {"ok": True, "kind": "chart",
                                "image_base64": "BYFRIEND", "is_plotly": False})
    resp = client.post(f"/api/dashboards/{dash}/tiles/{tile['tile_id']}/refresh", json={})
    # chat42's real meta has FRIEND in shared_with (written by the share), so
    # _require_chat passes and the snapshot lands in the OWNER's doc.
    assert resp.json()["ok"] is True
    client.post(f"/_login/{OWNER}")
    doc = client.get(f"/api/dashboards/{dash}").json()
    assert doc["tiles"][0]["snapshot"]["image_base64"] == "BYFRIEND"


def test_owner_delete_prunes_recipient_pointer_on_next_list(client, monkeypatch):
    _stub_chat(monkeypatch)
    _stub_share_email(monkeypatch)
    client.post(f"/_login/{OWNER}")
    dash = _mk_dash(client)
    _share(client, dash)
    client.post(f"/api/dashboards/{dash}/delete", json={})
    client.post(f"/_login/{FRIEND}")
    assert client.get("/api/dashboards").json()["dashboards"] == []
    assert client.get(f"/api/dashboards/{dash}").status_code == 404


# ---------------------------------------------------------------------------
# Stored-shape backward compatibility
# ---------------------------------------------------------------------------

def test_old_shape_doc_loads_with_defaults(client, monkeypatch, tmp_path):
    """A hand-written doc missing frozen/layout/tile_count/sharing must load
    (Data-safety rule: previous-release shapes still work after upgrade)."""
    client.post(f"/_login/{OWNER}")
    dash_id = "ab" * 8
    ddir = tmp_path / "users" / OWNER / "dashboards"
    ddir.mkdir(parents=True)
    (ddir / f"{dash_id}.json").write_text(json.dumps({
        "dash_id": dash_id, "name": "Old", "owner": OWNER,
        "tiles": [{"tile_id": "cd" * 8, "chat_id": "c1", "kind": "chart",
                   "snapshot": {"image_base64": "OLD"}}],
    }), encoding="utf-8")
    (ddir / "index.json").write_text(json.dumps(
        [{"dash_id": dash_id, "name": "Old"}]), encoding="utf-8")
    rows = client.get("/api/dashboards").json()["dashboards"]
    assert rows and rows[0]["dash_id"] == dash_id
    doc = client.get(f"/api/dashboards/{dash_id}").json()
    assert doc["tiles"][0]["snapshot"]["image_base64"] == "OLD"
    # Layout update on the old tile works (fills the missing layout).
    assert client.post(f"/api/dashboards/{dash_id}/layout", json={"tiles": [
        {"tile_id": "cd" * 8, "x": 1, "y": 1, "w": 3, "h": 3}]}).json()["ok"]
