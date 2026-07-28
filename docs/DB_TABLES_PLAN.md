# Database Tables Feature — Plan (2026-07-27)

Source spec for the "database tables" feature: let a local admin register external
database tables so users can analyze them in chats exactly like uploaded files.
**Phase 1 (this plan) is CLIENT-SIDE ONLY, snapshot mode only — no brain changes.**

## Why this fits the architecture

Everything downstream of `load_dataframes()` (schema_text, planning, safe_execute,
charts, reports, Auto Analytics) consumes a `dict[str, pd.DataFrame]` and is
source-agnostic. A DB table that enters as a parquet-backed named DataFrame with a
meta.json entry is indistinguishable from an uploaded file — the LLM layer, sandbox,
retry loop, and brain protocol need no changes in Phase 1.

## Decisions (2026-07-27)

1. **Visibility:** Phase 1 — all users see all non-connector tables. Role-based
   table visibility (admin-defined roles with view permissions assigned to users)
   is a standalone follow-up task after this feature ships.
2. **DB set:** launch with PostgreSQL, MySQL/MariaDB, MS SQL Server, Oracle. Build
   a dialect REGISTRY so adding a DB type later = one registry entry (SQLAlchemy
   URL template, default port, quoting) + a driver package. Later candidates:
   IBM DB2, SAP HANA, Snowflake, ClickHouse, SQLite.
3. **Descriptions:** English.
4. **Sharing:** chats/dashboards on DB tables shareable freely in Phase 1 — the
   sharing user is responsible. Later (with roles): recipients without permission
   lose the ability to refresh shared items.
5. **Refresh schedule:** admin-configurable, default midnight (container-local).
6. **No threshold logic in Phase 1.** Working assumption: all registered tables
   are snapshot-sized (< ~2M rows). No size checks, no mode flags, no routing.
   Row count and size ARE captured at registration (metadata for the future), but
   nothing acts on them.
7. **Large tables → Phase 2.** Live SQL mode (brain writes dialect-aware
   aggregation SELECTs; client validates + executes; the customer DB does the
   heavy work and only the small result enters pandas) is a separate later phase,
   implemented on client + brain in parallel. A middle "add a WHERE filter" lane
   was considered and rejected (one day of retail transactions can alone be 10M
   rows — filters don't reliably shrink tables).

## Data model

Two entity types, stored in `data_sources.json` under `DATA_ROOT`:

- **connections** — id, name, db_type, host, port, database/service name (Oracle
  service/SID, MSSQL database), user, password, SSL flag, timeouts. One
  connection is reused by many tables. Passwords Fernet-encrypted at rest (key
  from a new `CLIENT_ENCRYPTION_KEY` env var), masked in every API response,
  never logged, never sent to the brain.
- **tables** — id, connection_id, schema, table_name, display_name, description
  (English), columns (name, dtype, description, indexed flag), is_connector
  flag, relations `[{related_table, join_keys}]`, row_count + size estimate,
  refreshed_at, snapshot path. Optional per-table WHERE filter / row cap (a tool,
  never required).

## ladmin

Fixed local admin username `ladmin`, password bootstrapped from an env var
(hash-only on disk via password_utils, forced change on first login). A `role`
field on the local user record (default "user"; ladmin = "admin") so roles can
later be assigned from the brain side without migration. All admin routes behind
a require-admin guard. Append-only audit JSONL under DATA_ROOT for every admin
action.

## Registration flow (admin "Data sources" page, ladmin only)

1. Add connection → **Test connection** button (`SELECT 1`, short timeout, driver
   error shown on failure).
2. Register table: pick schema + table → introspect via SQLAlchemy `inspect()`
   (columns, dtypes, PK/FK, existing indexes, estimated row count, table size) →
   preview first rows → AI-drafted table + field descriptions in English via the
   existing schema-autofill flow.
3. **Mandatory confirm:** ladmin must review/edit descriptions before save — no
   unconfirmed descriptions are ever persisted.
4. Editable fields: display name (`CL_INFO` → "clients information"), table
   description (contents + how it joins to other tables), is_connector, relations
   (related table + join keys), per-column indexed flag (pre-filled from
   introspection, overridable). Row count + size auto-captured and shown.
5. On save, snapshot the table to parquet (chunked `pd.read_sql`, statement
   timeout, dtype optimization: category strings, downcast numerics).

## Connector tables and relations

A table flagged `is_connector` is a helper/join table (dictionaries, link tables):

- Never listed in the client-facing table picker.
- Auto-included in a chat whenever any related table is selected (walk the
  relations graph transitively).
- Relations + descriptions flow into schema_text so the model writes correct joins.

## Dataset integration

- New meta entry source type `"database"`: `ChatDataStore`/`load_dataframes()`
  read the snapshot parquet instead of parsing a file. The df key visible to
  users and generated code = the display name.
- DB tables and uploaded files can mix in one chat.
- Existing two-layer caching (in-memory + parquet cache manifests) applies; a
  refresh rewrites the snapshot and bumps the manifest so invalidation works
  exactly like an overwritten upload.

## Client-facing UI

Create-New modal and the Add Data flow get an "Add database tables" section:
registered non-connector tables listed by display name + short description with a
DB badge, multi-select.

## Refresh

Background scheduler (same pattern as auto_analytics): re-snapshot all registered
tables at the admin-configured time (default midnight), plus "Refresh now" per
table/connection in the admin UI. Show "data as of <refreshed_at>" on chats using
DB tables. On schema drift (columns added/removed in the source DB), re-sync meta
entries the way `_resync_meta_after_add` does.

## Security invariants

- **The sandbox never gets DB access.** DB drivers must not be importable inside
  the code-execution namespace — generated code can never open a connection.
  (Belongs in AI_CONSTITUTION.)
- The connector only ever issues SELECT/introspection statements. Customers are
  instructed (CUSTOMER_INSTALL) to provide a dedicated SELECT-only DB login,
  ideally on a read replica — the grant is the real guarantee.
- Article II unchanged: raw DB values never reach the brain; only names, dtypes,
  and descriptions cross via the existing schema_text path.
- Credentials: encrypted at rest, masked in APIs, never logged, never in any
  brain_client payload.

## Testing and docs

- Offline pytest using in-memory SQLite through the same SQLAlchemy code path;
  regression tests that pre-existing meta.json shapes still load; full suite
  stays green.
- Docs updated in the same change: CLIENT_ENDPOINTS.md, ENTERPRISE_ARCHITECTURE.md,
  AI_CONSTITUTION.md, BUILD_AND_RUN.md, CUSTOMER_INSTALL.md, client.env.example.
- After implementation: restart the local stack
  (`docker compose -f docker-compose.local.yml up -d --build`, never `down -v`)
  and verify `GET /health`.

## Phase 2 (later, client + brain in parallel — NOT in scope now)

Live SQL mode for large tables: per-table mode routing (threshold ~2M rows was
discussed as a default, with a column-count-aware RAM guard), SQL-generation
skill + per-dialect cards on the brain, client-side validation gate (sqlglot AST,
single-SELECT-only, injected LIMIT, statement timeout), SQL retry loop mirroring
the Python one, per-conversation extract caching. Data boundary preserved: the
brain emits SQL text only, never touches the DB.

## Follow-up standalone task (after Phase 1)

Client-side user management: admin creates roles with table-view permissions and
assigns them to users; recipients of shared chats/dashboards without permission
lose refresh.
