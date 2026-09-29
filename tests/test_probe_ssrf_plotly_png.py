"""Re-assessment probe: server-side request forgery through the PNG export.

The original probes (V/ssrf_route.py, V/ssrf_route_file.py) posted chart
markup naming a URL or a local file to POST /api/chat/{id}/export_plotly_png;
kaleido's Chromium in the web container then fetched it. The probe scripts
are not in the repository; this test re-creates them with a loopback
listener that records every request.

- Posted markup is refused before any renderer starts (runs everywhere).
- A STORED chart that names the listener renders without fetching it (needs
  kaleido's Chromium, which hangs on Windows: Linux / CI only).
"""
import http.server
import sys
import threading

import plotly.graph_objects as go
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import local_store
import plot_utils
import routes.chat as chat_mod
from settings import settings

OWNER = "alice@acme.com"
CHAT = "chat_probe"


class _Listener:
    def __init__(self):
        hits = self.hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def listener():
    lst = _Listener()
    yield lst
    lst.close()


def _probe_figure(port: int) -> go.Figure:
    fig = go.Figure(data=[go.Bar(x=["a", "b"], y=[1, 2])])
    fig.add_layout_image(source=f"http://127.0.0.1:{port}/layout-image",
                         xref="paper", yref="paper", x=0, y=1, sizex=1, sizey=1)
    fig.update_layout(title={"text": f"<span style=\"background:url(http://127.0.0.1:{port}/css)\">t</span>"})
    return fig


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(local_store, "chat_exists", lambda cid: True)
    monkeypatch.setattr(local_store, "get_chat_meta_owner", lambda cid: OWNER)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="probe-secret-" * 4)
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    return tc


@pytest.mark.parametrize("target", ["http://127.0.0.1:{port}/route", "file:///etc/passwd"])
def test_posted_markup_is_refused_and_nothing_is_fetched(world, listener, target):
    html = plot_utils._plotly_to_html(_probe_figure(listener.port)).replace(
        f"http://127.0.0.1:{listener.port}/layout-image", target.format(port=listener.port))
    r = world.post(f"/api/chat/{CHAT}/export_plotly_png", json={"html": html})
    assert r.status_code == 400, r.text[:200]
    assert listener.hits == []


@pytest.mark.skipif(sys.platform == "win32", reason="kaleido's Chromium hangs on Windows")
def test_a_stored_chart_naming_the_listener_fetches_nothing(world, listener):
    store = local_store.ChatDataStore(CHAT)
    conv = store.new_conversation("probe")
    html = plot_utils._plotly_to_html(_probe_figure(listener.port))
    store.append_history(conv, {"role": "ai", "content": "x", "image_base64": html})
    r = world.post(f"/api/chat/{CHAT}/export_plotly_png", json={"conv_id": conv, "ai_index": 0})
    assert r.status_code == 200, r.text[:200]
    assert r.content[:8] == bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
    assert listener.hits == []
