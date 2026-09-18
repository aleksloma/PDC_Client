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
    """The details of a REAL segfault, as `executor/app.py` reports one.

    A runner killed by SIGSEGV returns `exit_code=-11`, from which the app
    derives `signal=11`; it wrote no response, so `response_sink` is empty and
    `signal_no` is truthy, and the crashed branch picks `reason="signal"` (the
    token `test_executor_infra_short_circuit` documents as "a provoked
    segfault"). `"segfault"` itself is not a value any assignment in
    `executor/app.py` or `executor/runner.py` can produce, so the fixture used
    to describe a response the sandbox cannot send.
    """
    # `reason` is inlined into the sentence the planner's retry prompt reads,
    # so only the closed vocabulary survives; a fixture outside it would make
    # the assertion below pass on the `unknown` fallback instead of on echoing.
    assert "signal" in exec_transport.CRASH_REASONS, exec_transport.CRASH_REASONS
    calls = []
    _install(monkeypatch, _body_handler(
        _synthetic_response(None, kind=kind, status="crashed",
                            exit_code=-11, signal=11, reason="signal"), calls))
    out = dispatcher.execute(kind, "RESULT = 1", _dfs(), sid="t", timeout_s=60)
    _assert_error_shape(out, kind, exec_transport.crash_error_text(-11, 11, "signal"))
    # Pinned literally as well: `crash_error_text` is imported from the
    # product, so an equality against it alone would stay green if the crash
    # text stopped carrying the details at all.
    assert "exit=-11" in out["error"] and "signal=11" in out["error"], out
    assert "reason=signal" in out["error"], out


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


def test_a_failed_startup_greeting_is_retried_exactly_once_not_per_dispatch(
        dispatcher, exec_env, monkeypatch):
    """A greeting that failed at startup buys ONE retry, and spends it whether
    or not it succeeds.

    The retry runs inside the held dispatch slot and pays a connect timeout,
    so retrying per dispatch would tax every question for as long as the
    service stays down — which is precisely when it is least affordable. The
    greeting itself re-arms the latch when it fails (that is how startup arms
    it), so the dispatch path has to clear it afterwards, not before.
    """
    healthz_calls = []

    def handler(request):
        if request.url.path.endswith("/healthz"):
            healthz_calls.append(request.url.path)
            raise httpx.ConnectError("refused", request=request)
        raise httpx.ConnectError("refused", request=request)

    _install(monkeypatch, handler)

    dispatcher.startup()
    after_startup = len(healthz_calls)
    assert after_startup == 1, healthz_calls

    for _ in range(3):
        out = dispatcher.execute("PYTHON", "RESULT = 1", _dfs(), sid="t", timeout_s=60)
        assert out == {"error": dispatcher.UNAVAILABLE_TEXT}, out

    retries = len(healthz_calls) - after_startup
    assert retries == 1, f"the greeting was retried {retries} times, not once"
    left = _job_dirs(exec_env)
    assert left == [], left


def test_sweep_orphans_removes_stale_job_dirs_and_stale_strays(dispatcher, exec_env):
    """INVERTED on purpose: an aged non-job entry is now REMOVED.

    The previous version of this test asserted that `keep_me` SURVIVED, and
    that expectation was wrong. The jobs root is group-writable — the sandbox
    has to be able to clear an abandoned job directory in it — so generated
    code can create files and directories directly in that root, and while
    both sweeps skipped every name that was not job-id shaped, nothing ever
    removed them. The volume is named and disk-backed, so a stash outlived
    restarts, an image upgrade and any number of intervening jobs, and a later
    job run for a DIFFERENT user could read it back. The root is supposed to
    hold nothing but job directories, so an aged entry that is not one is
    debris or a deliberate stash, and both go.

    The age threshold is unchanged, which is what keeps a job mid-creation
    (and a fresh stray) out of it.
    """
    stale = exec_transport.create_job_dir(exec_env, exec_transport.new_job_id())
    fresh = exec_transport.create_job_dir(exec_env, exec_transport.new_job_id())
    stray = exec_env / "keep_me"
    stray.mkdir()
    two_hours_ago = time.time() - 2 * 3600
    for path in (stale, stray):
        os.utime(path, (two_hours_ago, two_hours_ago))

    dispatcher.sweep_orphans()

    assert not stale.exists(), "a job directory older than an hour survived the sweep"
    assert fresh.is_dir(), "a fresh job directory was swept"
    assert not stray.exists(), "an aged entry that is not a job directory survived"


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
    # A status the sandbox answers with `payload: null` carries no keys of its
    # own: the dispatcher synthesizes the caller's error shape from the
    # envelope (reason/exit_code/signal). The captured `python_crashed` body is
    # what pins that -- `payload_keys` is empty there, and `["error"]` is the
    # contract the caller sees.
    if case["status"] in ("timeout", "crashed", "killed"):
        expected = ["error"]
    else:
        expected = sorted(case["payload_keys"])
    assert keys == expected, (keys, expected)


