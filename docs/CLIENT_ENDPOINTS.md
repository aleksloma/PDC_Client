# Client-side endpoints

The on-prem client serves the existing `/lab` page (copied verbatim from the
B2C app). `dashboard.js` calls a particular set of backend endpoints; this
file documents the enterprise client's implementation of each one.

> **Why this matters:** the dashboard expects the B2C internal API shape. A
> verbatim template copy without the matching backend is just a visual shell.
> The fix is to implement the same endpoints the B2C app exposes (or stub
> them gracefully where the feature does not exist on-prem). See
> "Lesson learned" at the bottom.

### Behaviour common to every route

- **Pending forced password change.** While the session carries
  `must_change_password` (a temp-password or bootstrap-password sign-in),
  every route answers `403 {"error": "Password change required", "code":
  "PASSWORD_CHANGE_REQUIRED"}` EXCEPT: the pages `/`, `/lab`, `/c/…`,
  `/dashboards/…`, `/admin/data_sources`, `/power/data_sources` (each
  redirects to the change form itself), `/static/…`, `/health`, `/version`,
  `/auth/login`, `/auth/logout`, `/auth/change_password`,
  `/auth/reset_password`, `/auth/me`, the reset-link routes (`/auth/reset/…`)
  and the Microsoft sign-in routes (`/auth/microsoft…`). Source:
  `PasswordChangeGate` in `app.py`.
- **Requests from the analysis sandbox's network.** A request whose network
  peer lies inside the sandbox's subnet (`EXECUTOR_NETWORK_CIDR`) is refused
  on EVERY path, `/health` included, with a bare `403 {"error": "forbidden"}`
  (no path, range or caller named) and one `BACKEND_REQUEST_REFUSED` log line
  per peer address. Source: `BackendNetworkGuard` in `app.py`.
- **Chat ids.** A chat id outside `[A-Za-z0-9_-]{1,64}` answers
  `404 {"error": "Chat not found"}`, like an unknown chat.
- **CSRF.** The session cookie is `SameSite=lax`, and every state-changing
  endpoint is a `POST` — a cross-site form or link cannot carry the session
  into a write.
- **Content-Security-Policy.** Every `text/html` response carries a
  `Content-Security-Policy` header with a fresh nonce per response:
  `default-src 'self'; script-src 'self' 'nonce-…'; style-src 'self'
  'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:;
  connect-src 'self'; frame-src 'self'; frame-ancestors 'self';
  base-uri 'self'; form-action 'self'; object-src 'none'`. Every inline
  `<script>` in the templates carries that nonce, and the page exposes it as
  `window.__CSP_NONCE__`. JSON, event-stream, file and static responses carry
  no policy. `ENABLE_THIRD_PARTY_SCRIPTS` adds the analytics and billing
  origins; `GCS_UPLOAD_BUCKET` adds `https://storage.googleapis.com` to
  `connect-src`; `CSP_REPORT_ONLY=true` sends the same policy as
  `Content-Security-Policy-Report-Only` (a diagnostic). The page policy never
  allows `'unsafe-eval'`, and it is not added to a response that already
  carries its own policy (the chart documents below). Source:
  `ContentSecurityPolicy` in `app.py`.
- **Rendered chart and table markup.** Chart HTML (`image_base64` holding a
  Plotly document, from a stream, history, a refresh or a dashboard tile) is
  rendered only inside an iframe with `sandbox="allow-scripts"` — an opaque
  origin with no cookies, storage or access to the page — built by
  `PDCViewers.setChartFrame` (`static/vendor/viewers.js`), which registers the
  HTML with `POST /api/charts` and loads the returned `/charts/{token}` (see
  "Chart documents"). `styled_html` (a pandas Styler's table) is sanitised
  on the server to an allowlist of table elements, `T_`-prefixed ids, the
  class names pandas Styler generates (`T_<hex>`, `col<n>`, `row<n>`,
  `level<n>`, `data`, `index_name`, `blank`, `col_heading`, `row_heading`),
  CSS values without functions other than `rgb`/`rgba`/`hsl`/`hsla` and
  without `!important`, bounded sizes (width/height, margins and padding,
  font size ≤ 72px / 5em, line height, border widths ≤ 20px, border spacing,
  a non-negative text indent; no `text-shadow` or `font` shorthand), the
  Styler's `row_trim`/`col_trim` classes, and `#T_`-scoped style rules
  (`html_sanitize.clean_styled_html`) when it is
  produced, when a table tile is pinned or refreshed, and whenever a stored
  history row or dashboard tile is served; when nothing survives, the field
  is omitted and the page shows the plain `{columns, rows}` table. Stored
  files are never rewritten. Matplotlib charts stay base64 PNGs shown as
  `data:image/png` images.

---

## Chart documents

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/api/charts` | `{html}` (a chart document, ≤ 5,000,000 characters) → `{url: "/charts/<token>"}`. Signed-in only (`401` otherwise); `400` for a missing, non-string, empty or oversize `html`. The HTML goes into a bounded in-memory store (30-minute lifetime; sizes counted in UTF-8 bytes, 200 MB in total and 40 MB per user; when full, the registering user's own oldest entries go first, then the oldest overall; one web worker). Called by `PDCViewers.setChartFrame` for every chart it renders — live, from history, refreshed, or from a dashboard tile. |
| `GET` | `/charts/{token}` | the registered document. The token is signed with `SECRET_KEY`, valid 30 minutes and carries only the store entry's id (no address); the entry is bound to the user who registered it; a bad, expired, evicted or other user's token, or no session, → `404`. Headers: `Content-Security-Policy: sandbox allow-scripts; default-src 'none'; script-src 'self' 'unsafe-eval' 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'` (no nonce: the document is served exactly as registered, its inline scripts run inside the sandbox, and external scripts can come only from this server), `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`. The only response in the application whose policy allows `'unsafe-eval'` (Plotly's WebGL traces need it); the document runs with an opaque origin. |

---

## Pages (HTML)

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/` | auth landing (email + password + "Remember me"); redirects to `/lab` if already signed in (or to `/auth/change_password` when a forced change is pending). When Microsoft SSO is ENABLED (`sso_store.is_enabled()`), a "Sign in with Microsoft" link renders ABOVE the unchanged password form (i18n'd, EN/GEO/RU); with **auto-redirect** also on, an unauthenticated visitor is 302'd straight to `/auth/microsoft` — EXCEPT `/?local=1`, the always-available escape hatch that shows the password form (ladmin recovery). The authenticated-session redirect runs FIRST, so a signed-in user never bounces to Microsoft. SSO disabled ⇒ the page renders byte-identically to a pre-SSO build. |
| `GET` | `/lab` | the dashboard page (no session → `/`; `must_change_password` pending → `/auth/change_password`; **bootstrap ladmin → `/admin/data_sources`** — only the appliance account is config-only; a PROMOTED admin renders /lab like any user with the B2C `is_admin` template flag still `false` for everyone, 19g) |
| `GET` | `/c/{conv_id}` | deep-link / hard-refresh into one conversation. Resolves the conv's `chat_id` from the caller's conversations index and seeds `open_conv_id`/`open_chat_id` so `dashboard.js` auto-opens it. No session → `/`; forced change pending → `/auth/change_password`; unknown/foreign conv → `/lab` (never 404). |
| `GET` | `/auth/change_password` | forced set-a-new-password page shown after a temp-password login (no session → `/`; no pending flag → `/lab`) |
| `GET` | `/dashboards/{dash_id}` | the dashboard page (grid of pinned tiles, `dashboard_view.html` + `dashboard_view.js`). Resolves the dashboard via own-doc-or-shared-pointer; no session → `/`; forced change pending → `/auth/change_password`; unknown/unshared → `/lab` (never 404). |
| `GET` | `/health` | liveness + `{brain_reachable, tenant_token_configured}`; v4.2 adds `build_commit` (additive). Additive again: `executor_reachable` + `executor_checked_at` for the analysis sandbox. `executor_reachable` is `null` until the web service has contacted the sandbox once (nobody has checked yet, which is NOT "down"), then `true`/`false`; `executor_checked_at` is the epoch seconds of that observation, `null` while it is `null`. Both come from a CACHED verdict refreshed in the background (the startup handshake, every dispatch, a short refresh thread) and never from a live probe inside the handler — an unresolvable sandbox name costs ~8 s in `getaddrinfo`, which would stall this async handler past the container's own 5 s healthcheck. **The endpoint stays 200 either way, deliberately**: a stack whose sandbox is down must still serve the page that says so, so a caller has to read the body and never the status alone |
| `GET` | `/version` | which build is running: `{commit, build_time, started_at}` — nulls on an unstamped image, never a fabricated identity. Unauthenticated like `/health`, carries no secrets. Exists because the static `?v=` parameter is `int(time.time())` at page render (per request) and therefore **cannot** identify a build — reading it as one misled release verification twice. Fed by the `BUILD_COMMIT`/`BUILD_TIME` Docker build args; the admin sidebar shows the same string |
| `GET` | `/admin/data_sources` | the ladmin **Data sources** admin panel (`admin_data_sources.html` + `admin_data_sources.js`, standalone stylesheet — no dashboard.css): sidebar-navigated sections (DB connections, registered tables, relations — a confirmed-relations overview with per-row Edit/Delete above the discovery tools — users, roles, single sign-on (Microsoft Entra ID config — see the SSO admin-routes section + `docs/SSO_MICROSOFT.md`), refresh schedule, audit log), a 3-step register-table wizard (whose relations step auto-suggests relations from the table's own FKs + name/description similarity, FK rows pre-checked) (Source → Describe → Confirm & snapshot), onboarding hero when no connections exist, and sidebar account controls (change password via `POST /auth/password`, sign out). This is the BOOTSTRAP ladmin's landing page — its login and forced-change redirect here (a PROMOTED admin lands on /lab and reaches this page via the dropdown's "DB config", 19g). Redirect philosophy: no session → `/`; forced change → `/auth/change_password`; non-admin → `/lab` (never an error page). Renders with `manager_mode: "admin"` (→ `window.__MANAGER_MODE__`); 19g adds `back_to_chat` to the context — true for promoted admins, so the sidebar footer carries the "← Back to chat" link (absent for the bootstrap account, which has no chat). |
| `GET` | `/power/data_sources` | the POWER-USER variant of the same page (prompt 19): the SAME `admin_data_sources.html` rendered with `manager_mode: "power"`. Sidebar shows only Connections (READ-ONLY list of in-scope connections — no Add/Edit/Delete/Test/Refresh; "＋ Register table" is the one action), Registered tables (Delete only on tables the power user registered themselves — `registered_by`), and Relations; the per-table ⏱ schedule modal stays, Users/Roles/Audit/global-schedule are not rendered; the wizard step-3 panel renders as "Share with your roles" (the caller's HELD roles from GET /api/admin/my_roles, all unchecked by default — 19f publish+share) and the page header carries the server-computed manage-scope summary ("You can manage: …", app._power_scope_summary) plus a muted read-beyond-manage hint; the sidebar footer gains a "← Back to chat" link to `/lab`. Redirects: no session → `/`; forced change → `/auth/change_password`; ladmin → `/admin/data_sources`; non-power users → `/lab` (`POWER_PAGE_DENIED` log). Power users reach it via the "DB config" item in the /lab profile dropdown (rendered when `is_power_user` — a promoted admin's "DB config" targets the full `/admin/data_sources` page instead, 19g). |

---

## Auth (email + password, all local)

Passwords never leave this container — only a HASH is stored, at
`DATA_ROOT/users/{email}/auth.json` (`password_hash`, optional
`temp_password_hash` + `must_change_password`). Hashing:
`password_utils.py` (stdlib PBKDF2-HMAC-SHA256, werkzeug-compatible
format). Sign-in is by INVITATION: an account exists only after an
administrator's invite (`POST /api/admin/users/invite`), a share (password-less
placeholder) or a Microsoft SSO sign-in. An account without a password —
invited, shared with, or a LEGACY user from the old email-only build — sets
one through the mailed reset link, which proves mailbox ownership.
`ALLOW_SELF_REGISTRATION=true` (public demo only) restores the old rule that a
genuinely NEW email (no user folder) adopts the entered password. The brain
is only involved as an email relay (`/v1/send_welcome_email`,
`/v1/send_password_reset_email`, which carries the reset link).

Attempt limits (`auth_limiter.py`, in memory, one web worker): an attempt is
counted before it is evaluated and retracted on success. After
`AUTH_FAIL_THRESHOLD` (5) failures for one address within `AUTH_FAIL_WINDOW_S`
(900 s) the next attempts must wait 1, 2, 4, 8 s, then the address is locked
for `AUTH_LOCKOUT_S` (900 s); sign-in and reset requests count in separate
buckets. One peer address is spaced the same way after
`AUTH_FAIL_THRESHOLD_IP` (20) failures and never locked. The configured
`LOCAL_ADMIN_USERNAME` is never address-locked either (`begin(...,
lockout=False)`): where it would lock it waits another 8 s instead. An early or locked
attempt answers `429` with `Retry-After` and "Too many attempts. Please try
again later." without evaluating anything. A lockout logs `AUTH_LOCKOUT
kind=… h=<hash prefix>`, never the address. Addresses must match
`routes.auth._EMAIL_RE` (letters, digits, `._%+-` before the `@`, ≤ 254
characters) on sign-in, reset, invite and every share route.

Password rule: every password SET — reset link, forced change,
`/auth/password`, demo self-registration — must have at least
`PASSWORD_MIN_LENGTH` (8) characters; the one message is "Password must be at
least N characters". Sign-in never checks it, so an older, shorter password
keeps working until it is changed.

Sessions: sign-in stamps the session with `iat` (sign-in time) and `gen` (the
account's `session_generation` from `auth.json`). Every password write gives
the account a new generation; `SessionGenerationGate` (app.py, inside the
session middleware) empties a session whose `gen` no longer matches, so the
request continues signed out (pages → `/`, APIs → 401) and the response clears
the cookie. The route that changed the password re-stamps its own session. A
session ends `REMEMBER_ME_MAX_DAYS` (30) days after `iat`, remembered or not;
a remembered cookie's `Max-Age` is the remaining lifetime. A cookie from an
earlier release carries neither key: an account whose password has not
changed since keeps it, and its lifetime is counted from its last renewal.
The generation is cached in the one web process, so a password written by
another process (an operator script) ends other sessions only after the next
restart; and a request the changing browser had already sent before the
change completed can sign that browser out too (it signs in again).

Microsoft-only accounts (`sso_provider` set, no local hash —
`AuthStore.is_sso_only`) never get a local password: the anonymous reset
request mints nothing and answers the neutral page; the reset-link POST, the
forced change and `/auth/password` answer 403 with "This account signs in with
Microsoft and has no local password." (`/auth/password`: `{error, code:
"SSO_ACCOUNT"}`); the invite answers 409 with the same code.

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/auth/login` | form-encoded `email=`, `password=`, `remember?`. Every failure — unknown address, an account without a password (invited, shared with, legacy) and a wrong password — answers `401` with the SAME page and line, "Sign-in failed. Check your email and password, or use “Reset password” if you have not set one yet.", plus the Reset action, after exactly one password-hash verification (a fixed dummy hash where there is none), so neither the body nor the time says which case it was; nothing is created. The exceptions are the unbootstrapped `ladmin` (403, the server-side fix) and `ALLOW_SELF_REGISTRATION=true`, where a NEW email adopts the entered password + welcome mail. A malformed form → 400; over the attempt limit → 429 + `Retry-After`. A temporary password from a release before this one → session flagged and redirected to `/auth/change_password`. Success target: `/lab` for everyone — promoted admins included — except the bootstrap ladmin account → `/admin/data_sources` (`_post_login_target`, keyed on `AuthStore.is_bootstrap_admin`, 19g). `remember` → persistent session cookie whose `Max-Age` is the time left of the 30-day lifetime (RememberMeSessionMiddleware in app.py); otherwise browser-session cookie; either way the session ends 30 days after sign-in. |
| `POST` | `/auth/reset_password` | form-encoded `email=`. Every well-formed address — known, unknown, or an email-shaped `ladmin` — answers `200` with the same page: "If an account exists for this address, a reset link has been sent. It expires in 30 minutes." Everything that depends on the account runs in a background thread: for an existing account a token (`secrets.token_urlsafe(32)`) is minted, only its SHA-256 stored in `auth.json` (`reset_token_hash`, `reset_expires_at`, `reset_used`; a new request replaces the old token) and the link `<PUBLIC_BASE_URL>/auth/reset/<token>` brain-relayed; a failed send discards the token. With `PUBLIC_BASE_URL` unset or not an http(s) URL nothing is minted or sent (`PASSWORD_RESET_NO_BASE_URL` logged) — the link is never built from the request's `Host`, which the caller controls. The user's own password stays valid until the link is used. A non-email id → 400; over the attempt limit → 429 + `Retry-After`. |
| `GET` | `/auth/reset/{token}` | the set-new-password form (`reset_password.html`, no script) with `Cache-Control: no-store` and `Referrer-Policy: no-referrer`; no side effect. A malformed, unknown, expired or used token → the sign-in page with "This reset link is invalid or has expired. Request a new one." (404). Invalid tokens count per peer address. |
| `POST` | `/auth/reset/{token}` | form-encoded `new_password=`, `confirm_password=` (the password rule above, matching; a rule failure re-renders the form, 400; a Microsoft-only account → 403). Validates and consumes the token in one locked step, sets the password (which also clears any temporary password and pending forced change), `302` → `/?reset=done` ("Your password has been updated. Sign in with it."). No automatic sign-in; every open session of the account ends. A second use → 404; a 429 here carries the same no-store / no-referrer headers. |
| `POST` | `/auth/change_password` | form-encoded `new_password=`, `confirm_password=` — the forced-change submit (session required; the password rule above, 400; a Microsoft-only account → 403). Ends the account's other sessions; this one stays signed in. |
| `POST` | `/auth/logout` | clears session, redirects to `/`. |
| `GET`  | `/auth/me` | `{authenticated, email}` |
| `GET`  | `/auth/profile` | `{username: email, email, full_name: "", subscription_plan: "Enterprise", is_local_admin, is_power_user, is_admin_user}` — shape that dashboard.js expects. `is_local_admin` and `is_admin_user` (deliberately NEVER `is_admin` — that key feeds the B2C Publish menu, 400 by design on-prem); `is_power_user` = the user's per-account PERMISSION is "power" (AuthStore profile `role`, 19e — roles_store.is_power_user delegates to it; admin is NOT power; fail-closed false on any error); `is_admin_user` (19g) = a PROMOTED admin — permission "admin" AND not the bootstrap account (fail-closed false). Both feed the profile-dropdown "DB config" item: power → `/power/data_sources`, promoted admin → `/admin/data_sources` (the partial bakes the target into `data-target`) |
| `POST` | `/auth/profile/update` | email is the identity; attempts to change it are silently ignored |
| `POST` | `/auth/password` | JSON `{current_password, new_password}` — real change-password (verified server-side), used by the /lab profile-dropdown modal and the admin panel's password modal. 400 on the password rule; 401 on wrong current password; 403 `{code: "SSO_ACCOUNT"}` for a Microsoft-only account. Ends the account's other sessions; this one stays signed in. |
| `GET`  | `/auth/subscription` | constant `{plan: "Enterprise"}` |
| `GET`  | `/auth/active_chats` | list of user's chats |
| `GET`  | `/auth/conversations` | list of user's conversations |
| `POST` | `/auth/active_chats/rename` | `{chat_id, title}` |
| `POST` | `/auth/active_chats/pin` | `{chat_id, pinned}` → `{ok, pinned}` — pin/unpin a chat; pinned chats sort FIRST in `/auth/active_chats` (then newest-first as before). Additive `pinned` flag on the user's own jsonl row; absent = unpinned (old rows unaffected) |
| `POST` | `/auth/conversations/rename` | `{conv_id, title}` |
| `POST` | `/auth/conversations/delete` | `{conv_id}` |

