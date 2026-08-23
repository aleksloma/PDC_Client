"""Deterministic post-execution result inspection — the client-side backstop
for the planner's flat-result / wrong-grain rules.

CRITICAL RULE 7 tells the model to check its own aggregation for a constant
metric or identical series and to state the finding in the answer. Verified
live (cv_892c97edf1340e4b and a fresh repro): it complies only sometimes — the
same prompt-adherence problem `outlier_utils` was extracted for. So the client
checks the EXECUTED result itself, in pure pandas, and makes the explanation
mandatory: the detected facts are sent to `/v1/describe` as a data caveat, and
if the model's description does not carry the caveat marker the client prepends
its own localized sentence. The note can never be lost.

Detection is purely STRUCTURAL — no column names, no domain words, no
thresholds tied to any dataset. Three shapes fire:
  * constant_metric  — every numeric series plotted is effectively constant
  * identical_series — a multi-series chart where every series is the same
  * constant_table   — every value (numeric) column of a table is constant
Conservative by construction: anything that varies, anything too small to
judge, and any unavailable/odd-shaped input yields no detection (keep the
answer as-is). Never raises (Article IV).

Leaf module (outlier_utils discipline): pandas + logger_utils only — never
run_chat_local / brain_client / local_store.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from logger_utils import log_with_sid

# Same relative tolerance as run_chat_local._DEGENERATE_REL_TOL (the Prompt-12
# degenerate-chart skip). Deliberately re-declared instead of imported: a leaf
# module must not import run_chat_local. Keeping the two equal matters — the
# skip always KEEPS the sole/last flat chart, and that survivor is exactly what
# this backstop must caveat, so this net may never be narrower than that one.
_REL_TOL = 1e-6

# The token the brain is instructed to start a caveat-carrying description with.
# Presence proves the model complied; absence triggers the client fallback.
DATA_NOTE_MARKER = "[[DATA_NOTE]]"

_VALUE_MAXLEN = 40      # constant-value hints crossing to the brain
_MAX_FACTS = 6
_ROUND_SIG = 4          # significant figures for series comparison

# A matrix whose smaller dimension holds at most this many values is a grouped
# bar waiting to happen — same threshold as the planner's grouped-bar default
# (colour dimension <= 6). Above it, a matrix is a legitimate choice.
MATRIX_SMALL_DIM_MAX = 6

# Deterministic fallback sentences. Server-side i18n does not exist in this
# client (schema_builder._detect_language is the only language signal), so
# these are literals for the three languages that detector supports.
_FALLBACK = {
    "ka": {
        "generic": "შენიშვნა: ამ მონაცემებში მაჩვენებელი ყველა ჯგუფისთვის ერთნაირია, "
                   "ამიტომ შედეგი განსხვავებებს ვერ აჩვენებს.",
        "catalog": "შენიშვნა: ჩატვირთული მონაცემები კატალოგის/კავშირების ინფორმაციაა "
                   "(რა არსებობს), და არა გაზომილი რაოდენობები — ამიტომ ყველა ჯგუფს "
                   "ერთი და იგივე მნიშვნელობა აქვს.",
        "matrix": "ეს რიცხვების ცხრილი (სითბური რუკა) ძნელად საკითხავია — "
                  "დაჯგუფებული სვეტოვანი დიაგრამა ამ მონაცემებს უკეთ აჩვენებდა.",
    },
    "ru": {
        "generic": "Примечание: в этих данных показатель одинаков для всех групп, "
                   "поэтому результат не показывает различий.",
        "catalog": "Примечание: загруженные данные — это справочная информация о связях "
                   "(что существует), а не измеренные количества, поэтому у всех групп "
                   "одно и то же значение.",
        "matrix": "Эту таблицу чисел (тепловую карту) трудно читать — "
                  "сгруппированная столбчатая диаграмма показала бы эти данные лучше.",
    },
    "en": {
        "generic": "Note: in this data the metric is the same for every group, so the "
                   "result cannot show differences.",
        "catalog": "Note: the loaded data contains catalog/link information (what exists), "
                   "not measured quantities — that is why every group has the same value.",
        "matrix": "This grid of numbers (a heatmap) is hard to read — a grouped bar "
                  "chart would show this data better.",
    },
}


# ── primitives ───────────────────────────────────────────────────────────────

def _clean(vals) -> list:
    """Non-null values of an iterable (NaN/NaT/None dropped)."""
    out = []
    for v in vals:
        if v is None:
            continue
        try:
            if pd.isna(v):
                continue
        except (TypeError, ValueError):
            pass
        out.append(v)
    return out


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def values_are_constant(vals) -> bool:
    """True when >=2 non-null values are all effectively the same.

    Numeric values use the RELATIVE-spread test (identical to the degenerate
    chart check) so float jitter counts as constant; anything else compares
    stringified values. Fewer than 2 non-null values -> False (not enough
    evidence — never claim a finding from a single point)."""
    vals = _clean(vals)
    if len(vals) < 2:
        return False
    if all(_is_num(v) for v in vals):
        nums = [float(v) for v in vals]
        lo, hi = min(nums), max(nums)
        spread = hi - lo
        scale = max(abs(hi), abs(lo))
        rel = (spread / scale) if scale > 0 else spread
        return rel <= _REL_TOL
    return len({str(v) for v in vals}) <= 1


def _fmt_value(v) -> str:
    try:
        if _is_num(v) and float(v) == int(float(v)):
            v = int(float(v))
    except (TypeError, ValueError, OverflowError):
        pass
    return str(v)[:_VALUE_MAXLEN]


def _round_sig(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f == 0:
        return 0.0
    from math import floor, log10
    try:
        return round(f, -int(floor(log10(abs(f)))) + (_ROUND_SIG - 1))
    except (ValueError, OverflowError):
        return None


# ── chart inspection (works off chart_data — the figure never escapes) ───────

def _row_sets(chart_data) -> list:
    """The chart's row lists: one per subplot table, or the single flat list.
    Mirrors the dispatch in run_chat_local._chart_is_degenerate."""
    tables = chart_data.get("tables")
    if isinstance(tables, list) and tables:
        return [t.get("rows") or [] for t in tables if isinstance(t, dict)]
    return [chart_data.get("rows") or []]


def _numeric_columns(rows: list) -> list:
    """Columns that are a numeric SERIES: >=2 non-null values, all numeric."""
    if not rows or not isinstance(rows[0], dict):
        return []
    out = []
    for col in list(rows[0].keys()):
        non_null = _clean(r.get(col) for r in rows if isinstance(r, dict))
        nums = [v for v in non_null if _is_num(v)]
        if len(nums) >= 2 and len(nums) == len(non_null):
            out.append(col)
    return out


def _constant_metric_facts(rows: list) -> Optional[list]:
    """Facts when EVERY numeric series in these rows is constant, else None."""
    cols = _numeric_columns(rows)
    if not cols:
        return None
    facts = []
    for col in cols:
        vals = [r.get(col) for r in rows if isinstance(r, dict)]
        if not values_are_constant(vals):
            return None
        const = _clean(vals)[0]
        facts.append(f"{col} is the same for every group in the chart "
                     f"(= {_fmt_value(const)})")
    return facts


def _series_column(rows: list, num_cols: set) -> Optional[str]:
    """Which column separates the chart's series.

    `Series` is what plot_utils adds for multi-trace charts (a grouped bar or a
    multi-line chart). A MATRIX chart (heatmap) is a single trace with two label
    dimensions instead, so the series dimension is the label column with fewer
    distinct values — pivoting on it is the same comparison the planner prompt
    asks generated code to make (`wide.T.drop_duplicates()`)."""
    first = rows[0]
    if "Series" in first:
        return "Series"
    labels = [c for c in first.keys() if c not in num_cols]
    if len(labels) != 2:
        return None
    counts = {c: len({str(r.get(c)) for r in rows if isinstance(r, dict)})
              for c in labels}
    series_col = min(labels, key=lambda c: counts[c])
    other = [c for c in labels if c != series_col][0]
    # Need a real grid: >=2 series compared over >=2 shared keys.
    if counts[series_col] < 2 or counts[other] < 2:
        return None
    return series_col


def _identical_series_facts(rows: list) -> Optional[list]:
    """Facts when the chart has >=2 series and every series carries the same
    values for the same labels (identical lines that overlap, or a matrix whose
    every column repeats the same numbers)."""
    if not rows or not isinstance(rows[0], dict):
        return None
    num_cols = set(_numeric_columns(rows))
    if not num_cols:
        return None
    series_col = _series_column(rows, num_cols)
    if not series_col:
        return None
    per_series: dict = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = str(r.get(series_col))
        label = tuple(str(v) for k, v in r.items()
                      if k != series_col and k not in num_cols)
        vals = tuple(_round_sig(r.get(c)) for c in sorted(num_cols))
        per_series.setdefault(name, {})[label] = vals
    if len(per_series) < 2:
        return None
    maps = list(per_series.values())
    if not all(maps):
        return None
    first = maps[0]
    if any(m != first for m in maps[1:]):
        return None
    what = "series" if series_col == "Series" else str(series_col)
    return [f"all {len(per_series)} {what} values in the chart carry identical "
            f"numbers — every one of them repeats the same values"]


def _matrix_shape(rows: list) -> Optional[tuple]:
    """(series_col, other_col, n_series, n_other) when these rows describe a
    MATRIX chart: no `Series` column (that marks a multi-trace chart), exactly
    two label dimensions and exactly one numeric column — the signature
    plot_utils emits for px.imshow / go.Heatmap. None otherwise."""
    if not rows or not isinstance(rows[0], dict):
        return None
    if "Series" in rows[0]:
        return None
    num_cols = _numeric_columns(rows)
    if len(num_cols) != 1:
        return None
    labels = [c for c in rows[0].keys() if c not in set(num_cols)]
    if len(labels) != 2:
        return None
    counts = {c: len({str(r.get(c)) for r in rows if isinstance(r, dict)})
              for c in labels}
    series_col = min(labels, key=lambda c: counts[c])
    other = [c for c in labels if c != series_col][0]
    if counts[series_col] < 2 or counts[other] < 2:
        return None
    return series_col, other, counts[series_col], counts[other]


def _is_count_like(rows: list, num_col: str) -> bool:
    """True when the matrix cells hold COUNTS: whole numbers, none negative.
    A measured quantity (revenue, averages) is not second-guessed."""
    vals = _clean(r.get(num_col) for r in rows if isinstance(r, dict))
    if len(vals) < 2 or not all(_is_num(v) for v in vals):
        return False
    return all(float(v) >= 0 and float(v) == int(float(v)) for v in vals)


def matrix_readability(chart_data) -> Optional[dict]:
    """A number-grid matrix small enough to read as a GROUPED BAR.

    Verified live: a 13 city x 5 product count matrix rendered as px.imshow
    with the numbers printed in the cells. Nothing about that result is wrong —
    the encoding is. Fires only for a real grid of COUNTS whose smaller
    dimension is at most MATRIX_SMALL_DIM_MAX values, which is exactly the
    shape the planner's grouped-bar default covers."""
    if not isinstance(chart_data, dict):
        return None
    for rows in _row_sets(chart_data):
        if not rows:
            continue
        shape = _matrix_shape(rows)
        if not shape:
            continue
        series_col, other, n_series, n_other = shape
        if n_series > MATRIX_SMALL_DIM_MAX:
            continue
        num_col = _numeric_columns(rows)[0]
        if not _is_count_like(rows, num_col):
            continue
        return {
            "kind": "matrix_readability",
            "matrix": True,
            "facts": [f"the chart is a {n_other}x{n_series} grid of {num_col} "
                      f"numbers; with only {n_series} {series_col} values a "
                      f"grouped bar (one bar per {series_col}, grouped by "
                      f"{other}) reads far better than a number grid"],
        }
    return None


