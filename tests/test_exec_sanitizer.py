"""Unit tests for the Article XIII pre-execution sanitize gate
(exec_sanitizer.sanitize_for_execution): every non-standard dtype handed to
generated code must come out as plain, standard pandas — and clean frames must
pass through untouched (identity fast path). All offline."""
import numpy as np
import pandas as pd
import pytest

import code_exec
import exec_sanitizer
from exec_sanitizer import sanitize_for_execution


def test_categorical_object_to_object():
    df = pd.DataFrame({"seg": pd.Categorical(["a", "b", None, "a"])})
    out = sanitize_for_execution({"t": df}, "t")["t"]
    assert str(out["seg"].dtype) == "object"
    assert out["seg"].tolist()[:2] == ["a", "b"]
    missing = out["seg"].iloc[2]
    assert missing is not pd.NA and (missing is None or missing != missing)


def test_categorical_datetime_to_datetime():
    dates = pd.to_datetime(["2025-01-15", "2025-02-10"])
    df = pd.DataFrame({"d": pd.Categorical(dates)})
    out = sanitize_for_execution({"t": df}, "t")["t"]
    assert str(out["d"].dtype) == "datetime64[ns]"
    assert out["d"].dt.month.tolist() == [1, 2]


def test_date_string_categories_to_datetime():
    df = pd.DataFrame({"d": pd.Categorical(["2025-01-15", "2025-02-10",
                                            "2025-01-15"])})
    out = sanitize_for_execution({"t": df}, "t")["t"]
    assert str(out["d"].dtype) == "datetime64[ns]"
    assert out["d"].tolist() == list(pd.to_datetime(
        ["2025-01-15", "2025-02-10", "2025-01-15"]))


@pytest.mark.parametrize("cats", [["seg1", "seg2"], ["2021", "2022"],
                                  ["1", "2"], ["A-1", "B-2"]])
def test_nondate_categories_stay_strings(cats):
    df = pd.DataFrame({"c": pd.Categorical(cats)})
    out = sanitize_for_execution({"t": df}, "t")["t"]
    assert str(out["c"].dtype) == "object"
    assert out["c"].tolist() == cats


def test_numeric_categories_to_numeric():
    df = pd.DataFrame({"n": pd.Categorical([1, 2, 1])})
    out = sanitize_for_execution({"t": df}, "t")["t"]
    assert out["n"].dtype.kind in "if"
    assert out["n"].tolist() == [1, 2, 1]


def test_clean_frame_identity_fast_path():
    df = pd.DataFrame({
        "i": np.array([1, 2], dtype="int64"),
        "f": np.array([1.5, 2.5]),
        "s": ["a", "b"],
        "d": pd.to_datetime(["2025-01-01", "2025-01-02"]),
        "tz": pd.to_datetime(["2025-01-01", "2025-01-02"]).tz_localize("UTC"),
    })
    dfs = {"t": df}
    out = sanitize_for_execution(dfs, "t")
    assert out is dfs                      # same dict object — zero copies
    assert out["t"] is df                  # same frame object
    empty = {}
    assert sanitize_for_execution(empty, "t") is empty


def test_sparse_to_dense():
    df = pd.DataFrame({"v": pd.arrays.SparseArray([1.0, np.nan, 3.0])})
    out = sanitize_for_execution({"t": df}, "t")["t"]
    assert str(out["v"].dtype) == "float64"
    assert out["v"].fillna(0).tolist() == [1.0, 0.0, 3.0]


def test_nullable_int():
    with_na = pd.DataFrame({"v": pd.array([1, pd.NA], dtype="Int64")})
    out = sanitize_for_execution({"t": with_na}, "t")["t"]
    assert str(out["v"].dtype) == "float64"
    assert out["v"].iloc[0] == 1.0 and np.isnan(out["v"].iloc[1])
    no_na = pd.DataFrame({"v": pd.array([1, 2], dtype="Int64")})
    out2 = sanitize_for_execution({"t": no_na}, "t")["t"]
    assert str(out2["v"].dtype) == "int64"


