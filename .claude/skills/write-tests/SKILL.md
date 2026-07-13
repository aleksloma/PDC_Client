---
name: write-tests
description: Write pytest tests for PDC_Client the way the existing suite does — offline, brain calls stubbed via the fake-client pattern, DATA_ROOT isolated to tmp_path, regression test per bugfix.
---
# Writing client tests

Location: `tests/test_*.py`. `tests/conftest.py` puts the repo root on
`sys.path` — rely on it, don't duplicate path hacks.

## Mocking seams (use these, not ad-hoc patches)
- **Brain HTTP**: the seam is `brain_client._get_client` — monkeypatch it to
  return a fake client (`_FakeClient`/`_FakeResp` pattern in
  `tests/test_brain_debug_logging.py`). Set
  `monkeypatch.setattr(brain_client.settings, "BRAIN_TENANT_TOKEN", "tkn", raising=False)`.
  NEVER the live brain, never a real token.
- **Planner/summarizer level**: for chat-flow logic, monkeypatch the
  functions `run_chat_local` calls (planner, retry, summarize) and feed
  canned `{"raw_text": ..., "kind": ...}` dicts — pattern in
  `tests/test_retry_loop.py` (including the `###NEXT_PLOT###` block builder).
- **Settings/flags**: `monkeypatch.setattr(settings, "FLAG", value, raising=False)`.
- **Storage**: monkeypatch `DATA_ROOT` / store roots to `tmp_path`. NEVER
  `./client_data`, NEVER the volume.
- **Endpoints**: FastAPI `TestClient(app)` — pattern in
  `tests/test_disabled_stubs.py` and `tests/test_excel_endpoints.py`.
- **Kaleido/PNG**: follow `tests/test_export_plotly_png.py`'s handling for
  machines without a working kaleido.
- **Log assertions**: monkeypatch `log_with_sid` where imported and capture.

## Rules
- Every bugfix ships with a regression test asserting the fixed behavior
  (existing test docstrings name the fix they pin — keep doing that).
- **Data-boundary tests**: when touching anything that posts to the brain,
  assert the posted payload contains NO forbidden keys
  (`image_base64`, `chart_data`, `table`, row values) — `_sanitize_history_rows`
  and `_safe_preview` are the guards under test.
- **Stored-format compatibility**: any change to a persisted shape
  (`users/{email}/*`, `chatdata/{chat_id}/meta.json`,
  `conversations/*.jsonl`, parquet-cache manifest) gets a test that loads an
  OLD-shape fixture and asserts it still works after the change.
- Never weaken or delete an existing assertion.
- No network, no ordering dependence between tests.
