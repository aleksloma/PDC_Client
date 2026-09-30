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

The customer's side is TWO containers, not one. Everything sensitive stays
in the company's LAN either way; the split inside the LAN exists because the
one thing the product must run — code written by a language model — is the
one thing that must not run next to the data and the credentials.

```
        the company's LAN                                  PowerDataChat GCP
 ┌──────────────────────────────────────────────┐
 │  default network (browser + outbound)        │
 │  ┌────────────────────────────────────────┐  │   HTTPS + tenant token
 │  │ pdc-client            uid 10001        │──┼──────────────────────────▶  Brain
 │  │  /lab, upload, storage, reports        │  │   question + metadata only
 │  │  DATA_ROOT volume  (raw data, chats)   │  │
 │  │  DB connections (SELECT-only login)    │──┼──▶ the company's databases
 │  └───────────────┬────────────────────────┘  │
 │                  │  jobs volume: frames in, result out
 │  backend network │  (internal: true — no gateway)
 │  ┌───────────────┴────────────────────────┐  │
 │  │ pdc-executor          uid 10002        │  │      ✗ no route out at all
 │  │  generated Python runs HERE, one job   │  │
 │  │  at a time. No secrets, no DB driver,  │  │
 │  │  no customer data mounted, no port.    │  │
 │  └────────────────────────────────────────┘  │
 └──────────────────────────────────────────────┘
```

### Client container — `pdc-client` (company LAN, uid 10001)
- Data upload and storage. Raw data values never leave the LAN.
- The `/lab` frontend, reports (PDF, PPTX) and chart rasterization
  (kaleido). These are not LLM steps and they stay here because they touch
  raw data and the company's template.
- Database connections. The credentials live here, encrypted at rest, and
  the SELECT-only login the customer provisions is the real guarantee.
- It orchestrates execution but does not perform it: it writes each job's
  input frames into the shared jobs directory, asks the sandbox to run the
  code, and reads the result back.
- Presentation templates (the company's branded templates / decks).

### Analysis sandbox — `pdc-executor` (company LAN, uid 10002)
- **The only place generated Python runs.** One job at a time, each in a
  fresh subprocess with its own memory, process and file-size limits.
- Holds no secret, no database driver, no credential-bearing module, and
  refuses to start if a secret appears in its environment.
- Joins ONE `internal: true` Docker network and publishes no port, so it has
  no route to the LAN, to a database, or to the internet.
- Mounts no customer data. The jobs directory is the only shared storage,
  and each job's frames are written into it per job — which is what keeps
  the per-role table permissions meaningful from inside generated code.
- Answers are treated as untrusted input by the client, because the code
  that produced them is untrusted by definition.

*Amendment 2026-09-29 (job directory).* Rationale: a job directory is mode
2770 with the shared group and no sticky bit, so the sandbox can replace its
entries, and an abandoned one holds another question's input frames.
Impact: the client never reads back or unpickles anything from a job
directory (the input pickle is verified in memory before it is written);
both sides sweep abandoned job directories after five minutes, every minute,
skipping jobs still in flight; the sandbox locks the job directories it owns
at startup. Article XIV's "stash lasts about five minutes" follows from this.

*Amendment 2026-09-29 (stray processes and scratch).* Rationale: one `/proc`
snapshot let a process forked after it survive into the next user's job.
Impact: the sandbox's same-uid sweep repeats until the uid is clean (at most
10 passes), runs at every concurrency setting, and on failure latches the
sandbox unhealthy (`/healthz` and `/execute` 503 `EXECUTOR_UNHEALTHY`) until
the operator restarts it. Each job gets a private scratch directory for the
default temp and cache locations; the `/tmp` root stays shared by the job uid.
Article XIV property 5, its "One job at a time" rule and its honest limits
record this.

`docs/AI_CONSTITUTION.md` Article XIV is the normative version of this
boundary, and `docs/EXECUTOR_PROTOCOL.md` is the field-level contract between
the two containers.

### What crosses each boundary

