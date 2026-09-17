"""The dispatch hop: `code_exec.safe_execute` / `plot_utils.render_plot_safe`
hand the job to the analysis-sandbox service over HTTP instead of running
`exec()` in the web process.

Three layers:

1. `executor_client.execute` as a unit — the request body, the read timeout,
   the job-directory lifecycle, and every failure mode mapped onto the two
   caller shapes. The transport is an `httpx.MockTransport` injected through
   the module seam `executor_client._TRANSPORT`.
2. End to end against the CAPTURED REAL responses in
   `tools/fixtures/executor_responses/` (see its README): a handler copies the
   case's `out/` into the job directory named in the request and replays the
   captured body verbatim, so the real public functions and the real chat
   orchestrator run against bytes the sandbox service actually produced. Those
   tests carry `real_executor_dispatch`, which turns OFF the suite-wide
   in-process rebinding, and each asserts its handler was invoked — otherwise
   a rebinding regression would let them pass without touching the transport.
3. Structural pins, AST-based: `exec()` lives only in the two functions the
   sandbox runner imports, only that runner reaches them, the dispatcher
   module is absent from the sandbox image's module-scope imports, and the
   transport's key allow-lists still match those functions' own returns.

Error texts that belong to the transport (`timeout_error_text`,
`MEMORY_ERROR_TEXT`, `crash_error_text`) are IMPORTED, never retyped: they are
forwarded verbatim to the planner's retry prompt.
"""
import ast
import base64
import json
import math
import os
import shutil
import threading
import time
from pathlib import Path

import httpx
import pandas as pd
import pytest

import code_exec
import exec_transport
import plot_utils
import run_chat_local
from settings import settings

try:  # red until the dispatcher module lands
    import executor_client
except ModuleNotFoundError as exc:  # pragma: no cover - the pre-implementation state
    executor_client = None
    _DISPATCHER_IMPORT_ERROR = exc
else:
    _DISPATCHER_IMPORT_ERROR = None

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tools" / "fixtures" / "executor_responses"

UNAVAILABLE_TEXT = "ExecutorUnavailable: the analysis service is not reachable"
BUSY_TEXT = "ExecutorBusy: the analysis service is busy, try again"
REJECTED_TEXT = "ExecutorError: the analysis service rejected the job ({code})"
PREPARE_TEXT = "ExecutorError: cannot prepare the job ({exc})"

JSON_HEADERS = {"content-type": "application/json"}
READ_TIMEOUT_MARGIN_S = 30
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
LOCAL_PLOTLY_URL = "/static/vendor/plotly/plotly.min.js"
CDN_PLOTLY_HOST = "cdn.plot.ly"

EXECUTOR_SETTING_NAMES = (
    "EXECUTOR_URL", "EXECUTOR_SHARED_DIR", "EXECUTOR_CONNECT_TIMEOUT",
    "EXECUTOR_PLOT_TIMEOUT_S", "EXECUTOR_MAX_CONCURRENT", "EXECUTOR_QUEUE_MAX_S",
)

# The files copied into the sandbox image (kept as a private literal — the
# suite's convention is not to import across test modules).
SANDBOX_IMAGE_MODULES = (
    "code_exec.py", "plot_utils.py", "exec_sanitizer.py", "sandbox_guard.py",
    "outlier_utils.py", "logger_utils.py", "settings.py", "exec_transport.py",
    "executor/__init__.py", "executor/app.py", "executor/runner.py",
)

CASE_NAMES = sorted(p.name for p in FIXTURES.iterdir() if p.is_dir()) if FIXTURES.is_dir() else []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _sales() -> pd.DataFrame:
    """The input frame the captured cases were produced from."""
    return pd.DataFrame({
        "city": ["Tbilisi", "Batumi", "Kutaisi", "Tbilisi"],
        "revenue": [100.75, 200.0, 0.0, 50.0],
    })


def _dfs() -> dict:
    return {"sales": _sales()}


def _case(name: str) -> dict:
    """One captured case: its metadata, the request that produced it and the
    verbatim response bytes."""
    directory = FIXTURES / name
    assert directory.is_dir(), f"missing captured case directory: {directory}"
    case = json.loads((directory / "case.json").read_text(encoding="utf-8"))
    case["directory"] = directory
    case["body"] = (directory / "response.json").read_bytes()
    case["request"] = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    return case


def _healthz_response() -> httpx.Response:
    import importlib

    versions = {}
    for name in ("matplotlib", "numpy", "pandas", "plotly", "pyarrow"):
        try:
            versions[name] = str(getattr(importlib.import_module(name), "__version__", ""))
        except Exception:
            versions[name] = ""
    body = {"ok": True, "version": "captured", "build_time": "", "versions": versions}
    return httpx.Response(200, content=exec_transport.dumps(body), headers=JSON_HEADERS)


def _recorder(calls: list):
    """Record one `/execute` request; returns the decoded body."""
    def record(request):
        body = exec_transport.loads(request.content)
        job_id = body.get("job_id")
        shared = _shared_dir()
        calls.append({
            "body": body,
            "timeout": dict(request.extensions.get("timeout") or {}),
            "url": str(request.url),
            "job_dir": str(Path(shared) / str(job_id)),
            "job_dir_exists": (Path(shared) / str(job_id)).is_dir(),
            "inputs": sorted(p.name for p in (Path(shared) / str(job_id) / "in").glob("*"))
            if (Path(shared) / str(job_id) / "in").is_dir() else [],
        })
        return body
    return record


def _replay_handler(case: dict, calls: list):
    """Serve `/healthz` and replay ONE captured case for `/execute`.

    The case's `out/` directory is copied into the job directory named in the
    request, because a `result` / `image` entry references `out/<file>`
    relative to it.
    """
    record = _recorder(calls)
    out_src = case["directory"] / "out"
    status = int(case.get("http_status", 200))

    def handler(request):
        if request.url.path.endswith("/healthz"):
            return _healthz_response()
        body = record(request)
        job_dir = Path(_shared_dir()) / str(body.get("job_id"))
        if out_src.is_dir():
            shutil.copytree(out_src, job_dir / "out", dirs_exist_ok=True)
        return httpx.Response(status, content=case["body"], headers=JSON_HEADERS)

    return handler


