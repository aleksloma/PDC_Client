---
name: add-brain-call
description: Checklist for adding a new client→brain /v1 call (brain_client wrapper) without violating the data boundary — sanitization, timeout, fallback, debug logging, protocol mirror, tests.
---
# Add a client→brain `/v1` call

The data boundary (Constitution Art. II) is the blocking constraint: raw
data values never leave this container.

1. **Wrapper in `brain_client.py`**, shaped like the existing ones:
   `_get_client()`, bearer header from `BRAIN_TENANT_TOKEN`,
   `BRAIN_REQUEST_TIMEOUT`, non-200 → log status + body via `log_with_sid`
   (never swallowed), return a safe fallback so callers degrade gracefully.
2. **Sanitize everything that crosses**:
   - history → `_sanitize_history_rows` (role/content(+code) only — the
     persisted `image_base64`/`chart_data`/`table`/`usage` never leave);
   - previews → `_safe_preview` semantics (str|int|float|bool only);
   - NEVER DataFrames, rows, cell values, rendered charts, templates.
3. **Debug logging**: emit truncated `BRAIN_REQUEST` / `BRAIN_RESPONSE`
   lines gated on `CLIENT_LLM_DEBUG`, like the existing wrappers.
4. **Protocol first**: the shape must already exist in `docs/PROTOCOL.md`
   (added on the brain side via its `/add-v1-endpoint` skill); the copies in
   both repos stay byte-identical (`/sync-docs`).
5. **Callers** (routes / run_chat_local / auto_analytics): handle the
   fallback value; the UI must degrade, not crash (Art. IV).
6. **Tests** (see `/write-tests`): fake-client pattern from
   `tests/test_brain_debug_logging.py`; assert (a) the success path, (b) the
   non-200 fallback + logged body, and (c) the posted payload contains no
   forbidden keys — the sanitization test is mandatory.
7. **Docs**: `CLAUDE.md` file-reference row for `brain_client.py` if the
   surface changed; `docs/CLIENT_ENDPOINTS.md` if a `/lab` endpoint fronts it.
8. Reviewer agent on the diff; `/smoke-test` before commit.
