---
name: sync-docs
description: Reconcile PDC_Client docs with the code — code is the source of truth. Run after route/module/UI-contract changes or whenever doc drift is suspected. Includes the cross-repo mirror check with PDC_Brain.
---
# Sync docs to code (code wins)

## 1. CLAUDE.md vs reality
- Endpoint lists: `grep -ohE '@router\.(get|post|put|delete)\("[^"]+"' routes/*.py`
  (mind the router prefixes: chat/report use `/api/chat`) vs the
  file-reference table and layout in `CLAUDE.md`.
- Module list: every non-trivial `*.py` at root appears in the layout; no
  listed file is missing from disk.

## 2. docs/CLIENT_ENDPOINTS.md vs routes/
The `/lab` dashboard contract: paths, methods, request/response shapes,
SSE event names, and the intentional 400 stubs (GCS upload, publish) must
match the handlers. `static/dashboard.js` / `chat.js` fetch calls must have
a documented counterpart.

## 3. Cross-repo mirror (workspace rule 6)
`AI_CONSTITUTION.md`, `ENTERPRISE_ARCHITECTURE.md`, `PROTOCOL.md`,
`BUILD_AND_RUN.md` are duplicated into both repos and must be
**byte-identical**:
```
diff docs/AI_CONSTITUTION.md ../PDC_Brain/docs/AI_CONSTITUTION.md
diff docs/ENTERPRISE_ARCHITECTURE.md ../PDC_Brain/docs/ENTERPRISE_ARCHITECTURE.md
diff docs/PROTOCOL.md ../PDC_Brain/docs/PROTOCOL.md
diff docs/BUILD_AND_RUN.md ../PDC_Brain/docs/BUILD_AND_RUN.md
```
On drift: the copy matching the CODE wins; copy it over the stale one and
commit in BOTH repos. `CLIENT_ENDPOINTS.md` is client-only; `DEPLOY.md` is
brain-only — do not mirror those.

## 4. Fix direction
Docs are corrected to match code — never "fix" code to match a doc unless
the code is genuinely the bug (then say so explicitly and treat it as a code
change with tests). Delete contradictions; never leave both versions
(Constitution Art. X).
