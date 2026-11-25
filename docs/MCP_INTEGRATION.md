# MCP integration

Other chatbots inside a customer's network can connect to the client through
MCP (Model Context Protocol), ask a question in an existing PowerDataChat chat
and receive the answer the web chat gives: text, tables, charts as images and
a link to the conversation.

Everything is in PDC_Client. The brain is not changed and sees an MCP question
like a web question (one difference: no title call, §7).

The feature is off until an administrator creates the first token.

Code: `routes/mcp.py`, `mcp_tokens.py`, `mcp_conversations.py`, `mcp_jobs.py`,
`mcp_answer.py`, `chat_turn.py`, `tools/mcp_test_client.py`, the "MCP access"
section of the admin page.

---

## 1. Decisions

| # | Decision |
|---|---|
| D1 | Client side only. No change in PDC_Brain. |
| D2 | Standard MCP, no vendor-specific code, no REST API in v1. |
| D3 | Existing chats only. No chat creation and no data update through MCP. |
| D4 | Access by tokens the local admin creates, in mode `per_user` or `shared_account`. |
| D5 | `per_user`, unknown email: an address in an approved sign-in domain gets an account, any other is refused. |
| D6 | One conversation per user, per chat, per calendar day, visible in the normal web page. |
| D7 | Charts are returned as PNG images, plus a link to the same conversation, where the chart is interactive. |
| D8 | The answer is saved by a separate path ("option B"). The browser handler is not restructured (§9). |
| D9 | A test client is in the repository (§10). |

Decided in the build, not in the decisions above: a question is refused while
today's conversation of the chat is being answered; the request body must
arrive within 30 s; a chart render has 60 s, and three renders stuck past that
stop further rendering; an answer that could not be saved is delivered with a
sentence saying so; the chat access is checked again every 5 s while
`get_answer` waits; a token name holds no invisible characters; a shared
account needs a creation date.

---

## 2. Operator guide

1. Set `PUBLIC_BASE_URL` in `client.env` (the http(s) address users type).
   Without it the endpoint address is not shown and answers carry no link.
2. Use HTTPS: over plain HTTP the token and the answers can be read on the
   network (`CUSTOMER_INSTALL.md` §3).
3. Sign in to the admin page and open **MCP access**. The bootstrap `ladmin`
   and every user with the **Local admin** permission can use it; power users
   do not see it.
4. Enter a name (1–60 characters, unique among the active tokens), choose the
   mode, for **Shared account** enter the account's email, and click **Create
   token**.
5. Copy the token now. It is shown once, with the endpoint address
   (`<PUBLIC_BASE_URL>/mcp`), and cannot be shown again.
6. Give both to the chatbot's operator (§5 says what to send). To end access
   click **Revoke**: the chatbot loses access at once and the token cannot be
   restored. Revoked tokens stay in the list.

The section is in English, Georgian and Russian. It follows the language the
administrator's browser stored on `/lab`; the rest of the admin page is
English.

If a reverse proxy stands in front: it must pass the `Authorization`,
`X-PDC-User-Email`, `MCP-Protocol-Version`, `Mcp-Method` and `Mcp-Name`
headers, and its read timeout must be longer than `MCP_ANSWER_WAIT_SECONDS`.
The same holds for the chatbot's own request timeout.

---

## 3. The endpoint: `POST /mcp`

JSON-RPC 2.0 over the MCP Streamable HTTP transport, plain JSON answers, on the
application's own port. Written by hand: the official `mcp` SDK is not used,
because it conflicts with the pinned dependencies. No session, no cookie, no
state between requests.

### 3.1 Protocol revisions

Exactly two revisions are served, chosen per request. A client that speaks
only an older revision cannot connect: `initialize` answers 2025-11-25, and a
version header naming another one is refused.

| | 2025-11-25 ("legacy") | 2026-07-28 ("modern") |
|---|---|---|
| Methods | `initialize`, `ping`, `tools/list`, `tools/call` | `server/discover`, `tools/list`, `tools/call` |
| Version travels in | the `initialize` answer; `MCP-Protocol-Version` header optional | the `MCP-Protocol-Version` header and `params._meta` (`io.modelcontextprotocol/protocolVersion`) |
| Unknown method | `-32601`, HTTP 200 | `-32601`, HTTP 404 |
| Every result carries | nothing extra | `resultType: "complete"` and `_meta` with the server info |

