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

**Answer language** (`/v1/plan`, `/v1/describe`, `/v1/summarize`): the
answer text is written in the language the question is written in, whatever
the tenant's languages and whatever language the welcome or earlier turns
are in. The language is recognized ONCE per question, by the query
classifier call `/v1/plan` already makes (no extra model call) — by
language, not alphabet (an Uzbek question in Latin or Cyrillic letters is
Uzbek). The tenant's `main_language` / `secondary_languages` (brain-side
tenant config, admin panel) are given to that classifier only, as the
organization's likely languages for a very short or ambiguous question;
they never appear in a prompt that writes the answer. The recognized
language travels to the later calls with no new request field (changed
2026-10-04):

- `/v1/plan` returns it as `context_decision.answer_language` (an English
  language name such as `"English"`, `"Uzbek"`; `null` when it could not be
  recognized) and writes `# answer_language: <Name>` as the first line of
  every Python / plot code block it returns — exactly one such line per
  block; a line copied from earlier code in the history is replaced by the
  current question's language. Text replies (ANSWER / CLARIFICATION /
  MISSING_DATA) carry no such line; the plan prompt itself names the
  language.
- `/v1/describe` reads the language from that comment line in `code`.
- `/v1/summarize` reads it from `context_decision.answer_language` (the
  client passes the plan's `context_decision` back verbatim).
- `/v1/retry` keeps the language of `failed_code` on the corrected code.

When no language is carried (older client flow, classifier failure, a
question with no words) the prompt asks the model to answer in the
question's language, and when that cannot be determined in `main_language`,
else English. Chart text (titles, axis labels, legends) stays English, as
before. `/v1/chat_metadata` (`welcome_language`) and `/v1/title` (`lang`)
are unchanged.

---

## Field-shape mapping back to the B2C code

| Brain field          | Same field in B2C | Built by |
|---------------------|-------------------|----------|
| `schema_text`       | return value of `_schema_text(schema_docs, dfs, common_fields)`. VALUE POLICY (enterprise, stricter than B2C): a text column with at most 20 distinct values lists them, each cut to 40 characters; any other text column carries its distinct COUNT only (`object (N unique)`); a numeric or date column carries its dtype only, never a value. The stored per-column `technical_description` is filtered to the same rule as a string when the text is built (a `sample:` list written by an earlier release is dropped, a categorical list of more than 20 values becomes its count). A column named in the client setting `SCHEMA_VALUE_DENY_COLUMNS` (comma-separated, case-insensitive) carries its dtype only, a technical line of dtype + fill count, and no `Value Descriptions` entry. Unchanged by the narrative release: the category lists of text columns with ≤ 20 distinct values stay | client (`schema_builder.schema_text`, `_safe_technical_description`) |
| `df_names`          | `list(dfs.keys())` | client |
| `df_columns`        | `{name: list(df.columns)}` (only sent on retry, for column self-correction) | client |
| `history_rows`      | the `history_rows` arg to `generate_pandas_code` / `summarize_answer`. Each row carries **only** `role`, `content`, and (when present) `code` — the client's `brain_client._sanitize_history_rows` strips every other persisted field (`image_base64`, `chart_data`, `table`, `usage`, `full_table_key`, `ts`, …) before the POST, so raw data values in locally persisted records never cross the boundary (Article II). From its narrative release the client writes the `content` of a templated AI turn as a `[Findings: …]` marker line plus the unfilled template (see `/v1/plan` "History") | client (`local_store.get_history`, sanitized in `brain_client.plan/retry/summarize`) |
| `common_fields`     | the `common_fields` arg to `generate_pandas_code` | client |
| `error_msg`         | `exec_out["error"]` from `safe_execute`. From its narrative release the client replaces every quoted literal that is not an input column name, a df name or text the failed code itself contains (a whole identifier token, or text inside one of the code's string literals) with `'<value>'`, and the `NearZeroChartError` text carries no number. With `narrative_v1` the client also sends one request that is not an execution error: `narrative_invalid: <nested|unclosed|mismatched|first_sentence>`, the narrative-only retry (see `/v1/retry`), with `failed_code` the code that already ran and `use_pro: true`; when a slot made the first sentence show `n/a` it is `narrative_invalid: first_sentence slot=<name> cause=<missing|none|nan>` (the slot name is a template identifier, never a value). The client waits at most 45 s for that reply and uses only its `narrative` | client |
| `failed_code`       | the failed code block | client |
| `preview` (summarize) | the `_safe_preview` value: a `str`/`int`/`float`/`bool`, a flat object whose keys and values are all such scalars (or null values), or `null` — lists, tables and objects holding anything else become `null`. Capped in transport: a string at 500 characters + `…[truncated]`; an object at its first 20 keys, each string key/value cut the same way, plus `"_truncated_keys": <number dropped>` when more existed. From its narrative release the client always sends `null` (see `/v1/summarize`) | client (`run_chat_local._safe_preview`, capped by `brain_client._compact_preview_for_transport`) |
| `qa_pairs` (report) | the `findings_for_llm` list the B2C `_generate_report_structure` already builds | client |
| `dataset_profile` (plan/retry, **optional**) | enterprise-only (no B2C equivalent): `{df_key: profile}` of computed FACTS per loaded table — rows, duplicate count, per-column dtype/nunique/null rates/min-max/constant/all-unique flags, truncated top-value hints, detected grain, deterministic warnings. Aggregate metadata only, never row data (Article II — same class as the cardinality hints in `technical_description`). `top_values` (≤ 5 pairs, each value cut to 40 characters) only for a column with `nunique` ≤ 20; a column on `SCHEMA_VALUE_DENY_COLUMNS` carries only `dtype`, `nunique`, `null_count`, `null_pct`, `constant`, `all_unique`, and its constant-column warning reads `<col> is constant` without the value. The transport compaction applies both rules to profiles stored by an earlier release too. Absent field ⇒ pre-profile behavior everywhere. From its narrative release the client stops sending `min`, `max` and `top_values` for every column and writes every constant-column warning as `<col> is constant` (transport only; stored profiles are not rewritten); the brain renders a profile without them | client (`dataset_profile.compute_profile`, stored as sidecar JSON, compacted by `brain_client._compact_profiles_for_transport`) |
| `data_caveat` (describe, **optional**) | enterprise-only: the client's DETERMINISTIC post-execution finding about the result it just rendered — `{kind: constant_metric\|identical_series\|constant_table\|matrix_readability, facts[], grain[], catalog}`. Aggregate findings + column names + truncated constant-value hints only (Article II, same class as `dataset_profile`). Makes the flat-result explanation mandatory instead of prompt-dependent; absent field ⇒ byte-identical describe prompt. From its narrative release the client stops sending the constant-value hint in `facts`; the brain renders the caveat without it | client (`result_backstop.inspect_outputs`, compacted by `brain_client._compact_caveat_for_transport`) |
| `live_tables` (plan/retry, **optional**) | enterprise-only (no B2C equivalent): `[{name, dialect, row_cap, filtered}]` — one row per LIVE database table the chat holds (`name` = the df key, `dialect` = the connector registry key such as `postgresql` / `mysql` / `mariadb` / `mssql` / `oracle` / `clickhouse`, `row_cap` = the effective row cap, `filtered` = true when an administrator row filter applies, in which case the client fetches the rows itself and expects no SQL). Metadata only; sent only when the chat holds a live table | client (`run_chat_local._live_tables_for_brain` from `ChatDataStore.schema_docs`) |
| `sql` (plan/retry response, **optional**; echoed on a retry request) | enterprise-only: `{<df key>: "<one read-only SELECT>"}` written by the brain for the live tables the code references. The client validates it (read-only guard + a per-table allowlist), runs it in the web application, and places the result under the df key; a missing entry ⇒ the client's default capped fetch. On a retry request (every retry, the regeneration ones included) the client sends what RAN this turn: the brain's own text for a SELECT that reached the database (also when it failed there), `null` for a default read, for a SELECT ignored on a `filtered` table and for a SELECT the client's guard refused; keys never fetched are absent — never a result | brain (planner); echoed by client (`brain_client.retry(sql=)`) |
| `sql_error` (retry, **optional**) | enterprise-only: `{table, dialect, class, guard, message}` describing a failed live SELECT, or (once, on the next retry) a SELECT ignored on a `filtered` table — `class` ∈ `syntax` / `unknown_column` / `unknown_table` / `timeout` / `permission` / `guard` / `other`; `guard` true when the client's own SQL gate refused the text, and then `message` is the gate's sentence (naming at most an identifier), otherwise `message` is null. Never the driver message, a literal or a cell value (Article II) | client (`brain_client.live_sql_error`) |
| `capabilities` (plan/retry, **optional**) | enterprise-only (no B2C equivalent): the feature names the client supports — `narrative_v1` (answer text as a template), `diagnostics_v1` (the diagnostic playbook; honoured only with `narrative_v1`) and `two_phase_v1` (one second code pass; honoured only with `diagnostics_v1`, sent by no client until the two-phase flow ships). Unknown names, non-string items and a value that is not a list are ignored. A constant list, no data. Absent (or no known name) ⇒ the earlier prompts and response | client (`brain_client.plan/retry`, from the `BRAIN_CAPABILITIES` setting) |
| `findings` (plan, **optional**) | enterprise-only: `{key: value}` — the deterministic checks the phase-1 code computed, sent only on the second pass of a two-phase diagnostic. At most 24 keys matching `^[a-z][a-z0-9_]{0,39}$`; each value a boolean or a string of 1–64 characters without control characters. On the client a string is an exact input column name (never a column the code created) or one fixed code (`coverage_change`, `coverage_stable`, `partial_period`, `metric_definition_overlap`, `mixed_units`, `concentrated_in_one_dim`, `spread_evenly`, `weak_signal`, `dominant_feature`, `anomaly_confirmed`, `no_anomaly`, `data_gap`, `outlier_driven`, `seasonal_pattern`), and the key `needs_phase2` is reserved for a boolean. The brain checks the shape only (the 400 rules are under `/v1/plan`) | client (`narrative.validate_findings`) |
| `data_caveat.findings` (describe, **optional**) | enterprise-only: the same FINDINGS shape as `findings`, sent with or without `kind` when describe runs for a turn whose code set FINDINGS. Cleaned like `findings`; a bad entry is dropped, never a 400 | client (`narrative.validate_findings`, carried by `brain_client._compact_caveat_for_transport`) |
| `narrative` / `captions` (plan/retry response, **optional**) | enterprise-only: the answer template (≤ 4000 characters) and the per-visual caption templates `{"<n>": template}` (≤ 600 characters each) the planner wrote in ```` ```narrative ```` / ```` ```caption:<n> ```` fences. Returned only with `narrative_v1`, next to `PYTHON` / `PLOT_CODE` | brain (planner); filled by the client (`narrative.render`) |
| `diagnostic` (plan response, **optional**) | enterprise-only: `true` on a phase-1 diagnostic or advisory code plan when `two_phase_v1` is on; never `false` | brain (classifier task_type) |

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
      "profile_version": 2, "rows": 12480, "duplicate_row_count": 0,
      "computed_at": "2026-08-23T09:14:02+00:00", "sampled": false,
      "grain": { "columns": ["order_id"], "kind": "single", "text": "one row per (order_id)" },
      "warnings": ["Quantity is constant"],  // the value is never sent (narrative release on)
      "columns": { "salary": { "dtype": "int64", "nunique": 240, "null_count": 0,
                                "null_pct": 0.0,  // no min / max / top_values (narrative release on)
                                "constant": false, "all_unique": false } }
    }
  },
  "live_tables": [                         // OPTIONAL — present ONLY when the chat holds
    { "name": "transactions",              //   a LIVE database table (queried at question time)
      "dialect": "postgresql",             // connector registry key: postgresql | mysql | mariadb | mssql | oracle | clickhouse
      "row_cap": 200000,                   // the effective row cap of the SELECT's result
      "filtered": false }                  // true ⇒ an administrator row filter applies: the client
  ],                                       //   pre-fetches the rows itself and expects NO SQL for it
  "capabilities": ["narrative_v1", "diagnostics_v1"],  // OPTIONAL — absent ⇒ the earlier prompts and response
  "findings": {                            // OPTIONAL — phase 2 only (the second and last pass)
    "anomaly": "anomaly_confirmed",        //   a fixed code
    "coverage_dim": "CURRENCY",            //   an input column name
    "needs_phase2": true                   //   a boolean
  }
}
```

The brain injects the profile LAZILY into the planner prompt: a one-line
micro-summary per table (`PROFILE <name>: rows=N; warnings: ...`) ALWAYS,
and the full block (capped ~15 lines/table) only when the query analyzer
classifies the question as a data task (aggregation / visualization /
statistical or complex analysis, and diagnostic / advisory, which the
classifier offers only with `diagnostics_v1`; classifier failure counts as
a data task).
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

**Capabilities.** `capabilities` is an optional list of the features the
client supports (field table above). The brain keeps the known names,
drops `diagnostics_v1` without `narrative_v1` and `two_phase_v1` without
`diagnostics_v1`, and ignores everything else. A request with no known
name gets exactly the earlier planner prompt, classifier prompt and
response, except for four changes that apply to every client:

- the fence stripping under "Narrative and captions" below (a reply
  without such a fence comes back untouched);
- a non-empty `findings` adds the PHASE 2 block ("Findings (phase 2)"
  below);
- the skill name `diagnostics` is always removed from the skill list
  ("Diagnostic questions" below);
- a null `/v1/summarize` preview gets the reading guide ("Null preview"
  under `/v1/summarize`).

Design: `docs/DIAGNOSTICS_SPEC.md` in the client repository.

**Diagnostic questions.** Only with `diagnostics_v1`, the classifier
prompt offers two more task types and one more skill name:
`diagnostic` (why something changed, what drives a metric, what problems
the data shows) and `advisory` (what to do, grounded in the loaded data),
and the skill `diagnostics`. Both task types count as data tasks for the
dataset-profile gate. When the classifier picks one of them, or lists
`diagnostics` in `skills_needed`, the planner's system instruction gains
the diagnostics playbook (`skills/core/diagnostics.yaml`, after the other
skills): a coverage check, confirm the anomaly, then decompose the change
or attribute the level, as `###NEXT_PLOT###` blocks. Without
`two_phase_v1` the playbook does every step in one pass and never asks
for a second one. The name `diagnostics` (any case, any padding) is
always removed from `skills_needed` before the generic skill loader runs,
capability or not; a classifier reply that listed only `diagnostics` then
gets the keyword skill selection.

