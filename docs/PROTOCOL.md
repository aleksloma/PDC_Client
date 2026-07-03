# Client ↔ Brain protocol

This file documents the **brain HTTP surface** (`/v1/*`). For the client-side
HTTP surface that `dashboard.js` calls, see
[`CLIENT_ENDPOINTS.md`](CLIENT_ENDPOINTS.md).

---


> **Important:** the prompt explicitly said *"DO NOT invent new request /
> response JSON shapes — read how the existing app already passes this
> same data internally and reuse those existing shapes across the new
> client↔brain boundary."*
>
> Every endpoint below corresponds 1:1 to a function in the existing B2C
> codebase, with the only change being that pandas DataFrames are
> replaced by pre-built schema text + df names. The pure-LLM payloads
> are identical to what the B2C agent already builds in-process.

Transport: **HTTPS**. Every `/v1/*` call requires
`Authorization: Bearer <tenant_token>`. The brain validates the token by
lookup; revoked / suspended tenants get **HTTP 403** (the kill-switch).

---

## Field-shape mapping back to the B2C code

| Brain field          | Same field in B2C | Built by |
|---------------------|-------------------|----------|
| `schema_text`       | return value of `_schema_text(schema_docs, dfs, common_fields)` | client (`schema_builder.schema_text`) |
| `df_names`          | `list(dfs.keys())` | client |
| `df_columns`        | `{name: list(df.columns)}` (only sent on retry, for column self-correction) | client |
| `history_rows`      | the `history_rows` arg to `generate_pandas_code` / `summarize_answer` | client (`local_store.get_conversation_history`) |
| `common_fields`     | the `common_fields` arg to `generate_pandas_code` | client |
| `error_msg`         | `exec_out["error"]` from `safe_execute` | client |
| `failed_code`       | the failed code block | client |
| `preview` (summarize) | the `safe_preview` value the B2C code already restricts to scalars | client |
| `qa_pairs` (report) | the `findings_for_llm` list the B2C `_generate_report_structure` already builds | client |

---

## `POST /v1/plan`

The brain's planner — same prompt + multi-turn history + model-fallback
chain as `agent.generate_pandas_code`. Returns the same `(raw_text,
usage, context_decision)` tuple the B2C code returns internally, plus
the convenience `kind` + `code` from `_extract_code_kind`.

### Request

```jsonc
{
  "sid": "8af3d2e1",
  "question": "show me average salary by department",
  "schema_text": "File: sales.csv\nColumns: name, department, salary\n...",
  "df_names": ["sales.csv"],
  "history_rows": [
    { "role": "human", "content": "first question..." },
    { "role": "ai",    "content": "first answer...", "code": "df.groupby(...)..." }
  ],
  "common_fields": [],
  "user_email": "alice@acme.com"
}
```

### Response

```jsonc
{
  "raw_text": "```python\\nresult = df.groupby('department')['salary'].mean()...\\n```",
  "kind": "PYTHON",                       // PYTHON | PLOT_CODE | NO_CODE | CLARIFICATION
  "code": "result = df.groupby('department')['salary'].mean()",
  "usage": { "input_tokens": 1234, "output_tokens": 56, "total_tokens": 1290 },
  "context_decision": { "complexity": "simple", "complexity_score": 5, "skills_needed": ["analytics_libraries"], "is_greeting": false, ... },
  "model_used": "gemini-2.5-pro"
}
```

---

## `POST /v1/retry`

Same shape as the B2C `_retry_code_with_error`. The brain rebuilds the
"previous code failed with X — here is the schema — produce corrected
code" prompt and calls the simple (or complex, on later attempts) model.

### Request

```jsonc
{
  "sid": "8af3d2e1",
  "question": "...",
  "schema_text": "...",
  "df_names": ["sales.csv"],
  "df_columns": { "sales.csv": ["name", "department", "salary"] },
  "history_rows": [ ... ],
  "error_msg": "KeyError: 'Department'",
  "failed_code": "df.groupby('Department')['salary'].mean()",
  "use_pro": false,                       // promotes to complex model on 2nd retry
  "use_search": false,                    // enables Google Search grounding on last retry
  "user_email": "alice@acme.com"
}
```

