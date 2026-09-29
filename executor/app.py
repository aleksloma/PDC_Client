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
import tempfile
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
# An abandoned job directory holds another question's input frames and is
# readable through the shared group, so it is removed after five minutes (a
# job still in flight on this side is skipped by name, see _ACTIVE_JOBS).
_ORPHAN_MAX_AGE_S = 300

# A modification time in the FUTURE is treated as aged (generated code can
# forward-date an entry it owns so it never looks old), but only beyond this
# tolerance: a directory created a moment ago can carry an mtime slightly
# ahead of this process's clock, and deleting a live job dir would break
# the job in flight.
_FUTURE_MTIME_TOLERANCE_S = 300
_ORPHAN_SWEEP_INTERVAL_S = 60
# The same-uid process sweep after every job repeats until a pass over /proc
# finds nothing, at most this many passes this far apart.
_SWEEP_MAX_PASSES = 10
# 10 x 0.25 s: long enough for a SIGKILLed process to finish exiting (a large
# address space takes a moment to tear down), so only a real survivor latches
# the service unhealthy.
_SWEEP_PAUSE_S = 0.25
# Each job gets a private scratch directory (TMPDIR, HOME, XDG_CACHE_HOME,
# MPLCONFIGDIR), created fresh and removed after the job. matplotlib's font
# cache is built ONCE into this template at startup and copied per job.
_SCRATCH_PREFIX = "pdcjob-"
_MPL_TEMPLATE_NAME = "pdc-mpl-template"
_VERSION_MODULES = ("matplotlib", "numpy", "pandas", "plotly", "pyarrow")

_STATE: dict = {}
_VERSIONS: dict = {}
# Jobs currently in flight. With the default EXECUTOR_MAX_CONCURRENT=1 this is
# only ever 0 or 1; it is tracked so the same-uid process sweep can be SKIPPED
# when an operator raised the limit — the sweep would otherwise kill a sibling
# job's runner.
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT = {"count": 0}
# Job ids this service has accepted and not yet answered. The orphan sweep
# skips them by name: its age threshold is shorter than the longest job
# (EXECUTOR_MAX_TIMEOUT_S), and a running job does not refresh its
# directory's modification time.
_ACTIVE_JOBS: set = set()
# One-shot latch for the EXECUTOR_NOT_READY log line (no lock needed: the
# route that sets it runs on the event loop).
_NOT_READY_LOGGED = {"logged": False}
# Set when the process sweep could not clear the job uid: the service then
# refuses every job (and /healthz says so) until it is restarted.
_UNHEALTHY: dict = {"reason": None}
# Pids THIS service's sweep killed. A runner that dies of one of them is
# reported `crashed` (reason `signal`), never `killed` — `killed` means a
# SIGKILL the service did not send (the OOM killer), which tells the planner
# to use less memory. Only reachable with EXECUTOR_MAX_CONCURRENT > 1.
_SWEPT_PIDS: set = set()
# Killed pids whose zombie this service may have to reap (see _reap_swept).
_REAP_PENDING: set = set()
# Pids of live job runners (their exit status belongs to their Popen).
_RUNNER_PIDS: set = set()
# The font-cache template's files as warmed: {name: (size, sha256)}. Only a
# file that still matches is copied into a job's scratch.
_MPL_TEMPLATE_FILES: dict = {}
_MPL_TAMPER_LOGGED = {"logged": False}


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
            log_with_sid("executor", "error",
                         f"EXECUTOR_REFUSED_SECRET_ENV "
                         f"name={exec_transport.log_safe_text(name, 200)}")
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
                     f"EXECUTOR_SHARED_DIR_NOT_WRITABLE "
                     f"dir={exec_transport.log_safe_text(str(shared_dir), 300)} "
                     f"{exec_transport.log_safe_text(str(e))}")
        with suppress(OSError):
            os.unlink(probe)
        raise SystemExit(1)


