"""Direct-to-GCS upload is OPTIONAL and OFF by default (GCS_UPLOAD_BUCKET).

History: the B2C frontend routed files > 25 MB through /upload/init → signed
PUT → /upload/finalize; on-prem those were 400 stubs, so big uploads failed
(48ae23b removed the branch). The Cloud Run demo then hit Google Frontend's
32 MiB request-body cap on multipart /upload, so the B2C flow came back as a
GATED feature. These tests pin the gate from every side:

- bucket UNSET (every customer install): the three endpoints keep answering
  the historical 400 whatever the body/session, the JS branch is compiled in
  but switched off by the server flag, multipart /upload is unchanged;
- bucket SET (fake storage client — never the network): init signs a
  session-namespaced key and validates name/extension/size/count, finalize
  refuses foreign paths, bounds the real object size, pulls the object into
  the session store, runs the /upload pipeline and deletes the object;
- /upload_from_url stays 400 either way; routes.upload never imports google
  at module import (the customer image has no GCS environment).
"""
import io
import subprocess
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
SID = "s_directgcs"
CSV = b"a,b\n1,2\n3,4\n"
STUB_BODIES = {
    "/upload/init": "Direct-to-GCS upload is not available in the on-prem build. "
                    "The standard /upload path is used for all sizes.",
    "/upload/finalize": "Disabled in the on-prem build.",
    "/upload_from_url": "Google Sheets / Drive import is not available in the on-prem build.",
}


# ---------------------------------------------------------------------------
# Structural: the branch exists but is gated on the server flag
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("js_name", ["dashboard.js", "config.js"])
def test_frontend_branch_is_gated_on_server_flag(js_name):
    src = (_ROOT / "static" / js_name).read_text(encoding="utf-8")
    assert "const directUploadEnabled = !!(window.__DIRECT_UPLOAD__);" in src
    assert "const hasLargeFile = directUploadEnabled && " in src
    for marker in ("LARGE_UPLOAD_THRESHOLD_BYTES", "_directUploadFile", "/upload/init", "/upload/finalize"):
        assert marker in src, f"{js_name} lost {marker!r}"
    # The multipart path is still there for the flag-off (customer) case.
    assert "fetch('/upload', { method: 'POST', body: formData })" in src


