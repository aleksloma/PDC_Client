"""VALUES / FINDINGS capture at both exec sites and their transport
(docs/DIAGNOSTICS_SPEC.md §A3, decision D4; §B7.7 for the NearZeroChartError
sentence; §B8 registration of `pdc_diag`).

The sandbox side (`exec_transport.encode_values` / `encode_findings` /
`capture_outputs`, the two in-process exec functions) and the main-app side
(`decode_values` / `decode_findings` inside `_reconstruct`) share one rule
set: a VALUES entry is a bool/int/float/None or a str of at most 200 chars
under an identifier-shaped key (at most 40 keys); a FINDINGS entry is a bool
or a short control-free str under a lowercase key (at most 32 keys). Anything
else is DROPPED and COUNTED — never logged, never raised on.

Offline; the in-process functions are called directly (the sandbox runner's
entry points), exactly like tests/test_sandbox_import_guard.py.
"""
import decimal
import logging
import math

import numpy as np
import pandas as pd
import pytest

import code_exec
import exec_transport
import plot_utils
import sandbox_guard

NEW_KEYS = ("values", "values_dropped", "findings", "findings_dropped")
SENTINEL_TEXT = "SENTINEL_CATEGORY_TEXT"
SENTINEL_CELL = "CELL_VALUE_987654321"


@pytest.fixture(autouse=True)
def _close_figures():
    import matplotlib.pyplot as plt
    plt.close("all")
    yield
    plt.close("all")


def _frame() -> pd.DataFrame:
    return pd.DataFrame({"x": ["a", "b", "c"], "y": [1, 2, 3], "z": [3, 1, 2],
                         "secret": [SENTINEL_CELL, SENTINEL_CELL, SENTINEL_CELL]})


# ---------------------------------------------------------------------------
# encode_values / decode_values
# ---------------------------------------------------------------------------
def _values_case() -> dict:
    return {
        "a": 1,
        "b": 2.5,
        "c": True,
        "d": None,
        "e": "txt",
        "f": [1],                               # list: dropped
        "g": {"x": 1},                          # dict: dropped
        "h": pd.Timestamp("2025-01-01"),        # Timestamp: dropped
        "i": "x" * 201,                         # over 200 chars: dropped
        "j": decimal.Decimal("1.5"),            # Decimal: dropped
        1: 5,                                   # non-str key: dropped
        "bad-key": 1,                           # not VALUE_KEY_RE: dropped
        "_ok": 3,
        "9x": 1,                                # starts with a digit: dropped
        "n": float("nan"),                      # kept (fmt shows "n/a")
        "inf": float("inf"),                    # kept
        "s200": "y" * 200,                      # exactly 200: kept
        "df": pd.DataFrame({"q": [1]}),         # DataFrame: dropped
    }


def _check_values_case(kept: dict, dropped: int):
    assert list(kept) == ["a", "b", "c", "d", "e", "_ok", "n", "inf", "s200"], list(kept)
    assert kept["a"] == 1 and type(kept["a"]) is int
    assert kept["b"] == 2.5 and type(kept["b"]) is float
    assert kept["c"] is True
    assert kept["d"] is None
    assert kept["e"] == "txt"
    assert kept["_ok"] == 3
    assert math.isnan(kept["n"])
    assert kept["inf"] == float("inf")
    assert kept["s200"] == "y" * 200
    assert dropped == 9, dropped


def test_encode_values_types_keys():
    kept, dropped = exec_transport.encode_values(_values_case())
    _check_values_case(kept, dropped)
    assert exec_transport.encode_values(None) == ({}, 0)
    assert exec_transport.encode_values("x") == ({}, 1)
    assert exec_transport.encode_values([1, 2]) == ({}, 1)
    assert exec_transport.encode_values(42) == ({}, 1)
    assert exec_transport.encode_values({}) == ({}, 0)
    assert exec_transport.VALUE_KEY_RE.match("A_b9") and exec_transport.VALUE_KEY_RE.match("_x")
    assert not exec_transport.VALUE_KEY_RE.match("9a")
    assert not exec_transport.VALUE_KEY_RE.match("a" * 41)
    assert exec_transport.VALUE_KEY_RE.match("a" * 40)
    assert not exec_transport.VALUE_KEY_RE.match("a b")
    assert not exec_transport.VALUE_KEY_RE.match("a\n")
    assert exec_transport.VALUES_MAX_KEYS == 40
    assert exec_transport.VALUES_STR_MAX == 200


