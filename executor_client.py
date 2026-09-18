"""HTTP dispatch to the analysis-sandbox service — MAIN APP ONLY.

`code_exec.safe_execute` and `plot_utils.render_plot_safe` hand every piece of
generated Python to the sandbox container through `execute()` here; nothing in
this process ever runs it. The two public functions import this module LAZILY,
because the sandbox image ships `code_exec.py` / `plot_utils.py` verbatim and
has no httpx: a module-scope import there would break the sandbox, and the
sandbox must never hold the client that dispatches to it (it would ask itself
to run the job).

The job travels on the shared jobs directory, the request only names it:
`<shared dir>/<job id>/in/*.parquet` written here, `out/*` written by the
sandbox, both identities in the same group. This module owns that directory's
whole lifecycle — create, remove in a `finally`, and sweep what a crashed web
worker, or generated code writing straight into the group-writable root, left
behind.

Nothing here raises (Article IV): every failure — an unwritable job directory,
an unreachable service, a refused job, a hostile answer — comes back as the
caller's own error dict, the shape the callers already handle.
"""
from __future__ import annotations

import hashlib
import importlib
import os
import re
import shutil
import stat
import threading
import time
from contextlib import suppress
from pathlib import Path

import httpx

import exec_transport
from logger_utils import log_with_sid
from settings import settings

# The HTTP read must outlast the sandbox's own hard kill (`timeout_s` + its
# grace): a job that answers late must be READ, not reported as unreachable.
_READ_TIMEOUT_MARGIN_S = 30
_WRITE_TIMEOUT_S = 30

_DIR_MODE = 0o2770
_ORPHAN_MAX_AGE_S = 3600
_SWEEP_INTERVAL_S = 600

# Compared against the sandbox's own `/healthz` report: the two images are
# versioned and upgraded independently, and a chart that silently renders
# differently is worth a log line.
_VERSION_MODULES = ("matplotlib", "numpy", "pandas", "plotly", "pyarrow")

UNAVAILABLE_TEXT = "ExecutorUnavailable: the analysis service is not reachable"
BUSY_TEXT = "ExecutorBusy: the analysis service is busy, try again"
_REJECTED_TEXT = "ExecutorError: the analysis service rejected the job ({code})"
# The shape of a rejection code the sandbox may claim (BAD_JOB_ID, BAD_KIND,
# CODE_TOO_LARGE, ...). The response is untrusted and this value reaches the
# planner's retry prompt, so it is validated rather than passed through.
_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,39}")
_PREPARE_TEXT = "ExecutorError: cannot prepare the job ({exc})"
_UNREADABLE_TEXT = "ExecutorError: the analysis answer could not be read ({exc})"

# The prefix of every failure this hop invents, and the two reasons that are
# NOT one — see `is_infrastructure_error`.
_INFRA_PREFIX = "Executor"
_CODE_CAUSED_CRASH_REASONS = ("reason=exit", "reason=signal")

# Test seam: an `httpx.BaseTransport` used for BOTH `/execute` and `/healthz`,
# so a handshake retry is mockable too. None in production.
_TRANSPORT = None

# The dispatch gate. Every other setting is read at call time; the gate's SIZE
# cannot be, because the semaphore has to exist before the first caller — so
# it is built on first use and rebuilt whenever the setting's value changes.
_GATE_LOCK = threading.Lock()
_GATE: dict = {"size": None, "semaphore": None}

# A failed startup handshake buys ONE retry on the first dispatch; a version
# difference is never fatal, so the retry only ever produces log lines.
_PENDING: dict = {"handshake": False}

# Opportunistic orphan sweep after a dispatch — no thread: an always-on
# background thread leaks into every pytest session.
_LAST_SWEEP: dict = {"at": 0.0}

# Cached reachability, for `/health` to REPORT rather than measure. `ok` is
# None until something has actually talked to the service — unknown is not the
# same answer as "down". A live probe inside the handler is exactly what this
# avoids: an unresolvable sandbox name costs ~8 s in `getaddrinfo` before any
# HTTP timeout applies (measured in the running container), `/health` is an
# async handler calling sync clients, and the container's own healthcheck
# allows 5 s — so the web service would be marked unhealthy precisely when the
# operator needs it to answer that the sandbox is down.
_REACH: dict = {"ok": None, "checked_at": 0.0}
_REACH_LOCK = threading.Lock()
_REACH_MAX_AGE_S = 30
# The refresh probe's own total budget: short, because nobody waits for it.
# It is an httpx budget, so a DNS miss can still outlast it — which is the
# whole reason the probe is not on the request path.
_PROBE_TIMEOUT_S = 2.0

