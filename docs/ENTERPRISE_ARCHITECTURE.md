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
   `code_exec.safe_execute`. The result stays local.
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
  look un-branded" symptom is an undeployed image, not a render bug. The
  `CLIENT_BUILD marker=` startup log lets an operator confirm the running
  image carries the new renderer (`docker logs pdc-client | grep
  CLIENT_BUILD`).
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