def test_template_emits_server_flag():
    html = (_ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert "window.__DIRECT_UPLOAD__ = {{ 'true' if direct_upload_enabled else 'false' }}" in html


def test_routes_upload_never_imports_google_at_import():
    """The customer image must work with no GCS environment: google.* is
    imported lazily inside gcs_upload's functions, never at module import."""
    code = ("import sys, routes.upload, gcs_upload; "
            "bad=[m for m in sys.modules if m.startswith('google')]; "
            "assert not bad, bad; print('clean')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(_ROOT))
    assert out.returncode == 0, out.stderr
    assert "clean" in out.stdout


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _make_app(monkeypatch):
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
def anon_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "GCS_UPLOAD_BUCKET", "")
    local_store._DATAFRAME_CACHE.invalidate()
    yield TestClient(_make_app(monkeypatch))
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def client(anon_client):
    anon_client.post(f"/_login/{OWNER}")
    return anon_client


class FakeGCS:
    """Stands in for gcs_upload's four network functions (no google import)."""

    def __init__(self, size=len(CSV), download_raises=False):
        self.size = size
        self.download_raises = download_raises
        self.signed = []
        self.deleted = []
        self.downloaded = []

    def sign_put_url(self, object_path, content_type, minutes=15):
        self.signed.append((object_path, content_type, minutes))
        return f"https://storage.googleapis.com/test-bucket/{object_path}?X-Goog-Signature=fake"

    def blob_size(self, object_path):
        return self.size

    def download_to(self, object_path, dest):
        if self.download_raises:
            raise RuntimeError("boom")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(CSV)
        self.downloaded.append((object_path, str(dest)))
        return len(CSV)

    def delete_blob(self, object_path):
        self.deleted.append(object_path)


def _enable(monkeypatch, fake):
    import routes.upload as upload_mod
    monkeypatch.setattr(settings, "GCS_UPLOAD_BUCKET", "test-bucket")
    for fn in ("sign_put_url", "blob_size", "download_to", "delete_blob"):
        monkeypatch.setattr(upload_mod.gcs_upload, fn, getattr(fake, fn))
    return fake


def _init(client, name="data.csv", size=1024, ctype="text/csv"):
    return client.post("/upload/init", json={"filename": name, "content_type": ctype, "size_bytes": size})


# ---------------------------------------------------------------------------
# Bucket UNSET — the customer default: everything stays as before
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", sorted(STUB_BODIES))
def test_disabled_by_default_400_with_or_without_session(anon_client, path):
    # no session, garbage body
    r = anon_client.post(path, content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == STUB_BODIES[path]
    # logged in, well-formed body — still the same 400 (the gate is the bucket, not auth)
    anon_client.post(f"/_login/{OWNER}")
    r = anon_client.post(path, json={"filename": "x.csv", "size_bytes": 1, "gcs_path": f"tmp/{SID}/x/x.csv"})
    assert r.status_code == 400 and r.json()["error"] == STUB_BODIES[path]


def test_multipart_upload_unchanged_by_factoring(client):
    r = client.post("/upload", files=[("files", ("data.csv", io.BytesIO(CSV), "text/csv"))])
    assert r.status_code == 200
    js = r.json()
    assert js["ok"] is True and js["saved"] == ["data.csv"] and js["dataframes"] == ["data.csv"]
    assert js["files"] == [{"file": "data.csv", "status": "ok"}]


def test_multipart_upload_all_failed_still_400(client):
    r = client.post("/upload", files=[("files", ("junk.bin", io.BytesIO(b"\x00\x01"), "application/octet-stream"))])
    assert r.status_code == 400
    assert r.json()["ok"] is False and "junk.bin" in r.json()["error"]


# ---------------------------------------------------------------------------
# Bucket SET — init
# ---------------------------------------------------------------------------
def test_upload_from_url_stays_400_when_enabled(client, monkeypatch):
    _enable(monkeypatch, FakeGCS())
    r = client.post("/upload_from_url", json={"url": "https://docs.google.com/x"})
    assert r.status_code == 400 and r.json()["error"] == STUB_BODIES["/upload_from_url"]


def test_init_requires_session(anon_client, monkeypatch):
    _enable(monkeypatch, FakeGCS())
    assert _init(anon_client).status_code == 401


def test_init_returns_session_namespaced_signed_url(client, monkeypatch):
    fake = _enable(monkeypatch, FakeGCS())
    r = _init(client, size=40 * 1024 * 1024)
    assert r.status_code == 200, r.text
    js = r.json()
    assert js["gcs_path"].startswith(f"tmp/{SID}/") and js["gcs_path"].endswith("/data.csv")
    assert js["signed_url"].startswith("https://storage.googleapis.com/")
    assert js["expires_at"].endswith("+00:00")
    assert fake.signed == [(js["gcs_path"], "text/csv", 15)]


@pytest.mark.parametrize("name,size,frag", [
    ("payload.exe", 1024, "Unsupported file type"),
    (".hidden.csv", 1024, "Invalid filename"),
    ("..", 1024, "Invalid filename"),
    ("data.csv", 0, "File too large"),
    ("data.csv", 500 * 1024 * 1024 + 1, "File too large"),
])
def test_init_rejects_bad_name_extension_size(client, monkeypatch, name, size, frag):
    fake = _enable(monkeypatch, FakeGCS())
    r = _init(client, name=name, size=size)
    assert r.status_code == 400 and frag in r.json()["error"]
    assert fake.signed == []  # nothing signed for a rejected request


def test_init_uses_basename_of_a_traversal_path(client, monkeypatch):
    """B2C rule: directory components are dropped, the basename is the key.
    The object lives under tmp/{sid}/{uuid}/ and the local file under the
    session files_dir, so a traversal prefix can never escape either."""
    _enable(monkeypatch, FakeGCS())
    r = _init(client, name="../../etc/passwd.csv")
    assert r.status_code == 200
    assert r.json()["gcs_path"].startswith(f"tmp/{SID}/") and r.json()["gcs_path"].endswith("/passwd.csv")
    assert "/../" not in r.json()["gcs_path"]


def test_init_invalid_body_is_400_not_422(client, monkeypatch):
    _enable(monkeypatch, FakeGCS())
    r = client.post("/upload/init", json={"filename": "data.csv"})  # size_bytes missing
    assert r.status_code == 400 and r.json()["error"] == "Invalid request."


def test_init_counts_distinct_source_files_not_sheets(client, monkeypatch):
    """MAX_FILES counts SOURCE files: a 3-sheet workbook is one, database
    entries are none (the B2C raw len(meta['files']) refused a 2nd file after
    a multi-sheet workbook)."""
    _enable(monkeypatch, FakeGCS())
    monkeypatch.setattr(settings, "MAX_FILES", 2)
    store = local_store.UserStore(SID)
    meta = store.read_meta()
    meta["files"] = [
        {"file_name": "book.xlsx::S1", "schema": {}}, {"file_name": "book.xlsx::S2", "schema": {}},
        {"file_name": "book.xlsx::S3", "schema": {}},
        {"file_name": "clients", "source": "database", "schema": {}},
    ]
    store.write_meta(meta)
    assert _init(client, name="second.csv").status_code == 200      # 1 source file so far
    meta["files"].append({"file_name": "second.csv", "schema": {}})
    store.write_meta(meta)
    r = _init(client, name="third.csv")
    assert r.status_code == 400 and "up to 2 files" in r.json()["error"]
    assert _init(client, name="second.csv").status_code == 200      # re-upload of a present name is fine


def test_init_signing_failure_is_fixed_500(client, monkeypatch):
    fake = _enable(monkeypatch, FakeGCS())

    def boom(*a, **k):
        raise RuntimeError("https://iamcredentials.googleapis.com/secret-looking-url")
    import routes.upload as upload_mod
    monkeypatch.setattr(upload_mod.gcs_upload, "sign_put_url", boom)
    r = _init(client)
    assert r.status_code == 500
    assert r.json()["error"] == "Direct upload is not configured on this server."


# ---------------------------------------------------------------------------
# Bucket SET — finalize
# ---------------------------------------------------------------------------
def test_finalize_rejects_foreign_namespace(client, monkeypatch):
    fake = _enable(monkeypatch, FakeGCS())
    for bad in ("tmp/s_someoneelse/abc/data.csv", f"/tmp/{SID}/../s_other/data.csv", "data.csv", ""):
        r = client.post("/upload/finalize", json={"gcs_path": bad})
        assert r.status_code == 400, bad
        assert r.json()["error"] == "Invalid upload path."
    assert fake.downloaded == [] and fake.deleted == []


def test_finalize_rejects_bad_name_in_namespace(client, monkeypatch):
    fake = _enable(monkeypatch, FakeGCS())
    r = client.post("/upload/finalize", json={"gcs_path": f"tmp/{SID}/abc/payload.exe"})
    assert r.status_code == 400 and r.json()["error"] == "Invalid filename in upload path."
    assert fake.downloaded == []


def test_finalize_404_when_object_missing(client, monkeypatch):
    fake = _enable(monkeypatch, FakeGCS(size=None))
    r = client.post("/upload/finalize", json={"gcs_path": f"tmp/{SID}/abc/data.csv"})
    assert r.status_code == 404 and "not found" in r.json()["error"]
    assert fake.downloaded == []


def test_finalize_bounds_real_object_size(client, monkeypatch):
    """A signed PUT does not bind Content-Length — the REAL size is checked."""
    fake = _enable(monkeypatch, FakeGCS(size=500 * 1024 * 1024 + 1))
    path = f"tmp/{SID}/abc/data.csv"
    r = client.post("/upload/finalize", json={"gcs_path": path})
    assert r.status_code == 400 and "File too large" in r.json()["error"]
    assert fake.downloaded == [] and fake.deleted == [path]


def test_finalize_success_runs_upload_pipeline_and_deletes(client, monkeypatch):
    fake = _enable(monkeypatch, FakeGCS())
    init = _init(client).json()
    r = client.post("/upload/finalize", json={"gcs_path": init["gcs_path"]})
    assert r.status_code == 200, r.text
    js = r.json()
    assert js == {"ok": True, "saved": ["data.csv"], "dataframes": ["data.csv"],
                  "files": [{"file": "data.csv", "status": "ok"}]}
    assert fake.deleted == [init["gcs_path"]]
    store = local_store.UserStore(SID)
    assert (store.files_dir / "data.csv").read_bytes() == CSV
    names = [e["file_name"] for e in store.read_meta()["files"]]
    assert names == ["data.csv"]
    # a second finalize in the same batch accumulates (no reset), reports only its own file
    init2 = _init(client, name="more.csv").json()
    r2 = client.post("/upload/finalize", json={"gcs_path": init2["gcs_path"]})
    assert r2.status_code == 200
    assert r2.json()["saved"] == ["more.csv"] and r2.json()["files"] == [{"file": "more.csv", "status": "ok"}]
    assert sorted(e["file_name"] for e in store.read_meta()["files"]) == ["data.csv", "more.csv"]


def test_finalize_deletes_object_even_when_download_fails(client, monkeypatch):
    fake = _enable(monkeypatch, FakeGCS(download_raises=True))
    path = f"tmp/{SID}/abc/data.csv"
    r = client.post("/upload/finalize", json={"gcs_path": path})
    assert r.status_code == 500
    assert r.json()["error"] == "Failed to import the uploaded file. Please try again."
    assert fake.deleted == [path]


def test_finalize_keeps_database_entries_and_ids(client, monkeypatch):
    """No reset on the direct path — DB selections made before the upload survive."""
    fake = _enable(monkeypatch, FakeGCS())
    store = local_store.UserStore(SID)
    meta = store.read_meta()
    meta["files"] = [{"file_name": "clients", "source": "database", "schema": {"file_name": "clients", "fields": {}}}]
    meta["db_table_ids"] = ["0123456789abcdef"]
    store.write_meta(meta)
    init = _init(client).json()
    assert client.post("/upload/finalize", json={"gcs_path": init["gcs_path"]}).status_code == 200
    meta = store.read_meta()
    assert meta["db_table_ids"] == ["0123456789abcdef"]
    assert sorted(e["file_name"] for e in meta["files"]) == ["clients", "data.csv"]


def test_unicode_filename_round_trips(client, monkeypatch):
    """Georgian names must survive (B2C's ASCII regex would have made them '_____.csv')."""
    fake = _enable(monkeypatch, FakeGCS())
    name = "გაყიდვები 2026.csv"
    init = _init(client, name=name).json()
    assert init["gcs_path"].endswith("/" + name)
    r = client.post("/upload/finalize", json={"gcs_path": init["gcs_path"]})
    assert r.status_code == 200 and r.json()["saved"] == [name]
    assert (local_store.UserStore(SID).files_dir / name).is_file()
