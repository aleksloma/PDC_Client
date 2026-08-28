# PowerDataChat — Enterprise (On-Prem) Architecture

> Decisions document for the enterprise edition. Reference: the agreed
> architecture that the brain/client split is built against. Things that
> were originally marked **OPEN — DO NOT ASSUME** but have since been
> decided are noted inline under "Resolved" lines.

---

## 1. Core idea

The enterprise version splits the existing (B2C) single application into
TWO separate parts:

- **Client container** — runs in the company's own LAN. Holds everything
  sensitive: raw data, calculation, the frontend, chart rendering, and the
  company's presentation templates.
- **Brain** — runs on PowerDataChat's GCP (a separate project from the B2C
  service). Holds the "intelligence" / IP: the skill engine, skill and
  model selection, and LLM-based code and summary generation.

The sensitive part stays on the client side. The intellectual-property
part stays on the brain side. This split is the whole point of the
enterprise design.

---

## 2. What lives where

### Client container (company LAN — sensitive side)
- Data upload and storage. Raw data values never leave the LAN.
- Python execution engine. Runs the generated code against the raw data
  locally. Results stay local.
- Chart rendering (kaleido) and final file rendering (python-pptx). These
  are NOT LLM steps and they stay client-side because they touch raw data
  and/or the company's template.
- Frontend (the `/lab` dashboard, copied verbatim from B2C).
- Presentation templates (the company's branded templates / decks).

### Brain (PowerDataChat GCP — enterprise only)
- Skill engine + skill library (the IP).
- Skill selection and model selection (routing logic).
- Code generation and summary/narrative generation (the LLM calls).
- Per-tenant configuration (see §5).
- Operator admin panel (see §6).

### Internal demo instance (exception, not a customer topology)

PowerDataChat additionally hosts ONE demo/showcase client instance on Cloud
Run in its own GCP project (`pdcclient-demo`), used for business meetings. It
runs the standard, unmodified client image under a dedicated demo tenant and
holds only PowerDataChat's own demo data, so the customer data-boundary model
above is unaffected — no customer's raw data is ever on that instance.
Runbook: `PDC_Client/docs/DEMO_CLOUD_RUN.md`.

---

## 3. Separation from the existing B2C instance

The enterprise brain is a SEPARATE service from the existing B2C instance —
a different deployment with a different auth model (tenant tokens rather
than user sessions). They may share libraries/code, but they do NOT run as
the same live service. An enterprise issue must not be able to take down
B2C, and vice versa.

---

## 4. Multi-tenant brain (NOT one instance per company)

There is ONE shared, multi-tenant brain for all enterprise companies — NOT
a separate brain instance per company. Each company is a tenant identified
by a tenant token. The brain loads that tenant's customization by tenant ID.

**Reason:** per-company instances create deployment, logging, secret, and
update sprawl. A shared multi-tenant brain keeps a single code path and a
single update rollout while still giving each company its own behavior via
config.

**Exception (kept in back pocket, not the default):** a truly isolated
per-company brain instance is only justified if a specific client
contractually requires physical processing isolation and pays for it. Do
not build this unless asked.

---

## 5. Per-tenant customization is data, not code

Company-specific behavior (which skills are enabled, domain vocabulary,
prompt tuning, model choices, SMTP, sharing-domain allowlist, application
settings — `max_files` / `title_max_len` / `title_break_min` —
`welcome_language`, etc.) is per-tenant CONFIGURATION loaded by the brain
by tenant ID. It is data, not code. Adding a new company should be adding
configuration, NOT editing the skill engine.

`welcome_language` sets the language of the auto-generated welcome message +
suggested starter questions for that tenant (e.g. `"Georgian (ქართული)"` for
the bank). The **client no longer forces a language** — it only sends the
detected language as a hint; the brain applies the tenant's
`welcome_language` override on top (precedence: tenant config → client hint →
English). Unset = today's behavior (client-detected → English). See
[`PROTOCOL.md`](PROTOCOL.md) `/v1/chat_metadata`.

The skill ENGINE and skill LIBRARY (definitions, selection logic,
code-generation templates) live on the brain and are shared across all
tenants.

**Domain SKILLS are shared brain assets, not per-tenant data.** Domain
skills (the YAML files under `brain/skills/domain/`) are code/config that
lives on the brain and is reusable across every client in a similar
domain. The admin portal lets operators author a new domain skill once
and select it from any tenant — there is no per-tenant copy. Only raw
client DATA stays client-side; the skill definitions themselves are
shared. This is consistent with §2: skill IP belongs on the brain.

Presentation TEMPLATES are client-side (they are the company's branded
assets). The brain never holds or sees the rendered file or the template —
it only ever produces structured content.

**Resolved:** per-tenant config lives at
`<BRAIN_STORAGE_ROOT>/tenants/{tenant_id}/config.json`. Loaded by
`tenant_store.effective_settings()` in `brain/tenant_store.py` and read at
each `/v1/*` call via the `_TENANT_CTX` contextvar in
`brain/brain_agent.py`.

---

## 6. Admin panel is operator tooling (for PowerDataChat, not the client)

The company-admin page lives on the brain and is for PowerDataChat to
operate the business — NOT for the client. It is used to:

- create / suspend / revoke a tenant,
- view per-tenant user counts,
- view usage volume,
- see which models are being used,
- rotate tenant tokens,
- edit per-tenant overrides (model tiers, API key, SMTP, allowed sharing
  domains).

The client never touches this panel. It is gated by a separate admin
session at `/admin/login` and is configured at first boot via
`ADMIN_DEFAULT_PASSWORD`.

---

## 7. Kill-switch (non-payment)

**Default (soft) kill-switch:** revoke the tenant token / disable the
tenant record. Every `/v1/*` call from that company's client server then
returns HTTP `403`. The client surfaces a single SSE error event to the
chat UI. This is the simple default and does not require literally
stopping a server.

**Hard option (stopping a dedicated GCP instance):** only applies to the
contractual-isolation exception in §4. Not the default.

**Resolved:** implemented in `brain/routes/llm.py:_check_tenant()` —
returns `403 {"detail": "Tenant <status>"}` before any LLM call when
`tenant.status != "active"`.

---

## 8. Normal query flow

1. User asks a question in the client frontend.
2. Client sends `{question, schema_text, df_names, history_rows,
   common_fields, user_email}` to the brain. `history_rows` are sanitized
   in the `brain_client` wrappers (`_sanitize_history_rows`) down to
   `role`/`content`(+`code`) — the extra fields persisted locally for
   conversation reload (`image_base64`, `chart_data`, `table`, `usage`, …)
   never leave the client. Column names going to the LLM is acceptable
   and is covered in the client agreement. **Raw data VALUES are never
   sent.**
3. Brain selects the skill and model (4-tier hybrid) and loads that
   tenant's config.
4. Brain generates Python code.
5. Brain returns the code only to the client.
6. Client executes the code against the raw data locally via
   `code_exec.safe_execute`. The result stays local. Before EVERY
   execution (both exec sites: `code_exec.safe_execute` and
   `plot_utils.render_plot_safe`) the Article XIII sanitize gate
   (`exec_sanitizer.sanitize_for_execution`) normalizes the dataframes to
   plain standard dtypes — generated code must never observe category /
   sparse / extension dtypes (a categorical dimension column once made a
   two-key groupby emit the cartesian product of all categories and put
   every category on a chart axis). Storage-layer optimizations (numeric
   downcasts in snapshot parquet) remain allowed because generated code
   cannot observe them.
7. On execution error: client POSTs `{error, code, schema_text, ...}` to
   `/v1/retry`, brain returns corrected code, client retries (up to 2
   attempts).
8. For chart turns the client also POSTs to `/v1/describe` (per chart) to
   get the natural-language intro. For scalar/non-chart answers it POSTs
   to `/v1/summarize` with a `_safe_preview`-filtered scalar — the
   `_safe_preview` guard ensures only `str | int | float | bool` cross
   the boundary; dicts, lists, and DataFrames become `None`.

---

## 9. Presentation / report download flow

Established facts about the B2C implementation that this design is built on:

- The presentation flow DOES use the LLM, but only for SUMMARIZATION: it
  generates the narrative JSON (`report_title`, `filename`,
  `executive_summary`, per-finding narratives, `key_takeaways`).
- The LLM does NOT need to know about presentation templates. Templates,
  brand colors, fonts, and layout are applied afterward by the
  `python-pptx` rendering code, separate from the LLM.
- The LLM does NOT look at the generated charts/plots. For each finding
  it receives only: question text, answer text, `has_chart` / `has_table`
  booleans, table COLUMN NAMES (not values), and a short code snippet.
  It never receives the chart image, the chart's underlying data, or any
  cell values.
- The non-LLM steps (kaleido chart-to-PNG, python-pptx rendering) stay
  client-side because they touch raw data and/or the template.

### Enterprise flow

1. Client builds a findings payload (questions, answer text, column
   names, code snippets, `has_chart`/`has_table`) — no data values.
2. Client POSTs the findings payload to `/v1/report` on the brain.
3. Brain runs the LLM to produce the narrative/summary content (the
   `report_structure` equivalent) and returns it as structured JSON.
4. Client renders its charts locally (kaleido).
5. Client merges the returned content into ITS OWN template via
   `python-pptx` and produces the final file locally. The rendered file
   never reaches the brain; the template never leaves the client.

**Resolved:** the findings payload shape is documented in
[`PROTOCOL.md`](PROTOCOL.md) under `/v1/report`. The brain's response
shape is `{report_title, filename, executive_summary, findings: [...],
key_takeaways: [...]}` matching the B2C `report_structure` exactly.

### 9a. Per-tenant PowerPoint template (clone render, native fallback)

The brain admin panel exposes a "Presentation template" card on every
per-tenant page. Operators upload the tenant's branded `.pptx` there.

**Render strategy (revised).** The primary renderer now CLONES the
tenant's DESIGNED slides so the deck visually matches the template —
backgrounds, header/footer art, colour bands, dividers, logos and theme
all carry through unchanged — then drops the template author's own sample
content and injects the analysis (titles, narratives, agenda, takeaways,
charts) into the template's designated shapes. This supersedes the earlier
"native, no-clone" decision (which preserved only the palette, two fonts
and a single corner logo, so generated decks looked nothing like the
template). The fully-native design-spec render is RETAINED as the
fallback. Both render paths are driven by artefacts the brain already
produces from the template's structure; no raw client data is involved.

The brain still learns the template's DESIGN (palette, fonts, layout
geometry, branding placement) and produces BOTH a per-shape v2 build plan
(keep/drop/replace labels + chart region — consumed by the clone renderer)
and a v3 `layout_plan.json` (geometry for the native fallback). The
pipeline:

