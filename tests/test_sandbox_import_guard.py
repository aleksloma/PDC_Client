"""Sandbox import guard: DB drivers and this client's credential modules must
not be importable inside generated code, while every import existing
generated code legitimately performs keeps working (a denylist miss is a gap,
an allowlist miss would fail-freeze historical stored code).

The guard cases call `code_exec._execute_in_process` /
`plot_utils._render_in_process` directly: those are the functions the sandbox
runner imports, i.e. the ones that actually install the guarded builtins. The
two public names now hand the job to the sandbox service over HTTP, and the
last two cases pin exactly that — with no service reachable, the main app
answers with an error instead of executing anything locally.
"""
import pytest

import code_exec
import plot_utils
import sandbox_guard
from settings import settings

UNAVAILABLE_TEXT = "ExecutorUnavailable: the analysis service is not reachable"
# A closed LOOPBACK port on purpose: an unresolvable hostname costs seconds of
# DNS timeout, a refused connection comes back at once.
DEAD_EXECUTOR_URL = "http://127.0.0.1:9"
EXECUTOR_SETTING_NAMES = ("EXECUTOR_URL", "EXECUTOR_SHARED_DIR",
                          "EXECUTOR_CONNECT_TIMEOUT", "EXECUTOR_PLOT_TIMEOUT_S",
                          "EXECUTOR_MAX_CONCURRENT", "EXECUTOR_QUEUE_MAX_S")


@pytest.mark.parametrize("mod", [
    "sqlalchemy", "psycopg2", "pymysql", "pyodbc", "oracledb", "sqlite3",
    "db_connector", "db_sources", "db_scheduler", "local_store", "settings",
    "brain_client", "password_utils", "relation_discovery", "sqlglot",
    "roles_store", "clickhouse_driver", "clickhouse_sqlalchemy",
    "clickhouse_connect", "sso_store", "gcs_upload", "google",
])
def test_denied_import_raises_in_safe_execute(mod):
    out = code_exec._execute_in_process(f"import {mod}\nRESULT = 1", {}, sid="t")
    assert "error" in out
    assert "not permitted in the analysis sandbox" in out["error"]


def test_denied_dotted_and_dunder_import():
    out = code_exec._execute_in_process("import sqlalchemy.engine\nRESULT = 1", {}, sid="t")
    assert "not permitted" in out.get("error", "")
    out2 = code_exec._execute_in_process("__import__('psycopg2')\nRESULT = 1", {}, sid="t")
    assert "not permitted" in out2.get("error", "")


def test_allowed_imports_still_work():
    """The empirical proof an allowlist would break production: the imports
    generated code routinely performs must all succeed."""
    import pandas as pd
    code = (
        "import re\nimport datetime\nimport json\nimport math\nimport itertools\n"
        "import pandas\nimport numpy\n"
        "from sklearn.linear_model import LogisticRegression\n"
        "RESULT = dfs['t']['a'].sum() + len(re.findall('a', 'aaa'))"
    )
    out = code_exec._execute_in_process(code, {"t": pd.DataFrame({"a": [1, 2]})}, sid="t")
    assert out.get("error") is None or "error" not in out
    assert out.get("result") == 6


def test_existing_generated_code_shape_still_executes():
    import pandas as pd
    code = ("df = dfs['sales.csv']\n"
            "RESULT = df.groupby('dept')['salary'].mean().to_dict()")
    dfs = {"sales.csv": pd.DataFrame({"dept": ["a", "a", "b"],
                                      "salary": [10, 20, 40]})}
    out = code_exec._execute_in_process(code, dfs, sid="t")
    assert out.get("result") == {"a": 15.0, "b": 40.0}


def test_matplotlib_render_still_works_with_guard():
    """Chart rendering triggers lazy transitive imports inside the sandbox —
    the guard must not break them (plot_utils exec site carries it too)."""
    import pandas as pd
    code = ("import matplotlib.pyplot as plt\n"
            "df = dfs['t']\n"
            "plt.figure()\nplt.bar(df['x'], df['y'])\n")
    out = plot_utils._render_in_process(code, {"t": pd.DataFrame({"x": ["a", "b"], "y": [1, 2]})}, "t")
    assert out.get("error") is None
    assert out.get("image") or out.get("image_base64")


