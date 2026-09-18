"""Shared job transport between the main app and the `pdc-executor` container.

Both sides import this ONE module (it is copied into the executor image), so
the request/response grammar can never drift between writer and reader:

    main app                                   executor
    --------                                   --------
    create_job_dir(shared_dir, job_id)
    write_inputs(dfs, job_dir)  --manifest-->   read_inputs(manifest, job_dir)
                                                serialize_result(exec_return, job_dir)
    deserialize_result(response, job_dir, ...)  <--HTTP body--

JSON dialect
------------
`dumps` / `loads` are Python's own JSON with `allow_nan=True`: NaN and
Infinity travel as the `NaN` / `Infinity` tokens on EVERY hop (runner → the
response fd → the executor's HTTP body → the main app), so a computed NaN is
still a NaN after transport and is never silently coerced to None. The
executor must answer with a plain `starlette.responses.Response(content=
dumps(...))` — `JSONResponse` renders with `allow_nan=False` and raises.

Pickle
------
`write_inputs` falls back to `.pkl` for the few dataframes a parquet
round-trip cannot reproduce byte-identically (a mixed numeric/string object
column, list-valued cells) — the same precedent as the on-disk parquet cache
in `local_store._parquet_cache_write`. That is safe HERE and only in this
direction because (a) the writer and the reader are both this codebase, (b)
the job directory is not user-writable (mode 2770, owned by the web uid and
the shared `pdc` group), and (c) at most one job exists per directory while it
executes. Nothing in the OTHER direction is ever unpickled: the executor is
the untrusted side, so `deserialize_result` refuses a `.pkl` reference even
when the file exists, and reads result frames only as UNCOMPRESSED parquet
inside hard row/column/byte caps.

Article II: no value in this module is ever forwarded to the brain. The
`preview` encoder mirrors `run_chat_local._safe_preview` exactly (including
its `.item()` quirks) so the bytes the brain eventually receives are the same
bytes it receives today; anything that guard would reject crosses as a bare
type NAME (`Opaque`), never as content.
"""
from __future__ import annotations

import base64
import json
import math
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from exec_sanitizer import sanitize_for_execution
from logger_utils import log_with_sid

# --- caps ------------------------------------------------------------------
# Inline vs file ref for a base64 PNG / an HTML chart: 2 MiB keeps an ordinary
# chart in the JSON body and a 20 MiB figure out of it.
INLINE_MAX_BYTES = 2 * 1024 * 1024
# A referenced PNG/HTML file the main app will read back.
REF_FILE_MAX_BYTES = 64 * 1024 * 1024
# Result-parquet guards. Every parquet metadata field is written by the
# untrusted side, so the reader checks rows/columns/file size BEFORE decoding
# and then sums the decoded batches too.
RESULT_MAX_ROWS = 5_000_000
RESULT_MAX_COLUMNS = 2_000
RESULT_FILE_MAX_BYTES = 256 * 1024 * 1024
RESULT_MAX_DECODED_BYTES = 512 * 1024 * 1024
# Captured stdout/stderr per job.
STDIO_MAX_CHARS = 65536

# The widest slice of a string the untrusted side wrote that a log line will
# repeat by default (`log_safe_text`); callers with a narrower field pass their
# own cap.
LOG_TEXT_MAX_CHARS = 2000
# Error / traceback strings (they reach the brain's retry prompt).
ERROR_MAX_CHARS = 20000
# `chart_data` is display-only; a pathological one is dropped, not fatal.
CHART_DATA_MAX_BYTES = 4 * 1024 * 1024
# Styler HTML caps — duplicated from `run_chat_local._STYLED_MAX_*` (that
# module is not in the executor image); a test pins the two together.
STYLED_MAX_ROWS = 200
STYLED_MAX_COLS = 40
STYLED_MAX_CHARS = 500_000

# --- grammar ---------------------------------------------------------------
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_INPUT_PATH_RE = re.compile(r"in/[A-Za-z0-9_.-]+")
_REF_PATH_RE = re.compile(r"out/[A-Za-z0-9_.-]+")
_INPUT_FORMATS = ("parquet", "pickle")
_DIR_MODE = 0o2770

KINDS = ("PYTHON", "PLOT")

MEMORY_ERROR_TEXT = "MemoryError: execution exceeded the memory limit"

# The executor's CLOSED `reason` vocabulary, shared by both images so the two
# sides cannot drift (a structural test pins it against `executor/app.py`'s own
# literals). It is an allowlist because `reason` arrives in the response body —
# the untrusted side — and is inlined into the error TEXT that
# `brain_client.retry` forwards into the planner's retry prompt. The same
# defect was already fixed for the rejection code: 40 000 characters or an
# embedded newline of the sandbox's choosing must never reach a prompt.
# The widest `exit_code` / `signal` the crash sentence will repeat: a process
# exit status is -NSIG..255 and a signal number is smaller still.
EXIT_VALUE_MAX = 65535
# The widest `elapsed_ms` / `peak_rss_mb` a LOG LINE will repeat. They are a
# job duration in milliseconds and a resident-set size in MB, so a billion
# bounds either at ten characters without ever rejecting a real reading.
METRIC_VALUE_MAX = 10 ** 9

CRASH_REASONS = frozenset({
    "spawn_failed", "hard_timeout", "sigkill", "response_invalid",
    "signal", "exit", "queued", "executor_error",
})


def dumps(obj: Any) -> str:
    """The wire dialect: compact, Unicode-literal, NaN/Infinity preserved."""
    return json.dumps(obj, ensure_ascii=False, allow_nan=True, separators=(",", ":"))


def loads(payload: Any) -> Any:
    """Parse the wire dialect from `str` or `bytes`."""
    return json.loads(payload)