def _body_handler(body: dict, calls: list, status: int = 200):
    """Serve `/healthz` and one synthetic body for `/execute`."""
    record = _recorder(calls)

    def handler(request):
        if request.url.path.endswith("/healthz"):
            return _healthz_response()
        record(request)
        return httpx.Response(status, content=exec_transport.dumps(body), headers=JSON_HEADERS)

    return handler


def _raising_handler(error_factory, calls: list):
    record = _recorder(calls)

    def handler(request):
        if request.url.path.endswith("/healthz"):
            raise error_factory(request)
        record(request)
        raise error_factory(request)

    return handler


def _install(monkeypatch, handler) -> None:
    monkeypatch.setattr(executor_client, "_TRANSPORT", httpx.MockTransport(handler),
                        raising=False)


def _shared_dir() -> Path:
    """The same call-time resolution the dispatcher owes: the setting when it
    is set, `<DATA_ROOT>/exec_jobs` when it is empty."""
    configured = getattr(settings, "EXECUTOR_SHARED_DIR", "") or ""
    if configured:
        return Path(configured)
    return Path(settings.DATA_ROOT) / "exec_jobs"


def _job_dirs(shared: Path) -> list:
    return sorted(p.name for p in Path(shared).iterdir()
                  if p.is_dir() and exec_transport.valid_job_id(p.name))


def _reset_gate() -> None:
    """Rebuild the module-level dispatch semaphore from the current setting.

    The implementer must expose `executor_client._reset_gate()`: the gate has
    to exist before the first call, so its SIZE cannot be read at call time
    like every other setting, and a test that sizes it needs a documented way
    to have it rebuilt.
    """
    reset = getattr(executor_client, "_reset_gate", None)
    assert callable(reset), ("executor_client must expose _reset_gate() so the "
                            "dispatch gate can be re-sized deterministically")
    reset()


def _assert_error_shape(out, kind: str, text: str) -> None:
    assert isinstance(out, dict), out
    if kind == "PLOT":
        assert out == {"ok": False, "error": text, "trace": ""}, out
    else:
        assert out == {"error": text}, out


def _synthetic_response(payload, kind="PYTHON", status="ok", **extra) -> dict:
    body = {
        "status": status,
        "kind": kind,
        "payload": payload,
        "elapsed_ms": 11,
        "peak_rss_mb": 99.5,
        "stdout": "",
        "stderr": "",
        "traceback": "",
        "exit_code": 0,
        "signal": None,
        "reason": None,
    }
    body.update(extra)
    return body


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def dispatcher():
    assert executor_client is not None, (
        f"executor_client is not importable: {_DISPATCHER_IMPORT_ERROR!r}")
    return executor_client


@pytest.fixture
def exec_env(dispatcher, tmp_path, monkeypatch):
    """An isolated shared job directory plus the analysis-sandbox settings.

    Everything lands under `tmp_path`; `DATA_ROOT` is redirected too so a
    call-time default can never reach the real data root.
    """
    missing = [name for name in EXECUTOR_SETTING_NAMES if not hasattr(settings, name)]
    assert not missing, f"settings lacks the analysis-sandbox fields: {missing}"

    data_root = tmp_path / "data"
    data_root.mkdir()
    shared = tmp_path / "jobs"
    shared.mkdir()
    monkeypatch.setattr(settings, "DATA_ROOT", str(data_root))
    monkeypatch.setattr(settings, "EXECUTOR_SHARED_DIR", str(shared))
    monkeypatch.setattr(settings, "EXECUTOR_URL", "http://pdc-executor:8090")
    monkeypatch.setattr(settings, "EXECUTOR_CONNECT_TIMEOUT", 5.0)
    monkeypatch.setattr(settings, "EXECUTOR_PLOT_TIMEOUT_S", 120)
    monkeypatch.setattr(settings, "EXECUTOR_MAX_CONCURRENT", 1)
    monkeypatch.setattr(settings, "EXECUTOR_QUEUE_MAX_S", 600)
    monkeypatch.setattr(dispatcher, "_TRANSPORT", None, raising=False)
    _reset_gate()
    yield shared
    _reset_gate()


@pytest.fixture(autouse=True)
def _trivial_schema(monkeypatch):
    """The planner is stubbed in every chat-level test here."""
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")


# ===========================================================================
# 1. the dispatcher as a unit
# ===========================================================================
def test_request_body_carries_the_job_id_kind_manifest_and_options(dispatcher, exec_env,
                                                                   tmp_path, monkeypatch):
    calls = []
    _install(monkeypatch, _replay_handler(_case("python_scalar"), calls))

    out = dispatcher.execute("PYTHON", "RESULT = 1", _dfs(), sid="t", timeout_s=60)

    assert len(calls) == 1, calls
    body = calls[0]["body"]
    job_id = body.get("job_id")
    assert isinstance(job_id, str) and exec_transport.JOB_ID_RE.match(job_id), job_id
    kind = body.get("kind")
    assert kind == "PYTHON", body
    code = body.get("code")
    assert code == "RESULT = 1", body
    options = body.get("options")
    assert options == {"split_multi_axes": False}, body
    assert isinstance(out, dict), out

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    expected_manifest = exec_transport.write_inputs(
        _dfs(), exec_transport.create_job_dir(scratch, exec_transport.new_job_id()), sid="t")
    manifest = body.get("dataframes")
    assert manifest == expected_manifest, (manifest, expected_manifest)
    written = calls[0]["inputs"]
    assert written == ["0.parquet"], written


def test_an_integral_timeout_stays_an_int_on_the_wire(dispatcher, exec_env, monkeypatch):
    calls = []
    _install(monkeypatch, _replay_handler(_case("python_scalar"), calls))
    dispatcher.execute("PYTHON", "RESULT = 1", _dfs(), sid="t", timeout_s=60.0)
    posted = calls[0]["body"].get("timeout_s")
    assert posted == 60 and isinstance(posted, int), repr(posted)


def test_a_fractional_timeout_keeps_its_float_rendering(dispatcher, exec_env, monkeypatch):
    calls = []
    _install(monkeypatch, _replay_handler(_case("python_scalar"), calls))
    dispatcher.execute("PYTHON", "RESULT = 1", _dfs(), sid="t", timeout_s=2.5)
    posted = calls[0]["body"].get("timeout_s")
    assert posted == 2.5 and isinstance(posted, float), repr(posted)


