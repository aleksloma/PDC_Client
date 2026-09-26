# Live database tables — design

Status: design accepted 2026-09-26. The registry and admin UI phase and the
SQL guard and live query phase have shipped (the scheduler skip included);
the chat pre-fetch phase and the planner prompt change (brain) follow
(section 10). Companions: `docs/DB_TABLES_PLAN.md`, `docs/ENTERPRISE_ARCHITECTURE.md`,
`docs/AI_CONSTITUTION.md` (Articles II, VII, XIV). References name files,
functions and routes.

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
  runner imports (`executor/Dockerfile`), none of which imports
  `db_connector` or `db_sources`; `sandbox_guard.py` denies those imports on
  top; its network is `internal: true` (`docker-compose.yml`).
- A `pdc_sql()` callback from generated code back into the application is
  out of scope. It would hand generated code a route to the database.

A snapshot table is copied to `db_snapshots/{table_id}.parquet` by
`db_scheduler.refresh_one_table` and loaded into `dfs` by
`local_store._load_db_snapshots` (from the load funnel). The live path
replaces those two steps with a SELECT at question time; everything from
`dfs` onward is unchanged.

## 2. Table document

Four keys on the registered-table document in `data_sources.json`:
`mode` (`"snapshot"`, the default, or `"live"`), `live_reason`
(`"threshold"`, `"manual"` or `null`; server-derived), `live_set_by` (email
or `null`) and `live_set_at` (ISO timestamp or `null`). A live profile adds
two bookkeeping keys, `live_profiled_at` and `live_sample_rows` (section 5).

- An absent `mode` reads as snapshot through `db_sources.table_mode(doc)`,
  which every reader uses. No boot migration, no rewrite of existing
  documents: `read_doc` passes table rows through untouched, so an older
  document loads as it always did.
- `validate_mode_fields(doc)` rejects any other value with `ValueError`; the
  routes answer 400 `BAD_MODE`. `upsert_table` normalises a new document to
  `mode: "snapshot"`.
- Edit-save carry-over. `upsert_table` replaces the whole document, and the
  wizard's `_build_table_doc` (`routes/admin_data.py`) carries only the keys
  it knows (`schedule`, `schedule_last_fired_at`, `registered_by`). `mode`
  and the three stamp keys join that set, so an edit-save cannot silently
  turn a live table back into a snapshot table. `where_filter` and `row_cap`
  are kept too when the save body omits them; an explicit `null` clears
  them.
- When a save flips a table to live, the route stamps `live_set_by` from the
  session identity, `live_set_at` from the server clock and `live_reason` as
  `"threshold"` when the cell count is at or above `LIVE_MODE_CELL_THRESHOLD`,
  else `"manual"`; the browser never asserts the reason. A flip back to
  snapshot clears all five live keys (`db_sources._LIVE_KEYS`: the three
  stamps plus `live_profiled_at` and `live_sample_rows`).
- Downgrade: an older build ignores the keys and treats the table as a
  snapshot table. Nothing is deleted in either direction. A regression test
  loads an old-shape document.
- The field-level setter `set_table_mode` follows `set_table_schedule`
  (`db_sources.py`): validate, read-modify-write under the store lock, audit
  after the lock, `False` for an unknown table.

## 3. Threshold and the cell count

Two settings, read through `settings._int_env` (a mistyped value falls back
to the default instead of stopping the boot):

| Setting | Default | Effect |
|---|---|---|
| `LIVE_MODE_CELL_THRESHOLD` | 50 000 000 | at or above: the admin UI suggests live |
| `LIVE_MODE_FORCE_THRESHOLD` | 500 000 000 | at or above: a snapshot is refused, live is required. The effective value is `max(force, cell)` |