**Findings (phase 2).** `findings` carries the checks the phase-1 code
computed (field table above). The brain checks its shape before the
planner runs:

- not an object (`null` counts as absent) → HTTP 400
  `{"detail": "Invalid 'findings': findings must be an object"}`;
- an object with a value that is not a boolean or a string → HTTP 400
  `{"detail": "Invalid 'findings': findings values must be booleans or strings"}`.

Both messages are fixed and never name a key or a value. The rejection is
logged as `BRAIN_PLAN_REJECTED reason=findings_shape` with the tenant id
and no content. Every other violation (a bad key, an empty or longer
string, a control character, keys past the 24th) silently drops that
entry. Non-empty cleaned findings alone, with or without capabilities,
add a PHASE 2 block to the planner prompt: the findings as sorted
`- key: true|false|value` lines, and the instruction to write only step 3
of the playbook with its own narrative and never a third pass. The block
sits after the dataset profile and BEFORE the Common Fields section. It
is never part of the `CODE_PROMPT` log snippet; the brain logs
`PHASE2_PLAN findings_n=<count>`.

**History.** A client from its narrative release sends an AI history row
for a templated turn whose `content` is a marker line
`[Findings: key=value; …]` (only when the turn set FINDINGS; keys sorted,
at most 400 characters) followed by the unfilled template. Slots are
never filled in history. With `narrative_v1` the planner prompt tells the
model that a slot stands for a value the user saw and the model never
sees. The brain parses neither line; it trims history rows as before (800
characters per row, 150 in the classifier prompt), which is why the
marker comes first. Turns without a template keep their describe or
summarize text.

