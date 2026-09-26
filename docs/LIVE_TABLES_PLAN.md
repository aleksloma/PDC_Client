# Live database tables — design

Status: design accepted 2026-09-26. The registry and admin UI half ships in
this release; the SQL guard and live query function, the chat pre-fetch and
brain protocol change, and the planner prompt change (brain) follow
(section 10). Companions: `docs/DB_TABLES_PLAN.md`, `docs/ENTERPRISE_ARCHITECTURE.md`,
`docs/AI_CONSTITUTION.md` (Articles II, VII, XIV). `file:line` references
point at today's code.

## 1. Purpose and the fixed decision

Some registered database tables are too large to copy into a parquet
snapshot every night. "Live" mode keeps such a table in the database: each
question runs one SELECT against it at question time, and the result of that
SELECT is what the analysis code sees.

The fixed decision:

- The SQL is validated and executed by the main application (the
  `pdc-client` container) before the analysis sandbox is called.
- The sandbox (`pdc-executor`) receives the result as an ordinary DataFrame,
  written into the job directory like any other input frame
  (`exec_transport.write_inputs`); it cannot tell a live table from a
  snapshot table. It never receives a database driver, a credential or a
  network route to the database: its image copies only the modules the
  runner imports (`executor/Dockerfile:54`), none of which imports
  `db_connector` or `db_sources`; `sandbox_guard.py:30-35` denies those
  imports on top; its network is `internal: true` (`docker-compose.yml:127`).
- A `pdc_sql()` callback from generated code back into the application is
  out of scope. It would hand generated code a route to the database.

Today a registered table is snapshotted to `db_snapshots/{table_id}.parquet`
by `db_scheduler.refresh_one_table` (`db_scheduler.py:79-275`) and loaded
into `dfs` by `local_store._load_db_snapshots` (`local_store.py:838-870`,
from the load funnel at `:951`). The live path replaces those two steps with
a SELECT at question time; everything from `dfs` onward is unchanged.

## 2. Table document

Four new keys on the registered-table document in `data_sources.json`:
`mode` (`"snapshot"`, the default, or `"live"`), `live_reason`
(`"threshold"`, `"manual"` or `null`; server-derived), `live_set_by` (email
or `null`) and `live_set_at` (ISO timestamp or `null`).

- An absent `mode` reads as snapshot through `db_sources.table_mode(doc)`,
  which every reader uses. No boot migration, no rewrite of existing
  documents: `read_doc` passes table rows through untouched
  (`db_sources.py:284`), so an older document loads as it always did.
- `validate_mode_fields(doc)` rejects any other value with `ValueError`; the
  routes answer 400 `BAD_MODE`. `upsert_table` (`db_sources.py:434-467`)
  normalises a new document to `mode: "snapshot"`.
- Edit-save carry-over. `upsert_table` replaces the whole document, and the
  wizard's `_build_table_doc` carries only the keys it knows
  (`routes/admin_data.py:1661-1669`: `schedule`, `schedule_last_fired_at`,
  `registered_by`). The four mode keys join that set, so an edit-save cannot
  silently turn a live table back into a snapshot table.
- When a save flips a table to live, the route stamps `live_set_by` from the
  session identity, `live_set_at` from the server clock and `live_reason` as
  `"threshold"` when the cell count is at or above `LIVE_MODE_CELL_THRESHOLD`,
  else `"manual"`; the browser never asserts the reason. A flip back to
  snapshot clears the three.
- Downgrade: an older build ignores the four keys and treats the table as a
  snapshot table. Nothing is deleted in either direction. A regression test
  loads an old-shape document.
- The field-level setter `set_table_mode` follows `set_table_schedule`
  (`db_sources.py:787-809`): validate, read-modify-write under the store
  lock, audit after the lock, `False` for an unknown table.

## 3. Threshold and the cell count

Two settings, read through `settings._int_env` (`settings.py:17-38`, a
mistyped value falls back to the default instead of stopping the boot):

| Setting | Default | Effect |
|---|---|---|
| `LIVE_MODE_CELL_THRESHOLD` | 50 000 000 | at or above: the admin UI suggests live |
| `LIVE_MODE_FORCE_THRESHOLD` | 500 000 000 | at or above: a snapshot is refused, live is required. The effective value is `max(force, cell)` |