1. **Brain — structural analysis.** The brain stores the upload under
   `tenants/<tenant_id>/pptx_template.pptx` and extracts a structural
   summary (per-slide / per-shape: name, type, text, geometry in
   inches, theme palette + fonts read straight off the .pptx zip,
   branding-vs-content picture hints). No raw client data is involved —
   only the operator-supplied branded asset's structure.
2. **Brain — COMPLEX-tier LLM authors the design.** `generate_design_spec()`
   in [`brain/pptx_template_analyzer.py`](../brain/pptx_template_analyzer.py)
   calls Gemini through `brain_agent._tier_settings("complex")` +
   `brain_agent._call_gemini_rest` (REST only, no LangChain, model id
   logged, never hardcoded — picks up per-tenant overrides via
   `_TENANT_CTX`). Acting as a senior presentation designer, the model
   returns two artefacts: a human-readable **`design.md`** design system
   and a strict **`layout_plan.json`** (version 3) that fixes the
   geometry of every region on the four canonical slide types. The raw
   response is logged as `PPTX_DESIGN_RAW_RESP` (operator-side template
   metadata + model text only — permitted under Article II). A one-shot
   tightened-prompt retry (`PPTX_DESIGN_RETRY`) runs if the first
   response is missing `layout_plan`.
3. **Brain — deterministic validate + normalize.**
   `_validate_and_normalize_layout()` turns the model's draft into a
   guaranteed-renderable plan: every region snapped inside the slide
   with a ≥0.3in margin, no two regions overlapping, the chart kept
   clear of title/body/branding, the content title forced above the
   body, and sane point sizes. It logs `PPTX_LAYOUT_VALID` when the
   draft was already clean or `PPTX_LAYOUT_NORMALIZED fixes=[...]`
   listing each correction, then `PPTX_DESIGN_SPEC_DONE`.
   `layout_plan_usable()` is the gate that confirms a version-3 plan
   with all four slide types and a content chart region. Because the
   normalizer always falls back to a safe grid, a usable plan is
   produced even when the LLM call fails entirely.
4. **Brain — persist.** The admin upload endpoint
   ([`brain/routes/admin.py`](../brain/routes/admin.py)) calls
   `save_design_doc` + `save_layout_plan`
   ([`brain/tenant_store.py`](../brain/tenant_store.py)), writing
   `tenants/<tenant_id>/design.md` and
   `tenants/<tenant_id>/layout_plan.json`.
5. **Brain — serve.** `GET /v1/pptx_layout_plan` returns
   `{has_plan, plan}` and `GET /v1/pptx_design` returns the design.md
   ([`brain/routes/llm.py`](../brain/routes/llm.py)).
6. **Client — cache (v3).**
   [`client/pptx_template_cache.py`](../client/pptx_template_cache.py)
   fetches via `brain_client.get_pptx_layout_plan()`, caches under
   `DATA_ROOT/templates_cache/` keyed by `_CACHE_SCHEMA="v3"`
   (`layout_plan.v3.json`; older v1/v2 caches are purged on read), and
   returns the `layout_plan` in its bundle.
7. **Client — render (clone primary, native fallback).**
   `_render_pptx()` ([`client/routes/report.py`](../client/routes/report.py))
   chooses the most-faithful renderer that can run, never crashing
   (Article IV):
   - **Templated clone — `_render_pptx_templated_clone()`** (primary,
     gated by `_spec_deck_usable()` on the v2 build plan). Opens the
     template and DEEP-CLONES the designed cover / agenda / content
     slides at the XML level (`_clone_slide()` — re-creates each slide's
     image/chart relationships with remapped rIds, skips the notesSlide
     and layout rels, copies any slide-level background). On each clone it
     applies the v2 labels: `drop` removes the author's sample shapes
     (tables, demo bullets, author lines), `replace:*` injects our text
     into the template's own shape (shrink-to-fit, preserving the shape's
     font/colour), and the chart is placed "contain" in the analyzer's
     region. The content body is resized to the validated `layout_plan`
     region when a chart shares the slide so narrative + chart never
     overlap. When a `replace:body`/`replace:title`/`replace:agenda` label
     lands on a shape that cannot hold text (e.g. the author's sample
     TABLE `graphicFrame`, which is what the Time template labels
     `replace:body`), `_inject_text` returns False, the renderer DROPS that
     sample shape, and the text is rendered in a fresh `PDC_INJ_*` region
     instead — so the narrative is never silently lost and the sample table
     never leaks. Deck order: cover → agenda (when the v2 deck names one) →
     one content per finding → takeaways. Logs `PPTX_RENDER_TEMPLATED
     mode=clone` then `PPTX_RENDER_CLONE_DONE`.
   - **Design-spec native — `_render_pptx_native()`** (fallback, gated by
     `_layout_plan_usable()`). Builds a fresh deck and places NATIVE
     textboxes/pictures at the `layout_plan` coordinates;
     `_add_branding()` re-places the tenant's branding media (matched by
     `image_ref`) on every slide. Logs `PPTX_RENDER_TEMPLATED
     mode=design_spec_native` then `PPTX_RENDER_NATIVE_DONE`.

   Shapes the clone renderer owns are tagged with a `PDC_INJ_*` sentinel
   name (`PDC_INJ_<role>` for shapes we position, `PDC_INJT_<role>` for
   text injected into a template-owned shape) so the regression checker
   can scope its geometry checks to OUR content and exempt the template's
   legitimate full-bleed chrome (see "Composition checks" below).