@pytest.mark.real_executor_dispatch
def test_the_captured_crash_pins_the_envelope_field_names(exec_env, monkeypatch):
    """The crash detail was previously asserted only against test-authored
    payloads, so renaming `reason`, `exit_code` or `signal` on the sandbox side
    stayed green here while production lost the detail. This replays the body a
    real `os._exit(1)` produced, so a rename fails.
    """
    case = _case("python_crashed")
    envelope = json.loads(case["body"].decode("utf-8"))
    # What the sandbox actually sent -- read from the captured bytes, not retyped.
    assert envelope["status"] == "crashed"
    assert envelope["payload"] is None
    assert (envelope["exit_code"], envelope["signal"], envelope["reason"]) == (1, None, "exit")

    calls = []
    _install(monkeypatch, _replay_handler(case, calls))
    out = _run_public(case)

    assert len(calls) >= 1, "the captured response was never requested"
    _assert_error_shape(out, "PYTHON", exec_transport.crash_error_text(1, None, "exit"))


@pytest.mark.real_executor_dispatch
def test_the_captured_memory_case_keeps_retrying_rather_than_short_circuiting(
        exec_env, monkeypatch):
    """An allocation past RLIMIT_AS is an ORDINARY execution error, not an
    infrastructure failure: the runner survives and reports `MemoryError`. That
    is what makes D7-9's split correct -- the planner can write cheaper code, so
    this must NOT be treated as "the service is down". Pinned against the real
    body because the distinction was argued from the limit's behaviour.
    """
    case = _case("python_memory")
    envelope = json.loads(case["body"].decode("utf-8"))
    assert envelope["status"] == "error"
    assert envelope["reason"] is None
    assert envelope["payload"]["error"].startswith("MemoryError:")

    calls = []
    _install(monkeypatch, _replay_handler(case, calls))
    out = _run_public(case)

    assert len(calls) >= 1, "the captured response was never requested"
    error = out["error"]
    assert error.startswith("MemoryError:")
    assert not executor_client.is_infrastructure_error(error), (
        "a MemoryError must keep its retries -- short-circuiting it would tell "
        "the user the service is down when the code was simply too greedy")


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


# ===========================================================================
# 4. reachability cache, /health, the mismatch warning, the log tails
#    (the cached /health answer, the once-per-path warnings, the log tails)
# ===========================================================================
HEALTH_BASE_KEYS = ("status", "service", "brain_reachable",
                    "tenant_token_configured", "build_commit")
HEALTH_NEW_KEYS = ("executor_reachable", "executor_checked_at")

LOG_TAIL_MAX = 2000
NOT_GROUP_WRITABLE = "EXECUTOR_SHARED_DIR_NOT_GROUP_WRITABLE"
REMOVE_FAILED = "EXEC_JOB_DIR_REMOVE_FAILED"


def _log_recorder(monkeypatch, module) -> list:
    """Capture `log_with_sid(sid, level, message, **kwargs)` calls.

    The name is imported into each module's own namespace, so patching the
    module attribute is the seam.
    """
    lines = []

    def record(sid, level, message, *args, **kwargs):
        lines.append({"sid": sid, "level": level, "message": message,
                      "kwargs": kwargs})

    monkeypatch.setattr(module, "log_with_sid", record)
    return lines


def _messages(lines, needle) -> list:
    return [row for row in lines if needle in str(row["message"])]


def _reset_reach(dispatcher):
    """Put the reachability cache back to "never checked".

    The implementer must expose `_REACH` (the cached state `/health` reads)
    and `reachable()`: `/health` may not make a live call — a stopped sandbox
    costs 8 s in `getaddrinfo` alone, measured inside the running web
    container, which would stall the event loop and fail the container own
    5 s healthcheck exactly when the operator needs the answer.
    """
    state = getattr(dispatcher, "_REACH", None)
    assert isinstance(state, dict), (
        "executor_client must expose the cached reachability state _REACH "
        "with the keys ok (None initially) and checked_at (0.0 initially)")
    reach = getattr(dispatcher, "reachable", None)
    assert callable(reach), (
        "executor_client must expose reachable() -> (ok, checked_at) — the "
        "cached tuple /health reports, never a live blocking call")
    state["ok"] = None
    state["checked_at"] = 0.0
    dispatcher._PENDING["handshake"] = False
    return state


def _healthz_with(versions: dict) -> httpx.Response:
    body = {"ok": True, "version": "captured", "build_time": "",
            "versions": versions}
    return httpx.Response(200, content=exec_transport.dumps(body),
                          headers=JSON_HEADERS)


def test_reachability_starts_unknown(dispatcher, exec_env):
    """Unknown is NOT False: a boot that has not greeted the sandbox yet must
    not report an outage."""
    _reset_reach(dispatcher)
    ok, checked_at = dispatcher.reachable()
    assert ok is None, ok
    assert checked_at == 0.0, checked_at


def test_a_successful_handshake_marks_the_sandbox_reachable(dispatcher, exec_env,
                                                            monkeypatch):
    _reset_reach(dispatcher)
    calls = []
    _install(monkeypatch, _body_handler(_synthetic_response({"error": None}), calls))

    greeted = dispatcher.handshake()

    assert greeted is True, greeted
    ok, checked_at = dispatcher.reachable()
    assert ok is True, ok
    assert checked_at > 0.0, checked_at


