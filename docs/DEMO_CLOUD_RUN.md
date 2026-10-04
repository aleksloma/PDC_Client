# Internal demo client on Cloud Run (`pdcclient-demo`)

> **INTERNAL ONLY — this is NOT a customer topology.** Customers always run the
> client image inside their own LAN (see `BUILD_AND_RUN.md` and
> `CUSTOMER_INSTALL.md`). This document describes the single demo/showcase
> instance that PowerDataChat hosts itself for business meetings. It runs the
> **standard, unmodified client images** against the production brain under a
> dedicated demo tenant, holding only PowerDataChat's own demo data — so the
> customer data-boundary model (Constitution Art. II) is unaffected.

A second hosted instance, `pdcclient-brb` at `brb.powerdatachat.com` (BRB demo on Cloud SQL, same images), is documented in `BRB_CLOUD_RUN.md`.

**Build once, deploy both.** Every release builds the two images ONCE from a
merged `main` commit ("Build" below) and deploys them to both services, one
after the other: `pdcclient-demo` first, then `pdcclient-brb` with the same
tag the demo has just been verified on. Each service keeps its own spec,
data bucket, backups and secrets; only the two `image:` lines change.

## Topology

| Piece | Value |
|---|---|
| GCP project | `pdc-enterprise` |
| Region | `europe-west1` |
| Cloud Run service | `pdcclient-demo` (public via `--no-invoker-iam-check`) |
| Containers | ONE revision, TWO containers: `web` (ingress, port 8000) and `executor` (the analysis sandbox, no port) — see "Two containers in one revision" |
| Images | `europe-west1-docker.pkg.dev/pdc-enterprise/client/pdcclient-demo:<git-sha>` (web) and `…/client/pdcexecutor-demo:<git-sha>` (sandbox), always built together from one commit by `cloudbuild.yaml` |
| Data volume | GCS bucket `pdc-enterprise-client-demo-data` mounted at `/data/client` in `web` only, mount options `uid=10001,gid=10001` |
| Jobs volume | in-memory volume `exec-jobs` (1 GiB) at `/jobs` in both containers |
| Backups | `gs://pdc-enterprise-client-demo-data-backups/<YYYYMMDD-HHMMSS>/` — a full copy of the data bucket taken before every deploy |
| Upload hop | GCS bucket `pdc-enterprise-demo-uploads` (`GCS_UPLOAD_BUCKET`) — transient home for files > 25 MB between the browser's signed PUT and `/upload/finalize`; objects deleted on finalize, 1-day lifecycle backstop; CORS for the two site origins; runtime SA has `objectAdmin` on it + `iam.serviceAccountTokenCreator` on itself (signBlob). NOT a customer setting |
| Brain | the production `pdcbrain` service (`BRAIN_URL` = its `status.url`) |
| Tenant | a dedicated **demo tenant** created in the brain admin panel |
| Secrets | `CLIENT_DEMO_TENANT_TOKEN` → `BRAIN_TENANT_TOKEN`, `CLIENT_DEMO_SECRET_KEY` → `SECRET_KEY`, `CLIENT_DEMO_LADMIN_PASSWORD` → `LOCAL_ADMIN_PASSWORD`, `CLIENT_DEMO_ENCRYPTION_KEY` → `CLIENT_ENCRYPTION_KEY` (all Secret Manager, pinned `:latest`, on the `web` container ONLY — the sandbox refuses to start with any of them) |
| Service URL | `https://pdcclient-demo-873133613631.europe-west1.run.app` |
| Custom domain | `https://client.powerdatachat.com` (Cloud Run domain mapping; the brain's admin panel is `admin.powerdatachat.com`) |

Do **not** confuse this service with `pdcbrain`; the brain deploy runbook
(`PDC_Brain/docs/DEPLOY.md` + its `deploy` skill) is unchanged and never
touches this service, and vice versa.

## Two containers in one revision (demo-only exception)

From the executor release on, generated Python runs only in the `pdc-executor`
sandbox, and a customer install is two containers on a private Docker network
(`CUSTOMER_INSTALL.md` §3). This service mirrors that as a Cloud Run
**multi-container revision**: the sandbox is a sidecar of the web container,
started first (`run.googleapis.com/container-dependencies: {"web":["executor"]}`,
with an HTTP startup probe on the sandbox's `/healthz`), and the two exchange
job files through an in-memory volume. There is no liveness probe, so a
sandbox that latched unhealthy (`EXECUTOR_UNHEALTHY` in its log: a job left
processes it could not stop) keeps refusing jobs until its instance is
replaced — deploy a new revision to recover.

| Setting | `web` | `executor` |
|---|---|---|
| Image | `pdcclient-demo:<sha>` (uid 10001) | `pdcexecutor-demo:<sha>`, the default hardened target (uid 10002, gid 10001) |
| Port | 8000, the only ingress | none |
| Resources | 2 CPU / 4 GiB | 1 CPU / 3 GiB (compose's `mem_limit: 3g`) |
| Volumes | `client-data` at `/data/client`, `exec-jobs` at `/jobs` | `exec-jobs` at `/jobs` only |
| Executor settings | `EXECUTOR_URL=http://127.0.0.1:8090`, `EXECUTOR_SHARED_DIR=/jobs`, `EXECUTOR_MAX_CONCURRENT=1` | `EXECUTOR_SHARED_DIR=/jobs`, `EXECUTOR_MEM_LIMIT_MB=2048`, `EXECUTOR_MAX_CONCURRENT=1` |
| Guard settings | `EXECUTOR_NETWORK_CIDR=127.0.0.0/8`, `FORWARDED_ALLOW_IPS=""` | — |

The web container refuses to start unless `SECRET_KEY` is at least 32
characters (`SECRET_KEY_UNSET`) and, because `EXECUTOR_URL` is set, unless
`EXECUTOR_NETWORK_CIDR` is a valid network (`EXECUTOR_CIDR_UNSET`); the demo's
`127.0.0.0/8` satisfies the second check.

**What the demo keeps of the customer topology's isolation (Constitution
Art. XIV):**

- two identities: the sandbox runs as uid 10002 and the web process as 10001;
- no secret in the sandbox: its environment carries only the three
  `EXECUTOR_*` values, and the service would refuse to start otherwise;
- no database driver, HTTP client or credential module in the sandbox image;
- no customer data mounted: only `web` mounts the bucket, the sandbox sees
  the jobs volume alone, and each job's frames are written per job;
- one job at a time on both sides;
- the jobs volume is in memory, so nothing a job leaves there survives the
  instance (stricter than the customer's disk-backed volume);
- the web service still treats every sandbox response as hostile;
- a request from the sandbox to the web service over loopback is refused.
  `EXECUTOR_NETWORK_CIDR=127.0.0.0/8` makes the web service answer 403 to any
  peer on loopback, and `FORWARDED_ALLOW_IPS=""` stops uvicorn trusting
  `X-Forwarded-For` from loopback — its default — which would otherwise let
  sandbox code present any address. Cloud Run's ingress reaches the web
  container from `169.254.169.126`, so users are unaffected. The empty value
  also means no proxy is trusted at all: the sign-in limiter sees every user
  as that one ingress address, which was already the case before. The loopback
  refusal would also answer 403 to the web image's own Docker `HEALTHCHECK`
  (a `curl` of `localhost:8000/health`); Cloud Run ignores that instruction and
  probes TCP 8000 instead, but under Docker the container would report
  unhealthy. That is one more reason the setting belongs to this service only.

**What it cannot keep, accepted for a demo holding demo data only:**

- **Network isolation.** Containers of one revision share a network
  namespace, so the sandbox has the revision's internet egress: generated
  code can reach the brain URL, the internet, and the web container on the
  instance's own non-loopback address, which the loopback guard does not
  cover. There is no `internal: true` network to put it on. This breaks
  property 4 of Art. XIV for this service only.
- **Container hardening flags.** Cloud Run offers no read-only root
  filesystem, `cap_drop`, `no-new-privileges` or `pids_limit`. The runner's
  own limits (address space, process count, file size) still apply per job.
- **Jobs-root ownership.** The in-memory volume comes up with Cloud Run's
  own ownership and mode rather than `root:10001 2770`; job directories are
  still created `2770` by the web service. Both identities must be able to
  write its root — the sandbox checks this at startup and exits
  (`EXECUTOR_SHARED_DIR_NOT_WRITABLE`) if not, so a wrong mode shows up as a
  revision that never becomes ready, never as a broken live service.

Never copy this layout into a customer install.

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
- `web`: 2 CPU / 4 GiB — pandas/plotly/kaleido (headless Chromium) plus the
  ~500 MB df cache (`DF_CACHE_MAX_MB`). `executor`: 1 CPU / 3 GiB. The
  instance is billed for the sum.
- **Non-root web container on a bucket mount.** A Cloud Storage volume is
  owned by root unless the mount says otherwise, and `chmod`/`chown` do not
  work on it, so the uid-10001 web image could not write `/data/client`. The
  volume carries `uid=10001,gid=10001` in its mount options. The symptom
  without them is a healthy service that answers 500 on sign-in.
- Upload size: the container caps a multipart upload body at
  `MAX_UPLOAD_BYTES` (100 MiB by default, a 413 from the app), but Cloud
  Run's ingress (Google Frontend) caps HTTP/1 request bodies at 32 MiB — a
  multipart `POST /upload` above that gets an HTML 413 that never reaches the
  app or its request log (verified 2026-09-03: 31 MiB → app 401, 34 MiB →
  GFE 413). Hence `GCS_UPLOAD_BUCKET`: with it set, `dashboard.js` sends any
  batch containing a file > 25 MB through `/upload/init` → signed PUT straight
  to the bucket → `/upload/finalize` (500 MB cap; that path is not under
  `MAX_UPLOAD_BYTES`, but its workbooks still pass the xlsx archive check).
  Customer LAN installs have no ingress cap and leave the variable unset.
- `--timeout=900` — long analyses (60 s exec windows + brain round-trips up to
  `BRAIN_REQUEST_TIMEOUT=180 s` each).
- `--min-instances=0` — near-zero idle cost; first hit after idle cold-starts
  (two images now, so allow ~30–45 s). Bump to 1 shortly before an important
  meeting if desired:
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

# Direct-upload hop bucket (2026-09-03). Transient only: objects are deleted by
# /upload/finalize; the lifecycle rule is the backstop. `... set` REPLACES the
# whole CORS/lifecycle config — `describe` first and merge if it ever grows.
gcloud storage buckets create gs://pdc-enterprise-demo-uploads \
  --location=europe-west1 --uniform-bucket-level-access --project=pdc-enterprise
cat > /tmp/lifecycle.json <<'EOF'
{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 1}}]}
EOF
gcloud storage buckets update gs://pdc-enterprise-demo-uploads --lifecycle-file=/tmp/lifecycle.json
cat > /tmp/cors.json <<'EOF'
[{"origin": ["https://client.powerdatachat.com",
             "https://pdcclient-demo-873133613631.europe-west1.run.app"],
  "method": ["PUT", "GET", "HEAD", "OPTIONS"],
  "responseHeader": ["Content-Type", "Content-Length"],
  "maxAgeSeconds": 3600}]
