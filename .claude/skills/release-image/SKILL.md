---
name: release-image
description: Build and ship the customer client Docker images (web application + analysis sandbox) — upgrade-safe by construction (new images, same data volume). Use for every client release handed to a customer.
---
# Customer client image release

A release is **two images**: `powerdatachat-client` (the web application, uid
10001) and `powerdatachat-executor` (the analysis sandbox, uid 10002). They
ship and version together, because a customer running a mismatched pair is an
unsupported stack, so every gate below applies to both.

The customer upgrade model: pull/load both new images, `docker compose up -d`
against their existing `pdc_client_data` volume. A release may touch NOTHING
but code — their users, chats, uploads, and history live on the volume. The
shared jobs volume holds in-flight jobs only and needs no migration.

## 1. Gates (all must pass first)
- Full suite green: `python -m pytest tests/ -q`.
- `/smoke-test` passed — the local persistent-volume stack IS the upgrade
  rehearsal (new image meets old data), including the browser cycle and the
  "pre-existing chats still load" check.
- Data-safety review: the diff contains no path that deletes/relocates
  anything under `DATA_ROOT`; stored-shape changes carry old-shape
  regression tests (see `/write-tests`).
- Docs current: `/sync-docs` clean; `CUSTOMER_INSTALL.md` still accurate for
  this version.
- **Both images run as their non-root user.** Nothing else in the repo checks
  the shipped artifacts, so a lost `USER` line would be invisible until a
  customer ran one as root:
  ```
  docker image inspect --format '{{.Config.User}}' powerdatachat-client:enterprise-<tag>
  docker image inspect --format '{{.Config.User}}' powerdatachat-executor:enterprise-<tag>
  ```
  must print `pdc` and `pdcexec`. The compose files add the read-only rootfs
  and the limits, but the identity has to be in each image. A plain build of
  `executor/Dockerfile` must be the hardened stage: if `Config.User` is empty
  or `root`, the `test` target was shipped by mistake.
- **Dependencies still clean, in BOTH images**: run the audit in
  `docs/BUILD_AND_RUN.md` §8 (throwaway venv, the BUILT image's freeze as
  input, BOTH advisory services) against each image and get no
  vulnerabilities. The pinned set is the security deliverable, and transitives
  that are not pinned can drift on any rebuild. The unit suite only guards the
  floors already reached — this scan is the detector.
- **Each image ships what its pin file says**: nothing else verifies it, since
  the images have no test runner, so the suite's installed-equals-pinned check
  only ever covers the developer's venv. Run it twice, once per pair of image
  and pin file (`requirements.txt`, then `executor/requirements.txt`):
  ```
  docker run --rm powerdatachat-client:enterprise-<tag> pip list --format=freeze > /tmp/image.txt
  grep -E '^[A-Za-z0-9].*==' requirements.txt | sed 's/\[.*\]//' | while read -r pin; do
    grep -qix "$pin" /tmp/image.txt || echo "MISMATCH: $pin"
  done

  docker run --rm powerdatachat-executor:enterprise-<tag> pip list --format=freeze > /tmp/exec.txt
  grep -E '^[A-Za-z0-9].*==' executor/requirements.txt | sed 's/\[.*\]//' | while read -r pin; do
    grep -qix "$pin" /tmp/exec.txt || echo "MISMATCH: $pin"
  done
  ```
  Any `MISMATCH` line means that image was built from a different pin file (or
  a pin was edited after the build), so rebuild before shipping. The sandbox's
  pins are an identical-version SUBSET of the root ones, so a version that
  differs between the two files is itself the bug.
- **Vulnerability scan of both images** (OS packages as well as Python; trivy
  is not installed locally, so run it from its own image):
  ```
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy \
    image --severity HIGH,CRITICAL powerdatachat-client:enterprise-<tag>
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy \
    image --severity HIGH,CRITICAL powerdatachat-executor:enterprise-<tag>
  ```
  Every OS finding that HAS a fix must be closed before shipping: both
  Dockerfiles carry an `apt-get upgrade` layer, so rebuilding with a fresh
  package index is usually the whole fix. Findings with no fix available are
  written down as accepted, never silently ignored.

