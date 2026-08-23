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
├── app.py                   # FastAPI app + lifespan + /health, /version (build stamp)
├── brain_client.py          # HTTP client to brain (bearer token)
├── run_chat_local.py        # local execution + _safe_preview guard
├── local_store.py           # users + chats + conversations on local disk
├── password_utils.py        # stdlib PBKDF2 password hashing (werkzeug-compatible format)
├── schema_builder.py        # _schema_text builder (memoized, 300s TTL)
├── excel_table_detector.py  # 6-stage Excel table detection
├── auto_analytics.py        # background job (brain planner → local exec → PPTX)
├── code_exec.py             # safe_execute
├── plot_utils.py            # render_plot_safe
├── exec_sanitizer.py        # Article XIII pre-execution dtype sanitize gate (both exec sites)
├── outlier_utils.py         # deterministic outlier helpers (outlier_mask / drop_extreme_outliers), pre-imported at both exec sites
├── sandbox_guard.py         # exec-env __builtins__ with a DB-driver import denylist
├── db_connector.py          # SQLAlchemy dialect registry: test/introspect/preview/snapshot
├── db_sources.py            # data_sources.json registry (Fernet creds) + audit + connector closure
├── db_scheduler.py          # nightly snapshot refresh + drift resync (lifespan-scoped thread)
├── relation_discovery.py    # relation candidates (FK/name/description/pasted-SQL) + snapshot verification + banding (ladmin review, proposals only)
├── roles_store.py           # roles.json registry (built-in Base role, scope grants) + dynamic effective-access helpers — the DB-table role gate
├── pptx_template_cache.py   # local cache of the brain-served tenant template/spec
├── settings.py
├── models.py
├── logger_utils.py
├── routes/
│   ├── auth.py              # /auth/* — email+password login, reset, change
│   ├── upload.py            # /upload, /schema_autofill_full, /generate_chatdata, /session/db_tables
│   ├── schema.py            # /schema_details, /schema_common_fields, /schema
│   ├── chat.py              # /api/chat/* — SSE stream, edit-regenerate, sharing
│   ├── dashboards.py        # /api/dashboards/* — pinned-tile dashboards + sharing
│   ├── admin_data.py        # /api/admin/* — ladmin "Data sources" (connections/tables/refresh)
│   ├── admin_users.py       # /api/admin/users* + /api/admin/roles* — ladmin "User management" (roles, grants)
│   └── report.py            # /download_report (PDF), /download_pptx
├── templates/
│   ├── dashboard.html       # /lab page
│   ├── dashboard_view.html  # /dashboards/{id} page (pinned-tile grid)
│   ├── admin_data_sources.html # /admin/data_sources admin panel (ladmin's ONLY page: sidebar nav incl. Relations — confirmed-relations overview + persistent Recommended-tables block (v4) + discovery — plus Users (searchable list, instant role dropdown, first/last login) and Roles (role cards + #roleModal tri-state access tree connection → schema → tables, #roleDeleteModal narrow confirm) — 3-step register wizard with auto-suggested relations at step 3 AND the #twAccessRoles role-access panel, the v4.2 #recAcceptModal type-choice dialog reusing the #pwModal narrow-modal chrome, a muted build stamp in the sidebar footer, standalone CSS/JS)
│   ├── auth_landing.html    # email + password + remember-me sign-in
│   ├── change_password.html # forced new-password page (temp-password logins)
│   └── partials/
├── static/                  # JS + CSS + images (+ vendor/ — ALL offline, never a CDN: gridstack 10.3.1 for the dashboard grid; cytoscape 3.34.0 + dagre 0.8.5 + cytoscape-dagre 2.5.0 for the admin ER relations graph. Each lib has a LICENSE.txt sidecar)
├── Dockerfile
├── docker-compose.yml       # CUSTOMER install (image-only, client.env)
├── docker-compose.local.yml # LOCAL testing stack (build ., persistent pdc_* volumes)
├── tests/                   # pytest (offline — brain calls stubbed)
├── tools/                   # dev tools + fixtures (sample_sales.csv, wide_data.csv); canary_check.py — post-release pipeline canary vs canary_expected.json
└── docs/                    # constitution, protocol, endpoints, build & run
```

## File reference

| File | Purpose |
|---|---|
| [`routes/chat.py`](routes/chat.py) | Chat SSE stream, multi-chart accumulation + persistence, edit-regenerate, sharing, full_table, conversation title generation, Auto Analytics endpoints; MULTI-TABLE answers persist a `tables` array + aligned `full_table_keys` on the AI record (single-shot and mixed dashboard turns); each durable full-table record may carry `result_key` — the dict entry of the re-executed RESULT it came from — so per-table Show-full-table / Download Excel re-execute correctly (`_persist_full_table` / `_reexecute_full_df`); chart-PNG + table-Excel download routes (`export_plotly_png`, `download_excel/{key}`, `export_excel`); `refresh_item` — per-chart/per-table refresh (re-runs ONE item's stored code against the current dataframes, purely local, no brain call; execution failures → `{ok:false,error}` so the UI keeps the previous render); `file_fingerprints` — `{source_filename: {size_bytes, sha256}}` of the chat's files for the Add Data name-collision check (client↔client only, hashing in the executor, per-file failure → name-only entry); `probe_columns` — header-level structure comparison of an uploaded file vs the chat's same-named file for the collision dialog (openpyxl read_only / csv header, first 50 rows cap, no detection pipeline; `{ok, match, uploaded, existing}`, any failure → `{ok:false}`; cell values never logged); ROLE GATE on refresh — `_role_refresh_block(email, chat_id, code)` (per-table: the item's `dfs['…']`-referenced non-connector DB tables checked against `roles_store.allowed_table_ids_for(REQUESTER)`; blocked → 200 `{ok:false, code:"ROLE_DENIED", blocked_tables}`; denied df keys also dropped from the exec namespace via the `drop_df_keys` kwarg on `run_item_refresh`/`_reexecute_full_df` — filtered AFTER the per-chat cached load; fail-open only on gate crash, `ROLE_GATE_FAILED`); `/schema` emits additive per-table `allowed` |
| [`plot_utils.py`](plot_utils.py) | `render_plot_safe` + `_plotly_to_html`; trims the non-functional Plotly modebar tools (`toImage`, `sendDataToCloud`, `select2d`, `lasso2d`) and widens the discrete color palette so >10-category charts never repeat a hue (`_widen_discrete_colors`; continuous/2nd-measure scales untouched); installs the Article XIII sanitize gate + `SANDBOX_BUILTINS` before every exec |
| [`exec_sanitizer.py`](exec_sanitizer.py) | Article XIII pre-execution dtype sanitize gate: `sanitize_for_execution(dfs, sid)` runs inside BOTH exec sites (`code_exec.safe_execute`, `plot_utils.render_plot_safe` — the SANDBOX_BUILTINS pair), so every path (chat, retries, refresh, dashboards, Auto Analytics, full-table re-exec) is covered. Converts category → object/datetime64 (date-likeness heuristic on the categories index: all parse AND digit AND `-`/`/` separator), sparse → dense, nullable/Arrow extension dtypes → numpy equivalents, Period/Interval → str, categorical index → plain; tz-aware datetimes count as standard. Identity fast path (clean frames return the SAME dict — zero copies); never mutates caller frames (`copy(deep=False)` + positional `isetitem` only — never `.loc`/`inplace=` writes, frames are shared across worklists/threads); never raises (per-column `EXEC_SANITIZE_SKIP` log + passthrough, Article IV). Leaf module — must not import code_exec/plot_utils/local_store |
| [`static/dashboard.js`](static/dashboard.js) | Served `/lab` UI; on the constant Enterprise plan it hides the B2C subscription plan-cards so the Paddle upsell (no billing backend on-prem) is unreachable. Profile dropdown carries exactly two items — Change Password (small modal → `POST /auth/password`) and Logout; the B2C Profile/Subscriptions dropdown items don't exist here (bindings are optional-chained). "Add Data" topbar button (same visibility as View / Edit Descriptions) reopens the Create-New modal in 'add' mode (`wizardMode`) — same upload+autofill flow, final step `POST /add_data_to_chat`. Per-chart/per-table refresh buttons (`_wireRefreshButtons`) POST the item's stored code to `refresh_item` and swap the render in place (charts via the plotly container's `_setChartHtml` hook / `img.src`; tables via a rebuilt `.pdc-table-block` that carries the Show data/Show code buttons over); no stored single-block code → no button. REFRESH FREEZING: a button whose code references a df key (`dfs['…']` regex, keys only) missing from the chat's current `/schema` keys renders DISABLED (greyed) with an i18n tooltip — applied on render, on history reload, and re-applied after Add Data (`_applyKeyFreeze`/`_reapplyRefreshFreezes`); a runtime refresh failure FAIL-FREEZES that button for the session (`_showRefreshError`). ADD DATA NAME-COLLISION flow: `addFilesToSelection` resolves collisions BEFORE upload — vs the target chat via `GET file_fingerprints` (size then browser-side `crypto.subtle` SHA-256 on tie; identical → "already in this chat" notice; different → `#fileCollisionModal` with Overwrite (primary button) or Upload-as-`_vN` (via the FormData filename override `_pdcUploadName`); the dialog shows the generic structure warning immediately and a non-blocking ~3s background probe (`POST probe_columns`) may replace it with a specific same-columns info line or different-columns warning — probe failure/timeout keeps the generic text, choosing early ignores the probe) and vs the same selection batch (identical → "Duplicate file ignored"; different → auto-rename `_vN`) — shared helpers `_suggestUniqueName`/`_fileSha256Hex`; NO silent overwrite path remains. MULTI-TABLE answers: `appendMessage` renders `extras.tables` as one titled `.pdc-table-block` per table (per-table full keys aligned by index), live done-handler / history reload / edit-regenerate all pass `tables`+`full_table_keys` through; mixed dashboard turns append the tables after the streamed charts. DB-TABLE PICKER: the Create-New/Add-Data wizard's "🗄️ Select from DB" checkbox dropdown (`_loadDbTablesList`/`_openDbPanel`/`_renderDbTableList` — names only, search, scrollable; `position:fixed` panel because the wizard modal is overflow:hidden, re-anchored by `_positionDbPanel` after selection since the centered modal grows; outside-click close is CAPTURE-phase `closest('#dbSelectWrap')` — modal handlers stopPropagation and would swallow a bubbling listener — plus Escape/scroll; selection count is an overlay badge so the button width never changes — an inline count wrapped the flex row; dashboard.css has NO generic `.hidden`, so these elements carry their own `.hidden` rules; the button shows whenever `/api/db_tables` responds 200 — zero registered tables render an "ask your administrator" empty state so the feature stays discoverable, hidden only while no fetch has ever succeeded; CACHE-FIRST: `/api/db_tables` is prefetched once at page load (`_fetchDbTables`) and every wizard open renders the button same-tick from the last successful fetch with a background refresh re-rendering on success — awaiting the round trip made the button pop in seconds late on slow storage (GCS-fuse demo); a failed background refresh keeps the cached render since `POST /session/db_tables` re-validates ids server-side); checked tables join `selectedFiles` as `{name,_isDbTable,_tableId}` and render as chips with the DB badge; ADD mode marks tables ALREADY in the target chat checked+DISABLED with an "Already in this chat" note (`addDataExistingDbTableIds` from a wizard-open `/schema` fetch, `_fetchAddDataDbTableIds` — stale-response guarded by chat id; disabled on purpose: the add_data DB merge is ADD/UPDATE only, an uncheckable box would fake removal; fetch failure → unmarked picker, never blocked). ROLE GATE UX: `currentChatBlockedKeys` from `/schema` rows with `allowed:false` → `_applyKeyFreeze` greys refresh buttons referencing a blocked key with the `lab.refresh_no_access` tooltip; a `code:"ROLE_DENIED"` refresh response maps to the same localized message + fail-freeze (dashboard_view.js mirrors both via `frozenText('role_denied')` / `applyRefreshPreFreeze`) |
| [`routes/upload.py`](routes/upload.py) | `/new_session`, `/upload`, `/schema_autofill_full`, `/generate_chatdata`, `/add_data_to_chat` (Add Data to an EXISTING chat — same upload+autofill pipeline, then merge into the ChatDataStore; a same-named upload OVERWRITES the file — always user-confirmed upstream by the frontend collision dialog, never silent — and its meta entries are RE-SYNCED via `_resync_meta_after_add`: vanished df keys deleted (stale-entry fix), surviving keys rebuilt from fresh autofill with old file/column descriptions + `values` maps carried over, new keys appended; resync failure logs `ADD_DATA_RESYNC_FAILED` and falls back to the old append-merge; response `{added, updated, removed, files}`), GCS-path stubs (400). DATABASE TABLES: `GET /api/db_tables` (non-connector picker rows), `POST /session/db_tables` (validates ids, 400 on a directly-selected connector, freezes the `expand_with_connectors` closure into the session meta, REPLACES the previous selection), `/upload` preserves DB entries across its `reset_all()`, autofill/generate/add-data accept DB-only sessions (`include_db=False` loads — never read a snapshot to list keys; autofill SKIPS DB entries — ladmin-confirmed descriptions are never overwritten), `/add_data_to_chat` merges DB selections by table_id via `merge_schema_entry`. EVENT-LOOP SAFETY: `/new_session`'s `UserStore(...).reset_all()` (constructor included — `_ensure_layout` mkdirs/writes) and `/api/db_tables`' registry read run via `run_in_executor(_EXEC, ...)` — inline they blocked the whole loop on slow storage (GCS-fuse demo volume), which is what made the wizard's Select-from-DB button appear seconds late. ROLE GATE: `/api/db_tables` filters rows to `roles_store.allowed_table_ids_for(email)` (Base ⇒ empty list, fail-closed); `/session/db_tables` 403s `{code:"ROLE_DENIED"}` on non-allowed SEEDS naming the denied display names — the connector closure stays exempt by design |
| [`routes/auth.py`](routes/auth.py) | Email+password login (a genuinely NEW email sets the entered password + brain-relayed welcome mail; a LEGACY email-only account is refused with a "set your password via reset" notice — mailbox ownership is proven through the reset flow, never a silent adopt), reset flow (client-generated temp password, hash + `must_change_password` stored locally, mailed via the brain), forced password change, `/auth/password` change endpoint, remember-me sessions, profile (constant "Enterprise" plan), sidebar listings, rename/delete, conversation-level share. LADMIN: `_valid_login_id` accepts the configured non-email admin username at LOGIN only (`_EMAIL_RE` untouched elsewhere); reset is REFUSED for ladmin (no mailbox — a temp hash would lock the admin out); `_public_profile` exposes `is_local_admin`, deliberately NOT `is_admin` (that key feeds the B2C Publish menu, 400 by design on-prem); ladmin is CONFIG-ONLY — `_post_login_target` sends admins to `/admin/data_sources` after login/forced-change (and `app.py`'s `/lab` route redirects admins there too); `_start_session` (the single funnel for BOTH login branches) stamps `last_login_at` via `AuthStore.touch_last_login` |
| [`password_utils.py`](password_utils.py) | `generate_password_hash` / `check_password_hash` — stdlib PBKDF2-HMAC-SHA256 in werkzeug's `pbkdf2:sha256:iter$salt$hex` format (werkzeug is not a dependency of this container). Only hashes are stored (`users/{email}/auth.json`) |
| [`routes/report.py`](routes/report.py) | PDF (ReportLab + DejaVu) and PPTX (python-pptx) report rendering — local only |
| [`routes/dashboards.py`](routes/dashboards.py) | `/api/dashboards/*` — per-user dashboards of pinned chart/table tiles. CRUD + tile pin/remove/layout + per-tile refresh (reuses `routes.chat.run_item_refresh` — purely local re-execution, no brain call; deleted source chat → persisted `frozen`, execution failure → `200 {ok:false}` keeping the stored snapshot; ROLE GATE via `_role_refresh_block` BEFORE the branch split so the `_reexecute_full_df` table branch is covered too — blocked → caller-specific `{ok:false, frozen:true, reason:"role_denied", blocked_tables}`, NEVER persisted, mirror of `access_revoked`) + owner-only sharing that mirrors chat sharing (recipient pointer rows + source-chat access grant; brain SMTP relay gets name+comment only, Article II). Storage: `local_store.DashboardStore` under `users/{email}/dashboards/` (atomic writes, 16-hex regex-guarded ids, old-shape docs load with defaults). Page route `GET /dashboards/{dash_id}` in `app.py` renders `dashboard_view.html` |
| [`static/dashboard_view.js`](static/dashboard_view.js) | The `/dashboards/{id}` page: renders tiles from stored snapshots (Plotly iframe / PNG / table — adapted copies of the dashboard.js renderers, parameterized by `tile.chat_id`), GridStack grid (drag by grab strip, resize, non-overlap, debounced layout POST, 1-col read-only under 768px, `pdc-grid-moving` disables iframe pointer-events during drag), tile toolbar (description popover w/ backdrop dismiss, Show data/code via PDCViewers, Download, View larger, Refresh, Remove), Refresh-all (concurrency-2), rename/share/delete, read-only mode for shared recipients |
| [`auto_analytics.py`](auto_analytics.py) | Auto Analytics background job (planner via brain → execute locally → render PPTX) |
| [`run_chat_local.py`](run_chat_local.py) | `run_chat` / `run_chat_multi_plot`; `_safe_preview` data-boundary guard (strict scalar allow-list — dict values must be plain scalars). MULTI-TABLE answers: a RESULT that is a dict of DataFrames/Series/Stylers becomes a `tables` list (`_build_tables_from_result`, dict key = table title; single-entry dicts unwrap to the plain `table`); mixed dashboard plans (python RESULT block + ###NEXT_PLOT### chart blocks) pull the TABLE blocks out of the chart worklist (`_looks_like_table_block`) and carry them on the done event as `tables`/`table_codes`/`table_result_keys` instead of dying in the chart renderer |
| [`local_store.py`](local_store.py) | `AuthStore`, `UserStore`, `ChatDataStore`, `DashboardStore` (per-user dashboards: `users/{email}/dashboards/index.json` + one doc per dashboard incl. tile snapshots; atomic tmp+`os.replace` writes under `_LOCK`; own rows + `shared_by` pointer rows; `resolve_dashboard` own-or-shared access) on local disk; `append_history` / `truncate_conv_history` (every history write goes through `_json_safe` — the B2C normalizer: Timestamp→ISO, NaT/NaN→None, numpy→native, catch-all str() — so a result table with dates or a stray pandas object can never fail persistence; `routes/chat.py` reuses the same `_json_safe` for SSE payloads); TWO-LAYER dataframe caching: (1) in-memory `_DATAFRAME_CACHE` (`LRUTTLCache`: 300s TTL with active sweep + sweeper thread, `DF_CACHE_MAX_MB` byte budget with LRU eviction, oversized datasets served-not-cached; every hit signature-validated against source `(name, mtime_ns, size)` so overwrites/refreshes can never serve stale data); (2) self-healing on-disk parquet cache (`_load_one_file_cached` → `<files_dir>/.parquet_cache/`, manifest keyed on source size+mtime_ns AND `parser_version` — bump `_PARQUET_CACHE_PARSER_VERSION` whenever the parse pipeline changes cached shapes so old caches re-parse instead of serving stale column semantics; atomic writes, round-trip-verified, PICKLE fallback entry for dataframes parquet can't reproduce — e.g. mixed numeric/string object columns — so one bad column no longer aborts the whole file's cache; falls back to the detection pipeline on any failure; Add Data's in-place overwrite invalidates via the stat change); `clone_from_user_store` also copies `.parquet_cache` (mtime-preserving) so a new chat's first question skips the parse. DATABASE TABLES: meta entries with `source:"database"` load from the ONE central snapshot `db_snapshots/{table_id}.parquet` keyed by DISPLAY NAME (`db_entries_from_meta` / `_load_db_snapshots`; `_db_signature` joins the memory-cache signature so a re-snapshot invalidates like an overwritten upload; missing snapshot → key skipped, chat keeps answering; absent `source` ⇒ file — old metas load byte-identically); `schema_docs()` emits `source`/`db_table`/`refreshed_at`/`relations` extras ONLY for DB entries; shared `merge_schema_entry` (Add-Data overwrite resync + DB drift resync + DB re-merge) and `unique_df_key` (display-name collisions resolved at META-WRITE time). `AuthStore` roles: `role` on profile.json (legacy → "user"), `get_role`/`set_role`/`is_admin`, `ensure_local_admin` lifespan bootstrap (hash-only, forced change, NEVER overwrites an existing password), `set_password(force_change=)` kwarg; DATA ROLES: `get_data_role`/`set_data_role` (`data_role` on profile.json, absent ⇒ "base"), `touch_last_login` (stamped by auth's `_start_session`), `list_users` (profile scan — skips ladmin/admin-role/unreadable; the admin Users window) |
| [`brain_client.py`](brain_client.py) | HTTP wrappers for every `/v1/*` call (with `BRAIN_TENANT_TOKEN` bearer auth); `_sanitize_history_rows` strips history to role/content(+code) inside `plan`/`retry`/`summarize` so persisted `image_base64`/`chart_data`/`table`/`usage` never reach the brain (Article II); `post_activity` is FIRE-AND-FORGET on a single-worker `_ACTIVITY_EXEC` thread — its call sites sit in async handlers (login, chat SSE, report, upload), and a synchronous brain call there blocks uvicorn's whole event loop (a slow `/v1/activity` once froze the entire platform); telemetry may lag, requests never do. `_post` takes an optional per-call `timeout` (forwarded by `schema_autofill`) and raises `BrainTimeoutError` — caught BEFORE the generic `httpx.RequestError`, which it subclasses — so a caller behind an interactive click can name the stalled dependency instead of riding the 180s client-wide default |
| [`db_connector.py`](db_connector.py) | SQLAlchemy connector for admin-registered DB sources. Frozen `Dialect` REGISTRY (postgresql / mysql / mariadb / mssql / oracle + a hidden test-only sqlite entry) — URL drivername, default port, quoting, SELECT-1 probe, per-dialect statement-timeout mechanism, catalog row-count/size SQL, LIMIT syntax all live in the entry; adding a DB type = one `Dialect(...)` literal + one pinned driver. `test_connection` / `list_schemas` / `list_tables` / `introspect` (Inspector + catalog estimates, individually `degraded[]` on missing privileges — never COUNT(*) on a customer table) / `preview_rows` / `snapshot_table` (chunked `pd.read_sql` on a Connection → `pyarrow.ParquetWriter` → atomic `os.replace`; chunk-1 `dtype_plan` pins numeric downcasts on write; its `category` entries are RECORDED ONLY — never baked into the file (per-chunk categoricals destabilize the Arrow schema) and never applied by the loader: categorical frames made generated `groupby` code (pandas < 3.0 `observed=False` default) emit the full cartesian product of ALL categories, putting every city/product on a chart axis). SELECT-only by construction: `_assert_single_select` guards the one assembled statement + optional WHERE; a structural test asserts every `text(` literal is SELECT/SET. `URL.create` everywhere (str() masks the password); NullPool + dispose-in-finally; `_scrub` removes secrets from every error before logging/returning. EVERY networked dialect bounds its CONNECT (postgres/mysql/mariadb via `connect_args`, mssql via the URL's `LoginTimeout`, oracle via `tcp_connect_timeout` — added in v4.2; without it thin-mode oracledb fell back to the ~127s OS TCP timeout, the one dialect able to outlast an admin click; a structural test asserts the bound exists per dialect). `introspect` bounds its catalog estimates with `apply_stmt_timeout` like preview/snapshot do (Oracle's all_tables/all_segments is the slow case, and introspect is the FIRST accept phase). `_friendly_db_error` turns timeout-shaped driver errors (each of the five drivers words it differently — psycopg2 "timeout expired", pyodbc `HYT00`, oracledb `DPY-6005`) into ONE sentence by walking the `__cause__`/`__context__` chain; anything not timeout-shaped keeps its scrubbed driver text, so a wrong password is never reported as a timeout |
| [`db_sources.py`](db_sources.py) | `DataSourceStore` — `DATA_ROOT/data_sources.json` (connections + registered tables; DashboardStore idiom: `_LOCK`, atomic replace, `.get()` defaults, 16-hex regex-guarded ids). Passwords Fernet-encrypted at rest (`CLIENT_ENCRYPTION_KEY`; missing key → `EncryptionUnavailable`, NO plaintext fallback; rotated key → `password_readable:false`, nothing deleted; `CLIENT_ENCRYPTION_KEY_OLD` MultiFernet rotation); `_mask_connection` is the ONLY API-facing shape (strips `password_enc` + `url_override`). `audit()` append-only `admin_audit.jsonl` with a recursive secret scrubber. `expand_with_connectors` — the connector transitive closure (undirected, connectors-only, cycle-safe, sorted-deterministic, capped). V4 RECOMMENDATIONS: additive top-level `recommendations` section (`_default_doc` + `read_doc` — read_doc whitelists sections) holding the persistent "Recommended tables" (identifier-only evidence, statuses open/dismissed/registered with `prior_status`); `list/upsert_recommendations` (merge by physical key, frequencies accumulate, evidence unions by (origin, other, pairs), dismissed sticky, registered untouched, already-registered keys create nothing), `set_recommendation_status`, `sync_recommendations`, and `_reconcile_recommendations` — a hook inside EVERY registry mutation (upsert/delete/cascade; cascade also drops the connection's recs): registration flips matching recs to registered remembering prior_status, a vanished registration reverts to prior_status (a dismissed rec never resurrects — this is also what makes Accept's rollback work) |
| [`db_scheduler.py`](db_scheduler.py) | Nightly snapshot refresh. `refresh_one_table` is the ONE refresh implementation (admin Refresh-now + nightly run): snapshot → registry columns updated keeping confirmed descriptions → technical_descriptions recomputed from the snapshot → drift diff → `resync_chats_for_table` (walks `chatdata/*/meta.json`, `merge_schema_entry` carry-over — user edits survive, vanished columns deleted; per-chat failure isolated) or `_touch_chat_refreshed_at`. A FAILED refresh keeps the previous snapshot AND `refreshed_at`. `run_all_due` refreshes sequentially under an `O_CREAT|O_EXCL` lock file (stale-reclaim); `compute_next_run` is pure. Thread started/stopped ONLY from app lifespan — never at import (the local_store sweeper lesson) |
| [`roles_store.py`](roles_store.py) | Roles registry `DATA_ROOT/roles.json` (DataSourceStore discipline: module `_LOCK`, atomic writes, section-whitelisting `read_doc`, 16-hex ids) — `{id, name, description, table_ids, scope_grants:[{connection_id, schema\|null}]}`; built-in Base role (literal id `"base"`, seeded in lifespan right after `ensure_local_admin`, undeletable/unrenamable); `create/update/delete_role` (audited), `set_table_roles` exact reconcile (the wizard Access panel write), `remove_table` / `remove_connection` prunes (table delete + connection cascade delete). EFFECTIVE ACCESS IS DYNAMIC: `effective_table_ids` (explicit ids ∩ live registry ∪ scope matches — schema case-insensitive, `null` = whole connection, later-registered tables covered, CONNECTORS ALWAYS EXCLUDED) / `role_for_email` (missing/dangling `data_role` → Base → empty stub — denials fail closed through defaults, role delete reverts members with no profile rewrites) / `allowed_table_ids_for(email)`. Denied in the exec sandbox |
| [`outlier_utils.py`](outlier_utils.py) | Deterministic extreme-outlier helpers `outlier_mask(series)` / `drop_extreme_outliers(df, col) → (filtered_df, n_dropped)` — the union of robust median/MAD + 1st-99th percentile + 3×IQR tests with a gap guard (only values FAR beyond the bulk drop; benign spread and <5-point samples untouched; non-numeric → clear ValueError, weird numeric input never raises). Exact port of the algorithm the brain planner prompt used to spell out inline (QA 2.6) — the prompt now just tells generated code to call the helper, so behavior is deterministic. Registered at BOTH exec sites (`code_exec` env + `plot_utils.GLOBAL_PLOT_SCOPE`). Leaf module — pandas only, never imports code_exec/plot_utils/local_store |
| [`sandbox_guard.py`](sandbox_guard.py) | `SANDBOX_BUILTINS` — full builtins with a guarded `__import__` DENYING SQLAlchemy/every DB driver/sqlite3 + this client's credential modules + `relation_discovery`/`sqlglot`/`roles_store` (grant tampering = privilege escalation); installed at BOTH exec sites (`code_exec.safe_execute`, `plot_utils.render_plot_safe`). Denylist not allowlist (plotting stacks lazy-import transitively; an allowlist miss would fail-freeze historical stored code). Defense in depth — the SELECT-only DB grant is the real guarantee (Article VII rules 8–9) |
| [`routes/admin_data.py`](routes/admin_data.py) | `/api/admin/*` — ladmin Data-sources API. `_require_admin` 2-tuple guard (401 / 403 incl. pending forced change / `admin.denied` audit). Connection CRUD + Test (saved id OR unsaved draft), schema/table listing, introspect+preview, `draft_descriptions` (reuses `brain_client.schema_autofill`, English, NO write path), save+snapshot with the four mandatory-confirm locks (`confirm:true` → 400 `CONFIRM_REQUIRED`; draft persists nothing; `descriptions_confirmed_by/at` session-stamped; fresh-introspect column comparison → 409 `SCHEMA_DRIFT`), refresh-now per table/connection, refresh_settings, audit tail. TABLES: one registration per PHYSICAL table (connection+schema+table) — a save creating a mapping another registration covers → 400 `DUPLICATE_TABLE`; an edit keeping its stored physical key always passes (LEGACY duplicates keep loading/editing, never auto-deleted); `connection_tables` rows carry `registered_as` for the wizard's disabled-dropdown labels. RELATION DISCOVERY: `/relations/scan` (live FK introspect per registered table — per-connection failures → `degraded[]`, FK evidence only — + name/description candidates via `relation_discovery.discover` incl. the physical-identity filters/dedupe, snapshot verification, banding; response carries `confirmed_count` so the UI can explain a zero-candidate scan — `analyze_sql` too), `/relations/analyze_sql` (sqlglot parse of pasted SQL — in memory only, audit rows carry COUNTS never the SQL), `/relations/accept` (relations-only write: validates ids/cols/cardinality/origin, dedupes either-orientation via `candidate_id`, ONE read-modify-write per child table, deliberately NO confirm/drift/re-snapshot; additive `replaces` = the overview Edit — swap-in-one-write with skip-preserves-old semantics, stale replaces degrades to plain accept, `replaced` count in response/audit), `/relations/delete` (exact-match removal by related ref — id or legacy name — + ORDERED join_keys, removes every identical duplicate, 404 on no match), `/relations/dismiss` (audit-only, session-local by design), `/relations/graph` (read-only graph data via `build_graph`; body carries the client's last-scan ghosts; rendered as an ER DIAGRAM with VENDORED Cytoscape 3.34.0 + dagre 0.8.5 + cytoscape-dagre 2.5.0, all MIT under static/vendor/ — never a CDN; dagre load/run failure degrades to built-in breadthfirst, never to no graph), `/relations/wizard_suggest` (register-wizard suggestions from wizard-held state ONLY — introspected FKs + preview sample + typed descriptions — vs registry + parent snapshots; synthetic child doc with non-hex sentinel `__wizard__`; orientation normalized so the wizard table is ALWAYS the stored child, pre-verify so overlap = share of sample keys in the parent snapshot, data-flipped candidates drop their numbers; FK rows `precheck:true` N:1 unless parent measured non-unique; sample values never reach the audit — counts only, test-pinned). V4 RECOMMENDED TABLES: `GET /relations/recommendations` (read-time role enrichment, bridge-first sort), `POST .../status` (persistent dismiss/restore), `POST .../accept` (one-click connector registration reusing `_draft_table_descriptions` — extracted from draft_descriptions — and `_build_table_doc` — extracted from save_table so the two can never drift; snapshot failure → `delete_table` ROLLBACK, no half-registered state, unlike the wizard save's keep-on-failure; success replays the rec's SQL evidence through the analyze_sql filter chain + `_verify_and_band` — proposed, never auto-confirmed); `_persist_sql_recommendations` on analyze_sql; scan upserts fk recs + replays registered recs' SQL evidence; graph unions server-side OPEN recs with body refs (frozen param kept). V4.1: accept's column rejection names table+column ("Column 'x' does not exist on 'T'." — the v1-era message fused the pair into an 'a=b' token); `_collect_evidence_warnings` feeds additive `evidence_warnings` into scan + rec-accept responses (replay-validator exclusions, `REL_REPLAY_INVALID_COLUMN` logs). V4.2 TIME-BOUNDED ACCEPT + TABLE TYPE: `_draft_table_descriptions` passes `settings.BRAIN_DRAFT_TIMEOUT` (60s) and catches `BrainTimeoutError` first → "The AI description service did not respond within 60s." (deliberately NO `asyncio.wait_for` around `_run(_register)` — executor threads can't be cancelled, so an outer deadline would report failure while registration continued in the thread, i.e. the half-registered state the rollback prevents); `REC_ACCEPT_PHASE phase=introspect|draft|register|snapshot` logs on ENTRY (completion-only logging is why the original hang left no log line at all) + `REC_ACCEPT_DONE`; accept body takes `chosen_type` (default connector = old behavior, invalid → 400) and audit-only `suggested_type` (enum-validated, NOT recomputed server-side), and BOTH audit call sites record the pair; new `POST /relations/recommendations/classify` (bounded introspect → `classify_table_type`, no brain call/write/audit, any failure → `{classified:false}` connector fallback so it can never block Accept); `tables/introspect` returns additive `classification` for the wizard pre-tick. Blocking DB work on a bounded `_DB_EXEC` pool + atexit. ROLES: `save_table` takes optional `access_role_ids` (wizard Access panel) → `roles_store.set_table_roles` reconcile AFTER upsert (absent ⇒ no role writes — rec-accept + old payloads; failure never fails the save); `delete_table` best-effort prunes the id from every role. NOTE: `/connections/test` is registered BEFORE `/connections/{cid}` — literal beats parameter |
| [`routes/admin_users.py`](routes/admin_users.py) | `/api/admin/users*` + `/api/admin/roles*` — ladmin User management (imports `_require_admin`/`_json_body` from routes.admin_data; own router so the guard-coverage test enumerates it separately). `GET /users` (readable profiles sorted by email, RESOLVED role ids — dangling→base, ladmin + admin-role profiles excluded), `POST /users/set_role` (EMAILS BODY-CARRIED never path params; 400 ladmin/admin/unknown role, 404 unknown user; audited `user.set_role`), `GET /roles` (Base first, `member_count` from resolved members), `POST /roles[/{rid}]` (create 201 / edit; 400 dup name case-insensitive incl. reserved "base", unknown table id, CONNECTOR table id — exempt ⇒ ungrantable, unknown connection, malformed grant, Base rename — description/grants stay editable), `POST /roles/{rid}/delete` (400 base; `reverted_members` is a COUNT — dynamic revert, no writes) |
| [`relation_discovery.py`](relation_discovery.py) | Pure-logic relation discovery (no store/network/filesystem): `normalize_column_name` (separator canonicalization + one prefix/suffix strip), FK/name/description candidate generators (ubiquity hard cap `NAME_MAX_TABLE_SHARE` with `MIN_UBIQUITY_TABLES` floor so tiny registries survive, idf down-weight + generic-name penalty, per-source `MAX_CANDIDATES_PER_SOURCE` cap logged when hit), `extract_sql_joins` (sqlglot `traverse_scope` resolves aliases/CTEs/subqueries, composite ON → one multi-pair candidate, Column=Column only so literals drop, frequency = distinct statements; sqlglot error text embeds the SQL → only exception TYPES are ever logged), `verify_candidates` (injected loader, per-(table,cols) cache, uniqueness → cardinality, 1:N flipped to N:1 EXCEPT declared FKs, overlap via left-merge against de-duplicated parent keys, numeric-aware dtype casts), `band`/`band_all` (CONFIRMED/SUGGESTED/ATTENTION, thresholds in ONE constants block), `candidate_id` (direction- and pair-order-independent sha1 — also the accept dup-check key), `filter_existing` (either orientation + legacy `related_table` name refs). PHYSICAL IDENTITY (`physical_key` = connection+schema+table, case-insensitive; `_keys_match` requires completeness so wizard drafts never false-positive): `filter_same_physical` (never propose a table joined to its own duplicate registration), `filter_existing_physical` (declared relations suppress candidates across duplicate registrations), `dedupe_physical_targets` (fan-out → ONE candidate + `alternate_targets`; preference `registration_rank` = connector > earliest created_at > id, symmetric on the child side), `prefer_registration` (ONLY within one physical table — same name on two connections stays ambiguous); SQL endpoints resolve duplicates to the preferred registration, unresolved ones → `stats.unknown_tables` (response-only). V3: `unregistered_fk_refs` (FKs at unregistered physical tables, connection-scoped, ghost-list + graph fixtures), `resolve_unknown_tables` (register-shortcut hints, unambiguous-connection rule, never for registered names), `build_graph` (nodes/edges + BFS components/isolated/suspicious/ghosts for the admin graph view — pure, pytest-covered; v4: two-pass ghost section rendering recommendation `evidence` as dashed edges — sql ghost→partner with key labels, mirror-deduped, ghost↔ghost supported; fk as classic child→ghost deduped vs referenced_by_ids). V4: `extract_sql_joins` additionally emits `stats.unregistered_joins` (anchored identifier-only evidence for predicates touching UNREGISTERED tables — missing-table column FIRST, both-sides-unknown → two records, ambiguous-other skipped, per-statement dedupe, `UNREG_TABLE_CAP`=20) + `stats.unregistered_tables` (per-table distinct-statement counts); `unregistered_fk_refs` gains additive `referenced_pairs`; pure `recommendation_candidates` (SQL evidence ONLY — fk never replayed so band()'s fk-auto-confirm can't fire from replay) and `recommendation_summary` (dynamic bridge/referenced role from REGISTERED partners only + the FULL partner list incl. `registered:false` entries + the locked `pending` preview + per-rec `evidence_warnings`). V4.1 COLUMN VALIDATION (wrong SQL = first-class, never silent, never a bogus candidate): analyze-time — any predicate side resolving to a registered table is checked case-insensitively (`_has_column`; qualify normalizes case, its fallback doesn't) against registry metadata, invalid pairs skipped (composite ONs keep valid pairs) + reported in `stats.invalid_column_refs` with the 1-based ANALYZED-statement number; replay-time — pure `validate_rec_evidence(rec, tables) -> (clean, invalid)` excludes pairs the now-known registration lacks (also the corrupted-store guard, no migration); `stats.unresolved_predicates` counts the known-limitation silent drop of computed CTE/subquery projection joins. V4.2: pure `classify_table_type(columns) -> {suggested_type, reason}` — a column is key-like when its NAME equals/ends with `KEYLIKE_SUFFIXES` (id|code|key|no|num) AND its dtype is integer-family or a varchar ≤ `KEYLIKE_SHORT_VARCHAR_MAX` (both halves required: `VARCHAR(4000) note_code` is free text, `INTEGER amount` is a measure); ALL key-like → connector, else normal naming the descriptive columns; empty/odd input or any exception → connector + "could not classify — defaulted to connector" (never raises). Deterministic rather than AI because the schema-autofill prompt lives BRAIN-side and its parser drops unknown response keys — an AI-suggested type needs a brain change. Deliberately ignores pk/fk/row_count. Denied in the exec sandbox along with `sqlglot` |
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
