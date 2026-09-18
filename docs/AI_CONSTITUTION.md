# PowerDataChat Enterprise — AI Constitution

> Engineering standards and coding rules for AI agents working on the
> enterprise (on-prem) edition. Adapted from the original B2C constitution
> for the brain/client split.

**Version:** 1.2 (enterprise)
**Last Updated:** 2026-09-17

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
- **The log file is newline-delimited, so any text you did not write must be
  escaped before it reaches a line.** Pass it through
  `exec_transport.log_safe_text`, which escapes CR/LF and caps the length.
  This applies to the message AND to every context value AND to the `sid`
  field itself, and it applies to text from any origin you do not control:
  the request body (a question, a conversation id from a path segment), a
  customer's data (a column label, a cell value), an executor response, a
  brain response, and — the case most often missed — a LIBRARY's own
  exception, because a library quotes the value it choked on, so a poisoned
  column name arrives wrapped in what reads like pandas's own words.
  Otherwise one embedded newline forges a complete, plausible, backdated
  record in the file an operator reads to reconstruct what happened. This
  rule exists because that defect was found open at eleven sites across four
  modules over four review rounds, every time by someone reading the code
  rather than by a test failing.
- **A value never goes on a log line even escaped.** An exception message
  that embeds a cell value (openpyxl's illegal-character error is the known
  one) must be reduced to its type plus a fixed, value-free reason before it
  is logged. Article II governs what leaves the container; this governs what
  lands on disk inside it.

---

## Article V: Local-Filesystem Storage (no GCS adapter on-prem)

**On-prem runs against a local filesystem only.**

- Brain storage root: `BRAIN_STORAGE_ROOT` (default `/data/brain`).
- Client storage root: `DATA_ROOT` (default `/data/client`).
- Both default to bind-mounted Docker volumes (`pdc_brain_data`, `pdc_client_data`).

There is no `GCSPath` and no GCS storage adapter: the local filesystem is
the default and the ONLY mode for customer installs. One optional, transient
transport exists on top of it — the direct-to-GCS large-file upload path
(`gcs_upload.py`, `POST /upload/init` + `POST /upload/finalize`), a port of the
B2C flow used solely by the PowerDataChat-hosted Cloud Run demo, whose ingress
caps HTTP/1 request bodies at 32 MiB. It is INERT unless `GCS_UPLOAD_BUCKET`
is set: with the variable empty (every customer install) both endpoints
return 400 and the frontend sends every file through multipart `POST /upload`.
When set, the browser PUTs the file to the bucket with a V4 signed URL (signed
through the runtime service account's IAM `signBlob` — no key file, Art. VII),
`/upload/finalize` pulls it into the per-session store under `DATA_ROOT` and
deletes the object; the bucket never holds data beyond that hop.
`POST /upload_from_url` returns 400 regardless — see
[`docs/CLIENT_ENDPOINTS.md`](CLIENT_ENDPOINTS.md).

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
| client | SSO configuration     | `sso_config.json` (Microsoft Entra ID; client secret Fernet-encrypted) |
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
5. User-supplied Python is executed only via
   `client/code_exec.safe_execute()` / `plot_utils.render_plot_safe()`, which
   dispatch it to the `pdc-executor` sandbox container. `exec()` exists ONLY
   inside the in-process functions that container's runner imports
   (`code_exec._execute_in_process`, `plot_utils._render_in_process`); the web
   process never executes generated code and never falls back to doing so.
   The sandbox holds no secret, no database driver and no customer data, and
   reaches no network but the internal one it shares with the web service —
   see Article XIV, which owns this boundary in full.
6. Never commit `.env` files. Commit `.env.example` templates only.
7. SMTP credentials live on the brain (per-tenant config). The client relays
   share emails through `POST /v1/send_share_email`.
8. **Database credentials (client "Data sources")** are Fernet-encrypted at
   rest (`CLIENT_ENCRYPTION_KEY` env var; no key → the feature refuses to
   save, NEVER a plaintext fallback), masked in every API response, never
   logged, never included in any `brain_client` payload, and only ever
   decrypted into function-locals at the moment of use — never held in
   cleartext at importable module scope. The Microsoft SSO client secret
   (`sso_config.json`, `sso_store.py`) follows the same rule in full — and
   the ID/access tokens from the OIDC flow are never logged either.
9. **The code-exec sandbox never gets DB access.** Both exec sites
   (`code_exec._execute_in_process`, `plot_utils._render_in_process`, which
   run inside the sandbox container) install
   `sandbox_guard.SANDBOX_BUILTINS`, whose `__import__` DENIES SQLAlchemy,
   every DB driver (psycopg2, pymysql, pyodbc, oracledb, sqlite3, …) and this
   client's credential modules (`db_connector`, `db_sources`, `db_scheduler`,
   `local_store`, `settings`, `brain_client`, `password_utils`). A denylist,
   deliberately not an allowlist — plotting stacks lazy-import transitively at
   call time, and an allowlist miss would fail-freeze historical stored code.
   **Honest limit:** the denylist is defense in depth, NOT the boundary
   (`open`/`eval` remain; already-imported modules stay reachable). The
   boundary is the sandbox container itself — a different unprivileged uid, a
   read-only rootfs, no credentials in its environment, and no database
   driver, HTTP client or credential module in its image at all, so the
   denylist now guards what is mostly absent anyway. Alongside it: the
   dedicated SELECT-only database login the customer provisions (the grant is
   the real guarantee), plus rules 8 above
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
  BOTH exec sites — `code_exec._execute_in_process` and
  `plot_utils._render_in_process`, the same pair that installs
  `sandbox_guard.SANDBOX_BUILTINS` — so every execution path passes
  through it. Both live in the sandbox container, and the frames reach it as
  parquet written per job, so the gate runs on what the sandbox actually
  loads. Do not add a third exec site without installing the gate.
