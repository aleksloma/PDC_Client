# PowerDataChat Enterprise — Client releases

One entry per tagged release, newest first. A release is always the PAIR of
images built from one commit: the web application and the analysis sandbox
(`pdc-executor`). They ship, upgrade and roll back together
(`CUSTOMER_INSTALL.md`, `docs/BUILD_AND_RUN.md`). Upgrades are image-only
against the existing data volume.

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

Current state of the working tree on 2026-09-29, before the next release:
web suite 3938 passed, 74 skipped, 0 failed (`tests/test_export_plotly_png.py`
run separately in a Linux container: 8 passed); executor suite as uid 10002
79 passed, 5 skipped, and as root 83 passed, 1 skipped.
