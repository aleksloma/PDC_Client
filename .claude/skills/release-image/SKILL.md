---
name: release-image
description: Build and ship the customer client Docker image — upgrade-safe by construction (new image, same data volume). Use for every client release handed to a customer.
---
# Customer client image release

The customer upgrade model: pull/load a new image, `docker compose up -d`
against their existing `pdc_client_data` volume. A release may touch NOTHING
but code — their users, chats, uploads, and history live on the volume.

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
- **Image runs as the non-root user** — nothing else in the repo checks the
  shipped artifact, so a lost `USER` line would be invisible until a customer
  ran it as root:
  ```
  docker image inspect --format '{{.Config.User}}' powerdatachat-client:enterprise-<tag>
  ```
  must print `pdc`. The compose files add the read-only rootfs and the limits,
  but the identity has to be in the image.
- **Dependencies still clean**: run the audit in `docs/BUILD_AND_RUN.md` §8
  (throwaway venv, the BUILT image's freeze as input, BOTH advisory services)
  and get no vulnerabilities. The pinned set is the security deliverable, and
  transitives that are not pinned can drift on any rebuild. The unit suite only
  guards the floors already reached — this scan is the detector.
- **The image ships what the pin file says**: nothing else verifies it, since
  the image has no test runner, so the suite's installed-equals-pinned check
  only ever covers the developer's venv.
  ```
  docker run --rm powerdatachat-client:enterprise-<tag> pip list --format=freeze > /tmp/image.txt
  grep -E '^[A-Za-z0-9].*==' requirements.txt | sed 's/\[.*\]//' | while read -r pin; do
    grep -qix "$pin" /tmp/image.txt || echo "MISMATCH: $pin"
  done
  ```
  Any `MISMATCH` line means the image was built from a different pin file (or a
  pin was edited after the build) — rebuild before shipping.

## 2. Build an immutable tag
```
docker build -t powerdatachat-client:enterprise-<git-sha-or-date> \
  --build-arg BUILD_COMMIT=$(git rev-parse --short HEAD) \
  --build-arg BUILD_TIME=$(date -u +%Y-%m-%dT%H:%MZ) .
```
The build args are what `GET /version` and the admin sidebar report — omit
them and the running image can only say when it started.
(`:enterprise` stays a local/moving tag; customers get immutable tags so
rollback is deterministic.)

## 3. Verify no secrets in the image
`.dockerignore` excludes `*.env` — verify anyway:
```
docker run --rm powerdatachat-client:enterprise-<tag> sh -c "ls -la /app | grep -c 'env'"
```
No `.env` / `client.env` / `client.local.env` may exist in the image. A
tenant token baked into a shipped image means rotating that tenant's token.

## 4. Ship
Registry push (per customer agreement) or a tarball:
```
docker save powerdatachat-client:enterprise-<tag> -o pdc-client-<tag>.tar
```

## 5. Customer upgrade instructions (what they run)
Per `CUSTOMER_INSTALL.md` / `docker-compose.yml`: load/pull the new image,
update the tag in their compose file, `docker compose up -d`. Their volume
is untouched. **Rollback** = re-run compose with the previous tag — same
volume, so it must also be data-compatible (that's why stored shapes only
ever change backward-compatibly — including the Parquet writer: snapshots and
parse caches written by the current pyarrow are read back by the version the
previous image shipped, checked before release with the recipe in
`tests/test_parquet_old_writer_compat.py`, and both artifacts regenerate
anyway). Rolling back to an image older than the
non-root release works (root ignores ownership), but everything that image
writes afterwards is root-owned again, so rolling forward a second time needs
the one-time `chown -R 10001:10001` repeated.
