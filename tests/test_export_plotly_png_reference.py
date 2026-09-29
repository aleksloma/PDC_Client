"""The chart PNG export renders only charts the SERVER produced.

kaleido drives a Chromium inside the web container (secrets, LAN egress), so
`POST /api/chat/{id}/export_plotly_png` never takes chart markup from the
request: the body is a reference — `{chart_ref}` for a chart the server just
sent (registered by `_chart_ref_for`), or `{conv_id, ai_index, image_index}`
for a chart stored in the conversation. A posted `html` field is ignored. The
dashboard tile export renders a snapshot the tile refresh wrote, else the
source chat's stored chart for the tile's code — never a browser-posted pin.

These tests never start Chromium: `go.Figure.to_image` is replaced by a stub
that records the figure it was asked to render.
"""
import plotly.graph_objects as go
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import local_store
import plot_utils
import routes.chat as chat_mod
import routes.dashboards as dash_mod
from settings import settings

PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"stub"
OWNER = "alice@acme.com"
CHAT = "chat123"
FIG_A = plot_utils._plotly_to_html(go.Figure(data=[go.Bar(x=["a", "b"], y=[1, 2])]))
FIG_B = plot_utils._plotly_to_html(go.Figure(data=[go.Bar(x=["c", "d"], y=[3, 4])]))


