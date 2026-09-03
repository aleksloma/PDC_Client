"""Every upload goes through multipart POST /upload — no direct-to-GCS branch.

The B2C frontend routed files > 25 MB through /upload/init → signed PUT →
/upload/finalize. On-prem those endpoints are 400 stubs by design (Article V),
so the branch could only fail: any file over the threshold surfaced the stub's
error banner and nothing was uploaded. The branch was removed from BOTH static
copies; these tests pin that removal structurally and keep the stubs' clean
400 contract (a stale cached B2C page must still get JSON, never a 404).
"""
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC = os.path.join(_ROOT, "static")

_GCS_MARKERS = (
    "/upload/init",
    "/upload/finalize",
    "LARGE_UPLOAD_THRESHOLD_BYTES",
    "_directUploadFile",
    "hasLargeFile",
)


@pytest.mark.parametrize("js_name", ["dashboard.js", "config.js"])
def test_frontend_has_no_direct_to_gcs_branch(js_name):
    src = Path(_STATIC, js_name).read_text(encoding="utf-8")
    for marker in _GCS_MARKERS:
        assert marker not in src, f"{js_name} still references {marker!r}"
    # The multipart path itself must still be there.
    assert "fetch('/upload', { method: 'POST', body: formData })" in src


@pytest.fixture
def client():
    import routes.upload as upload_mod
    app = FastAPI()
    app.include_router(upload_mod.router)
    return TestClient(app)


@pytest.mark.parametrize("path", ["/upload/init", "/upload/finalize", "/upload_from_url"])
def test_direct_upload_stubs_stay_clean_400(client, path):
    r = client.post(path, json={"filename": "x.csv", "size_bytes": 1})
    assert r.status_code == 400
    assert "error" in r.json()