`cell_count = row_count x column_count`. `row_count` comes from a new
`db_connector.count_rows(cfg, password, schema, table, *, where=None,
timeout_s=None, sid)` returning `{ok, count, timed_out, error}`. The count is
`SELECT COUNT(*)` built as a SQLAlchemy construct over
`_select_stmt(schema, table, where=...)` as a subquery, the `fingerprint_table`
idiom (`db_connector.py:991-1001`), so the admin's WHERE filter and the
dialect's identifier quoting apply and the compiled statement passes the
SELECT-only gate like every statement (`_compiled_sql`, `:593-601`). Today
`introspect` never counts; it reads a catalog estimate (`:811-831`, `:857`).

The count runs under the connection's `statement_timeout` capped at
`COUNT_TIMEOUT_CAP_S` = 60 seconds (a module constant): the wizard's
introspect step is interactive, the default statement timeout is 300 seconds
(`settings.py:244`), and a five-minute wait behind a click is the kind of
hang the Oracle connect bound was added to remove. The cap is applied by
building the engine from a copy of the connection config with
`statement_timeout` replaced, because SQL Server (pyodbc `timeout`,
`db_connector.py:195-198`) and ClickHouse (URL `max_execution_time`,
`:249-275`) take their bound from connection arguments, not a session
statement; `apply_stmt_timeout` alone would bound nothing on those two.

| Outcome of the count | Verdict |
|---|---|
| ok | `cell_count` computed; suggested / required from the thresholds |
| timed out | treated as above the force threshold: `live_required: true` |
| any other failure | fall back to `introspect`'s catalog `row_count_estimate` |
| estimate `None` as well | unknown: `cell_count: null`, no suggestion, no refusal; the hint asks the admin to choose |

`count_source` names which of the two numbers was used. Unknown is
deliberately fail-open: a degraded catalog must not block registration.

Where it is computed:

- `POST /api/admin/tables/introspect` (`routes/admin_data.py:1494-1552`)
  returns an additive `size_verdict`
  `{row_count, count_source, timed_out, cell_count, live_suggested, live_required}`.
- `POST /api/admin/tables[/{tid}]` (`save_table`, `:1734-1893`) counts
  again after its own fresh introspection (`:1817-1825`): the enforcement
  point. Two counts per registration are accepted.
- `POST /api/admin/relations/recommendations/accept` (`:1264-1409`) counts
  in a new `count` phase before `register` (`:1364`); at or above force it
  answers 400 `LIVE_REQUIRED` and registers nothing. A recommendation never
  registers live.
- "At each refresh" without a scheduler change: `mark_refreshed` overwrites
  `row_count` with the actual snapshot rows (`db_sources.py:524`), and
  `GET /api/admin/tables` (`:1464-1470`) derives `cell_count`,
  `live_suggested` and `live_required` from the stored fields on every read.
  The scheduler-side refusal (a snapshot table that grew past force) belongs
  to the chat pre-fetch release (section 9).

## 4. Who may set live

Guards. `_require_source_manager` (`routes/admin_data.py:76-101`) admits an
administrator with scope `None` and a power user with the management scope
from `roles_store.management_scope_for` (`roles_store.py:470-496`), `None`
unless the account's permission is "power" (`is_power_user`, `:458-467`);
anyone else gets 403 and an `admin.denied` audit row. The per-table check is
`_in_scope(scope, connection_id, schema)` (`routes/admin_data.py:110-117`),
the idiom of the schedule route (`:2057-2096`). Regular users have no route.

Not owner-only: mode is an operational property like the schedule override;
`NOT_OWNER` stays a delete rule (`routes/admin_data.py:1896-1930`). A power
user may switch any table inside their management scope.

Endpoint `POST /api/admin/tables/{tid}/mode`, body `{"mode": "live" | "snapshot"}`:

| Response | When |
|---|---|
| 401 / 403 | not signed in / neither administrator nor power user (audited `admin.denied`) |
| 403 `OUT_OF_SCOPE` | power user; the table's (connection, schema) is outside the management scope |
| 404 | unknown table id |
| 400 `BAD_MODE` | any other value |
| 400 `LIVE_REQUIRED` | `snapshot` requested while the stored cell count is at or above force |
| 200 `{ok, table}` | same mode as stored: nothing written, no audit row |
| 200 `{ok, table, live_profile}` | to live: the four keys set; an existing parquet is KEPT (no update deletes state); a profile is computed from a sample (section 5) |
| 200 `{ok, table, snapshot}` | to snapshot: the live keys cleared and `db_scheduler.refresh_one_table(force=True)` runs through `_run`, exactly as the refresh-now route does (`routes/admin_data.py:1935-1949`); always a fresh snapshot, because a parquet kept through the live period would be stale |

The save route takes an optional body `mode` (absent: the existing mode, or
snapshot for a new registration), answers 400 `LIVE_REQUIRED` for a snapshot
at or above force, and for a live table runs `_profile_live_table` instead of
`refresh_one_table` (response `snapshot: null` plus `live_profile`).
Refresh-now on a live table re-counts and re-samples; the connection-wide
refresh (administrator only, `:364-381`) skips live rows, `{skipped: "live"}`.

Audit: one `table.mode` row `{from, to, reason, cell_count}` through
`db_sources.audit` (`db_sources.py:155-180`), `actor_kind: "power_user"` for
a power user (`_kind`, `routes/admin_data.py:104-107`). Admin UI: a
Snapshot / Live choice in the wizard's last step with the verdict as a hint
(Snapshot disabled when live is required), a LIVE badge and a "live,
profiled <date>" cell in the tables list, no schedule chip on a live row, a
mode dialog, and a warning that a live table queries the database on every
question and is not offered in chats until the live query path ships.

## 5. Schema and profile for live tables

- Columns and types: `db_connector.introspect`, unchanged (`:743-876`).
  Sample: a new `db_connector.sample_rows(cfg, password, schema, table, *,
  columns=None, where=None, limit, sid)` returning `{ok, df, error}`, a twin
  of `preview_rows` (`:877-912`) that returns the frame instead of JSON
  rows; `_select_stmt(..., row_cap=LIVE_PROFILE_SAMPLE_ROWS)` (10 000, a
  module constant) lets the dialect render the limit (`:588-589`); the
  statement timeout applies as today; the engine is disposed in `finally`.
- Profile: `dataset_profile.compute_profile(df, *, total_rows=None)` gains
  one keyword. With `total_rows` given, `rows` is that count, `sampled` is
  true and the statistics run on the sample; today `sampled` is true only
  above 1 000 000 rows (`dataset_profile.py:27`, `:204`). No per-column
  `COUNT(DISTINCT)`: `nunique` comes from the sample, since N extra queries
  per huge table are not worth a hint (a possible later refinement).
- Technical descriptions come from the sample through
  `_generate_technical_description(df[c], total)` (`dataset_profile.py:38-75`),
  mirroring `db_scheduler._stats_from_snapshot` (`db_scheduler.py:277-300`),
  stored through `mark_live_profiled(tid, *, row_count, columns, profiled_at,
  sample_rows)`.
- Storage: `local_store.db_profile_path(tid)` (`local_store.py:659-665`)
  with the stamp `{"kind": "live", "profiled_at": ..., "sample_rows": n}`;
  `read_profile(path, src_stamp)` skips the staleness check when
  `src_stamp` is `None` (`:676-690`), which is how a live profile is read.
  No parquet is written; `refreshed_at` stays unset; `live_profiled_at` is
  stamped on the document.
- Keep-on-failure: a failed count, sample or profile leaves the
  registration in place with `live_profile: {ok: false, error}`, like the
  wizard's failed snapshot today. Refresh-now re-counts and re-samples.

## 6. Live tables in `dfs` and `schema_text`

Design, delivered by the chat pre-fetch and brain protocol change:

- Same df key: the display name, as `_load_db_snapshots` keys snapshot
  tables, so generated code addresses a live table like any other.
- `ChatDataStore.schema_docs()` (`local_store.py:1976-1998`) gains
  `live: true`, `dialect` (the registry key, e.g. `postgresql`) and
  `row_cap` for a live entry.
- `schema_builder` replaces the `(snapshot as of ...)` suffix
  (`schema_builder.py:146-152`) with `[LIVE, dialect=<key>, sampled profile]`
  so the brain writes SQL in the right dialect and knows the profile is a
  sample.
