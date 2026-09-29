# Microsoft Entra ID single sign-on (SSO)

PowerDataChat can authenticate your users against your own **Microsoft
Entra ID** (Azure AD) tenant using the standard OpenID Connect
authorization-code flow. Once enabled, employees open the PowerDataChat URL
and land in the workspace already signed in with their Windows / Microsoft
365 identity — the browser's existing Microsoft session handles the silent
part.

The connection is configured from the local-admin web UI — no code
changes, no container restart. Two optional settings in `client.env`
(`SSO_ALLOW_GUESTS`, `SSO_AUTO_PROVISION`, see "Who can sign in with
Microsoft" below) widen who is admitted; they are read at start, so changing
them needs a restart of the web container.

What stays local: PowerDataChat never sees a password. From the ID token it
reads the user's address (`preferred_username`, falling back to the `email`
claim for a member of your tenant, never for a guest), the Entra tenant id
and object id (`tid`, `oid`) that bind the account to one Entra identity,
and `upn` to recognise a guest. Nothing about SSO is sent to the
PowerDataChat brain except the same anonymous "login" activity event a
password login already emits.

---

## Prerequisites

- **`CLIENT_ENCRYPTION_KEY` must be set** at install time. It protects both
  your database credentials and the SSO client secret at rest (Fernet).
  Without it the SSO Save/Test buttons are disabled. Generate a key once:

  ```
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```

  (Keep the trailing `=` characters; store the key in your secret manager —
  losing it means re-entering every stored credential.)
- **HTTPS.** Microsoft requires `https://` redirect URIs (plain `http://`
  is allowed only for `localhost` testing). Production SSO therefore needs
  the PowerDataChat client to be reachable over TLS — typically a reverse
  proxy in front of the container — and the **Public base URL** field set
  to that external `https://` address.

## Step 1 — Register PowerDataChat in your Entra tenant