def log_safe_text(value: Any, max_chars: int = LOG_TEXT_MAX_CHARS, *,
                  tail: bool = False) -> str:
    """ONE line of an untrusted string, length-capped, for a log field.

    The execution error text, the traceback and the stderr tail are written by
    the UNTRUSTED side: generated code failing in the sandbox produces the
    message, which `code_exec` renders as `f"{type(e).__name__}: {e}"`, so
    every character of it is that code's choice. So is an exception this hop
    RAISES ABOUT that side: pyarrow quotes the result file's own metadata
    strings back when it refuses to read it, and pandas quotes a customer
    column name, so an `{e}` in a log MESSAGE is the same untrusted string
    wearing a library's error text. The durable client log is
    NEWLINE-DELIMITED, so an embedded newline forges a complete, plausible
    extra record into the file operators grep — it can claim any other event's
    shape and any other job's id. Truncation does not help: a newline at
    character 10 still splits the line. CR and LF are therefore escaped at
    EVERY site that puts such a string on a log line, which is why this helper
    lives in the module both containers already share instead of in one
    caller.

    `tail=True` keeps the LAST `max_chars` (where an exception's own message
    sits, after a long class path); the default keeps the first, matching the
    `[:N]` slices the callers wrote. Escaping before the second cut keeps the
    field bounded by the cap either way, since an escape doubles a character.
    Never raises (Article IV): an unrenderable field is logged as empty rather
    than costing the caller its whole log line.
    """
    try:
        if not isinstance(value, str) or not value:
            return ""
        limit = (max_chars if isinstance(max_chars, int) and max_chars > 0
                 else LOG_TEXT_MAX_CHARS)
        cut = value[-limit:] if tail else value[:limit]
        cut = cut.replace("\r", "\\r").replace("\n", "\\n")
        return cut[-limit:] if tail else cut[:limit]
    except Exception:
        return ""


def normalize_timeout(value):
    """A timeout that is INTEGRAL renders as an `int`, never as `60.0`.

    `CODE_EXEC_TIMEOUT_SECONDS` is the int `60`, so the in-process timeout
    text says "60 seconds limit" — but the value crosses to the executor as
    JSON and `ExecuteRequest.timeout_s` is typed `float`, which turns it into
    `60.0` and the runner's own text into "60.0 seconds limit". That string is
    forwarded to the brain by `brain_client.retry`, so the two characters
    matter: BOTH sides normalize here, and a genuinely fractional timeout
    (`2.5`) keeps its float rendering. Non-numeric / NaN / infinite input is
    returned unchanged — this helper never raises (Article IV).
    """
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return value
    if not math.isfinite(as_float):
        return value
    if as_float.is_integer():
        return int(as_float)
    return as_float


def timeout_error_text(timeout) -> str:
    """Byte-identical to `code_exec.safe_execute`'s own timeout text."""
    return f"TimeoutError: Code execution exceeded {normalize_timeout(timeout)} seconds limit"


def crash_error_text(exit_code, signal_no, reason) -> str:
    return ("ExecutorCrashError: the analysis process exited unexpectedly "
            f"(exit={exit_code}, signal={signal_no}, reason={reason})")