def test_split_multi_axes_rides_in_the_options_block(dispatcher, exec_env, monkeypatch):
    calls = []
    _install(monkeypatch, _replay_handler(_case("plot_multi_charts"), calls))
    dispatcher.execute("PLOT", "plot", _dfs(), sid="t", timeout_s=120, split_multi_axes=True)
    options = calls[0]["body"].get("options")
    assert options == {"split_multi_axes": True}, calls[0]["body"]


@pytest.mark.parametrize("timeout_s", [3, 60, 120])
def test_the_http_read_timeout_is_the_job_budget_plus_the_margin(dispatcher, exec_env,
                                                                 monkeypatch, timeout_s):
    """The sandbox kills at `timeout_s + 15`; the HTTP read must outlast that
    so a job that answers late is read, not reported as unreachable."""
    calls = []
    _install(monkeypatch, _replay_handler(_case("python_scalar"), calls))
    dispatcher.execute("PYTHON", "RESULT = 1", _dfs(), sid="t", timeout_s=timeout_s)
    timeout = calls[0]["timeout"]
    read = timeout.get("read")
    assert read == timeout_s + READ_TIMEOUT_MARGIN_S, timeout
    connect = timeout.get("connect")
    assert connect == settings.EXECUTOR_CONNECT_TIMEOUT, timeout


def test_the_response_body_is_decoded_in_the_wire_dialect_so_nan_survives(dispatcher, exec_env,
                                                                          monkeypatch):
    """The captured body carries the literal `NaN` token, which a strict JSON
    decoder rejects and `_safe_preview` forwards as-is today."""
    case = _case("python_nan_preview")
    assert b"NaN" in case["body"], case["body"][:200]
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))

    out = dispatcher.execute("PYTHON", case["code"], _dfs(), sid="t", timeout_s=60)

    preview = out.get("preview")
    assert isinstance(preview, float) and math.isnan(preview), repr(preview)
    result = out.get("result")
    assert isinstance(result, float) and math.isnan(result), repr(result)


def test_the_posted_url_is_the_execute_endpoint_of_the_configured_service(dispatcher, exec_env,
                                                                          monkeypatch):
    calls = []
    _install(monkeypatch, _replay_handler(_case("python_scalar"), calls))
    dispatcher.execute("PYTHON", "RESULT = 1", _dfs(), sid="t", timeout_s=60)
    url = calls[0]["url"]
    assert url == "http://pdc-executor:8090/execute", url


@pytest.mark.parametrize("kind", ["PYTHON", "PLOT"])
def test_the_job_directory_exists_with_its_inputs_while_the_job_runs(dispatcher, exec_env,
                                                                     monkeypatch, kind):
    calls = []
    case = _case("python_scalar" if kind == "PYTHON" else "plot_matplotlib")
    _install(monkeypatch, _replay_handler(case, calls))
    dispatcher.execute(kind, case["code"], _dfs(), sid="t", timeout_s=60)
    existed = calls[0]["job_dir_exists"]
    assert existed is True, calls[0]
    inputs = calls[0]["inputs"]
    assert inputs == ["0.parquet"], calls[0]


@pytest.mark.parametrize("case_name", ["python_scalar", "python_error", "python_timeout"])
def test_the_job_directory_is_removed_after_a_served_response(dispatcher, exec_env,
                                                              monkeypatch, case_name):
    calls = []
    case = _case(case_name)
    _install(monkeypatch, _replay_handler(case, calls))
    dispatcher.execute("PYTHON", case["code"], _dfs(), sid="t",
                       timeout_s=case.get("timeout_s", 60))
    left = _job_dirs(exec_env)
    assert left == [], left
    assert not Path(calls[0]["job_dir"]).exists(), calls[0]["job_dir"]


def test_the_job_directory_is_removed_after_a_rejected_request(dispatcher, exec_env, monkeypatch):
    calls = []
    _install(monkeypatch, _body_handler({"code": "JOB_DIR_INVALID", "message": "no"},
                                        calls, status=400))
    out = dispatcher.execute("PYTHON", "RESULT = 1", _dfs(), sid="t", timeout_s=60)
    _assert_error_shape(out, "PYTHON", REJECTED_TEXT.format(code="JOB_DIR_INVALID"))
    left = _job_dirs(exec_env)
    assert left == [], left


def test_the_job_directory_is_removed_after_a_connect_failure(dispatcher, exec_env, monkeypatch):
    calls = []
    _install(monkeypatch, _raising_handler(
        lambda request: httpx.ConnectError("connection refused", request=request), calls))
    out = dispatcher.execute("PYTHON", "RESULT = 1", _dfs(), sid="t", timeout_s=60)
    _assert_error_shape(out, "PYTHON", UNAVAILABLE_TEXT)
    left = _job_dirs(exec_env)
    assert left == [], left


@pytest.mark.parametrize("kind", ["PYTHON", "PLOT"])
def test_the_job_directory_is_removed_when_reconstruction_itself_explodes(dispatcher, exec_env,
                                                                          monkeypatch, kind):
    """`deserialize_result` is written never to raise; if it ever does, the
    dispatcher must still answer with an error dict and still clean up."""
    calls = []
    case = _case("python_scalar" if kind == "PYTHON" else "plot_matplotlib")
    _install(monkeypatch, _replay_handler(case, calls))

    def boom(*a, **k):
        raise RuntimeError("reconstruction blew up")

    monkeypatch.setattr(exec_transport, "deserialize_result", boom)
    if hasattr(dispatcher, "deserialize_result"):
        monkeypatch.setattr(dispatcher, "deserialize_result", boom)

    out = dispatcher.execute(kind, case["code"], _dfs(), sid="t", timeout_s=60)

    assert isinstance(out, dict), out
    error = out.get("error")
    assert isinstance(error, str) and error, out
    # Pinned, not merely "some error": this is one of the fixed failure texts
    # the contract lists, it names only the exception TYPE (no path, no URL,
    # no datum), and it reaches the planner's retry prompt.
    assert error == "ExecutorError: the analysis answer could not be read (RuntimeError)", error
    if kind == "PLOT":
        assert out.get("ok") is False, out
    left = _job_dirs(exec_env)
    assert left == [], left