| Boundary | Out | In |
|---|---|---|
| client → brain | question text, schema and column metadata, aggregate profiles, generated code, error text, scalar previews. **Never rows, tables or charts.** | generated Python, narrative text |
| client → sandbox | the code to run, and the input frames as parquet written per job | the result (parquet, chart HTML or PNG, a scalar preview), plus a capped tail of the sandbox's stderr and traceback for the local log. A job's stdout crosses in the same response but is NOT written to the web log. The sandbox keeps no log file and logs only the lengths of a job's stdout and stderr, so what a job printed is logged nowhere |
| sandbox → anywhere else | nothing. It has no network route and no credentials. | — |

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
suggested starter questions for that tenant (e.g.
`"Georgian (ქართული)"`). The **client no longer forces a language** — it only sends the
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
6. Client runs the code against the raw data **inside the LAN but outside
   its own process**: `code_exec.safe_execute` / `plot_utils.render_plot_safe`
   write the referenced frames into the shared jobs directory and dispatch
   the job to `pdc-executor`, which executes it as a different unprivileged
   user in a container with no credentials and no network route, and answers
   with the result. The result stays local; nothing about this step reaches
   the brain. If the sandbox cannot be reached the client returns a plain
   "the analysis service is not reachable" answer and never falls back to
   executing the code itself. Before EVERY execution the Article XIII
   sanitize gate (`exec_sanitizer.sanitize_for_execution`) normalizes the
   dataframes to plain standard dtypes — it runs at both exec sites, which
   now live inside the sandbox — because generated code must never observe
   category / sparse / extension dtypes (a categorical dimension column once
   made a two-key groupby emit the cartesian product of all categories and
   put every category on a chart axis). Storage-layer optimizations (numeric
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
2. Client works through the instructions via `run_chat_local.run_chat`
   (bounded 4-worker pool), each one dispatched to the sandbox like any
   other question — so a deck's worth of analyses is executed one job at a
   time, and a saturated sandbox answers "busy" rather than failing a
   finding that never ran. For each instruction the brain provides plan /
   retry / describe LLM steps but never receives row values.
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
Identifier quoting and row limiting live in the SQLAlchemy dialect, not the
registry: every statement built from introspected names is a construct
(`_select_stmt`), and queries carry an explicit column list so the frame comes
back keyed by the INTROSPECTED names. Both matter on Oracle, whose Inspector
normalizes case-folded identifiers to lowercase while the server stores them
uppercase — hand-quoted names raised ORA-00942, and driver-cased result
columns would have made every refresh read as full schema drift.
PHYSICALLY case-sensitive names (created quoted, e.g. by a pandas `to_sql`
pipeline) are the mirror image: the Inspector marks them
`quoted_name(quote=True)`, and that flag is the ONLY bit distinguishing them
from an ordinary fold-case name — the plain string is ambiguous, and losing
the flag at a JSON hop compiled them unquoted (ORA-00904). The flag is
therefore PERSISTED from introspection (per-column `quote: true` +
`schema_quote`/`table_quote` on the registry doc, emitted only when true;
derived server-side at save from a fresh introspection, never from browser
payloads) and REBUILT at every query-construction point
(`db_connector.qname`/`col_ident`); the dialect decides at compile time what
it means — no per-dialect branches, never inferred. Legacy docs without the
flag repair themselves on the next refresh, which re-introspects live.
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
as a `Database Relations` block), and — during registration's AI draft and
when a refresh drafts descriptions for newly added columns — the same
`unique_hints` uploaded files send to `/v1/schema_autofill`: the distinct
values of a categorical column (at most `SCHEMA_AUTOFILL_UNIQUE_THRESHOLD` of
them) and ONE computed `[profile: …]` string per high-cardinality column
(dtype, distinct count, null share, a character mask, lengths, rounded
magnitudes, year-month bounds), never sampled row values. Raw DB values
reach only the admin's browser preview and the local parquet. DB credentials
never cross (Fernet-encrypted at rest, masked in APIs, never logged).