- The gate never mutates the caller's frames (they are shared across
  worklists and threads) and never raises (Article IV): a column that
  cannot be converted is logged and passed through unchanged.

---

## Article XIV: The Executor Boundary

**Generated Python runs in the `pdc-executor` container and nowhere else.
The container IS the boundary; everything else is defence on top of it.**

### The five properties that make it a boundary
1. **A different unprivileged identity.** The web service is uid 10001, the
   sandbox uid 10002, sharing one group only so they can exchange files in
   the job directory. Neither is ever root.
2. **No secrets.** No brain token, no session key, no encryption key, no
   database credential. The service REFUSES TO START if any of them is in
   its environment, and the runner's environment is an allowlist built from
   scratch, so even a container wrongly handed the web service's
   environment passes none of it to generated code.
3. **No database access of any kind.** SQLAlchemy, every NETWORK driver, and
   this client's credential-bearing modules are absent from the image.
   `sqlite3` is the exception worth naming: it ships with Python, so only the
   import denylist covers it — and a local database file is not a route to
   the customer's data, which is what this property is about.
   `sandbox_guard`'s import denylist is defence in depth on top of that
   absence, never the guarantee — `open` and `eval` remain reachable by
   design, and the real guarantees are the missing image contents and the
   SELECT-only database login the customer provisions.
4. **No route off its own network.** The sandbox joins ONE Docker network
   declared `internal: true` and publishes no port: no gateway, so no path
   to the LAN, to a database host, or to the internet. `POST /execute` is
   unauthenticated by decision, and **the network is what protects it** — a
   shared secret would put a secret into the one container defined by
   holding none. Any deployment that publishes that port or attaches the
   sandbox to a routable network has broken this article, not merely
   weakened it.
5. **No customer data mounted.** Nothing under `DATA_ROOT` is mounted into
   the sandbox — not uploads, chats, snapshots, the credential store or the
   log. The only shared storage is the job directory, and each job's input
   frames are written into it per job rather than mounted wholesale, which is
   what stops generated code reading a table the requesting user's role does
   not grant.
   **What that does NOT give you, measured:** a job can leave data for the
   next one, in TWO places. The container's `/tmp` is one mode-1777 directory
   that only a restart clears. The jobs volume's ROOT is group-writable —
   it has to be, because the sandbox deletes its own finished job
   directories — so code can also write straight into it, and that volume is
   on disk and survives restarts and image upgrades. Both were verified by
   running two separate jobs and reading the first one's file back from the
   second. The sweeps on both sides remove aged entries of the jobs root
   that are not job directories, so a stash there lasts about an hour rather
   than forever; nothing bounds the scratch directory at all.

   Be exact about what that bound rests on, because the obvious reasoning is
   wrong and was MEASURED to be wrong. The web sweep deliberately skips what
   it OWNS — that is what stands between a misconfigured
   `EXECUTOR_SHARED_DIR` and the customer's own state, since everything
   under `DATA_ROOT` belongs to the web identity. Ownership is therefore
   useless as a test of who WROTE something: the root must be
   group-writable so the sandbox can clear an abandoned job directory, which
   also lets it RENAME one, and a rename preserves the owner. Generated code
   can hand itself a web-owned entry instead of creating one — verified by
   renaming its own live job directory, which left a writable directory
   holding that question's input frames that BOTH sweeps then skipped, each
   believing the other owned it. The sandbox sweep therefore removes every
   aged non-job entry regardless of owner: it has no customer data mounted,
   so the protection ownership buys on the web side buys nothing there,
   while the gap it left was the whole problem.

   So the per-job frame write bounds what a job is GIVEN, not what a job can
   leave behind, and the role-permission sentence above is true of the input
   path only. Closing it properly is a design decision of the same kind as
   the concurrency limit: a private scratch namespace per job, or a distinct
   identity per job. A redirected temporary directory would not do it,
   because code can name an absolute path, and the jobs root cannot simply be
   made unwritable without taking the sandbox's own cleanup with it.