@pytest.mark.parametrize("kind", ["PYTHON", "PLOT"])
def test_an_input_write_failure_is_reported_as_a_preparation_error(dispatcher, exec_env,
                                                                   monkeypatch, kind):
    calls = []
    _install(monkeypatch, _replay_handler(_case("python_scalar"), calls))

    def boom(dfs, job_dir, sid=None):
        raise ValueError("cannot transport dataframe 'sales'")

    monkeypatch.setattr(exec_transport, "write_inputs", boom)
    if hasattr(dispatcher, "write_inputs"):
        monkeypatch.setattr(dispatcher, "write_inputs", boom)

    out = dispatcher.execute(kind, "RESULT = 1", _dfs(), sid="t", timeout_s=60)

    _assert_error_shape(out, kind, PREPARE_TEXT.format(exc="ValueError"))
    assert calls == [], "nothing may be posted when the inputs could not be written"
    left = _job_dirs(exec_env)
    assert left == [], left


@pytest.mark.parametrize("kind", ["PYTHON", "PLOT"])
@pytest.mark.parametrize("status_code,code", [(400, "JOB_DIR_INVALID"),
                                              (503, "EXECUTOR_NOT_READY")])
def test_a_non_200_answer_names_the_service_code(dispatcher, exec_env, monkeypatch,
                                                 kind, status_code, code):
    calls = []
    _install(monkeypatch, _body_handler({"code": code, "message": "refused"},
                                        calls, status=status_code))
    out = dispatcher.execute(kind, "RESULT = 1", _dfs(), sid="t", timeout_s=60)
    _assert_error_shape(out, kind, REJECTED_TEXT.format(code=code))


@pytest.mark.parametrize("kind", ["PYTHON", "PLOT"])
@pytest.mark.parametrize("failure", ["connect", "read_timeout"])
def test_an_unreachable_service_is_a_generic_unavailable_answer(dispatcher, exec_env,
                                                                monkeypatch, kind, failure):
    """The text reaches the planner's retry prompt, so it must carry no URL."""
    calls = []
    if failure == "connect":
        factory = lambda request: httpx.ConnectError("refused", request=request)  # noqa: E731
    else:
        factory = lambda request: httpx.ReadTimeout("read timed out", request=request)  # noqa: E731
    _install(monkeypatch, _raising_handler(factory, calls))

    out = dispatcher.execute(kind, "RESULT = 1", _dfs(), sid="t", timeout_s=60)

    _assert_error_shape(out, kind, UNAVAILABLE_TEXT)
    text = out.get("error")
    assert "pdc-executor" not in text and "http" not in text, text


@pytest.mark.parametrize("kind", ["PYTHON", "PLOT"])
def test_a_timeout_status_maps_to_the_transports_own_timeout_text(dispatcher, exec_env,
                                                                  monkeypatch, kind):
    calls = []
    _install(monkeypatch, _body_handler(
        _synthetic_response({"error": "ignored"}, kind=kind, status="timeout"), calls))
    out = dispatcher.execute(kind, "while True: pass", _dfs(), sid="t", timeout_s=30)
    _assert_error_shape(out, kind, exec_transport.timeout_error_text(30))


@pytest.mark.parametrize("kind", ["PYTHON", "PLOT"])
def test_a_killed_status_maps_to_the_memory_error_text(dispatcher, exec_env, monkeypatch, kind):
    calls = []
    _install(monkeypatch, _body_handler(
        _synthetic_response(None, kind=kind, status="killed"), calls))
    out = dispatcher.execute(kind, "RESULT = 1", _dfs(), sid="t", timeout_s=60)
    _assert_error_shape(out, kind, exec_transport.MEMORY_ERROR_TEXT)


@pytest.mark.parametrize("kind", ["PYTHON", "PLOT"])
def test_a_crashed_status_maps_to_the_crash_text_with_its_details(dispatcher, exec_env,
                                                                  monkeypatch, kind):
    calls = []
    _install(monkeypatch, _body_handler(
        _synthetic_response(None, kind=kind, status="crashed",
                            exit_code=-11, signal=11, reason="segfault"), calls))
    out = dispatcher.execute(kind, "RESULT = 1", _dfs(), sid="t", timeout_s=60)
    _assert_error_shape(out, kind, exec_transport.crash_error_text(-11, 11, "segfault"))


@pytest.mark.parametrize("kind", ["PYTHON", "PLOT"])
def test_an_ok_status_returns_the_reconstructed_caller_dict(dispatcher, exec_env,
                                                            monkeypatch, kind):
    calls = []
    if kind == "PYTHON":
        payload = {"error": None, "result": {"kind": "scalar", "value": 7},
                   "preview": 7, "image_base64": None}
    else:
        payload = {"ok": True, "image": None, "is_plotly": False, "chart_data": None}
    _install(monkeypatch, _body_handler(_synthetic_response(payload, kind=kind), calls))

    out = dispatcher.execute(kind, "RESULT = 7", _dfs(), sid="t", timeout_s=60)

    if kind == "PYTHON":
        assert out == {"error": None, "result": 7, "preview": 7, "image_base64": None}, out
    else:
        assert out == {"ok": True, "image": None, "is_plotly": False, "chart_data": None}, out


def test_error_shape_is_the_per_kind_failure_dict(dispatcher):
    python_shape = dispatcher._error_shape("PYTHON", "boom")
    assert python_shape == {"error": "boom"}, python_shape
    plot_shape = dispatcher._error_shape("PLOT", "boom")
    assert plot_shape == {"ok": False, "error": "boom", "trace": ""}, plot_shape


def test_the_shared_directory_resolves_under_data_root_at_call_time(dispatcher, exec_env,
                                                                    tmp_path, monkeypatch):
    """The setting defaults to empty on purpose: the whole suite redirects
    `DATA_ROOT`, and a path computed from the environment at import time would
    ignore every one of those redirections."""
    calls = []
    monkeypatch.setattr(settings, "EXECUTOR_SHARED_DIR", "")
    data_root = Path(settings.DATA_ROOT)
    expected = data_root / "exec_jobs"
    _install(monkeypatch, _replay_handler(_case("python_scalar"), calls))

    dispatcher.startup()
    assert expected.is_dir(), f"{expected} was not created"

    dispatcher.execute("PYTHON", "RESULT = 1", _dfs(), sid="t", timeout_s=60)

    job_dir = Path(calls[0]["job_dir"])
    assert job_dir.parent == expected, (job_dir, expected)
    assert calls[0]["job_dir_exists"] is True, calls[0]
    assert not job_dir.exists(), job_dir