# Paths whose removal already failed once in this process. In a healthy stack
# the sandbox's own hourly sweep clears a directory whose mode generated code
# changed (it can `chmod 0500` a directory it created under `out/`), and the
# web uid cannot chmod what the sandbox uid owns — so without this the web
# side would warn about the same unfixable directory on every sweep forever.
_REMOVE_WARNED: set = set()
# ... and a bound on it, because the job-id paths are all distinct: a stack
# where every removal fails would otherwise grow this set for the life of the
# process. Passing the bound just re-arms the warnings.
_REMOVE_WARNED_MAX = 1000
# One-shot latch for the stray-sweep refusal line (`_stray_removal_allowed`):
# the condition is a misconfigured setting, so it cannot change while the
# process runs and one line is the whole signal.
_STRAY_REFUSED: dict = {"logged": False}

# Tails of the sandbox's own stderr/traceback on the error log lines. In
# process, a failing block's traceback landed in THIS container's log; now it
# exists only in the sandbox's log, which sits on a tmpfs and is gone on
# restart. Capped because the strings are untrusted and unbounded.
_LOG_TAIL_MAX_CHARS = 2000


# ---------------------------------------------------------------------------
# configuration, resolved at call time
# ---------------------------------------------------------------------------
def _shared_dir() -> Path:
    """The jobs directory both containers mount.

    Resolved on every call, never bound at import: the setting is empty by
    default and falls back to `<DATA_ROOT>/exec_jobs`, and the test suite
    redirects `DATA_ROOT` per test — a path computed once from the environment
    would ignore every one of those redirections.
    """
    configured = str(getattr(settings, "EXECUTOR_SHARED_DIR", "") or "").strip()
    if configured:
        return Path(configured)
    return Path(settings.DATA_ROOT) / "exec_jobs"


def _service_url(path: str) -> str:
    base = str(getattr(settings, "EXECUTOR_URL", "") or "").rstrip("/")
    return f"{base}{path}"


def _connect_timeout() -> float:
    try:
        return float(settings.EXECUTOR_CONNECT_TIMEOUT)
    except Exception:
        return 5.0


def _queue_max_s() -> float:
    # Clamped to `threading.TIMEOUT_MAX`: a larger configured value makes
    # `Semaphore.acquire` raise OverflowError, which would escape a function
    # whose whole contract is that it never raises.
    try:
        return max(0.0, min(float(settings.EXECUTOR_QUEUE_MAX_S), threading.TIMEOUT_MAX))
    except Exception:
        return 600.0


def _max_concurrent() -> int:
    try:
        return max(1, int(settings.EXECUTOR_MAX_CONCURRENT))
    except Exception:
        return 1


def _client(read_timeout: float) -> httpx.Client:
    """One client per call: one job runs at a time, so a pooled connection
    would save nothing measurable next to the job itself (Articles VI/XI)."""
    timeout = httpx.Timeout(connect=_connect_timeout(), read=read_timeout,
                            write=_WRITE_TIMEOUT_S, pool=_connect_timeout())
    if _TRANSPORT is not None:
        return httpx.Client(timeout=timeout, transport=_TRANSPORT)
    return httpx.Client(timeout=timeout)


# ---------------------------------------------------------------------------
# the dispatch gate
# ---------------------------------------------------------------------------
def _gate() -> threading.BoundedSemaphore:
    size = _max_concurrent()
    with _GATE_LOCK:
        semaphore = _GATE["semaphore"]
        if semaphore is None or _GATE["size"] != size:
            semaphore = threading.BoundedSemaphore(size)
            _GATE["size"] = size
            _GATE["semaphore"] = semaphore
        return semaphore


def _reset_gate() -> None:
    """Drop the gate so the next call rebuilds it from the current setting."""
    with _GATE_LOCK:
        _GATE["size"] = None
        _GATE["semaphore"] = None


# ---------------------------------------------------------------------------
# error shapes and log helpers
# ---------------------------------------------------------------------------
def _error_shape(kind: str, text: str) -> dict:
    """The failure dict of the caller that asked — the two producers are told
    apart by which keys exist, so neither gains a key here."""
    if kind == "PLOT":
        return {"ok": False, "error": text, "trace": ""}
    return {"error": text}


