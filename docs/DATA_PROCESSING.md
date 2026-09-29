# Data processing — PowerDataChat Enterprise

This page is for the customer's data-protection, security and procurement
reviewers. It says where each kind of data is processed, by whom, and for how
long. Values marked **[FILL]** are contractual or operational facts that
PowerDataChat must supply for your contract; they are left open on purpose
rather than guessed.

What crosses from your network to the PowerDataChat service, field by field,
is listed in `CUSTOMER_INSTALL.md` → "What leaves your network" and in
`README.md`. This page does not repeat that table.

## 1. The two halves

| Half | Where it runs | Operated by | Holds |
|---|---|---|---|
| **Client** (this image + the analysis sandbox) | Your own network, on your host | You | Users and password hashes, uploaded files, database snapshots, chats, conversation history, dashboards, reports, logs |
| **Brain** (the AI service) | Google Cloud Platform, Cloud Run, region `europe-west1` | PowerDataChat | Per-tenant configuration, usage and activity records, service logs |

Raw data values stay in the client. The brain receives question text, schema
metadata (column names, data types, descriptions, the values of
low-cardinality text columns), capped statistics and short result previews,
as the data-boundary table describes. Columns you list in
`SCHEMA_VALUE_DENY_COLUMNS` send no values at all.

## 2. The language model

| Item | Value |
|---|---|
| Provider | Google (Gemini) |
| Endpoint | Gemini API, `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`, called over HTTPS by the brain only (the client never calls it) |
| Model tier | **[FILL]** — the models are configured per tenant in the brain; state the tier and models for this contract |
| Region of model processing | **[FILL]** — the Gemini API endpoint above is global; state the processing location Google commits to for this account |
| Training on prompts | **[FILL]** — state that prompts and responses are not used to train models and link the provider terms that apply to this account (e.g. the Gemini API paid-service terms) |

## 3. Retention on the brain

| Record | Content | Retention |
|---|---|---|
| Service logs (Cloud Logging) | Request metadata: tenant, event names, timings, model name, token counts, error classes. With `LLM_DEBUG_LOG` switched on (off by default, a diagnostic flag) the full prompt and model response of each call, truncated to 20 000 characters, are logged as well | **[FILL]** — the Cloud Logging retention period of the `pdc-enterprise` project, and whether `LLM_DEBUG_LOG` is on for this tenant |
| `usage.jsonl` (per tenant) | One row per brain request: time, endpoint, model, user address, input and output token counts | **[FILL]** — kept without pruning today; state the contractual period |
| `users.jsonl` (per tenant) | Each user address seen, with the time it was first seen | **[FILL]** — kept without pruning today |
| `activity.jsonl` (per tenant) | Sign-in, upload, chart and report events: time, event name, user address, event metadata (e.g. a file name) | **[FILL]** — kept without pruning today; state the contractual period |
| Tenant configuration | Tenant name, token, model settings, report template | For the life of the contract |

The brain stores no uploaded file, no table and no chart. The storage bucket
behind the brain (`pdc-enterprise-brain-data`) has object versioning; deleted
records remain recoverable for **[FILL]** days.

## 4. Sub-processors

| Sub-processor | Purpose | Data |
|---|---|---|
| Google Cloud (Cloud Run, Cloud Storage, Cloud Logging) | Hosts the brain and its records | Everything in §3 |
| Google (Gemini API) | Language-model calls | Prompts built from the metadata in §1; model responses |
| Gmail relay operated by PowerDataChat | Welcome, password-reset / invitation and share-notification mails | Recipient address, the link in the mail, the shared item's title and the sender's note |
| Your own SMTP server (optional, per tenant) | The same mails, when you configure it instead of the relay | The same |
| Microsoft Entra ID (optional) | Single sign-on, when you enable it | The sign-in exchange between the user's browser, Microsoft and your client; the brain is not involved |

## 5. Administrator access to user data

No administrator route reads another user's uploads; the local admin manages
data sources and accounts only.

Precisely: the administrator and power-user routes (`/api/admin/*`) work on
the data-source registry, database snapshots the administrator registered,
roles, and accounts. Removing an account deletes the chats that account owns
without reading them, and takes the address off other owners' share lists by
reading only the `owner` and `sharing` fields of chat records. A scheduled
refresh of a registered database table updates the column metadata of the
chats that use it. No administrator route opens a user's uploaded files,
conversation history or dashboards.

Anyone with shell access to the client host can read the data volume
directly; protect the host accordingly.