8. **Client — QA preview + deliverable.** The native renderer also writes
   an HTML preview (`PPTX_HTML_PREVIEW_WRITTEN`) used ONLY for QA — it is
   not the product. The deliverable is the editable `.pptx` (clones are
   native, editable shapes on the template base).

**Invariants:**

- Templated decks **intentionally omit the PowerDataChat logo**; only
  the tenant's branding shows through.
- **No raw client data crosses the boundary** — the brain analysis is
  purely structural (template geometry + branding), and the narrative
  path is unchanged (no values, §9).
- **REST-only COMPLEX tier** — every design LLM call goes through
  `_call_gemini_rest` on the complex tier; no hardcoded model.
- **One renderer entry point** (`_render_pptx`) serves both manual
  `/download_pptx` and Auto Analytics; there is no second code path. The
  built-in (no-template) deck is byte-for-byte unchanged.
- **Fallback chain (most faithful → least, never crash):** templated
  clone → design-spec native → built-in PowerDataChat-branded deck.
  Logged as `PPTX_RENDER_TEMPLATED mode=clone` / `mode=design_spec_native`
  or `PPTX_RENDER_BUILTIN`, with `PPTX_TPL_FALLBACK reason=...` on each
  downgrade (`clone_exception:<Type>` if the clone render raises,
  `render_exception:<Type>` if the native render raises,
  `layout_plan_not_usable` when neither the v2 deck nor the v3 plan is
  usable).

