# PowerDataChat Enterprise — Client — Claude Code Instructions

The client half of the PowerDataChat enterprise (on-prem) edition.
Runs inside the customer's LAN. Holds raw data + execution + rendering
+ the `/lab` UI. Talks to a multi-tenant brain (a separate, hosted
service operated by PowerDataChat) over HTTPS with a per-tenant
bearer token. **Raw data values never leave this container.**

## Required reading before ANY code change

You MUST read these before writing or modifying code:

1. [`docs/AI_CONSTITUTION.md`](docs/AI_CONSTITUTION.md) — client engineering rules + the data boundary.
2. [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — the brain `/v1/*` API this client calls.
3. [`docs/CLIENT_ENDPOINTS.md`](docs/CLIENT_ENDPOINTS.md) — this client's HTTP surface (`/lab` contract).

The client overview, the data boundary, and the invariants live in
[`docs/ENTERPRISE_ARCHITECTURE.md`](docs/ENTERPRISE_ARCHITECTURE.md). Read it
before changing any behavior that touches the brain↔client boundary.

## Critical rules summary

### The data boundary (Article II of the constitution)
Raw data values must never cross from this client to the brain. The
summarizer's `_safe_preview`
([`run_chat_local.py`](run_chat_local.py)) is the hard guard — only
scalars pass; dicts/lists/DataFrames become `None`. Do not weaken it
or bypass it.

### Error handling (Article IV)
Every `try/except` must:
1. Log with `log_with_sid(sid, level, message, ...)`.
2. Return a safe fallback value.
3. Never crash silently.

### Local filesystem only (Article V)
On-prem runs against local disk (`DATA_ROOT`, default `/data/client`).
There is no `GCSPath`. Direct-to-GCS upload endpoints (`/upload/init`,
`/upload/finalize`, `/upload_from_url`) return 400 by design.

### No tokens, no API keys in this repo (Article VII)
- This client never holds the Gemini key — the brain does.
- The `BRAIN_TENANT_TOKEN` is injected at install time via an env var
  and identifies this tenant to the brain. Never commit it.
- `SECRET_KEY` is local session-cookie signing only. Generate fresh
  per install.
- Never commit `.env`. Commit `.env.example` templates only.

## Repository layout

```
PDC_Client/
├── app.py                   # FastAPI app + lifespan
├── brain_client.py          # HTTP client to brain (bearer token)
├── run_chat_local.py        # local execution + _safe_preview guard
├── local_store.py           # users + chats + conversations on local disk
├── password_utils.py        # stdlib PBKDF2 password hashing (werkzeug-compatible format)
├── schema_builder.py        # _schema_text builder (memoized, 300s TTL)
├── excel_table_detector.py  # 6-stage Excel table detection
├── auto_analytics.py        # background job (brain planner → local exec → PPTX)
├── code_exec.py             # safe_execute
├── plot_utils.py            # render_plot_safe
├── pptx_template_cache.py   # local cache of the brain-served tenant template/spec
├── settings.py
├── models.py
├── logger_utils.py
├── routes/
│   ├── auth.py              # /auth/* — email+password login, reset, change
│   ├── upload.py            # /upload, /schema_autofill_full, /generate_chatdata
│   ├── schema.py            # /schema_details, /schema_common_fields, /schema
│   ├── chat.py              # /api/chat/* — SSE stream, edit-regenerate, sharing
│   ├── dashboards.py        # /api/dashboards/* — pinned-tile dashboards + sharing
│   └── report.py            # /download_report (PDF), /download_pptx
├── templates/
│   ├── dashboard.html       # /lab page
│   ├── dashboard_view.html  # /dashboards/{id} page (pinned-tile grid)
│   ├── auth_landing.html    # email + password + remember-me sign-in
│   ├── change_password.html # forced new-password page (temp-password logins)
│   └── partials/
├── static/                  # JS + CSS + images (+ vendor/gridstack — offline GridStack 10.3.1 for the dashboard grid)
├── Dockerfile
├── docker-compose.yml       # CUSTOMER install (image-only, client.env)
├── docker-compose.local.yml # LOCAL testing stack (build ., persistent pdc_* volumes)
├── tests/                   # pytest (offline — brain calls stubbed)
├── tools/                   # dev tools + fixtures (sample_sales.csv, wide_data.csv)
└── docs/                    # constitution, protocol, endpoints, build & run
```

## File reference

| File | Purpose |
|---|---|
| [`routes/chat.py`](routes/chat.py) | Chat SSE stream, multi-chart accumulation + persistence, edit-regenerate, sharing, full_table, conversation title generation, Auto Analytics endpoints; MULTI-TABLE answers persist a `tables` array + aligned `full_table_keys` on the AI record (single-shot and mixed dashboard turns); each durable full-table record may carry `result_key` — the dict entry of the re-executed RESULT it came from — so per-table Show-full-table / Download Excel re-execute correctly (`_persist_full_table` / `_reexecute_full_df`); chart-PNG + table-Excel download routes (`export_plotly_png`, `download_excel/{key}`, `export_excel`); `refresh_item` — per-chart/per-table refresh (re-runs ONE item's stored code against the current dataframes, purely local, no brain call; execution failures → `{ok:false,error}` so the UI keeps the previous render); `file_fingerprints` — `{source_filename: {size_bytes, sha256}}` of the chat's files for the Add Data name-collision check (client↔client only, hashing in the executor, per-file failure → name-only entry); `probe_columns` — header-level structure comparison of an uploaded file vs the chat's same-named file for the collision dialog (openpyxl read_only / csv header, first 50 rows cap, no detection pipeline; `{ok, match, uploaded, existing}`, any failure → `{ok:false}`; cell values never logged) |
| [`plot_utils.py`](plot_utils.py) | `render_plot_safe` + `_plotly_to_html`; trims the non-functional Plotly modebar tools (`toImage`, `sendDataToCloud`, `select2d`, `lasso2d`) and widens the discrete color palette so >10-category charts never repeat a hue (`_widen_discrete_colors`; continuous/2nd-measure scales untouched) |
| [`static/dashboard.js`](static/dashboard.js) | Served `/lab` UI; on the constant Enterprise plan it hides the B2C subscription plan-cards so the Paddle upsell (no billing backend on-prem) is unreachable. Profile dropdown carries exactly two items — Change Password (small modal → `POST /auth/password`) and Logout; the B2C Profile/Subscriptions dropdown items don't exist here (bindings are optional-chained). "Add Data" topbar button (same visibility as View / Edit Descriptions) reopens the Create-New modal in 'add' mode (`wizardMode`) — same upload+autofill flow, final step `POST /add_data_to_chat`. Per-chart/per-table refresh buttons (`_wireRefreshButtons`) POST the item's stored code to `refresh_item` and swap the render in place (charts via the plotly container's `_setChartHtml` hook / `img.src`; tables via a rebuilt `.pdc-table-block` that carries the Show data/Show code buttons over); no stored single-block code → no button. REFRESH FREEZING: a button whose code references a df key (`dfs['…']` regex, keys only) missing from the chat's current `/schema` keys renders DISABLED (greyed) with an i18n tooltip — applied on render, on history reload, and re-applied after Add Data (`_applyKeyFreeze`/`_reapplyRefreshFreezes`); a runtime refresh failure FAIL-FREEZES that button for the session (`_showRefreshError`). ADD DATA NAME-COLLISION flow: `addFilesToSelection` resolves collisions BEFORE upload — vs the target chat via `GET file_fingerprints` (size then browser-side `crypto.subtle` SHA-256 on tie; identical → "already in this chat" notice; different → `#fileCollisionModal` with Overwrite (primary button) or Upload-as-`_vN` (via the FormData filename override `_pdcUploadName`); the dialog shows the generic structure warning immediately and a non-blocking ~3s background probe (`POST probe_columns`) may replace it with a specific same-columns info line or different-columns warning — probe failure/timeout keeps the generic text, choosing early ignores the probe) and vs the same selection batch (identical → "Duplicate file ignored"; different → auto-rename `_vN`) — shared helpers `_suggestUniqueName`/`_fileSha256Hex`; NO silent overwrite path remains. MULTI-TABLE answers: `appendMessage` renders `extras.tables` as one titled `.pdc-table-block` per table (per-table full keys aligned by index), live done-handler / history reload / edit-regenerate all pass `tables`+`full_table_keys` through; mixed dashboard turns append the tables after the streamed charts |
| [`routes/upload.py`](routes/upload.py) | `/new_session`, `/upload`, `/schema_autofill_full`, `/generate_chatdata`, `/add_data_to_chat` (Add Data to an EXISTING chat — same upload+autofill pipeline, then merge into the ChatDataStore; a same-named upload OVERWRITES the file — always user-confirmed upstream by the frontend collision dialog, never silent — and its meta entries are RE-SYNCED via `_resync_meta_after_add`: vanished df keys deleted (stale-entry fix), surviving keys rebuilt from fresh autofill with old file/column descriptions + `values` maps carried over, new keys appended; resync failure logs `ADD_DATA_RESYNC_FAILED` and falls back to the old append-merge; response `{added, updated, removed, files}`), GCS-path stubs (400) |
| [`routes/auth.py`](routes/auth.py) | Email+password login (a genuinely NEW email sets the entered password + brain-relayed welcome mail; a LEGACY email-only account is refused with a "set your password via reset" notice — mailbox ownership is proven through the reset flow, never a silent adopt), reset flow (client-generated temp password, hash + `must_change_password` stored locally, mailed via the brain), forced password change, `/auth/password` change endpoint, remember-me sessions, profile (constant "Enterprise" plan), sidebar listings, rename/delete, conversation-level share |
| [`password_utils.py`](password_utils.py) | `generate_password_hash` / `check_password_hash` — stdlib PBKDF2-HMAC-SHA256 in werkzeug's `pbkdf2:sha256:iter$salt$hex` format (werkzeug is not a dependency of this container). Only hashes are stored (`users/{email}/auth.json`) |
| [`routes/report.py`](routes/report.py) | PDF (ReportLab + DejaVu) and PPTX (python-pptx) report rendering — local only |
| [`routes/dashboards.py`](routes/dashboards.py) | `/api/dashboards/*` — per-user dashboards of pinned chart/table tiles. CRUD + tile pin/remove/layout + per-tile refresh (reuses `routes.chat.run_item_refresh` — purely local re-execution, no brain call; deleted source chat → persisted `frozen`, execution failure → `200 {ok:false}` keeping the stored snapshot) + owner-only sharing that mirrors chat sharing (recipient pointer rows + source-chat access grant; brain SMTP relay gets name+comment only, Article II). Storage: `local_store.DashboardStore` under `users/{email}/dashboards/` (atomic writes, 16-hex regex-guarded ids, old-shape docs load with defaults). Page route `GET /dashboards/{dash_id}` in `app.py` renders `dashboard_view.html` |
| [`static/dashboard_view.js`](static/dashboard_view.js) | The `/dashboards/{id}` page: renders tiles from stored snapshots (Plotly iframe / PNG / table — adapted copies of the dashboard.js renderers, parameterized by `tile.chat_id`), GridStack grid (drag by grab strip, resize, non-overlap, debounced layout POST, 1-col read-only under 768px, `pdc-grid-moving` disables iframe pointer-events during drag), tile toolbar (description popover w/ backdrop dismiss, Show data/code via PDCViewers, Download, View larger, Refresh, Remove), Refresh-all (concurrency-2), rename/share/delete, read-only mode for shared recipients |
| [`auto_analytics.py`](auto_analytics.py) | Auto Analytics background job (planner via brain → execute locally → render PPTX) |
| [`run_chat_local.py`](run_chat_local.py) | `run_chat` / `run_chat_multi_plot`; `_safe_preview` data-boundary guard (strict scalar allow-list — dict values must be plain scalars). MULTI-TABLE answers: a RESULT that is a dict of DataFrames/Series/Stylers becomes a `tables` list (`_build_tables_from_result`, dict key = table title; single-entry dicts unwrap to the plain `table`); mixed dashboard plans (python RESULT block + ###NEXT_PLOT### chart blocks) pull the TABLE blocks out of the chart worklist (`_looks_like_table_block`) and carry them on the done event as `tables`/`table_codes`/`table_result_keys` instead of dying in the chart renderer |
| [`local_store.py`](local_store.py) | `AuthStore`, `UserStore`, `ChatDataStore`, `DashboardStore` (per-user dashboards: `users/{email}/dashboards/index.json` + one doc per dashboard incl. tile snapshots; atomic tmp+`os.replace` writes under `_LOCK`; own rows + `shared_by` pointer rows; `resolve_dashboard` own-or-shared access) on local disk; `append_history` / `truncate_conv_history` (every history write goes through `_json_safe` — the B2C normalizer: Timestamp→ISO, NaT/NaN→None, numpy→native, catch-all str() — so a result table with dates or a stray pandas object can never fail persistence; `routes/chat.py` reuses the same `_json_safe` for SSE payloads); TWO-LAYER dataframe caching: (1) in-memory `_DATAFRAME_CACHE` (`LRUTTLCache`: 300s TTL with active sweep + sweeper thread, `DF_CACHE_MAX_MB` byte budget with LRU eviction, oversized datasets served-not-cached; every hit signature-validated against source `(name, mtime_ns, size)` so overwrites/refreshes can never serve stale data); (2) self-healing on-disk parquet cache (`_load_one_file_cached` → `<files_dir>/.parquet_cache/`, manifest keyed on source size+mtime_ns AND `parser_version` — bump `_PARQUET_CACHE_PARSER_VERSION` whenever the parse pipeline changes cached shapes so old caches re-parse instead of serving stale column semantics; atomic writes, round-trip-verified, PICKLE fallback entry for dataframes parquet can't reproduce — e.g. mixed numeric/string object columns — so one bad column no longer aborts the whole file's cache; falls back to the detection pipeline on any failure; Add Data's in-place overwrite invalidates via the stat change); `clone_from_user_store` also copies `.parquet_cache` (mtime-preserving) so a new chat's first question skips the parse |
| [`brain_client.py`](brain_client.py) | HTTP wrappers for every `/v1/*` call (with `BRAIN_TENANT_TOKEN` bearer auth); `_sanitize_history_rows` strips history to role/content(+code) inside `plan`/`retry`/`summarize` so persisted `image_base64`/`chart_data`/`table`/`usage` never reach the brain (Article II) |
| [`excel_table_detector.py`](excel_table_detector.py) | Streaming Excel table detection (port of global's calamine rewrite): header anchor/span detected on a top slice (`HEADER_SCAN_ROWS`, 50+margin) streamed from ONE shared read-only openpyxl workbook (also used by the hidden-sheet probe), body read from ONE shared calamine `pd.ExcelFile` per workbook — multi-sheet files are parsed once, not once per sheet; column names are ALWAYS str()-normalized and DEDUPED (pandas "name.1" convention — datetime headers and repeated header names otherwise crash meta writes / schema_text), label_cols/totals run on the loaded df — no full openpyxl object-graph load (the old 3-pass path took ~250s on a 9MB workbook; this takes ~15s). ListObject precheck + merged-cell S1 span signal intentionally dropped (need non-read-only loads; validated unnecessary on global). `load_excel_sheets` processes only VISIBLE sheets (hidden/veryHidden skipped in the probe step, before the single-vs-multi keying — the one choke point for every load path) |

## Documentation

| Document | Purpose |
|---|---|
| [`docs/AI_CONSTITUTION.md`](docs/AI_CONSTITUTION.md) | Coding rules (12 articles, enterprise-specific) |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | Brain `/v1/*` request/response shapes (what this client calls) |
| [`docs/CLIENT_ENDPOINTS.md`](docs/CLIENT_ENDPOINTS.md) | This client's HTTP surface (dashboard contract) |
| [`docs/BUILD_AND_RUN.md`](docs/BUILD_AND_RUN.md) | Build, run, configure, logs |

## Before committing

1. **Smoke locally — ALWAYS via the Docker stack on the persistent data**
   (`docker compose -f docker-compose.local.yml up -d --build` here :8091,
   and the brain from the PDC_Brain repo :8090). The stack runs on the
   external `pdc_*` volumes so real users / chats / history are exercised
   like a production upgrade. Never ad-hoc native runs, never
   `docker compose down -v`; back up the volumes before a rebuild. See
   [`docs/BUILD_AND_RUN.md`](docs/BUILD_AND_RUN.md) §3. Then the full
   upload→chart→edit→report cycle in the browser.
2. **`GET /health`** returns 200 (`brain_reachable: true`,
   `tenant_token_configured: true`).
3. **No `.env` staged** — check `git status` and `git diff --cached`
   for secrets. `.env.example` is OK.
4. **No raw values leaving** — grep your diff for places where
   DataFrames, full tables, or row dicts might cross into a
   `brain_client.*` call.
5. Follow commit format from Article XII.

## Deployment

- **Client** (this repo) → packaged as a Docker image, installed by
  each customer in their own network. Configured via `BRAIN_URL` +
  `BRAIN_TENANT_TOKEN` environment variables at install time.
- **Brain** → a separate, hosted service operated by PowerDataChat,
  not run or deployed by the customer.

The two are NEVER deployed together. That would defeat the split.

One intentional exception exists: PowerDataChat hosts a single internal
demo/showcase instance of this client on Cloud Run (`pdcclient-demo`, demo
tenant, demo data only — no customer data). See `docs/DEMO_CLOUD_RUN.md`.
It is NOT a customer topology.

## Differences from the B2C edition (intentional)

- **Local email+password auth** — no Google OAuth, no B2C registration.
  The password is set on first login for NEW accounts (hash-only, local
  disk); legacy email-only accounts must set theirs through the reset flow.
  The brain is involved only as a Gmail relay for welcome / password-reset mails
  (`/v1/send_welcome_email`, `/v1/send_password_reset_email`).
- **No subscriptions / quotas** — `/auth/subscription` returns
  constant `{"plan": "Enterprise"}`. No daily message caps. No Paddle.
- **No public publishing** — `POST /publish`/`/unpublish` return 400.
  Sharing is recipient-list only.
- **No GCS adapter** — local filesystem only. Direct-to-GCS upload
  endpoints return 400.
- **No Soro blog, Vlog, B2C marketing site, customer-analytics
  reporter** — those modules don't exist on-prem.

## Data safety — no update may ever lose state

Persistent state is strictly separated from deployable code. **Code is
replaceable; state is not.**

- Client state = everything under `DATA_ROOT` (`/data/client`): locally the
  external `pdc_client_data` volume, in production the customer's own volume.
  Users + auth hashes, uploaded raw files, chats, conversation history,
  rendered decks, the parquet cache.
- No script, test, migration, or deploy step may delete or overwrite it.
  Tests run against `tmp_path` / a monkeypatched `DATA_ROOT` only — never
  `./client_data`, never the volume.
- Stored-format changes (`users/{email}/*`, `chatdata/{chat_id}/meta.json`,
  `conversations/*.jsonl`, `.parquet_cache` manifests) must stay **backward
  compatible** — data written by the previous release must still load after
  an upgrade (customers upgrade the image against their existing volume).
  Add a regression test with an old-shape fixture whenever a stored shape
  changes.
- Upgrades are image-only: `docker compose up -d` with a new image against the
  same volume. See `/release-image`.
- The local volume holds realistic test state — it survives rebuilds,
  reinstalls, and test runs. Back it up before any rebuild (`/smoke-test`
  step 1). **Never `docker compose down -v`.**

## Agents & skills — when to use what

Agents (`.claude/agents/`):

- **explorer** (read-only, fast) — find code, map ALL call sites before
  touching a shared helper, summarize a module.
- **reviewer** — run on every non-trivial diff BEFORE commit; checks the
  data boundary, constitution articles, protocol/doc mirroring, data safety.
- **debugger** — stack won't boot, SSE/chart/report failures, brain 4xx;
  reads `docker logs pdc-client` / `logs/datachat.log`, proposes a fix.
- **test-runner** — runs pytest; may fix test-side bugs and env issues, never
  weakens assertions, reports product bugs without fixing.
- **ui-tester** — Playwright against `http://localhost:8091/lab` only, with
  `tools/fixtures/` data and test accounts only; reports root cause with
  file:line (dashboard.js / routes / templates), never fixes.

Skills:

- **/smoke-test** — before every commit and after every rebuild.
- **/write-tests** — whenever adding or changing tests.
- **/sync-docs** — after any route/module change; code is the source of truth.
- **/release-image** — building/shipping a customer image (upgrade-safe).
- **/add-brain-call** — checklist when adding a new client→brain `/v1` call.

## Definition of done — regression protection

1. **Grep all call sites** of any shared function/constant you change; update
   every caller in the same change.
2. **Full suite green** — `python -m pytest tests/ -q` passes entirely, not
   just the new tests. Never weaken or delete an old test to get green.
3. Routes or `/lab` UI touched → **/smoke-test** on the persistent-volume
   stack, full upload→chart→edit→report cycle in the browser; ui-tester for
   UI changes.
4. **reviewer agent on the diff** before commit.
5. **No silent behavior changes** — anything protocol- or user-visible is
   named in the commit body and mirrored into the docs in the same commit.
6. **No data loss on update** — see "Data safety" above; stored-shape changes
   carry an old-shape regression test.

## Memory

Check your agent's memory folder for session-specific learnings. Memory
is shared with the broader project — entries are tagged by context;
pay attention to which apply on-prem (e.g. GCSPath learnings do NOT
apply here).
