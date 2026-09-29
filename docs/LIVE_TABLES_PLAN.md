# Live database tables — design

Status: design accepted 2026-09-26. The registry and admin UI phase, the
SQL guard and live query phase (the scheduler skip included) and the chat
pre-fetch phase have shipped: a live table is offered in chats and queried
at question time. The planner prompt change (brain) follows (section 10);
until it ships every referenced live table takes the client's default capped
read. Companions: `docs/DB_TABLES_PLAN.md`, `docs/ENTERPRISE_ARCHITECTURE.md`,
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
replaced, because SQL Server (the query-timeout attribute of the ODBC
connection, set per connection — the connect-time `timeout` keyword is only
the login/connection timeout) and ClickHouse (URL `max_execution_time`) take
their bound from the connection, not a session statement;
`apply_stmt_timeout` alone would bound nothing on those two.

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
| 200 `{ok, table, snapshot, reverted: true}` | to snapshot, but that snapshot failed: the table is set back to live (keeping its previous `live_reason`, logged `LIVE_MODE_REVERTED`; the profile stamps `live_profiled_at` / `live_sample_rows` the flip cleared are restored from the pre-flip document, so a reverted row is not an unprofiled one) and `snapshot` carries the failure |
| 404 `Unknown table.` | to snapshot, the snapshot failed and the table vanished while it ran: nothing to revert, nothing reported as reverted |

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
scheduled refreshes and is queried at question time, each question reading
at most the configured row cap.

## 5. Schema and profile for live tables

- Columns and types: `db_connector.introspect`, unchanged. Sample:
  `db_connector.sample_rows(cfg, password, schema, table, *, columns=None,
  where=None, limit, timeout_s=None, sid)` returning `{ok, df, error,
  error_class}`, a twin of `preview_rows` that returns the frame instead of
  JSON rows; `_select_stmt(..., row_cap=LIVE_PROFILE_SAMPLE_ROWS)` (10 000,
  a module constant) lets the dialect render the limit. The sample runs
  under the connection's statement timeout capped at the same 60 seconds as
  the count (or the explicit `timeout_s`, which the default live read
  passes as `LIVE_QUERY_TIMEOUT_S`), through a config copy; the engine is
  disposed in `finally`.
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

Shipped in the chat pre-fetch phase, as built:

- Picker and session. `GET /api/db_tables` (`routes/upload.py`) lists live
  tables too (`list_tables(include_connector=False, include_live=True)`)
  with an additive `mode` per row, and `POST /session/db_tables` accepts a
  live seed exactly like a snapshot one; the meta entry is the same
  meta-only entry. `expand_with_connectors` (`db_sources.py`) still never
  adds, or walks through, a live connector.
- Loader partition. `ChatDataStore.load_dataframes(include_db=True,
  include_live=False)` partitions the DB entries by the registry's mode
  BEFORE the cache (`_partition_db_entries`, one `list_tables()` read; a
  registry failure logs `LIVE_REGISTRY_PROBE_FAILED` and loads NO database
  entry at all — fail closed, so a parquet kept from before a table's live
  period can never be served while the registry cannot be read). Snapshot entries load through `_load_dataframes_cached`
  as before; a live entry never reaches `_load_db_snapshots`, so a parquet
  kept from before the live period is never served. With
  `include_live=True` (the chat stream, edit-regenerate, `run_item_refresh`,
  `_reexecute_full_df`) each live entry is appended outside the memory cache
  as a placeholder (`_live_placeholder`): an empty frame with the registered
  column names, dtypes from `_placeholder_dtype` (bool / datetime64[ns] /
  int64 / float64 / object) and `attrs["pdc_live"] = True`. Every other
  caller keeps the default and never sees a live key — Auto Analytics
  included, so the job never queries a live table.
- Same df key: the display name, as `_load_db_snapshots` keys snapshot
  tables, so generated code addresses a live table like any other.
