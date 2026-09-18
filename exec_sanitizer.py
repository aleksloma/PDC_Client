"""exec_sanitizer.py — pre-execution dataframe sanitize gate (Article XIII).

Dataframes handed to the execution sandbox must behave like plain, standard
pandas: strings as object, dates as datetime64, ordinary numerics. Generated
code cannot be trusted to handle category/sparse/extension dtypes (the demo
3D-chart bug: a categorical dimension column made ``groupby`` — pandas < 3.0
defaults to ``observed=False`` — emit the full cartesian product of ALL
categories, putting every city/product on the chart axes). This gate is the
in-code enforcement of that rule: ``sanitize_for_execution`` runs inside BOTH
exec sites (``code_exec.safe_execute`` and ``plot_utils.render_plot_safe``,
the same pair that installs ``sandbox_guard.SANDBOX_BUILTINS``), so every
execution path — chat, retries, refresh, dashboards, Auto Analytics, full
table re-execution — passes through it.

Leaf module: imports only pandas/numpy/warnings + ``logger_utils`` — it must
never import ``code_exec``/``plot_utils``/``local_store`` (import cycles). The
one exception is the log-escaping helper ``exec_transport.log_safe_text``,
which ``_log_safe`` imports INSIDE the call because ``exec_transport`` imports
this module at its own module scope.

Copy discipline (pandas 2.2.x, Copy-on-Write not enabled): the caller's dfs
dict may be shared across a multi-plot worklist and across parallel Auto
Analytics threads, so this module NEVER mutates the caller's frames. When a
frame needs conversion it works on ``df.copy(deep=False)`` and replaces whole
columns positionally via ``isetitem`` (rebinds a block in the copy only;
duplicate-label safe). Never add ``.loc``/``.iloc``/``inplace=`` writes here —
they would write through the shallow copy into the caller's frame. When
nothing needs converting, the SAME dict and frames are returned (no copies).
"""
import warnings

import numpy as np
import pandas as pd

from logger_utils import log_with_sid


def _log_safe(value, max_chars: int = 200) -> str:
    """ONE escaped, capped log field for an untrusted string.

    Every identifier this module puts on a log line — a df key, a column
    label, a dtype repr, a library exception raised ABOUT a customer frame —
    comes from data, and this gate also runs in the WEB process, where a
    quoted CSV/Excel header may legally contain a newline. The durable log is
    newline-delimited, so one embedded newline forges a complete extra record
    in the file operators grep.

    The implementation is the shared `exec_transport.log_safe_text`, imported
    LAZILY: `exec_transport` imports THIS module at module scope, so a
    module-level import here would be an import cycle. Never raises
    (Article IV) — a field that cannot be rendered costs its own text, never
    the caller's log line.
    """
    try:
        from exec_transport import log_safe_text
        return log_safe_text(value if isinstance(value, str) else str(value),
                             max_chars)
    except Exception:
        return ""


def _is_standard_dtype(dt) -> bool:
    """Standard = any numpy dtype (int*/uint*/float*/bool/object/datetime64/
    timedelta64) plus tz-aware datetimes. DatetimeTZDtype is technically an
    extension dtype but behaves like plain datetime for generated code
    (.dt, groupby, plotly all work); converting it would drop the timezone."""
    return isinstance(dt, np.dtype) or isinstance(dt, pd.DatetimeTZDtype)


def _categories_look_like_dates(cats) -> bool:
    """True only when EVERY category is a string that contains a digit AND a
    date separator ('-' or '/') AND parses via pd.to_datetime. The separator
    requirement is load-bearing: bare "2021" / "12345" / "seg1" must never be
    coerced to dates. Runs on the (small, unique) categories index only."""
    try:
        if len(cats) == 0 or cats.inferred_type not in ("string", "unicode"):
            return False
        if not all(isinstance(c, str) and ("-" in c or "/" in c)
                   and any(ch.isdigit() for ch in c) for c in cats):
            return False
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            parsed = pd.to_datetime(cats, errors="coerce")
        return bool(parsed.notna().all())
    except Exception:
        return False


