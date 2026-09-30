# Data processing — PowerDataChat Enterprise

This page is for the customer's data-protection, security and procurement
reviewers. It says where each kind of data is processed, by whom, and for how
long. Every statement is taken from the brain's code and deployment
documentation. The settings of the running service and the contractual
periods are stated as configured on 2026-09-30; the contract fixes them for
its term, and PowerDataChat notifies the customer's named security contact
before any of them changes.

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
| Provider | Google Gemini API (the Gemini Developer API; the brain does not use Vertex AI) |
| Endpoint | `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`, called over HTTPS with an API key by the brain only (the client never calls it). The host is fixed in the brain's code. |
| Model tier | Four tiers, each selected per tenant by the PowerDataChat operator in the brain's admin panel: a classifier that scores each question, then the model for questions scored 0–3 (greetings; also used for result descriptions, schema autofill and titles), 4–8 (most questions) and 9–10 (deep analysis). Code corrections use the 4–8 model, or the 9–10 model when escalated. When a call fails the brain retries with another tier's model and finally `gemini-2.5-flash`. The defaults in the brain's code today are `gemini-2.5-pro` for the classifier and the 4–8 and 9–10 tiers and `gemini-2.5-flash` for the 0–3 tier; the operator can change the brain-wide defaults without a code change. The models set for your tenant: the brain-wide defaults named above (`gemini-2.5-pro` for the classifier and the 4–8 and 9–10 tiers, `gemini-2.5-flash` for the 0–3 tier), pinned in the tenant configuration and fixed in the contract; no other model is called for this tenant |
| Region of processing | The brain runs on Cloud Run in `europe-west1` (project `pdc-enterprise`); its storage bucket and image registry are in `europe-west1` too. The Gemini API endpoint is global and the brain does not choose where Google processes a call. Google's Paid Services terms state that this data "may be stored transiently or cached in any country in which Google or its agents maintain facilities"; no EU-only processing commitment applies to this endpoint. Processing of model calls within the EU is available as a contract option by moving the brain's model calls to Vertex AI with a European regional endpoint |
| Training on prompts | The brain calls the Gemini API under the paid Google terms, under which Google does not use prompts or responses to improve its models: [Gemini API Additional Terms of Service](https://ai.google.dev/gemini-api/terms). The API key used for your tenant (the brain's default key, or a per-tenant key set in the admin panel) belongs to a Google Cloud project with billing enabled, so the Paid Services terms (effective 2026-03-23) apply: Google "doesn't use your prompts (including associated system instructions, cached content, and files ...) or responses to improve our products", and processing is covered by Google's Data Processing Addendum for products where Google is a data processor. Google keeps prompts and responses for 55 days solely for abuse monitoring, where they are "not used to train or fine-tune any AI/ML models besides those used specifically for policy enforcement" ([Abuse monitoring](https://ai.google.dev/gemini-api/docs/usage-policies)) |

## 3. Retention on the brain

| Record | Content | Retention |
|---|---|---|
| Service logs (Cloud Logging) | Always logged: tenant, event names, timings, model name, token counts, error classes, the error texts the model API returns and the brain's own exception texts; the user's address on each planning call; the question — its first 120 characters on the planning line and, on the code-generation prompt line (its first 800 characters), a language note, the question in full when it is shorter than about 700 characters, the classifier's summary of it (intent, task type, approach, key column names) when one was made, the table names and whatever of the schema text still fits; on a regeneration after an error, the first 200 characters of the execution error and the first 800 characters of the model's answer; the uploaded file's name on schema autofill; the names of live tables a SELECT was written for (never the SQL, which is logged as a hash); the recipient address and subject of each relayed mail. Generated code is otherwise logged as a hash only. With `LLM_DEBUG_LOG` switched on (off in the code's default) the full prompt and model response of each call made through the brain's main model-call function are logged as well, each cut to 20 000 characters | No retention setting exists in the brain's deployment documentation or configuration, so the Cloud Logging default applies: 30 days (the `pdc-enterprise` project's default `_Default` log bucket retention, not extended). `LLM_DEBUG_LOG` is off in the running service for every tenant; it is switched on only for a diagnostic session agreed with the customer in writing and switched off at the end of that session; while it is on, the logged prompts and responses fall under the same 30-day retention |
| `usage.jsonl` (per tenant) | One row per brain request: time, endpoint, model, user address, input and output token counts | Not pruned or aged: the rows are kept until PowerDataChat deletes the tenant in the brain's admin panel, which removes the tenant's whole folder. Contractual period: the life of the contract; the tenant folder is deleted within 30 days of written termination notice |
| `users.jsonl` (per tenant) | Each user address seen, with the time it was first seen | Not pruned or aged: the rows are kept until PowerDataChat deletes the tenant in the brain's admin panel, which removes the tenant's whole folder. The operator can also remove one address; it is recorded again on that user's next request. Contractual period: the life of the contract; the tenant folder is deleted within 30 days of written termination notice, and individual addresses are removed on the customer's request |
| `activity.jsonl` (per tenant) | Sign-in, upload, chart and report events: time, event name, user address, event metadata (e.g. a file name) | Not pruned or aged: the rows are kept until PowerDataChat deletes the tenant in the brain's admin panel, which removes the tenant's whole folder. Contractual period: the life of the contract; the tenant folder is deleted within 30 days of written termination notice |
| Tenant configuration | Tenant name, token, model settings, report template | For the life of the contract |

The brain stores no uploaded file, no table and no chart. The storage bucket
behind the brain (`pdc-enterprise-brain-data`) has object versioning. The
brain's deployment documentation sets no lifecycle rule for it, so unless one
was added outside that documentation the previous version of a deleted or
overwritten record is kept until it is removed by hand. No such rule exists
today; superseded versions are kept until an operator removes them. When a
tenant is deleted, its noncurrent versions are deleted in the same operation,
so nothing of that tenant remains recoverable afterwards.

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
