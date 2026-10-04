# BRB demo client on Cloud Run (`pdcclient-brb`)

> **INTERNAL ONLY — this is NOT a customer topology.** Like `pdcclient-demo`
> (`DEMO_CLOUD_RUN.md`), this is an instance PowerDataChat hosts itself. It
> runs the **standard, unmodified client images** against the production
> brain under a dedicated BRB demo tenant and holds only synthetic data: the
> `brb_demo` database built by `BRB_Database_template` (a synthetic core
> banking database for BRB Tech), copied to Cloud SQL. The customer
> data-boundary model (Constitution Art. II) is unaffected.

This file is the complete runbook for `brb.powerdatachat.com`: a release can
be deployed here by following it alone. `DEMO_CLOUD_RUN.md` explains WHY the
shared Cloud Run settings are what they are (two containers in one revision,
the loopback guard, the bucket mount options); read it once.

## Topology

| Piece | Value |
|---|---|
| GCP project / region | `pdc-enterprise` (number `873133613631`) / `europe-west1`; gcloud account `founder@powerdatachat.com` |
| Cloud Run service | `pdcclient-brb` (public: `run.googleapis.com/invoker-iam-disabled: 'true'`) |
| Containers | ONE revision, TWO containers: `web` (port 8000, the only ingress) and `executor` (the analysis sandbox, no port, HTTP startup probe on `/healthz:8090`); template annotation `run.googleapis.com/container-dependencies: '{"web":["executor"]}'` |
| Images | the SAME two images as `pdcclient-demo`, built once per release: `europe-west1-docker.pkg.dev/pdc-enterprise/client/pdcclient-demo:<sha>` (`web`) and `europe-west1-docker.pkg.dev/pdc-enterprise/client/pdcexecutor-demo:<sha>` (`executor`). There is no separate BRB build |
| Resources | `web` 4 CPU / 8 GiB, `DF_CACHE_MAX_MB=2048`; `executor` 2 CPU / 6 GiB, `EXECUTOR_MEM_LIMIT_MB=4096`; `EXECUTOR_MAX_CONCURRENT=1` on both; in-memory volume `exec-jobs` (1 GiB) at `/jobs` in both |
| Scaling | `max-instances 1` (mandatory: single-writer state), `min-instances 0` by default, no CPU throttling, startup CPU boost, request timeout 900 s |
| Data volume | GCS bucket `pdc-enterprise-client-brb-data`, mounted at `/data/client` in `web` only, mount options `uid=10001,gid=10001` |
| Backups | `gs://pdc-enterprise-client-brb-data-backups/<YYYYMMDD-HHMMSS>/` |
| Upload hop | shared with the demo: `pdc-enterprise-demo-uploads` (`GCS_UPLOAD_BUCKET`); its CORS rule lists `https://brb.powerdatachat.com` next to the demo's two origins |
| Brain | the production `pdcbrain` service (`BRAIN_URL=https://pdcbrain-th2ceoqcba-ew.a.run.app`) |
| Tenant | the dedicated BRB demo tenant (`BRBtech`, tenant id `t_a316a43142021fbf`; welcome language Uzbek, set in the brain admin panel) |
| Secrets (names only, Secret Manager, `:latest`, on `web` only) | `CLIENT_BRB_TENANT_TOKEN` → `BRAIN_TENANT_TOKEN`; `CLIENT_BRB_SECRET_KEY` → `SECRET_KEY`; `CLIENT_BRB_LADMIN_PASSWORD` → `LOCAL_ADMIN_PASSWORD`; `CLIENT_BRB_ENCRYPTION_KEY` → `CLIENT_ENCRYPTION_KEY`. Database passwords, never read by the service: `CLIENT_BRB_PG_READER_PASSWORD` (the `pdc_reader` login, entered once in the connection form), `CLIENT_BRB_PG_ADMIN_PASSWORD` |
| Other `web` settings | `DATA_ROOT=/data/client`, `EXECUTOR_URL=http://127.0.0.1:8090`, `EXECUTOR_SHARED_DIR=/jobs`, `EXECUTOR_NETWORK_CIDR=127.0.0.0/8`, `FORWARDED_ALLOW_IPS=""`, `ALLOW_SELF_REGISTRATION=false`, `PUBLIC_BASE_URL=https://brb.powerdatachat.com` |
| Database | Cloud SQL `brb-demo-pg` (PostgreSQL 16, `db-custom-2-7680`), connection name `pdc-enterprise:europe-west1:brb-demo-pg`, database `brb_demo`, read-only user `pdc_reader` |
| Cloud SQL connection | template annotation `run.googleapis.com/cloudsql-instances: pdc-enterprise:europe-west1:brb-demo-pg`; the runtime SA `873133613631-compute@developer.gserviceaccount.com` has `roles/cloudsql.client` |
| Service URL | `https://pdcclient-brb-th2ceoqcba-ew.a.run.app` |
| Custom domain | `https://brb.powerdatachat.com` — Cloud Run domain mapping (Ready, certificate provisioned 2026-10-03); Cloudflare `CNAME brb → ghs.googlehosted.com.`, DNS only (grey cloud) |

