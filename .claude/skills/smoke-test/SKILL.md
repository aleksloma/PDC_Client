---
name: smoke-test
description: Smoke-test the client via the local Docker stack on the persistent pdc_client_* volumes, including the full upload→chart→edit→report browser cycle. Run before every commit and after every rebuild. Never ad-hoc uvicorn, never down -v.
---
# Client smoke test

Standing rule (`docs/BUILD_AND_RUN.md` §3): smoke = the Docker stack on the
persistent `pdc_client_*` external volumes — the same shape as a customer
upgrading the image against their existing data. Never native `uvicorn` runs.

## Steps

1. **Backup the volumes before a rebuild** (PowerShell, one per volume):
   ```powershell
   docker run --rm -v pdc_client_data:/src -v C:\tmp\pdc_backup\pdc_client_data:/dest alpine cp -a /src/. /dest/
   docker run --rm -v pdc_client_logs:/src -v C:\tmp\pdc_backup\pdc_client_logs:/dest alpine cp -a /src/. /dest/
   ```
2. **Brain reachable**: either the local brain stack from `../PDC_Brain`
   (`:8090`) or whatever `BRAIN_URL` is configured in `client.local.env`.
3. **Rebuild + start**:
   ```
   docker compose -f docker-compose.local.yml up -d --build
   ```
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
7. **Clean boot**: `docker logs pdc-client --tail 50` — no tracebacks.
8. **Unit tests**: `python -m pytest tests/ -q` — must be fully green.

Any failing step blocks the commit. NEVER run `docker compose down -v` and
never delete/recreate the `pdc_*` volumes.
