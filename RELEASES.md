# PowerDataChat Enterprise — Client releases

One entry per tagged release, newest first. A release is always the PAIR of
images built from one commit: the web application and the analysis sandbox
(`pdc-executor`). They ship, upgrade and roll back together
(`CUSTOMER_INSTALL.md`, `docs/BUILD_AND_RUN.md`). Upgrades are image-only
against the existing data volume.

---

## Unreleased (on `main`, not yet tagged)

### Authlib 1.8.0 → 1.9.1 — 2026-10-28

**Why.** pip-audit (PyPI and OSV) reported six advisories against
`Authlib==1.8.0`: CVE-2026-96760 (an empty `signatures` array accepted by
`JsonWebSignature.deserialize_json`), CVE-2026-114615 (PEM / public-key bytes
usable as an HMAC secret), CVE-2026-114617 (an ID token's
`nonce_supported=false` claim disabled nonce validation), CVE-2026-114618 (the
default JWT-bearer grant did not match `aud`) and CVE-2026-114620 (an
unvalidated JAR `request_uri` fetch), all fixed in 1.9.0, and CVE-2026-114616
(an empty HMAC key accepted), fixed in 1.9.1. OSV lists a seventh against
1.8.0, CVE-2026-114619 (the OpenID implicit grant checked the raw scope instead
of the allowed scope), also fixed in 1.9.0. They made the CI `images` check
fail for every pull request. Authlib is the Microsoft Entra ID SSO library of
the web image only.

**Change.**
- `Authlib==1.9.1` in `requirements.txt` (the sandbox image has no Authlib).
- The regression floor `FIX_VERSIONS["Authlib"] = "1.9.1"` in
  `tests/test_dependency_pins.py`.
- Nothing else changed.

**Release notes reviewed.** The 1.9.0 breaking changes concern
authorization-server features this client does not use (JAR, `request_uri`,
JWT bearer, ID-JAG, RFC 7592) or stricter validation the authorization-code
flow already satisfies (the nonce is always enforced; the client always sends
an Authlib-generated nonce). The client's surface (`OAuth().register`,
`authorize_redirect`, `authorize_access_token`, `token["userinfo"]`,
`OAuthError`) is unchanged; a non-JSON token-endpoint body now raises an HTTP
error, which the callback's existing handlers turn into the same sign-in-failed
page. joserfc 1.7.5 and cryptography 50.0.1 already meet 1.9.1's floors
(`joserfc>=1.6.8`, `cryptography>=45.0.1`).

**Gates (local, before the pull request).**

| Gate | Result |
|---|---|
| Web suite, Python 3.11, Windows (`tests/test_export_plotly_png.py` excluded locally — kaleido does not start natively on this machine; CI runs it) | 5641 passed, 87 skipped, 0 failed |
| Local web image build | Built; the container reports Authlib 1.9.1 and joserfc 1.7.5, `/health` 200 |
| Microsoft sign-in smoke test (localhost, temporary SSO config) | `/auth/microsoft` 302 to `login.microsoftonline.com/common/oauth2/v2.0/authorize` with the expected client_id, redirect_uri, `response_type=code`, `scope=openid profile email`, state and nonce; `/?local=1` shows the password form; an error callback and a wrong state answer the sign-in-failed page (401); config removed afterwards |
| CI tests / executor-tests / images | Required on the pull request; merged only when all three pass |

### pillow 12.3.0 → 13.0.0 — 2026-10-23

**Why.** pip-audit (PyPI and OSV) reported 35 known vulnerabilities against
`pillow==12.3.0` (34 CVE-2026 entries and `GHSA-78g4-xqwv-q3g8`), all fixed in
13.0.0. They made the CI `images` check fail for every pull request. pillow is
the matplotlib / reportlab / python-pptx imaging dependency in both images.

**Change.**
- `pillow==13.0.0` in `requirements.txt` and `executor/requirements.txt`.
- The regression floor `FIX_VERSIONS["pillow"] = "13.0.0"` in
  `tests/test_dependency_pins.py`.
- Nothing else changed.

**Gates (local, before the pull request).**

