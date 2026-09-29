"""The multipart upload routes refuse a body above MAX_UPLOAD_BYTES with 413.

`app.UploadByteCap` answers a declared Content-Length above the cap before
the app or the multipart parser runs, and counts a body without one as it is
read; `/upload` also sums the files' own bytes. Other routes are untouched.
"""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import app as app_mod
import local_store
from settings import settings

SID = "s_" + "ab" * 8
EMAIL = "owner@acme.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 4096)
    from routes.upload import router as upload_router
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t" * 40)
    app.add_middleware(app_mod.UploadByteCap)
    app.add_exception_handler(app_mod.UploadTooLarge, app_mod.upload_too_large_handler)

    @app.get("/_login")
    def _login(request: Request):
        request.session["email"] = EMAIL
        request.session["sid"] = SID
        return {"ok": True}

    @app.post("/api/other")
    async def other(request: Request):
        return {"n": len(await request.body())}

    app.include_router(upload_router)
    c = TestClient(app)
    c.get("/_login")
    return c


def test_a_declared_body_over_the_cap_is_413_before_the_route(client, monkeypatch):
    reached = []
    monkeypatch.setattr(local_store.UserStore, "reset_all", lambda self: reached.append(1))
    big = b"a,b\n" + b"1,2\n" * 2000
    r = client.post("/upload", files={"files": ("a.csv", big, "text/csv")})
    assert r.status_code == 413, r.text
    assert r.json() == {"error": "Upload too large", "max_bytes": 4096}
    assert reached == [], "the route ran for an oversized body"


def test_a_streamed_body_over_the_cap_is_413(client):
    def chunks():
        yield b"--x\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.csv\"\r\n\r\n"
        for _ in range(100):
            yield b"1,2\n" * 64
        yield b"\r\n--x--\r\n"

    r = client.post("/upload", content=chunks(),
                    headers={"content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413, r.text
    assert r.json() == {"error": "Upload too large", "max_bytes": 4096}


def test_the_add_data_column_probe_is_capped_too(client):
    big = b"a,b\n" + b"1,2\n" * 2000
    r = client.post("/api/chat/abc/probe_columns",
                    files={"file": ("a.csv", big, "text/csv")})
    assert r.status_code == 413, r.text


def test_the_real_app_answers_the_same_body_for_a_streamed_overflow():
    handlers = app_mod.app.exception_handlers
    assert handlers.get(app_mod.UploadTooLarge) is app_mod.upload_too_large_handler


def test_a_small_upload_still_reaches_the_route(client):
    r = client.post("/upload", files={"files": ("a.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 200, r.text


def test_other_routes_are_not_counted(client):
    r = client.post("/api/other", content=b"x" * 10000)
    assert r.status_code == 200 and r.json() == {"n": 10000}


def test_the_real_app_registers_the_cap_outside_the_session():
    names = [m.cls.__name__ for m in app_mod.app.user_middleware]
    assert "UploadByteCap" in names
    assert names.index("UploadByteCap") < names.index("RememberMeSessionMiddleware")
    assert names[0] == "BackendNetworkGuard"


def test_the_default_cap_is_100_mib(monkeypatch):
    monkeypatch.delenv("MAX_UPLOAD_BYTES", raising=False)
    from settings import Settings
    assert Settings().MAX_UPLOAD_BYTES == 100 * 1024 * 1024