A request is legacy when its method is `initialize`. Otherwise it is modern
when the method is `server/discover`, the header `MCP-Protocol-Version` is
`2026-07-28`, or `params._meta` holds the protocol version key. Everything else
is legacy.

Legacy: `initialize` always answers `2025-11-25`, whatever version was
requested, and ignores the version header. It is not required before the other
methods; there is no `Mcp-Session-Id`. On any other method a version header
that is present and not `2025-11-25` gets HTTP 400, `-32022`.

Modern: these checks run in this order, each failure an HTTP 400 JSON-RPC
error: version header missing (`-32020`); version missing in `params._meta`
(`-32602`); header differs from the body (`-32020`); version not `2026-07-28`
(`-32022`, with `supported` and `requested`); client capabilities in
`params._meta` not an object (`-32602`); `Mcp-Method` header missing or not
the body's method (`-32020`); for `tools/call`, `Mcp-Name` header missing or
not `params.name` (`-32020`). `server/discover` and `tools/list` also carry
`ttlMs: 300000` and `cacheScope: "private"`.

### 3.2 Transport rules

- One JSON object per POST; an array or anything else is HTTP 400, `-32600`,
  `id` null. Unparseable JSON: HTTP 400, `-32700`.
- A message without an `id` (a notification) gets HTTP 202 with an empty body
  and does nothing, whatever its method. `id` must be a string or an integer.
- Unknown tool or arguments that are not an object: `-32602`, HTTP 200.
- A tool problem (empty question, unknown job, refused user) is a normal
  result with `isError: true`. An unexpected failure is `-32603` "Internal
  error", HTTP 200; the log gets the exception type only.
- `Content-Type` must be `application/json` (parameters allowed), else 415
  from `JsonContentTypeGate`. Another HTTP method gets 405; a `POST`, `PUT`,
  `PATCH` or `DELETE` without the JSON content type gets 415 first.

### 3.3 Order of the checks and HTTP statuses

Origin, token, body, the token again (it may be revoked while the body is
sent), JSON-RPC, and for `tools/call` the acting user. `initialize`,
`server/discover`, `ping` and `tools/list` need a valid token only. The
backend-network refusal of the sandbox subnet and the other middleware run
before all of this (`docs/CLIENT_ENDPOINTS.md`).

| Status | When |
|---|---|
| 200 | an answer, JSON-RPC errors included unless listed below |
| 202 | a notification |
| 400 | a malformed JSON-RPC message, a failed modern or version-header check, or a client that goes away while sending the body |
| 401 | missing, unknown, malformed or revoked token, or more than one `Authorization` header: the same answer, `WWW-Authenticate: Bearer`. Also when the token is revoked while the body is sent or while `get_answer` waits |
| 403 | an `Origin` that is not allowed; a peer inside the sandbox subnet; a browser session that still has to change its password |
| 404 | a modern request with an unknown method |
| 405 | `GET /mcp` and other methods (see 3.2) |
| 408 | the body did not arrive within 30 seconds |
| 413 | the body is larger than 1 MiB; a declared larger length is refused unread |
| 415 | the content type is not JSON |
| 429 | a failed token from an address that is over its limit, with `Retry-After` |

**Origin.** A request with an `Origin` header passes only when there is exactly
one and it equals the origin of `PUBLIC_BASE_URL`; no `Origin` is accepted
(server-side chatbots send none). This keeps a browser page of another site out.

**Failed-token limiter.** In memory, per `request.client.host`, at most 10,000
addresses: after 20 failed tokens within 600 s the next failed one gets 429
instead of 401 (an attempt over the limit is not counted). A valid token always
passes, whatever the address's counter, so a shared proxy address cannot lock a
working chatbot out. Refusals are logged once per address, reason and window.

---

## 4. Tools

`tools/list` describes the three tools and tells the chatbot's model the order:
`list_chats`, then `ask`, then `get_answer`, again while it says running.

### `list_chats`

No arguments. Result: `{"chats": [{"chat_id", "title", "tables"}]}` — the chats
in the acting user's own sidebar that they may open now, in the sidebar's
order, the title the user sees, and the names of the tables or files. Names
only, no data values. A chat whose share was removed is not listed. The names
are the chat's own list, so tables the user's role does not cover are named
too (§12).

### `ask` (`chat_id`, `question`)

Checks, each done by the existing function, unchanged:

| Check | Function | When |
|---|---|---|
| Chat access (owner or share recipient, id shape, existence) | `routes.chat._require_chat`, through `mcp_jobs.may_open` | in `ask`, again in the job |
| Empty question | `routes/mcp._start_question` | in `ask` |
| Auto Analytics busy | `routes.chat._auto_analysis_busy_response` | in `ask`, again in `chat_turn.prepare_turn` |
| Running question of this user in this chat, global cap | `mcp_jobs.start` | in `ask` |
| Today's conversation is being answered on the web | `routes.chat._is_generating` | in the job |
| Empty dataset, missing tables | `routes.chat._empty_dataset_response` | in the job |
| Role gate on database tables, snapshot and live | `routes.chat._drop_uncovered_db_keys` | in the job |
| Nothing left after the gate | `routes.chat._live_denied_text` / `_denied_events`; the sentence is the saved answer | in the job |
| Second role gate at fetch time | `run_chat_local._ensure_live`, inside the unchanged pipeline | in the job |

Result at once: `{"job_id", "state": "running"}`. A refusal found in `ask` is a
tool result with `isError`; one found in the job ends it as `failed` with the
text the web shows.

### `get_answer` (`job_id`)

Waits on the server until the job is finished, at most
`MCP_ANSWER_WAIT_SECONDS`, then answers the state: `running`, `done` or
`failed` (`isError`). The first content item is always a text item holding the
JSON `{"job_id", "state"}`. A running answer also carries it as
`structuredContent`. A done or failed answer carries no `structuredContent`, so
a client that shows only that cannot lose the answer; the items of §8 follow
the state item. A job is read only with the same token and the same acting user that started it; anything
else, an expired or an unknown job, is the same "Job not found." While it
waits, and before every answer, the token and the chat access are checked
again: a revoked token gets 401, a removed share gets "Job not found.". A data
role removed after `ask` does not withhold a finished answer.

The job id is only in the answer of `ask`. If that answer is lost it cannot be
recovered: there is no list of jobs, and a new `ask` in the chat is refused
while the first runs. The answer is saved in the conversation.

---

## 5. Tokens, and who the caller acts as

Send `Authorization: Bearer <token>`. A token is `pdcmcp_` plus 43 characters.
Only its sha256 is stored, in `DATA_ROOT/mcp_tokens.json` (all keys below; an
absent file means no token, so the feature is off; an unreadable file refuses
every call and no token write replaces it). Verification compares every record
with `hmac.compare_digest`. `last_used_at` is written at most once a minute.
A name: 1–60 characters, no control or invisible characters.

```
{"version": 1, "tokens": [{"id", "name", "mode": "per_user" | "shared_account",
  "account", "account_created_at",   // shared_account only
  "token_hash", "created_at", "created_by", "last_used_at",
  "revoked_at", "revoked_by"}]}
```

### 5.1 per_user

The chatbot's backend sends the user's address in the HTTP header
`X-PDC-User-Email` with every tool call. It is a header and not a tool
argument, because an argument is filled by the chatbot's language model, and an
end user could talk the model into sending someone else's address.

- Missing, blank, malformed or repeated header: refused with a sentence.
- Existing account (the address is lower-cased): acts as it. The bootstrap
  admin is refused with the same sentence as an address outside the approved
  domains, so the answer never says whether an account exists.
- No account, address in an approved sign-in domain (`signin_domains.py`):
  `AuthStore.create_account` creates it without a password (audit row
  `mcp.account_created`, actor `mcp:<token name>`), then acts as it.
- No account, other address: refused, nothing created.

Checks that are **not** applied, with the reason: "no password" (an account
created here has none), "Microsoft sign-in enforced" (it only blocks the local
password) and a pending forced password change (it guards only the web
password).

**Trust model.** Whoever holds a `per_user` token can act as any existing
account, and as any address in the approved sign-in domains. "Any existing
account" includes users with the **Local admin** permission; only the bootstrap
account is refused. They act as chat users: the admin routes are not reachable
through MCP. `list_chats` shows the user's chat titles and the names of the
tables and files in them, tables outside the user's role included. Nothing but
the header says who the user is, and the mailbox is not
verified. Treat the token like a password and give it only to a chatbot backend
that authenticates its users itself.

