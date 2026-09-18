# Executor protocol — `pdc-client` ↔ `pdc-executor`

The internal contract between the web service and the analysis sandbox.
Generated Python runs in `pdc-executor`, never in the web process.

This document describes the **implemented** surface: `exec_transport.py`
(imported by both sides), `executor/app.py` (the HTTP service) and
`executor/runner.py` (one subprocess per job). It is the one home for the
field-level contract — `docs/ENTERPRISE_ARCHITECTURE.md` covers the topology,
`docs/AI_CONSTITUTION.md` Article VII the rules, and `docs/PROTOCOL.md` is a
different thing entirely (the Brain's `/v1/*` surface).

> Status: implemented and wired. `code_exec.safe_execute` and
> `plot_utils.render_plot_safe` keep their signatures and return shapes and
> dispatch here (`executor_client.py`); their former bodies are
> `code_exec._execute_in_process` and `plot_utils._render_in_process`, which
> only `executor/runner.py` calls. Both compose files declare the two
> services together on an `internal: true` network with a shared jobs volume,
> so a stack brought up from this repository answers questions. A stack that
> starts only the web container answers every question
> `ExecutorUnavailable` — that is a broken install, not a degraded mode.

---

## 1. Trust model

The executor runs **untrusted code by design**. Everything else follows from
that:

- **It holds no secrets.** No brain token, no session key, no encryption key,
  no database credential. It refuses to start if any of them is in its
  environment (§7).
- **It has no database access.** SQLAlchemy, every network driver, and the
  credential-bearing modules of the web service are absent from the image, so
  the sandbox denylist (`sandbox_guard.py`) is defence in depth on top of
  their simple absence. `sqlite3` is the one the denylist genuinely carries
  on its own: it ships with Python and cannot be left out of the image. That
  is not a route to the customer's data, which is what this property is
  about, but the sentence would be wrong without it.
- **`POST /execute` is unauthenticated.** This is deliberate and is a recorded
  risk acceptance, not an oversight. A shared secret would put a secret into
  the one container whose defining property is that it holds none, and it
  would protect nothing that the network does not already protect. **The
  control is the network**: the executor joins an internal-only Docker
  network, publishes no ports, and is reachable only from the web container.
  Any deployment that publishes the executor's port, or puts it on a routable
  network, breaks the model. **Enforced by both compose files** (`backend`,
  `internal: true`, no `ports`, pinned by a structural test), and because a
  Docker network is bidirectional the web service additionally refuses any
  request whose peer address falls inside that subnet — generated code cannot
  call back into the unauthenticated login or reset endpoints. A standalone
  run for development, outside compose, has neither protection and should
  bind to loopback only.
- **The web service treats every response as hostile** (§5). The executor can
  write anything into the shared job directory, including symlinks and FIFOs,
  and the web service reads it under a mount where customer data IS present.

## 2. Shape of one job

```
web service                             executor
-----------                             --------
create <jobs>/<job_id>/ and in/   ──▶   (the directory must already exist)
write in/<n>.parquet                    POST /execute
POST /execute ────────────────────▶     write job.json
                                        spawn: python -I -u executor/runner.py job.json
                                          ├─ set resource limits, then import
                                          ├─ read_inputs()
                                          ├─ _execute_in_process / _render_in_process   (the SAME code as before)
                                          └─ serialize_result() ──▶ response fd
                                        collect stdout/stderr, wait, kill group,
                                        sweep strays, read the response
◀──────────────────────────────── 200   {status, kind, payload, …}
deserialize_result()
rm -rf <jobs>/<job_id>/
```

One job at a time per executor (`EXECUTOR_MAX_CONCURRENT`, default 1). The
web service owns creation and deletion of the job directory; the executor
never deletes one except the orphan sweep (§7).

## 3. Job directory and ownership

```
<EXECUTOR_SHARED_DIR>/<job_id>/        2770  web-uid : shared-gid
    in/                                2770  web-uid : shared-gid
        <n>.parquet | <n>.pkl          0644 (the web umask; group-read is what matters)
    job.json                           written by the executor
    out/                               2770  executor-uid : shared-gid
        result.parquet                 0660
        result_<i>.parquet             one per entry of a dict RESULT
        figure.png | chart_<n>.png     only when a payload exceeds the inline cap
        chart.html | chart_<n>.html    same, for plotly HTML
```

**Two identities, one shared group.** The web service runs as uid 10001, the
executor as uid 10002, and both are in gid 10001. Neither is root.
`exec_transport.create_job_dir` sets mode `2770` **explicitly** on the job
directory and on `in/` rather than relying on a umask — the web service's
umask is `0022`, which would otherwise strip group write and make the first
result write fail. The setgid bit keeps the group on everything created
inside. Both executor processes run with `umask 007`.

Group write on `in/` is deliberate: it is what lets the executor remove an
abandoned job directory during the orphan sweep.

A freshly created named volume first mounted by the executor image inherits
`root:<shared-gid> 2770` from the image, so both identities can create job
directories in it. A volume created by any other image, or one whose mode
comes from `driver_opts`, needs that ownership applied once — the compose
files and the install guide own that step.

**Nothing under `DATA_ROOT` is ever mounted into the executor.** Not the
snapshot directory, not the per-chat parquet caches. Every input frame is
written per job instead. A whole-directory mount would let generated code read
tables the asking user's role does not grant, bypassing the role gate
entirely.

## 4. Request

```
POST /execute        Content-Type: application/json

{
  "job_id":     "<32 lowercase hex>",
  "kind":       "PYTHON" | "PLOT",
  "code":       "<generated python>",
  "dataframes": [ {"name": "sales.csv::Sheet1",
                   "path": "in/0.parquet",
                   "format": "parquet" | "pickle"} ],
  "timeout_s":  60,
  "options":    {"split_multi_axes": false}
}
```

`dataframes` **is the manifest** and its ORDER is load-bearing: `read_inputs`
rebuilds the dict in list order, and `code_exec` aliases `df` to the first
entry, so re-sorting it would silently point historical stored code at another
table.

Rejections are `400` with `{"code": …, "message": …}`: `BAD_JOB_ID`,
`BAD_KIND`, `CODE_TOO_LARGE` (1 MiB), `TOO_MANY_FRAMES` (64), `BAD_TIMEOUT`
(must be in `(0, EXECUTOR_MAX_TIMEOUT_S]`), `BAD_INPUT_PATH`,
`JOB_DIR_INVALID` (missing, not a directory, or a symlink).

`503 EXECUTOR_NOT_READY`, same body shape, means the service is up but its
startup never completed — it will not execute anything in that state, and in
particular it has not run the secret self-check. Under its own start command
the startup hook always runs, so this is a guard against being driven in a way
that skips it rather than an expected response.

Input paths must match `in/<name>` with a plain name, resolve inside the job
directory, be regular files and carry a known format — checked in the route
before anything spawns and again in `read_inputs`, because a pickle is
deserialized on the executor side.

## 5. Response

```
{
  "status":      "ok" | "error" | "timeout" | "killed" | "crashed",
  "kind":        "PYTHON" | "PLOT",
  "payload":     { … the caller-shaped dict, see below … } | null,
  "elapsed_ms":  1157,          # from runner process start, so imports show
  "peak_rss_mb": 269.7,
  "stdout":      "…",           # the generated code's own output, capped
  "stderr":      "…",
  "traceback":   "…",           # runner-side failures only
  "exit_code":   0,
  "signal":      null,
  "reason":      null
}
```

`payload` carries **every key of the in-process return dict verbatim,
including the absences** — `is_plotly` is missing on a matplotlib multi-axes
error, `ok` never appears on the `safe_execute` shape, and `error: None` is
present on success. Consumers distinguish the two producers by which keys
exist, so the shape must not be normalized.

`stdout`, `stderr` and `traceback` are for the local log only, and the web
service now writes a truncated tail of the last two onto its own
`EXEC_ERROR` / `EXEC_TIMEOUT` lines — in-process, a failing block's traceback
landed in the client log, and the sandbox's own file log sits on a tmpfs that
a restart wipes. **Nothing new crosses to the Brain**: the retry text stays
exactly `payload["error"]`.

### Serialization matrix (`result`)

| RESULT | Wire | Rebuilt as |
|---|---|---|
| `None` | `{"kind": "none"}` | `None` |
| scalar | `{"kind": "scalar", "value": …}` (numpy scalars via `.item()`) | the same value |
| list / nested dict of JSON-native values | `{"kind": "json", …}` | the same value |
| `DataFrame` | `{"kind": "frame", "ref": "out/result.parquet"}` | `read` → `DataFrame` (index and label types restored from the parquet metadata, MultiIndex included) |
| `Series` | `{"kind": "series", "ref": …, "unnamed": bool}` | `Series` (`unnamed` restores `.name = None`, which parquet turns into `0`) |
| Styler | `{"kind": "styler", "ref": …, "styled_html": str \| null}` | a two-attribute value object with `.data` and `.to_html()` |
| dict of the above | `{"kind": "dict", "entries": [{"key", "key_type", "value"}]}` | a dict in the original order, keys restored to their type |
| anything else | `{"kind": "opaque", "type_name": "Figure"}` | an `Opaque` marker — no repr, no content |

Result frames are written **uncompressed** and read back under caps (§6).

**Three accepted-lossy normalizations** before a frame is written, each
because pyarrow refuses or silently changes the value otherwise:

1. duplicate or tuple column labels are flattened and de-duplicated with the
   same `name.1` convention the table builder already applies;
2. an object column mixing numbers and strings has its non-null values cast to
   `str`, keeping NaN;
3. mixed-type column LABELS (what `crosstab(margins=True)` produces) are cast
   to `str` — pyarrow does this itself with a warning, so doing it explicitly
   makes the loss documented instead of incidental.

A frame that still cannot be written comes back as an ordinary execution
error, `ResultSerializationError: …`.

### `preview` and the data boundary

`preview` exists only to feed the web service's `_safe_preview` guard, whose
verdict must not change. The encoder therefore **mirrors that guard exactly**,
including its quirks: a dict passes only when every key is a plain
`str|int|float|bool` and every value is a scalar, `None`, or an object whose
`.item()` yields one; anything else becomes an opaque marker, which the guard
then drops exactly as it drops a `Timestamp` today.

**NaN and Inf travel as NaN and Inf**, never as `null`. The whole hop uses
Python's JSON dialect (`allow_nan=True`) and the executor answers with a plain
response rather than the framework's JSON response, which would refuse them.
This is not cosmetic: `_safe_preview(nan)` returns `nan` today and the Brain
receives the literal token, so coercing it would be a protocol-visible change.
A test asserts wire-byte parity with the in-process guard across the whole
matrix of scalars, numpy types, keys and container shapes.

### Status mapping

| `status` | What it means | The web service returns |
|---|---|---|
| `ok` / `error` | the job ran; `error` means the code raised or a guard refused | the payload, key for key as today |
| `timeout` | the job exceeded `timeout_s`, including time spent queued INSIDE this service | `TimeoutError: Code execution exceeded N seconds limit` — byte-identical to the in-process text |
| `killed` | a SIGKILL the executor did not send, i.e. the cgroup out-of-memory killer | `MemoryError: execution exceeded the memory limit` |
| `crashed` | any other abnormal exit: a segfault, a failed import, or exit 0 with an unusable response | `ExecutorCrashError: the analysis process exited unexpectedly (…)` |

`killed` and `crashed` are separate on purpose. Reporting a segfault as a
memory error tells the model to optimise memory after a crash it cannot fix,
and burns every retry doing it. A `MemoryError` **raised inside** the code is
an ordinary `error` carrying the interpreter's own message.

The `crashed` text quotes the response's `reason`, so that field is checked
against the executor's closed vocabulary (`spawn_failed`, `hard_timeout`,
`sigkill`, `response_invalid`, `signal`, `exit`, `queued`, `executor_error`)
and anything else becomes the literal `unknown`; an unrecognised `status` is
likewise never echoed. The reason: this text reaches the Brain's retry
prompt, and the response is written by the untrusted side — the same class of
defect already closed for the rejection code, where an unbounded string could
have carried arbitrary prose and newlines into a prompt.

