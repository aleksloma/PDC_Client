# Relations UX — findings and TODO plan

> Working plan for the Relations precision/readability rework (follows the
> discovery feature 9d67910 and the overview/wizard-suggest rework b9e4e2c).
> Live-tested on the dev stack; the registry noise described below is real
> stored state, kept as a regression fixture until manually cleaned.

## Confirmed root-cause findings (live dev stack)

1. **Same-physical-table noise (main precision bug).** `shop.city_dict` is
   registered twice; the scan compared the two registrations to each other and
   proposed (and bulk-accept confirmed) 4 meaningless relations between the
   copies. The declared FK from `shop.cl_info` fanned out to BOTH
   registrations. 5 of 6 confirmed relations in the dev registry are noise
   from this one root cause: candidate generation never checks whether two
   registrations resolve to the same physical source (connection + schema +
   table).
2. **Wizard suggestions invisible in the empty case.** The block works (both
   `tr_data` FKs suggest correctly) but collapses to one quiet line when there
   are zero suggestions — users conclude the feature doesn't exist. It sits
   mid-way down step 3 with no emphasis and no earlier mention.
3. **Overview readability.** Display names only ("cities dictionary" vs
   "city dictionary" look like inexplicable duplicates), no physical source
   shown, no grouping by table pair, subset/composite variants of one pair
   render as unrelated rows.

## Additional findings from the Phase-1 research pass

4. **(HIGH)** Re-registering an already-registered table is unguarded — the
   step-1 dropdown only appends "(registered)", stays selectable, and nothing
   warns later. This is the mechanism that created finding 1.
5. **(HIGH)** Analyze SQL's zero-result feedback is factually wrong when the
   pasted SQL touches an UNREGISTERED table: it claims "already confirmed /
   excluded from scans" when the real reason is that a join endpoint isn't a
   registered table. The reason is never surfaced.
6. **(MED)** Scan feedback lands at the bottom of the page (below the SQL
   card), far from the top-right trigger, and the toast overlaps it; the
   discovery zone has no heading tying the Scan button to its output.
7. **(MED)** Origin/cardinality chips are unlabeled jargon ("name", "fk")
   with no tooltips in the overview.
8. **(MED)** Pre-checked wizard suggestions are committed under the
   descriptions-review consent checkbox; saving while suggestions are still
   loading silently commits zero of them.
9. **(LOW)** Wizard suggestion rows omit the child table label
   (`client_id → clients info.client_id`), inconsistent with the overview.

Deliberately NOT addressed here (recorded for later): busy-overlay flash on
fast scans; step-1 selection feedback; metadata edits forcing a full
re-snapshot; favicon 404 (pre-existing, site-wide); restoring accepted-time
evidence (overlap %) on confirmed rows.

## TODO plan (prioritized)

Physical identity throughout = `(connection_id, lower(schema), lower(table_name))`.

- [x] **(a) Same-physical exclusion** — candidate generation (scan AND wizard
  suggestions) never proposes a relation whose two sides resolve to the same
  physical source. New-candidate generation only; existing confirmed
  relations are cleaned via (e), never auto-deleted.
- [x] **(b) Fan-out dedupe** — while duplicate registrations exist (legacy
  state), a physical table with multiple registrations yields ONE suggestion
  per (child, physical target, join columns), not one per registration.
  Default-target rule: prefer the registration flagged `is_connector`
  (connectors exist to be auto-included via relations); tie → the earliest
  `created_at`, then id, deterministic. The chosen row carries
  `alternate_targets` so the UI notes the other registration(s) and the
  existing per-row Edit target picker covers the "genuinely ambiguous" case.
  Applied symmetrically (duplicate registrations on the child side collapse
  the same way — same root cause, same fix).