**Registration** (admin "Data sources" page): add connection → Test →
introspect (Inspector + catalog-estimate row count/size; the Inspector step
itself never runs `COUNT(*)` on a customer table, the size verdict below adds
one bounded count) → preview → AI-draft English descriptions → **mandatory
ladmin review/confirm** (server-enforced: `confirm:true`, session-stamped
confirmation, re-introspection drift check; the draft endpoint has no write
path) → save + chunked snapshot (per-query statement timeout, atomic
replace). The chunked snapshot writes against **ONE canonical Arrow schema**
derived from the introspected column types (chunk-1 inference only for
exotic types, normalized), and every chunk is converted against it — pandas
re-infers dtypes per chunk, so pinning the writer to chunk-1 inference made
an all-NULL leading column, an int growing NULLs, or a timestamp resolution
flip (`timestamp[ns]` vs `[us]`) kill a later `write_table`. Timestamps are
canonically `us` (year-9999 sentinel dates overflow ns; sub-µs values are
truncated with a log). A genuinely lossy cast (overflow/incompatible values)
fails the snapshot naming the column — previous snapshot kept — and prunes
the stale downcast from the stored plan so the next run self-heals. Row
count/size are stored as metadata only — **nothing routes on them** (no size
thresholds in Phase 1).

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

**Phase 2 — live SQL mode for large tables (client side shipped; the
planner prompt on the brain follows).** The brain writes dialect-aware
SELECTs; the client validates and executes them. Per
`docs/DB_TABLES_PLAN.md` and, in full, `docs/LIVE_TABLES_PLAN.md`.