@pytest.fixture
def rendered(monkeypatch):
    seen = []

    def fake_to_image(self, *a, **k):
        seen.append(self.to_dict())
        return PNG

    monkeypatch.setattr(go.Figure, "to_image", fake_to_image)
    return seen


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(local_store, "chat_exists", lambda cid: True)
    monkeypatch.setattr(local_store, "get_chat_meta_owner", lambda cid: OWNER)
    with chat_mod._CHART_REF_LOCK:
        chat_mod._CHART_REFS.clear()
        chat_mod._CHART_REF_TOTAL["bytes"] = 0
    store = local_store.ChatDataStore(CHAT)
    conv = store.new_conversation("t")
    store.append_history(conv, {"role": "human", "content": "q"})
    store.append_history(conv, {"role": "ai", "content": "one", "image_base64": FIG_A,
                                "code": "fig = 1"})
    store.append_history(conv, {"role": "human", "content": "q2"})
    store.append_history(conv, {"role": "ai", "content": "two", "images": [
        {"image_base64": FIG_A, "answer": "a", "code": "fig = 2"},
        {"image_base64": FIG_B, "answer": "b", "code": "fig = 3"}]})
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t" * 40)
    app.include_router(chat_mod.router)
    app.include_router(dash_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    client = TestClient(app)
    client.post(f"/_login/{OWNER}")
    return client, conv


def _export(client, body):
    return client.post(f"/api/chat/{CHAT}/export_plotly_png", json=body)


def _bars(fig_dict):
    return [list(t.get("x") or []) for t in fig_dict.get("data", [])]


def test_a_stored_single_chart_is_rendered(world, rendered):
    client, conv = world
    r = _export(client, {"conv_id": conv, "ai_index": 1, "filename": "one"})
    assert r.status_code == 200, r.text
    assert r.content == PNG
    assert _bars(rendered[-1]) == [["a", "b"]]
    assert 'filename="one.png"' in r.headers["content-disposition"]


def test_each_chart_of_a_multi_chart_row_is_addressable(world, rendered):
    client, conv = world
    assert _export(client, {"conv_id": conv, "ai_index": 3, "image_index": 1}).status_code == 200
    assert _bars(rendered[-1]) == [["c", "d"]]


@pytest.mark.parametrize("body", [
    {"conv_id": "cv_" + "0" * 16, "ai_index": 1},     # unknown conversation
    {"ai_index": 0},                                  # a human row (conv added below)
    {"ai_index": 99},
    {"ai_index": 3, "image_index": 5},
    {"ai_index": -1},
    {"ai_index": "1"},
])
def test_an_unresolvable_reference_is_404_and_renders_nothing(world, rendered, body):
    client, conv = world
    body = dict(body)
    body.setdefault("conv_id", conv)
    r = _export(client, body)
    assert r.status_code in (403, 404), (body, r.status_code, r.text)
    assert rendered == []


def test_posted_markup_is_never_rendered(world, rendered):
    client, _ = world
    r = _export(client, {"html": FIG_B, "filename": "x"})
    assert r.status_code == 400, r.text
    assert rendered == []


def test_markup_next_to_a_reference_is_ignored(world, rendered):
    client, conv = world
    r = _export(client, {"conv_id": conv, "ai_index": 1, "html": FIG_B})
    assert r.status_code == 200
    assert _bars(rendered[-1]) == [["a", "b"]]


def test_a_chart_ref_renders_the_registered_chart(world, rendered):
    client, _ = world
    ref = chat_mod._chart_ref_for(OWNER, CHAT, FIG_B)
    assert ref
    r = _export(client, {"chart_ref": ref})
    assert r.status_code == 200, r.text
    assert _bars(rendered[-1]) == [["c", "d"]]


def test_a_chart_ref_is_bound_to_its_user_and_chat(world, rendered):
    client, _ = world
    other_user = chat_mod._chart_ref_for("mallory@acme.com", CHAT, FIG_B)
    other_chat = chat_mod._chart_ref_for(OWNER, "another", FIG_B)
    for ref in (other_user, other_chat, "0" * 32, "../x", 5):
        assert _export(client, {"chart_ref": ref}).status_code == 404, ref
    assert rendered == []


def test_only_plotly_markup_gets_a_reference():
    assert chat_mod._chart_ref_for(OWNER, CHAT, "iVBORw0KGgo=") is None
    assert chat_mod._chart_ref_for(OWNER, CHAT, None) is None


def test_the_filename_is_sanitised(world, rendered):
    client, conv = world
    r = _export(client, {"conv_id": conv, "ai_index": 1, "filename": "../../etc/passwd"})
    after = r.headers["content-disposition"].split("filename=")[1]
    assert "/" not in after and ".." not in after


def test_a_recipient_without_the_conversation_is_refused(world, rendered, monkeypatch):
    client, conv = world
    monkeypatch.setattr(chat_mod, "_may_use_conversation", lambda e, c, v: False)
    r = _export(client, {"conv_id": conv, "ai_index": 1})
    assert r.status_code == 403
    assert rendered == []


def test_one_failed_registration_leaves_only_that_chart_without_a_reference(world, monkeypatch):
    """A registration failure (here: the second chart) is logged and answers
    None; the other charts keep their references and the response is whole."""
    calls = {"n": 0}
    import routes.report as report_mod
    original = report_mod._is_plotly_html

    def flaky(html):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("registry down")
        return original(html)

    monkeypatch.setattr(report_mod, "_is_plotly_html", flaky)
    out = chat_mod._with_chart_refs(
        {"answer": "x", "images": [{"image_base64": FIG_A}, {"image_base64": FIG_B},
                                   {"image_base64": FIG_A}]}, OWNER, CHAT)
    refs = [img.get("chart_ref") for img in out["images"]]
    assert refs[0] and refs[2], refs
    assert refs[1] is None, refs
    assert out["answer"] == "x"


def test_with_chart_refs_never_mutates_the_persisted_rows():
    row_images = [{"image_base64": FIG_A}]
    out = chat_mod._with_chart_refs({"images": row_images}, OWNER, CHAT)
    assert "chart_ref" not in row_images[0]
    assert out["images"][0]["chart_ref"]


# ---------------------------------------------------------------- dashboard tiles
def _tile_world(world, snapshot, code):
    client, conv = world
    store = dash_mod._dash_store
    dash_id = store.create_dashboard(OWNER, "D")["dash_id"]
    doc = store.get_dashboard(OWNER, dash_id)
    doc["tiles"] = [{"tile_id": "t1", "kind": "chart", "chat_id": CHAT, "code": code,
                     "snapshot": snapshot}]
    store._write_doc(OWNER, doc)
    return client, dash_id


def test_a_pinned_snapshot_is_never_rendered_the_stored_chart_is(world, rendered):
    """The pin's snapshot came from the browser; the export renders the
    source chat's stored chart for the tile's code instead."""
    client, dash_id = _tile_world(world, {"image_base64": FIG_B, "is_plotly": True}, "fig = 1")
    r = client.post(f"/api/dashboards/{dash_id}/tiles/t1/export_png", json={})
    assert r.status_code == 200, r.text
    assert _bars(rendered[-1]) == [["a", "b"]]


def test_a_refreshed_snapshot_is_rendered(world, rendered):
    client, dash_id = _tile_world(
        world, {"image_base64": FIG_B, "is_plotly": True, "server_rendered": True}, "fig = 1")
    r = client.post(f"/api/dashboards/{dash_id}/tiles/t1/export_png", json={})
    assert r.status_code == 200, r.text
    assert _bars(rendered[-1]) == [["c", "d"]]


def test_a_tile_whose_code_is_not_stored_is_404(world, rendered):
    client, dash_id = _tile_world(world, {"image_base64": FIG_B, "is_plotly": True}, "fig = 999")
    r = client.post(f"/api/dashboards/{dash_id}/tiles/t1/export_png", json={})
    assert r.status_code == 404
    assert rendered == []