Both analyzer outputs are now CONSUMED: the **v2 build plan** (per-shape
keep/drop/replace labels + content chart region) drives the clone
renderer, and the **v3 `layout_plan`** drives the native fallback (and
supplies the clone renderer's non-overlapping content body/chart split).

**Text-fit hardening (iteration 4).** Injected text is sized to its slot so it
never overflows, and the narrative is generated to fit in the first place:

- **Constraint-bearing layout_plan.** `_annotate_slot_constraints` adds
  `size_pt_max` / `size_pt_min` / `max_chars` / `max_lines` / `kind` per slot to
  the v3 `layout_plan`, and appends a constraint table to `design.md`. These are
  generic per-slot facts, never template-specific names.
- **Mandatory title fit.** The renderer shrinks an over-long title toward a
  floor (12pt titles / 9pt body); if it still overflows it truncates at a WORD
  boundary and appends a single ellipsis (U+2026), and also sets
  `MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE` so PowerPoint shrinks on open.
- **List/bullet structure preserved.** In-place injection reuses each template
  paragraph's `pPr` (via `_fill_keep_bullets`) instead of `tf.clear()`, so
  bullet/numbering formatting survives.
- **No duplicate title.** `_kept_text_matches` detects a KEEP chrome shape that
  already shows the title (e.g. an "Agenda" wordmark) so we neither inject a
  second copy nor add a fresh fallback.
- **Narrative sized to the slot.** `generate_report_structure` takes
  `slot_budgets` (per-slot char budgets from the layout_plan via
  `_slot_budgets_for`) and emits a short `page_title` + a bounded
  "What / How to read / Takeaway" narrative. `slot_budgets` is threaded
  brain<->client through `/v1/report`; both the manual export and Auto Analytics
  paths pass it.
- **Checker tolerance.** Because a fitted title may be ellipsis-truncated, the
  deterministic checker matches page titles via `_title_match` (exact OR a
  trailing-ellipsis prefix of the expected title); no other check is weakened.

**Content quality (iteration 5).** Beyond fit, the slides now read like a
consultant's deck:

- **Real titles, never "Finding N".** Each slide title is a 3–4 word Title-Case
  topic name. `generate_report_structure`'s prompt forbids "Finding N", and both
  the brain (`_short_title_from_question`) and the renderer (`_finding_title`)
  defensively derive a title from the finding's question whenever the model
  returns an empty/placeholder/over-long title — including the agenda list.
- **Structured bullets.** Narratives are 2–4 short parallel bullets (one idea
  per line), front-loading the insight instead of restating the question.
- **Emphasis.** The brain wraps 1–3 key terms per line in `**double asterisks**`;
  the renderer (`_parse_emphasis` / `_write_runs`) styles those spans in bold +
  the theme accent and strips the markers, so a literal `**` never appears in any
  deck (clone, native, built-in, PDF). Emphasis only bolds words already present
  in the allowed narrative — no new data crosses the boundary — and titles are
  never emphasized.
- **Slide-writing guide in the prompt.** The report prompt embeds the
  conventions it asks the model to follow (6×6 / one-idea-per-bullet,
  assertion-evidence / front-loaded insight, parallel structure).

**`layout_plan.json` schema (version 3):** `slide_size_in{w,h}`;
`palette{bg,title,accent,body,muted}`; `fonts{title,body}`;
`branding[]{image_ref,x_in,y_in,w_in,h_in,roles[]}`;
`slides{cover,agenda,content,takeaways}` — each with regions in INCHES
(`title`/`body`/`list`/`chart`, each `x`/`y`/`w`/`h` plus `size_pt`,
`color`, `align`; the chart region carries `fit:"contain"`). The
validator guarantees: every region inside the slide with ≥0.3in margin;
no two regions overlap; the chart is clear of title/body/branding; the
content title sits above the body; point sizes are sane. Persisted at
`tenants/<tenant_id>/{design.md,layout_plan.json}`. `PROTOCOL.md`
documents the wire shape under `GET /v1/pptx_layout_plan`.

**Composition checks (do not regress):**
[`tools/check_templated_pptx.py`](../tools/check_templated_pptx.py)
(run with `--layout-plan plan.json`) validates a rendered deck against
the design-spec invariants:

- **G1** no two content regions overlap by more than ~2% of slide area.
- **G2** every (fresh) shape in-bounds with a ≥0.3in margin.
- **G3** content-slide title sits above the body and the chart is clear
  of title/body.
- **G4** content body height is sufficient (above a floor).
- **G5** chart aspect ratio preserved within 1% (the "contain" fit).
- **G6** branding present on every slide (matched by md5).
- **G7** prior structural checks still hold (slide count + role coverage).
- **G8** (clone path only) every injected shape — FRESH `PDC_INJ_<role>`
  AND in-place `PDC_INJT_<role>` — stays within the slide.
- **G9** (clone path only) no injected text overlaps a protected
  (logo-sized) template picture by more than 10%.
- **G10** (clone path only) the cover carries injected text only when the
  template has a genuine title placeholder (a logo-only cover stays
  untouched).

**Clone-aware scope.** A faithful clone legitimately carries the
template's full-bleed backgrounds and edge-touching chrome — and even
deliberately off-canvas author boxes (the Time template's agenda title
box sits at x≈−2.3in) — which the native-render G1/G2/G3 checks were never
meant to police. When the deck carries `PDC_INJ_*` sentinel shapes (or
`--clone` is passed) the checker RE-SCOPES — it does not disable — the
geometry checks to the content we control. The sentinel distinguishes two
ownership classes: `PDC_INJ_<role>` = a FRESH shape we positioned at our
own coordinates; `PDC_INJT_<role>` = text injected into a template-owned
shape whose geometry we inherit verbatim. Accordingly: G2 enforces the
margin only on fresh shapes and G3 evaluates over `PDC_INJ_*`; G4/G5 are
unchanged. **Placement validation (iteration 3)** removed the case the
older scope tolerated — the renderer no longer injects into an off-canvas
or over-logo template shape (it leaves that shape untouched as design and
writes our text in a computed safe region), so injected shapes are now
provably on-canvas and logo-clear. The geometry guards were therefore
re-tightened: G1 flags an overlap whenever both shapes are ours (the
"≥1 fresh" relaxation is gone); G8 now keeps EVERY injected shape
(`PDC_INJ_*` and `PDC_INJT_*`) on-slide; G9 forbids injected text over a
protected logo; and G10 keeps a logo-only cover untouched. The native
fallback carries no sentinels, so G1–G7 run in their original whole-slide
form. Verified: placement diagnostic 0 flags on the real Time-template
spec render; fixture clone path 33/33; the production `_render_pptx`
dispatch routes to the clone with no fallback.

**Analyzer / renderer pitfalls already fixed (do not regress):**

- **A `replace:*` label can target a NON-text shape.** The real Time
  template labels its content slide's sample TABLE (`graphicFrame`)
  `replace:body`; a table has no `text_frame`, so the original
  `_inject_text` silently no-op'd (narrative lost) and `_set_shape_rect`
  merely moved the sample table into view. `_inject_text` now returns a
  bool; on a non-text target the clone renderer DROPS the sample shape and
  renders the text in a fresh `PDC_INJ_*` region.
- **The CLIENT IMAGE MUST BE REBUILT after a renderer change.** A stale
  `pdc-client` container ran the old native path and produced un-branded
  decks even though the clone code was committed — the classic "decks still
  look un-branded" symptom is an undeployed image, not a render bug.
  **Which build is running is now a first-class fact** (v4.2): `GET /version`
  returns `{commit, build_time, started_at}`, the admin sidebar shows the same
  one-line stamp, and the `CLIENT_BUILD` startup log carries the commit
  (`docker logs pdc-client | grep CLIENT_BUILD`). All three are fed by the
  `BUILD_COMMIT`/`BUILD_TIME` **Docker build args** — `.git` is dockerignored,
  so the commit can only arrive from the builder; an unstamped image reports
  its process start time instead of faking an identity. **Never read the
  static `?v=` query parameter as a build marker**: it is `int(time.time())`
  computed per request at page render, and mistaking it for one has sent
  release verification down the wrong path twice.
- **A `replace:*` label can target a shape OFF-CANVAS, OVER A LOGO, or a
  blank full-bleed rectangle on a logo-only cover** (the analyzer backstop
  — or the LLM — mislabeling decorative geometry; the real Time template
  labels a full-slide cover rectangle `replace:title` and its off-canvas
  agenda title box `replace:title`). The clone renderer validates every
  labeled slot before injecting: it writes in place only when the shape is
  on-canvas, clear of every protected (logo-sized) picture, and can hold
  text; otherwise it leaves the template shape untouched (as design) and
  renders our text in a computed safe region — and on a logo-only cover it
  injects nothing. Protected logos = small KEEP pictures (`w<40%·slide` AND
  `h<25%·slide`, or area `<12%`); large kept banners are not protected (a
  title may sit on a banner).

- `prs.slides[:8]` slice — python-pptx 1.0.2's `Slides.__getitem__` returns
  `sldIdLst.sldId_lst[idx].rId`; passing a slice makes the inner indexing
  return a list, then `.rId` raises `AttributeError`. The analyzer must
  enumerate by integer index (`prs.slides[i]` per loop iteration).
- Theme extraction via `master.part.rels` is brittle across python-pptx
  versions. The analyzer reads `ppt/theme/*.xml` directly off the .pptx
  zip (`_theme_colors_from_blob` / `_fonts_from_blob`) so the real palette
  (e.g. Office's `44546A` for `dk2`, `4472C4` for `accent1`) and fonts
  feed the version-3 `palette` / `fonts`.
- MAX_TOKENS truncation: with the COMPLEX tier's thinking mode enabled
  the model burns part of its output budget on reasoning, so the design
  JSON can be cut off mid-plan and `_parse_json_response` returns None.
  Guarded by (a) a high output-token cap on attempt 1 and a higher one on
  retry, (b) a ONE-SHOT tightened schema-only retry
  (`PPTX_DESIGN_RETRY`) when the first response is empty / truncated /
  non-JSON / REST-errored, (c) the deterministic
  `_validate_and_normalize_layout` safe-grid fallback so a usable plan is
  saved regardless. `PPTX_DESIGN_RAW_RESP` records body length,
  finish_reason, and whether a dict parsed — operator-side template
  metadata + model text, NOT client data, so capturing it is permitted
  under Article II.

### 9b. Persisted domain skills

Domain skills authored from the admin portal live at
`<BRAIN_STORAGE_ROOT>/domain_skills/<skill_id>.yaml` — under the same
`pdc_brain_data` volume as the rest of the per-tenant state, so they
survive container rebuilds and restarts. The bundled directory at
`brain/skills/domain/` continues to ship built-in shared skills
(`real_estate`, `ecommerce`, ...) baked into the image. On read,
`skill_loader._resolve_domain_skill_path` prefers the persisted copy,
so operator edits SHADOW the bundled defaults without modifying the
image. `set_domain_skill_status` copies a bundled YAML into the
persisted dir on first edit, then mutates the copy.

No raw data values cross the boundary at any point in this flow — only
the template file itself (the operator-supplied branded asset) and the
no-values findings payload that was already sent today.

---

## 10. Auto Analytics flow (added since the original architecture doc)

The same boundary applies to the background "Auto Analytics" feature:

1. Client gathers `schema_text`, `df_names`, `common_fields` and POSTs to
   `/v1/auto_analytics_plan`. The brain planner (COMPLEX tier) reasons over
   the schema **plus the tenant's domain context** — its enabled domain skill
   (terminology / KPIs / expected columns / analysis style via `skill_loader`),
   the free-text `domain_vocabulary`, and the operator's `prompt_tuning_planner`,
   all from `effective_settings()`. These are shared brain assets, not client
   row data, so the boundary holds; if no skill is configured (or it fails to
   load) the planner degrades to schema-only. The planner is steered toward a
   RICH, NON-REPETITIVE set (each instruction a distinct finding on a different
   dimension/metric, detailed for the code-writer). Server-side it then drops
   near-duplicates, does ONE targeted re-ask if below the target (7), and caps
   at 15 plots (analyses/plots, NOT total slides). Returns the natural-language
   instruction list.
2. Client executes each instruction locally via `run_chat_local.run_chat`
   (bounded 4-worker pool). For each one the brain provides plan / retry
   / describe LLM steps but never receives row values.
3. Client builds the findings payload, POSTs to `/v1/report` for
   narrative, then renders the deck locally through the SAME renderer as
   manual export — the design-spec native path (§9a) when a usable
   `layout_plan` is cached for the tenant, or the built-in deck if not.
4. Result file persists at `<DATA_ROOT>/chatdata/{chat_id}/auto_analysis.pptx`.

---

## 10a. Database tables — snapshot mode (Phase 1, client-side only)

A local admin (`ladmin`, the fixed role=admin account bootstrapped from
`LOCAL_ADMIN_PASSWORD`) registers external database tables (PostgreSQL,
MySQL/MariaDB, MSSQL, Oracle, ClickHouse — a dialect REGISTRY in
`client/db_connector.py`; adding a type = one registry entry + one driver
package) so users can analyze them in chats exactly like uploaded files.
ClickHouse databases appear as schemas in the browser, and ClickHouse carries
no FK metadata, so FK-based relation discovery yields nothing for its tables
(name / description / pasted-SQL candidates still work). Spec: `docs/DB_TABLES_PLAN.md`.
The BOOTSTRAP ladmin account is **config-only**: login lands directly on the
`/admin/data_sources` panel and `/lab` redirects it back there — the appliance
account has no chat UI. (A PROMOTED admin — the per-user "admin" permission,
19g — is a full chat user; see §10b.)
Users pick registered tables via a compact "Select from DB" checkbox dropdown
in the Create-New / Add-Data wizard.

**Why this fits the split.** Everything downstream of `load_dataframes()`
(schema_text, planning, safe_execute, charts, reports, Auto Analytics)
consumes a `dict[str, pd.DataFrame]` and is source-agnostic. A registered
table enters as a parquet-backed named DataFrame (ONE central snapshot per
table at `DATA_ROOT/db_snapshots/{table_id}.parquet`; chats reference it
meta-only by `table_id`, df key = display name), so **the brain, the protocol
and the LLM layer are unchanged** in Phase 1.

**What crosses the boundary — Article II unchanged.** Only names, dtypes,
ladmin-confirmed descriptions, declared relations (rendered into schema_text
as a `Database Relations` block), and — during registration's AI draft — the
same truncated sampled `unique_hints` uploaded files already send to
`/v1/schema_autofill`. Raw DB values reach only the admin's browser preview
and the local parquet. DB credentials never cross (Fernet-encrypted at rest,
masked in APIs, never logged).

**Registration** (admin "Data sources" page): add connection → Test →
introspect (Inspector + catalog-estimate row count/size; never `COUNT(*)` on
a customer table) → preview → AI-draft English descriptions → **mandatory
ladmin review/confirm** (server-enforced: `confirm:true`, session-stamped
confirmation, re-introspection drift check; the draft endpoint has no write
path) → save + chunked snapshot (per-query statement timeout, dtype
optimization, atomic replace). Row count/size are stored as metadata only —
**nothing routes on them** (no size thresholds in Phase 1).

**Connector tables** (`is_connector` — dictionaries/link tables) are hidden
from the user picker and auto-included transitively through the relations
graph when a related table is selected (closure frozen into the chat meta at
selection time; undirected; only connectors are pulled in; capped).

**Refresh.** A lifespan-scoped scheduler thread re-snapshots all tables at an
admin-configured container-local time (default midnight) + per-table/-connection
"Refresh now". The atomic snapshot replace flips the dataframe memory-cache
signature, so every chat serves fresh data with the existing invalidation. A
failed refresh keeps the previous snapshot and `refreshed_at` (chats keep the
last good data). Schema drift re-syncs every referencing chat meta with the
same carry-over rules as Add Data's `_resync_meta_after_add` (user edits
survive, vanished columns deleted). Chats using DB tables show "data as of
<refreshed_at>" (min across tables) via `GET /api/chat/{id}/schema`.

**Relation discovery** (ladmin "Discover relations" section; proposals only —
nothing is applied without an explicit accept). Candidates come from three
deterministic sources, merged and deduped on the (table, columns) pair:
declared FKs (fetched by LIVE introspection at scan time — FKs are not
persisted in the registry; an unreachable connection degrades only the FK
source), normalized column-name / confirmed-description similarity (inverse-
frequency down-weighting for ubiquitous names, hard share cap, no LLM), and
optionally admin-pasted SELECT statements parsed locally with sqlglot
(aliases/CTEs/subqueries resolved via scope traversal; composite ON
predicates become one multi-column candidate; literal predicates dropped;
frequency counted across distinct statements). Every candidate is then
verified against the local snapshot parquets — cardinality derived from
key-side uniqueness (one-to-many inputs are flipped so the stored direction
is always many-to-one with the correct parent; a declared FK's direction is
never overridden by data), overlap % of child keys present in the
de-duplicated parent keys, orphan count; a missing snapshot renders the
candidate `unverified` instead of hiding it — and banded CONFIRMED /
SUGGESTED / NEEDS ATTENTION (thresholds in one constants block in
`relation_discovery.py`). Accepting writes the relation onto the CHILD
table's `relations` with two additive fields — `cardinality`
("N:1"|"1:1"|"1:N"|"N:M") and `origin` ("fk"|"sql"|"name"|"description";
manual rows simply lack the keys) — through a relations-only write path with
no confirm gate / drift check / re-snapshot (those locks protect the
column+description shape, which the accept endpoint cannot touch). Old-shape
relation entries load and render byte-identically; schema_text appends a
"(many-to-one)"-style suffix only when `cardinality` is present, and the
cardinality is carried into NEW chats' meta at selection time (existing
chats keep their frozen meta, as with every relation edit). Dismissals are
audited but deliberately not persisted. Known limits: the accept
read-modify-write is not atomic vs a concurrent nightly refresh of the same
table doc (single-admin exposure, same class as save), and a source-DB
column rename leaves a dangling join key that surfaces as `unverified` on
the next scan.

