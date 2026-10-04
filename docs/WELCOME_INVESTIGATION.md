# The missing chat welcome (investigation, 2026-10-04)

**Finding: brain-side.** The client asks for the welcome correctly and stores
what it receives. Since 2026-10-03 05:27 UTC the brain has answered
`/v1/chat_metadata` with HTTP 200 and an **empty** `welcome_message` and
`suggested_questions`, for every tenant. The client then shows its fallback
line "Hello! I'm ready to help you analyze your data. Ask me anything!". No
client change fixes it, and none was made. The fix belongs in `PDC_Brain`
(below).

## Symptom

A new chat on `brb.powerdatachat.com` (tenant BRBtech, welcome language
Uzbek) and on `client.powerdatachat.com` opened with the fallback line and no
suggested questions. On brb it was seen for chats built from registered
database tables. As the evidence below shows, the kind of data source plays
no part: every chat created after 2026-10-03 05:27 UTC is affected.

## What the client does

1. `routes/upload.py` `generate_chatdata` builds the arguments from the
   session meta and calls `brain_client.chat_metadata` synchronously before it
   answers (both call sites: with and without a chat name):
   - `files_info`: the uploaded files' keys plus every database entry's
     display name (`db_entries_from_meta`).
   - `file_descriptions` (`schema_builder.file_descriptions_dict`).
   - `context` and `lang_instruction` (`schema_builder.build_context_for_questions`).
   - `columns_to_human` (`schema_builder.columns_to_human_map`).

   All four read `meta["files"]`, which holds the database entries written by
   `/session/db_tables`, so a chat built only from tables sends the same kind
   of context as a file chat.
2. A transport error, timeout or 4xx/5xx is logged `CHAT_METADATA_BRAIN_ERROR`
   (any other exception `CHAT_METADATA_ERROR`) and the chat is created without
   a welcome. Otherwise the answer is stored as received (`meta.json` plus
   `welcome.txt` / `suggested_questions.json`) and `CHAT_CREATED` logs
   `questions=<n> welcome_chars=<n>`.
3. `GET /api/chat/{chat_id}/welcome` serves the stored message and questions.
   `static/dashboard.js` `openChat` shows the fallback line only when that
   message is empty.

## Evidence

### Hosted client logs (Cloud Run, `pdcclient-brb` and `pdcclient-demo`)

There is no `CHAT_METADATA_BRAIN_ERROR`, no `CHAT_METADATA_ERROR` and no
`BrainTimeoutError` on either service. Every `/v1/chat_metadata` call
answered `BRAIN_OK` within 1.1–3.4 s. Until 2026-10-02 every chat got a
welcome; on 2026-10-04 both services got an empty one:

| Time (UTC) | Service | Chat | Brain answer | Stored |
|---|---|---|---|---|
| 2026-10-02 21:49:08 | brb | `c_d76eb154e63f9fb1` (2 DB tables) | `BRAIN_OK` 1.37 s | `questions=3 welcome_chars=407` |
| 2026-10-04 07:40:59 | brb | `c_7d2198e25b10bac5` "Ledger Metrics" (5 DB tables) | `BRAIN_OK` 1.37 s | `questions=0 welcome_chars=0` |
| 2026-10-04 07:45:18 | demo | `c_72d23add2ab594a7` "Member Demographics" (1 source) | `BRAIN_OK` 1.34 s | `questions=0 welcome_chars=0` |

The 2026-10-04 chats did receive a generated title, which is a separate brain
call (see below).

### Hosted brain log (`pdcbrain`, revision `pdcbrain-00023-vow` throughout)

For both 2026-10-04 requests (tenant `t_a316a43142021fbf` = brb, tenant
`t_5e078443c5a5b109` = demo):

```
WARNING:root:[WELCOME] Failed: Client error '400 Bad Request' for url
  'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent'
WARNING:root:[QUESTIONS] Failed: Client error '400 Bad Request' for url
  'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent'
"POST /v1/chat_metadata HTTP/1.1" 200 OK
```

The 2026-10-02 request, on the same brain revision, shows no failure line.
Between the two, the brain's admin panel saved the global LLM settings:

```
2026-10-03 05:27:54 [sid=admin] [fields=LLM_AGENT_MODEL,…,LLM_LIGHT_MODEL,LLM_LIGHT_TEMPERATURE,
  LLM_LIGHT_USE_THINKING,…]   POST /admin/api/settings 200
```

