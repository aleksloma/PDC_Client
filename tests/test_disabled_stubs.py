"""Unit tests for the B2C billing/publish stubs added for the enterprise build.

These routes exist only so the verbatim B2C dashboard JS degrades cleanly (no
404s, no console errors). They make no brain call and touch no store.
"""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import routes.auth as auth_mod


@pytest.fixture
def client():
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(auth_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    return TestClient(app)


def test_paddle_config_is_clean_200_no_auth_required(client):
    """Page-load billing bootstrap: 200 with a benign 'billing disabled' config
    so Paddle.Initialize is a no-op (no client_token) and there is no 404."""
    resp = client.get("/paddle/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["client_token"] is None  # JS guard `if (cfg.client_token)` → skip


def test_subscription_post_disabled(client):
    # 401 without auth …
    assert client.post("/auth/subscription", json={"plan": "Pro"}).status_code == 401
    # … 400 (clean JSON) with auth.
    client.post("/_login/alice@acme.com")
    r = client.post("/auth/subscription", json={"plan": "Pro"})
    assert r.status_code == 400
    assert r.json()["ok"] is False and "error" in r.json()


def test_subscription_get_still_constant(client):
    # The GET constant must be untouched by the new POST stub.
    r = client.get("/auth/subscription")
    assert r.status_code == 200
    assert r.json()["plan"] == "Enterprise"


@pytest.mark.parametrize("path", [
    "/api/paddle/subscription/update-payment",
    "/api/paddle/subscription/reactivate",
    "/api/paddle/subscription/preview",
    "/api/paddle/subscription/cancel",
    "/api/paddle/subscription/update",
])
def test_paddle_subscription_stubs(client, path):
    assert client.post(path, json={}).status_code == 401
    client.post("/_login/alice@acme.com")
    r = client.post(path, json={})
    assert r.status_code == 400
    assert r.json()["ok"] is False and "error" in r.json()


@pytest.mark.parametrize("verb", ["publish", "unpublish"])
def test_conversation_publish_stubs(client, verb):
    assert client.post(f"/auth/conversations/cv_1/{verb}").status_code == 401
    client.post("/_login/alice@acme.com")
    r = client.post(f"/auth/conversations/cv_1/{verb}")
    assert r.status_code == 400
    assert "error" in r.json()
