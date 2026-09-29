"""Response headers and the absent API documentation.

- /docs, /redoc and /openapi.json do not exist (the route map and a request
  console are not published to anyone who reaches the port).
- Every response carries `X-Content-Type-Options: nosniff` (a response that
  sets its own keeps exactly one).
- `Strict-Transport-Security` is sent only on a request that arrived over
  HTTPS; a plain-HTTP install is never pinned to TLS.
"""
import pytest
from starlette.testclient import TestClient

import app as app_mod
import local_store
from settings import settings

HSTS = "max-age=31536000; includeSubDomains"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    local_store._DATAFRAME_CACHE.invalidate()
    return tmp_path


def _https():
    return TestClient(app_mod.app, base_url="https://testserver")


def _http():
    return TestClient(app_mod.app, base_url="http://testserver")


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_the_api_documentation_is_not_served(env, path):
    assert _https().get(path).status_code == 404


def test_the_app_is_built_without_documentation_urls():
    assert app_mod.app.docs_url is None
    assert app_mod.app.redoc_url is None
    assert app_mod.app.openapi_url is None


@pytest.mark.parametrize("path", ["/", "/health", "/version", "/static/http.js",
                                  "/api/dashboards", "/no-such-route"])
def test_every_response_is_nosniff(env, path):
    r = _https().get(path, follow_redirects=False)
    assert r.headers.get_list("x-content-type-options") == ["nosniff"], (path, r.status_code)


def test_a_state_changing_refusal_is_nosniff_too(env):
    r = _https().post("/api/dashboards", content=b"x", headers={"content-type": "text/plain"})
    assert r.status_code == 415
    assert r.headers.get("x-content-type-options") == "nosniff"


@pytest.mark.parametrize("path", ["/", "/health", "/api/dashboards"])
def test_hsts_on_https_requests(env, path):
    r = _https().get(path, follow_redirects=False)
    assert r.headers.get_list("strict-transport-security") == [HSTS], path


@pytest.mark.parametrize("path", ["/", "/health", "/api/dashboards"])
def test_no_hsts_on_plain_http_requests(env, path):
    r = _http().get(path, follow_redirects=False)
    assert "strict-transport-security" not in r.headers, path


def test_the_headers_layer_sits_inside_the_backend_guard():
    names = [m.cls.__name__ for m in app_mod.app.user_middleware]
    assert names[0] == "BackendNetworkGuard"
    assert names.index("SecurityHeaders") == names.index("ContentSecurityPolicy") - 1