### Response

```jsonc
{
  "raw_text": "...",
  "kind": "PYTHON",
  "code": "df.groupby('department')['salary'].mean()",
  "usage": { ... },
  "model_used": "gemini-2.5-pro"
}
```

---

## `POST /v1/describe`

Mirror of `agent._describe_from_code` — generates a brief natural intro
from **the code only**, never the result. No data values ever sent.

### Request

```jsonc
{
  "sid": "8af3d2e1",
  "question": "show me average salary by department",
  "code": "result = df.groupby('department')['salary'].mean()",
  "user_email": "alice@acme.com"
}
```

### Response

```jsonc
{ "text": "Below is the average salary for each department.", "usage": { ... } }
```

---

## `POST /v1/greeting`

Mirror of `agent._respond_to_greeting`. Only `df_names` is sent so the
LLM can mention the user's uploaded file names in the reply — no data.

### Request

```jsonc
{ "sid": "...", "question": "hi", "df_names": ["sales.csv"], "user_email": "..." }
```

### Response

```jsonc
{ "text": "Hello! I'm a data analyst assistant. ...", "usage": { ... } }
```

---

## `POST /v1/summarize`

Mirror of `agent.summarize_answer`. Used only for **scalar** results
(non-table, non-image). The client is responsible for filtering `preview`
to scalar-safe values — the same `safe_preview` guard the B2C code
already enforces.

### Request

```jsonc
{
  "sid": "...",
  "question": "what is the highest salary?",
  "schema_text": "...",
  "history_rows": [ ... ],
  "preview": 162000,                      // scalar ONLY; DataFrames are stripped client-side
  "context_decision": { "complexity": "simple", ... },
  "user_email": "..."
}
```

### Response

```jsonc
{ "text": "The highest salary in the dataset is **$162,000**.", "usage": { ... } }
```

---

## `POST /v1/chat_metadata`

Verbatim port of global `_generate_all_parallel` (3 parallel sub-calls:
chat name + welcome message + suggested questions). Per-tenant API key +
per-tier model overrides apply automatically.

### Request

```jsonc
{
  "sid": "...",
  "files_info": ["sales.csv"],
  "file_descriptions": {"sales.csv": "Employee salaries by department"},
  "context": "File: sales.csv\nDescription: ...\nColumns:\n  - name ...",
  "lang_instruction": "English",
  "columns_to_human": {"loan_int_rate": "loan int rate"},
  "user_email": "..."
}
```

`lang_instruction` is now only a **fallback hint** (the language the client
detected from the file). The brain decides the welcome/questions language by
this precedence:

1. the tenant's `welcome_language` config override (`effective_settings()`),
2. else the request's `lang_instruction` (the client-detected hint),
3. else `"English"`.

So a tenant with `welcome_language = "Georgian (ქართული)"` always gets a
Georgian welcome + questions regardless of column-name language; a tenant that
leaves it unset behaves exactly as before (client-detected → English). The same
resolved language governs BOTH the welcome message and the suggested questions
(one call).

### Response

```jsonc
{
  "name": "Employee Insights",
  "welcome_message": "Hello! I am your personal data analyst, ... For example you can ask:",
  "suggested_questions": [
    "Which department has the highest average salary?",
    "How does the total salary spend compare across different departments?",
    "Who are the highest paid employees in the company?"
  ]
}
```

The welcome message and questions are the same prompts and sanitizers
global uses — output is byte-compatible.

---

## `POST /v1/auto_analytics_plan`

Auto Analytics planner (COMPLEX tier). Designs the set of analyses for the
report. The **request shape is unchanged** (still schema-text only), but the
planner now reasons over the tenant's DOMAIN CONTEXT in addition to the schema:
it injects the tenant's enabled domain skill (terminology / KPIs / expected
columns / analysis style via `skill_loader`), the free-text `domain_vocabulary`,
and the operator's `prompt_tuning_planner` — all resolved server-side from
`effective_settings()`. These are **shared brain assets, not client row data**,
so the boundary is intact. If no domain skill is configured (or it fails to
load) the planner degrades gracefully to schema-only planning.

