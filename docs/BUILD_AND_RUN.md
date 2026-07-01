# Build & run

Both containers are **independently buildable**. The brain is intended to run
on GCP (PowerDataChat side); the client runs in the customer's LAN.

For local development they are convenient to run together — see
`docker-compose.yml` at the root of `/enterprise`.

---

## 1. Build the BRAIN container

```bash
cd enterprise/brain
docker build -t powerdatachat-brain:enterprise .
```

Run it:

```bash
docker run --rm -p 8080:8080 \
  -e GOOGLE_API_KEY="$YOUR_GEMINI_KEY" \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -e ADMIN_DEFAULT_PASSWORD="changeme" \
  -v $(pwd)/brain_data:/data/brain \
  powerdatachat-brain:enterprise
```

The brain listens on **port 8080**. Endpoints:

| Path                                          | Purpose                                       | Auth                   |
|-----------------------------------------------|-----------------------------------------------|------------------------|
| `GET /health`                                 | Liveness                                      | none                   |
| `POST /v1/*`                                  | LLM endpoints (see `PROTOCOL.md`)             | `Bearer <tenant_token>`|
| `GET /admin/*`                                | Operator console (tenant mgmt, LLM settings)  | session (admin user)   |
| `GET /admin/api/domain-skills`                | List shared domain skills (active + inactive) | session (admin user)   |
| `POST /admin/api/domain-skills`               | Author a new shared domain skill              | session (admin user)   |
| `POST /admin/api/domain-skills/{id}/status`   | Activate / deactivate a domain skill          | session (admin user)   |

Open `http://localhost:8080/admin/login` and sign in as `admin` /
`<ADMIN_DEFAULT_PASSWORD>`. Change the password immediately under
**"Admin password"**.

---

## 2. Build the CLIENT container

```bash
cd enterprise/client
docker build -t powerdatachat-client:enterprise .
```

The client image is heavier because it ships pandas, matplotlib, plotly,
kaleido, scikit-learn, python-pptx — every library the user's generated
code might run on the user's data.

To run the client you need a **tenant token from the brain**. Create one
through the brain's admin panel — when the token is created it is shown
ONCE and never again. Save it.

```bash
docker run --rm -p 8000:8000 \
  -e BRAIN_URL="https://brain.your-domain.example.com" \
  -e BRAIN_TENANT_TOKEN="<token-from-brain-admin>" \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -v $(pwd)/client_data:/data/client \
  powerdatachat-client:enterprise
```

Open `http://localhost:8000` → enter your work email → land in `/lab`.

---

## 3. End-to-end smoke test (native, no Docker)

Useful while iterating. From a clean checkout:

```powershell
# Terminal A — brain
cd enterprise\brain
$env:GOOGLE_API_KEY = "<your-key>"
$env:ADMIN_DEFAULT_PASSWORD = "test12345"
$env:SECRET_KEY = "dev-secret"
$env:BRAIN_STORAGE_ROOT = ".\brain_data"
python -m uvicorn app:app --host 127.0.0.1 --port 8085

# Browser: http://127.0.0.1:8085/admin/login
#   user: admin, pass: test12345 → create tenant → copy token

# Terminal B — client
cd enterprise\client
$env:DATA_ROOT = ".\client_data"
$env:SECRET_KEY = "dev-secret-client"
$env:BRAIN_URL = "http://127.0.0.1:8085"
$env:BRAIN_TENANT_TOKEN = "<token-from-brain-admin>"
python -m uvicorn app:app --host 127.0.0.1 --port 8001

# Browser: http://127.0.0.1:8001 → enter email → /lab
```

Verification of this exact path was performed during development:
landing → email login → upload `sample.csv` → schema endpoint → chat
endpoint → conversation history. The brain returned token-validated
responses, and revoking the tenant returned `403` on every subsequent
client call (the kill-switch).

---

## 4. Onboarding a new tenant (operator runbook)

1. Sign in to the brain admin panel.
2. Under **Tenants**, enter the company name and click **Add tenant**.
3. The panel shows the new bearer token ONCE. Copy it.
4. On the customer's installation, set `BRAIN_TENANT_TOKEN=<token>` in the
   client container's environment (e.g. inject via Docker run args, a
   `.env` file, or a Kubernetes Secret).