def _library_versions() -> dict:
    if not _VERSIONS:
        for name in _VERSION_MODULES:
            try:
                _VERSIONS[name] = str(getattr(importlib.import_module(name), "__version__", ""))
            except Exception as e:
                log_with_sid("executor", "warning",
                             f"EXECUTOR_VERSION_UNKNOWN "
                             f"module={exec_transport.log_safe_text(name, 80)}: "
                             f"{exec_transport.log_safe_text(str(e))}")
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
                     f"EXECUTOR_SHARED_DIR_INVALID "
                     f"dir={exec_transport.log_safe_text(str(config.shared_dir), 300)}")
        raise SystemExit(1)
    _refuse_on_unwritable_shared_dir(config.shared_dir)
    if config.max_concurrent > 1:
        # More than one job at a time gives up the guarantee the stray-process
        # sweep rests on (one runner per uid), so it must never be silent.
        log_with_sid("executor", "warning",
                     f"EXECUTOR_CONCURRENCY_UNSAFE max_concurrent={int(config.max_concurrent)}")
    _STATE["config"] = config
    _STATE["semaphore"] = asyncio.Semaphore(config.max_concurrent)
    _STATE["pool"] = ThreadPoolExecutor(max_workers=config.max_concurrent + 1,
                                        thread_name_prefix="exec_job")
    _STATE["stop"] = threading.Event()
    # Lock every job directory already present (the ones this uid may chmod)
    # before the first job runs, then sweep the aged ones. A FRESH one is left
    # for the next sweeps: the web service may be about to post that job.
    _lock_preexisting_job_dirs(config.shared_dir)
    _sweep_orphans(config.shared_dir)
    _warm_mpl_template()
    sweeper = threading.Thread(target=_orphan_loop, args=(config.shared_dir, _STATE["stop"]),
                               daemon=True, name="orphan_sweep")
    sweeper.start()
    log_with_sid("executor", "info",
                 f"EXECUTOR_START "
                 f"shared_dir={exec_transport.log_safe_text(str(config.shared_dir), 300)} "
                 f"mem_limit_mb={int(config.mem_limit_mb)} "
                 f"max_concurrent={int(config.max_concurrent)} "
                 f"max_timeout_s={float(config.max_timeout_s)} "
                 f"grace_s={float(config.grace_s)}")
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
def _lock_preexisting_job_dirs(shared_dir: Path) -> int:
    """chmod 000 every job directory present when the service starts.

    The first jobs after a restart must not read what an earlier job left.
    Only the OWNER may chmod: job directories are created by the web uid, so
    from this uid the call succeeds only on entries it owns (a directory
    generated code renamed into a job-id shape) and fails with EPERM on the
    rest, which stay readable to the shared group until the sweeps remove
    them (at most _ORPHAN_MAX_AGE_S). Counts only are logged. Never raises.
    """
    locked = not_permitted = 0
    try:
        entries = list(Path(shared_dir).iterdir())
    except OSError as e:
        log_with_sid("executor", "warning",
                     f"EXEC_ORPHAN_LOCK_FAILED {exec_transport.log_safe_text(type(e).__name__, 80)}")
        return 0
    for child in entries:
        if not exec_transport.valid_job_id(child.name):
            continue
        try:
            info = os.lstat(child)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                continue
            os.chmod(child, 0o000)
            locked += 1
        except PermissionError:
            not_permitted += 1
        except OSError as e:
            log_with_sid("executor", "warning",
                         f"EXEC_ORPHAN_LOCK_FAILED {exec_transport.log_safe_text(type(e).__name__, 80)}")
    if locked or not_permitted:
        log_with_sid("executor", "info",
                     f"EXEC_ORPHAN_LOCKED count={int(locked)} not_owned={int(not_permitted)}")
    return locked