The database side (how `brb_demo` is built, loaded into Cloud SQL and
registered) is documented in `BRB_Database_template/docs/CLOUD_DEPLOYMENT.md`.

## What differs from the demo

- **Invitation only.** `ALLOW_SELF_REGISTRATION=false`. Users are invited by
  `ladmin` from the admin panel (Users → Invite user); the mailed link points
  at `PUBLIC_BASE_URL` and is valid for 30 days. Until the invitee sets a
  password the invitation can be sent again (a new link replaces the old).
  `ladmin` signs in at `/?local=1` with the password in
  `CLIENT_BRB_LADMIN_PASSWORD`. Signing out in any tab ends every session of
  the account, the admin page's included.
- **Database tables instead of uploads.** See "Registered data" below.
- **Bigger revision.** `web` and `executor` have twice the demo's CPU and
  memory, and the frame cache is raised to 2 GiB, because the bank tables
  reach two million rows.
- **Its own data and backups buckets, tenant and secrets.** Nothing is shared
  with the demo except the images, the brain and the uploads bucket.

## Registered data

- Connection **"BRB Core Banking"** (PostgreSQL) reaches Cloud SQL through
  the Unix socket Cloud Run provides: host
  `/cloudsql/pdc-enterprise:europe-west1:brb-demo-pg`, port 5432, database
  `brb_demo`, user `pdc_reader`, SSL off (libpq ignores SSL on a socket). The
  client passes the host to libpq unchanged, so no code change and no proxy
  container are needed. If the socket ever stops working, the fallback is a
  third container `gcr.io/cloud-sql-connectors/cloud-sql-proxy:2` with args
  `--port=5432 --address=127.0.0.1 pdc-enterprise:europe-west1:brb-demo-pg`
  (0.25 CPU / 256 MiB, listed in `container-dependencies` before `web`) and
  host `127.0.0.1` on the connection.
- **39 tables** of schemas `core`, `lending`, `digital` and `ops`, all in
  **snapshot** mode (none live), with the database's table and column
  comments as descriptions.
- **Relations** confirmed from the database's foreign keys.
- **Global refresh schedule: weekly, Sunday 03:00.** The data never changes,
  so a nightly refresh would be wasted work. A refresh needs the database
  running (see "Cost controls").

## Cost controls (client testing periods)

The instance scales to zero by default. During a client testing period or
before a meeting, keep one instance warm, and return to zero afterwards:

```bash
gcloud run services update pdcclient-brb --project=pdc-enterprise \
  --region=europe-west1 --min-instances=1
gcloud run services update pdcclient-brb --project=pdc-enterprise \
  --region=europe-west1 --min-instances=0
```

`--min-instances` creates a new revision with the same spec; it is the one
flag allowed outside the export-edit-replace rule below, because it touches
nothing else. Note the new revision's name: a later deploy pins and rolls
back to whatever revision is serving.

Stop Cloud SQL between test periods (storage keeps billing, data is kept) and
start it before one:

```bash
gcloud sql instances patch brb-demo-pg --project=pdc-enterprise --activation-policy=NEVER
gcloud sql instances patch brb-demo-pg --project=pdc-enterprise --activation-policy=ALWAYS
gcloud sql instances describe brb-demo-pg --project=pdc-enterprise --format='value(state,settings.activationPolicy)'
```

With the database stopped, chats keep working on the snapshots; a refresh,
a live-mode table or a connection test fails until it is started again.

## Deploy (every release)

**Build once, deploy both.** Both hosted instances run the same two images,
built ONCE from a merged `main` commit with `cloudbuild.yaml`
(`DEMO_CLOUD_RUN.md` → "Build"). Deploy `pdcclient-demo` first, verify it,
then deploy `pdcclient-brb` with the same `<sha>`. Never build from an
unmerged branch.

