"""Offline plotly.js regression tests (Prompt 21 — empty charts on CDN-blocked LANs).

Chart HTML must reference the locally served /static/vendor/plotly/plotly.min.js,
never require cdn.plot.ly at view time. The asset is copied from the plotly pip
package (Docker build + app lifespan); when it is missing the generator falls
back to the CDN src (pre-fix behavior, loudly logged) — never a broken src.
"""
import plotly.graph_objects as go
import pytest

import plot_utils


def _fig():
    return go.Figure(data=[go.Bar(x=["a", "b"], y=[1, 2])])


@pytest.fixture
def asset_in_tmp(tmp_path, monkeypatch):
    """Point the asset path at tmp_path (never the repo tree) and reset the
    once-per-process missing-asset log flag."""
    dst = tmp_path / "vendor" / "plotly" / "plotly.min.js"
    monkeypatch.setattr(plot_utils, "_plotly_js_asset_path", lambda: dst)
    monkeypatch.setattr(plot_utils, "_plotly_js_missing_logged", False)
    return dst


def test_ensure_asset_copies_and_is_idempotent(asset_in_tmp):
    plot_utils.ensure_plotly_js_asset()
    assert asset_in_tmp.exists()
    size = asset_in_tmp.stat().st_size
    assert size > 1_000_000  # the real bundle is ~4.5MB — a stub means a broken copy
    mtime = asset_in_tmp.stat().st_mtime_ns

    plot_utils.ensure_plotly_js_asset()  # second run must no-op (same size)
    assert asset_in_tmp.stat().st_mtime_ns == mtime
    # no leftover tmp file from the atomic write
    assert list(asset_in_tmp.parent.glob("*.tmp")) == []


def test_generated_html_uses_local_src(asset_in_tmp):
    plot_utils.ensure_plotly_js_asset()
    html = plot_utils._plotly_to_html(_fig())
    assert plot_utils._PLOTLY_JS_URL in html
    assert "cdn.plot.ly" not in html
    # plotly's to_html string-src branch requires a bare .js URL
    assert plot_utils._PLOTLY_JS_URL.endswith(".js")


def test_generated_html_falls_back_to_cdn_when_asset_missing(asset_in_tmp):
    # asset never copied — include must degrade to the pre-fix CDN src
    html = plot_utils._plotly_to_html(_fig())
    assert "cdn.plot.ly" in html
    assert plot_utils._PLOTLY_JS_URL not in html


def test_plotly_asset_served_by_static_mount():
    """The real app serves the materialized bundle at the URL baked into chart HTML."""
    from starlette.testclient import TestClient
    import app as app_mod

    plot_utils.ensure_plotly_js_asset()  # real path — idempotent, gitignored
    client = TestClient(app_mod.app)  # no lifespan needed for the static mount
    resp = client.get(plot_utils._PLOTLY_JS_URL)
    assert resp.status_code == 200
    assert len(resp.content) > 1_000_000
    assert b"Plotly" in resp.content[:200_000]
