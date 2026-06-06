# PowerDataChat Enterprise — Client AI Constitution

> Engineering standards and coding rules for AI agents working on the
> **client** (on-prem) container. The client runs inside the customer's
> network, holds the raw data, and talks to a hosted brain service over an
> authenticated HTTPS API.

**Version:** 1.0 (client)
**Last Updated:** 2026-05-25

---

## Article I: Read Before Write

**No code shall be written before understanding context.**

1. Read [`docs/ENTERPRISE_ARCHITECTURE.md`](ENTERPRISE_ARCHITECTURE.md) — the client overview + the data boundary.
2. Read [`docs/PROTOCOL.md`](PROTOCOL.md) — the brain `/v1/*` API this client calls.
3. Read [`docs/CLIENT_ENDPOINTS.md`](CLIENT_ENDPOINTS.md) — the client's own HTTP surface.
4. Read the relevant client source files before modifying them.
5. Check `MEMORY.md` (in your agent's memory folder) for patterns and pitfalls.

**Rationale:** This codebase has one hard invariant — no raw data values cross
the boundary to the brain — that is easy to violate without context.

---

## Article II: The Data Boundary Is Sacred

**Raw data values must never cross from the client container to the brain.**

The split is the whole product. Violating it defeats the on-prem promise.

### What MAY cross to the brain
- The question text (natural language).
- `schema_text` — column names, dtypes, descriptions, and lightly-sampled
  metadata (truncated unique values, cardinality hints).
- Conversation history of past **text** turns (role/content/code), never
  values.
- Generated code (Python) and execution error text.
- Findings for reports — `{question, answer_text, has_chart, has_table,
  table_columns (NAMES only), code_snippet}`.
- Operational events for `/v1/activity` — `event`, `user_email`, lightweight
  metadata.

### What MUST NOT cross
- DataFrame rows, cell values, or computed result tables.
- Rendered charts (PNG/HTML).
- The customer's branded templates.
- Any value that the user uploaded.

### The guard
The summarizer's `_safe_preview` helper (in
[`run_chat_local.py`](../run_chat_local.py)) is the hard guard against
accidental row leakage in `/v1/summarize`: only `str | int | float | bool`
pass through; dicts, lists, DataFrames are dropped to `None`. Do not weaken or
bypass this.

### Client-side logging
The client logs metadata only — never full request/response payloads — by
default (`log_with_sid`, truncated). Client logs stay on the local host.

**Exception — `CLIENT_LLM_DEBUG` (default OFF).** When the operator sets the
`CLIENT_LLM_DEBUG` env flag ON, `brain_client._post` emits the full brain
request (`BRAIN_REQUEST` — question, df_names, schema_text, history size,
error_msg, failed_code) and the full brain response (`BRAIN_RESPONSE` — kind,
code, text, finish_reason, usage), each truncated to `CLIENT_LLM_DEBUG_MAX_CHARS`
(default 20000). This stays inside the data boundary because the brain request
carries no raw row values by design (question text, schema/column names,
generated code, error text — see above); there are no row values to leak. It is
a diagnostic switch, OFF by default, and must be turned back OFF once validation
is done. Independently of the flag, the HTTP status + error body on any non-200
or transport error is logged UNCONDITIONALLY (truncated to ~2000 chars) — a
brain rejection must never be silently swallowed (Article IV).

---

## Article III: The Client Never Calls a Model

**All LLM work happens on the brain. The client never calls a model directly
and holds no model API keys.**

The client builds the no-value request (schema text, df names, generated code,
error text, findings), calls the brain over HTTPS, and executes / renders the
result locally. When you need model output, add (or reuse) a `/v1/*` call in
[`brain_client.py`](../brain_client.py) — never embed a model SDK in the
client.

---

## Article IV: Error Handling

**All errors must be caught, logged, and handled gracefully.**

### Required pattern
```python
try:
    ...
except Exception as e:
    log_with_sid(sid, "error", f"Operation failed: {e}", context=...)
    return fallback_value          # never crash silently
```

### Retry on transient errors
```python
def _is_retryable_status(status_code: int) -> bool:
    return status_code in (429, 500, 502, 503, 504)

# settings on the client
max_retries = settings.LLM_MAX_RETRIES        # 3
backoff     = settings.LLM_INITIAL_BACKOFF    # 1.0s
multiplier  = settings.LLM_BACKOFF_MULTIPLIER # 2.0
```

### Logging requirements
- Always include `sid` (session ID) for traceability.
- Levels: `info`, `warning`, `error`.
- Context: `chat_id=`, `endpoint=`. Never log raw payloads.

---

## Article V: Local-Filesystem Storage

**On-prem runs against a local filesystem only.**

- Client storage root: `DATA_ROOT` (default `/data/client`), a bind-mounted
  Docker volume (`pdc_client_data`).

There is no `GCSPath` and no cloud-storage adapter on the client. The
`POST /upload/init`, `POST /upload/finalize`, `POST /upload_from_url`
endpoints return 400 by design — see [`docs/CLIENT_ENDPOINTS.md`](CLIENT_ENDPOINTS.md).

### JSONL pattern (append-only)
```python
def append_line(path: Path, data: dict) -> None:
    line = json.dumps(data, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
```

### Client storage layout
| Entity                | Path                                                |
|-----------------------|-----------------------------------------------------|
| User profile          | `users/{email}/profile.json`                        |
| Active chats          | `users/{email}/active_chats.jsonl`                  |
| Chat metadata         | `chatdata/{chat_id}/meta.json`                      |
| Conversation history  | `chatdata/{chat_id}/conversations/{conv_id}.jsonl`  |

---

## Article VI: Resource Cleanup