5. The customer's container hits `/health` on startup — verify
   `brain_reachable: true` and `tenant_token_configured: true`.
6. (Optional) Edit the tenant from the panel to set
   `enabled_skills`, `domain_skill_id`, or `prompt_tuning_planner`.
7. (Optional) Set the **welcome language** for this tenant — the language of
   the auto-generated welcome message + suggested starter questions. In the
   per-tenant page → **Application Settings** → **Welcome language**, enter a
   language instruction string, e.g. `Georgian (ქართული)` for the bank
   (`English`, `Russian (Русский)`, … also work). **Empty = brain default**
   (the client-detected language, then English — i.e. unchanged behavior).

   Equivalently via the settings API / `config.json`:

   ```bash
   curl -X POST .../admin/api/tenants/<id>/settings \
     -H 'Content-Type: application/json' \
     -d '{"welcome_language": "Georgian (ქართული)"}'
   # clear it (revert to detected language):  {"welcome_language": null}
   ```

   This writes `welcome_language` into
   `<BRAIN_STORAGE_ROOT>/tenants/<id>/config.json`; the brain applies it on
   `/v1/chat_metadata` with precedence tenant config → client hint → English.

The admin panel does **not** reach into the customer's network. It only
manages tenant records on the brain side.

---

## 4a. Adding a new domain skill (operator runbook)

Domain skills are **shared brain assets** — authoring one in the portal
makes it selectable from every tenant. Raw client data never leaves the
client; only the skill definition (YAML) lives on the brain.

1. Sign in to the brain admin panel → click a tenant row to open the
   per-tenant page.
2. Open **Company-specific extras**.
3. Click **+ New skill**. Fill in:
   - **ID (slug)** — `^[a-z0-9_]+$`. Becomes the YAML filename under
     `brain/skills/domain/<id>.yaml`.
   - **Display name** — what appears in the dropdown.
   - **Description** (optional) — short hint shown next to the name.
   - **Terminology / domain vocabulary** — free text, becomes the
     `terminology` section the planner reads as "DOMAIN KPIs".
   - **Expected columns** — one entry per line; surfaced as
     "EXPECTED COLUMNS".
   - **Code hints** — one bullet per line; surfaced as
     "DOMAIN CODE HINTS".
   - **Analysis-style guidance** (optional) — free text; surfaced as
     "ANALYSIS STYLE".
4. Click **Create skill**. The dropdown refreshes and the new skill is
   ACTIVE by default. Select it for any tenant whose
   `domain_skill_id` should point to it.
5. Use the **Activate / Deactivate** button next to the dropdown to flip
   the skill's status. Inactive skills remain visible in the dropdown
   (rendered as `name (inactive)`) but are ignored by the planner.

There is no portal-side delete/rename/edit — by design. Operators tweak
a skill by editing its YAML on the brain volume directly, or by
authoring a new one with a different slug.

### Where authored skills are stored

Operator-authored skills are written to
`<BRAIN_STORAGE_ROOT>/domain_skills/<id>.yaml` — that path lives under
the `pdc_brain_data` named volume, so the file survives container
rebuilds and restarts. Built-in shared skills (`real_estate`,
`ecommerce`, ...) continue to ship from `brain/skills/domain/` baked
into the image; on read the persisted copy SHADOWS the bundled one when
the ids collide. Editing a bundled skill's status through the portal
COPIES the YAML into the persisted dir on first edit so the image stays
clean.

If you used a `BRAIN_STORAGE_ROOT` location that is NOT volume-mounted
(e.g. an ephemeral path inside the container), authored skills will be
discarded on every rebuild — that was the symptom of the bug fixed in
2026-05-28. The default compose file mounts
`pdc_brain_data:/data/brain` and that is correct.

---

## 4b. Uploading a tenant's PowerPoint template (operator runbook)

Each tenant can ship a branded `.pptx` template that all PPTX exports +
Auto Analytics decks are built on top of. With no template uploaded the
client falls back to the built-in PowerDataChat-branded renderer (logo
included). **Templated decks intentionally omit the PowerDataChat logo —
only the tenant's master assets show through.**

