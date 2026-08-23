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
   timeout, dtype optimization: downcast numerics on write; low-cardinality
   string columns are recorded in the plan as `category` but served as plain
   `object` — categorical frames break generated `groupby` code, which
   defaults to `observed=False` on pandas < 3.0).

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

*(Revised after Phase-1 ship: the section became a compact "🗄️ Select from DB"
checkbox dropdown — names only, search on top, scrolls past ~8 tables; the
description now shows as a hover tooltip. Selection semantics unchanged.)*

## Refresh

Background scheduler (same pattern as auto_analytics): re-snapshot registered
tables on the admin-configured schedule, plus "Refresh now" per
table/connection in the admin UI. Show "data as of <refreshed_at>" on chats using
DB tables. On schema drift (columns added/removed in the source DB), re-sync meta
entries the way `_resync_meta_after_add` does.

### Flexible scheduling (Prompt 13 Part B — implemented)

One schedule object (global setting + optional per-table override, same
shape) replaces the single daily `refresh_time`:
`{mode: daily|weekly|monthly|interval|cron, time, weekdays, monthly_days,
every_minutes, cron, enabled}` — see `schedule_utils.py`. Every mode
normalizes to canonical 5-field cron:

| mode | cron(s) |
|---|---|
| daily `02:30` | `30 2 * * *` |
| weekly Mon+Thu `06:00` | `0 6 * * 1,4` |
| monthly 1,15 `00:15` | `15 0 1,15 * *` |
| monthly `last` `02:30` | `30 2 l * *` (separate cron — croniter's `l`, never mixed into a numeric day list) |
| interval 15m / 2h | `*/15 * * * *` / `0 */2 * * *` (fixed-mark semantics — :00/:15/:30/:45, the established-scheduler convention) |
| cron | passthrough (5 fields, croniter-validated) |

- croniter pinned at 6.0.0; the `l` support is guaranteed by an executable
  test (`tests/test_schedule_utils.py::test_croniter_last_day_of_month_pin`).
  If a future re-pin loses `l`, monthly-"last" switches to a
  `calendar.monthrange` daily check — the schedule shape does not change.
- Days 29–31 are rejected on purpose (they silently skip shorter months);
  28 or "last" always fires. Interval minimum is 15 minutes (don't hammer
  the customer DB), under 60 minutes or whole hours only.
- Times stay container-local naive (no timezone feature); the admin UI
  always shows the computed next run so the semantics are visible.
- The scheduler loop keeps its 60s slices (schedule edits apply without a
  restart) and replaces the old once-per-day guard with PERSISTED
  last-fired stamps (`settings.last_fired_at` global;
  `schedule_last_fired_at` per overridden table). A fire moment that
  passed while the container was down is caught up ONCE
  (`next_fire(after=last_fired) <= now`); stamps are written BEFORE the
  run so a backward clock jump or slow run can never double-fire.
  First-ever start initializes stamps to now — no refresh storm on
  upgrade. Overridden tables are excluded from the global run and get
  their own due-checks — a table is never refreshed twice for one due
  moment; the run lock still serializes all snapshot work.
- Migration: an old `{refresh_time, refresh_enabled}` doc loads as
  mode=daily (`schedule_from_settings`); POST /refresh_settings accepts
  both body shapes; legacy keys are mirrored on write for downgrade-compat.
- Connection-level "Refresh now" (`POST /connections/{cid}/refresh`)
  bypasses the run-lock by design (pre-existing behavior, unchanged) and,
  being admin-initiated, always forces a full snapshot.

### Smart refresh — change detection (Prompt 13 Part C — implemented)

Scheduled refreshes probe the source BEFORE snapshotting: ONE SQL aggregate
query per table (`db_connector.fingerprint_table`) — `COUNT(*)`, `SUM`+`AVG`
of up to 4 numeric columns and `MAX` of up to 2 date/timestamp columns
(picked deterministically by registry column order), all wrapped around the
same `_build_select` the snapshot uses so the WHERE filter / row cap compare
like for like — plus the live column name+type list from introspection.
Serialized canonically and hashed (`db_scheduler.compose_fingerprint`):

```
payload = {"v":1, "count":10432,
           "sums":{"amount":"1234567.89"}, "avgs":{"amount":"118.36"},
           "maxes":{"updated_at":"2026-08-21 23:59:12"},
           "schema":[["id","INTEGER"],["amount","NUMERIC(12,2)"],...]}
fingerprint = "fp1:" + sha256(canonical_json)      # e.g. fp1:9c2f0a…
```

- Scheduled run (`force=False`, only the scheduler passes it): fingerprint
  equal to the stored one ⇒ SKIP — `last_checked_at` moves,
  `DB_REFRESH_UNCHANGED` logged, snapshot/profile/chat metas untouched.
  The schema list is inside the hash, so "unchanged AND schema identical"
  is one compare. Otherwise the existing full snapshot runs and stores the
  new fingerprint.
- "Refresh now" / registration / rec-accept / connection refresh:
  `refresh_one_table` defaults to `force=True` — always a full snapshot.
- A fingerprint failure of ANY kind (permissions, exotic types, dialect
  quirks) logs a warning and falls through to the full snapshot — the
  optimization can never block a refresh (Article IV). The stored
  fingerprint is cleared on such a round so a later run can never
  false-skip on a stale hash.
- **Accepted limitation:** offsetting edits could in theory cancel out in
  SUM/AVG; combined with COUNT, MAX(date) and the schema hash this is
  vanishingly unlikely and is the accepted trade for not hammering
  customer DBs (chosen design). The concrete corollary (observed live in
  the Prompt 13 smoke): an in-place edit of a TEXT column with the row
  count unchanged is invisible to the fingerprint — a table with no
  numeric/temporal columns (e.g. a pure dictionary) only re-snapshots on
  scheduled runs when its row count or schema changes. "Refresh now"
  always forces the full snapshot, so any suspect table is one click from
  fresh.

### Schema-drift policy (Prompt 13 Part C — implemented)

Detection now covers dtype changes too: each full refresh updates every
registry column's `dtype` from the fingerprint call's live introspection
(they used to be frozen at registration); `retyped = [{col, from, to}]`
where the normalized types differ.

Policy — **apply source truth immediately, make every structural change
visible, never pause-and-hold-stale-data**:

- ADDED column: auto-included in snapshot and metas (unchanged), pandas
  technical_description computed; the drift record doubles as the "new
  column(s) need description review" flag until the admin edits the table
  or dismisses the banner (descriptions are admin-confirmed by design — no
  background LLM autofill).
- REMOVED column: applied — a snapshot honestly mirrors the source; a
  mirror must never retain phantom columns (Fivetran-style soft-delete
  suits warehouses with history, not snapshots).
- RETYPED column: applied, technical_description recomputed, and a
  retype-only drift ALSO resyncs chat metas (their technical descriptions
  changed).
- RATIONALE: generated pandas code is written fresh per question from the
  CURRENT schema, so new questions self-heal after any drift; the dangerous
  failure is a silently stale or mismatched snapshot/meta — the same
  silent-wrongness class as fabrication. Airbyte's pause/approve model
  suits pipelines feeding fixed SQL, not an LLM that replans every
  question.
- SURFACING: `last_drift` on the table row
  `{added, removed, retyped, at, dismissed}` (a new drift overwrites and
  resets dismissal); the Data sources page shows a red banner per drifted
  table (details incl. `col: from → to`) with an audited Dismiss, plus a
  `schema drift` chip on the row; refresh history (`record_run`) carries
  per-table `skipped`/`drift`. No new notification subsystem.
- Dashboard tiles on a removed column (verified in code): a tile refresh
  failure returns `200 {ok:false}` and the stored snapshot is only patched
  on success (`routes/dashboards.py refresh_tile`); the viewer shows
  "Refresh failed — showing saved version" and keeps the last rendered
  state — the tile is never deleted. The drift banner is where the admin
  learns why.

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
