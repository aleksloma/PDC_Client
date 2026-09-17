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
worker left behind.

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


def _response_meta(response) -> dict:
    if not isinstance(response, dict):
        return {}
    return {"elapsed_ms": response.get("elapsed_ms"),
            "peak_rss_mb": response.get("peak_rss_mb")}


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
                             f"EXECUTOR_SHARED_DIR_CHMOD_FAILED dir={shared}: {e}")
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
        log_with_sid("startup", "error", f"EXECUTOR_SHARED_DIR_UNUSABLE dir={shared}: {e}")
    return shared


def _remove_job_dir(job_dir, sid: str) -> None:
    if job_dir is None:
        return
    try:
        shutil.rmtree(job_dir, ignore_errors=False)
    except Exception as e:
        # The orphan sweeps on both sides take it from here.
        log_with_sid(sid, "warning",
                     f"EXEC_JOB_DIR_REMOVE_FAILED {type(e).__name__}: {e}",
                     job_id=Path(job_dir).name)


def sweep_orphans() -> None:
    """Remove job directories older than an hour, nothing else.

    Every dispatch removes its own directory in a `finally`; this catches the
    ones a web worker that died mid-job left behind.
    """
    shared = _shared_dir()
    try:
        entries = list(shared.iterdir())
    except OSError as e:
        # Startup created this directory, so a failure here means the volume
        # itself is gone or unreadable — the next dispatch will say so too.
        log_with_sid("exec", "warning", f"EXEC_ORPHAN_SWEEP_FAILED dir={shared}: {e}")
        return
    now = time.time()
    for child in entries:
        if not exec_transport.valid_job_id(child.name):
            continue
        try:
            info = os.lstat(child)
            if not stat.S_ISDIR(info.st_mode):
                continue
            if now - info.st_mtime <= _ORPHAN_MAX_AGE_S:
                continue
            shutil.rmtree(child)
            log_with_sid(child.name, "info", "EXEC_ORPHAN_REMOVED")
        except OSError as e:
            log_with_sid(child.name, "warning", f"EXEC_ORPHAN_REMOVE_FAILED: {e}")


def _maybe_sweep() -> None:
    try:
        now = time.monotonic()
        if now - _LAST_SWEEP["at"] < _SWEEP_INTERVAL_S:
            return
        _LAST_SWEEP["at"] = now
        sweep_orphans()
    except Exception as e:
        log_with_sid("exec", "warning", f"EXEC_ORPHAN_SWEEP_FAILED {type(e).__name__}: {e}")


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
            log_with_sid("startup", "warning",
                         f"EXECUTOR_VERSION_MISMATCH module={name} "
                         f"app={ours} executor={theirs}")


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
        log_with_sid("startup", "warning",
                     f"EXECUTOR_UNREACHABLE_AT_STARTUP {type(e).__name__}: {e}",
                     url=_service_url("/healthz"))
        return False
    _PENDING["handshake"] = False
    version = (body or {}).get("version") if isinstance(body, dict) else ""
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
        log_with_sid("startup", "error", f"EXECUTOR_STARTUP_FAILED {type(e).__name__}: {e}")
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
        log_with_sid(log_sid, "error",
                     f"EXEC_INPUT_FAILED {type(e).__name__}: {e}", code_hash=code_hash)
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
            log_with_sid(log_sid, "error",
                         f"EXEC_UNAVAILABLE {type(e).__name__}: {e}",
                         job_id=job_id, url=_service_url("/execute"))
            return _error_shape(kind, UNAVAILABLE_TEXT)

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
            out = exec_transport.deserialize_result(decoded, job_dir, kind, budget)
        except Exception as e:
            log_with_sid(log_sid, "error",
                         f"EXEC_RESPONSE_UNREADABLE {type(e).__name__}: {e}",
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
        if error == exec_transport.timeout_error_text(budget):
            # `reason` separates a real overrun from a slot that expired while
            # the job waited — the second never ran and must not read as
            # "your code was too slow".
            reason = response.get("reason") if isinstance(response, dict) else None
            log_with_sid(log_sid, "error", f"EXEC_TIMEOUT after {budget}s",
                         code_hash=code_hash, job_id=job_id, reason=reason,
                         code=_code_snippet(code), dfs=keys, **meta)
            return
        log_with_sid(log_sid, "error", f"EXEC_ERROR {error}",
                     code_hash=code_hash, job_id=job_id,
                     code=_code_snippet(code), dfs=keys, **meta)
    except Exception:
        # Logging must never be the reason an answer is lost.
        pass
