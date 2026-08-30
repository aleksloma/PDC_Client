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
signs in with its password) and as the recovery path if the SSO
configuration ever breaks while auto-redirect is on. **Disable SSO** on the
ladmin page returns the landing page to the plain password form instantly.

## Behavior details

- **Sessions**: an SSO sign-in uses a browser-session cookie — the session
  ends when the browser closes, and sign-in is automatic again on the next
  visit (Microsoft re-authenticates silently on Entra-joined devices).
- **Logout is local-only** (intentional): *Sign out* ends the PowerDataChat
  session but not the Microsoft browser session — PowerDataChat performs no
  Microsoft front-channel logout. On a shared machine, users should also
  sign out of Microsoft 365 or use a private window.
- **First SSO login auto-provisions** the local profile. Existing
  password accounts with the same email simply gain SSO — their password
  keeps working, and password reset flows are untouched.
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
| "Microsoft sign-in failed" after the Microsoft page | Check the container log (`logs/datachat.log`, events `SSO_CALLBACK_FAILED` / `SSO_CALLBACK_NO_EMAIL`). A guest account without a usable `preferred_username`/`email` claim cannot sign in. |
| Token validation errors mentioning `iat`/`exp`/`nbf` | Clock skew — the container's clock must be NTP-accurate (JWT validation allows only small leeway). |
| Everyone in the tenant can sign in | Enable **Assignment required** on the enterprise application (Step 1.6). |
| SSO stopped working after a key rotation | Rotating `CLIENT_ENCRYPTION_KEY` without `CLIENT_ENCRYPTION_KEY_OLD` makes the stored secret unreadable — the ladmin page shows "re-enter it"; paste the secret again. |