### Rules
- `exec()` may exist ONLY on the two code paths the sandbox's runner enters
  — `plot_utils._render_in_process`, which calls it directly, and
  `code_exec._execute_in_process`, which reaches it through its private
  `_execute_code_in_env`. Be exact about the second one: the call is in the
  CALLEE, and the structural test pins the function that contains the call,
  not the function the runner imports. The public entry points dispatch over
  HTTP and must never fall back to executing locally on any error path. A
  structural test pins both the call sites and the absence of a fallback.
- The dispatcher never raises: every failure is the caller's own error
  shape. Its own five failure texts carry no URL, no path and no datum —
  the detail stays in the local log — because a text the chat loops hand to
  the brain must say what happened without describing this installation.
  The infrastructure predicate now stops most of them reaching the brain at
  all, which narrows the exposure but is not the reason for the rule: a
  string that names the storage layout is worth avoiding wherever it goes.
- **Everything in an executor response is untrusted input**, including the
  fields that look like protocol metadata. The response is read under a
  mount where customer data IS present, so references are resolved through
  a no-follow chain, and pickle is never read inbound. Every field that
  DESCRIBES the outcome is constrained before it reaches a prompt or a log —
  an enumerated vocabulary where one exists (`status`, `reason`), a bounded
  grammar where the value is the sandbox's own token (`code`), a numeric
  bound where it is a number (`exit_code`, `signal`, the timing fields).
  None of those may arrive as free text.

  The one field that IS free text is the exception message, and it is free
  text on purpose: the planner has to read the real error to rewrite the
  code, so it crosses verbatim under a length cap and nothing else. That
  makes it the field to be careful with rather than the exception to the
  rule: **every site that writes it to a log must escape it first**, because
  the log is newline-delimited and a message the sandbox chose could
  otherwise forge a whole record in the file an operator reads to
  reconstruct what happened. That is an obligation on every writer, not a
  property of the text — it was found open at eight sites in two rounds, in
  three different modules, each time by looking again rather than by a test
  failing. The same obligation covers any string DERIVED from the response
  or from a customer frame: a library's own exception quotes the value it
  choked on, so a poisoned field arrives inside an error message that looks
  like the library's.

  The PLOT shape carries a second free-text field, `trace`. No product code
  reads it today, which is the only reason it is not a third paragraph here.

  Any new field follows the first paragraph; any new use of the message, or
  of anything derived from the response, follows the second.
- Because a Docker network is bidirectional, the web application refuses
  any request whose peer address falls inside the sandbox's subnet. A
  reverse proxy must never be trusted to rewrite that address from a header
  for arbitrary peers.
- SQL never executes in the sandbox. Database results reach it only as
  frames the web service already fetched and gated.
- One job at a time per sandbox. The limit is part of the design, not a
  throughput knob: properties 1 and 5 hold BETWEEN the two containers at any
  setting, but they only hold between two JOBS while there is one. With two
  in flight they share a single uid, so one job can read another's input
  frames during its load window, plant a symlink where its result will be
  written, or signal its process — and writing frames per job stops being a
  per-job guarantee. Concurrency above one is defensible only with a separate
  identity per job, which this service does not do.

**Honest limits, stated because a boundary described better than it is
becomes a liability:** generated code can still consume CPU and memory up to
the container's limits, can read anything the image itself contains, and can
write into `/tmp`, into its own job directory AND into the jobs volume's
root — the last two both shared, as property 5 records, so they are channels
between consecutive jobs rather than private space, the volume one bounded
to about an hour by the sweeps and the scratch directory not bounded at all.
A job can also RENAME an entry in that root, which is why the sandbox sweep
cannot use ownership to decide what to remove. What it cannot do is
reach the customer's network, its data at rest, or its credentials:
everything it is handed was selected for the job it is running.

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
| Storage      | Local filesystem. JSONL for history, JSON for metadata. No GCSPath. Direct-to-GCS upload hop only with `GCS_UPLOAD_BUCKET` (demo). |
| Errors       | Catch → log with sid → return fallback. Never crash silently.             |
| Logging      | Escape any text you did not write (`log_safe_text`) — message, context values and the sid. Never log a data value at all. |
| Resources    | `atexit` for executors; close HTTP clients on lifespan shutdown.          |
| Security     | Secrets in env only. Never commit `.env`. Brain holds the Gemini key.     |
| Deploy       | Brain → enterprise GCP. Client → customer LAN. Two independent images.    |
| Docs         | Five required docs under `docs/`. Fix contradictions in the same PR.      |
| Exec dtypes  | Sandbox sees standard dtypes only. `sanitize_for_execution` is the gate.  |
| Exec location| Generated Python runs in `pdc-executor`, never in the web process. `exec()` only on the two paths its runner enters (one of them a private callee). |
| Exec boundary| Art. XIV: the sandbox container is the boundary — separate uid, no secrets, no DB, no network route, no customer data mounted. Its responses are untrusted input. |