**Live mode.** Every registered table has a storage `mode`: `snapshot`
(everything above) or `live`. An absent field reads as snapshot, so older
registry documents load unchanged. The register wizard sizes the table with
one `COUNT(*)` (a SQLAlchemy construct through the same read-only gate,
bounded by the connection's statement timeout capped at 60 s) and derives a
cell count (rows x columns). At or above `LIVE_MODE_CELL_THRESHOLD` live mode
is suggested; at or above `LIVE_MODE_FORCE_THRESHOLD` a new registration is
refused as a snapshot (`LIVE_REQUIRED`), and a count that times out counts
as above that limit; an edit keeps the table's stored mode. Registering a
table live takes no snapshot. Instead the client profiles a bounded head
sample (at most 10 000 rows) locally, so the registry and the dataset
profile still describe it; only the truncated sampled hints already
permitted for snapshot tables (profile aggregates, top values up to 40
characters) reach the brain, never rows. An administrator or a scoped power
user can switch a table between modes (`POST /api/admin/tables/{tid}/mode`,
audited `table.mode`); switching to live keeps any old parquet, switching
back counts again and always takes a fresh snapshot (a failed snapshot
leaves the table live, its profile stamps restored). Scheduled refreshes
skip live tables; a resync of a live table writes registry metadata into
the chat metas only, never a `refreshed_at`.

**The live path, end to end.** A live table is offered by the chat picker
(`GET /api/db_tables`, rows carry `mode`) and accepted by
`POST /session/db_tables` like a snapshot table; the chat meta entry is the
same meta-only entry. At question time `ChatDataStore.load_dataframes
(include_live=True)` partitions the DB entries by the registry's mode: a
snapshot entry loads its parquet as before, a live entry NEVER reads a
parquet (one kept from before the live period is never served) and enters
`dfs` as an empty typed placeholder under the same df key. `schema_docs`
marks it `live` with the connector dialect, the effective row cap and
whether an administrator row filter applies, and `schema_text` renders
`[LIVE, dialect=<key>, row_cap=<n>]` plus a one-SELECT contract sentence.
Before the planner is called, the database keys (snapshot and live alike;
connectors exempt, uploaded files ungated) the requester's role does not
cover are dropped from the frames and the schema (`_drop_uncovered_db_keys`;
a turn left with nothing ends with a denial sentence and no brain call) — so
a share recipient without the data role gets no answers from the chat's
snapshot tables either. The
plan request carries `live_tables` `[{name, dialect, row_cap, filtered}]`;
the response may carry `sql` `{df key: SELECT}`. Immediately before EVERY
sandbox call (`run_chat_local._ensure_live`, idempotent, retries and
regenerations included), each live key the code references is fetched: the
role gate runs again, the brain's SELECT is checked by
`assert_read_only_query` (strict parse, CTEs allowed) with a per-table
allowlist that binds it to the one registered table, wrapped under the row
cap and run by `run_live_select` in the main application — or, without a
SELECT or behind an administrator filter, the connector's own capped
default read (`default_live_fetch`). The result frame takes the placeholder's
key and is written into the job directory for the sandbox like any other
input frame. A failed SELECT never runs the sandbox: the attempt fails with
a value-free class sentence and the retry request carries `sql` (what ran)
and `sql_error` (the class; the driver's text stays local); a retry that
brings no new SELECT for that key is a failed attempt, never a default
read. The AI history row persists `sql` (per key, `null` for a default
read), `live_truncated` and `live_rows`, and durable full-table records
carry the `sql` subset their code references (the `df` alias of the
first frame counts as a reference). Per-item refresh, dashboard tile
refresh, "Show data" (a chart's or a dashboard tile's) and "Download Excel"
re-run the stored SQL only under the requester's role, before the stored
Python runs: an item whose code references a live table the role does not
cover — by the same rule the pre-fetch uses (the key quoted anywhere, a
generic `dfs` walk, the `df` alias of the first frame) — is refused with no
database query. When the role check itself fails, all four refuse on a chat
that holds a live table (or whose live status cannot be read) and keep
working on a chat without one; "Show data" and "Download Excel" also serve
a record that carries no code. A retry's `sql` carries only what ran. Results are capped by rows
(`LIVE_RESULT_ROW_CAP`) and by size (`LIVE_RESULT_MAX_MB`) and bounded by
`LIVE_QUERY_TIMEOUT_S`; a capped result adds a localized note to the answer.

```
brain ──(plan: live_tables)──▶ ┌ pdc-client (web) ──────────────────────────┐
      ◀──(response: sql)────── │ role gate → guard + per-table allowlist    │
                               │ → wrap under the row cap → run_live_select │──▶ customer DB
                               │ → frame under the df key → per-job parquet │◀── rows (capped)
                               └────────────────────────┬───────────────────┘
                                                        ▼
                                              pdc-executor (sandbox)
                                      sees an ordinary input frame; no driver,
                                      no credential, no route to the database
```

Still to come: the planner prompt on the brain (until it ships no `sql` is
returned and every referenced live table takes the default capped read);
Auto Analytics loads its frames without live tables (`include_live` off) and
so never queries one; a live connector table is never auto-included by the
relations closure. Design and status: `docs/LIVE_TABLES_PLAN.md`.

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
  blocked per-table via `routes.chat._role_refresh_block`: for EVERY denied
  table, snapshot and live alike, the item's code is scanned with the
  pre-fetch's own referencing rule (the `dfs['…']` key regex the frontend
  freeze uses, a quoted key anywhere, a generic `dfs` walk, the `df` alias
  of the first frame) and a reference refuses with `ROLE_DENIED` naming the
  table; an item touching only allowed tables still refreshes. The rule errs
  toward refusing — `df` counts as the first frame even when the code
  rebinds it, and a quoted string equal to a denied table's df key counts
  as a reference. The frontend's pre-freeze recognises only `dfs['…']`, so
  other references are refused at click time with the same role message.
  Denied frames are also dropped from the exec namespace after the
  (per-chat, user-agnostic) cached load, as defence in depth. Dashboard
  denials are caller-specific and never persisted (mirror of
  `access_revoked`). Genuine denials fail CLOSED (Base defaults). Once a
  denial is known, a failure loading the chat's frames or applying the rule
  (`ROLE_GATE_REFERENCE_FAILED`) refuses naming every denied table. An
  earlier unexpected gate crash (`ROLE_GATE_FAILED` logged)
  refuses, naming no table, on a chat that holds a live table or whose live
  status cannot be read — a stored SELECT must never run for a requester
  whose role was not checked — and fails OPEN on a chat without one.
- `GET /api/chat/{id}/schema` — advisory per-table `allowed` flag so the /lab
  and dashboard-view UIs grey refresh buttons proactively.
- `chat/stream` and `edit_regenerate` — not refused, but every
  non-connector database table (snapshot and live) the requester's role does
  not cover is dropped from the frames and the schema before the planner
  (`_drop_uncovered_db_keys`; nothing left → the denial sentence, no brain
  call). Uploaded files are not gated. So a share recipient without the data
  role cannot compute new answers from the chat's snapshot tables.
- **Not gated by design** (confirmed decisions — no retroactive blocking;
  snapshot data the user could already see stays viewable):
  full-table/Download-Excel re-execution of SNAPSHOT data
  (a LIVE fetch on those two routes is gated, and fails closed on a chat
  holding a live table), Auto Analytics,
  `add_data_to_chat` (its DB entries were validated at selection time), and
  the central nightly snapshot scheduler. Shared-chat/dashboard recipients
  keep VIEWING stored answers and snapshots; fresh computation is gated,
  keyed on the requester.

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
`user.set_permission` / `user.sessions_ended` / `user.removed` / `role.*`),
plus the Users + Roles sections on the
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
anyone). Each row also offers, after a confirmation, **End sessions**
(`POST /api/admin/users/end_sessions`: a new session generation, so every
session of the account ends on its next request; the password is untouched)
and **Remove** (`POST /api/admin/users/remove`: deletes `users/<email>/` —
held roles included — and every chat the account owns, deactivated ones
included; its sessions end, and recipients of its chats and dashboards meet
the existing owner-vanished paths. It then takes the address off every
other owner's chat and dashboard share list and removes `registered_by`
from every table it registered, connectors included — such a table reads
as administrator-registered from then on — while `descriptions_confirmed_by`
and the audit rows keep their attribution; these steps are best-effort —
each logs its own per-item failures and reports what it achieved in the
response and the `user.removed` audit row, so a partial failure shows as a
lower count, not an error).
Every account is created with a fresh session generation
(`AuthStore.create_account`, behind invitation, share placeholder, first
Microsoft sign-in, demo self-registration and the bootstrap admin), so no
session of a removed account matches an account created again at the same
address, and that account normally starts empty (a step that failed —
e.g. an owned chat that could not be deleted — is visible in the counts);
an account created by an earlier release without a generation keeps
reading `""` until its first password write or End sessions.
Every other `AuthStore` writer of the account's records and sidebar rows
refuses, under the store lock the removal also holds, an address with
neither profile nor auth record (writes nothing, logs
`ACCOUNT_WRITE_REFUSED`), so a request still in flight at the removal cannot
re-create the account, and a sign-in finishing after it starts no session
(or one the gate ends on its next request). Dashboard writes are not
guarded: an in-flight dashboard change can re-create
`users/<email>/dashboards/`, which an account created later at that address
would find.
Neither targets the bootstrap account, and an admin cannot remove their own.
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
absent = ladmin-registered/legacy, or released when the registrant's
account was removed) must equal the power user
(`403 NOT_OWNER`). Connection lifecycle, the global refresh schedule,
users/roles/permissions and the audit tail stay strictly ladmin-only
(`_require_admin`). Power-user writes are labeled
`actor_kind: "power_user"` in their audit detail so ladmin can tell them
apart in the tail; helpers: `roles_store.is_power_user` (delegates to
`AuthStore.is_power`) / `management_scope_for` (None unless the permission,
else the union) / `can_manage_physical` / `manageable_table_ids_for`
(connectors INCLUDED — management is about the registry, unlike read
access) / `scope_covers`, all fail-closed.

