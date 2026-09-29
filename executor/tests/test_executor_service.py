"""The executor service end to end (Linux only; runs in the
`executor/Dockerfile` `test` stage as root: `docker run --rm --tmpfs /jobs
pdc-executor-test python -m pytest executor/tests -q`).

Drives `executor/runner.py` and the FastAPI app in `executor/app.py`
directly. The test plays the main app: it creates the job dir with
`exec_transport.create_job_dir`, writes the inputs with `write_inputs`,
POSTs `/execute`, decodes the body with `exec_transport.loads` (never
`resp.json()` — NaN tokens) and reconstructs the caller-shaped dict with
`deserialize_result`. Every test sets its `EXECUTOR_*` env BEFORE entering
`with TestClient(app)` — Starlette 1.6 runs the lifespan on every context
entry and not for a bare `TestClient(app).get()`.

`executor.app` is imported ONLY inside fixtures/tests so the module
collects-and-skips cleanly on Windows (`resource` is POSIX-only).

Covers the HTTP contract, timeouts, the memory/thread/signal/fork limits, env
isolation, the orphan sweep, hostile requests and two-uid job-directory
ownership. One case runs against the REAL process tree by launching uvicorn as
a subprocess: the same-uid /proc sweep must spare BOTH the app and its parent.
"""
import pytest

pytest.importorskip("resource")

import sys  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="executor tests need Linux: RLIMIT_*, killpg, /proc, setuid")

import base64  # noqa: E402
import contextlib  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import socket  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import exec_transport  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
RUNNER = ROOT / "executor" / "runner.py"

SECRET_NAMES = ("SECRET_KEY", "CLIENT_ENCRYPTION_KEY", "CLIENT_ENCRYPTION_KEY_OLD",
                "LOCAL_ADMIN_PASSWORD", "GCS_UPLOAD_BUCKET")
WEB_UID = 10001
SHARED_GID = 10001
EXEC_UID = 10002
COLD_JOB_BUDGET_S = 60          # a first job imports matplotlib/sklearn/... in the runner
TIMEOUT_TEXT = "TimeoutError: Code execution exceeded {timeout} seconds limit"
MEMORY_TEXT = "MemoryError: execution exceeded the memory limit"

FORK_CODE = "import os; pid = os.fork()\nif pid == 0:\n    import time; time.sleep(60)\nRESULT = 1"
FORK_SETSID_CODE = ("import os; pid = os.fork()\nif pid == 0:\n"
                    "    os.setsid(); import time; time.sleep(60)\nRESULT = 1")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _sales() -> pd.DataFrame:
    return pd.DataFrame({"a": [1, 2, 3], "b": [1.5, 2.5, 3.5], "c": ["x", "y", "z"]})


def _app():
    from executor.app import app
    return app


@contextlib.contextmanager
def _client():
    from starlette.testclient import TestClient
    with TestClient(_app()) as client:
        yield client


def _is_secret_env(name: str) -> bool:
    return name.startswith("BRAIN_") or name in SECRET_NAMES