**Relations overview + wizard auto-suggest** (UX layer over discovery). The
admin section is named "Relations": Zone A lists EVERY confirmed relation
across all registered tables (legacy pre-discovery entries render with
origin "manual" and blank cardinality) with per-row Edit — the same inline
editor as candidates, saved through `accept`'s additive `replaces` field
(swap-in-one-write; an edit that would duplicate a DIFFERENT entry is
skipped with the old entry preserved, never a silent delete) — and Delete
(`/relations/delete`, exact-match by related ref + ordered join keys,
removes every identical duplicate). Zone B keeps the scan/SQL discovery
unchanged; a zero-candidate scan is explained with the confirmed-relations
count instead of an empty list. The register wizard's relations step
auto-suggests relations for the table being registered
(`/relations/wizard_suggest`): declared FKs from the introspection the
wizard already performed render PRE-CHECKED (direction is ground truth,
default N:1; referred tables resolve across ALL connections — broader than
the old same-connection seeding, which was removed in favor of the
suggestion block), name/description similarity renders unchecked; the
parent side is verified against its snapshot, the child side is ESTIMATED
from the wizard's preview sample (labeled as such; a candidate whose
measured direction had to be flipped drops its numbers rather than show a
misleading percentage; date-typed join keys may under-estimate — preview
datetimes serialize with a time-of-day the parquet string form lacks).
Checked suggestions ride the wizard's NORMAL confirm+snapshot save.
Suggestions are computed only when the step opens; no background scanning.