## 2. Build immutable tags (both images, same tag)
```
docker build -t powerdatachat-client:enterprise-<git-sha-or-date> \
  --build-arg BUILD_COMMIT=$(git rev-parse --short HEAD) \
  --build-arg BUILD_TIME=$(date -u +%Y-%m-%dT%H:%MZ) .

docker build -f executor/Dockerfile \
  -t powerdatachat-executor:enterprise-<git-sha-or-date> \
  --build-arg BUILD_COMMIT=$(git rev-parse --short HEAD) \
  --build-arg BUILD_TIME=$(date -u +%Y-%m-%dT%H:%MZ) .
```
Build the sandbox from the repository root (never from `executor/`) and never
with `--target test`: that stage is root plus pytest. Give both images the SAME
tag, because the pair is the release.

The build args are what `GET /version` and the admin sidebar report — omit
them and the running image can only say when it started.

**Build from the COMMITTED tree, or pass the sha explicitly.** `git rev-parse
HEAD` is evaluated when the build command runs, so building with uncommitted
changes stamps the image with the PREVIOUS commit — the running container then
reports a commit that does not contain the code it is running, which is
indistinguishable from having deployed the wrong image. Either commit first,
or pass the intended sha (`--build-arg BUILD_COMMIT=<sha>`) and re-capture
anything that quotes `/version` afterwards.
(`:enterprise` stays a local/moving tag; customers get immutable tags so
rollback is deterministic.)

## 3. Verify the images carry nothing they should not
Two different questions. Both have been wrong before.

**Secrets.** `.dockerignore` excludes `*.env` — verify anyway, in both:
```
docker run --rm powerdatachat-client:enterprise-<tag> sh -c "ls -la /app | grep -c 'env'"
docker run --rm powerdatachat-executor:enterprise-<tag> sh -c "ls -la /app | grep -c 'env'"
```
No `.env` / `client.env` / `client.local.env` may exist in either image. A
tenant token baked into a shipped image means rotating that tenant's token.
The sandbox also refuses to START if it is handed a brain token, session key,
encryption key, admin password or upload bucket at run time. That refusal is
a backstop, not a substitute for this check.

**Local working material.** The web image is built with a whole-tree `COPY`
from the repository root, so ANY file present in your working tree ships
inside `/app` — including files git ignores. Git-ignoring something keeps it
out of the repository, not out of the artifact, and the customer receives the
artifact. Check the image, not the repository:
```
docker run --rm --entrypoint sh powerdatachat-client:enterprise-<tag> -c   'ls /app/docs; find /app -name "*.docx" -o -name "*.xlsx" -o -name "*.pptx" | head'
```
`/app/docs` must contain only the durable documents this product ships. Any
working folder, report, spreadsheet or document that is not part of the
product is a finding: add it to `.dockerignore` and REBUILD, because the tag
you already built still contains it. A structural test pins the current
exclusions, but a test cannot know about a folder invented after it was
written — this step is the one that can.

## 4. Ship
Registry push (per customer agreement) or tarballs, both images together:
```
docker save powerdatachat-client:enterprise-<tag> -o pdc-client-<tag>.tar
docker save powerdatachat-executor:enterprise-<tag> -o pdc-executor-<tag>.tar
```

## 5. Customer upgrade instructions (what they run)
Per `CUSTOMER_INSTALL.md` / `docker-compose.yml`: load/pull BOTH new images,
update the tags in their compose file, `docker compose up -d`. Their data
volume is untouched. An install upgrading from a release with no sandbox also
takes the new compose file (second service, two networks, jobs volume); see
the ownership note for the jobs volume in `CUSTOMER_INSTALL.md` §3.

**Rollback** = re-run compose with the previous tag on BOTH images, on the
same volume, so it must also be data-compatible (that's why stored shapes only
ever change backward-compatibly — including the Parquet writer: snapshots and
parse caches written by the current pyarrow are read back by the version the
previous image shipped, checked before release with the recipe in
`tests/test_parquet_old_writer_compat.py`, and both artifacts regenerate
anyway). Rolling back to an image older than the
non-root release works (root ignores ownership), but everything that image
writes afterwards is root-owned again, so rolling forward a second time needs
the one-time `chown -R 10001:10001` repeated.