- Profile read: `_profile_src_stamp_for_entry` stats the snapshot parquet
  (`local_store.py:693-706`); a live entry has none, so `ensure_chat_profiles`
  (`:709-735`) would omit it. A live entry resolves to `db_profile_path(tid)`
  and is read with `src_stamp=None`.
- `missing_db_tables` (`local_store.py:747-789`) gains a distinct `live`
  reason, so a live entry is never reported as `snapshot_missing`; the
  `missing` flag on `/schema` (`routes/chat.py:772`) follows.
- The planner's "other registered tables" hint (`run_chat_local.py:247-311`,
  registry read at `:265`) carries the live marker as well.

Interim state, shipped by this release:

- The chat picker `GET /api/db_tables` (`routes/upload.py:754-759`) calls
  `list_tables(include_connector=False, include_live=False)`: a live table
  is not offered.
- `POST /session/db_tables` (validation at `routes/upload.py:851-860`)
  refuses a live id with 400 `{error, code: "LIVE_NOT_AVAILABLE"}`.
- `expand_with_connectors` (`db_sources.py:840-901`) skips live connectors.
- A chat that already holds a table later switched to live keeps loading
  its kept parquet: the loader is unchanged and a mode change never deletes
  the file. The planner hint is left as it is.
- Why: a live registration has no parquet, so offering it would only
  produce "snapshot missing". The admin UI states this plainly.

## 7. Per-dialect table

Row limit as SQLAlchemy `.limit()` renders it today (`db_connector.py:553-590`),
the form the live query function's `wrap_with_row_limit(sql, dialect, n)`
will emit, and the timeout mechanism the connector uses today.

| Dialect | Row limit today | `wrap_with_row_limit` | Timeout mechanism today | Note |
|---|---|---|---|---|
| postgresql | `LIMIT n` | `SELECT * FROM (<sql>) AS pdc_q LIMIT n` | `SET statement_timeout` (`db_connector.py:126-128`) plus `-c statement_timeout` in the connect `options` (`:114-118`) | |
| mysql | `LIMIT n` | as postgresql | `SET SESSION MAX_EXECUTION_TIME` in ms, SELECT only, 5.7.8+ (`:184-187`) | |
| mariadb | `LIMIT n` | as postgresql | `SET SESSION max_statement_time` in seconds (`:190-192`) | |
| mssql | `TOP n` | `SELECT TOP n * FROM (<sql>) AS pdc_q`; an inner `ORDER BY` needs `TOP` or `OFFSET` inside the subquery | pyodbc `timeout` connect argument (`:195-198`); there is no session SET | `SET LOCK_TIMEOUT` is not what the connector uses |
| oracle | `FETCH FIRST n ROWS ONLY` (12c+) or a `ROWNUM` wrapper, by server version | `SELECT * FROM (<sql>) pdc_q FETCH FIRST n ROWS ONLY` (no `AS` on a subquery alias) | `call_timeout` in ms on the DBAPI connection (`:242-245`); connect bound `tcp_connect_timeout` (`:219-222`) | |
| clickhouse | `LIMIT n` | as postgresql | URL settings `max_execution_time` and `send_receive_timeout` (`:249-275`); a session SET does not survive | bounded through the URL, not a statement |
| sqlite (tests only) | `LIMIT n` | as postgresql | none (`_no_op_timeout`, `:71`) | hidden test dialect |