**Physical identity + the one-registration rule.** A registration's physical
identity is `(connection_id, lower(schema), lower(table_name))`. Live
testing showed that a table registered TWICE turned discovery into a noise
generator (self-relations between the copies, FK fan-out to every copy), so:
(1) candidate generation never proposes a relation whose two sides are
registrations of one physical table; (2) a relation confirmed to ANY
registration of a physical target suppresses re-proposals to its
duplicates; (3) duplicate-registration fan-out collapses to ONE candidate
targeting the preferred registration — connector first (connectors exist to
be auto-included via relations), then earliest-registered, deterministic —
with `alternate_targets` noted for retargeting; (4) a physical table can be
REGISTERED only once — the save rejects a new physical mapping another
registration covers (`400 DUPLICATE_TABLE`), the wizard dropdown disables
registered tables ("already registered as 'X'"), and connector-vs-normal is
toggled on the existing registration instead of registering a second copy.
Preference never crosses physical tables: a same-named table on two
connections stays ambiguous. LEGACY duplicates in stored data keep loading
and working (an edit keeping its stored physical key always saves; nothing
is auto-deleted) — the overview flags their self-relations with a
"same physical table" badge and offers per-row and bulk delete, so the
admin cleans them deliberately. The overview groups relations by table pair
and shows the physical `schema.table` under the display names, so duplicate
registrations are visible instead of looking like inexplicable twins.
Findings + plan: `docs/RELATIONS_UX_PLAN.md`.