def _sweep_orphans(shared_dir: Path) -> None:
    """Remove aged job directories, and the strays generated code leaves.

    The main app removes its own job directory in a `finally`; this only
    catches the ones a crashed web worker left behind. Possible at all because
    `create_job_dir` gives the shared group write access — which is also what
    lets generated code create files and directories in the ROOT of the
    volume, so this sweep owns those too (`_remove_stray_entry`).
    """
    try:
        entries = list(Path(shared_dir).iterdir())
    except OSError as e:
        log_with_sid("executor", "warning",
                     f"EXEC_ORPHAN_SWEEP_FAILED: {exec_transport.log_safe_text(str(e))}")
        return
    now = time.time()
    for child in entries:
        if not exec_transport.valid_job_id(child.name):
            _remove_stray_entry(child, now)
            continue
        try:
            info = os.lstat(child)
            if not stat.S_ISDIR(info.st_mode):
                # A job-id-shaped name that is not a directory was never
                # created by `create_job_dir`, which only makes directories —
                # so it is a stash wearing a job id, not a job.
                _remove_stray_entry(child, now)
                continue
            with _INFLIGHT_LOCK:
                active = child.name in _ACTIVE_JOBS
            if active:
                continue
            # A FUTURE mtime is not fresh: generated code can forward-date
            # what it writes, and a negative age would keep it forever.
            if -_FUTURE_MTIME_TOLERANCE_S <= now - info.st_mtime <= _ORPHAN_MAX_AGE_S:
                continue
            _rmtree_repairing_modes(child)
            if _still_present(child):
                log_with_sid(child.name, "warning",
                             "EXEC_ORPHAN_REMOVE_FAILED: entry still present after removal")
                continue
            log_with_sid(child.name, "info", "EXEC_ORPHAN_REMOVED")
        except OSError as e:
            log_with_sid(child.name, "warning",
                         f"EXEC_ORPHAN_REMOVE_FAILED: {exec_transport.log_safe_text(str(e))}")


def _remove_stray_entry(child, now: float) -> None:
    """Remove one aged entry of the jobs root that is not a job directory.

    Generated code runs as this uid with the jobs root group-writable, so it
    can create files and directories directly in that root — and until now
    nothing ever removed them, since both sweeps skipped every name that is
    not job-id shaped. The volume is named and disk-backed, so a stash
    outlived restarts, image upgrades and any number of intervening jobs, was
    readable by a later job run for a different user, and was visible from the
    web container. The root is supposed to hold nothing but job directories,
    so anything else is debris or a deliberate stash, and both go.

    OWNERSHIP IS DELIBERATELY NOT CHECKED HERE, and that is the one rule this
    side does not share with the main app's copy. It used to remove only what
    THIS uid owns, on the reasoning that its own uid is exactly the set
    generated code can have created. It is not: the root is group-writable, so
    generated code does not have to CREATE a web-owned entry, it can ACQUIRE
    one — `os.rename` of its own live job directory to a name that is not
    job-id shaped keeps `st_uid` at the web identity while the content becomes
    a stash. The two sweeps then disagreed by construction and both skipped
    it: the web side because the entry's uid IS its own, this side because it
    is not. Authorship and ownership are different things on a group-writable
    directory, so ownership classifies nothing useful here — the only entry
    that legitimately exists in this root is a 32-hex job directory created by
    the web service, and that shape never reaches this function.

    The rule the main app keeps is not portable to this side either: it exists
    to protect a misconfigured `EXECUTOR_SHARED_DIR` pointed at customer state,
    where every file belongs to the web uid. Nothing of the customer's is
    mounted in this container at all — the jobs volume is its only mount — so
    that direction buys no protection here while leaving the hole above. And
    deleting a web-owned entry is not a new capability on this side: clearing
    an abandoned, web-created job directory is what the group write is for.

    Still bounded two ways: the age threshold a job directory gets, so nothing
    mid-creation is taken (an entry dated in the FUTURE is not fresh — a
    negative age would keep a forward-dated stash forever); and a symlink is UNLINKED, never followed —
    generated code chooses where it points, and the same volume is read by the
    container where the customer's data IS mounted.

    Never raises (Article IV) — best effort, like the job-directory path.
    """
    try:
        info = os.lstat(child)
        if -_FUTURE_MTIME_TOLERANCE_S <= now - info.st_mtime <= _ORPHAN_MAX_AGE_S:
            return
        mode = info.st_mode
        if stat.S_ISLNK(mode):
            entry_kind = "link"
            os.unlink(child)
        elif stat.S_ISDIR(mode):
            entry_kind = "dir"
            _rmtree_repairing_modes(child)
        else:
            entry_kind = "file" if stat.S_ISREG(mode) else "other"
            os.unlink(child)
        if _still_present(child):
            log_with_sid("executor", "warning",
                         f"EXEC_STRAY_ENTRY_REMOVE_FAILED "
                         f"name={exec_transport.log_safe_text(Path(child).name, 200)}: "
                         f"entry still present after removal")
            return
        log_with_sid("executor", "info",
                     f"EXEC_STRAY_ENTRY_REMOVED kind={entry_kind} "
                     f"name={exec_transport.log_safe_text(Path(child).name, 200)}")
    except FileNotFoundError:
        # The main app's sweep got there first; that is the success case.
        return
    except OSError as e:
        log_with_sid("executor", "warning",
                     f"EXEC_STRAY_ENTRY_REMOVE_FAILED "
                     f"name={exec_transport.log_safe_text(Path(child).name, 200)}: "
                     f"{exec_transport.log_safe_text(str(e))}")


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
        log_with_sid(exec_transport.log_safe_text(Path(path).name, 200) or "executor",
                     "warning",
                     f"EXEC_ORPHAN_MODE_REPAIR_FAILED: {exec_transport.log_safe_text(str(e))}")


