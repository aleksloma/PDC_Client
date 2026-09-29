"""Authorization on the dashboard pin and share routes.

* Pinning (`POST /api/dashboards/{id}/tiles`) stores code that the tile
  refresh later executes, so it accepts only code the source chat already
  holds — an AI history row (whole or one `###NEXT_PLOT###` segment), the turn
  in flight, or, for a table tile, the durable full-table record the pin's
  `full_table_key` resolves (whose code is authoritative). Anything else is
  `400 CODE_NOT_STORED`. A pin without code stays allowed; that tile simply
  cannot refresh.
* The tile refresh itself is unchanged: the tile's code is already stored.
* Sharing a dashboard grants the recipients access only to the source chats
  the dashboard OWNER owns. A tile pinned from a chat the owner merely
  received freezes for the recipients with the existing caller-specific
  `access_revoked` shape instead of widening that chat's share list.

Real chats on disk under a tmp DATA_ROOT; execution stubbed at the
`routes.dashboards.run_item_refresh` seam; the share mail relay stubbed.
"""
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import local_store
from conftest import seed_history
from settings import settings

OWNER = "alice@acme.com"      # owns chat X
BOB = "bob@acme.com"          # recipient of X, owns chat Z
CAROL = "carol@acme.com"      # receives Bob's dashboard
CHAT_X = "c_dashroutes0x01"
CHAT_Z = "c_dashroutes0z01"
CHART_CODE = "fig = px.bar(dfs['d.csv'], x='a')"
TABLE_CODE = "RESULT = dfs['d.csv'].head()"
PNG = "iVBORfakepng"


def _make_chat(chat_id, owner, shared_with=()):
    store = local_store.ChatDataStore(chat_id)
    meta = store.read_meta()
    meta["owner"] = owner
    meta["files"] = []
    if shared_with:
        meta["sharing"] = {"shared_with": sorted(shared_with)}
    store.write_meta(meta)
    return store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    for email in (OWNER, BOB, CAROL):
        local_store.AuthStore().ensure_user(email)
    _make_chat(CHAT_X, OWNER, shared_with=[BOB])
    _make_chat(CHAT_Z, BOB)

    import routes.chat as chat_mod
    import routes.dashboards as dash_mod
    monkeypatch.setattr(dash_mod.brain_client, "send_share_email",
                        lambda **kw: {"smtp_configured": True,
                                      "sent": kw.get("to") or [], "failed": []})

    async def fake_refresh(chat_id, code, kind, sid, *, drop_df_keys=None):
        return {"ok": True, "kind": "chart", "image_base64": "FRESH",
                "is_plotly": False}

    monkeypatch.setattr(dash_mod, "run_item_refresh", fake_refresh)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(dash_mod.router)
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    return tc


def _as(client, email):
    assert client.post(f"/_login/{email}").status_code == 200


