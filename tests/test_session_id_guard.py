"""A session id never becomes a path unless it is the canonical shape, and no
recursive delete leaves the directory it is documented to delete under.

The sid comes back from the signed session cookie and names
DATA_ROOT/sessions/<sid>; `/upload` and `/new_session` recursively delete that
folder. With a known key a user could sign `sid: "../users/victim"` (deleting
another account) or `sid: ".."` (deleting DATA_ROOT). Three layers stop that:
the routes refuse a malformed sid with 401 and clear the session,
`UserStore` refuses to build a path from one, and every `rmtree` in
local_store asserts its target lies strictly inside its own root.
"""
import secrets

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import local_store
from local_store import InvalidSessionId, UserStore, _assert_inside, valid_sid
from settings import settings

GOOD = "s_" + "0123456789abcdef"
EMAIL = "owner@acme.com"


@pytest.fixture(autouse=True)
def _root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    yield tmp_path


# ---------------------------------------------------------------- grammar
@pytest.mark.parametrize("value", [GOOD, "s_" + secrets.token_hex(8)])
def test_canonical_sids_are_valid(value):
    assert valid_sid(value)


@pytest.mark.parametrize("value", [
    "..", ".", "", "../users/victim@x.ge", "s_" + "0" * 15, "s_" + "0" * 17,
    "s_" + "ABCDEF0123456789", "S_0123456789abcdef", "s_0123456789abcdeg",
    "s_0123456789abcdef\n", "s_dbflow", None, 123, b"s_0123456789abcdef",
])
def test_everything_else_is_invalid(value):
    assert not valid_sid(value)


# ---------------------------------------------------------------- the store
@pytest.mark.parametrize("sid", ["..", "../users/victim@x.ge", "../../etc", "s_x"])
def test_userstore_refuses_a_traversal_sid_and_creates_nothing(_root, sid):
    victim = _root / "users" / "victim@x.ge"
    victim.mkdir(parents=True)
    (victim / "auth.json").write_text("{}", encoding="utf-8")
    with pytest.raises(InvalidSessionId):
        UserStore(sid)
    assert (victim / "auth.json").exists()
    assert not (_root / "sessions").exists() or not any((_root / "sessions").iterdir())


def test_reset_all_refuses_a_root_outside_sessions(_root):
    store = UserStore(GOOD)
    victim = _root / "users" / "victim@x.ge"
    victim.mkdir(parents=True)
    (victim / "profile.json").write_text("{}", encoding="utf-8")
    store.root = victim                      # as if the path had been subverted
    with pytest.raises(InvalidSessionId):
        store.reset_all()
    assert (victim / "profile.json").exists()


def test_reset_all_still_resets_a_valid_session(_root):
    store = UserStore(GOOD)
    (store.files_dir / "a.csv").write_text("x", encoding="utf-8")
    store.reset_all()
    assert store.files_dir.is_dir() and not any(store.files_dir.iterdir())


# ---------------------------------------------------------------- the assertion
def test_assert_inside_accepts_a_child(_root):
    (_root / "sessions" / GOOD).mkdir(parents=True)
    assert _assert_inside(_root / "sessions" / GOOD, _root / "sessions")


@pytest.mark.parametrize("rel", ["sessions", "sessions/..", "users", "sessions/../users/x", "."])
def test_assert_inside_refuses_the_root_itself_and_everything_outside(_root, rel):
    (_root / "sessions").mkdir(exist_ok=True)
    with pytest.raises(ValueError):
        _assert_inside(_root / rel, _root / "sessions")


def test_assert_inside_follows_a_symlink_out(_root):
    (_root / "sessions").mkdir()
    outside = _root / "outside"
    outside.mkdir()
    link = _root / "sessions" / GOOD
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available to this user")
    with pytest.raises(ValueError):
        _assert_inside(link, _root / "sessions")


def test_remove_user_only_deletes_under_users(_root, monkeypatch):
    store = local_store.AuthStore()
    store.create_account(EMAIL)
    seen = []
    real = local_store._assert_inside

    def spy(path, root):
        seen.append(root.resolve())
        return real(path, root)

    monkeypatch.setattr(local_store, "_assert_inside", spy)
    assert store.remove_user(EMAIL)
    assert seen == [(_root / "users").resolve()]


def test_delete_chats_owned_by_only_deletes_under_chatdata(_root, monkeypatch):
    chat = local_store.ChatDataStore("chat1")
    chat.write_meta({"owner": EMAIL, "files": []})
    seen = []
    real = local_store._assert_inside

    def spy(path, root):
        seen.append(root.resolve())
        return real(path, root)

    monkeypatch.setattr(local_store, "_assert_inside", spy)
    assert local_store.delete_chats_owned_by(EMAIL) == 1
    assert seen == [(_root / "chatdata").resolve()]


# ---------------------------------------------------------------- the routes
def _app(sid):
    from routes.upload import router as upload_router
    from routes.schema import router as schema_router
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t" * 40)

    @app.get("/_login")
    def _login(request: Request):
        request.session["email"] = EMAIL
        request.session["sid"] = sid
        return {"ok": True}

    @app.get("/_whoami")
    def _whoami(request: Request):
        return dict(request.session)

    app.include_router(upload_router)
    app.include_router(schema_router)
    return app


@pytest.mark.parametrize("sid", ["..", "../users/victim@x.ge", "s_nothex"])
def test_upload_with_a_forged_sid_is_401_and_clears_the_session(_root, sid):
    victim = _root / "users" / "victim@x.ge"
    victim.mkdir(parents=True)
    (victim / "auth.json").write_text("{}", encoding="utf-8")
    c = TestClient(_app(sid))
    c.get("/_login")
    r = c.post("/upload", files={"files": ("a.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 401, r.text
    assert c.get("/_whoami").json() == {}
    assert (victim / "auth.json").exists()


@pytest.mark.parametrize("path,method", [("/schema_details", "get"), ("/schema", "get"),
                                         ("/schema_autofill_full", "post")])
def test_other_session_routes_refuse_a_forged_sid(_root, path, method):
    c = TestClient(_app(".."))
    c.get("/_login")
    r = getattr(c, method)(path)
    assert r.status_code == 401, (path, r.status_code, r.text[:200])
    assert c.get("/_whoami").json() == {}


def test_a_canonical_sid_still_uploads(_root):
    c = TestClient(_app(GOOD))
    c.get("/_login")
    r = c.post("/upload", files={"files": ("a.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 200, r.text
    assert (_root / "sessions" / GOOD / "files" / "a.csv").exists()
