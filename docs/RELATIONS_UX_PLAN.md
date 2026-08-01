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