**A removed user comes back.** Removing a user on the **Users** page deletes
the account. While a `per_user` token is active and the address is in an
approved domain, the next call for that address (a `get_answer` poll included)
creates the account again, empty. To keep the person out, take the domain off
the list, or stop the chatbot from sending the address.

**Offboarding.** Access through a `per_user` token does not depend on a web
session or a password. **End sessions**, a password change and disabling the
person in Entra do not end it. Remove the account (in an approved domain it is
created again, see above) or revoke the token.

**First web sign-in of an account MCP created.** It exists without a password,
so the sign-in form refuses a typed password. The user gets in by

- **Reset password** on the sign-in page (needs `PUBLIC_BASE_URL`), or
- an administrator's **Invite user** (a 30-day link), or
- Microsoft sign-in, when it is enabled (it binds the existing account).

### 5.2 shared_account

The token is bound to one existing account, stored with the account's creation
date; the header is ignored. Refused: the bootstrap admin, an account without a
creation date, an account that was removed, or the same address removed and
created again (the dates differ).

All end users of such a token share one conversation per chat per day and see
each other's questions as history. Two of them asking in the same chat at once:
the second gets "A question is already running for this user in this chat. Wait
for its answer, then try again." Links to the conversation open only for
someone signed in as that account.

---

## 6. Conversation and history

- One conversation per acting user, per chat, per calendar day of the server
  clock in **UTC**. At UTC+4 the day turns at 04:00 local time.
- The first question of the day creates it, titled `MCP YYYY-MM-DD`, registered
  in the acting user's own index, with the chat's welcome row like a new web
  conversation. Later questions that day continue it, with the earlier rows as
  history. A deleted one is replaced by a new one. Titles are never generated,
  so the brain gets no `/v1/title` call.
- Which conversation is today's: `users/<email>/mcp_conversations.json`,
  `{"version": 1, "chats": {<chat_id>: {"day", "conv_id"}}}`, the newest day
  per chat. It is written last, and only while the account exists. The stored
  conversation is used only when it is in the user's own index for this chat
  and its file exists there.
- Question and answer are saved in the format the web page reads: text, tables
  and the interactive chart show on the web.
- A stop pressed on the web ends the question with the stopped answer (the job
  is `done`). There is no MCP cancel tool.
- When today's conversation is being answered on the web at that moment, the job
  ends as `failed` with "Another question is being answered in today's
  conversation of this chat. Please try again when it has finished." The other
  way round is not prevented: the web accepts a question in today's MCP
  conversation while an MCP question runs there, and the rows can interleave.

---

## 7. Jobs

`mcp_jobs.py` keeps questions in memory; each runs on its own daemon thread.
Correct only with the one worker process the image runs.

| Limit | Value |
|---|---|
| Running questions per (user, chat) | 1 |
| Running questions in all | 8 |
| Jobs kept | 256 (a running one is never dropped) |
| Content kept | 200 MB; over it the caller holding the most loses its oldest finished job |
| A finished job is kept | 30 minutes |

- The reservation is taken inside `ask` and freed in the step that ends the
  job. One user can fill all 8 running slots with 8 chats (§12).
- A restart forgets the jobs. A question that was running is lost; a finished
  answer is in the conversation.
- Revoking a token hides its jobs; a turn already running goes on and is saved.
- A finished job that was not saved still answers, with "This answer could not
  be saved in PowerDataChat." in place of the link.

The only brain-visible difference from a web question: no `/v1/title` call.
The `plot_generated` activity event per chart is posted by
`mcp_jobs._activity_poster` with the web's payload.

The sandbox runs one job at a time, so MCP questions and web questions queue
with each other. Measured on the local stack: a chart question 75–105 s (almost
all of it planning by the language model), a table question about 5 s.

---

## 8. Answer content

Built from the saved record (`mcp_answer.py`) with the display text the web
shows, never the unfilled templates, in the order of `static/dashboard.js`
`renderAiRecord`. After the state item (§4) a finished answer is a list of MCP
content items:

- **Text.**
- **Tables** as Markdown inside a text item: at most `MCP_TABLE_MAX_ROWS` rows
  (never more than 50, a saved answer holds no more). When rows are cut:
  "Showing N of M rows." and a pointer to the link. Identifiers are plain
  digits, other numbers as on the web.