**Relations v3 — structured editing, missing-table hints, graph.** Manual
join keys are edited ONLY through the structured pair editor (paired column
dropdowns fed by stored registry metadata; non-blocking dtype-family warning
mirroring the verification rule; stored columns absent from the registry
render "(missing)" and gate Save on the accept-backed paths — the wizard's
store-verbatim save contract is frozen, so unknown columns there are made
observable via the `REL_SAVE_UNKNOWN_COLUMN` log instead of rejected). FKs
pointing at UNREGISTERED tables surface after a scan as "Referenced but not
registered" rows, and analyze_sql's unknown tables carry the same
"Register as connector" shortcut when the connection is unambiguous — the
shortcut only PREFILLS the register wizard (connection/schema/table +
connector ticked); nothing auto-registers, and the post-registration flow is
the normal one (the next scan proposes its relations). The Relations section
has a Graph view (List default): nodes = registered tables (connector-badged,
sized by relation count, component-colored, isolated tables warned "the AI
cannot combine it with others"), edges = confirmed relations labeled with
join keys + cardinality (same-physical noise in warning style, dangling refs
skipped), dashed ghost nodes for the unregistered FK refs, edge popover with
Edit (hands off to the list's structured editor) and Delete. Rendering uses
**vendored Cytoscape.js 3.34.0 + dagre 0.8.5 + cytoscape-dagre 2.5.0 (all
MIT, under `static/vendor/` with license sidecars)** — enterprise clients
run on LANs, so no CDN ever;
graph data is assembled by the pure `relation_discovery.build_graph` behind
`POST /api/admin/relations/graph` (metadata only). Dangling relations (a
deleted target registration) are flagged in the list instead of showing raw
ids; failed relation-accept validation is audited `ok:false`.

**Relations v4 — persistent "Recommended tables".** Join evidence that used
to be thrown away (predicates touching UNREGISTERED tables) is retained as
identifier-only evidence and persisted in a new additive top-level
`recommendations` section of `data_sources.json` (`_default_doc` +
`read_doc` both know the key — read_doc whitelists sections; old docs load
unchanged). One entry per unregistered physical table (merge by connection +
schema + table, case-insensitive) with `status open|dismissed|registered`,
accumulated statement frequency, and anchored evidence entries
`{origin sql|fk, other (physical names, resolved fresh at read), pairs
(recommended-table column FIRST — orientation fixed by construction),
count}`. Sources are ONLY pasted-SQL joins (names resolvable to one
connection via the hint rule) and the scan's live FK introspection — no
schema-wide scanning. Dismiss is PERSISTENT (with restore); a store-level
reconcile hook inside every registry mutation keeps statuses consistent:
any registration path flips a matching rec to `registered` (remembering
`prior_status`), a vanished registration reverts to `prior_status` (a
dismissed rec can never resurrect), a deleted connection drops its recs.
One-click **Accept** registers the table as a connector server-side reusing
the existing pieces verbatim (`_draft_table_descriptions` = the draft
route's AI mechanism; `_build_table_doc` = the wizard save's doc shape;
`refresh_one_table` = the one snapshot path) — with STRICTER atomicity than
the wizard save: a snapshot failure deletes the just-created registration so
no half-registered state remains. After any registration the stored SQL
evidence replays through the NORMAL candidate pipeline (instantly on
Accept, next scan otherwise); FK evidence is never replayed — live
introspection re-derives it, and replayed candidates never carry `fk`, so
banding's fk-auto-confirm cannot fire (proposed, never auto-confirmed).
Roles (bridge/referenced) are computed at read time from the current
registry. The graph renders open recommendations as ghost nodes with
evidence-labeled dashed edges; dismissed ones never render.

**Relations v4.1 — evidence validation + evidence UX.** Wrong pasted SQL is
a first-class case with visible feedback, never a silent drop and never a
bogus candidate (root causes recorded in docs/RELATIONS_UX_PLAN.md § v4.1):
column-existence validation runs at TWO stages, both case-insensitive
(sqlglot's qualify normalizes identifier case on the happy path; its
fallback preserves raw casing — exact matching would false-flag).
At ANALYZE time, any predicate side resolving to a registered table is
validated against registry metadata; invalid pairs are skipped (valid pairs
of the same statement kept) and reported with the statement number
(`stats.invalid_column_refs`). At REPLAY time — for evidence on tables that
were unregistered at analyze time, whose columns were unknowable then
(analysis stays metadata-only, no live DB calls) — the pure
`validate_rec_evidence` excludes pairs the now-known registration cannot
satisfy and surfaces them as `evidence_warnings` + a log line; this is also
the corrupted-store guard (stale evidence from any earlier release dies at
replay; no migration). The accept endpoint's column rejection names the
table and the missing column instead of the v1-era fused `'a=b'` token.
Honest limit: a wrong join naming a column that exists on BOTH tables is
genuinely valid to every layer — mitigated by the analyze-time report and
the "script may be outdated" caution wording. Known limitation (visible via
`stats.unresolved_predicates`): joins through COMPUTED CTE/subquery
projections cannot be column-resolved and contribute no evidence.
Evidence UX: recommendation rows always render their FULL join evidence —
unregistered partners tagged "not registered" — plus a locked 🔒 preview of
the relations that will be proposed once the blocking tables register
(SQL-origin evidence only; identifiers only, computed at read time, nothing
new persisted); accepting the blocker turns them into normal candidates via
the same validated replay — one pipeline, no fork.

**Relations v4.2 — time-bounded Accept + table-type choice.** Accept is one
interactive click, so every dependency it touches is bounded at its OWN seam
(root causes in docs/RELATIONS_UX_PLAN.md § v4.2): the AI draft carries
`BRAIN_DRAFT_TIMEOUT` (60s, env-tunable) instead of riding the 180s
client-wide default, and timeout-shaped driver errors become one sentence
naming the database. Deliberately NOT an `asyncio.wait_for` around the whole
gesture: executor threads cannot be cancelled, so an outer deadline would
report failure while the registration continued in the thread — exactly the
half-registered state the snapshot rollback exists to prevent. `REC_ACCEPT_PHASE`
logs on ENTRY to each phase, because the original hang produced no log line
at all (every dependency logged only on return, so a wedged request was
invisible). The browser caps its own wait too, and says honestly that an
aborted wait does not stop the server.
**Table type is suggested, never assumed.** Accept used to hard-code
`is_connector=True`, which hides a content table like `prod_dict` from the
user picker. `relation_discovery.classify_table_type` (pure, metadata-only:
a column is key-like when its name ends with `id|code|key|no|num` AND its
dtype is integer-family or a ≤32-char varchar; all key-like → connector, else
normal naming the descriptive columns) feeds a dialog default the admin
confirms or flips; the audit row keeps both `suggested_type` and
`chosen_type`. It is deterministic rather than AI because the schema-autofill
prompt lives brain-side and that repo is out of scope for this change — an
AI-suggested type is a separate, brain-side commission.

**Relations v4.3 — the graph reads as an ER diagram.** Database relations
have an established visual language (Power BI model view, dbdiagram.io,
DBeaver), and the force-directed original fought it: labels sat OUTSIDE
circles and collided, one rotated mid-edge string fused join columns with
cardinality and truncated ("city_code · ma…"), the cluster color filled the
whole node so a single-cluster registry was a field of identical blobs.
Now: **table CARDS** (round-rectangle, sized to the text, name + schema.table
INSIDE), the cluster color as a border ACCENT rather than a fill, connector
tables double-bordered with ⚙, ghosts dashed and still click-to-register,
isolated tables keeping their red accent. **Cardinality moved to the line
ENDS** — `N` at the many end, a bar fused into the direction arrow
(`triangle-tee`) at the one end, nothing at all when cardinality is unknown
(every ghost edge) — so the mid-edge chip carries the join COLUMNS only,
horizontal and legible. The decisions are server-side and unit-tested
(`edge_label`, `edge_end_markers` → additive `label`/`source_marker`/
`target_marker`), which also retired the client-side
string-fusing that produced the old rotated caption. Layout is layered left-to-right via **vendored dagre**, with the
built-in `breadthfirst` as a fallback that engages if the extension fails to
load OR throws at run time — the layered layout is an enhancement, and
losing it must never cost the admin the graph (Article IV). Zoom in/out/fit
controls were added (wheel zoom and node dragging kept), and the click
popover now hides on pan/zoom/drag instead of drifting away from its edge.
Honest limit: Cytoscape canvas labels take ONE font per node, so both card
lines share a size and weight — accepted over vendoring an HTML-label
overlay for typography alone.

**Security** — see AI_CONSTITUTION Article VII (rules 8–9): SELECT-only
connector, sandbox import denylist (defense in depth — the customer's
dedicated SELECT-only DB login is the real guarantee), encrypted credentials,
append-only admin audit JSONL. Relation discovery adds one more invariant:
**admin-pasted SQL text is parsed in memory on this client only** — never
persisted, logged, audited, or sent to the brain; sqlglot error messages
(they embed the SQL text) never leave the parser (exception types only), and
snapshot verification emits aggregates only (counts/percentages, no values).
v4 amendment (Article VII rule 10): table/column IDENTIFIERS and statement
counts extracted from the SQL may persist locally as recommendation
evidence — literals never survive extraction (only Column = Column
predicates are read), and the SQL-box UI states this truthfully.

**Phase 2 (later, client + brain in parallel — NOT built):** live SQL mode
for large tables (brain writes dialect-aware aggregation SELECTs; client
validates + executes). Per `docs/DB_TABLES_PLAN.md`.

### 10b. User roles & DB-table privileges (client-side only)

The role-based table-visibility follow-up to §10a. Entirely client-side — no
brain involvement, no protocol change.

**Model (19c: MULTIPLE roles per user; 19e: roles = ACCESS ONLY; 19f: read
and manage are SEPARATE axes on the role).** A user holds a LIST of roles;
read access is the UNION across them. Roles live in `DATA_ROOT/roles.json`
(`roles_store.RolesStore`, DataSourceStore discipline: locked atomic writes,
section-whitelisting reads, 16-hex ids): `{id, name, description,
table_ids: [], scope_grants: [{connection_id, schema|null}],
manage_grants: [{connection_id, schema|null}]}`. `scope_grants` are the
READ axis — the deliberate opt-in "every table on this connection/schema,
present AND future" choice; `manage_grants` are the MANAGEMENT axis (where
power-permission members may register tables/relations/schedules) and never
grant read. The split follows the industry pattern (Looker permission set ×
model set, Metabase's tri-state grid): before 19f a schema grant meant both,
so opening a schema for a power user force-exposed all of its tables to the
whole role. A grant with `schema:null` covers the whole connection; schemas
match case-insensitively; `schema:""` is a legal literal (sqlite).
**Migration**: roles.json is versioned — `migrate_manage_grants()` at boot
upgrades a v1 doc to v2 by copying each role's scope_grants into
manage_grants ONCE (behavior preserved exactly on upgrade; afterwards the
lists diverge freely; idempotent via the version stamp; fresh docs start at
v2). Downgrade caveat: a 19e build's normalize drops `manage_grants` while
the version stays 2, so a downgrade → role edit → upgrade cycle loses manage
scopes (fail-closed — re-grant from the Roles UI); same lossy-edit class as
the 19c `data_roles` caveat. The built-in **Base** role (literal id
`"base"`) is seeded at boot right after the ladmin bootstrap — undeletable,
unrenamable, grants editable, empty by default. The 19c-era `power_user`
role flag and built-in "poweruser" role are GONE (the capability moved to
the per-user permission, below): `_normalize_role` drops a stored
`power_user` key silently so 19c-era roles.json docs keep loading, and
`remove_poweruser_role()` at boot deletes a previously seeded "poweruser"
doc (members holding its id go dangling and resolve like any deleted role).
The user's held ids live in the additive `data_roles` list on
`users/{email}/profile.json`, with the legacy single `data_role` MIRRORED to
the first id on every write (an older build reading the same DATA_ROOT keeps
working); reads are tolerant — a legacy profile with only `data_role` reads
as a one-element list, missing/empty resolves to Base (`last_login_at` is
stamped there by the login funnel). Old-shape profiles and an absent
roles.json keep loading — everything resolves to Base through `.get()`
defaults.

**Effective access is computed at request time**, never frozen:
`allowed_table_ids_for(email)` = the union over all held roles of
(explicit `table_ids` ∩ live registry ∪ scope-grant matches), PLUS the
**ownership read** (19f): non-connector tables whose `registered_by` equals
the email (case-insensitive) — a power user always sees and can chat with
what they registered, before any role share. So a table registered later
under a granted schema is covered without a role edit, grant changes
propagate instantly, and deleting a role drops out of its members' held
lists dynamically (a dangling id is skipped at read time — no profile
rewrites, which is also why role deletion is safe against concurrent logins;
a user whose every held id dangles reverts to Base). **Connector tables are
exempt** from role checks everywhere: they are invisible to users and
auto-included through the relations closure — gating them would silently
break allowed joins.

**Enforcement points** (and, just as deliberately, non-enforcement):

- `GET /api/db_tables` — the picker lists only allowed tables.
- `POST /session/db_tables` — non-allowed SEEDS → 403 `ROLE_DENIED`; the
  connector closure stays exempt.
- Per-item refresh (chat `refresh_item` + BOTH dashboard tile branches) —
  blocked per-table via `routes.chat._role_refresh_block`: the item's code is
  scanned with the same `dfs['…']` key regex the frontend freeze uses; an
  item touching only allowed tables still refreshes. Denied frames are also
  dropped from the exec namespace after the (per-chat, user-agnostic) cached
  load. Dashboard denials are caller-specific and never persisted (mirror of
  `access_revoked`). Genuine denials fail CLOSED (Base defaults); an
  unexpected gate crash fails OPEN with `ROLE_GATE_FAILED` logged.
- `GET /api/chat/{id}/schema` — advisory per-table `allowed` flag so the /lab
  and dashboard-view UIs grey refresh buttons proactively.
- **Not gated by design** (confirmed decisions — no retroactive blocking;
  snapshot data the user could already see stays viewable): `chat/stream`,
  `edit_regenerate`, full-table/Download-Excel re-execution, Auto Analytics,
  `add_data_to_chat` (its DB entries were validated at selection time), and
  the central nightly snapshot scheduler. Shared-chat/dashboard recipients
  keep VIEWING stored snapshots; only their fresh re-execution is gated, keyed
  on the requester.

**Canonical storage on the role record, never the table doc:** the register
wizard's step-3 Access panel posts `access_role_ids`, which the save
reconciles into the roles via `set_table_roles`; table deletion prunes the id
from every role. 19f "registration = publish + share": POWER users get the
panel too, retitled "Share with your roles" and limited to their HELD roles
(fed by `GET /api/admin/my_roles`; the built-in Base is excluded even when
held — everyone is a member, so sharing through it would publish to the
whole platform, an administrator action), ALL UNCHECKED by default — a
fresh registration is visible only to the registerer (the ownership read)
until they opt in; the server enforces the held-subset rule
(`403 ROLE_NOT_HELD` up-front, before anything is registered — never a
silent drop) and limits the power user's reconcile to that held subset, so
a role they do NOT hold keeps its ladmin-granted membership (ladmin's
reconcile stays exact). Admin surface: `routes/admin_users.py` (`/api/admin/users*`,
`/api/admin/roles*`, same `_require_admin` guard, audited `user.set_roles` /
`user.set_permission` / `role.*`), plus the Users + Roles sections on the
admin page (searchable user list with the 19c multi-role checkbox picker —
every toggle POSTs the full held list — and the 19e per-row Permission
dropdown; role cards + ONE tri-state access tree connection → schema →
tables with TWO checkbox columns since 19f: "Chat access" on every level,
"Manage" on connection/schema rows only — the Metabase-grid shape; note the
tree derives schema rows from REGISTERED tables, so a schema-level manage
grant on a still-empty schema takes a connection-level grant or the API).
The bootstrap ladmin account is config-only and is excluded from the Users
window; other admin-permission users ARE listed (they must stay demotable)
with their roles picker ENABLED (19g — promoted admins hold roles like
anyone).
`roles_store` is denied inside the code-exec sandbox (grant tampering =
privilege escalation). Downgrade caveat: an OLD build's `set_data_role`
rewrites only the mirrored `data_role`, leaving `data_roles` stale — a
downgrade → role change → upgrade cycle resurrects the pre-downgrade held
list (read-compat holds in both directions; in-place role edits from an old
build are the one lossy path).

**Per-user PERMISSION + power users (prompts 19 + 19e + 19g) — delegated,
scoped data-source management.** The permission is a property of the USER:
the profile `role` field holds `"user"` (standard, the default —
legacy/unknown values read as it) | `"power"` | `"admin"`.
`AuthStore.get_role` normalizes; `is_admin` / `is_power` read it (admin is
NOT power — admins use the full admin page instead of /power). The
permission LADDER (19g) is standard ⊂ power ⊂ admin **for capabilities
only** — READ access always comes from held roles (union + ownership read),
with no implicit all-tables read at any level. A PROMOTED admin is a full
analysis user (lands on /lab, keeps chats and roles — the picker follows
their roles like any user) PLUS unrestricted Data-sources administration
(the /lab dropdown's "DB config" targets `/admin/data_sources`, whose
sidebar footer carries "← Back to chat" for them). Only the BOOTSTRAP
ladmin account (`AuthStore.is_bootstrap_admin` — an IDENTITY compare
against `LOCAL_ADMIN_USERNAME`, never the permission) keeps the config-only
behavior: login → admin page, /lab redirects away, no data roles, unlisted,
permission immutable. Ladmin sets the permission from the Users window via
`POST /api/admin/users/set_permission` (`{email, permission:
"standard"|"power"|"admin"}`, "standard" stored as "user"; refuses the
bootstrap account and the caller's own account — no self-demotion; audited
`user.set_permission` with old → new). Promote/demote never touches
`data_roles` — a promoted admin's roles stay ACTIVE, and a demote
round-trip preserves them.

The POWER permission decides only WHETHER the user may manage data sources —
register tables, define relations, set per-table refresh schedules,
self-service on `/power/data_sources` (the SAME admin template in a stripped
`"power"` mode, reached via the "DB config" item in the /lab profile
dropdown; power users stay normal /lab users; the page header carries a
server-computed scope summary — "You can manage: <connection> / <schema>,
… — all schemas" — plus a muted "read-only roles give chat access, not
management" hint when read access reaches beyond the manage scope,
`app._power_scope_summary`). WHERE they may manage — the **management
scope** — is the UNION of connection/schema `manage_grants` across ALL
their held roles (deduped; 19f — `scope_grants` are the read axis and no
longer contribute). Explicit `table_ids` grant READ access only, never
management. Enforcement is the second guard in `routes/admin_data.py`,
`_require_source_manager` (`(email, scope, err)`; scope `None` = ladmin,
unrestricted): every referenced physical table must fall inside the scope
(`403 OUT_OF_SCOPE`), list responses are filtered to it, `access_role_ids`
from a power user must be a subset of their held roles
(`403 ROLE_NOT_HELD`; the share panel above), and table DELETE additionally
requires ownership — the doc's
`registered_by` (stamped from the session at FIRST save by
`_build_table_doc`, carried through edit-saves like the schedule override;
absent = ladmin-registered/legacy) must equal the power user
(`403 NOT_OWNER`). Connection lifecycle, the global refresh schedule,
users/roles/permissions and the audit tail stay strictly ladmin-only
(`_require_admin`). Power-user writes are labeled
`actor_kind: "power_user"` in their audit detail so ladmin can tell them
apart in the tail; helpers: `roles_store.is_power_user` (delegates to
`AuthStore.is_power`) / `management_scope_for` (None unless the permission,
else the union) / `can_manage_physical` / `manageable_table_ids_for`
(connectors INCLUDED — management is about the registry, unlike read
access) / `scope_covers`, all fail-closed.

---

## 11. Sharing restriction

The enterprise/company version restricts sharing to within the company's
own domain (no sharing outside their domain). This builds on the existing
share logic.

**Resolved (partial):** per-tenant config carries an `allowed_sharing_domains`
list; the client's `POST /api/chat/{id}/share` and
`POST /auth/conversations/{conv_id}/share` filter recipients against that
list before calling the brain to send invites. Recipients outside the
allowlist are rejected with `400`.

**Still OPEN:** whether to enforce on the brain side too as defense in
depth.

---

## 12. What remains explicitly OPEN

Items still undecided. Do not invent or assume:

- Auth/token rotation policy (lifetime, automatic rotation cadence).
- How the client server reaches the brain at the network level (public
  HTTPS endpoint + token vs. VPN/private link — likely tenant-specific).
- Whether to add brain-side enforcement of the sharing-domain allowlist
  (currently client-side only).
- Whether the dashboard top-bar should expose a "tenant ID" badge for
  operator support.

If any of these need to be decided for a future task to make sense, **ASK
in plain chat first** — do not put assumptions into code or into a prompt.
