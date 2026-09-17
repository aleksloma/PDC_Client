"""The `pdc-executor` HTTP surface: `GET /healthz` and `POST /execute`.

One job at a time (`EXECUTOR_MAX_CONCURRENT`, default 1), each in its own
`python -I` subprocess in its own session, answered over a dedicated pipe and
killed by process group when the deadline passes. The container holds NO
secrets and NO database access: the lifespan REFUSES to start when a brain
token, session key, encryption key, admin password or upload bucket is present
in its environment, and the runner is handed a hand-built env allowlist, so
even a container mistakenly handed the web service's env file leaks nothing
into generated code.

There is no authentication on `/execute` by design: a shared secret would put
a secret into the one container whose defining property is that it has none.
The network is the control — the service is on the internal compose network
with no published ports, and `docs/EXECUTOR_PROTOCOL.md` records the trust
model.

Configuration is environment-only (`EXECUTOR_*`), read in the lifespan.
Nothing here reads a `.env` file.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field
from starlette.responses import Response

_APP_ROOT = Path(__file__).resolve().parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

import exec_transport  # noqa: E402
from logger_utils import log_with_sid  # noqa: E402

_RUNNER = _APP_ROOT / "executor" / "runner.py"

# Build identity, mirrored from the image's build args (the same stamp
# mechanism as the main image) and reported by /healthz so the main app can
# check it is talking to a matching executor.
BUILD_COMMIT = os.environ.get("BUILD_COMMIT", "")
BUILD_TIME = os.environ.get("BUILD_TIME", "")

# Startup refusal. Any of these present and non-empty means the container was
# handed the web service's configuration.
_REFUSED_SECRET_PREFIX = "BRAIN_"
_REFUSED_SECRET_NAMES = (
    "SECRET_KEY",
    "CLIENT_ENCRYPTION_KEY",
    "CLIENT_ENCRYPTION_KEY_OLD",
    "LOCAL_ADMIN_PASSWORD",
    "GCS_UPLOAD_BUCKET",
)

_CODE_MAX_CHARS = 1024 * 1024
_MAX_INPUT_FRAMES = 64
_RESPONSE_MAX_BYTES = 64 * 1024 * 1024
_READER_JOIN_S = 5.0
_ORPHAN_MAX_AGE_S = 3600
_ORPHAN_SWEEP_INTERVAL_S = 600
_VERSION_MODULES = ("matplotlib", "numpy", "pandas", "plotly", "pyarrow")

_STATE: dict = {}
_VERSIONS: dict = {}
# Jobs currently in flight. With the default EXECUTOR_MAX_CONCURRENT=1 this is
# only ever 0 or 1; it is tracked so the same-uid process sweep can be SKIPPED
# when an operator raised the limit — the sweep would otherwise kill a sibling
# job's runner.
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT = {"count": 0}
# One-shot latch for the EXECUTOR_NOT_READY log line (no lock needed: the
# route that sets it runs on the event loop).
_NOT_READY_LOGGED = {"logged": False}


# ---------------------------------------------------------------------------
# configuration (environment only)
# ---------------------------------------------------------------------------
class Config:
    __slots__ = ("shared_dir", "mem_limit_mb", "max_concurrent", "max_timeout_s", "grace_s")

    def __init__(self, shared_dir: Path, mem_limit_mb: int, max_concurrent: int,
                 max_timeout_s: float, grace_s: float):
        self.shared_dir = shared_dir
        self.mem_limit_mb = mem_limit_mb
        self.max_concurrent = max_concurrent
        self.max_timeout_s = max_timeout_s
        self.grace_s = grace_s


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(value, minimum)


def _env_float(name: str, default: float, minimum: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return max(value, minimum)


def _load_config() -> Config:
    return Config(
        shared_dir=Path(os.environ.get("EXECUTOR_SHARED_DIR", "/jobs")),
        mem_limit_mb=_env_int("EXECUTOR_MEM_LIMIT_MB", 2048, 256),
        max_concurrent=_env_int("EXECUTOR_MAX_CONCURRENT", 1, 1),
        max_timeout_s=_env_float("EXECUTOR_MAX_TIMEOUT_S", 600.0, 1.0),
        grace_s=_env_float("EXECUTOR_GRACE_S", 15.0, 1.0),
    )


def _refuse_on_secret_env() -> None:
    """Refuse to start when a secret is in the environment.

    Runs after `logger_utils` (and through it `settings`) is imported, so a
    value a mounted `.env` put into the process is caught too.
    """
    for name in sorted(os.environ):
        if not (os.environ.get(name) or "").strip():
            continue
        if name.startswith(_REFUSED_SECRET_PREFIX) or name in _REFUSED_SECRET_NAMES:
            log_with_sid("executor", "error", f"EXECUTOR_REFUSED_SECRET_ENV name={name}")
            raise SystemExit(1)


def _refuse_on_unwritable_shared_dir(shared_dir: Path) -> None:
    """Refuse to start when the shared job directory cannot be written.

    A write-and-unlink probe, not a mode check: the directory arrives from a
    volume mount whose ownership this process does not control, and only an
    actual write proves the job output can be produced. Without it every job
    would come back as a crash with the real cause buried in a runner
    traceback — the same reason the secret self-check refuses loudly.
    """
    probe = Path(shared_dir) / f".write_probe.{os.getpid()}"
    try:
        with open(probe, "wb") as handle:
            handle.write(b"ok")
        os.unlink(probe)
    except OSError as e:
        log_with_sid("executor", "error",
                     f"EXECUTOR_SHARED_DIR_NOT_WRITABLE dir={shared_dir} {e}")
        with suppress(OSError):
            os.unlink(probe)
        raise SystemExit(1)


def _library_versions() -> dict:
    if not _VERSIONS:
        for name in _VERSION_MODULES:
            try:
                _VERSIONS[name] = str(getattr(importlib.import_module(name), "__version__", ""))
            except Exception as e:
                log_with_sid("executor", "warning", f"EXECUTOR_VERSION_UNKNOWN module={name}: {e}")
                _VERSIONS[name] = ""
    return dict(_VERSIONS)


# ---------------------------------------------------------------------------
# lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Everything this process and its children create in the shared job volume
    # must stay group-writable for the web uid.
    os.umask(0o007)
    _refuse_on_secret_env()
    config = _load_config()
    if not config.shared_dir.is_dir():
        log_with_sid("executor", "error",
                     f"EXECUTOR_SHARED_DIR_INVALID dir={config.shared_dir}")
        raise SystemExit(1)
    _refuse_on_unwritable_shared_dir(config.shared_dir)
    if config.max_concurrent > 1:
        # More than one job at a time gives up the guarantee the stray-process
        # sweep rests on (one runner per uid), so it must never be silent.
        log_with_sid("executor", "warning",
                     f"EXECUTOR_CONCURRENCY_UNSAFE max_concurrent={config.max_concurrent}")
    _STATE["config"] = config
    _STATE["semaphore"] = asyncio.Semaphore(config.max_concurrent)
    _STATE["pool"] = ThreadPoolExecutor(max_workers=config.max_concurrent + 1,
                                        thread_name_prefix="exec_job")
    _STATE["stop"] = threading.Event()
    _sweep_orphans(config.shared_dir)
    sweeper = threading.Thread(target=_orphan_loop, args=(config.shared_dir, _STATE["stop"]),
                               daemon=True, name="orphan_sweep")
    sweeper.start()
    log_with_sid("executor", "info",
                 f"EXECUTOR_START shared_dir={config.shared_dir} "
                 f"mem_limit_mb={config.mem_limit_mb} max_concurrent={config.max_concurrent} "
                 f"max_timeout_s={config.max_timeout_s} grace_s={config.grace_s}")
    try:
        yield
    finally:
        _STATE["stop"].set()
        with suppress(Exception):
            _STATE["pool"].shutdown(wait=False, cancel_futures=True)
        log_with_sid("executor", "info", "EXECUTOR_STOP")


app = FastAPI(title="pdc-executor", lifespan=lifespan)


def _json(body: dict, status_code: int = 200) -> Response:
    """Every response is rendered with `exec_transport.dumps`.

    Never FastAPI's JSON response class: it renders with `allow_nan=False` and
    raises on a NaN anywhere in a result preview or chart data.
    """
    return Response(content=exec_transport.dumps(body), status_code=status_code,
                    media_type="application/json")


# ---------------------------------------------------------------------------
# orphan sweep
# ---------------------------------------------------------------------------
def _sweep_orphans(shared_dir: Path) -> None:
    """Remove job-id-shaped directories older than an hour, nothing else.

    The main app removes its own job directory in a `finally`; this only
    catches the ones a crashed web worker left behind. Possible at all because
    `create_job_dir` gives the shared group write access.
    """
    try:
        entries = list(Path(shared_dir).iterdir())
    except OSError as e:
        log_with_sid("executor", "warning", f"EXEC_ORPHAN_SWEEP_FAILED: {e}")
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
            _rmtree_repairing_modes(child)
            log_with_sid(child.name, "info", "EXEC_ORPHAN_REMOVED")
        except OSError as e:
            log_with_sid(child.name, "warning", f"EXEC_ORPHAN_REMOVE_FAILED: {e}")


def _repair_mode_and_retry(function, path, excinfo) -> None:
    """`rmtree` error hook: make the path traversable, then try once more.

    Generated code can `chmod 0500` a directory it created under `out/`, which
    makes the removal fail for the identity that owns it — the job would then
    be leaked forever. Only modes on what this uid owns can be repaired, which
    is exactly that subtree.
    """
    try:
        os.chmod(path, 0o770)
        function(path)
    except OSError as e:
        log_with_sid(Path(path).name, "warning", f"EXEC_ORPHAN_MODE_REPAIR_FAILED: {e}")


def _rmtree_repairing_modes(path: Path) -> None:
    # Python 3.12 renamed rmtree's error callback `onerror` -> `onexc`; keep
    # working if either is the one this interpreter offers.
    try:
        shutil.rmtree(path, onexc=_repair_mode_and_retry)
    except TypeError:
        shutil.rmtree(path, onerror=_repair_mode_and_retry)


def _orphan_loop(shared_dir: Path, stop: threading.Event) -> None:
    while not stop.wait(_ORPHAN_SWEEP_INTERVAL_S):
        _sweep_orphans(shared_dir)


# ---------------------------------------------------------------------------
# same-uid process sweep
# ---------------------------------------------------------------------------
def _sweep_same_uid_processes() -> None:
    """SIGKILL every process of our own euid except this process and its parent.

    A `setsid()` escapee survives the job's process-group kill; this is what
    catches it and what makes "no other process exists during an exec" true.
    The PARENT exclusion is load-bearing: the app is not always pid 1 (a
    supervisor, `--reload`, a future multi-worker uvicorn), and the process
    above it shares this uid — killing it would take the service down
    mid-request.
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or not os.path.isdir("/proc"):
        return
    me = geteuid()
    spare = {os.getpid(), os.getppid()}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in spare:
            continue
        try:
            status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        uid = None
        for line in status.splitlines():
            if line.startswith("Uid:"):
                with suppress(IndexError, ValueError):
                    uid = int(line.split()[1])
                break
        if uid != me:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue
        log_with_sid("executor", "warning", f"EXEC_STRAY_KILLED pid={pid}")