def _still_present(path) -> bool:
    """True while an entry is still on disk after a removal attempt.

    The `rmtree` error hook swallows what it cannot repair, so `rmtree`
    returning is NOT proof of removal; a REMOVED line is written only once an
    `lstat` confirms the entry is gone. Anything but "not found" counts as
    present, so the log never claims a removal it cannot see.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _open_directory_modes(top) -> None:
    """Make every DIRECTORY under `top` writable and traversable, top-down.

    Generated code can `chmod 0500` (or `0000`) a directory it created. The
    `rmtree` error hook only ever sees the entry whose removal failed — a FILE
    whose parent is read-only — and chmodding that file never makes its
    parent writable, so the directories are opened first, from the top, each
    one before the walk descends into it. Only what this uid owns can be
    changed; everything else is left for the removal to report. Symlinks are
    never followed or chmodded (`os.chmod` would change their TARGET).
    Never raises.
    """
    with suppress(OSError):
        if stat.S_ISDIR(os.lstat(top).st_mode):
            os.chmod(top, 0o770)
    for current, dirnames, _ in os.walk(top, topdown=True, followlinks=False,
                                        onerror=lambda _error: None):
        for name in dirnames:
            candidate = os.path.join(current, name)
            with suppress(OSError):
                if stat.S_ISDIR(os.lstat(candidate).st_mode):
                    os.chmod(candidate, 0o770)


def _rmtree_repairing_modes(path: Path) -> None:
    _open_directory_modes(path)
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
def _same_uid_pids() -> set:
    """Live processes of our own euid other than this process and its parent.

    A zombie (`Z`) or dead (`X`) entry is not counted: it runs nothing, and an
    escapee killed by an earlier pass stays a zombie until whoever it was
    reparented to reaps it — this service, when it is pid 1, never does.
    The PARENT exclusion is load-bearing: the app is not always pid 1 (a
    supervisor, `--reload`, a future multi-worker uvicorn), and the process
    above it shares this uid — killing it would take the service down.
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or not os.path.isdir("/proc"):
        return set()
    me = geteuid()
    spare = {os.getpid(), os.getppid()}
    found = set()
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
        state = ""
        ppid = None
        for line in status.splitlines():
            if line.startswith("State:"):
                state = line.split(":", 1)[1].strip()[:1]
            elif line.startswith("PPid:"):
                with suppress(IndexError, ValueError):
                    ppid = int(line.split()[1])
            elif line.startswith("Uid:"):
                with suppress(IndexError, ValueError):
                    uid = int(line.split()[1])
        if uid != me:
            continue
        if state not in ("Z", "X"):
            found.add(pid)
        elif ppid == os.getpid():
            # A zombie reparented to this service (a job's child that exited
            # on its own): queue it for reaping, unless it is a job runner,
            # whose own Popen collects its status.
            with _INFLIGHT_LOCK:
                if pid not in _RUNNER_PIDS:
                    _REAP_PENDING.add(pid)
    return found


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return
    with _INFLIGHT_LOCK:
        if len(_SWEPT_PIDS) > 4096:
            _SWEPT_PIDS.clear()
        _SWEPT_PIDS.add(int(pid))
        if int(pid) not in _RUNNER_PIDS:
            _REAP_PENDING.add(int(pid))
    log_with_sid("executor", "warning", f"EXEC_STRAY_KILLED pid={int(pid)}")