| Gate | Result |
|---|---|
| Web suite, Python 3.11, Windows (`tests/test_export_plotly_png.py` excluded locally — kaleido does not start natively on this machine; CI runs it) | 4410 passed, 87 skipped, 0 failed |
| Both images build | yes, each with `pillow==13.0.0` installed and no pip |
| pip-audit 2.10.1, PyPI and OSV, each built image's installed set | no known vulnerabilities (web 102 packages, sandbox 52) |
| Localhost smoke on the rebuilt stack (`docker-compose.local.yml`) | A matplotlib PNG was rendered in the sandbox and decoded by pillow in the web container. A Plotly chart was converted to PNG by kaleido. A PDF (both charts embedded) and a PPTX (two PNG media) were built by `routes/report.py`. Both containers report uid 10001 / 10002, and their logs show no tracebacks |

---

## v1.0-security-r1 — 2026-09-28

**Commit:** `2089883072548edd5c9ba069e46362ea4e3bbd8a` (annotated tag `v1.0-security-r1`)

**Images** (Cloud Build `2d763656-5fc4-474b-81c8-f4319e9ca989`, built from the
tag, stamped `BUILD_COMMIT=2089883`, `BUILD_TIME=2026-09-28T19:22:00Z`):

| Image | Digest |
|---|---|
| `europe-west1-docker.pkg.dev/pdc-enterprise/client/pdcclient-demo:2089883` (web, uid 10001 `pdc`) | `sha256:a5119234aadaba1f883b0b1566601ce8070113c8e58003938e2f31a3cade7351` |
| `europe-west1-docker.pkg.dev/pdc-enterprise/client/pdcexecutor-demo:2089883` (sandbox, uid 10002 `pdcexec`) | `sha256:1931cb7588d510ceef60680a728218ce2ce1c621bfedd768abbfc7dfc2b88b04` |

The Artifact Registry names carry the internal demo's `-demo` suffix, but these
are the standard images. A customer build of the same commit follows
`/release-image` and gets its own tags and digests.

### Scope
The first release after the security re-assessment remediation:
- **Analysis sandbox:** generated Python runs in a separate `pdc-executor`
  container (its own uid, no secrets, no customer data mounted, no database
  drivers, one job at a time), reached over an internal network. The web
  service refuses requests arriving from the sandbox's network.
- **Hardened containers:** both run as non-root users on a read-only root
  filesystem with dropped capabilities and memory and process limits. All
  dependencies are pinned, and the builds apply current OS updates.
- **Rendering isolation:** a nonce-based Content-Security-Policy on every page.
  Chart documents are served from their own route under their own sandboxed
  policy, and styled table markup is sanitised.
- **Sign-in:** accounts come by invitation, a share or SSO. Passwords are set
  through mailed single-use reset links; sign-in and reset attempts are
  limited; the session cookie is Secure by default. Sessions end on a password
  change and have an absolute lifetime. There is a minimum password length.
  Administrators can end a user's sessions or remove a user.
- **Authorization:** chat, conversation, dashboard and refresh routes are
  bound to their owners and role grants. Refresh and re-run of a stored answer
  accept only code that the chat actually stored.
- **Uploads and logs:** uploaded filenames are sanitised and contained.
  Untrusted text is escaped in every log line.
- **Databases:** TLS is enforced per dialect when SSL is ticked, and free-form
  SQL passes a SELECT-only guard.

### Release gates
- Unit suite: 3676 passed, 73 skipped, 0 failed.
- pip-audit (PyPI and OSV) on `requirements.txt`, `executor/requirements.txt`
  and each built image's installed set: no known vulnerabilities.
- Pins: each image's installed versions match its pin file (0 mismatches).
- Trivy 0.74.0, HIGH and CRITICAL, by digest:

  | Image | CRITICAL | HIGH, no fix available | HIGH, fix available |
  |---|---|---|---|
  | web | 1 (no fix available) | 70 | 2 (accepted, see below) |
  | executor | 0 | 48 | 2 (accepted, see below) |

  Every OS finding is a Debian 13.7 package with no fixed version published
  (`affected` / `fix_deferred`). A later rebuild picks up fixes as Debian
  ships them.

