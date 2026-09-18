# PowerDataChat Enterprise — Client

The **client** half of the PowerDataChat enterprise (on-prem) edition.
Runs inside the customer's LAN. Holds raw data, runs generated Python in a
separate unprivileged sandbox container, renders charts, generates reports,
and serves the `/lab` chat dashboard. **No uploaded file, result table, or rendered chart is ever
transmitted off the customer's own server.**

## What this repo is

The client stack is deployed inside the customer's own network. It
talks to a multi-tenant brain service (a separate, hosted service
operated by PowerDataChat) over HTTPS using a per-tenant bearer
token issued by the operator. Every request to the brain carries the
token; if the operator revokes it, every brain call returns `403` and
the chat UI surfaces a single "service unavailable" error.

## Architecture (at a glance)

- **Client** (this repo, runs in customer LAN) — TWO containers shipped as one
  release. The **web application** (uid 10001) does email+password auth (local,
  hash-only), file upload + storage, schema autofill, data preprocessing,
  report (PDF/PPTX) generation and the `/lab` UI. The **analysis sandbox**
  (`executor/`, uid 10002) is where the generated Python and the chart
  rendering run, never in the web container. The sandbox has no data volume,
  no credentials, no database driver and no network route out; the two share
  one jobs directory, one directory per in-flight question. All raw data +
  result tables + rendered files stay on this server.
- **Brain** (a separate, hosted service operated by PowerDataChat) —
  the LLM gateway. Receives column names, schema text, sampled
  metadata, generated code, error text, and findings — see the table
  below for the exact list. Never receives uploaded files, DataFrames,
  query result sets, rendered charts, or the customer's templates.

## Data boundary (the whole point)

No uploaded file, DataFrame, query result set, or rendered chart is ever
transmitted. What does cross to the brain over HTTPS: the question text, schema
and column names and descriptions, capped aggregate profile statistics (at most
5 top values per column, 40 characters each), scalar result previews, and answer
text truncated to 500 characters for reports. These can contain individual
values derived from your data. User email is sent for tenant routing.

- The summarizer's `_safe_preview` helper in
  [`run_chat_local.py`](run_chat_local.py) is the hard guard: only
  `str | int | float | bool` pass through; dicts, lists, and
  DataFrames become `None`. Do not weaken it.
- The `/lab` page loads **no third-party script** by default (no analytics, no
  billing widget): `settings.ENABLE_THIRD_PARTY_SCRIPTS` is False, so the
  browser talks only to this server. The analysis sandbox transmits nothing at
  all: it has no route off its internal network.

### Data that leaves the container

| What | Sent when | Shape |
|---|---|---|
| Question text | Every question | Verbatim, as typed |
| Conversation history | Every question | Past turns: role, content, generated code |
| Schema text and column metadata | Every question | Table/sheet names, column names, dtypes, descriptions, cardinality and truncated unique-value hints |
| Dataset profile | Every question | Row/duplicate counts, null rates, min/max, constant and all-unique flags, up to 5 top values per column truncated to 40 characters |
| Generated code and execution errors | Every question and retry | Python source, traceback text |
| Excel header text above a table | Upload, when a sheet has text above the table and no description | The extracted text VERBATIM, truncated to 2000 characters — it is read from your file, so treat it as content |
| File and sheet names | Upload and every question | The name as STORED after sanitization, plus sheet names |
| Share invitations | Sharing a chat or dashboard | Recipient addresses, the item title, and the note the sender types |
| Scalar result previews | Summarize step | Single `str`/`int`/`float`/`bool` values only — dicts, lists and DataFrames are dropped |
| Answer text | Report generation | Question and answer truncated to 500 characters, code snippet to 300 |
| Table column names | Report generation | Column NAMES only, first 10 — never rows |
| User email | Every call | Tenant routing and per-user activity |
| Activity events | Login, upload, chat, report | Event name, user email, lightweight counters |
| Password-reset payload | Password reset only | The e-mail address and a temporary password, relayed through the brain's mail service (a tokenized reset link replaces this in a future release) |
| Third-party browser scripts | Never, by default | `/lab` loads no analytics or billing script (`ENABLE_THIRD_PARTY_SCRIPTS=false`) |