def known_number(value, maximum, allow_float: bool = False):
    """A BOUNDED number from the response, or None. Never raises.

    PUBLIC and the ONE implementation for every numeric field the untrusted
    side sends into a text or a log line — the crash sentence's `exit_code` /
    `signal`, and the dispatcher's `elapsed_ms` / `peak_rss_mb`. JSON puts no
    limit on what type a field holds nor on the digits of a number, so without
    this a 40 000-character string or a 40 000-digit int would be repeated
    verbatim into a prompt the brain reads or a file an operator greps.

    An int passes; a float that is exactly an integer passes AS AN INT; a
    string, a dict, a non-finite float and `True` all become None, as does any
    magnitude past `maximum`. `None` itself stays None — that is what a normal
    crash already reports for the field that does not apply. With
    `allow_float` a fractional value keeps its fraction (a memory reading);
    without it, only an integral one passes.

    `bool` is rejected before `int` on purpose: it IS an int in Python, and
    `exit=True` is not an exit code.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if value.is_integer():
            value = int(value)
        elif not allow_float:
            return None
    if not isinstance(value, (int, float)):
        return None
    if abs(value) > maximum:
        return None
    return value


def _known_exit_value(value):
    """An `exit_code` / `signal` for the crash sentence, or None.

    The crash sentence interpolates three fields, and ALL THREE come from the
    response body — which the untrusted side writes — into a text that
    `brain_client.retry` forwards to the brain verbatim. Validating only the
    field that was noticed first would leave the same injection route open
    through the other two, so a small process number is all either of these
    may be. Real values are a process exit status (-NSIG..255) or a signal
    number, so the bound never rejects one and the field can add at most six
    characters.
    """
    return known_number(value, EXIT_VALUE_MAX)


def known_reason(reason):
    """A `reason` from the response, or the literal `unknown`.

    PUBLIC because the dispatcher logs the same field on its `EXEC_TIMEOUT`
    line and must apply the same closed vocabulary — a second copy of this
    idea is how the two would drift.

    `None` is part of the vocabulary (the executor's own
    `setdefault("reason", None)`), so a plain crash keeps reading as one.
    Everything else the response chose collapses to one token, because this
    value ends up inside the sentence the planner's retry prompt reads and on
    a durable log line.
    """
    if reason is None:
        return None
    if isinstance(reason, str) and reason in CRASH_REASONS:
        return reason
    return "unknown"


class Opaque:
    """A value that could not cross the boundary — only its TYPE NAME did.

    Every consumer treats it exactly as it treats the original object today:
    `_build_table_from_result` returns None, `_safe_preview` returns None.
    """

    __slots__ = ("type_name",)

    def __init__(self, type_name: str):
        self.type_name = str(type_name)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Opaque({self.type_name!r})"

    def __eq__(self, other) -> bool:
        return isinstance(other, Opaque) and other.type_name == self.type_name

    def __hash__(self) -> int:
        return hash(("Opaque", self.type_name))


class StyledFrame:
    """A transported pandas Styler: the frame plus its rendered HTML.

    Shaped so `run_chat_local._styler_to_html` / `_build_table_from_result`
    accept it unchanged (`.data` + `.to_html()`). `to_html()` returns `""`
    when the executor skipped the render (over the Styler caps) — the blank
    check in `_styler_to_html` then yields None while the row count stays
    real.
    """

    __slots__ = ("data", "_html")

    def __init__(self, data: pd.DataFrame, html: Optional[str]):
        self.data = data
        self._html = html if isinstance(html, str) else None

    def to_html(self, *args, **kwargs) -> str:
        return self._html if isinstance(self._html, str) else ""

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"StyledFrame(shape={getattr(self.data, 'shape', None)}, html={bool(self._html)})"


class _Violation(Exception):
    """A hostile / malformed executor response (mapped to an exec error).

    `str(...)` is the RETURNED half: it becomes the caller's error text, which
    `brain_client.retry` forwards into the planner's retry prompt, so it may
    name the failure and the reference but never a URL, a path or a datum.
    `detail` is the operator-only half — an OSError repr carries the absolute
    path it failed on, which an operator needs and a returned string must not
    disclose (the storage layout is not the sandbox's to learn, and the
    sandbox can provoke this raise: it holds group write on the job directory,
    so it can replace a component with a symlink). Only `deserialize_result`
    reads it, straight onto the local log line.
    """

    def __init__(self, message: str, detail: Any = None):
        super().__init__(message)
        self.detail = detail


class _TooLarge(Exception):
    """A result over one of the caps (mapped to an exec error)."""


# ---------------------------------------------------------------------------
# job ids and directories
# ---------------------------------------------------------------------------
def new_job_id() -> str:
    return uuid.uuid4().hex


def valid_job_id(job_id: Any) -> bool:
    """True only for a 32-char lowercase hex id.

    The explicit length check is load-bearing: `$` also matches before a
    trailing newline, so `"0" * 32 + "\\n"` passes the pattern alone.
    """
    if not isinstance(job_id, str) or len(job_id) != 32:
        return False
    return bool(JOB_ID_RE.match(job_id))


def create_job_dir(shared_dir, job_id: str) -> Path:
    """Create `<shared_dir>/<job_id>/in/` for one job and return the job dir.

    The modes are set EXPLICITLY (2770 on both) instead of being left to the
    creator's umask: the web process runs with umask 0022, and the executor
    (a different uid in the same `pdc` group) must be able to read the inputs,
    write into the directory, and `rmtree` the whole thing when it is orphaned
    — which needs group-write on `in/` as well. Nothing here deletes; the
    caller owns removal.
    """
    if not valid_job_id(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")
    job_dir = Path(shared_dir) / job_id
    job_dir.mkdir(exist_ok=True)
    in_dir = job_dir / "in"
    in_dir.mkdir(exist_ok=True)
    for path in (job_dir, in_dir):
        try:
            os.chmod(path, _DIR_MODE)
        except OSError as e:
            # Windows has no setgid bit; a dev run must not fail on it.
            log_with_sid(job_id, "warning",
                         f"JOB_DIR_CHMOD_FAILED {path.name}: "
                         f"{log_safe_text(str(e))}")
    return job_dir


def ensure_out_dir(job_dir) -> Path:
    """Create `<job_dir>/out/` (2770, same reason as `create_job_dir`)."""
    out_dir = Path(job_dir) / "out"
    out_dir.mkdir(exist_ok=True)
    try:
        os.chmod(out_dir, _DIR_MODE)
    except OSError as e:
        # Windows has no setgid bit; a dev run must not fail on it. Mirrors
        # `create_job_dir`'s JOB_DIR_CHMOD_FAILED so a REAL permission problem
        # on the shared volume is visible instead of silent.
        log_with_sid(Path(job_dir).name, "warning",
                     f"OUT_DIR_CHMOD_FAILED: {log_safe_text(str(e))}")
    return out_dir


# ---------------------------------------------------------------------------
# inputs
# ---------------------------------------------------------------------------
def check_input_entry(entry: Any) -> dict:
    """Validate ONE manifest entry; raises ValueError. Used by `read_inputs`
    and again by the HTTP layer before a runner is spawned."""
    if not isinstance(entry, dict):
        raise ValueError("manifest entry is not an object")
    name = entry.get("name")
    path = entry.get("path")
    fmt = entry.get("format", "parquet")
    if not isinstance(name, str) or not name:
        raise ValueError("manifest entry has no name")
    if not isinstance(path, str) or not _INPUT_PATH_RE.fullmatch(path):
        raise ValueError(f"manifest path outside in/: {path!r}")
    if fmt not in _INPUT_FORMATS:
        raise ValueError(f"unknown input format: {fmt!r}")
    return {"name": name, "path": path, "format": fmt}


def validate_manifest(manifest: Any) -> list:
    if manifest is None:
        return []
    if not isinstance(manifest, (list, tuple)):
        raise ValueError("manifest is not a list")
    return [check_input_entry(e) for e in manifest]


def write_inputs(dfs: dict, job_dir, sid: Optional[str] = None) -> list:
    """Write every input frame into `<job_dir>/in/` and return the manifest.

    The Article XIII sanitize gate runs FIRST, so the executor writes — and
    generated code sees — exactly the standard dtypes it sees in-process
    today. Each parquet write is round-trip verified (labels + `equals`);
    anything parquet cannot reproduce falls back to a pickle entry.

    The returned list IS the manifest and rides in the request body; its ORDER
    is the dict order, because `code_exec` aliases the FIRST frame as `df`.
    """
    job_dir = Path(job_dir)
    in_dir = job_dir / "in"
    if not in_dir.is_dir():
        in_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(in_dir, _DIR_MODE)
        except OSError:
            pass
    safe = sanitize_for_execution(dfs or {}, sid or "exec")
    manifest: list = []
    for index, (name, df) in enumerate(safe.items()):
        rel = f"in/{index}.parquet"
        target = job_dir / rel
        try:
            df.to_parquet(target, engine="pyarrow", compression=None)
            back = pd.read_parquet(target, engine="pyarrow")
            if list(back.columns) != list(df.columns) or not back.equals(df):
                raise ValueError("parquet round-trip altered the dataframe")
            manifest.append({"name": str(name), "path": rel, "format": "parquet"})
            continue
        except Exception as e:
            # `except ... as e` unbinds at the end of the block — keep the
            # parquet reason for the fallback log line.
            parquet_error = f"{type(e).__name__}: {e}"
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        rel = f"in/{index}.pkl"
        target = job_dir / rel
        try:
            df.to_pickle(target)
            back = pd.read_pickle(target)
            if list(back.columns) != list(df.columns) or not back.equals(df):
                raise ValueError("pickle round-trip altered the dataframe")
        except Exception as e_pkl:
            log_with_sid(sid or "exec", "error",
                         f"EXEC_INPUT_WRITE_FAILED key={log_safe_text(str(name), 200)}: "
                         f"parquet: {log_safe_text(parquet_error)}; "
                         f"pickle: {log_safe_text(str(e_pkl))}")
            raise ValueError(f"cannot transport dataframe {name!r}: {e_pkl}") from e_pkl
        log_with_sid(sid or "exec", "info",
                     f"EXEC_INPUT_PICKLE_FALLBACK "
                     f"key={log_safe_text(str(name), 200)}: "
                     f"{log_safe_text(parquet_error)}")
        manifest.append({"name": str(name), "path": rel, "format": "pickle"})
    return manifest


def read_inputs(manifest: Any, job_dir) -> dict:
    """Rebuild the `dfs` dict from the manifest, IN LIST ORDER.

    Every path is re-validated against the grammar and the filesystem (inside
    `in/`, a regular file, not a symlink) — the manifest arrives over HTTP.
    """
    job_dir = Path(job_dir)
    entries = validate_manifest(manifest)
    root = job_dir.resolve()
    out: dict = {}
    for entry in entries:
        target = job_dir / entry["path"]
        try:
            st = os.lstat(target)
        except OSError as e:
            raise ValueError(f"input not readable: {entry['path']}: {e}") from e
        if stat.S_ISLNK(st.st_mode):
            raise ValueError(f"input is a symlink: {entry['path']}")
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"input is not a regular file: {entry['path']}")
        if target.resolve().parent.parent != root:
            raise ValueError(f"input escapes the job dir: {entry['path']}")
        if entry["format"] == "parquet":
            out[entry["name"]] = pd.read_parquet(target, engine="pyarrow")
        else:
            out[entry["name"]] = pd.read_pickle(target)
    return out


# ---------------------------------------------------------------------------
# preview — an exact mirror of run_chat_local._safe_preview
# ---------------------------------------------------------------------------
def _native_scalar(value):
    """Plain Python for a scalar (numpy subclasses of str/int/float included)."""
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    return value


def encode_preview(preview: Any) -> Any:
    """Encode the exec preview for the wire.

    Mirrors `_safe_preview`'s accept rules EXACTLY, quirks included: the top
    level is inlined only when it already is None/str/int/float/bool (`.item()`
    is never called there), and inside a dict a non-scalar value is coerced
    through a callable `.item()` when that returns None/str/int/float/bool
    (so `np.datetime64[ns]` → int, `np.datetime64("NaT")` → None,
    `np.array([5])` → 5 stay accepted just as today). Anything the guard would
    reject makes the WHOLE preview an opaque type name — no sibling scalars,
    no row values, nothing but the name.
    """
    try:
        if preview is None:
            return None
        if isinstance(preview, (str, int, float, bool)):
            return _native_scalar(preview)
        if isinstance(preview, dict):
            safe: dict = {}
            for key, value in preview.items():
                if not isinstance(key, (str, int, float, bool)):
                    return _opaque_preview(preview)
                item = getattr(value, "item", None)
                if callable(item) and not isinstance(value, (str, int, float, bool)):
                    try:
                        value = value.item()
                    except Exception:
                        return _opaque_preview(preview)
                if value is not None and not isinstance(value, (str, int, float, bool)):
                    return _opaque_preview(preview)
                safe[key] = None if value is None else _native_scalar(value)
            return safe
        return _opaque_preview(preview)
    except Exception as e:
        log_with_sid("exec", "warning",
                     f"EXEC_PREVIEW_ENCODE_FAILED: {log_safe_text(str(e))}")
        return _opaque_preview(preview)


def _opaque_preview(value: Any) -> dict:
    return {"__opaque__": type(value).__name__}


def decode_preview(encoded: Any) -> Any:
    """Inverse of `encode_preview`: the opaque marker becomes an `Opaque`."""
    if isinstance(encoded, dict) and len(encoded) == 1:
        marker = encoded.get("__opaque__")
        if isinstance(marker, str):
            return Opaque(marker)
    return encoded


# ---------------------------------------------------------------------------
# result serialization
# ---------------------------------------------------------------------------
def dedupe_columns(df: pd.DataFrame) -> pd.DataFrame:
    """The column half of `run_chat_local._normalize_df_for_table`.

    Tuple/MultiIndex labels are flattened to `" / "`-joined strings and
    duplicates get pandas' `name.1` suffixes, so a frame with repeated labels
    survives parquet (which keys columns by name) and lands on the table path
    identically to the in-process render. Identity — the SAME object — when
    the labels are already unique. Never mutates the caller's frame.
    """
    try:
        cols = df.columns
        if cols.is_unique and len(set(map(str, cols))) == len(cols):
            return df
        out = df.copy(deep=False)
        if isinstance(cols, pd.MultiIndex) or any(isinstance(c, tuple) for c in cols):
            out.columns = [
                " / ".join(str(p) for p in c if str(p).strip()) if isinstance(c, tuple)
                else str(c)
                for c in out.columns]
        if len(set(map(str, out.columns))) != len(out.columns):
            seen: dict = {}
            flat = []
            for c in map(str, out.columns):
                n = seen.get(c, 0)
                seen[c] = n + 1
                flat.append(c if n == 0 else f"{c}.{n}")
            out.columns = flat
        return out
    except Exception as e:
        log_with_sid("exec", "warning",
                     f"EXEC_DEDUPE_COLUMNS_FAILED: {log_safe_text(str(e))}")
        return df


def _cast_mixed_labels(df: pd.DataFrame) -> pd.DataFrame:
    """Cast mixed-type column LABELS (`[1, 2, "All"]` from
    `crosstab(margins=True)`) to `str` explicitly.

    pyarrow performs the same cast on its own with a "mixed type" UserWarning;
    doing it here makes the accepted, documented loss warning-free. All-string,
    all-tuple and single-numeric-type labels are untouched.
    """
    try:
        cols = list(df.columns)
        if not cols or isinstance(df.columns, pd.MultiIndex):
            return df
        if all(isinstance(c, str) for c in cols) or all(isinstance(c, tuple) for c in cols):
            return df
        if len({type(c) for c in cols}) <= 1:
            return df
        out = df.copy(deep=False)
        out.columns = [str(c) for c in cols]
        return out
    except Exception as e:
        log_with_sid("exec", "warning",
                     f"EXEC_LABEL_CAST_FAILED: {log_safe_text(str(e))}")
        return df


def _fix_unwritable_object_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Stringify object columns pyarrow refuses (mixed numeric/string cells),
    keeping NaN/None/NaT as missing values."""
    out = df
    for pos, dtype in enumerate(df.dtypes):
        if dtype != object:
            continue
        col = df.iloc[:, pos]
        try:
            pa.array(col)
            continue
        except (pa.lib.ArrowInvalid, pa.lib.ArrowTypeError):
            # EXPECTED: the documented mixed numeric/string cell case, which
            # the stringify below is here to fix. Not logged (it is normal,
            # and the pyarrow message quotes the offending cell).
            pass
        except Exception as e:
            # Anything else is unexpected, but the stringify stays the right
            # fallback. TYPE and column POSITION only: the pyarrow message
            # embeds a CELL VALUE, and this module never logs values (and a
            # column NAME is user data too).
            log_with_sid("exec", "warning",
                         f"EXEC_PARQUET_PROBE_FAILED pos={pos} "
                         f"{log_safe_text(type(e).__name__, 200)}")
        if out is df:
            out = df.copy(deep=False)
        out.isetitem(pos, col.where(col.isna(), col.astype(str)))
    return out


