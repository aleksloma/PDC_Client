"""Task 2 — the plotly.js self-heal must tolerate a read-only rootfs.

`plot_utils.ensure_plotly_js_asset()` copies the pip package's plotly.min.js
into static/vendor/ at app lifespan. In the hardened image (`read_only: true`,
non-root `pdc`) that directory is not writable, and the asset is already baked
at Docker build — so the lifespan call must:

  * skip the write ENTIRELY when the destination exists with the package
    source's size (no copyfile at all — the idempotency guard IS the read-only
    story for the shipped image);
  * treat a read-only / permission `OSError` (EROFS, EACCES, EPERM) as an
    expected condition: ONE INFO record on the root logger (message carries
    `PLOTLY_JS_ASSET`), never an ERROR;
  * still log ERROR for any other OSError (e.g. ENOSPC — a genuinely broken
    copy that the operator must see).

The asset path is monkeypatched into tmp_path (the `asset_in_tmp` pattern from
tests/test_plotly_offline.py) — never the repo tree. `shutil.copyfile` is
patched on the shutil MODULE because the function imports shutil locally.
"""
import errno
import logging
import shutil
from pathlib import Path

import pytest

import plot_utils


@pytest.fixture
def asset_in_tmp(tmp_path, monkeypatch):
    """Point the asset path at tmp_path (never the repo tree) and reset the
    once-per-process missing-asset log flag."""
    dst = tmp_path / "vendor" / "plotly" / "plotly.min.js"
    monkeypatch.setattr(plot_utils, "_plotly_js_asset_path", lambda: dst)
    monkeypatch.setattr(plot_utils, "_plotly_js_missing_logged", False)
    return dst


def _package_source() -> Path:
    import plotly
    return Path(plotly.__file__).resolve().parent / "package_data" / "plotly.min.js"


def _asset_records(caplog):
    return [r for r in caplog.records if "PLOTLY_JS_ASSET" in r.getMessage()]


@pytest.mark.parametrize("code", [errno.EROFS, errno.EACCES, errno.EPERM])
def test_readonly_or_permission_error_logs_info_never_error(asset_in_tmp, monkeypatch, caplog, code):
    """A read-only rootfs / permission denial on the copy is EXPECTED in the
    hardened container: exactly one INFO record, no ERROR, no exception."""
    def _raise(src, dst, *args, **kwargs):
        raise OSError(code, "read-only or permission denied", str(dst))

    monkeypatch.setattr(shutil, "copyfile", _raise)
    with caplog.at_level(logging.INFO):
        plot_utils.ensure_plotly_js_asset()   # must not raise

    recs = _asset_records(caplog)
    levels = [(r.levelno, r.getMessage()[:80]) for r in recs]
    assert len(recs) == 1, levels
    assert recs[0].levelno == logging.INFO, levels
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], [r.getMessage()[:120] for r in errors]
    assert not asset_in_tmp.exists()


def test_other_oserror_still_logs_error(asset_in_tmp, monkeypatch, caplog):
    """ENOSPC is not a read-only condition — the operator must still see ERROR."""
    def _raise(src, dst, *args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device", str(dst))

    monkeypatch.setattr(shutil, "copyfile", _raise)
    with caplog.at_level(logging.INFO):
        plot_utils.ensure_plotly_js_asset()   # must not raise

    recs = _asset_records(caplog)
    levels = [(r.levelno, r.getMessage()[:80]) for r in recs]
    assert len(recs) == 1, levels
    assert recs[0].levelno == logging.ERROR, levels
    assert not asset_in_tmp.exists()


def test_existing_asset_with_matching_size_triggers_no_write(asset_in_tmp, monkeypatch, caplog):
    """dst present with the package source's size -> no copyfile call at all
    (the shipped image bakes the asset at build; the lifespan run on a
    read-only rootfs must never even attempt the write)."""
    src = _package_source()
    assert src.is_file(), src
    asset_in_tmp.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(src), str(asset_in_tmp))       # the real copy, BEFORE patching
    assert asset_in_tmp.stat().st_size == src.stat().st_size
    mtime_before = asset_in_tmp.stat().st_mtime_ns

    calls = []

    def _fail_if_called(*args, **kwargs):
        calls.append(args)
        raise AssertionError("shutil.copyfile must not be called when the asset is already materialized")

    monkeypatch.setattr(shutil, "copyfile", _fail_if_called)
    with caplog.at_level(logging.INFO):
        plot_utils.ensure_plotly_js_asset()

    assert calls == []
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], [r.getMessage()[:120] for r in errors]
    assert asset_in_tmp.stat().st_mtime_ns == mtime_before
    assert list(asset_in_tmp.parent.glob("*.tmp")) == []