def is_infrastructure_error(text) -> bool:
    """Is this failure one the planner cannot fix by rewriting the code?

    PUBLIC — `run_chat_local`'s three retry loops consult it BEFORE calling
    `brain_client.retry`. Those loops were written when generated code ran in
    process, where every `error` string really was the code's fault; the same
    channel now also carries this hop's own conditions, and asking for three
    rewrites of a service that is down costs three brain calls and three
    round-trips for nothing.

    The test is a PREFIX match against the names this hop invents
    (`ExecutorUnavailable`, `ExecutorBusy`, `ExecutorError`,
    `ExecutorCrashError`, `ExecutorResponseError`). It is a match on the TEXT,
    not a proof of origin, and it cannot be: `code_exec` renders a failing
    block as `f"{type(e).__name__}: {e}"`, so generated code that names its
    own exception class `ExecutorBusy` and raises it produces exactly
    `ExecutorBusy: ...` in the same `error` field, and this predicate calls it
    infrastructure.

    That is accepted rather than defended against, because the whole effect
    is self-inflicted: such code opts ITSELF out of the planner's retries and
    gets the canned "analysis service" sentence instead of its own answer,
    which costs it the answer and nothing else. Nothing downstream grants any
    privilege on the strength of this verdict. The price is one misleading
    operator line (`EXEC_INFRA_ERROR`, and a user sentence naming a condition
    the stack was not in), so an operator reading it should check that the
    sandbox was actually unreachable before believing it. A real proof of
    origin would mean a sentinel the producer sets on the dict, threaded
    through four callers and two error shapes.

    Two deliberate exclusions:
      * an `ExecutorCrashError` whose `reason` is `exit` or `signal` is what
        the GENERATED CODE did to the runner (`sys.exit`, a provoked
        segfault), so a rewrite may well help and it keeps its retries;
      * `MemoryError` and `ResultTooLarge` are infrastructure-shaped but
        SHOULD still be retried — cheaper code is a real fix — so they are
        outside the predicate by design, not by oversight.

    Never raises: it sits on the hot error path of four call sites and reads
    whatever the dict happened to hold.
    """
    if not isinstance(text, str) or not text.startswith(_INFRA_PREFIX):
        return False
    return not any(marker in text for marker in _CODE_CAUSED_CRASH_REASONS)


def _code_hash(code: str) -> str:
    try:
        return hashlib.sha256((code or "").encode("utf-8", errors="ignore")).hexdigest()[:10]
    except Exception:
        return ""


def _code_snippet(code: str) -> str:
    try:
        lines = (code or "").strip().splitlines()[:settings.EXEC_ERROR_SNIPPET_LINES]
        return " \\n".join(lines)
    except Exception:
        return ""


def _error_text(out: dict):
    value = out.get("error") if isinstance(out, dict) else None
    return value if isinstance(value, str) and value else None


def _success_meta(kind: str, out: dict) -> dict:
    if kind == "PLOT":
        charts = out.get("multi_charts")
        return {
            "image": bool(out.get("image") or out.get("plotly_html") or charts),
            "is_plotly": out.get("is_plotly"),
            "charts": len(charts) if isinstance(charts, list) else None,
        }
    preview = out.get("preview")
    return {
        "has_result": out.get("result") is not None,
        "preview_type": type(preview).__name__ if preview is not None else "None",
        "image": bool(out.get("image_base64")),
    }


def _tail(value) -> str:
    """The shared log-field guard, bound to this module's cap.

    `exec_transport.log_safe_text` owns the behaviour and the reasoning: the
    string was written by the sandbox and the log file is newline-delimited,
    so CR/LF are escaped before the text can forge a record. It lives in the
    shared transport because `run_chat_local` and `routes/chat` log the same
    untrusted text and must not each invent their own version. `tail=True`
    keeps the LAST characters, which is where an exception's message is.
    """
    return exec_transport.log_safe_text(value, _LOG_TAIL_MAX_CHARS, tail=True)


def _log_tails(response) -> dict:
    """`traceback=` / `stderr=` for the two error lines — LOG ONLY.

    When `exec()` ran in this process, a failing block's traceback landed in
    this container's log. It now exists only in the sandbox's log, which lives
    on a tmpfs and is wiped on restart, so the durable copy is this tail. It
    never reaches the returned dict: the callers' error channel is the text the
    planner and the user see, and it is unchanged.
    """
    if not isinstance(response, dict):
        return {}
    return {"traceback": _tail(response.get("traceback")),
            "stderr": _tail(response.get("stderr"))}


def _response_meta(response) -> dict:
    """`elapsed_ms=` / `peak_rss_mb=` for the outcome lines — LOG ONLY.

    Both come from the response body, i.e. from the untrusted side, and both
    land on a durable log line: JSON puts no limit on the type of a field nor
    on the digits of a number, so passing them through would let the sandbox
    write 40 000 characters of its choosing onto every `EXEC_OK` line. They
    are validated by the transport's ONE numeric validator — the same one the
    crash sentence's `exit_code` / `signal` use — so the rule cannot drift
    per field: a bounded int, a bounded float or None, never anything else.
    Both are measurements rather than identifiers, so a fractional value is
    kept rather than dropped. The field NAMES are unchanged; the existing log
    tooling reads them.
    """
    if not isinstance(response, dict):
        return {}
    return {
        "elapsed_ms": exec_transport.known_number(
            response.get("elapsed_ms"), exec_transport.METRIC_VALUE_MAX,
            allow_float=True),
        "peak_rss_mb": exec_transport.known_number(
            response.get("peak_rss_mb"), exec_transport.METRIC_VALUE_MAX,
            allow_float=True),
    }