def inspect_chart(chart_data) -> Optional[dict]:
    """Constant-metric / identical-series / matrix-readability detection on a
    rendered chart's data. None when the chart varies, is readably encoded, has
    no numeric series, or the data is unavailable.

    A flat finding always wins — it is the more important thing to tell the
    user — but when the flat chart is ALSO an unreadable matrix, the readable
    alternative rides along on the same caveat."""
    if not isinstance(chart_data, dict):
        return None
    row_sets = [rows for rows in _row_sets(chart_data) if rows]
    if not row_sets:
        return None
    readable = matrix_readability(chart_data)

    def _with_matrix(det: dict) -> dict:
        if readable:
            det["facts"] = (det["facts"] + readable["facts"])[:_MAX_FACTS]
            det["matrix"] = True
        return det

    const_facts: list = []
    for rows in row_sets:
        facts = _constant_metric_facts(rows)
        if facts is None:
            const_facts = []
            break
        const_facts.extend(facts)
    if const_facts:
        return _with_matrix({"kind": "constant_metric",
                             "facts": const_facts[:_MAX_FACTS]})
    for rows in row_sets:
        facts = _identical_series_facts(rows)
        if facts:
            return _with_matrix({"kind": "identical_series", "facts": facts})
    return readable


# ── table inspection (works off the real pandas RESULT object) ───────────────