In the [Microsoft Entra admin center](https://entra.microsoft.com):

1. **App registrations → New registration.**
   - Name: e.g. `PowerDataChat`.
   - Supported account types: **Accounts in this organizational directory
     only (single tenant)**.
2. Add a **Web** redirect URI, exactly as shown on the ladmin SSO page
   (copy button provided):

   ```
   <your PowerDataChat base URL>/auth/microsoft/callback
   ```

   The base URL must be the address **users** reach the app on. If that
   differs from what the ladmin page shows (reverse proxy, different
   hostname), set **Public base URL** in Step 2 — the two must match
   exactly or Microsoft answers `AADSTS50011` (redirect URI mismatch).
3. Permissions: the delegated **openid, profile, email** scopes (present by
   default on a new registration; no admin consent needed for these).
4. **Certificates & secrets → New client secret.** Copy the secret's
   **Value** (not the "Secret ID") immediately — it is shown only once.
   > Entra client secrets **expire** (max 24 months). Note the expiry date
   > and rotate the secret on the ladmin SSO page before it lapses;
   > otherwise sign-in stops with `AADSTS7000222`.
5. Copy the **Directory (tenant) ID** and **Application (client) ID** from
   the registration's Overview page.
6. **Require assignment — mandatory.** Open the matching *Enterprise
   application* (Enterprise applications → the app's name) → **Properties**
   → set **Assignment required?** to **Yes** and save; then, under **Users
   and groups**, assign only the users or groups who may use PowerDataChat.
   Without it every member of your tenant can complete a Microsoft sign-in
   to PowerDataChat: any member whose address matches an existing account
   that has not yet signed in with Microsoft (an invited colleague, a share
   recipient, a password account) can sign in to that account, and with
   `SSO_AUTO_PROVISION=true` any member at all gets an account.
   PowerDataChat checks that an account exists for the identity; who is
   allowed to use the application is decided in Entra.

## Step 2 — Connect from the ladmin panel

Sign in as the local admin (`ladmin`) → **Single sign-on** in the sidebar.

1. Paste **Tenant ID**, **Client ID**, and the **Client secret**.
   - Tenant ID may be the GUID or a verified domain
     (`contoso.onmicrosoft.com`).
   - A stored secret shows as `(unchanged)` — leave the field empty to keep
     it, type a new value to rotate it.
2. **Public base URL** (optional): set it when users reach PowerDataChat on
   a different address than the admin page (reverse proxy / external
   hostname). The computed redirect URI shown in Step 1 updates from it.
3. **Save**, then **Test connection**. The test fetches your tenant's
   OpenID discovery document and requests an app-only token, proving all
   three values without a browser round-trip.
   - A yellow *"tenant's policy blocked the test token"* result
     (`AADSTS500011` / `AADSTS65001`) means the credentials are right but
     your tenant blocks app-only tokens — sign-in itself may still work,
     and Enable is **not** blocked by this outcome.
4. **Enable SSO.** Enabling requires a test that passed for the currently
   saved values (changing any value requires re-testing). Takes effect on
   the next request.

The sign-in page now shows **Sign in with Microsoft** above the unchanged
email/password form. **Auto-redirect to Microsoft** (checkbox) skips the
form entirely and sends visitors straight to Microsoft.

## The `/?local=1` escape hatch

The password form is **always** reachable at:

```
<your PowerDataChat URL>/?local=1
```

Use it for the `ladmin` account (which has no Microsoft identity and always
signs in with its password), for local accounts that have never signed in
with Microsoft, and as the recovery path if the SSO configuration ever breaks
while auto-redirect is on. While SSO is enabled, an account that has signed
in with Microsoft cannot use it, whether or not it holds a local password
(below). **Disable SSO** on the ladmin page returns the landing page to the
plain password form instantly and gives every account that holds a local
password its password back.

## Who can sign in with Microsoft

A Microsoft sign-in reaches an account only when every check below passes.
A refusal shows the same "Microsoft sign-in failed" page; the log line
(`/data/client/logs/datachat.log`) says which check refused it.

- **The token must carry the Entra identity.** A token without a GUID
  tenant id (`tid`) and object id (`oid`) is refused (`SSO_CLAIMS_MISSING`,
  HTTP 401).
- **Guests are refused by default.** A B2B guest of your tenant (`#EXT#` in
  its `upn` or `preferred_username`) is refused (`SSO_GUEST_REFUSED`, 403)
  unless `SSO_ALLOW_GUESTS=true`. When guests are allowed, a guest is
  identified by `preferred_username` only, never by the `email` claim, which
  comes from the guest's home directory and is not verified by your tenant.
- **Each account is bound to one Entra identity.** At its first Microsoft
  sign-in the account is bound to the tenant id + object id it signed in
  with (`sso_tid` / `sso_oid` in `users/<email>/auth.json`,
  `SSO_IDENTITY_BOUND`). From then on:
  - the same identity reaches the same account even after its username
    changes in Entra (`SSO_USERNAME_CHANGED`) — the account keeps its
    original address;
  - a different Entra identity presenting an address that is already bound
    is refused (`SSO_BIND_CONFLICT`, 403).
- **Existing accounts are bound once.** Invited users, share placeholders
  (including the Microsoft-only placeholder a share creates while SSO is
  on) and accounts created before this binding existed are bound at their
  next Microsoft sign-in. A binding is refused when the account's
  `auth.json` cannot be read (`SSO_BIND_REFUSED_UNREADABLE`, 403), so a
  damaged record is never overwritten.
- **An identity without an account is refused.** Nothing is created
  (`SSO_UNKNOWN_ACCOUNT`, 401): add the person with **Invite user** on the
  admin panel's **Users** page, or share with them. With
  `SSO_AUTO_PROVISION=true` the account is created and bound instead
  (`SSO_ACCOUNT_PROVISIONED`). Before this release every successful
  Microsoft sign-in created an account.
- The `ladmin` account never signs in with Microsoft
  (`SSO_BOOTSTRAP_ADMIN_REFUSED`, 403).

| Setting (`client.env`) | Default | Effect |
|---|---|---|
| `SSO_ALLOW_GUESTS` | `false` | `true` lets guests of your tenant sign in (subject to the other checks). |
| `SSO_AUTO_PROVISION` | `false` | `true` creates an account for any identity Entra lets through that has none. Use it only with **Assignment required? = Yes** (Step 1.6). |

**Removing a user** (admin panel → **Users** → **Remove**) also removes the
account's binding. Invited again, the address is bound afresh at its next
Microsoft sign-in.

**Re-binding an account.** If a user is deleted and re-created in Entra (a
new object id), or the wrong person was bound to an account, the next
sign-in is refused with `SSO_BIND_CONFLICT`. To fix it, an operator removes
the `sso_tid` and `sso_oid` keys from that user's `users/<email>/auth.json`
on the data volume and restarts the web container (the identity lookup is
cached per process); the next Microsoft sign-in binds the account again.
Alternatively the admin removes the account and invites it again — that
deletes the user's chats and dashboards.

## Behavior details

- **Sessions**: an SSO sign-in uses a browser-session cookie — the session
  ends when the browser closes, and sign-in is automatic again on the next
  visit (Microsoft re-authenticates silently on Entra-joined devices).
- **Logout is local-only** (intentional): *Sign out* ends the PowerDataChat
  session but not the Microsoft browser session — PowerDataChat performs no
  Microsoft front-channel logout. On a shared machine, users should also
  sign out of Microsoft 365 or use a private window.
- **A Microsoft sign-in does not create an account** unless
  `SSO_AUTO_PROVISION=true` (above); accounts come from an invitation or a
  share. An existing password account with the same email becomes a
  Microsoft account at its first Microsoft sign-in (below).
- **No local password for Microsoft accounts.** An account that has signed in
  with Microsoft and holds no local password cannot obtain one: "Reset
  password" mails it nothing, and a password change or an invitation for it
  is refused ("This account signs in with Microsoft and has no local
  password."). Multi-factor authentication and conditional access apply at
  sign-in, in Entra; a local password would bypass them.
- **Share recipients while SSO is enabled.** Sharing a chat, conversation or
  dashboard with an address in an allowed domain that has no account creates
  a Microsoft-only placeholder (no local password; "Reset password" mails it
  nothing); the person signs in with Microsoft. Placeholders created by a
  share before SSO was enabled keep the reset-link path. Such an account
  stays Microsoft-only if SSO is later switched off; recover it as described
  below.
- **While SSO is enabled, Microsoft accounts sign in with Microsoft only.**
  Once an account has signed in with Microsoft, a local password it also
  holds is refused at the password form (the same "Sign-in failed" line as a
  wrong password), "Reset password" mails it nothing, and a reset link mailed
  before is refused with "This account signs in with Microsoft and has no
  local password." The `ladmin` account is exempt. Nothing is deleted:
  **Disable SSO** and the password works again. Such a user can still set a
  local password through **Change Password** in the profile menu, but it
  stays unusable while SSO is enabled.
- **The enforcement starts at the first Microsoft sign-in, not before.**
  Enabling SSO does not stop an account that has a local password and has
  never signed in with Microsoft: it keeps signing in with that password,
  and Entra's multi-factor authentication and conditional access do not
  apply to it until the user signs in with Microsoft once. If every user
  must go through Entra, have each of them sign in with Microsoft once after
  SSO is enabled.
- **Disabling a user in Entra does not end an open session here.** There is
  no back-channel logout: an open PowerDataChat session lasts until the
  browser drops the cookie or its 30-day lifetime (`REMEMBER_ME_MAX_DAYS`)
  runs out. To end it at once, use **End sessions** on that user's row of the
  admin panel's **Users** page; **Remove** deletes the account and everything it
  owns and takes the address off everything shared with it and off the
  tables it registered. The removed user's next Microsoft sign-in is refused
  as an unknown account — unless `SSO_AUTO_PROVISION=true`, in which case a
  user still assigned in Entra gets a new, empty account (none of the old
  chats, shares, roles or registrations, and no old session works for it);
  unassign them in Entra as well.
- **Recovering a stranded account.** If SSO is switched off, an account that
  has signed in with Microsoft and has no local password cannot sign in at
  all; while SSO stays on, the same is true of any Microsoft user who leaves
  your Entra tenant but still needs access. Recover it by hand: remove the
  `sso_provider` key from `users/<email>/auth.json` on the data volume,
  restart the web container, then — unless the account already had a
  password — send the user a reset link (**Invite user** on the Users page,
  or "Reset password" on the sign-in page).
- Every Save / Test / Enable / Disable is written to the admin audit log
  (tenant and client IDs only — never the secret).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `AADSTS50011` (redirect URI mismatch) | The redirect URI registered in Entra doesn't byte-match `<base>/auth/microsoft/callback`. Check trailing slashes, `http` vs `https`, hostname; set **Public base URL** when behind a proxy. |
| `AADSTS700016` on Test | Application not found in the tenant — wrong Client ID, or wrong Tenant ID. |
| `AADSTS90002` on Test | Tenant not found — wrong Tenant ID. |
| `AADSTS7000215` on Test | Invalid client secret — you likely pasted the secret's *ID* instead of its *Value*, or the secret expired. Create a new one and rotate it here. |
| `AADSTS500011` / `AADSTS65001` on Test (yellow) | Tenant policy blocks the app-only test token. Credentials are fine; Enable still works — try a real browser sign-in. |
| "Microsoft sign-in failed" after the Microsoft page | Check the container log (`/data/client/logs/datachat.log`). `SSO_CALLBACK_FAILED`: the token exchange or validation failed. `SSO_CALLBACK_NO_EMAIL`: the token carried no usable address (a guest needs a `preferred_username`). `SSO_CLAIMS_MISSING`: no `tid`/`oid` in the token. `SSO_GUEST_REFUSED`: a guest, and `SSO_ALLOW_GUESTS` is off. `SSO_UNKNOWN_ACCOUNT`: no account for this identity — invite the user (or set `SSO_AUTO_PROVISION`). `SSO_BIND_CONFLICT`: the address is bound to another Entra identity — see "Re-binding an account". `SSO_BIND_REFUSED_UNREADABLE`: the account's `auth.json` is damaged. `SSO_BOOTSTRAP_ADMIN_REFUSED`: the `ladmin` account, which signs in with its password only. |
| Token validation errors mentioning `iat`/`exp`/`nbf` | Clock skew — the container's clock must be NTP-accurate (JWT validation allows only small leeway). |
| Sign-in fails right after an image upgrade, at the return from Microsoft | The library that verifies the identity token changed with the dependency set, so an upgrade is the moment to re-test SSO. Grep `SSO_CALLBACK_FAILED` in `/data/client/logs/datachat.log` for the reason; `/?local=1` (above) keeps the password form reachable meanwhile. Report the log line rather than re-entering the credentials — a validation failure is not a configuration problem. |
| Members of the tenant who should not use PowerDataChat can sign in | Set **Assignment required? = Yes** on the enterprise application and assign only the intended users or groups (Step 1.6, mandatory). |
| SSO stopped working after a key rotation | Rotating `CLIENT_ENCRYPTION_KEY` without `CLIENT_ENCRYPTION_KEY_OLD` makes the stored secret unreadable — the ladmin page shows "re-enter it"; paste the secret again. |
