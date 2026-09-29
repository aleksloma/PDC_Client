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
| `history_rows`      | the `history_rows` arg to `generate_pandas_code` / `summarize_answer`. Each row carries **only** `role`, `content`, and (when present) `code` — the client's `brain_client._sanitize_history_rows` strips every other persisted field (`image_base64`, `chart_data`, `table`, `usage`, `full_table_key`, `ts`, …) before the POST, so raw data values in locally persisted records never cross the boundary (Article II) | client (`local_store.get_history`, sanitized in `brain_client.plan/retry/summarize`) |
| `common_fields`     | the `common_fields` arg to `generate_pandas_code` | client |
| `error_msg`         | `exec_out["error"]` from `safe_execute` | client |
| `failed_code`       | the failed code block | client |
| `preview` (summarize) | the `safe_preview` value the B2C code already restricts to scalars | client |
| `qa_pairs` (report) | the `findings_for_llm` list the B2C `_generate_report_structure` already builds | client |
| `dataset_profile` (plan/retry, **optional**) | enterprise-only (no B2C equivalent): `{df_key: profile}` of computed FACTS per loaded table — rows, duplicate count, per-column dtype/nunique/null rates/min-max/constant/all-unique flags, truncated top-value hints, detected grain, deterministic warnings. Aggregate metadata only, never row data (Article II — same class as the cardinality hints in `technical_description`). Absent field ⇒ pre-profile behavior everywhere | client (`dataset_profile.compute_profile`, stored as sidecar JSON, compacted by `brain_client._compact_profiles_for_transport`) |
| `data_caveat` (describe, **optional**) | enterprise-only: the client's DETERMINISTIC post-execution finding about the result it just rendered — `{kind: constant_metric\|identical_series\|constant_table\|matrix_readability, facts[], grain[], catalog}`. Aggregate findings + column names + truncated constant-value hints only (Article II, same class as `dataset_profile`). Makes the flat-result explanation mandatory instead of prompt-dependent; absent field ⇒ byte-identical describe prompt | client (`result_backstop.inspect_outputs`, compacted by `brain_client._compact_caveat_for_transport`) |
| `live_tables` (plan/retry, **optional**) | enterprise-only (no B2C equivalent): `[{name, dialect, row_cap, filtered}]` — one row per LIVE database table the chat holds (`name` = the df key, `dialect` = the connector registry key such as `postgresql` / `mysql` / `mariadb` / `mssql` / `oracle` / `clickhouse`, `row_cap` = the effective row cap, `filtered` = true when an administrator row filter applies, in which case the client fetches the rows itself and expects no SQL). Metadata only; sent only when the chat holds a live table | client (`run_chat_local._live_tables_for_brain` from `ChatDataStore.schema_docs`) |
| `sql` (plan/retry response, **optional**; echoed on a retry request) | enterprise-only: `{<df key>: "<one read-only SELECT>"}` written by the brain for the live tables the code references. The client validates it (read-only guard + a per-table allowlist), runs it in the web application, and places the result under the df key; a missing entry ⇒ the client's default capped fetch. On a retry request (every retry, the regeneration ones included) the client sends what RAN this turn: the brain's own text for a SELECT that reached the database (also when it failed there), `null` for a default read, for a SELECT ignored on a `filtered` table and for a SELECT the client's guard refused; keys never fetched are absent — never a result | brain (planner); echoed by client (`brain_client.retry(sql=)`) |
| `sql_error` (retry, **optional**) | enterprise-only: `{table, dialect, class, guard, message}` describing a failed live SELECT, or (once, on the next retry) a SELECT ignored on a `filtered` table — `class` ∈ `syntax` / `unknown_column` / `unknown_table` / `timeout` / `permission` / `guard` / `other`; `guard` true when the client's own SQL gate refused the text, and then `message` is the gate's sentence (naming at most an identifier), otherwise `message` is null. Never the driver message, a literal or a cell value (Article II) | client (`brain_client.live_sql_error`) |

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
  "user_email": "alice@acme.com",
  "dataset_profile": {                     // OPTIONAL — omitted by pre-profile clients
    "sales.csv": {
      "profile_version": 1, "rows": 12480, "duplicate_row_count": 0,
      "computed_at": "2026-08-23T09:14:02+00:00", "sampled": false,
      "grain": { "columns": ["order_id"], "kind": "single", "text": "one row per (order_id)" },
      "warnings": ["Quantity is constant: every value = 1"],
      "columns": { "salary": { "dtype": "int64", "nunique": 240, "null_count": 0,
                                "null_pct": 0.0, "min": 900, "max": 21000,
                                "constant": false, "all_unique": false } }
    }
  },
  "live_tables": [                         // OPTIONAL — present ONLY when the chat holds
    { "name": "transactions",              //   a LIVE database table (queried at question time)
      "dialect": "postgresql",             // connector registry key: postgresql | mysql | mariadb | mssql | oracle | clickhouse
      "row_cap": 200000,                   // the effective row cap of the SELECT's result
      "filtered": false }                  // true ⇒ an administrator row filter applies: the client
  ]                                        //   pre-fetches the rows itself and expects NO SQL for it
}
```

The brain injects the profile LAZILY into the planner prompt: a one-line
micro-summary per table (`PROFILE <name>: rows=N; warnings: ...`) ALWAYS,
and the full block (capped ~15 lines/table) only when the query analyzer
classifies the question as a data task (aggregation / visualization /
statistical or complex analysis; classifier failure counts as a data task).
Logs carry only `profile_tables=N`, never the profile body.

**Live tables.** `live_tables` lists the chat's live database tables (their
`schema_text` entry is marked `[LIVE, dialect=<key>, row_cap=<n>]` and
carries a one-SELECT contract sentence). A chat without one posts exactly
the payload above without the field. The planner is expected to answer with
one read-only SELECT per live table the code references (the `sql` map in
the response); a `filtered` table gets no SELECT — the client reads it
itself under the administrator's filter, and a SELECT sent for it is
ignored. The client treats a live df key as referenced when the key is
quoted anywhere in the code (in a string, a label or a comment included),
when the code walks `dfs` generically (iteration, `.values()`, `in dfs`, a
variable index — then every live key not yet fetched counts), or when the
code uses `df` and the first frame is that live key (the sandbox binds `df`
to the first frame). A SELECT for a key the code does not reference is never
run. The planner side ships as of brain commit `0908843`: a request
with a non-empty `live_tables` gets a live-table block in the planner prompt,
and the response carries `sql` when the model wrote a SELECT for a referenced
live table. It needs a client at commit `884e27e` or later. An older client
never sends `live_tables` and gets exactly the earlier prompt and response,
so the brain and the client can be deployed in either order.

How the brain produces `sql`: it asks the model for one fenced ```` ```sql ````
block per table whose first line is `-- table: <df key>`, takes the SELECTs
out of the reply and removes those blocks from `raw_text` before `kind` and
`code` are extracted, so `raw_text` and `code` carry only the code. It drops
a SELECT for a filtered or unknown df key, and every SELECT next to an
`ANSWER` / `CLARIFICATION` / `MISSING_DATA` / `NO_CODE` reply. It does not
check the SQL itself: the client's gate is the authority, and a refusal comes
back as a `guard` retry. The brain logs the df keys and a hash of the `sql`
map, never the SQL text.