EOF
gcloud storage buckets update gs://pdc-enterprise-demo-uploads --cors-file=/tmp/cors.json
# Runtime SA: write/delete objects in THIS bucket only, and sign URLs via IAM
# signBlob (no key file anywhere — token creator on ITSELF).
gcloud storage buckets add-iam-policy-binding gs://pdc-enterprise-demo-uploads \
  --member="serviceAccount:873133613631-compute@developer.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"
gcloud iam service-accounts add-iam-policy-binding \
  873133613631-compute@developer.gserviceaccount.com \
  --member="serviceAccount:873133613631-compute@developer.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator" --project=pdc-enterprise

# Secret values: tenant token copied one-time from the brain admin panel;
# SECRET_KEY random, at least 32 characters or the web container will not start
# (e.g. python -c "import secrets;print(secrets.token_urlsafe(48))").
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

From a clean `PDC_Client/` working tree, pushed, build BOTH images of the
commit with the repository's `cloudbuild.yaml` (a `--tag` build cannot build
the sandbox's Dockerfile or pass build args):

```bash
SHA=$(git rev-parse --short HEAD)
gcloud builds submit . --project=pdc-enterprise --config=cloudbuild.yaml --async \
  --substitutions=_GIT_SHA=$SHA,_BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
gcloud builds describe <BUILD_ID> --project=pdc-enterprise --format='value(status)'
```

