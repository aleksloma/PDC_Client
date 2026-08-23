# PowerDataChat Enterprise — AI Constitution

> Engineering standards and coding rules for AI agents working on the
> enterprise (on-prem) edition. Adapted from the original B2C constitution
> for the brain/client split.

**Version:** 1.1 (enterprise)
**Last Updated:** 2026-07-29

---

## Article I: Read Before Write

**No code shall be written before understanding context.**

1. Read [`docs/ENTERPRISE_ARCHITECTURE.md`](ENTERPRISE_ARCHITECTURE.md) — the decisions document.
2. Read [`docs/PROTOCOL.md`](PROTOCOL.md) — the brain `/v1/*` surface.
3. Read [`docs/CLIENT_ENDPOINTS.md`](CLIENT_ENDPOINTS.md) — the client HTTP surface.
4. Read relevant source files on the side you are editing (`brain/` or `client/`)
   before modifying them.
5. Check `MEMORY.md` (in your agent's memory folder) for patterns and pitfalls.

**Rationale:** This codebase has two hard invariants (no raw data over the
boundary; brain logs only metadata) that are easy to violate without context.

---

## Article II: The Data Boundary Is Sacred

**Raw data values must never cross from the client container to the brain.**

The split is the whole product. Violating it defeats the on-prem promise.

### What MAY cross to the brain
- The question text (natural language).
- `schema_text` — column names, dtypes, descriptions, and the same sampled
  metadata the B2C `_schema_text` already produces (truncated unique values,
  cardinality hints).
- Conversation history of past **text** turns (role/content/code), never
  values.
- Generated code (Python) and execution error text.
- Findings for reports — `{question, answer_text, has_chart, has_table,
  table_columns (NAMES only), code_snippet}`.
- The aggregate dataset profile (`dataset_profile` on `/v1/plan` /
  `/v1/retry`) — row counts, duplicate counts, null rates, min/max,
  constant/all-unique flags, detected grain, deterministic warnings, and
  top-value hints truncated to 40 chars. Same class as the cardinality
  hints `schema_text` already carries; never row data. The client's
  `brain_client._compact_profiles_for_transport` is the boundary guard for
  this field; the brain logs only `profile_tables=N`, never the body.
- Operational events for `/v1/activity` — `event`, `user_email`, lightweight
  metadata.

### What MUST NOT cross
- DataFrame rows, cell values, or computed result tables.
- Rendered charts (PNG/HTML).
- The company's branded templates.
- Any value that the user uploaded.

### The guard
The summarizer's `_safe_preview` helper (in
[`client/run_chat_local.py`](../client/run_chat_local.py)) is the hard guard
against accidental row leakage in `/v1/summarize`: only `str | int | float |
bool` pass through; dicts, lists, DataFrames are dropped to `None`. Do not
weaken or bypass this.

### Brain-side mirror
The brain must NOT log full payloads. Use truncated logging
(`log_with_sid(sid, "info", f"PLAN q='{question[:120]}'")`), never `repr(body)`.

**Exception — `LLM_DEBUG_LOG` (default OFF).** When the operator sets the
`LLM_DEBUG_LOG` env flag ON, `_call_gemini_rest` emits the FULL system
instruction + prompt (`LLM_REQUEST`) and the full raw model response
(`LLM_RESPONSE`), each truncated to `LLM_DEBUG_LOG_MAX_CHARS` (default 20000).
This stays inside the data boundary because the brain only ever receives the
Article II metadata (question text, schema text — which, per Article II,
includes truncated unique-value hints for low-cardinality columns — generated
code, error text); no raw rows or result tables ever reach the brain, so the
debug log can expose at most what the protocol already sends the LLM. It is a
diagnostic switch for validating LLM behavior, OFF by default, and must be
turned back OFF once validation is done. Independently of the flag, the HTTP
error body on any non-200 / exception is logged UNCONDITIONALLY (truncated to
`LLM_ERROR_BODY_LOG_CHARS`, default 2000) — an API rejection must never be
silently swallowed (Article IV).