def test_a_failed_handshake_marks_the_sandbox_unreachable(dispatcher, exec_env,
                                                          monkeypatch):
    _reset_reach(dispatcher)
    calls = []
    _install(monkeypatch, _raising_handler(
        lambda request: httpx.ConnectError("refused", request=request), calls))

    greeted = dispatcher.handshake()

    assert greeted is False, greeted
    ok, _ = dispatcher.reachable()
    assert ok is False, ok


def test_a_dispatch_transport_failure_marks_the_sandbox_unreachable(dispatcher,
                                                                    exec_env,
                                                                    monkeypatch):
    """Every dispatch is a free probe — the state must not depend on a 30 s
    timer when a question already proved the answer."""
    _reset_reach(dispatcher)
    dispatcher._REACH["ok"] = True
    calls = []
    _install(monkeypatch, _raising_handler(
        lambda request: httpx.ConnectError("refused", request=request), calls))

    out = dispatcher.execute("PYTHON", "RESULT = 1", _dfs(), sid="t", timeout_s=60)

    assert out == {"error": dispatcher.UNAVAILABLE_TEXT}, out
    ok, _ = dispatcher.reachable()
    assert ok is False, ok


@pytest.mark.parametrize("status", [200, 400, 503])
def test_any_http_answer_marks_the_sandbox_reachable(dispatcher, exec_env,
                                                     monkeypatch, status):
    """A rejection is still a conversation: the service is up."""
    _reset_reach(dispatcher)
    calls = []
    body = (_synthetic_response({"error": None}) if status == 200
            else {"error": "no", "code": "BAD_KIND"})
    _install(monkeypatch, _body_handler(body, calls, status=status))

    dispatcher.execute("PYTHON", "RESULT = 1", _dfs(), sid="t", timeout_s=60)

    ok, _ = dispatcher.reachable()
    assert ok is True, ok


@pytest.mark.executor_probe
def test_reachable_never_waits_for_its_own_probe(dispatcher, exec_env, monkeypatch):
    """The refresh runs on a short-lived daemon thread for the NEXT caller.

    A stale cache must not turn `reachable()` into the blocking call the whole
    design exists to avoid, so the probe here blocks until the assertions are
    done and the call still has to return immediately.
    """
    _reset_reach(dispatcher)
    dispatcher._REACH["ok"] = True
    dispatcher._REACH["checked_at"] = 1.0        # stale by decades
    probe = getattr(dispatcher, "probe", None)
    assert callable(probe), (
        "executor_client must expose probe() — the /healthz GET the "
        "short-lived refresh thread runs")

    started = threading.Event()
    release = threading.Event()

    def blocking_probe(*args, **kwargs):
        started.set()
        release.wait(10)
        return False

    monkeypatch.setattr(dispatcher, "probe", blocking_probe, raising=False)
    try:
        began = time.monotonic()
        ok, checked_at = dispatcher.reachable()
        elapsed = time.monotonic() - began
        assert elapsed < 1.0, elapsed
        assert ok is True, ok            # the CACHED value, not the probe result
        assert started.wait(2) is True, "the refresh probe never started"
        assert release.is_set() is False, "reachable() waited for the probe"
    finally:
        release.set()


@pytest.fixture
def health_client(dispatcher, tmp_path, monkeypatch):
    """The real app, TestClient WITHOUT the context manager (the lifespan
    starts the db_scheduler thread — see tests/test_version_endpoint.py)."""
    from starlette.testclient import TestClient

    import app as app_mod

    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    return TestClient(app_mod.app)


def test_health_reports_the_cached_sandbox_state_additively(dispatcher, health_client):
    """Additive only: the smoke-test skill and the operator docs assert on the
    older keys."""
    _reset_reach(dispatcher)
    dispatcher._REACH["ok"] = True
    dispatcher._REACH["checked_at"] = time.time()

    response = health_client.get("/health")
    status = response.status_code
    assert status == 200, (status, response.text[:300])
    body = response.json()
    missing = [key for key in HEALTH_BASE_KEYS + HEALTH_NEW_KEYS if key not in body]
    assert missing == [], (missing, sorted(body))
    assert body["executor_reachable"] is True, body
    checked_at = body["executor_checked_at"]
    assert isinstance(checked_at, float), (type(checked_at).__name__, checked_at)


def test_health_stays_200_when_the_sandbox_is_known_down(dispatcher, health_client):
    """The web container must NOT go unhealthy because the sandbox is down —
    an operator needs a reachable page that says so."""
    _reset_reach(dispatcher)
    dispatcher._REACH["ok"] = False
    dispatcher._REACH["checked_at"] = time.time()

    response = health_client.get("/health")
    status = response.status_code
    assert status == 200, (status, response.text[:300])
    body = response.json()
    assert body["status"] == "ok", body
    assert body["executor_reachable"] is False, body


def test_health_is_honest_before_the_first_check(dispatcher, health_client):
    _reset_reach(dispatcher)

    body = health_client.get("/health").json()
    reachable = body.get("executor_reachable")
    checked_at = body.get("executor_checked_at")
    assert reachable is None, reachable
    assert checked_at is None, checked_at


