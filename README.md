# PowerDataChat Enterprise — Client

The **client** half of the PowerDataChat enterprise (on-prem) edition.
Runs inside the customer's LAN. Holds raw data, runs generated Python
locally, renders charts, generates reports, and serves the `/lab` chat
dashboard. **Raw data values never leave this container.**

## What this repo is

The client container is deployed inside the customer's own network. It
talks to a multi-tenant brain service (a separate, hosted service
operated by PowerDataChat) over HTTPS using a per-tenant bearer
token issued by the operator. Every request to the brain carries the
token; if the operator revokes it, every brain call returns `403` and
the chat UI surfaces a single "service unavailable" error.

## Architecture (at a glance)

- **Client** (this repo, runs in customer LAN) — email-only auth, file
  upload + storage, schema autofill, data preprocessing, Python code
  execution, chart rendering, report (PDF/PPTX) generation, the
  `/lab` UI. All raw data + result tables + rendered files stay on
  this server.
- **Brain** (a separate, hosted service operated by PowerDataChat) —
  the LLM gateway. Receives only column names, schema
  text, sampled metadata, generated code, error text, and findings
  with no row values. Never receives DataFrames, rendered charts, or
  the customer's templates.

## Data boundary (the whole point)

- **Raw data never leaves this container.** Only column names + schema
  metadata + generated code + scalar previews + findings (no values)
  cross to the brain.
- The summarizer's `_safe_preview` helper in
  [`run_chat_local.py`](run_chat_local.py) is the hard guard: only
  `str | int | float | bool` pass through; dicts, lists, and
  DataFrames become `None`. Do not weaken it.

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
├── settings.py              # env-driven settings
├── models.py
├── logger_utils.py
├── requirements.txt
├── Dockerfile
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
├── static/                  # JS + CSS + images for the dashboard
└── docs/
    ├── AI_CONSTITUTION.md   # client engineering rules + the data boundary
    ├── PROTOCOL.md          # the brain /v1/* surface (what this client calls)
    ├── CLIENT_ENDPOINTS.md  # this client's HTTP surface (dashboard contract)
    └── BUILD_AND_RUN.md     # build + run + configure
```

## Install (customer-side)

> **Quickstart:** see [`CUSTOMER_INSTALL.md`](CUSTOMER_INSTALL.md) for the
> copy-paste handoff (pull/load image → configure `client.env` → run → verify).

Customers receive this image already built. To run it you need:

| Variable | Purpose |
|---|---|
| `BRAIN_URL` | Where the brain is reachable from inside the client's network. |
| `BRAIN_TENANT_TOKEN` | Per-tenant bearer token issued by PowerDataChat (shown ONCE in the operator's admin panel). |
| `SECRET_KEY` | Local session-cookie signing secret (`openssl rand -hex 32`). |
| `DATA_ROOT` | Local-disk root for raw data + chats. Mount a volume. |

See [`.env.example`](.env.example) for the full list and
[`docs/BUILD_AND_RUN.md`](docs/BUILD_AND_RUN.md) for build + run.

```bash
docker build -t powerdatachat-client:enterprise .

docker run --rm -p 8000:8000 \
  -e BRAIN_URL="https://brain.your-domain.example.com" \
  -e BRAIN_TENANT_TOKEN="<token-from-brain-admin>" \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -v $(pwd)/client_data:/data/client \
  powerdatachat-client:enterprise
```

Open `http://localhost:8000` → enter your work email → land in `/lab`.

## Key docs

| Doc | Purpose |
|---|---|
| [`CUSTOMER_INSTALL.md`](CUSTOMER_INSTALL.md) | Customer install quickstart (pull/load → configure → run → verify) |
| [`docs/BUILD_AND_RUN.md`](docs/BUILD_AND_RUN.md) | Build, run, configure, health-check, logs |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | The brain `/v1/*` API this client consumes |
| [`docs/CLIENT_ENDPOINTS.md`](docs/CLIENT_ENDPOINTS.md) | The endpoints this client exposes (dashboard contract) |
| [`docs/AI_CONSTITUTION.md`](docs/AI_CONSTITUTION.md) | Engineering rules — read before any code change |