### Microsoft Entra ID SSO (`routes/sso.py`, optional — ladmin-configured)

OIDC authorization-code login against the customer's own Entra tenant,
driven entirely by `DATA_ROOT/sso_config.json` (managed from the ladmin
"Single sign-on" panel — no env vars, no restart; the client secret is
Fernet-encrypted with the SAME `CLIENT_ENCRYPTION_KEY` the DB credentials
use). Authlib does discovery/JWKS/ID-token validation and keeps state+nonce
in the cookie session; PDC never sees a password and reads ONLY the email
(`preferred_username`, fallback `email` claim) from the ID token. Logout
stays local-only by design (no Microsoft front-channel logout). Customer
guide: [`docs/SSO_MICROSOFT.md`](SSO_MICROSOFT.md).

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/auth/microsoft` | starts the flow: 302 to `login.microsoftonline.com` with state+nonce in the session. **404 while SSO is not enabled** (unconfigured installs look pre-SSO). Enabled but secret unreadable (encryption key rotated away) → landing with the generic failure message (503). |
| `GET` | `/auth/microsoft/callback` | exchanges the code, validates the ID token (authlib — signature/issuer/audience/nonce), lower-cases the email claim, auto-provisions the local profile (`ensure_user` — access control is Entra's "Assignment required", no client-side allow-list), stamps `sso_provider`/`sso_last_login` on auth.json (`mark_sso_login` — merge-only, password hashes untouched), starts the session via the SAME `_start_session` as password login but with `remember=False` (browser-session cookie — Entra re-auth is silent) and never `must_change`, logs `USER_LOGIN_SSO`, posts the normal `login` activity event, 302 → `/lab`. Any failure (state mismatch, token error, missing email claim) → landing with "Microsoft sign-in failed…" (401/400), token contents never logged. 404 while disabled. |

---

## Upload flow

The dashboard's "frictionless drop" runs these four endpoints in order. The
enterprise build keeps the same shape. The B2C large-file (direct-to-GCS)
branch is present but GATED on the server flag `window.__DIRECT_UPLOAD__`
(true only when `GCS_UPLOAD_BUCKET` is set — the Cloud Run demo); on every
customer install the flag is false and step 2 handles every size:

1. **`POST /new_session`** — resets the per-session temp `UserStore`.
   Returns `{ok: true}`. Issues a fresh SID into the session cookie.

2. **`POST /upload`** (multipart, field `files`; EVERY file regardless of
   size whenever direct upload is off — the server sets no request-body
   limit) — saves uploads to the
   per-session temp area (under `<DATA_ROOT>/sessions/<sid>/files/`).
   **The multipart filename is SANITIZED before it touches the filesystem**
   (`local_store.sanitize_upload_filename`, re-checked for containment inside
   `files_dir` by `UserStore.save_upload`): path components, control
   characters and leading dots are removed, the name is capped at 200 UTF-8
   bytes keeping the extension, and a name with nothing left becomes
   `upload_<8 hex>`. Unicode is preserved, so an ordinary name — Georgian
   included — is stored byte-identically to what the browser sent.
   `saved`, `dataframes` and `files[].file` all report the name **as stored**,
   which for a repaired name differs from the one the client supplied. Two
   names in one batch that sanitize to the same string overwrite each other.
   Returns `{ok, saved, dataframes, files}` — the B2C keys unchanged, plus an
   additive per-file result list: `files: [{file, status: "ok"|"warning"|
   "error", skipped_rows?, first_bad_line?, message?}]`. CSV parsing is
   TOLERANT (strict parse first; on failure the python engine skips and
   COUNTS malformed rows — e.g. an unquoted comma inside a value): a parsed
   file with skips gets a `warning` row the frontend toasts. A saved file
   that yields NO dataframe is a per-file `error`, and `/upload` then
   answers `ok: false` with a top-level `error` naming the bad file(s)
   (HTTP 400 when every file failed) — it never again answers `ok: true`
   with an empty `dataframes` list (the old silent no-op). **Raw bytes
   never leave this server.** Excel workbooks load
   only **visible** sheets — hidden and veryHidden sheets are skipped by
   `load_excel_sheets` (the single choke point every load path shares:
   chat creation, Add Data, and chat-time dataframe loading). A workbook
   whose sheets are ALL hidden yields zero dataframes and flows through
   the normal "no valid tables in file" handling.

   **Parquet cache:** every `load_dataframes()` call goes through a
   self-healing parquet cache (`local_store._load_one_file_cached`) under
   `<files_dir>/.parquet_cache/` — one parquet per resulting dataframe key
   plus a per-source-file manifest keyed on the source's size + `mtime_ns`.
   A matching manifest skips the detection pipeline entirely (fast path);
   a missing/mismatched/unreadable cache runs the existing pipeline and
   atomically rewrites the cache. Overwriting a file's bytes (Add Data)
   changes its stat and invalidates its cache automatically. Cached frames
   are the POST-detection output (hidden sheets skipped, totals dropped)
   and are round-trip-verified on write, so results are identical to a
   fresh parse; any cache failure falls back to the old slow path.

3. **`POST /schema_autofill_full`** — verbatim port of global's
   `/schema_autofill_full` (`backend/routes/schema.py` L884-1012), split for
   the brain/client boundary:
     - Client builds per-file context locally: dtypes, sampled / truncated
       unique values, language hint (from column names), columns needing fill.
       Identical to global's `_prepare_file_context` — runs against the local
       DataFrames so raw row data never leaves.
     - Client POSTs `/v1/schema_autofill` to the brain (one call per file, run
       in parallel via `asyncio.gather` + bounded ThreadPool). Brain returns
       `{file_description, columns: {col: desc}}`.
     - Client merges results into `meta.json` and **also** generates a
       `technical_description` for every column from local pandas stats
       (dtype, fill rate, categorical/sample values) — verbatim port of
       global's `_generate_technical_description`.
   Returns `{ok, filled: <total>, files: [...], updated: <total>}`. Failure of
   any single file falls back to leaving descriptions blank (just like global)
   and the technical_description step still runs.

   **Dataset profiles (Prompt 13):** the same step 4 pass also computes a
   per-table profile (`dataset_profile.compute_profile` — rows, duplicates,
   per-column stats/flags, detected grain, ≤6 deterministic warnings) and
   stores it at `<files_dir>/.profiles/<safe>.profile.json` with a staleness
   stamp (source size + mtime_ns + parser_version). DB tables get theirs at
   every actual re-snapshot (`db_snapshots/{tid}.profile.json`). The chat
   handlers backfill any missing/stale profile once per table
   (`local_store.ensure_chat_profiles`) and attach the compacted dict to
   `/v1/plan` / `/v1/retry` as the optional `dataset_profile` field (see
   `docs/PROTOCOL.md`); a missing profile always degrades silently.

4. **`POST /generate_chatdata`** — clones the temp `UserStore` into a
   permanent `ChatDataStore` under `<DATA_ROOT>/chatdata/<chat_id>/`,
   records the chat in the user's `active_chats.jsonl`, and calls the
   brain's `/v1/chat_metadata` endpoint. That endpoint is a verbatim port
   of global `_generate_all_parallel` (3 parallel sub-calls: chat name,
   welcome message, suggested questions) — same prompts, same sanitizers
   — so the output is identical to the B2C app. Returns
   `{ok, chat_id, name, welcome_message, suggested_questions}`.

### Direct-to-GCS large files (OPTIONAL — `GCS_UPLOAD_BUCKET`)

Port of the B2C flow, for deployments behind an ingress with a request-body
cap (the Cloud Run demo: 32 MiB on HTTP/1). **Off by default**: with
`GCS_UPLOAD_BUCKET` unset (every customer install, the local Docker stack)
`POST /upload/init` and `POST /upload/finalize` return `400`
(`{"error": "Direct-to-GCS upload is not available in the on-prem build. …"}`
/ `{"error": "Disabled in the on-prem build."}`) whatever the body or session,
and `dashboard.html` renders `window.__DIRECT_UPLOAD__ = false`, so the
frontend never calls them. `POST /upload_from_url` (Google Sheets/Drive import)
returns `400` regardless of the flag.

With the bucket set, `dashboard.js`'s `runFrictionlessFlow` — the ONE call
site, covering page drop, the Create-New wizard and Add Data — routes a batch
containing any file > 25 MiB (`LARGE_UPLOAD_THRESHOLD_BYTES`) through, per
file, after the usual `POST /new_session`:

- **`POST /upload/init`** — JSON `{filename, content_type, size_bytes}`
  (session required → 401). Validates the basename (Unicode kept, control
  chars stripped, no leading dot / `..` / >200 chars → 400 `Invalid
  filename.`), the extension (`.xlsx .xls .csv .tsv` → 400 `Unsupported file
  type…`), `0 < size_bytes ≤ 500 MB` (400 `File too large…`) and the session's
  distinct source-file count vs `MAX_FILES` (400). Returns
  `{signed_url, gcs_path, expires_at}`: a 15-minute V4 signed PUT URL bound
  to `content_type`, signed with the runtime service account through IAM
  `signBlob` (no key file), and the object key `tmp/{sid}/{uuid}/{name}` —
  namespaced under the CALLER'S SESSION. A signing failure (no credentials,
  no `iam.serviceAccountTokenCreator`) is a fixed 500 `Direct upload is not
  configured on this server.`; the signed URL is never logged.
- The browser PUTs the bytes to `signed_url` with the same `Content-Type`
  (XHR, progress into `#frictionlessStatus`).
- **`POST /upload/finalize`** — JSON `{gcs_path, file_descriptions?}`.
  Rejects any path outside `tmp/{sid}/` (400 `Invalid upload path.`), checks
  the REAL object size (404 when missing, 400 over 500 MB — a signed PUT does
  not bind Content-Length), re-checks that the destination RESOLVES inside
  `files_dir` before writing anything (400 `Invalid filename in upload path.`,
  the same containment assert `UserStore.save_upload` makes), streams it into
  the session `files_dir`, deletes
  the object (best effort, also on failure; the bucket's 1-day lifecycle rule
  is the backstop) and runs the SAME post-save pipeline as `/upload`
  (`_finish_upload`), returning the same `{ok, saved, dataframes, files}` shape
  and the same `ok:false`/400 semantics. It never resets the session, so a
  multi-file batch accumulates one finalize per file (DB-table selections
  survive). Log lines: `UPLOAD_INIT`, `FILE_SAVED via=gcs`, `UPLOAD_OK`, `UPLOAD_FINALIZE_UNSAFE_PATH`.

### Add Data to an existing chat

