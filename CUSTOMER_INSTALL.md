# Customer install — PowerDataChat Client

Run the PowerDataChat **client** on your own Docker server. The client holds all
your raw data and runs entirely inside your network. No uploaded file, result
table, or rendered chart is ever transmitted to the PowerDataChat brain — see
"What leaves your network" below for the exact list of what does.

This is the short, operational quickstart. For build internals and the full
endpoint contract see [`docs/BUILD_AND_RUN.md`](docs/BUILD_AND_RUN.md).

## 1. Get the image

Either pull it from the registry PowerDataChat gave you:

```
docker pull <registry>/powerdatachat-client:<tag>
```

…or, for an air-gapped install, load the offline tarball PowerDataChat sent:

```
docker load < pdc-client.tar.gz
```

> The image is large — it ships pandas, matplotlib, plotly, kaleido, and
> python-pptx so your data is analysed and rendered locally.

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

### Connecting your own databases (optional)

The `ladmin` account can register tables from your PostgreSQL, MySQL/MariaDB,
SQL Server, Oracle, or ClickHouse databases so your users analyze them in
chats. ClickHouse needs nothing extra installed; it is reached over its
**native protocol — port 9000 plain, port 9440 when you tick SSL** (the form
pre-fills 9000, so change it yourself for a TLS connection) — and its databases
appear as "schemas" when your admin browses the connection. (SQL Server is the
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

### Single sign-on with Microsoft Entra ID (optional)

After install, the `ladmin` account can connect your Microsoft Entra ID
(Azure AD) tenant from the admin panel's **Single sign-on** page so employees
sign in with their Microsoft 365 identity — see
[`docs/SSO_MICROSOFT.md`](docs/SSO_MICROSOFT.md).

## 3. Run

The container listens on port **8000** and keeps all state in **`/data/client`**.
Mount a persistent volume there so nothing is lost on restart/upgrade:

```
docker run -d --name pdc-client -p 8000:8000 \
  --env-file client.env \
  -v pdc_client_data:/data/client \
  --read-only --tmpfs /tmp:size=512m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --memory 4g --pids-limit 512 \
  powerdatachat-client:<tag>
```

(Prefer Docker Compose? See [`docker-compose.yml`](docker-compose.yml) in this
repo — `docker compose up -d`. It applies the same restrictions, so prefer it
over a hand-written `docker run`.)

### How the container is locked down

The image ships hardened, and the flags above — or the equivalent lines in
`docker-compose.yml` — are what enforce it at run time. Keep them.

| Restriction | What it means |
|---|---|
| Runs as an unprivileged user | uid/gid **10001** (`pdc`), never root. |
| Read-only container filesystem | The application cannot modify its own code or image. |
| `/tmp` on a 512 MB RAM disk | The only writable scratch space: chart-rendering caches, and the temporary copy of every file being uploaded. Wiped on every restart. |
| All Linux capabilities dropped | No raw sockets, no mounting, no privileged operations. |
| `no-new-privileges` | A process inside can never gain more rights than it started with. |
| Memory capped at 4 GB, 512 processes | A runaway analysis cannot exhaust the host. |
| All state on the data volume | Uploads, chats, history, snapshots, rendered decks **and the application log** live under `/data/client` only. |

Raise `--memory` if your users analyse very large files — and raise the
`/tmp` size with it. An upload is written to `/tmp` before it is stored, so a
single batch of uploaded files must fit in that 512 MB; users uploading larger
files get an upload error. Both values are RAM, so raise them together and keep
`/tmp` well below `--memory`.

The log is at `/data/client/logs/datachat.log` on the volume, so it survives
restarts and upgrades.

### Restrict what the container can reach (recommended)

The client needs outbound HTTPS to your `BRAIN_URL` and, if you register
database tables, TCP to those database hosts. Nothing else. On a
security-sensitive network apply a default-deny egress rule on the host or
firewall and allow only:

- `BRAIN_URL` on port 443,
- each registered database host on its configured port,
- your internal DNS and NTP servers.

Inbound, only the port you publish needs to be reachable by your users — 8000,
or the HTTPS port of the reverse proxy in front of it.

### Serve it over HTTPS

The session cookie is marked `Secure`, so browsers return it only over HTTPS
(`http://localhost` is exempt). Put the container behind your own TLS
terminator — a reverse proxy or load balancer with your certificate — and
publish that HTTPS address to your users.

If you must run plain HTTP on the LAN, set `SESSION_HTTPS_ONLY=false` in
`client.env`. Sessions then travel unencrypted and can be captured on your
network; only do this on an isolated segment or for a short evaluation.

## 4. Verify

- Open `http://<host>:8000` → enter your work email → you land in `/lab`.
- Check the health endpoint:

  ```
  curl http://<host>:8000/health
  ```

  A healthy install shows:

  ```json
  {"status":"ok","brain_reachable":true,"tenant_token_configured":true}
  ```

  If `brain_reachable` is `false`, check outbound HTTPS to `BRAIN_URL`. If
  `tenant_token_configured` is `false`, `BRAIN_TENANT_TOKEN` is empty in
  `client.env`.

## What leaves your network

No uploaded file, DataFrame, query result set, or rendered chart is ever
transmitted. What does cross to the brain over HTTPS: the question text, schema
and column names and descriptions, capped aggregate profile statistics (at most
5 top values per column, 40 characters each), scalar result previews, and answer
text truncated to 500 characters for reports. These can contain individual
values derived from your data. User email is sent for tenant routing.

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
| Password-reset payload | Password reset only | The e-mail address and a temporary password, relayed through the brain's mail service (a tokenized reset link replaces this in a future release) |

Never sent: uploaded files, DataFrames, query result sets, rendered charts or
decks, and your branded templates.

## Notes

- **One tenant token per customer.** If service stops with `403`, your token may
  have been revoked — contact PowerDataChat.
- **Your data stays yours.** Raw uploads, chats, and rendered decks live only in
  the `/data/client` volume on your server and are never transmitted. See
  "What leaves your network" for exactly what does reach the brain.
- **Upgrades:** pull/load the new image tag, then `docker rm -f pdc-client` and
  re-run step 3 with the new tag. The `pdc_client_data` volume (your data) is
  preserved across upgrades.
- **ONE-TIME STEP when upgrading an install created before this release.** The
  container now runs as user 10001 instead of root, and an existing data volume
  is still owned by root, so the new container cannot write to it. Hand the
  volume over ONCE, with the container stopped:

  ```
  docker run --rm -v pdc_client_data:/data alpine chown -R 10001:10001 /data
  ```

  Skip it and the container starts but fails on its first write: `docker logs
  pdc-client` shows `Permission denied`, and users can neither sign in nor
  upload. A volume created by this release or later already has the right
  owner. Run it again after restoring a backup taken from an older install.
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