The short git SHA is the immutable tag of both images (same convention as the
brain). Both are stamped with `BUILD_COMMIT`/`BUILD_TIME`, so `GET /version`
reports the commit and the admin sidebar shows it.

## Backup (before every deploy)

```bash
STAMP=$(date -u +%Y%m%d-%H%M%S)
gcloud storage rsync -r gs://pdc-enterprise-client-demo-data \
  gs://pdc-enterprise-client-demo-data-backups/$STAMP/ --project=pdc-enterprise
```

Restore is the same command with source and destination swapped (service
scaled to zero or traffic pinned to a revision that is not writing).

Check a backup by object list, not by line count: the data bucket also holds
0-byte "folder" objects (names ending in `/`, left by the gcsfuse mount) that
`rsync` does not copy. A complete backup has the same total size
(`gcloud storage du -s`) and the same object names once those are ignored.

## Deploy

Every deploy goes through a candidate revision that receives **no traffic**
until it has passed the checks below.

The multi-container spec is kept in the service itself, not in the
repository, because it names secrets and the service URL. A release changes
it by exporting it, editing the two image tags (and, only when a release
needs one, adding a new variable), and replacing it. Everything else —
secrets, `GCS_UPLOAD_BUCKET`, probes, volumes — carries over verbatim, which
is the multi-container form of the image-only redeploy rule: never
`--set-env-vars`, `--set-secrets` or `--clear-*` on this service.

