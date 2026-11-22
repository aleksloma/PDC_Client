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
    # The union mask can also flag the percentile edge row(s). Since C1 / F3
    # only the side whose gap fired is dropped (here the high side, so the
    # low percentile-edge row stays); the bound below holds either way.
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


# --- one-sided drop (C1 / F3) -------------------------------------------------
# Only the side whose gap fired is dropped: high side = flagged rows ABOVE the
# median of the column's non-null values, low side = flagged rows BELOW it.
# The mask, the gap guard (span of the never-flagged rows), the `< 5 kept`
# rule and the same-object no-op are unchanged.

def _sides(df, col):
    v = df[col].astype(float)
    mask = outlier_mask(df[col])
    med = float(v.dropna().median())
    return mask, mask & (v > med), mask & (v < med)


def _high_extreme_frame():
    """500 values around 100 (an ordinary low tail the percentile leg flags)
    plus ONE value far above the bulk."""
    rng = np.random.default_rng(42)
    vals = list(rng.normal(100, 10, 500).round(2)) + [10_000.0]
    return pd.DataFrame({"grp": ["a", "b"] * 250 + ["a"], "v": vals})


def _low_extreme_frame():
    """The mirror: 500 values around 1000 plus ONE value far below the bulk."""
    rng = np.random.default_rng(43)
    vals = list(rng.normal(1000, 10, 500).round(2)) + [-50_000.0]
    return pd.DataFrame({"grp": ["a", "b"] * 250 + ["a"], "v": vals})


def test_far_high_extreme_drops_only_the_high_side():
    df = _high_extreme_frame()
    mask, hi, lo = _sides(df, "v")
    # precondition: the ordinary low tail IS flagged (else the case is moot)
    assert int(lo.sum()) > 0 and int(hi.sum()) > 0, (int(lo.sum()), int(hi.sum()))
    out, n = drop_extreme_outliers(df, "v")
    assert n == int(hi.sum()), n
    assert out is not df
    assert len(out) == len(df) - n
    assert list(out.index) == list(df.index[~hi])
    # every low-side flagged row is kept, untouched
    assert set(df.index[lo]) <= set(out.index)
    pd.testing.assert_frame_equal(out, df[~hi])
    assert out["v"].max() < 1_000
    assert out["v"].min() == df["v"].min()


def test_far_low_extreme_drops_only_the_low_side():
    df = _low_extreme_frame()
    mask, hi, lo = _sides(df, "v")
    assert int(lo.sum()) > 0 and int(hi.sum()) > 0, (int(lo.sum()), int(hi.sum()))
    out, n = drop_extreme_outliers(df, "v")
    assert n == int(lo.sum()), n
    assert out is not df
    assert list(out.index) == list(df.index[~lo])
    assert set(df.index[hi]) <= set(out.index)
    pd.testing.assert_frame_equal(out, df[~lo])
    assert out["v"].min() > 0
    assert out["v"].max() == df["v"].max()


def test_both_sides_far_drop_both_sides():
    rng = np.random.default_rng(42)
    vals = list(rng.normal(100, 10, 500).round(2)) + [10_000.0, -10_000.0]
    df = pd.DataFrame({"v": vals})
    mask, hi, lo = _sides(df, "v")
    out, n = drop_extreme_outliers(df, "v")
    # both gaps fire -> today's behaviour: every flagged row goes
    assert n == int(mask.sum()) == int(hi.sum()) + int(lo.sum()), n
    assert list(out.index) == list(df.index[~mask])
    assert out["v"].max() < 1_000 and out["v"].min() > -1_000


def test_one_sided_drop_keeps_nan_rows_and_uses_non_null_median():
    df = _high_extreme_frame()
    df = pd.concat([df, pd.DataFrame({"grp": ["c", "c"], "v": [np.nan, np.nan]})],
                   ignore_index=True)
    mask, hi, lo = _sides(df, "v")
    out, n = drop_extreme_outliers(df, "v")
    assert n == int(hi.sum()), n
    assert int(out["v"].isna().sum()) == 2          # NaN rows are never flagged
    assert set(df.index[lo]) <= set(out.index)


def test_one_sided_drop_returns_a_copy():
    df = _high_extreme_frame()
    out, n = drop_extreme_outliers(df, "v")
    assert n > 0
    before = df.copy()
    out.loc[out.index[0], "v"] = -1.0
    pd.testing.assert_frame_equal(df, before)       # the caller's frame is untouched


def test_benign_with_both_tails_flagged_is_still_a_same_object_noop():
    rng = np.random.default_rng(42)
    df = pd.DataFrame({"v": rng.normal(100, 10, 500).round(2)})
    mask, hi, lo = _sides(df, "v")
    assert int(hi.sum()) > 0 and int(lo.sum()) > 0   # tails flagged, gap ~ 0
    out, n = drop_extreme_outliers(df, "v")
    assert n == 0 and out is df


@pytest.mark.parametrize("vals", [
    [1.0, 2.0, 3.0, 4.0, 5.0, 1000.0],        # one far side, 4 rows never flagged
    [-1000.0, 1.0, 2.0, 3.0, 4.0, 1000.0],    # both far sides, 4 rows never flagged
])
def test_fewer_than_five_unflagged_rows_is_a_noop_as_today(vals):
    df = pd.DataFrame({"v": vals})
    mask = outlier_mask(df["v"])
    assert int((~mask).sum()) < 5, mask.tolist()
    out, n = drop_extreme_outliers(df, "v")
    assert n == 0 and out is df


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