def _as_frame(result_obj) -> Optional[pd.DataFrame]:
    if result_obj is None:
        return None
    if hasattr(result_obj, "data") and isinstance(getattr(result_obj, "data"), pd.DataFrame):
        return result_obj.data          # Styler
    if isinstance(result_obj, pd.DataFrame):
        return result_obj
    if isinstance(result_obj, pd.Series):
        return result_obj.reset_index()
    return None


def _value_columns(df: pd.DataFrame) -> list:
    """Measure columns. Identifiers/labels (text, dates, booleans) are ignored —
    a constant label column says nothing about the metric."""
    out = []
    for col in df.columns:
        s = df[col]
        if isinstance(s, pd.DataFrame):       # duplicate column names
            continue
        try:
            if pd.api.types.is_bool_dtype(s) or pd.api.types.is_datetime64_any_dtype(s):
                continue
            if pd.api.types.is_numeric_dtype(s):
                out.append(col)
        except (TypeError, ValueError):
            continue
    return out


def _constant_columns_facts(df: pd.DataFrame, cols: list) -> Optional[list]:
    """Facts when EVERY value column is constant DOWN its rows, else None."""
    facts = []
    for col in cols:
        vals = list(df[col])
        if not values_are_constant(vals):
            return None
        const = _clean(vals)[0]
        facts.append(f"{col} is the same in every row of the table "
                     f"(= {_fmt_value(const)})")
    return facts or None