def _normalize_for_parquet(df: pd.DataFrame) -> pd.DataFrame:
    """The three normalizations, in the order they must run in."""
    if not df.columns.is_unique:
        df = dedupe_columns(df)
    df = _fix_unwritable_object_columns(df)
    df = _cast_mixed_labels(df)
    return df


class _OutWriter:
    """Names and writes the `out/` artifacts of one job."""

    def __init__(self, job_dir: Path):
        self.job_dir = Path(job_dir)
        self.out_dir = ensure_out_dir(self.job_dir)
        self._frames = 0

    def frame_ref(self) -> str:
        name = "result.parquet" if self._frames == 0 else f"result_{self._frames}.parquet"
        self._frames += 1
        return f"out/{name}"

    def write_frame(self, df: pd.DataFrame) -> str:
        rel = self.frame_ref()
        _normalize_for_parquet(df).to_parquet(
            self.job_dir / rel, engine="pyarrow", compression=None)
        return rel

    def write_blob(self, text: str, rel: str, as_png: bool) -> dict:
        """Store an over-cap string as a file and return its reference."""
        path = self.job_dir / rel
        if as_png:
            try:
                path.write_bytes(base64.b64decode(text, validate=True))
                return {"ref": rel, "encoding": "png-b64"}
            except Exception:
                rel = rel.rsplit(".", 1)[0] + ".html"
                path = self.job_dir / rel
        path.write_text(text, encoding="utf-8")
        return {"ref": rel, "encoding": "utf8"}