### Response

```jsonc
{
  "raw_text": "```python\\nresult = df.groupby('department')['salary'].mean()...\\n```",
  "kind": "PYTHON",                       // PYTHON | PLOT_CODE | NO_CODE | CLARIFICATION | ANSWER | MISSING_DATA
  "code": "result = df.groupby('department')['salary'].mean()",
  "usage": { "input_tokens": 1234, "output_tokens": 56, "total_tokens": 1290 },
  "context_decision": { "complexity": "simple", "complexity_score": 5, "skills_needed": ["analytics_libraries"], "is_greeting": false, "answer_language": "English", ... },
  "model_used": "gemini-2.5-pro",
  "sql": {                                // OPTIONAL — one read-only SELECT per live df key the
    "transactions": "SELECT region, SUM(amount) AS total FROM public.transactions GROUP BY region"
  },                                      //   code references; unknown fields are ignored
  "narrative": "{{total_loans:count}} loans were granted in {{latest_period:period}}. The bars show the count per month.",
                                          // OPTIONAL — narrative_v1 only, next to PYTHON / PLOT_CODE
  "captions": { "1": "Count per month; {{anomaly_period:period}} is highlighted." },
                                          // OPTIONAL — keys are strings "1".."12"
  "diagnostic": true                      // OPTIONAL — phase-1 plan with two_phase_v1 only; never false
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

**Narrative and captions.** With `narrative_v1` the brain asks for the
answer text as a template. The system instruction gains the narrative
rules (`planner_narrative_system`, right after `planner_system`), and the
user prompt gains a format line (`planner_narrative_suffix`, after
`planner_human_suffix` and before the live block). Every code reply then
ends with one ```` ```narrative ```` fence after its last code block,
optionally followed by ```` ```caption:<n> ```` fences, one per visual. `n`
is the 1-based position of the code block in the reply as split by
`###NEXT_PLOT###`; every `python` / `plot_code` block counts, and
`1 ≤ n ≤ 12`. The code sets the dict `VALUES` for every slot and `FINDINGS`
for its checks; the client fills the template after execution, so no
result value reaches the brain.