def test_startup_never_raises_when_the_service_is_unreachable(dispatcher, exec_env, monkeypatch):
    calls = []
    _install(monkeypatch, _raising_handler(
        lambda request: httpx.ConnectError("refused", request=request), calls))
    assert dispatcher.startup() is None


def test_sweep_orphans_removes_only_stale_job_shaped_directories(dispatcher, exec_env):
    stale = exec_transport.create_job_dir(exec_env, exec_transport.new_job_id())
    fresh = exec_transport.create_job_dir(exec_env, exec_transport.new_job_id())
    keep = exec_env / "keep_me"
    keep.mkdir()
    two_hours_ago = time.time() - 2 * 3600
    for path in (stale, keep):
        os.utime(path, (two_hours_ago, two_hours_ago))

    dispatcher.sweep_orphans()

    assert not stale.exists(), "a job directory older than an hour survived the sweep"
    assert fresh.is_dir(), "a fresh job directory was swept"
    assert keep.is_dir(), "a directory that is not job-id shaped was swept"


# ===========================================================================
# 2. the dispatch gate
# ===========================================================================
def test_only_one_dispatch_reaches_the_service_at_a_time(dispatcher, exec_env, monkeypatch):
    """With the cap at one, the second caller waits for the slot instead of
    queueing inside the sandbox — and still gets its FULL budget, because the
    clock starts when the slot is acquired."""
    monkeypatch.setattr(settings, "EXECUTOR_MAX_CONCURRENT", 1)
    monkeypatch.setattr(settings, "EXECUTOR_QUEUE_MAX_S", 60)
    _reset_gate()

    case = _case("python_scalar")
    arrived = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    state = {"inflight": 0, "max_inflight": 0}
    reads = []

    def handler(request):
        if request.url.path.endswith("/healthz"):
            return _healthz_response()
        with lock:
            state["inflight"] += 1
            state["max_inflight"] = max(state["max_inflight"], state["inflight"])
            reads.append((request.extensions.get("timeout") or {}).get("read"))
        arrived.set()
        release.wait(30)
        with lock:
            state["inflight"] -= 1
        return httpx.Response(200, content=case["body"], headers=JSON_HEADERS)

    _install(monkeypatch, handler)

    results = {}

    def _dispatch(tag):
        results[tag] = dispatcher.execute("PYTHON", "RESULT = 1", _dfs(),
                                          sid="t", timeout_s=60)

    first = threading.Thread(target=_dispatch, args=("first",), daemon=True)
    first.start()
    assert arrived.wait(20), "the first dispatch never reached the transport"
    second = threading.Thread(target=_dispatch, args=("second",), daemon=True)
    second.start()
    time.sleep(0.6)
    arrivals_while_blocked = len(reads)
    release.set()
    first.join(30)
    second.join(30)

    assert arrivals_while_blocked == 1, (
        f"{arrivals_while_blocked} dispatches reached the service while one held the slot")
    assert state["max_inflight"] == 1, state
    assert len(reads) == 2, reads
    assert reads == [60 + READ_TIMEOUT_MARGIN_S, 60 + READ_TIMEOUT_MARGIN_S], reads
    assert results.get("first", {}).get("result") == 350.75, results
    assert results.get("second", {}).get("result") == 350.75, results


@pytest.mark.parametrize("kind", ["PYTHON", "PLOT"])
def test_waiting_past_the_queue_cap_is_a_busy_answer_and_creates_nothing(dispatcher, exec_env,
                                                                         monkeypatch, kind):
    """A caller that never got the slot must not be told its code was too
    slow: the planner's retry prompt would then try to optimise a queue."""
    monkeypatch.setattr(settings, "EXECUTOR_MAX_CONCURRENT", 1)
    monkeypatch.setattr(settings, "EXECUTOR_QUEUE_MAX_S", 0.25)
    _reset_gate()

    case = _case("python_scalar")
    arrived = threading.Event()
    release = threading.Event()
    calls = []

    def handler(request):
        if request.url.path.endswith("/healthz"):
            return _healthz_response()
        calls.append(exec_transport.loads(request.content).get("job_id"))
        arrived.set()
        release.wait(30)
        return httpx.Response(200, content=case["body"], headers=JSON_HEADERS)

    _install(monkeypatch, handler)

    holder = threading.Thread(
        target=lambda: dispatcher.execute("PYTHON", "RESULT = 1", _dfs(), sid="hold",
                                          timeout_s=60),
        daemon=True)
    holder.start()
    assert arrived.wait(20), "the holding dispatch never reached the transport"
    before = _job_dirs(exec_env)

    started = time.monotonic()
    out = dispatcher.execute(kind, "RESULT = 1", _dfs(), sid="t", timeout_s=60)
    waited = time.monotonic() - started

    after = _job_dirs(exec_env)
    release.set()
    holder.join(30)

    _assert_error_shape(out, kind, BUSY_TEXT)
    text = out.get("error")
    assert "TimeoutError" not in text, text
    assert after == before, (before, after)
    assert len(calls) == 1, calls
    assert waited < 15, waited


# ===========================================================================
# 3. end to end against the captured responses
# ===========================================================================
def _run_public(case: dict) -> dict:
    """Drive the REAL public function for a captured case."""
    split = bool((case.get("request") or {}).get("options", {}).get("split_multi_axes"))
    if case["kind"] == "PYTHON":
        return code_exec.safe_execute(case["code"], _dfs(), sid="t",
                                      timeout=case.get("timeout_s"))
    return plot_utils.render_plot_safe(case["code"], _dfs(), "t", split_multi_axes=split)


