# Customer install — PowerDataChat Client

Run the PowerDataChat **client** on your own Docker server. The client holds all
your raw data and runs entirely inside your network. No uploaded file, result
table, or rendered chart is ever transmitted to the PowerDataChat brain — see
"What leaves your network" below for the exact list of what does.

The install is **two containers**: the web application, and an analysis sandbox
where the Python that answers a question runs. Both come from PowerDataChat,
both are started together by Docker Compose, and neither is useful alone.

This is the short, operational quickstart. For build internals and the full
endpoint contract see [`docs/BUILD_AND_RUN.md`](docs/BUILD_AND_RUN.md).

## 1. Get the images

Two images make up one release and must always be installed together, at the
same tag:

| Image | What it is | Runs as |
|---|---|---|
| `powerdatachat-client` | The web application: your raw data, your users, the `/lab` UI. | uid **10001** |
| `powerdatachat-executor` | The analysis sandbox. The generated Python that answers a question runs here, never in the web container. | uid **10002** |

A stack with only the web image is not a reduced install, it is a broken one.
Every question in the chat is answered "The analysis service is not available
right now. Please try again in a moment or contact your administrator." and
nothing is analysed.

Either pull both from the registry PowerDataChat gave you:

```
docker pull <registry>/powerdatachat-client:<tag>
docker pull <registry>/powerdatachat-executor:<tag>
```

…or, for an air-gapped install, load the offline tarballs PowerDataChat sent:

```
docker load < pdc-client.tar.gz
docker load < pdc-executor.tar.gz
```

> The images are large. They ship pandas, matplotlib and plotly so your data
> is analysed and rendered locally; the web image additionally carries kaleido
> and python-pptx for chart and deck export.

## 2. Configure

```
cp client.env.example client.env
```

Edit `client.env` and fill in:

- **`BRAIN_TENANT_TOKEN`** — the token from the PowerDataChat admin panel (shown
  once at tenant creation).
- **`SECRET_KEY`** — generate once with `openssl rand -hex 32`; keep it stable so
  logins persist.