**`POST /add_data_to_chat`** — body `{chat_id}`. Triggered by the **Add Data**
button in the chat topbar (left of "View / Edit Descriptions"; the Create-New
modal reopens in "add" mode: title `Add Data To "<chat name>"`, primary button
**Upload**). The frontend runs the SAME preprocessing pipeline as chat
creation — `/new_session` → `/upload` (table detection) →
`/schema_autofill_full` (descriptions) — and then calls this endpoint instead
of `/generate_chatdata`. It merges the temp session store into the existing
`ChatDataStore`:

- Raw files are copied into `chatdata/{chat_id}/files/`; a filename that
  already exists in the chat is **overwritten as a data update — never
  silently**: the frontend's name-collision dialog (below) has already made
  the user choose Overwrite vs upload-as-`_vN`. **Caveat:** that dialog
  compares the BROWSER name against the STORED names from
  `GET file_fingerprints`, so for the narrow set of names
  `sanitize_upload_filename` alters (leading dots, control characters, >200
  UTF-8 bytes, embedded path components) the two disagree, no dialog appears
  and the add overwrites silently. Ordinary names are unchanged by the
  sanitizer and unaffected.
- meta.json merge: new keys get their autofilled entries appended. For an
  **overwritten** source file the entries are **re-synced**
  (`_resync_meta_after_add` in `routes/upload.py`): entries whose df key
  vanished from the new upload are **deleted** (no stale entries in the
  descriptions modal / schema_text); a surviving key takes the fresh
  autofilled entry but keeps the old `file_description` and, per column that
  existed before, the old (possibly user-edited) `description` + `values`
  mappings; brand-new keys/columns keep their fresh autofill. Any resync
  failure logs `ADD_DATA_RESYNC_FAILED` and falls back to the previous
  append-only merge (meta is never corrupted). Response carries
  `{added, updated, removed, files}`.
- The overwritten bytes bump the file's size/mtime, so its parquet cache
  invalidates on the next load (see the Parquet-cache note above).
- The user's `active_chats.jsonl` record gets its `files` list refreshed
  (sidebar subtitle).
- Nothing else changes: `schema_text` is rebuilt from all loaded dataframes on
  every question, so added files are immediately visible to generated code and
  to the View / Edit Descriptions modal (which re-fetches
  `/api/chat/{id}/schema` on open).

Requires an authenticated session that OWNS `{chat_id}` — a shared recipient
reads the chat but does not change it, and gets `403 {"error": "Access
denied"}`. `400` when no files were uploaded in the session; raw data never
leaves the client.

### Add Data name-collision dialog (frontend)

**`GET /api/chat/{chat_id}/file_fingerprints`** →
`{files: {source_filename: {size_bytes, sha256}}}` for the chat's current
source files (owner/shared access via `_require_chat`; hashing runs in the
executor; a per-file failure degrades to a name-only `{null, null}` entry).
Served by the client container itself — fingerprints never reach the brain.

