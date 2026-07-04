# Client-side endpoints

The on-prem client serves the existing `/lab` page (copied verbatim from the
B2C app). `dashboard.js` calls a particular set of backend endpoints; this
file documents the enterprise client's implementation of each one.

> **Why this matters:** the dashboard expects the B2C internal API shape. A
> verbatim template copy without the matching backend is just a visual shell.
> The fix is to implement the same endpoints the B2C app exposes (or stub
> them gracefully where the feature does not exist on-prem). See
> "Lesson learned" at the bottom.

---

## Pages (HTML)

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/` | auth landing (email + password + "Remember me"); redirects to `/lab` if already signed in (or to `/auth/change_password` when a forced change is pending) |
| `GET` | `/lab` | the dashboard page (no session → `/`; `must_change_password` pending → `/auth/change_password`) |
| `GET` | `/c/{conv_id}` | deep-link / hard-refresh into one conversation. Resolves the conv's `chat_id` from the caller's conversations index and seeds `open_conv_id`/`open_chat_id` so `dashboard.js` auto-opens it. No session → `/`; forced change pending → `/auth/change_password`; unknown/foreign conv → `/lab` (never 404). |
| `GET` | `/auth/change_password` | forced set-a-new-password page shown after a temp-password login (no session → `/`; no pending flag → `/lab`) |

---

## Auth (email + password, all local)

Passwords never leave this container — only a HASH is stored, at
`DATA_ROOT/users/{email}/auth.json` (`password_hash`, optional
`temp_password_hash` + `must_change_password`). Hashing:
`password_utils.py` (stdlib PBKDF2-HMAC-SHA256, werkzeug-compatible
format). Migration rule: a LEGACY user from the old email-only build
(user folder exists, no hash) must set their password through the RESET
flow — login with any password is refused with a "set your password via
reset" notice, because only the emailed temp password proves mailbox
ownership. Only a genuinely NEW email (no user folder) gets the entered
password adopted on first login. The brain is only involved as an email
relay (`/v1/send_welcome_email`, `/v1/send_password_reset_email`).

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/auth/login` | form-encoded `email=`, `password=`, `remember?`. Genuinely NEW email (no user folder) → entered password becomes the password + welcome email (fire-and-forget). LEGACY email-only account (folder, no hash) → 403 with the "set your password via Reset password" notice (never adopts the typed password). Wrong password → landing re-rendered with red "Incorrect password" + Reset action (401). Temp password → session flagged and redirected to `/auth/change_password`. `remember` → persistent ~30-day session cookie (RememberMeSessionMiddleware in app.py); otherwise browser-session cookie. |
| `POST` | `/auth/reset_password` | form-encoded `email=`. Unknown email → "This account does not exist." Known → generates a temp password locally, stores its hash + `must_change_password`, brain-relays it by mail; on relay failure the temp credential is rolled back and an error shown. The user's own password stays valid until the temp one is used (a stranger's reset request can't lock the real user out). |
| `POST` | `/auth/change_password` | form-encoded `new_password=`, `confirm_password=` — the forced-change submit (session required) |
| `POST` | `/auth/logout` | clears session, redirects to `/`. |
| `GET`  | `/auth/me` | `{authenticated, email}` |
| `GET`  | `/auth/profile` | `{username: email, email, full_name: "", subscription_plan: "Enterprise"}` — shape that dashboard.js expects |
| `POST` | `/auth/profile/update` | email is the identity; attempts to change it are silently ignored |
| `POST` | `/auth/password` | JSON `{current_password, new_password}` — real change-password (verified server-side), used by the /lab profile-dropdown modal. 401 on wrong current password. |
| `GET`  | `/auth/subscription` | constant `{plan: "Enterprise"}` |
| `GET`  | `/auth/active_chats` | list of user's chats |
| `GET`  | `/auth/conversations` | list of user's conversations |
| `POST` | `/auth/active_chats/rename` | `{chat_id, title}` |
| `POST` | `/auth/conversations/rename` | `{conv_id, title}` |
| `POST` | `/auth/conversations/delete` | `{conv_id}` |

