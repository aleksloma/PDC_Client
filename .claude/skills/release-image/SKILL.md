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

## 2. Build an immutable tag
```
docker build -t powerdatachat-client:enterprise-<git-sha-or-date> .
```
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
ever change backward-compatibly).