1. Sign in to the brain admin panel → click a tenant row.
2. Open **Presentation template**.
3. Pick the tenant's `.pptx` file (max 25 MB) and click **Upload &
   analyze**. The brain runs a Pro-tier Gemini analysis of the deck
   structure (layouts, placeholders, theme colors, fonts, header /
   footer text) and saves the JSON style-spec.
4. The card shows the saved spec — which layout is cover vs. content,
   which placeholders hold the title and body, the chart region in
   inches, the four theme color tokens and font names. Use this to
   sanity-check the analysis.
5. To revert to the built-in renderer, click **Remove**. The template
   file and the spec are both deleted.

Storage on the brain:

```
tenants/<tenant_id>/
  pptx_template.pptx          # uploaded file bytes
  pptx_template_spec.json     # analyzer output (strict schema)
```

The client fetches both via `GET /v1/pptx_template` and `GET
/v1/pptx_template_spec` at render time and caches them under
`DATA_ROOT/templates_cache/` with a short TTL. A re-uploaded template is
picked up within ~60 s without restarting the client.

---

## 5. Revoking a tenant (kill-switch)

In the admin panel, click **Revoke** on the tenant's row. The brain
records `status: "revoked"`. Every subsequent call from that customer's
client container fails with HTTP `403`. To restore service, click
**Re-activate**.

Brand-name distinction:
- **suspend** — temporary hold (e.g. payment in progress).
- **revoke** — terminal block (non-payment, contract termination).
Both result in `403`; the difference is bookkeeping for the operator.

---

## 6. Updating LLM model selection

Brain admin panel → **LLM models (4-tier hybrid)**:
- Agent (classifier)
- Light (greetings, score 0–3)
- Simple (charts/filters/code, score 4–8)
- Complex (deep analysis, score 9–10)

Each tier has its own model + temperature + thinking toggle, identical to
the B2C `/admin` panel. Changes are saved to `admin_config.json` in the
brain's persistent root (next to `tenants/`) and applied to the live
settings object — no restart required.

## 6a. Live Gemini model discovery + health check

In the **Gemini model discovery** card on the admin panel:
- **Reload** — fetch the list of available Gemini models for this API key.
  Cached for 10 minutes; falls back to a hardcoded list if the API is
  unreachable.
- **Force refresh** — clear the cache and re-fetch.
- **Health check** — ping each model with a minimal `generateContent` call
  and show status (`ok` / `unavailable` / `slow` / `dead`) + latency.

Use this after rotating `GOOGLE_API_KEY` or to debug failing model tiers.

## 6a-bis. Tenant-list landing + tenant-scoped admin

Since the enterprise build is multi-tenant, the admin panel is structured as
two pages:

1. **Tenant list** (`/admin/`) — **strictly** the list + Add tenant + Remove
   tenant. Nothing else lives here. Click a row to open the per-tenant page.
2. **Per-tenant admin** (`/admin/tenants/{tenant_id}`) — same look and
   functionality as global `/admin` (dark theme, tabbed Settings / End-users /
   Activity), scoped to one tenant. Settings sections (8 cards):
   Agent / Light / Simple / Complex models (model + temperature + thinking),
   API Keys (per-tenant `GOOGLE_API_KEY`), Sharing Policy, Application
   Settings (MAX_FILES, TITLE_MAX_LEN, TITLE_BREAK_MIN), SMTP, Skills &
   prompt config, Danger zone. End-users tab is sortable / searchable /
   paginated (email, full name, first seen, last login, last activity).
   Activity tab shows the last 100 brain calls.

Settings on the per-tenant page are **overrides**. Leave a field empty to
fall back to the brain-wide default. All overrides are honored at LLM-call
time via a per-request context variable inside the brain — no restart is
needed for changes to take effect.

The client fetches `MAX_FILES / TITLE_MAX_LEN / TITLE_BREAK_MIN` from the
brain `/v1/app_settings` endpoint at request time, so per-tenant Application
Settings overrides propagate without redeploying the client container.