An integral `timeout_s` renders as an integer (`60`, not `60.0`) so the
timeout text matches the in-process one character for character — that string
reaches the Brain's retry prompt.

## 6. What the web service refuses to read

Every reference is opened through a directory-handle chain that refuses to
follow symlinks at any component and requires a regular file, so a symlinked
`out/` cannot redirect a read into the customer-data mount and a planted FIFO
cannot block a web worker forever. Pickles are never read in this direction —
only into the executor, where both writer and reader are this codebase.

Result parquet is accepted only when **every column chunk is uncompressed**,
then under row, column, file-size and decoded-byte caps, read in batches that
are summed as they arrive. Metadata alone is not trusted: it is written by the
untrusted side, and even honest metadata understates the decoded size by
orders of magnitude for dictionary-encoded data.

Any violation is reported as a failed execution
(`ExecutorResponseError: …` / `ResultTooLarge: …`), never as an exception into
the request. Inline strings, chart data and error text are capped; unknown
payload keys are dropped.

**Residual risk, accepted:** a deliberately crafted single batch can still
exceed the decoded cap in one step. The web service is nonetheless strictly
better off than while executing in-process, where the same code could
allocate the whole container.

## 7. Executor service

Configuration is **environment only** — no `.env` is read, and no
configuration file exists:

| Variable | Default | Meaning |
|---|---|---|
| `EXECUTOR_SHARED_DIR` | `/jobs` | the shared job volume |
| `EXECUTOR_MEM_LIMIT_MB` | `2048` | address-space limit per job |
| `EXECUTOR_MAX_CONCURRENT` | `1` | jobs in flight — **not an ordinary knob**, see below |
| `EXECUTOR_MAX_TIMEOUT_S` | `600` | the largest `timeout_s` a request may ask for |
| `EXECUTOR_GRACE_S` | `15` | how long past `timeout_s` the parent waits before killing |

The port is fixed at **8090** by the image's own start command and is not
configurable by environment; it is internal only and must never be published.

**What one job can leave for the next one.** Two places, both measured by
running two separate jobs and reading the first one's file back from the
second.

`/tmp` is a single world-writable directory on a tmpfs; every job runs as the
same user and only a restart clears it. The jobs volume's ROOT is
group-writable — necessarily, since this service deletes its own finished job
directories — so a job can write straight into it, and that volume is
disk-backed, visible from the web container, and survives restarts and image
upgrades. Both sweeps remove aged entries of the jobs root that are not job
directories, which bounds a stash there to roughly an hour; `/tmp` has no
such bound.

The two sides decide differently, and the asymmetry is the point. The web
service removes only what it did NOT write, because everything under its own
data root belongs to it and that is what makes a misconfigured jobs
directory harmless. This service removes every aged non-job entry whatever
its owner, because ownership proves nothing here: the root is group-writable
so that this service can clear an abandoned job directory, which also lets it
RENAME one, and a rename keeps the original owner — generated code can
therefore acquire a web-owned entry rather than create one (measured: it
renamed its own live job directory, and both sweeps then skipped the result).
Nothing of the customer's is mounted in this container, so removing a
web-owned stray here costs nothing that the ownership rule protects on the
other side. Neither sweep follows a symlink.