When files are picked in the Add Data modal, `dashboard.js` compares each
selected name against these fingerprints (sizes first; the browser computes
the local file's SHA-256 via `crypto.subtle` only on a size tie):

- **identical content** → small notice "This file is already in this chat —
  no changes needed"; the file is dropped from the upload (no dialog).
- **different content** → dialog with exactly two choices: **Overwrite
  existing file** (primary/colored button) or **Upload as `<base>_vN.<ext>`**
  (neutral; first free suffix, applied through the whole pipeline via the
  FormData filename override). Dismissing the dialog drops the file.

The dialog opens with the generic warning ("If the new file has different
columns/sheets, some previous charts and tables may no longer be
refreshable.") and immediately fires a background **column probe** with a
~3s budget (`POST /api/chat/{chat_id}/probe_columns`, below). If the probe
returns in time it replaces the line in place: structures match → green
informational "Both files appear to contain the same columns — existing
charts should keep working after overwrite."; structures differ → "Different
columns/sheets detected — after overwrite some previous charts and tables may
not be refreshable." On probe failure/timeout the generic warning stays. The
dialog never blocks on the probe — the buttons are clickable immediately, and
a choice made before the probe returns simply ignores its result.

**`POST /api/chat/{chat_id}/probe_columns`** — multipart `file`. Returns a
fast, header-level structure comparison of the uploaded file vs the chat's
EXISTING same-named file WITHOUT running the detection pipeline:
`{ok, match, uploaded, existing}` where the summaries map
`sheet_name (or "" for csv/tsv) → [approximate column strings]`
(.xlsx/.xlsm via openpyxl read_only — visible sheets, first non-empty row
within the first 50 rows; .csv/.tsv — the header line). Behind
`_require_chat`; parsing runs in the executor; any failure (unsupported
format, no same-named file, parse error) returns `{ok: false}`. Served by the
client container only — nothing reaches the brain; cell values are never
logged (filename + match/mismatch only).

Duplicate names **within one selection batch** (chat creation or Add Data)
are resolved the same way without a dialog: identical → one kept + "Duplicate
file ignored" notice; different → the second is auto-renamed to the first
free `_vN` name (shown renamed in the wizard's file list).

---

## Database tables (admin-registered "Data sources", snapshot and live mode)

An ladmin-registered database table enters a chat exactly like an uploaded
file: its snapshot parquet (ONE central copy at
`DATA_ROOT/db_snapshots/{table_id}.parquet`) is merged into the chat's
dataframes by `load_dataframes()`, keyed by the table's **display name**. The
chat meta carries a meta-only entry (`source: "database"`, `db: {table_id,…}`,
no path) — see `docs/ENTERPRISE_ARCHITECTURE.md` §"Database tables". Raw DB
values never reach the brain (Article II unchanged): only names, dtypes,
ladmin-confirmed descriptions and the same truncated sampled hints uploaded
files already send cross via `schema_text` / `schema_autofill`.

**Storage mode.** Every registered table has a `mode`: `"snapshot"` (the
default, described above) or `"live"`. A document without the field reads as
snapshot and is never rewritten. A live table has no snapshot parquet. A
live table is registered, counted and profiled from a bounded sample, and
scheduled refreshes skip it (`LIVE_SKIP`). In a chat it is queried at
question time: `load_dataframes(include_live=True)` (the chat stream,
edit-regenerate and the refresh paths) enters it as an empty typed
placeholder under its display-name key — a parquet kept from before the
live period is never served — and, immediately before every sandbox call,
the main application runs one read-only SELECT for each live key the code
references (the planner's `sql`, validated by `assert_read_only_query` with
a per-table allowlist and wrapped under the row cap, or the connector's own
capped default read when no SELECT was sent or an administrator row filter
applies) and places the result under the key; the sandbox sees an ordinary
frame. Results are capped by `LIVE_RESULT_ROW_CAP` rows and
`LIVE_RESULT_MAX_MB` megabytes and bounded by `LIVE_QUERY_TIMEOUT_S`; a
capped result appends a localized note to the answer. The requester's role
must cover the table before the planner is called (uncovered live keys are
dropped from the frames and the schema; a turn left with nothing answers the
denial sentence without a brain call) and again before every fetch. Every
other loader keeps the default (`include_live=False`) and never sees a live
key — Auto Analytics included. Sizes are measured in
CELLS (rows x columns) against `LIVE_MODE_CELL_THRESHOLD` (default
50 000 000: live is suggested) and `LIVE_MODE_FORCE_THRESHOLD` (default
500 000 000: a snapshot is refused for a new registration or a mode switch
and live is required; the effective force threshold is the larger of the
two). An edit of an existing table keeps its stored mode. The design is in
`docs/LIVE_TABLES_PLAN.md`.

### Client-facing routes (any signed-in user)

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/api/db_tables` | Registered NON-connector tables for the Create-New / Add Data picker, **filtered to the requester's role** (`roles_store.allowed_table_ids_for` — explicit grants ∪ schema/connection scope grants, computed per request): `{tables: [{table_id, display_name, description, row_count, refreshed_at, mode}]}`. LIVE tables are listed too (`mode: "live"`, `refreshed_at` null — they are queried at question time). Connector (helper/join) tables are never listed — they are auto-included through the relations graph and exempt from role checks. A Base-role user with no grants gets an empty list. |
| `POST` | `/session/db_tables` | Body `{table_ids: [...]}`. Sets the temp session's DB-table selection: validates ids, **rejects a directly-selected connector (400)**, accepts a LIVE seed exactly like a snapshot one (the chat loads it as a placeholder and queries it at question time), **rejects seeds outside the requester's role (403 `{error, code:"ROLE_DENIED"}` naming the denied display names)** — the connector CLOSURE below stays exempt (gating connectors would silently break allowed joins) — expands through the connector relations graph (transitive, undirected, capped; a live connector is never added nor walked through — frozen into the session meta so later admin edits never silently change an existing chat), REPLACES any previous selection, and writes meta-only entries built from the registry (no brain call, no snapshot read). The wizard calls it between `/upload` and `/schema_autofill_full`; `/upload` defensively preserves DB entries across its session reset. |

`/schema_autofill_full`, `/generate_chatdata` and `/add_data_to_chat` accept a
DB-only session (their "no dataframes → 400" guards also count DB selections);
autofill **skips** database entries (descriptions are ladmin-confirmed — never
overwritten). `/add_data_to_chat` merges DB selections by `table_id`: an
already-present table keeps the chat's (possibly user-edited) descriptions via
the shared `merge_schema_entry` carry-over; new tables append with a df key
deduped against the chat's existing keys. Frontend: in ADD mode the picker
dropdown renders tables already in the target chat **checked + disabled** with
an "Already in this chat" note (table_ids from a wizard-open `/schema` fetch)
— disabled deliberately, since the merge is add/update-only and unchecking
could not remove; merge semantics unchanged.

`GET /api/chat/{chat_id}/schema` additionally returns (additive — file-only
chats get `db_tables: []`, `data_as_of: null`):

```jsonc
{
  "db_tables": [{"df_key", "table_id", "display_name", "is_connector",
                  "auto_included", "row_count", "refreshed_at", "missing",
                  "allowed", "live"}],
  "data_as_of": "2026-07-27T00:00:12+00:00"   // MIN refreshed_at — oldest data in the chat
}
```

The `/lab` topbar shows "Data as of <refreshed_at>" from this. A `missing`
table (unregistered / snapshot gone) is skipped by `load_dataframes` and the
existing refresh key-freeze disables items that referenced its key. `live`
(additive) is true for a table in live mode: such a row reports
`refreshed_at: null` (it is queried at question time, there is no "as of"),
never feeds `data_as_of`, and is never `missing` for lacking a parquet. `allowed`
(additive) is the requester's ROLE verdict per table (connectors always
`true`; a role-probe failure reports `true` — the server-side refresh gate is
the enforcement): dashboard.js / dashboard_view.js pre-freeze refresh buttons
whose code references an `allowed:false` table with a role tooltip instead of
letting the click fail.

`GET /api/chat/{chat_id}/schema` also returns an additive `is_owner` (bool —
`true` only for the chat's recorded owner, fails closed to `false`). The
owner-only mutations — `POST /api/chat/{id}/schema` (descriptions),
`POST /add_data_to_chat`, `POST /api/chat/{id}/share` and
`POST /api/chat/{id}/auto_analysis/start` — answer a share recipient
`403 {"error": "Access denied"}`; when `is_owner` is `false` the `/lab` page
hides View / Edit Descriptions, Add Data and Auto Analytics instead of letting
them fail.

### Admin routes — `/api/admin/*` (ladmin + scoped POWER USERS)

Two guards (prompt 19):

- **`_require_admin`** (ladmin only — `role == "admin"`): connection lifecycle
  (`POST /connections`, `/connections/test`, `/connections/{cid}`,
  `/connections/{cid}/delete`, `/connections/{cid}/refresh`), the GLOBAL
  refresh schedule (`GET/POST /refresh_settings`), `GET /audit`, and all of
  `routes/admin_users.py`.
- **`_require_source_manager`** (everything else): any admin permission
  passes unrestricted (the bootstrap ladmin and 19g promoted admins alike);
  a user whose per-account PERMISSION is `"power"` (19e — AuthStore profile
  `role`, set via `POST /api/admin/users/set_permission`) gets the UNION of
  `manage_grants` across ALL their held roles as their **management scope**
  (19f — the SEPARATE management axis; `scope_grants` are the read axis and
  no longer contribute, and explicit `table_ids` grant READ access only,
  never management). Semantics for a power user:
  - Every referenced physical table (connection, schema) must fall inside the
    scope — schema match case-insensitive, a `schema:null` grant covers the
    whole connection — else `403 {"code": "OUT_OF_SCOPE"}` (scope rejections
    are ordinary validation, not audited as denials).
  - List responses are FILTERED to the scope: `GET /connections` (only
    granted connections, table counts over manageable tables),
    `GET /connections/{cid}/schemas` (schema-level grants narrow the listing;
    any whole-connection grant keeps it full), `GET /tables` (manageable
    tables, CONNECTORS INCLUDED), relations scan/graph/recommendations
    (candidates, recs and the confirmed count computed over the scoped set).
    The recommendation WRITE is scope-bounded too: `analyze_sql`'s and
    `scan`'s "Recommended tables" upserts drop any resolved physical
    (connection, schema) outside the scope — an out-of-scope row must never
    exist, not merely be hidden.
  - `save_table`: the posted physical AND (on edit) the existing doc's
    physical must be in scope; `access_role_ids` (19f "Share with your
    roles") is accepted but must be a SUBSET of the power user's held roles
    MINUS the built-in Base (everyone is a member — publishing to the whole
    platform stays ladmin's; `my_roles` never offers it) — an outside id is
    `403 {"code": "ROLE_NOT_HELD"}` validated UP-FRONT, before anything is
    registered or snapshotted (never a silent drop). The power user's
    reconcile is limited to that held subset: roles they do NOT hold keep
    their ladmin-granted membership (ladmin's reconcile stays exact). A
    fresh registration with the panel untouched is visible only to the
    registerer via the ownership read. The posted `relations` array is
    scope-checked too: a relation the power user ADDS or CHANGES whose related
    table is outside their management scope → `403 OUT_OF_SCOPE`; an
    unchanged copy of a stored out-of-scope relation passes, and a stored
    out-of-scope relation the post omits is KEPT (they can neither add nor
    remove it). Everything else — confirm lock, drift check, duplicate check,
    snapshot — is unchanged.
  - `POST /tables/{tid}/delete`: in scope AND `registered_by == <the power
    user>` — else `403 OUT_OF_SCOPE` / `403 {"code": "NOT_OWNER"}` (absent
    `registered_by` = ladmin-registered/legacy → NOT_OWNER). Ladmin deletes
    anything, unchanged.
  - `/relations/accept|delete|dismiss`: EVERY referenced side (child AND
    parent/related) must be manageable — on `accept` this includes the
    related table of a `replaces` entry (the relation being replaced is
    removed, so it must be in scope as well; else `403 OUT_OF_SCOPE`);
    recommendation status/classify/accept
    check the rec's (connection, schema); `accept_recommendation` registers
    with `registered_by` = the power user.
  - Every power-user WRITE's audit row carries `actor_kind: "power_user"` in
    its detail (threaded through the store/scheduler mutators too:
    `table.save/delete/refresh/schedule/mode/drift_dismiss`, `relations.*`);
    ladmin rows stay byte-identical.

Table docs carry **`registered_by`** (ownership): stamped from the SESSION
identity at FIRST save only (wizard save and recommendation Accept, via
`_build_table_doc`), carried through every edit-save like the schedule
override — an edit never changes or introduces it; docs written before the
field exist without it and read as ladmin-registered.

Both guards: 401 unauthenticated; 403 while `must_change_password` is
pending; 403 + one `admin.denied` audit row per denial (the source-manager
guard adds detail `{"reason": "not_power_user"}`). Connectivity/introspection
failures return `200 {ok:false, error}` (the dashboards idiom). Every
response masks credentials (`password_set`/`password_readable`/
`password_masked` — never `password_enc` or `url_override`). Every admin
action appends to the append-only audit JSONL `DATA_ROOT/admin_audit.jsonl`
(secrets scrubbed).

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/api/admin/dialects` | dialect registry for the UI (`postgresql`, `mysql`, `mariadb`, `mssql`, `oracle`, `clickhouse`) with per-dialect `{available, unavailable_reason}` (e.g. msodbcsql18 not installed), plus `plaintext_port` (the port the server speaks WITHOUT TLS where it differs from `default_port`, else `null`) and `ssl_default` (the form pre-ticks SSL / Encrypt for a NEW connection). ClickHouse: `default_port` 9440 (TLS), `plaintext_port` 9000, `ssl_default: true`; a stored connection with no port resolves to 9000 unless SSL is on, and the form warns when port 9000 is entered or SSL is off. The connection form is built from this response alone — `label`, `default_port`, `needs`, `available` — so a new dialect needs no UI change unless it needs a form field beyond database / service_name |
| `GET/POST` | `/api/admin/connections` | list (masked) / create. Create requires `CLIENT_ENCRYPTION_KEY` (else 503 — passwords are Fernet-encrypted at rest, never plaintext) |
| `POST` | `/api/admin/connections/test` | SELECT-1 probe with a short connect timeout. Accepts `{connection_id}` OR a full unsaved draft incl. `password` (Test-before-Save). `{ok, error?, server_version?, elapsed_ms}` |
| `POST` | `/api/admin/connections/{cid}` | edit; omitted/empty `password` keeps the stored credential |
| `POST` | `/api/admin/connections/{cid}/delete` | `409 {tables:[…]}` while registered tables reference it; `{cascade:true}` deletes those tables + snapshots too. Best-effort prunes the connection's scope grants + cascaded table ids from every role (`roles_store.remove_connection`) |
| `GET` | `/api/admin/connections/{cid}/schemas`, `…/tables?schema=` | live introspection listings; already-registered tables carry `registered`, `table_id`, and `registered_as` (the registration's display name — the wizard disables those rows: a physical table registers only once) |
| `POST` | `/api/admin/connections/{cid}/refresh` | Refresh-now for every table on the connection (sequential). A LIVE table takes no snapshot here: its result row is `{table_id, display_name, ok: true, rows: null, error: null, skipped: "live"}` (its profile is refreshed by the per-table Refresh now) |
| `POST` | `/api/admin/tables/introspect` | columns+dtypes / PK / FK / indexes / comment via SQLAlchemy Inspector + catalog-estimate row count & size (degraded gracefully on missing catalog privileges; the Inspector step never runs `COUNT(*)`, the one bounded count belongs to `size_verdict` below) + first-rows preview (admin's browser only). v4.2: additive `classification:{suggested_type, reason}` from the columns already in hand (no extra round-trip) — the wizard pre-ticks its connector box from it instead of assuming every recommended table is a connector; an explicit prefill and a registered table's stored type both win over it. Additive identifier-case keys: per-column `quote: true` and top-level `schema_quote`/`table_quote` (present only when the live catalog marks the name PHYSICALLY case-sensitive — created quoted; Oracle-class dialects) plus per-column `type_info {py, precision, scale, timezone}` (feeds the snapshot's canonical Arrow schema). The browser never echoes any of these back — `save_table` re-derives them from its own fresh introspection. Additive `size_verdict: {row_count, count_source: "count"\|"estimate"\|"cap"\|null, timed_out, cell_count, live_suggested, live_required}` for the storage choice: an exact `COUNT(*)` (with the posted `where_filter`) under the connection's statement timeout capped at 60 s. A count that TIMES OUT is treated as above the force threshold (`live_required: true`, `row_count: null`). Any other count failure falls back to the catalog estimate. Both missing ⇒ unknown (`cell_count: null`, no suggestion, no refusal). The wizard's Storage block pre-ticks Live and disables Snapshot on a required verdict |
| `POST` | `/api/admin/tables/draft_descriptions` | AI-drafted ENGLISH table+column descriptions via the existing `brain_client.schema_autofill` (same truncated sampled hints files send; payload carries no host/user/password/connection id). **Persists nothing** — one of the four mandatory-confirm locks |
| `GET` | `/api/admin/my_roles` | 19f — the CALLER's held roles (`{roles:[{id, name, is_base, table_ids, scope_grants}]}`), what the power-mode wizard's "Share with your roles" panel offers and locks against. `_require_source_manager` (power users allowed); held roles only, never the whole registry (that stays ladmin's `GET /roles`), and the built-in Base is EXCLUDED even when held — sharing with everyone is an administrator action (save_table refuses `"base"` too) |
| `GET/POST` | `/api/admin/tables`, `POST /api/admin/tables/{tid}` | list / register / edit + snapshot. Optional body field `access_role_ids: [role ids]` (the wizard step-3 **Access** panel; 19f: rendered for POWER users too as "Share with your roles" — held roles only, all unchecked by default, server-enforced subset `403 ROLE_NOT_HELD` before anything is registered): after the save, the table id is reconciled into exactly those roles' `table_ids` via `roles_store.set_table_roles` (canonical storage on the ROLE record — the table doc never carries access; scope-covered roles are checked+disabled in the UI and excluded from the list; power-user reconciles audited with `actor_kind`). Field ABSENT ⇒ no role writes (recommendation-Accept + pre-feature payloads); a role write failure never fails the save. **One registration per physical table** (connection + schema + table, case-insensitive): a save creating a physical mapping another registration already covers → `400 DUPLICATE_TABLE` naming it; an edit that keeps its stored physical key always passes — including on a LEGACY duplicate (stored duplicates keep loading and working; only NEW saves are blocked, connector-vs-normal is toggled on the existing registration instead). **Mandatory confirm**: `confirm:true` required (`400 CONFIRM_REQUIRED`); `descriptions_confirmed_by/at` stamped from the session + server clock, never the body; a fresh introspection must match the posted column set (`409 SCHEMA_DRIFT`). Snapshot failure keeps the registration saved with `last_refresh_error` (Refresh retries); success = chunked SELECT → parquet, atomic `os.replace`. **Storage mode**: optional body `mode: "snapshot"\|"live"` (absent ⇒ the existing registration's mode, `"snapshot"` for a new one; anything else → `400 {code:"BAD_MODE"}`). After the fresh introspection the rows are counted again with the filter and cap the saved doc will carry (the introspect `size_verdict` rules; a known count is taken at most at the `row_cap`; a timeout stays "required" unless that cap x the column count stays below the force threshold, which reads as `count_source: "cap"` with the cap as `row_count`). Every save response carries the verdict as an additive `size_verdict` (new registrations and edits alike). A NEW registration saved as a SNAPSHOT at or above the force threshold — or an edit whose body posts a `mode` different from the stored one, as snapshot — is refused BEFORE anything is written: `400 {error, code:"LIVE_REQUIRED"}`. Any other edit never changes the stored mode: the verdict is advisory only (a count timeout included) and the save proceeds. `where_filter` / `row_cap` ABSENT from the body keep the stored values; an explicit `null` clears them. `live_reason` (`threshold` when the size suggested live, else `manual`), `live_set_by` (session) and `live_set_at` (server clock) are SERVER-stamped when a save turns a table live; posted values are ignored. An edit-save carries `mode` and these keys through; a save as snapshot removes them. A mode change writes a `table.mode` audit row. A snapshot save answers `{table, snapshot, size_verdict}`. A LIVE save takes no snapshot and answers `{table, snapshot: null, live_profile, size_verdict}`, where `live_profile` is `{ok, rows, sample_rows, profiled_at}` or `{ok: false, error}`: a sample of at most 10 000 rows (capped by `row_cap`; the first rows the database returns, bounded at 60 s) is profiled locally with the counted rows as the true size, the profile sidecar and the columns' technical descriptions are written, and `refreshed_at` is not. A profiling failure keeps the registration. Rows of `GET /api/admin/tables` gain additive `mode`, `cell_count`, `live_suggested` and `live_required`, derived on every read from the STORED row count x column count (`null` / `false` when the row count is unknown); live rows also carry `live_reason`, `live_set_by`, `live_set_at`, `live_profiled_at` and `live_sample_rows` |
| `POST` | `/api/admin/tables/{tid}/refresh` | Refresh-now (same `db_scheduler.refresh_one_table` the nightly run uses). Returns `{ok, rows, bytes, refreshed_at, drift:{added,removed}}`; on schema drift every chat meta referencing the table is re-synced with the `_resync_meta_after_add` carry-over rules (user edits survive, vanished columns deleted). A failed refresh keeps the previous snapshot AND `refreshed_at` — chats serve the last good data. On a LIVE table no parquet is written: the rows are re-counted and a fresh sample is re-profiled, answering `{ok, live_profile, rows, error}`. Scheduled refreshes skip live tables entirely (`LIVE_SKIP`) |
| `POST` | `/api/admin/tables/{tid}/mode` | Switch a registered table between storage modes. Body `{mode: "snapshot"\|"live"}`. Guard `_require_source_manager`: administrators unrestricted, a power user only for a table inside their management scope (not owner-only: the mode is operational, like the schedule override). Scoped power user and a table outside the scope or unknown → `403 {code:"OUT_OF_SCOPE"}`. Administrator and unknown table → `404 {error: "Unknown table."}`. Mode outside the two values → `400 {code:"BAD_MODE"}`. The SAME mode as stored → `200 {ok: true, table}`, nothing written, no audit row. **To live**: `live_reason` (`threshold` when the stored size suggests live, else `manual`), `live_set_by` and `live_set_at` are stamped, any existing parquet is KEPT (no update deletes state), and the table is re-counted and profiled from a sample → `200 {ok: true, table, live_profile}`. **To snapshot**: the rows are counted again (the doc's `where_filter` and `row_cap`, the introspect `size_verdict` rules; a count failure other than a timeout falls back to the stored row count) and the switch is refused with `400 {error, code:"LIVE_REQUIRED"}`, nothing written, when the result is at or above the force threshold — a count timeout counts as required. Otherwise all live keys (`live_reason`, `live_set_by`, `live_set_at`, `live_profiled_at`, `live_sample_rows`) are cleared and a fresh full snapshot is ALWAYS taken, even when an older parquet exists (a parquet from before the live period would otherwise be served as current data) → `200 {ok: true, table, snapshot}`. When that snapshot fails the table is set back to live (keeping its previous `live_reason`; the profile stamps `live_profiled_at` / `live_sample_rows` the flip cleared are restored from the pre-flip document, so a reverted row is not an unprofiled one) and the answer is `200 {ok: true, table, snapshot, reverted: true}` with the failed `snapshot` object; if the table vanished while the snapshot ran there is nothing to revert → `404 {error: "Unknown table."}`. Audited `table.mode` `{from, to, reason, cell_count}` |
| `POST` | `/api/admin/tables/{tid}/delete` | unregister (+ delete the snapshot by default). Best-effort prunes the id from every role's `table_ids` (a stale id grants nothing — effective access intersects the live registry — but would clutter the roles UI) |
| `POST` | `/api/admin/relations/scan` | **relation discovery** (proposals only — nothing is applied without an explicit accept): live-introspects every registered table for declared FKs (FKs are not persisted in the registry; an unreachable connection lands in `degraded[]` and only skips FK evidence) + deterministic name/description-similarity candidates (ubiquity down-weighted, no LLM), verifies each candidate against the local snapshot parquets (cardinality with direction normalization, overlap %, orphan count; missing snapshot → `unverified`), excludes already-declared relations (either orientation), and bands `confirmed` / `suggested` / `attention`. PHYSICAL-IDENTITY PRECISION: candidates joining two registrations of ONE physical source (connection + schema + table) are never proposed; a relation confirmed to ANY registration of a physical target suppresses re-proposals to its duplicates; duplicate-registration fan-out collapses to ONE candidate targeting the preferred registration (connector first, then earliest-registered) carrying `alternate_targets:[{id,label}]`. Returns `{ok, candidates:[…], degraded:[…], confirmed_count, unregistered_refs:[{connection_id, schema, table, referenced_by, referenced_by_ids}]}` (`confirmed_count` = total confirmed relation entries, explains a zero-candidate scan — `analyze_sql` returns it too; `unregistered_refs` = FKs on registered tables pointing at UNREGISTERED physical tables, connection-scoped, each entry additively carrying `referenced_pairs` aligned with `referenced_by_ids` — missing-table column first, empty on a malformed FK, rendered with a "Register as connector" wizard-prefill shortcut) — aggregates only, never values. v4: the scan also UPSERTS these refs into the persistent "Recommended tables" (source `fk`), and replays the stored SQL evidence of `registered`-status recommendations through the same candidate pipeline (merge → verify → band), so a table registered via the wizard gets its SQL-derived relations proposed on the next scan without re-pasting anything. v4.1: replayed evidence passes `validate_rec_evidence` first — pairs naming columns the now-known registration lacks are excluded (never a bogus candidate; this is also how corrupted/stale stores are handled, no migration) and surfaced in the additive response key `evidence_warnings:[{table, column, source:"sql-evidence"}]` + a `REL_REPLAY_INVALID_COLUMN` log line per pair |
| `POST` | `/api/admin/relations/analyze_sql` | `{sql, db_type?}` — extracts join candidates from admin-pasted SELECT statements with sqlglot (aliases/CTEs/subqueries resolved; composite ON predicates → one multi-column candidate; literal predicates dropped; pair frequency counted across distinct statements). **The SQL TEXT is parsed in memory on this client only — never persisted, logged, audited, or sent to the brain**; the audit rows and any error text carry counts only (sqlglot messages embed the SQL and never leave the parser). v4: only table/column IDENTIFIERS extracted from it persist — predicates touching UNREGISTERED tables leave anchored evidence in `stats.unregistered_joins` (`{name, other:{table_id|name}, pairs [missing-table column first], count}`, per-statement-deduped, ≤20 distinct tables per analyze) plus per-table counts in `stats.unregistered_tables`, and evidence whose name resolves to ONE connection (the hint rule) is upserted into the persistent "Recommended tables" (source `sql`; response gains `recommendations:{created,updated}`). v4.1 — wrong SQL is a first-class case, never a silent drop: any predicate side resolving to a REGISTERED table has its column validated (case-insensitively — sqlglot's qualify normalizes identifier case on the happy path, the fallback path preserves raw casing) against registry metadata; an invalid pair is skipped (candidate AND evidence — valid pairs of the same statement are kept) and reported in additive `stats.invalid_column_refs:[{statement (1-based over the ANALYZED statements — the salvage parser drops unparseable chunks), table, column (SQL-side spelling)}]` (deduped, capped 20) which the UI renders with "the script may be outdated or wrong — treat its evidence with caution" wording. Additive `stats.unresolved_predicates` counts predicates whose column reference cannot be resolved at all (computed CTE/subquery projections, unqualified column refs — a KNOWN LIMITATION: such joins contribute no evidence; the counter makes the drop visible, semantics unchanged). Join endpoints resolving to duplicate registrations of one physical table pick the preferred registration; endpoints spanning DIFFERENT physical tables (same name, two connections) stay ambiguous and are dropped; endpoints matching NO registration are reported in `stats.unknown_tables` (names from the admin's own SQL, response-only — the UI explains a zero-candidate run with them). The same physical-identity filters/dedupe as scan apply. Returns candidates + `stats {statements, parsed, failed, unknown_tables}` + `unknown_table_hints:[{name, connection_id, schema, table}]` (register-shortcut hints, offered only when the connection is unambiguous — one connection exists, or exactly one connection's registered tables share the schema — and never for names that already match a registered table) |
| `POST` | `/api/admin/relations/accept` | `{relations:[{table_id, related_table_id, join_keys, cardinality?, origin?, replaces?}]}` (bulk = same endpoint) — writes accepted candidates into the child table's `relations` in `data_sources.json` using the extended format. Validates ids/columns/cardinality/origin (400), skips duplicates in either orientation (`skipped` count), groups items per child table into ONE read-modify-write. v4.1: a join key naming a nonexistent column is rejected with a readable per-side message naming the table and the column ("Column 'city_code' does not exist on 'tr data'.") — the v1-era message fused the pair into a single 'a=b' token and leaked `relations[N]` internals; semantics (400 + ok:false audit) unchanged. `replaces: {related_table_id|related_table, join_keys}` is the overview **Edit** path: the matching old entry is swapped for the new one in the same write (`replaced` count in the response/audit); an edit that would duplicate a DIFFERENT existing entry is skipped WITH the old entry preserved (never a silent delete); a stale `replaces` degrades to a plain accept. Deliberately **no confirm gate / SCHEMA_DRIFT / re-snapshot** — those locks protect the column+description shape, which this endpoint cannot touch. NOTE: the read-modify-write is not atomic vs a concurrent nightly refresh of the same table doc (same exposure class as save) |
| `POST` | `/api/admin/relations/delete` | remove a confirmed relation from the owning (child) table's doc: `{table_id, related_table_id\|related_table, join_keys}` — matches by related ref (id or legacy name) + ORDERED join_keys and removes EVERY exact match (identical duplicates are indistinguishable in the overview). 400 bad input, 404 unknown table / no match; `{ok, removed}`; audited (`relations.delete`). Distinct from dismiss, which never mutates |
| `POST` | `/api/admin/relations/dismiss` | audit-only (`relations.dismiss`); dismissals are session-local by design — nothing persisted, candidate may reappear on the next scan |
| `POST` | `/api/admin/relations/graph` | graph-view data (read-only, nothing written): registered tables as nodes `{id, label, sub(schema.table), connector, relation_count, component, isolated, ghost}` and confirmed relations as child→parent edges `{keys_label, cardinality, origin, suspicious, join_keys, related_ref}` — connected components via BFS, legacy name refs resolved to the preferred registration (dangling refs skipped + logged), per-pair key coercion. v4: ghost nodes come from the server-persisted OPEN recommendations (dismissed never render), unioned by physical key with any body-passed `unregistered_refs` (the pre-v4 param stays accepted; the v4 frontend no longer sends it); recommendation evidence renders as dashed edges — SQL evidence ghost→partner with real key labels (mirror-deduped, ghost↔ghost supported), FK evidence as the classic child→ghost edges deduped against `referenced_by_ids`. v4.3 adds three ADDITIVE edge fields for ER rendering: `label` (the join COLUMNS only — a same-named pair collapses to one name, composites comma-joined, ghost evidence falls back to its `keys_label` caption), and `source_marker`/`target_marker` (`"one"|"many"|null`) derived from `cardinality` — source is the CHILD, so `N:1` is `("many","one")`; unknown cardinality (every ghost edge) yields `(null, null)`. `keys_label` and every pre-v4.3 key are unchanged. Rendered as an ER diagram (table cards, cardinality at the line ends, layered left-to-right layout) with vendored **Cytoscape.js 3.34.0 + dagre 0.8.5 + cytoscape-dagre 2.5.0 (all MIT, under `static/vendor/`)** — never a CDN (LAN-only clients); a dagre load/run failure degrades to the built-in `breadthfirst` layout rather than losing the graph; metadata only, never values |
| `GET` | `/api/admin/relations/recommendations` | v4 — the persistent **"Recommended tables"** (`data_sources.json` top-level `recommendations`; identifiers + counts ONLY, never SQL text/literals/values): one entry per unregistered physical table with accumulated evidence from pasted SQL and/or live FK introspection. Read-time enrichment: `role` (`bridge` = evidence joins ≥2 DIFFERENT REGISTERED physical tables in the CURRENT registry, else `referenced` — computed, never stored; unregistered partners deliberately don't count) + the FULL partner list `joins:[{label, registered, cols, origins}]` — v4.1: unresolved partners are listed with `registered:false` (a "not registered" tag in the UI) instead of being dropped, so a row is never frequency-only — + the locked `pending:[{left, right, blocked_by}]` preview of relations that WILL be proposed once the blocking table(s) register (SQL-origin evidence only — replay never proposes FK evidence; rendered 🔒 non-interactive inside the row) + per-rec `evidence_warnings` from replay validation (invalid pairs are excluded from joins/pending and flagged with a warn chip). All computed at read time from stored identifiers; nothing new persisted. Sorted bridge-first, then by accumulated frequency. Dismissed entries included (flagged by `status`) for the show/restore affordance |
| `POST` | `/api/admin/relations/recommendations/status` | `{id, status: open\|dismissed}` — **persistent** dismiss/restore (unlike candidate dismiss, which stays session-local by design). A dismissed recommendation keeps accumulating evidence but never reappears until restored; `registered` entries are immutable here (404) — the registry owns them via the store's reconcile hook (any registration path flips a matching rec to `registered` remembering `prior_status`; a deleted registration reverts it to `prior_status` — a dismissed rec can never resurrect; a deleted connection drops its recs) |
| `POST` | `/api/admin/relations/recommendations/classify` | v4.2 — `{id}` → `{ok, classified, suggested_type: connector\|normal, reason}`. Metadata-only probe backing the accept dialog's type default: ONE bounded introspection (column names + dtypes) through the pure `relation_discovery.classify_table_type`. **No brain call, no data values, no writes, no audit row.** A SUGGESTION only — the admin confirms or flips it, and the registration uses their pick. Any dependency failure degrades to `{classified:false, suggested_type:"connector", reason:"could not classify — defaulted to connector"}` so classification can never block Accept (the real error surfaces on the accept attempt); `404` only for an unknown recommendation |
| `POST` | `/api/admin/relations/recommendations/accept` | `{id, chosen_type?, suggested_type?}` — **one-click registration** as the type the admin picked in the dialog (v4.2; `chosen_type` defaults to `connector` = the pre-v4.2 behavior, anything outside `connector\|normal` → 400. `suggested_type` is audit metadata only — enum-validated, garbage coerced to null, deliberately NOT recomputed server-side: it cannot change what gets registered and would cost a second introspection). Flow: introspect → AI-draft descriptions (the SAME `_draft_table_descriptions` mechanism as `draft_descriptions`; the UI dialog is the review act — it states descriptions are AI-drafted and editable later; `descriptions_confirmed_by/at` stamped from the session) → register with the SAME `_build_table_doc` shape as the wizard save (humanized display name = `table.lower().replace('_',' ')`, `is_connector` = the chosen type) → snapshot via `refresh_one_table`. **Every dependency is time-bounded** (v4.2): the draft call carries `settings.BRAIN_DRAFT_TIMEOUT` (60s) and a stall returns `{ok:false}` naming it ("The AI description service did not respond within 60s. Use “Edit first”…"), while DB stalls surface as "Database connection timed out." / "Database snapshot timed out." — a hung Accept used to leave the page spinning with no error and no log line. `REC_ACCEPT_PHASE phase=introspect\|count\|draft\|register\|snapshot` is logged on ENTRY to each phase (completion-only logging is why the original hang was invisible), `REC_ACCEPT_DONE ok= elapsed_s=` after. The audit row carries `suggested_type` + `chosen_type` (so "the tool was wrong" is distinguishable from "the admin chose"). **Snapshot failure rolls the registration back** (`delete_table`; the reconcile hook reopens the rec) — unlike the wizard save, Accept never leaves a half-registered state; connectivity-style failures → `200 {ok:false}`. Success replays the rec's stored SQL evidence through the NORMAL candidate pipeline (same filter chain + verify + band as `analyze_sql`; FK evidence is never replayed — the next scan re-derives it live, and replayed candidates never carry `fk` so banding's fk-auto-confirm cannot fire) and returns `{ok, table, snapshot, candidates}` plus (v4.1) additive `evidence_warnings` for stored pairs the replay validator excluded — relations are still PROPOSED, never auto-confirmed. Already-registered race → `{ok:true, status:"registered", note}` after a status sync. A recommendation never registers live: after the introspection a `count` phase sizes the table, and one at or above the force threshold is refused with `400 {ok:false, error, code:"LIVE_REQUIRED"}` before the draft call and before anything is written (register it from the wizard in live mode) |
| `POST` | `/api/admin/relations/wizard_suggest` | relation suggestions for the register wizard's relations step: `{editing_tid?, connection_id?, schema, table_name, display_name?, columns:[{name,pk?,description?}], foreign_keys:[introspection fk dicts], relations:[current wizard rows], sample:{columns,rows}?}` (`connection_id` feeds the physical-identity filters — same-physical exclusion and duplicate-registration dedupe apply exactly as in scan) — everything comes from state the wizard already holds; **no live DB access, nothing written**. FK candidates (`precheck:true`, cardinality `N:1` unless the parent snapshot measured non-unique — FK direction is ground truth, and referred tables resolve across ALL connections) + name/description similarity (`precheck:false`); duplicates vs stored AND just-typed relations excluded in either orientation. Verification: parent side from its snapshot parquet, child side ESTIMATED from the preview sample (`estimated:true`; overlap = share of sample keys found in the parent snapshot; a candidate whose measured direction had to be flipped drops its numbers rather than show a misleading estimate; missing snapshot/sample → `unverified`). Orientation is always normalized so the WIZARD table is the stored child. Sample row values never reach the audit — counts only (Article II) |
| `GET/POST` | `/api/admin/refresh_settings` | flexible refresh schedule (Prompt 13). Response: `{schedule: {mode: daily\|weekly\|monthly\|interval\|cron, time "HH:MM", weekdays [0-6 cron convention], monthly_days [1-28\|"last"], every_minutes (≥15; <60 or whole hours), cron "5-field", enabled}, description, next_run_at (computed), last_fired_at, last_run_at, last_run_summary, refresh_enabled+refresh_time (legacy mirrors)}`. POST accepts BOTH `{schedule: {...}}` and the legacy `{refresh_time, refresh_enabled}` pair (mapped to daily); validation → 400 `{error, code: "BAD_SCHEDULE"}` with an admin-readable message. Times stay container-local naive; all modes normalize to canonical 5-field cron (croniter; monthly "last" = a separate `l` cron). Days 29-31 rejected on purpose (they skip shorter months) |
| `POST` | `/api/admin/schedule_preview` | validate a schedule draft, no writes: `{ok, schedule, crons, description, next_runs[3]}`; 400 `BAD_SCHEDULE` |
| `POST` | `/api/admin/tables/{tid}/schedule` | per-table override, same schedule shape; `{"schedule": null}` = inherit global (default). Overridden tables get their own due-checks in the scheduler loop and are excluded from the global run — never refreshed twice for one due moment. Wizard edit-saves carry the override through (`_build_table_doc existing=`). 404 unknown table, 400 `BAD_SCHEDULE` |
| `POST` | `/api/admin/tables/{tid}/dismiss_drift` | acknowledge the schema-drift banner (audited `table.drift_dismiss`); 404 when no drift recorded. Smart refresh (Part C): scheduled runs fingerprint the source first (one SQL aggregate — COUNT/SUM/AVG/MAX + schema hash, WHERE/row-cap respected) and SKIP unchanged tables (`DB_REFRESH_UNCHANGED`, `skipped:true` in run results, profile untouched); `/tables/{tid}/refresh` and all admin-initiated paths always force a full snapshot. Table rows now carry `last_fingerprint`, `last_checked_at`, `last_drift {added, removed, retyped[{col,from,to}], at, dismissed}` (dtype drift detected against live introspection; registry dtypes refreshed each full run); `last_run_summary` results gain `skipped`/`drift`. See docs/DB_TABLES_PLAN.md for the fingerprint design + accepted SUM/AVG limitation and the apply+notify drift policy |
| `GET` | `/api/admin/audit?limit=` | newest-first tail of `admin_audit.jsonl` |

### Admin routes — Users & Roles (`routes/admin_users.py`, same `/api/admin` prefix + guard)

User management for the DB-table role gate + the per-user permission. A user
holds SEVERAL roles (19c: `data_roles` list on `users/{email}/profile.json`;
the legacy single `data_role` is mirrored to the first id on write and still
read; empty ⇒ the built-in **Base** role, id `"base"`). READ access is the
UNION across held roles. Roles live in `DATA_ROOT/roles.json`
(`roles_store.RolesStore`) as `{id, name, description, table_ids:[],
scope_grants:[{connection_id, schema|null}], manage_grants:[same shape]}` —
`schema:null` = the whole connection, grants cover tables **registered
later** too (effective access is computed per request, never frozen). 19f
two-axis model: `scope_grants` = READ ("all current and future tables
here", an explicit ladmin opt-in), `manage_grants` = MANAGEMENT (where
power-permission members register tables/relations/schedules — never read);
the doc is versioned and a v1 store migrates once at boot (scope copied into
manage, `migrate_manage_grants`). `allowed_table_ids_for` additionally
includes the user's own registrations (`registered_by`, the ownership read).
The POWER-USER CAPABILITY is the per-user PERMISSION (`profile.role`:
"user"/"power"/"admin"); the MANAGEMENT scope on `/power/data_sources` is
the union of `manage_grants` across ALL held roles (see the admin-routes
guard section). The 19c-era `power_user` role flag is dropped silently on
read and the once-seeded built-in "poweruser" role is removed at boot.
Deleting a role drops it from its holders DYNAMICALLY — dangling ids are
skipped at read time, no profile rewrites. Emails are always body-carried
(never path params); role ids are path params.

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/api/admin/users` | Everyone with a readable profile, sorted by email: `{users:[{email, permission ("standard"\|"power"\|"admin"), role_ids (RESOLVED held list — dangling dropped, empty→["base"]), role_names, role_id/role_name (legacy = first entry), created_at, last_login_at}]}`. Only the bootstrap local-admin account is excluded — admin-permission users ARE listed (19e: they must stay demotable; their held roles stay visible/stored but inert). `member_count` on roles counts holders among these rows. `last_login_at` is stamped by `_start_session` on every login; `created_at` = when the account was created (invite, share, SSO or, on the demo, first sign-in) |
| `POST` | `/api/admin/users/invite` | ladmin only. `{email}` → `200 {ok, email, created, mail_sent[, mail_error]}`: creates a password-less account when the address has none (`created: true`; an existing password-less account is re-invited, `created: false`), mints a reset token and mails the link (`<PUBLIC_BASE_URL>/auth/reset/<token>`) through the brain, waiting up to `BRAIN_DRAFT_TIMEOUT`; with `PUBLIC_BASE_URL` unset the account is still created and the answer is `mail_sent: false` with that reason. A failed mail discards the token and answers `mail_sent: false` with the reason; the account stays. 400 invalid address / the bootstrap account; 409 `{code: "USER_EXISTS"}` when the account already has a password; 409 `{code: "SSO_ACCOUNT"}` for a Microsoft-only account. The mail is sent with `kind: "invite"` (invitation wording). Audited `user.invite` with `{created, mail_sent}` — never the link. Not attempt-limited (admin-guarded) |
| `POST` | `/api/admin/users/set_role` | Sets the user's HELD ROLE LIST (19c): `{email, role_ids: [...]}` → `{ok, user}`; the legacy `{email, role_id}` shape is still accepted (→ one-element list); empty list reverts to Base. 400 missing email / the bootstrap ladmin account / any unknown role id (19g: PROMOTED admins take roles like anyone — only the bootstrap identity is refused); 404 unknown user. Audited `user.set_roles` with `{role_ids, role_names}` |
| `POST` | `/api/admin/users/set_permission` | 19e — sets the per-user PERMISSION: `{email, permission: "standard"\|"power"\|"admin"}` ("standard" stored as "user") → `{ok, user}`. 400 missing email / invalid value / the bootstrap ladmin account / the CALLER's own account (no self-demotion); 404 unknown user. Never touches `data_roles` — and since 19g a promoted admin's roles stay ACTIVE (full analysis user), so promote/demote round-trips are lossless. Audited `user.set_permission` with `{old, new}` |
| `GET` | `/api/admin/roles` | `{roles:[{…, is_base, is_builtin, member_count}]}` — Base first, the rest by name; `member_count` counts users HOLDING the role (a user with 3 roles counts in all 3) |
| `POST` | `/api/admin/roles` | create: `{name, description?, table_ids?, scope_grants?, manage_grants?}` → 201 `{role}` (a stray 19c-era `power_user` key is silently ignored). 400: empty/duplicate name (case-insensitive, "base" reserved), unknown table id, **connector table id** (exempt from role checks — ungrantable), unknown connection / malformed entry in EITHER grant list (`manage_grants` validate exactly like `scope_grants`). Audited `role.create` (grant + manage counts) |
| `POST` | `/api/admin/roles/{rid}` | edit (fields present ⇒ replace, absent ⇒ keep — `scope_grants` and `manage_grants` replace independently). 404 unknown; 400 renaming Base — but a RESTATED IDENTICAL name is ignored, not a 400 (19c fix: the UI save payload always carries the unchanged name, which used to make every built-in edit fail); description/grants stay editable. Audited `role.update` |
| `POST` | `/api/admin/roles/{rid}/delete` | → `{ok, reverted_members}` (a COUNT of users who HELD the role — the revert is dynamic, nothing rewritten; multi-role holders keep their other roles). 400 Base; 404 unknown. Audited `role.delete` |

The wizard save's `access_role_ids` writes through `roles_store.set_table_roles`
(audited `role.set_tables`). Admin-page UI: **Users** section (searchable list,
19c multi-role checkbox picker — every toggle POSTs the full held list — and
the 19e per-row Permission dropdown Standard / Power user / Local admin with a
confirm dialog before promoting to admin; the roles picker stays enabled on
admin rows — promoted admins hold roles like anyone) + **Roles** section (cards + a tri-state access tree
connection → schema → tables; checking a schema/connection stores a scope
grant and locks its descendants "via schema/connection").

**Role-gate enforcement map** (client-side only, no brain involvement):
`GET /api/db_tables` (picker filter) · `POST /session/db_tables` (403 seeds)
· `POST /api/chat/{id}/refresh_item` + dashboard tile refresh (per-table
block, below) · `GET /api/chat/{id}/schema` (advisory `allowed` flag).
Deliberately NOT gated (decision: no retroactive blocking — snapshots stay
viewable): `chat/stream`, `edit_regenerate`, full-table/Download-Excel
re-execution, Auto Analytics, and the central nightly scheduler.

**Security invariants** (stated in code — `db_connector.py`, `sandbox_guard.py`):
the connector only ever issues SELECT/introspection statements
(`assert_read_only_query` — a regex layer plus a sqlglot parse under the
connection's dialect — guards the one assembled statement + the optional
WHERE, strictly without CTEs; no route accepts free SQL from a browser — the
only free-form SELECT is the brain's, run by `run_live_select` in the main
application only, bound by the guard's per-table allowlist to the one
registered table, under `LIVE_RESULT_ROW_CAP` / `LIVE_RESULT_MAX_MB` /
`LIVE_QUERY_TIMEOUT_S`; a failure surfaces as an error class, the driver's
text stays local); DB drivers and the client's credential
modules are **denied inside the code-exec sandbox** (`sandbox_guard.SANDBOX_BUILTINS`
installed at both exec sites — defense in depth on top of their simple
ABSENCE from the sandbox image, which is the real boundary; for database
reach specifically the SELECT-only grant is the guarantee); credentials are never logged, never in any brain payload,
never at importable module scope in cleartext.

### Admin routes — Single sign-on (`routes/sso.py` `admin_router`, same `/api/admin` prefix, `_require_admin` guard — ladmin + promoted admins)

Configuration store: `DATA_ROOT/sso_config.json` (`sso_store.py` — atomic
writes, secret Fernet-encrypted via the db_sources helpers, NO plaintext
fallback: no `CLIENT_ENCRYPTION_KEY` ⇒ save/test answer 503). The masked
shape mirrors `_mask_connection`: `client_secret_set` / `client_secret_readable`
/ `client_secret_masked: "••••••••"`, secret material stripped. Every
Save/Test/Enable/Disable writes an `sso.*` row to `admin_audit.jsonl`
(tenant_id + client_id only — never the secret). The ENABLE GATE: enabling
requires a successful Test for the CURRENTLY saved values (`last_test_hash`
= sha256 over tenant_id+client_id+secret; any re-save of a credential field
invalidates it). All changes take effect on the next request — no restart.

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/api/admin/sso` | the masked config + computed `redirect_uri` (`public_base_url` or the request base + `/auth/microsoft/callback`) + `test_current` + `encryption_ready` |
| `POST` | `/api/admin/sso/save` | `{tenant_id, client_id, client_secret?, public_base_url?, auto_redirect?}`. 400: tenant_id not a GUID/domain, empty client_id, no secret when none stored, non-http(s) public_base_url. Empty secret keeps the stored one (the connection-password idiom). Deliberately never changes `enabled`. 503 `EncryptionUnavailable`. Audited `sso.save` (`secret_changed` boolean only) |
| `POST` | `/api/admin/sso/test` | proves the triple against Microsoft without a browser: GET the tenant's OpenID discovery doc + a client-credentials token request (scope `graph.microsoft.com/.default`), 10s timeout. 200 `{ok, message}` outcomes (admin_data connectivity convention); failure message = Microsoft's `error_description` only. Success records the enable-gate hash. **POLICY_BLOCKED** (`AADSTS500011`/`AADSTS65001` — tenant blocks app-only tokens but the credentials are right): `{ok:false, code:"POLICY_BLOCKED", message:…}` yet the gate hash IS recorded (Enable not held hostage to tenant policy) and the audit row carries ok=true + the code. `AADSTS7000215`/`AADSTS700016`/`AADSTS90002` stay plain failures. 400 nothing saved; 503 no encryption key |
| `POST` | `/api/admin/sso/enable` | 400 incomplete config; 409 `{code:"TEST_REQUIRED"}` unless the last successful test matches the saved values; else flips `enabled` on. Audited both ways |
| `POST` | `/api/admin/sso/disable` | flips `enabled` off (absent-file safe) — the landing page returns to the plain password form on the next request. Audited |

---

## Execution sandbox namespace

Generated code runs in the `pdc-executor` container, never in the web
process: `code_exec.safe_execute` (PYTHON blocks) and
`plot_utils.render_plot_safe` (PLOT_CODE) dispatch the job over HTTP
(`executor_client.py`, contract in `docs/EXECUTOR_PROTOCOL.md`) and keep their
return shapes. The code itself still executes at two exec sites — now
`code_exec._execute_in_process` and `plot_utils._render_in_process`, which the
sandbox's runner imports — each with a pre-imported namespace. **Any helper the brain's AVAILABLE LIBRARIES prompt
promises must be registered at BOTH sites**, or plot code will NameError
(`upset_plot_from_sets` set the precedent; `exec_sanitizer` and
`sandbox_guard.SANDBOX_BUILTINS` are likewise installed at both).

Injected names (kept in sync with the brain's planner/classifier library
lists): `pd, np, plt, sns, px, go` (+ `mticker, mpatches, make_subplots` at
the plot site); specialized viz `venn2, venn3, venn2_circles, venn3_circles,
WordCloud, nx, squarify, scipy_stats, msno, calplot, upset_plot_from_sets,
adjust_text`; ML `train_test_split, LogisticRegression,
DecisionTreeClassifier, plot_tree, RandomForestClassifier, KMeans,
StandardScaler, LabelEncoder, accuracy_score, classification_report,
confusion_matrix, roc_curve, auc, silhouette_score`; deterministic outlier
helpers **`outlier_mask(series)`** and **`drop_extreme_outliers(df, col) →
(filtered_df, n_dropped)`** (`outlier_utils.py` — the union of robust
median/MAD + 1st-99th percentile + 3×IQR tests with a gap guard, exactly the
algorithm the planner prompt used to spell out inline; QA 2.6); data
`dfs` (+ a `df` first-frame alias at the python site) and `RESULT`; guarded
`__builtins__` (per-call copy of `SANDBOX_BUILTINS`).

---

## Chat (SSE stream)

**`POST /api/chat/{chat_id}/chat/stream`** — body `{question, conv_id?}`.

**Conversation access.** The chat's owner may act on any conversation of the
chat. Anyone else (a share recipient) may act only on conversations recorded in
their OWN conversation list (their own conversations in the shared chat and the
snapshot copies shared with them) — otherwise `403 {"error": "Access denied"}`,
checked before any history is read or written. This applies to `chat/stream`
with a supplied `conv_id`, `edit-regenerate`,
`conversation/{conv_id}/history` and `conversation/{conv_id}/stop`. The legacy
`GET /api/chat/{chat_id}/history` (newest conversation, which may be anyone's)
returns `{"history": []}` to a non-owner. `conversation/{conv_id}/stop` and
`conversation/{conv_id}/status` also answer `403` when the conversation does
not belong to the chat in the path — for the owner too.

The endpoint is implemented as a real SSE stream (`text/event-stream`), same
content-type and event shape as the B2C `chat_stream_api`:

```
data: {"progress": true, "message": "Working...", "conv_id": "cv_..."}\n\n
data: {"done": true, "partial": false, "conv_id": "cv_...",
       "answer": "...", "image_base64": "...", "table": {...},
       "tokens": {...}}\n\n
```

`{partial: true, answer, image_base64, chart_n, chart_total}` is part of the
contract (for multi-chart responses) — the on-prem build currently emits a
single final event but the frontend handles both paths identically.

**Chart HTML is offline-safe.** Interactive Plotly charts travel as a full HTML
document in `image_base64` (the frontend sniffs the leading `<`). Generated
HTML references the locally served `/static/vendor/plotly/plotly.min.js`
(copied from the plotly pip package at image build + app startup —
`plot_utils.ensure_plotly_js_asset`), never `cdn.plot.ly`: customer LANs may
have no internet at all, and a CDN script src rendered chart iframes blank
there. Legacy persisted chart HTML (history records / dashboard tile snapshots
written by older builds) still carries the CDN src — the frontend rewrites it
to the local asset at every iframe injection point (`PDCViewers.fixPlotlyOffline`
in `static/vendor/viewers.js`), with a `window.Plotly||document.write(CDN)`
guard so a missing local asset degrades to the old CDN behavior instead of
failing harder. If the asset is missing server-side, `_plotly_js_include()`
logs `PLOTLY_JS_ASSET_MISSING` once and falls back to the CDN src for new
charts.

On kill-switch (tenant revoked / suspended), the stream emits a single
`{error: "Service unavailable. Please contact your administrator.", done: true}`
event and the chat UI surfaces it to the user.

**Auto Analytics busy guard (QA 2.3):** when Auto Analytics is currently
`processing` for the chat, `chat/stream` and `edit-regenerate` return an
immediate `409 {error: "Auto Analytics is currently running for this chat…",
busy: "auto_analysis"}` BEFORE any history append (fail-open on a corrupt
meta). The frontend's non-SSE fallback renders the message in the chat. A
`BrainTimeoutError` mid-generation (pool/connection contention, a subclass of
`BrainError` caught first) now reads "The analysis service is busy right now.
Please try again in a moment." — the generic "temporarily unavailable" text is
reserved for genuine brain unreachability.

**Missing database tables (Prompt 15):** when a chat's dataframes come back
empty AND its meta references registered DB tables that can no longer be
loaded, the 400 NAMES them instead of the bare "Chat dataset is empty.":
`{error: "This chat uses database tables 'products dictionary',
'transactions', which are no longer registered as data sources, so there is no
data left to answer with. Ask your administrator to register them again, or add
data to this chat.", code: "DB_TABLES_MISSING", missing_tables: [{df_key,
table_id, display_name, reason: "unregistered"|"snapshot_missing"}]}`.
`local_store.missing_db_tables` is the ONE classifier (also behind `/schema`'s
`missing` flag): the names come from the chat meta, the registry is read only to
tell the two reasons apart, and a registry failure degrades to
`snapshot_missing` rather than failing the response. Applied to `chat/stream`,
`edit-regenerate`, `run_item_refresh` (so per-message refresh and dashboard
tiles say it too, as `{ok:false, code, missing_tables}`) and the Auto Analytics
job error. A genuinely empty chat still returns exactly
`"Chat dataset is empty."` with no `code`. The frontend needs no change — it
already renders `error` verbatim; `code`/`missing_tables` are there for
localization later.

**Live tables.** A chat holding a live database table queries it at question
time (see "Database tables" above): the planner's SELECT — or the default
capped read — runs in the main application right before each sandbox call,
and the AI history row (single-shot and multi-chart; a stopped turn too)
additively persists `sql` (`{df key: SELECT text}`, `null` for a key served
by the default read), `live_truncated` (bool) and `live_rows`
(`{df key: rows}`); a streamed partial and the done event carry `sql` as
well, and the durable full-table records (`full_table_key(s)`) store the
`sql` subset their code references, so Download Excel / Show full table
re-run the same query. A capped result appends a localized note to the
answer text ("Note: <table> is a live table; only the first N rows of the
query result were used."). A failed SELECT never runs the sandbox: the
attempt counts against the same three retries with a value-free class
sentence, and a retry that brings no new SELECT for that key fails rather
than falling back to the default read. A live table the requester's role
does not cover is dropped before the planner sees it; a turn left with no
frame answers "You no longer have access to <table>; ask your
administrator." as the AI row, with no brain call. `brain_client
._sanitize_history_rows` strips the three fields before history reaches
the brain.

**`GET /api/chat/{chat_id}/conversation/{conv_id}/status`** → `{"generating": bool}`.
Lightweight in-memory registry lookup (no I/O): `true` while a generation worker
is still running for that conversation. The generation worker persists the AI
turn regardless of the connection, so a page reloaded/reopened mid-generation
uses this (polled, mirroring the Auto Analytics status pattern) to show the
working indicator, block new questions, and auto-render the answer on completion
without a second manual refresh. Unmarked only after the AI turn is persisted —
so once it returns `false`, the turn is already readable from history.

**`POST /api/chat/{chat_id}/conversation/{conv_id}/stop`** → `{"ok": true, "stopping": true}`.
Requests cancellation of an in-progress generation (the send button becomes a
Stop button while generating). **Instant for the user, cooperative on the
server.** The client abandons the live stream the moment Stop is clicked —
aborts the SSE reader (so NO further or late events render, killing both a late
chart and the planner "couldn't generate…" fallback), finalizes the in-progress
message (keeps any charts already rendered and appends a subtle "⏹ Response
stopped by user" note), and re-enables the input immediately — all WITHOUT
awaiting this endpoint (it is POSTed fire-and-forget). Server-side the worker
checks the flag between charts and halts at the NEXT chart boundary (the
in-flight chart finishes), then persists a **STOPPED** turn, which is
authoritative on reload: ≥1 chart → the partial charts (same shape + per-chart
code/chart_data) with the stopped marker; 0 charts → a short `"Response stopped
by user."` turn. When cancelled it never persists the NO_CODE / "couldn't
generate analysis code" fallback or a late single-shot result. Exactly-once
persistence and the in-progress/cancel flags (cleared in `finally`) are
preserved. Idempotent; setting the flag when nothing is running is a harmless
no-op.

The full enterprise split inside one turn:

1. Client loads dfs from local disk + builds schema text (`_schema_text` port).
2. POST → `/v1/plan` → brain returns code.
3. Client executes code through `safe_execute` / `render_plot_safe`, which
   dispatch it to the sandbox container; raw data never leaves the LAN.
4. On execution error → POST `/v1/retry` → client re-executes. The orchestrator
   (`run_chat_local`) retries each failing unit up to **3 attempts**, escalating
   `use_pro` / `use_search` to `true` from the 2nd retry onward. A retry that
   returns prose (`NO_CODE`/`CLARIFICATION`/`ANSWER`) or the wrong code kind
   counts as a failed attempt — it never aborts the loop early, so the
   harder-model escalation is always reached. In a multi-chart response a retry
   that returns runnable `PYTHON` is executed and accepted only if it produces a
   chart image. If a multi-chart turn ends with **zero** rendered charts (and no
   tables), the persisted answer is "Something went wrong with this analysis.
   Please try again." (never the bare "Analysis complete.").
5. POST → `/v1/describe` (or `/v1/summarize` for scalar results) → brain
   returns the natural-language intro. No row values cross the boundary.
6. A plan of kind `CLARIFICATION` or `ANSWER` skips execution entirely — the
   text is returned to the user as-is (`ANSWER` = a context-based explanation,
   e.g. "how did you compute this?").

---

## Reports (rendered locally, narrative from brain)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/chat/{chat_id}/conversation/{conv_id}/download_report` | PDF (ReportLab + DejaVu fonts) |
| `POST` | `/api/chat/{chat_id}/conversation/{conv_id}/download_pptx` | PPTX (python-pptx) |

Both call `/v1/report` (no values) to get `report_structure` JSON, then merge
the narrative into the client's own template locally.

**Per-tenant PowerPoint template (PPTX exports + Auto Analytics):**

When the operator uploads a branded `.pptx` for a tenant in the brain admin
panel, the client renderer (`client/routes/report._render_pptx`) opens that
file as the base presentation, inheriting the tenant's slide master, theme
colors, and fonts natively through python-pptx. The brain's COMPLEX-tier
analyzer emits a strict **v2 build plan** that the renderer consumes
verbatim:

- The plan picks three template slides — `deck.cover_slide_index`,
  `deck.agenda_slide_index` (optional), `deck.content_slide_index` — and
  labels EVERY SHAPE on each of those slides exactly one of:
  `keep`, `drop`, `replace:title`, `replace:body`, `replace:agenda`.
- Shapes labeled `replace:*` carry a `text_style` (font, size, bold,
  color, align) the renderer applies verbatim.
- The content slide also carries a `chart_region` (inches) where the
  chart is dropped.

For every export the renderer deep-clones the cover slide once, the
agenda slide once (if present), and the content slide ONCE PER FINDING.
On each clone it applies labels by `shape_id` (drop → remove the
element; keep → untouched; `replace:*` → overwrite text using
`text_style`), then drops the chart on content slides at `chart_region`.
Every ORIGINAL template slide is removed before saving so the template
author's own tables / sample bullets / author names never appear in the
output — only chrome (logos, headers, page numbers, dividers) plus the
report's title / narrative / chart in the declared spots.

Templated decks **intentionally omit the PowerDataChat logo** — only the
tenant's own branding shows through. The client fetches both the template
file (`GET /v1/pptx_template`) and the v2 spec (`GET /v1/pptx_template_spec`),
caching them on `DATA_ROOT/templates_cache/` keyed by a schema marker
(`*.v2.pptx`, `*.v2.json`) with a short TTL so a re-upload is picked up
without a client restart. If the v2 plan is unusable
(`spec.version != 2`, missing cover/content, no chart_region, render
exception) the renderer falls back to the built-in PowerDataChat-branded
deck and logs `PPTX_TPL_FALLBACK reason=...`. Auto Analytics reuses the
same `_render_pptx` path, so templates apply to it automatically.

---

## Downloads (chart PNG + table Excel — rendered locally)

The per-chart **Download** button and the per-table **Download Excel** button
post to these three routes. Both the chart render and the `.xlsx` build happen
on the client — **raw data never leaves this server** (Article II); nothing here
calls the brain.

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/api/chat/{chat_id}/export_plotly_png` | Body `{html, filename, scale}`. Renders the interactive chart's raw Plotly HTML to a high-resolution PNG server-side (via `routes/report._plotly_html_to_png`, kaleido) and returns `image/png` as an attachment. `400` when `html` is missing; `502` if the chart cannot be rendered. |
| `POST` | `/api/chat/{chat_id}/download_excel/{key}` | Body `{filename}`. Streams the full result table cached under `{key}` (the `full_table_key` / `chart_data_key` the chat stream emits — the same bounded LRU as `full_table`) as an `.xlsx` spreadsheet. Returns `404 {"error": "Table not found or expired."}` when the key is missing/expired; `502` on build failure. |
| `POST` | `/api/chat/{chat_id}/export_excel` | Body `{columns, rows, filename}`. Builds an `.xlsx` directly from the posted preview table and returns the spreadsheet mime. `400` when no table data is posted. |

All three require an authenticated session with access to `{chat_id}`. `.xlsx`
files are built with pandas + openpyxl. Matplotlib/seaborn charts are already
PNGs, so their Download is a pure client-side save (no route).

---

## Per-chart / per-table refresh (local re-execution)

**`POST /api/chat/{chat_id}/refresh_item`** — body `{code, kind: "chart"|"table"}`.

The execution body lives in the module-level coroutine
`routes.chat.run_item_refresh(chat_id, code, kind, sid)`; `refresh_item` is a
thin auth/validation wrapper around it and the dashboard tile refresh
(`routes/dashboards.py`) reuses the same helper — one source of truth for
local re-execution.

Every chart and table that carries its own stored `code` (live events and
persisted history records both do) gets a small refresh icon button (double
curved arrows) in its action bar. Clicking it re-runs ONLY that item's stored
code against the chat's **current** dataframes (the server enforces this — see
**Stored code only** below) — re-execution via
`render_plot_safe` / `safe_execute`, i.e. the sandbox container, the same path
`_reexecute_full_df` uses;
**no LLM/brain call** — and swaps the chart image / table content in place.
Purpose: after updating a file via Add Data (overwrite), existing items can be
refreshed to reflect the new data.

- Charts return `{ok, image_base64, is_plotly, chart_data_key?}` — the fresh
  `chart_data_key` re-points "Show data" at the refreshed values; Plotly
  "View Larger" / "Download" follow the updated HTML automatically.
- Tables return `{ok, table, full_table_key?}` — the block is re-rendered
  (styled_html included when the code yields a pandas Styler) and "Download
  Excel" is rebound to the new durable key.
- Legacy history records (saved before per-chart code persistence) carry only
  the joined `###NEXT_PLOT###` record-level code. The frontend SPLITS that code
  on the marker when rendering history and assigns segment *i* to chart *i*, so
  legacy charts are refreshable per segment (and Show code shows the clean
  segment). Only items with NO code at all — or an ambiguous multi-segment
  code that can't be matched to the item — show no button. The endpoint keeps
  rejecting joined code with `400` as a guard; the frontend always sends a
  single clean segment.
- **Stored code only.** The posted `code` must be code the chat already
  holds: the stored code of an AI answer in any of the chat's conversations,
  one `###NEXT_PLOT###` segment of a multi-chart answer's joined code, the
  code of a durable full-table record, or the code of the answer currently
  being generated (so a chart streamed a moment ago is refreshable before its
  history row exists). The comparison is made after trimming surrounding
  whitespace and turning CRLF into LF. Anything else →
  `403 {"error": "This item's code is not part of the chat's history.",
  "code": "CODE_NOT_STORED"}` (logged `REFRESH_CODE_NOT_STORED`). Checked
  after the two `400`s (empty code, joined code) and BEFORE the role gate, so
  unstored code is never role-checked or executed.
- Auth/permission failures use HTTP codes; **execution** failures return
  `200 {ok: false, error}` — the frontend keeps the previous render and shows
  a small non-blocking note.
- **Live tables.** When the item's code references a live table, the stored
  SELECT (`stored_sql_for_code`: the in-flight turn, then the AI rows holding
  the code together with a `sql` map, then the durable full-table records)
  is re-run first, after the role gate below, and its result takes the key
  before the code runs. A referenced live key with NO stored query (the table
  went live after the answer was computed) → `200 {ok:false,
  code:"LIVE_NO_QUERY", error:"<display name> is now a live table; ask the
  question again to re-run it."}` — a default read would silently change
  what the item computes; a key stored as `null` takes the default read
  again; a fetch failure → `200 {ok:false, error:<class sentence>}`. A table
  since switched back to snapshot is served from its parquet and the stored
  SQL is ignored. The dashboard tile refresh and `_reexecute_full_df` follow
  the same rules.
- **Role gate** (`_role_refresh_block`, shared with the dashboard tile
  refresh): when the item's code references (same `dfs['…']` key regex as the
  freeze below) a non-connector DB table the REQUESTER's role does not cover,
  the refresh returns `200 {ok:false, code:"ROLE_DENIED", blocked_tables,
  error}` — per-table semantics: an item touching only allowed tables still
  refreshes even when the chat holds denied ones. Denied frames are ALSO
  dropped from the exec namespace (`drop_df_keys`, filtered AFTER the per-chat
  cached load) so unreferenced access is impossible. For shared chats the gate
  keys on the requester, not the owner. The dashboard tile variant returns the
  caller-specific `{ok:false, frozen:true, reason:"role_denied",
  blocked_tables}` — never persisted (mirror of `access_revoked`). File-only
  chats perform zero role reads. An unexpected gate crash fails OPEN (logged
  `ROLE_GATE_FAILED`); genuine denials fail closed through Base-role defaults.
- **Refresh freezing** (frontend): a button whose stored code subscripts a df
  key (`dfs['…']` / `dfs["…"]` — exact keys only, no column analysis) that no
  longer exists in `/api/chat/{id}/schema` renders **disabled** (greyed, not
  hidden) with the tooltip "Data structure changed — this chart/table can't
  be refreshed." — evaluated on live render, on history reload, and re-applied
  after an Add Data completes. A refresh that **fails at runtime** (e.g. a
  column vanished while the key survived) additionally disables that button
  for the rest of the session after its transient red note (the key check
  re-evaluates it on the next reload).

---

## Dashboards (curated grids of pinned charts/tables)

A dashboard is a named grid of TILES pinned from conversation responses. Each
tile stores a **snapshot** (chart base64-PNG / Plotly HTML, or a ≤50-row table
preview) plus the item's stored `code`, description, inline `chart_data`
(≤200k chars) and `full_table_key`. Opening a dashboard renders snapshots
instantly — zero execution; per-tile refresh re-runs the code locally via the
shared `run_item_refresh` helper (the `refresh_item` execution path — **no
brain call**) and persists the fresh snapshot.

**Storage** (Article V, all under `DATA_ROOT`):
`users/{email}/dashboards/index.json` — light listing rows (own rows +
`shared_by` pointer rows for dashboards shared with this user) — and
`users/{email}/dashboards/{dash_id}.json` — the full doc
`{dash_id, name, owner, version, sharing: {shared_with}, tiles: [...]}` with
atomic tmp+`os.replace` writes (`local_store.DashboardStore`). IDs are 16 hex
chars, regex-guarded (path-traversal safe). Old-shape docs load with defaults
(backward compatible).

| Method | Path | Behavior |
|---|---|---|
| `GET` | `/api/dashboards` | own + shared rows merged, MRU-sorted (`last_used_at` desc); shared rows carry `shared_by`/`owner` (rendered green in the UI). Stale pointers to owner-deleted dashboards are lazily pruned here. |
| `POST` | `/api/dashboards` | `{name}` (1–100 chars) → creates an empty dashboard, returns the index row |
| `GET` | `/api/dashboards/{id}` | full doc + `is_owner`; **bumps `last_used_at`** (opened == used) |
| `POST` | `/api/dashboards/{id}/rename` | `{name}` — owner only (shared recipients → 403) |
| `POST` | `/api/dashboards/{id}/delete` | owner → deletes the doc (+ own index row); shared recipient → drops only their pointer row (`{ok, deleted: bool}`); idempotent |
| `POST` | `/api/dashboards/{id}/tiles` | pin one item. Body `{chat_id, kind: "chart"\|"table", description?, code?, image_base64?, is_plotly?, table?, full_table_key?, chart_data?\|chart_data_key?}`. Owner only + `_require_chat(chat_id)` (can only pin from accessible chats). Volatile `chart_data_key` is resolved to durable inline data AT PIN TIME; table rows capped at 50 (honest `total_rows`) with `styled_html`/`dtype`/`title` preserved (styled_html dropped only over 2M chars — plain rows remain) so conditional formatting survives on the tile; chart snapshots >5M chars → 400; `###NEXT_PLOT###` code is nulled (tile renders, can't refresh). **Table code is authoritative-from-record**: when `full_table_key` resolves to a durable record with clean `code`, that code (+ its `result_key`) is stored on the tile — the client-sent code can be the CHART's code in mixed chart+table answers, and the frontend sends no code for a table pinned from such a message. **Stored code only** (same rule as `refresh_item`, because every later tile refresh re-runs it): a chart tile — or a table tile whose `full_table_key` did not resolve to a durable record — posted with code the source chat does not hold → `400 {"error", "code": "CODE_NOT_STORED"}` (logged `DASH_PIN_CODE_NOT_STORED`); a pin WITHOUT code is still accepted (the tile renders but cannot refresh). **Journal layout defaults (QA 3.1)**: charts are HALF-width (w6) and a new chart pairs into the right half of the previous left-half chart's row (2 per row); tables are FULL-width (w12); text blocks w12×h2. **Text blocks**: `{kind: "text", text (≤2000, required), style: header1\|header2\|paragraph, color: default\|gray\|red\|orange\|green\|blue\|purple, size: S\|M\|L, align: left\|center\|right (default left), valign: top\|middle\|bottom (default top)}` — no `chat_id`/snapshot/code; invalid enums → 400; alignment lives at tile TOP LEVEL (never inside `layout`, which every drag rewrites to exactly `{x,y,w,h}`) and tiles stored before it existed render as left/top; rendered as styled text tiles (draggable/resizable like any tile, visible read-only in the shared view; refresh on them is a no-op that never freezes). Legacy layoutless tiles get a computed half-width default in the GET RESPONSE only (never persisted — the first drag persists real positions). |
| `POST` | `/api/dashboards/{id}/tiles/{tile_id}/update` | owner only; TEXT tiles only (chart/table tiles → 400 — their content changes via refresh, never free edits). Body: any subset of `{text, style, color, size, align, valign}`, same validation as create; empty body → 400. Returns `{ok, tile}`. |
| `POST` | `/api/dashboards/{id}/tiles/{tile_id}/remove` | owner only |
| `POST` | `/api/dashboards/{id}/layout` | `{tiles: [{tile_id, x, y, w, h}]}` bulk save — owner only; ints validated/clamped, unknown tile_ids ignored (stale client) |
| `POST` | `/api/dashboards/{id}/tiles/{tile_id}/refresh` | allowed for owner AND shared recipients. Table tiles first **re-resolve + self-heal** their code from the durable full-table record (tiles pinned with a wrong/chart code get the corrected code persisted); tiles with a `result_key` (one table of a multi-table RESULT) re-execute via `_reexecute_full_df` and persist a fresh durable key, others via `run_item_refresh` (Styler results keep `styled_html`). Deleted source chat → persists `frozen/frozen_reason="source_deleted"` on the tile, returns `200 {ok:false, frozen:true, reason}`; a caller without source-chat access gets the same shape with `reason:"access_revoked"` but nothing is persisted (caller-specific). Execution failures → `200 {ok:false, error}`, stored snapshot untouched; a tile whose code references a live table with no stored SELECT → `200 {ok:false, code:"LIVE_NO_QUERY", error}` and a failed live fetch → `200 {ok:false, error:<class sentence>}`, both passed through from `run_item_refresh`, nothing persisted. Success updates the snapshot (+ re-inlined `chart_data` / new `full_table_key`), clears `frozen`, returns `{ok, kind, image_base64\|table, is_plotly?, tile}`. |
| `POST` | `/api/dashboards/{id}/share` | `{emails: [...]\|"a@x, b@y", message?}` — owner only, mirrors the chat share contract (`{ok, shared_with, added, email_sent, smtp_configured, failed}`). Adds recipients to the doc's `shared_with`, writes a pointer row into each recipient's dashboard index, **and grants them access to every tile's source chat that the dashboard OWNER owns** (`add_share_recipients`, same grant conversation-sharing performs) so their Show-data/refresh work. A tile pinned from a chat the owner merely RECEIVED is not re-shared: those recipients see its stored snapshot, and its refresh answers `{ok:false, frozen:true, reason:"access_revoked"}` for them. An address that has never signed in gets a password-less placeholder account (see Sharing rules). Brain SMTP relay gets only the dashboard name + comment (Article II — never tile content). No revoke exists (parity with chat sharing). |

**Frontend**: the `/lab` top bar has a Dashboards dropdown at the LEFT corner
(filled navy `#001E44` bold button with an inline-SVG list icon — rows of
square-bullet + bar; left-anchored menu; always visible when
signed in; "＋ Add new" first, then MRU; shared dashboards green with "Shared
by …"). "＋ Add new" only CREATES the dashboard (toast, no navigation) — the
user pins items to it later. Dashboard entries in the menu are real `<a>`
anchors (middle-click / Ctrl+click open a new tab), and sidebar conversation
items support middle-click via their `/c/{conv_id}` deep link. The
add-to-dashboard picker is an ANCHORED COMBOBOX POPOVER at the 📌 button
(`_openDashPicker`): search input with the suggestion list attached beneath,
all own dashboards shown immediately, live filter, Enter picks the first
match, "＋ Add new" pinned at the bottom, explicit loading/empty/error rows. Every chart and table block in a response carries a 📌 pin button in
its `.pdc-action-bar` (live stream, history reload, multi-chart, multi-table)
that opens the anchored combobox popover; the payload is read at CLICK time so a
prior in-chat refresh pins the refreshed render. The dashboard page uses
vendored GridStack 10.3.1 (`static/vendor/gridstack/`, MIT, offline) — drag by
the tile grab strip (a `⠿` grip glyph; the old grey title snippet is gone —
QA 3.1 — the title lives in the strip tooltip and the info popover). The grip
is the ONLY drag handle (`handle: '.pdc-tile-drag'`) because chart iframes
swallow mouse events, so it names itself: owners see a localized
"Drag to move" tooltip (appended after the tile title) on a `grab` cursor;
read-only viewers, whose grip is hidden and dragging disabled, keep the plain
title. Resize by
edges, **free placement** (`float:true`): tiles stay exactly where the user
puts them, vertical gaps included — the only restriction is no overlap
(dragging onto an occupied cell pushes). Layout saves are armed only AFTER the
initial render (save-on-load guard), so loading a stored layout can never
compact and overwrite it; 1-column read-only mode under 768px (narrow layout
is presentational and never saved). ONE owner-only "＋ Header" top-bar button
opens the `#textTileModal` (textarea + style/size/alignment selects + 7 color
swatches) to add text blocks between visuals — the separate "＋ Text" button
was removed: it opened the same modal with a different preset, and the style
dropdown (header 1 / header 2 / paragraph) already covers both. Horizontal and
vertical alignment render through `tt-align-*` / `tt-valign-*` classes in the
one shared tile renderer, so owner and shared views always agree. Text tiles get
an owner-only ✏ edit button (same modal) instead of the data/code/download/refresh actions.
Tile toolbar: description popover (backdrop-dismissed — clicks inside Plotly
iframes don't bubble), Show data / Show code (PDCViewers), Download
(`export_plotly_png` / client-side PNG / `download_excel`), View larger,
Refresh, Remove; plus a top-bar "Refresh all" (client-side concurrency-2 queue,
per-tile failure isolation). Shared recipients see a read-only grid (no
drag/resize/rename/share/remove-tile) with Delete becoming "Remove from my
list".

---

## Endpoints that exist purely to keep the page non-broken

The B2C dashboard.html references B2C-only features that don't exist on-prem.
Rather than ripping JS out, the client returns clean errors so the UI
gracefully handles them:

| Endpoint | Returns | Reason |
|---|---|---|
| `POST /api/chat/{id}/publish`, `/unpublish` | 400 | no public pages on-prem (architecture: sharing is OPEN, public publish is out of scope) |
| `POST /upload/init`, `/upload/finalize` | 400 unless `GCS_UPLOAD_BUCKET` is set | the direct-to-GCS large-file path is OPTIONAL (Cloud Run demo only); off, the frontend never calls them and every size goes through multipart `/upload` — see "Direct-to-GCS large files" above |
| `POST /upload_from_url` | 400 | Google Drive/Sheets are off-prem |
| `GET /paddle/config` | 200 `{enabled:false, client_token:null}` | page-load Paddle bootstrap becomes a no-op (no 404 / console error) |
| `POST /auth/subscription` | 400 | enterprise plan is constant |
| `POST /api/paddle/subscription/update-payment`, `/reactivate`, `/preview`, `/cancel`, `/update` | 400 | billing is off-prem |
| `POST /auth/conversations/{id}/publish`, `/unpublish` | 400 | no public pages on-prem (mirrors the chat-level stub) |

> Auto Analytics (`*/auto_analysis/start|status|download`) is **implemented** on-prem
> (brain-side planner + client-side execution + PPTX render). See the "Implemented
> on-prem" table below for the full row.

---

## Schema endpoints (session-level)

`dashboard.js` calls these between `/upload` and `/generate_chatdata` to
populate the column-edit form. They are verbatim ports of global
`/schema_details` and `/schema_common_fields` — pure pandas, no LLM, no row
values leave the client.

| Method | Path | Behavior |
|---|---|---|
| `GET`  | `/schema_details` | per-column stats (dtype, nunique, sample unique values, needs_description), with sampling for >50k-row datasets and ThreadPool fan-out for large col counts |
| `GET`  | `/schema_common_fields` | auto-detected join columns across multiple uploaded files (fuzzy name matching + dtype + cardinality) |
| `POST` | `/schema_common_fields` | persist user-confirmed join relationships into `meta.json["common_fields"]` |
| `GET`  | `/schema` | full session `meta.json` (file list + per-file schema) |
| `POST` | `/schema` | save schema edits (`files[].fields[].description`, `file_description`) |

---

## Implemented on-prem (replaces previous stubs)

| Endpoint | Behavior |
|---|---|
| `POST /api/chat/{id}/share` | owner only (`403 {"error": "Access denied"}` for a share recipient); adds recipients to `meta.json["sharing"]["shared_with"]` (a never-signed-in address gets a placeholder account — see Sharing rules), lists the chat in each recipient's chat list (`AuthStore.record_shared_chat`, a row with `shared_by`; idempotent), asks brain `/v1/send_share_email` to SMTP-relay invites using this tenant's SMTP config |
| `GET  /api/chat/{id}/share` | returns the current sharing record (`{shared_with, owner}`) |
| `POST /auth/conversations/{conv_id}/share` | **conversation-level share** — only the chat's OWNER may share (sharing a conversation also grants access to its chat): anyone else → `403 {"error": "Access denied"}`. For each recipient, snapshot the conversation history into a fresh `conv_id` via `ChatDataStore.copy_conv_to_new`, add them to the chat's `sharing.shared_with`, record the new conv in the recipient's `conversations.jsonl` with title prefix "(Shared) …" and `shared_by` field, then SMTP-relay an invite. Recipients access the chat through `_require_chat`'s shared-recipient check |
| `GET  /api/chat/{id}/full_table/{key}` | returns the full result table cached under `key`. The chat stream sets `full_table_key` on responses that contain a tabular result. Backed by a bounded in-memory LRU (256 most recent results) |
| Conversation title generation | After the 2nd human message, the chat stream fires a background `brain_client.title()` call and renames the conversation via `AuthStore.rename_conversation` |
| Activity logging | `auth.py` (login), `upload.py` (file_uploaded), `chat.py` (plot_generated, per chart), `report.py` (report_exported), `auto_analytics.py` (auto_analytics_completed) all call `brain_client.post_activity` → brain `/v1/activity`. Fire-and-forget: the post runs on a single background worker thread (ordered), so a slow brain can never block a request or the event loop — telemetry lags instead. |
| **Auto Analytics** | `POST /api/chat/{id}/auto_analysis/start` kicks a background job → brain `/v1/auto_analytics_plan` (planner returns 3-15 natural-language analytical instructions) → client executes each via `run_chat_local.run_chat` against the local dataframes (bounded 4-worker pool; loaded WITHOUT live tables — `include_live` off — so a live table is never queried by the job and its findings cover the snapshot and file tables only) → brain `/v1/report` for narrative → client renders PPTX via `routes/report._render_pptx` → persists to `chatdata/{id}/auto_analysis.pptx`. `GET /auto_analysis/status` reports `{status: idle|processing|done, progress, error, pptx_path}`. `GET /auto_analysis/download` streams the deck. Raw row data never leaves the client |
| **Multi-chart streaming** | The chat SSE stream uses `run_chat_multi_plot` (a generator port of global's). The brain Agent classifier sets `suggested_approach` to "Decompose into multiple PLOT_CODE blocks ... separated by ###NEXT_PLOT###" for dashboard/overview-style queries; the planner emits the multi-block raw_text; the client splits it via `_extract_multi_plot_blocks` and executes each block locally with retry, yielding a `{partial: true, chart_n, chart_total, image_base64, answer}` SSE event per chart and a final `{done: true}` combined event. Capped at 6 charts per response. Single-chart queries fall through to the existing one-shot path |
| **Edit-regenerate** | `POST /api/chat/{id}/edit-regenerate` — verbatim port of global's `edit_regenerate_api` (`backend/routes/chat.py` L1136-1296). dashboard.js fires this from the pencil-edit affordance on a past user message. Server-side: find last `human` turn, `truncate_conv_history` to drop it (and everything after), append the edited human turn, run `run_chat_multi_plot` against the local dfs, persist the AI turn in the same shape the SSE stream uses (`image_base64` for 0/1 charts, `images: [...]` for 2+). Returns a single JSON (NOT SSE — global's is also a one-shot JSON response) |
| **Chart persistence across refresh / reopen** | `routes/chat.py` accumulates each multi-plot partial's `(image_base64, answer)` while streaming. On the final `done` event the AI turn is appended to `conversations/{conv_id}.jsonl` using global's shape (`backend/routes/chat.py` L1088–1105): 0 images → no image fields, 1 image → top-level `image_base64`, 2+ images → `images: [{image_base64, answer}, ...]`. The single-chart path already stored `image_base64` directly. `dashboard.js` reopens the conversation via `GET /api/chat/{id}/conversation/{conv_id}/history`; lines 1551 and 2184 render `msg.images` as one assistant bubble per chart, otherwise render `msg.image_base64` — identical to global. **Bug fixed (May 2026):** multi-plot history previously persisted `image_base64: null` and no `images` field, so charts vanished on refresh |

### Sharing rules

- **New addresses.** Sharing a chat, a conversation or a dashboard with an address that has never signed in creates a password-less placeholder account (`AuthStore.ensure_invited_user`, `invited_by`/`invited_at` on the profile). Its sign-in is refused like any failed sign-in, so the recipient sets a password through the mailed reset link — whoever types the address first at the sign-in page cannot claim the share. Recipient addresses must match `routes.auth._EMAIL_RE`.
- **Dashboards.** A dashboard share grants the recipients access only to the source chats the dashboard OWNER owns. Tiles pinned from a chat the owner merely received show their stored snapshot, and their refresh answers `{ok:false, frozen:true, reason:"access_revoked"}` for those recipients.
- **Conversations.** A conversation share is owner-only, like the chat-level share.

---

## Lesson learned (recorded so we don't repeat it)

In the first cut, the team copied `dashboard.html` verbatim and built a
parallel **minimal** backend on the side. The smoke test then exercised
THAT side-API by curl. The result: the visual page rendered, but every
JS action (drag-drop, file picker, chat send, profile save) silently
failed because nothing was wired to the endpoints `dashboard.js` actually
calls.

The right move is to always treat the page as a contract: every endpoint
listed in this file must either be implemented or stubbed to return a
clean error code so the UI can handle it. Direct browser click-through
verification is mandatory before declaring the page working.