def _reap_swept() -> None:
    """Collect the exit status of killed escapees that were reparented to
    this process. When the service is pid 1 (the container's init) it is
    their parent, and an unreaped zombie keeps a pid slot counted against the
    uid's process limits — enough of them and the NEXT user's job cannot
    start a thread. Only pids the sweep killed are reaped (never a job
    runner, whose status its own Popen collects), plus zombies
    `_same_uid_pids` found parented to this process. When the service is not
    pid 1 (a supervisor, `--reload`) orphans go to that parent instead and
    are dropped here as not ours (dev only). Never raises."""
    with _INFLIGHT_LOCK:
        pending = list(_REAP_PENDING)
    for pid in pending:
        with _INFLIGHT_LOCK:
            if pid in _RUNNER_PIDS:           # a runner: its Popen collects it
                _REAP_PENDING.discard(pid)
                continue
        try:
            done, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            done = pid                     # not our child: someone else reaps it
        except OSError:
            continue
        if done:
            with _INFLIGHT_LOCK:
                _REAP_PENDING.discard(pid)


def _sweep_same_uid_processes() -> bool:
    """SIGKILL every process of our own euid except this process and its
    parent, REPEATING until a full pass over /proc finds none.

    A `setsid()` escapee survives the job's process-group kill; this is what
    catches it and what makes "no other process exists during an exec" true.
    One snapshot is not enough: a process forked after it was taken would
    survive into the next user's job. The loop is bounded
    (`_SWEEP_MAX_PASSES`, `_SWEEP_PAUSE_S` apart); if it is exhausted the
    service marks itself unhealthy (`_UNHEALTHY`) — `/healthz` answers 503 and
    `/execute` refuses every further job until the container is restarted.
    Returns True when the uid is clean.
    """
    if _alone_in_flight():
        # No sibling runner can be in the record: stale escapee pids go, so a
        # reused pid can never turn a real OOM kill into `crashed`.
        with _INFLIGHT_LOCK:
            _SWEPT_PIDS.clear()
    for _ in range(_SWEEP_MAX_PASSES):
        _reap_swept()
        pids = _same_uid_pids()
        if not pids:
            # This pass may have queued zombies (children that died with the
            # runner's process group): reap them before the next job runs.
            _reap_swept()
            return True
        for pid in sorted(pids):
            _kill_pid(pid)
        time.sleep(_SWEEP_PAUSE_S)
    _reap_swept()
    if not _same_uid_pids():
        return True
    if _UNHEALTHY["reason"] is None:
        _UNHEALTHY["reason"] = "stray_processes"
        log_with_sid("executor", "error",
                     "EXECUTOR_UNHEALTHY reason=stray_processes: processes of the job uid "
                     "survived every sweep pass; refusing further jobs until restarted")
    return False


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
def _runner_env(config: Config, response_fd: int, scratch: Optional[Path] = None) -> dict:
    """The runner's environment, built from scratch as an ALLOWLIST.

    `ARROW_DEFAULT_MEMORY_POOL=system` matters: jemalloc/mimalloc reserve large
    virtual ranges that `RLIMIT_AS` counts against the job. With a `scratch`
    directory, every temp/cache location points into it (TMPDIR, TMP, TEMP,
    HOME, XDG_CACHE_HOME, MPLCONFIGDIR), so what a job writes there is removed
    with it instead of waiting in the shared /tmp for the next job.
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
        # The runner logs through the same logger_utils: it must never open a
        # log FILE that a later job (same uid) could read.
        "PDC_EXECUTOR": "1",
    }
    if scratch is not None:
        env.update({"TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch),
                    "HOME": str(scratch), "XDG_CACHE_HOME": str(scratch),
                    "MPLCONFIGDIR": str(scratch / "mpl")})
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

    This wrapper owns the in-flight accounting the sweep consults and the
    job's private scratch directory (created before, removed after).
    """
    _enter_job()
    scratch = None
    try:
        scratch = _make_scratch(request.job_id)
        return _execute_job(config, request, job_dir, remaining, scratch)
    finally:
        if scratch is not None:
            _remove_scratch(scratch, request.job_id)
        _leave_job()


def _swept_by_us(pid: int) -> bool:
    with _INFLIGHT_LOCK:
        if pid in _SWEPT_PIDS:
            _SWEPT_PIDS.discard(pid)
            return True
    return False


def _file_digest(path: Path) -> tuple:
    data = path.read_bytes()
    return (len(data), hashlib.sha256(data).hexdigest())


def _mpl_template() -> Path:
    return Path(tempfile.gettempdir()) / _MPL_TEMPLATE_NAME


