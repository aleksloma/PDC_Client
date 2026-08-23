"""Deterministic outlier helpers (QA 2.6) — the sandbox pair
`outlier_mask` / `drop_extreme_outliers` that replaced the 87-line planner
prompt block. Pure unit + registration checks at BOTH exec sites.
"""
import numpy as np
import pandas as pd
import pytest

from outlier_utils import outlier_mask, drop_extreme_outliers


def _hr_frame():
    """20 salaries around 3k + one planted 750k (the QA 2.6 repro shape)."""
    rng = np.random.default_rng(7)
    vals = list(rng.normal(3000, 300, 20).round(0)) + [750_000.0]
    return pd.DataFrame({"dept": ["HR"] * 21, "salary": vals})


def test_lone_extreme_dropped_with_count():
    df = _hr_frame()
    out, n = drop_extreme_outliers(df, "salary")
    # The union mask can also flag the percentile edge row(s); once the gap
    # guard fires, ALL flagged rows drop (exact port of the prompt algorithm).
    assert 1 <= n <= 2
    assert len(out) == len(df) - n
    assert out["salary"].max() < 10_000          # the 750k row is gone
    assert out["salary"].mean() < 5_000          # the average is sane again


def test_benign_spread_untouched_same_object():
    rng = np.random.default_rng(3)
    df = pd.DataFrame({"v": rng.normal(100, 15, 500)})
    out, n = drop_extreme_outliers(df, "v")
    # percentile/IQR always FLAG ~1-2% of normal data, but the gap guard sees
    # gap ~ 0 and refuses to act — the ORIGINAL frame comes back.
    assert n == 0
    assert out is df


def test_clustered_sentinel_caught():
    # >=1% of rows share the extreme → p99 sits ON it (percentile blind);
    # the MAD leg still flags it (bulk has natural spread → MAD > 0).
    rng = np.random.default_rng(11)
    vals = list(rng.normal(100, 10, 200)) + [999_999.0] * 5
    df = pd.DataFrame({"v": vals})
    mask = outlier_mask(df["v"])
    assert int(mask.loc[df["v"] > 500_000].sum()) == 5   # all sentinels flagged
    out, n = drop_extreme_outliers(df, "v")
    assert n >= 5
    assert out["v"].max() < 500_000


def test_small_sample_flags_nothing():
    s = pd.Series([1.0, 2.0, 900.0, 3.0])  # 4 points — too few to judge
    assert not outlier_mask(s).any()
    df = pd.DataFrame({"v": s})
    out, n = drop_extreme_outliers(df, "v")
    assert n == 0 and out is df


def test_non_numeric_raises_value_error():
    s = pd.Series(["a", "b", "c", "d", "e", "f"])
    with pytest.raises(ValueError):
        outlier_mask(s)
    df = pd.DataFrame({"name": s})
    with pytest.raises(ValueError):
        drop_extreme_outliers(df, "name")


def test_missing_column_raises_value_error():
    with pytest.raises(ValueError):
        drop_extreme_outliers(pd.DataFrame({"a": [1.0] * 6}), "nope")


def test_empty_and_all_nan_are_noops():
    empty = pd.Series([], dtype=float)
    assert list(outlier_mask(empty)) == []
    nans = pd.Series([np.nan] * 10)
    assert not outlier_mask(nans).any()
    df = pd.DataFrame({"v": [np.nan] * 10})
    out, n = drop_extreme_outliers(df, "v")
    assert n == 0 and out is df


def test_mask_aligned_to_input_index():
    df = _hr_frame()
    df.index = [f"r{i}" for i in range(len(df))]
    mask = outlier_mask(df["salary"])
    assert list(mask.index) == list(df.index)
    assert mask.dtype == bool


# --- registration at BOTH exec sites -----------------------------------------

def test_registered_in_code_exec_env_and_plot_scope():
    import code_exec
    import plot_utils
    assert code_exec.outlier_mask is outlier_mask
    assert code_exec.drop_extreme_outliers is drop_extreme_outliers
    assert plot_utils.GLOBAL_PLOT_SCOPE["outlier_mask"] is outlier_mask
    assert plot_utils.GLOBAL_PLOT_SCOPE["drop_extreme_outliers"] is drop_extreme_outliers


def test_safe_execute_code_can_call_helper():
    from code_exec import safe_execute
    df = _hr_frame()
    out = safe_execute(
        "df2, n = drop_extreme_outliers(dfs['hr.csv'], 'salary')\n"
        "RESULT = {'n': int(n), 'mean': float(df2['salary'].mean())}",
        {"hr.csv": df}, sid="test")
    assert out["error"] is None
    assert out["result"]["n"] >= 1
    assert out["result"]["mean"] < 10_000


def test_render_plot_safe_code_can_call_helper():
    from plot_utils import render_plot_safe
    df = _hr_frame()
    code = (
        "df2, n = drop_extreme_outliers(dfs['hr.csv'], 'salary')\n"
        "fig = px.bar(df2.groupby('dept', as_index=False)['salary'].mean(), "
        "x='dept', y='salary', title=f'excluded {n}')\n")
    out = render_plot_safe(code, {"hr.csv": df}, "test")
    assert isinstance(out, dict)
    assert not out.get("error"), out.get("error")