Brain-wide LLM defaults still exist (kept in `admin_config.json` in the
brain's persistent root) and are administered via the `POST /admin/api/settings`
API. They are reached by leaving every per-tenant model field empty.

## 6c. Per-tenant admin — invariants the per-tenant page enforces

- **use_thinking** is true/false only. There is no "use brain default" option
  on the use_thinking dropdown — matches global. The per-tenant page always
  sends a boolean for every tier's `llm_<tier>_use_thinking`.
- **Skills are additive only.** Every tenant gets the FULL global skill set
  (charting / statistics / time-series / ML / preprocessing / quality / output
  formatting / dashboarding) at all times. The brain's planner never filters
  these by a per-tenant subset list; the only per-tenant skill-config knobs
  are additive: `domain_skill_id` (e.g. real_estate / ecommerce), free-form
  `prompt_tuning_planner` (appended to every planner system prompt), and
  free-form `domain_vocabulary` (appended verbatim under
  "COMPANY-SPECIFIC DOMAIN VOCABULARY"). The legacy `enabled_skills` field is
  ignored by the brain — stored values from older configs are no-ops.
- **API keys**: only `GOOGLE_API_KEY` is per-tenant. `SECRET_KEY` (session
  signing) and any OAuth client secret are brain-wide infrastructure handled
  via env vars / `POST /admin/api/settings`.
- **SMTP**: per-tenant override on top of any brain-wide default. The brain
  is the SMTP relay (one decision point per tenant), called by client
  `/api/chat/{id}/share` and `/auth/conversations/{conv_id}/share` via
  `/v1/send_share_email`.

## 6d. Client UI freezes / removals

- The profile button in `/lab` is **temporarily frozen** — the partial
  `templates/partials/profile_button.html` renders the avatar + email +
  "Enterprise" plan badge but a capture-phase click handler swallows the
  click so the dropdown / modal never opens. A separate visible Logout
  button in the topbar POSTs to `/auth/logout` so users can still sign out.
  The underlying `/auth/profile*` endpoints stay live for any out-of-band
  tools.
- Enterprise has **no message-count cap**. The dashboard profile modal's
  "Messages today" line, when it is visible at all (the modal is currently
  frozen), reads "unlimited". `run_chat_local` enforces no per-request quota,
  and the brain does not rate-limit by tenant beyond the `active|suspended|
  revoked` status flag (kill-switch).

## 6e. Chart persistence in conversation history

Conversation history is stored per-conv in `chatdata/{chat_id}/conversations/{conv_id}.jsonl`.
The AI turn on a chart-producing response uses **the same shape as global**:

- **Single chart** — top-level `image_base64` on the AI message.
- **Two or more charts** — `image_base64: null`, plus
  `images: [{image_base64, answer}, ...]` (one entry per rendered chart, in
  display order).

This is what `dashboard.js` expects when it reopens a conversation
(`GET /api/chat/{chat_id}/conversation/{conv_id}/history`):
`msg.images` → one assistant bubble per chart; otherwise `msg.image_base64`.

If you tail a `*.jsonl` file in the client volume and the AI turn of a
multi-chart response shows `image_base64: null` with **no `images` key**, that
turn was written before the May 2026 fix and the charts will not reappear on
reload (the bug). Newer turns persist correctly. The fix lives in
`routes/chat.py` (`all_images` / `all_answers` accumulators on the multi-plot
SSE path).

## 6b. Per-tenant detail view (users + recent calls)

Click **Edit** on any tenant row in the admin panel to open its editor.
Two collapsed sections at the bottom expose:
- **End-user emails this tenant has sent** — every distinct
  `user_email` the brain has seen for the tenant, plus the first-seen
  timestamp. Useful for billing reviews / capacity planning.
- **Recent calls** — the last 100 brain calls for this tenant
  (timestamp, endpoint, model, user_email, input/output tokens).

The data comes from `tenants/{tenant_id}/users.jsonl` and
`tenants/{tenant_id}/usage.jsonl` — append-only files written by
`tenant_store.record_request`.

---

## 7. Logs

- **Brain logs** (in `logs/brain.log`): every received request, including
  tenant id, sid, question, schema chars, model used, code hash, error
  text (during retry), and usage tokens. Per the architecture, the brain
  only ever sees no-raw-value data, so logging everything it receives is
  safe.
- **Client logs** (in `logs/datachat.log`): upload events, code execution
  errors (which contain raw data values, like KeyError row indices),
  and brain HTTP responses. These stay on the client server and never
  leave it.