---

## Upload flow

The dashboard's "frictionless drop" runs these four endpoints in order. The
enterprise build keeps the same shape so the existing JS works unchanged:

1. **`POST /new_session`** — resets the per-session temp `UserStore`.
   Returns `{ok: true}`. Issues a fresh SID into the session cookie.

2. **`POST /upload`** (multipart, field `files`) — saves uploads to the
   per-session temp area (under `<DATA_ROOT>/sessions/<sid>/files/`).
   Returns `{ok, saved, dataframes}` (the same shape the B2C `/upload`
   returns). **Raw bytes never leave this server.** Excel workbooks load
   only **visible** sheets — hidden and veryHidden sheets are skipped by
   `load_excel_sheets` (the single choke point every load path shares:
   chat creation, Add Data, and chat-time dataframe loading). A workbook
   whose sheets are ALL hidden yields zero dataframes and flows through
   the normal "no valid tables in file" handling.

3. **`POST /schema_autofill_full`** — verbatim port of global's
   `/schema_autofill_full` (`backend/routes/schema.py` L884-1012), split for
   the brain/client boundary:
     - Client builds per-file context locally: dtypes, sampled / truncated
       unique values, language hint (from column names), columns needing fill.
       Identical to global's `_prepare_file_context` — runs against the local
       DataFrames so raw row data never leaves.
     - Client POSTs `/v1/schema_autofill` to the brain (one call per file, run
       in parallel via `asyncio.gather` + bounded ThreadPool). Brain returns
       `{file_description, columns: {col: desc}}`.
     - Client merges results into `meta.json` and **also** generates a
       `technical_description` for every column from local pandas stats
       (dtype, fill rate, categorical/sample values) — verbatim port of
       global's `_generate_technical_description`.
   Returns `{ok, filled: <total>, files: [...], updated: <total>}`. Failure of
   any single file falls back to leaving descriptions blank (just like global)
   and the technical_description step still runs.

4. **`POST /generate_chatdata`** — clones the temp `UserStore` into a
   permanent `ChatDataStore` under `<DATA_ROOT>/chatdata/<chat_id>/`,
   records the chat in the user's `active_chats.jsonl`, and calls the
   brain's `/v1/chat_metadata` endpoint. That endpoint is a verbatim port
   of global `_generate_all_parallel` (3 parallel sub-calls: chat name,
   welcome message, suggested questions) — same prompts, same sanitizers
   — so the output is identical to the B2C app. Returns
   `{ok, chat_id, name, welcome_message, suggested_questions}`.

Direct-to-GCS paths (`/upload/init`, `/upload/finalize`, `/upload_from_url`)
return `400` — the on-prem build uses the standard path for all sizes.

### Add Data to an existing chat

**`POST /add_data_to_chat`** — body `{chat_id}`. Triggered by the **Add Data**
button in the chat topbar (left of "View / Edit Descriptions"; the Create-New
modal reopens in "add" mode: title `Add Data To "<chat name>"`, primary button
**Upload**). The frontend runs the SAME preprocessing pipeline as chat
creation — `/new_session` → `/upload` (table detection) →
`/schema_autofill_full` (descriptions) — and then calls this endpoint instead
of `/generate_chatdata`. It merges the temp session store into the existing
`ChatDataStore`:

- Raw files are copied into `chatdata/{chat_id}/files/`; a filename that
  already exists in the chat is **silently overwritten** (intentional — acts
  as a data update), and its existing meta/schema/description entry is kept
  as-is. New filenames get their autofilled meta entries appended.
- The user's `active_chats.jsonl` record gets its `files` list refreshed
  (sidebar subtitle).
- Nothing else changes: `schema_text` is rebuilt from all loaded dataframes on
  every question, so added files are immediately visible to generated code and
  to the View / Edit Descriptions modal (which re-fetches
  `/api/chat/{id}/schema` on open).

