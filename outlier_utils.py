"""Deterministic extreme-outlier helpers, pre-imported into BOTH execution
sandboxes (`code_exec.safe_execute` env and `plot_utils.GLOBAL_PLOT_SCOPE`).

These encode EXACTLY the algorithm the brain's planner prompt used to spell
out inline (QA 2.6): the union of three robust, scale-free tests —
median/MAD distance, 1st-99th percentile, and 3x-IQR — followed by a GAP
GUARD so benign edge-of-bulk tails are never trimmed, only values sitting far
beyond the bulk of the data — and only on the SIDE (high or low tail) whose
gap fired, so one far-off tail never takes the other, benign tail with it.
The planner prompt now just tells generated code
to call `drop_extreme_outliers(df, "col")` instead of re-implementing this,
so the behavior is deterministic instead of prompt-adherence-dependent.

Leaf module (exec_sanitizer discipline): imports pandas/numpy only — never
code_exec / plot_utils / local_store. Never raises on weird numeric input
(empty, all-NaN, tiny samples → no-op); non-numeric input raises a clear
ValueError so generated code fails visibly instead of silently mis-filtering.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def outlier_mask(series: pd.Series) -> pd.Series:
    """Boolean mask of GENUINE extreme outliers in a numeric Series.

    Union of 3 robust methods (any fires → flagged), aligned to the input
    index. Conservative multipliers so only true extremes flag:
      (1) median/MAD distance  > 10 robust std (1.4826*MAD; std fallback)
      (2) outside the 1st-99th percentile
      (3) outside q1 - 3*IQR .. q3 + 3*IQR (when IQR > 0)
    Empty / all-NaN / fewer than 5 non-null values → all-False (flag nothing).
    Non-numeric input → ValueError.
    """
    if not isinstance(series, pd.Series):
        try:
            series = pd.Series(series)
        except Exception:
            raise ValueError("outlier_mask requires a pandas Series or array-like")
    try:
        x = series.astype(float)
    except (ValueError, TypeError) as e:
        raise ValueError(
            f"outlier_mask requires a numeric column, got dtype {series.dtype!r}: {e}"
        ) from e
    ok = x.notna()
    v = x[ok]
    none_flagged = pd.Series(False, index=x.index)
    if len(v) < 5:
        return none_flagged  # too few points to judge -> flag nothing
    med = v.median()
    # (1) robust distance from the median — size-independent, catches small-N
    #     lone extremes AND clustered sentinels
    mad = (v - med).abs().median()
    scale = 1.4826 * mad if mad > 0 else v.std(ddof=0)
    if scale and scale > 0:
        mad_flag = (x - med).abs() > 10 * scale
    else:
        mad_flag = none_flagged
    # (2) percentile — helps large data with a thin tail
    lo, hi = v.quantile(0.01), v.quantile(0.99)
    pct_flag = (x < lo) | (x > hi)
    # (3) IQR — generous 3x so only real extremes
    q1, q3 = v.quantile(0.25), v.quantile(0.75)
    iqr = q3 - q1
    iqr_flag = ((x < q1 - 3 * iqr) | (x > q3 + 3 * iqr)) if iqr > 0 else none_flagged
    return (mad_flag | pct_flag | iqr_flag) & ok


def drop_extreme_outliers(df: pd.DataFrame, col: str) -> tuple[pd.DataFrame, int]:
    """(filtered_df, n_dropped): drop rows whose `col` value is a genuine
    extreme — but ONLY when the GAP GUARD confirms the flagged values sit far
    beyond the bulk (gap > 3x the kept data's own span). Percentile/IQR always
    flag ~1-2% of perfectly normal data, but those tails sit right at the edge
    of the bulk (gap ~ 0) → nothing is dropped and the ORIGINAL df is returned
    with n_dropped=0. No silent trimming on benign data.

    ONE-SIDED: the gaps are measured per side against the rows not flagged at
    all (`hi_gap` = highest flagged value - highest kept value, `lo_gap` =
    lowest kept value - lowest flagged value; `span` = the kept values'
    range). `hi_gap > 3*span` drops the flagged rows ABOVE the median of the
    column's non-null values, `lo_gap > 3*span` the flagged rows BELOW it;
    both gaps -> both sides. A flagged row on the side whose gap did not fire
    (or exactly at the median) is kept. `n_dropped` = the rows actually
    dropped; a drop returns a copy.

    Missing column / non-numeric column → ValueError. Empty df, all-NaN,
    nothing flagged, fewer than 5 rows left unflagged, no gap over 3x the
    span → (df, 0) (same object, no copy).
    """
    if not isinstance(df, pd.DataFrame):
        raise ValueError("drop_extreme_outliers requires a DataFrame as its first argument")
    if col not in df.columns:
        raise ValueError(f"drop_extreme_outliers: column {col!r} does not exist in the DataFrame")
    mask = outlier_mask(df[col])  # ValueError on non-numeric propagates
    n_excluded = int(mask.sum())
    if n_excluded == 0:
        return df, 0
    kept = df[~mask]
    if len(kept) < 5:
        return df, 0
    kept_vals = kept[col].astype(float)
    span = float(kept_vals.max() - kept_vals.min())
    if span <= 0:
        span = abs(float(kept_vals.median())) or 1.0
    drp = df.loc[mask, col].astype(float).dropna()
    hi_gap = (float(drp.max()) - float(kept_vals.max())) if len(drp) else 0.0
    lo_gap = (float(kept_vals.min()) - float(drp.min())) if len(drp) else 0.0
    drop_hi = hi_gap > 3 * span
    drop_lo = lo_gap > 3 * span
    if not (drop_hi or drop_lo):
        return df, 0
    # One-sided: only the side whose gap fired loses its flagged rows. A side
    # is decided against the median of the column's non-null values.
    values = df[col].astype(float).to_numpy()
    flagged = mask.to_numpy(dtype=bool)
    median = float(pd.Series(values).dropna().median())
    drop = np.zeros(len(values), dtype=bool)
    if drop_hi:
        drop |= flagged & (values > median)
    if drop_lo:
        drop |= flagged & (values < median)
    n_dropped = int(drop.sum())
    if n_dropped == 0:
        return df, 0
    return df[~drop].copy(), n_dropped
