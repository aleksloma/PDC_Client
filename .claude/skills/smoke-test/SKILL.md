---
name: smoke-test
description: Smoke-test the client via the local Docker stack on the persistent pdc_client_* volumes, including the full upload→chart→edit→report browser cycle. Run before every commit and after every rebuild. Never ad-hoc uvicorn, never down -v.
---
# Client smoke test

Standing rule (`docs/BUILD_AND_RUN.md` §3): smoke = the Docker stack on the
persistent `pdc_client_*` external volumes — the same shape as a customer
upgrading the image against their existing data. Never native `uvicorn` runs.

## Steps

1. **Backup the data volume before a rebuild** (PowerShell). It now carries
   the log too (`/data/client/logs/datachat.log`); `pdc_client_logs` is no
   longer mounted so it needs no backup — but never delete it, it holds the
   older history:
   ```powershell
   docker run --rm -v pdc_client_data:/src -v C:\tmp\pdc_backup\pdc_client_data:/dest alpine cp -a /src/. /dest/
   ```
2. **Brain reachable**: either the local brain stack from `../PDC_Brain`
   (`:8090`) or whatever `BRAIN_URL` is configured in `client.local.env`.
3. **Rebuild + start**. The container runs as uid 10001 on a read-only
   rootfs; a volume that predates that image needs the one-time
   `docker run --rm -v pdc_client_data:/data alpine chown -R 10001:10001 /data`
   (see `docs/BUILD_AND_RUN.md` §3) or the app cannot write:
   ```
   docker compose -f docker-compose.local.yml up -d --build
   ```
   If anything from this run will be quoted later (`GET /version`, the admin
   sidebar's build stamp), commit first or pass the sha: `BUILD_COMMIT` is read
   from `git rev-parse HEAD` at build time, so an uncommitted tree stamps the
   image with the PREVIOUS commit and the container then reports a commit that
   lacks the change under test.
4. **Health**: `curl -s http://localhost:8091/health` → 200 with
   `brain_reachable: true` and `tenant_token_configured: true`.
5. **Pre-existing state intact** (the upgrade-safety check): open `/lab`,
   sign in as an existing user — sidebar chats and an old conversation's
   charts/history still load.
6. **Full cycle in the browser** (Constitution Art. VIII), using
   `tools/fixtures/sample_sales.csv`:
   upload → schema autofill populates → generate → ask a question → chart
   renders → reload page → chart persists → edit-regenerate → download PDF
   and PPTX. (Delegate to the ui-tester agent for a thorough pass.)
7. **Clean boot + hardening intact**: `docker logs pdc-client --tail 50` — no
   tracebacks, and no `Permission denied` / `Read-only file system` lines
   (those mean the volume ownership or a write path regressed, and the app
   degrades quietly rather than crashing). `docker exec pdc-client id` must
   report uid 10001.
8. **Unit tests**: `python -m pytest tests/ -q` — must be fully green.

Any failing step blocks the commit. NEVER run `docker compose down -v` and
never delete/recreate the `pdc_*` volumes.