def _json_native(value: Any, depth: int = 0):
    """(ok, converted) — is this a JSON-native value (numpy scalars coerced)?"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return True, _native_scalar(value)
    if depth >= 8:
        return False, None
    if isinstance(value, list):
        out = []
        for element in value:
            ok, converted = _json_native(element, depth + 1)
            if not ok:
                return False, None
            out.append(converted)
        return True, out
    if isinstance(value, dict):
        out = {}
        for key, element in value.items():
            if not isinstance(key, str):
                return False, None
            ok, converted = _json_native(element, depth + 1)
            if not ok:
                return False, None
            out[key] = converted
        return True, out
    item = getattr(value, "item", None)
    if callable(item):
        try:
            coerced = value.item()
        except Exception:
            return False, None
        if coerced is None or isinstance(coerced, (str, int, float, bool)):
            return True, _native_scalar(coerced)
    return False, None


def _is_styler(value: Any) -> bool:
    return (hasattr(value, "data")
            and isinstance(getattr(value, "data", None), pd.DataFrame)
            and hasattr(value, "to_html"))


def _styled_html(styler) -> Optional[str]:
    """`styler.to_html()` within the Styler caps, else None."""
    try:
        frame = styler.data
        if frame.shape[0] > STYLED_MAX_ROWS or frame.shape[1] > STYLED_MAX_COLS:
            return None
        html = styler.to_html()
        if not isinstance(html, str) or not html.strip() or len(html) > STYLED_MAX_CHARS:
            return None
        return html
    except Exception as e:
        log_with_sid("exec", "warning",
                     f"EXEC_STYLER_HTML_FAILED: {log_safe_text(str(e))}")
        return None


def _key_spec(key: Any):
    """(key_type, wire key) for a dict RESULT key.

    str/int/float/bool keys keep their type; anything else becomes `str(key)`
    — which is what `_build_tables_from_result` titles with and what
    `_reexecute_full_df` looks up.
    """
    if isinstance(key, bool):
        return "bool", key
    if isinstance(key, int):
        return "int", int(key)
    if isinstance(key, float):
        return "float", float(key)
    if isinstance(key, str):
        return "str", key
    return "str", str(key)


def _serialize_value(value: Any, writer: _OutWriter, depth: int = 0) -> dict:
    if value is None:
        return {"kind": "none"}
    if isinstance(value, (str, int, float, bool)):
        return {"kind": "scalar", "value": _native_scalar(value)}
    if isinstance(value, np.generic):
        try:
            coerced = value.item()
        except Exception:
            coerced = None
        if isinstance(coerced, (str, int, float, bool)):
            return {"kind": "scalar", "value": _native_scalar(coerced)}
        return {"kind": "opaque", "type_name": type(value).__name__}
    if isinstance(value, pd.DataFrame):
        return {"kind": "frame", "ref": writer.write_frame(value)}
    if isinstance(value, pd.Series):
        return {"kind": "series", "ref": writer.write_frame(value.to_frame()),
                "unnamed": value.name is None}
    if _is_styler(value):
        return {"kind": "styler", "ref": writer.write_frame(value.data),
                "styled_html": _styled_html(value)}
    if isinstance(value, dict):
        if depth == 0:
            entries = []
            for key, item in value.items():
                key_type, wire_key = _key_spec(key)
                entries.append({"key": wire_key, "key_type": key_type,
                                "value": _serialize_value(item, writer, depth + 1)})
            return {"kind": "dict", "entries": entries}
        ok, converted = _json_native(value)
        if ok:
            return {"kind": "json", "value": converted}
        return {"kind": "opaque", "type_name": type(value).__name__}
    if isinstance(value, list):
        ok, converted = _json_native(value)
        if ok:
            return {"kind": "json", "value": converted}
        return {"kind": "opaque", "type_name": type(value).__name__}
    return {"kind": "opaque", "type_name": type(value).__name__}


def serialize_result(exec_return: dict, job_dir) -> dict:
    """Map a `safe_execute` / `render_plot_safe` return dict onto the wire.

    EVERY key of the original dict is copied verbatim — absences included
    (`is_plotly` missing on the matplotlib multi-axes error, no `ok` on the
    `safe_execute` shape, `error: None` on success) — except `result`,
    `preview` and the big strings, which become typed specs / file refs.
    """
    writer = _OutWriter(Path(job_dir))
    try:
        payload: dict = {}
        top_is_plotly = bool(exec_return.get("is_plotly")) if isinstance(exec_return, dict) else False
        for key, value in exec_return.items():
            if key == "result":
                payload[key] = _serialize_value(value, writer)
            elif key == "preview":
                payload[key] = encode_preview(value)
            elif key in ("image_base64", "image"):
                payload[key] = _serialize_blob(value, writer, "out/figure.png", not top_is_plotly)
            elif key == "plotly_html":
                payload[key] = _serialize_blob(value, writer, "out/chart.html", False)
            elif key == "multi_charts":
                payload[key] = _serialize_multi_charts(value, writer, top_is_plotly)
            else:
                payload[key] = value
        return payload
    except Exception as e:
        text = f"ResultSerializationError: {type(e).__name__}: {e}"
        log_with_sid("exec", "error",
                     f"EXEC_SERIALIZE_FAILED {log_safe_text(text)}")
        if isinstance(exec_return, dict) and ("ok" in exec_return or "trace" in exec_return):
            return {"ok": False, "error": text[:ERROR_MAX_CHARS], "trace": ""}
        return {"error": text[:ERROR_MAX_CHARS]}


def _serialize_blob(value: Any, writer: _OutWriter, rel: str, as_png: bool):
    if not isinstance(value, str) or len(value) <= INLINE_MAX_BYTES:
        return value
    return writer.write_blob(value, rel, as_png)


def _serialize_multi_charts(charts: Any, writer: _OutWriter, top_is_plotly: bool):
    if not isinstance(charts, list):
        return charts
    out = []
    for index, chart in enumerate(charts):
        if not isinstance(chart, dict):
            out.append(chart)
            continue
        entry = dict(chart)
        is_plotly = bool(entry.get("is_plotly", top_is_plotly))
        rel = f"out/chart_{index}." + ("html" if is_plotly else "png")
        entry["image"] = _serialize_blob(entry.get("image"), writer, rel, not is_plotly)
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# deserialization — the input here is hostile
# ---------------------------------------------------------------------------
_PYTHON_KEYS = ("error", "result", "preview", "image_base64")
_PLOT_KEYS = ("ok", "error", "trace", "multi_axes", "is_plotly", "image",
              "plotly_html", "multi_charts", "chart_data", "result")
_CHART_KEYS = ("image", "is_plotly", "title", "chart_data")
_MULTI_CHARTS_MAX = 32


def _exec_error(kind: str, text: str) -> dict:
    text = text[:ERROR_MAX_CHARS]
    if kind == "PLOT":
        return {"ok": False, "error": text, "trace": ""}
    return {"error": text}


def deserialize_result(response: Any, job_dir, kind: str, timeout_s=60,
                       sid: Optional[str] = None) -> dict:
    """Rebuild the dict the in-process callers expect, for EVERY status.

    Never raises (Article IV): a malformed, hostile or absent response is a
    failed execution, reported through the same `error` channel the callers
    already handle.

    `sid` is LOG-ONLY and optional: the two failure lines this function writes
    are the operator's record of a response it refused, and without it they
    read `sid=exec` — unattributable to the chat whose question produced the
    job. The dispatcher passes the one it was called with; a caller that has
    no session (a test, a script) keeps the old tag.
    """
    try:
        if not isinstance(response, dict):
            raise _Violation("the executor response is not an object")
        status = response.get("status")
        if status == "timeout":
            return _exec_error(kind, timeout_error_text(timeout_s))
        if status == "killed":
            return _exec_error(kind, MEMORY_ERROR_TEXT)
        if status == "crashed":
            # EVERY interpolated field is validated, not just `reason`.
            return _exec_error(kind, crash_error_text(
                _known_exit_value(response.get("exit_code")),
                _known_exit_value(response.get("signal")),
                known_reason(response.get("reason"))))
        if status not in ("ok", "error"):
            # The received value is NOT interpolated: it is the response's own
            # choice of text and this sentence reaches the planner's retry
            # prompt, where 40 000 characters or an embedded newline would be
            # an injection channel.
            raise _Violation("unknown status")
        payload = response.get("payload")
        if not isinstance(payload, dict):
            raise _Violation("the executor response carries no payload object")
        return _reconstruct(payload, Path(job_dir), kind)
    except _TooLarge as e:
        log_with_sid(sid or "exec", "error",
                     f"EXEC_RESPONSE_TOO_LARGE {log_safe_text(str(e))}")
        return _exec_error(kind, f"ResultTooLarge: {e}")
    except Exception as e:
        # `detail` exists only on this side of the boundary: the log keeps the
        # path an operator needs, the returned text (which reaches a prompt)
        # never gets it. Escaped because a filesystem error message is not
        # this process's own text and the log file is newline-delimited.
        detail = log_safe_text(getattr(e, "detail", None))
        # The MESSAGE is escaped for the same reason as `detail`, and it is
        # the one that matters most: these violation texts interpolate a raw
        # pyarrow/pandas exception (`result parquet is unreadable: ...`), and
        # pyarrow quotes the file's own metadata strings back — which the
        # sandbox wrote. A newline there forged a COMPLETE extra record,
        # timestamp, level, sid and event name included, into the file an
        # operator greps to reconstruct what happened.
        log_with_sid(sid or "exec", "error",
                     f"EXEC_RESPONSE_INVALID "
                     f"{log_safe_text(f'{type(e).__name__}: {e}')}",
                     detail=detail)
        return _exec_error(kind, f"ExecutorResponseError: {e}"
                           if isinstance(e, _Violation)
                           else f"ExecutorResponseError: {type(e).__name__}: {e}")


def _reconstruct(payload: dict, job_dir: Path, kind: str) -> dict:
    known = _PLOT_KEYS if kind == "PLOT" else _PYTHON_KEYS
    out: dict = {}
    for key, value in payload.items():
        if key not in known:
            continue
        if key in ("error", "trace"):
            out[key] = _as_error_text(value, key)
        elif key in ("ok", "multi_axes", "is_plotly"):
            if not isinstance(value, bool):
                raise _Violation(f"{key} is not a boolean")
            out[key] = value
        elif key == "preview":
            out[key] = decode_preview(value)
        elif key == "result":
            out[key] = _decode_value(value, job_dir)
        elif key in ("image_base64", "image"):
            out[key] = _decode_blob(value, job_dir)
        elif key == "plotly_html":
            out[key] = _decode_blob(value, job_dir)
        elif key == "chart_data":
            out[key] = _decode_chart_data(value)
        elif key == "multi_charts":
            out[key] = _decode_multi_charts(value, job_dir)
    return out


def _as_error_text(value: Any, key: str):
    if value is None:
        return None
    if not isinstance(value, str):
        raise _Violation(f"{key} is not a string")
    return value[:ERROR_MAX_CHARS]


def _decode_chart_data(value: Any):
    if not isinstance(value, dict):
        return None
    try:
        if len(dumps(value)) > CHART_DATA_MAX_BYTES:
            return None
    except Exception:
        return None
    return value


def _decode_multi_charts(value: Any, job_dir: Path):
    if not isinstance(value, list):
        raise _Violation("multi_charts is not a list")
    if len(value) > _MULTI_CHARTS_MAX:
        raise _TooLarge(f"multi_charts carries {len(value)} entries")
    charts = []
    for chart in value:
        if not isinstance(chart, dict):
            raise _Violation("a multi_charts entry is not an object")
        entry = {}
        for key, item in chart.items():
            if key not in _CHART_KEYS:
                continue
            if key == "image":
                entry[key] = _decode_blob(item, job_dir)
            elif key == "chart_data":
                entry[key] = _decode_chart_data(item)
            elif key == "is_plotly":
                if not isinstance(item, bool):
                    raise _Violation("multi_charts is_plotly is not a boolean")
                entry[key] = item
            else:
                if item is not None and not isinstance(item, str):
                    raise _Violation("multi_charts title is not a string")
                entry[key] = item
        charts.append(entry)
    return charts


def _decode_blob(value: Any, job_dir: Path):
    """An inline string, or a file reference read back to the same string."""
    if value is None or isinstance(value, str):
        return value
    if not isinstance(value, dict):
        raise _Violation("an image/html field is neither a string nor a reference")
    encoding = value.get("encoding")
    if encoding == "png-b64":
        handle, _st = _open_ref(job_dir, value.get("ref"), (".png",), REF_FILE_MAX_BYTES)
        with handle:
            return base64.b64encode(handle.read()).decode("ascii")
    if encoding == "utf8":
        handle, _st = _open_ref(job_dir, value.get("ref"), (".html",), REF_FILE_MAX_BYTES)
        with handle:
            return handle.read().decode("utf-8")
    raise _Violation(f"unknown reference encoding {encoding!r}")


def _decode_value(spec: Any, job_dir: Path):
    if not isinstance(spec, dict):
        raise _Violation("result is not a typed object")
    kind = spec.get("kind")
    if kind == "none":
        return None
    if kind == "scalar":
        value = spec.get("value")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise _Violation("result scalar is not a scalar")
        return value
    if kind == "json":
        value = spec.get("value")
        if not isinstance(value, (list, dict, str, int, float, bool)) and value is not None:
            raise _Violation("result json value has a non-JSON type")
        return value
    if kind == "opaque":
        name = spec.get("type_name")
        if not isinstance(name, str):
            raise _Violation("opaque result carries no type name")
        return Opaque(name)
    if kind == "frame":
        return _read_result_frame(job_dir, spec.get("ref"))
    if kind == "series":
        frame = _read_result_frame(job_dir, spec.get("ref"))
        if frame.shape[1] < 1:
            raise _Violation("series result carries no column")
        series = frame.iloc[:, 0]
        if spec.get("unnamed") is True:
            series = series.rename(None)
        return series
    if kind == "styler":
        frame = _read_result_frame(job_dir, spec.get("ref"))
        html = spec.get("styled_html")
        if html is not None and not isinstance(html, str):
            raise _Violation("styled_html is not a string")
        if isinstance(html, str) and len(html) > STYLED_MAX_CHARS:
            raise _TooLarge(f"styled_html is {len(html)} chars")
        return StyledFrame(frame, html)
    if kind == "dict":
        entries = spec.get("entries")
        if not isinstance(entries, list):
            raise _Violation("result dict carries no entries")
        out: dict = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise _Violation("a result dict entry is not an object")
            out[_decode_key(entry.get("key"), entry.get("key_type"))] = \
                _decode_value(entry.get("value"), job_dir)
        return out
    raise _Violation(f"unknown result kind {kind!r}")


def _decode_key(key: Any, key_type: Any):
    try:
        if key_type == "bool":
            return bool(key)
        if key_type == "int":
            return int(key)
        if key_type == "float":
            return float(key)
        if key_type == "str":
            return key if isinstance(key, str) else str(key)
    except Exception as e:
        raise _Violation(f"result dict key is not a {key_type}: {e}") from e
    raise _Violation(f"unknown result dict key type {key_type!r}")


def _open_ref(job_dir: Path, rel: Any, suffixes: tuple, max_bytes: Optional[int]):
    """Open `<job_dir>/out/<name>` safely and return (binary handle, stat).

    The executor writes into a directory the main app also mounts, so the
    reference is opened through a `dir_fd` chain with `O_NOFOLLOW` (a symlinked
    `out/` or file would otherwise resolve inside the main app's namespace,
    where DATA_ROOT IS mounted), `O_NONBLOCK` plus an `S_ISREG` check (a FIFO
    planted by the runner would block the web worker forever) and a size cap.
    On Windows (`O_NOFOLLOW` does not exist) an `lstat` chain does the same job.
    `.pkl` is never readable on this direction.
    """
    if not isinstance(rel, str) or not _REF_PATH_RE.fullmatch(rel):
        raise _Violation(f"reference outside out/: {rel!r}")
    name = rel.split("/", 1)[1]
    if name in (".", "..") or not name.endswith(suffixes):
        raise _Violation(f"reference is not one of {suffixes}: {rel!r}")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    binary = getattr(os, "O_BINARY", 0)

    if directory and os.open in getattr(os, "supports_dir_fd", set()):
        job_fd = _os_open(str(job_dir), os.O_RDONLY | directory | nofollow | cloexec,
                          what=f"{rel!r} (job directory)")
        try:
            out_fd = _os_open("out", os.O_RDONLY | directory | nofollow | cloexec,
                              dir_fd=job_fd, what=f"{rel!r} (out directory)")
        finally:
            os.close(job_fd)
        try:
            fd = _os_open(name, os.O_RDONLY | nofollow | nonblock | cloexec | binary,
                          dir_fd=out_fd, what=repr(rel))
        finally:
            os.close(out_fd)
        # ONE guard over everything that can raise while the descriptor is
        # open: with per-branch closes an `os.fstat` failure LEAKED the fd.
        # `os.fdopen` deliberately stays OUTSIDE it — CPython closes the fd
        # itself when the wrapper cannot be built, so guarding it here would
        # risk a double close.
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise _Violation(f"reference is not a regular file: {rel!r}")
            if max_bytes is not None and st.st_size > max_bytes:
                raise _Violation(f"reference is {st.st_size} bytes (cap {max_bytes})")
        except BaseException:
            os.close(fd)
            raise
        return os.fdopen(fd, "rb"), st

    out_dir = Path(job_dir) / "out"
    target = out_dir / name
    for component in (Path(job_dir), out_dir, target):
        try:
            st = os.lstat(component)
        except OSError as e:
            raise _Violation(f"reference not readable: {rel!r}: {type(e).__name__}",
                             detail=f"{component}: {e}") from e
        if stat.S_ISLNK(st.st_mode):
            raise _Violation(f"reference component is a symlink: {component.name}")
    if not stat.S_ISREG(st.st_mode):
        raise _Violation(f"reference is not a regular file: {rel!r}")
    if max_bytes is not None and st.st_size > max_bytes:
        raise _Violation(f"reference is {st.st_size} bytes (cap {max_bytes})")
    return open(target, "rb"), st


def _os_open(path, flags, dir_fd=None, what: Optional[str] = None):
    """`os.open`, with the failure reported WITHOUT the path it failed on.

    `what` names the reference and the stage of the chain (its own short
    `out/...` name is the sandbox's own token and safe to repeat); the
    absolute path and the OSError text go to `_Violation.detail`, i.e. to the
    log line only, because the message becomes the returned error text.
    """
    try:
        return os.open(path, flags, dir_fd=dir_fd)
    except OSError as e:
        raise _Violation(f"cannot open {what or 'the reference'}: {type(e).__name__}",
                         detail=f"{path!r}: {e}") from e


def _read_result_frame(job_dir: Path, rel: Any) -> pd.DataFrame:
    """Read a result frame as UNCOMPRESSED parquet inside every cap.

    Parquet metadata is written by the untrusted side and dictionary/RLE
    encoding inflates far beyond the stored size, so the codec field — the
    reader's own decompression instruction — must say UNCOMPRESSED, and the
    decoded batches are summed against a hard cap while they are read.
    """
    handle, st = _open_ref(job_dir, rel, (".parquet",), None)
    with handle:
        if st.st_size > RESULT_FILE_MAX_BYTES:
            raise _TooLarge(f"result parquet is {st.st_size} bytes")
        try:
            parquet_file = pq.ParquetFile(handle)
        except Exception as e:
            raise _Violation(f"result parquet is unreadable: {e}") from e
        meta = parquet_file.metadata
        for group in range(meta.num_row_groups):
            for column in range(meta.num_columns):
                codec = meta.row_group(group).column(column).compression
                if codec != "UNCOMPRESSED":
                    raise _TooLarge(f"result parquet column chunk is {codec}-compressed")
        if meta.num_rows > RESULT_MAX_ROWS:
            raise _TooLarge(f"result parquet has {meta.num_rows} rows")
        if meta.num_columns > RESULT_MAX_COLUMNS:
            raise _TooLarge(f"result parquet has {meta.num_columns} columns")
        batches = []
        decoded = 0
        try:
            for batch in parquet_file.iter_batches(batch_size=2048, use_threads=False):
                decoded += batch.nbytes
                if decoded > RESULT_MAX_DECODED_BYTES:
                    raise _TooLarge(f"result parquet decodes to more than "
                                    f"{RESULT_MAX_DECODED_BYTES} bytes")
                batches.append(batch)
            table = pa.Table.from_batches(batches, schema=parquet_file.schema_arrow)
            return table.to_pandas()
        except (_TooLarge, _Violation):
            raise
        except Exception as e:
            raise _Violation(f"result parquet is unreadable: {e}") from e