The live query function's settings, added with it: `LIVE_RESULT_ROW_CAP`
(default 200 000) and `LIVE_QUERY_TIMEOUT_S` (default 60), through
`_int_env`. The wrapper applies the cap outside the statement, so an inner
`LIMIT` cannot exceed it. `run_live_select` reads in chunks, stops at
cap + 1 rows and flags truncation, disposes the engine in `finally`, scrubs
driver errors (`_scrub`, `db_connector.py:404`), classifies timeouts
(`_friendly_db_error`, `:429-445`, today's only timeout classifier) and
logs `LIVE_QUERY_OK` / `LIVE_QUERY_ERROR` with a SQL hash and elapsed time,
never the SQL text at info level.

## 8. Historical chat items on live tables

Delivered by the chat pre-fetch and brain protocol change:

- A history row produced with a live table carries `sql` (per table key),
  `live_truncated` and `live_rows` beside `code`, so it can be reproduced.
- Per-item refresh (`refresh_item`, `routes/chat.py:2107`, through
  `run_item_refresh`, `:2023`), full-table re-execution (`_reexecute_full_df`,
  `:127`) and dashboard tile refresh (`routes/dashboards.py:542`, reusing
  those) re-run the stored SQL first, under the requester's role: the
  `_role_refresh_block` gate (`routes/chat.py:1982`) over
  `roles_store.allowed_table_ids_for` (`roles_store.py:435-451`) that drops
  denied df keys today. The SQL passes the read-only guard again, the capped
  result takes the table's key, then the stored Python runs in the sandbox
  as today. The request body never carries SQL; only the SQL stored on the
  row is re-run, the same rule as stored code.
- If the table has since switched back to snapshot, the refresh uses the
  snapshot and drops the SQL: the parquet is authoritative again. An
  unregistered table or a role that no longer covers it produces the
  existing `ROLE_DENIED` and missing-table outcomes, unchanged.

## 9. Impact on `db_scheduler`, `resync_chats_for_table` and drift

Today `refresh_one_table` (`db_scheduler.py:79-275`) fingerprints,
snapshots, diffs columns, derives descriptions and the profile from the
parquet, marks drift and resyncs chat metas; `run_all_due` (`:455-515`)
walks every registered table; `_loop` (`:523-589`) fires the schedules.

Design, delivered by the chat pre-fetch and brain protocol change:

- `refresh_one_table`: a live table logs `LIVE_SKIP`; no fingerprint, no
  snapshot, no `mark_refreshed`. An introspection-only column diff against
  the stored columns keeps drift detection alive (`added`, `removed`,
  `retyped`, through `mark_drift`, `db_sources.py:559-579`).
- `resync_chats_for_table` (`:349-385`): for a live table only registry
  metadata is written into the chat metas (`_registry_meta_entry`,
  `:323-347`: columns, descriptions, technical descriptions); no
  `refreshed_at`, no row-count cache from a snapshot.
- `run_all_due` and `_loop` skip live tables, so a scheduled run never opens
  a connection for them.
- The grown-table refusal lands here: a snapshot table whose fresh count is
  at or above force is not snapshotted; the run records `last_refresh_error`
  naming the threshold, the list shows the required verdict, the previous
  parquet stays.

## 10. Rollout and interim states

| Phase | Ships |
|---|---|
| Registry and admin UI (this release) | the mode keys, the two thresholds, `count_rows` and `sample_rows`, the mode endpoint, the live profile, the picker and session refusal, the UI |
| SQL guard and live query function | `assert_read_only_query(sql, *, allow_cte)` (a sqlglot parse with the existing regex layer kept underneath), `wrap_with_row_limit`, `run_live_select`, `LIVE_RESULT_ROW_CAP`, `LIVE_QUERY_TIMEOUT_S`; the snapshot path keeps the strict no-CTE variant |
| Chat pre-fetch and brain protocol change | `live_tables` in the plan and retry requests, `sql` in the responses, the pre-fetch in `run_chat_local` before dispatch, the history fields, the refresh paths, the scheduler skip, the `schema_text` marker, the protocol documents |
| Planner prompt change (brain) | the brain writes one SELECT per referenced live table in the given dialect and pushes filtering and aggregation into it |

What is true between the phases:

- A live registration is unusable in chats until the chat pre-fetch
  release: hidden from the picker, refused by the session route. The admin
  UI says so.
- Until then the scheduler is untouched: `run_all_due` still walks a live
  table and `refresh_one_table` still snapshots it. A table registered live
  receives a parquet on its next scheduled run, and a table switched to
  live after registration keeps being refreshed. Nothing breaks, but the
  point of live mode (no full copy) is not yet delivered for scheduled runs.
- Recommendation: switch large tables to live after the chat pre-fetch
  release ships. Meanwhile give such a table a disabled per-table schedule
  (`POST /api/admin/tables/{tid}/schedule` with `enabled: false`): an
  overridden table is excluded from the global run (`db_scheduler.py:544-547`)
  and a disabled override never fires (`schedule_utils.next_fire` returns
  `None` when not enabled, `schedule_utils.py:178-188`).
- A table at or above force cannot be registered as a snapshot in this
  release: live (profiled, not yet usable in chats) or not at all. A chat
  holding a table switched to live keeps its kept parquet until the chat
  pre-fetch release, which then serves live results under the same key.

## 11. Design decisions

1. Decided: four keys on the table document, absent means snapshot, no
   migration or rewrite, validation in `db_sources`, edit-save carry-over,
   server-derived `live_reason`. Reason: backward compatible both ways; the
   one place that drops keys (the wizard edit-save) is the one fixed.
2. Decided: a `COUNT(*)` construct bounded by the connection timeout capped
   at 60 seconds through a config copy; timeout means required; other
   failures fall to the catalog estimate; unknown gives no verdict; counted
   at introspect, save and recommendation accept; "at each refresh" through
   the derived list fields. Reason: explicit where the requirement is
   explicit, fail-open where it is silent, the scheduler untouched here.
3. Decided: the mode endpoint uses the source-manager guard plus the
   per-table scope, is not owner-only, keeps the parquet on a switch to
   live, always takes a fresh snapshot through `refresh_one_table` on a
   switch to snapshot, refuses with `LIVE_REQUIRED`, and is audited.
   Reason: consistency with the schedule override; no state deleted; a
   parquet kept through the live period must not be served as current.
4. Decided: a 10 000-row sample through the dialect's own limit,
   `compute_profile(total_rows=)` marks it sampled with the true count, no
   `COUNT(DISTINCT)`, the live stamp read with `None`, keep-on-failure with
   refresh-now re-profiling. Reason: one query per registration.
5. Decided: live tables hidden from the picker and refused by the session
   route, live connectors skipped by the closure, chats holding a switched
   table keep the kept snapshot, the planner hint waits. Reason: no parquet
   exists for a live registration; a user must not be offered a table that
   only says "snapshot missing".
6. Decided: the admin UI copy states the interim truthfully, in English
   like the rest of the page, with structural tests. Reason: a claim beyond
   what the code does is a false claim, in UI text too.
7. Decided: only the two thresholds become settings, through `_int_env`;
   the sample size and count cap are module constants; the row cap and query
   timeout arrive with the live query function. Reason: Article XI; each
   setting is owned by the release that uses it.
8. Decided: SQL runs in the main application only, the sandbox receives a
   DataFrame, no callback from generated code. Reason: Article XIV
   properties 3 and 4; a callback would hand generated code a route to the
   database.
9. Decided: the row cap is applied outside the statement by wrapping it
   (an inner `LIMIT` cannot exceed a cap that encloses it), and the SQL text
   is never logged at info level, only a hash and timings (the text may
   contain literal values from the question).

## 12. Trust boundaries

- Article II is unchanged. Only SQL TEXT crosses from the brain to the
  client, inside the plan response; the client sends the question, the
  schema text (with a live marker and a dialect name, both metadata) and the
  sampled profile (aggregates, already permitted). Results never leave.
- The sandbox is unchanged: a DataFrame in, no driver, no credential, no
  route (section 1). A structural check that no executor-copied file imports
  `db_connector` or `db_sources` stays in the suite.
- The database login must be SELECT-only, ideally on a read replica, with a
  database-side statement timeout and resource group for that account; the
  application's guard and caps are defence in depth, the grant is the
  guarantee (Article VII, rule 9).
- The count and the sample run under the connection's statement timeout
  (the count under the 60-second cap) on a `NullPool` engine disposed in
  `finally` (`get_engine`, `db_connector.py:501-517`); the password is
  decrypted into a function local at the moment of use
  (`db_sources.decrypt_password`, `db_sources.py:98`).
- Free-form SELECT is accepted only through `assert_read_only_query` and
  executed only in the main application; the snapshot path keeps the strict
  variant (`_assert_single_select`, `db_connector.py:459-470`, no CTE).
  Before every live SELECT the requester's role must cover the table
  (`roles_store.allowed_table_ids_for`), as the picker and refresh gates
  require today.
- The SQL text is never logged at info level; driver errors pass `_scrub`;
  audit rows carry counts and hashes, never SQL. Sample values feed only the
  profile and technical descriptions the snapshot path already derives and
  reach the brain under the existing profile rules, never as rows.