### 10c. Identity — Microsoft Entra ID SSO (optional, client-side only)

The identity provider is the **customer's own Entra tenant**. The client
speaks standard OIDC (authorization-code flow via authlib in
`routes/sso.py`): PDC never sees a password. From the validated ID token it
reads the address (`preferred_username`, fallback `email` for a member of
the tenant; a guest — `#EXT#` in `upn` or `preferred_username` — is
addressed by `preferred_username` only), and the tenant id + object id
(`tid`, `oid`, both required GUIDs). An account is BOUND to that tid+oid at
its first Microsoft sign-in (`auth.json` `sso_tid`/`sso_oid`,
`AuthStore.bind_sso_identity`); afterwards the identity reaches the same
account whatever username it presents (`find_sso_account`), and a different
identity presenting a bound address is refused (403). Invited users, share
placeholders and accounts from before binding are bound at their next
Microsoft sign-in. A Microsoft sign-in does NOT create an account: an
identity matching none is refused (401) unless `SSO_AUTO_PROVISION` is set,
and a guest is refused (403) unless `SSO_ALLOW_GUESTS` is set (both env
settings, default false). SSO is therefore a way an account comes to exist
only under `SSO_AUTO_PROVISION`; otherwise accounts come from an
administrator's invitation or a share, since a password sign-in never
creates one either. Who may complete a Microsoft sign-in at all is decided
in Entra — "Assignment required? = Yes" on the enterprise application is a
mandatory install step; the client holds no allow-list. Nothing about SSO
crosses to the brain except the normal `login` activity event that password
logins already post.