### Shared-vs-client classification
Skill DEFINITIONS (the YAML files under `brain/skills/domain/` and
`brain/skills/core/`) are shared brain assets, reusable across all
tenants — they are code/config, not per-tenant data. Authoring a new
domain skill from the admin portal writes it into the shared library on
the brain. Only raw client DATA stays client-side. Do not "per-tenant
isolate" skill files.

---

## Article III: REST API Only

**All LLM calls must use REST API, never LangChain or gRPC.**

```python
# CORRECT
from brain_agent import _call_gemini_rest
result = _call_gemini_rest(prompt, api_key, model=eff.get("light_model"))

# FORBIDDEN
from langchain_google_genai import ChatGoogleGenerativeAI   # DO NOT USE
```

**4-tier hybrid (always on):**
- `agent_model` — query classifier (complexity 0-10)
- `light_model` — greetings, trivial (score 0-3)
- `simple_model` — code gen, retry, describe, summarize (score 4-8)
- `complex_model` — deep analysis (score 9-10, thinking enabled)

Per-tenant overrides come from `effective_settings()` on the brain
(`brain/tenant_store.py`). Read the tier choice via `_eff(key, fallback)`
in `brain/brain_agent.py`; do not bake model names into call sites.

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

# settings on each container
max_retries = settings.LLM_MAX_RETRIES        # 3
backoff     = settings.LLM_INITIAL_BACKOFF    # 1.0s
multiplier  = settings.LLM_BACKOFF_MULTIPLIER # 2.0
```

### Logging requirements
- Always include `sid` (session ID / tenant ID) for traceability.
- Levels: `info`, `warning`, `error`.
- Context: `tenant=`, `chat_id=`, `endpoint=`. Never log raw payloads.

---

## Article V: Local-Filesystem Storage (no GCS adapter on-prem)

**On-prem runs against a local filesystem only.**

- Brain storage root: `BRAIN_STORAGE_ROOT` (default `/data/brain`).
- Client storage root: `DATA_ROOT` (default `/data/client`).
- Both default to bind-mounted Docker volumes (`pdc_brain_data`, `pdc_client_data`).

There is no `GCSPath`, no Cloud Run, no signed-URL upload path. The
`POST /upload/init`, `POST /upload/finalize`, `POST /upload_from_url`
endpoints return 400 by design — see [`docs/CLIENT_ENDPOINTS.md`](CLIENT_ENDPOINTS.md).

### JSONL pattern (append-only)
```python
def append_line(path: Path, data: dict) -> None:
    line = json.dumps(data, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
```

### Storage layout
| Side   | Entity                | Path                                                |
|--------|-----------------------|-----------------------------------------------------|
| client | User profile          | `users/{email}/profile.json`                        |
| client | Active chats          | `users/{email}/active_chats.jsonl`                  |
| client | Chat metadata         | `chatdata/{chat_id}/meta.json`                      |
| client | Conversation history  | `chatdata/{chat_id}/conversations/{conv_id}.jsonl`  |
| client | DB sources registry   | `data_sources.json` (connections Fernet-encrypted + registered tables) |
| client | DB table snapshots    | `db_snapshots/{table_id}.parquet` (ONE central copy per table) |
| client | Admin audit trail     | `admin_audit.jsonl` (append-only, secrets scrubbed) |
| brain  | Tenant registry       | `tenants/{tenant_id}/meta.json`                     |
| brain  | Per-tenant config     | `tenants/{tenant_id}/config.json`                   |
| brain  | Per-tenant usage      | `tenants/{tenant_id}/usage.jsonl`                   |
| brain  | Per-tenant activity   | `tenants/{tenant_id}/activity.jsonl`                |
| brain  | Per-tenant user list  | `tenants/{tenant_id}/users.jsonl`                   |

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

HTTP clients (`httpx`, `requests.Session`) used to call the brain from the
client must be closed on FastAPI lifespan shutdown.

---

## Article VII: Security

**Never expose credentials, never allow code injection.**

1. Secrets only from environment variables. Never hardcode.
2. The Gemini API key lives on the brain (in `brain/.env` →
   `GOOGLE_API_KEY`), NOT on the client.
3. The client authenticates to the brain with `BRAIN_TENANT_TOKEN` (bearer).
   Per-tenant. Rotatable from the brain admin panel.
4. Never log full tokens or API keys (`key[:8]…` is the most you may emit).
5. User-supplied Python is executed only via `client/code_exec.safe_execute()`.
6. Never commit `.env` files. Commit `.env.example` templates only.
7. SMTP credentials live on the brain (per-tenant config). The client relays
   share emails through `POST /v1/send_share_email`.
8. **Database credentials (client "Data sources")** are Fernet-encrypted at
   rest (`CLIENT_ENCRYPTION_KEY` env var; no key → the feature refuses to
   save, NEVER a plaintext fallback), masked in every API response, never
   logged, never included in any `brain_client` payload, and only ever
   decrypted into function-locals at the moment of use — never held in
   cleartext at importable module scope.
9. **The code-exec sandbox never gets DB access.** Both exec sites
   (`code_exec.safe_execute`, `plot_utils.render_plot_safe`) install
   `sandbox_guard.SANDBOX_BUILTINS`, whose `__import__` DENIES SQLAlchemy,
   every DB driver (psycopg2, pymysql, pyodbc, oracledb, sqlite3, …) and this
   client's credential modules (`db_connector`, `db_sources`, `db_scheduler`,
   `local_store`, `settings`, `brain_client`, `password_utils`). A denylist,
   deliberately not an allowlist — plotting stacks lazy-import transitively at
   call time, and an allowlist miss would fail-freeze historical stored code.
   **Honest limit:** this is defense in depth, NOT a security boundary
   (`open`/`eval` remain; already-imported modules stay reachable). The
   load-bearing controls are the dedicated SELECT-only database login the
   customer provisions (the grant is the real guarantee), plus rules 8 above
   and the connector's SELECT-only statement gate
   (`db_connector._assert_single_select`; no route accepts free SQL —
   relation discovery's "Analyze SQL" box PARSES pasted SQL, it never
   executes it).
10. **Admin-pasted SQL never leaves this client.** The relation-discovery
   "Analyze SQL" box (`relation_discovery.py`) parses pasted SELECT
   statements in memory only: the SQL TEXT is never persisted, logged,
   audited, or included in any `brain_client` payload, and sqlglot error
   messages — which embed the offending SQL — never leave the parser
   (exception TYPES only may be logged). Audit rows carry counts only.
   Table/column IDENTIFIERS and statement counts extracted from the SQL
   MAY persist locally (the "Recommended tables" evidence in
   `data_sources.json`) — literals never survive extraction because only
   Column = Column predicates are read at all, and the SQL-box UI states
   this truthfully. Snapshot verification emits aggregates only
   (uniqueness, overlap %, orphan counts) — never cell values.

---

## Article VIII: Testing Before Release

**Both containers must boot, health-check, and complete one real
upload→chart→edit→report cycle locally before any release candidate.**

```bash
# Local stack (default ports: brain 8090, client 8091)
cd enterprise
docker compose up --build -d brain
# 1) open http://localhost:8090/admin/login → create tenant → copy token
# 2) put token in enterprise/.env as BRAIN_TENANT_TOKEN=<token>
docker compose up -d client
open http://localhost:8091           # email + password login → /lab
```

### Required checks before release
1. Brain `GET /health` returns 200.
2. Client `GET /health` returns 200.
3. Tenant token is recognized; revoking it returns 403 on every `/v1/*` call.
4. Drag-drop a small CSV → schema autofill populates per-column descriptions.
5. Send a chat question → chart renders → refresh page → chart persists.
6. Download PDF and PPTX from a conversation with ≥2 findings.
7. Trigger Auto Analytics → deck file appears under
   `chatdata/{id}/auto_analysis.pptx`.

If UI behavior cannot be verified in a browser, say so explicitly. Type
checks and unit tests are not sufficient evidence for a feature being done.

---

## Article IX: Deployment

**Two independent deploys.**

- **Brain** → PowerDataChat's enterprise GCP project (separate from the B2C
  project). The brain is a single multi-tenant service. See
  [`docs/DEPLOY.md`](DEPLOY.md).
- **Client** → built as a self-contained Docker image, handed to each
  customer for installation inside their own network. Customers run it
  themselves. The brain URL + per-tenant token are configured via
  environment variables at install time.

The two containers are NEVER deployed together: that would defeat the split.

---

## Article X: Documentation

**Keep documentation current and minimal.**

### Required docs
- [`docs/ENTERPRISE_ARCHITECTURE.md`](ENTERPRISE_ARCHITECTURE.md) — decisions, invariants, what is OPEN.
- [`docs/PROTOCOL.md`](PROTOCOL.md) — the brain `/v1/*` request/response shapes.
- [`docs/CLIENT_ENDPOINTS.md`](CLIENT_ENDPOINTS.md) — the client HTTP surface and dashboard contract.
- [`docs/BUILD_AND_RUN.md`](BUILD_AND_RUN.md) — build + run + onboard + revoke.
- [`docs/DEPLOY.md`](DEPLOY.md) — brain GCP deploy.

### Forbidden
- Duplicate documentation (one home per topic).
- Outdated docs (delete or archive — don't leave contradictions).
- README per directory.
- Comments explaining obvious code.

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
- [ ] Local smoke (brain + client) still passes.
- [ ] No `.env` / token / credential staged.
- [ ] No raw-data values leak to brain logs.
- [ ] Doc updates committed with the code change.
- [ ] No unrelated changes bundled.

---

## Article XIII: Standard Dtypes at the Execution Boundary

**Dataframes handed to the execution sandbox must behave like plain,
standard pandas: strings as object, dates as datetime64, ordinary
numerics.**

- No category, sparse, or extension dtypes (tz-aware `datetime64` is the
  one allowed extension dtype — it behaves like plain datetime and
  stripping it would drop the timezone) — no optimization that is
  observable by generated code in any way. Generated code cannot be
  trusted to handle them (a categorical dimension column once made
  `groupby` — pandas < 3.0 defaults to `observed=False` — emit the full
  cartesian product of ALL categories, putting every category on a chart
  axis).
- Performance optimizations are permitted only in storage/caching layers
  where generated code can never observe them (e.g. numeric downcasts
  baked into snapshot parquet files are fine; serving a categorical to
  the sandbox is not).
- Enforcement is in code, not in memory: the pre-execution sanitize gate
  (`exec_sanitizer.sanitize_for_execution` on the client) runs inside
  BOTH exec sites — `code_exec.safe_execute` and
  `plot_utils.render_plot_safe`, the same pair that installs
  `sandbox_guard.SANDBOX_BUILTINS` — so every execution path passes
  through it. Do not add a third exec site without installing the gate.
- The gate never mutates the caller's frames (they are shared across
  worklists and threads) and never raises (Article IV): a column that
  cannot be converted is logged and passed through unchanged.

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
| Data boundary| Raw values never cross to brain. `_safe_preview` is the guard.            |
| LLM calls    | REST API only, no LangChain. Tier via `_eff()`.                            |
| Storage      | Local filesystem. JSONL for history, JSON for metadata. No GCSPath.       |
| Errors       | Catch → log with sid → return fallback. Never crash silently.             |
| Resources    | `atexit` for executors; close HTTP clients on lifespan shutdown.          |
| Security     | Secrets in env only. Never commit `.env`. Brain holds the Gemini key.     |
| Deploy       | Brain → enterprise GCP. Client → customer LAN. Two independent images.    |
| Docs         | Five required docs under `docs/`. Fix contradictions in the same PR.      |
| Exec dtypes  | Sandbox sees standard dtypes only. `sanitize_for_execution` is the gate.  |