The per-job input write bounds what a job is GIVEN, which is what makes the
per-role table grants meaningful on the way in. It does not bound what a job
can deposit on the way out, so read that property as applying to the input
path and treat both locations as scratch space shared between consecutive
analyses. Closing it properly needs a private scratch namespace or a distinct
identity per job: redirecting the temporary directory would not, because
generated code can name an absolute path, and the jobs root cannot be made
unwritable without disabling this service's own cleanup.

**Raising `EXECUTOR_MAX_CONCURRENT` forfeits guarantees, it does not just add
throughput.** Generated code keeps filesystem access and every job runs as the
same user, so with two jobs in flight one can read the other's inputs during
its load window, plant a symlink where the other's result will be written, or
signal its process. The stray sweep, which is what catches a job that detaches
itself, is also skipped while a sibling job is running — so a leaked pipe can
additionally cost a job its response. Concurrency above one is only defensible
with a separate identity per job, which this service does not do. Treat the
default as part of the design.

**It refuses to start** when any of `BRAIN_*`, `SECRET_KEY`,
`CLIENT_ENCRYPTION_KEY`, `CLIENT_ENCRYPTION_KEY_OLD`, `LOCAL_ADMIN_PASSWORD`
or `GCS_UPLOAD_BUCKET` is non-empty: it logs
`EXECUTOR_REFUSED_SECRET_ENV name=<var>` and exits. The check runs after the
settings module is imported, so a value that arrived through a mounted `.env`
is caught too. In a container this presents as a non-zero exit with
`Application startup failed` and nothing ever served. One compose
copy-and-paste of the web service's `env_file` is all it would take, and every
field has a default, so nothing else would have failed loudly.