The multi-container spec lives in the service, not in the repository (it
names secrets and the Cloud SQL instance). A release exports it, changes ONLY
the two `image:` lines and the template name, and replaces it through the
Cloud Run Admin API. Never `--set-env-vars`, `--set-secrets` or `--clear-*`:
the secrets, buckets, resources, probes and the
`run.googleapis.com/cloudsql-instances` annotation must carry over verbatim
(without the annotation the revision loses the database).

```bash
gcloud auth login                      # founder@powerdatachat.com
gcloud config set project pdc-enterprise
SHA=<short sha of the merged main commit>    # the tag the demo was just verified on

# 1. Backup the data bucket, then check it by size and object names.
STAMP=$(date -u +%Y%m%d-%H%M%S)
gcloud storage rsync -r gs://pdc-enterprise-client-brb-data \
  gs://pdc-enterprise-client-brb-data-backups/$STAMP/ --project=pdc-enterprise
gcloud storage du -s gs://pdc-enterprise-client-brb-data
gcloud storage du -s gs://pdc-enterprise-client-brb-data-backups/$STAMP/
#    Object names: list both, ignore 0-byte "folder" objects (names ending
#    in "/", left by the gcsfuse mount, not copied by rsync), compare.

# 2. Pin traffic to the serving revision, so the candidate gets none.
LIVE=$(gcloud run services describe pdcclient-brb --project=pdc-enterprise \
  --region=europe-west1 --format='value(status.traffic[0].revisionName)')
gcloud run services update-traffic pdcclient-brb --project=pdc-enterprise \
  --region=europe-west1 --to-revisions=$LIVE=100

# 3. Export, edit, diff.
gcloud run services describe pdcclient-brb --project=pdc-enterprise \
  --region=europe-west1 --format=export > brb_service.yaml
cp brb_service.yaml brb_service.orig.yaml
#    Edit brb_service.yaml:
#    - web image      → …/client/pdcclient-demo:$SHA
#    - executor image → …/client/pdcexecutor-demo:$SHA
#    - spec.template.metadata.name → pdcclient-brb-$SHA
#    - spec.traffic → [{revisionName: $LIVE, percent: 100}]  (already so after step 2)
diff brb_service.orig.yaml brb_service.yaml   # exactly those lines, nothing else
grep -n "cloudsql-instances" brb_service.yaml # must still be present

# 4. Replace through the Cloud Run Admin API (`gcloud run services replace`
#    needs the Cloud Resource Manager API, disabled in this project).
python -c "import yaml,json;json.dump(yaml.safe_load(open('brb_service.yaml')),open('brb_service.json','w'))"
curl -X PUT -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" --data-binary @brb_service.json \
  https://europe-west1-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/873133613631/services/pdcclient-brb
gcloud run revisions describe pdcclient-brb-$SHA --project=pdc-enterprise \
  --region=europe-west1 --format='value(status.conditions[0].status)'   # True

# 5. Give the candidate its own URL and run the checks below on it.
gcloud run services update-traffic pdcclient-brb --project=pdc-enterprise \
  --region=europe-west1 --set-tags=candidate=pdcclient-brb-$SHA
#    → https://candidate---pdcclient-brb-th2ceoqcba-ew.a.run.app

# 6. Shift traffic once the checks pass, then verify the custom domain.
gcloud run services update-traffic pdcclient-brb --project=pdc-enterprise \
  --region=europe-west1 --to-revisions=pdcclient-brb-$SHA=100
curl -s https://brb.powerdatachat.com/health
curl -s https://brb.powerdatachat.com/version

# Rollback: the same command naming the previous revision.
gcloud run services update-traffic pdcclient-brb --project=pdc-enterprise \
  --region=europe-west1 --to-revisions=$LIVE=100
```

Keep the previous revision: it is the rollback. A restore of the data is the
rsync of step 1 with source and destination swapped (traffic pinned to a
revision that is not writing, or the service at zero instances). The
database itself has no backups: it is rebuilt from `BRB_Database_template`.

## Candidate checks (on the `candidate---…` URL, before shifting traffic)