How the brain takes the fences out of the reply:

- **Every client.** The fences are cut out of every plan and retry reply,
  with or without capabilities, before the SQL, language-tag and code
  parsers run, so a template is normally never read as code or shown
  raw, and is never logged outside the opt-in `LLM_DEBUG_LOG`.
  Without `narrative_v1` nothing is returned, and the brain logs
  `NARRATIVE_FENCE_STRIPPED_NO_CAP n=<count>`. A reply without such a
  fence comes back unchanged.
- **First wins.** The first `narrative` fence and the first `caption:<n>`
  per index win; later ones are removed and dropped.
- **Caps.** A narrative over 4000 characters or a caption over 600 is
  dropped, not truncated. An empty one is dropped too.
- **Malformed.** A fence whose info string starts with `narrative` or
  `caption` (any case) but is not exactly `narrative` or `caption:<1–12>`
  (for example `caption:0`, `caption:13`, `captions:1`, `narrative text`)
  is removed and never returned.
- **Cut-off reply.** An UNTERMINATED fence after the last complete fence,
  on its own line (only spaces or tabs before it), whose info string starts
  with `narrative` or `caption` is cut from there to the end of the reply,
  as malformed. A prose mention of ```` ```narrative ```` inside a line is
  left alone.
- **When returned.** `narrative` / `captions` come back only with
  `narrative_v1`, and only next to `PYTHON` / `PLOT_CODE`; next to `ANSWER` /
  `CLARIFICATION` / `MISSING_DATA` / `NO_CODE` both are dropped. Caption
  keys are strings (`"1"`), and `n` must not exceed the number of code
  blocks in the reply (a single code block counts as one); a caption past
  that is dropped. A field with nothing in it is absent.

**Template validation (`narrative_v1`).** Every narrative and caption the
brain is about to return — on `/v1/plan`, `/v1/retry` and the
narrative-only retry — is first checked against the client's template
grammar (DIAGNOSTICS_SPEC A2.1, the same token regex), without rendering:

- **Structure.** A section opened inside an open section (`nested`), a
  close without an open or with a different name (`mismatched`), and a
  section never closed (`unclosed`) make the template invalid.
- **Section names.** Every `{{#name}}` / `{{^name}}` must name a pdc_diag
  finding code or key (the codes `planner_narrative_system` lists and the
  FINDINGS names of `diagnostics.yaml` `playbook.keys`) or a FINDINGS-shaped
  key (`^[a-z][a-z0-9_]{0,39}$`) quoted as a string literal in the reply's
  code (the client's FINDINGS key rule). A name `<prefix>_<pdc_diag name>`,
  where the reply's code passes that prefix as `prefix="<prefix>_"` (1–10
  lowercase letters or digits starting with a letter), is rewritten to the
  bare name in its open and close tags before the template is returned
  (`{{#ovl_metric_definition_overlap}}` → `{{#metric_definition_overlap}}`;
  slots keep their prefix). A prefix the code never passes (`no_`, `not_`,
  `has_`) is never rewritten — it could flip the condition. Any other name
  is an `unknown section <name>`.
- **One correction call** (`/v1/plan` and the code retry; never on the
  narrative-only retry). When a template is invalid, the brain calls the
  model once more with the same system instruction, model and settings and
  the same prompt plus a fixed correction block: the problem classes with
  the section names involved (which section is opened inside which, which
  close tag closes what, which section is never closed, which name is
  unknown), the one-level rule, and the first reply's code to repeat
  unchanged. The narrative and captions come from that reply; `code`,
  `kind`, `raw_text` and `sql` stay those of the first reply. A template
  the second reply lacks or breaks keeps the first reply's version when
  that one was valid. A template invalid in both replies is dropped —
  without `narrative` the client describes the result as before. `usage`
  sums both calls.
- **Time bound.** The call shares the client's request timeout (180 s): it
  runs only while at least 30 s of a 170 s budget, counted from the start
  of the request, are left, and its per-attempt timeout is capped at the
  time left (the REST layer's retries of a retryable HTTP status can still
  add to it).
  Skipped for lack of time, it counts as failed: the invalid templates are
  dropped and the valid ones kept.
- Without `narrative_v1`, or next to a text kind, nothing is checked and no
  call is made.

**Template language** (rendered by the client). The template is written
in the answer language:

- a slot is `{{name}}` or `{{name:kind}}`: `name` = letters, digits and
  underscores, at most 40 characters; `kind` is `count`, `float`, `pct`
  (the value is a fraction: 0.125 is shown as 12.5%), `ratio`, `amount`,
  `text` or `period`. A slot no block sets, or whose value is null,
  renders `n/a`, and the client removes every sentence holding one
  before display (a caption emptied that way is described instead; a
  narrative emptied that way is not used). So a sentence should depend
  only on values its own section guarantees. The FIRST sentence is never
  dropped: when it would show `n/a`, or when the template's sections do not
  nest correctly, the client asks once for a new template (the
  narrative-only retry under `/v1/retry`). A slot in the first sentence
  must therefore never be None: never a value computed from a subset that
  may be empty, and a headline VALUES assignment carries a defined default
  (`planner_narrative_system`). When the template is dropped after that
  retry, the client shows one local sentence with the main figure
  (`Result: …`) when VALUES has a key `result`, `total` or `*_total`;
- `{{#flag}}…{{/flag}}` is shown when FINDINGS has `flag` true, holds a
  column name or code under `flag`, or `flag` is a code some FINDINGS value
  equals; `{{^flag}}…{{/flag}}` is shown otherwise. One level only. On
  the client a name `<prefix>_<code>` whose `<code>` is a FINDINGS code or
  key counts as that code when the block's code passes that prefix as
  `prefix="<prefix>_"` (the brain's template-validation rule, applied to
  a template the brain did not rewrite); a prefix the code never passes
  is never resolved, and any other name the FINDINGS do not hold is
  false. After the template, the client appends one fixed
  sentence in the answer language for each data-quality finding (mixed
  units, coverage change, thin period or data gap, Unknown-dominated
  dimension, metric overlap, latest period in the past) that no shown
  section already states, and formats Uzbek and Russian numbers in their
  own locale (no-break-space thousands, decimal comma, mln/mlrd/trln or
  млн/млрд/трлн);
- plain markdown only (bold, `- ` bullets, blank lines), never three
  backticks. The model never writes a value itself: every number, name and
  date is a slot.

**`diagnostic: true`.** Set only when all of these hold:
`two_phase_v1` and `diagnostics_v1` both survive cleaning, the classifier's
`analysis.task_type` is `diagnostic` or `advisory`, the kind is `PYTHON` /
`PLOT_CODE`, and the request carried no `findings` (none left after
cleaning). It is never sent as `false`, and never on `/v1/retry`. No
client sends `two_phase_v1` yet, so no plan carries the flag today. With
it, the client may run exactly one more `/v1/plan`: the same question, the
same history plus one `ai` row with the phase-1 code (code only, no
values), and `findings`. That second plan never carries `diagnostic`, and
there is no third.

**Model tier (`narrative_v1`).** The classifier's complexity picks the
tier (light / simple / complex) as before. With `narrative_v1` the plan
runs on the complex tier — its model, temperature, thinking and timeout
from the tenant's effective settings — when the classifier's
`analysis.task_type` is `diagnostic` or `advisory` (reason `narr_diag`), or
is `aggregation`, `visualization`, `statistical_analysis` or
`complex_analysis` and a column is period-like (reason `narr_period`): a
word of its name starts with `date`, `month`, `period` or `year` (any case,
snake_case or camelCase: `REPORT_DATE`, `fiscal_year`, `reportDate`, not
`updated_by`) or its dtype is a datetime, read from the `Columns:` /
`Column Types:` lines of
`schema_text` and the `dataset_profile` columns. The prompt does not change
(it still follows the classified complexity); `model_used` names the model
that answered. Without the capability the routing is unchanged.

**Logs.** `BRAIN_PLAN` adds `caps` (the cleaned names, comma-joined;
absent when none) and `findings_n` (the cleaned count). `BRAIN_PLAN_DONE`
adds `narrative` (a boolean, only with `narrative_v1`), `captions_n` and
`diagnostic`. `MODEL_SELECTION` adds `tier_reason` (`narr_diag`,
`narr_period` or `classifier`), only with `narrative_v1`.
`NARRATIVE_EXTRACT` and `NARRATIVE` carry lengths and counts
(duplicates, too long, empty, malformed, out of range, dropped kind).
Template validation logs `NARRATIVE_SECTIONS_REWRITTEN n=` (with
`retry=1` for the correction reply),
`NARRATIVE_INVALID templates= nested= unclosed= mismatched= unknown=`,
`NARRATIVE_CORRECTED`, `NARRATIVE_INVALID_TWICE` (the same counts plus
`dropped_narrative` / `dropped_captions` and `new_invalid`, a template only
the second reply has and breaks) and `NARRATIVE_CORRECTION_FAILED
error=<Type>` (`TimeoutError` when the budget was spent) — classes and
counts, never a section name.
Outside the opt-in `LLM_DEBUG_LOG`, no log line carries template text or a
findings value.

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
  "error_msg": "KeyError: 'Department'",  // with narrative_v1, "narrative_invalid: <problem>"
                                          //   asks for a narrative-only retry (below)
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
  },                                      //   (an identifier at most), otherwise null
  "capabilities": ["narrative_v1"]        // OPTIONAL — same list and cleaning as /v1/plan
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

**Narrative.** With `narrative_v1`, the retry system instruction gains
`retry_narrative_system`, after the live retry block: keep every `VALUES`
and `FINDINGS` key the failed code sets, with the same meaning; end the
reply with a new ```` ```narrative ```` block only when the corrected code
changes what those values mean, optionally with ```` ```caption:<n> ````
blocks numbered within this reply. The fences are cut out exactly as on
`/v1/plan`, for every client, before the SQL map, the code parsers and the
`RETRY_CODE_RAW` log line. The response may carry `narrative` / `captions`
under the same rules (template validation included), never `diagnostic`.
On the client, a single-block
answer's retry `narrative` replaces the plan's (absent ⇒ the plan's is
kept); in a multi-block answer a per-block retry's `narrative` is ignored
and its `caption:1` replaces that block's caption (the narrative-only
retry below is the exception in both flows). `BRAIN_RETRY` logs
`caps`; `BRAIN_RETRY_DONE` logs `narrative` and `captions_n`.