def _dash(client, name="Board"):
    r = client.post("/api/dashboards", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["dash_id"]


def _pin_chart(client, dash_id, chat_id=CHAT_X, code=CHART_CODE):
    body = {"chat_id": chat_id, "kind": "chart", "description": "d",
            "image_base64": PNG, "is_plotly": False}
    if code is not None:
        body["code"] = code
    return client.post(f"/api/dashboards/{dash_id}/tiles", json=body)


def _pin_table(client, dash_id, chat_id=CHAT_X, code=TABLE_CODE, full_table_key=None):
    body = {"chat_id": chat_id, "kind": "table", "description": "d",
            "table": {"columns": ["a"], "rows": [{"a": 1}], "total_rows": 1}}
    if code is not None:
        body["code"] = code
    if full_table_key:
        body["full_table_key"] = full_table_key
    return client.post(f"/api/dashboards/{dash_id}/tiles", json=body)


def _tiles(dash_id, owner=OWNER):
    doc = local_store.DashboardStore().get_dashboard(owner, dash_id) or {}
    return doc.get("tiles") or []


# ===========================================================================
# pinning binds the tile's code to the chat's stored code
# ===========================================================================
def test_pin_chart_with_unstored_code_is_refused(client):
    dash = _dash(client)
    r = _pin_chart(client, dash, code="import os\nos.system('id')")
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "CODE_NOT_STORED"
    assert _tiles(dash) == []


def test_pin_chart_with_stored_code_is_accepted(client):
    seed_history(CHAT_X, CHART_CODE)
    dash = _dash(client)
    r = _pin_chart(client, dash)
    assert r.status_code == 200, r.text
    assert r.json()["tile"]["code"] == CHART_CODE


def test_pin_chart_with_one_segment_of_a_joined_row_is_accepted(client):
    seed_history(CHAT_X, "fig1 = 1\n\n###NEXT_PLOT###\n\n" + CHART_CODE)
    dash = _dash(client)
    r = _pin_chart(client, dash)
    assert r.status_code == 200, r.text


def test_pin_chart_with_an_in_flight_code_is_accepted(client):
    """A chart streamed on a `partial` event can be pinned before the turn is
    persisted — the generating worker has registered its code."""
    import routes.chat as chat_mod
    assert hasattr(chat_mod, "_inflight_add"), "in-flight registry is missing"
    dash = _dash(client)
    chat_mod._inflight_add(CHAT_X, CHART_CODE)
    try:
        r = _pin_chart(client, dash)
        assert r.status_code == 200, r.text
    finally:
        chat_mod._inflight_discard(CHAT_X, [CHART_CODE])


def test_pin_table_without_record_and_with_unstored_code_is_refused(client):
    dash = _dash(client)
    r = _pin_table(client, dash, code="RESULT = __import__('os').environ")
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "CODE_NOT_STORED"
    assert _tiles(dash) == []


def test_pin_table_whose_key_resolves_a_durable_record_uses_the_record_code(client):
    """The record's code is authoritative for a table tile, whatever the
    client posted (the posted code may belong to the answer's chart)."""
    key = "abcdef0123456789"
    full = local_store.ChatDataStore(CHAT_X).conversations_dir / "full"
    full.mkdir(parents=True, exist_ok=True)
    (full / f"{key}.json").write_text(json.dumps(
        {"columns": ["a"], "rows": [{"a": 1}], "code": TABLE_CODE}), encoding="utf-8")
    dash = _dash(client)
    r = _pin_table(client, dash, code="fig = make_subplots()", full_table_key=key)
    assert r.status_code == 200, r.text
    assert r.json()["tile"]["code"] == TABLE_CODE


def test_pin_table_with_stored_code_and_no_record_is_accepted(client):
    seed_history(CHAT_X, TABLE_CODE)
    dash = _dash(client)
    r = _pin_table(client, dash)
    assert r.status_code == 200, r.text
    assert r.json()["tile"]["code"] == TABLE_CODE


@pytest.mark.parametrize("kind", ["chart", "table"])
def test_pin_without_code_stays_allowed(client, kind):
    dash = _dash(client)
    r = (_pin_chart if kind == "chart" else _pin_table)(client, dash, code=None)
    assert r.status_code == 200, r.text
    assert r.json()["tile"]["code"] is None


def test_recipient_pins_stored_code_of_a_shared_chat(client):
    """By design: a share recipient pins the chat's answers onto their OWN
    dashboard."""
    seed_history(CHAT_X, CHART_CODE)
    _as(client, BOB)
    dash = _dash(client)
    r = _pin_chart(client, dash)
    assert r.status_code == 200, r.text


def test_tile_refresh_runs_the_tiles_stored_code_unchanged(client):
    """The tile's code is stored on the tile; the refresh does not look it up
    in the chat history (a tile pinned before the binding keeps refreshing)."""
    ds = local_store.DashboardStore()
    row = ds.create_dashboard(OWNER, "Legacy")
    tile = ds.add_tile(OWNER, row["dash_id"], {
        "chat_id": CHAT_X, "kind": "chart", "code": "fig = legacy_code()",
        "snapshot": {"image_base64": PNG, "is_plotly": False}})
    r = client.post(f"/api/dashboards/{row['dash_id']}/tiles/{tile['tile_id']}/refresh",
                    json={})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["image_base64"] == "FRESH"


# ===========================================================================
# a dashboard share grants only the chats the dashboard owner owns
# ===========================================================================
def _bob_board_with_tiles_from_x_and_z(client):
    seed_history(CHAT_X, CHART_CODE)
    seed_history(CHAT_Z, CHART_CODE)
    _as(client, BOB)
    dash = _dash(client, "Bob's board")
    tile_x = _pin_chart(client, dash, chat_id=CHAT_X).json()["tile"]
    tile_z = _pin_chart(client, dash, chat_id=CHAT_Z).json()["tile"]
    r = client.post(f"/api/dashboards/{dash}/share", json={"emails": [CAROL]})
    assert r.status_code == 200, r.text
    return dash, tile_x, tile_z


def _shared_with(chat_id):
    meta = local_store.ChatDataStore(chat_id).read_meta()
    return (meta.get("sharing") or {}).get("shared_with") or []


def test_dashboard_share_does_not_extend_a_chat_the_owner_only_received(client):
    _bob_board_with_tiles_from_x_and_z(client)
    assert CAROL not in _shared_with(CHAT_X)
    assert _shared_with(CHAT_X) == [BOB]


def test_dashboard_share_still_grants_the_owners_own_chat(client):
    _bob_board_with_tiles_from_x_and_z(client)
    assert CAROL in _shared_with(CHAT_Z)


def test_recipient_tile_from_an_unowned_source_chat_freezes_access_revoked(client):
    dash, tile_x, tile_z = _bob_board_with_tiles_from_x_and_z(client)
    _as(client, CAROL)
    r = client.post(f"/api/dashboards/{dash}/tiles/{tile_x['tile_id']}/refresh", json={})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "frozen": True, "reason": "access_revoked"}
    # Caller-specific: the shared document is not frozen for everyone.
    stored = next(t for t in _tiles(dash, owner=BOB) if t["tile_id"] == tile_x["tile_id"])
    assert stored.get("frozen") is not True
    # The tile from Bob's own chat stays live for Carol.
    r = client.post(f"/api/dashboards/{dash}/tiles/{tile_z['tile_id']}/refresh", json={})
    assert r.json()["ok"] is True, r.text


def test_unshare_revokes_the_chat_grant_it_made_and_the_dashboard(client):
    dash, _, tile_z = _bob_board_with_tiles_from_x_and_z(client)
    assert CAROL in _shared_with(CHAT_Z)
    r = client.post(f"/api/dashboards/{dash}/unshare", json={"email": CAROL})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "shared_with": []}
    # The chat grant that the share made alongside ends with it.
    assert CAROL not in _shared_with(CHAT_Z)
    assert _shared_with(CHAT_X) == [BOB]
    # Carol no longer lists or resolves the dashboard, nor refreshes its tiles.
    _as(client, CAROL)
    assert client.get("/api/dashboards").json()["dashboards"] == []
    assert client.get(f"/api/dashboards/{dash}").status_code == 404
    r = client.post(f"/api/dashboards/{dash}/tiles/{tile_z['tile_id']}/refresh", json={})
    assert r.status_code == 404