1. `GET /health` → `brain_reachable`, `tenant_token_configured` and
   `executor_reachable` all `true` (the endpoint is 200 whatever the
   sandbox's state; only the body tells).
2. `GET /version` reports the new `<sha>`, the same as `pdcclient-demo`.
3. `ladmin` signs in at `/?local=1` and lands on `/admin/data_sources`; the
   connection "BRB Core Banking" tests OK and 39 tables are listed.
4. A chat created from database tables (e.g. `lending.loans` and
   `core.customers`) opens with the brain's welcome message and suggested
   questions in the tenant's language, and answers a question from
   `BRB_Database_template/docs/DEMO_QUESTIONS.md` with a chart. If it shows
   the fallback line instead, see `docs/WELCOME_INVESTIGATION.md` (a brain
   setting, not this service).
5. The Create New Chat wizard offers "Choose Files" and "Select from DB" only.
6. Users → Invite user to a throwaway address (`…@example.com`) twice: the
   second is accepted as a new invitation. Remove the account afterwards
   (`POST /api/admin/users/remove`).
7. Revision log: `EXECUTOR_HANDSHAKE_OK version=<sha>`, no 5xx, no error
   lines:
   `gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.revision_name="pdcclient-brb-<sha>" AND severity>=ERROR' --project=pdc-enterprise --freshness=1h`
8. After the shift: `https://brb.powerdatachat.com/health` shows the same
   body, and neither `pdcclient-demo` nor `pdcbrain` gained a revision:
   `gcloud run revisions list --service=pdcbrain --region=europe-west1 --project=pdc-enterprise`

The uploads bucket's CORS lists the site origins only, so a browser upload
above 25 MB cannot pass on the `candidate---…` URL; check it on the custom
domain after the shift if a release touches uploads.

## How the service was created

The service was first created with a `POST` of the transformed demo export
to `…/namespaces/873133613631/services`; the diff of that first spec against
the demo export is kept in `BRB_Database_template/docs/BRB_SERVICE_DIFF.md`.

## Deploy history

**2026-10-03 — first deploy, `main` at `6b4dce5`.** The service was created
from the `pdcclient-demo` export (revision `pdcclient-demo-6b4dce5`) with
the changes listed in `BRB_Database_template/docs/BRB_SERVICE_DIFF.md`,
POSTed to the Cloud Run Admin API. No new images: the revision resolved
the demo's two digests:

| Image | Digest |
|---|---|
| `pdcclient-demo:6b4dce5` | `sha256:afdd1ba7a7f783e53da72c6bff4313b5b8bdba7a16958bd075a570d85eb0c264` |
| `pdcexecutor-demo:6b4dce5` | `sha256:324dd31a369211ff1334a3cc4e32f179d306df283993b794635ef55a11d92bf4` |

Revision `pdcclient-brb-6b4dce5`, serving 100 % since 2026-10-02 21:22 UTC
(first revision; there is nothing to roll back to). No backup was taken:
the data bucket was new and empty.

Checks on `https://pdcclient-brb-th2ceoqcba-ew.a.run.app`:
- `/health` answered `brain_reachable`, `tenant_token_configured` and
  `executor_reachable` all `true`; `/version` reported `6b4dce5` with the
  demo's `BUILD_TIME`.
- The revision log shows `EXECUTOR_HANDSHAKE_OK version=6b4dce5`, no 5xx
  and no error lines, including during the snapshots below.
- `ladmin` signed in at `/?local=1` (first sign-in completed the forced
  change keeping the password in `CLIENT_BRB_LADMIN_PASSWORD`) and landed
  on `/admin/data_sources`.
- Connection "BRB Core Banking" over the Cloud SQL socket tested OK
  (PostgreSQL 16.15). All 39 tables registered in snapshot mode with the
  database's column and table comments as descriptions, none in live mode;
  the four largest snapshotted last in 14-37 s each
  (`core.card_transactions` 2,222,482 rows), memory below 15 % of the
  limit. Global refresh: weekly, Sunday 03:00.
- A chat over `lending.loans` and `core.customers` answered three
  questions from `DEMO_QUESTIONS.md`: Q10 (mortgages 2022-2026) rendered
  two charts, Q11 (NPL 90+ by product) a 22-row table, Q12 (2024 H2
  microloans, Fergana and Andijan against the rest) a two-row comparison,
  11.8 % against 4.9 % by count, in line with the expected ~11 % against
  ~5 %. With only those two tables in the chat the USD products'
  outstanding amounts are not converted to UZS. The chat was run as
  `ladmin` (an invitation link is only ever mailed); the invitation flow
  itself was checked with `brb-verify@example.com`: account created, mail
  sent, then removed through `/api/admin/users/remove`.
- `pdcclient-demo` and `pdcbrain` gained no revision.

Domain mapping `brb.powerdatachat.com` created; the certificate was issued
once the Cloudflare record `CNAME brb → ghs.googlehosted.com.` (DNS only)
existed (Ready since 2026-10-03 06:34 UTC).