`GET /healthz` returns `{"ok": true, "version", "build_time", "versions": {…}}`
— the five library versions the web service needs to detect an image mismatch
between the two containers. **It answers while a job runs**: the job wait is
on a worker thread, never on the event loop, so a health check cannot be
starved by a 120-second render.

**Each job runs in a fresh subprocess** started with `python -I -u` in a new
session, with an environment built from scratch as an allowlist — so even a
container wrongly handed the web service's environment passes none of it to
generated code. The runner sets its resource limits **before** importing
anything heavy: an address-space limit, a process limit as a fork-bomb brake,
a file-size limit that turns a disk-filling write into an ordinary execution
error, and no core dumps. Numeric libraries are pinned to one thread each and
the Arrow allocator to the system one, without which the address-space limit
would be exhausted by reservations before any user code ran.

The parent then, **in this order**: waits, kills the process group, sweeps any
remaining process of its own uid except itself and its parent, and only then
reads the response. The order is load-bearing — code that forks and calls
`setsid()` escapes the process-group kill while still holding the response
pipe open, so sweeping after the read loses the response of a job that
actually succeeded. The parent exclusion matters because the service is not
always the first process in its container.

One consequence to know about: the sweep also kills anything else running as
the service's own user at that moment — a Docker `HEALTHCHECK` probe, or an
operator's `docker exec`. A single killed probe is absorbed by the health
check's retries; an interactive command may simply die mid-job. That is the
cost of guaranteeing no process of the job survives it.

Abandoned job directories older than an hour are removed at startup and every
ten minutes. The web service owns the normal deletion; this only covers a web
service that died holding a job.

## 8. Logging

The executor logs to stdout and to a rotating file under a `DATA_ROOT` that is
a throwaway tmpfs (5 MiB × 2) — it keeps no state, so **`docker logs
pdc-executor` is the evidence path**, not a file on a volume. Each job logs
one `EXEC_JOB_START` and one `EXEC_JOB_END` line carrying the status, elapsed
time and exit code, with the job id as the session id. The process sweep logs
`EXEC_STRAY_KILLED`, and the orphan sweep `EXEC_ORPHAN_REMOVED` for an
abandoned job directory and `EXEC_STRAY_ENTRY_REMOVED kind=file|dir|link|other`
for something generated code left in the jobs root (with
`EXEC_STRAY_ENTRY_REMOVE_FAILED` when it cannot). The web service adds
`EXEC_STRAY_SWEEP_REFUSED` on the one configuration where it declines to
look at strays at all. A removal line is worth reading rather than
filtering: it means a question wrote something outside its own job
directory.

The web service re-emits its own `EXEC_OK` / `EXEC_ERROR` / `EXEC_TIMEOUT`
lines with the same code hash as before, plus `EXEC_DISPATCH` when it hands a
job over. Both sides carry the code hash and the job id — the job id is this
service's session id — so one job can be followed across the two containers
from either log.

