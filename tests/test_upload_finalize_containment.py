"""C1 — /upload/finalize must contain its download inside the session
files_dir, like `UserStore.save_upload` already does.

`routes/upload.py` builds the destination as `store.files_dir / filename`
and hands it straight to `gcs_upload.download_to`. Every other write path
gained a `resolve().is_relative_to(files_dir)` check with the R-02 fix;
this one did not. Today the ONLY thing standing between an attacker-supplied
object key and an arbitrary write is the upstream `_safe_upload_filename`
helper — one edit away (a new extension, an NFC tweak, a refactor that lets
a separator through) from an arbitrary file write by any authenticated user.

The check is defense in depth, so the tests make the upstream helper return
an unsafe name (exactly the regression the containment check must survive)
and assert the download NEVER happens and nothing lands outside files_dir.

Fully offline: `gcs_upload`'s four network functions are replaced by a fake
(no google import), DATA_ROOT is tmp_path, and the escape targets are
tmp_path-rooted — never a real /tmp, never client_data.
"""
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import local_store
from settings import settings

OWNER = "user@x.com"
SID = "s_finalizecontain"
CSV = b"a,b\n1,2\n3,4\n"


class FakeGCS:
    """The four gcs_upload network functions, in-process (see
    tests/test_upload_no_gcs_branch.py — same stub)."""

    def __init__(self, size=len(CSV)):
        self.size = size
        self.signed = []
        self.deleted = []
        self.downloaded = []

    def sign_put_url(self, object_path, content_type, minutes=15):
        self.signed.append((object_path, content_type, minutes))
        return f"https://storage.googleapis.com/test-bucket/{object_path}?X-Goog-Signature=fake"

    def blob_size(self, object_path):
        return self.size

    def download_to(self, object_path, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(CSV)
        self.downloaded.append((object_path, str(dest)))
        return len(CSV)

    def delete_blob(self, object_path):
        self.deleted.append(object_path)


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    yield tmp_path
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def logs(monkeypatch):
    """Capture every log_with_sid call routes.upload makes."""
    import routes.upload as upload_mod
    out = []
    monkeypatch.setattr(upload_mod, "log_with_sid",
                        lambda sid, level, message, **ctx: out.append((sid, level, message, ctx)))
    return out


@pytest.fixture
def fake(data_root, logs, monkeypatch):
    import routes.upload as upload_mod
    monkeypatch.setattr(upload_mod.brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(upload_mod.brain_client, "file_description", lambda **k: {"description": ""})
    monkeypatch.setattr(settings, "GCS_UPLOAD_BUCKET", "test-bucket")
    stub = FakeGCS()
    for fn in ("sign_put_url", "blob_size", "download_to", "delete_blob"):
        monkeypatch.setattr(upload_mod.gcs_upload, fn, getattr(stub, fn))
    return stub


@pytest.fixture
def client(fake, monkeypatch):
    import routes.upload as upload_mod
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(upload_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = SID
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    return tc


def _break_the_sanitizer(monkeypatch, unsafe_name: str):
    """Simulate the upstream helper letting a path component through."""
    import routes.upload as upload_mod
    monkeypatch.setattr(upload_mod, "_safe_upload_filename", lambda name: unsafe_name)


def _files_dir() -> Path:
    return Path(settings.DATA_ROOT) / "sessions" / SID / "files"


def test_finalize_refuses_a_traversal_destination(data_root, client, fake, logs, monkeypatch):
    """`files_dir / "../../../evil.csv"` lands at <DATA_ROOT>/evil.csv."""
    _break_the_sanitizer(monkeypatch, "../../../evil.csv")
    r = client.post("/upload/finalize", json={"gcs_path": f"tmp/{SID}/abc/data.csv"})
    assert fake.downloaded == [], f"the download escaped files_dir: {fake.downloaded}"
    assert not (data_root / "evil.csv").exists(), "file written outside files_dir"
    assert list(_files_dir().iterdir()) == []
    assert r.status_code == 400, r.text
    assert isinstance(r.json().get("error"), str) and r.json()["error"]
    warnings = [m for (_s, lvl, m, _c) in logs if lvl == "warning"]
    assert any("FINALIZE" in m.upper() or "UNSAFE" in m.upper() for m in warnings), warnings


def test_finalize_refuses_an_absolute_destination(tmp_path, data_root, client, fake, monkeypatch):
    """`files_dir / "<abs>"` IS the absolute path — pathlib drops the left side."""
    outside = tmp_path / "outside"
    outside.mkdir()
    evil = outside / "evil.csv"
    _break_the_sanitizer(monkeypatch, str(evil))
    r = client.post("/upload/finalize", json={"gcs_path": f"tmp/{SID}/abc/data.csv"})
    assert fake.downloaded == [], f"the download escaped files_dir: {fake.downloaded}"
    assert not evil.exists(), "file written outside files_dir"
    assert r.status_code == 400, r.text


def test_finalize_still_imports_an_ordinary_file(data_root, client, fake):
    """The containment check must not change the happy path (same response
    shape as /upload, object deleted afterwards)."""
    path = f"tmp/{SID}/abc/data.csv"
    r = client.post("/upload/finalize", json={"gcs_path": path})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "saved": ["data.csv"], "dataframes": ["data.csv"],
                        "files": [{"file": "data.csv", "status": "ok"}]}
    assert (_files_dir() / "data.csv").read_bytes() == CSV
    assert fake.deleted == [path]