- **Charts** as PNG image items, at most 6 per answer; a text item says how
  many were left out. A Plotly chart is rendered by the report renderer at
  2400×1600 (about 130–230 KB each, a third more as base64); a matplotlib
  chart is its stored PNG. Rendering runs on the web container, one at a time,
  at most 60 s each; while three timed-out renders are still alive no further
  chart is rendered. A chart that cannot be rendered is replaced by "A chart could not be included
  here as an image.".
- **The last item** says where the conversation is: "Open in PowerDataChat:
  `<PUBLIC_BASE_URL>/c/<conv_id>`". With `PUBLIC_BASE_URL` empty (or not
  http(s)) no link is built, because it is never derived from the request; it
  says instead `Open PowerDataChat, chat "<title>", conversation "MCP
  YYYY-MM-DD".` The link opens when signed in as the acting user.

A failed answer is the same message the web shows, never a stack trace.

**The answer holds data values** (text, table rows and chart images) and can
quote text from the customer's data. Chat titles and file or table names from `list_chats` are untrusted text
too. A chatbot must show all of it as untrusted Markdown: no HTML, and no
instructions taken from it.

---

## 9. Saving: option B now, option A later

### Why B

The answer is saved by two pieces of code: the browser handler
`routes/chat.py` `chat_stream` (its closure `_worker`) and `chat_turn.py`. The
handler is tied to its request, its session and its event stream, and it was
not to be changed for this feature (D8). So `chat_turn.py` mirrors it, takes
plain arguments and is written so that the handler can call it later.

### Exactly which logic exists twice

| Logic | Browser path | MCP path |
|---|---|---|
| Pre-run order: busy, load, empty, schema, role gate, profiles, conversation, history, human row | `routes/chat.py` `chat_stream`, from `local_store.ChatDataStore(chat_id)` to the human row | `chat_turn.prepare_turn` |
| The event loop: table persistence order, in-flight code registration, live fields, chart / caption / narrative accumulation, stop check, stop synthesis | `routes/chat.py` `chat_stream` → `_worker` | `chat_turn.run_prepared_turn` |
| The five error classes to the user's text (three texts are inline literals in `_worker`) | `_worker` | `run_prepared_turn` |
| Building and appending the record, title trigger, clean-up | `_worker` | `run_prepared_turn` |
| New conversation: id, index row, welcome row | `chat_stream` | `mcp_conversations._create`, through `mcp_conversations.make_factory` |
| The `plot_generated` activity event per chart | `chat_stream` `_sse_generator` | `mcp_jobs._activity_poster` |

Everything else is a call into the existing function, read off the module at
call time (`_chat.<name>`): `_build_ai_history_record`,
`_build_stopped_record`, `_persist_full_table`, `_event_codes`, `_event_sql`,
`_event_table_fields`, `_inflight_add` / `_inflight_discard`,
`_drop_uncovered_db_keys`, `_live_denied_text`, `_denied_events`,
`_mark_generating` / `_unmark_generating`, `_is_cancelled` / `_clear_cancel`,
`_title_due`, `_start_title_generation`, `_answer_for_title`,
`_narr_event_view`. `edit_regenerate` is a third consumer of the same events,
already different in details; the MCP path mirrors the stream.

Display is mirrored too, but not saving: `mcp_answer._parts` follows
`renderAiRecord`, and `mcp_answer._value_text` the number format of
`dashboard.js` `_fmtCellValue`.

Deliberate differences: a failing consumer (`on_event`) is logged and ignored;
`title="never"` in MCP turns; `CHAT_THREAD_ERROR` and `CHAT_PERSIST_ERROR` log
the exception type only (the web adds 200 characters of its text); the new
conversation is refused when the account has been removed (the web ignores
that).

### The rule

**When saving changes in `chat_stream`, change the MCP module too. The drift
test fails until you do.**

### How the drift test works

`tests/test_chat_turn_drift.py` feeds the same scripted pipeline events through
the real `chat_stream` route and through `chat_turn`, and compares the saved
rows (key order included), the durable table records, the planner's arguments
(minus `sid` and `on_progress`), the events handed to the consumer, the
in-flight registry and the "generating" mark after every event, the title
trigger, the log markers and the row the web history route serves. It
normalises only `ts` and the random table keys. `CHAT_STREAM_PIN` is the hash
of the code of `chat_stream` (comments, blank lines and layout do not count),
so an edit that no scenario covers is noticed too. The new-conversation steps
and `plot_generated` are covered by `tests/test_mcp_conversations.py` and
`tests/test_mcp_tools.py`.