# ---------------------------------------------------------------------------
# the job directory
# ---------------------------------------------------------------------------
def ensure_shared_dir() -> Path:
    """Create the jobs directory `2770` so the sandbox uid can work in it."""
    shared = _shared_dir()
    try:
        existed = shared.is_dir()
        shared.mkdir(parents=True, exist_ok=True)
        if not existed:
            try:
                os.chmod(shared, _DIR_MODE)
            except OSError as e:
                # Windows has no setgid bit; a dev run must not fail on it.
                log_with_sid("startup", "warning",
                             f"EXECUTOR_SHARED_DIR_CHMOD_FAILED dir={shared}: "
                             f"{_tail(str(e))}")
        elif not (os.stat(shared).st_mode & stat.S_IWGRP):
            # A pre-existing directory is only ever REPORTED on, never
            # repaired. The expected shape is a shared volume whose root
            # belongs to a different owner with the mode already set, and a
            # process cannot chmod what it does not own — attempting it would
            # warn on every boot of a correctly configured stack. Missing
            # group write, on the other hand, means the sandbox uid cannot
            # create `out/` and every job will come back crashed, so that is
            # worth a line.
            log_with_sid("startup", "warning",
                         f"EXECUTOR_SHARED_DIR_NOT_GROUP_WRITABLE dir={shared}")
    except OSError as e:
        log_with_sid("startup", "error",
                     f"EXECUTOR_SHARED_DIR_UNUSABLE dir={shared}: {_tail(str(e))}")
    return shared


def _warn_once_per_path(path, sid: str, message: str, **fields) -> None:
    """Log a removal failure the FIRST time it happens for this path.

    Generated code can `chmod 0500` a directory it created under `out/`, and
    only the sandbox uid — which owns it — can then clear it, by its own hourly
    sweep. This side cannot fix it, so warning on every pass would turn one
    unfixable directory into an unbounded log stream.
    """
    key = str(path)
    if key in _REMOVE_WARNED:
        return
    if len(_REMOVE_WARNED) >= _REMOVE_WARNED_MAX:
        _REMOVE_WARNED.clear()
    _REMOVE_WARNED.add(key)
    log_with_sid(sid, "warning", message, **fields)


def _remove_job_dir(job_dir, sid: str) -> None:
    if job_dir is None:
        return
    try:
        shutil.rmtree(job_dir, ignore_errors=False)
    except Exception as e:
        # The orphan sweeps on both sides take it from here.
        # The sandbox uid owns everything under `out/`, so the OSError text
        # can quote a file name IT chose — escaped like every other field it
        # writes.
        _warn_once_per_path(job_dir, sid,
                            f"EXEC_JOB_DIR_REMOVE_FAILED "
                            f"{_tail(f'{type(e).__name__}: {e}')}",
                            job_id=Path(job_dir).name)


def _name_field(path) -> str:
    """One entry name of the jobs root as a log field.

    The name of anything that is not a job directory was chosen by generated
    code, so it is untrusted text on a newline-delimited line (Article IV) and
    goes through the shared escaper like every other field the sandbox writes.
    """
    return exec_transport.log_safe_text(Path(path).name, 200)


