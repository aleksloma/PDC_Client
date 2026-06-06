"""Unit tests for the client-local Plotly PNG export endpoint.

100% client-local (no brain call). We assert: valid PNG bytes for a real plotly
HTML fixture, scale clamping, the Content-Disposition filename, 400 on garbage
HTML, and 401/403 without auth.
"""
import plotly.graph_objects as go
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import local_store
import plot_utils
import routes.chat as chat_mod

_PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])

# A real interactive Plotly HTML string, rendered exactly like the chat path.
_FIXTURE_HTML = plot_utils._plotly_to_html(go.Figure(data=[go.Bar(x=["a", "b"], y=[1, 2])]))


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    return TestClient(app)


def _as_owner(monkeypatch, email="alice@acme.com"):
    monkeypatch.setattr(local_store, "chat_exists", lambda cid: True)
    monkeypatch.setattr(local_store, "get_chat_meta_owner", lambda cid: email)


def test_export_returns_valid_png_and_filename(client, monkeypatch):
    _as_owner(monkeypatch)
    client.post("/_login/alice@acme.com")
    resp = client.post(
        "/api/chat/chat123/export_plotly_png",
        json={"html": _FIXTURE_HTML, "filename": "brand_vs_stock", "scale": 3},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert 'filename="brand_vs_stock.png"' in resp.headers["content-disposition"]
    assert resp.content[:8] == _PNG_MAGIC


def test_scale_is_clamped(client, monkeypatch):
    _as_owner(monkeypatch)
    client.post("/_login/alice@acme.com")
    seen = {}

    def fake_render(html, scale):
        seen["scale"] = scale
        return _PNG_MAGIC + b"rest"

    monkeypatch.setattr(chat_mod, "_plotly_png_from_html", fake_render)

    # Above the cap → clamped to 5.0
    r1 = client.post("/api/chat/c/export_plotly_png",
                     json={"html": _FIXTURE_HTML, "filename": "x", "scale": 99})
    assert r1.status_code == 200 and seen["scale"] == 5.0
    # Below the floor → clamped to 1.0
    client.post("/api/chat/c/export_plotly_png",
                json={"html": _FIXTURE_HTML, "filename": "x", "scale": 0.1})
    assert seen["scale"] == 1.0
    # Non-numeric → default 2.0
    client.post("/api/chat/c/export_plotly_png",
                json={"html": _FIXTURE_HTML, "filename": "x", "scale": "abc"})
    assert seen["scale"] == 2.0


def test_filename_sanitized(client, monkeypatch):
    _as_owner(monkeypatch)
    client.post("/_login/alice@acme.com")
    monkeypatch.setattr(chat_mod, "_plotly_png_from_html", lambda html, scale: _PNG_MAGIC)
    resp = client.post("/api/chat/c/export_plotly_png",
                       json={"html": _FIXTURE_HTML, "filename": "../../etc/passwd"})
    assert resp.status_code == 200
    after = resp.headers["content-disposition"].split("filename=")[1]
    assert "/" not in after and ".." not in after


def test_garbage_html_returns_400(client, monkeypatch):
    _as_owner(monkeypatch)
    client.post("/_login/alice@acme.com")
    resp = client.post("/api/chat/c/export_plotly_png",
                       json={"html": "<html><body>no chart here</body></html>", "filename": "x"})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_unparseable_newplot_returns_400(client, monkeypatch):
    """HTML that *contains* the marker but has no decodable JSON args → 400 (not 500)."""
    _as_owner(monkeypatch)
    client.post("/_login/alice@acme.com")
    resp = client.post("/api/chat/c/export_plotly_png",
                       json={"html": "Plotly.newPlot( <<<garbage", "filename": "x"})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_requires_auth_401(client, monkeypatch):
    _as_owner(monkeypatch)
    resp = client.post("/api/chat/c/export_plotly_png",
                       json={"html": _FIXTURE_HTML, "filename": "x"})
    assert resp.status_code == 401


def test_non_owner_403(client, monkeypatch):
    monkeypatch.setattr(local_store, "chat_exists", lambda cid: True)
    monkeypatch.setattr(local_store, "get_chat_meta_owner", lambda cid: "bob@acme.com")

    class _Boom:
        def __init__(self, cid):
            raise RuntimeError("no sharing record")

    monkeypatch.setattr(local_store, "ChatDataStore", _Boom)
    client.post("/_login/alice@acme.com")
    resp = client.post("/api/chat/c/export_plotly_png",
                       json={"html": _FIXTURE_HTML, "filename": "x"})
    assert resp.status_code == 403