When it fails:

1. Read which assertion failed. A comparison: one side saves differently. The
   pin: somebody edited `chat_stream`.
2. Mirror the change in `chat_turn.py` (or in the other one of the two, when
   that is the side that is wrong).
3. Add a scenario to the test for what changed, when none covers it.
4. For the pin: put the new hash, printed by the failing test, in
   `CHAT_STREAM_PIN`. Never update it without step 2.

### Plan for option A

1. Check that the drift test is green and that `CHAT_STREAM_PIN` matches.
2. In `routes/chat.py`, add `import chat_turn` (both sides read the other's
   attributes at call time, so the import cycle is harmless).
3. In `chat_stream`, keep access, empty question, `_may_use_conversation` and
   the SSE generator. Replace the block from `store` to the human row with
   `chat_turn.prepare_turn(email, chat_id, question, conv_id=conv_id,
   new_conversation=…)` run through `_EXEC`, where the factory does the web
   steps (id, title `question[:80]`, index row, welcome row). A `TurnRefusal`
   becomes `JSONResponse(refusal.body, status_code=refusal.status_code)`.
4. Replace the body of `_worker` with `chat_turn.run_prepared_turn(turn,
   on_event=lambda tag, payload: loop.call_soon_threadsafe(queue.put_nowait,
   (tag, payload)), title="auto")` and put `None` on the queue in a `finally`.
   The consumer (SSE, live UX, `plot_generated`) stays.
5. Check the differences: the log line `CHAT_STREAM_REQ` becomes
   `CHAT_TURN_REQ`; a conversation that cannot be created is a refusal.
6. Run the drift test, the stream tests (`test_c2_stream_route.py`,
   `test_narrative_records.py`, …), the full suite and `/smoke-test`.
7. Remove the copies left in `chat_stream`, then `CHAT_STREAM_PIN`. The drift
   test becomes a test of one path called from two places: keep its scenarios.
8. Update §9, `CLAUDE.md`, the docstring of `chat_turn.py` and this rule.
   `edit_regenerate` is a separate decision.

---

## 10. Test client

`tools/mcp_test_client.py`, Python standard library only, not in the image.
The default address is the local stack, `http://127.0.0.1:8091/mcp`.

```
export PDC_MCP_TOKEN=<token>         # PowerShell: $env:PDC_MCP_TOKEN = "<token>"
python tools/mcp_test_client.py --email user@company.com chats
python tools/mcp_test_client.py --email user@company.com ask c_123 "Sales by month?" --out charts
python tools/mcp_test_client.py --email user@company.com selftest
python tools/mcp_test_client.py --url https://pdc.company.com/mcp selftest   # a shared-account token
```

`--url`, `--token`, `--email`, `--protocol` and `--timeout` stand before the
command; the command's own options (`--out` and `--max-wait` of `ask` and
`selftest`, `--chat` and `--question` of `selftest`) stand after it. A per-user
token needs `--email`. `--protocol 2026-07-28` uses the modern revision
(default `2025-11-25`). `--timeout` (default 100 s) bounds one request; keep it
above `MCP_ANSWER_WAIT_SECONDS`. `selftest` runs `initialize` (or
`server/discover`), `tools/list`, `list_chats` and one question and prints `[PASS]` or `[FAIL]` per step, ending at the first failure; `--chat`
chooses the chat, `--question` the question. `ask` and `selftest` save the
charts as PNG files in `--out`, else in a new folder `mcp_answer_<time>`, and
print the link. `--max-wait` (default 900 s) bounds the wait. `--help` has the
rest. The exit code is 0 for success, 1 for a refusal, a failure or a question
still running, 2 when the server could not be used.

---

## 11. Claude Code as a client