@pytest.fixture
def shared_dir():
    d = Path(tempfile.mkdtemp(prefix="pdc_exec_jobs_"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def executor_env(shared_dir, monkeypatch):
    """The executor's env, set BEFORE the lifespan runs; secrets scrubbed."""
    for name in list(os.environ):
        if _is_secret_env(name):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("EXECUTOR_SHARED_DIR", str(shared_dir))
    monkeypatch.setenv("EXECUTOR_MEM_LIMIT_MB", "2048")
    monkeypatch.setenv("EXECUTOR_MAX_CONCURRENT", "1")
    monkeypatch.setenv("EXECUTOR_MAX_TIMEOUT_S", "600")
    monkeypatch.setenv("EXECUTOR_GRACE_S", "15")
    return shared_dir


def _new_job(shared_dir: Path, dfs=None, sid=None):
    job_id = exec_transport.new_job_id()
    job_dir = exec_transport.create_job_dir(shared_dir, job_id)
    manifest = exec_transport.write_inputs(dfs if dfs is not None else {"sales": _sales()}, job_dir, sid=job_id)
    return job_id, job_dir, manifest


def _body(job_id, code, manifest, kind="PYTHON", timeout_s=30, split_multi_axes=False) -> dict:
    return {"job_id": job_id, "kind": kind, "code": code, "dataframes": manifest,
            "timeout_s": timeout_s, "options": {"split_multi_axes": split_multi_axes}}


def _submit(client, shared_dir, code, kind="PYTHON", timeout_s=30, split_multi_axes=False, dfs=None):
    """POST one job the way the main app will; returns (job_dir, response, wall_s)."""
    job_id, job_dir, manifest = _new_job(shared_dir, dfs)
    t0 = time.monotonic()
    resp = client.post("/execute", json=_body(job_id, code, manifest, kind, timeout_s, split_multi_axes))
    wall = time.monotonic() - t0
    assert resp.status_code == 200, (resp.status_code, resp.text[:2000])
    response = exec_transport.loads(resp.content)
    assert isinstance(response, dict), response
    return job_dir, response, wall


def _decode(response, job_dir, kind="PYTHON", timeout_s=30) -> dict:
    return exec_transport.deserialize_result(response, job_dir, kind, timeout_s)


def _same_uid_pids(exclude=()) -> dict:
    """{pid: cmdline} of every LIVE (non-zombie) process of our euid except `exclude`.

    Zombies are ignored: under `docker run ... python -m pytest` this process
    is pid 1 and never reaps the orphans the sweep kills, so they linger as
    `Z` entries — present in /proc, but not running anything."""
    me = os.geteuid()
    out = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in exclude:
            continue
        try:
            status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        uid = None
        state = ""
        for line in status.splitlines():
            if line.startswith("Uid:"):
                uid = int(line.split()[1])
            elif line.startswith("State:"):
                state = line.split()[1]
        if uid != me or state.startswith("Z"):
            continue
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            cmd = "?"
        out[pid] = cmd
    return out


def _wait_no_strays(exclude, timeout_s=10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    strays = _same_uid_pids(exclude)
    while strays and time.monotonic() < deadline:
        time.sleep(0.2)
        strays = _same_uid_pids(exclude)
    return strays


def _uid_env(uid: int, gid: int) -> dict:
    """A minimal env for a helper process running as `uid`: its own writable
    HOME / DATA_ROOT / caches (logger_utils writes under DATA_ROOT/logs)."""
    d = Path(tempfile.mkdtemp(prefix=f"pdc_uid{uid}_"))
    os.chown(d, uid, gid)
    os.chmod(d, 0o770)
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(d),
        "DATA_ROOT": str(d / "data"),
        "MPLCONFIGDIR": str(d / "mpl"),
        "XDG_CACHE_HOME": str(d / "cache"),
        "MPLBACKEND": "Agg",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "ARROW_IO_THREADS": "1",
        "ARROW_DEFAULT_MEMORY_POOL": "system",
    }


_CREATE_AS_WEB_UID = """
import json, sys
sys.path.insert(0, {root!r})
from pathlib import Path
import pandas as pd
import exec_transport
job_dir = exec_transport.create_job_dir(Path({shared!r}), {job_id!r})
df = pd.DataFrame({{"a": [1, 2, 3], "b": [1.5, 2.5, 3.5], "c": ["x", "y", "z"]}})
manifest = exec_transport.write_inputs({{"sales": df}}, job_dir, sid={job_id!r})
print(json.dumps(manifest))
"""


def _create_job_as_web_uid(shared_dir: Path, job_id: str) -> list:
    """`create_job_dir` + `write_inputs` in a child running as uid 10001 gid 10001."""
    script = _CREATE_AS_WEB_UID.format(root=str(ROOT), shared=str(shared_dir), job_id=job_id)
    proc = subprocess.run([sys.executable, "-c", script], user=WEB_UID, group=SHARED_GID, extra_groups=[],
                          env=_uid_env(WEB_UID, SHARED_GID), cwd=str(ROOT),
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-2000:], proc.stderr[-4000:])
    manifest = exec_transport.loads(proc.stdout.strip().splitlines()[-1])
    assert isinstance(manifest, list) and manifest, manifest
    return manifest


def _rm_rf_as(path: Path, uid: int, gid: int) -> subprocess.CompletedProcess:
    return subprocess.run(["rm", "-rf", str(path)], user=uid, group=gid, extra_groups=[],
                          capture_output=True, text=True, timeout=60)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(method: str, url: str, body=None, timeout=120):
    """Plain urllib round trip; (status, decoded body) — decoded with exec_transport.loads."""
    data = None if body is None else exec_transport.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, exec_transport.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, exec_transport.loads(raw)
        except Exception:
            return e.code, raw.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------
def test_healthz_fields(executor_env):
    import matplotlib
    import plotly
    import pyarrow
    with _client() as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200, resp.text
        body = exec_transport.loads(resp.content)
    assert body.get("ok") is True, body
    assert isinstance(body.get("version"), str), body
    assert isinstance(body.get("build_time"), str), body
    versions = body.get("versions")
    assert isinstance(versions, dict), body
    assert sorted(versions) == ["matplotlib", "numpy", "pandas", "plotly", "pyarrow"], sorted(versions)
    assert versions["pandas"] == pd.__version__, versions
    assert versions["numpy"] == np.__version__, versions
    assert versions["pyarrow"] == pyarrow.__version__, versions
    assert versions["matplotlib"] == matplotlib.__version__, versions
    assert versions["plotly"] == plotly.__version__, versions


# ---------------------------------------------------------------------------
# result kinds through the real runner
# ---------------------------------------------------------------------------
def test_scalar_result(executor_env):
    with _client() as client:
        job_dir, response, wall = _submit(client, executor_env, "RESULT = 42")
    assert response.get("status") == "ok", response
    assert response.get("kind") == "PYTHON", response
    assert isinstance(response.get("elapsed_ms"), (int, float)), response
    assert isinstance(response.get("peak_rss_mb"), (int, float)), response
    assert response.get("exit_code") == 0, response
    decoded = _decode(response, job_dir)
    assert decoded == {"error": None, "result": 42, "preview": 42, "image_base64": None}, decoded
    assert wall < COLD_JOB_BUDGET_S, wall


def test_dataframe_result_reads_back_equal(executor_env):
    code = "RESULT = dfs['sales'].copy()\nRESULT['d'] = RESULT['a'] * 2"
    with _client() as client:
        job_dir, response, _ = _submit(client, executor_env, code)
    assert response.get("status") == "ok", response
    decoded = _decode(response, job_dir)
    expected = _sales()
    expected["d"] = expected["a"] * 2
    result = decoded["result"]
    assert isinstance(result, pd.DataFrame), type(result)
    assert result.equals(expected), result
    assert (job_dir / "out" / "result.parquet").is_file()
    assert isinstance(decoded["preview"], exec_transport.Opaque), decoded["preview"]


def test_plotly_figure_uses_the_offline_bundle(executor_env):
    code = "fig = px.bar(dfs['sales'], x='c', y='a')"
    with _client() as client:
        job_dir, response, _ = _submit(client, executor_env, code, kind="PLOT")
    assert response.get("status") == "ok", response
    decoded = _decode(response, job_dir, "PLOT")
    assert decoded.get("ok") is True, decoded
    assert decoded.get("is_plotly") is True, decoded
    html = decoded.get("plotly_html")
    assert isinstance(html, str) and html, decoded
    assert "/static/vendor/plotly/plotly.min.js" in html, html[:500]
    assert "cdn.plot.ly" not in html, "plotly HTML references the CDN"
    assert "error" not in decoded, sorted(decoded)


def test_matplotlib_figure_is_a_png(executor_env):
    code = "plt.figure()\nplt.bar(dfs['sales']['c'], dfs['sales']['a'])"
    with _client() as client:
        job_dir, response, _ = _submit(client, executor_env, code, kind="PLOT")
    assert response.get("status") == "ok", response
    decoded = _decode(response, job_dir, "PLOT")
    assert decoded.get("ok") is True, decoded
    assert decoded.get("is_plotly") is False, decoded
    png = base64.b64decode(decoded["image"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n", png[:8]
    assert "error" not in decoded, sorted(decoded)


def test_split_multi_axes_yields_two_charts(executor_env):
    code = ("fig, axes = plt.subplots(1, 2)\n"
            "axes[0].bar(dfs['sales']['c'], dfs['sales']['a'])\n"
            "axes[1].plot(dfs['sales']['a'], dfs['sales']['b'])")
    with _client() as client:
        job_dir, response, _ = _submit(client, executor_env, code, kind="PLOT", split_multi_axes=True)
    assert response.get("status") == "ok", response
    decoded = _decode(response, job_dir, "PLOT")
    assert decoded.get("ok") is True, decoded
    charts = decoded.get("multi_charts")
    assert isinstance(charts, list) and len(charts) == 2, decoded
    for chart in charts:
        png = base64.b64decode(chart["image"])
        assert png[:8] == b"\x89PNG\r\n\x1a\n", png[:8]


def test_multi_axes_without_split_is_the_multi_axes_error(executor_env):
    code = ("fig, axes = plt.subplots(1, 2)\n"
            "axes[0].bar(dfs['sales']['c'], dfs['sales']['a'])\n"
            "axes[1].plot(dfs['sales']['a'], dfs['sales']['b'])")
    with _client() as client:
        job_dir, response, _ = _submit(client, executor_env, code, kind="PLOT")
    assert response.get("status") == "error", response
    decoded = _decode(response, job_dir, "PLOT")
    assert decoded.get("ok") is False and decoded.get("multi_axes") is True, decoded
    assert decoded.get("error", "").startswith("MultiAxesChartError:"), decoded
    assert "is_plotly" not in decoded, sorted(decoded)


def test_styler_result_ships_structural_html(executor_env):
    code = "RESULT = dfs['sales'][['a', 'b']].style.background_gradient(cmap='Blues').format('{:.2f}')"
    with _client() as client:
        job_dir, response, _ = _submit(client, executor_env, code)
    assert response.get("status") == "ok", response
    decoded = _decode(response, job_dir)
    result = decoded["result"]
    assert isinstance(result, exec_transport.StyledFrame), type(result)
    assert result.data.equals(_sales()[["a", "b"]]), result.data
    html = result.to_html()
    assert "<style" in html, html[:300]
    assert "#T_" in html, html[:300]
    assert "background-color" in html, html[:300]
    assert "1.50" in html and "2.50" in html, html[:300]


def test_print_is_captured_in_stdout(executor_env):
    with _client() as client:
        job_dir, response, _ = _submit(client, executor_env, 'print("hello")\nRESULT = 1')
    assert response.get("status") == "ok", response
    assert response.get("stdout") == "hello\n", response.get("stdout")
    assert _decode(response, job_dir)["result"] == 1


def test_syntax_error_is_passed_through(executor_env):
    with _client() as client:
        job_dir, response, _ = _submit(client, executor_env, "RESULT = (")
    assert response.get("status") == "error", response
    payload = response.get("payload")
    assert isinstance(payload, dict), response
    assert payload.get("error", "").startswith("SyntaxError:"), payload
    decoded = _decode(response, job_dir)
    assert decoded.get("error", "").startswith("SyntaxError:"), decoded


# ---------------------------------------------------------------------------
# timeouts
# ---------------------------------------------------------------------------
def _healthz_probe(client, holder: dict, delay_s: float = 0.5):
    time.sleep(delay_s)
    t0 = time.monotonic()
    resp = client.get("/healthz")
    holder["status"] = resp.status_code
    holder["seconds"] = time.monotonic() - t0


@pytest.mark.parametrize("kind", ["PYTHON", "PLOT"])
def test_while_true_times_out_with_the_byte_identical_text(executor_env, kind):
    with _client() as client:
        _, _, baseline = _submit(client, executor_env, "RESULT = 1")

        probe = {}
        t = threading.Thread(target=_healthz_probe, args=(client, probe), daemon=True)
        t.start()
        job_dir, response, wall = _submit(client, executor_env, "while True: pass", kind=kind, timeout_s=2)
        t.join(timeout=30)

        _, _, after = _submit(client, executor_env, "RESULT = 1")

    assert response.get("status") == "timeout", response
    assert wall < 2 + 15 + COLD_JOB_BUDGET_S, wall
    text = TIMEOUT_TEXT.format(timeout=2)
    decoded = _decode(response, job_dir, kind, 2)
    if kind == "PYTHON":
        assert decoded == {"error": text}, decoded
    else:
        assert decoded == {"ok": False, "error": text, "trace": ""}, decoded
    assert probe.get("status") == 200, probe
    assert probe.get("seconds", 99) < 1.0, probe
    # the next job starts immediately — not a grace period later
    assert after < max(2 * baseline, baseline + 3.0), (after, baseline)


def test_queued_job_expires_without_running_when_the_slot_is_busy(executor_env):
    """MAX_CONCURRENT=1: job B (timeout_s=1) queued behind a 4 s job A comes
    back `timeout` before A finishes and never executes its code."""
    # Jobs are created AFTER the service starts, as in production: a job
    # directory present at startup is locked (chmod 000) before any job runs.
    code_a = "import time\ntime.sleep(4)\nRESULT = 'A'"
    holder = {}

    with _client() as client:
        job_a, dir_a, manifest_a = _new_job(executor_env)
        job_b, dir_b, manifest_b = _new_job(executor_env)
        marker = dir_b / "in" / "ran.txt"
        code_b = f"open({str(marker)!r}, 'w').write('ran')\nRESULT = 'B'"
        # warm the runner imports so A holds the slot for ~4 s of real sleep
        _submit(client, executor_env, "RESULT = 0")

        def _run_a():
            resp = client.post("/execute", json=_body(job_a, code_a, manifest_a, timeout_s=30))
            holder["a"] = (resp.status_code, exec_transport.loads(resp.content))

        ta = threading.Thread(target=_run_a, daemon=True)
        ta.start()
        time.sleep(1.0)
        t0 = time.monotonic()
        resp_b = client.post("/execute", json=_body(job_b, code_b, manifest_b, timeout_s=1))
        wall_b = time.monotonic() - t0
        ta.join(timeout=60)

    assert resp_b.status_code == 200, resp_b.text
    response_b = exec_transport.loads(resp_b.content)
    assert response_b.get("status") == "timeout", response_b
    assert wall_b < 3.0, wall_b
    assert not marker.exists(), "job B ran although its deadline expired while queued"
    assert holder["a"][0] == 200 and holder["a"][1].get("status") == "ok", holder


# ---------------------------------------------------------------------------
# memory, threads, signals, forks
# ---------------------------------------------------------------------------
def test_memory_limit_is_an_ordinary_memory_error(executor_env, monkeypatch):
    """RLIMIT_AS makes the interpreter RAISE; nothing is killed, so the
    status is `error` with a `MemoryError` text. `crashed` would mean the
    runner was too starved to write its response — a failure here."""
    monkeypatch.setenv("EXECUTOR_MEM_LIMIT_MB", "1024")
    code = "x=[]\nwhile True:\n    x.append(bytearray(50_000_000))"
    with _client() as client:
        job_dir, response, _ = _submit(client, executor_env, code, timeout_s=120)
        health = client.get("/healthz").status_code
    assert response.get("status") == "error", response
    payload = response.get("payload")
    assert isinstance(payload, dict), response
    assert payload.get("error", "").startswith("MemoryError"), payload
    assert _decode(response, job_dir)["error"].startswith("MemoryError")
    assert health == 200


def test_threadpool_inside_generated_code_works(executor_env):
    code = ("from concurrent.futures import ThreadPoolExecutor\n"
            "def sq(i):\n    return i * i\n"
            "with ThreadPoolExecutor(8) as ex:\n"
            "    RESULT = sum(ex.map(sq, range(8)))")
    with _client() as client:
        job_dir, response, _ = _submit(client, executor_env, code)
    assert response.get("status") == "ok", response
    assert _decode(response, job_dir)["result"] == 140


def test_sigkill_inside_the_runner_is_reported_as_killed(executor_env):
    code = "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)"
    with _client() as client:
        job_dir, response, _ = _submit(client, executor_env, code)
        health = client.get("/healthz").status_code
    assert response.get("status") == "killed", response
    decoded = _decode(response, job_dir)
    assert decoded == {"error": MEMORY_TEXT}, decoded
    assert health == 200


def test_sigsegv_inside_the_runner_is_reported_as_crashed(executor_env):
    code = "import os, signal\nos.kill(os.getpid(), signal.SIGSEGV)"
    with _client() as client:
        job_dir, response, _ = _submit(client, executor_env, code)
    assert response.get("status") == "crashed", response
    assert response.get("signal") == 11, response
    decoded = _decode(response, job_dir)
    err = decoded.get("error", "")
    assert err.startswith("ExecutorCrashError:"), decoded
    assert "signal=11" in err, err


def test_junk_on_the_response_fd_then_exit_zero_is_crashed(executor_env):
    code = ('import os\nfd = int(os.environ["EXECUTOR_RESPONSE_FD"])\n'
            'os.write(fd, b"junk{{{not json")\nos._exit(0)')
    with _client() as client:
        job_dir, response, _ = _submit(client, executor_env, code)
    assert response.get("status") == "crashed", response
    assert response.get("reason") == "response_invalid", response
    assert response.get("exit_code") == 0, response
    decoded = _decode(response, job_dir)
    assert decoded.get("error", "").startswith("ExecutorCrashError:"), decoded


@pytest.mark.parametrize("code", [FORK_CODE, FORK_SETSID_CODE], ids=["fork", "fork_setsid"])
def test_forked_child_is_gone_after_the_request(executor_env, code):
    """A forked grandchild that sleeps must not hold the request open, and
    after the response no process of the executor's uid other than this
    process (the app, under TestClient) and its parent may exist."""
    exclude = {os.getpid(), os.getppid()}
    strays_before = _wait_no_strays(exclude, timeout_s=10)
    assert strays_before == {}, strays_before
    with _client() as client:
        job_dir, response, wall = _submit(client, executor_env, code, timeout_s=30)
        strays = _wait_no_strays(exclude, timeout_s=10)
        health = client.get("/healthz").status_code
    assert response.get("status") == "ok", response
    assert _decode(response, job_dir)["result"] == 1
    assert wall < 10 + COLD_JOB_BUDGET_S, wall
    assert strays == {}, f"processes of uid {os.geteuid()} survived the job: {strays}"
    assert health == 200


# ---------------------------------------------------------------------------
# env isolation
# ---------------------------------------------------------------------------
def test_generated_code_sees_a_clean_allowlisted_environment(executor_env, monkeypatch):
    monkeypatch.setenv("PDC_TEST_CANARY", "leak-me")
    with _client() as client:
        job_dir, response, _ = _submit(client, executor_env, "import os\nRESULT = dict(os.environ)")
    assert response.get("status") == "ok", response
    env = _decode(response, job_dir)["result"]
    assert isinstance(env, dict), env
    leaked = sorted(k for k in env if _is_secret_env(k) or k == "PDC_TEST_CANARY")
    assert leaked == [], leaked
    assert env.get("OMP_NUM_THREADS") == "1", env
    assert env.get("OPENBLAS_NUM_THREADS") == "1", env
    assert env.get("MKL_NUM_THREADS") == "1", env
    assert env.get("MPLBACKEND") == "Agg", env
    assert "EXECUTOR_RESPONSE_FD" in env, sorted(env)
    assert "PYTHONPATH" not in env, sorted(env)


@pytest.mark.parametrize("name", ["BRAIN_TENANT_TOKEN", "BRAIN_URL", "SECRET_KEY",
                                  "CLIENT_ENCRYPTION_KEY", "LOCAL_ADMIN_PASSWORD", "GCS_UPLOAD_BUCKET"])
def test_app_refuses_to_start_with_a_secret_in_its_env(executor_env, monkeypatch, name):
    """Both halves of the refusal, without guessing at exception plumbing.

    The exception TYPE that surfaces through `TestClient.__enter__` is anyio's
    business (today a `CancelledError`: the blocking portal reports the death
    of the lifespan task that way), so it is deliberately NOT asserted on.
    What is asserted is what the requirement actually is: the guard raises
    SystemExit, and startup does not complete — the refusal line is logged
    and the app never serves. The real container exits with code 3.
    """
    import executor.app as executor_app

    monkeypatch.setenv(name, "x")
    logged: list = []
    monkeypatch.setattr(executor_app, "log_with_sid",
                        lambda sid, level, message, *a, **k: logged.append(message))

    # (a) the guard itself
    with pytest.raises(SystemExit):
        executor_app._refuse_on_secret_env()
    assert f"EXECUTOR_REFUSED_SECRET_ENV name={name}" in logged, logged

    # (b) startup does not complete
    logged.clear()
    with pytest.raises(BaseException) as excinfo:
        with _client():
            pass
    assert not isinstance(excinfo.value, KeyboardInterrupt), repr(excinfo.value)
    assert f"EXECUTOR_REFUSED_SECRET_ENV name={name}" in logged, logged
    assert "EXECUTOR_START" not in " ".join(logged), logged


@pytest.mark.parametrize("name", ["BRAIN_TENANT_TOKEN", "SECRET_KEY",
                                  "CLIENT_ENCRYPTION_KEY", "GCS_UPLOAD_BUCKET"])
def test_secret_guard_ignores_an_empty_value(executor_env, monkeypatch, name):
    """An EMPTY secret var is not a secret: the guard must return normally."""
    import executor.app as executor_app

    monkeypatch.setenv(name, "")
    assert executor_app._refuse_on_secret_env() is None


def test_app_starts_with_an_empty_secret_value(executor_env, monkeypatch):
    monkeypatch.setenv("BRAIN_TENANT_TOKEN", "")
    with _client() as client:
        assert client.get("/healthz").status_code == 200


# ---------------------------------------------------------------------------
# orphan sweep
# ---------------------------------------------------------------------------
def test_orphan_sweep_removes_old_job_dirs_and_old_strays(executor_env):
    """INVERTED on purpose: an aged non-job entry is now REMOVED.

    The previous version asserted that `keep_me` SURVIVED, and that
    expectation was wrong. `create_job_dir` gives the shared root group
    write — which is what lets this service clear an abandoned job directory
    in it — and the same permission lets GENERATED CODE create files and
    directories directly in that root. While both sweeps skipped every name
    that was not job-id shaped, nothing ever removed those: the volume is
    named and disk-backed, so a stash outlived restarts, an image upgrade and
    any number of intervening jobs, and a later job run for a different user
    read it back. The root is supposed to hold nothing but job directories,
    so an aged entry that is not one is debris or a deliberate stash.

    The age threshold is unchanged, and the ownership rule here is the
    mirror of the main app's — see
    `test_the_sandbox_sweep_ignores_ownership`.
    """
    shared = executor_env
    old = exec_transport.create_job_dir(shared, exec_transport.new_job_id())
    (old / "in" / "0.parquet").write_bytes(b"x")
    fresh = exec_transport.create_job_dir(shared, exec_transport.new_job_id())
    (fresh / "in" / "0.parquet").write_bytes(b"x")
    keep = shared / "keep_me"        # a stray, not a job — now swept
    keep.mkdir()
    old_uid = None
    if os.geteuid() == 0:
        old_uid = exec_transport.create_job_dir(shared, exec_transport.new_job_id())
        (old_uid / "in" / "0.parquet").write_bytes(b"x")
        os.chown(old_uid / "in" / "0.parquet", WEB_UID, SHARED_GID)
        os.chown(old_uid / "in", WEB_UID, SHARED_GID)
        os.chown(old_uid, WEB_UID, SHARED_GID)
    two_hours_ago = time.time() - 2 * 3600
    for p in (old, keep, old_uid):
        if p is not None:
            os.utime(p, (two_hours_ago, two_hours_ago))
    with _client() as client:
        assert client.get("/healthz").status_code == 200
    assert not old.exists(), "old job dir survived the startup sweep"
    assert fresh.is_dir(), "fresh job dir was swept"
    assert not keep.exists(), "an aged entry that is not a job directory survived"
    if old_uid is not None:
        assert not old_uid.exists(), "old job dir with uid-10001 inputs survived the sweep"


def test_startup_locks_the_job_dirs_already_present(executor_env):
    """A job directory present when the service starts is chmod 000 before the
    first job runs (this uid can do that only to what it owns — here the test
    creates it, so it does), then left to the age-based sweep."""
    shared = executor_env
    present = exec_transport.create_job_dir(shared, exec_transport.new_job_id())
    (present / "in" / "0.parquet").write_bytes(b"x")
    try:
        with _client() as client:
            assert client.get("/healthz").status_code == 200
        mode = stat.S_IMODE(os.lstat(present).st_mode)
        assert mode == 0, oct(mode)
    finally:
        with contextlib.suppress(OSError):
            os.chmod(present, 0o770)


def test_the_startup_lock_counts_what_it_may_not_chmod(executor_env, monkeypatch, caplog):
    """A job directory owned by the web uid cannot be chmodded from this uid:
    the lock counts it and moves on — never raises."""
    import logging
    from executor import app as app_module
    shared = executor_env
    exec_transport.create_job_dir(shared, exec_transport.new_job_id())

    def refused(path, mode):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(app_module.os, "chmod", refused)
    with caplog.at_level(logging.INFO):
        locked = app_module._lock_preexisting_job_dirs(shared)
    assert locked == 0
    assert "EXEC_ORPHAN_LOCKED count=0 not_owned=1" in caplog.text


def test_a_finished_job_restarts_its_directory_age(executor_env):
    """The web service reads `out/` AFTER the answer. A job that ran longer
    than the sweep threshold must not be swept while it does, so completion
    sets the directory's mtime to now."""
    with _client() as client:
        job_id, job_dir, manifest = _new_job(executor_env)
        long_ago = time.time() - 3600
        os.utime(job_dir, (long_ago, long_ago))
        resp = client.post("/execute", json=_body(job_id, "RESULT = 1", manifest,
                                                  timeout_s=COLD_JOB_BUDGET_S))
        assert resp.status_code == 200, resp.text
    age = time.time() - os.lstat(job_dir).st_mtime
    assert age < 120, age


def test_the_sweep_takes_a_job_dir_after_five_minutes_but_not_one_in_flight(executor_env):
    from executor import app as app_module
    shared = executor_env
    aged = exec_transport.create_job_dir(shared, exec_transport.new_job_id())
    running_id = exec_transport.new_job_id()
    running = exec_transport.create_job_dir(shared, running_id)
    young = exec_transport.create_job_dir(shared, exec_transport.new_job_id())
    six_minutes_ago = time.time() - 6 * 60
    for p in (aged, running):
        os.utime(p, (six_minutes_ago, six_minutes_ago))
    four_minutes_ago = time.time() - 4 * 60
    os.utime(young, (four_minutes_ago, four_minutes_ago))
    with app_module._INFLIGHT_LOCK:
        app_module._ACTIVE_JOBS.add(running_id)
    try:
        app_module._sweep_orphans(shared)
    finally:
        with app_module._INFLIGHT_LOCK:
            app_module._ACTIVE_JOBS.discard(running_id)
    assert not aged.exists(), "a job dir older than five minutes survived"
    assert running.is_dir(), "a job in flight was swept"
    assert young.is_dir(), "a job dir younger than five minutes was swept"


# ---------------------------------------------------------------------------
# hostile requests
# ---------------------------------------------------------------------------
def test_bad_job_id_is_rejected(executor_env):
    with _client() as client:
        resp = client.post("/execute", json=_body("ABC", "RESULT = 1", []))
        assert resp.status_code in (400, 422), (resp.status_code, resp.text)
        resp = client.post("/execute", json=_body("../" + "0" * 30, "RESULT = 1", []))
        assert resp.status_code in (400, 422), (resp.status_code, resp.text)


def test_missing_or_symlinked_job_dir_is_400(executor_env):
    missing = exec_transport.new_job_id()
    linked = exec_transport.new_job_id()
    elsewhere = Path(tempfile.mkdtemp(prefix="pdc_elsewhere_"))
    (elsewhere / "in").mkdir()
    os.symlink(elsewhere, executor_env / linked, target_is_directory=True)
    with _client() as client:
        resp = client.post("/execute", json=_body(missing, "RESULT = 1", []))
        assert resp.status_code == 400, (resp.status_code, resp.text)
        assert "JOB_DIR_INVALID" in resp.text, resp.text
        resp = client.post("/execute", json=_body(linked, "RESULT = 1", []))
        assert resp.status_code == 400, (resp.status_code, resp.text)
        assert "JOB_DIR_INVALID" in resp.text, resp.text
    assert (elsewhere / "in").is_dir(), "the symlink target was touched"


@pytest.mark.parametrize("entry", [
    {"name": "t", "path": "in/../job.json", "format": "parquet"},
    {"name": "t", "path": "/etc/passwd", "format": "parquet"},
    {"name": "t", "path": "out/0.parquet", "format": "parquet"},
    {"name": "t", "path": "in/0.parquet", "format": "json"},
    {"name": "t", "path": "in/sub/0.parquet", "format": "parquet"},
])
def test_hostile_dataframes_path_is_rejected_before_spawning(executor_env, entry):
    # Created AFTER the service starts: a job directory present at startup is
    # locked (chmod 000) before any job runs.
    with _client() as client:
        job_id, job_dir, _ = _new_job(executor_env)
        resp = client.post("/execute", json=_body(job_id, "RESULT = 1", [entry]))
    assert resp.status_code in (400, 422), (resp.status_code, resp.text)
    assert not (job_dir / "job.json").exists(), "job.json was written for a rejected request"


@pytest.mark.parametrize("timeout_s", [0, -1, 601, 10 ** 9])
def test_timeout_outside_the_allowed_range_is_rejected(executor_env, timeout_s):
    job_id, _, manifest = _new_job(executor_env)
    with _client() as client:
        resp = client.post("/execute", json=_body(job_id, "RESULT = 1", manifest, timeout_s=timeout_s))
    assert resp.status_code in (400, 422), (resp.status_code, resp.text)


def test_unknown_kind_is_rejected(executor_env):
    job_id, _, manifest = _new_job(executor_env)
    with _client() as client:
        resp = client.post("/execute", json=_body(job_id, "RESULT = 1", manifest, kind="SHELL"))
    assert resp.status_code in (400, 422), (resp.status_code, resp.text)


def test_execute_without_a_completed_lifespan_is_503(executor_env):
    """A BARE `TestClient(app)` does NOT run the lifespan, and `/execute` must
    refuse rather than build its own state: serving here would mean serving
    without the secret-env refusal and the shared-directory check.

    `_STATE` is MODULE level and an earlier test's lifespan leaves its config
    behind, so it is cleared before the call (and restored after) — without
    the clear this test would pass vacuously against an already-ready app.
    """
    from starlette.testclient import TestClient

    from executor import app as executor_app

    job_id, job_dir, manifest = _new_job(executor_env)
    saved = dict(executor_app._STATE)
    executor_app._STATE.clear()
    try:
        client = TestClient(_app())     # NOT the `with` form: no lifespan runs
        resp = client.post("/execute", json=_body(job_id, "RESULT = 1", manifest))
    finally:
        executor_app._STATE.clear()
        executor_app._STATE.update(saved)
    assert resp.status_code == 503, (resp.status_code, resp.text)
    assert "EXECUTOR_NOT_READY" in resp.text, resp.text
    assert not (job_dir / "job.json").exists(), "job.json was written without a lifespan"
    assert not (job_dir / "out").exists(), "out/ was created without a lifespan"


# ---------------------------------------------------------------------------
# two-uid ownership — root only
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.geteuid() != 0, reason="setuid to 10001/10002 needs root (the Dockerfile test stage)")
def test_two_uid_web_creates_the_job_dir_and_the_executor_writes_the_result(shared_dir):
    shared = shared_dir
    os.chown(shared, 0, SHARED_GID)
    os.chmod(shared, 0o2770)
    job_id = exec_transport.new_job_id()
    manifest = _create_job_as_web_uid(shared, job_id)
    job_dir = shared / job_id
    assert job_dir.is_dir()
    assert os.stat(job_dir).st_uid == WEB_UID
    assert os.stat(job_dir).st_mode & 0o7777 == 0o2770, oct(os.stat(job_dir).st_mode)

    job_json = job_dir / "job.json"
    job_json.write_text(exec_transport.dumps(
        _body(job_id, "RESULT = dfs['sales'].copy()", manifest, timeout_s=30)), encoding="utf-8")
    os.chown(job_json, EXEC_UID, SHARED_GID)
    os.chmod(job_json, 0o660)

    r, w = os.pipe()
    env = _uid_env(EXEC_UID, SHARED_GID)
    env.update({"EXECUTOR_RESPONSE_FD": str(w), "EXECUTOR_MEM_LIMIT_MB": "2048"})
    proc = subprocess.Popen([sys.executable, "-I", "-u", str(RUNNER), str(job_json)],
                            user=EXEC_UID, group=SHARED_GID, extra_groups=[], pass_fds=(w,),
                            env=env, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    os.close(w)
    chunks = []

    def _read_response():
        with os.fdopen(r, "rb") as fh:
            chunks.append(fh.read())

    reader = threading.Thread(target=_read_response, daemon=True)
    reader.start()
    try:
        out, err = proc.communicate(timeout=300)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    reader.join(timeout=30)
    assert proc.returncode == 0, (proc.returncode, out[-2000:], err[-4000:])
    assert chunks and chunks[0], (out[-2000:], err[-4000:])
    response = exec_transport.loads(chunks[0])
    assert response.get("status") == "ok", (response, err[-4000:])

    result_file = job_dir / "out" / "result.parquet"
    assert result_file.is_file(), sorted(p.name for p in (job_dir / "out").iterdir()) if (job_dir / "out").exists() else "no out/"
    st = os.stat(result_file)
    assert stat.S_IMODE(st.st_mode) == 0o660, oct(st.st_mode)
    assert st.st_gid == SHARED_GID, st.st_gid
    assert st.st_uid == EXEC_UID, st.st_uid
    out_st = os.stat(job_dir / "out")
    assert out_st.st_mode & 0o7777 == 0o2770, oct(out_st.st_mode)
    assert out_st.st_gid == SHARED_GID, out_st.st_gid

    decoded = exec_transport.deserialize_result(response, job_dir, "PYTHON", 30)
    assert isinstance(decoded.get("result"), pd.DataFrame), decoded
    assert decoded["result"].equals(_sales())

    rm = _rm_rf_as(job_dir, WEB_UID, SHARED_GID)
    assert rm.returncode == 0, (rm.returncode, rm.stderr)
    assert not job_dir.exists(), "uid 10001 could not remove the job dir the executor wrote into"


@pytest.mark.skipif(os.geteuid() != 0, reason="setuid to 10001/10002 needs root (the Dockerfile test stage)")
def test_two_uid_executor_can_remove_an_orphan_the_web_uid_created(shared_dir):
    """The orphan sweep runs as uid 10002: it needs group-write on `in/` —
    the explicit 0o2770 chmod in `create_job_dir` (a 0022 umask would leave
    `in/` at 0755 and the sweep would fail on the first file inside)."""
    shared = shared_dir
    os.chown(shared, 0, SHARED_GID)
    os.chmod(shared, 0o2770)
    job_id = exec_transport.new_job_id()
    _create_job_as_web_uid(shared, job_id)
    job_dir = shared / job_id
    in_st = os.stat(job_dir / "in")
    assert in_st.st_mode & 0o7777 == 0o2770, oct(in_st.st_mode)
    assert any(p.is_file() for p in (job_dir / "in").iterdir())
    rm = _rm_rf_as(job_dir, EXEC_UID, SHARED_GID)
    assert rm.returncode == 0, (rm.returncode, rm.stderr)
    assert not job_dir.exists()


# ---------------------------------------------------------------------------
# the same-uid sweep against the REAL process tree
# ---------------------------------------------------------------------------
def test_sweep_spares_the_uvicorn_process_and_its_parent(shared_dir, monkeypatch):
    """Launch the app exactly as the Dockerfile CMD does (uvicorn subprocess,
    parent = this pytest process, same uid), run the forked-child job over
    plain HTTP and assert: the request returns, the grandchild is gone, the
    uvicorn process AND its parent are still alive, a second job still
    answers. The sweep must exclude both `os.getpid()` and `os.getppid()`."""
    for name in list(os.environ):
        if _is_secret_env(name):
            monkeypatch.delenv(name, raising=False)
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not _is_secret_env(k)}
    env.update({
        "EXECUTOR_SHARED_DIR": str(shared_dir),
        "EXECUTOR_MEM_LIMIT_MB": "2048",
        "EXECUTOR_MAX_CONCURRENT": "1",
        "EXECUTOR_MAX_TIMEOUT_S": "600",
        "EXECUTOR_GRACE_S": "15",
    })
    log_path = shared_dir.parent / f"uvicorn_{port}.log"
    log = open(log_path, "wb")
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "executor.app:app", "--host", "127.0.0.1",
         "--port", str(port), "--workers", "1"],
        cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 90
        healthy = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                status, body = _http("GET", base + "/healthz", timeout=5)
                if status == 200 and body.get("ok") is True:
                    healthy = body
                    break
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            time.sleep(0.25)
        log.flush()
        assert healthy is not None, (
            f"uvicorn did not answer /healthz (exit={proc.poll()}); log tail:\n"
            + log_path.read_text(encoding="utf-8", errors="replace")[-4000:])

        exclude = {os.getpid(), os.getppid(), proc.pid}
        strays_before = _wait_no_strays(exclude, timeout_s=10)
        assert strays_before == {}, strays_before

        job_id, job_dir, manifest = _new_job(shared_dir)
        t0 = time.monotonic()
        status, response = _http("POST", base + "/execute", _body(job_id, FORK_CODE, manifest, timeout_s=30),
                                 timeout=30 + 15 + COLD_JOB_BUDGET_S)
        wall = time.monotonic() - t0
        assert status == 200, (status, response)
        assert response.get("status") == "ok", response
        assert exec_transport.deserialize_result(response, job_dir, "PYTHON", 30)["result"] == 1
        assert wall < 10 + COLD_JOB_BUDGET_S, wall

        strays = _wait_no_strays(exclude, timeout_s=10)
        assert strays == {}, f"the forked grandchild (or another same-uid process) survived: {strays}"

        # the app and its parent are both still alive
        assert proc.poll() is None, f"uvicorn died (exit={proc.returncode}); it swept itself or was swept"
        os.kill(proc.pid, 0)
        os.kill(os.getppid(), 0)
        os.kill(os.getpid(), 0)

        job_id2, job_dir2, manifest2 = _new_job(shared_dir)
        status2, response2 = _http("POST", base + "/execute", _body(job_id2, "RESULT = 2", manifest2, timeout_s=30),
                                   timeout=30 + 15 + COLD_JOB_BUDGET_S)
        assert status2 == 200, (status2, response2)
        assert response2.get("status") == "ok", response2
        assert exec_transport.deserialize_result(response2, job_dir2, "PYTHON", 30)["result"] == 2
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            proc.wait(timeout=10)
        log.close()


# ---------------------------------------------------------------------------
# startup write probe on the shared job volume
# ---------------------------------------------------------------------------
def _write_probe_guard():
    """The startup guard that refuses an unwritable shared job directory.

    Resolved by name so a missing implementation names itself; called with or
    without the directory argument, whichever the guard declares.
    """
    import inspect

    import executor.app as executor_app

    guard = getattr(executor_app, "_refuse_on_unwritable_shared_dir", None)
    assert callable(guard), ("executor/app.py must expose "
                             "_refuse_on_unwritable_shared_dir(): a shared job "
                             "directory it cannot write to means every job "
                             "fails, so it must refuse to serve")
    takes_argument = bool(inspect.signature(guard).parameters)
    return guard, takes_argument


def _run_write_probe(shared_dir: Path):
    guard, takes_argument = _write_probe_guard()
    return guard(shared_dir) if takes_argument else guard()


def test_the_write_probe_accepts_a_writable_shared_dir(executor_env):
    assert _run_write_probe(executor_env) is None
    leftovers = sorted(p.name for p in executor_env.iterdir())
    assert leftovers == [], f"the probe left files behind: {leftovers}"


def test_startup_refuses_when_the_shared_dir_cannot_be_written(executor_env, monkeypatch):
    """Root ignores mode bits, so this case removes the directory instead: the
    probe's write fails the same way, and the refusal is what is under test."""
    import executor.app as executor_app

    logged: list = []
    monkeypatch.setattr(executor_app, "log_with_sid",
                        lambda sid, level, message, *a, **k: logged.append(message))
    gone = executor_env / "not_there"

    with pytest.raises(SystemExit) as excinfo:
        _run_write_probe(gone)

    code = excinfo.value.code
    assert code not in (0, None), code
    refusals = [m for m in logged if m.startswith("EXECUTOR_SHARED_DIR_NOT_WRITABLE")]
    assert len(refusals) == 1, logged


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory mode bits")
def test_startup_refuses_on_a_read_only_shared_dir(executor_env, monkeypatch):
    import executor.app as executor_app

    logged: list = []
    monkeypatch.setattr(executor_app, "log_with_sid",
                        lambda sid, level, message, *a, **k: logged.append(message))
    read_only = executor_env / "ro"
    read_only.mkdir()
    os.chmod(read_only, 0o500)
    try:
        with pytest.raises(SystemExit):
            _run_write_probe(read_only)
    finally:
        os.chmod(read_only, 0o700)
    refusals = [m for m in logged if m.startswith("EXECUTOR_SHARED_DIR_NOT_WRITABLE")]
    assert len(refusals) == 1, logged


# ---------------------------------------------------------------------------
# concurrency above one: loud, and the stray sweep stands down
# ---------------------------------------------------------------------------
def test_a_concurrency_setting_above_one_is_logged_as_unsafe(executor_env, monkeypatch):
    """More than one job at a time gives up the one-process-per-uid guarantee
    the stray sweep rests on, so it must never be silent."""
    import executor.app as executor_app

    monkeypatch.setenv("EXECUTOR_MAX_CONCURRENT", "2")
    logged: list = []
    monkeypatch.setattr(executor_app, "log_with_sid",
                        lambda sid, level, message, *a, **k: logged.append(message))

    with _client() as client:
        assert client.get("/healthz").status_code == 200

    unsafe = [m for m in logged if m.startswith("EXECUTOR_CONCURRENCY_UNSAFE")]
    assert len(unsafe) == 1, logged
    assert "max_concurrent=2" in unsafe[0], unsafe[0]


def test_the_default_concurrency_is_not_reported_as_unsafe(executor_env, monkeypatch):
    import executor.app as executor_app

    logged: list = []
    monkeypatch.setattr(executor_app, "log_with_sid",
                        lambda sid, level, message, *a, **k: logged.append(message))

    with _client() as client:
        assert client.get("/healthz").status_code == 200

    unsafe = [m for m in logged if m.startswith("EXECUTOR_CONCURRENCY_UNSAFE")]
    assert unsafe == [], logged


def test_two_jobs_at_once_both_answer_with_the_stray_sweep_skipped(executor_env, monkeypatch):
    import executor.app as executor_app

    monkeypatch.setenv("EXECUTOR_MAX_CONCURRENT", "2")
    logged: list = []
    monkeypatch.setattr(executor_app, "log_with_sid",
                        lambda sid, level, message, *a, **k: logged.append(message))

    # Jobs are created AFTER the service starts: a job directory present at
    # startup is locked (chmod 000) before any job runs.
    code = "import time\ntime.sleep(3)\nRESULT = 'done'"
    results: dict = {}

    with _client() as client:
        job_a, dir_a, manifest_a = _new_job(executor_env)
        job_b, dir_b, manifest_b = _new_job(executor_env)
        _submit(client, executor_env, "RESULT = 0")      # warm the runner imports

        def _post(tag, job_id, manifest):
            resp = client.post("/execute", json=_body(job_id, code, manifest, timeout_s=90))
            results[tag] = (resp.status_code, exec_transport.loads(resp.content))

        first = threading.Thread(target=_post, args=("a", job_a, manifest_a), daemon=True)
        second = threading.Thread(target=_post, args=("b", job_b, manifest_b), daemon=True)
        first.start()
        time.sleep(0.3)
        second.start()
        first.join(timeout=180)
        second.join(timeout=180)

    for tag, job_dir in (("a", dir_a), ("b", dir_b)):
        status_code, response = results.get(tag, (None, None))
        assert status_code == 200, (tag, status_code, response)
        assert response.get("status") == "ok", (tag, response)
        decoded = _decode(response, job_dir)
        assert decoded.get("result") == "done", (tag, decoded)

    skipped = [m for m in logged if m.startswith("EXEC_STRAY_SWEEP_SKIPPED")]
    assert skipped, "the stray sweep ran while a sibling job was in flight"


# ---------------------------------------------------------------------------
# the sweep repairs the modes generated code can leave behind
# ---------------------------------------------------------------------------
def test_the_sweep_removes_an_orphan_whose_output_dir_was_locked(executor_env):
    """Generated code can `chmod 0500` a directory it created under `out/`,
    which makes a plain `rmtree` fail for the identity that owns it — the
    sweep must repair the mode and retry instead of leaking the job forever.
    (As root the modes are advisory; the case is a pin for the runtime
    identity, where they are not.)"""
    shared = executor_env
    orphan = exec_transport.create_job_dir(shared, exec_transport.new_job_id())
    locked = orphan / "out" / "x"
    locked.mkdir(parents=True)
    (locked / "result.parquet").write_bytes(b"x")
    os.chmod(locked, 0o500)
    two_hours_ago = time.time() - 2 * 3600
    os.utime(orphan, (two_hours_ago, two_hours_ago))

    with _client() as client:
        assert client.get("/healthz").status_code == 200

    assert not orphan.exists(), "an orphan with a locked out/ subdirectory survived the sweep"


# ---------------------------------------------------------------------------
# the log join key between the two containers
# ---------------------------------------------------------------------------
def test_both_job_log_lines_carry_the_code_hash(executor_env, monkeypatch):
    """The web side logs the same hash, so an operator can join the two logs
    for one answer."""
    import hashlib

    import executor.app as executor_app

    logged: list = []
    monkeypatch.setattr(executor_app, "log_with_sid",
                        lambda sid, level, message, *a, **k: logged.append(message))
    code = "RESULT = 1 + 1"
    expected = hashlib.sha256(code.encode("utf-8", errors="ignore")).hexdigest()[:10]

    with _client() as client:
        _submit(client, executor_env, code)

    starts = [m for m in logged if m.startswith("EXEC_JOB_START")]
    ends = [m for m in logged if m.startswith("EXEC_JOB_END")]
    assert len(starts) == 1, logged
    assert len(ends) == 1, logged
    assert f"code_hash={expected}" in starts[0], starts[0]
    assert f"code_hash={expected}" in ends[0], ends[0]


# ---------------------------------------------------------------------------
# the sandbox image never ships the HTTP dispatcher
# ---------------------------------------------------------------------------
def test_generated_code_cannot_import_the_dispatcher(executor_env):
    """`executor_client` is main-app only: were it in the image's copy set,
    the runner's public-name call would dispatch the sandbox to itself."""
    code = ("try:\n"
            "    import executor_client\n"
            "    RESULT = 'importable'\n"
            "except ImportError as e:\n"
            "    RESULT = type(e).__name__")
    with _client() as client:
        job_dir, response, _ = _submit(client, executor_env, code)
    decoded = _decode(response, job_dir)
    result = decoded.get("result")
    assert result != "importable", "the sandbox image ships the HTTP dispatcher"
    assert result in ("ModuleNotFoundError", "ImportError"), decoded


# ---------------------------------------------------------------------------
# the stray pass of the jobs root (this side)
#
# `create_job_dir` gives the root group write so this service can clear an
# abandoned job directory; the same permission is what lets generated code —
# which runs as THIS uid — create entries directly in the root. Nothing
# removed them while both sweeps only looked at 32-hex names. These pin the
# widened behaviour and the ONE rule that is deliberately INVERTED between
# the two sides.
# ---------------------------------------------------------------------------
STRAY_REMOVED = "EXEC_STRAY_ENTRY_REMOVED"
TWO_HOURS_S = 2 * 3600


def _age_entry(path, seconds: float = TWO_HOURS_S) -> None:
    """Back-date an entry past the sweep's threshold; the LINK, not its
    target, for a symlink."""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def _sweep(shared_dir) -> None:
    """Run the sweep the way the lifespan does."""
    from executor.app import _sweep_orphans

    _sweep_orphans(Path(shared_dir))


def test_an_aged_stray_file_is_removed(executor_env):
    """The commonest shape: generated code writing `open("/jobs/x", "w")`."""
    stray = executor_env / "stash.txt"
    stray.write_text("exfiltrated", encoding="utf-8")
    _age_entry(stray)

    _sweep(executor_env)

    assert not stray.exists(), "an aged stray file survived"


def test_an_aged_stray_directory_is_removed_with_its_contents(executor_env):
    stray = executor_env / "stash_dir"
    stray.mkdir()
    (stray / "inner.txt").write_text("x", encoding="utf-8")
    _age_entry(stray)

    _sweep(executor_env)

    assert not stray.exists(), "an aged stray directory survived"


def test_a_job_id_shaped_stray_file_is_removed(executor_env):
    """The case the widening would otherwise have missed: `create_job_dir`
    only ever makes DIRECTORIES, so a 32-hex name that is a file was never a
    job — it is a stash wearing a job id."""
    disguised = executor_env / ("b" * 32)
    assert exec_transport.valid_job_id(disguised.name), disguised.name
    disguised.write_text("hiding", encoding="utf-8")
    _age_entry(disguised)

    _sweep(executor_env)

    assert not disguised.exists(), "a stash wearing a job-id name survived"


def test_a_fresh_stray_is_kept(executor_env):
    """The age threshold bounds everything: a job being prepared right now
    must not have its neighbours deleted underneath it."""
    stray = executor_env / "just_written.txt"
    stray.write_text("x", encoding="utf-8")

    _sweep(executor_env)

    assert stray.is_file(), "a fresh stray was swept"


def test_an_aged_stray_symlink_is_unlinked_and_never_followed(executor_env, tmp_path):
    """Generated code chooses where a symlink points, and the SAME volume is
    read by the container where the customer's data IS mounted — so the link
    is unlinked and its target is left intact."""
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("customer data", encoding="utf-8")
    link = executor_env / "shortcut"
    link.symlink_to(target, target_is_directory=True)
    _age_entry(link)

    _sweep(executor_env)

    assert not link.is_symlink(), "an aged stray symlink survived"
    assert target.is_dir(), "the sweep followed the link and removed its target"
    assert (target / "keep.txt").is_file(), "the link's target lost content"


@pytest.mark.skipif(getattr(os, "geteuid", lambda: -1)() != 0,
                    reason="needs root to chown an entry to the web uid")
def test_the_sandbox_sweep_ignores_ownership(executor_env):
    """INVERTED on purpose: this side no longer classifies strays by owner.

    The earlier version of this test asserted that an entry owned by the WEB
    uid survived, on the reasoning that this sweep should only touch what
    generated code could have created — i.e. what it owns. That reasoning is
    wrong, and `test_a_laundered_job_directory_is_still_swept` below is the
    reason: on a group-writable root, generated code does not have to CREATE
    a web-owned entry, it can ACQUIRE one with a single `os.rename`, and
    ownership survives it. So ownership classified nothing useful and the two
    sweeps disagreed by construction, each skipping the laundered directory
    because of the other's rule.

    The asymmetry is deliberate and is NOT a drift: the main app keeps its
    ownership rule, because there it is what protects a misconfigured
    `EXECUTOR_SHARED_DIR` pointed at customer state, where every file belongs
    to the web uid. Nothing of the customer's is mounted in this container —
    the jobs volume is its only mount — so that direction buys nothing here
    while leaving the hole open.
    """
    mine = executor_env / "written_by_me.txt"
    mine.write_text("x", encoding="utf-8")
    _age_entry(mine)
    theirs = executor_env / "written_by_the_web.txt"
    theirs.write_text("x", encoding="utf-8")
    os.chown(theirs, WEB_UID, SHARED_GID)
    _age_entry(theirs)

    _sweep(executor_env)

    assert not mine.exists(), "an aged stray this uid owns survived"
    assert not theirs.exists(), "an aged stray owned by the web uid survived"


@pytest.mark.skipif(getattr(os, "geteuid", lambda: -1)() != 0,
                    reason="needs root to chown the job directory to the web uid")
def test_a_laundered_job_directory_is_still_swept(executor_env, caplog):
    """THE REGRESSION TEST FOR THE DEFEAT OF THE FIRST FIX.

    The attack, in full. The jobs root must be group-writable so this service
    can clear an abandoned job directory in it. That same permission lets
    generated code RENAME one — and a rename preserves `st_uid`:

        root = '/jobs'
        mine = [n for n in os.listdir(root) if len(n) == 32]
        os.rename(os.path.join(root, mine[0]), os.path.join(root, 'zz_launder'))
        open('/jobs/zz_launder/stash.csv', 'w').write('cross-job stash')

    What is left is a directory with an ordinary name, owner uid 10001 — the
    WEB identity — mode 2770, still holding that question's `in/0.parquet`,
    i.e. the customer's frames. The first version of this sweep classified
    strays by ownership, and both sides then skipped it FOREVER: the web side
    because the owner WAS its own, this side because it was NOT. On a named
    disk-backed volume it outlived restarts and image upgrades and was
    readable by every later job, for any user.

    OWNERSHIP IS NOT AUTHORSHIP on a group-writable directory. This test
    exists because the first fix was defeated by one call, so the property is
    pinned rather than reasoned about: the entry goes, whoever owns it.

    Correct only on THIS side — see `test_the_sandbox_sweep_ignores_ownership`
    for why the main app must keep the opposite rule.
    """
    import logging

    laundered_name = "zz_launder"
    job_dir = exec_transport.create_job_dir(executor_env, exec_transport.new_job_id())
    (job_dir / "in" / "0.parquet").write_bytes(b"customer frames")
    for path in (job_dir / "in" / "0.parquet", job_dir / "in", job_dir):
        os.chown(path, WEB_UID, SHARED_GID)
    owner_uid = os.lstat(job_dir).st_uid
    assert owner_uid == WEB_UID, owner_uid
    assert owner_uid != os.geteuid(), (owner_uid, os.geteuid())

    # The attack: one rename, and the name stops being job-id shaped while
    # the owner stays the web identity.
    laundered = executor_env / laundered_name
    os.rename(job_dir, laundered)
    (laundered / "stash.csv").write_text("cross-job stash", encoding="utf-8")
    assert not exec_transport.valid_job_id(laundered.name), laundered.name
    assert os.lstat(laundered).st_uid == WEB_UID, os.lstat(laundered).st_uid
    _age_entry(laundered)

    # A SECOND laundered entry, left fresh: the test must not be able to pass
    # because the sweep deletes everything it sees.
    fresh_job = exec_transport.create_job_dir(executor_env, exec_transport.new_job_id())
    (fresh_job / "in" / "0.parquet").write_bytes(b"x")
    for path in (fresh_job / "in" / "0.parquet", fresh_job / "in", fresh_job):
        os.chown(path, WEB_UID, SHARED_GID)
    fresh_laundered = executor_env / "zz_launder_fresh"
    os.rename(fresh_job, fresh_laundered)

    with caplog.at_level(logging.INFO):
        _sweep(executor_env)

    assert not laundered.exists(), (
        "a laundered job directory survived the sweep — the stash is readable "
        "by every later job on the volume")
    assert fresh_laundered.is_dir(), "a FRESH laundered entry was swept"

    hits = [record.getMessage() for record in caplog.records
            if STRAY_REMOVED in record.getMessage()]
    assert len(hits) == 1, [record.getMessage() for record in caplog.records]
    message = hits[0]
    assert "kind=dir" in message, message
    assert f"name={laundered_name}" in message, message


def test_a_stray_name_carrying_a_newline_is_logged_on_one_line(executor_env, caplog):
    """The name of anything that is not a job directory was chosen by
    generated code, so it is untrusted text on a newline-delimited line."""
    import logging

    forged = "stash\n2026-09-17 00:00:00,000 | INFO | [sid=x] EXEC_OK job_id=forged"
    # Padding chosen to exceed the 200-char log cap while staying under the
    # filesystem's 255-BYTE name limit, which a longer name hits first.
    stray = executor_env / (forged + "A" * 150)
    stray.write_text("x", encoding="utf-8")
    _age_entry(stray)

    with caplog.at_level(logging.INFO):
        _sweep(executor_env)

    hits = [record.getMessage() for record in caplog.records
            if STRAY_REMOVED in record.getMessage()]
    assert len(hits) == 1, [record.getMessage() for record in caplog.records]
    message = hits[0]
    assert "\n" not in message, repr(message[:300])
    assert "\r" not in message, repr(message[:300])
    assert "\\n" in message, message[:300]
    assert len(message) < 400, len(message)
    impostors = [record.getMessage() for record in caplog.records
                 if record.getMessage().startswith("EXEC_OK")]
    assert impostors == [], impostors


# ---------------------------------------------------------------------------
# forward-dated entries, and removal as the sandbox's own uid
# ---------------------------------------------------------------------------
ONE_YEAR_S = 365 * 24 * 3600


def test_a_forward_dated_stray_and_job_dir_are_treated_as_aged(executor_env):
    """`now - mtime` is NEGATIVE for an entry dated in the future, and a
    negative age used to read as "fresh" forever. Generated code sets any
    mtime it likes on what it creates (`os.utime` needs only ownership), so a
    forward-dated stash would have been immune to the sweep. A negative age is
    anything but fresh."""
    stray = executor_env / "future_stash"
    stray.mkdir()
    (stray / "inner.csv").write_text("x", encoding="utf-8")
    job = exec_transport.create_job_dir(executor_env, exec_transport.new_job_id())
    for path in (stray, job):
        _age_entry(path, -ONE_YEAR_S)
    assert os.lstat(stray).st_mtime > time.time() + ONE_YEAR_S / 2

    _sweep(executor_env)

    assert not stray.exists(), "a stray dated one year ahead survived the sweep"
    assert not job.exists(), "a job directory dated one year ahead survived the sweep"


def test_an_entry_slightly_ahead_of_the_clock_is_still_fresh(executor_env):
    """Clock skew between the two containers and the filesystem must not make
    a just-created job directory look forward-dated: only an mtime beyond the
    tolerance is treated as aged."""
    stray = executor_env / "clock_skew"
    stray.mkdir()
    job = exec_transport.create_job_dir(executor_env, exec_transport.new_job_id())
    for path in (stray, job):
        _age_entry(path, -60)

    _sweep(executor_env)

    assert stray.exists() and job.exists()


_SWEEP_AS_EXEC_UID = """
import json, os, sys, time
sys.path.insert(0, {root!r})
from pathlib import Path
import executor.app as app

lines = []
app.log_with_sid = lambda sid, level, message, *a, **k: lines.append(
    [str(sid), str(level), str(message)])
shared = Path({shared!r})

# What generated code leaves when it locks its own output: created AS THIS
# uid, then `chmod 0500` on the directory that holds the file.
stray = shared / {own_name!r}
(stray / "out").mkdir(parents=True)
(stray / "out" / "stash.csv").write_text("x")
os.chmod(stray / "out", 0o500)
stamp = time.time() - 7200
os.utime(stray, (stamp, stamp))

app._sweep_orphans(shared)
print(json.dumps({{"lines": lines, "own_left": stray.exists()}}))
"""


@pytest.mark.skipif(os.geteuid() != 0, reason="setuid to 10002 needs root (the Dockerfile test stage)")
def test_two_uid_the_sandbox_uid_clears_its_own_locked_stash_and_logs_only_what_went(shared_dir):
    """As root the mode bits are advisory, so the one-uid tests above prove
    nothing about the runtime identity. Here the sweep runs AS uid 10002:

    * a stray it OWNS, with a `chmod 0500` directory inside, must be gone — the
      directory modes are repaired top-down BEFORE the removal, because the
      `rmtree` error hook only sees the file whose unlink failed, and chmodding
      the FILE never makes its parent writable;
    * a stray and a job directory holding a subtree owned by ANOTHER uid,
      which this uid cannot chmod, survive — and the log must not claim
      otherwise: `EXEC_STRAY_ENTRY_REMOVED` / `EXEC_ORPHAN_REMOVED` are
      written only after an `lstat` confirms the entry is gone.
    """
    shared = shared_dir
    os.chown(shared, 0, SHARED_GID)
    os.chmod(shared, 0o2770)
    stamp = time.time() - 2 * 3600

    def plant_foreign_subtree(top: Path) -> None:
        foreign = top / "out" / "foreign"
        foreign.mkdir(parents=True)
        (foreign / "held.csv").write_text("x", encoding="utf-8")
        os.chmod(foreign, 0o500)                      # root-owned, not ours to chmod
        for path in (top, top / "out"):
            os.chown(path, EXEC_UID, SHARED_GID)
            os.chmod(path, 0o2770)
        os.utime(top, (stamp, stamp))

    stuck_stray = shared / "zz_foreign_stash"
    plant_foreign_subtree(stuck_stray)
    stuck_job = shared / exec_transport.new_job_id()
    plant_foreign_subtree(stuck_job)

    own_name = "zz_locked_stash"
    script = _SWEEP_AS_EXEC_UID.format(root=str(ROOT), shared=str(shared), own_name=own_name)
    proc = subprocess.run([sys.executable, "-c", script], user=EXEC_UID, group=SHARED_GID,
                          extra_groups=[], env=_uid_env(EXEC_UID, SHARED_GID), cwd=str(ROOT),
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, (proc.returncode, proc.stdout[-2000:], proc.stderr[-4000:])
    report = exec_transport.loads(proc.stdout.strip().splitlines()[-1])
    lines = report["lines"]

    assert report["own_left"] is False, (
        "the sandbox uid could not clear a stash it owns after generated code "
        f"locked it: {lines}")
    assert not (shared / own_name).exists()

    assert stuck_stray.is_dir(), "the foreign subtree was expected to block removal"
    assert stuck_job.is_dir(), "the foreign subtree was expected to block removal"
    false_stray = [line for line in lines
                   if "EXEC_STRAY_ENTRY_REMOVED" in line[2] and stuck_stray.name in line[2]]
    assert false_stray == [], ("a stray that is still on disk was logged as removed", lines)
    false_orphan = [line for line in lines
                    if "EXEC_ORPHAN_REMOVED" in line[2] and line[0] == stuck_job.name]
    assert false_orphan == [], ("a job dir that is still on disk was logged as removed", lines)
