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

- **`BRAIN_URL`** — set to the brain endpoint PowerDataChat gave you (the
  template ships a placeholder).

`DATA_ROOT=/data/client` should stay as-is.
**Never commit or share the filled-in `client.env`.**

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