```jsonc
{
  "sid": "...",
  "schema_text": "File: sales.xlsx\nColumns: ...",
  "df_names": ["sales.xlsx"],
  "common_fields": [],
  "user_email": "alice@acme.com"
}
```

Returns `{"instructions": ["...", "...", ...]}`. The planner is steered to
produce a RICH, NON-REPETITIVE set — each instruction a DISTINCT finding on a
different dimension/metric/relationship, detailed enough for the code-writer
(intent + exact column names + chart hint). Server-side post-processing:
near-duplicate instructions are dropped (Jaccard token overlap), and if the
usable count is below the target (`_AUTO_ANALYTICS_TARGET` = 7) the brain does
ONE targeted re-ask for additional distinct directions rather than padding with
trivial charts. The list is then capped to `_AUTO_ANALYTICS_MAX` = **15 plots**
(this is analyses/plots, NOT total slides). A soft `AUTO_ANALYTICS_PLAN_UNDER_TARGET`
log line fires when the final count stays under target so chronic
under-production stays visible. The client iterates each instruction through
`run_chat_local.run_chat` (unchanged — same `/v1/plan` + `/v1/retry` path chat
uses), builds synthetic Q&A pairs, then calls `/v1/report` for the narrative and
renders the PPTX locally. NO raw row data ever crosses the boundary.

---

## `POST /v1/activity`

Centralized per-tenant activity log. The client posts events on
login / file_uploaded / plot_generated / report_exported. Stored as
`tenants/{tenant_id}/activity.jsonl` (append-only JSONL). Powers the
"Last login / Last activity" columns in the per-tenant admin Users tab.

```jsonc
{
  "event": "login" | "file_uploaded" | "plot_generated" | "report_exported",
  "user_email": "alice@acme.com",
  "metadata": { /* event-specific, e.g. {"filename": "...", "size_bytes": 1234} */ }
}
```

Returns `{ok: true}`.

---

## `GET /v1/pptx_template`

Streams the tenant's uploaded branded `.pptx` template file as
`application/vnd.openxmlformats-officedocument.presentationml.presentation`.
Returns **404** when the operator has not uploaded a template for this tenant
(the client falls back to the built-in renderer in that case). The companion
spec endpoint is below.

---

## `GET /v1/pptx_template_spec`

Returns whether this tenant has a `.pptx` template uploaded, and the strict
**v2 build plan** the brain produced (COMPLEX-tier analysis at upload time):

```jsonc
{
  "has_template": true,
  "spec": {
    "version": 2,
    "deck": {
      "cover_slide_index": 0,           // template slide cloned for page 1
      "agenda_slide_index": 1,          // null = skip the agenda slide
      "content_slide_index": 2          // cloned once per finding
    },
    "slides": {
      // Key = stringified template slide index. One entry per slide
      // referenced by `deck` (cover / agenda / content).
      "0": {
        "role": "cover",
        "chart_region": null,
        "shapes": [
          // EVERY shape on the cloned slide gets one of:
          //   "keep" | "drop"
          //   "replace:title" | "replace:body" | "replace:agenda"
          { "shape_id": 4, "shape_name": "Logo",      "label": "keep",          "text_style": null },
          { "shape_id": 5, "shape_name": "TitleText", "label": "replace:title",
            "text_style": { "font": "Calibri Light", "size_pt": 48, "bold": true,
                             "color_hex": "001E44", "align": "center" } },
          { "shape_id": 7, "shape_name": "AuthorLine","label": "drop",          "text_style": null }
        ]
      },
      "2": {
        "role": "content",
        "chart_region": { "left_in": 0.5, "top_in": 2.0, "width_in": 12.3, "height_in": 4.5 },
        "shapes": [
          { "shape_id": 12, "shape_name": "HeaderBar",  "label": "keep",          "text_style": null },
          { "shape_id": 13, "shape_name": "PageTitle",  "label": "replace:title",
            "text_style": { "font": "Calibri Light", "size_pt": 28, "bold": true,
                             "color_hex": "001E44", "align": "left" } },
          { "shape_id": 14, "shape_name": "Narrative",  "label": "replace:body",
            "text_style": { "font": "Calibri", "size_pt": 14, "bold": false,
                             "color_hex": "374151", "align": "left" } },
          { "shape_id": 15, "shape_name": "SampleTable","label": "drop",          "text_style": null }
        ]
      }
    },
    "theme_colors": { "title": "44546A", "accent": "4472C4", "body": "000000", "muted": "E7E6E6" },
    "fonts": { "header": "Calibri Light", "body": "Calibri" },
    "notes": "Cover is a logo-only page; content slides have a left header bar with the page number."
  }
}
```