**Narrative-only retry (`narrative_v1`).** A request whose `error_msg`
starts with `narrative_invalid:` (any case, leading whitespace allowed)
asks for a new template for code that already ran, not for corrected code.
The text after the colon is the kind of problem the client found:
exactly `nested`, `unclosed` or `mismatched` (the template's sections do
not parse) or `first_sentence` (the first sentence would show `n/a`),
optionally followed by the slot that caused it:
`first_sentence slot=<name> cause=<missing|none|nan>` (`name` in the slot
grammar, at most 40 characters; the key was absent, its value None, or a
non-finite number); the
brain also accepts `unknown section <name>`. Any other text is passed on
with its whitespace collapsed, at most 200 characters. `failed_code` is
the code that ran, unchanged: the whole answer, its blocks joined by
`###NEXT_PLOT###` for a multi-block answer.

- **Prompt.** No code is regenerated. The system instruction is
  `retry_narrative_only_system` followed by `planner_narrative_system`; the
  prompt carries the answer-language block (from the code's
  `# answer_language:` line), the question, the schema and columns, the
  micro profile lines, the problem with one fixed sentence explaining its
  kind (for `first_sentence`: use only slots the code assigns at the top
  level of the block that computes the answer; with a slot cause instead:
  "The slot <name> was missing / None / NaN at execution; rewrite the
  first sentence so it uses only values the code sets unconditionally and
  that cannot be None. The code is not run again, so the new first
  sentence must use only the VALUES the existing code already sets."), and
  the code, marked as kept unchanged. It runs on the complex tier (model,
  temperature, thinking), like a narrative plan; `use_pro` (the client
  sends `true`), `use_search`, `history_rows`, `live_tables`, `sql` and
  `sql_error` are ignored.
- **Response.** `code` is `failed_code` byte-for-byte; `kind` is
  `PLOT_CODE` for several blocks or chart code, else `PYTHON`; `raw_text`
  is the code re-fenced per block; `narrative` / `captions` are new
  (captions numbered by the code's blocks) and validated like any template
  above, but with NO correction call: this path makes exactly one model
  call (the client time-boxes it at 45 s). An invalid narrative or caption
  is dropped, a valid one kept. Never `sql`, never `diagnostic`. An invalid
  narrative, a failed model call or any other
  failure on this path comes back without `narrative`, `code` still
  unchanged — the client then answers without a narrative. An empty or
  blank `failed_code` gives `kind: "NO_CODE"`, empty `code` and no model
  call.
  The client uses only the reply's `narrative`, never its code or
  captions.
- Without `narrative_v1` the request is an ordinary code retry, and so it
  is on a brain from before this contract — deploy the brain first.
- **Logs.** `BRAIN_RETRY` logs `err` as `narrative_invalid: <class word>`
  (`nested` / `unclosed` / `mismatched` / `first_sentence` / `unknown` /
  `other`) instead of
  the raw `error_msg`, so a section name never reaches the log;
  `NARRATIVE_ONLY_RETRY` logs the model and the same class word (plus
  `cause=` for a slot cause — never the slot name),
  `NARRATIVE_RETRY_INVALID templates= nested= unclosed= mismatched=
  unknown=` an invalid reply template, and
  `NARRATIVE_ONLY_RETRY_FAILED error=<Type>` a failure on this path;
  `BRAIN_RETRY_DONE` adds `narrative_only=True`.

### Response

```jsonc
{
  "raw_text": "...",
  "kind": "PYTHON",                       // PYTHON | PLOT_CODE | NO_CODE | CLARIFICATION | ANSWER | MISSING_DATA
  "code": "df.groupby('department')['salary'].mean()",
  "usage": { ... },
  "model_used": "gemini-2.5-pro",
  "sql": { ... },                         // OPTIONAL — a new SELECT per live df key, same
                                          //   rules as /v1/plan; a key it names is re-fetched
  "narrative": "...",                     // OPTIONAL — narrative_v1 only; same rules as /v1/plan
  "captions": { "1": "..." }              // OPTIONAL — never "diagnostic" on a retry
}
```

A narrative-only retry (`error_msg` "narrative_invalid: …") answers:

```jsonc
{
  "raw_text": "```python\n...\n```",      // the failed code re-fenced per block
  "kind": "PYTHON",                       // PYTHON | PLOT_CODE (NO_CODE for an empty failed_code)
  "code": "<failed_code, byte-for-byte>",
  "usage": { ... },                       // the one model call
  "model_used": "gemini-2.5-pro",
  "narrative": "...",                     // OPTIONAL — absent ⇒ the client describes the result
  "captions": { "1": "..." }              // OPTIONAL
}
```

**Data boundary (Article II).** Across the whole live path only the brain's
own SQL text (in the response, echoed on a retry) and an error CLASS cross;
the rows a SELECT returns never leave the client — they reach the analysis
sandbox as an ordinary input frame and the brain sees, as always, only the
code and the schema text (and, from a client before its narrative release,
the capped summarize preview; the current client sends `preview: null`).

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
    "facts": ["Quantity is the same for every group in the chart"],
    "grain": ["cl prod link: one row per (client_id, product_id)"],
    "catalog": true,                     // catalog/link grain, not measured quantities
    "findings": {                        // OPTIONAL — accepted with or without `kind`
      "units": "mixed_units",
      "coverage_dim": "CURRENCY"
    }
  }
}
```

`data_caveat` carries aggregate findings and column NAMES only — never a
value; the same Article II class as `dataset_profile` (guarded client-side by
`_compact_caveat_for_transport`: known keys, known `kind`, ≤6 items × ≤200
chars, and any fact ending in a `(= …)` value hint dropped). A client before
its narrative release appended a 40-char-truncated constant-value hint to a
fact (`… (= 1)`); the current client writes none. The brain renders it as a MANDATORY DATA NOTE
block and requires the description to START with the explanation, prefixed by
the literal marker `[[DATA_NOTE]]`. The client strips that marker before
display and, if it is absent (or the call failed), prepends its own localized
sentence — so the explanation reaches the user even when the model omits it.
Unknown/malformed caveats are ignored, never fatal.

**Findings.** `data_caveat.findings` carries the FINDINGS the executed
code set, in the `/v1/plan` `findings` shape, cleaned the same way (a bad
entry is dropped; describe never answers 400 for it). It is accepted with
or without `kind`: the MANDATORY DATA NOTE block still needs a known
`kind`, the findings do not. Non-empty cleaned findings add a separate
block after the data note, `DIAGNOSTIC FINDINGS (names and codes only — no
values):`, with sorted `- key: value` lines. It lets the model say in one
short clause what a code means for reading the chart, and forbids any
number, name or category value. It is not subject to `[[DATA_NOTE]]`.
Absent, empty or fully dropped findings ⇒ a byte-identical describe
prompt. The brain logs `DESCRIBE_FINDINGS_APPLIED n=<count>`. The client
sends findings only when describe runs: an answer without a template, or
a visual whose caption is missing.

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

Mirror of `agent.summarize_answer`. Used only for results without a table
or image. The client is responsible for filtering `preview`. A client from
its narrative release always sends `null` and shows the value itself. An
older client's `_safe_preview` guard passes a scalar or a flat object of
scalars and turns anything else into `null`, and its `brain_client` then caps
it — a string at 500 characters (+ `…[truncated]`), an object at its first 20
keys plus `"_truncated_keys": <number dropped>`.

### Request

```jsonc
{
  "sid": "...",
  "question": "what is the highest salary?",
  "schema_text": "...",
  "history_rows": [ ... ],
  "preview": null,                        // narrative-release client: always null; an older client: a scalar
                                          //   or flat object of scalars, capped (500 chars / 20 keys), or null
  "context_decision": { "complexity": "simple", ... },
  "user_email": "..."
}
```

### Response

```jsonc
{ "text": "This is the highest salary across all employees in the dataset.", "usage": { ... } }
// with "preview": null the reply names no value; the client shows it locally
```

**Null preview.** When `preview` is `null` or absent (or cannot be
serialized), the prompt has no preview section; instead a READING GUIDE
block follows the question. It says that no result value is available,
forbids stating, estimating or guessing any number, total, percentage,
name or category, and asks for 1–2 sentences on what the result shows and
how to read it; the client displays the value. The brain logs
`SUMMARIZE_READING_GUIDE`. A non-null preview gets a byte-identical
prompt. **Behaviour change (2026-10-23):** this applies to every client,
so an older client's null preview (for example a value its guard turned
into `null`) now gets the reading guide too. A client from its narrative
release never calls summarize for a templated answer, and always sends
`"preview": null` when it falls back to summarize.

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
  "existing_names": ["Ledger Pulse", "Card Insights"],   // optional
  "user_email": "..."
}
```