- **`CLIENT_ENCRYPTION_KEY`** — **set this at install time.** It encrypts every
  credential the admin panel stores at rest: database passwords ("Data
  sources") AND the Microsoft SSO client secret — without it those Save/Test
  buttons are disabled. Generate once with
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
  and keep it stable — changing it makes the stored secrets unreadable
  (they must be re-entered in the admin UI).
- **`LOCAL_ADMIN_PASSWORD`** — one-time bootstrap password for the local admin
  account `ladmin` (manages database sources). Only a hash is stored and the
  admin must change it on first login; once set, this variable is ignored.

`BRAIN_URL` is pre-filled and `DATA_ROOT=/data/client` should stay as-is.
`GCS_UPLOAD_BUCKET` is optional and should stay **unset**: it exists only for
PowerDataChat's own cloud-hosted demo (an ingress with a request-body cap);
on your installation every upload goes straight to the container, with no
size threshold.
**Never commit or share the filled-in `client.env`.**

Two more settings are worth knowing about, both optional:

- **`PDC_BACKEND_SUBNET`** — the address range of the private network the web
  application and the sandbox share, `192.168.255.240/28` by default. It is a
  **compose-level** variable, so set it in your shell or in a `.env` file next
  to `docker-compose.yml`, not in `client.env`. Change it only if that range
  collides with something in your own network; the symptom of a collision is
  described under "The two networks" in §3. One variable feeds both the
  network definition and the application setting that refuses traffic from it,
  so the two can never drift apart.
- **`ENABLE_THIRD_PARTY_SCRIPTS`** — leave it unset. By default the `/lab` page
  loads no third-party script at all: no browser analytics, no billing widget.
  Setting it to `true` restores them, which no on-premise install needs.
- **`CSP_REPORT_ONLY`** — leave it unset. Every page carries a
  Content-Security-Policy that the browser enforces. Setting this to `true`
  makes the browser only report violations in its console instead of
  blocking them, which is useful for a short diagnosis of a page that renders
  wrongly behind your proxy, and nothing else. The container logs
  `CSP_REPORT_ONLY_ENABLED` at startup while it is on.

Leave the `EXECUTOR_*` variables out of `client.env`. The ones that wire the
two containers together (`EXECUTOR_URL`, `EXECUTOR_SHARED_DIR`,
`EXECUTOR_MAX_CONCURRENT`, `EXECUTOR_NETWORK_CIDR`) are set in
`docker-compose.yml`, where compose `environment` overrides `env_file`. A
well-meant edit to the env file therefore cannot break the topology. Timeouts
and sizes stay tunable; `client.env.example` marks which is which.

### Connecting your own databases (optional)

The `ladmin` account can register tables from your PostgreSQL, MySQL/MariaDB,
SQL Server, Oracle, or ClickHouse databases so your users analyze them in
chats. ClickHouse needs nothing extra installed; it is reached over its
**native protocol — port 9440 with SSL, port 9000 plain** — and its databases
appear as "schemas" when your admin browses the connection. For a NEW ClickHouse
connection the form pre-fills 9440 with **SSL / Encrypt** ticked, and it shows a
warning when port 9000 is entered or SSL is off. A stored connection keeps the
port it has; a stored connection saved without a port keeps reaching 9000
unless SSL is on, and the form shows it the same way when you open it.
(SQL Server is the
one type with an image-build dependency: the Microsoft ODBC driver is installed
only on amd64/arm64 builds, and the admin panel greys the type out with a
reason if it is missing.) Table
data is snapshotted **inside your own `/data/client` volume**; no row of it is
transmitted. Column names, types, and the descriptions your admin confirms are
shared with the AI — see "What leaves your network" below.

**Ask your DBA to create a dedicated read-only database login for
PowerDataChat with SELECT-only grants** (ideally on a read replica). The
client only ever issues SELECT/introspection statements, and that grant is
your hard guarantee. Set the nightly snapshot-refresh time in the admin UI
(container-local time — set `TZ` in `client.env` if your server isn't UTC).

**What the "SSL / Encrypt" box enforces.** Ticked:

| Database | Ticked | Server identity |
|---|---|---|
| PostgreSQL | `sslmode=require`: a server that does not offer TLS is refused | Not verified. (The PostgreSQL client library upgrades `require` to certificate verification if a `root.crt` exists in the container user's `~/.postgresql`; this image does not ship one.) |
| MySQL / MariaDB | The connection is refused if the server did not negotiate TLS — the driver would otherwise carry on in plaintext. The check runs after sign-in, so it detects a server without TLS, not an attacker in the network path | Not verified |
| SQL Server | `Encrypt=yes` | Certificate verified, unless "Trust server certificate" is ticked |
| Oracle | TCPS (TLS) instead of plain TCP | Server certificate DN is matched, unless "Trust server certificate" is ticked |
| ClickHouse | Secure native protocol on port 9440 | Certificate verified, unless "Trust server certificate" is ticked |

Unticked, no database requires encryption and the connection normally runs in
plaintext. PostgreSQL alone still tries TLS first (`sslmode=prefer`) and falls
back to plaintext when the server does not offer it, without verifying
anything.

The form has no field for your own CA certificate today, so a verified
connection needs a server certificate issued by a CA the container already
trusts. If the database server's identity matters to you, ask your DBA for such
a certificate.

**Upgrade note for stored connections.** Two kinds of connection behave
differently from their next refresh on: a MySQL/MariaDB connection with SSL
ticked against a server that does not offer TLS now FAILS instead of silently
running in plaintext, and an Oracle connection with SSL ticked now switches to
TCPS (the server must offer TCPS on the configured port). A failed refresh
keeps the previous snapshot, so chats keep answering from it while you fix the
connection.

**Live tables.** A very large table can be marked **live** instead of being
snapshotted. The admin panel suggests it at or above
`LIVE_MODE_CELL_THRESHOLD` cells (rows x columns) and requires it at or above
`LIVE_MODE_FORCE_THRESHOLD`. To size a table, the register wizard runs an
exact `SELECT COUNT(*)` against it when it loads the table's structure and
again at save, each bounded at 60 seconds. Saving or refreshing a live table
reads a sample of up to 10 000 rows, also bounded at 60 seconds, to profile
it; no copy is taken. Scheduled refreshes skip live tables. Editing an
existing table keeps its storage mode.

A live table is queried directly in your database at question time. Each
question runs one SELECT per live table it uses — written by the AI planner
once that part ships; until then a capped read of the whole table, under the
administrator's row filter when one is set. The SQL is validated against a
read-only allowlist and may only touch the registered table; the result is
capped at `LIVE_RESULT_ROW_CAP` rows and `LIVE_RESULT_MAX_MB` megabytes, and
each read is bounded by `LIVE_QUERY_TIMEOUT_S` and by the connection's
statement timeout. Users see a note in the answer when a result was
truncated. The database login must therefore be read-only: SELECT only,
EXECUTE revoked from PUBLIC on functions and packages the login does not
need (a function called inside a SELECT can have side effects no query guard
can see), ideally on a read replica, with a database-side statement timeout
and a resource group / workload limit for the application account. The
application's own query guard and caps are a second line, not a replacement
for that grant.

Size the database side for the capped read. Until the planner writes SQL,
every question on a live table, every refresh of such an answer and every
"Download Excel" of it reads up to `LIVE_RESULT_ROW_CAP` rows of the whole
table (under the administrator's row filter when one is set) — that is the
load a read replica must carry. What is recorded: the SQL text of each
answer is stored with the chat's history on the data volume and is visible
to that chat's users (its owner and the people it was shared with); the
application log carries only a hash of each statement, row counts and
timings. There is no administrator-facing log of which user ran which query;
use the database's own audit for that. A user whose role no longer covers a
live table cannot re-run an answer that reads it: a chart or table refresh,
a dashboard tile refresh, "Show data" (on a chart or a dashboard tile) and
"Download Excel" refuse before any query reaches the database. If the role
check itself fails, these are refused on every chat that holds a live table
(on a chat without one they keep working).

### Users and sign-in

Nobody can create an account by typing an address at the sign-in page.
Accounts come to exist in three ways:

- **Invitation.** On the admin panel's **Users** page, `ladmin` enters an
  address and clicks **Invite user**. The account is created without a
  password and a link that sets one is mailed to the address. If the mail
  cannot be sent, the page says so; the colleague can still click **Reset
  password** on the sign-in page.
- **Sharing.** Sharing a chat, a conversation or a dashboard with a new
  address creates the same password-less account. The colleague sets a
  password through **Reset password**.
- **Single sign-on.** When Microsoft Entra ID sign-in is enabled, the first
  Microsoft sign-in creates the account (who may sign in is decided in
  Entra).

**Ending sessions and removing users.** Each row of the **Users** page has
two more actions, both confirmed in a dialog first. **End sessions** signs
the user out everywhere, on their next click; their password and their data
stay as they are, and they can sign in again. **Remove** deletes the account
together with every chat and dashboard it owns; the shares built on them
end (colleagues lose the chats and dashboards it shared with them, and a
tile pinned from one of its chats keeps its last picture but no longer
refreshes), and it cannot be undone — take a backup of the data volume first if you may
need anything back. The address is also taken off every chat and dashboard
other users shared with it, and loses its roles. Tables a removed power user
registered stay registered and keep working for everyone who has access, but
from then on count as registered by an administrator: only an administrator
can delete them. Who confirmed the descriptions and the audit log keep the
removed address. If the address is later used again — invited, shared with,
or signing in with Microsoft — it becomes a new account that inherits none of
this (the removal answer lists what was deleted and unshared; if a count is
lower than you expected, check the log before reusing the address), and no
session of the removed account works for it. A
Microsoft user who is still assigned in Entra gets such an account simply by
signing in again, so unassign them in Entra as well. Neither action can
target the `ladmin` account, and you cannot remove your own.

**Passwords** must be at least 8 characters (`PASSWORD_MIN_LENGTH` in
`client.env`; it cannot be set below 4). The rule applies whenever a password
is set: through a reset or invitation link, the forced change after a
temporary password, and **Change Password** in the profile menu. A password
set before this release keeps working until its owner changes it.

**Set `PUBLIC_BASE_URL`** in `client.env` on every install, to the address
your users type, e.g. `https://pdc.example.com`. Reset and invitation links
are built from it and from nothing else. Without it no reset or invitation
mail is sent at all: the sign-in page still answers normally, an invite
reports that no mail went out, and the log shows `PUBLIC_BASE_URL_UNSET` at
start-up. The application deliberately never takes the link's address from
the incoming request, because a caller controls that address and could
otherwise have a genuine reset mail point at a server of their choosing.

**Password reset.** "Reset password" mails a link valid for 30 minutes that
works once. The sign-in page gives the same answer whether or not the address
has an account. The link's token appears in the browser history and in your
proxy's access log, like any URL (the
application's own access log shows it as `/auth/reset/<redacted>`); single use
and the 30-minute lifetime are the protection.

**Sessions.** Setting a new password — through a reset link, the forced
change or **Change Password** — signs the account out everywhere else; the
browser that made the change stays signed in. Every session ends 30 days
after sign-in (`REMEMBER_ME_MAX_DAYS`), whether or not "Remember me" was
ticked and however often it is used; "Remember me" only decides whether the
session survives closing the browser.
"Sign out" ends the session of this browser only. A copy of the session
cookie taken from that browser keeps working until its 30-day end. A password
set from inside the container by an operator ends the other sessions only
after the web container restarts. To sign one user out everywhere — a lost
laptop, a copied cookie — use **End sessions** on the **Users** page; a
browser whose session has ended shows the sign-in page on its next action.

**Microsoft accounts have no local password.** An account that has signed in
with Microsoft and never had a password here cannot get one: "Reset password"
sends it nothing (the page answers as for any address), and a password change
or an invitation for it is refused with "This account signs in with Microsoft
and has no local password." Such users get multi-factor authentication and
conditional access from Entra; a local password would let them sign in
without either. The same holds, while single sign-on is enabled, for an
account that had a password here and has since signed in with Microsoft:
its password is refused at the sign-in page (the same "Sign-in failed" line
as a wrong password), "Reset password" sends it nothing, and a reset link
mailed earlier is refused. Such users sign in with Microsoft. The `ladmin`
account is exempt. The password is not deleted: switching single sign-on
off makes it work again.
This rule starts at an account's first Microsoft sign-in and not before.
Enabling single sign-on does not stop anyone who has a password here and has
never signed in with Microsoft: they keep signing in with that password, and
Entra's multi-factor authentication and conditional access do not apply to
them until they sign in with Microsoft once. If every user must go through
Entra, ask them to sign in with Microsoft once after you enable it.
Disabling a user in Entra does not end a PowerDataChat session that is
already open: there is no back-channel logout. The session lasts until the
browser drops the cookie or its 30 days run out, unless you also use **End
sessions** for that user on the **Users** page.
If single sign-on is later switched off, an account that signed in with
Microsoft and never had a password here cannot sign in at all; while it stays
on, the same is true of any Microsoft user who leaves your Entra tenant but
still needs access. Recover such an account by hand: remove the
`sso_provider` key from `users/<email>/auth.json` on the data volume, restart
the web container, then — unless the account already had a password — send
the user a reset link (the admin panel's **Invite user**, or "Reset
password" on the sign-in page).
The `ladmin` password cannot be reset by mail — to recover it, delete
`users/ladmin/auth.json` on the data volume and restart with
`LOCAL_ADMIN_PASSWORD` set.

**Repeated failures are slowed down, then locked.** After 5 failed attempts
for one address within 15 minutes, the next four attempts must wait 1, 2, 4
and 8 seconds, and if the last of them fails too, sign-in for that address is
locked for 15 minutes; an attempt made too early or during the lock is
answered "Too many attempts" with a `Retry-After` header.
Password-reset requests have their own count per address. Attempts from one
network address are slowed the same way after 20 failures, but never locked,
so a proxy that hides your users' addresses cannot lock the whole company
out. Because anyone can trigger the per-address lock, a colleague who meets
it waits 15 minutes or signs in with Microsoft, which is not limited here.
The `ladmin` account is never locked, so nobody can lock the operator out: its
attempts are only spaced, one every 8 seconds at most after the first five
failures. Give it a long `LOCAL_ADMIN_PASSWORD`. The password-length rule
does not apply to that bootstrap value; if it is shorter than
`PASSWORD_MIN_LENGTH`, the startup log shows the warning
`LADMIN_BOOTSTRAP_WEAK`. While its sign-in is being spaced the log shows
`AUTH_ADMIN_SPACED` (once per 15-minute window); watch for it, because
`AUTH_LOCKOUT` never fires for this account. Restarting the web container
clears every counter and lifts any lock.
The numbers are `AUTH_FAIL_THRESHOLD`, `AUTH_FAIL_THRESHOLD_IP`,
`AUTH_FAIL_WINDOW_S` and `AUTH_LOCKOUT_S` in `client.env`. The counters live in
the web process: they reset on restart and assume the single web worker this
product runs (see "Concurrency and waiting"). Keep a rate limit on your
reverse proxy as well.

**Addresses.** An address must use letters, digits and `. _ % + -` before the
`@`. An existing account whose address uses another character, such as an
apostrophe, can no longer sign in with a password or receive a share; give
that user a new address. Single sign-on lets such a user sign in, but shares
and invitations to that address are still refused.

### Single sign-on with Microsoft Entra ID (optional)

After install, the `ladmin` account can connect your Microsoft Entra ID
(Azure AD) tenant from the admin panel's **Single sign-on** page so employees
sign in with their Microsoft 365 identity — see
[`docs/SSO_MICROSOFT.md`](docs/SSO_MICROSOFT.md).

## 3. Run

Use Docker Compose. [`docker-compose.yml`](docker-compose.yml) in this repo is
the supported way to run the product: it starts both containers, puts them on
the two networks described below, shares the one directory they need to share,
and applies every hardening flag. A hand-written `docker run` of a single image
cannot answer a question, so there is no single-container form to fall back
to.

```
docker compose up -d
docker compose ps        # both services up, `executor` reported healthy
curl http://localhost:8000/health
```

The web container listens on port **8000** and keeps all state in
**`/data/client`**, mounted from the persistent `pdc_client_data` volume so
nothing is lost on restart or upgrade. The sandbox publishes no port.

### How the two containers are locked down

Both images ship hardened, and the lines in `docker-compose.yml` are what
enforce it at run time. Keep them.

| Restriction | Web application | Analysis sandbox |
|---|---|---|
| Unprivileged user | uid/gid **10001** (`pdc`), never root | uid **10002** (`pdcexec`) in gid 10001, never root |
| Read-only container filesystem | Yes: it cannot modify its own code or image | Yes |
| Writable scratch | `/tmp` on a 512 MB RAM disk: chart-rendering caches and the temporary copy of every file being uploaded | `/tmp` on a 1 GB RAM disk, plus the shared jobs volume at `/jobs` — the two places generated code can write (see "The shared jobs volume") |
| All Linux capabilities dropped | Yes | Yes |
| `no-new-privileges` | Yes | Yes |
| Memory and process caps | 4 GB, 512 processes | 3 GB, 256 processes; each job's own address space is capped at `EXECUTOR_MEM_LIMIT_MB` (2048) |
| Persistent state | Uploads, chats, history, snapshots, rendered decks **and the application log** under `/data/client` only | No data volume. Its only mount is the shared jobs volume at `/jobs`, which holds work in flight (see "The shared jobs volume") |
| Network | The published port, outbound HTTPS to the brain, TCP to your databases | The private sandbox network only, which has no gateway |

Both `/tmp` filesystems are RAM-backed and charged against the container's own
memory cap, and both are wiped on every restart.

Raise the web container's `mem_limit` if your users analyse very large files,
and raise its `/tmp` size with it. An upload is written to `/tmp` before it is
stored, so a single batch of uploaded files must fit in that 512 MB; users
uploading larger files get an upload error. Keep `/tmp` well below `mem_limit`.

**Host sizing.** Both containers run at once, so size the host for the sum:
roughly **8 GB** of headroom, rather than the 4 GB a single container needed.

The log is at `/data/client/logs/datachat.log` on the volume, so it survives
restarts and upgrades. The sandbox keeps no log on any volume. See
"Collecting logs" below.

### The two networks

The web container joins two networks. The sandbox joins only one:

- the **normal** network carries your users' browsers on the published port,
  the outbound HTTPS calls to `BRAIN_URL`, and the TCP connections to your
  databases;
- the **sandbox** network is marked internal, which means Docker gives it no
  gateway. Code running in the sandbox therefore has no route to your LAN, to
  any database, or to the internet. That is the containment, not a
  convenience. Do not publish the sandbox's port, and do not attach it to
  another network.

Nothing but these two services may join the internal sandbox network
(`backend` in `docker-compose.yml`). Point monitoring at `/health` over the
normal network: a monitoring sidecar attached to `backend` gets HTTP 403, like
anything else in that range.

`PDC_BACKEND_SUBNET` pins the sandbox network's address range
(`192.168.255.240/28` by default) and the same value reaches the web
application as `EXECUTOR_NETWORK_CIDR`. The application **refuses any request
whose peer address falls inside that range** and answers HTTP 403. The reason
is that a Docker network is bidirectional and the sandbox runs code written by
a language model. Without the refusal, that code could call endpoints that
need no session, such as login and password reset.

**Check the range against your own addressing before the first install.**
Docker does not verify a subnet you specify against the host's routes, so a
collision is never reported. What you would see instead: users whose own
machines hold addresses inside that range get a bare HTTP 403 from `/lab`,
with nothing on the page explaining it, while everyone else works normally.
Change `PDC_BACKEND_SUBNET` to a range you do not use, and both halves move
together: the network Docker creates, and the range the application refuses.
One variable, so they cannot disagree.

### The shared jobs volume

One volume, `pdc_client_exec_jobs`, is mounted at `/jobs` in **both**
containers. It holds one directory per in-flight job: the input tables for
that question and the result. Each directory is deleted as soon as its answer
has been read.

Two things follow that are worth knowing before you plan backups. The
directory has to be writable by the sandbox, because the sandbox deletes its
own finished jobs — so analysis code can also write straight into it, and a
file it leaves there is visible to the next question and to the web container
until a sweep removes it (both sides sweep strays on the same hourly schedule
as abandoned job directories). And this volume is on disk, not in memory, so
unlike the container's scratch space a restart does not clear it. Treat it as
shared working space for questions in flight: **do not** include it in a
backup you intend to restore elsewhere, because it can contain fragments of
the tables a question was analysing, and do not put anything of your own in
it.

The volume can also hold one question's input tables for a while after the
question ends — for example a job directory the sandbox locked, which its own
sweep removes within about an hour. So exclude `pdc_client_exec_jobs` from host
backups, and remove it with `docker volume rm pdc_client_exec_jobs` (stack
stopped) after a failed upgrade or when decommissioning the install; it is
recreated empty on the next start (check the ownership note below).

**Nothing under `/data/client` is mounted into the sandbox**: not your users,
not chats, not the database snapshots, not the credential store, not the log.
The tables for one question are written out per job instead, which is what
keeps per-role table permissions meaningful: the sandbox only ever sees the
data that question was allowed to use.

The root of that volume must be owned `root:<gid 10001>` with mode `2770`
before the first job runs, so that the web uid and the sandbox uid can both
work in it. A volume the sandbox image creates comes up correct. A volume that
was first written by the web image, or restored from a backup, may not, and
neither container can repair it, because both are non-root with every
capability dropped. The repair is one throwaway command:

```
docker run --rm --user 0 -v pdc_client_exec_jobs:/jobs \
  powerdatachat-executor:<tag> sh -c "chown 0:10001 /jobs && chmod 2770 /jobs"
```

Symptom if it is wrong: the stack never becomes healthy and
`docker logs pdc-executor` shows a permission error on the jobs directory
(`EXECUTOR_SHARED_DIR_NOT_WRITABLE`). If only group write is missing, the web
log reports `EXECUTOR_SHARED_DIR_NOT_GROUP_WRITABLE` at startup and every
question fails. Both point at the same repair.

### Startup order

The web container waits for the sandbox to report healthy before it starts.
On a current Docker engine the sandbox's health probe begins shortly after
launch, so this costs seconds. On an engine too old to honour the image's
start-interval setting, the first probe is only asked after a full 30-second
interval, which is slower and nothing worse.

After a host reboot the restart policy may bring the web container up first.
Nothing needs intervention: it greets the sandbox again on the first question
it handles.

### Concurrency and waiting

The sandbox runs **one job at a time**, and `EXECUTOR_MAX_CONCURRENT` must stay
`1` on both services. Raising it forfeits the isolation the sandbox exists for.

The web service itself runs **one worker and one replica**. Keep it that way:
charts are handed to the browser through a short-lived in-memory store, so a
second worker or a load-balanced second container would show blank charts
whenever the browser's two requests land on different processes. The sign-in
attempt counters and the reset-link lookup live in the same process for the
same reason.
One job at a time is what makes "no other process shares this identity" true.

Two honest consequences. Chart rendering is serialized, so a question that
draws several charts renders them one after another. And a question that waits
longer than `EXECUTOR_QUEUE_MAX_S` (600 seconds by default) for a free slot is
answered "The analysis service is busy right now. Please try again in a
moment." rather than being told its code was too slow.

Those are the sentences a chat user sees. The log, and the narrower paths that
re-run one stored block (a per-chart or per-table refresh, a dashboard tile,
a report, an Auto Analytics deck), carry the raw internal form instead:
`ExecutorBusy: ...` and `ExecutorUnavailable: ...`. Same two conditions, two
different audiences, so quote whichever matches where you read it.

### Collecting logs

The two containers log in two different places, and a failing question usually
needs both.

| Where | What is in it | Survives a restart |
|---|---|---|
| `/data/client/logs/datachat.log` on the data volume | The web application: uploads, brain calls, errors. Rotated, so collect `datachat.log*` | Yes |
| `docker logs pdc-executor` | The sandbox: one line per job, the traceback of a failing analysis block, and anything the generated code printed | Only as long as Docker keeps the container's output |

The sandbox writes a file log too, but it lands on its RAM-backed `/tmp` and
is on no volume, so it is **lost on restart**. Treat `docker logs
pdc-executor` as the evidence path and capture it before restarting anything.

### Restrict what the containers can reach (recommended)

The web application needs outbound HTTPS to your `BRAIN_URL` and, if you
register database tables, TCP to those database hosts. Nothing else. On a
security-sensitive network apply a default-deny egress rule on the host or
firewall and allow only:

- `BRAIN_URL` on port 443,
- each registered database host on its configured port,
- your internal DNS and NTP servers.

That allow-list applies to the **web container only**. The sandbox needs no
egress rule at all, because it has no route to anywhere: its only network is
internal and has no gateway. There is nothing to permit and nothing to block.

Inbound, only the port you publish needs to be reachable by your users — 8000,
or the HTTPS port of the reverse proxy in front of it. The sandbox publishes
no port.

### Serve it over HTTPS

The session cookie is marked `Secure`, so browsers return it only over HTTPS
(`http://localhost` is exempt). Put the container behind your own TLS
terminator — a reverse proxy or load balancer with your certificate — and
publish that HTTPS address to your users.

If you must run plain HTTP on the LAN, set `SESSION_HTTPS_ONLY=false` in
`client.env`. Sessions then travel unencrypted and can be captured on your
network; only do this on an isolated segment or for a short evaluation.

**Forward your users' addresses.** The sign-in limits count failures per
network address as well as per account. Behind a proxy that address is the
proxy's own unless `FORWARDED_ALLOW_IPS` names the proxy and the proxy sets
`X-Forwarded-For`. Without that, every user shares one address, which only
slows everyone down after 20 failures in 15 minutes; it never locks anyone
out.

**Do not tell the application to trust every proxy.** uvicorn rewrites the
client address of a request from its `X-Forwarded-For` header for any peer
listed in `FORWARDED_ALLOW_IPS`. With `FORWARDED_ALLOW_IPS=*` the sandbox
could present any address it likes and slip past the refusal described under
"The two networks". Leave the variable unset, which trusts only localhost, or
list your proxy's own address explicitly. Never include the backend subnet,
and never use the wildcard.

**Give the proxy a long read timeout.** A question can wait up to
`EXECUTOR_QUEUE_MAX_S` (600 seconds by default) for a free analysis slot with
nothing sent on the response stream. A proxy that gives up first shows the
user a gateway error on a question that was about to be answered. In nginx:

```
proxy_read_timeout 900s;
```

Set it comfortably above `EXECUTOR_QUEUE_MAX_S`, or lower that setting to fit
the timeout you already have.

**Pass the application's security headers through unchanged.** Every page
carries a `Content-Security-Policy` header with a value that changes on each
response. A proxy that strips it, caches pages, or adds a second policy of its
own can leave charts blank or the page unprotected. Do not cache HTML
responses, and do not rewrite the header.

## 4. Verify

- Open `http://<host>:8000` → sign in as `ladmin` with `LOCAL_ADMIN_PASSWORD`
  → you land on the admin panel; invite a first user from **Users** and check
  that the invitation mail arrives with a link to your `PUBLIC_BASE_URL`.
- Check the health endpoint:

  ```
  curl http://<host>:8000/health
  ```

  A healthy install shows:

  ```json
  {"status":"ok","brain_reachable":true,"tenant_token_configured":true,
   "executor_reachable":true,"executor_checked_at":1770000000.0}
  ```

  If `brain_reachable` is `false`, check outbound HTTPS to `BRAIN_URL`. If
  `tenant_token_configured` is `false`, `BRAIN_TENANT_TOKEN` is empty in
  `client.env`. If `executor_reachable` is `false`, the sandbox is down or
  unreachable and no question can be answered; `executor_reachable` is `null`
  until the web application has spoken to it for the first time.

  `executor_checked_at` is when that observation was made. The value is a
  cached observation refreshed in the background, not a live probe, so it can
  lag reality by a few seconds.

- **"Healthy" is not enough. Read the body.** `/health` answers 200 whether
  the sandbox is up or not, deliberately: an operator needs a page that says
  what is wrong. So a stack whose sandbox is down passes the container health
  check, looks healthy in `docker compose ps`, and answers no questions at
  all. This is the second thing in this document that a green health check
  does not prove; the volume-ownership step under "Upgrades" is the other.
  Judge an install by the JSON body and by asking a real question, never by
  the HTTP status.

- Confirm the sandbox is actually answering, not merely running:

  ```
  docker compose ps executor          # State "Up (healthy)"
  ```

  then ask one question in `/lab` that produces a number or a chart. That is
  the only check that exercises the whole path: a table is written to the jobs
  volume, the sandbox runs the code, and the result comes back.

## What leaves your network

No uploaded file, DataFrame, query result set, or rendered chart is ever
transmitted. What does cross to the brain over HTTPS: the question text, schema
and column names and descriptions, capped aggregate profile statistics (at most
5 top values per column, 40 characters each), scalar result previews, and answer
text truncated to 500 characters for reports. These can contain individual
values derived from your data. User email is sent for tenant routing.

Everything in the list below is sent by the web application. Two things are
worth stating plainly:

- **The browser sends nothing to a third party either.** By default the `/lab`
  page loads no third-party script: no analytics, no billing widget, no
  external font or chart CDN. Your users' browsers talk only to your own
  server. (`ENABLE_THIRD_PARTY_SCRIPTS=true` would restore the analytics and
  billing scripts; no on-premise install needs it.)
- **The analysis sandbox sends nothing anywhere.** It has no route out of its
  internal network, no credentials and no database driver, so the container
  that handles your data most directly cannot transmit it at all.

### Data that leaves the container

| What | Sent when | Shape |
|---|---|---|
| Question text | Every question | Verbatim, as typed |
| Conversation history | Every question | Past turns: role, content, generated code |
| Schema text and column metadata | Every question | Table/sheet names, column names, dtypes, the descriptions your admin confirms, cardinality and truncated unique-value hints |
| Dataset profile | Every question | Row/duplicate counts, null rates, min/max, constant and all-unique flags, up to 5 top values per column truncated to 40 characters |
| Generated code and execution errors | Every question and retry | Python source, traceback text |
| Excel header text above a table | Upload, when a sheet has text above the table and no description | The extracted text VERBATIM, truncated to 2000 characters — it is read from your file, so treat it as content |
| File and sheet names | Upload and every question | The name as STORED after sanitization, plus sheet names |
| Share invitations | Sharing a chat or dashboard | Recipient addresses, the item title, and the note the sender types |
| Scalar result previews | Summarize step | Single text/number/true-false values only — tables and DataFrames are dropped |
| Answer text | Report generation | Question and answer truncated to 500 characters, code snippet to 300 |
| Table column names | Report generation | Column NAMES only, first 10 — never rows |
| User email | Every call | Tenant routing and per-user activity |
| Activity events | Login, upload, chat, report | Event name, user email, lightweight counters |
| Password-reset payload | Password reset and invitation | The e-mail address, whether it is a reset or an invitation, and a single-use reset link (valid 30 minutes), relayed through the brain's mail service; the brain never logs or stores the link. Until it is used or expires the link sets the account's password, so the operator of the brain and of its mail relay is trusted with it, as with any mailed reset link |
| Third-party browser scripts | Never, by default | The `/lab` page loads no analytics or billing script; the browser contacts only your own server |

Never sent: uploaded files, DataFrames, query result sets, rendered charts or
decks, and your branded templates.

## Notes

- **One tenant token per customer.** If service stops with `403`, your token may
  have been revoked — contact PowerDataChat.
- **Your data stays yours.** Raw uploads, chats, and rendered decks live only in
  the `/data/client` volume on your server and are never transmitted. See
  "What leaves your network" for exactly what does reach the brain.
- **Sharing rules your users will meet.**
  - Sharing with a colleague who has never signed in creates an account for
    that address with no password. The colleague signs in by clicking "Reset
    password" first and setting a password through the mailed link; typing a
    password at the sign-in page is refused. Nobody else can claim the share
    by signing in with that address first.
  - Sharing a dashboard gives the recipients access only to the chats the
    dashboard's owner owns. A tile pinned from a chat that was itself shared
    with the owner shows its saved picture to the recipients, but cannot be
    refreshed by them.
  - Only a chat's owner can share one of its conversations.
  - A colleague a chat is shared with can read it, ask new questions in it,
    refresh its charts and pin them, but cannot edit its descriptions, add
    data, start Auto Analytics or share it on. The shared chat appears in
    their chat list.
  - Charts and styled tables that anyone shares are shown in an isolated
    frame or reduced to plain table formatting, so what one user shares
    cannot act in another user's browser session.
- **A forced password change blocks everything until it is done.** After the
  first administrator sign-in with the bootstrap password (or a sign-in with a
  temporary password mailed by a release before this one) the user must set a
  new password before anything else works: the
  pages redirect to the change form, and every data request is refused with
  "Password change required" until the change is made.
- **Upgrades:** pull or load the new tag of **both** images, update the tag in
  your `docker-compose.yml`, then `docker compose up -d`. The two images belong
  to one release, so upgrading only one leaves the pair mismatched. The
  `pdc_client_data` volume (your data) is preserved across upgrades.
- **Upgrading an install that predates the analysis sandbox.** Nothing to
  migrate, and no data touched. Pull or load both images, take the new
  `docker-compose.yml` from PowerDataChat (it carries the second service, the
  two networks and the shared jobs volume), and `docker compose up -d`. The
  jobs volume is created empty on first start and holds only work in flight,
  so it needs no backup — and per "The shared jobs volume" above, it is
  better left out of one. Read the ownership note under "The shared jobs volume"
  first: a jobs volume that the web container creates before the sandbox has
  ever run may come up with the wrong owner, and the one-line repair is there.
  Any `EXECUTOR_*` values you may have put into `client.env` while there was
  no sandbox can be deleted. Compose sets the ones that matter.
- **ONE-TIME STEP when upgrading an install created before this release.** The
  container now runs as user 10001 instead of root, and an existing data volume
  is still owned by root, so the new container cannot write to it. Hand the
  volume over ONCE, with the container stopped:

  ```
  docker volume ls                     # find YOUR data volume's real name
  docker run --rm -v <that name>:/data alpine chown -R 10001:10001 /data
  ```

  **Check the name first.** Compose prefixes a volume with the project name,
  which is taken from the directory the compose file sits in, so the data
  volume is usually `<project>_pdc_client_data` rather than the bare name.
  Running the command against a name that does not exist does not fail: Docker
  CREATES an empty volume, repairs that, and leaves the real one untouched, so
  the symptom afterwards looks exactly like not having run it at all. (The
  jobs volume is the exception — it carries an explicit name and is
  `pdc_client_exec_jobs` on every install.)

  Skip it and the container still starts AND still reports healthy — `GET
  /health` returns 200 — while every write fails: `docker logs pdc-client`
  shows `LADMIN_BOOTSTRAP_FAILED` and `Permission denied`, and users get a 500
  when they try to sign in. Do not judge this upgrade by the health check. A
  volume created by this release or later already has the right owner. Run the
  command again after restoring a backup taken from an older install.
- **Rolling back after that step.** Going back to an image from before this
  release still works — it runs as root, which ignores file ownership. But
  everything it writes from then on belongs to root again, so if you later move
  forward to this release a second time, run the `chown` command again.
- **Rolling back is also safe for the cached data files.** This release ships a
  newer Parquet library, and the database snapshots and parse caches it writes
  are read back correctly by the older library in the previous image (checked in
  both directions before release). Even if a file were unreadable, nothing is
  lost: a snapshot
  is re-created by the next refresh and a parse cache is rebuilt from your
  original upload.
- **The log moved onto your data volume.** It is now
  `/data/client/logs/datachat.log` (it used to live inside the container),
  because the container filesystem is read-only. Collect `datachat.log*` from
  the volume.
- **BREAKING on upgrade if you serve plain HTTP.** From this release the
  session cookie is marked `Secure`, so a browser will not send it back over
  `http://`. Users on an HTTP install see the login form again after signing in
  — an endless login loop with nothing in the log. It does NOT reproduce on the
  installer's own laptop, because browsers exempt `http://localhost`. Before
  upgrading, either front the container with TLS (see "Serve it over HTTPS") or
  add `SESSION_HTTPS_ONLY=false` to `client.env`.
- **Test single sign-on right after this upgrade if you use it.** The library
  that validates the Microsoft identity token changed in this release. Sign-in
  through Microsoft is exercised end to end for the first time on your own
  tenant, so have an administrator complete one Microsoft sign-in immediately
  after upgrading rather than discovering it on Monday morning. If it fails,
  `https://<your-host>/?local=1` always shows the email-and-password form, so
  `ladmin` can still get in and switch single sign-on off (which gives every
  account that had a password here its password back) while you contact
  PowerDataChat. While single sign-on stays on, that form refuses the
  password of any account that has signed in with Microsoft. The log line
  to quote is `SSO_CALLBACK_FAILED` in `/data/client/logs/datachat.log`.
- **Security fixes in the Python layer arrive only as a new image tag.** Both
  container filesystems are read-only and both run as unprivileged users, so
  nothing inside a running container can install or change a package. That is
  by design, not an oversight. Every dependency of both images is pinned to an
  exact version and the whole set is scanned before a release, which is what
  makes an upgrade reproducible. When you receive a CVE notice about a Python
  package, the fix is a new tag from PowerDataChat plus the upgrade above; do
  not try to install anything into either container.
