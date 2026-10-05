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
transmitted. What does cross to the brain over HTTPS is listed in the table
below: the question text, the schema text (names, dtypes, descriptions and the
values of low-cardinality text columns), capped profile statistics, a capped
result preview for the summarize step, and truncated answer text for titles
and reports. Some of these carry individual values from your data: the
categories of a text column with at most 20 distinct values, a column's
minimum and maximum, a constant value, a scalar result. A column named in
`SCHEMA_VALUE_DENY_COLUMNS` sends its name, dtype and counts only, never a
value. User email is sent for tenant routing.

- The summarizer's `_safe_preview` helper in
  [`run_chat_local.py`](run_chat_local.py) is the hard guard: a
  `str | int | float | bool` passes, and so does a flat dict whose keys and
  values are all such scalars (numpy scalars are converted first); a list, a
  DataFrame, or a dict holding anything else becomes `None`. What passes is
  capped in transport by `brain_client`: a string at 500 characters, a dict at
  its first 20 keys (the number dropped is sent as `_truncated_keys`), each
  string value at 500 characters. Do not weaken either.
- `SCHEMA_VALUE_DENY_COLUMNS` (comma-separated column names, case-insensitive,
  empty by default) removes a column's values from the schema text, the
  technical descriptions, the dataset profile and the schema-autofill hints.
  Use it for columns such as account numbers or national ids. It does not
  filter what an analysis computes: result previews, answer text and past
  answers in the conversation history can still carry values.
- The `/lab` page loads **no third-party script** by default (no analytics, no
  billing widget): `settings.ENABLE_THIRD_PARTY_SCRIPTS` is False, so the
  browser talks only to this server. The analysis sandbox transmits nothing at
  all: it has no route off its internal network.

### Data that leaves the container