def test_encode_values_numpy_item():
    kept, dropped = exec_transport.encode_values({
        "i": np.int64(3), "f": np.float64(0.5), "b": np.bool_(True), "s": np.str_("s"),
        "i32": np.int32(-7), "f32": np.float32(0.25), "nan": np.float64("nan"),
    })
    assert dropped == 0
    assert kept["i"] == 3 and type(kept["i"]) is int
    assert kept["f"] == 0.5 and type(kept["f"]) is float
    assert kept["b"] is True and type(kept["b"]) is bool
    assert kept["s"] == "s" and isinstance(kept["s"], str)
    assert kept["i32"] == -7 and type(kept["i32"]) is int
    assert kept["f32"] == 0.25 and type(kept["f32"]) is float
    assert math.isnan(kept["nan"]) and type(kept["nan"]) is float
    # a numpy ARRAY has .item() too, but a 2-element one raises there: dropped,
    # never raised through
    kept2, dropped2 = exec_transport.encode_values({"arr": np.array([1, 2]), "ok": 1})
    assert kept2 == {"ok": 1}
    assert dropped2 == 1


def test_encode_values_cap_40():
    raw = {f"k{i:02d}": i for i in range(45)}
    kept, dropped = exec_transport.encode_values(raw)
    assert len(kept) == 40
    assert list(kept) == [f"k{i:02d}" for i in range(40)]      # the first 40, insertion order
    assert dropped == 5
    # a dropped (invalid) entry does not take a slot
    raw2 = {"bad-0": 1, **{f"k{i:02d}": i for i in range(40)}}
    kept2, dropped2 = exec_transport.encode_values(raw2)
    assert len(kept2) == 40 and "bad-0" not in kept2
    assert dropped2 == 1


def test_decode_values_hostile():
    kept, dropped = exec_transport.decode_values(_values_case())
    _check_values_case(kept, dropped)
    assert exec_transport.decode_values("notadict") == ({}, 1)
    assert exec_transport.decode_values([1, 2, 3]) == ({}, 1)
    assert exec_transport.decode_values(None) == ({}, 0)
    assert exec_transport.decode_values(3.5) == ({}, 1)
    kept, dropped = exec_transport.decode_values({"nested": {"a": {"b": 1}}, "ok": 1})
    assert kept == {"ok": 1} and dropped == 1
    kept, dropped = exec_transport.decode_values({"frame": pd.DataFrame({"a": [1]})})
    assert kept == {} and dropped == 1
    kept, dropped = exec_transport.decode_values({"long": "z" * 201, "fine": "z" * 200})
    assert kept == {"fine": "z" * 200} and dropped == 1
    kept, dropped = exec_transport.decode_values({"n": float("nan")})
    assert list(kept) == ["n"] and math.isnan(kept["n"]) and dropped == 0
    kept, dropped = exec_transport.decode_values({"bad key": 1, "9": 2, "": 3, "ok_1": 4, None: 5})
    assert kept == {"ok_1": 4} and dropped == 4
    kept, dropped = exec_transport.decode_values({f"k{i}": i for i in range(50)})
    assert len(kept) == 40 and dropped == 10
    # a dict whose iteration raises: never raises out, counts what it saw
    class Nasty(dict):
        def items(self):
            raise RuntimeError("boom")
    kept, dropped = exec_transport.decode_values(Nasty(a=1))
    assert kept == {} and isinstance(dropped, int)


