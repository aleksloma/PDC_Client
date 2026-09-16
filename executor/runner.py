"""One job, one process: the `pdc-executor` subprocess entry point.

Invoked by `executor.app` as `python -I -u executor/runner.py <job_dir>/job.json`
with `EXECUTOR_RESPONSE_FD` in its environment. It applies the resource limits
FIRST (so they bound the whole process, heavy imports included), loads the job
inputs, calls the UNCHANGED execution functions of the main app, and writes ONE
JSON response to the inherited response fd.

Why the response goes to a dedicated fd and not to stdout: generated code may
print anything (or nothing), so stdout/stderr are the job's OWN output —
captured for the answer — and must not be able to forge a response.

`-u` matters: `-I` implies `-E`, which discards `PYTHONUNBUFFERED`, and a
block-buffered stdout plus `os._exit` would lose every `print`.
"""
import os
import resource
import signal
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

# Wall clock from process START, so `elapsed_ms` includes the interpreter and
# library imports (a cold job's dominant cost) and not just the exec call.
_STARTED = time.monotonic()

# The job directory is group-writable for the web uid; anything this process
# creates in it must be too (the main app reads the result, then removes the
# directory).
os.umask(0o007)

# RLIMIT_FSIZE is enforced with SIGXFSZ; CPython already ignores that signal
# and turns the write into an ordinary OSError (EFBIG) — the explicit handler
# documents that the limit is meant to surface as a normal exec error.
try:
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
except (AttributeError, ValueError, OSError):
    pass

_DEFAULT_MEM_LIMIT_MB = 2048
# == exec_transport.RESULT_FILE_MAX_BYTES; duplicated because the limits are
# applied before that module (and pyarrow behind it) may be imported.
_MAX_OUTPUT_FILE_BYTES = 256 * 1024 * 1024
# Linux counts every THREAD of the uid against RLIMIT_NPROC (the runner's own
# exec pool, Arrow's IO threads, anything generated code starts), so 512 is a
# fork-bomb brake, not a thread budget. The container's `pids_limit` is the
# real wall.
_MAX_PROCESSES = 512
_APP_ROOT = str(Path(__file__).resolve().parent.parent)


def _limit_env_int(name: str, default: int, minimum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(value, minimum)


def _set_limit(which, soft: int) -> None:
    """Lower one rlimit, never raising: a host whose hard limit is already
    lower keeps its own value and the job still runs bounded."""
    try:
        _current_soft, hard = resource.getrlimit(which)
        if hard != resource.RLIM_INFINITY:
            soft = min(soft, hard)
            hard_value = hard
        else:
            hard_value = soft
        resource.setrlimit(which, (soft, hard_value))
    except (ValueError, OSError, AttributeError) as e:
        print(f"EXEC_RUNNER_LIMIT_SKIPPED which={which}: {e}", file=sys.stderr)


def _apply_limits() -> None:
    mem_bytes = _limit_env_int("EXECUTOR_MEM_LIMIT_MB", _DEFAULT_MEM_LIMIT_MB, 256) * 1024 * 1024
    _set_limit(resource.RLIMIT_AS, mem_bytes)
    _set_limit(resource.RLIMIT_NPROC, _MAX_PROCESSES)
    _set_limit(resource.RLIMIT_FSIZE, _MAX_OUTPUT_FILE_BYTES)
    _set_limit(resource.RLIMIT_CORE, 0)


_apply_limits()

# `-I` drops the script directory from sys.path, so the app root is added
# explicitly before the shared modules are imported.
sys.path.insert(0, _APP_ROOT)

import exec_transport  # noqa: E402  (after the limits, on purpose)
from logger_utils import log_with_sid  # noqa: E402
import code_exec  # noqa: E402
import plot_utils  # noqa: E402


def _error_shape(kind: str, text: str) -> dict:
    if kind == "PLOT":
        return {"ok": False, "error": text, "trace": ""}
    return {"error": text}


def _is_error_payload(payload: dict, kind: str) -> bool:
    if not isinstance(payload, dict):
        return True
    if kind == "PLOT":
        return payload.get("ok") is False
    return bool(payload.get("error"))


def _run_one(kind: str, code: str, dfs: dict, job_id: str, timeout_s, options: dict) -> dict:
    """Call the main app's own execution functions, unchanged."""
    if kind == "PLOT":
        return plot_utils.render_plot_safe(
            code, dfs, job_id,
            split_multi_axes=bool(options.get("split_multi_axes")))
    return code_exec.safe_execute(code, dfs, sid=job_id, timeout=timeout_s)


def _write_response(response: dict) -> None:
    """Write the one JSON response to the inherited fd, then flush the job's
    own stdout/stderr so nothing is lost to `os._exit`."""
    try:
        data = exec_transport.dumps(response).encode("utf-8")
    except Exception as e:
        data = exec_transport.dumps({
            "status": "error",
            "kind": response.get("kind"),
            "payload": {"error": f"ResultSerializationError: {type(e).__name__}: {e}"},
            "elapsed_ms": response.get("elapsed_ms"),
            "peak_rss_mb": response.get("peak_rss_mb"),
            "stdout": "", "stderr": "",
            "traceback": traceback.format_exc()[:exec_transport.ERROR_MAX_CHARS],
        }).encode("utf-8")
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        fd = int((os.environ.get("EXECUTOR_RESPONSE_FD") or "-1").strip())
    except ValueError:
        fd = -1
    if fd >= 0:
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        os.close(fd)
    sys.stdout.flush()
    sys.stderr.flush()


def _peak_rss_mb() -> float:
    try:
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1)
    except Exception:
        return 0.0


