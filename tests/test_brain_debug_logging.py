"""Unit tests for brain-call debug logging in brain_client._post.

The brain HTTP layer is mocked. We assert: CLIENT_LLM_DEBUG on → BRAIN_REQUEST /
BRAIN_RESPONSE lines emitted and field-truncated; off → absent; a non-200 brain
response always logs the status + body snippet (never swallowed).
"""
import pytest

import brain_client


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    def post(self, path, json=None, headers=None, timeout=None):
        return self._resp


@pytest.fixture
def records(monkeypatch):
    """Capture every log_with_sid call as (sid, level, message, context)."""
    out = []
    monkeypatch.setattr(brain_client, "log_with_sid",
                        lambda sid, level, message, **ctx: out.append((sid, level, message, ctx)))
    monkeypatch.setattr(brain_client.settings, "BRAIN_TENANT_TOKEN", "tkn", raising=False)
    return out


def _install_resp(monkeypatch, resp):
    monkeypatch.setattr(brain_client, "_get_client", lambda: _FakeClient(resp))


def _messages(records):
    return [m for (_s, _l, m, _c) in records]


def test_debug_on_emits_request_and_response(monkeypatch, records):
    monkeypatch.setattr(brain_client.settings, "CLIENT_LLM_DEBUG", True, raising=False)
    _install_resp(monkeypatch, _FakeResp(200, {
        "kind": "PYTHON", "code": "result = df.head()", "raw_text": "...",
        "usage": {"finish_reason": "STOP", "total_tokens": 42},
    }))
    out = brain_client.plan(sid="s1", question="avg salary by dept", schema_text="File: x",
                            df_names=["a.csv"], history_rows=[{"role": "human", "content": "hi"}],
                            common_fields=[], user_email="alice@acme.com")
    assert out["kind"] == "PYTHON"
    msgs = _messages(records)
    assert "BRAIN_REQUEST" in msgs
    assert "BRAIN_RESPONSE" in msgs
    # Request carries the metadata fields; response carries kind + finish_reason.
    req_ctx = next(c for (_s, _l, m, c) in records if m == "BRAIN_REQUEST")
    assert req_ctx["endpoint"] == "/v1/plan"
    assert req_ctx["question"] == "avg salary by dept"
    assert req_ctx["history_n"] == 1
    resp_ctx = next(c for (_s, _l, m, c) in records if m == "BRAIN_RESPONSE")
    assert resp_ctx["kind"] == "PYTHON"
    assert resp_ctx["finish_reason"] == "STOP"


def test_debug_truncates_long_fields(monkeypatch, records):
    monkeypatch.setattr(brain_client.settings, "CLIENT_LLM_DEBUG", True, raising=False)
    monkeypatch.setattr(brain_client.settings, "CLIENT_LLM_DEBUG_MAX_CHARS", 10, raising=False)
    _install_resp(monkeypatch, _FakeResp(200, {"kind": "PYTHON", "usage": {}}))
    brain_client.plan(sid="s1", question="Q" * 500, schema_text="S",
                      df_names=["a.csv"], history_rows=[], common_fields=[], user_email="a@x.com")
    req_ctx = next(c for (_s, _l, m, c) in records if m == "BRAIN_REQUEST")
    assert req_ctx["question"].startswith("QQQQQQQQQQ")  # 10 chars
    assert "…(+490 chars)" in req_ctx["question"]


def test_debug_off_no_request_response_lines(monkeypatch, records):
    monkeypatch.setattr(brain_client.settings, "CLIENT_LLM_DEBUG", False, raising=False)
    _install_resp(monkeypatch, _FakeResp(200, {"kind": "PYTHON", "usage": {}}))
    brain_client.plan(sid="s1", question="q", schema_text="s",
                      df_names=["a.csv"], history_rows=[], common_fields=[], user_email="a@x.com")
    msgs = _messages(records)
    assert "BRAIN_REQUEST" not in msgs
    assert "BRAIN_RESPONSE" not in msgs
    assert any(m.startswith("BRAIN_OK") for m in msgs)  # normal path still logged


def test_non_200_always_logs_status_and_body(monkeypatch, records):
    # Even with debug OFF, a 4xx must be logged with the body snippet.
    monkeypatch.setattr(brain_client.settings, "CLIENT_LLM_DEBUG", False, raising=False)
    _install_resp(monkeypatch, _FakeResp(400, json_data=None, text="bad request: malformed payload XYZ"))
    with pytest.raises(brain_client.BrainError):
        brain_client.plan(sid="s1", question="q", schema_text="s",
                          df_names=["a.csv"], history_rows=[], common_fields=[], user_email="a@x.com")
    line = next((f"{m}") for (_s, _l, m, _c) in records if m.startswith("BRAIN_4XX"))
    assert "400" in line and "malformed payload XYZ" in line


def test_revoked_token_logs_body(monkeypatch, records):
    monkeypatch.setattr(brain_client.settings, "CLIENT_LLM_DEBUG", False, raising=False)
    _install_resp(monkeypatch, _FakeResp(403, json_data=None, text="tenant revoked detail"))
    with pytest.raises(brain_client.TenantRevokedError):
        brain_client.plan(sid="s1", question="q", schema_text="s",
                          df_names=["a.csv"], history_rows=[], common_fields=[], user_email="a@x.com")
    assert any(m.startswith("BRAIN_AUTH") and "403" in m for (_s, _l, m, _c) in records)