def test_a_version_difference_warns_once_and_names_the_module(dispatcher, exec_env,
                                                              monkeypatch):
    """The mismatch warning had never been CONSTRUCTED by a test: the
    shared `/healthz` fixture builds its versions from the app own imports,
    so the two sides always agreed. A chart that silently renders differently
    between the two independently upgraded images is the whole point."""
    module = dispatcher._VERSION_MODULES[0]
    ours = dispatcher._app_version(module)
    assert ours, f"the app cannot report its own {module} version"
    versions = {name: dispatcher._app_version(name)
                for name in dispatcher._VERSION_MODULES}
    theirs = "0.0.0+drift"
    versions[module] = theirs

    def handler(request):
        assert request.url.path.endswith("/healthz"), str(request.url)
        return _healthz_with(versions)

    _install(monkeypatch, handler)
    lines = _log_recorder(monkeypatch, dispatcher)

    greeted = dispatcher.handshake()

    assert greeted is True, greeted
    hits = _messages(lines, "EXECUTOR_VERSION_MISMATCH")
    assert len(hits) == 1, [row["message"] for row in lines]
    message = hits[0]["message"]
    assert f"module={module}" in message, message
    assert f"app={ours}" in message, message
    assert f"executor={theirs}" in message, message


def test_matching_versions_warn_about_nothing(dispatcher, exec_env, monkeypatch):
    versions = {name: dispatcher._app_version(name)
                for name in dispatcher._VERSION_MODULES}

    def handler(request):
        return _healthz_with(versions)

    _install(monkeypatch, handler)
    lines = _log_recorder(monkeypatch, dispatcher)

    dispatcher.handshake()

    hits = _messages(lines, "EXECUTOR_VERSION_MISMATCH")
    assert hits == [], [row["message"] for row in hits]


def test_an_execution_error_logs_the_traceback_and_stderr_tails(dispatcher, exec_env,
                                                                monkeypatch):
    """The generated code traceback used to land in the web container own
    log when `exec()` ran there. After the rewire it lives only in the
    sandbox tmpfs log, which is wiped on restart, so the one durable copy is
    a capped tail on the dispatcher own error line."""
    error_text = "KeyError: city"
    traceback_text = "T" * (LOG_TAIL_MAX + 5000)
    stderr_text = "S" * (LOG_TAIL_MAX + 5000)
    calls = []
    _install(monkeypatch, _body_handler(
        _synthetic_response({"error": error_text}, status="error",
                            traceback=traceback_text, stderr=stderr_text), calls))
    lines = _log_recorder(monkeypatch, dispatcher)

    out = dispatcher.execute("PYTHON", "RESULT = df[0]", _dfs(), sid="t",
                             timeout_s=60)

    assert out == {"error": error_text}, out          # the tails never RETURN
    hits = _messages(lines, "EXEC_ERROR")
    assert len(hits) == 1, [row["message"] for row in lines]
    kwargs = hits[0]["kwargs"]
    logged_traceback = kwargs.get("traceback")
    logged_stderr = kwargs.get("stderr")
    assert isinstance(logged_traceback, str), kwargs
    assert isinstance(logged_stderr, str), kwargs
    assert len(logged_traceback) <= LOG_TAIL_MAX, len(logged_traceback)
    assert len(logged_stderr) <= LOG_TAIL_MAX, len(logged_stderr)
    assert set(logged_traceback) == {"T"}, logged_traceback[:50]
    assert set(logged_stderr) == {"S"}, logged_stderr[:50]


def test_a_timeout_logs_the_traceback_and_stderr_tails(dispatcher, exec_env,
                                                       monkeypatch):
    """A killed run partial stderr is often the only clue to WHICH loop hung."""
    traceback_text = "T" * (LOG_TAIL_MAX + 5000)
    stderr_text = "S" * (LOG_TAIL_MAX + 5000)
    calls = []
    _install(monkeypatch, _body_handler(
        _synthetic_response(None, status="timeout", reason="hard_timeout",
                            traceback=traceback_text, stderr=stderr_text), calls))
    lines = _log_recorder(monkeypatch, dispatcher)

    out = dispatcher.execute("PYTHON", "while True: pass", _dfs(), sid="t",
                             timeout_s=60)

    expected = {"error": exec_transport.timeout_error_text(60)}
    assert out == expected, out                      # the tails never RETURN
    hits = _messages(lines, "EXEC_TIMEOUT")
    assert len(hits) == 1, [row["message"] for row in lines]
    kwargs = hits[0]["kwargs"]
    logged_traceback = kwargs.get("traceback")
    logged_stderr = kwargs.get("stderr")
    assert isinstance(logged_traceback, str), kwargs
    assert isinstance(logged_stderr, str), kwargs
    assert len(logged_traceback) <= LOG_TAIL_MAX, len(logged_traceback)
    assert len(logged_stderr) <= LOG_TAIL_MAX, len(logged_stderr)
    assert kwargs.get("reason") == "hard_timeout", kwargs