```bash
LIVE=$(gcloud run services describe pdcclient-demo --project=pdc-enterprise \
  --region=europe-west1 --format='value(status.traffic[0].revisionName)')

# 1. Pin traffic to the serving revision, so the candidate gets none.
gcloud run services update-traffic pdcclient-demo --project=pdc-enterprise \
  --region=europe-west1 --to-revisions=$LIVE=100

# 2. Export, edit, replace.
gcloud run services describe pdcclient-demo --project=pdc-enterprise \
  --region=europe-west1 --format=export > service.yaml
#    - both `image:` lines → the new <sha>
#    - spec.template.metadata.name → pdcclient-demo-<sha>
#    - spec.traffic → [{revisionName: $LIVE, percent: 100}]
#    `gcloud run services replace service.yaml` needs the Cloud Resource
#    Manager API, which is disabled in pdc-enterprise, and refuses the
#    numeric project. The same call through the Cloud Run Admin API:
python -c "import yaml,json;json.dump(yaml.safe_load(open('service.yaml')),open('service.json','w'))"
curl -X PUT -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" --data-binary @service.json \
  https://europe-west1-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/873133613631/services/pdcclient-demo

# 3. Give the candidate its own URL, run the checks against it.
gcloud run services update-traffic pdcclient-demo --project=pdc-enterprise \
  --region=europe-west1 --set-tags=candidate=pdcclient-demo-<sha>
#    → https://candidate---pdcclient-demo-th2ceoqcba-ew.a.run.app

# 4. Shift traffic once the checks pass.
gcloud run services update-traffic pdcclient-demo --project=pdc-enterprise \
  --region=europe-west1 --to-revisions=pdcclient-demo-<sha>=100

# Rollback: the same command naming the previous revision.
gcloud run services update-traffic pdcclient-demo --project=pdc-enterprise \
  --region=europe-west1 --to-revisions=$LIVE=100
```

The template portion that makes the revision two containers (as first
applied on the executor release; the export carries it from then on):

```yaml
spec:
  template:
    metadata:
      annotations:
        run.googleapis.com/container-dependencies: '{"web":["executor"]}'
    spec:
      containers:
      - name: web
        image: europe-west1-docker.pkg.dev/pdc-enterprise/client/pdcclient-demo:<sha>
        ports: [{name: http1, containerPort: 8000}]
        env:   # existing entries kept, these added
        - {name: EXECUTOR_URL, value: "http://127.0.0.1:8090"}
        - {name: EXECUTOR_SHARED_DIR, value: /jobs}
        - {name: EXECUTOR_MAX_CONCURRENT, value: "1"}
        - {name: EXECUTOR_NETWORK_CIDR, value: 127.0.0.0/8}
        - {name: FORWARDED_ALLOW_IPS, value: ""}
        - {name: ALLOW_SELF_REGISTRATION, value: "true"}
        - {name: PUBLIC_BASE_URL, value: "https://client.powerdatachat.com"}
        volumeMounts:
        - {name: client-data, mountPath: /data/client}
        - {name: exec-jobs, mountPath: /jobs}
      - name: executor
        image: europe-west1-docker.pkg.dev/pdc-enterprise/client/pdcexecutor-demo:<sha>
        env:
        - {name: EXECUTOR_SHARED_DIR, value: /jobs}
        - {name: EXECUTOR_MEM_LIMIT_MB, value: "2048"}
        - {name: EXECUTOR_MAX_CONCURRENT, value: "1"}
        resources: {limits: {cpu: "1", memory: 3Gi}}
        startupProbe:
          httpGet: {path: /healthz, port: 8090}
          periodSeconds: 2
          timeoutSeconds: 2
          failureThreshold: 60
        volumeMounts:
        - {name: exec-jobs, mountPath: /jobs}
      volumes:
      - name: client-data
        csi:
          driver: gcsfuse.run.googleapis.com
          volumeAttributes:
            bucketName: pdc-enterprise-client-demo-data
            mountOptions: uid=10001,gid=10001
      - name: exec-jobs
        emptyDir: {medium: Memory, sizeLimit: 1Gi}
```