def _warm_mpl_template() -> None:
    """Build matplotlib's font cache ONCE into the template directory, so a
    job's fresh MPLCONFIGDIR starts from a copy instead of rebuilding it (a
    rebuild costs seconds per job). Never raises; without a template each job
    simply builds its own."""
    template = _mpl_template()
    try:
        template.mkdir(mode=0o700, exist_ok=True)
        subprocess.run([sys.executable, "-I", "-c", "import matplotlib.font_manager"],
                       env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                            "HOME": str(template), "MPLCONFIGDIR": str(template),
                            "MPLBACKEND": "Agg"},
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=180, check=False)
        # Record what was built: the template is writable by the job uid, so a
        # file a job later adds or rewrites there is never copied into
        # another job's scratch.
        _MPL_TEMPLATE_FILES.clear()
        for item in template.glob("*.json"):
            if item.is_file() and not item.is_symlink():
                _MPL_TEMPLATE_FILES[item.name] = _file_digest(item)
    except Exception as e:
        log_with_sid("executor", "warning",
                     f"EXEC_MPL_TEMPLATE_FAILED {exec_transport.log_safe_text(type(e).__name__, 80)}")


def _make_scratch(job_id: str) -> Path:
    """A fresh 0700 directory for one job, seeded with the font-cache files.

    Only template files whose size and hash still match the warm-up record are
    copied: the template lives in the shared /tmp and is writable by the job
    uid, so it must not become a channel into later jobs' scratch. Residuals,
    stated: the job uid can still write the template (a changed file is then
    skipped, not copied), and a path pre-planted as a symlink at this job's
    scratch name is unreachable in practice (job ids are random) — were it
    there, the removal below would touch only entries this uid owns.
    Raises OSError when it cannot be created (the job then fails to spawn)."""
    scratch = Path(tempfile.gettempdir()) / f"{_SCRATCH_PREFIX}{job_id}"
    if scratch.exists() or scratch.is_symlink():
        _rmtree_repairing_modes(scratch)
    scratch.mkdir(mode=0o700)
    mpl = scratch / "mpl"
    mpl.mkdir(mode=0o700)
    try:
        for name, expected in _MPL_TEMPLATE_FILES.items():
            item = _mpl_template() / name
            # Size first (lstat): a job-planted huge file is skipped unread.
            if (item.is_symlink() or not item.is_file()
                    or os.lstat(item).st_size != expected[0]
                    or _file_digest(item) != expected):
                if not _MPL_TAMPER_LOGGED["logged"]:
                    _MPL_TAMPER_LOGGED["logged"] = True
                    log_with_sid(job_id, "warning",
                                 "EXEC_MPL_TEMPLATE_TAMPERED a font-cache template file "
                                 "changed after warm-up; it is no longer copied")
                continue
            (mpl / name).write_bytes(item.read_bytes())
    except OSError as e:
        log_with_sid(job_id, "warning",
                     f"EXEC_MPL_TEMPLATE_COPY_FAILED {exec_transport.log_safe_text(type(e).__name__, 80)}")
    return scratch


def _remove_scratch(scratch: Path, job_id: str) -> None:
    """Remove a job's scratch directory; never raises."""
    try:
        _rmtree_repairing_modes(scratch)
    except Exception as e:
        log_with_sid(job_id, "warning",
                     f"EXEC_SCRATCH_REMOVE_FAILED {exec_transport.log_safe_text(type(e).__name__, 80)}")
    if _still_present(scratch):
        log_with_sid(job_id, "warning", "EXEC_SCRATCH_REMOVE_FAILED entry still present")


