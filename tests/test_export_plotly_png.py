"""The client-local Plotly PNG export, with the REAL kaleido renderer.

The export renders only a chart the server produced, named by a reference
(`{conv_id, ai_index, image_index}` for a stored chart, `{chart_ref}` for one
the server just sent); posted markup is never rendered. These tests drive
kaleido's Chromium for real, so they also prove that a chart naming a URL or
a local file makes the renderer fetch nothing.

NOTE: kaleido 0.2.1 hangs on the Windows dev box; this file runs in CI
(Linux). tests/test_export_plotly_png_reference.py covers the route contract
without starting Chromium.
"""
import http.server
import json
import socket
import threading

import plotly.graph_objects as go
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import local_store
import plot_utils
import routes.chat as chat_mod
from routes import report
from settings import settings

_PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
OWNER = "alice@acme.com"
CHAT = "chat123"

# A real interactive Plotly HTML string, rendered exactly like the chat path.
_FIXTURE_HTML = plot_utils._plotly_to_html(go.Figure(data=[go.Bar(x=["a", "b"], y=[1, 2])]))


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(local_store, "chat_exists", lambda cid: True)
    monkeypatch.setattr(local_store, "get_chat_meta_owner", lambda cid: OWNER)
    store = local_store.ChatDataStore(CHAT)
    conv = store.new_conversation("t")
    store.append_history(conv, {"role": "ai", "content": "x", "image_base64": _FIXTURE_HTML})
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret-" * 4)
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    return TestClient(app), conv


def test_a_stored_chart_exports_as_a_png(world):
    client, conv = world
    client.post(f"/_login/{OWNER}")
    resp = client.post(f"/api/chat/{CHAT}/export_plotly_png",
                       json={"conv_id": conv, "ai_index": 0, "filename": "brand_vs_stock"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/png"
    assert 'filename="brand_vs_stock.png"' in resp.headers["content-disposition"]
    assert resp.content[:8] == _PNG_MAGIC


def test_a_chart_ref_exports_as_a_png(world):
    client, _ = world
    client.post(f"/_login/{OWNER}")
    ref = chat_mod._chart_ref_for(OWNER, CHAT, _FIXTURE_HTML)
    resp = client.post(f"/api/chat/{CHAT}/export_plotly_png", json={"chart_ref": ref})
    assert resp.status_code == 200, resp.text
    assert resp.content[:8] == _PNG_MAGIC


def test_posted_markup_is_refused(world):
    """Replaces the old garbage-HTML / unparseable-newPlot 400 tests: the
    route no longer parses posted markup at all."""
    client, _ = world
    client.post(f"/_login/{OWNER}")
    for html in (_FIXTURE_HTML, "<html><body>no chart</body></html>", "Plotly.newPlot( <<<"):
        resp = client.post(f"/api/chat/{CHAT}/export_plotly_png", json={"html": html})
        assert resp.status_code == 400, resp.text
        assert "error" in resp.json()


def test_requires_auth_401(world):
    client, conv = world
    resp = client.post(f"/api/chat/{CHAT}/export_plotly_png",
                       json={"conv_id": conv, "ai_index": 0})
    assert resp.status_code == 401


def test_non_owner_403(world, monkeypatch):
    client, conv = world
    monkeypatch.setattr(local_store, "get_chat_meta_owner", lambda cid: "bob@acme.com")
    client.post(f"/_login/{OWNER}")
    resp = client.post(f"/api/chat/{CHAT}/export_plotly_png",
                       json={"conv_id": conv, "ai_index": 0})
    assert resp.status_code == 403


class _Listener:
    """A loopback HTTP server that records every request it receives."""

    def __init__(self):
        hits = self.hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_the_real_renderer_fetches_nothing_a_chart_names(tmp_path):
    """The audit probe, end to end: a stored chart whose layout names a
    loopback URL (and a local file) renders to a PNG, and the listener sees
    no request."""
    listener = _Listener()
    try:
        secret = tmp_path / "red.png"
        secret.write_bytes(_PNG_MAGIC + b"x")
        layout = {"images": [
            {"source": f"http://127.0.0.1:{listener.port}/ssrf.png",
             "xref": "paper", "yref": "paper", "x": 0, "y": 1, "sizex": 1, "sizey": 1},
            {"source": secret.as_uri(),
             "xref": "paper", "yref": "paper", "x": 0, "y": 1, "sizex": 1, "sizey": 1}]}
        html = ('<div id="c"></div><script>Plotly.newPlot("c", '
                + json.dumps([{"type": "bar", "x": [1], "y": [1]}]) + ", "
                + json.dumps(layout) + ", {})</script>")
        png = report._plotly_html_to_png(html, "probe")
        assert png and png[:8] == _PNG_MAGIC
        assert listener.hits == [], listener.hits
    finally:
        listener.close()


def _probe_html(data, layout):
    return ('<div id="c"></div><script>Plotly.newPlot("c", '
            + json.dumps(data) + ", " + json.dumps(layout) + ", {})</script>")


def test_a_css_url_in_chart_text_fetches_nothing():
    """plotly.js copies a pseudo-HTML `style` attribute onto the SVG text, and
    Chromium would resolve a CSS url() there."""
    listener = _Listener()
    try:
        url = f"http://127.0.0.1:{listener.port}/css.png"
        text = f'<span style="background-image:url({url})">x</span>'
        quoted = f'<span title="t"style="background-image:url({url}q)">y</span>'
        layout = {"title": {"text": text},
                  "annotations": [{"text": text, "x": 0, "y": 0, "showarrow": False},
                                  {"text": quoted, "x": 1, "y": 1, "showarrow": False}]}
        png = report._plotly_html_to_png(_probe_html([{"type": "bar", "x": [1], "y": [1]}],
                                                     layout), "probe")
        assert png and png[:8] == _PNG_MAGIC
        assert listener.hits == [], listener.hits
    finally:
        listener.close()


def test_a_map_style_object_and_tile_list_fetch_nothing():
    listener = _Listener()
    try:
        base = f"http://127.0.0.1:{listener.port}"
        layout = {"mapbox": {"style": {"version": 8,
                                       "sources": {"t": {"type": "raster",
                                                         "tiles": [base + "/t/{z}/{x}/{y}.png"],
                                                         "tileSize": 256}},
                                       "sprite": base + "/sprite",
                                       "glyphs": base + "/g/{fontstack}/{range}.pbf",
                                       "layers": [{"id": "t", "type": "raster", "source": "t"}]},
                             "layers": [{"sourcetype": "raster",
                                         "source": [base + "/l/{z}/{x}/{y}.png"],
                                         "below": "traces"}]}}
        data = [{"type": "scattermapbox", "lat": [41.7], "lon": [44.8]}]
        report._plotly_html_to_png(_probe_html(data, layout), "probe")
        assert listener.hits == [], listener.hits
    finally:
        listener.close()