def test_nullable_boolean_and_string():
    df = pd.DataFrame({
        "b": pd.array([True, pd.NA], dtype="boolean"),
        "s": pd.array(["x", pd.NA], dtype="string"),
    })
    out = sanitize_for_execution({"t": df}, "t")["t"]
    assert str(out["b"].dtype) == "object"
    assert out["b"].iloc[0] is True
    assert out["b"].iloc[1] is not pd.NA and out["b"].iloc[1] != out["b"].iloc[1]
    assert str(out["s"].dtype) == "object"
    assert out["s"].iloc[0] == "x"
    assert out["s"].iloc[1] is not pd.NA


def test_period_and_interval_to_str():
    df = pd.DataFrame({
        "p": pd.period_range("2025-01", periods=2, freq="M"),
        "iv": pd.IntervalIndex.from_breaks([0, 1, 2]),
    })
    out = sanitize_for_execution({"t": df}, "t")["t"]
    assert str(out["p"].dtype) == "object"
    assert all(isinstance(v, str) for v in out["p"])
    assert str(out["iv"].dtype) == "object"
    assert all(isinstance(v, str) for v in out["iv"])


def test_categorical_index_converted():
    df = pd.DataFrame({"v": [1, 2]},
                      index=pd.CategoricalIndex(["a", "b"]))
    out = sanitize_for_execution({"t": df}, "t")["t"]
    assert not isinstance(out.index.dtype, pd.CategoricalDtype)
    assert list(out.index) == ["a", "b"]


def test_injected_failure_passthrough(monkeypatch):
    def _boom(s):
        raise RuntimeError("injected")
    monkeypatch.setattr(exec_sanitizer, "_convert_series", _boom)
    df = pd.DataFrame({"seg": pd.Categorical(["a", "b"])})
    out = sanitize_for_execution({"t": df}, "t")  # must not raise
    assert str(out["t"]["seg"].dtype) == "category"  # column passed through


def test_original_frame_unmutated():
    df = pd.DataFrame({"seg": pd.Categorical(["a", "b"]), "v": [1, 2]})
    dfs = {"t": df}
    out = sanitize_for_execution(dfs, "t")
    assert str(df["seg"].dtype) == "category"      # original untouched
    assert out["t"] is not df
    assert dfs["t"] is df                          # caller's dict untouched
    out["t"]["v"] = 0                              # column REPLACEMENT on copy
    assert df["v"].tolist() == [1, 2]


def test_non_dataframe_values_tolerated():
    df = pd.DataFrame({"seg": pd.Categorical(["a"])})
    out = sanitize_for_execution({"junk": "not a df", "t": df}, "t")
    assert out["junk"] == "not a df"
    assert str(out["t"]["seg"].dtype) == "object"
    assert sanitize_for_execution("not a dict", "t") == "not a dict"


def test_safe_execute_integration_observed_groupby():
    """The gate is on-path inside code_exec.safe_execute: a categorical with
    an unused 'ghost' category must NOT surface in groupby results, and the
    `df` alias must see the sanitized frame too."""
    df = pd.DataFrame({
        "seg": pd.Categorical(["a", "b", "a"], categories=["a", "b", "ghost"]),
        "v": [1, 2, 3],
    })
    res = code_exec.safe_execute(
        "RESULT = dfs['t'].groupby('seg')['v'].sum().to_dict()",
        {"t": df}, sid="t")
    assert res.get("result") == {"a": 4, "b": 2}   # no 'ghost' key
    res2 = code_exec.safe_execute("RESULT = df['seg'].dtype.name",
                                  {"t": df}, sid="t")
    assert res2.get("result") == "object"
    assert str(df["seg"].dtype) == "category"      # caller frame untouched
