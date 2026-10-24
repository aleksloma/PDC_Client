"""`pdc_diag` cannot be redefined by generated code (BRB retest follow-up 1).

The English Q3 retest answer had a retry block that "replicated" the library
(`def detect_anomalies_mock(...)`, `FINDINGS = ""`) and silently replaced the
anomaly FINDINGS with its own. `sandbox_guard.pdc_diag_redefinition(code)`
is an AST check run at BOTH exec sites (`code_exec._execute_in_process`,
`plot_utils._render_in_process` — the functions the sandbox runner imports)
before anything executes: a block that rebinds, deletes or mutates
`pdc_diag`, or defines a function under one of its PUBLIC names, is refused
with the fixed text `PDC_DIAG_REDEFINED_TEXT`, which carries no code.

Documented limits (defense in depth, not a boundary): `setattr(pdc_diag, …)`,
`globals()["pdc_diag"] = …` and `pdc_diag.__dict__[…] = …` are not caught;
a name that merely STARTS with `pdc_diag` (`pdc_diag_out`) is accepted.

Offline, invented numbers. Every case that would mutate the real module
before the guard exists (`pdc_diag.fmt = …`, `del pdc_diag.fmt`) is undone by
the autouse fixture below, so a failing run cannot poison later tests.
"""
import logging

import pandas as pd
import pytest

import code_exec
import pdc_diag
import plot_utils
import sandbox_guard

TEXT = "pdc_diag cannot be redefined"


@pytest.fixture(autouse=True)
def _restore_pdc_diag_module():
    """Before the guard exists the rejected cases run for real against the
    shared module object — put every attribute back afterwards."""
    saved = dict(vars(pdc_diag))
    yield
    for name in list(vars(pdc_diag)):
        if name not in saved:
            delattr(pdc_diag, name)
    for name, value in saved.items():
        if vars(pdc_diag).get(name, object()) is not value:
            setattr(pdc_diag, name, value)


def _guard(code):
    fn = getattr(sandbox_guard, "pdc_diag_redefinition", None)
    assert callable(fn), "sandbox_guard.pdc_diag_redefinition is missing"
    return fn(code)


def _python(code, dfs=None):
    return code_exec._execute_in_process(code, dfs or {}, sid="t")


def _plot(code, dfs=None):
    return plot_utils._render_in_process(code, dfs or {}, "t")


# ---------------------------------------------------------------- the forms

REJECTED = {
    "assign": "pdc_diag = 1",
    "augassign": "pdc_diag += 1",
    "tuple_unpack": "a, pdc_diag = 1, 2",
    "list_unpack": "[pdc_diag, b] = [1, 2]",
    "annassign": "x: int = 0\npdc_diag: int = 1",
    "walrus": "(pdc_diag := 1)",
    "del_name": "del pdc_diag",
    "class": "class pdc_diag:\n    pass",
    "def_name": "def pdc_diag():\n    pass",
    "def_public_detect": "def detect_anomalies(df):\n    return {}",
    "def_public_fmt": "def fmt(v, k=None): return ''",
    "async_def_public": "async def coverage_check(): pass",
    "import_as": "import numpy as pdc_diag",
    "from_import_as": "from math import sqrt as pdc_diag",
    "attr_assign": "pdc_diag.fmt = lambda *a: ''",
    "attr_augassign": "pdc_diag.x += 1",
    "attr_del": "del pdc_diag.fmt",
    "for_target": "for pdc_diag in range(2):\n    pass",
    "except_as": "try:\n    pass\nexcept Exception as pdc_diag:\n    pass",
    "comprehension": "[1 for pdc_diag in range(3)]",
    "nested_in_function": ("def helper():\n    pdc_diag = 3\n    return pdc_diag\n"
                           "RESULT = helper()"),
    "global": "global pdc_diag",
    "brb_mock_plus_public": ("def detect_anomalies_mock(df):\n    return {}\n"
                             "def detect_anomalies(df, *a, **k):\n"
                             "    return detect_anomalies_mock(df)\n"
                             "FINDINGS = ''"),
}

# Guard-level only (exec would fail for an unrelated reason, or the form is
# only pinned at the guard).
GUARD_ONLY_REJECTED = {
    "with_as": "with open('/dev/null') as pdc_diag:\n    pass",
    "from_import_name": "from os import pdc_diag",
    "nonlocal": ("def outer():\n    x = 1\n    def inner():\n        nonlocal pdc_diag\n"
                 "    return inner"),
}

ACCEPTED = {
    "fmt_call": "RESULT = pdc_diag.fmt(1234, 'count')",
    # Binding the real module again is harmless: a planner may write it.
    "import_plain": "import pdc_diag\nRESULT = pdc_diag.fmt(7, 'count')",
    "prefixed_name": "pdc_diag_out = 1\nRESULT = pdc_diag_out",
    "suffixed_name": "my_pdc_diag = 2\nRESULT = my_pdc_diag",
    "alias_read": "x = pdc_diag\nRESULT = x.fmt(5, 'count')",
    "helper_reads": "def helper(): return pdc_diag.fmt(1)\nRESULT = helper()",
    "brb_mock_alone": ("def detect_anomalies_mock(df):\n    return {}\n"
                       "RESULT = len(detect_anomalies_mock(None))"),
}


# -------------------------------------------------------------- the guard

def test_constant_text():
    assert getattr(sandbox_guard, "PDC_DIAG_REDEFINED_TEXT", None) == TEXT