- [x] **(c) Duplicate-registration hard block** — a physical table can be
  registered only ONCE. Server: the save route rejects a duplicate
  (different table id, same physical source) with a clear 400
  `DUPLICATE_TABLE` error naming the existing registration; editing an
  existing registration (same id) stays allowed. UI: step-1 dropdown shows
  registered tables disabled/grayed with "already registered as 'X'" (the
  edited table's own row stays enabled in edit mode). Connector-vs-normal is
  the one legitimate reason for a second registration: verified that the
  edit flow ALREADY toggles the "Connector table" flag both directions
  (wizard step 3 checkbox → `is_connector` in the save body) — pinned with a
  server-side test, no code change needed. Legacy duplicates keep loading
  and working (no auto-delete, no migration); (b) governs suggestions while
  they exist.
- [x] **(d) Overview readability** — rows grouped by table pair (one group
  card per child→parent pair; each key-set variant is a sub-row inside it,
  so exact duplicates and subset/composite overlaps sit together); the
  physical source (`schema.table`, plus the connection name when ambiguous)
  renders beneath the display names.
- [x] **(e) Suspicious-relation badge + cleanup** — a relation whose two
  sides resolve to the same physical table gets a warning badge ("same
  physical table — likely noise") and keeps the existing one-click delete;
  a "Delete all flagged" bulk action appears when any are flagged.
- [x] **(f) Suggestions visibility** — the wizard suggestions block becomes a
  visually prominent panel, always rendered; the zero case keeps the panel
  with "No suggested relations found — suggestions appear when this table's
  foreign keys or column names match another registered table"; step 2
  mentions that relation suggestions will appear at the confirm step.

Additions from Phase-1 research (marked as such):

- [x] **(g, addition — finding 5)** Analyze SQL reports unresolved tables:
  `extract_sql_joins` counts and names join endpoints that are not registered
  tables (`stats.unknown_tables`, physical names only — they come from the
  admin's own SQL and never leave the client); the zero-candidate message for
  SQL analysis explains "N table(s) in the SQL are not registered: …" instead
  of the misleading "already confirmed" line.
- [x] **(h, addition — findings 6+4)** Discovery zone gets its own headed
  card ("Proposed candidates") placed directly below the confirmed list and
  ABOVE the SQL card, so scan output appears where the eye is; the scan
  feedback line lives inside it.
- [x] **(i, addition — finding 7)** Origin and cardinality chips carry
  explanatory tooltips everywhere they render (overview, candidates, wizard).
- [x] **(j, addition — finding 8)** Saving the wizard while suggestions are
  still loading is blocked with a toast (sub-second window; prevents
  silently committing zero of the pre-checked rows).
- [x] **(k, addition — finding 9)** Wizard suggestion rows name the child
  table on the left side, matching the overview syntax.

## Constraints honored

No changes to scan scoring thresholds, sqlglot parsing, accept/dismiss/delete
semantics beyond the plan items, snapshots, scheduler, schema_text, or user
pages. No brain calls. Single-registration behavior byte-identical.

---

# v3 — structured editor, missing-table hints, relations graph

## Baseline findings (research pass on the verified-fresh stack)

1. **(Critical) Wizard save persists join-key typos silently** — the free-text
   editor accepts `client_idd=client_id` and `save_table` stores relations
   verbatim while `/relations/accept` validates columns: the same typo is a
   400 in the inline editor and silent corruption via the wizard.
2. **(High) Column names must be typed from memory** — neither side's column
   list is on screen at the relations step; no pickers, no dtype hints;
   malformed text (`a b` without `=`) silently drops the whole row.
3. **(Critical) The scan says nothing about unregistered FK targets** —
   `tr_data`'s live FK to unregistered `prod_dict` appears nowhere; a
   forgotten dictionary table is invisible.
4. **(High) Dangling relations render as raw 16-hex ids** — deleting a
   registration leaves its inbound relations "confirmed", labeled with the
   dead id, counted in the badge, never flagged.
5. **(Med) SQL unknown-table message is honest but a dead end** — no way to
   act on "register them first".
6. **(High) No structure is readable from the flat list** — hubs, isolated
   tables, and clusters are invisible; at 20 tables it would be unreadable.
7. Inline-editor validation errors are transient API-speak toasts; a failed
   accept leaves no audit row; suggestion rows for structurally-verified
   (numbers-dropped) candidates show no evidence chip.

## v3 items

- [x] **(A) Structured pair editor** `createPairEditor` (paired column
  dropdowns with dtypes shown, per-row remove, "+ add column pair",
  non-blocking dtype-family warning mirroring the verification rule, missing
  registry columns rendered "<name> (missing)" in invalid style — never
  dropped) replacing free text at ALL manual surfaces: wizard step-3 rows,
  overview inline edit, candidate inline edit, and the NEW overview
  "+ Add relation" (child+target pickers, saved via accept, renders as
  manual). Accept-backed editors disable Save while a missing pair remains
  (accept 400s on unknown columns); the wizard path stays non-blocking.
  `parseJoinKeys` deleted. Storage format and API payloads unchanged.
- [x] **(B) Missing-table hints**: scan response gains `unregistered_refs`
  (pure `unregistered_fk_refs` — connection-scoped, schema falls back to the
  child's) rendered as "Referenced but not registered" rows; analyze_sql
  gains `unknown_table_hints` (pure `resolve_unknown_tables` — hint only on
  an unambiguous connection, never for already-registered names) rendered
  with the same shortcut. **Register as connector** opens the wizard
  prefilled (connection/schema/table via the loadSchemas path,
  case-insensitive table match, connector ticked inside introspectNow);
  nothing auto-registers. Just-registered ghosts drop from the list at
  render without a re-scan.
- [x] **(C) Graph view** (List | Graph toggle in the Relations head, List
  default): vendored **Cytoscape.js 3.34.0 (MIT)** at
  `static/vendor/cytoscape/` (license sidecar; lazy-loaded on first
  toggle). Pure `build_graph` (components via BFS, isolated flags, connector
  + relation-count styling data, suspicious same-physical edges, legacy name
  refs resolved to the preferred registration, dashed ghost nodes/edges)
  behind `POST /api/admin/relations/graph` — the one new endpoint, required
  by the pure-logic test mandate. Distinct component colors, red isolated
  nodes, ⚙ connector badge, legend; edge tap → popover with Edit (hands off
  to the List row's structured editor) / Delete; node tap → neighbor
  highlight; ghost tap → the register shortcut; cose layout;
  `cy.destroy()` on toggle/section switch.

Additions from the research pass (marked as such):

- [x] **(v3-d, addition — finding 1)** `save_table` logs
  `REL_SAVE_UNKNOWN_COLUMN` when a posted relation references a column absent
  from the posted/registered metadata — observability for the wizard path
  WITHOUT changing the frozen API contract (rejecting would break payloads
  the API must keep accepting; the structured editor removes the UI-side
  cause).
- [x] **(v3-e, addition — finding 4)** Overview entries whose
  `related_table_id` no longer resolves are flagged "target registration
  deleted" in warning style (no more raw hex labels) and keep one-click
  delete; the graph already skips them (logged).
- [x] **(v3-f, addition — finding 7)** Failed `/relations/accept` validation
  now writes an `ok:false` audit row (parity with `admin.denied`).

Deferred (recorded, out of v3 scope): evidence upgrades for already-confirmed
edges (frozen candidate rules); auto re-analyze after registering from an SQL
hint; snapshot-free metadata edits; favicon 404; richer near-miss suggestions
in validation errors.

---

## v4 — persistent SQL-derived table recommendations (this iteration)

### Baseline findings (research pass, verified in-browser on the HEAD stack)

1. **Nothing persists.** Scan ghosts (`unregistered_refs`) and SQL unknown
   names live in page-scope JS arrays; an F5 wipes every trace, and the graph
   can only ever show ghost nodes in the same session as a scan (the legend
   still advertises them — mildly misleading after reload).
2. **Join evidence is thrown away.** Predicates touching an unregistered
   table are dropped at extraction; only the bare table name survives. After
   registering the table the admin must re-paste the SQL to rediscover its
   relations.
3. **Duplicate rows.** The FK-ghost list and the SQL-unknown list do not
   dedupe against each other — one missing table could render two rows with
   two Register buttons (confirmed in code; both lists share row class and
   action).
4. **SQL hints are not existence-verified.** A typo'd table name gets a
   Register shortcut that dead-ends in the wizard.

### Design decisions

- **Merged block (decision recorded per the task):** ONE persistent
  "Recommended tables" block replaces the session FK-ghost list, absorbing
  FK- and SQL-derived hints — one row per physical table, both source badges,
  combined evidence. The SQL-unknown list remains ONLY for names that cannot
  become recommendations (no resolvable connection, or no join evidence),
  filtered against the recommendation rows so no table shows twice
  (fixes finding 3).
- **Storage:** additive top-level `recommendations` section in
  `data_sources.json` (`_default_doc` + `read_doc` both updated — read_doc
  whitelists sections). Rec doc: physical identity + status
  (open/dismissed/registered) + `prior_status` + sources + accumulated
  frequency + evidence entries `{origin, other (physical names, no id
  hints), pairs (rec-table column FIRST — orientation fixed by
  construction), count}`. Identifiers and counts ONLY; the privacy grep test
  pins that no SQL text/literals persist.
- **Extraction:** additive `stats["unregistered_joins"]` (per-edge,
  per-statement-deduped) + `stats["unregistered_tables"]` (accurate distinct-
  statement count per table) — not a return-shape change (11 test unpackings
  pinned the 2-tuple, and the existing SQL-marker privacy test covers the new
  keys for free). Both-sides-unknown predicates emit two anchored records.
  Cap: 20 distinct unregistered tables per analyze (logged).
- **Statuses & lifecycle:** a store-level reconcile hook runs inside every
  registry mutation (upsert, delete, connection cascade): any registration
  path flips matching recs to `registered` (remembering `prior_status`); a
  vanished registration reverts to `prior_status` — a dismissed rec can never
  resurrect via Accept-rollback or table deletion. Connection delete drops
  its recs entirely.
- **Instant Accept:** one server-side route reusing the exact existing
  pieces — introspect → `_draft_table_descriptions` (the draft route's
  mechanism, extracted, not forked) → `_build_table_doc` (save_table's doc
  shape, extracted) → `upsert_table` → `refresh_one_table`. On snapshot
  failure the registration is DELETED (rollback; unlike the wizard save,
  which keeps it for "Refresh now" — Accept is one atomic gesture) and the
  rec reverts to open. The UI confirm dialog is the review act; it states
  descriptions are AI-drafted and editable later.
- **Close the loop:** after registration the stored SQL evidence replays
  through the NORMAL candidate pipeline (`recommendation_candidates` → the
  same filter chain + `_verify_and_band` as analyze_sql) — instantly on
  Accept, on the next scan for other paths. FK evidence is deliberately NOT
  replayed: the next scan's live introspection re-derives it as ground truth
  (and replayed candidates never carry `fk` in sources, so banding's
  fk-auto-confirm cannot fire — proposed, never auto-confirmed).
- **Role:** bridge/referenced computed at read time from the current
  registry (a stored role would go stale); bridge rows first, then by
  frequency.
- **Graph:** the route unions server-side OPEN recs (dismissed excluded)
  with any body-passed refs (param kept for compatibility; the frontend
  stops sending it); recommendation evidence renders as keys-labeled dashed
  edges (ghost→registered and ghost→ghost), FK evidence as the classic
  child→ghost edges.
- **Privacy wording** (SQL box, minimal + truthful): "SQL is parsed in
  memory on this server only — the SQL text is never stored, logged, or sent
  anywhere. Only the table and column names found in it are kept on this
  server to recommend missing tables." Mirrored in the module/route
  docstrings and Article VII rule 10.
- **Typo'd tables** (finding 4, recorded): recommendations are still not
  existence-verified at creation (that would need live DB calls inside
  analyze). Accept's introspect surfaces a nonexistent table with a clear
  error and the rec stays open; Dismiss is the cleanup. Deliberate.

### Deferred (recorded, out of v4 scope)

Existence pre-verification of recommended tables; recommendation rows for
bare-ambiguous names (multi-connection registries); wording alignment of the
scan button labels; favicon 404 (pre-existing).

---

## v4.1 — evidence-replay bugs: root causes (recorded BEFORE fixing) + evidence UX

### Reproduction method

Both bugs were reproduced offline against a byte copy of the LIVE dev store
(the state the user's session left behind), plus three discriminator SQL
variants for the missing-evidence observation — no guesswork.

### BUG A — bogus candidate "city dict.city_code → tr data.city_code"

**Root cause: stale invalid evidence + a missing column-validation layer. NOT
cross-table mis-attribution.** The live store's city_dict recommendation
(created in an earlier session) still carried evidence
`{origin sql, other tr_data, pairs [["city_code","city_code"]], count 1}` —
because THAT session's pasted SQL genuinely contained the wrong join
`t.city_code = ci.city_code` (tr_data has no city_code). v4 attribution
followed the SQL faithfully; nothing ever crossed wires, and accept order is
irrelevant. The defects are: (1) `recommendation_candidates` replayed stored
evidence with NO column validation once the partner table's columns became
known; (2) analyze time never validated even the REGISTERED side's columns.
When the user re-registered tr_data, the scan's registered-rec replay
resurrected the stale pair as an unverifiable candidate. Replay of the rec
reproduced the exact bogus candidate; the user's own recs contain only
correct client_id/city_code evidence — their SQL was right.
**Honest limit:** when wrong SQL names a column that exists on BOTH tables,
no validation layer can flag it (the pair is genuinely valid to every layer)
— that case is mitigated by the analyze-time report + caution wording, and a
byte-identical guard test pins that valid pairs are never over-filtered.

### BUG B — toast "relations[0]: column 'city_code=city_code' not found"

**Root cause: v1-era message formatting in the accept validation (predates
v4; fixed where it lives).** routes/admin_data.py formatted the per-pair
column check failure as `column '{a}={b}'` — fusing a well-formed
[child_col, parent_col] pair into what reads as a single column token, plus
leaking `relations[idx]` internals. There is NO serialization mismatch:
replay-produced candidates are built by the SAME `_make_candidate`
constructor as scan-produced ones (verified + now pinned by an
analyze→persist→replay→accept round-trip test).

### Finding — silent predicate drop for computed CTE projections

The user's tr_data↔prod_dict joins left no evidence anywhere. Discriminators
(same 5-statement base + one CTE variant each): a join INSIDE a CTE body and
an outer join through a PLAIN-projection CTE alias both persist evidence; an
outer join through a COMPUTED projection (`MAX(t.product_id) AS product_id`)
silently drops the whole predicate — `_resolve_column` returns None for
computed projections and the pair vanishes without a trace (not even into
`unknown_tables`; `failed` stays 0). The `_persist_sql_recommendations`
table_id branch was ruled out (dead defensive code — ids are minted and
consumed from one tables list inside a single request; a log line was added).
Fixing the resolution semantics is out of scope (frozen extraction
semantics); v4.1 adds only the additive observability counter
`stats["unresolved_predicates"]` so the drop is at least visible, and records
this as a KNOWN LIMITATION: joins through computed CTE/subquery projections
contribute no evidence.

### v4.1 fixes (validated design)

- Analyze-time validation inside extraction (where the statement index
  lives): any predicate side resolving to a REGISTERED table has its column
  checked (CASE-INSENSITIVELY — qualify normalizes case on the happy path
  but the qualify-fallback preserves raw SQL casing; reporting keeps the
  SQL-side spelling, never canonicalizes) against registry metadata. Invalid
  → the pair is skipped (candidate AND evidence; composite ONs keep their
  valid pairs) and reported in additive `stats["invalid_column_refs"]`
  `[{statement (Nth ANALYZED statement), table, column}]`.
- Replay-time validation — pure `validate_rec_evidence(rec, tables)` — for
  evidence sides unknowable at analyze time: pairs naming columns the
  now-known registration lacks are skipped and surfaced (warn box + log),
  never a bogus candidate, never silent. Corrupted existing stores are
  handled by exactly this path; no migration.
- Accept rejection message names the table and the missing column, no list
  internals. Semantics (400 + ok:false audit) unchanged.
- Warnings surface: `#relWarnings` (a NEW sibling of #relDegraded — that box
  is scan-owned and overwritten wholesale) with "the script may be outdated
  or wrong — treat its evidence with caution" wording, fed by analyze's
  `invalid_column_refs` and scan/accept's additive `evidence_warnings`.

### v4.1 evidence UX

- Recommendation rows always render FULL join evidence: unresolved partners
  are now listed with a "not registered" tag instead of being dropped from
  the summary (role still counts REGISTERED partners only — the bridge
  semantic is "registering it connects ≥2 registered tables").
- Locked "pending relations" preview INSIDE each recommendation row (data is
  strictly per-rec; a separate block would duplicate names, need its own
  empty state, and desync on show/hide-dismissed): SQL-origin evidence only
  (replay never proposes FK evidence — scan re-derives it live), rows like
  "cl_info.city_code → city dict.city_code — pending: register shop.cl_info",
  lock glyph, muted, non-interactive; invalid-at-replay pairs excluded.
  Rendered from stored evidence at read time — identifiers only, nothing new
  persisted. When the blocker registers, the same (now validated) replay
  turns them into normal candidates — same pipeline, no fork.

### v4.1 deferrals (recorded)

- `/relations/accept` keeps its EXACT-match column check by design: stored
  join_keys are consumed downstream by exact-match code (connector closure,
  verification, schema_text), so accepting a case-mismatched pair verbatim
  would trade a visible 400 for silent downstream misses. The
  analyze/replay validators are case-insensitive for REPORTING only. A
  candidate minted through the qualify-fallback path against a mixed-case
  registry can therefore still 400 at accept — now with a readable message
  naming the column.
- The warning box is per-response (a later warning-free scan/accept clears
  it); per-rec warn chips persist the signal.
- Evidence-line partner order follows stored-evidence order.


---

# v4.2 — time-bounded Accept, table-type classification, real build stamp

## The Accept hang — what the investigation actually found

**Symptom (live testing, 2026-08-02).** Accept on the `shop.city_dict`
recommendation spun for over 60s: two spinners, no error, no timeout, the
recommendation never resolved.

**Finding 1 — the hang left NO log line, and that is the root finding.**
Nothing on the accept path logs until a dependency RETURNS: `BRAIN_OK`,
`DB_SNAPSHOT_OK`, `DB_INTROSPECT_FAILED` are all completion events. A request
blocked *inside* a dependency that has not yet timed out is therefore
invisible — the app log for the window contains no accept-related line at
all. Post-hoc attribution was impossible (the container was recreated at
11:17, killing any in-flight request). v4.2 adds `REC_ACCEPT_PHASE` entry
logging so the next occurrence names its own wedged phase.

**Finding 2 — only one dependency could have exceeded 60s.** Measured from
the same log: every completed accept took draft 1.2–1.8s and snapshot
0.02–1.4s. The test Postgres (`pdc-test-db`) was up for 20 hours across the
window, and its connects are bounded at `DB_CONNECT_TIMEOUT` (8s). The brain
is remote (Cloud Run) and `brain_client._post` passed **no per-call timeout**,
so the draft rode the client-wide `BRAIN_REQUEST_TIMEOUT` — **180s**. That is
the only unbounded-enough path, and it is the prime suspect; recorded as such
rather than as proof.

**Finding 3 — three more unbounded points, fixed while in here.**
- `_run` has no deadline, and executor threads are not cancellable, so a hung
  accept permanently consumes 1 of the 4 `db_admin` workers.
- The **oracle** dialect carried no `connect_args` at all — its connect fell
  back to the OS TCP timeout (~127s on Linux), the one dialect that could
  outlast a click on its own.
- The admin page's `api()` helper had no abort and a real bug: a rejected
  `fetch` left `r` undefined, so the caller's `r.data.ok` threw a TypeError
  and the user got **no message at all**.

## Design decision — bound each dependency, not the whole gesture

No `asyncio.wait_for` around `_run(_register)`. Executor threads cannot be
cancelled: an outer deadline would report failure to the browser while the
registration continued in the thread — precisely the half-registered state
Accept exists to prevent. Each dependency is bounded at its own seam instead
(httpx per-call timeout; driver connect + statement timeouts), so the worker
thread itself can no longer hang unboundedly, and the existing snapshot
rollback stays the single abort path.

Timeout budgets: `BRAIN_DRAFT_TIMEOUT` 60s (new, env-tunable) for the draft
only — upload autofill keeps the 180s default, unchanged. Client-side:
15s on the classify probe, a "still working" message at 20s, and a 390s hard
abort (60s draft + 300s statement + margin). Aborting only ends OUR wait, so
that toast says the server may still finish and the list is refetched rather
than asserting failure.

Timeout-shaped driver errors become one sentence ("Database connection timed
out." / "Database snapshot timed out.") via `_friendly_db_error`, which walks
the whole `__cause__`/`__context__` chain because each of the five drivers
words it differently (psycopg2 "timeout expired", pyodbc `HYT00`, oracledb
`DPY-6005`, …). Anything not timeout-shaped keeps its scrubbed driver text —
no error is ever replaced by a guess, and a wrong password can never be
reported as a timeout.

## Table-type classification — why it is deterministic, not AI

One-click Accept hard-coded `is_connector=True`. That is right for a pure
junction table and wrong for a content table like `prod_dict`
(`product_name`, `category`): registering it as a connector hides it from the
user picker entirely.

**Investigated first, per the task: the AI route is not available from this
repo.** The schema-autofill prompt is built entirely brain-side
(`PDC_Brain/routes/llm.py:_build_combined_autofill_prompt`), the brain's
response parser hard-drops any key outside `file_description`/`columns`, and
no generic completion endpoint exists on the `/v1` surface. The only
free-form field the client sends (`notes_text`) reaches the prompt but cannot
widen the response contract. An AI-suggested `{suggested_type, reason}`
therefore **requires a brain-side change**, which this task forbids — so v4.2
ships the deterministic client-side classifier and the AI version stays
commissionable separately.

`relation_discovery.classify_table_type(columns)` — pure, never raises:
a column is key-like when its NAME equals/ends with `id|code|key|no|num`
**and** its dtype is an integer family or a varchar of ≤32 chars. Both halves
are required: `VARCHAR(4000) note_code` is free text, `INTEGER amount` is a
measure. Every column key-like → `connector`; otherwise `normal` with the
deciding columns named. Empty/odd input → `connector` with "could not
classify — defaulted to connector". Deliberately does NOT consult PK/FK/row
count (a richer signal set is a future option, not this change).

The suggestion is never applied by itself. The native `window.confirm` is
replaced by an in-page dialog (the existing narrow-modal chrome) that states
the descriptions are AI-drafted and carries the Connector/Normal choice
defaulted to the suggestion with its one-line reason; a manual pick wins over
a suggestion that lands afterwards; the Register button is never gated on the
probe. "Edit first" pre-ticks the wizard from the same suggestion (served
additively by `tables/introspect`, no extra round-trip) instead of assuming
connector. The audit row records `suggested_type` AND `chosen_type`, so a bad
suggestion is distinguishable from a deliberate admin choice.

## Build stamp

The static `?v=` parameter is `int(time.time())` computed per request at page
render. It looks like a build marker and is not one — it sent build
verification down the wrong path twice. `GET /version` +
`{{ build_stamp }}` in the admin sidebar now report the real thing, fed by
`BUILD_COMMIT`/`BUILD_TIME` Docker build args (`.git` is dockerignored, so
the commit can only come from the builder). An unstamped image reports its
process start time rather than faking an identity. The `?v=` behavior is
unchanged; the old hardcoded `CLIENT_BUILD marker=` startup log now carries
the same commit/time.

Verification workflow going forward: check `GET /version` after a rebuild —
not file hashes.

## Deferrals / known limits (v4.2)

- The classifier reads names + dtypes only. `INTEGER amount` on an
  all-numeric fact table can still read as connector-ish only if it is also
  named like a key; PK/FK/row-count signals are deliberately unused.
- `suggested_type` is trusted from the dialog for the audit row (enum-
  validated). Recomputing it server-side would cost a second introspection
  and cannot change the registration.
- The 390s client abort ends the wait, not the server's work; the toast says
  so explicitly.
- The oracle connect bound ships tested at the dict level only (kwarg
  `tcp_connect_timeout` verified against the pinned oracledb 2.5.1 in the
  running image; no Oracle instance exists in the local stack). The
  CLICKHOUSE bounds, by contrast, were verified against a live ClickHouse
  24.8: a blackhole host returned in 4.3s on a 4s bound (vs the ~127s OS
  fallback), and a 2s `max_execution_time` killed a long query at 2.0s with
  `Code: 159. … Timeout exceeded`, which `_friendly_db_error` maps. Both ride
  in the URL QUERY — the native dialect discards `connect_args` outright, and
  a session `SET max_execution_time` is a measured no-op there (the driver
  re-sends its own settings per query), which is why the dialect deliberately
  has no `apply_stmt_timeout`.
- ClickHouse's `system.tables.total_rows` / `total_bytes` are NULL for View /
  Distributed / Merge / Log engines (measured on 24.8: a MergeTree table
  reports both, a View reports neither). `introspect` only records `degraded`
  when the estimate query RAISES, so such a table shows a blank row count
  rather than "unavailable" — the same behavior MySQL views already have, so
  it is consistent rather than new, just more common here.
- ClickHouse has no FK metadata, so FK-based relation discovery yields nothing
  for its tables; name / description / pasted-SQL candidates still work
  (`SQLGLOT_DIALECT` maps it to sqlglot's `clickhouse`). Relatedly,
  `classify_table_type`'s short-varchar rule needs a DECLARED length and
  ClickHouse `String` has none, so a ClickHouse dictionary table keyed on
  `String` columns suggests "normal" rather than "connector" — integer keys
  (`UInt64`, …) classify correctly via `py_type == "int"`, and the admin flips
  the type in the Accept dialog either way. Not special-cased on purpose:
  `_VARCHAR_DTYPE_RE` is shared with every other dialect.
- Blocking store reads still run on the event loop at two spots in the accept
  route (`list_tables` before and after `_register`) — pre-existing, out of
  scope.
- The classify probe costs one live introspection on the 4-worker
  `_DB_EXEC` pool. Mitigated, not eliminated: the browser caches a
  successful classification per recommendation for the session (a failure is
  never cached — the next open retries), so repeated dialog opens cost
  nothing. Against a slow database, several *first* opens can still occupy
  workers; the abort ends the wait, not the thread.
- `_TIMEOUT_PAT` is anchored on timeout PHRASES rather than the bare word,
  because postgres embeds `statement_timeout=…` in every DSN and an
  unrelated connect error echoing it would otherwise be relabelled a
  timeout (a regression test pins exactly that string). Oracle's `DPY-6005`
  is deliberately NOT treated as a timeout — it is the generic "cannot
  connect" (listener down, refused, bad DNS). A driver phrasing the list
  misses simply keeps its scrubbed driver text, which is still truthful.
- Key-suffix matching requires a whole TOKEN when the column name is
  separated (`is_valid` is a flag, not a key) and falls back to a plain
  suffix test for unseparated names (`clientid`). An unseparated flag name
  can still read as key-like — the admin sees the reason and can flip it.
- The register wizard's draft banner claims "Only column names and types were
  shared with the AI, never row values", but `_draft_table_descriptions`
  sends the same truncated sampled values file uploads do (documented,
  deliberate parity). The wording is inaccurate and should be corrected in a
  separate change — it is not a leak, it is a labeling bug.


---

# v4.3 — the graph reads as an ER diagram

## What was wrong

The v3 graph rendered generic network blobs. Live testing named five concrete
defects:

1. Nodes were solid circles with labels OUTSIDE, below them — labels collided
   with edges and with each other.
2. The mid-edge label fused join columns and cardinality into one rotated
   string that truncated ("city_code · ma…").
3. Cardinality was therefore unreadable at a glance, and the direction arrow
   drowned in the label noise.
4. The cluster color filled the whole circle, so a single-cluster registry —
   the normal case — was a field of identical blue blobs, and the
   connector/ghost distinctions were lost inside it.
5. `cose` (force-directed) produced crossings and no hierarchy.

Database relations have an established visual language (Power BI model view,
dbdiagram.io, DBeaver, draw.io): entities as rectangular cards with the name
INSIDE, cardinality read off markers at the LINE ENDS, layered layouts. v4.3
adopts it. Rendering only — `build_graph`'s existing keys, every endpoint, the
List view, the popover Edit/Delete semantics, ghost/register behavior, and the
warning semantics are unchanged.

## Cards, accents, ends

- **Table cards**: round-rectangle sized to its text (`width/height: 'label'`,
  10px padding), display name on line 1 and `schema.table` on line 2, both
  INSIDE the box. Connector tables carry a double border + ⚙; ghosts are
  dashed, muted, and still click-to-register.
- **The cluster color is a border ACCENT, never a fill.** That single change
  is what makes connector/ghost/isolated styling legible again — the fill was
  swallowing all of it. Isolated tables keep the red accent.
- **Cardinality moved to the line ends**: `N` text at the many end, and at the
  "one" end the bar is fused into the direction arrow (`triangle-tee`) so one
  glyph carries both direction and "exactly one". Unknown cardinality — which
  is EVERY ghost edge, since evidence is never measured — renders a plain
  directed line with no end markers at all rather than implying a guess.
- The mid-edge chip therefore carries the join COLUMNS only, horizontal
  (`text-rotation: none`) on a white round-rect background: `city_code`, or
  `cust_id = id, region` for a composite. A pair of identically-named columns
  prints once — the `city_code = city_code` echo was pure noise on a diagram.
  Full details (cardinality wording, origin, both table names) still live in
  the click popover, unchanged.

## Where the decisions live

`edge_label` and `edge_end_markers` are pure functions in
`relation_discovery.py`, feeding three ADDITIVE edge fields (`label`,
`source_marker`, `target_marker`) from all four edge-construction sites via
`_er_edge_fields` — so the four sites cannot drift. This satisfies the
unit-test mandate (rendering config is only testable in a browser; these
decisions are testable offline) and it retired the client-side caption
fusing that was an untested seam. (The cardinality-WORDING map stays in the
JS — the click popover's chip still spells out "many-to-one".)
`keys_label` and every pre-v4.3 key are untouched, which is what keeps the
four tests that pin its exact strings green.

Marker orientation is the one subtle bit: graph edges run child → parent and
`cardinality` reads child:parent, so `N:1` puts the MANY end at the source.

## Layout and controls

Layered left-to-right via vendored **dagre 0.8.5 + cytoscape-dagre 2.5.0**
(both MIT, license sidecars, `static/vendor/` — never a CDN; dagre's dist
bundles graphlib, so no fourth script). LR was chosen over top-down because
text-sized cards are wide: ranks become tidy vertical columns, the edges run
mostly horizontally, and horizontal edges are what the horizontal key chips
need. `rankSep` is 110 to leave room for those chips.

**The layered layout is an enhancement, not a dependency.** A load failure of
either extension, or a throw inside the layout at run time, falls back to the
built-in `breadthfirst` with a console warning — the admin keeps the graph
(Article IV). Only a failure of the Cytoscape CORE still rejects, which is the
pre-existing "could not load the graph library" path.

Added: zoom in / zoom out / Fit buttons in the graph subhead, and
`minZoom`/`maxZoom` 0.2–2.5. Wheel zoom and node dragging are Cytoscape
defaults and stay. The click popover is positioned in card coordinates, so it
used to drift away from its edge when the canvas moved; it now hides on
pan/zoom/node-drag — the honest fix, since re-anchoring it every frame buys
nothing the popover needs.

## Deferrals / honest limits

- **Uniform card typography.** Cytoscape canvas labels take ONE font, size and
  weight per node, so the two card lines cannot differ in weight/size. The
  alternatives were vendoring an HTML-label overlay (a third library, plus
  overlay/event and `.dim`-mirroring friction) or generating per-node SVG
  backgrounds (DIY text metrics, blurs when zoomed). Chosen deliberately by
  the user: keep one font, no new library.
- Node positions are still not persisted, and every data change rebuilds the
  instance — so a hand-arranged layout does not survive an edit. Unchanged
  from v3 and out of scope here.
- No export-to-image, no column-level edge anchoring (cards are table-level;
  join columns live on the edge label and in the popover).
- `relation_count` is no longer read by the renderer (node size was the only
  consumer). It stays in the contract — the List view and any future consumer
  may still want it, and removing a contract key is not additive.
- **`width/height: 'label'` logs a deprecation warning on every render** and
  is kept deliberately. It is what auto-sizes a card to its text using real
  font metrics; the alternative is measuring the label ourselves, which would
  mis-size non-Latin table names (this deployment has Georgian-speaking
  customers). Cytoscape is vendored at a pinned 3.34.0, so the value cannot
  disappear underneath us — an upgrade would be a deliberate act that
  revisits this.

## Caught in browser verification (recorded so it is not repeated)

The first build of this rework constructed the graph with
`layout: { name: 'preset' }`, intending the guarded layered layout to run
immediately afterwards. With no `position` on any element, `preset` parks
everything at (0,0) and the renderer caches that degenerate zero-area state:
the later layout assigned correct positions, but **most cards and every edge
were never painted** (2 of 5 cards, 0 of 6 edges on real data; 14 of 29 and
0 of 24 on the synthetic set). Nothing in the model looked wrong —
`ele.visible()` reported true — and the elements only appeared after an
unrelated class mutation. The constructor now runs `grid` (built-in, cannot
throw) so positions are always real before first paint, and the guarded dagre
run replaces them. **A cytoscape instance must be constructed with a layout
that actually positions its elements** — this is exactly the class of defect
that unit tests cannot see and only a browser pass catches.