# ---------------------------------------------------------------------------
# encode_findings / decode_findings
# ---------------------------------------------------------------------------
def test_encode_findings_shape():
    raw = {
        "ok": True,
        "code": "coverage_change",
        "col": "REGION",
        "nb": np.bool_(False),
        "n": 3,                       # a number: dropped
        "f": 1.5,                     # dropped
        "none": None,                 # dropped
        "long": "x" * 65,             # over 64: dropped
        "edge": "x" * 64,             # exactly 64: kept
        "empty": "",                  # 1..64 chars: dropped
        "ctrl": "a\x01b",             # C0 control: dropped
        "tab": "a\tb",                # TAB is a C0 control: dropped
        "nl": "a\nb",                 # dropped
        "Bad": True,                  # uppercase key: dropped
        "9k": True,                   # digit first: dropped
        "needs_phase2": True,
        "lst": ["a"],                 # dropped
        "k" * 41: True,               # key too long: dropped
    }
    kept, dropped = exec_transport.encode_findings(raw)
    assert kept == {"ok": True, "code": "coverage_change", "col": "REGION", "nb": False,
                    "edge": "x" * 64, "needs_phase2": True}
    assert kept["nb"] is False and type(kept["nb"]) is bool
    assert dropped == 12, dropped
    assert exec_transport.encode_findings(None) == ({}, 0)
    assert exec_transport.encode_findings("x") == ({}, 1)
    assert exec_transport.encode_findings([]) == ({}, 1)
    assert exec_transport.FINDING_KEY_RE.match("a_b9") and not exec_transport.FINDING_KEY_RE.match("A")
    assert not exec_transport.FINDING_KEY_RE.match("_a")
    assert exec_transport.FINDINGS_PREFILTER_MAX_KEYS == 32
    assert exec_transport.FINDINGS_STR_MAX == 64
    # cap 32
    many = {f"k{i:02d}": True for i in range(40)}
    kept, dropped = exec_transport.encode_findings(many)
    assert len(kept) == 32 and list(kept) == [f"k{i:02d}" for i in range(32)]
    assert dropped == 8
    # decode: the same rules, on hostile input, never raises
    kept_d, dropped_d = exec_transport.decode_findings(raw)
    assert dropped_d == 12
    assert kept_d == {"ok": True, "code": "coverage_change", "col": "REGION", "nb": False,
                      "edge": "x" * 64, "needs_phase2": True}
    assert exec_transport.decode_findings({"k": {"nested": True}}) == ({}, 1)
    assert exec_transport.decode_findings(["k"]) == ({}, 1)
    assert exec_transport.decode_findings(None) == ({}, 0)


def test_capture_outputs_shape():
    env = {"VALUES": {"a": 1, "bad": [1]}, "FINDINGS": {"ok": True, "n": 2}, "RESULT": 1}
    out = exec_transport.capture_outputs(env)
    assert set(out) == set(NEW_KEYS)
    assert out["values"] == {"a": 1} and out["values_dropped"] == 1
    assert out["findings"] == {"ok": True} and out["findings_dropped"] == 1
    # the names missing from the env (generated code deleted them): empty, nothing dropped
    assert exec_transport.capture_outputs({}) == {"values": {}, "values_dropped": 0,
                                                  "findings": {}, "findings_dropped": 0}
    # replaced with a non-dict: counted as one drop
    out = exec_transport.capture_outputs({"VALUES": "x", "FINDINGS": 7})
    assert out["values"] == {} and out["values_dropped"] == 1
    assert out["findings"] == {} and out["findings_dropped"] == 1


# ---------------------------------------------------------------------------
# hostile containers / scalars: the capture runs AFTER the timed exec, so no
# override written by generated code may run there (it could loop forever)
# ---------------------------------------------------------------------------
class _TrapDict(dict):
    """A dict subclass whose every iteration path fails loudly if called —
    deterministic stand-in for one whose items() never terminates."""
    def items(self):
        raise AssertionError("items() override was called")

    def keys(self):
        raise AssertionError("keys() override was called")

    def values(self):
        raise AssertionError("values() override was called")

    def __iter__(self):
        raise AssertionError("__iter__ override was called")

    def __len__(self):
        raise AssertionError("__len__ override was called")

    def get(self, *a, **k):
        raise AssertionError("get() override was called")