Validation rules: missing shape entries default to `keep` (safer than
silently dropping chrome). `replace:title` / `replace:body` /
`replace:agenda` are capped at one each per slide. If the cover or
content slide is missing / out of range, the client falls back to the
built-in renderer. The renderer never invents shapes — it only touches
shapes the plan references on the cloned slides, plus an `add_picture`
in `chart_region` on content slides.

When `has_template` is `false`, `spec` is `null`. The client caches both the
file and the spec on `DATA_ROOT/templates_cache/` keyed by a schema marker
(`*.v2.pptx`, `*.v2.json`) with a short TTL so an operator re-uploading a
template is picked up without a client restart. Older v1 caches are purged
on first refresh after a deploy.

---

## `GET /v1/app_settings`

Returns the per-tenant application settings the client should honor at upload
time: `MAX_FILES`, `TITLE_MAX_LEN`, `TITLE_BREAK_MIN`. Empty per-tenant values
fall back to the brain-wide default (then to a hardcoded fallback).

```jsonc
{ "max_files": 10, "title_max_len": 80, "title_break_min": 30 }
```

---

## `POST /v1/title`

Background conversation-title generation. The client fires this after the 2nd
human message in a conversation (matches global's UX). Uses the Light model.

```jsonc
{ "sid": "...", "question": "...", "answer": "...", "lang": "English", "user_email": "..." }
```

Returns `{ "title": "Compensation Overview" }` (2-3 words, language-aware).

---

## `POST /v1/send_share_email`

Brain-side SMTP relay for tenant-issued share invites. Uses the tenant's
`smtp_host` / `smtp_port` / `smtp_username` / `smtp_password` / `smtp_from`
config set on the per-tenant admin page. NO raw row data is forwarded — only
the invitation prose.

```jsonc
{
  "to": ["a@x.com", "b@y.com"],
  "subject": "alice@acme.com shared an analysis with you",
  "sender_email": "alice@acme.com",
  "chat_title": "Compensation Overview",
  "message": "Optional accompanying text."
}
```

Returns `{ok, sent: [], failed: [], smtp_configured: bool}`. If the tenant
has no SMTP configured, returns HTTP 503 with `smtp_configured: false` (the
client surfaces "shared but no email sent — share credentials manually").

---

## `POST /v1/send_welcome_email`

Gmail relay for the client's password-auth lifecycle. Sends the fixed
"Welcome to PowerDataChat" mail to a user who signed in (and set their
password) for the first time on this tenant's client. Uses the brain-wide
operator Gmail account (`GMAIL_SENDER` / `GMAIL_APP_PASSWORD` env vars,
stdlib `smtplib`, smtp.gmail.com:587 STARTTLS) — NOT the per-tenant
`smtp_*` config that `/v1/send_share_email` uses.

```jsonc
{ "sid": "8af3d2e1", "email": "alice@acme.com" }
```

Returns `{ok: true, email_configured: true}`. Errors:

| HTTP | When |
|------|------|
| 400  | missing/invalid `email` |
| 429  | rate limit — max 5 mails per (tenant, recipient) per hour |
| 502  | SMTP send failed (`{ok: false, error, email_configured: true}`) |
| 503  | Gmail relay not configured (`{email_configured: false}`) |

The client treats this call as fire-and-forget: a failure is logged
client-side and login proceeds regardless.

---

## `POST /v1/send_password_reset_email`

Same relay + same rate limit / error shape as the welcome mail. Sends the
user their temporary password and tells them they must change it after
login. The temp password is generated ON THE CLIENT (which stores only its
hash); it appears solely in the outgoing mail body — the brain never logs
or stores it.

```jsonc
{ "sid": "8af3d2e1", "email": "alice@acme.com", "temp_password": "k3P…" }
```

Returns `{ok: true, email_configured: true}` (400 also when
`temp_password` is missing).

---

## `POST /v1/schema_autofill`

Combined autofill — file description + per-column descriptions in one LLM call,
**verbatim port** of global's `_build_combined_autofill_prompt` +
`_parse_combined_response` (`backend/routes/schema.py` L813-881). One call per
file; the client runs them in parallel and merges the results into `meta.json`.

The client builds the per-file context locally (same logic as global's
`_prepare_file_context`) so the brain receives **only** the same inputs global
itself feeds to the LLM: filename, dtypes, sampled / truncated unique values,
language hint, user notes. No raw row data ever crosses the boundary beyond
what global itself samples for this prompt.

### Request

```jsonc
{
  "sid": "...",
  "user_email": "alice@acme.com",
  "fname": "sales.csv",
  "cols_to_fill": ["name", "department", "salary"],
  "unique_hints": {
    "department": ["Engineering", "Sales", "Support"],
    "salary": ["145000", "82000", "158000"]
  },
  "dtypes": {"name": "object", "department": "object", "salary": "int64"},
  "file_desc": "",                       // existing description, if any
  "notes_text": "",                      // user notes blob (≤ 2000 chars)
  "lang_name": "English",                // detected from column names
  "desc_word_limit": 20
}
```

### Response

```jsonc
{
  "file_description": "Employee compensation by department.",
  "columns": {
    "name":       "Full name of the employee",
    "department": "Organizational unit where the employee works",
    "salary":     "Annual gross compensation in dollars"
  }
}
```

Brain uses the **Light** tier model (`light_model` + per-tenant override),
temperature 0.1, max 4096 tokens. On parse failure / LLM error the brain
returns `{file_description: "", columns: {}}` and the client falls back to a
generic file_description like global does.

---

## `POST /v1/file_description`

Verbatim port of global upload.py's auto file-description LLM call (summarize
text extracted from Excel headers, or any blurb, into a 2-3 sentence dataset
description).