| What | Sent when | Shape |
|---|---|---|
| Question text | Every question | Verbatim, as typed |
| Conversation history | Every question | Past turns: role, content, generated code |
| Schema text and column metadata | Every question | Table/sheet names, column names, dtypes, business descriptions and a technical line per column (dtype, fill count). A text column with at most 20 distinct values lists those values, each cut to 40 characters; any other text column sends its distinct count only; numeric and date columns send no value. Value descriptions written for categorical values (keyed by the value). A `SCHEMA_VALUE_DENY_COLUMNS` column sends dtype and fill count only, and no value descriptions. Technical lines stored by an earlier release are cut to the same rule when the text is built. For a database table: its qualified name, snapshot date or, for a live table, dialect and row cap |
| Other registered tables | Every question on a chat that uses database tables | Display names, column NAMES and declared join keys of registered tables the user's role may add but that are not loaded in the chat — never values |
| Live-table fields | Plan and retry, only when the chat has a live table | `live_tables` (name, dialect, row cap, filtered flag); on retry `sql` (the SELECT the planner wrote, as run) and `sql_error` (table, dialect, error class, and the client's own refusal sentence — never the database's error text) |
| Dataset profile | Every question | Row/duplicate counts, per-column distinct and null counts, null rates, min/max of numeric and date columns, constant and all-unique flags, grain (column names), up to 6 warnings (a constant column's warning names its value). Up to 5 top values, each cut to 40 characters, only for a column with at most 20 distinct values. A `SCHEMA_VALUE_DENY_COLUMNS` column sends dtype, distinct and null counts and the two flags only. Profiles stored by an earlier release are filtered to this rule before sending |
| Generated code and execution errors | Every question and retry | Python source, traceback text |
| Excel header text above a table | Upload, when a sheet has text above the table and no description | The extracted text VERBATIM, truncated to 2000 characters — it is read from your file, so treat it as content |
| File and sheet names | Upload and every question | The name as STORED after sanitization, plus sheet names |
| Share invitations | Sharing a chat or dashboard | Recipient addresses, the item title, and the note the sender types |
| Schema-autofill hints | Upload, Add Data, database-table registration (AI descriptions) | Column names and dtypes. A column with at most 10 distinct values (`SCHEMA_AUTOFILL_UNIQUE_THRESHOLD`) sends those values, each cut to 60 characters; any other column sends one `[profile: …]` summary (dtype, distinct count, null share; for text a character mask, lengths and frequent first words; for numbers min/max/mean at two significant figures; for dates the year-month range). A `SCHEMA_VALUE_DENY_COLUMNS` column sends `[profile: dtype=…, distinct=…, nulls=…%]` only |
| Result preview | Summarize step | A single `str`/`int`/`float`/`bool` value or a flat dict of such values; a string capped at 500 characters, a dict at its first 20 keys plus `_truncated_keys` (the count dropped). Lists and DataFrames are dropped |
| Result caveat | Describe step, only when the client's result check fires (a constant metric, identical series, or a count matrix that reads better as a bar chart) | Finding kind and up to 6 short facts naming result columns; a constant metric's fact includes its value |
| Answer text | Conversation title | Question and the first 300 characters of the answer |
| Answer text | Report generation | Question and answer truncated to 500 characters, code snippet to 300 |
| Table column names | Report generation | Column NAMES only, first 10 — never rows |
| User email | Every call | Tenant routing and per-user activity |
| Activity events | Login, upload, chat, report | Event name, user email, lightweight counters |
| Password-reset payload | Password reset and invitation | The e-mail address, whether it is a reset or an invitation, and a single-use link (valid 30 minutes for a reset, 30 days for an invitation), relayed through the brain's mail service; until it is used or expires the link sets the account's password |
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
│   ├── auth.py              # /auth/* — email+password sign-in (by invitation), reset link, change
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
| `SECRET_KEY` | **Required.** Local session-cookie signing secret, at least 32 characters. Generate once with `python -c "import secrets; print(secrets.token_hex(32))"`. The app refuses to start (`SECRET_KEY_UNSET`) without it. |
| `DATA_ROOT` | Local-disk root for raw data + chats. Mount a volume. |
| `SCHEMA_VALUE_DENY_COLUMNS` | Optional. Comma-separated column names (case-insensitive) whose values never reach the brain: those columns send name, dtype and counts only. Empty by default. |
| `SHARE_ALLOWED_DOMAINS` | Optional. Comma-separated recipient domains a chat, conversation or dashboard may be shared with. Empty by default: the domains of the administrator accounts are used, or, when none has an email address, the domains of all accounts with one; with no such account every share is refused. |
| `SSO_ALLOW_GUESTS` | Optional, default `false`. `true` lets guests (`#EXT#`) of your Entra tenant sign in with Microsoft. See [`docs/SSO_MICROSOFT.md`](docs/SSO_MICROSOFT.md). |
| `SSO_AUTO_PROVISION` | Optional, default `false`. `true` creates an account at the Microsoft sign-in of an identity that has none; otherwise such a sign-in is refused. Requires "Assignment required? = Yes" on the Entra enterprise application. |

The `EXECUTOR_*` topology values are set by the compose file, not by the env
file. `EXECUTOR_NETWORK_CIDR` is mandatory while `EXECUTOR_URL` is set: the
app refuses to start (`EXECUTOR_CIDR_UNSET`) without a valid network, and the
compose files feed it from `PDC_BACKEND_SUBNET`. See [`client.env.example`](client.env.example) for the full list and
[`docs/BUILD_AND_RUN.md`](docs/BUILD_AND_RUN.md) for build + run.

```bash
# both images, from this repo
docker build -t powerdatachat-client:enterprise .
docker build -f executor/Dockerfile -t powerdatachat-executor:enterprise .

cp client.env.example client.env      # fill BRAIN_TENANT_TOKEN + SECRET_KEY
docker compose up -d
```

The web port 8000 is published on the host's loopback address only
(`127.0.0.1:8000`): your users reach the application through a reverse proxy
that terminates TLS on the same host and forwards to `http://127.0.0.1:8000`.
The compose-level variable `PDC_WEB_BIND_HOST` (default `127.0.0.1`) changes
that address; set it only when the proxy runs on another machine, and then
firewall the port to the proxy's address, because the application itself
speaks plain HTTP. See [`CUSTOMER_INSTALL.md`](CUSTOMER_INSTALL.md) §3,
"Serve it over HTTPS".

On the Docker host itself, open `http://localhost:8000` and sign in as
`ladmin` with `LOCAL_ADMIN_PASSWORD`, then invite users from the admin panel's
**Users** page; each sets a password through the mailed link and lands in
`/lab`. `curl http://127.0.0.1:8000/health`, run on the host, must report
`executor_reachable: true`; it stays 200 either way, so read the body.

The compose files carry the hardening both containers need (read-only rootfs,
tmpfs `/tmp`, all capabilities dropped, `no-new-privileges`, memory and pid
caps, a 2-CPU cap per container — the host needs at least 2 vCPUs — and
Docker log rotation of 5 × 20 MB) and the jobs volume with the ownership both
uids require. See
[`CUSTOMER_INSTALL.md`](CUSTOMER_INSTALL.md) §3. A HOST BIND MOUNT for
`/data/client` must be writable by uid 10001 (`chown -R 10001:10001
./client_data`, or use a named volume, which inherits the image's ownership).
Skip it and the container still starts and still reports healthy — but every
write fails: `LADMIN_BOOTSTRAP_FAILED` / `Permission denied` in the log, and
users get a 500 when they try to sign in.

**Sharing.** A chat, a conversation or a dashboard can be shared only with
addresses in the allowed domains (`SHARE_ALLOWED_DOMAINS`, else the domains of
the administrator accounts, else the domains of all accounts with an email
address). One address outside them refuses the whole share
(`400 RECIPIENT_DOMAIN_NOT_ALLOWED`): nothing is shared and no account is
created. An allowed address without an account gets a password-less account
(Microsoft-only while single sign-on is enabled). A chat's owner can remove a
recipient from the share dialog (a conversation's share dialog removes that
person's copy of the conversation), and unsharing a dashboard also ends the chat
access that share gave. A recipient whose data role does not cover a chat's
database tables gets no answers from those tables. See
[`CUSTOMER_INSTALL.md`](CUSTOMER_INSTALL.md), "Sharing".


## Key docs

| Doc | Purpose |
|---|---|
| [`CUSTOMER_INSTALL.md`](CUSTOMER_INSTALL.md) | Customer install quickstart (pull/load → configure → run → verify) |
| [`docs/BUILD_AND_RUN.md`](docs/BUILD_AND_RUN.md) | Build, run, configure, health-check, logs |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | The brain `/v1/*` API this client consumes |
| [`docs/CLIENT_ENDPOINTS.md`](docs/CLIENT_ENDPOINTS.md) | The endpoints this client exposes (dashboard contract) |
| [`docs/AI_CONSTITUTION.md`](docs/AI_CONSTITUTION.md) | Engineering rules — read before any code change |
| [`docs/DATA_PROCESSING.md`](docs/DATA_PROCESSING.md) | Where the AI service runs, the language model, retention on the brain, sub-processors, administrator access |