def _own_euid():
    """This process's effective uid, or None where the platform has none.

    Windows has no uid model, and a native dev run is not the deployment the
    ownership guard below is about.
    """
    getter = getattr(os, "geteuid", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None


def _stray_removal_allowed(shared) -> bool:
    """False when the configured jobs directory ENCLOSES the data root.

    Until now this sweep only ever touched 32-hex names, and that was its
    whole protection against `EXECUTOR_SHARED_DIR` — an operator-supplied
    path — pointing at something real. Removing strays gives that up, because
    a stray is defined by its name saying nothing, so the shape in which the
    mistake would be unrecoverable is refused outright: a jobs directory that
    IS `DATA_ROOT`, or contains it, is a misconfigured install rather than a
    jobs volume. The refusal is logged once — the condition cannot change
    while the process runs, and a line every 10 minutes forever is a stream.

    This is the bound that holds on any platform; the per-entry ownership
    check in `_remove_stray_entry` is the one that holds inside the
    containers, where everything under `DATA_ROOT` belongs to the web uid and
    a stash left by generated code does not.
    """
    try:
        target = Path(shared).resolve()
        data_root = Path(settings.DATA_ROOT).resolve()
        if target == data_root or data_root.is_relative_to(target):
            if not _STRAY_REFUSED["logged"]:
                _STRAY_REFUSED["logged"] = True
                log_with_sid("exec", "warning",
                             f"EXEC_STRAY_SWEEP_REFUSED dir={target} "
                             f"reason=encloses_data_root")
            return False
        return True
    except Exception as e:
        # Fail closed: a path that will not resolve is never a directory to
        # widen a delete on.
        if not _STRAY_REFUSED["logged"]:
            _STRAY_REFUSED["logged"] = True
            log_with_sid("exec", "warning",
                         f"EXEC_STRAY_SWEEP_REFUSED reason=unresolved "
                         f"{_tail(f'{type(e).__name__}: {e}')}")
        return False


def _remove_stray_entry(child, now: float, own_uid) -> None:
    """Remove one aged entry of the jobs root that is not a job directory.

    The root is group-writable because the sandbox has to be able to clear an
    abandoned job directory in it, which also lets generated code create files
    and directories directly in the root — and until now nothing ever removed
    those, since both sweeps skipped every name that is not job-id shaped. The
    volume is named and disk-backed, so a stash outlived restarts, image
    upgrades and any number of intervening jobs, was readable by a later job
    run for a different user, was visible from the web container, and was
    captured by any backup of the volume. The root is supposed to hold nothing
    but job directories, so anything else is debris or a deliberate stash, and
    both go.

    Bounded three ways: the same age threshold a job directory gets, so
    nothing mid-creation is taken; an entry this identity OWNS is left alone,
    which is what keeps a misconfigured `EXECUTOR_SHARED_DIR` pointed at the
    customer's own state from being swept (everything under `DATA_ROOT` was
    written by this uid, while a stash written by generated code carries the
    sandbox's); and a symlink is UNLINKED, never followed — generated code
    chooses where it points and this sweep runs in the container where
    `DATA_ROOT` IS mounted. `shutil.rmtree` is the same choice for a stray
    directory: it unlinks the links it finds instead of descending them.

    Never raises (Article IV) — best effort, like the job-directory path.
    """
    try:
        info = os.lstat(child)
        if own_uid is not None and info.st_uid == own_uid:
            return
        if now - info.st_mtime <= _ORPHAN_MAX_AGE_S:
            return
        mode = info.st_mode
        if stat.S_ISLNK(mode):
            kind = "link"
            os.unlink(child)
        elif stat.S_ISDIR(mode):
            kind = "dir"
            shutil.rmtree(child)
        else:
            kind = "file" if stat.S_ISREG(mode) else "other"
            os.unlink(child)
        log_with_sid("exec", "info", f"EXEC_STRAY_ENTRY_REMOVED kind={kind}",
                     name=_name_field(child))
    except FileNotFoundError:
        # A concurrent sweep got there first; that is the success case.
        return
    except OSError as e:
        _warn_once_per_path(child, "exec",
                            f"EXEC_STRAY_ENTRY_REMOVE_FAILED: {_tail(str(e))}",
                            name=_name_field(child))


def sweep_orphans() -> None:
    """Remove job directories older than an hour, and stale strays with them.

    Every dispatch removes its own directory in a `finally`; this catches the
    ones a web worker that died mid-job left behind — and, because the root is
    group-writable, whatever generated code created directly in it
    (`_remove_stray_entry`).
    """
    shared = _shared_dir()
    try:
        entries = list(shared.iterdir())
    except OSError as e:
        # Startup created this directory, so a failure here means the volume
        # itself is gone or unreadable — the next dispatch will say so too.
        log_with_sid("exec", "warning",
                     f"EXEC_ORPHAN_SWEEP_FAILED dir={shared}: {_tail(str(e))}")
        return
    now = time.time()
    strays = _stray_removal_allowed(shared)
    own_uid = _own_euid()
    for child in entries:
        if not exec_transport.valid_job_id(child.name):
            if strays:
                _remove_stray_entry(child, now, own_uid)
            continue
        try:
            info = os.lstat(child)
            if not stat.S_ISDIR(info.st_mode):
                # A job-id-shaped name that is not a directory was not written
                # by this hop — `create_job_dir` only ever makes directories —
                # so it is a stash wearing a job id, not a job.
                if strays:
                    _remove_stray_entry(child, now, own_uid)
                continue
            if now - info.st_mtime <= _ORPHAN_MAX_AGE_S:
                continue
            shutil.rmtree(child)
            log_with_sid(child.name, "info", "EXEC_ORPHAN_REMOVED")
        except OSError as e:
            _warn_once_per_path(child, child.name,
                                f"EXEC_ORPHAN_REMOVE_FAILED: {_tail(str(e))}")


def _maybe_sweep() -> None:
    try:
        now = time.monotonic()
        if now - _LAST_SWEEP["at"] < _SWEEP_INTERVAL_S:
            return
        _LAST_SWEEP["at"] = now
        sweep_orphans()
    except Exception as e:
        log_with_sid("exec", "warning",
                     f"EXEC_ORPHAN_SWEEP_FAILED {_tail(f'{type(e).__name__}: {e}')}")


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------
def _app_version(module_name: str) -> str:
    try:
        return str(getattr(importlib.import_module(module_name), "__version__", ""))
    except Exception:
        return ""


def _compare_versions(reported) -> None:
    if not isinstance(reported, dict):
        return
    for name in _VERSION_MODULES:
        theirs = str(reported.get(name) or "")
        ours = _app_version(name)
        if theirs and ours and theirs != ours:
            # A warning, never a refusal: the two images are upgraded
            # independently and a customer may run a new web image against a
            # not-yet-replaced sandbox image.
            # `theirs` came out of the `/healthz` BODY, i.e. from the
            # untrusted side, and JSON bounds neither its length nor its
            # characters; `ours` is this image's own metadata.
            log_with_sid("startup", "warning",
                         f"EXECUTOR_VERSION_MISMATCH module={name} "
                         f"app={ours} "
                         f"executor={exec_transport.log_safe_text(theirs, 120)}")


def _note_reachable(ok: bool) -> None:
    """Record what the last conversation with the sandbox proved.

    Logs the TRANSITION, never the observation. `probe()` deliberately
    swallows its own failure (a refresh firing every 30 s while the service is
    down would flood the log), which left the operator with no line at all for
    a sandbox that went away between two questions — the one event the log was
    missing. One line per actual flip fixes that without the flood (Article
    IV). Unknown → reachable stays quiet: a healthy boot already says
    `EXECUTOR_HANDSHAKE_OK`, and "changed" would be a lie about the first
    observation.
    """
    ok = bool(ok)
    previous = _REACH.get("ok")
    _REACH["ok"] = ok
    _REACH["checked_at"] = time.time()
    if previous == ok or (previous is None and ok):
        return
    try:
        log_with_sid("exec", "info" if ok else "warning",
                     f"EXECUTOR_REACHABLE_CHANGED ok={ok}")
    except Exception:
        # Never the reason a probe, a handshake or a dispatch fails.
        pass


def probe() -> bool:
    """One `/healthz` GET whose only job is to refresh `_REACH`.

    Its own SHORT total timeout, not `EXECUTOR_CONNECT_TIMEOUT`: nobody is
    waiting for this answer, and a slow one must not keep a thread (or a
    socket) around. Never raises and never logs its own outcome — a refresh
    that fires every 30 s while the sandbox is down would flood the log. The
    one line it can produce is `_note_reachable`'s, and only when the verdict
    actually FLIPS.
    """
    ok = False
    try:
        timeout = httpx.Timeout(_PROBE_TIMEOUT_S)
        if _TRANSPORT is not None:
            client = httpx.Client(timeout=timeout, transport=_TRANSPORT)
        else:
            client = httpx.Client(timeout=timeout)
        with client:
            response = client.get(_service_url("/healthz"))
        ok = response.status_code == 200
    except Exception:
        ok = False
    finally:
        _note_reachable(ok)
    return ok


def _claim_refresh() -> bool:
    """Take the refresh slot, or report that someone else holds it.

    Stamping `checked_at` at the START is what stops a second thread: the
    state is fresh again immediately, so no further caller can claim the slot
    until the window reopens, however long the probe itself takes (a DNS miss
    outlasts its httpx budget by seconds). The probe overwrites both fields
    when it lands, so a stale verdict can outlive its timestamp by at most one
    probe — and the CALLER always gets the values as they were before the
    claim.
    """
    now = time.time()
    with _REACH_LOCK:
        if (now - (_REACH.get("checked_at") or 0.0)) <= _REACH_MAX_AGE_S:
            return False
        _REACH["checked_at"] = now
        return True


def reachable() -> tuple:
    """The CACHED `(ok, checked_at)` — never a blocking call.

    When the cache is older than `_REACH_MAX_AGE_S` this starts ONE
    short-lived daemon thread to refresh it for the NEXT caller and returns
    immediately with what it already had. `/health` is an async handler, so
    waiting here would stall the whole event loop for as long as a DNS miss
    takes (~8 s, measured) — which is exactly the moment `/health` has to keep
    answering.
    """
    ok = _REACH.get("ok")
    checked_at = _REACH.get("checked_at") or 0.0
    try:
        if _claim_refresh():
            threading.Thread(target=probe, name="executor-probe",
                             daemon=True).start()
    except Exception as e:
        # Never the reason a health check fails (Article IV): the caller still
        # gets the cached pair, just without a refresh.
        log_with_sid("exec", "warning",
                     f"EXEC_PROBE_NOT_STARTED {_tail(f'{type(e).__name__}: {e}')}")
    return ok, checked_at


def handshake() -> bool:
    """Ask `/healthz` who is answering; compare library versions."""
    try:
        with _client(read_timeout=_connect_timeout()) as client:
            response = client.get(_service_url("/healthz"))
        if response.status_code != 200:
            raise httpx.HTTPError(f"status {response.status_code}")
        body = exec_transport.loads(response.content)
    except Exception as e:
        _PENDING["handshake"] = True
        _note_reachable(False)
        log_with_sid("startup", "warning",
                     f"EXECUTOR_UNREACHABLE_AT_STARTUP "
                     f"{_tail(f'{type(e).__name__}: {e}')}",
                     url=_service_url("/healthz"))
        return False
    _PENDING["handshake"] = False
    _note_reachable(True)
    # The version string is the response's own choice too: capped and
    # escaped before it lands on the boot line.
    version = (body or {}).get("version") if isinstance(body, dict) else ""
    version = exec_transport.log_safe_text(version, 120)
    log_with_sid("startup", "info", f"EXECUTOR_HANDSHAKE_OK version={version or 'dev'}")
    _compare_versions((body or {}).get("versions") if isinstance(body, dict) else None)
    return True


def startup() -> None:
    """Lifespan hook: prepare the jobs directory, greet the service, sweep.

    Never raises — a boot must not fail on the analysis service being slow to
    come up, and the dispatcher reports it per call anyway.
    """
    try:
        ensure_shared_dir()
        handshake()
        sweep_orphans()
        concurrent = _max_concurrent()
        if concurrent > 1:
            # Raising this forfeits the isolation the sandbox exists for (see
            # docs/EXECUTOR_PROTOCOL.md §7), and it must never exceed the
            # sandbox's own limit.
            log_with_sid("startup", "warning",
                         f"EXECUTOR_CONCURRENCY_UNSAFE max_concurrent={concurrent}")
    except Exception as e:
        log_with_sid("startup", "error",
                     f"EXECUTOR_STARTUP_FAILED {_tail(f'{type(e).__name__}: {e}')}")
    return None


# ---------------------------------------------------------------------------
# the one dispatcher
# ---------------------------------------------------------------------------
def execute(kind: str, code: str, dfs: dict, sid: str | None = None,
            timeout_s=60, split_multi_axes: bool = False) -> dict:
    """Run one job in the sandbox service and return the caller's own dict.

    `kind` is "PYTHON" (the analysis shape) or "PLOT" (the chart shape); the
    returned dict is whatever that producer returns, reconstructed from the
    response — including which keys are ABSENT.
    """
    kind = "PLOT" if kind == "PLOT" else "PYTHON"
    log_sid = sid or "exec"
    budget = exec_transport.normalize_timeout(timeout_s)
    code_hash = _code_hash(code)

    # The gate is taken BEFORE anything is created, and `budget` only starts
    # counting after it: a caller that waited for the slot gets its FULL
    # budget, and the sandbox's own queue stays empty. Otherwise a wave of
    # chart renders would time out siblings that never ran, and each would
    # burn its planner retries on a queue artefact.
    gate = _gate()
    if not gate.acquire(timeout=_queue_max_s()):
        log_with_sid(log_sid, "error", "EXEC_QUEUE_EXPIRED",
                     code_hash=code_hash, kind=kind, waited_s=_queue_max_s())
        return _error_shape(kind, BUSY_TEXT)
    try:
        return _dispatch(kind, code, dfs, log_sid, sid, budget, split_multi_axes, code_hash)
    finally:
        with suppress(ValueError):
            gate.release()


def _dispatch(kind: str, code: str, dfs: dict, log_sid: str, sid, budget,
              split_multi_axes: bool, code_hash: str) -> dict:
    """One dispatch, with the slot already held."""
    if _PENDING["handshake"]:
        # The startup greeting failed; spend the one retry it bought here so a
        # service that came up late is still identified in the log. The retry
        # is spent whether or not it SUCCEEDS: `handshake()` re-arms the latch
        # when it fails, which is what lets startup arm it, so clearing it
        # only beforehand would re-arm it here for every dispatch — each one
        # paying a connect timeout inside the held slot for as long as the
        # service stays down, which is exactly when that hurts most.
        handshake()
        _PENDING["handshake"] = False

    job_dir = None
    try:
        job_id = exec_transport.new_job_id()
        shared = _shared_dir()
        # `create_job_dir` does not create PARENTS, and not every caller runs
        # through the app lifespan that calls `ensure_shared_dir` — the
        # pipeline canary and any standalone script dispatch directly. Without
        # this, such a caller fails with a path error naming an internal
        # directory instead of the plain "service not reachable" it deserves.
        # One idempotent mkdir against a job that takes ~a second.
        shared.mkdir(parents=True, exist_ok=True)
        job_dir = exec_transport.create_job_dir(shared, job_id)
        # Never pre-create `job.json`: the sandbox writes it, and a file owned
        # by the web uid could not be truncated by the sandbox uid.
        manifest = exec_transport.write_inputs(dfs, job_dir, sid)
    except Exception as e:
        # The pandas/pyarrow text quotes a CUSTOMER frame — a column name
        # with a newline in it would split this line in two.
        log_with_sid(log_sid, "error",
                     f"EXEC_INPUT_FAILED {_tail(f'{type(e).__name__}: {e}')}",
                     code_hash=code_hash)
        _remove_job_dir(job_dir, log_sid)
        return _error_shape(kind, _PREPARE_TEXT.format(exc=type(e).__name__))

    try:
        log_with_sid(log_sid, "info",
                     f"EXEC_DISPATCH job_id={job_id} kind={kind} "
                     f"code_hash={code_hash} frames={len(manifest)}")
        body = {
            "job_id": job_id,
            "kind": kind,
            "code": code,
            "dataframes": manifest,
            "timeout_s": budget,
            "options": {"split_multi_axes": bool(split_multi_axes)},
        }
        try:
            with _client(read_timeout=budget + _READ_TIMEOUT_MARGIN_S) as client:
                response = client.post(_service_url("/execute"),
                                       content=exec_transport.dumps(body),
                                       headers={"content-type": "application/json"})
        except Exception as e:
            # The returned text carries no URL: it is forwarded to the
            # planner's retry prompt. The detail stays in the local log.
            _note_reachable(False)
            log_with_sid(log_sid, "error",
                         f"EXEC_UNAVAILABLE {_tail(f'{type(e).__name__}: {e}')}",
                         job_id=job_id, url=_service_url("/execute"))
            return _error_shape(kind, UNAVAILABLE_TEXT)

        # Any HTTP status at all is a conversation: the service is up. A
        # rejection says so just as well as an answer does.
        _note_reachable(True)

        if response.status_code != 200:
            # The body is hostile by contract and this code ends up in the
            # text the planner's retry prompt reads, so only a short
            # SCREAMING_CASE token is accepted; anything else is UNKNOWN
            # rather than an arbitrary string of the sandbox's choosing.
            rejected = "UNKNOWN"
            try:
                decoded = exec_transport.loads(response.content)
                if isinstance(decoded, dict):
                    claimed = decoded.get("code")
                    if isinstance(claimed, str) and _CODE_RE.fullmatch(claimed):
                        rejected = claimed
            except Exception:
                pass
            log_with_sid(log_sid, "error",
                         f"EXEC_REJECTED status={response.status_code} code={rejected}",
                         job_id=job_id, code_hash=code_hash)
            return _error_shape(kind, _REJECTED_TEXT.format(code=rejected))

        try:
            # `exec_transport.loads`, never `response.json()`: the wire dialect
            # carries the NaN/Infinity tokens a strict JSON decoder rejects.
            decoded = exec_transport.loads(response.content)
            out = exec_transport.deserialize_result(decoded, job_dir, kind, budget,
                                                   sid=log_sid)
        except Exception as e:
            log_with_sid(log_sid, "error",
                         f"EXEC_RESPONSE_UNREADABLE "
                         f"{_tail(f'{type(e).__name__}: {e}')}",
                         job_id=job_id, code_hash=code_hash)
            return _error_shape(kind, _UNREADABLE_TEXT.format(exc=type(e).__name__))

        _log_outcome(kind, out, decoded, log_sid, job_id, code, code_hash, budget, dfs)
        return out
    finally:
        _remove_job_dir(job_dir, log_sid)
        _maybe_sweep()


def _log_outcome(kind: str, out: dict, response, log_sid: str, job_id: str,
                 code: str, code_hash: str, budget, dfs: dict) -> None:
    """The three outcome lines, with the field names the existing log tooling
    already reads (`code_hash`, the success meta) plus the join keys."""
    try:
        meta = _response_meta(response)
        error = _error_text(out)
        if error is None:
            log_with_sid(log_sid, "info", f"EXEC_OK code_hash={code_hash}",
                         job_id=job_id, **_success_meta(kind, out), **meta)
            return
        keys = list(dfs.keys()) if isinstance(dfs, dict) else []
        tails = _log_tails(response)
        if error == exec_transport.timeout_error_text(budget):
            # `reason` separates a real overrun from a slot that expired while
            # the job waited — the second never ran and must not read as
            # "your code was too slow". It arrives in the response body, so it
            # passes the transport's CLOSED vocabulary first (anything else
            # becomes `unknown`): a log line is as much a destination for an
            # injected string as a prompt is.
            reason = (exec_transport.known_reason(response.get("reason"))
                      if isinstance(response, dict) else None)
            log_with_sid(log_sid, "error", f"EXEC_TIMEOUT after {budget}s",
                         code_hash=code_hash, job_id=job_id, reason=reason,
                         code=_code_snippet(code), dfs=keys, **tails, **meta)
            return
        # The error TEXT goes to the caller verbatim — the planner has to see
        # the real exception to rewrite the code — but the LOG copy is escaped
        # like every other untrusted field: the text is written by the sandbox
        # and this file is newline-delimited, so an embedded newline would
        # forge a whole line into what operators grep. Same treatment as the
        # traceback and stderr tails; the shared helper keeps the LAST
        # characters, which is where an exception's own message is.
        log_with_sid(log_sid, "error", f"EXEC_ERROR {_tail(error)}",
                     code_hash=code_hash, job_id=job_id,
                     code=_code_snippet(code), dfs=keys, **tails, **meta)
    except Exception:
        # Logging must never be the reason an answer is lost.
        pass
