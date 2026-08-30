# Internal demo client on Cloud Run (`pdcclient-demo`)

> **INTERNAL ONLY — this is NOT a customer topology.** Customers always run the
> client image inside their own LAN (see `BUILD_AND_RUN.md` and
> `CUSTOMER_INSTALL.md`). This document describes the single demo/showcase
> instance that PowerDataChat hosts itself for business meetings. It runs the
> **standard, unmodified client image** against the production brain under a
> dedicated demo tenant, holding only PowerDataChat's own demo data — so the
> customer data-boundary model (Constitution Art. II) is unaffected.

## Topology

| Piece | Value |
|---|---|
| GCP project | `pdc-enterprise` |
| Region | `europe-west1` |
| Cloud Run service | `pdcclient-demo` (public via `--no-invoker-iam-check`) |
| Image | `europe-west1-docker.pkg.dev/pdc-enterprise/client/pdcclient-demo:<git-sha>` |
| Data volume | GCS bucket `pdc-enterprise-client-demo-data` mounted at `/data/client` |
| Brain | the production `pdcbrain` service (`BRAIN_URL` = its `status.url`) |
| Tenant | a dedicated **demo tenant** created in the brain admin panel |
| Secrets | `CLIENT_DEMO_TENANT_TOKEN` → `BRAIN_TENANT_TOKEN`, `CLIENT_DEMO_SECRET_KEY` → `SECRET_KEY`, `CLIENT_DEMO_LADMIN_PASSWORD` → `LOCAL_ADMIN_PASSWORD`, `CLIENT_DEMO_ENCRYPTION_KEY` → `CLIENT_ENCRYPTION_KEY` (all Secret Manager, pinned `:latest`; verified against the live service 2026-08-30 — the last two were mounted after this doc was first written) |
| Service URL | `https://pdcclient-demo-873133613631.europe-west1.run.app` |
| Custom domain | `https://client.powerdatachat.com` (Cloud Run domain mapping; the brain's admin panel is `admin.powerdatachat.com`) |

Do **not** confuse this service with `pdcbrain`; the brain deploy runbook
(`PDC_Brain/docs/DEPLOY.md` + its `deploy` skill) is unchanged and never
touches this service, and vice versa.

## Why these Cloud Run settings

- `--port=8000` — the Dockerfile CMD hardcodes uvicorn on 8000; Cloud Run
  routes traffic there. No code change needed.
- Public access: an org policy (domain-restricted sharing) blocks the
  `allUsers` IAM binding that `--allow-unauthenticated` tries to create, so
  public access is granted with `--no-invoker-iam-check` instead — the same
  mechanism `pdcbrain` uses (`run.googleapis.com/invoker-iam-disabled=true`).
- `--max-instances=1` — **mandatory.** Client state is single-writer: local
  files under `DATA_ROOT` plus an in-RAM dataframe cache. Two instances would
  see different data.
- `--no-cpu-throttling` — Auto Analytics runs in a background thread that
  outlives the HTTP request (`auto_analytics.py`); without always-allocated
  CPU the deck job stalls after the response is sent.
- `--memory=4Gi --cpu=2` — pandas/plotly/kaleido (headless Chromium) plus the
  ~500 MB df cache (`DF_CACHE_MAX_MB`).
- `--timeout=900` — long analyses (60 s exec windows + brain round-trips up to
  `BRAIN_REQUEST_TIMEOUT=180 s` each).
- `--min-instances=0` — near-zero idle cost; first hit after idle cold-starts
  in ~15–30 s. Bump to 1 shortly before an important meeting if desired:
  `gcloud run services update pdcclient-demo --region=europe-west1 --min-instances=1`
- GCS-FUSE caveat: the client does atomic renames / parquet cache / JSONL
  appends; GCS-FUSE is weaker than POSIX. Accepted for a demo. If it misbehaves,
  options are disabling the parquet cache or moving to Filestore.

## One-time infra (already created 2026-07-23)

```bash
gcloud artifacts repositories create client --repository-format=docker \
  --location=europe-west1 --project=pdc-enterprise

gcloud storage buckets create gs://pdc-enterprise-client-demo-data \
  --location=europe-west1 --uniform-bucket-level-access --project=pdc-enterprise

# Secret values: tenant token copied one-time from the brain admin panel;
# SECRET_KEY random (e.g. python -c "import secrets;print(secrets.token_urlsafe(48))").
# NEVER commit either value.
gcloud secrets create CLIENT_DEMO_TENANT_TOKEN --data-file=<file> --project=pdc-enterprise
gcloud secrets create CLIENT_DEMO_SECRET_KEY   --data-file=<file> --project=pdc-enterprise

# Runtime SA (<PROJECT_NUMBER>-compute@developer.gserviceaccount.com) needs accessor:
gcloud secrets add-iam-policy-binding CLIENT_DEMO_TENANT_TOKEN \
  --member="serviceAccount:873133613631-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" --project=pdc-enterprise
gcloud secrets add-iam-policy-binding CLIENT_DEMO_SECRET_KEY \
  --member="serviceAccount:873133613631-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" --project=pdc-enterprise
```

## Build (every release)

From a clean `PDC_Client/` working tree:

```bash
gcloud builds submit . --project=pdc-enterprise \
  --tag=europe-west1-docker.pkg.dev/pdc-enterprise/client/pdcclient-demo:<git-sha>
```

Use the short git SHA as an immutable tag (same convention as the brain).

`gcloud builds submit --tag` cannot pass Docker build args, so the demo image
ships **unstamped**: `GET /version` reports `commit: null` and the admin
sidebar shows the container start time instead of a commit. The image tag is
the identity here — adding a `cloudbuild.yaml` just to carry two build args
was judged not worth it. A local `docker build` (see `docs/BUILD_AND_RUN.md`)
does stamp the image.

## Deploy

First-time deploy (full flag set):

```bash
BRAIN=$(gcloud run services describe pdcbrain --project=pdc-enterprise \
  --region=europe-west1 --format='value(status.url)')

gcloud run deploy pdcclient-demo \
  --project=pdc-enterprise --region=europe-west1 \
  --image=europe-west1-docker.pkg.dev/pdc-enterprise/client/pdcclient-demo:<git-sha> \
  --port=8000 \
  --no-invoker-iam-check \
  --memory=4Gi --cpu=2 \
  --min-instances=0 --max-instances=1 \
  --concurrency=20 --timeout=900 \
  --no-cpu-throttling \
  --set-env-vars=DATA_ROOT=/data/client,BRAIN_URL=$BRAIN \
  --set-secrets=BRAIN_TENANT_TOKEN=CLIENT_DEMO_TENANT_TOKEN:latest,SECRET_KEY=CLIENT_DEMO_SECRET_KEY:latest \
  --add-volume=name=client-data,type=cloud-storage,bucket=pdc-enterprise-client-demo-data \
  --add-volume-mount=volume=client-data,mount-path=/data/client
```

**Redeploy (new image only)** — same state-preserving rule as the brain: pass
ONLY the new image so env vars, secrets, and the volume mount carry over.
Never use `--set-env-vars` / `--set-secrets` / `--clear-*` on the live service.

```bash
gcloud run deploy pdcclient-demo --project=pdc-enterprise --region=europe-west1 \
  --image=europe-west1-docker.pkg.dev/pdc-enterprise/client/pdcclient-demo:<new-git-sha>
```

Never delete or recreate the `pdc-enterprise-client-demo-data` bucket — it
holds the demo accounts, uploaded demo datasets, chats, and rendered decks.

## Verify after deploy

1. `GET <service-url>/health` → `brain_reachable: true`,
   `tenant_token_configured: true`.
2. Sign in with the demo account, open an existing chat, ask a question that
   renders a chart (proves brain round-trip + kaleido inside the container).
3. Confirm `pdcbrain` gained no new revision:
   `gcloud run revisions list --service=pdcbrain --region=europe-west1 --project=pdc-enterprise`

## Security notes

- The URL is public and the client has **open self-registration** — anyone
  with the URL can create an account. Acceptable because this instance holds
  demo data only and uses a dedicated demo tenant (kill-switchable from the
  brain admin panel: suspend/revoke the tenant or rotate its token).
- Never point this instance at a real customer's tenant token.
- If the demo tenant token is rotated in the admin panel, add a new version to
  the secret and redeploy:
  `gcloud secrets versions add CLIENT_DEMO_TENANT_TOKEN --data-file=<file>`
  then a no-op redeploy (secrets pinned to `:latest` are resolved at instance start).

## Custom domain

Created 2026-07-24:

```bash
gcloud beta run domain-mappings create --service pdcclient-demo \
  --domain client.powerdatachat.com --region europe-west1 --project pdc-enterprise
```

- DNS at the domain provider: `CNAME client → ghs.googlehosted.com.` The
  Google-managed certificate provisions only after the CNAME resolves; until
  then `CertificateProvisioned` stays `Unknown/False` and only the `*.run.app`
  URL works.
- A domain mapping is routing-only — no new revision, no service changes.
- Status check:
  `gcloud beta run domain-mappings describe --domain client.powerdatachat.com --region europe-west1`