def test_plot_exec_site_denies_db_drivers():
    out = plot_utils._render_in_process("import sqlalchemy\n", {}, "t")
    assert "not permitted" in (out.get("error") or "")


def test_guard_is_shared_single_source():
    assert "sqlalchemy" in sandbox_guard.DENIED_MODULES
    assert sandbox_guard.SANDBOX_BUILTINS["__import__"] is sandbox_guard._guarded_import
    assert "print" in sandbox_guard.SANDBOX_BUILTINS


def test_one_execution_cannot_poison_the_guard_for_the_next():
    """Each exec gets a per-call COPY of SANDBOX_BUILTINS — code that mutates
    its own __builtins__ (restoring the real __import__, deleting names) must
    not weaken the guard for any later execution."""
    evil = ("import builtins as _b\n"
            "__builtins__['__import__'] = getattr(_b, '__import__')\n"
            "del __builtins__['len']\n"
            "RESULT = 'mutated'")
    out1 = code_exec._execute_in_process(evil, {}, sid="t")
    assert out1.get("result") == "mutated"
    # The guard is intact for the next execution...
    out2 = code_exec._execute_in_process("import psycopg2\nRESULT = 1", {}, sid="t")
    assert "not permitted" in out2.get("error", "")
    # ...and so are ordinary builtins.
    out3 = code_exec._execute_in_process("RESULT = len([1, 2, 3])", {}, sid="t")
    assert out3.get("result") == 3
    # The module-level template itself was never touched.
    assert sandbox_guard.SANDBOX_BUILTINS["__import__"] is sandbox_guard._guarded_import
    assert "len" in sandbox_guard.SANDBOX_BUILTINS


# ---------------------------------------------------------------------------
# The main-app path never executes generated code in this process
# ---------------------------------------------------------------------------
def _point_at_a_dead_sandbox(tmp_path, monkeypatch):
    missing = [n for n in EXECUTOR_SETTING_NAMES if not hasattr(settings, n)]
    assert not missing, f"settings lacks the analysis-sandbox fields: {missing}"
    monkeypatch.setattr(settings, "EXECUTOR_URL", DEAD_EXECUTOR_URL)
    monkeypatch.setattr(settings, "EXECUTOR_SHARED_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "EXECUTOR_CONNECT_TIMEOUT", 2.0)
    monkeypatch.setattr(settings, "EXECUTOR_PLOT_TIMEOUT_S", 5)
    monkeypatch.setattr(settings, "EXECUTOR_MAX_CONCURRENT", 1)
    monkeypatch.setattr(settings, "EXECUTOR_QUEUE_MAX_S", 5)
    try:
        import executor_client
    except ModuleNotFoundError as exc:
        pytest.fail(f"executor_client is not importable: {exc!r}")
    monkeypatch.setattr(executor_client, "_TRANSPORT", None, raising=False)
    reset = getattr(executor_client, "_reset_gate", None)
    assert callable(reset), "executor_client must expose _reset_gate()"
    reset()
    return executor_client


def _leftovers(tmp_path) -> list:
    import exec_transport

    return sorted(p.name for p in tmp_path.iterdir()
                  if p.is_dir() and exec_transport.valid_job_id(p.name))


@pytest.mark.real_executor_dispatch
def test_the_analysis_path_never_runs_code_locally_without_a_sandbox(tmp_path, monkeypatch):
    _point_at_a_dead_sandbox(tmp_path, monkeypatch)

    out = code_exec.safe_execute("import os\nRESULT = os.getpid()", {}, sid="t")

    assert out == {"error": UNAVAILABLE_TEXT}, out
    assert "result" not in out, sorted(out)
    left = _leftovers(tmp_path)
    assert left == [], left


@pytest.mark.real_executor_dispatch
def test_the_chart_path_never_runs_code_locally_without_a_sandbox(tmp_path, monkeypatch):
    _point_at_a_dead_sandbox(tmp_path, monkeypatch)

    out = plot_utils.render_plot_safe("import os\nopen('leak.txt', 'w').write('x')", {}, "t")

    assert out == {"ok": False, "error": UNAVAILABLE_TEXT, "trace": ""}, out
    assert "image" not in out and "plotly_html" not in out, sorted(out)
    left = _leftovers(tmp_path)
    assert left == [], left