**All resources must be released on shutdown.**

```python
from concurrent.futures import ThreadPoolExecutor
import atexit

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="task_")

def _shutdown():
    _EXECUTOR.shutdown(wait=False, cancel_futures=True)

atexit.register(_shutdown)
```

HTTP clients (`httpx`, `requests.Session`) used to call the brain must be
closed on FastAPI lifespan shutdown.

---

## Article VII: Security

**Never expose credentials, never allow code injection.**

1. Secrets only from environment variables. Never hardcode.
2. The client holds **no model API key** — the brain does.
3. The client authenticates to the brain with `BRAIN_TENANT_TOKEN` (bearer).
   Per-tenant, injected at install time, rotatable by the operator.
4. Never log full tokens (`key[:8]…` is the most you may emit).
5. User-supplied Python is executed only via
   [`code_exec.py`](../code_exec.py)'s `safe_execute()`.
6. Never commit `.env` files. Commit `.env.example` templates only.
7. The client does not send SMTP itself — it relays share invites through the
   brain's `POST /v1/send_share_email` (no raw data forwarded).

---

## Article VIII: Testing Before Release

**The client must boot, health-check, and complete one real
upload→chart→edit→report cycle against a brain you have a valid token for
before any release candidate.**

```bash
docker build -t powerdatachat-client:enterprise .
docker run --rm -p 8000:8000 --env-file client.env \
  -v $(pwd)/client_data:/data/client powerdatachat-client:enterprise
open http://localhost:8000           # email-only login → /lab
```

### Required checks before release
1. Client `GET /health` returns 200 with `brain_reachable: true` and
   `tenant_token_configured: true`.
2. A revoked tenant token returns 403 on every brain call and the UI surfaces
   "service unavailable".
3. Drag-drop a small CSV → schema autofill populates per-column descriptions.
4. Send a chat question → chart renders → refresh page → chart persists.
5. Download PDF and PPTX from a conversation with ≥2 findings.
6. Trigger Auto Analytics → deck file appears under
   `chatdata/{id}/auto_analysis.pptx`.

If UI behavior cannot be verified in a browser, say so explicitly. Type
checks and unit tests are not sufficient evidence for a feature being done.

---

## Article IX: Deployment

**The client is an independent deployable.**

- **Client** (this repo) → built as a self-contained Docker image, handed to
  each customer for installation inside their own network. Customers run it
  themselves. `BRAIN_URL` + the per-tenant `BRAIN_TENANT_TOKEN` are configured
  via environment variables at install time.
- The **brain** is a separate, hosted service operated by PowerDataChat. The
  client never runs or deploys the brain.

The two are NEVER deployed together: that would defeat the split.

---

## Article X: Documentation

**Keep documentation current and minimal.**

### Required docs
- [`docs/ENTERPRISE_ARCHITECTURE.md`](ENTERPRISE_ARCHITECTURE.md) — client overview, the data boundary, invariants.
- [`docs/PROTOCOL.md`](PROTOCOL.md) — the brain `/v1/*` request/response shapes the client calls.
- [`docs/CLIENT_ENDPOINTS.md`](CLIENT_ENDPOINTS.md) — the client HTTP surface and dashboard contract.
- [`docs/BUILD_AND_RUN.md`](BUILD_AND_RUN.md) — build + run + configure + logs.

### Forbidden
- Duplicate documentation (one home per topic).
- Outdated docs (delete or archive — don't leave contradictions).
- README per directory.
- Comments explaining obvious code.
- **Brain internals** (hosting/infra, secret names, the brain's architecture or
  IP, operator/admin procedures) — those belong only in the private brain repo.

If a doc claim contradicts the code, fix the doc in the same PR.

---

## Article XI: Simplicity

**Prefer simple solutions over abstractions.**

1. No wrapper layers around framework capabilities.
2. No premature abstractions (3 uses before extracting a helper).
3. No feature flags for one-time changes.
4. Delete unused code completely — no `# removed for …` comments.
5. Stub a B2C-only endpoint with a clean 400 instead of half-porting it.

---

## Article XII: Commit Standards

**Atomic, descriptive commits.**

```bash
git commit -m "$(cat <<'EOF'
Short summary (imperative, <50 chars)

- Bullet for each change
- Why, not what

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

### Commit checklist
- [ ] Local smoke (client + a brain you control) still passes.
- [ ] No `.env` / token / credential staged.
- [ ] No raw-data values leak across the boundary.
- [ ] No brain internals added to a public doc.
- [ ] Doc updates committed with the code change.
- [ ] No unrelated changes bundled.

---

## Amendment process

To modify this constitution:
1. Propose the change with rationale.
2. Document architectural impact in [`docs/ENTERPRISE_ARCHITECTURE.md`](ENTERPRISE_ARCHITECTURE.md).
3. Update this document.
4. Update memory (`MEMORY.md`) if pattern-related.

---

## Quick reference

| Rule         | Summary                                                                   |
|--------------|---------------------------------------------------------------------------|
| Data boundary| Raw values never cross to the brain. `_safe_preview` is the guard.        |
| LLM calls    | The client never calls a model — it calls the brain's `/v1/*` API.        |
| Storage      | Local filesystem. JSONL for history, JSON for metadata. No GCSPath.       |
| Errors       | Catch → log with sid → return fallback. Never crash silently.             |
| Resources    | `atexit` for executors; close HTTP clients on lifespan shutdown.          |
| Security     | Secrets in env only. Never commit `.env`. The client holds no model key.  |
| Deploy       | Client → customer LAN as a Docker image. Brain → hosted separately.       |
| Docs         | Four required docs under `docs/`. No brain internals in public docs.      |