### Accepted findings
Two Trivy HIGH findings with a fix version are **accepted for this release**,
the same two that were accepted in the 2026-09-17 image scan:

| ID | Package | Installed | Fixed in |
|---|---|---|---|
| GHSA-6v7p-g79w-8964 | msgpack | 1.1.2 | 1.2.1 |
| CVE-2025-47273 | setuptools | 70.3.0 | 78.1.1 |

Reasons:
- **Not installed dependencies of the product.** Both are copies vendored
  inside pip 26.2.1 (listed in `pip/_vendor/vendor.txt`), not packages the
  application installs or imports.
- **setuptools:** pip vendors only `pkg_resources` from it. The affected
  `PackageIndex` code is not present in the image at all.
- **msgpack:** it is reachable only when pip itself runs (its HTTP cache), and
  the running application never runs pip.
- **No fix available through pip.** pip 26.2.1 is the newest release on PyPI
  (checked 2026-09-28) and still vendors exactly these versions.

Revisit when a pip release vendors newer copies. The release gate
(`/release-image`) re-runs Trivy on every release.

### Deployed
Internal demo `pdcclient-demo`, revision `pdcclient-demo-2089883`, serving
100 % from 2026-09-28. The record and rollback are in `docs/DEMO_CLOUD_RUN.md`.

### Errata (2026-09-29)
Three statements in this entry were wrong. They are corrected here rather
than rewritten above, so the record of what was claimed stays visible.

- **"All dependencies are pinned."** Every DIRECT dependency is pinned in
  `requirements.txt`, and the installed versions of those match their pins.
  The installed set of the web image has 43 more packages that are pulled in
  transitively and pinned nowhere (among them `lxml`, `requests`, `urllib3`,
  `certifi`, `pydantic-core`, `pyyaml`, `packaging`). A rebuild of the same
  commit can therefore install different versions of those. The base image
  is referenced by tag, not by digest.
- **"Untrusted text is escaped in every log line."** Escaping covered the
  modules listed in the structural log guard, not every module. Other log
  lines still carried raw text, for example an upload's sheet names and
  exception texts in parts of the upload route and the account store, so a
  newline in a workbook's sheet name could start a forged log line.
- **"Unit suite: 3676 passed, 73 skipped, 0 failed."** The audit of
  2026-09-29 found that at this commit four tests in
  `tests/test_export_plotly_png.py` failed (they still posted the old
  request body) and six executor sweep tests failed when the executor suite
  ran as root. The figure came from a run that did not include those.

Also: the one CRITICAL web finding (libxml2, CVE-2026-6653) concerns the
Debian `libxml2` package in the image. The application does parse XML: every
.xlsx upload is read by openpyxl, which uses `lxml`. `lxml` 6.1.3 bundles and
statically links its own libxml2 2.14.6, so the Debian package is not on the
.xlsx path.

Verification of `main` after the September 2026 fixes (commit `a2ec7e6`,
CI run 36680671756, all three checks green), before the next release:

| Gate | Result |
|---|---|
| Web suite, Linux, non-root (CI `tests`) | 4091 passed, 63 skipped, 0 failed — the whole `tests/` directory, `tests/test_export_plotly_png.py` included |
| Executor suite as uid 10002 / as root (CI `executor-tests`) | 79 passed, 5 skipped / 83 passed, 1 skipped |
| pip-audit, PyPI and OSV, each image's installed set | no known vulnerabilities (web 103 packages, sandbox 53) |
| Trivy 0.74.0 HIGH/CRITICAL, web image | 0 CRITICAL (the libxml2 finding is gone with the package), 63 HIGH without a published fix, 2 HIGH with a fix: the accepted pip-vendored msgpack and setuptools above |
| Trivy 0.74.0 HIGH/CRITICAL, sandbox image | 0 CRITICAL, 48 HIGH without a published fix, the same 2 accepted |
| Re-assessment probes | forged session id (`tests/test_probe_sid_forge.py`) and chart-export SSRF (`tests/test_probe_ssrf_plotly_png.py`, rendering half with real Chromium on Linux) both refused; the original probe scripts were not in the repository and are re-created as these tests |