**What the planner must avoid, per dialect.** One SELECT per live table,
naming only that table (joins between live tables are two SELECTs; the
client's allowlist refuses any other table, CTE aliases excepted). No DML,
no procedures, no second statement, no row-locking clause. On SQL Server:
no table hints (`NOLOCK` included, with or without `WITH`); quote an alias
spelled like a T-SQL command word (`AS [open]`, not `AS open`); and give
EVERY expression in the SELECT list an alias (`count(*) AS n`), because the
client places the statement inside a derived table to apply the row cap,
and SQL Server rejects unnamed columns there. A leading `WITH` block is
hoisted above the wrapper on every dialect, so CTEs are allowed. The gate
also refuses these words wherever they appear as whole words, in string
literals and identifiers too (comments excepted): INSERT, UPDATE, DELETE,
MERGE, DROP, CREATE, ALTER, TRUNCATE, GRANT, REVOKE, EXEC, EXECUTE, CALL,
INTO, ATTACH, PRAGMA, VACUUM, COPY. On ClickHouse it refuses a `SETTINGS`
clause.

### Response

```jsonc
{
  "raw_text": "```python\\nresult = df.groupby('department')['salary'].mean()...\\n```",
  "kind": "PYTHON",                       // PYTHON | PLOT_CODE | NO_CODE | CLARIFICATION | ANSWER | MISSING_DATA
  "code": "result = df.groupby('department')['salary'].mean()",
  "usage": { "input_tokens": 1234, "output_tokens": 56, "total_tokens": 1290 },
  "context_decision": { "complexity": "simple", "complexity_score": 5, "skills_needed": ["analytics_libraries"], "is_greeting": false, ... },
  "model_used": "gemini-2.5-pro",
  "sql": {                                // OPTIONAL — one read-only SELECT per live df key the
    "transactions": "SELECT region, SUM(amount) AS total FROM public.transactions GROUP BY region"
  }                                       //   code references; unknown fields are ignored
}
```

`sql` (optional): `{<df key>: "<one read-only SELECT>"}`. The client never
runs it as sent: the text passes the read-only guard (strict parse, CTEs
allowed) and a per-table allowlist — the SELECT may read only the table it
was written for (its registered schema, the unqualified name or the
connection's database name; CTE aliases exempt) — is wrapped under the row
cap, and runs in the web application, never in the analysis sandbox. The
result frame is placed under the df key, so the Python must use the SELECT's
own columns. A key without an entry gets the client's default capped fetch.
A value is always a non-empty string; the brain never sends `null` in the
response. Unknown fields in the response are ignored.

`kind` semantics: `PYTHON` / `PLOT_CODE` carry executable code in `code`;
`CLARIFICATION` carries the clarifying question in `code`; `NO_CODE` is
greetings-only (empty `code`); `ANSWER` carries a plain-text/markdown answer
in `code` for questions about an EXISTING result or method (e.g. "how did
you compute this?") that need no new computation. `MISSING_DATA` carries, in
`code`, a plain-text notice that the loaded data cannot answer the question —
what is missing, which registered table would provide it (when the schema's
OTHER REGISTERED TABLES list names one), and what IS answerable instead; the
planner emits it instead of fabricating lookup tables or id-to-name mappings
from domain context. The client returns `CLARIFICATION`, `ANSWER`, and
`MISSING_DATA` text to the user directly without executing anything; a flow
that requires code treats `ANSWER` and `MISSING_DATA` exactly like `NO_CODE`
(retry loops count them as failed prose attempts).

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
  "user_email": "alice@acme.com",
  "dataset_profile": { ... },             // OPTIONAL — same shape as /v1/plan; retry
                                          // injects the micro-summary lines only (no
                                          // classifier runs on the retry path)
  "live_tables": [ ... ],                 // OPTIONAL — same rows as /v1/plan, only when the
                                          //   chat holds a live table
  "sql": { "transactions": "SELECT ..." },// OPTIONAL — what RAN this turn: the brain's text for
                                          //   a SELECT that reached the database, null for a
                                          //   default read / an ignored or refused SELECT
  "sql_error": {                          // OPTIONAL — present when a live SELECT failed, or
                                          //   once after a SELECT ignored on a filtered table
    "table": "transactions",              //   the df key
    "dialect": "postgresql",
    "class": "unknown_column",            //   syntax | unknown_column | unknown_table | timeout
                                          //   | permission | guard | other
    "guard": false,                       //   true ⇒ the client's SQL gate refused the text
    "message": null                       //   the gate's own sentence when guard is true
  }                                       //   (an identifier at most), otherwise null
}
```

**Live tables.** When the failure that triggered the retry is a live SELECT
(not the Python), `error_msg` is the value-free sentence
`Live query for table '<df key>' failed: <class sentence>` — one fixed
sentence per class (e.g. "The query names a column that does not exist.",
"The live query timed out."), or the guard's own message for `guard` — and
`sql_error` carries the class. The driver's message never crosses: it can
quote literals and cell values, and it stays in the client. The response
may carry `sql` again (a new SELECT for the failed key); without one the
attempt counts as failed — the client never falls back to its default fetch
for a SELECT that failed, so a retry cannot silently change what the answer
computes. A retry after a Python failure carries `sql` (what ran) and no
`sql_error` — except when a SELECT was sent for a `filtered` table and
ignored: then `sql` has `null` for that key and `sql_error` is
`{class: "guard", guard: true, message: "The table is filtered by the
administrator; no SELECT is accepted for it."}`. That refusal rides exactly
one retry: the next one that reports an execution error (a regeneration
retry, below, never carries or consumes it, and a live SELECT that failed
on another table in the same attempt is reported first, the refusal
waiting for the retry after it); a planner that sends another SELECT for
the filtered table gets it once more, on the next such retry.
The filtered table itself is read by the default read once per turn and is
not re-read for a resent SELECT. A SELECT the guard refused never reached
the database, so it also appears as `null` in `sql`, with the refusal in
`sql_error`. The four regeneration retries (a chart redrawn as separate
single-figure blocks, as Plotly instead of a static image, or as a grouped
bar instead of a count matrix) carry `sql` (what ran) and `sql_error: null`.

On the brain, the retry prompt shows a SELECT's text only for a key whose
`sql` value is a string. A `null` key is described as a default read or a
refused SELECT, never as a SELECT that ran. For `guard` the prompt carries
the gate's message; for the other classes only the class sentence. The brain
accepts a new SELECT for the failed table, for any unfiltered live table and
for any key in `sql`, never for a filtered table. It drops a returned SELECT
identical to the one that already ran for that key, since the client would
only run it again. It logs the `sql_error` class, not `sql_error.message`.
For a `guard` refusal that sentence still reaches the log inside
`error_msg`, which the brain has always logged truncated; it names at most
an identifier.

### Response

```jsonc
{
  "raw_text": "...",
  "kind": "PYTHON",                       // PYTHON | PLOT_CODE | NO_CODE | CLARIFICATION | ANSWER | MISSING_DATA
  "code": "df.groupby('department')['salary'].mean()",
  "usage": { ... },
  "model_used": "gemini-2.5-pro",
  "sql": { ... }                          // OPTIONAL — a new SELECT per live df key, same
                                          //   rules as /v1/plan; a key it names is re-fetched
}
```

**Data boundary (Article II).** Across the whole live path only the brain's
own SQL text (in the response, echoed on a retry) and an error CLASS cross;
the rows a SELECT returns never leave the client — they reach the analysis
sandbox as an ordinary input frame and the brain sees, as always, only the
code, the schema text and the scalar preview.

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
  "user_email": "alice@acme.com",

  // OPTIONAL (Prompt 14). The client's DETERMINISTIC inspection of the executed
  // result — pure pandas, no values. Sent only when a flat/degenerate result was
  // detected; absent ⇒ the describe prompt is byte-identical to before.
  "data_caveat": {
    // constant_metric | identical_series | constant_table | matrix_readability
    // (Prompt 15: matrix_readability = the result is fine but the ENCODING is
    // not — a count matrix small enough to read as a grouped bar. The client
    // asks the planner for one redraw first; the caveat is the fallback.)
    "kind": "constant_metric",
    "facts": ["Quantity is the same for every group in the chart (= 1)"],
    "grain": ["cl prod link: one row per (client_id, product_id)"],
    "catalog": true                      // catalog/link grain, not measured quantities
  }
}
```

`data_caveat` carries aggregate findings, column NAMES, and 40-char-truncated
constant-value hints only — the same Article II class as `dataset_profile`
(guarded client-side by `_compact_caveat_for_transport`: known keys, known
`kind`, ≤6 items × ≤200 chars). The brain renders it as a MANDATORY DATA NOTE
block and requires the description to START with the explanation, prefixed by
the literal marker `[[DATA_NOTE]]`. The client strips that marker before
display and, if it is absent (or the call failed), prepends its own localized
sentence — so the explanation reaches the user even when the model omits it.
Unknown/malformed caveats are ignored, never fatal.

### Response

```jsonc
{ "text": "[[DATA_NOTE]] Every product has the same value per city here — this is availability, not sold quantities. The table below lists them.", "usage": { ... } }
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
user a link that sets a new password. The link is generated ON THE CLIENT: it
carries a single-use token valid for 30 minutes, of which the client stores
only a SHA-256 hash. Its address is the client's configured
`PUBLIC_BASE_URL`, never taken from an incoming request; a client without
that setting sends no reset mail. The link is a credential until it is used
or expires, so it appears solely in the outgoing mail body — the brain never
logs or stores it.

```jsonc
{ "sid": "reset-email", "email": "alice@acme.com",
  "reset_url": "https://pdc.acme.internal/auth/reset/Q2x…",
  "kind": "reset" }            // optional: "reset" (default) | "invite"
```

`reset_url` must be an absolute `http://` or `https://` URL with a host and
no user info (`user@` / `user:pw@`), at most 2048 characters, with no
whitespace, control or invisible format characters (zero-width, bidi
overrides — they could make the mailed link read differently from its
target). The brain has no
per-tenant allowed-hosts list: the host is whatever the client's
`PUBLIC_BASE_URL` says.

`kind` picks the wording; the link handling is the same for both:

| `kind` | Subject | Body |
|--------|---------|------|
| `reset` (default) | "PowerDataChat password reset" | a reset was requested; set a new password at the link; if you did not request it, ignore the mail — the current password stays valid |
| `invite` | "You have been invited to PowerDataChat" | you have been given access; set your password at the link; if it has expired, use "Reset password" on the sign-in page |

Both mails state that the link is valid for 30 minutes and can be used only
once. Neither contains a password.

**Deprecated shape — `temp_password`.** Clients not yet upgraded (the hosted
demo) still send `{sid, email, temp_password}`. The brain keeps accepting
it: the mail is the old one, unchanged ("Your temporary password is: …",
change it after login), and every such call logs one
`RESET_EMAIL_TEMP_PASSWORD_DEPRECATED` warning (tenant id only — never the
password). Upgraded clients never send it; it will be removed once no
client does.

Returns `{ok: true, email_configured: true}`. Errors, besides the welcome
mail's 400 / 429 / 502 / 503:

| HTTP | When |
|------|------|
| 400  | neither `reset_url` nor `temp_password` (only `null` / `""` count as absent) |
| 400  | both `reset_url` and `temp_password` |
| 400  | `reset_url` not a string, or not an acceptable URL (rule above) |
| 400  | `kind` other than `reset` / `invite` (`null` / `""` mean `reset`) |
| 400  | `kind: "invite"` with `temp_password` (an invitation needs a link) |
| 400  | `temp_password` not a string |

The field checks run before the email / rate-limit checks, so a malformed
request never uses up a recipient's hourly allowance. Every field rejection
logs `RESET_EMAIL_REJECTED reason=<the error text>` with the tenant id — the
reason is a fixed string, never the submitted value.

The client calls this in two places and always sends `kind`: `reset` from
the anonymous "Reset password" request, `invite` from the administrator's
invitation. An anonymous "Reset password" request calls it from a background
thread, so the sign-in page answers the same way
whether or not the address has an account; on failure the client logs
`PASSWORD_RESET_EMAIL_FAILED` with the exception type and discards the
token. The administrator's "Invite user" action calls it while the request
waits (bounded by `BRAIN_DRAFT_TIMEOUT`) and reports `mail_sent: false` on
failure.

**Deployment order.** A brain that still requires `temp_password` answers 400
to the `reset_url` shape, so reset and invitation mails fail (logged, nothing
else breaks) until the brain accepts `reset_url`. Deploy the brain change
first.

---

## `POST /v1/schema_autofill`

Combined autofill — file description + per-column descriptions in one LLM call,
**verbatim port** of global's `_build_combined_autofill_prompt` +
`_parse_combined_response` (`backend/routes/schema.py` L813-881). One call per
file; the client runs them in parallel and merges the results into `meta.json`.

The client builds the per-file context locally (derived from global's
`_prepare_file_context`): filename, dtypes, unique-value hints, language hint,
user notes. `unique_hints` carries, per column, EITHER its distinct values
(only when there are at most `SCHEMA_AUTOFILL_UNIQUE_THRESHOLD` of them — a
categorical vocabulary) OR exactly one computed string starting with
`[profile: ` that holds no real value: dtype, distinct count, null share, and
per type a character mask of a typical value (letters → `A`, digits → `9`)
with min/avg/max length, uniqueness and structural prefixes (text), two-
significant-figure min/max/mean with integer/non-negative/increasing flags
(numeric), year-month bounds and granularity (datetime), or the true share
(boolean). No sampled row value of a high-cardinality column crosses the
boundary.

### Request

```jsonc
{
  "sid": "...",
  "user_email": "alice@acme.com",
  "fname": "sales.csv",
  "cols_to_fill": ["name", "department", "salary"],
  "unique_hints": {
    "department": ["Engineering", "Sales", "Support"],
    "salary": ["[profile: dtype=int64, distinct=412, nulls=0.0%, min=41000, max=210000, mean=97000, integers=yes, non_negative=yes, increasing=no]"]
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
