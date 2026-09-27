# Microsoft Entra ID single sign-on (SSO)

PowerDataChat can authenticate your users against your own **Microsoft
Entra ID** (Azure AD) tenant using the standard OpenID Connect
authorization-code flow. Once enabled, employees open the PowerDataChat URL
and land in the workspace already signed in with their Windows / Microsoft
365 identity — the browser's existing Microsoft session handles the silent
part.

Everything is configured from the local-admin web UI. **No `.env` editing,
no code changes, no container restart.**

What stays local: PowerDataChat never sees a password. The only thing read
from the ID token is the user's email (`preferred_username`, falling back
to the `email` claim). Nothing about SSO is sent to the PowerDataChat
brain except the same anonymous "login" activity event a password login
already emits.

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
6. **Restrict who can sign in** (recommended): open the matching
   *Enterprise application* → Properties → set **Assignment required** to
   Yes, then assign the users or groups that may use PowerDataChat.
   PowerDataChat itself auto-provisions a local profile for every identity
   Entra lets through — access control is done in Entra, not in
   PowerDataChat.

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

## Behavior details

- **Sessions**: an SSO sign-in uses a browser-session cookie — the session
  ends when the browser closes, and sign-in is automatic again on the next
  visit (Microsoft re-authenticates silently on Entra-joined devices).
- **Logout is local-only** (intentional): *Sign out* ends the PowerDataChat
  session but not the Microsoft browser session — PowerDataChat performs no
  Microsoft front-channel logout. On a shared machine, users should also
  sign out of Microsoft 365 or use a private window.
- **First SSO login auto-provisions** the local profile — the one way an
  account comes to exist without an invitation or a share, since a password
  sign-in never creates one. An existing password account with the same
  email becomes a Microsoft account at its first Microsoft sign-in (below).
- **No local password for Microsoft accounts.** An account that has signed in
  with Microsoft and holds no local password cannot obtain one: "Reset
  password" mails it nothing, and a password change or an invitation for it
  is refused ("This account signs in with Microsoft and has no local
  password."). Multi-factor authentication and conditional access apply at
  sign-in, in Entra; a local password would bypass them.
- **While SSO is enabled, Microsoft accounts sign in with Microsoft only.**
  Once an account has signed in with Microsoft, a local password it also
  holds is refused at the password form (the same "Sign-in failed" line as a
  wrong password), "Reset password" mails it nothing, and a reset link mailed
  before is refused with "This account signs in with Microsoft and has no
  local password." The `ladmin` account is exempt. Nothing is deleted:
  **Disable SSO** and the password works again. Such a user can still set a
  local password through **Change Password** in the profile menu, but it
  stays unusable while SSO is enabled.
- **Disabling a user in Entra does not end an open session here.** There is
  no back-channel logout: an open PowerDataChat session lasts until the
  browser drops the cookie or its 30-day lifetime (`REMEMBER_ME_MAX_DAYS`)
  runs out. To end it at once, use **End sessions** on that user's row of the
  admin panel's **Users** page; **Remove** deletes the account and everything it
  owns, and a user still assigned in Entra would get a new, empty account at
  the next Microsoft sign-in.
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
| "Microsoft sign-in failed" after the Microsoft page | Check the container log (`/data/client/logs/datachat.log`, events `SSO_CALLBACK_FAILED` / `SSO_CALLBACK_NO_EMAIL`). A guest account without a usable `preferred_username`/`email` claim cannot sign in. |
| Token validation errors mentioning `iat`/`exp`/`nbf` | Clock skew — the container's clock must be NTP-accurate (JWT validation allows only small leeway). |
| Sign-in fails right after an image upgrade, at the return from Microsoft | The library that verifies the identity token changed with the dependency set, so an upgrade is the moment to re-test SSO. Grep `SSO_CALLBACK_FAILED` in `/data/client/logs/datachat.log` for the reason; `/?local=1` (above) keeps the password form reachable meanwhile. Report the log line rather than re-entering the credentials — a validation failure is not a configuration problem. |
| Everyone in the tenant can sign in | Enable **Assignment required** on the enterprise application (Step 1.6). |
| SSO stopped working after a key rotation | Rotating `CLIENT_ENCRYPTION_KEY` without `CLIENT_ENCRYPTION_KEY_OLD` makes the stored secret unreadable — the ladmin page shows "re-enter it"; paste the secret again. |