@pytest.mark.parametrize("fn", ["encode_values", "decode_values", "encode_findings", "decode_findings"])
def test_dict_subclass_is_refused_without_running_overrides(fn):
    func = getattr(exec_transport, fn)
    assert func(_TrapDict(a=True, ok=True)) == ({}, 1)


def test_capture_outputs_dict_subclass_refused():
    out = exec_transport.capture_outputs({"VALUES": _TrapDict(a=1), "FINDINGS": _TrapDict(ok=True)})
    assert out == {"values": {}, "values_dropped": 1, "findings": {}, "findings_dropped": 1}


def test_python_site_rebinding_to_a_trap_dict_does_not_hang():
    code = (
        "class T(dict):\n"
        "    def items(self):\n"
        "        while True:\n"
        "            pass\n"
        "VALUES = T(a=1)\nFINDINGS = T(ok=True)\nRESULT = 1\n"
    )
    out = code_exec._execute_in_process(code, {"t": _frame()}, sid="t", timeout=10)
    assert out.get("error") is None, out.get("error")
    assert out["values"] == {} and out["values_dropped"] == 1
    assert out["findings"] == {} and out["findings_dropped"] == 1


class _Counter:
    calls = 0


def _hostile_item_classes():
    counter = _Counter()

    class Raises:
        def item(self):
            counter.calls += 1
            raise AssertionError("item() was called")

    class ReturnsHostile:
        def item(self):
            counter.calls += 1
            return Raises()

    class LoopsForever:
        def item(self):
            counter.calls += 1
            raise AssertionError("an item() that loops forever was called")

    class NpSub(np.float64):
        def item(self):
            counter.calls += 1
            raise AssertionError("np subclass item() override was called")

    class StrSub(str):
        def __str__(self):
            counter.calls += 1
            raise AssertionError("__str__ override was called")

        def __len__(self):
            counter.calls += 1
            raise AssertionError("__len__ override was called")

    class IntSub(int):
        def __int__(self):
            counter.calls += 1
            raise AssertionError("__int__ override was called")

    return counter, Raises, ReturnsHostile, LoopsForever, NpSub, StrSub, IntSub


@pytest.mark.parametrize("side", ["encode", "decode"])
def test_hostile_scalars_dropped_and_item_never_overridden(side):
    counter, Raises, ReturnsHostile, LoopsForever, NpSub, StrSub, IntSub = _hostile_item_classes()
    values = getattr(exec_transport, f"{side}_values")
    findings = getattr(exec_transport, f"{side}_findings")
    raw = {"r": Raises(), "h": ReturnsHostile(), "l": LoopsForever(), "s": StrSub("abc"),
           "i": IntSub(3), "n": NpSub(1.5), "ok": 1}
    kept, dropped = values(raw)
    assert counter.calls == 0, counter.calls
    # the numpy subclass is read through numpy's own item(), never the override
    assert kept == {"n": 1.5, "ok": 1} and type(kept["n"]) is float
    assert dropped == 5
    kept, dropped = findings({"r": Raises(), "s": StrSub("abc"), "nb": NpSub(1.0), "ok": True})
    assert counter.calls == 0, counter.calls
    assert kept == {"ok": True} and dropped == 3
    # a str-subclass KEY is refused too (its __str__ is never called)
    kept, dropped = values({StrSub("key"): 1, "fine": 2})
    assert counter.calls == 0
    assert kept == {"fine": 2} and dropped == 1


