# BRB demo client on Cloud Run (`pdcclient-brb`)

> **INTERNAL ONLY — this is NOT a customer topology.** Like `pdcclient-demo`
> (`DEMO_CLOUD_RUN.md`), this is an instance PowerDataChat hosts itself. It
> runs the **standard, unmodified client images** against the production
> brain under a dedicated BRB demo tenant and holds only synthetic data: the
> `brb_demo` database built by `BRB_Database_template` (a synthetic core
> banking database for BRB Tech), copied to Cloud SQL. The customer
> data-boundary model (Constitution Art. II) is unaffected.

Everything not stated here is as in `DEMO_CLOUD_RUN.md`: the two-container
revision, the Cloud Run settings and their reasons, the build, the security
notes. Read that first.

## Topology

| Piece | Value |
|---|---|
| GCP project / region | `pdc-enterprise` / `europe-west1` |
| Cloud Run service | `pdcclient-brb` (public, `run.googleapis.com/invoker-iam-disabled: 'true'`) |
| Containers | ONE revision, TWO containers, as the demo: `web` (port 8000) and `executor` (the analysis sandbox, no port), `container-dependencies` `{"web":["executor"]}` |
| Images | the SAME tags the serving `pdcclient-demo` revision runs: `europe-west1-docker.pkg.dev/pdc-enterprise/client/pdcclient-demo:<git-sha>` and `…/client/pdcexecutor-demo:<git-sha>` (no separate BRB build) |
| Resources | `web` 4 CPU / 8 GiB, `DF_CACHE_MAX_MB=2048`; `executor` 2 CPU / 6 GiB, `EXECUTOR_MEM_LIMIT_MB=4096`; `EXECUTOR_MAX_CONCURRENT=1` on both; `exec-jobs` in-memory 1 GiB at `/jobs` |
| Scaling | `max-instances 1` (mandatory, as the demo), `min-instances 0`, no CPU throttling, startup CPU boost, timeout 900 s |
| Data volume | GCS bucket `pdc-enterprise-client-brb-data` at `/data/client` in `web` only, mount options `uid=10001,gid=10001` |
| Backups | `gs://pdc-enterprise-client-brb-data-backups/<YYYYMMDD-HHMMSS>/` |
| Upload hop | shared with the demo: `pdc-enterprise-demo-uploads` (`GCS_UPLOAD_BUCKET`); its CORS rule lists `https://brb.powerdatachat.com` next to the demo's two origins |
| Brain | the production `pdcbrain` service (`BRAIN_URL`, as the demo) |
| Tenant | a dedicated **BRB demo tenant** |
| Secrets | `CLIENT_BRB_TENANT_TOKEN` → `BRAIN_TENANT_TOKEN`, `CLIENT_BRB_SECRET_KEY` → `SECRET_KEY`, `CLIENT_BRB_LADMIN_PASSWORD` → `LOCAL_ADMIN_PASSWORD`, `CLIENT_BRB_ENCRYPTION_KEY` → `CLIENT_ENCRYPTION_KEY` (Secret Manager, pinned `:latest`, on `web` only) |
| Database | Cloud SQL `brb-demo-pg` (PostgreSQL 16, `db-custom-2-7680`), connection name `pdc-enterprise:europe-west1:brb-demo-pg`, database `brb_demo`, read-only user `pdc_reader` (password in `CLIENT_BRB_PG_READER_PASSWORD`; admin password in `CLIENT_BRB_PG_ADMIN_PASSWORD`, never read by the service) |
| Cloud SQL connection | template annotation `run.googleapis.com/cloudsql-instances: pdc-enterprise:europe-west1:brb-demo-pg`; runtime SA has `roles/cloudsql.client` |
| Service URL | `https://pdcclient-brb-th2ceoqcba-ew.a.run.app` |
| Custom domain | `https://brb.powerdatachat.com` (Cloud Run domain mapping; Cloudflare `CNAME brb → ghs.googlehosted.com.`, DNS only) |

The database side (how `brb_demo` is built, loaded into Cloud SQL and
registered) is documented in `BRB_Database_template/docs/CLOUD_DEPLOYMENT.md`.

## What differs from the demo

- **Invitation only.** `ALLOW_SELF_REGISTRATION=false`. Users are invited by
  `ladmin` from the admin panel (Users → Invite); the mailed link points at
  `PUBLIC_BASE_URL=https://brb.powerdatachat.com`.
- **Database tables instead of uploads.** The 39 tables of `brb_demo`
  (schemas core, lending, digital, ops) are registered on the connection
  "BRB Core Banking". The connection reaches Cloud SQL through the Unix socket Cloud Run
  provides: host `/cloudsql/pdc-enterprise:europe-west1:brb-demo-pg`, port
  5432, database `brb_demo`, user `pdc_reader`, SSL off (libpq ignores SSL
  on a socket). The client passes the host to libpq unchanged, so no code
  change and no proxy container are needed. If the socket ever stops
  working, the fallback is a third container
  `gcr.io/cloud-sql-connectors/cloud-sql-proxy:2` with args
  `--port=5432 --address=127.0.0.1 pdc-enterprise:europe-west1:brb-demo-pg`
  (0.25 CPU / 256 MiB, listed in `container-dependencies` before `web`) and
  host `127.0.0.1` on the connection.