`existing_names` (optional, added 2026-10-04): the titles of the user's
active chats — a list of at most 50 strings, each at most 60 characters.
A value that is not a list counts as absent; entries that are not strings,
are empty after trimming or are longer than 60 characters are dropped, and
only the first 50 valid entries are used. When the list is non-empty, the
returned `name` differs from every entry (compared trimmed and
case-insensitive): the name prompt lists the names in use, and if the model
still returns a taken name, the name sub-call is retried once naming the
taken name. If the second name is taken too, it is returned as is — the
client's `_2`, `_3` suffix stays the last resort. Field absent or empty: the
name prompt and the response are byte-identical to before. The titles are
logged as a count only (`existing_names_n`), never as text. Naming style
(exactly 2 words, English, proper case), the welcome and questions
sub-calls, the response shape and the time bounds are unchanged.

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
  ],
  "welcome_source": "model"
}
```

The welcome message and questions are the same prompts and sanitizers
global uses — output is byte-compatible.

`welcome_message` is never empty. The welcome, questions and chat-name calls
(and `/v1/title`) make at most 2 attempts each: a model that rejects
`thinkingConfig.thinkingBudget = 0` with 400 is retried without it (and is
then remembered in-process, so later calls skip it), a MAX_TOKENS cut is
retried with a larger budget, a 429 / 5xx after a 1 s backoff, and an empty
or unparseable answer is retried once. When the welcome is still empty, the brain returns a fixed welcome in
the resolved language (English, Georgian, Russian or Uzbek; any other
language gets English) — greeting, "your data analyst for this dataset",
analysis / visualizations / reports, and the localized final line "For
example you can ask:" — and `suggested_questions` may be `[]`.

`welcome_source` (optional, added 2026-10-04): `"model"` when the model wrote
the welcome, `"fallback"` when it is the fixed one. Clients that ignore it
are unaffected.

Time bound: each attempt is capped at 20 s wall clock, the three sub-calls
run in parallel, and the whole request is cut at 45 s (fallback welcome, no
questions, `CHAT_METADATA_TIMEOUT` logged). Every request logs one `BRAIN_CHAT_METADATA_RESULT` line
(`name_ok`, `welcome_ok`, `questions_ok`, `welcome_source`, `elapsed`); each
failed or empty attempt logs `CHAT_METADATA_{WELCOME|QUESTIONS|NAME}_{FAILED|EMPTY}`
with the model, HTTP status, finishReason and token counts.

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

`answer` carries at most the first 300 characters of the answer text (cut by
`brain_client.title`). For a templated answer, a client from its narrative
release sends a slot-free view of the template (each slot replaced by
`[slot name]`), never the text it rendered.

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
carries a single-use token — valid for 30 minutes for a reset, 30 days for an
invitation — of which the client stores only a SHA-256 hash. Its address is the client's configured
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

The reset mail states "This link is valid for 30 minutes and can be used only
once."; the invitation mail states "This link is valid for 30 days and can be
used only once." (matching the client's link lifetimes). Neither contains a
password.

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
boundary. A column on the client's `SCHEMA_VALUE_DENY_COLUMNS` list always
takes the profile form, reduced to
`[profile: dtype=…, distinct=…, nulls=…%]` — no values even when it has few
distinct ones, and no mask, prefix, length or magnitude.

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
template locally. For a templated answer, a client from its narrative
release sends a slot-free view of the narrative or caption template as
`answer_text` (each slot replaced by `[slot name]`), never the text it
rendered.

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
      "table_columns": ["department", "salary"],     // column NAMES only — from the narrative release, INPUT
                                                     //   column names only (a pivot/crosstab label is removed; may be [])
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