The connection configuration is DATA, not env: `DATA_ROOT/sso_config.json`
(`sso_store.py`), managed entirely from the ladmin "Single sign-on" panel —
the client secret is Fernet-encrypted at rest with the SAME
`CLIENT_ENCRYPTION_KEY` as DB credentials (no key ⇒ refuse to save, never
plaintext), masked in every API response, never logged or audited in
cleartext. Enabling requires a passed connection test for the currently
saved values (hash-gated); enable/disable/edit take effect on the next
request with no restart. While disabled, the `/auth/microsoft*` routes 404
and the landing page is byte-identical to a pre-SSO build. SSO sessions are
browser-session cookies (`remember=False` — Entra re-auth is silent on
joined devices); logout is local-only (no Microsoft front-channel logout,
documented). The bootstrap ladmin keeps password login (`/?local=1` is the
always-on escape hatch when auto-redirect is enabled). An account that
signed in with Microsoft and holds no local password can never obtain one
(reset, password change and invite all refuse it, `AuthStore.is_sso_only`),
because a local password would bypass Entra's MFA and conditional access.
For the same reason, while SSO is ENABLED an account whose record carries
`sso_provider` cannot use a local password it also holds
(`routes.auth._sso_enforced_for`, read per request): the sign-in form
answers the neutral failure, a reset request mints nothing and a reset
link is refused. The bootstrap ladmin is exempt, and disabling SSO restores
those passwords — nothing is deleted. The enforcement starts at an
account's FIRST Microsoft sign-in: a password account that has never
signed in with Microsoft keeps signing in with its local password while
SSO is enabled, and is not subject to Entra's MFA or conditional access
until it signs in with Microsoft once.
Customer guide: `docs/SSO_MICROSOFT.md`.

---

## 11. Sharing restriction

Sharing is restricted to the company's own domains, enforced by the client
alone (no brain-side list is consulted). The share routes
(`POST /api/chat/{id}/share`, `POST /auth/conversations/{conv_id}/share`,
`POST /api/dashboards/{id}/share`) check every recipient against the allowed
domains before anything is written (`routes.auth.share_recipient_refusal`):
the client setting `SHARE_ALLOWED_DOMAINS` when set, otherwise the domains of
the existing administrator accounts, or, when no administrator has an email
address, the domains of every account with one, derived at call time. One recipient
outside them refuses the whole request (`400`, code
`RECIPIENT_DOMAIN_NOT_ALLOWED`): nothing is shared, no account is created, no
mail is sent; with no allowed domain at all every share is refused. Beyond
that: only the owner of a chat can share it or one of its conversations, a
dashboard share grants only the chats the dashboard's owner owns, and an
allowed address that has never signed in gets a password-less placeholder
account, so the share cannot be claimed by whoever types that address at the
sign-in page first. Password sign-in never creates an account at all; the
placeholder's owner sets a password through a mailed single-use link — or,
for a placeholder created while Microsoft SSO is enabled, signs in with
Microsoft (the placeholder is SSO-only and the reset page refuses it).