# ---------------------------------------------------------------------------
# the data boundary: dropped, counted, never logged
# ---------------------------------------------------------------------------
def _all_log_text(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def test_values_dataframe_dropped_and_counted_not_logged(caplog):
    code = (
        "VALUES['frame'] = dfs['t']\n"
        "VALUES['cells'] = list(dfs['t']['secret'])\n"
        "VALUES['n'] = int(len(dfs['t']))\n"
        "RESULT = VALUES['n']\n"
    )
    with caplog.at_level(logging.DEBUG):
        out = code_exec._execute_in_process(code, {"t": _frame()}, sid="t")
        _ = exec_transport.capture_outputs({"VALUES": {"frame": _frame(), "cells": [SENTINEL_CELL]}})
        _ = exec_transport.decode_values({"frame": _frame(), "cells": [SENTINEL_CELL]})
    assert out.get("error") is None, out.get("error")
    assert out["values"] == {"n": 3}
    assert out["values_dropped"] == 2
    text = _all_log_text(caplog)
    assert SENTINEL_CELL not in text
    for rec in caplog.records:
        assert SENTINEL_CELL not in str(rec.args) and SENTINEL_CELL not in rec.getMessage()
    # whatever line reports the drop carries the COUNT, never the content
    for line in text.splitlines():
        if "VALUES_DROPPED" in line:
            assert "n=2" in line, line


def test_findings_number_and_category_dropped_counted(caplog):
    code = (
        "FINDINGS['coverage'] = 'coverage_change'\n"
        "FINDINGS['count'] = 987654321\n"
        f"FINDINGS['segment'] = '{SENTINEL_TEXT}' * 2\n"
        "FINDINGS['share'] = 0.42\n"
        "RESULT = 1\n"
    )
    with caplog.at_level(logging.DEBUG):
        out = code_exec._execute_in_process(code, {"t": _frame()}, sid="t")
        _ = exec_transport.encode_findings({"segment": SENTINEL_TEXT * 2, "count": 987654321})
        _ = exec_transport.decode_findings({"segment": SENTINEL_TEXT * 2, "count": 987654321})
    assert out.get("error") is None, out.get("error")
    # A3.1: capture/decode is SHAPE-only (bool or str 1-64 chars) -> the 44-char str is kept;
    # the semantic drop of a category value is narrative.validate_findings (B7.1, test_findings_validator.py).
    assert out["findings"] == {"coverage": "coverage_change", "segment": SENTINEL_TEXT * 2}
    assert out["findings_dropped"] == 2
    text = _all_log_text(caplog)
    assert SENTINEL_TEXT not in text
    assert "987654321" not in text
    for line in text.splitlines():
        if "FINDINGS_DROPPED" in line:
            assert "n=2" in line, line


# ---------------------------------------------------------------------------
# the python exec site
# ---------------------------------------------------------------------------
def test_python_site_captures_values_findings():
    for key in NEW_KEYS:
        assert key in exec_transport._PYTHON_KEYS, key
    code = (
        "VALUES = {'total': 5, 'name': 'North', 'share': 0.25, 'flag': True, 'none': None}\n"
        "FINDINGS = {'coverage': 'coverage_change', 'needs_phase2': True}\n"
        "RESULT = 1\n"
    )
    out = code_exec._execute_in_process(code, {"t": _frame()}, sid="t")
    assert out.get("error") is None, out.get("error")
    assert out["result"] == 1
    assert out["values"] == {"total": 5, "name": "North", "share": 0.25, "flag": True, "none": None}
    assert out["values_dropped"] == 0
    assert out["findings"] == {"coverage": "coverage_change", "needs_phase2": True}
    assert out["findings_dropped"] == 0
    assert set(NEW_KEYS) <= set(out)
    # the two names start as FRESH dicts in every job: item assignment works
    # without an assignment, and nothing leaks from the previous job
    out2 = code_exec._execute_in_process("VALUES['x'] = 1\nFINDINGS['ok'] = True\nRESULT = 2\n",
                                         {"t": _frame()}, sid="t")
    assert out2["values"] == {"x": 1} and out2["findings"] == {"ok": True}
    out3 = code_exec._execute_in_process("RESULT = 3\n", {"t": _frame()}, sid="t")
    assert out3["values"] == {} and out3["findings"] == {}
    assert out3["values_dropped"] == 0 and out3["findings_dropped"] == 0
    # numpy scalars from pandas land as plain Python numbers
    out4 = code_exec._execute_in_process(
        "VALUES['total'] = dfs['t']['y'].sum()\nVALUES['mean'] = dfs['t']['y'].mean()\nRESULT = 0\n",
        {"t": _frame()}, sid="t")
    assert out4["values"] == {"total": 6, "mean": 2.0}
    assert type(out4["values"]["total"]) is int and type(out4["values"]["mean"]) is float
    # a failing block carries no capture keys with content worth reading: the
    # error shape is unchanged (no `result`)
    bad = code_exec._execute_in_process("VALUES['a'] = 1\nraise ValueError('x')\n", {"t": _frame()}, sid="t")
    assert bad.get("error")
    assert "result" not in bad


# ---------------------------------------------------------------------------
# the plot exec site — all four success returns
# ---------------------------------------------------------------------------
PLOT_CASES = {
    "plotly_single": (
        "VALUES['k'] = 1\nFINDINGS['ok'] = True\n"
        "fig = px.bar(dfs['t'], x='x', y='y')\n", False, "plotly_html"),
    "plotly_grid_split": (
        "VALUES['k'] = 2\nFINDINGS['ok'] = True\n"
        "from plotly.subplots import make_subplots\n"
        "fig = make_subplots(rows=1, cols=2)\n"
        "fig.add_trace(go.Bar(x=dfs['t']['x'], y=dfs['t']['y']), row=1, col=1)\n"
        "fig.add_trace(go.Bar(x=dfs['t']['x'], y=dfs['t']['z']), row=1, col=2)\n", True, "multi_charts"),
    "mpl_single": (
        "VALUES['k'] = 3\nFINDINGS['ok'] = True\n"
        "plt.figure()\nplt.bar(dfs['t']['x'], dfs['t']['y'])\n", False, "image"),
    "mpl_multi_axes_split": (
        "VALUES['k'] = 4\nFINDINGS['ok'] = True\n"
        "fig, axes = plt.subplots(1, 2)\n"
        "axes[0].bar(dfs['t']['x'], dfs['t']['y'])\n"
        "axes[1].bar(dfs['t']['x'], dfs['t']['z'])\n", True, "multi_charts"),
}


@pytest.mark.parametrize("case", list(PLOT_CASES))
def test_plot_site_captures_on_all_success_returns(case, monkeypatch):
    for key in NEW_KEYS:
        assert key in exec_transport._PLOT_KEYS, key
    code, split, marker = PLOT_CASES[case]
    seen = []

    def recording_capture(env):
        seen.append(dict(env.get("VALUES") or {}))
        return {"values": {"stub": case}, "values_dropped": 7,
                "findings": {"stub": True}, "findings_dropped": 8}

    monkeypatch.setattr(exec_transport, "capture_outputs", recording_capture)
    out = plot_utils._render_in_process(code, {"t": _frame()}, "t", split_multi_axes=split)
    assert out.get("ok") is True, out.get("error")
    assert marker in out, sorted(out)
    assert out["values"] == {"stub": case}
    assert out["values_dropped"] == 7
    assert out["findings"] == {"stub": True}
    assert out["findings_dropped"] == 8
    assert len(seen) == 1, seen
    assert seen[0] == {"k": list(PLOT_CASES).index(case) + 1}


@pytest.mark.parametrize("case", ["plotly_single", "mpl_single"])
def test_plot_site_real_capture_values(case):
    code, split, marker = PLOT_CASES[case]
    out = plot_utils._render_in_process(code, {"t": _frame()}, "t", split_multi_axes=split)
    assert out.get("ok") is True, out.get("error")
    assert out["values"] == {"k": list(PLOT_CASES).index(case) + 1}
    assert out["findings"] == {"ok": True}
    assert out["values_dropped"] == 0 and out["findings_dropped"] == 0


def test_plot_site_error_returns_unchanged():
    out = plot_utils._render_in_process("VALUES['k'] = 1\nraise ValueError('x')\n",
                                        {"t": _frame()}, "t")
    assert out.get("ok") is False
    for key in NEW_KEYS:
        assert key not in out, key


def test_global_plot_scope_not_mutated():
    before = set(plot_utils.GLOBAL_PLOT_SCOPE)
    assert "VALUES" not in before and "FINDINGS" not in before
    code, split, _ = PLOT_CASES["mpl_single"]
    out = plot_utils._render_in_process(code, {"t": _frame()}, "t", split_multi_axes=split)
    assert out.get("ok") is True
    assert "VALUES" not in plot_utils.GLOBAL_PLOT_SCOPE
    assert "FINDINGS" not in plot_utils.GLOBAL_PLOT_SCOPE
    assert set(plot_utils.GLOBAL_PLOT_SCOPE) == before
    # a second job never sees the first job's VALUES
    out2 = plot_utils._render_in_process("plt.figure()\nplt.bar(dfs['t']['x'], dfs['t']['y'])\n",
                                         {"t": _frame()}, "t")
    assert out2.get("ok") is True
    assert out2["values"] == {} and out2["findings"] == {}


# ---------------------------------------------------------------------------
# main-app side: _reconstruct keeps the new keys, degrades hostile input
# ---------------------------------------------------------------------------
def _response(payload, kind="PYTHON") -> dict:
    return {"status": "ok", "kind": kind, "payload": payload, "elapsed_ms": 1,
            "peak_rss_mb": 1.0, "stdout": "", "stderr": "", "traceback": "",
            "exit_code": 0, "signal": None, "reason": None}


def _wire(obj):
    return exec_transport.loads(exec_transport.dumps(obj))


def _python_payload(job_dir, **extra) -> dict:
    """The wire form of a `safe_execute` success (typed `result` / `preview`
    specs through the real serializer) with the HOSTILE extras laid on top
    AFTER serialization — exactly what a tampered sandbox could send."""
    base = exec_transport.serialize_result(
        {"error": None, "result": None, "preview": None, "image_base64": None}, job_dir)
    base.update(extra)
    return _wire(base)


def test_reconstruct_keeps_new_keys(tmp_path):
    job_dir = exec_transport.create_job_dir(tmp_path, exec_transport.new_job_id())
    good = _python_payload(job_dir, values={"a": 1, "b": "x", "c": [1]}, values_dropped=2,
                           findings={"ok": True, "n": 5}, findings_dropped=1)
    decoded = exec_transport.deserialize_result(_response(good), job_dir, "PYTHON", 60)
    assert decoded.get("error") is None, decoded
    assert decoded["values"] == {"a": 1, "b": "x"}
    assert decoded["values_dropped"] == 3          # the payload's 2 + the list
    assert decoded["findings"] == {"ok": True}
    assert decoded["findings_dropped"] == 2        # the payload's 1 + the number

    hostile = _python_payload(job_dir, values="garbage", values_dropped="x",
                              findings=[1, 2], findings_dropped=10 ** 9)
    decoded = exec_transport.deserialize_result(_response(hostile), job_dir, "PYTHON", 60)
    assert decoded.get("error") is None, decoded     # never a violation
    assert decoded["values"] == {}
    assert decoded["values_dropped"] == 1             # known_number("x") -> 0, + the non-dict
    assert decoded["findings"] == {}
    assert decoded["findings_dropped"] == 1           # 1e9 is past known_number's cap -> 0, + 1

    nested = _python_payload(job_dir, values={"deep": {"a": {"b": 1}}, "ok": 2, "big": "z" * 500},
                             values_dropped=1.5,
                             findings={"ok": "x" * 100, "fine": "coverage_change"},
                             findings_dropped=2.5)
    decoded = exec_transport.deserialize_result(_response(nested), job_dir, "PYTHON", 60)
    assert decoded.get("error") is None
    assert decoded["values"] == {"ok": 2}
    assert decoded["values_dropped"] == 2             # 1.5 is not a count -> 0, + deep + big
    assert decoded["findings"] == {"fine": "coverage_change"}
    assert decoded["findings_dropped"] == 1

    # a payload without the keys at all (an older sandbox image): no error
    old = _python_payload(job_dir)
    decoded = exec_transport.deserialize_result(_response(old), job_dir, "PYTHON", 60)
    assert decoded.get("error") is None
    assert "values" not in decoded or decoded["values"] == {}

    # the PLOT shape through the real serializer
    plot_ret = {"ok": True, "plotly_html": "<div>chart</div>", "is_plotly": True, "chart_data": None,
                "values": {"k": 1, "bad": {"x": 1}}, "values_dropped": 0,
                "findings": {"ok": True, "cat": 3}, "findings_dropped": 0}
    payload = exec_transport.serialize_result(plot_ret, job_dir)
    for key in NEW_KEYS:
        assert key in payload, key
    decoded = exec_transport.deserialize_result(_response(_wire(payload), kind="PLOT"), job_dir, "PLOT", 60)
    assert decoded.get("ok") is True, decoded
    assert decoded["plotly_html"] == "<div>chart</div>"
    assert decoded["values"] == {"k": 1} and decoded["values_dropped"] == 1
    assert decoded["findings"] == {"ok": True} and decoded["findings_dropped"] == 1

    # NaN survives the wire into VALUES (the module's own dialect keeps the token)
    nan_ret = _python_payload(job_dir, values={"n": float("nan")}, values_dropped=0,
                              findings={}, findings_dropped=0)
    decoded = exec_transport.deserialize_result(_response(nan_ret), job_dir, "PYTHON", 60)
    assert math.isnan(decoded["values"]["n"])


# ---------------------------------------------------------------------------
# pdc_diag registration
# ---------------------------------------------------------------------------
def test_pdc_diag_registered_at_both_exec_sites():
    import pdc_diag
    assert plot_utils.GLOBAL_PLOT_SCOPE["pdc_diag"] is pdc_diag
    out = code_exec._execute_in_process("RESULT = pdc_diag.PDC_DIAG_VERSION\n", {"t": _frame()}, sid="t")
    assert out.get("error") is None, out.get("error")
    assert out["result"] == pdc_diag.PDC_DIAG_VERSION == 1
    # usable for real inside both sites
    diag_code = (
        "r = pdc_diag.detect_anomalies(dfs['t'], 'p', 'v')\n"
        "VALUES.update(r['values'])\nFINDINGS.update(r['findings'])\n"
    )
    series = pd.DataFrame({"p": [f"2025-{m:02d}" for m in range(1, 7)], "v": [10, 11, 10, 12, 100, 11]})
    out = code_exec._execute_in_process(diag_code + "RESULT = r['table']\n", {"t": series}, sid="t")
    assert out.get("error") is None, out.get("error")
    assert out["findings"].get("anomaly") == "anomaly_confirmed"
    assert out["values"].get("anomaly_period") == "2025-05"
    plot = plot_utils._render_in_process(
        diag_code + "fig = pdc_diag.plot_highlighted_series(r['table'])\n", {"t": series}, "t")
    assert plot.get("ok") is True, plot.get("error")
    assert plot["findings"].get("anomaly") == "anomaly_confirmed"


def test_pdc_diag_not_in_sandbox_denylist():
    assert "pdc_diag" not in sandbox_guard.DENIED_MODULES
    out = code_exec._execute_in_process("import pdc_diag\nRESULT = pdc_diag.PDC_DIAG_VERSION\n",
                                        {"t": _frame()}, sid="t")
    assert out.get("error") is None, out.get("error")
    assert out["result"] == 1
    plot = plot_utils._render_in_process(
        "import pdc_diag\nplt.figure()\nplt.bar(dfs['t']['x'], dfs['t']['y'])\n", {"t": _frame()}, "t")
    assert plot.get("ok") is True, plot.get("error")


# ---------------------------------------------------------------------------
# B7.7 — the near-zero chart error is a fixed, number-free sentence
# ---------------------------------------------------------------------------
def test_near_zero_chart_error_has_no_numbers():
    out = plot_utils._render_in_process(
        "plt.figure()\nplt.bar(dfs['t']['x'], [0.01, 0.02, 0.03])\n", {"t": _frame()}, "t")
    assert out.get("ok") is False
    err = out.get("error") or ""
    assert err.startswith("NearZeroChartError:"), err
    assert not any(ch.isdigit() for ch in err), err
    assert "max=" not in err and "range=" not in err
    # the same sentence for different magnitudes: no value leaks through it
    out2 = plot_utils._render_in_process(
        "plt.figure()\nplt.bar(dfs['t']['x'], [0.0001, 0.00002, 0.00003])\n", {"t": _frame()}, "t")
    assert out2.get("error") == err