def test_a_removal_failure_warns_once_per_path_not_once_per_dispatch(dispatcher,
                                                                     exec_env,
                                                                     monkeypatch):
    """Generated code can chmod 0500 a directory it created under
    `out/`, and only the SANDBOX uid can then clear it (within the hour, by
    its own sweep). The web side cannot fix it, so warning on every pass turns
    one unfixable directory into an unbounded log stream."""
    fixed_job_id = "a" * 32
    assert exec_transport.valid_job_id(fixed_job_id), fixed_job_id
    monkeypatch.setattr(exec_transport, "new_job_id", lambda: fixed_job_id)

    def refuse(path, *args, **kwargs):
        raise PermissionError(f"cannot remove {path}")

    monkeypatch.setattr(dispatcher.shutil, "rmtree", refuse)
    calls = []
    _install(monkeypatch, _body_handler(_synthetic_response({"error": None}), calls))
    lines = _log_recorder(monkeypatch, dispatcher)

    for _ in range(2):
        dispatcher.execute("PYTHON", "RESULT = 1", _dfs(), sid="t", timeout_s=60)

    assert len(calls) == 2, calls
    hits = _messages(lines, REMOVE_FAILED)
    assert len(hits) == 1, [row["message"] for row in hits]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits only")
def test_an_existing_shared_dir_without_group_write_is_reported_not_repaired(
        dispatcher, exec_env, tmp_path, monkeypatch):
    """The production shape is a volume root owned by ANOTHER uid with
    the mode already set, and a process cannot chmod what it does not own:
    attempting it would warn on every boot of a healthy stack. Missing group
    write, on the other hand, means the sandbox uid cannot create `out/` and
    every job comes back crashed — worth exactly one line."""
    shared = tmp_path / "locked_jobs"
    shared.mkdir(mode=0o2700)
    os.chmod(shared, 0o2700)
    monkeypatch.setattr(settings, "EXECUTOR_SHARED_DIR", str(shared))
    chmods = []
    monkeypatch.setattr(dispatcher.os, "chmod",
                        lambda path, mode, *a, **k: chmods.append((str(path), mode)))
    lines = _log_recorder(monkeypatch, dispatcher)

    resolved = dispatcher.ensure_shared_dir()

    assert Path(resolved) == shared, resolved
    hits = _messages(lines, NOT_GROUP_WRITABLE)
    assert len(hits) == 1, [row["message"] for row in lines]
    assert chmods == [], chmods


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits only")
def test_a_shared_dir_the_call_creates_is_never_reported(dispatcher, exec_env,
                                                         tmp_path, monkeypatch):
    fresh = tmp_path / "fresh_jobs"
    monkeypatch.setattr(settings, "EXECUTOR_SHARED_DIR", str(fresh))
    lines = _log_recorder(monkeypatch, dispatcher)

    resolved = dispatcher.ensure_shared_dir()

    assert Path(resolved) == fresh, resolved
    assert fresh.is_dir(), fresh
    hits = _messages(lines, NOT_GROUP_WRITABLE)
    assert hits == [], [row["message"] for row in hits]


# ---------------------------------------------------------------------------
# the outcome LINES themselves are a destination for untrusted input
#
# Every field below is written by the sandbox and ends up in
# `<DATA_ROOT>/logs/datachat.log` — a newline-delimited file operators grep
# and, during an incident, trust. So a log line gets the same treatment as a
# prompt: bounded length, a closed vocabulary where one exists, and CR/LF
# escaped so nothing can forge a second line.
# ---------------------------------------------------------------------------
# What a forged line would look like if a newline survived: the sandbox picks
# the timestamp, the level, the sid and the event name.
FORGED_LINE = "2026-09-17 00:00:00,000 | INFO | [sid=security] EXEC_OK forged=1"
FORGED_MARKER = "forged=1"

HOSTILE_TIMEOUT_REASONS = [
    f"hard_timeout\n{FORGED_LINE}",        # a real token, then a whole new line
    "the job was slow, nothing to see",    # a plausible-looking sentence
    FORGED_LINE,
]

HOSTILE_METRICS = ["M" * 40000, {"elapsed_ms": 11}, 10 ** 4000 - 1]
HOSTILE_METRIC_IDS = ["long_string", "dict", "huge_number"]


def _kwarg_strings(kwargs: dict) -> str:
    """Every kwarg value as one string — what the formatter will write."""
    return " ".join(f"{key}={value!r}" for key, value in kwargs.items())


@pytest.mark.parametrize("reason", HOSTILE_TIMEOUT_REASONS,
                         ids=range(len(HOSTILE_TIMEOUT_REASONS)))
def test_a_hostile_timeout_reason_is_logged_as_unknown(dispatcher, exec_env,
                                                       monkeypatch, reason):
    """`reason` tells a real overrun apart from an expired queue slot, so it
    IS logged — which means it has to pass the same closed vocabulary the
    crash sentence uses. The happy-path value alone proves nothing: it is the
    values outside the vocabulary that had to stop being repeated."""
    calls = []
    _install(monkeypatch, _body_handler(
        _synthetic_response(None, status="timeout", reason=reason), calls))
    lines = _log_recorder(monkeypatch, dispatcher)

    dispatcher.execute("PYTHON", "while True: pass", _dfs(), sid="t", timeout_s=60)

    hits = _messages(lines, "EXEC_TIMEOUT")
    assert len(hits) == 1, [row["message"] for row in lines]
    kwargs = hits[0]["kwargs"]
    logged_reason = kwargs.get("reason")
    assert logged_reason == "unknown", logged_reason
    rendered = hits[0]["message"] + " " + _kwarg_strings(kwargs)
    assert FORGED_MARKER not in rendered, rendered[:400]
    assert "nothing to see" not in rendered, rendered[:400]