def _execute_job(config: Config, request: ExecuteRequest, job_dir: Path,
                 remaining: float, scratch: Optional[Path] = None) -> dict:
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
            # Spawned under the lock and registered before it is released:
            # a concurrent sweep can only see this pid after the fork, and it
            # then always finds it in _RUNNER_PIDS (never queued for reaping —
            # its exit status belongs to this Popen).
            with _INFLIGHT_LOCK:
                process = subprocess.Popen(
                    [sys.executable, "-I", "-u", str(_RUNNER), str(job_json)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    pass_fds=(write_fd,),
                    env=_runner_env(config, write_fd, scratch),
                    cwd=str(_APP_ROOT),
                    start_new_session=True,
                )
                _RUNNER_PIDS.add(process.pid)
        finally:
            os.close(write_fd)
    except Exception as e:
        os.close(read_fd)
        log_with_sid(job_id, "error",
                     f"EXEC_JOB_SPAWN_FAILED "
                     f"{exec_transport.log_safe_text(f'{type(e).__name__}: {e}')}")
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
    # The sweep runs at EVERY concurrency. With EXECUTOR_MAX_CONCURRENT > 1
    # (non-default, documented as forfeiting the isolation) it can kill a
    # sibling job's runner, which then answers `crashed`; skipping it instead
    # would let one job's escapee run on into the next user's job.
    if not _alone_in_flight():
        log_with_sid(job_id, "warning", "EXEC_STRAY_SWEEP_CONCURRENT sibling jobs may be killed")
    _sweep_same_uid_processes()
    for reader in readers:
        reader.join(timeout=_READER_JOIN_S)

    with _INFLIGHT_LOCK:
        _RUNNER_PIDS.discard(process.pid)
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
        elif exit_code == -signal.SIGKILL and _swept_by_us(process.pid):
            # A sibling job's sweep killed this runner (EXECUTOR_MAX_CONCURRENT
            # > 1): the service's own kill, not a memory problem of the code.
            response = {"status": "crashed", "kind": request.kind, "payload": None,
                        "elapsed_ms": elapsed_ms, "peak_rss_mb": None,
                        "stdout": stdout_text, "stderr": stderr_text,
                        "traceback": stderr_text, "exit_code": exit_code,
                        "signal": signal_no, "reason": "signal"}
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

    # Lengths only, never the text: stderr and stdout are written by the job,
    # and this log is not the place another user's values should land. The
    # full streams travel in the response; the web service logs its own tail.
    level = "info" if response["status"] in ("ok", "error") else "warning"
    log_with_sid(job_id, level,
                 f"EXEC_JOB_END status={response['status']} code_hash={code_hash} "
                 f"elapsed_ms={int(elapsed_ms)} "
                 f"exit_code={exit_code} "
                 f"reason={exec_transport.log_safe_text(str(response.get('reason')), 80)} "
                 f"stderr_len={len(stderr_text)} "
                 f"stdout_len={len(response.get('stdout') or '')}")
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
    on the event loop. 503 once the process sweep could not clear the job uid
    (`_UNHEALTHY`): the container's healthcheck then reports it.
    """
    if _UNHEALTHY["reason"]:
        return _json({"ok": False, "unhealthy": _UNHEALTHY["reason"], "version": BUILD_COMMIT,
                      "build_time": BUILD_TIME}, status_code=503)
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
    if _UNHEALTHY["reason"]:
        return _json({"code": "EXECUTOR_UNHEALTHY",
                      "message": "the executor could not clear a previous job's processes"},
                     status_code=503)
    invalid = _validate(request, config)
    if invalid is not None:
        return invalid

    job_dir = config.shared_dir / request.job_id
    with _INFLIGHT_LOCK:
        _ACTIVE_JOBS.add(request.job_id)
    try:
        return await _execute_accepted(config, request, job_dir)
    finally:
        # Restart the directory's age clock at COMPLETION: the web service
        # still reads `out/` after this answer, and a job that ran longer than
        # the sweep threshold would otherwise be eligible for removal while it
        # does. Group write is enough to set the time to "now".
        with suppress(OSError):
            os.utime(job_dir, None)
        with _INFLIGHT_LOCK:
            _ACTIVE_JOBS.discard(request.job_id)


async def _execute_accepted(config, request, job_dir: Path) -> Response:
    """The body of /execute once the request is valid; the job id is in
    `_ACTIVE_JOBS` for the whole call, so the orphan sweep leaves its
    directory alone however long it queues or runs."""
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
        log_with_sid(request.job_id, "error",
                     f"EXEC_JOB_FAILED "
                     f"{exec_transport.log_safe_text(f'{type(e).__name__}: {e}')}")
        response = {"status": "crashed", "kind": request.kind, "payload": None,
                    "elapsed_ms": None, "peak_rss_mb": None, "stdout": "", "stderr": "",
                    "traceback": f"{type(e).__name__}: {e}", "exit_code": None,
                    "signal": None, "reason": "executor_error"}
    finally:
        semaphore.release()
    return _json(response)