Requires an authenticated session with access to `{chat_id}` (owner or shared
recipient). `400` when no files were uploaded in the session; raw data never
leaves the client.

---

## Chat (SSE stream)

**`POST /api/chat/{chat_id}/chat/stream`** — body `{question, conv_id?}`.

The endpoint is implemented as a real SSE stream (`text/event-stream`), same
content-type and event shape as the B2C `chat_stream_api`:

```
data: {"progress": true, "message": "Working...", "conv_id": "cv_..."}\n\n
data: {"done": true, "partial": false, "conv_id": "cv_...",
       "answer": "...", "image_base64": "...", "table": {...},
       "tokens": {...}}\n\n
```

`{partial: true, answer, image_base64, chart_n, chart_total}` is part of the
contract (for multi-chart responses) — the on-prem build currently emits a
single final event but the frontend handles both paths identically.

On kill-switch (tenant revoked / suspended), the stream emits a single
`{error: "Service unavailable. Please contact your administrator.", done: true}`
event and the chat UI surfaces it to the user.

**`GET /api/chat/{chat_id}/conversation/{conv_id}/status`** → `{"generating": bool}`.
Lightweight in-memory registry lookup (no I/O): `true` while a generation worker
is still running for that conversation. The generation worker persists the AI
turn regardless of the connection, so a page reloaded/reopened mid-generation
uses this (polled, mirroring the Auto Analytics status pattern) to show the
working indicator, block new questions, and auto-render the answer on completion
without a second manual refresh. Unmarked only after the AI turn is persisted —
so once it returns `false`, the turn is already readable from history.