**Where each line actually lands.** The service's own lines (`EXECUTOR_START`,
`EXEC_JOB_START` / `EXEC_JOB_END`, the sweeps, the startup refusal) go to
stdout and so to `docker logs`. The RUNNER's own lines — its start and end
records, and the `EXEC_OK` / `EXEC_ERROR` that the execution module emits
inside the job — are written on its inherited stdout, which is a pipe the
parent reads but keeps only when the job produced no usable response. On a
normal job the runner's own response already carries `stdout` (the generated
code's output, captured separately), so the pipe tail is dropped. Those lines
therefore reach **neither** the response nor `docker logs`: they exist only in
the runner's rotating file under its throwaway `DATA_ROOT`, readable with
`docker exec` until the container restarts. Do not expect a per-job `EXEC_OK`
in the container log, and do not treat the absence of one as a failure.

## 9. Image

Built from the repository root. **The default target is the hardened image** —
`docker build -f executor/Dockerfile -t powerdatachat-executor:enterprise .`
— and `--target test` is the only way to get the privileged image that carries
a test runner. That ordering is deliberate: a compose build passes no target,
and an image that ran as root with a test runner installed would undo the
unprivileged-container requirement that the rest of the stack already meets. A structural test asserts
the default target ends unprivileged with no test runner in it.

The image copies **only** the modules the runner imports — the two execution
modules, the dtype gate, the sandbox guard, the outlier helpers, the logger,
the settings module and the transport — plus the Georgian font and the offline
plotly bundle. Never `COPY . .`. There is no `local_store`, no `db_*`, no
brain client, no route, no template.

Its pin set is an **identical-version subset** of the audited root
requirements: same versions, nothing added. A structural test asserts both
halves, because a drift in the chart library between the two images would
change the HTML that the report path parses. Two entries look like web
dependencies and are not: the imaging library is there for matplotlib and the
word cloud, and the template library is there because pandas implements
`DataFrame.style` with it — the executor renders styled tables, and no
template is ever rendered in that image.

The chart rasterizer and its headless browser are deliberately absent. Plotly
figures cross as HTML; only the web service's report and export paths
rasterize, so the sandbox image carries no browser.

## 10. Web service side

`executor_client.execute(kind, code, dfs, sid, timeout_s, split_multi_axes)`
is the only caller of this protocol. `code_exec.safe_execute` and
`plot_utils.render_plot_safe` are thin wrappers over it and keep their old
signatures, so nothing upstream changed.

| Variable | Default | Meaning |
|---|---|---|
| `EXECUTOR_URL` | `http://pdc-executor:8090` | where the sandbox answers; internal only |
| `EXECUTOR_SHARED_DIR` | `<DATA_ROOT>/exec_jobs` | the job volume, resolved at call time. **It must be the same storage the sandbox mounts at its own `EXECUTOR_SHARED_DIR`**, or every job is refused as `JOB_DIR_INVALID` |
| `EXECUTOR_CONNECT_TIMEOUT` | `5` s | bounds the connect only |
| `EXECUTOR_PLOT_TIMEOUT_S` | `120` s | the budget for a chart; analysis blocks use `code_exec.CODE_EXEC_TIMEOUT_SECONDS` (60 s) |
| `EXECUTOR_MAX_CONCURRENT` | `1` | jobs dispatched at once — must never exceed the sandbox's own limit (§7) |
| `EXECUTOR_QUEUE_MAX_S` | `600` s | how long a job may wait for a slot before it is answered "busy" |
| `EXECUTOR_NETWORK_CIDR` | *(empty)* | the sandbox network's own subnet. Requests whose peer address falls inside it are answered 403: a Docker network is bidirectional, so this is what stops generated code calling the web service's unauthenticated endpoints. Compose sets it from the same variable that pins the network, so the two cannot drift. Empty disables the refusal (a single-container dev run) |

Every one is read at call time, and a malformed value falls back to its
default rather than raising during import — a settings module that throws
crash-loops the container before any log exists.

**The dispatch gate.** A job acquires a semaphore sized from
`EXECUTOR_MAX_CONCURRENT` *before* anything is created, and its `timeout_s`
starts on acquisition. Two consequences, both deliberate: the sandbox's own
queue stays empty, and a job that waited still gets its full budget — it is
never told its code was too slow because a sibling was slow. A wait that
outlives `EXECUTOR_QUEUE_MAX_S` is answered `ExecutorBusy`, which is a
distinct text precisely so it can never be read as a code overrun. Chart
rendering is therefore serialized where it used to run four-ways parallel in
the web process, and it has a deadline where it used to have none.

