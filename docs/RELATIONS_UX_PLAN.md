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