Never sent: uploaded files, DataFrames, query result sets, rendered charts or
decks, and your branded templates.

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
├── code_exec.py             # safe_execute (dispatches to the executor)
├── plot_utils.py            # render_plot_safe (dispatches to the executor)
├── executor_client.py       # the HTTP hop to the executor container
├── executor/                # the executor service (generated Python runs HERE)
├── settings.py              # env-driven settings
├── models.py
├── logger_utils.py
├── requirements.txt
├── Dockerfile               # the web image (uid 10001)
├── executor/Dockerfile      # the sandbox image (uid 10002)
├── docker-compose.yml       # customer stack: both services, two networks
├── docker-compose.local.yml # local stack: both services, built from this repo
├── routes/
│   ├── auth.py              # /auth/* — email+password login, reset, change
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
> copy-paste handoff (pull/load both images → configure `client.env` → run →
> verify).

Customers receive **two** images already built, `powerdatachat-client` (the
web application) and `powerdatachat-executor` (the analysis sandbox), and run
them with Docker Compose. Compose is the supported form, not a convenience: the
generated Python runs only in the sandbox, so a stack with just the web image
answers every question "The analysis service is not available right now."
(the internal form, `ExecutorUnavailable:`, is what the log and the refresh /
dashboard / report / Auto Analytics paths carry). It is also what creates the
internal network that gives the sandbox no route out, and the one jobs
directory the two containers share.

To run the stack you need:

| Variable | Purpose |
|---|---|
| `BRAIN_URL` | Where the brain is reachable from inside the client's network. |
| `BRAIN_TENANT_TOKEN` | Per-tenant bearer token issued by PowerDataChat (shown ONCE in the operator's admin panel). |
| `SECRET_KEY` | Local session-cookie signing secret (`openssl rand -hex 32`). |
| `DATA_ROOT` | Local-disk root for raw data + chats. Mount a volume. |

The `EXECUTOR_*` topology values are set by the compose file, not by the env
file. See [`client.env.example`](client.env.example) for the full list and
[`docs/BUILD_AND_RUN.md`](docs/BUILD_AND_RUN.md) for build + run.

```bash
# both images, from this repo
docker build -t powerdatachat-client:enterprise .
docker build -f executor/Dockerfile -t powerdatachat-executor:enterprise .

cp client.env.example client.env      # fill BRAIN_TENANT_TOKEN + SECRET_KEY
docker compose up -d
```

Open `http://localhost:8000` → enter your work email → land in `/lab`.
`curl http://localhost:8000/health` must report `executor_reachable: true`;
it stays 200 either way, so read the body.

The compose files carry the hardening both containers need (read-only rootfs,
tmpfs `/tmp`, all capabilities dropped, `no-new-privileges`, memory and pid
caps) and the jobs volume with the ownership both uids require. See
[`CUSTOMER_INSTALL.md`](CUSTOMER_INSTALL.md) §3. A HOST BIND MOUNT for
`/data/client` must be writable by uid 10001 (`chown -R 10001:10001
./client_data`, or use a named volume, which inherits the image's ownership).
Skip it and the container still starts and still reports healthy — but every
write fails: `LADMIN_BOOTSTRAP_FAILED` / `Permission denied` in the log, and
users get a 500 when they try to sign in.


## Key docs

| Doc | Purpose |
|---|---|
| [`CUSTOMER_INSTALL.md`](CUSTOMER_INSTALL.md) | Customer install quickstart (pull/load → configure → run → verify) |
| [`docs/BUILD_AND_RUN.md`](docs/BUILD_AND_RUN.md) | Build, run, configure, health-check, logs |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | The brain `/v1/*` API this client consumes |
| [`docs/CLIENT_ENDPOINTS.md`](docs/CLIENT_ENDPOINTS.md) | The endpoints this client exposes (dashboard contract) |
| [`docs/AI_CONSTITUTION.md`](docs/AI_CONSTITUTION.md) | Engineering rules — read before any code change |