From then on the light tier is `gemini-3.5-flash-lite`.

### The model refuses the request the brain sends

The same prompt sent to Google's API directly (from the local brain
container, with its own key):

| Model | `thinkingConfig: {thinkingBudget: 0}` | No `thinkingConfig` |
|---|---|---|
| `gemini-3.5-flash-lite` | **400** "Request contains an invalid argument." | 200 |
| `gemini-2.5-flash` | 200 | 200 |

### Local reproduction (local stack, light model `gemini-2.5-flash`)

A chat from two registered `brb_demo` tables ("branches", "app users") and a
chat from `tools/fixtures/sample_sales.csv`, created over HTTP on the local
stack:

- DB chat `c_3a9d1df1145220c2`: `BRAIN_OK` 1.38 s, `questions=3
  welcome_chars=384`; `/welcome` served the message and three questions.
- CSV chat `c_74a3374a4f2b71e8`: `questions=3 welcome_chars=356`.

The payload sent for the DB chat (rebuilt from its stored meta with the same
four helpers):

- `files_info`: `["app users", "branches"]`.
- `file_descriptions`: both table descriptions.
- `context`: both tables with every column and its description.
- `lang_instruction`: `"English"`.
- `columns_to_human`: every column.

No cell value is in it. The brain answered with a 384-character welcome and
three questions.

The local tenant has no `welcome_language`, so the answer was in English. On
the hosted brain the BRB tenant's `welcome_language` was saved on 2026-10-03
05:59 UTC, and the handler applies it before the request's `lang_instruction`
(`routes/llm.py` `chat_metadata_api`). That is not the cause: the model call
fails before any language matters.

## Classification

| Hypothesis | Verdict |
|---|---|
| (a) The client never calls the brain or discards the answer | No. Every chat creation logs `BRAIN_OK`, and a non-empty answer is stored and served (`tests/test_chat_welcome_metadata.py`). |
| (b) The brain call fails or times out | **Yes, inside the brain.** Its model call fails with 400, the brain hides that and answers 200 with empty fields. |
| (c) The client sends empty context for database-table chats | No. `files_info`, `file_descriptions`, `context` and `columns_to_human` are all non-empty for a table-only chat (local payload above; pinned by `tests/test_chat_welcome_metadata.py`). |
| (d) The brain ignores `welcome_language` | No. It is applied first; the failure happens before language matters. |

## Root cause in `PDC_Brain` (not changed here)

- **`chat_metadata.py` `_call_gemini_async`.** With `disable_thinking=True` it
  always sends `generationConfig.thinkingConfig = {"thinkingBudget": 0}`.
  `gemini-3.5-flash-lite` rejects that field value with 400. The brain's own
  `brain_agent._resolve_thinking_budget` already notes that a budget of 0 is
  rejected by some models; this path does not use it.
- **`_generate_welcome_message_async` and `_generate_all_questions_async`**
  pass `disable_thinking=True` (`max_tokens=600`), so both fail on that
  model. `_generate_chat_name_async` sends no thinking config, which is why
  the chats still got a title. `generate_title` (conversation titles) also
  passes `disable_thinking=True` and fails the same way.
- **Errors are swallowed.** Each generator catches every exception, logs it
  with `logging.warning` on the root logger (not the `brain` logger, so not
  in the brain's own log format), and returns `""` / `[]`.
  `generate_all_parallel` and `routes/llm.py` `chat_metadata_api` then
  answer 200 with the empty fields. The client cannot tell an empty answer
  from a failure.

### What the brain needs (recommendation for the brain owner)

1. Stop sending `thinkingBudget: 0` to models that reject it. For example,
   omit `thinkingConfig` and rely on `maxOutputTokens`, or build the config
   through the shared tier logic (`_eff` / `_resolve_thinking_budget`), so a
   light-model change in the admin panel cannot break these calls.
2. Log the failures through the `brain` logger with the model and the
   response body's error message (Article IV).
3. **Immediate workaround with no code change:** set the light model back to
   one that accepts `thinkingBudget: 0` (e.g. `gemini-2.5-flash`) in the
   brain admin panel's global settings.

After either fix, chats created from then on get their welcome. Chats
created while the brain answered empty keep the fallback line; nothing
regenerates their welcome.