Never delete or recreate the `pdc-enterprise-client-demo-data` bucket — it
holds the demo accounts, uploaded demo datasets, chats, and rendered decks.

## Verify (on the candidate URL, before shifting traffic)

1. `GET <candidate-url>/health` → `brain_reachable: true`,
   `tenant_token_configured: true` **and `executor_reachable: true`**. The
   endpoint answers 200 whatever the sandbox's state, so the body is the only
   thing that tells you.
2. Sign in (`/?local=1`), upload a small CSV (`tools/fixtures/sample_sales.csv`),
   ask a question that renders a chart, create a dashboard and pin the chart.
   A **500 on sign-in from a healthy service** is the bucket-ownership failure
   (mount options missing); "the analysis service is not reachable" is the
   sandbox.
3. Revision logs: `EXECUTOR_HANDSHAKE_OK`, no `EXECUTOR_SHARED_DIR_*`, no
   `BACKEND_REQUEST_REFUSED` for the ingress address.
4. `GET /version` reports the new commit.
5. After the shift: `https://client.powerdatachat.com/health` shows the same
   body, and `pdcbrain` gained no new revision:
   `gcloud run revisions list --service=pdcbrain --region=europe-west1 --project=pdc-enterprise`
6. Direct upload: `GET /lab` HTML carries `window.__DIRECT_UPLOAD__ = true`;
   a new chat from a > 32 MiB CSV shows, in the browser's network tab,
   `POST /upload/init` 200 → `PUT storage.googleapis.com/...` 200 →
   `POST /upload/finalize` 200 and no `/upload` multipart; a small CSV still
   goes through `POST /upload`; afterwards
   `gcloud storage ls gs://pdc-enterprise-demo-uploads/**` lists nothing.

Remove the check's throwaway account and dashboard afterwards.

## Security notes

- The URL is public and the client has **open self-registration** — anyone
  with the URL can create an account. This is NOT the image default: the
  service carries `ALLOW_SELF_REGISTRATION=true`; without it only invited,
  shared-with or SSO accounts can sign in. Reset and invitation mails need
  `PUBLIC_BASE_URL=https://client.powerdatachat.com` (the address demo users
  type), which the service also carries; without it no link is mailed.
  Acceptable because this instance holds demo data only and uses a dedicated
  demo tenant (kill-switchable from the brain admin panel: suspend/revoke the
  tenant or rotate its token).