- `ChatDataStore.schema_docs()` adds, for a live registry row only, `live`,
  `dialect` (the connection's `db_type`), `row_cap` = min(`LIVE_RESULT_ROW_CAP`,
  the document's cap), `filtered` (the registration carries a `where_filter`)
  and `db_ref` (schema, table name, quote flags, connection and table ids,
  filter, cap — the pre-fetch's inputs). A snapshot entry emits exactly the
  keys it always did.
- `schema_builder` renders the source line as `Source: database table s.t
  [LIVE, dialect=<key>, row_cap=<n>]` followed by the contract sentence: for
  an unfiltered table, write ONE read-only SELECT for this table and
  `dfs['<key>']` will hold the rows that SELECT returns (the columns listed
  are the table's; a SELECT that aggregates or projects makes the Python use
  the SELECT's own columns); for a filtered table, `dfs['<key>']` holds the
  pre-fetched rows (at most the cap) and no SELECT is to be written. Column
  types are the dtypes alone (the sampled hints would describe an empty
  placeholder). The "other registered tables" hint carries `[LIVE]`.
- Profile read: `ensure_chat_profiles` never computes or writes a profile
  from a placeholder; a live entry reads `db_profile_path(tid)` with
  `src_stamp=None` and is served only when the stored stamp is the live
  kind, else the key is omitted.
- `missing_db_tables` skips a live row (no parquet by design), so it is
  never reported as `snapshot_missing`; `/schema` reports `live: true` and
  `refreshed_at: null` for it, and never feeds `data_as_of` from it.
- The planner request carries `live_tables` `[{name, dialect, row_cap,
  filtered}]`, built by `run_chat_local._live_tables_for_brain` from the
  live docs, only when the chat holds one (`docs/PROTOCOL.md`).
- The pre-fetch: `run_chat_local._ensure_live(sid, dfs, schema_docs, code,
  state, user_email, *, check_role=True)`, idempotent, runs immediately
  before EVERY executor call (the three entry points, every retry, the
  multi-axes split, the chart regenerations, the mixed table blocks). Which
  keys are fetched follows `_referenced_live_keys`: a live key NAMED
  anywhere in the code as `'key'` or `"key"` is referenced; when the text
  still uses `dfs` after every named form and every quoted df key of the chat
  is removed (iteration, `.values()`, `in dfs`, a variable index), every
  unfetched live key counts as referenced too, because an empty placeholder
  must never reach the sandbox silently; code naming only other keys fetches
  nothing. That rule decides WHICH keys are fetched, never how: every
  referenced key takes the planner's SELECT when the turn's `sql` map has
  one, else the default read.
- The role gates. Before the planner is called, the stream and
  edit-regenerate drop every non-connector database key — live, and since
  the sharing change snapshot too — the requester's role does not cover
  from `dfs` and `schema_docs` in place (`routes/chat._drop_uncovered_db_keys`,
  `LIVE_ROLE_DROPPED` / `SNAPSHOT_ROLE_DROPPED`; a gate failure drops every
  database key, `DB_ROLE_DROP_FAILED`), so the brain is never shown such a table;
  a turn left with no frame ends with "You no longer have access to
  <table>; ask your administrator." persisted as the AI row, with no brain
  call. `_ensure_live` checks again per referenced key
  (`roles_store.allowed_table_ids_for`, connectors exempt) — defence in
  depth against a planner-invented key or a revocation mid-turn; the refresh
  paths, whose route gate already ran, pass `check_role=False`.
- The fetch. The connection is decrypted into a local at the moment of use;
  the planner's SELECT goes through `run_live_select` with
  `allowed_tables=_allowed_table_pairs(row, conn)` (the registered schema,
  the unqualified name and the connection's database name, lowercased) so it
  can read only the table it was written for. Without a SELECT, or when the
  registration carries an administrator `where_filter` (a sent SELECT is
  ignored, `LIVE_SQL_IGNORED_FILTERED`, because brain-written SQL could not
  honour the filter), `default_live_fetch` runs the connector's own capped
  construct instead. Success puts the frame under the key (`attrs["pdc_live"]`
  kept) and the sandbox receives it as an ordinary input frame.
- A failed SELECT. The sandbox is not run; the attempt fails with
  `Live query for table '<key>' failed: <class sentence>` and rides the same
  three-retry loop with `sql` (the map that ran) and `sql_error`
  (`brain_client.live_sql_error`: table, dialect, class, guard, the guard's
  own message or null) on the retry request. A SELECT that failed is never
  re-run and never replaced by a default read: a retry that brings no new
  SELECT for that key is a failed attempt, so a retry cannot silently change
  what the answer computes. A failed fetch is a query error, never an
  infrastructure verdict.
- Truncation. Every result dict carries `sql` (per key: the SELECT text, or
  `null` for a default read), `live_truncated` and `live_rows`
  (`_finish_live_result`; the multi-chart done event and each partial carry
  them too), and a capped fetch appends `result_backstop.live_truncated_sentence`
  after the answer text ("Note: <table> is a live table; only the first N
  rows of the query result were used.", localized).
- Logs carry the key, counts, timings and the error CLASS
  (`LIVE_PREFETCH`, `LIVE_PREFETCH_FAILED`) — never the SQL text, never a
  driver message.
- Relation verification still reads snapshots: `_snapshot_key_loader` (scan,
  analyze-SQL and recommendation replay) and the register wizard's
  suggestions check key overlap against the parent's parquet. A live table
  has none, so a candidate touching it is reported unverified ("snapshot or
  column unavailable"); a bounded live read for verification is a later
  refinement.

## 7. Per-dialect table

Row limit as SQLAlchemy `.limit()` renders it for the connector's own
statements, the form `wrap_with_row_limit(sql, dialect, n)` emits for a
live query, and the timeout mechanism the connector uses.

| Dialect | Row limit (connector statements) | `wrap_with_row_limit` | Timeout mechanism | Note |
|---|---|---|---|---|
| postgresql | `LIMIT n` | `SELECT * FROM (<inner>) AS pdc_q LIMIT n` | `SET statement_timeout` plus `-c statement_timeout` in the connect `options` | |
| mysql | `LIMIT n` | as postgresql | `SET SESSION MAX_EXECUTION_TIME` in ms, SELECT only, 5.7.8+ | |
| mariadb | `LIMIT n` | as postgresql | `SET SESSION max_statement_time` in seconds | |
| mssql | `TOP n` | `SELECT TOP n * FROM (<inner>) AS pdc_q`; ` OFFSET 0 ROWS` is appended inside when the body has a top-level `ORDER BY` and no `TOP` / `OFFSET` / `FETCH` | the query-timeout attribute of the ODBC connection, set per connection (the connect-time `timeout` keyword is only the login/connection timeout); there is no session SET | a leading CTE is hoisted above the wrapper (below); every SELECT-list expression must carry an alias |
| oracle | `FETCH FIRST n ROWS ONLY` (12c+) or a `ROWNUM` wrapper, by server version | `SELECT * FROM (<inner>) pdc_q FETCH FIRST n ROWS ONLY` (no `AS` on the alias; 12c and later) | `call_timeout` in ms on the DBAPI connection; connect bound `tcp_connect_timeout` | |
| clickhouse | `LIMIT n` | as postgresql | URL settings `max_execution_time` and `send_receive_timeout`; a session SET does not survive | bounded through the URL, not a statement |
| sqlite (tests only) | `LIMIT n` | as postgresql | none (`_no_op_timeout`) | hidden test dialect |

The wrapper (`db_connector.wrap_with_row_limit`) keeps the inner text
VERBATIM, never re-rendered: comments are stripped and a trailing `;`
removed, nothing else. It applies the cap outside the statement, so an inner
`LIMIT` cannot exceed it. A non-positive cap or an unknown dialect raises
`ValueError`.

The CTE hoist. SQL Server refuses a CTE inside a derived table, so a
statement that starts with `WITH` is split into its CTE block and its body
(`_split_leading_cte`) and the block is HOISTED above the outer SELECT for
every dialect — `WITH <ctes> SELECT * FROM (<body>) AS pdc_q LIMIT n` — the
same query everywhere. Both halves are taken verbatim by tokenizer offsets:
a `RECURSIVE` keyword rides with the block, a ClickHouse expression CTE
(`WITH 1 AS x SELECT ...`) is recognised, and the split is cross-checked
against a real parse (the CTE count, and the body parsing as a SELECT or a
set operation); a mismatch raises `ValueError` ("The CTE block could not be
separated; write the query without a CTE."), which `run_live_select` reports
as class `guard`. The mssql `OFFSET 0 ROWS` rule applies to the body.

The table allowlist. `assert_read_only_query(sql, *, allow_cte,
strict_parse, dialect=None, allowed_schemas=None, allowed_tables=None)`:
`allowed_tables` is a collection of `(schema, table)` pairs compared
lowercased, `''` for an unqualified reference; every table in the parsed
tree that is not a CTE alias (aliases of every `With`, nested ones included)
must match one pair, else "Table '<name>' is not permitted." The chat passes
the one registered table in the forms it accepts (`_allowed_table_pairs`:
the registered schema, the unqualified name and the connection's database
name — MySQL and ClickHouse registrations carry no schema); case-insensitive
because Oracle folds unquoted names to upper case and PostgreSQL to lower.

`db_connector.run_live_select(cfg, password, sql, *, dialect=None,
row_cap=None, timeout_s=None, allowed_schemas=None, allowed_tables=None,
sid)`:

- Passes the text through `assert_read_only_query` (CTE allowed, strict
  parse, the schema and table allowlists forwarded) both as given and as it
  will run, then wraps it at `row_cap + 1` and reads in chunks, stopping at
  that row (`truncated_by: "rows"`). The frames read so far are also weighed
  (`memory_usage(deep=True)`) against `LIVE_RESULT_MAX_MB`; past it the last
  chunk is trimmed proportionally and `truncated_by` is `"bytes"`. A
  `dialect` different from the connection's is refused.
- Only a colon that SQLAlchemy's `text()` would read as a bind parameter is
  escaped; `::` casts and colons such as the one in `'10:30'` are left
  untouched. Two documented edges: a backslash-colon written inside a
  literal loses its backslash on the way through `text()`, and a colon-word
  followed by another colon (`':x:'`) keeps the second colon verbatim.
- The statement timeout is min(the connection's, `timeout_s` or
  `LIVE_QUERY_TIMEOUT_S`), put into a copy of the connection config and
  applied to the session; the engine is a `NullPool` engine disposed in
  `finally`.
- Returns `{ok, df, truncated, truncated_by, rows, elapsed_ms, timed_out,
  error, error_class, error_detail}`, plus `guard: true` when the guard
  refused the text (no engine is built then). Never raises.
- On failure `error` is a FIXED sentence per class (`error_class_text`) —
  the value the chat may show and the retry request may carry — and
  `error_detail` is the scrubbed driver text (`_friendly_db_error`), which
  can quote literals and cell values and therefore stays LOCAL: never
  logged, never sent. For a guard refusal `error` is the guard's own
  message, class `guard`.
- Logs `LIVE_QUERY_OK`, `LIVE_QUERY_ERROR` (with `class=`) or
  `LIVE_QUERY_REJECTED` with a hash of the SQL, row counts, timings and an
  exception type; never the SQL text and never the driver message.

Error classes (`classify_db_error`): `syntax`, `unknown_column`,
`unknown_table`, `timeout`, `permission`, `other`, plus the caller's `guard`.
Timeout wording anywhere in the cause chain (`_is_timeout`) comes first;
then the driver CODE of the exception, of its `.orig` (SQLAlchemy's
`DBAPIError`) or of anything down the `__cause__` / `__context__` chain:

| Driver | Where the code lives | syntax | unknown_column | unknown_table | timeout | permission |
|---|---|---|---|---|---|---|
| psycopg2 | `pgcode` (SQLSTATE) | 42601 | 42703 | 42P01 | 57014 | 42501 |
| PyMySQL | errno in `args[0]` | 1064 | 1054 | 1146 | 3024 | 1142, 1044, 1045 |
| pyodbc | SQLSTATE in `args[0]` | 42000 | 42S22 | 42S02 | HYT00 | 28000 |
| oracledb | `args[0].full_code` | ORA-00900, ORA-00907, ORA-00936 | ORA-00904 | ORA-00942 | ORA-01013 | ORA-01031 |
| clickhouse-driver | `ServerException.code` | 62 | 47 | 60 | 159 | 497 |
| sqlite (tests) | message text | "syntax error" | "no such column" | "no such table" | — | — |

Anything unrecognised is `other`. One fixed sentence per class: "The query
has a syntax error.", "The query names a column that does not exist.", "The
query names a table that does not exist.", "The live query timed out.", "The
database login may not read this.", "The database could not run the query."

`db_connector.default_live_fetch(cfg, password, doc, *, cap=None, sid)` is
the capped default read of a live table — what a question gets when the
planner sent no SELECT for it, or when the registration carries an
administrator row filter. Built exactly like the live profile's sample: the
persisted identifiers (`qname` / `col_ident`, case flags kept), the
document's `where_filter`, the dialect's own row limit at `cap + 1` so
truncation is detectable, then trimmed to `cap`; the statement timeout is
`LIVE_QUERY_TIMEOUT_S` (the connection's wins when lower) and the size cap
trims the frame too. Returns `{ok, df, truncated, truncated_by, rows, error
(the class sentence), error_class}`; never raises.

What the brain must avoid (the guard is strict and refuses rather than
repairs, so these are its false positives on otherwise valid SQL): a `;` or
a DML/DDL word (`INSERT`, `UPDATE`, `DELETE`, `INTO`, `EXEC`, `CALL`, ...)
inside a string literal — the regex layer runs on the comment-stripped text
with literals intact, so such a literal is refused as a chained statement or
a non-SELECT; Oracle `q'...'` quoting (not recognised as a literal); `#`
comments (only `--` and `/* */` are recognised and stripped); SQL Server
table hints such as `NOLOCK`, with or without `WITH`; and, on SQL Server, a
bare alias spelled like a command word (`SELECT 1 SHUTDOWN` reads as an
alias to the parser and is refused — quote it, `AS [shutdown]`). On SQL
Server every expression in the SELECT list must carry an alias
(`count(*) AS n`, not `count(*)`), because the wrapper places the statement
in a derived table, which SQL Server rejects for unnamed columns — a rule
found on a real server. The SELECT may read only the registered table (the
allowlist above); a join to any other table is refused.

Settings (through `_int_env`):

| Setting | Default | Effect |
|---|---|---|
| `LIVE_RESULT_ROW_CAP` | 200 000 | the most rows a live query returns; one more is read to flag truncation |
| `LIVE_RESULT_MAX_MB` | 256 | the most memory a live query's result may occupy, weighed per chunk while it is read; past it the read stops and the result is flagged truncated (`truncated_by: "bytes"`) |
| `LIVE_QUERY_TIMEOUT_S` | 60 | the live query's statement timeout, capped by the connection's own |

## 8. Historical chat items on live tables

Shipped in the chat pre-fetch phase, as built:

- A history row produced with a live table carries `sql` (per df key: the
  SELECT text, or `null` for a default read), `live_truncated` and
  `live_rows` beside `code` (`_attach_live_fields`, `routes/chat.py` — only
  the keys present, so a turn without a live table keeps its shape; the
  stopped-turn record too). The durable full-table records
  (`_persist_full_table(..., sql=)`) store the subset for the keys their
  code references. The in-flight registry keeps the map beside the codes
  (`_inflight_add(chat_id, code, sql=)`), so a chart streamed mid-turn is
  refreshable before its history row exists.
- `stored_sql_for_code(chat_id, code)` resolves the map for a stored code:
  the in-flight turn, then the AI rows whose code (whole or per
  `###NEXT_PLOT###` segment) holds it AND carry a `sql` dict, then the
  durable full-table records; None when the code is not stored with one.
- Per-item refresh (`refresh_item`, through `run_item_refresh`), full-table
  re-execution (`_reexecute_full_df`) and dashboard tile refresh
  (`routes/dashboards.py`, reusing those) load with `include_live=True` and
  re-run the stored SQL first (`_prefetch_stored_live`, blocking, off the
  loop), under the requester's role: the `_role_refresh_block` gate over
  `roles_store.allowed_table_ids_for` runs before it and drops denied df
  keys, so `_ensure_live` is called with `check_role=False`. The SQL passes
  the read-only guard and the table allowlist again, the capped result takes
  the table's key, then the stored Python runs in the sandbox as today. The
  request body never carries SQL; only the SQL stored for the code is
  re-run, the same rule as stored code.
- A referenced live key with NO stored query (the table went live after the
  answer was computed) answers `{ok: false, code: "LIVE_NO_QUERY", error:
  "<display name> is now a live table; ask the question again to re-run
  it."}` — a default read would silently change what the item computes. A
  key stored as `null` was answered from the default read and takes it
  again. A fetch failure answers `{ok: false, error: <class sentence>}`.
  The dashboard tile refresh passes both through, nothing persisted.
- Snapshot wins after a switch: if the table has since switched back to
  snapshot mode, the loader partition served its parquet and the stored SQL
  is ignored — the parquet is authoritative again. An unregistered table or
  a role that no longer covers it produces the existing `ROLE_DENIED` and
  missing-table outcomes, unchanged.
- Auto Analytics does not use live tables: its frames are loaded without
  them, so it never runs a SELECT.
- Relation candidates involving a live table stay unverified (section 6).

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

Shipped in the chat pre-fetch phase:

- `resync_chats_for_table` on a live table writes registry metadata only:
  `_registry_meta_entry` puts `mode` into the chat meta's `db` block and,
  for a live row, `refreshed_at: null` (a stamp from a parquet of the
  snapshot period is never written); columns, descriptions and technical
  descriptions carry over as for a snapshot table.

Still to come (later refinements):

- An introspection-only column diff against the stored columns keeps drift
  detection alive for live tables (`added`, `removed`, `retyped`, through
  `mark_drift`).
- The grown-table refusal: a snapshot table whose fresh count is at or above
  force is not snapshotted; the run records `last_refresh_error` naming the
  threshold, the list shows the required verdict, the previous parquet
  stays.

## 10. Rollout and interim states

| Phase | Ships |
|---|---|
| Registry and admin UI (shipped) | the mode keys, the two thresholds, `count_rows` and `sample_rows`, the mode endpoint, the live profile, the UI |
| SQL guard and live query (shipped) | `assert_read_only_query(sql, *, allow_cte, strict_parse, dialect, allowed_schemas, allowed_tables)` (a sqlglot parse under the connection's dialect with the regex layer kept in front), `wrap_with_row_limit` with the CTE hoist, `run_live_select` with error classes and the size cap, `default_live_fetch`, `LIVE_RESULT_ROW_CAP`, `LIVE_RESULT_MAX_MB`, `LIVE_QUERY_TIMEOUT_S`, the scheduler skip, the edit and mode-switch rules of sections 3 and 4; the connector's own statements keep the strict no-CTE check |
| Chat pre-fetch and brain protocol change (shipped) | the picker and session acceptance, the loader partition and placeholders, the `schema_docs` live keys and the `schema_text` marker and contract sentence, `live_tables` in the plan and retry requests, `sql` in the responses and `sql` / `sql_error` on the retry request, the pre-fetch in `run_chat_local` before every dispatch, the role gates, the history fields, the refresh paths (`LIVE_NO_QUERY`), the live resync, the protocol documents |
| Planner prompt change (brain) | the brain writes one SELECT per referenced live table in the given dialect and pushes filtering and aggregation into it; until then the response carries no `sql` and the client's default capped read applies |

What is true now:

- A live table is offered by the picker, accepted by the session route and
  queried at question time; every referenced live table takes the client's
  default capped read (the administrator's row filter applied) until the
  planner writes SELECTs. The admin UI says a live table is queried at
  question time and that each question reads at most the configured row
  cap.
- Scheduled refreshes skip live tables: a table registered or switched live
  is never copied by the scheduler. Its profile is refreshed only by
  Refresh-now.
- A table at or above force cannot be registered as a snapshot: live or not
  at all. An existing snapshot table keeps its mode on an edit; switching a
  live table back to snapshot is refused while it counts at or above force.
  A chat holding a table switched to live never serves its kept parquet
  again while the table is live: the loader partition serves live results
  under the same key, and a switch back to snapshot makes the fresh parquet
  authoritative again.
- Auto Analytics loads its frames without live tables and never queries one;
  a live connector is never auto-included by the relations closure; relation
  verification does not read live tables.

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
6. Decided: a live table is offered by the picker and accepted by the
   session route like a snapshot table; live connectors stay out of the
   closure; the loader partitions by mode so a kept parquet is never served
   while a table is live, and the parquet is authoritative again after a
   switch back. Reason: the frame under the key must always be what the
   mode says it is.
7. Decided: the admin UI copy states what the code does, in English like
   the rest of the page, with structural tests. Reason: a claim beyond what
   the code does is a false claim, in UI text too.
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
11. Decided: the pre-fetch runs immediately before every executor call and
    is idempotent; a key is fetched when the code names it in either quote
    style, or — when the code walks `dfs` generically — every unfetched
    live key is; the method (the planner's SELECT, else the default read)
    does not depend on how the key was referenced. Reason: an empty
    placeholder must never reach the sandbox silently, and a retry's code
    may reference a key the plan's did not.
12. Decided: a SELECT that failed is never re-run and never replaced by a
    default read; a retry without a new SELECT for that key is a failed
    attempt. Reason: a default read would silently change what the answer
    computes; the planner must own the correction.
13. Decided: a SELECT is bound to the one registered table it was written
    for by the guard's allowlist; an administrator row filter makes the
    default read the only statement that runs for that table. Reason: the
    brain must not widen a user's reach beyond the registered table, and
    brain-written SQL cannot be trusted to honour the filter.
14. Decided: a failed live query crosses as a class and a fixed sentence
    (or the guard's identifier-level message); the driver text stays local
    and unlogged. Reason: Article II — a driver quotes literals and cell
    values.
15. Decided: a refresh of an item whose live key has no stored SQL answers
    `LIVE_NO_QUERY` instead of a default read; a key stored as `null` takes
    the default read again. Reason: a refresh must reproduce the stored
    answer's query, never a different one.

## 12. Trust boundaries

- Article II is unchanged. Only SQL TEXT crosses from the brain to the
  client, inside the plan or retry response; the client sends the question,
  the schema text (with a live marker and a dialect name, both metadata),
  the `live_tables` rows (df key, dialect, row cap, filtered flag), the
  sampled profile (aggregates and truncated top values, already permitted
  for snapshot tables), and on a retry the brain's own SQL echoed back plus
  `sql_error` — a class and, for a guard refusal, the guard's
  identifier-level sentence. Results never leave; the driver's message never
  leaves the process.
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
  is built by the connector itself), and bound by the table allowlist to the
  one registered table it was written for. The requester's role must cover
  the table before the planner is called and again before every live SELECT
  (`roles_store.allowed_table_ids_for`), as the picker and refresh gates
  require today.
- The SQL text is never logged; driver errors pass `_scrub`; audit rows
  carry counts and hashes, never SQL. Sample values feed only the profile
  and technical descriptions the snapshot path already derives and reach
  the brain under the existing profile rules, never as rows.