def _identical_columns_facts(df: pd.DataFrame, cols: list) -> Optional[list]:
    """Facts when >=2 value columns are identical TO EACH OTHER — the values
    vary by row, but every column repeats the same series.

    This is the wide twin of the identical-series chart case: a city x product
    table where each product column carries the same per-city number says
    nothing about products, even though nothing in it is constant."""
    if len(cols) < 2:
        return None
    signatures = []
    for col in cols:
        vals = list(df[col])
        if not _clean(vals):
            return None                      # an all-null column proves nothing
        signatures.append(tuple(
            None if v is None or (_is_num(v) and pd.isna(v)) else _round_sig(v)
            if _is_num(v) else str(v)
            for v in vals))
    first = signatures[0]
    if any(s != first for s in signatures[1:]):
        return None
    return [f"all {len(cols)} value columns in the table ({', '.join(str(c) for c in cols[:5])}"
            f"{'…' if len(cols) > 5 else ''}) carry identical numbers — every "
            f"column repeats the same values row by row"]


def inspect_table(result_obj) -> Optional[dict]:
    """Flat-result detection on an executed table result.

    Two shapes fire: every value column CONSTANT down its rows
    (`constant_table`), or >=2 value columns IDENTICAL to each other while
    varying by row (`identical_series` — the same finding a multi-series chart
    would report, so it takes the same caveat path). A table with any genuinely
    varying, distinct measure is informative and must not be branded flat. A
    dict of tables fires only when every member fires (one caveat, one answer).
    """
    if isinstance(result_obj, dict):
        members = [v for v in result_obj.values() if _as_frame(v) is not None]
        if not members:
            return None
        facts: list = []
        kinds = set()
        for m in members:
            det = inspect_table(m)
            if det is None:
                return None
            kinds.add(det["kind"])
            facts.extend(det["facts"])
        # Mixed findings across members: report the weaker, always-true one.
        kind = kinds.pop() if len(kinds) == 1 else "constant_table"
        return {"kind": kind, "facts": facts[:_MAX_FACTS]}

    df = _as_frame(result_obj)
    if df is None or len(df) < 2:
        return None
    cols = _value_columns(df)
    if not cols:
        return None
    facts = _constant_columns_facts(df, cols)
    if facts:
        return {"kind": "constant_table", "facts": facts[:_MAX_FACTS]}
    facts = _identical_columns_facts(df, cols)
    if facts:
        return {"kind": "identical_series", "facts": facts[:_MAX_FACTS]}
    return None


# ── public entry ─────────────────────────────────────────────────────────────

def inspect_outputs(*, chart_data=None, chart_data_list=None, result_obj=None,
                    sid: str = "backstop") -> Optional[dict]:
    """Inspect one answer's executed outputs. Returns
    {"kind", "facts": [str]} when a flat/degenerate result is detected, else
    None. Any inspection error is logged and skipped — a working answer is
    never broken by this check (Article IV).

    `chart_data_list` (a split multi-chart group emitted under ONE description)
    fires only when every chart in the group fires.
    """
    try:
        if chart_data is not None:
            det = inspect_chart(chart_data)
            if det:
                return det
        if chart_data_list:
            dets = [inspect_chart(cd) for cd in chart_data_list]
            if dets and all(dets):
                return dets[0]
        if result_obj is not None:
            det = inspect_table(result_obj)
            if det:
                return det
        return None
    except Exception as e:                                   # noqa: BLE001
        log_with_sid(sid, "warning", f"RESULT_BACKSTOP_SKIP: {e}")
        return None


def fallback_sentence(lang: str, *, catalog: bool, matrix: bool = False,
                      flat: bool = True) -> str:
    """The client's own explanation, used when the model's description does not
    carry the marker. `lang` is a schema_builder._detect_language code.

    `matrix` appends the readable-alternative clause; `flat=False` (a chart that
    is only badly encoded, not flat) returns that clause alone."""
    table = _FALLBACK.get(lang) or _FALLBACK["en"]
    parts = []
    if flat:
        parts.append(table["catalog" if catalog else "generic"])
    if matrix:
        parts.append(table["matrix"])
    return " ".join(parts) if parts else table["generic"]


def strip_marker(text: str) -> str:
    """Remove every marker occurrence (and the whitespace it leaves)."""
    return (text or "").replace(DATA_NOTE_MARKER, "").strip()