- The analysis sandbox has internet egress here (see "Two containers in one
  revision"). Generated code could send whatever a demo chat's frames hold to
  the internet; that is acceptable only because they are demo data.
- Never point this instance at a real customer's tenant token.
- Several pieces of state live in the web process's memory: the sign-in
  limiter's counters and lockouts, the chart store, the session-generation
  cache and the reset-link index. Every scale-to-zero resets them, so a lock
  or a registered chart does not survive an idle period. Each cold start also
  scans the users directory on the mounted volume to rebuild the reset-link
  index, which adds to start-up time on the bucket mount.
- If the demo tenant token is rotated in the admin panel, add a new version to
  the secret and redeploy:
  `gcloud secrets versions add CLIENT_DEMO_TENANT_TOKEN --data-file=<file>`
  then a no-op redeploy (secrets pinned to `:latest` are resolved at instance start).

## Deploy history

**2026-10-02 — `main` at `6b4dce5`.** The "Select from DB" picker grouped by
connection and schema, the single "Download Analytics" dropdown with the Auto
Analytics item, decision-tree plots no longer rejected as empty charts, and
both images built without pip with `urllib3` pinned (not a tagged release).
Built by Cloud Build `c9234501-67b0-46a9-b624-91bbb20df5fa` (both images
stamped `6b4dce5`, `BUILD_TIME=2026-10-02T12:28:05Z`):

| Image | Digest |
|---|---|
| `pdcclient-demo:6b4dce5` | `sha256:afdd1ba7a7f783e53da72c6bff4313b5b8bdba7a16958bd075a570d85eb0c264` |
| `pdcexecutor-demo:6b4dce5` | `sha256:324dd31a369211ff1334a3cc4e32f179d306df283993b794635ef55a11d92bf4` |

Revision `pdcclient-demo-6b4dce5`, serving 100 % since 2026-10-02 ~13:09 UTC;
it resolved exactly the two digests above. It replaced
`pdcclient-demo-e396e49`, which stays available for rollback:

```bash
gcloud run services update-traffic pdcclient-demo --project=pdc-enterprise \
  --region=europe-west1 --to-revisions=pdcclient-demo-e396e49=100
```

Backup taken before the deploy:
`gs://pdc-enterprise-client-demo-data-backups/20261002-122847/`, which matched
the data bucket in size (438,787,392 bytes) and in its 515 object names.

The spec change was image-only (two `image:` lines and the template name),
checked by a diff against the export before the Admin API `PUT`. Candidate
checks (`candidate---…` URL):
- `/health` answered `brain_reachable`, `tenant_token_configured` and
  `executor_reachable` all `true`, and `/version` reported `6b4dce5`.
- A self-registered throwaway account signed in, uploaded
  `sample_sales.csv`, got a chart from a question, then created a dashboard,
  pinned the chart and read it back.
- The top bar showed one "Download Analytics" dropdown whose first item read
  "Run Auto Analytics" with the sparkle icon, and no separate button; the
  "Select from DB" list was grouped (connection `Moda_Line_Demo`, schema
  `moda`, 7 tables); a decision-tree question on the sample file rendered a
  tree image (sandbox job `EXEC_JOB_END status=ok`, `MULTI_PLOT_DONE
  rendered=1`, no `EmptyChartError`).
- Revision log: `EXECUTOR_HANDSHAKE_OK version=6b4dce5`; no
  `EXECUTOR_SHARED_DIR_*`, no `BACKEND_REQUEST_REFUSED`, no
  `ModuleNotFoundError`, no errors, no 5xx.
- Direct upload: `/lab` carried `window.__DIRECT_UPLOAD__ = true`, but the
  browser's `PUT` of a 35 MiB CSV was refused on the candidate URL — the
  uploads bucket's CORS lists only the two site origins, so that step cannot
  pass on a `candidate---…` address for any revision. `/upload/init` and
  `/upload/finalize` answered 200 around a relayed `PUT`. Traffic was shifted
  on that basis and the full browser sequence (`/upload/init` 200 → `PUT
  storage.googleapis.com/…` 200 → `/upload/finalize` 200, no multipart
  `/upload`) then passed on `client.powerdatachat.com`; the uploads bucket
  was empty afterwards.
- After the shift, `client.powerdatachat.com` reported the same `/version` and
  `/health`, the revision log stayed free of errors and 5xx, and `pdcbrain`
  gained no revision (`pdcbrain-00016-95f` still serving).

The throwaway account (and its three chats and dashboard) was removed
afterwards through `/api/admin/users/remove`. Two of its empty session
workspaces stay in the data bucket (`sessions/s_856f14e033208cf2/`,
`sessions/s_aed10daebc9ad2f1/`, 63 bytes each).

**2026-09-30 — `main` at `e396e49`.** The September follow-ups: sharing
domains derived from every account when no administrator has an address, the
share note read from `comment`, and the filled-in data-processing facts (not a
tagged release). Built by Cloud Build `1bdfa931-7f64-44fa-817e-8188bee6b4bb`
(both images stamped `e396e49`, `BUILD_TIME=2026-09-30T09:57:05Z`):

| Image | Digest |
|---|---|
| `pdcclient-demo:e396e49` | `sha256:44b4bc57367c0f1be9dff430f38960b630c109619030b9b66fa9d84bb425edf9` |
| `pdcexecutor-demo:e396e49` | `sha256:75642f81ff2e73db6e67d4b2b9129907e5bf4ed15a15e4a63015d97bd77ef6e4` |

Revision `pdcclient-demo-e396e49`, serving 100 % since 2026-09-30 ~10:09 UTC.
It replaced `pdcclient-demo-b7527a6`, which stays available for rollback:

```bash
gcloud run services update-traffic pdcclient-demo --project=pdc-enterprise \
  --region=europe-west1 --to-revisions=pdcclient-demo-b7527a6=100
```

Backup taken before the deploy:
`gs://pdc-enterprise-client-demo-data-backups/20260930-095747/`, which matched
the data bucket in size (365,715,955 bytes) and in its 485 object names.

The spec change was image-only (two `image:` lines and the template name),
checked by a diff against the export before the Admin API `PUT`. Candidate
checks (`candidate---…` URL) all passed:
- `/health` answered `brain_reachable`, `tenant_token_configured` and
  `executor_reachable` all `true`, and `/version` reported `e396e49`.
- `/lab` carried `window.__DIRECT_UPLOAD__ = true`.
- A self-registered throwaway account signed in (the landing's form token
  posted with the password), uploaded `sample_sales.csv`, got a chart from a
  question (sandbox job `EXEC_JOB_END status=ok`), then created a dashboard,
  pinned the chart, read it back and deleted it.
- Revision log: `EXECUTOR_HANDSHAKE_OK version=e396e49`; no
  `EXECUTOR_SHARED_DIR_*`, no `BACKEND_REQUEST_REFUSED`, no errors, no 5xx.
- After the shift, `client.powerdatachat.com` reported the same `/version` and
  `/health`, and `pdcbrain` gained no revision (`pdcbrain-00016-95f` still serving).

The throwaway account (and its chat) was removed afterwards through
`/api/admin/users/remove`.

**2026-09-29 — `main` at `b7527a6`.** Profile autofill hints and AI drafts
for added columns (not a tagged release). Built by Cloud Build
`6410995c-14da-4f13-a285-43b6f1899a20` (both images stamped `b7527a6`,
`BUILD_TIME=2026-09-29T08:19:01Z`):

| Image | Digest |
|---|---|
| `pdcclient-demo:b7527a6` | `sha256:02898e1d0298edc702742a9069357623d513345f6bb8cb9b9ac736c5b8129038` |
| `pdcexecutor-demo:b7527a6` | `sha256:516bb98072ef2744facc405779a0749643b57f9a07a395ee34c95b9587641d8d` |

Revision `pdcclient-demo-b7527a6`, serving 100 % since 2026-09-29 ~08:35 UTC;
it resolved exactly the two digests above. It replaced
`pdcclient-demo-2089883`, which stays available for rollback:

```bash
gcloud run services update-traffic pdcclient-demo --project=pdc-enterprise \
  --region=europe-west1 --to-revisions=pdcclient-demo-2089883=100
```

Backup taken before the deploy:
`gs://pdc-enterprise-client-demo-data-backups/20260929-081941/`, which matched
the data bucket in size (365,693,516 bytes) and in its 480 object names.

The spec change was image-only (two `image:` lines and the template name),
checked by a diff against the export before the Admin API `PUT`. Candidate
checks (`candidate---…` URL) all passed:
- `/health` answered `brain_reachable`, `tenant_token_configured` and
  `executor_reachable` all `true`, and `/version` reported `b7527a6`.
- `/lab` carried `window.__DIRECT_UPLOAD__ = true`.
- A self-registered throwaway account signed in, uploaded
  `sample_sales.csv`, got a chart from a question (sandbox job
  `EXEC_JOB_END status=ok`), then created a dashboard, pinned the chart,
  read it back and deleted it.
- Revision log: `EXECUTOR_HANDSHAKE_OK version=b7527a6`; no
  `EXECUTOR_SHARED_DIR_*`, no `BACKEND_REQUEST_REFUSED`, no errors, no 5xx.
- After the shift, `client.powerdatachat.com` reported the same `/version` and
  `/health`, and `pdcbrain` gained no revision (`pdcbrain-00016-95f` still serving).

The throwaway account (and its chat) was removed afterwards through
`/api/admin/users/remove`. `tools/canary_check.py` cannot run here (Cloud Run
has no exec into a container); it passed 8/8 inside the local stack's web
container on the same commit.

**2026-09-28 — release `v1.0-security-r1`.** Commit `2089883`, built from the
tag by Cloud Build `2d763656-5fc4-474b-81c8-f4319e9ca989` (both images stamped
`2089883`, `BUILD_TIME=2026-09-28T19:22:00Z`):

| Image | Digest |
|---|---|
| `pdcclient-demo:2089883` | `sha256:a5119234aadaba1f883b0b1566601ce8070113c8e58003938e2f31a3cade7351` |
| `pdcexecutor-demo:2089883` | `sha256:1931cb7588d510ceef60680a728218ce2ce1c621bfedd768abbfc7dfc2b88b04` |

Revision `pdcclient-demo-2089883`, serving 100 % since 2026-09-28 ~19:47 UTC;
it resolved exactly the two digests above. It replaced
`pdcclient-demo-cef5e17`, which stays available for rollback:

```bash
gcloud run services update-traffic pdcclient-demo --project=pdc-enterprise \
  --region=europe-west1 --to-revisions=pdcclient-demo-cef5e17=100
```

Both revisions run the same spec apart from the two image tags, so a rollback
needs no environment change. Backup taken before the deploy:
`gs://pdc-enterprise-client-demo-data-backups/20260928-192244/`, which matched
the data bucket in size (365,661,341 bytes) and in its 470 object names.

The spec change was an image-only edit (two `image:` lines and the template
name), checked by a diff against the export before the Admin API `PUT`.
Candidate checks (`candidate---…` URL) all passed:
- `/health` answered `brain_reachable`, `tenant_token_configured` and
  `executor_reachable` all `true`, and `/version` reported `2089883`.
- `/lab` carried `window.__DIRECT_UPLOAD__ = true`.
- A self-registered throwaway account signed in, uploaded
  `sample_sales.csv`, got an interactive Plotly chart from a question (sandbox
  job `EXEC_JOB_END status=ok`), then created a dashboard, pinned the chart,
  read it back and deleted it.
- Revision log: `EXECUTOR_HANDSHAKE_OK version=2089883`; no
  `EXECUTOR_SHARED_DIR_*`, no `BACKEND_REQUEST_REFUSED`, no errors, no 5xx.
- After the shift, `client.powerdatachat.com` reported the same `/version` and
  `/health`, and `pdcbrain` gained no revision (`pdcbrain-00016-95f` still serving).

The throwaway accounts (and their chats) were removed afterwards through
`/api/admin/users/remove`.

A check script reading the chat stream must take the chart from the `partial`
event: a chart answer arrives as `partial` (with `image_base64` and `code`),
and the closing `done` event carries `image_base64: null`.

**2026-09-27 — first two-container revision.** Commit `cef5e17` (images
`pdcclient-demo:cef5e17` and `pdcexecutor-demo:cef5e17`, both stamped),
revision `pdcclient-demo-cef5e17`, serving 100 %. It replaced
`pdcclient-demo-00015-wvd` (image `9d399c2`, single container, no sandbox),
which stays available for rollback:

```bash
gcloud run services update-traffic pdcclient-demo --project=pdc-enterprise \
  --region=europe-west1 --to-revisions=pdcclient-demo-00015-wvd=100
```

Rolling back also rolls back the image to one that predates the non-root
user, the sandbox and invitation-only sign-in; the env vars added here are
harmless to it. Backup taken before the deploy:
`gs://pdc-enterprise-client-demo-data-backups/20260927-000444/`.

This deploy closed the two items that had blocked any redeploy since the
executor release:

- **No sandbox.** Closed by the sidecar. On the candidate URL `/health`
  answered `executor_reachable: true` on the first request, the web log showed
  `EXECUTOR_HANDSHAKE_OK`, and a chart question ran as a sandbox job
  (`EXEC_JOB_END status=ok`). Cloud Run's in-memory volume was writable by the
  sandbox's uid 10002 as it comes up, so no ownership step is needed for it.
- **Non-root web container on the bucket mount.** Closed by the
  `uid=10001,gid=10001` mount options. With them the candidate signed in a new
  account, stored an upload, answered a chart question, created a dashboard
  and pinned the chart, with no 5xx and no permission error in the log.

Also observed: one gcsfuse `429` retry warning on the mount right after
start-up, retried by the driver without an error reaching the app.

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