def test_a_legitimate_timeout_reason_still_survives(dispatcher, exec_env, monkeypatch):
    """The vocabulary must not cost the operator the one distinction the field
    exists for: a slot that expired never ran the code."""
    calls = []
    _install(monkeypatch, _body_handler(
        _synthetic_response(None, status="timeout", reason="queued"), calls))
    lines = _log_recorder(monkeypatch, dispatcher)

    dispatcher.execute("PYTHON", "while True: pass", _dfs(), sid="t", timeout_s=60)

    hits = _messages(lines, "EXEC_TIMEOUT")
    assert len(hits) == 1, [row["message"] for row in lines]
    logged_reason = hits[0]["kwargs"].get("reason")
    assert logged_reason == "queued", logged_reason


@pytest.mark.parametrize("value", HOSTILE_METRICS, ids=HOSTILE_METRIC_IDS)
@pytest.mark.parametrize("field", ["elapsed_ms", "peak_rss_mb"])
def test_a_hostile_measurement_lands_as_none_on_the_success_line(dispatcher, exec_env,
                                                                 monkeypatch, field,
                                                                 value):
    """`elapsed_ms` / `peak_rss_mb` ride EVERY successful answer, so an
    unbounded value here is 40 000 characters of the sandbox's choosing on
    every line of a healthy day's log."""
    overrides = {field: value}
    calls = []
    _install(monkeypatch, _body_handler(
        _synthetic_response({"error": None}, **overrides), calls))
    lines = _log_recorder(monkeypatch, dispatcher)

    dispatcher.execute("PYTHON", "RESULT = 1", _dfs(), sid="t", timeout_s=60)

    hits = _messages(lines, "EXEC_OK")
    assert len(hits) == 1, [row["message"] for row in lines]
    kwargs = hits[0]["kwargs"]
    assert kwargs.get(field) is None, type(kwargs.get(field)).__name__
    rendered = _kwarg_strings(kwargs)
    assert "MMMM" not in rendered, rendered[:400]
    assert len(rendered) < 1000, len(rendered)


def test_legitimate_measurements_survive_as_numbers(dispatcher, exec_env, monkeypatch):
    """A memory reading is fractional on purpose, so the bound keeps floats."""
    calls = []
    _install(monkeypatch, _body_handler(
        _synthetic_response({"error": None}, elapsed_ms=11, peak_rss_mb=99.5), calls))
    lines = _log_recorder(monkeypatch, dispatcher)

    dispatcher.execute("PYTHON", "RESULT = 1", _dfs(), sid="t", timeout_s=60)

    kwargs = _messages(lines, "EXEC_OK")[0]["kwargs"]
    elapsed = kwargs.get("elapsed_ms")
    peak = kwargs.get("peak_rss_mb")
    assert elapsed == 11, (type(elapsed).__name__, elapsed)
    assert peak == 99.5, (type(peak).__name__, peak)


def test_the_error_text_is_escaped_in_the_log_and_verbatim_to_the_caller(dispatcher,
                                                                        exec_env,
                                                                        monkeypatch):
    """BOTH halves, because the whole value of the fix is that only ONE of the
    two copies is escaped.

    The error text must reach the caller byte-for-byte: the planner has to see
    the real exception to rewrite the code, and the user may see it. The LOG
    copy must not, because the sandbox writes that string and the log file is
    newline-delimited — an embedded newline forges a complete, plausible
    second line into the file an operator greps during an incident.
    """
    forged_error = f"ValueError: x\n{FORGED_LINE}"
    calls = []
    _install(monkeypatch, _body_handler(
        _synthetic_response({"error": forged_error}, status="error"), calls))
    lines = _log_recorder(monkeypatch, dispatcher)

    out = dispatcher.execute("PYTHON", "raise ValueError", _dfs(), sid="t",
                             timeout_s=60)

    # (1) the CALLER's copy is untouched.
    returned = out.get("error")
    assert returned == forged_error, returned
    assert "\n" in returned, repr(returned)

    # (2) the LOG copy is one line.
    hits = _messages(lines, "EXEC_ERROR")
    assert len(hits) == 1, [row["message"] for row in lines]
    message = hits[0]["message"]
    assert "\n" not in message, repr(message[:400])
    assert "\r" not in message, repr(message[:400])
    assert "\\n" in message, message[:400]
    # (3) and no second record was fabricated: the forged text claims to be an
    # EXEC_OK line, and there must be no such line in this dispatch.
    forged_records = [row for row in lines
                      if str(row["message"]).startswith("EXEC_OK")]
    assert forged_records == [], [row["message"] for row in forged_records]


@pytest.mark.parametrize("field", ["traceback", "stderr"])
def test_the_log_tails_are_escaped_too(dispatcher, exec_env, monkeypatch, field):
    """Same reasoning, same helper — pinned per field so a future tail added
    without the escape fails here."""
    overrides = {field: f"line one\n{FORGED_LINE}"}
    calls = []
    _install(monkeypatch, _body_handler(
        _synthetic_response({"error": "ValueError: x"}, status="error",
                            **overrides), calls))
    lines = _log_recorder(monkeypatch, dispatcher)

    dispatcher.execute("PYTHON", "raise ValueError", _dfs(), sid="t", timeout_s=60)

    kwargs = _messages(lines, "EXEC_ERROR")[0]["kwargs"]
    logged = kwargs.get(field)
    assert isinstance(logged, str), kwargs
    assert "\n" not in logged, repr(logged[:400])
    assert "\r" not in logged, repr(logged[:400])
    assert "\\n" in logged, logged[:400]