def _convert_series(s: pd.Series):
    """Return a converted standard-dtype Series, or None if `s` is already
    standard. Raises on unexpected failure — the caller catches per column."""
    dt = s.dtype
    if _is_standard_dtype(dt):
        return None

    if isinstance(dt, pd.CategoricalDtype):
        cats = dt.categories
        cdt = cats.dtype
        if isinstance(cdt, np.dtype) and cdt.kind in "mM":
            return s.astype(cdt)                     # datetime/timedelta cats
        if isinstance(cdt, pd.DatetimeTZDtype):
            return s.astype(cdt)
        if getattr(cdt, "kind", "?") in "iuf":
            try:
                return s.astype(cdt)
            except Exception:
                return s.astype("float64")           # int cats + NaN codes
        if _categories_look_like_dates(cats):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                return (s.cat.rename_categories(pd.to_datetime(cats))
                        .astype("datetime64[ns]"))
        return s.astype(object)

    if isinstance(dt, pd.SparseDtype):
        return s.sparse.to_dense()

    if isinstance(dt, (pd.PeriodDtype, pd.IntervalDtype)):
        return s.astype(str)

    # Generic extension dtypes: pandas nullable (Int64/Float64/boolean/
    # string[*]) and pyarrow-backed (ArrowDtype), matched by kind — never by
    # dtype name — so future backends are covered too.
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.astype("datetime64[ns]")            # Arrow timestamps
    if pd.api.types.is_integer_dtype(s):
        if s.isna().any():
            return s.astype("float64")               # pd.NA -> NaN
        return s.astype("int64")
    if pd.api.types.is_float_dtype(s):
        return s.astype("float64")
    if pd.api.types.is_bool_dtype(s):
        if s.isna().any():
            return pd.Series(s.to_numpy(dtype=object, na_value=np.nan),
                             index=s.index, name=s.name)
        return s.astype("bool")
    # Strings and anything else -> object with np.nan missing values (what
    # read_csv produces; generated code expects NaN semantics, not pd.NA).
    return pd.Series(s.to_numpy(dtype=object, na_value=np.nan),
                     index=s.index, name=s.name)


def _sanitize_frame(df: pd.DataFrame, key, sid: str) -> pd.DataFrame:
    """Return `df` itself when clean (fast path), else a shallow copy with
    the offending columns replaced. Never raises past a column."""
    bad = [i for i, dt in enumerate(df.dtypes) if not _is_standard_dtype(dt)]
    bad_index = (not isinstance(df.index, pd.MultiIndex)
                 and isinstance(df.index.dtype, pd.CategoricalDtype))
    if not bad and not bad_index:
        return df

    new_df = df.copy(deep=False)
    converted = []
    for i in bad:
        col = df.columns[i]
        dt = df.dtypes.iloc[i]
        try:
            conv = _convert_series(new_df.iloc[:, i])
            if conv is not None:
                new_df.isetitem(i, conv)
                converted.append(f"{col}:{dt}->{conv.dtype}")
        except Exception as e:
            log_with_sid(sid, "warning",
                         f"EXEC_SANITIZE_SKIP df={_log_safe(key, 120)} "
                         f"col={_log_safe(col, 120)} dtype={_log_safe(dt, 80)}: "
                         f"{_log_safe(e)}")
    if bad_index:
        try:
            new_df.index = df.index.astype(df.index.dtype.categories.dtype)
        except Exception as e:
            log_with_sid(sid, "warning",
                         f"EXEC_SANITIZE_SKIP df={_log_safe(key, 120)} index "
                         f"dtype={_log_safe(df.index.dtype, 80)}: {_log_safe(e)}")
    if converted:
        log_with_sid(sid, "info",
                     f"EXEC_SANITIZE df={_log_safe(key, 120)} "
                     f"converted=[{_log_safe(', '.join(converted), 800)}]")
    return new_df


def sanitize_for_execution(dfs: dict, sid: str) -> dict:
    """The gate. Returns the SAME dict object when every frame is already
    standard (near-free: dtype-metadata scan only); otherwise a shallow dict
    copy where only the offending frames are (shallow) copies with converted
    columns. Tolerates empty dicts and non-DataFrame values; never raises
    (Article IV) — any unexpected failure logs and returns the input as-is."""
    try:
        if not isinstance(dfs, dict) or not dfs:
            return dfs
        out = None
        for key, df in dfs.items():
            if not isinstance(df, pd.DataFrame):
                continue
            new_df = _sanitize_frame(df, key, sid)
            if new_df is not df:
                if out is None:
                    out = dict(dfs)
                out[key] = new_df
        return out if out is not None else dfs
    except Exception as e:
        try:
            log_with_sid(sid, "warning", f"EXEC_SANITIZE_FAILED: {_log_safe(e)}")
        except Exception:
            pass
        return dfs
