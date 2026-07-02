# PowerDataChat Enterprise — Client — Claude Code Instructions

The client half of the PowerDataChat enterprise (on-prem) edition.
Runs inside the customer's LAN. Holds raw data + execution + rendering
+ the `/lab` UI. Talks to a multi-tenant brain (a separate, hosted
service operated by PowerDataChat) over HTTPS with a per-tenant
bearer token. **Raw data values never leave this container.**

## Required reading before ANY code change

You MUST read these before writing or modifying code:

1. [`docs/AI_CONSTITUTION.md`](docs/AI_CONSTITUTION.md) — client engineering rules + the data boundary.
2. [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — the brain `/v1/*` API this client calls.
3. [`docs/CLIENT_ENDPOINTS.md`](docs/CLIENT_ENDPOINTS.md) — this client's HTTP surface (`/lab` contract).

The client overview, the data boundary, and the invariants live in
[`docs/ENTERPRISE_ARCHITECTURE.md`](docs/ENTERPRISE_ARCHITECTURE.md). Read it
before changing any behavior that touches the brain↔client boundary.

## Critical rules summary

### The data boundary (Article II of the constitution)
Raw data values must never cross from this client to the brain. The
summarizer's `_safe_preview`
([`run_chat_local.py`](run_chat_local.py)) is the hard guard — only
scalars pass; dicts/lists/DataFrames become `None`. Do not weaken it
or bypass it.

### Error handling (Article IV)
Every `try/except` must:
1. Log with `log_with_sid(sid, level, message, ...)`.
2. Return a safe fallback value.
3. Never crash silently.

### Local filesystem only (Article V)
On-prem runs against local disk (`DATA_ROOT`, default `/data/client`).
There is no `GCSPath`. Direct-to-GCS upload endpoints (`/upload/init`,
`/upload/finalize`, `/upload_from_url`) return 400 by design.

### No tokens, no API keys in this repo (Article VII)
- This client never holds the Gemini key — the brain does.
- The `BRAIN_TENANT_TOKEN` is injected at install time via an env var
  and identifies this tenant to the brain. Never commit it.
- `SECRET_KEY` is local session-cookie signing only. Generate fresh
  per install.
- Never commit `.env`. Commit `.env.example` templates only.

## Repository layout

```
PDC_Client/
├── app.py                   # FastAPI app + lifespan
├── brain_client.py          # HTTP client to brain (bearer token)
├── run_chat_local.py        # local execution + _safe_preview guard
├── local_store.py           # users + chats + conversations on local disk
├── schema_builder.py        # _schema_text builder
├── excel_table_detector.py  # 6-stage Excel table detection
├── auto_analytics.py        # background job (brain planner → local exec → PPTX)
├── code_exec.py             # safe_execute
├── plot_utils.py            # render_plot_safe
├── settings.py
├── models.py
├── logger_utils.py
├── routes/
│   ├── auth.py              # /auth/* — email-only login
│   ├── upload.py            # /upload, /schema_autofill_full, /generate_chatdata
│   ├── schema.py            # /schema_details, /schema_common_fields, /schema
│   ├── chat.py              # /api/chat/* — SSE stream, edit-regenerate, sharing
│   └── report.py            # /download_report (PDF), /download_pptx
├── templates/
│   ├── dashboard.html       # /lab page
│   ├── auth_landing.html
│   └── partials/
├── static/                  # JS + CSS + images
├── Dockerfile
└── docs/                    # constitution, protocol, endpoints, build & run
```

## File reference

| File | Purpose |
|---|---|
| [`routes/chat.py`](routes/chat.py) | Chat SSE stream, multi-chart accumulation + persistence, edit-regenerate, sharing, full_table, conversation title generation, Auto Analytics endpoints; chart-PNG + table-Excel download routes (`export_plotly_png`, `download_excel/{key}`, `export_excel`) |
| [`plot_utils.py`](plot_utils.py) | `render_plot_safe` + `_plotly_to_html`; trims the non-functional Plotly modebar tools (`toImage`, `sendDataToCloud`, `select2d`, `lasso2d`) and widens the discrete color palette so >10-category charts never repeat a hue (`_widen_discrete_colors`; continuous/2nd-measure scales untouched) |
| [`static/dashboard.js`](static/dashboard.js) | Served `/lab` UI; on the constant Enterprise plan it hides the B2C subscription plan-cards so the Paddle upsell (no billing backend on-prem) is unreachable |
| [`routes/upload.py`](routes/upload.py) | `/new_session`, `/upload`, `/schema_autofill_full`, `/generate_chatdata`, GCS-path stubs (400) |
| [`routes/auth.py`](routes/auth.py) | Email-only login/logout, profile (constant "Enterprise" plan), sidebar listings, rename/delete, conversation-level share |
| [`routes/report.py`](routes/report.py) | PDF (ReportLab + DejaVu) and PPTX (python-pptx) report rendering — local only |
| [`auto_analytics.py`](auto_analytics.py) | Auto Analytics background job (planner via brain → execute locally → render PPTX) |
| [`run_chat_local.py`](run_chat_local.py) | `run_chat` / `run_chat_multi_plot`; `_safe_preview` data-boundary guard |
| [`local_store.py`](local_store.py) | `AuthStore`, `UserStore`, `ChatDataStore` on local disk; `append_history` / `truncate_conv_history` |
| [`brain_client.py`](brain_client.py) | HTTP wrappers for every `/v1/*` call (with `BRAIN_TENANT_TOKEN` bearer auth) |
| [`excel_table_detector.py`](excel_table_detector.py) | 6-stage Excel table detection |

## Documentation

| Document | Purpose |
|---|---|
| [`docs/AI_CONSTITUTION.md`](docs/AI_CONSTITUTION.md) | Coding rules (12 articles, enterprise-specific) |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | Brain `/v1/*` request/response shapes (what this client calls) |
| [`docs/CLIENT_ENDPOINTS.md`](docs/CLIENT_ENDPOINTS.md) | This client's HTTP surface (dashboard contract) |
| [`docs/BUILD_AND_RUN.md`](docs/BUILD_AND_RUN.md) | Build, run, configure, logs |

## Before committing

1. **Smoke locally** — start the client and a brain you control, full
   upload→chart→edit→report cycle in the browser.
2. **`GET /health`** returns 200 (`brain_reachable: true`,
   `tenant_token_configured: true`).
3. **No `.env` staged** — check `git status` and `git diff --cached`
   for secrets. `.env.example` is OK.
4. **No raw values leaving** — grep your diff for places where
   DataFrames, full tables, or row dicts might cross into a
   `brain_client.*` call.
5. Follow commit format from Article XII.

## Deployment

- **Client** (this repo) → packaged as a Docker image, installed by
  each customer in their own network. Configured via `BRAIN_URL` +
  `BRAIN_TENANT_TOKEN` environment variables at install time.
- **Brain** → a separate, hosted service operated by PowerDataChat,
  not run or deployed by the customer.

The two are NEVER deployed together. That would defeat the split.

## Differences from the B2C edition (intentional)

- **Email-only auth** — no passwords, no Google OAuth.
- **No subscriptions / quotas** — `/auth/subscription` returns
  constant `{"plan": "Enterprise"}`. No daily message caps. No Paddle.
- **No public publishing** — `POST /publish`/`/unpublish` return 400.
  Sharing is recipient-list only.
- **No GCS adapter** — local filesystem only. Direct-to-GCS upload
  endpoints return 400.
- **No Soro blog, Vlog, B2C marketing site, customer-analytics
  reporter** — those modules don't exist on-prem.

## Memory

Check your agent's memory folder for session-specific learnings. Memory
is shared with the broader project — entries are tagged by context;
pay attention to which apply on-prem (e.g. GCSPath learnings do NOT
apply here).
