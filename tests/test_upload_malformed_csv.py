"""Malformed CSV uploads must fail loudly, never silently (QA 2.1).

A CSV with an unquoted comma inside a value makes rows ragged; the strict
parse fails, and /upload used to answer {"ok": true, ..., "dataframes": []}
with no error at all — the Create-New-Chat dialog then died silently. Now:
tolerant parse (skip + COUNT bad rows) with a per-file warning; hopeless
files produce a per-file error and ok:false.
"""
import io
import json

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import local_store
from local_store import _read_csv_tolerant
from settings import settings

OWNER = "user@x.com"

RAGGED_CSV = b"name,hired,salary\nalice,2022-01-05,100\nbob,March 3, 2022,200\ncara,2022-04-01,300\n"
GOOD_CSV = b"a,b\n1,2\n3,4\n"
# not UTF-8/UTF-16, not parseable as any CSV by either engine
HOPELESS = bytes(range(256)) * 4


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()

    import routes.upload as upload_mod
    monkeypatch.setattr(upload_mod.brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(upload_mod.brain_client, "file_description",
                        lambda **k: {"description": ""})

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(upload_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = "s_badcsv"
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    local_store._DATAFRAME_CACHE.invalidate()
    yield tc
    local_store._DATAFRAME_CACHE.invalidate()


def _upload(tc, files):
    return tc.post("/upload", files=[
        ("files", (name, io.BytesIO(content), "text/csv")) for name, content in files])


# --- API layer ---------------------------------------------------------------

def test_ragged_csv_parses_with_warning_and_count(client):
    resp = _upload(client, [("hr.csv", RAGGED_CSV)])
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["dataframes"] == ["hr.csv"]
    row = data["files"][0]
    assert row["file"] == "hr.csv"
    assert row["status"] == "warning"
    assert row["skipped_rows"] == 1
    assert row["first_bad_line"] == 3          # "bob,March 3, 2022,200"
    assert "1 malformed row(s) skipped" in row["message"]


def test_hopeless_file_alone_is_400_with_per_file_error(client):
    resp = _upload(client, [("junk.csv", HOPELESS)])
    assert resp.status_code == 400
    data = resp.json()
    assert data["ok"] is False
    assert data["dataframes"] == []
    assert data["files"][0]["status"] == "error"
    assert "junk.csv" in data["error"]


def test_mixed_good_and_hopeless_is_ok_false_naming_bad_file(client):
    resp = _upload(client, [("good.csv", GOOD_CSV), ("junk.csv", HOPELESS)])
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False                 # frontend aborts + shows error
    assert data["dataframes"] == ["good.csv"]
    statuses = {r["file"]: r["status"] for r in data["files"]}
    assert statuses == {"good.csv": "ok", "junk.csv": "error"}
    assert "junk.csv" in data["error"]


def test_clean_files_keep_ok_true_with_per_file_ok(client):
    resp = _upload(client, [("good.csv", GOOD_CSV)])
    data = resp.json()
    assert data["ok"] is True
    assert data["files"] == [{"file": "good.csv", "status": "ok"}]


def test_post_upload_load_matches_upload_row_count(client, tmp_path):
    _upload(client, [("hr.csv", RAGGED_CSV)])
    dfs = local_store.UserStore("s_badcsv").load_dataframes(include_db=False)
    # 3 data lines, 1 skipped → 2 rows, consistently on every later load
    assert len(dfs["hr.csv"]) == 2


# --- parse layer -------------------------------------------------------------

def test_read_csv_tolerant_three_outcomes(tmp_path):
    clean = tmp_path / "clean.csv"
    clean.write_bytes(GOOD_CSV)
    df, warn = _read_csv_tolerant(clean)
    assert warn is None and len(df) == 2

    ragged = tmp_path / "ragged.csv"
    ragged.write_bytes(RAGGED_CSV)
    df, warn = _read_csv_tolerant(ragged)
    assert len(df) == 2
    assert warn == {"skipped_rows": 1, "first_bad_line": 3}

    junk = tmp_path / "junk.csv"
    junk.write_bytes(HOPELESS)
    df, warn = _read_csv_tolerant(junk)
    assert df is None
    assert "error" in warn
