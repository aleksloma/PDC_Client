"""Sandbox import guard: DB drivers and this client's credential modules must
not be importable inside generated code, while every import existing
generated code legitimately performs keeps working (a denylist miss is a gap,
an allowlist miss would fail-freeze historical stored code)."""
import pytest

import code_exec
import plot_utils
import sandbox_guard


@pytest.mark.parametrize("mod", [
    "sqlalchemy", "psycopg2", "pymysql", "pyodbc", "oracledb", "sqlite3",
    "db_connector", "db_sources", "db_scheduler", "local_store", "settings",
    "brain_client", "password_utils", "relation_discovery", "sqlglot",
    "roles_store", "clickhouse_driver", "clickhouse_sqlalchemy",
    "clickhouse_connect",
])
def test_denied_import_raises_in_safe_execute(mod):
    out = code_exec.safe_execute(f"import {mod}\nRESULT = 1", {}, sid="t")
    assert "error" in out
    assert "not permitted in the analysis sandbox" in out["error"]


def test_denied_dotted_and_dunder_import():
    out = code_exec.safe_execute("import sqlalchemy.engine\nRESULT = 1", {}, sid="t")
    assert "not permitted" in out.get("error", "")
    out2 = code_exec.safe_execute("__import__('psycopg2')\nRESULT = 1", {}, sid="t")
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
    out = code_exec.safe_execute(code, {"t": pd.DataFrame({"a": [1, 2]})}, sid="t")
    assert out.get("error") is None or "error" not in out
    assert out.get("result") == 6


def test_existing_generated_code_shape_still_executes():
    import pandas as pd
    code = ("df = dfs['sales.csv']\n"
            "RESULT = df.groupby('dept')['salary'].mean().to_dict()")
    dfs = {"sales.csv": pd.DataFrame({"dept": ["a", "a", "b"],
                                      "salary": [10, 20, 40]})}
    out = code_exec.safe_execute(code, dfs, sid="t")
    assert out.get("result") == {"a": 15.0, "b": 40.0}


def test_matplotlib_render_still_works_with_guard():
    """Chart rendering triggers lazy transitive imports inside the sandbox —
    the guard must not break them (plot_utils exec site carries it too)."""
    import pandas as pd
    code = ("import matplotlib.pyplot as plt\n"
            "df = dfs['t']\n"
            "plt.figure()\nplt.bar(df['x'], df['y'])\n")
    out = plot_utils.render_plot_safe(code, {"t": pd.DataFrame({"x": ["a", "b"], "y": [1, 2]})}, "t")
    assert out.get("error") is None
    assert out.get("image") or out.get("image_base64")


def test_plot_exec_site_denies_db_drivers():
    out = plot_utils.render_plot_safe("import sqlalchemy\n", {}, "t")
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
    out1 = code_exec.safe_execute(evil, {}, sid="t")
    assert out1.get("result") == "mutated"
    # The guard is intact for the next execution...
    out2 = code_exec.safe_execute("import psycopg2\nRESULT = 1", {}, sid="t")
    assert "not permitted" in out2.get("error", "")
    # ...and so are ordinary builtins.
    out3 = code_exec.safe_execute("RESULT = len([1, 2, 3])", {}, sid="t")
    assert out3.get("result") == 3
    # The module-level template itself was never touched.
    assert sandbox_guard.SANDBOX_BUILTINS["__import__"] is sandbox_guard._guarded_import
    assert "len" in sandbox_guard.SANDBOX_BUILTINS
