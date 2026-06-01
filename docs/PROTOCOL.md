# Client ↔ Brain protocol

This file documents the **brain HTTP surface** (`/v1/*`) **as the client uses
it** — the request the client sends and the response it consumes. For the
client-side HTTP surface that `dashboard.js` calls, see
[`CLIENT_ENDPOINTS.md`](CLIENT_ENDPOINTS.md).

> Scope: this is the client-facing contract only. The brain's internal
> implementation (how it generates code or narratives, model selection, prompt
> engineering, storage) is out of scope and lives in the private brain repo.

Transport: **HTTPS**. Every `/v1/*` call requires
`Authorization: Bearer <tenant_token>`. The brain validates the token;
revoked / suspended tenants get **HTTP 403** (the kill-switch).

---

## Fields the client builds for these calls

| Field          | Built by the client from |
|----------------|--------------------------|
| `schema_text`  | `schema_builder.schema_text` — column names, dtypes, sampled hints |
| `df_names`     | `list(dfs.keys())` |
| `df_columns`   | `{name: list(df.columns)}` — only sent on retry, for column self-correction |
| `history_rows` | the local conversation history (text turns only) |
| `common_fields`| user-confirmed join columns |
| `error_msg`    | `exec_out["error"]` from `safe_execute` |
| `failed_code`  | the failed code block |
| `preview`      | the scalar-only `_safe_preview` value (summarize) |
| `qa_pairs`     | the no-value findings list the client builds locally (report) |

No DataFrame rows or cell values appear in any field above.

---

## `POST /v1/plan`

The brain's planner. The client sends the question + schema text + history and
receives generated code to execute locally.

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
  "context_decision": { "complexity": "simple", "complexity_score": 5, "skills_needed": ["analytics_libraries"], "is_greeting": false }
}
```

---

## `POST /v1/retry`

Used when local execution of the planned code raises an error. The client sends
the failed code + error text + schema and receives corrected code.

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
  "use_pro": false,                       // hint: harder retry
  "use_search": false,                    // hint: allow web grounding on last retry
  "user_email": "alice@acme.com"
}
```

### Response

```jsonc
{
  "raw_text": "...",
  "kind": "PYTHON",
  "code": "df.groupby('department')['salary'].mean()",
  "usage": { ... }
}
```

---

## `POST /v1/describe`

Generates a brief natural-language intro from **the code only**, never the
result. No data values are sent.

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

Only `df_names` is sent so the reply can mention the user's uploaded file
names — no data.

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

Used only for **scalar** results (non-table, non-image). The client filters
`preview` to scalar-safe values via the `_safe_preview` guard before sending.

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

Generates the chat name + welcome message + suggested questions for a newly
created chat. Only file names + descriptions + schema context are sent.

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

---

## `POST /v1/auto_analytics_plan`

Auto Analytics planner. The client sends schema text only and receives a set of
natural-language analytical instructions to run locally.

```jsonc
{
  "sid": "...",
  "schema_text": "File: sales.xlsx\nColumns: ...",
  "df_names": ["sales.xlsx"],
  "common_fields": [],
  "user_email": "alice@acme.com"
}
```

Returns `{"instructions": ["...", "...", ...]}` — each instruction a distinct
analytical direction (intent + column names + chart hint), detailed enough for
the client to turn into code. The client iterates each instruction through
`run_chat_local.run_chat` (the same `/v1/plan` + `/v1/retry` path chat uses),
builds synthetic Q&A pairs, then calls `/v1/report` for the narrative and
renders the PPTX locally. NO raw row data ever crosses the boundary.

---

## `POST /v1/activity`

Centralized per-tenant activity log. The client posts events on
login / file_uploaded / plot_generated / report_exported. No data values are
sent.

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
Returns **404** when no template is configured for this tenant (the client
falls back to the built-in renderer). The companion spec endpoint is below.

---

## `GET /v1/pptx_template_spec`

Returns whether this tenant has a `.pptx` template, and the strict **v2 build
plan** the client renderer consumes:

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

Client-side validation rules: missing shape entries default to `keep` (safer
than silently dropping chrome). `replace:title` / `replace:body` /
`replace:agenda` are capped at one each per slide. If the cover or content slide
is missing / out of range, the client falls back to the built-in renderer. The
renderer never invents shapes — it only touches shapes the plan references on
the cloned slides, plus an `add_picture` in `chart_region` on content slides.

When `has_template` is `false`, `spec` is `null`. The client caches both the
file and the spec under `DATA_ROOT/templates_cache/` keyed by a schema marker
(`*.v2.pptx`, `*.v2.json`) with a short TTL so a re-uploaded template is picked
up without a client restart.

---

## `GET /v1/app_settings`

Returns the application settings the client should honor at upload time:
`MAX_FILES`, `TITLE_MAX_LEN`, `TITLE_BREAK_MIN`.

```jsonc
{ "max_files": 10, "title_max_len": 80, "title_break_min": 30 }
```

---

## `POST /v1/title`

Background conversation-title generation. The client fires this after the 2nd
human message in a conversation.

```jsonc
{ "sid": "...", "question": "...", "answer": "...", "lang": "English", "user_email": "..." }
```

Returns `{ "title": "Compensation Overview" }` (2-3 words, language-aware).

---

## `POST /v1/send_share_email`

Brain-side SMTP relay for tenant-issued share invites. NO raw row data is
forwarded — only the invitation prose.

```jsonc
{
  "to": ["a@x.com", "b@y.com"],
  "subject": "alice@acme.com shared an analysis with you",
  "sender_email": "alice@acme.com",
  "chat_title": "Compensation Overview",
  "message": "Optional accompanying text."
}
```

Returns `{ok, sent: [], failed: [], smtp_configured: bool}`. If the tenant has
no SMTP configured, returns HTTP 503 with `smtp_configured: false` (the client
surfaces "shared but no email sent — share credentials manually").

---

## `POST /v1/schema_autofill`

Combined autofill — file description + per-column descriptions in one call. The
client builds the per-file context locally and sends **only** filename, dtypes,
sampled / truncated unique values, a language hint, and user notes. No raw row
data crosses the boundary beyond the sampled hints. One call per file; the
client runs them in parallel and merges into `meta.json`.

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

On parse failure / error the brain returns `{file_description: "", columns: {}}`
and the client falls back to a generic file description.

---

## `POST /v1/file_description`

Summarizes extracted header text (or any blurb) into a 2-3 sentence dataset
description.

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

The client first builds the no-value findings (`qa_pairs`) locally, then sends
them. The brain returns the narrative JSON; the client renders the PPTX/PDF
into its own template locally.

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

The client implements all of these in [`brain_client.py`](../brain_client.py)
as `BrainError` / `TenantRevokedError`.