@pytest.mark.real_executor_dispatch
@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_every_captured_case_reconstructs_exactly_its_own_key_set(exec_env, monkeypatch,
                                                                  case_name):
    """The ABSENCES are part of the contract: callers tell the two producers
    apart by which keys exist (no `ok` on the analysis shape, no `is_plotly`
    on the matplotlib multi-axes refusal)."""
    case = _case(case_name)
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))

    out = _run_public(case)

    assert len(calls) >= 1, "the captured response was never requested"
    keys = sorted(out)
    expected = sorted(case["payload_keys"]) if case["status"] != "timeout" else ["error"]
    assert keys == expected, (keys, expected)


@pytest.mark.real_executor_dispatch
def test_a_dataframe_result_comes_back_as_a_real_dataframe(exec_env, monkeypatch):
    case = _case("python_frame")
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))

    out = _run_public(case)

    assert len(calls) >= 1, calls
    result = out.get("result")
    assert isinstance(result, pd.DataFrame), type(result)
    columns = list(result.columns)
    assert columns == ["city", "revenue"], columns
    rows = len(result)
    assert rows == 3, rows
    assert out.get("error") is None, out


@pytest.mark.real_executor_dispatch
def test_a_dataframe_preview_never_crosses_the_data_boundary(exec_env, monkeypatch):
    """The guard's verdict on a table preview is unchanged by the hop."""
    case = _case("python_frame")
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))

    out = _run_public(case)

    assert len(calls) >= 1, calls
    guarded = run_chat_local._safe_preview(out.get("preview"))
    assert guarded is None, repr(guarded)


@pytest.mark.real_executor_dispatch
def test_a_scalar_result_is_inline(exec_env, monkeypatch):
    case = _case("python_scalar")
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))

    out = _run_public(case)

    assert len(calls) >= 1, calls
    result = out.get("result")
    assert result == 350.75, result
    preview = out.get("preview")
    assert preview == 350.75, preview


@pytest.mark.real_executor_dispatch
def test_only_the_styler_entry_of_a_dict_result_carries_rendered_html(exec_env, monkeypatch):
    case = _case("python_dict_styler")
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))

    out = _run_public(case)

    assert len(calls) >= 1, calls
    result = out.get("result")
    assert isinstance(result, dict), type(result)
    keys = list(result)
    assert keys == ["by city", "styled"], keys
    plain = result["by city"]
    assert isinstance(plain, pd.DataFrame), type(plain)
    assert not isinstance(plain, exec_transport.StyledFrame), type(plain)
    styled = result["styled"]
    assert isinstance(styled, exec_transport.StyledFrame), type(styled)
    html = styled.to_html()
    assert html and "<table" in html, (html or "")[:200]
    assert isinstance(styled.data, pd.DataFrame), type(styled.data)


@pytest.mark.real_executor_dispatch
def test_a_nan_scalar_round_trips_as_nan(exec_env, monkeypatch):
    case = _case("python_nan_preview")
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))

    out = _run_public(case)

    assert len(calls) >= 1, calls
    preview = out.get("preview")
    assert isinstance(preview, float) and math.isnan(preview), repr(preview)


@pytest.mark.real_executor_dispatch
def test_an_execution_error_arrives_as_the_error_alone(exec_env, monkeypatch):
    case = _case("python_error")
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))

    out = _run_public(case)

    assert len(calls) >= 1, calls
    assert out == {"error": "KeyError: 'nope'"}, out


@pytest.mark.real_executor_dispatch
def test_a_timeout_keeps_the_byte_identical_error_text(exec_env, monkeypatch):
    case = _case("python_timeout")
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))

    out = _run_public(case)

    assert len(calls) >= 1, calls
    expected = exec_transport.timeout_error_text(case["timeout_s"])
    assert out == {"error": expected}, (out, expected)
    assert "3.0 seconds" not in out["error"], out


@pytest.mark.real_executor_dispatch
def test_a_plotly_chart_references_the_local_bundle_and_never_the_cdn(exec_env, monkeypatch):
    case = _case("plot_plotly")
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))

    out = _run_public(case)

    assert len(calls) >= 1, calls
    assert out.get("ok") is True, out
    assert out.get("is_plotly") is True, out
    html = out.get("plotly_html") or ""
    assert LOCAL_PLOTLY_URL in html, html[:300]
    assert CDN_PLOTLY_HOST not in html, html[:300]


@pytest.mark.real_executor_dispatch
def test_a_matplotlib_chart_decodes_to_a_png(exec_env, monkeypatch):
    case = _case("plot_matplotlib")
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))

    out = _run_public(case)

    assert len(calls) >= 1, calls
    assert out.get("ok") is True, out
    assert out.get("is_plotly") is False, out
    image = out.get("image")
    assert isinstance(image, str) and image, type(image)
    head = base64.b64decode(image)[:8]
    assert head == PNG_MAGIC, head


@pytest.mark.real_executor_dispatch
def test_the_matplotlib_multi_axes_refusal_has_no_is_plotly_key(exec_env, monkeypatch):
    """The absence is how callers tell the two producers apart."""
    case = _case("plot_multi_axes_error")
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))

    out = _run_public(case)

    assert len(calls) >= 1, calls
    assert "is_plotly" not in out, sorted(out)
    assert out.get("ok") is False, out
    assert out.get("multi_axes") is True, out
    assert out.get("error", "").startswith("MultiAxesChartError:"), out


@pytest.mark.real_executor_dispatch
def test_splitting_a_two_axes_figure_yields_two_charts(exec_env, monkeypatch):
    case = _case("plot_multi_charts")
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))

    out = _run_public(case)

    assert len(calls) >= 1, calls
    charts = out.get("multi_charts")
    assert isinstance(charts, list), type(charts)
    count = len(charts)
    assert count == 2, count
    images = [bool(c.get("image")) for c in charts]
    assert images == [True, True], images


# ===========================================================================
# 4. end to end through the chat orchestrator
# ===========================================================================
def _stub_plan(monkeypatch, kind, code, raw_text=""):
    monkeypatch.setattr(run_chat_local.brain_client, "plan", lambda **k: {
        "raw_text": raw_text, "kind": kind, "code": code, "usage": {},
        "context_decision": {}})


def _stub_brain_text(monkeypatch):
    monkeypatch.setattr(run_chat_local.brain_client, "describe",
                        lambda **k: {"text": "described", "usage": {}})
    monkeypatch.setattr(run_chat_local.brain_client, "summarize",
                        lambda **k: {"text": "summarized", "usage": {}})
    monkeypatch.setattr(run_chat_local.brain_client, "retry",
                        lambda **k: {"kind": "NO_CODE", "code": "", "usage": {}})