Checked on 2026-11-25 with Claude Code 2.1.332
(<https://code.claude.com/docs/en/mcp>).

```
claude mcp add --transport http pdc http://127.0.0.1:8091/mcp \
  --header "Authorization: Bearer <token>" \
  --header "X-PDC-User-Email: <email>"
claude mcp list
claude mcp get pdc
claude mcp remove pdc
```

`--header` may be repeated. Leave the email header out with a shared-account
token. In a project, `.mcp.json` can read the token from the environment:

```json
{"mcpServers": {"pdc": {"type": "http", "url": "http://127.0.0.1:8091/mcp",
  "headers": {"Authorization": "Bearer ${PDC_MCP_TOKEN}",
              "X-PDC-User-Email": "user@company.com"}}}}
```

- It connects with revision 2026-07-28 (`server/discover`, `tools/list`);
  `claude mcp list` and `claude mcp get pdc` show `✔ Connected`.
- With a configured `Authorization` header, a 401 is reported as a failed
  connection and no OAuth sign-in starts. An unknown token shows "Server
  rejected the configured Authorization header (HTTP 401)".
- `MAX_MCP_OUTPUT_TOKENS` (default 25,000) bounds a tool result, results with
  images included; raise it when an answer has several charts.
- A text-only result longer than 50,000 characters is saved to a file and
  replaced by its path.

Any other MCP client works the same way: the address, the `Authorization`
header, and the user header for a `per_user` token.

---

## 12. Security notes

- A `per_user` token is as powerful as described in §5.1.
- **Answers (text with data values, table rows, chart images) go to the
  connecting chatbot.** This does not touch the brain boundary (the brain gets what a web question sends it).
  If the customer's chatbot uses an external language model, the values leave
  the customer's network. That is the customer's decision.
- A caller sees only what its acting user may see: chats, conversations and
  jobs are checked with the web's functions, jobs are bound to token and user,
  table permissions by role hold as on the web, and there is no MCP path into
  another user's conversation.
- Tokens are compared in constant time, only their hash is stored, and no log
  line holds a token, an answer, a table value or an image. `CHAT_THREAD_ERROR`
  and `CHAT_PERSIST_ERROR` of an MCP turn carry the exception type only; the
  web's lines carry 200 characters of its text, which can quote a label or a
  value (a deliberate difference, §9). The log gets one
  `MCP_CALL` line per tool call (token name, tool, outcome, duration, chat id,
  job id, acting user), plus `MCP_JOB_END` when a question ends. Other MCP
  requests and refused tokens have no `MCP_CALL` line (a refused token has
  `MCP_REFUSED`, an identity refusal `MCP_CALLER_REFUSED`). The first 120
  characters of the question are logged as on the web (`CHAT_TURN_REQ`).
- An administrator's token actions are audited (`mcp_token.create`,
  `mcp_token.revoke`, never the token), and account creation as
  `mcp.account_created`.
- `mcp_tokens.py` is not in the sandbox image (a test keeps it out). It is not
  on the sandbox import denylist (`sandbox_guard.py`), which is unchanged.

### Known limits and follow-ups

- A running job has no deadline of its own; a question that never ends keeps
  its slot until the process ends.
- One user can fill all 8 running slots, and one token with many users can too.
  There is no cap per user across chats.
- The length of a question is bounded only by the 1 MiB body.
- `list_chats` names tables the user's role does not cover (names only).
- A question containing U+2028 loses its row when the history is read
  (`local_store` splits lines there; it happens on the web too).
- `list_chats` fails with "Internal error" when the user's sidebar file
  (`active_chats.jsonl`) holds a row that is not an object
  (`local_store.AuthStore.list_active_chats`, the same on the web).
- The day is the UTC day; a time-zone setting does not exist.
- No token expiry date, no rename, no un-revoke.
- Restoring a backup of `DATA_ROOT` brings back its `mcp_tokens.json`: a token
  revoked after the backup is active again. Revoke it again after a restore.
- Times on the admin page (created, last used) are UTC.
- A chart title can be clipped at the top of the PNG. The web's PNG export
  makes the same file (existing renderer behaviour).

---

## 13. Settings

| Variable | Default | Meaning |
|---|---|---|
| `MCP_ANSWER_WAIT_SECONDS` | 20 | Seconds `get_answer` waits for a running question before it answers "running" (0–60) |
| `MCP_TABLE_MAX_ROWS` | 50 | Rows of a table in an answer (1–50) |
| `PUBLIC_BASE_URL` | empty | The address of the endpoint and of the link, and the allowed `Origin` |

---

## 14. Build status

As of 2026-11-25, branch `feat/mcp-integration`:

| Stage | Content | State |
|---|---|---|
| 1 | Token store and caller identity | done |
| 2 | Run-and-save module and drift test | done |
| 3 | Endpoint, tools, job store; security check | done |
| 4 | Answer content, logging | done |
| 5 | Admin section in English, Georgian, Russian | done |
| 6 | Test client; integration run on the local stack | done |
| 7 | Documentation | done |