def _tail(buffer: StringIO) -> str:
    try:
        text = buffer.getvalue()
    except Exception:
        return ""
    return text[:exec_transport.STDIO_MAX_CHARS]


def main(argv) -> None:
    job_path = Path(argv[1])
    job_dir = job_path.parent
    job = exec_transport.loads(job_path.read_text(encoding="utf-8"))
    job_id = str(job.get("job_id") or "job")
    kind = "PLOT" if job.get("kind") == "PLOT" else "PYTHON"
    code = job.get("code") or ""
    # Integral timeouts are normalized back to `int` HERE, before either
    # `safe_execute` or the PLOT timeout text sees them: the value arrives as
    # JSON through a `float`-typed pydantic field, so the int 60 would
    # otherwise report "60.0 seconds limit" in a string the main app forwards
    # to the brain (`exec_transport.normalize_timeout`).
    timeout_s = exec_transport.normalize_timeout(job.get("timeout_s") or 60)
    options = job.get("options") or {}
    # Logging is initialized BEFORE stdout is redirected, so `logger_utils`'
    # StreamHandler keeps writing to the real fd 1 (visible in `docker logs`)
    # while the buffers below capture only what the generated code itself
    # prints.
    log_with_sid(job_id, "info", f"EXEC_RUNNER_START kind={kind} timeout_s={timeout_s}")

    stdout_buffer = StringIO()
    stderr_buffer = StringIO()
    status = "ok"
    trace = ""
    try:
        dfs = exec_transport.read_inputs(job.get("dataframes"), job_dir)
        exec_transport.ensure_out_dir(job_dir)
        timeout_text = exec_transport.timeout_error_text(timeout_s)
        # `render_plot_safe` has no timeout of its own and `safe_execute`'s is
        # internal: running BOTH kinds through this single-thread pool makes
        # the inner deadline fire first for either one, so the timeout text is
        # byte-identical to today's and the parent's hard kill stays a backstop.
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="exec_job")
        outer_timeout = float(timeout_s) + (1.0 if kind == "PYTHON" else 0.0)
        timed_out = False
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
            future = pool.submit(_run_one, kind, code, dfs, job_id, timeout_s, options)
            try:
                out = future.result(timeout=outer_timeout)
            except FuturesTimeoutError:
                timed_out = True
                out = _error_shape(kind, timeout_text)
        payload = exec_transport.serialize_result(out, job_dir)
        if timed_out or payload.get("error") == timeout_text:
            status = "timeout"
        elif _is_error_payload(payload, kind):
            status = "error"
    except BaseException as e:
        status = "error"
        trace = traceback.format_exc()[:exec_transport.ERROR_MAX_CHARS]
        payload = _error_shape(kind, f"{type(e).__name__}: {e}")
        log_with_sid(job_id, "error", f"EXEC_RUNNER_FAILED {type(e).__name__}: {e}")

    log_with_sid(job_id, "info", f"EXEC_RUNNER_END status={status}")
    _write_response({
        "status": status,
        "kind": kind,
        "payload": payload,
        "elapsed_ms": int((time.monotonic() - _STARTED) * 1000),
        "peak_rss_mb": _peak_rss_mb(),
        "stdout": _tail(stdout_buffer),
        "stderr": _tail(stderr_buffer),
        "traceback": trace,
    })


if __name__ == "__main__":
    try:
        main(sys.argv)
    except BaseException:
        traceback.print_exc()
    # The exec thread may still be running after a timeout, and interpreter
    # shutdown would join it — exit now that the response is on the fd.
    os._exit(0)