**`POST /api/chat/{chat_id}/conversation/{conv_id}/stop`** → `{"ok": true, "stopping": true}`.
Requests cancellation of an in-progress generation (the send button becomes a
Stop button while generating). **Instant for the user, cooperative on the
server.** The client abandons the live stream the moment Stop is clicked —
aborts the SSE reader (so NO further or late events render, killing both a late
chart and the planner "couldn't generate…" fallback), finalizes the in-progress
message (keeps any charts already rendered and appends a subtle "⏹ Response
stopped by user" note), and re-enables the input immediately — all WITHOUT
awaiting this endpoint (it is POSTed fire-and-forget). Server-side the worker
checks the flag between charts and halts at the NEXT chart boundary (the
in-flight chart finishes), then persists a **STOPPED** turn, which is
authoritative on reload: ≥1 chart → the partial charts (same shape + per-chart
code/chart_data) with the stopped marker; 0 charts → a short `"Response stopped
by user."` turn. When cancelled it never persists the NO_CODE / "couldn't
generate analysis code" fallback or a late single-shot result. Exactly-once
persistence and the in-progress/cancel flags (cleared in `finally`) are
preserved. Idempotent; setting the flag when nothing is running is a harmless
no-op.

The full enterprise split inside one turn:

1. Client loads dfs from local disk + builds schema text (`_schema_text` port).
2. POST → `/v1/plan` → brain returns code.
3. Client executes code locally with `safe_execute` / `render_plot_safe`.
4. On execution error → POST `/v1/retry` (up to 2 retries), client retries.
5. POST → `/v1/describe` (or `/v1/summarize` for scalar results) → brain
   returns the natural-language intro. No row values cross the boundary.

---

## Reports (rendered locally, narrative from brain)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/chat/{chat_id}/conversation/{conv_id}/download_report` | PDF (ReportLab + DejaVu fonts) |
| `POST` | `/api/chat/{chat_id}/conversation/{conv_id}/download_pptx` | PPTX (python-pptx) |

Both call `/v1/report` (no values) to get `report_structure` JSON, then merge
the narrative into the client's own template locally.

**Per-tenant PowerPoint template (PPTX exports + Auto Analytics):**

When the operator uploads a branded `.pptx` for a tenant in the brain admin
panel, the client renderer (`client/routes/report._render_pptx`) opens that
file as the base presentation, inheriting the tenant's slide master, theme
colors, and fonts natively through python-pptx. The brain's COMPLEX-tier
analyzer emits a strict **v2 build plan** that the renderer consumes
verbatim:

- The plan picks three template slides — `deck.cover_slide_index`,
  `deck.agenda_slide_index` (optional), `deck.content_slide_index` — and
  labels EVERY SHAPE on each of those slides exactly one of:
  `keep`, `drop`, `replace:title`, `replace:body`, `replace:agenda`.
- Shapes labeled `replace:*` carry a `text_style` (font, size, bold,
  color, align) the renderer applies verbatim.
- The content slide also carries a `chart_region` (inches) where the
  chart is dropped.

For every export the renderer deep-clones the cover slide once, the
agenda slide once (if present), and the content slide ONCE PER FINDING.
On each clone it applies labels by `shape_id` (drop → remove the
element; keep → untouched; `replace:*` → overwrite text using
`text_style`), then drops the chart on content slides at `chart_region`.
Every ORIGINAL template slide is removed before saving so the template
author's own tables / sample bullets / author names never appear in the
output — only chrome (logos, headers, page numbers, dividers) plus the
report's title / narrative / chart in the declared spots.

Templated decks **intentionally omit the PowerDataChat logo** — only the
tenant's own branding shows through. The client fetches both the template
file (`GET /v1/pptx_template`) and the v2 spec (`GET /v1/pptx_template_spec`),
caching them on `DATA_ROOT/templates_cache/` keyed by a schema marker
(`*.v2.pptx`, `*.v2.json`) with a short TTL so a re-upload is picked up
without a client restart. If the v2 plan is unusable
(`spec.version != 2`, missing cover/content, no chart_region, render
exception) the renderer falls back to the built-in PowerDataChat-branded
deck and logs `PPTX_TPL_FALLBACK reason=...`. Auto Analytics reuses the
same `_render_pptx` path, so templates apply to it automatically.

---

## Downloads (chart PNG + table Excel — rendered locally)

The per-chart **Download** button and the per-table **Download Excel** button
post to these three routes. Both the chart render and the `.xlsx` build happen
on the client — **raw data never leaves this server** (Article II); nothing here
calls the brain.

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/api/chat/{chat_id}/export_plotly_png` | Body `{html, filename, scale}`. Renders the interactive chart's raw Plotly HTML to a high-resolution PNG server-side (via `routes/report._plotly_html_to_png`, kaleido) and returns `image/png` as an attachment. `400` when `html` is missing; `502` if the chart cannot be rendered. |
| `POST` | `/api/chat/{chat_id}/download_excel/{key}` | Body `{filename}`. Streams the full result table cached under `{key}` (the `full_table_key` / `chart_data_key` the chat stream emits — the same bounded LRU as `full_table`) as an `.xlsx` spreadsheet. Returns `404 {"error": "Table not found or expired."}` when the key is missing/expired; `502` on build failure. |
| `POST` | `/api/chat/{chat_id}/export_excel` | Body `{columns, rows, filename}`. Builds an `.xlsx` directly from the posted preview table and returns the spreadsheet mime. `400` when no table data is posted. |

All three require an authenticated session with access to `{chat_id}`. `.xlsx`
files are built with pandas + openpyxl. Matplotlib/seaborn charts are already
PNGs, so their Download is a pure client-side save (no route).

---

## Per-chart / per-table refresh (local re-execution)

**`POST /api/chat/{chat_id}/refresh_item`** — body `{code, kind: "chart"|"table"}`.

Every chart and table that carries its own stored `code` (live events and
persisted history records both do) gets a small refresh icon button (double
curved arrows) in its action bar. Clicking it re-runs ONLY that item's stored
code against the chat's **current** dataframes — purely local re-execution via
`render_plot_safe` / `safe_execute` (the same path `_reexecute_full_df` uses);
**no LLM/brain call** — and swaps the chart image / table content in place.
Purpose: after updating a file via Add Data (overwrite), existing items can be
refreshed to reflect the new data.

- Charts return `{ok, image_base64, is_plotly, chart_data_key?}` — the fresh
  `chart_data_key` re-points "Show data" at the refreshed values; Plotly
  "View Larger" / "Download" follow the updated HTML automatically.
- Tables return `{ok, table, full_table_key?}` — the block is re-rendered
  (styled_html included when the code yields a pandas Styler) and "Download
  Excel" is rebound to the new durable key.
- Legacy history records (saved before per-chart code persistence) carry only
  the joined `###NEXT_PLOT###` record-level code. The frontend SPLITS that code
  on the marker when rendering history and assigns segment *i* to chart *i*, so
  legacy charts are refreshable per segment (and Show code shows the clean
  segment). Only items with NO code at all — or an ambiguous multi-segment
  code that can't be matched to the item — show no button. The endpoint keeps
  rejecting joined code with `400` as a guard; the frontend always sends a
  single clean segment.
- Auth/permission failures use HTTP codes; **execution** failures return
  `200 {ok: false, error}` — the frontend keeps the previous render and shows
  a small non-blocking note.

---

## Endpoints that exist purely to keep the page non-broken

The B2C dashboard.html references B2C-only features that don't exist on-prem.
Rather than ripping JS out, the client returns clean errors so the UI
gracefully handles them:

| Endpoint | Returns | Reason |
|---|---|---|
| `POST /api/chat/{id}/publish`, `/unpublish` | 400 | no public pages on-prem (architecture: sharing is OPEN, public publish is out of scope) |
| `POST /upload/init`, `/upload/finalize` | 400 | direct-to-GCS path only |
| `POST /upload_from_url` | 400 | Google Drive/Sheets are off-prem |

> Auto Analytics (`*/auto_analysis/start|status|download`) is **implemented** on-prem
> (brain-side planner + client-side execution + PPTX render). See the "Implemented
> on-prem" table below for the full row.

---

## Schema endpoints (session-level)

`dashboard.js` calls these between `/upload` and `/generate_chatdata` to
populate the column-edit form. They are verbatim ports of global
`/schema_details` and `/schema_common_fields` — pure pandas, no LLM, no row
values leave the client.

| Method | Path | Behavior |
|---|---|---|
| `GET`  | `/schema_details` | per-column stats (dtype, nunique, sample unique values, needs_description), with sampling for >50k-row datasets and ThreadPool fan-out for large col counts |
| `GET`  | `/schema_common_fields` | auto-detected join columns across multiple uploaded files (fuzzy name matching + dtype + cardinality) |
| `POST` | `/schema_common_fields` | persist user-confirmed join relationships into `meta.json["common_fields"]` |
| `GET`  | `/schema` | full session `meta.json` (file list + per-file schema) |
| `POST` | `/schema` | save schema edits (`files[].fields[].description`, `file_description`) |

---

## Implemented on-prem (replaces previous stubs)

| Endpoint | Behavior |
|---|---|
| `POST /api/chat/{id}/share` | adds recipients to `meta.json["sharing"]["shared_with"]`, asks brain `/v1/send_share_email` to SMTP-relay invites using this tenant's SMTP config |
| `GET  /api/chat/{id}/share` | returns the current sharing record (`{shared_with, owner}`) |
| `POST /auth/conversations/{conv_id}/share` | **conversation-level share** — for each recipient, snapshot the conversation history into a fresh `conv_id` via `ChatDataStore.copy_conv_to_new`, add them to the chat's `sharing.shared_with`, record the new conv in the recipient's `conversations.jsonl` with title prefix "(Shared) …" and `shared_by` field, then SMTP-relay an invite. Recipients access the chat through `_require_chat`'s shared-recipient check |
| `GET  /api/chat/{id}/full_table/{key}` | returns the full result table cached under `key`. The chat stream sets `full_table_key` on responses that contain a tabular result. Backed by a bounded in-memory LRU (256 most recent results) |
| Conversation title generation | After the 2nd human message, the chat stream fires a background `brain_client.title()` call and renames the conversation via `AuthStore.rename_conversation` |
| Activity logging | `auth.py` (login), `upload.py` (file_uploaded), `chat.py` (plot_generated, per chart), `report.py` (report_exported), `auto_analytics.py` (auto_analytics_completed) all call `brain_client.post_activity` → brain `/v1/activity` |
| **Auto Analytics** | `POST /api/chat/{id}/auto_analysis/start` kicks a background job → brain `/v1/auto_analytics_plan` (planner returns 3-15 natural-language analytical instructions) → client executes each via `run_chat_local.run_chat` against the local dataframes (bounded 4-worker pool) → brain `/v1/report` for narrative → client renders PPTX via `routes/report._render_pptx` → persists to `chatdata/{id}/auto_analysis.pptx`. `GET /auto_analysis/status` reports `{status: idle|processing|done, progress, error, pptx_path}`. `GET /auto_analysis/download` streams the deck. Raw row data never leaves the client |
| **Multi-chart streaming** | The chat SSE stream uses `run_chat_multi_plot` (a generator port of global's). The brain Agent classifier sets `suggested_approach` to "Decompose into multiple PLOT_CODE blocks ... separated by ###NEXT_PLOT###" for dashboard/overview-style queries; the planner emits the multi-block raw_text; the client splits it via `_extract_multi_plot_blocks` and executes each block locally with retry, yielding a `{partial: true, chart_n, chart_total, image_base64, answer}` SSE event per chart and a final `{done: true}` combined event. Capped at 6 charts per response. Single-chart queries fall through to the existing one-shot path |
| **Edit-regenerate** | `POST /api/chat/{id}/edit-regenerate` — verbatim port of global's `edit_regenerate_api` (`backend/routes/chat.py` L1136-1296). dashboard.js fires this from the pencil-edit affordance on a past user message. Server-side: find last `human` turn, `truncate_conv_history` to drop it (and everything after), append the edited human turn, run `run_chat_multi_plot` against the local dfs, persist the AI turn in the same shape the SSE stream uses (`image_base64` for 0/1 charts, `images: [...]` for 2+). Returns a single JSON (NOT SSE — global's is also a one-shot JSON response) |
| **Chart persistence across refresh / reopen** | `routes/chat.py` accumulates each multi-plot partial's `(image_base64, answer)` while streaming. On the final `done` event the AI turn is appended to `conversations/{conv_id}.jsonl` using global's shape (`backend/routes/chat.py` L1088–1105): 0 images → no image fields, 1 image → top-level `image_base64`, 2+ images → `images: [{image_base64, answer}, ...]`. The single-chart path already stored `image_base64` directly. `dashboard.js` reopens the conversation via `GET /api/chat/{id}/conversation/{conv_id}/history`; lines 1551 and 2184 render `msg.images` as one assistant bubble per chart, otherwise render `msg.image_base64` — identical to global. **Bug fixed (May 2026):** multi-plot history previously persisted `image_base64: null` and no `images` field, so charts vanished on refresh |

---

## Lesson learned (recorded so we don't repeat it)

In the first cut, the team copied `dashboard.html` verbatim and built a
parallel **minimal** backend on the side. The smoke test then exercised
THAT side-API by curl. The result: the visual page rendered, but every
JS action (drag-drop, file picker, chat send, profile save) silently
failed because nothing was wired to the endpoints `dashboard.js` actually
calls.

The right move is to always treat the page as a contract: every endpoint
listed in this file must either be implemented or stubbed to return a
clean error code so the UI can handle it. Direct browser click-through
verification is mandatory before declaring the page working.