Shares can be revoked: the chat owner removes one recipient with
`DELETE /api/chat/{id}/share/{recipient}`, and a dashboard unshare also
revokes the source-chat grants that dashboard's share created (recorded on
the dashboard as `sharing.chat_grants`), keeping any grant that predates it,
that another of the owner's dashboards still shared with the recipient
needs, or that the owner has since made by sharing the chat directly. A
recipient's own data role gates the chat's database tables on every new
question (see the role-gate list above).

### 11a. Rendered content isolation

Chart and table markup is data a user can influence — a planner answer, the
code the analysis sandbox ran, or a tile another user pinned — and it is shown
to other users through shares. It is therefore never given the page's origin:

- **Script-bearing charts (Plotly)** render only in an iframe with
  `sandbox="allow-scripts"` and nothing else, loaded from a dedicated route.
  One builder (`PDCViewers.setChartFrame` in `static/vendor/viewers.js`)
  creates every chart frame: it registers the chart HTML with
  `POST /api/charts` and points the frame at the returned `/charts/{token}`
  (a signed token, valid 30 minutes, bound to the user who registered it;
  the document is fetched by the id inside the token from a bounded
  in-memory store — the web server runs one worker). That route serves the
  document with its OWN policy:
  `sandbox allow-scripts; default-src 'none'; script-src 'self' 'unsafe-eval'
  'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; frame-ancestors
  'self'`. Inline script is allowed because every script of a chart document
  is meant to run inside the sandbox; there is deliberately no nonce, since a
  nonce would also let a stored document load script from any host, while
  `'self'` limits external scripts to this server.
  The `sandbox` directive and the frame's sandbox attribute each give the
  document an opaque origin: no cookies, no storage, no access to the parent
  page, and no request that carries the user's session (the only fetch it
  may make is a script from this server, i.e. the Plotly bundle from
  `/static/`). One channel no Content-Security-Policy directive governs in
  current browsers remains: WebRTC (`RTCPeerConnection` to an outside STUN
  host). Plotly's WebGL traces (scatter and line charts over 1000
  points) compile code at run time, which is why `'unsafe-eval'` exists here
  and only here. The document is served exactly as stored.
- **Script-free tables (pandas Styler `styled_html`)** are inserted into the
  page itself, so they are sanitised on the server
  (`html_sanitize.clean_styled_html`, nh3): table elements only, `T_`-prefixed
  ids and the class names pandas Styler generates, a CSS property allowlist
  with colour functions only, no `!important`, and bounded sizes (box sizes,
  margins, padding, font size, line height, border widths and spacing, text
  indent), `<style>` rules scoped to `#T_…`, no event handler, link or URL.
  The chat page's table container also clips what it holds. It runs where the markup is produced, where a tile is
  pinned or refreshed, and wherever stored history or a tile is served, so
  data stored by earlier releases is covered without being rewritten.
- **Matplotlib charts** are base64 PNGs shown as `data:image/png` images; no
  HTML path exists for them.
- **Every authenticated page carries a nonce-based Content-Security-Policy**
  (`script-src 'self' 'nonce-…'`) and NEVER `'unsafe-inline'` or
  `'unsafe-eval'` for scripts. The split is deliberate: the pages that hold
  the session run no evaluated code at all; the chart documents that need
  evaluation run it only inside a sandboxed, opaque-origin document served by
  `/charts/{token}`. The page policy is not added to a response that already
  carries its own policy. The chart HTML stored in history and on tiles is
  unchanged.

The analysis sandbox still produces chart HTML exactly as before (Article XIV
treats its output as untrusted input); the isolation is applied where the
markup meets a browser.

---

## 12. What remains explicitly OPEN

Items still undecided. Do not invent or assume:

- Auth/token rotation policy (lifetime, automatic rotation cadence).
- How the client server reaches the brain at the network level (public
  HTTPS endpoint + token vs. VPN/private link — likely tenant-specific).
- Whether to enforce the sharing domain rule on the brain side too, as
  defence in depth (see §11; today the client alone enforces it).
- Whether the dashboard top-bar should expose a "tenant ID" badge for
  operator support.

If any of these need to be decided for a future task to make sense, **ASK
in plain chat first** — do not put assumptions into code or into a prompt.
