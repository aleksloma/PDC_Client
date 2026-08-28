# Customer install — PowerDataChat Client

Run the PowerDataChat **client** on your own Docker server. The client holds all
your raw data and runs entirely inside your network; only no-value metadata plus
your per-tenant bearer token ever reach the PowerDataChat brain.

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
- **`CLIENT_ENCRYPTION_KEY`** — only needed if you will connect your own
  databases ("Data sources"). Generate once with
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
  and keep it stable — changing it makes stored database passwords unreadable
  (they must be re-entered in the admin UI).
- **`LOCAL_ADMIN_PASSWORD`** — one-time bootstrap password for the local admin
  account `ladmin` (manages database sources). Only a hash is stored and the
  admin must change it on first login; once set, this variable is ignored.

`BRAIN_URL` is pre-filled and `DATA_ROOT=/data/client` should stay as-is.
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
data is snapshotted **inside your own `/data/client` volume** — like all raw
data, it never leaves your network; only column names, types, and the
descriptions your admin confirms are shared with the AI.

**Ask your DBA to create a dedicated read-only database login for
PowerDataChat with SELECT-only grants** (ideally on a read replica). The
client only ever issues SELECT/introspection statements, and that grant is
your hard guarantee. Set the nightly snapshot-refresh time in the admin UI
(container-local time — set `TZ` in `client.env` if your server isn't UTC).

## 3. Run

The container listens on port **8000** and keeps all state in **`/data/client`**.
Mount a persistent volume there so nothing is lost on restart/upgrade:

```
docker run -d --name pdc-client -p 8000:8000 \
  --env-file client.env \
  -v pdc_client_data:/data/client \
  powerdatachat-client:<tag>
```

(Prefer Docker Compose? See [`docker-compose.yml`](docker-compose.yml) in this
repo — `docker compose up -d`.)

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

## Notes

- **One tenant token per customer.** If service stops with `403`, your token may
  have been revoked — contact PowerDataChat.
- **Your data stays yours.** Raw uploads, chats, and rendered decks live only in
  the `/data/client` volume on your server and never leave it. Only no-value
  metadata and the bearer token reach the brain.
- **Upgrades:** pull/load the new image tag, then `docker rm -f pdc-client` and
  re-run step 3 with the new tag. The `pdc_client_data` volume (your data) is
  preserved across upgrades.