def _plot_plan_text(code: str) -> str:
    return f"```plot_code\n{code}\n```\n###NEXT_PLOT###\n"


@pytest.mark.real_executor_dispatch
def test_run_chat_delivers_a_table_answer_through_the_dispatcher(exec_env, monkeypatch):
    case = _case("python_frame")
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))
    _stub_plan(monkeypatch, "PYTHON", case["code"])
    _stub_brain_text(monkeypatch)

    out = run_chat_local.run_chat(
        sid="t", dfs=_dfs(), schema_docs={}, question="revenue by city",
        history_rows=[], user_email="alice@acme.com")

    assert len(calls) >= 1, "the answer did not go through the transport"
    table = out.get("table")
    assert isinstance(table, dict), out
    columns = table.get("columns")
    assert columns == ["city", "revenue"], table
    total = table.get("total_rows")
    assert total == 3, table
    assert out.get("text") == "described", out


@pytest.mark.real_executor_dispatch
def test_run_chat_multi_plot_delivers_a_styler_answer_through_the_dispatcher(exec_env,
                                                                            monkeypatch):
    case = _case("python_dict_styler")
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))
    _stub_plan(monkeypatch, "PYTHON", case["code"])
    _stub_brain_text(monkeypatch)

    events = list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=_dfs(), schema_docs={}, question="styled revenue",
        history_rows=[], user_email="alice@acme.com"))

    assert len(calls) >= 1, "the answer did not go through the transport"
    assert len(events) == 1 and events[0].get("single_response"), events
    result = events[0]["result"]
    tables = result.get("tables")
    assert isinstance(tables, list) and len(tables) == 2, result
    titles = [t.get("title") for t in tables]
    assert titles == ["by city", "styled"], titles
    styled_html = tables[1].get("styled_html")
    assert styled_html and "<table" in styled_html, (styled_html or "")[:200]
    assert tables[0].get("styled_html") in (None, ""), tables[0].get("styled_html")


@pytest.mark.real_executor_dispatch
def test_run_chat_multi_plot_delivers_a_plotly_chart_through_the_dispatcher(exec_env,
                                                                           monkeypatch):
    case = _case("plot_plotly")
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))
    _stub_plan(monkeypatch, "PLOT_CODE", "", raw_text=_plot_plan_text(case["code"]))
    _stub_brain_text(monkeypatch)

    events = list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=_dfs(), schema_docs={}, question="chart revenue by city",
        history_rows=[], user_email="alice@acme.com"))

    assert len(calls) >= 1, "the chart did not go through the transport"
    partials = [e for e in events if e.get("partial")]
    assert len(partials) == 1, events
    rendered = partials[0].get("image_base64") or ""
    assert LOCAL_PLOTLY_URL in rendered, rendered[:300]
    assert CDN_PLOTLY_HOST not in rendered, rendered[:300]


@pytest.mark.real_executor_dispatch
def test_run_chat_multi_plot_delivers_a_matplotlib_chart_through_the_dispatcher(exec_env,
                                                                               monkeypatch):
    case = _case("plot_matplotlib")
    calls = []
    _install(monkeypatch, _replay_handler(case, calls))
    _stub_plan(monkeypatch, "PLOT_CODE", "", raw_text=_plot_plan_text(case["code"]))
    _stub_brain_text(monkeypatch)

    events = list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=_dfs(), schema_docs={}, question="bar revenue by city",
        history_rows=[], user_email="alice@acme.com"))

    assert len(calls) >= 1, "the chart did not go through the transport"
    partials = [e for e in events if e.get("partial")]
    assert len(partials) == 1, events
    rendered = partials[0].get("image_base64") or ""
    head = base64.b64decode(rendered)[:8]
    assert head == PNG_MAGIC, head


# ===========================================================================
# 5. structural pins (AST — a comment mentioning exec() must not count)
# ===========================================================================
SKIP_DIR_PARTS = {".venv", "venv", "__pycache__", "tests", ".git", "node_modules",
                  "static", "docs", "logs", "build", "dist"}


def _python_sources():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIR_PARTS for part in rel.parts):
            continue
        yield path


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))


def _exec_call_sites(path: Path) -> list:
    """(enclosing function name) for every `exec(...)` CALL in the file."""
    sites = []

    def walk(node, enclosing):
        for child in ast.iter_child_nodes(node):
            name = (child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    else enclosing)
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                    and child.func.id == "exec"):
                sites.append(enclosing)
            walk(child, name)

    walk(_parse(path), None)
    return sites


def _module_scope_import_roots(path: Path) -> set:
    """Top-level module names imported at MODULE scope only.

    Function-local imports are the intended shape for the dispatcher (the two
    public functions import it lazily), so a walker that counted them would
    flag exactly the design.
    """
    roots = set()

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Import):
                for alias in child.names:
                    roots.add(alias.name.split(".")[0])
            elif isinstance(child, ast.ImportFrom) and child.level == 0 and child.module:
                roots.add(child.module.split(".")[0])
            walk(child)

    walk(_parse(path))
    return roots


def _module_scope_called_names(path: Path) -> set:
    """Every callee name invoked at MODULE scope (decorator expressions too)."""
    names = set()

    def note(call):
        func = call.func
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in child.decorator_list:
                    walk_expr(decorator)
                continue
            if isinstance(child, ast.Lambda):
                continue
            if isinstance(child, ast.Call):
                note(child)
            walk(child)

    def walk_expr(node):
        if isinstance(node, ast.Call):
            note(node)
        for child in ast.iter_child_nodes(node):
            walk_expr(child)

    walk(_parse(path))
    return names


def _name_references(path: Path, name: str) -> bool:
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Attribute) and node.attr == name:
            return True
        if isinstance(node, ast.Name) and node.id == name:
            return True
        if isinstance(node, ast.ImportFrom) and any(a.name == name for a in node.names):
            return True
    return False