- **Bigger revision.** `web` and `executor` have twice the demo's CPU and
  memory, and the frame cache is raised to 2 GiB, because the bank tables
  reach two million rows.
- **Weekly refresh.** The global refresh schedule is weekly, Sunday 03:00:
  the data never changes, so a nightly refresh would be wasted work.
- **Its own data and backups buckets and secrets.** Nothing is shared with
  the demo except the images, the brain and the uploads bucket.

## Cost controls

The instance scales to zero by default. Before a meeting, keep one instance
warm, and return to zero afterwards:

```bash
gcloud run services update pdcclient-brb --project=pdc-enterprise \
  --region=europe-west1 --min-instances=1
gcloud run services update pdcclient-brb --project=pdc-enterprise \
  --region=europe-west1 --min-instances=0
```

`--min-instances` is a template change that creates a new revision with the
same spec; it is the one flag allowed outside the export-edit-replace rule
below, because it touches nothing else.

Stop Cloud SQL between demos (storage keeps billing, data is kept) and start
it before one:

```bash
gcloud sql instances patch brb-demo-pg --project=pdc-enterprise --activation-policy=NEVER
gcloud sql instances patch brb-demo-pg --project=pdc-enterprise --activation-policy=ALWAYS
```

With the database stopped, chats keep working on the snapshots; a refresh,
a live-mode table or a connection test fails until it is started again.

## Backup (before every deploy)

```bash
STAMP=$(date -u +%Y%m%d-%H%M%S)
gcloud storage rsync -r gs://pdc-enterprise-client-brb-data \
  gs://pdc-enterprise-client-brb-data-backups/$STAMP/ --project=pdc-enterprise
```

Restore and checking a backup work as in `DEMO_CLOUD_RUN.md`. The database
itself has no backups: it is rebuilt from `BRB_Database_template`.

## Deploy

The same rules as the demo: the spec lives in the service, a release
exports it, edits the two `image:` lines and the template name, and replaces
it through the Cloud Run Admin API; never `--set-env-vars`, `--set-secrets`
or `--clear-*`. The export carries the
`run.googleapis.com/cloudsql-instances` annotation — keep it, or the
revision loses the database. Deploy BRB after the demo, with the tag the
demo has just been verified on.

```bash
LIVE=$(gcloud run services describe pdcclient-brb --project=pdc-enterprise \
  --region=europe-west1 --format='value(status.traffic[0].revisionName)')
gcloud run services update-traffic pdcclient-brb --project=pdc-enterprise \
  --region=europe-west1 --to-revisions=$LIVE=100

gcloud run services describe pdcclient-brb --project=pdc-enterprise \
  --region=europe-west1 --format=export > brb_service.yaml
#    - both `image:` lines → the new <sha>
#    - spec.template.metadata.name → pdcclient-brb-<sha>
#    - spec.traffic → [{revisionName: $LIVE, percent: 100}]
#    - check the cloudsql-instances annotation is still there
python -c "import yaml,json;json.dump(yaml.safe_load(open('brb_service.yaml')),open('brb_service.json','w'))"
curl -X PUT -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" --data-binary @brb_service.json \
  https://europe-west1-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/873133613631/services/pdcclient-brb

gcloud run services update-traffic pdcclient-brb --project=pdc-enterprise \
  --region=europe-west1 --set-tags=candidate=pdcclient-brb-<sha>
# checks below on https://candidate---pdcclient-brb-th2ceoqcba-ew.a.run.app, then:
gcloud run services update-traffic pdcclient-brb --project=pdc-enterprise \
  --region=europe-west1 --to-revisions=pdcclient-brb-<sha>=100

# Rollback: the same command naming the previous revision.
gcloud run services update-traffic pdcclient-brb --project=pdc-enterprise \
  --region=europe-west1 --to-revisions=$LIVE=100
```

The service was first created with a `POST` of the transformed demo export
to `…/namespaces/873133613631/services`; the diff of that first spec against
the demo export is kept in `BRB_Database_template/docs/BRB_SERVICE_DIFF.md`.

## Verify

1. `GET /health` → `brain_reachable`, `tenant_token_configured` and
   `executor_reachable` all `true`.
2. `GET /version` reports the same commit as `pdcclient-demo`.
3. `ladmin` signs in at `/?local=1` and lands on `/admin/data_sources`;
   the connection "BRB Core Banking" tests OK and 39 tables are listed.
4. Revision logs: `EXECUTOR_HANDSHAKE_OK`, no 5xx.
5. A chat over `lending.loans` and `core.customers` answers a question from
   `BRB_Database_template/docs/DEMO_QUESTIONS.md` and renders a chart.
6. After a traffic shift: `https://brb.powerdatachat.com/health` shows the
   same body, and neither `pdcclient-demo` nor `pdcbrain` gained a revision.

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

Domain mapping `brb.powerdatachat.com` created; the certificate is issued
once the Cloudflare record `CNAME brb → ghs.googlehosted.com.` (DNS only)
exists.
