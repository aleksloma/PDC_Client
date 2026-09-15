"""Security remediation Task 1 — upload filename sanitization (R-02), the
Secure session-cookie flag (R-07), log rotation (R-10) and the probe_columns
sibling of the filename bug (1e.1).

R-02: `/upload` handed the RAW multipart filename to `UserStore.save_upload`,
which did `self.files_dir / filename`. `Path(base) / "/abs/evil"` resolves to
the absolute path and `../` walks out of the session folder, so any
authenticated user could write anywhere the process can. The fix is ONE
sanitizer (`local_store.sanitize_upload_filename`) used by `save_upload`
(with a resolve()-containment assert) and by `routes.chat.probe_columns`.

Everything here is offline: brain calls stubbed, DATA_ROOT under tmp_path,
"outside" paths are tmp_path-rooted — never a real /tmp, never client_data.
"""
import io
import logging
import logging.handlers
import re
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import local_store
from settings import settings

_ROOT = Path(__file__).resolve().parent.parent
OWNER = "user@x.com"
SID = "s_sanitize"
CHAT = "chatsanitize1"
CSV = b"a,b\n1,2\n"
GEORGIAN = "გაყიდვები 2026.xlsx"


def _sanitize(name):
    # Looked up lazily so a missing helper fails the TEST, not collection.
    return local_store.sanitize_upload_filename(name)


# ---------------------------------------------------------------------------
# 1a. The sanitizer itself
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("sales.csv", "sales.csv"),               # normal name unchanged
    ("/tmp/evil.txt", "evil.txt"),            # absolute path -> basename
    ("../../etc/passwd", "passwd"),           # traversal -> basename
    ("..\\..\\x.csv", "x.csv"),               # Windows separators too
    ("sa\x00les\x1f.csv", "sales.csv"),       # NUL / control chars stripped
])
def test_sanitizer_reduces_paths_to_a_safe_basename(raw, expected):
    assert _sanitize(raw) == expected


def test_sanitizer_empty_name_gets_uuid_fallback_without_extension():
    assert re.fullmatch(r"upload_[0-9a-f]{8}", _sanitize("")), _sanitize("")


def test_sanitizer_extension_only_name_keeps_supported_extension():
    out = _sanitize(".csv")
    assert re.fullmatch(r"upload_[0-9a-f]{8}\.csv", out), out


def test_sanitizer_caps_at_200_bytes_keeping_the_extension():
    out = _sanitize("s" * 300 + ".xlsx")
    assert len(out.encode("utf-8")) <= 200, len(out.encode("utf-8"))
    assert out.endswith(".xlsx")
    assert out.startswith("sss")


def test_sanitizer_preserves_unicode_names_verbatim():
    """Georgian names must survive (B2C's ASCII regex made them '_____.xlsx')."""
    assert _sanitize(GEORGIAN) == GEORGIAN


# ---------------------------------------------------------------------------
# 1a. UserStore.save_upload is contained inside files_dir
# ---------------------------------------------------------------------------
@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(settings, "DATA_ROOT", str(root))
    local_store._DATAFRAME_CACHE.invalidate()
    yield root
    local_store._DATAFRAME_CACHE.invalidate()


def test_save_upload_contains_an_absolute_path(tmp_path, data_root):
    """`files_dir / "<abs>/evil.txt"` used to resolve to the absolute path."""
    outside = tmp_path / "outside"
    outside.mkdir()
    evil = outside / "evil.txt"
    store = local_store.UserStore(SID)
    out = store.save_upload(str(evil), b"x")
    assert Path(out).resolve().parent == store.files_dir.resolve()
    assert (store.files_dir / "evil.txt").read_bytes() == b"x"
    assert not evil.exists(), "the upload escaped files_dir"
    assert store.read_meta()["files"][0]["file_name"] == "evil.txt"