# ---------------------------------------------------------------------------
# request model
# ---------------------------------------------------------------------------
class ExecuteOptions(BaseModel):
    split_multi_axes: bool = False


class InputFrame(BaseModel):
    name: str
    path: str
    format: str = "parquet"


class ExecuteRequest(BaseModel):
    job_id: str
    kind: str
    code: str
    dataframes: List[InputFrame] = Field(default_factory=list)
    timeout_s: float
    options: ExecuteOptions = Field(default_factory=ExecuteOptions)


# ---------------------------------------------------------------------------
# the job subprocess
# ---------------------------------------------------------------------------
def _runner_env(config: Config, response_fd: int) -> dict:
    """The runner's environment, built from scratch as an ALLOWLIST.

    `ARROW_DEFAULT_MEMORY_POOL=system` matters: jemalloc/mimalloc reserve large
    virtual ranges that `RLIMIT_AS` counts against the job.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": os.environ.get("MPLCONFIGDIR", "/tmp/mpl"),
        "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME", "/tmp/cache"),
        "DATA_ROOT": os.environ.get("DATA_ROOT", "/tmp/executor"),
        "LOG_MAX_BYTES": os.environ.get("LOG_MAX_BYTES", "5242880"),
        "LOG_BACKUP_COUNT": os.environ.get("LOG_BACKUP_COUNT", "1"),
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "ARROW_IO_THREADS": "1",
        "ARROW_DEFAULT_MEMORY_POOL": "system",
        "EXECUTOR_MEM_LIMIT_MB": str(config.mem_limit_mb),
        "EXECUTOR_RESPONSE_FD": str(response_fd),
    }
    for optional in ("LANG", "LC_ALL"):
        value = os.environ.get(optional)
        if value:
            env[optional] = value
    return env


def _enter_job() -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT["count"] += 1


def _leave_job() -> None:
    """Decrement the in-flight count."""
    with _INFLIGHT_LOCK:
        _INFLIGHT["count"] = max(0, _INFLIGHT["count"] - 1)


def _alone_in_flight() -> bool:
    """True when THIS job is the only one in flight (its own entry counted)."""
    with _INFLIGHT_LOCK:
        return _INFLIGHT["count"] <= 1


def _drain(stream, sink: list, cap: int) -> None:
    """Read a pipe to EOF, keeping the first `cap` bytes.

    Everything is read (never just the first `cap`) so the child can never
    block on a full pipe.
    """
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            if len(sink[0]) < cap:
                sink[0] += chunk[:cap - len(sink[0])]
    except Exception:
        # EXPECTED, not exceptional — deliberately NOT logged. This thread
        # races the unconditional `killpg`, so a read on a pipe whose far end
        # is already gone is the NORMAL way this loop ends; the partial buffer
        # collected so far is the fallback the caller uses. A log line here
        # would fire on every timed-out job.
        pass
    finally:
        with suppress(Exception):
            stream.close()


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")[:exec_transport.STDIO_MAX_CHARS]


def _run_job(config: Config, request: ExecuteRequest, job_dir: Path, remaining: float) -> dict:
    """Run one job; the stray sweep lives INSIDE `_execute_job` (see there).

    This wrapper only owns the in-flight accounting the sweep consults.
    """
    _enter_job()
    try:
        return _execute_job(config, request, job_dir, remaining)
    finally:
        _leave_job()


def _execute_job(config: Config, request: ExecuteRequest, job_dir: Path,
                 remaining: float) -> dict:
    """Spawn the runner, wait for it, and turn its exit into ONE response."""
    job_id = request.job_id
    body = {
        "job_id": job_id,
        "kind": request.kind,
        "code": request.code,
        "dataframes": [{"name": f.name, "path": f.path, "format": f.format}
                       for f in request.dataframes],
        "timeout_s": request.timeout_s,
        "options": {"split_multi_axes": request.options.split_multi_axes},
    }
    job_json = job_dir / "job.json"
    job_json.write_text(exec_transport.dumps(body), encoding="utf-8")
    with suppress(OSError):
        os.chmod(job_json, 0o660)

    started = time.monotonic()
    # The same hash the web side logs, so one answer can be followed across
    # the two containers' logs after the main app removed `job.json`.
    code_hash = hashlib.sha256(request.code.encode("utf-8", errors="ignore")).hexdigest()[:10]
    log_with_sid(job_id, "info",
                 f"EXEC_JOB_START kind={request.kind} code_hash={code_hash} "
                 f"timeout_s={exec_transport.normalize_timeout(request.timeout_s)} "
                 f"frames={len(request.dataframes)}")
    stdout_sink = [b""]
    stderr_sink = [b""]
    response_sink = [b""]
    read_fd, write_fd = os.pipe()
    try:
        try:
            process = subprocess.Popen(
                [sys.executable, "-I", "-u", str(_RUNNER), str(job_json)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(write_fd,),
                env=_runner_env(config, write_fd),
                cwd=str(_APP_ROOT),
                start_new_session=True,
            )
        finally:
            os.close(write_fd)
    except Exception as e:
        os.close(read_fd)
        log_with_sid(job_id, "error", f"EXEC_JOB_SPAWN_FAILED {type(e).__name__}: {e}")
        return {"status": "crashed", "kind": request.kind, "payload": None,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "peak_rss_mb": None, "stdout": "", "stderr": "",
                "traceback": f"{type(e).__name__}: {e}", "exit_code": None,
                "signal": None, "reason": "spawn_failed"}

    readers = [
        threading.Thread(target=_drain, args=(process.stdout, stdout_sink,
                                              exec_transport.STDIO_MAX_CHARS), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr, stderr_sink,
                                              exec_transport.STDIO_MAX_CHARS), daemon=True),
        threading.Thread(target=_drain, args=(os.fdopen(read_fd, "rb"), response_sink,
                                              _RESPONSE_MAX_BYTES), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        process.wait(timeout=remaining + config.grace_s)
    except subprocess.TimeoutExpired:
        timed_out = True
    # ALWAYS kill the group (pgid == pid under start_new_session): a leaked
    # grandchild holding the pipes would otherwise keep the readers from ever
    # seeing EOF.
    with suppress(ProcessLookupError, OSError):
        os.killpg(process.pid, signal.SIGKILL)
    if timed_out:
        with suppress(Exception):
            process.wait(timeout=_READER_JOIN_S)
    # ORDER IS LOAD-BEARING — do not move the sweep after the join/parse.
    # A grandchild that called `os.setsid()` left our process group, so the
    # `killpg` above cannot reach it, and it inherited the write ends of the
    # stdout/stderr/RESPONSE pipes. The response reader therefore never sees
    # EOF: its join below burns the full `_READER_JOIN_S`, the runner's
    # already-written response is LOST, and a perfectly successful job is
    # reported as `crashed`. Killing the escapee FIRST closes those fds, so
    # the join returns at once with the real response.
    if _alone_in_flight():
        _sweep_same_uid_processes()
    else:
        # EXECUTOR_MAX_CONCURRENT > 1 (non-default) makes the sweep unsafe —
        # it would kill a sibling job's runner — so it is skipped. Cost of
        # that setting, on top of the shared-uid isolation it already gives
        # up: a `setsid()` escapee can hold the pipes open and cost THIS
        # job its response (reported `crashed` after the reader-join grace).
        log_with_sid(job_id, "warning", "EXEC_STRAY_SWEEP_SKIPPED concurrent_job")
    for reader in readers:
        reader.join(timeout=_READER_JOIN_S)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    exit_code = process.returncode
    signal_no = -exit_code if isinstance(exit_code, int) and exit_code < 0 else None
    stdout_text = _decode(stdout_sink[0])
    stderr_text = _decode(stderr_sink[0])

    if timed_out:
        response = {"status": "timeout", "kind": request.kind, "payload": None,
                    "elapsed_ms": elapsed_ms, "peak_rss_mb": None,
                    "stdout": stdout_text, "stderr": stderr_text,
                    "traceback": stderr_text, "exit_code": exit_code,
                    "signal": signal_no, "reason": "hard_timeout"}
    else:
        response = _parse_runner_response(response_sink[0])
        if response is not None:
            response["kind"] = request.kind
            response.setdefault("stdout", stdout_text)
            response.setdefault("stderr", stderr_text)
            response["exit_code"] = exit_code
            response.setdefault("signal", signal_no)
            response.setdefault("reason", None)
        elif exit_code == -signal.SIGKILL:
            # We only ever kill AFTER `wait` returned, so a SIGKILL exit is
            # somebody else's: the cgroup OOM killer (or the code itself).
            response = {"status": "killed", "kind": request.kind, "payload": None,
                        "elapsed_ms": elapsed_ms, "peak_rss_mb": None,
                        "stdout": stdout_text, "stderr": stderr_text,
                        "traceback": stderr_text, "exit_code": exit_code,
                        "signal": signal_no, "reason": "sigkill"}
        else:
            reason = "response_invalid" if response_sink[0] else (
                "signal" if signal_no else "exit")
            response = {"status": "crashed", "kind": request.kind, "payload": None,
                        "elapsed_ms": elapsed_ms, "peak_rss_mb": None,
                        "stdout": stdout_text, "stderr": stderr_text,
                        "traceback": stderr_text, "exit_code": exit_code,
                        "signal": signal_no, "reason": reason}

    level = "info" if response["status"] in ("ok", "error") else "warning"
    log_with_sid(job_id, level,
                 f"EXEC_JOB_END status={response['status']} code_hash={code_hash} "
                 f"elapsed_ms={elapsed_ms} "
                 f"exit_code={exit_code} reason={response.get('reason')}")
    if response["status"] in ("killed", "crashed"):
        log_with_sid(job_id, "warning", f"EXEC_JOB_STDERR {stderr_text[-2000:]}")
    return response


def _parse_runner_response(raw: bytes) -> Optional[dict]:
    """The runner's response, or None when there is nothing usable.

    Generated code inherits the response fd and can write junk to it, so the
    shape is validated before it is trusted.
    """
    if not raw:
        return None
    try:
        candidate = exec_transport.loads(raw)
    except Exception:
        return None
    if not isinstance(candidate, dict):
        return None
    if candidate.get("status") not in ("ok", "error", "timeout"):
        return None
    if not isinstance(candidate.get("payload"), dict):
        return None
    return candidate


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@app.get("/healthz")
async def healthz() -> Response:
    """Liveness plus the versions the main app's handshake needs.

    Answers while a job runs: the job wait happens on a worker thread, never
    on the event loop.
    """
    return _json({"ok": True, "version": BUILD_COMMIT, "build_time": BUILD_TIME,
                  "versions": _library_versions()})


def _bad_request(code: str, message: str) -> Response:
    return _json({"code": code, "message": message}, status_code=400)


def _not_ready() -> Response:
    """503 for an `/execute` that arrived without the lifespan having run.

    Serving such a request would mean serving WITHOUT the secret-env
    self-check and the shared-dir check having run, so it is refused. Logged
    exactly ONCE per process (a module dict latch, the same idiom as the
    in-flight counter): a misconfigured supervisor would otherwise write one
    line per request forever.
    """
    if not _NOT_READY_LOGGED["logged"]:
        _NOT_READY_LOGGED["logged"] = True
        log_with_sid("executor", "error",
                     "EXECUTOR_NOT_READY refusing /execute: startup state absent")
    return _json({"code": "EXECUTOR_NOT_READY",
                  "message": "the executor is not ready: startup did not complete"},
                 status_code=503)


def _validate(request: ExecuteRequest, config: Config) -> Optional[Response]:
    if not exec_transport.valid_job_id(request.job_id):
        return _bad_request("BAD_JOB_ID", "job_id is not a 32-character hex id")
    if request.kind not in exec_transport.KINDS:
        return _bad_request("BAD_KIND", "kind must be PYTHON or PLOT")
    if len(request.code) > _CODE_MAX_CHARS:
        return _bad_request("CODE_TOO_LARGE", "code exceeds 1 MiB")
    if len(request.dataframes) > _MAX_INPUT_FRAMES:
        return _bad_request("TOO_MANY_FRAMES", f"more than {_MAX_INPUT_FRAMES} input frames")
    if not (0 < request.timeout_s <= config.max_timeout_s):
        return _bad_request("BAD_TIMEOUT", f"timeout_s must be in (0, {config.max_timeout_s}]")
    try:
        exec_transport.validate_manifest(
            [{"name": f.name, "path": f.path, "format": f.format} for f in request.dataframes])
    except ValueError as e:
        return _bad_request("BAD_INPUT_PATH", str(e))
    return None


@app.post("/execute")
async def execute(request: ExecuteRequest) -> Response:
    # The lifespan is the ONLY builder of `_STATE`: no lazy fallback here, or
    # a process whose startup never ran would execute code without the
    # secret-env refusal and the shared-directory check.
    config = _STATE.get("config")
    if config is None:
        return _not_ready()
    invalid = _validate(request, config)
    if invalid is not None:
        return invalid

    job_dir = config.shared_dir / request.job_id
    try:
        info = os.lstat(job_dir)
    except OSError:
        return _bad_request("JOB_DIR_INVALID", "the job directory does not exist")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return _bad_request("JOB_DIR_INVALID", "the job directory is not a directory")

    # The queue wait is INSIDE the caller's budget: a job whose deadline
    # expires while it waits for the single slot is answered `timeout` and
    # never runs.
    deadline = time.monotonic() + request.timeout_s
    semaphore = _STATE["semaphore"]
    try:
        await asyncio.wait_for(semaphore.acquire(),
                               timeout=max(0.01, deadline - time.monotonic()))
    except asyncio.TimeoutError:
        log_with_sid(request.job_id, "warning", "EXEC_JOB_QUEUE_TIMEOUT")
        return _json({"status": "timeout", "kind": request.kind, "payload": None,
                      "elapsed_ms": int(request.timeout_s * 1000), "peak_rss_mb": None,
                      "stdout": "", "stderr": "", "traceback": "",
                      "exit_code": None, "signal": None, "reason": "queued"})
    try:
        pool = _STATE["pool"]
        remaining = max(0.5, deadline - time.monotonic())
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(pool, _run_job, config, request, job_dir, remaining)
    except Exception as e:
        log_with_sid(request.job_id, "error", f"EXEC_JOB_FAILED {type(e).__name__}: {e}")
        response = {"status": "crashed", "kind": request.kind, "payload": None,
                    "elapsed_ms": None, "peak_rss_mb": None, "stdout": "", "stderr": "",
                    "traceback": f"{type(e).__name__}: {e}", "exit_code": None,
                    "signal": None, "reason": "executor_error"}
    finally:
        semaphore.release()
    return _json(response)