### Request

```jsonc
{
  "sid": "...",
  "extracted_text": "Columns of file 'sales.csv': name, department, salary",
  "user_email": "..."
}
```

### Response

```jsonc
{ "description": "This dataset contains employee information..." }
```

---

## `POST /v1/report`

Mirror of `chat.py._generate_report_structure`. The client first runs the
same `_build_qa_pairs` logic locally (the B2C `_build_report_data`
helper), then sends only the no-values findings payload. The brain
returns the narrative JSON; the client renders the PPTX into its own
template locally.

### Request

```jsonc
{
  "sid": "...",
  "qa_pairs": [
    {
      "index": 0,
      "question": "show average salary by department",
      "answer_text": "Below is the average salary for each department.",
      "has_chart": false,
      "has_table": true,
      "table_columns": ["department", "salary"],     // column NAMES only
      "code_snippet": "result = df.groupby('department')['salary'].mean()"
    }
  ],
  "user_email": "..."
}
```

### Response

```jsonc
{
  "report_structure": {
    "report_title": "Compensation Overview",
    "filename": "compensation_overview",
    "executive_summary": "...",
    "findings": [
      { "page_title": "Compensation by Department",
        "narrative": "An analysis of compensation across departments reveals..." }
    ],
    "key_takeaways": []
  },
  "usage": { ... }
}
```

---

## Errors

| HTTP | When                                      | Client behavior                                |
|------|-------------------------------------------|------------------------------------------------|
| 401  | Missing or unknown bearer token           | Surface "Service unavailable — contact admin." |
| 403  | Tenant is `suspended` or `revoked` (kill) | Same surface — but also stop sending traffic. |
| 4xx  | Brain rejected the request payload         | Surface clean message; log full details client-side. |
| 5xx  | Brain internal error                       | Surface clean message; retry policy is per-endpoint. |

The client implements all of these in `brain_client.py` as
`BrainError` / `TenantRevokedError`.