**Failure vocabulary.** All five are ordinary execution errors in the
caller's own shape, never exceptions, because every caller — the chat stream,
the report renderers, dashboard refresh, Auto Analytics — treats a returned
error as an answer it can handle:

| Text | When |
|---|---|
| `ExecutorUnavailable: the analysis service is not reachable` | connect refused, DNS failure, read timeout, any transport error |
| `ExecutorBusy: the analysis service is busy, try again` | no slot within `EXECUTOR_QUEUE_MAX_S` |
| `ExecutorError: the analysis service rejected the job (<CODE>)` | any non-200; `<CODE>` is the sandbox's own code (§4), validated as a short token because the body is untrusted and this text reaches the planner's retry prompt |
| `ExecutorError: cannot prepare the job (<Type>)` | the job directory or the input write failed before dispatch |
| `ExecutorError: the analysis answer could not be read (<Type>)` | reconstruction itself raised. It is written never to raise, so this is the belt on top of the braces — and the caller still gets an answer it can handle |

None of them carries a URL, a path or any data: the detail goes to the local
log, the text goes upstream.

**Which failures the brain should be asked to fix.**
`executor_client.is_infrastructure_error(text)` is the public predicate the
chat loops use to decide whether a failed execution is worth a retry. The
five texts above are infrastructure: no prompt can fix a service that is
down, busy, or refusing the job, and retrying one costs a planner call plus
another wait for a slot. A `TimeoutError`, a `MemoryError` and a
`ResultTooLarge` are NOT in that set on purpose — the planner can genuinely
write cheaper code — and neither is a crash the generated code caused
(`reason=exit` or `reason=signal`), for the same reason. Without this split
one unreachable sandbox turned a single question into three pro-tier planner
calls and three further waits.

**Reported reachability.** `GET /health` on the web service carries
`executor_reachable` and `executor_checked_at`. The value is a CACHED
observation — the startup handshake, every dispatch outcome, and a
short-lived background probe refresh it — never a live call made while the
request waits. That is deliberate: a stopped sandbox drops out of Docker's
DNS, the lookup then falls through to the host resolver and costs seconds
before any HTTP timeout applies, which would block the event loop and fail
the container's own health probe at the exact moment the field exists to
report. `/health` therefore stays 200 with `executor_reachable: false`, and
an operator must not read a healthy container as a working stack.

**A job directory the sandbox locked.** Generated code can `chmod` a
directory it created under `out/`. The sandbox's own sweep repairs the mode
and removes it within the hour; the web service cannot, because it may not
chmod what the sandbox uid owns. It therefore logs such a failure once per
path per process rather than on every sweep. If the sandbox is stopped or
removed, those directories stay until it returns — they hold one job's input
frames, so the jobs volume is worth a glance after a failed upgrade.

**Job directory lifecycle.** The web service creates it, writes the inputs,
and removes it in a `finally` — success, failure and refusal alike. A removal
that fails is logged, not retried, because the sweeps cover it: this side
sweeps job-id-shaped directories older than an hour at startup and
opportunistically after a dispatch, and the sandbox does the same on its own
schedule (§7). Only 32-hex directory names inside the shared directory are
ever touched.

**Version handshake.** At startup the web service calls `/healthz` and logs
one warning per library whose version differs from its own, plus
`EXECUTOR_HANDSHAKE_OK` when it answers at all. A mismatch is never fatal —
the two images are versioned independently and a customer can upgrade one
first — and it is worth checking when charts or reports come back subtly
wrong, because the report path parses chart HTML that the plotting library's
version decides.

Be precise about what it does NOT tell you. It compares **five libraries**
(pandas, numpy, pyarrow, matplotlib, plotly) out of the twenty-three that
`executor/requirements.txt` pins, and it compares **no code at all**:
`/healthz` reports the
sandbox's build commit and the web service only logs it. Two images built
from different commits — exactly the case where the wire grammar in
`exec_transport.py` can differ, since it is copied into both — produce a
silent handshake. So a quiet handshake means "these five libraries match",
not "these images match". Build and ship the pair together.