# ---------------------------------------------------------------------------
# the stray pass of the jobs root
#
# The root is group-writable so the sandbox can clear an abandoned job
# directory in it; the same permission lets generated code create entries
# directly in the root, which nothing removed while both sweeps only ever
# looked at 32-hex names. These pin the widened behaviour, the bounds that
# keep it from becoming a general-purpose delete, and the ONE thing the two
# sides do in OPPOSITE directions.
# ---------------------------------------------------------------------------
STRAY_REMOVED = "EXEC_STRAY_ENTRY_REMOVED"
STRAY_REFUSED = "EXEC_STRAY_SWEEP_REFUSED"
TWO_HOURS_S = 2 * 3600


def _age(path, seconds: float = TWO_HOURS_S) -> None:
    """Back-date an entry past the sweep's age threshold.

    `follow_symlinks=False` matters for the symlink case (the LINK has to be
    aged, not its target) and is unavailable on Windows, where that case is
    skipped anyway — hence the fallback rather than a hard requirement.
    """
    stamp = time.time() - seconds
    try:
        os.utime(path, (stamp, stamp), follow_symlinks=False)
    except (NotImplementedError, OSError):
        os.utime(path, (stamp, stamp))


@pytest.fixture(autouse=True)
def _reset_stray_latch(dispatcher):
    """The refusal line is latched once per process; these tests assert on it
    more than once."""
    saved = dict(getattr(dispatcher, "_STRAY_REFUSED", {}) or {})
    if hasattr(dispatcher, "_STRAY_REFUSED"):
        dispatcher._STRAY_REFUSED["logged"] = False
    yield
    if hasattr(dispatcher, "_STRAY_REFUSED") and saved:
        dispatcher._STRAY_REFUSED.update(saved)


def test_an_aged_stray_file_is_removed(dispatcher, exec_env, monkeypatch):
    """The commonest shape: generated code writing `open("/jobs/x", "w")`."""
    stray = exec_env / "stash.txt"
    stray.write_text("exfiltrated", encoding="utf-8")
    _age(stray)
    lines = _log_recorder(monkeypatch, dispatcher)

    dispatcher.sweep_orphans()

    assert not stray.exists(), "an aged stray file survived"
    hits = _messages(lines, STRAY_REMOVED)
    assert len(hits) == 1, [row["message"] for row in lines]
    assert "kind=file" in hits[0]["message"], hits[0]["message"]


def test_an_aged_stray_directory_is_removed_with_its_contents(dispatcher, exec_env,
                                                              monkeypatch):
    stray = exec_env / "stash_dir"
    stray.mkdir()
    (stray / "inner.txt").write_text("x", encoding="utf-8")
    _age(stray)
    lines = _log_recorder(monkeypatch, dispatcher)

    dispatcher.sweep_orphans()

    assert not stray.exists(), "an aged stray directory survived"
    hits = _messages(lines, STRAY_REMOVED)
    assert len(hits) == 1, [row["message"] for row in lines]
    assert "kind=dir" in hits[0]["message"], hits[0]["message"]


def test_a_job_id_shaped_stray_file_is_removed(dispatcher, exec_env):
    """The case the widening would otherwise have missed.

    `create_job_dir` only ever makes DIRECTORIES, so a 32-hex name that is a
    file was never a job — it is a stash wearing a job id, and the old
    name-shape test would have waved it through.
    """
    disguised = exec_env / ("a" * 32)
    assert exec_transport.valid_job_id(disguised.name), disguised.name
    disguised.write_text("hiding", encoding="utf-8")
    _age(disguised)

    dispatcher.sweep_orphans()

    assert not disguised.exists(), "a stash wearing a job-id name survived"


def test_a_fresh_stray_is_kept(dispatcher, exec_env):
    """The age threshold still bounds everything: a job being prepared right
    now must not have its neighbours deleted underneath it."""
    stray = exec_env / "just_written.txt"
    stray.write_text("x", encoding="utf-8")

    dispatcher.sweep_orphans()

    assert stray.is_file(), "a fresh stray was swept"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_an_aged_stray_symlink_is_unlinked_and_never_followed(dispatcher, exec_env,
                                                              tmp_path):
    """Generated code chooses where a symlink points, and this sweep runs in
    the container where `DATA_ROOT` IS mounted — so the link is UNLINKED and
    its target is left untouched. Following it would turn a cleanup into an
    arbitrary delete."""
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("customer data", encoding="utf-8")
    link = exec_env / "shortcut"
    link.symlink_to(target, target_is_directory=True)
    _age(link)

    dispatcher.sweep_orphans()

    assert not link.exists(), "an aged stray symlink survived"
    assert not link.is_symlink(), "the link itself was left behind"
    assert target.is_dir(), "the sweep followed the link and removed its target"
    assert (target / "keep.txt").is_file(), "the link's target lost content"


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership")
def test_the_web_sweep_leaves_what_it_owns(dispatcher, exec_env, monkeypatch):
    """OPPOSITE to the sandbox's rule, and deliberately so.

    This side removes only what it does NOT own. Everything under `DATA_ROOT`
    was written by this uid, so a jobs directory misconfigured to point at
    customer state cannot be swept by this pass, while a stash written by
    generated code — which carries the SANDBOX uid — can. The sandbox's copy
    inverts it (only what it DOES own): there, its own uid is exactly the set
    generated code could have created, and removing what it does not own
    would let it clear entries the web side put on the volume.
    """
    mine = exec_env / "written_by_me.txt"
    mine.write_text("x", encoding="utf-8")
    _age(mine)
    own_uid = dispatcher._own_euid()
    assert own_uid is not None, "POSIX should report an euid"

    dispatcher.sweep_orphans()

    assert mine.is_file(), "the web sweep removed an entry it owns"

    # And with the ownership check reporting a DIFFERENT identity, the same
    # entry goes — the rule is the owner, not the name.
    monkeypatch.setattr(dispatcher, "_own_euid", lambda: own_uid + 1)
    dispatcher.sweep_orphans()
    assert not mine.exists(), "an entry owned by another identity survived"