def _function_node(path: Path, name: str):
    for node in ast.walk(_parse(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _return_dict_key_sets(node) -> list:
    out = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Return) or not isinstance(child.value, ast.Dict):
            continue
        keys = set()
        literal = True
        for key in child.value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
            else:
                literal = False
        if literal:
            out.append(keys)
    return out


def _charts_append_key_sets(path: Path) -> list:
    out = []
    for node in ast.walk(_parse(path)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "charts"
                and node.args and isinstance(node.args[0], ast.Dict)):
            continue
        keys = {k.value for k in node.args[0].keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        out.append(keys)
    return out


def test_exec_is_called_only_inside_the_two_in_process_functions():
    found = set()
    for path in _python_sources():
        for enclosing in _exec_call_sites(path):
            found.add((_rel(path), enclosing))
    expected = {
        ("code_exec.py", "_execute_code_in_env"),
        ("plot_utils.py", "_render_in_process"),
    }
    assert found == expected, sorted(found)


@pytest.mark.parametrize("module_file,function_name", [
    ("code_exec.py", "_execute_in_process"),
    ("plot_utils.py", "_render_in_process"),
])
def test_the_in_process_function_has_exactly_one_caller_outside_its_module(module_file,
                                                                          function_name):
    referencing = set()
    for path in _python_sources():
        rel = _rel(path)
        if rel == module_file:
            continue
        if _name_references(path, function_name):
            referencing.add(rel)
    assert referencing == {"executor/runner.py"}, sorted(referencing)


@pytest.mark.parametrize("module_file,function_name", [
    ("code_exec.py", "_execute_in_process"),
    ("plot_utils.py", "_render_in_process"),
])
def test_the_in_process_function_exists_with_the_public_signature(module_file, function_name):
    module = code_exec if module_file == "code_exec.py" else plot_utils
    function = getattr(module, function_name, None)
    assert callable(function), f"{module_file} has no {function_name}"
    import inspect

    parameters = list(inspect.signature(function).parameters)
    public = "safe_execute" if module_file == "code_exec.py" else "render_plot_safe"
    expected = list(inspect.signature(getattr(module, public)).parameters)
    assert parameters == expected, (parameters, expected)


@pytest.mark.parametrize("module_file", SANDBOX_IMAGE_MODULES)
def test_no_module_copied_into_the_sandbox_image_imports_the_dispatcher(module_file):
    path = ROOT / module_file
    assert path.is_file(), f"missing {module_file}"
    roots = _module_scope_import_roots(path)
    assert "executor_client" not in roots, sorted(roots)
    assert "httpx" not in roots, sorted(roots)


@pytest.mark.parametrize("module_file", ["code_exec.py", "plot_utils.py"])
def test_the_public_function_reaches_the_dispatcher_through_a_lazy_import(module_file):
    text = (ROOT / module_file).read_text(encoding="utf-8")
    assert "executor_client" in text, f"{module_file} never mentions the dispatcher"


def test_the_transport_python_key_list_matches_the_in_process_success_return():
    node = _function_node(ROOT / "code_exec.py", "_execute_in_process")
    assert node is not None, "code_exec.py has no _execute_in_process"
    key_sets = _return_dict_key_sets(node)
    success = [keys for keys in key_sets if "result" in keys]
    assert len(success) == 1, key_sets
    assert success[0] == set(exec_transport._PYTHON_KEYS), (success[0],
                                                            set(exec_transport._PYTHON_KEYS))


def test_the_transport_plot_key_list_covers_every_in_process_return():
    node = _function_node(ROOT / "plot_utils.py", "_render_in_process")
    assert node is not None, "plot_utils.py has no _render_in_process"
    key_sets = _return_dict_key_sets(node)
    assert key_sets, "no dict literal is returned from _render_in_process"
    produced = set().union(*key_sets)
    allowed = set(exec_transport._PLOT_KEYS)
    missing = sorted(produced - allowed)
    assert missing == [], missing


def test_the_transport_chart_key_list_covers_both_chart_entry_literals():
    key_sets = _charts_append_key_sets(ROOT / "plot_utils.py")
    assert len(key_sets) == 2, key_sets
    allowed = set(exec_transport._CHART_KEYS)
    for keys in key_sets:
        missing = sorted(keys - allowed)
        assert missing == [], (keys, missing)


@pytest.mark.parametrize("module_name,attribute", [
    ("plot_utils", "GLOBAL_PLOT_SCOPE"),
    ("plot_utils", "_plotly_to_html"),
    ("plot_utils", "_PLOTLY_JS_URL"),
    ("plot_utils", "_plotly_js_asset_path"),
    ("plot_utils", "_plotly_js_missing_logged"),
    ("plot_utils", "ensure_plotly_js_asset"),
    ("plot_utils", "_is_noninteractive_standard_chart"),
    ("code_exec", "_EXEC_POOL"),
    ("code_exec", "CODE_EXEC_TIMEOUT_SECONDS"),
    ("code_exec", "outlier_mask"),
    ("code_exec", "drop_extreme_outliers"),
])
def test_the_module_attributes_other_modules_reach_for_still_exist(module_name, attribute):
    module = code_exec if module_name == "code_exec" else plot_utils
    assert hasattr(module, attribute), f"{module_name}.{attribute} disappeared"


def test_no_test_module_executes_generated_code_at_module_scope():
    """Module-scope work runs BEFORE the in-process rebinding fixture, so such
    a call would dispatch over HTTP during collection."""
    offenders = []
    for path in sorted((ROOT / "tests").rglob("test_*.py")) + \
            sorted((ROOT / "executor" / "tests").rglob("test_*.py")):
        called = _module_scope_called_names(path)
        hit = sorted(called & {"safe_execute", "render_plot_safe"})
        if hit:
            offenders.append((_rel(path), hit))
    assert offenders == [], offenders


@pytest.mark.parametrize("name", EXECUTOR_SETTING_NAMES)
def test_settings_carries_the_analysis_sandbox_fields(name):
    text = (ROOT / "settings.py").read_text(encoding="utf-8")
    assert name in text, f"settings.py never defines {name}"
    assert hasattr(settings, name), f"settings has no attribute {name}"


@pytest.mark.parametrize("filename", ["client.env.example", "client.local.env.example"])
def test_the_env_examples_document_the_sandbox_url(filename):
    path = ROOT / filename
    assert path.is_file(), f"missing {filename}"
    text = path.read_text(encoding="utf-8")
    assert "EXECUTOR_URL" in text, f"{filename} does not mention EXECUTOR_URL"
