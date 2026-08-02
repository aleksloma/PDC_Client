"""brain_client timeout plumbing (v4.2).

A stalled brain used to ride the client-wide 180s BRAIN_REQUEST_TIMEOUT even
behind an interactive click. `_post` now takes a per-call timeout and raises a
distinct BrainTimeoutError so callers can name the stalled dependency.
"""
import httpx
import pytest

import brain_client


class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {"ok": True}


class _RecordingClient:
    """Captures the timeout the caller passed; optionally raises instead."""

    def __init__(self, raises=None):
        self.raises = raises
        self.calls = []

    def post(self, path, json=None, headers=None, timeout=None):
        self.calls.append({"path": path, "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        return _Resp()


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(brain_client.settings, "BRAIN_TENANT_TOKEN", "tkn")


def _install(monkeypatch, client):
    monkeypatch.setattr(brain_client, "_get_client", lambda: client)
    return client


def test_read_timeout_raises_brain_timeout_error(monkeypatch, token):
    _install(monkeypatch, _RecordingClient(raises=httpx.ReadTimeout("slow")))
    with pytest.raises(brain_client.BrainTimeoutError):
        brain_client._post("/v1/schema_autofill", {}, "sid")


def test_connect_timeout_raises_brain_timeout_error(monkeypatch, token):
    _install(monkeypatch, _RecordingClient(raises=httpx.ConnectTimeout("nope")))
    with pytest.raises(brain_client.BrainTimeoutError):
        brain_client._post("/v1/schema_autofill", {}, "sid")


def test_timeout_error_is_still_a_brain_error(monkeypatch, token):
    """Callers that only catch BrainError keep working unchanged."""
    _install(monkeypatch, _RecordingClient(raises=httpx.ReadTimeout("slow")))
    with pytest.raises(brain_client.BrainError):
        brain_client._post("/v1/plan", {}, "sid")


def test_non_timeout_network_error_still_raises_plain_brain_error(monkeypatch, token):
    _install(monkeypatch, _RecordingClient(raises=httpx.ConnectError("refused")))
    with pytest.raises(brain_client.BrainError) as ei:
        brain_client._post("/v1/plan", {}, "sid")
    assert not isinstance(ei.value, brain_client.BrainTimeoutError)


def test_timeout_message_carries_no_payload(monkeypatch, token):
    """Article II: the error text names the exception type only."""
    _install(monkeypatch, _RecordingClient(raises=httpx.ReadTimeout("slow")))
    with pytest.raises(brain_client.BrainTimeoutError) as ei:
        brain_client._post("/v1/schema_autofill", {"secret_col": "acme"}, "sid")
    assert "acme" not in str(ei.value)


def test_per_call_timeout_is_forwarded(monkeypatch, token):
    c = _install(monkeypatch, _RecordingClient())
    brain_client.schema_autofill(sid="s", fname="shop.t", cols_to_fill=["a"],
                                 unique_hints={}, timeout=12.5)
    assert c.calls[0]["timeout"] == 12.5


def test_default_keeps_the_client_wide_timeout(monkeypatch, token):
    """No override -> httpx's sentinel, i.e. BRAIN_REQUEST_TIMEOUT as before."""
    c = _install(monkeypatch, _RecordingClient())
    brain_client.schema_autofill(sid="s", fname="shop.t", cols_to_fill=["a"],
                                 unique_hints={})
    assert c.calls[0]["timeout"] is httpx.USE_CLIENT_DEFAULT
