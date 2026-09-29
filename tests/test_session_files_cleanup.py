"""Session workspaces (`DATA_ROOT/sessions/<sid>/`) do not outlive their use.

- The owner's address is recorded in the session meta.
- After /generate_chatdata has copied the uploads into the chat, the session
  folder is deleted and the session gets a new sid.
- /new_session deletes the PREVIOUS session's folder.
- Logout deletes the session's folder.
- `UserStore.destroy` refuses a malformed sid and another owner's folder,
  and never raises.
"""
import json

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import local_store
from settings import settings
from tests.conftest import JSON_HEADERS

OWNER = "owner@acme.com"
SID = "s_00000000000000a1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    local_store.AuthStore().ensure_user(OWNER)
    import routes.auth as auth_mod
    import routes.upload as upload_mod
    monkeypatch.setattr(upload_mod.brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(upload_mod.brain_client, "chat_metadata",
                        lambda **k: {"name": "Chat", "welcome_message": "hi",
                                     "suggested_questions": ["q1"]})
    monkeypatch.setattr(upload_mod.brain_client, "file_description",
                        lambda **k: {"description": ""})

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t" * 40)
    app.include_router(upload_mod.router)
    app.include_router(auth_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = SID
        return {"ok": True}

    @app.get("/_sid")
    async def _sid(request: Request):
        return {"sid": request.session.get("sid")}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    return tc


def _session_dir(tmp, sid=SID):
    return tmp / "sessions" / sid


def _upload(client):
    r = client.post("/upload", files={"files": ("sales.csv", b"region,amount\nE,1\nW,2\n", "text/csv")})
    assert r.status_code == 200, r.text[:300]


def test_the_owner_is_recorded_in_the_session_meta(client, tmp_path):
    _upload(client)
    meta = json.loads((_session_dir(tmp_path) / "meta.json").read_text(encoding="utf-8"))
    assert meta["owner"] == OWNER


def test_a_session_folder_without_an_owner_gets_one(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store.UserStore(SID)                             # the older shape
    local_store.UserStore(SID, owner="Owner@Acme.com")
    meta = json.loads((_session_dir(tmp_path) / "meta.json").read_text(encoding="utf-8"))
    assert meta["owner"] == OWNER


def test_generate_chatdata_deletes_the_session_uploads_and_rotates_the_sid(client, tmp_path):
    _upload(client)
    assert (_session_dir(tmp_path) / "files" / "sales.csv").exists()
    r = client.post("/generate_chatdata", json={})
    assert r.status_code == 200, r.text[:300]
    chat_id = r.json()["chat_id"]
    assert not _session_dir(tmp_path).exists()
    assert (tmp_path / "chatdata" / chat_id / "files" / "sales.csv").exists()
    new_sid = client.get("/_sid").json()["sid"]
    assert new_sid != SID and local_store.valid_sid(new_sid)


def test_new_session_deletes_the_previous_folder(client, tmp_path):
    _upload(client)
    r = client.post("/new_session", headers=JSON_HEADERS)
    assert r.status_code == 200
    assert not _session_dir(tmp_path).exists()
    new_sid = client.get("/_sid").json()["sid"]
    assert _session_dir(tmp_path, new_sid).is_dir()


def test_logout_deletes_the_session_folder(client, tmp_path):
    _upload(client)
    r = client.post("/auth/logout", headers=JSON_HEADERS, follow_redirects=False)
    assert r.status_code == 302
    assert not _session_dir(tmp_path).exists()


def test_destroy_refuses_another_owners_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store.UserStore(SID, owner="victim@acme.com")
    assert local_store.UserStore.destroy(SID, "mallory@acme.com") is False
    assert _session_dir(tmp_path).exists()
    assert local_store.UserStore.destroy(SID, "victim@acme.com") is True
    assert not _session_dir(tmp_path).exists()


@pytest.mark.parametrize("sid", ["../users", "s_../../x", "", None, "s_XYZ"])
def test_destroy_refuses_a_malformed_sid(tmp_path, monkeypatch, sid):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    (tmp_path / "users").mkdir()
    assert local_store.UserStore.destroy(sid, OWNER) is False
    assert (tmp_path / "users").exists()


def test_destroy_of_an_absent_folder_is_true(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    assert local_store.UserStore.destroy(SID, OWNER) is True
