"""Build identity: GET /version, /health's additive build_commit, and the
admin sidebar stamp.

The point of the feature: the static `?v=` cache-buster is the page-render
clock (recomputed per request), so it can never say which build is running.

TestClient is built WITHOUT the context manager on purpose — entering it runs
the lifespan, which starts the db_scheduler thread (app.py keeps it lifespan-
scoped precisely so it never leaks into a pytest session).
"""
import pytest
from starlette.testclient import TestClient

import app as app_mod
from settings import settings


@pytest.fixture
def client(monkeypatch):
    # No token -> /health short-circuits brain_reachable to None instead of
    # reaching over the network.
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    return TestClient(app_mod.app)


def test_version_reports_the_baked_build(client, monkeypatch):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    monkeypatch.setattr(settings, "BUILD_TIME", "2026-08-02T10:00Z")
    body = client.get("/version").json()
    assert body["commit"] == "abc1234"
    assert body["build_time"] == "2026-08-02T10:00Z"
    assert body["started_at"]           # always present


def test_version_is_honest_about_an_unstamped_image(client, monkeypatch):
    """Nulls, never a fabricated identity."""
    monkeypatch.setattr(settings, "BUILD_COMMIT", "")
    monkeypatch.setattr(settings, "BUILD_TIME", "")
    body = client.get("/version").json()
    assert body["commit"] is None and body["build_time"] is None
    assert body["started_at"]


def test_version_exposes_no_secrets(client, monkeypatch):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    assert set(client.get("/version").json()) == {
        "commit", "build_time", "started_at"}


def test_health_carries_the_commit_additively(client, monkeypatch):
    """The smoke-test skill asserts on the older keys — they must survive."""
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["tenant_token_configured"] is False
    assert body["brain_reachable"] is None
    assert body["build_commit"] == "abc1234"


def test_build_stamp_prefers_the_commit(monkeypatch):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    monkeypatch.setattr(settings, "BUILD_TIME", "2026-08-02T10:00Z")
    assert app_mod._build_stamp() == "build abc1234 · 2026-08-02T10:00Z"


def test_build_stamp_without_a_build_time(monkeypatch):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    monkeypatch.setattr(settings, "BUILD_TIME", "")
    assert app_mod._build_stamp() == "build abc1234"


def test_build_stamp_falls_back_to_the_start_time(monkeypatch):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "")
    monkeypatch.setattr(settings, "BUILD_TIME", "")
    stamp = app_mod._build_stamp()
    assert stamp.startswith("started ") and stamp.endswith(" UTC")