`cell_count = row_count x column_count`. `row_count` comes from
`db_connector.count_rows(cfg, password, schema, table, *, where=None,
timeout_s=None, sid)` returning `{ok, count, timed_out, error}`. The count is
`SELECT COUNT(*)` built as a SQLAlchemy construct over
`_select_stmt(schema, table, where=...)` as a subquery, the
`fingerprint_table` idiom, so the admin's WHERE filter and the dialect's
identifier quoting apply and the compiled statement passes the read-only
gate like every connector statement (`_compiled_sql`). `introspect` itself
never counts; it reads a catalog estimate.

The count runs under the connection's `statement_timeout` capped at
`COUNT_TIMEOUT_CAP_S` = 60 seconds (a module constant): the wizard's
introspect step is interactive, the default statement timeout is 300
seconds, and a five-minute wait behind a click is the kind of hang the
Oracle connect bound was added to remove. The cap is applied by building the
engine from a copy of the connection config with `statement_timeout`
replaced, because SQL Server (pyodbc `timeout`) and ClickHouse (URL
`max_execution_time`) take their bound from connection arguments, not a
session statement; `apply_stmt_timeout` alone would bound nothing on those
two.

| Outcome of the count | Verdict |
|---|---|
| ok | `cell_count` computed (the row count taken at most at the table's `row_cap`); suggested / required from the thresholds |
| timed out, with a `row_cap` whose cap x columns stays below force | `row_count` = the cap, `count_source: "cap"`, not required |
| timed out otherwise | treated as above the force threshold: `live_required: true` |
| any other failure | fall back to `introspect`'s catalog `row_count_estimate` |
| estimate `None` as well | unknown: `cell_count: null`, no suggestion, no refusal; the hint asks the admin to choose |

`count_source` names which number was used (`count`, `estimate` or `cap`).
Unknown is deliberately fail-open: a degraded catalog must not block
registration.

Where it is computed (`_size_verdict`, `routes/admin_data.py`):

- `POST /api/admin/tables/introspect` returns an additive `size_verdict`
  `{row_count, count_source, timed_out, cell_count, live_suggested, live_required}`.
- `POST /api/admin/tables[/{tid}]` (`save_table`) counts again after its own
  fresh introspection, with the filter and cap the saved document will
  carry, and returns the verdict as an additive `size_verdict` on every
  save response. It is the enforcement point for a NEW registration and for
  an edit whose body posts a mode different from the stored one: a snapshot
  at or above force answers 400 `LIVE_REQUIRED` before anything is written.
  An edit of an existing table never changes its mode unless the body posts
  a different mode; for such an edit the verdict is advisory only (a count
  timeout included), and the admin UI says the table keeps its stored mode
  until it is switched.
- `POST /api/admin/relations/recommendations/accept` counts in a `count`
  phase before `register`; at or above force it answers 400
  `LIVE_REQUIRED` and registers nothing. A recommendation never registers
  live.
- `POST /api/admin/tables/{tid}/mode` counts again before a switch to
  snapshot (section 4).
- "At each refresh": `mark_refreshed` overwrites `row_count` with the actual
  snapshot rows, and `GET /api/admin/tables` derives `cell_count`,
  `live_suggested` and `live_required` from the stored fields on every read.
  The scheduler-side refusal (a snapshot table that grew past force) belongs
  to the chat pre-fetch phase (section 9).

## 4. Who may set live

Guards. `_require_source_manager` (`routes/admin_data.py`) admits an
administrator with scope `None` and a power user with the management scope
from `roles_store.management_scope_for`, `None` unless the account's
permission is "power" (`is_power_user`); anyone else gets 403 and an
`admin.denied` audit row. The per-table check is
`_in_scope(scope, connection_id, schema)`, the idiom of the schedule route.
Regular users have no route.

Not owner-only: mode is an operational property like the schedule override;
`NOT_OWNER` stays a delete rule. A power user may switch any table inside
their management scope.

Endpoint `POST /api/admin/tables/{tid}/mode`, body `{"mode": "live" | "snapshot"}`:

| Response | When |
|---|---|
| 401 / 403 | not signed in / neither administrator nor power user (audited `admin.denied`) |
| 403 `OUT_OF_SCOPE` | power user; the table's (connection, schema) is outside the management scope |
| 404 | unknown table id |
| 400 `BAD_MODE` | any other value |
| 400 `LIVE_REQUIRED` | `snapshot` requested and a fresh count (the document's filter and cap) is at or above force, a count timeout included; nothing is written. A count failure other than a timeout falls back to the stored row count |
| 200 `{ok, table}` | same mode as stored: nothing written, no audit row |
| 200 `{ok, table, live_profile}` | to live: the stamp keys set; an existing parquet is KEPT (no update deletes state); a profile is computed from a sample (section 5) |
| 200 `{ok, table, snapshot}` | to snapshot: the live keys cleared and `db_scheduler.refresh_one_table(force=True)` runs through `_run`, exactly as the refresh-now route does; always a fresh snapshot, because a parquet kept through the live period would be stale |
| 200 `{ok, table, snapshot, reverted: true}` | to snapshot, but that snapshot failed: the table is set back to live (keeping its previous `live_reason`, logged `LIVE_MODE_REVERTED`) and `snapshot` carries the failure |

The save route takes an optional body `mode` (absent: the existing mode, or
snapshot for a new registration), applies the rule of section 3, and for a
live table runs `_profile_live_table` instead of `refresh_one_table`
(response `snapshot: null` plus `live_profile`). Refresh-now on a live table
re-counts and re-samples; the connection-wide refresh (administrator only)
skips live rows, `{skipped: "live"}`.

Audit: one `table.mode` row `{from, to, reason, cell_count}` through
`db_sources.audit`, `actor_kind: "power_user"` for a power user (`_kind`).
Admin UI: a Snapshot / Live choice in the wizard's last step with the
verdict as a hint (Snapshot disabled when live is required for a new
registration; an existing table's stored mode is pre-ticked), the counted,
capped or estimated row count in the summary, a LIVE badge and a "live,
profiled <date>" cell in the tables list, no schedule chip on a live row, a
mode dialog, and a note that a live table is not copied, is skipped by
scheduled refreshes and is not offered in chats until the live query path
ships.

## 5. Schema and profile for live tables

- Columns and types: `db_connector.introspect`, unchanged. Sample:
  `db_connector.sample_rows(cfg, password, schema, table, *, columns=None,
  where=None, limit, sid)` returning `{ok, df, error}`, a twin of
  `preview_rows` that returns the frame instead of JSON rows;
  `_select_stmt(..., row_cap=LIVE_PROFILE_SAMPLE_ROWS)` (10 000, a module
  constant) lets the dialect render the limit. The sample runs under the
  connection's statement timeout capped at the same 60 seconds as the
  count, through a config copy; the engine is disposed in `finally`.
- The sample is a HEAD sample: the first N rows the database returns for an
  unordered SELECT, not a random sample. On a clustered or insert-ordered
  table, min/max and top values describe those rows, not the whole table.
- Profile: `dataset_profile.compute_profile(df, *, total_rows=None)`. With
  `total_rows` given, `rows` is that count, `sampled` is true and the
  statistics run on the sample (without it, `sampled` is true only above
  1 000 000 rows). No per-column `COUNT(DISTINCT)`: `nunique` comes from
  the sample, since N extra queries per huge table are not worth a hint (a
  possible later refinement).
- Technical descriptions come from the sample through
  `_generate_technical_description(df[c], total)` (`dataset_profile.py`),
  mirroring `db_scheduler._stats_from_snapshot`, stored through
  `mark_live_profiled(tid, *, row_count, columns, profiled_at, sample_rows)`.
- Storage: `local_store.db_profile_path(tid)` with the stamp
  `{"kind": "live", "profiled_at": ..., "sample_rows": n}`;
  `read_profile(path, src_stamp)` skips the staleness check when
  `src_stamp` is `None`, which is how a live profile is read. No parquet is
  written; `refreshed_at` stays unset; `live_profiled_at` is stamped on the
  document.
- Keep-on-failure: a failed count, sample or profile leaves the
  registration in place with `live_profile: {ok: false, error}`, like the
  wizard's failed snapshot. Refresh-now re-counts and re-samples.

## 6. Live tables in `dfs` and `schema_text`

Design, delivered by the chat pre-fetch phase:

- Same df key: the display name, as `_load_db_snapshots` keys snapshot
  tables, so generated code addresses a live table like any other.
- `ChatDataStore.schema_docs()` gains `live: true`, `dialect` (the registry
  key, e.g. `postgresql`) and `row_cap` for a live entry.
- `schema_builder` replaces the `(snapshot as of ...)` suffix with
  `[LIVE, dialect=<key>, sampled profile]` so the brain writes SQL in the
  right dialect and knows the profile is a sample.
- Profile read: `_profile_src_stamp_for_entry` stats the snapshot parquet;
  a live entry has none, so `ensure_chat_profiles` would omit it. A live
  entry resolves to `db_profile_path(tid)` and is read with `src_stamp=None`.
- `missing_db_tables` gains a distinct `live` reason, so a live entry is
  never reported as `snapshot_missing`; the `missing` flag on `/schema`
  follows.
- The planner's "other registered tables" hint (`run_chat_local.py`)
  carries the live marker as well.
- Auto Analytics (planner, then sandbox) loads its frames through the same
  funnel and needs the same pre-fetch before dispatch; it lands in the same
  phase.
- Relation verification reads snapshots: `_snapshot_key_loader` (scan,
  analyze-SQL and recommendation replay) and the register wizard's
  suggestions check key overlap against the parent's parquet. A live table
  has none, so a candidate touching it is reported unverified ("snapshot or
  column unavailable") until the chat pre-fetch phase gives verification a
  bounded live read.

Interim state (registry and admin UI phase, unchanged by the SQL guard and
live query phase):

- The chat picker `GET /api/db_tables` (`routes/upload.py`) calls
  `list_tables(include_connector=False, include_live=False)`: a live table
  is not offered.
- `POST /session/db_tables` refuses a live id with 400
  `{error, code: "LIVE_NOT_AVAILABLE"}`.
- `expand_with_connectors` (`db_sources.py`) skips live connectors.
- A chat that already holds a table later switched to live keeps loading
  its kept parquet: the loader is unchanged and a mode change never deletes
  the file. The planner hint is left as it is.
- Why: a live registration has no parquet, so offering it would only
  produce "snapshot missing". The admin UI states this plainly.

## 7. Per-dialect table

Row limit as SQLAlchemy `.limit()` renders it for the connector's own
statements, the form `wrap_with_row_limit(sql, dialect, n)` emits for a
live query, and the timeout mechanism the connector uses.

| Dialect | Row limit (connector statements) | `wrap_with_row_limit` | Timeout mechanism | Note |
|---|---|---|---|---|
| postgresql | `LIMIT n` | `SELECT * FROM (<inner>) AS pdc_q LIMIT n` | `SET statement_timeout` plus `-c statement_timeout` in the connect `options` | |
| mysql | `LIMIT n` | as postgresql | `SET SESSION MAX_EXECUTION_TIME` in ms, SELECT only, 5.7.8+ | |
| mariadb | `LIMIT n` | as postgresql | `SET SESSION max_statement_time` in seconds | |
| mssql | `TOP n` | `SELECT TOP n * FROM (<inner>) AS pdc_q`; ` OFFSET 0 ROWS` is appended inside when the inner has a top-level `ORDER BY` and no `TOP` / `OFFSET` / `FETCH` | pyodbc `timeout` connect argument; there is no session SET | SQL Server does not accept a CTE inside a derived table (open point below) |
| oracle | `FETCH FIRST n ROWS ONLY` (12c+) or a `ROWNUM` wrapper, by server version | `SELECT * FROM (<inner>) pdc_q FETCH FIRST n ROWS ONLY` (no `AS` on the alias; 12c and later) | `call_timeout` in ms on the DBAPI connection; connect bound `tcp_connect_timeout` | |
| clickhouse | `LIMIT n` | as postgresql | URL settings `max_execution_time` and `send_receive_timeout`; a session SET does not survive | bounded through the URL, not a statement |
| sqlite (tests only) | `LIMIT n` | as postgresql | none (`_no_op_timeout`) | hidden test dialect |

The wrapper (`db_connector.wrap_with_row_limit`) keeps the inner text
VERBATIM, never re-rendered: comments are stripped and a trailing `;`
removed, nothing else. It applies the cap outside the statement, so an inner
`LIMIT` cannot exceed it. A non-positive cap or an unknown dialect raises
`ValueError`.

Open point for the planner phase: SQL Server refuses a CTE inside a derived
table, so a live query on SQL Server that starts with `WITH` passes the
guard but fails once wrapped. The planner prompt (or a CTE-aware wrapper)
has to settle this before live tables are offered on SQL Server.

`db_connector.run_live_select(cfg, password, sql, *, dialect=None,
row_cap=None, timeout_s=None, sid)`:

- Passes the text through `assert_read_only_query` (CTE allowed, strict
  parse) both as given and as it will run, then wraps it at `row_cap + 1`
  and reads in chunks, stopping at that row; `truncated` says whether it was
  reached. A `dialect` different from the connection's is refused.
- Only a colon that SQLAlchemy's `text()` would read as a bind parameter is
  escaped; `::` casts and colons such as the one in `'10:30'` are left
  untouched.
- The statement timeout is min(the connection's, `timeout_s` or
  `LIVE_QUERY_TIMEOUT_S`), put into a copy of the connection config and
  applied to the session; the engine is a `NullPool` engine disposed in
  `finally`.
- Returns `{ok, df, truncated, rows, elapsed_ms, timed_out, error}`, plus
  `guard: true` when the guard refused the text (no engine is built then).
  Never raises.
- Logs `LIVE_QUERY_OK`, `LIVE_QUERY_ERROR` or `LIVE_QUERY_REJECTED` with a
  hash of the SQL, row counts, timings and an exception type; never the SQL
  text and never the driver message. The returned `error` is the scrubbed
  driver text (`_friendly_db_error`, timeouts classified by `_is_timeout`),
  which may echo parts of the statement and, for a cast or format error, a
  cell value the database quoted. It is meant for the requester, not for a
  log, and not for the brain: the chat pre-fetch phase must reduce it to the
  exception type and the timeout flag (at most a value-free first line)
  before it reaches a retry request.

Settings (through `_int_env`):

| Setting | Default | Effect |
|---|---|---|
| `LIVE_RESULT_ROW_CAP` | 200 000 | the most rows a live query returns; one more is read to flag truncation |
| `LIVE_QUERY_TIMEOUT_S` | 60 | the live query's statement timeout, capped by the connection's own |

## 8. Historical chat items on live tables

Delivered by the chat pre-fetch phase:

- A history row produced with a live table carries `sql` (per table key),
  `live_truncated` and `live_rows` beside `code`, so it can be reproduced.
- Per-item refresh (`refresh_item`, through `run_item_refresh`,
  `routes/chat.py`), full-table re-execution (`_reexecute_full_df`) and
  dashboard tile refresh (`routes/dashboards.py`, reusing those) re-run the
  stored SQL first, under the requester's role: the `_role_refresh_block`
  gate over `roles_store.allowed_table_ids_for` that drops denied df keys
  today. The SQL passes the read-only guard again, the capped result takes
  the table's key, then the stored Python runs in the sandbox as today. The
  request body never carries SQL; only the SQL stored on the row is re-run,
  the same rule as stored code.
- Auto Analytics follows the same rule: its planner-produced SQL is
  pre-fetched in the main application before the sandbox is called.
- If the table has since switched back to snapshot, the refresh uses the
  snapshot and drops the SQL: the parquet is authoritative again. An
  unregistered table or a role that no longer covers it produces the
  existing `ROLE_DENIED` and missing-table outcomes, unchanged.
- Relation candidates involving a live table stay unverified until then
  (section 6).

## 9. Impact on `db_scheduler`, `resync_chats_for_table` and drift

`refresh_one_table` fingerprints, snapshots, diffs columns, derives
descriptions and the profile from the parquet, marks drift and resyncs chat
metas; `run_all_due` walks the registered tables; `_loop` fires the
schedules.

Shipped in the SQL guard and live query phase:

- `refresh_one_table` on a live table logs `LIVE_SKIP` and returns
  `{ok: true, skipped: true, reason: "live"}`: no connection, no
  fingerprint, no snapshot, no profile, no `mark_refreshed`, and
  `refreshed_at` stays as it is.
- `run_all_due` leaves live tables out before its loop (each logged
  `LIVE_SKIP`), so a scheduled run, global or per-table override, never
  opens a connection for them.

Still to come, in the chat pre-fetch phase:

- An introspection-only column diff against the stored columns keeps drift
  detection alive for live tables (`added`, `removed`, `retyped`, through
  `mark_drift`).
- `resync_chats_for_table`: for a live table only registry metadata is
  written into the chat metas (`_registry_meta_entry`: columns,
  descriptions, technical descriptions); no `refreshed_at`, no row-count
  cache from a snapshot.
- The grown-table refusal: a snapshot table whose fresh count is at or above
  force is not snapshotted; the run records `last_refresh_error` naming the
  threshold, the list shows the required verdict, the previous parquet
  stays.

## 10. Rollout and interim states

| Phase | Ships |
|---|---|
| Registry and admin UI (shipped) | the mode keys, the two thresholds, `count_rows` and `sample_rows`, the mode endpoint, the live profile, the picker and session refusal, the UI |
| SQL guard and live query (shipped) | `assert_read_only_query(sql, *, allow_cte, strict_parse, dialect, allowed_schemas)` (a sqlglot parse under the connection's dialect with the regex layer kept in front), `wrap_with_row_limit`, `run_live_select`, `LIVE_RESULT_ROW_CAP`, `LIVE_QUERY_TIMEOUT_S`, the scheduler skip, the edit and mode-switch rules of sections 3 and 4; the connector's own statements keep the strict no-CTE check |
| Chat pre-fetch and brain protocol change | `live_tables` in the plan and retry requests, `sql` in the responses, the pre-fetch in `run_chat_local` and Auto Analytics before dispatch, the history fields, the refresh paths, live relation verification, the live drift diff and resync, the `schema_text` marker, the protocol documents |
| Planner prompt change (brain) | the brain writes one SELECT per referenced live table in the given dialect and pushes filtering and aggregation into it |

What is true between the phases:

- A live registration is unusable in chats until the chat pre-fetch phase:
  hidden from the picker, refused by the session route. The admin UI says
  so.
- Scheduled refreshes skip live tables: a table registered or switched live
  is never copied by the scheduler. Its profile is refreshed only by
  Refresh-now.
- A table at or above force cannot be registered as a snapshot: live
  (profiled, not yet usable in chats) or not at all. An existing snapshot
  table keeps its mode on an edit; switching a live table back to snapshot
  is refused while it counts at or above force. A chat holding a table
  switched to live keeps its kept parquet until the chat pre-fetch phase,
  which then serves live results under the same key.

## 11. Design decisions

1. Decided: four keys on the table document, absent means snapshot, no
   migration or rewrite, validation in `db_sources`, edit-save carry-over,
   server-derived `live_reason`. Reason: backward compatible both ways; the
   one place that drops keys (the wizard edit-save) is the one fixed.
2. Decided: a `COUNT(*)` construct bounded by the connection timeout capped
   at 60 seconds through a config copy; timeout means required unless a
   `row_cap` keeps the table below force; other failures fall to the
   catalog estimate; unknown gives no verdict; counted at introspect, save,
   recommendation accept and the switch to snapshot; "at each refresh"
   through the derived list fields. Reason: explicit where the requirement
   is explicit, fail-open where it is silent.
3. Decided: an edit never changes a table's mode by itself; the verdict on
   an edit is advisory. Reason: a count that times out on a busy database
   must not lock an admin out of editing descriptions.
4. Decided: the mode endpoint uses the source-manager guard plus the
   per-table scope, is not owner-only, keeps the parquet on a switch to
   live, counts again and always takes a fresh snapshot through
   `refresh_one_table` on a switch to snapshot, reverts to live when that
   snapshot fails, refuses with `LIVE_REQUIRED`, and is audited. Reason:
   consistency with the schedule override; no state deleted; a parquet kept
   through the live period must not be served as current; a table must not
   end up as a snapshot table without a snapshot.
5. Decided: a 10 000-row head sample through the dialect's own limit under
   the 60-second cap, `compute_profile(total_rows=)` marks it sampled with
   the true count, no `COUNT(DISTINCT)`, the live stamp read with `None`,
   keep-on-failure with refresh-now re-profiling. Reason: one query per
   registration.
6. Decided: live tables hidden from the picker and refused by the session
   route, live connectors skipped by the closure, chats holding a switched
   table keep the kept snapshot, the planner hint waits. Reason: no parquet
   exists for a live registration; a user must not be offered a table that
   only says "snapshot missing".
7. Decided: the admin UI copy states the interim truthfully, in English
   like the rest of the page, with structural tests. Reason: a claim beyond
   what the code does is a false claim, in UI text too.
8. Decided: the two thresholds, the row cap and the query timeout are
   settings, through `_int_env`; the sample size and count cap are module
   constants. Reason: Article XI.
9. Decided: SQL runs in the main application only, the sandbox receives a
   DataFrame, no callback from generated code. Reason: Article XIV
   properties 3 and 4; a callback would hand generated code a route to the
   database.
10. Decided: the row cap is applied outside the statement by wrapping it
    (an inner `LIMIT` cannot exceed a cap that encloses it), and the SQL
    text is never logged, only a hash and timings (the text may contain
    literal values from the question).

## 12. Trust boundaries

- Article II is unchanged. Only SQL TEXT crosses from the brain to the
  client, inside the plan response; the client sends the question, the
  schema text (with a live marker and a dialect name, both metadata) and the
  sampled profile (aggregates and truncated top values, already permitted
  for snapshot tables). Results never leave.
- The sandbox is unchanged: a DataFrame in, no driver, no credential, no
  route (section 1). A structural check that no executor-copied file imports
  `db_connector` or `db_sources` stays in the suite.
- The database login must be SELECT-only, ideally on a read replica, with a
  database-side statement timeout and resource group for that account; the
  application's guard and caps are defence in depth, the grant is the
  guarantee (Article VII, rule 9).
- The count and the sample run under the connection's statement timeout
  capped at 60 seconds, on a `NullPool` engine disposed in `finally`
  (`get_engine`); the password is decrypted into a function local at the
  moment of use (`db_sources.decrypt_password`).
- Free-form SELECT is accepted only through `assert_read_only_query` and
  executed only in the main application by `run_live_select`; the
  connector's own statements pass the same gate strictly without CTEs
  (`_compiled_sql`, where a parse failure only warns because the statement
  is built by the connector itself). Before every live SELECT the
  requester's role must cover the table (`roles_store.allowed_table_ids_for`),
  as the picker and refresh gates require today.
- The SQL text is never logged; driver errors pass `_scrub`; audit rows
  carry counts and hashes, never SQL. Sample values feed only the profile
  and technical descriptions the snapshot path already derives and reach
  the brain under the existing profile rules, never as rows.