def test_save_upload_contains_a_relative_traversal(tmp_path, data_root):
    store = local_store.UserStore(SID)
    # Where the unsanitized join lands (files/../../etc) — pre-created so the
    # vulnerable write succeeds and the containment assertion is what fails.
    (data_root / "sessions" / "etc").mkdir(parents=True, exist_ok=True)
    out = store.save_upload("../../etc/passwd", b"x")
    assert Path(out).resolve().parent == store.files_dir.resolve()
    assert (store.files_dir / "passwd").read_bytes() == b"x"
    assert store.read_meta()["files"][0]["file_name"] == "passwd"
    # the only 'passwd' anywhere under the temp root is the contained one
    assert [p.resolve() for p in tmp_path.rglob("passwd")] == [(store.files_dir / "passwd").resolve()]


# ---------------------------------------------------------------------------
# 1a. POST /upload — route uses the STORED name
# ---------------------------------------------------------------------------
def _make_upload_app(monkeypatch):
    import routes.upload as upload_mod
    monkeypatch.setattr(upload_mod.brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(upload_mod.brain_client, "file_description", lambda **k: {"description": ""})
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(upload_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = SID
        return {"ok": True}

    return app


@pytest.fixture
def upload_client(data_root, monkeypatch):
    monkeypatch.setattr(settings, "GCS_UPLOAD_BUCKET", "")
    tc = TestClient(_make_upload_app(monkeypatch))
    tc.post(f"/_login/{OWNER}")
    return tc


def _assert_upload_contained(tmp_path, r):
    assert r.status_code == 200, r.text
    js = r.json()
    assert js["ok"] is True
    assert js["saved"] == ["evil.csv"]
    assert js["dataframes"] == ["evil.csv"]
    assert js["files"] == [{"file": "evil.csv", "status": "ok"}]
    expected = (Path(settings.DATA_ROOT) / "sessions" / SID / "files" / "evil.csv").resolve()
    assert expected.read_bytes() == CSV
    # exactly one evil.csv exists anywhere under the temp root: the contained one
    assert [p.resolve() for p in tmp_path.rglob("evil.csv")] == [expected]


def test_upload_traversal_filename_is_contained(tmp_path, upload_client):
    r = upload_client.post("/upload", files=[("files", ("../../evil.csv", io.BytesIO(CSV), "text/csv"))])
    _assert_upload_contained(tmp_path, r)


def test_upload_absolute_filename_is_contained(tmp_path, upload_client):
    """Forward-slash absolute paths reach the route verbatim (python-multipart
    only strips the IE-style `C:\\...` form), and `files_dir / "<abs>"` IS
    the absolute path."""
    outside = tmp_path / "outside"
    outside.mkdir()
    evil = outside / "evil.csv"
    r = upload_client.post("/upload", files=[("files", (evil.as_posix(), io.BytesIO(CSV), "text/csv"))])
    _assert_upload_contained(tmp_path, r)
    assert not evil.exists(), "the upload escaped the session files_dir"


# ---------------------------------------------------------------------------
# 1e.1. probe_columns resolves the stored basename, never the parent dir
# ---------------------------------------------------------------------------
@pytest.fixture
def probe_client(data_root, monkeypatch):
    store = local_store.ChatDataStore(CHAT)
    (store.files_dir / "x.csv").write_bytes(CSV)
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["files"] = [{"file_name": "x.csv", "schema": {}}]
    store.write_meta(meta)

    import routes.chat as chat_mod
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(chat_mod.router)  # router already carries /api/chat

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    return tc


@pytest.mark.parametrize("name", ["/tmp/x.csv", "..\\x.csv"])
def test_probe_columns_resolves_path_prefixes_to_the_stored_basename(probe_client, name):
    r = probe_client.post(f"/api/chat/{CHAT}/probe_columns",
                          files=[("file", (name, io.BytesIO(CSV), "text/csv"))])
    assert r.status_code == 200, r.text
    js = r.json()
    assert js["ok"] is True and js["match"] is True, js


def test_probe_columns_dotdot_is_not_an_existing_file(probe_client, monkeypatch):
    """`Path("..").name == ".."`, so `files_dir / ".."` existed (the parent
    dir). The sanitized name can never name a directory: {ok:false}, no
    exception, and the body is never parsed against the parent folder."""
    import routes.chat as chat_mod
    calls = []
    original = chat_mod._probe_structure

    def spy(data, filename):
        calls.append(filename)
        return original(data, filename)

    monkeypatch.setattr(chat_mod, "_probe_structure", spy)
    r = probe_client.post(f"/api/chat/{CHAT}/probe_columns",
                          files=[("file", ("..", io.BytesIO(CSV), "text/csv"))])
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": False}
    assert calls == [], f"parent directory treated as the existing file: {calls}"


# ---------------------------------------------------------------------------
# 1b. Secure cookie flag
# ---------------------------------------------------------------------------
def _cookie_client(https_only: bool) -> TestClient:
    # NOT entered as a context manager: the lifespan starts the db_scheduler
    # thread (see tests/test_version_endpoint.py).
    import app as app_mod
    inner = FastAPI()

    @inner.post("/_login")
    async def _login(request: Request):
        request.session["email"] = OWNER
        return {"ok": True}

    @inner.get("/_whoami")
    async def _whoami(request: Request):
        return {"email": request.session.get("email")}

    inner.add_middleware(app_mod.RememberMeSessionMiddleware, secret_key="test-secret",
                         same_site="lax", max_age=60, https_only=https_only)
    return TestClient(inner)


def test_session_cookie_carries_secure_when_https_only():
    r = _cookie_client(https_only=True).post("/_login")
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "secure" in cookie, cookie
    assert "httponly" in cookie


def test_plain_http_login_still_works_when_https_only_false():
    tc = _cookie_client(https_only=False)
    r = tc.post("/_login")
    assert "secure" not in r.headers["set-cookie"].lower()
    # default base_url is http://testserver — the session must round-trip
    assert tc.get("/_whoami").json()["email"] == OWNER


def test_session_https_only_defaults_true():
    # Bound to a local first: pytest's assertion rewrite would otherwise print
    # the whole Settings repr (SECRET_KEY included) on failure.
    value = getattr(settings, "SESSION_HTTPS_ONLY", None)
    assert value is True


@pytest.mark.parametrize("env,expected", [
    (None, True), ("true", True), ("1", True),
    ("false", False), ("0", False), ("no", False), ("off", False),
])
def test_session_https_only_parses_like_client_llm_debug(monkeypatch, env, expected):
    from settings import Settings
    if env is None:
        monkeypatch.delenv("SESSION_HTTPS_ONLY", raising=False)
    else:
        monkeypatch.setenv("SESSION_HTTPS_ONLY", env)
    value = getattr(Settings(), "SESSION_HTTPS_ONLY", None)   # local: no Settings repr on failure
    assert value is expected


def test_app_wires_https_only_from_settings():
    src = (_ROOT / "app.py").read_text(encoding="utf-8")
    assert "https_only=settings.SESSION_HTTPS_ONLY" in src


# ---------------------------------------------------------------------------
# 1c. Log rotation
# ---------------------------------------------------------------------------
def test_log_rotation_defaults():
    max_bytes = getattr(settings, "LOG_MAX_BYTES", None)      # locals: no Settings repr on failure
    backup_count = getattr(settings, "LOG_BACKUP_COUNT", None)
    assert max_bytes == 50 * 1024 * 1024
    assert backup_count == 5


def test_get_logger_installs_rotating_file_handler_and_stdout():
    import logger_utils
    logger = logging.getLogger("datachat")
    saved = list(logger.handlers)
    logger.handlers = []
    try:
        lg = logger_utils.get_logger()
        assert lg is logger
        rotating = [h for h in lg.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(rotating) == 1, [type(h).__name__ for h in lg.handlers]
        fh = rotating[0]
        assert fh.maxBytes == settings.LOG_MAX_BYTES
        assert fh.backupCount == settings.LOG_BACKUP_COUNT
        assert fh.encoding == "utf-8"
        assert Path(fh.baseFilename).resolve() == (_ROOT / "logs" / "datachat.log").resolve()
        stdout_handlers = [h for h in lg.handlers
                           if type(h) is logging.StreamHandler and h.stream is sys.stdout]
        assert stdout_handlers, "stdout StreamHandler is gone"
    finally:
        for h in list(logger.handlers):
            if h not in saved:
                h.close()
        logger.handlers = saved