@pytest.mark.parametrize("relation", ["equal", "parent"])
def test_the_stray_pass_stands_down_when_the_jobs_dir_encloses_the_data_root(
        dispatcher, exec_env, tmp_path, monkeypatch, relation):
    """The bound that holds on every platform.

    Until the widening, this sweep only ever touched 32-hex names, and that
    was its whole protection against `EXECUTOR_SHARED_DIR` — an
    operator-supplied path — pointing at something real. A stray is defined
    by its name saying nothing, so the one shape in which the mistake would
    be unrecoverable is refused outright: a jobs directory that IS the data
    root, or contains it, is a misconfigured install and not a jobs volume.
    Job directories are still swept; only the stray pass stands down.
    """
    data_root = tmp_path / "data_root"
    (data_root / "users").mkdir(parents=True)
    jobs = data_root if relation == "equal" else tmp_path
    monkeypatch.setattr(settings, "DATA_ROOT", str(data_root))
    monkeypatch.setattr(settings, "EXECUTOR_SHARED_DIR", str(jobs))
    precious = jobs / "users"
    if relation == "parent":
        precious = data_root
    _age(precious)
    stale_job = exec_transport.create_job_dir(jobs, exec_transport.new_job_id())
    _age(stale_job)
    lines = _log_recorder(monkeypatch, dispatcher)

    dispatcher.sweep_orphans()

    assert precious.exists(), "the stray pass ran against the data root"
    assert not stale_job.exists(), "job directories must still be swept"
    hits = _messages(lines, STRAY_REFUSED)
    assert len(hits) == 1, [row["message"] for row in lines]
    assert "encloses_data_root" in hits[0]["message"], hits[0]["message"]


def test_an_unresolvable_jobs_dir_fails_closed(dispatcher, exec_env, monkeypatch):
    """A path that will not resolve is never a directory to widen a delete
    on, so the stray pass stands down with the same one line."""
    stray = exec_env / "stash.txt"
    stray.write_text("x", encoding="utf-8")
    _age(stray)

    def refuse(self, *args, **kwargs):
        raise OSError("cannot resolve")

    monkeypatch.setattr(Path, "resolve", refuse)
    lines = _log_recorder(monkeypatch, dispatcher)

    dispatcher.sweep_orphans()

    assert stray.is_file(), "the stray pass ran with an unresolvable jobs dir"
    hits = _messages(lines, STRAY_REFUSED)
    assert len(hits) == 1, [row["message"] for row in lines]
    assert "unresolved" in hits[0]["message"], hits[0]["message"]


def test_a_stray_name_carrying_a_newline_is_logged_on_one_line(dispatcher, exec_env,
                                                               monkeypatch):
    """The name of anything that is not a job directory was chosen by
    generated code, so it is untrusted text on a newline-delimited line —
    escaped and capped like every other field the sandbox writes."""
    if os.name == "nt":
        pytest.skip("Windows filenames cannot contain a newline")
    forged = "stash\n2026-09-17 00:00:00,000 | INFO | [sid=x] EXEC_OK job_id=forged"
    # Padding chosen to exceed the 200-char log cap while staying under the
    # filesystem's 255-BYTE name limit, which a longer name hits first.
    stray = exec_env / (forged + "A" * 150)
    stray.write_text("x", encoding="utf-8")
    _age(stray)
    lines = _log_recorder(monkeypatch, dispatcher)

    dispatcher.sweep_orphans()

    hits = _messages(lines, STRAY_REMOVED)
    assert len(hits) == 1, [row["message"] for row in lines]
    rendered = hits[0]["message"] + " " + " ".join(
        f"{key}={value}" for key, value in hits[0]["kwargs"].items())
    assert "\n" not in rendered, repr(rendered[:300])
    assert "\r" not in rendered, repr(rendered[:300])
    assert "\\n" in rendered, rendered[:300]
    name_field = hits[0]["kwargs"].get("name")
    assert isinstance(name_field, str), hits[0]["kwargs"]
    assert len(name_field) <= 200, len(name_field)
    impostors = [row for row in lines if str(row["message"]).startswith("EXEC_OK")]
    assert impostors == [], [row["message"] for row in impostors]