@pytest.mark.parametrize("code", list({**REJECTED, **GUARD_ONLY_REJECTED}.values()),
                         ids=list({**REJECTED, **GUARD_ONLY_REJECTED}))
def test_guard_rejects(code):
    assert _guard(code) == TEXT


@pytest.mark.parametrize("code", list(ACCEPTED.values()), ids=list(ACCEPTED))
def test_guard_accepts(code):
    assert _guard(code) is None


def test_guard_accepts_real_coverage_check_call():
    code = ("out = pdc_diag.coverage_check(dfs['t'], 'period', ['region'], ['amount'])\n"
            "RESULT = out['findings']['coverage']")
    assert _guard(code) is None


@pytest.mark.parametrize("code", ["def broken(:\n    pass", "pdc_diag = = 1", "x = (", ""])
def test_guard_returns_none_on_parse_failure_or_empty(code):
    assert _guard(code) is None


def test_message_carries_no_code():
    code = "SECRET_FRAGMENT_77 = 1\npdc_diag = SECRET_FRAGMENT_77"
    text = _guard(code)
    assert text == TEXT
    assert "SECRET_FRAGMENT_77" not in text
    assert "=" not in text and "\n" not in text


# ------------------------------------------------------- the python site

@pytest.mark.parametrize("code", list(REJECTED.values()), ids=list(REJECTED))
def test_python_site_rejects(code):
    out = _python(code)
    assert out.get("error") == TEXT, out
    assert "result" not in out, out


@pytest.mark.parametrize("code", list(ACCEPTED.values()), ids=list(ACCEPTED))
def test_python_site_accepts(code):
    out = _python(code)
    assert not out.get("error"), out


def test_python_site_accepted_values():
    assert _python("RESULT = pdc_diag.fmt(1234, 'count')").get("result") == "1,234"
    assert _python("x = pdc_diag\nRESULT = x.fmt(5, 'count')").get("result") == "5"
    assert _python("pdc_diag_out = 1\nRESULT = pdc_diag_out").get("result") == 1


def test_python_site_real_coverage_check_call():
    frame = pd.DataFrame({
        "period": ["2025-01"] * 3 + ["2025-02"] * 3 + ["2025-03"] * 3,
        "region": ["A", "B", "C"] * 3,
        "amount": [10.0, 12.0, 11.0, 9.0, 13.0, 12.0, 10.0, 11.0, 12.0],
    })
    code = ("out = pdc_diag.coverage_check(dfs['t'], 'period', ['region'], ['amount'])\n"
            "RESULT = out['findings']['coverage']")
    out = _python(code, {"t": frame})
    assert not out.get("error"), out
    assert out.get("result") in pdc_diag.FINDINGS_ENUM


def test_python_site_syntax_error_keeps_python_text():
    out = _python("def broken(:\n    pass")
    assert (out.get("error") or "").startswith("SyntaxError:"), out


def test_python_site_message_carries_no_code():
    out = _python("SECRET_FRAGMENT_77 = 1\npdc_diag = SECRET_FRAGMENT_77")
    assert out.get("error") == TEXT
    assert "SECRET_FRAGMENT_77" not in str(out)


def test_python_site_rejects_before_executing(capsys):
    out = _python("import sys; sys.stdout.write('RAN')\npdc_diag = 1")
    assert out.get("error") == TEXT, out
    assert "RAN" not in capsys.readouterr().out


def test_python_site_logs_reason_without_code(caplog):
    caplog.set_level(logging.DEBUG, logger="datachat")
    _python("SECRET_FRAGMENT_88 = 1\npdc_diag = SECRET_FRAGMENT_88")
    msgs = [r.getMessage() for r in caplog.records]
    assert any("pdc_diag_redefined" in m for m in msgs), msgs
    assert not any("SECRET_FRAGMENT_88" in m for m in msgs), msgs


def test_python_site_mutation_never_reaches_the_module():
    original = pdc_diag.fmt
    _python("pdc_diag.fmt = lambda *a: ''")
    assert pdc_diag.fmt is original
    _python("del pdc_diag.fmt")
    assert getattr(pdc_diag, "fmt", None) is original


# --------------------------------------------------------- the plot site

@pytest.mark.parametrize("code", list(REJECTED.values()), ids=list(REJECTED))
def test_plot_site_rejects(code):
    out = _plot(code)
    assert out.get("ok") is False, out
    assert out.get("error") == TEXT, out


@pytest.mark.parametrize("code", list(ACCEPTED.values()), ids=list(ACCEPTED))
def test_plot_site_does_not_reject_accepted(code):
    # No figure is drawn, so the render fails for its own reason — never the
    # guard's.
    out = _plot(code)
    assert out.get("error") != TEXT, out


def test_plot_site_rejects_before_executing(capsys):
    out = _plot("import sys; sys.stdout.write('RAN')\npdc_diag = 1")
    assert out.get("ok") is False and out.get("error") == TEXT, out
    assert "RAN" not in capsys.readouterr().out


def test_plot_site_message_carries_no_code():
    out = _plot("SECRET_FRAGMENT_77 = 1\npdc_diag = SECRET_FRAGMENT_77")
    assert out.get("error") == TEXT
    assert "SECRET_FRAGMENT_77" not in str(out)


def test_plot_site_mutation_never_reaches_the_module():
    original = pdc_diag.fmt
    _plot("pdc_diag.fmt = lambda *a: ''")
    assert pdc_diag.fmt is original
